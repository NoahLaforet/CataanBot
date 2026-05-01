"""End-to-end bridge tests on real WS captures.

These exercise the full ingest pipeline:

    capture frames → game.feed → _feed_ws_payload bookkeeping →
    _build_advisor_snapshot → render_postmortem_html

So a regression in any one stage shows up as a failed assertion here.

Each test skips cleanly when the captures aren't on disk — captures
live under `ws_captures/` which is gitignored, so CI / fresh clones
won't have them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

CAPTURE_EARLY = (Path(__file__).parent.parent
                 / "ws_captures"
                 / "cataanbot-ws-fort4092-early-2026-04-21T23-23-22.json")
CAPTURE_MIDGAME = (Path(__file__).parent.parent
                   / "ws_captures"
                   / "cataanbot-ws-fort4092-midgame-2026-04-21T23-34-04.json")


def _iter_payloads(path: Path):
    from cataanbot.colonist_proto import load_capture
    for frame in load_capture(path):
        if frame.error:
            continue
        p = frame.payload
        if isinstance(p, dict):
            yield p


def _make_st(game):
    """Minimal bridge-state dict matching what _build_app constructs.

    Mirrors the shape inside bridge._build_app. Tests can poke at the
    fields they care about and let the bridge helpers initialize the
    rest as they would in production."""
    from cataanbot.live import ColorMap
    from cataanbot.tracker import Tracker
    return {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
        "pm_events": [], "pm_results": [], "pm_timestamps": [],
        "pm_dir": None, "pm_written": False,
        "pending_trade_offer": None,
        "dev_cards_held": 0, "dev_cards_bought_this_turn": 0,
    }


def test_full_capture_replay_does_not_crash_advisor():
    """Feeding every frame of a real capture through LiveGame and then
    asking for an advisor snapshot must not raise. This is the lowest-
    bar regression test — anything that throws (key error, attribute
    error on opp state, etc.) shows up as a real-world bridge crash."""
    if not CAPTURE_MIDGAME.exists():
        pytest.skip("midgame capture not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live_game import LiveGame

    game = LiveGame()
    for payload in _iter_payloads(CAPTURE_MIDGAME):
        game.feed(payload)
    st = _make_st(game)

    snap = _build_advisor_snapshot(st)
    assert snap["game_started"]
    # Self block must always come back when the capture booted; opps
    # may legitimately be empty pre-setup but our midgame capture is
    # well past setup.
    assert snap.get("self") is not None
    assert isinstance(snap.get("opps"), list)


def test_full_capture_drives_robber_snapshot_without_failure():
    """The robber snapshot helper has historically been the most
    fragile path because it depends on every opp having coherent
    session metadata. Feeding a real capture catches surprises in
    color_map / hand_card_counts / vp state alignment."""
    if not CAPTURE_MIDGAME.exists():
        pytest.skip("midgame capture not present")
    from cataanbot.bridge_robber import _compute_robber_snapshot
    from cataanbot.live_game import LiveGame

    game = LiveGame()
    for payload in _iter_payloads(CAPTURE_MIDGAME):
        game.feed(payload)

    snap = _compute_robber_snapshot(game)
    # Either we get a snapshot (live game, real session) or None
    # (snapshot deemed not useful) — both are valid; the test is that
    # this call doesn't throw.
    assert snap is None or isinstance(snap, list)
    if snap:
        for tile in snap:
            assert "coord" in tile and "score" in tile


def test_postmortem_renders_with_enrichment_on_real_capture(tmp_path):
    """End-to-end postmortem render: feed a real capture through the
    DOM-log-style postmortem feed, then write the HTML and check the
    new enrichment fields landed.

    Uses synthetic DOM-log payloads built from the WS-derived events so
    we don't need a parallel /log capture — the WS-derived events fall
    out the other side identically once they're in the pm pipeline.
    """
    if not CAPTURE_MIDGAME.exists():
        pytest.skip("midgame capture not present")
    from cataanbot.bridge_postmortem import _compute_board_fingerprint
    from cataanbot.events import (
        DevCardBuyEvent, DevCardPlayEvent, GameOverEvent, RollEvent,
    )
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.postmortem import render_postmortem_html
    from cataanbot.report import build_report
    from cataanbot.tracker import Tracker

    game = LiveGame()
    for payload in _iter_payloads(CAPTURE_MIDGAME):
        game.feed(payload)

    # Synthesize a tiny event stream — enough to trigger every new
    # report section. We're testing the enrichment plumbing, not the
    # WS→event extraction (which other tests cover).
    events = [
        RollEvent(player="Alice", d1=2, d2=3),
        DevCardBuyEvent(player="Alice"),
        RollEvent(player="Alice", d1=3, d2=4),
        DevCardPlayEvent(player="Alice", card="knight"),
        GameOverEvent(winner="Alice"),
    ]
    cm = ColorMap({"Alice": "RED"})
    fp = _compute_board_fingerprint(game)

    out_path = tmp_path / "pm.html"
    render_postmortem_html(
        events=events,
        dispatch_results=[],
        timestamps=[1000.0 + i for i in range(len(events))],
        color_map=cm,
        final_vp={"RED": 10},
        out_path=out_path,
        board_fingerprint=fp,
    )
    assert out_path.exists()
    html = out_path.read_text()

    # Fingerprint surfaced in the rendered text (we know it's a classic
    # 19-tile capture, so label="classic" and tiles=19).
    assert fp is not None
    assert fp.get("tile_count") == 19
    assert "Board: classic" in html
    assert "19 tiles" in html

    # Dev card timeline is in the rendered output.
    assert "Dev card timeline" in html
    assert "knight" in html

    # Sanity-check the underlying ReplayReport too — easier to debug
    # if the html assertion above ever fails.
    rep = build_report(
        events, [], cm, final_vp={"RED": 10},
        timestamps=[1000.0 + i for i in range(len(events))],
        board_fingerprint=fp,
    )
    assert len(rep.dev_card_timeline) == 2
    assert rep.board_fingerprint["tile_count"] == 19


def test_full_capture_advisor_snapshot_idempotent():
    """Calling _build_advisor_snapshot twice in a row on a stable game
    state must produce identical opp/self structures (sequence number
    aside). Catches accidental in-place mutation of game state during
    snapshot building — a class of bug that's invisible in unit tests
    but drifts the HUD between polls."""
    if not CAPTURE_MIDGAME.exists():
        pytest.skip("midgame capture not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live_game import LiveGame

    game = LiveGame()
    for payload in _iter_payloads(CAPTURE_MIDGAME):
        game.feed(payload)
    st = _make_st(game)

    snap1 = _build_advisor_snapshot(st)
    snap2 = _build_advisor_snapshot(st)

    # seq can differ; everything else must match.
    snap1.pop("seq", None)
    snap2.pop("seq", None)
    # Compare the heaviest fields that are most prone to drift.
    assert snap1.get("self") == snap2.get("self")
    assert snap1.get("opps") == snap2.get("opps")
    assert snap1.get("production") == snap2.get("production")


def test_resolve_final_vp_prefers_colonist_session_over_pm_tracker():
    """The pm_tracker is fed only via DOM-log payloads, where
    BuildEvents arrive without coords and dispatch as 'unhandled'.
    That leaves pm_tracker frozen at opening (2 VP each) regardless
    of how the game played out — exactly what produced the 2/2 final
    score on Noah's BrickdDaddy game.

    _resolve_final_vp must prefer colonist's authoritative
    victoryPointsState. We mock a session reporting 12/14 and verify
    the resolver returns those, not the pm_tracker's stale 2/2.
    """
    from cataanbot.bridge_postmortem import _resolve_final_vp

    class _FakeColorMap:
        def __init__(self, mapping):
            self._m = mapping
        def get(self, username):
            return self._m.get(username)

    class _FakeSession:
        def __init__(self):
            self.player_names = {1: "Noah", 2: "Opp"}
            # Mirrors colonist's per-color state dict — non-empty means
            # we have a real read, not a placeholder.
            self.victory_points_state = {1: {0: 5, 1: 3}, 2: {0: 4, 1: 5}}
        def vp_total(self, cid):
            return {1: 12, 2: 14}[cid]

    class _FakeTracker:
        def vp_status(self):
            # The "stale 2/2" pm_tracker would return — make sure we
            # don't pick this when colonist's state is available.
            return {"per_color": {"RED": 2, "BLUE": 2}}

    class _FakeGame:
        session = _FakeSession()
        color_map = _FakeColorMap({"Noah": "RED", "Opp": "BLUE"})
        tracker = _FakeTracker()

    st = {"game": _FakeGame(), "pm_tracker": _FakeTracker()}
    out = _resolve_final_vp(st)
    assert out == {"RED": 12, "BLUE": 14}, (
        f"Expected colonist's authoritative VPs, got {out}")


def test_resolve_final_vp_falls_back_to_live_tracker_without_session():
    """If colonist's session never populated victoryPointsState (rare
    edge: bridge attached too late), use the live tracker. Better than
    falling all the way to pm_tracker, which we know is stale."""
    from cataanbot.bridge_postmortem import _resolve_final_vp

    class _FakeTrackerLive:
        def vp_status(self):
            return {"per_color": {"RED": 8, "BLUE": 7}}

    class _FakeTrackerStale:
        def vp_status(self):
            return {"per_color": {"RED": 2, "BLUE": 2}}

    class _FakeGame:
        session = None
        color_map = None
        tracker = _FakeTrackerLive()

    st = {"game": _FakeGame(), "pm_tracker": _FakeTrackerStale()}
    out = _resolve_final_vp(st)
    assert out == {"RED": 8, "BLUE": 7}, (
        f"Expected live tracker fallback, got {out}")
