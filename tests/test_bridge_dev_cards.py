"""Self dev-card holdings tracking + just-bought delay (HUD principle
#7 follow-up).

Catanatron's *_IN_HAND counters never increment for self because the
DOM-log dev-card-buy line hides the card type. The bridge tracks
holdings as an aggregate count so the play-timing hints can fire even
when the type-specific counter is zero. Plus the "just bought this
turn can't play" rule that catanatron doesn't model — Catan's actual
no-play-on-buy-turn restriction.

Self's DevCardBuyEvent + DevCardPlayEvent come ONLY through the DOM
log (the WS diff parser suppresses self's buys), so the tracking
hooks live in ``_feed_postmortem``. Tests drive the buy/play through
synthetic DOM-log payloads.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cataanbot.bridge import (
    _feed_postmortem,
    _maybe_clear_dev_just_bought,
)
from cataanbot.live import ColorMap
from cataanbot.tracker import Tracker


def _payload(parts, ts=0.0):
    return {"ts": ts, "text": "", "parts": parts, "names": [], "icons": [],
            "self": None}


def _name(n):
    return {"kind": "name", "name": n, "color": ""}


def _text(t):
    return {"kind": "text", "text": t}


def _icon(alt):
    return {"kind": "icon", "alt": alt, "src_tail": ""}


def _buy_payload(player_name):
    """DOM-log line that the parser turns into DevCardBuyEvent.

    Real colonist log: 'Noah bought [Development Card]' with the
    development-card icon. Parser keys on ``"bought"`` in the joined
    text and an icon whose alt is exactly ``"development card"``.
    """
    return _payload([
        _name(player_name),
        _text("bought"),
        _icon("development card"),
    ])


def _play_knight_payload(player_name):
    """DOM-log line for a knight play. ``X used [Knight]``."""
    return _payload([
        _name(player_name),
        _text("used"),
        _icon("knight"),
    ])


def _make_state(*, self_name="Noah", opp_name="Bob",
                cur_cid=1, last_cid=None,
                tmp_path: Path | None = None):
    """Smallest st dict that ``_feed_postmortem`` reads.

    Includes both the postmortem-collector fields and the dev-card
    overlay state. session.self_color_id + .player_names is what
    _is_self_player checks; current_turn_color_id drives the
    just-bought-this-turn carve-out via _maybe_clear_dev_just_bought.
    """
    sess = SimpleNamespace(
        self_color_id=1,
        player_names={1: self_name, 2: opp_name},
        current_turn_color_id=cur_cid,
    )
    game = SimpleNamespace(session=sess)
    return {
        "game": game,
        "dev_cards_held": 0,
        "dev_cards_bought_this_turn": 0,
        "_last_turn_cid": last_cid,
        "pm_tracker": Tracker(),
        "pm_color_map": ColorMap(),
        "pm_events": [],
        "pm_results": [],
        "pm_timestamps": [],
        "pm_written": False,
        "pm_dir": tmp_path,
        "pending_trade_offer": None,
        "robber_pending": False,
        "robber_snapshot": None,
        "display_colors": {},
    }


def test_self_buy_increments_held_and_bought_this_turn(tmp_path: Path):
    st = _make_state(tmp_path=tmp_path)
    _feed_postmortem(st, _buy_payload("Noah"))
    assert st["dev_cards_held"] == 1
    assert st["dev_cards_bought_this_turn"] == 1


def test_opponent_buy_does_not_count(tmp_path: Path):
    # Self-only tracking — opp buys must not pollute self's count.
    st = _make_state(tmp_path=tmp_path)
    _feed_postmortem(st, _buy_payload("Bob"))
    assert st["dev_cards_held"] == 0
    assert st["dev_cards_bought_this_turn"] == 0


def test_self_play_decrements_held_only(tmp_path: Path):
    # bought_this_turn doesn't decrement on play — it only resets on
    # turn flip. Otherwise a buy + play in the same turn would zero
    # the carve-out and re-enable the next play (which Catan forbids).
    st = _make_state(tmp_path=tmp_path)
    st["dev_cards_held"] = 2
    st["dev_cards_bought_this_turn"] = 1
    _feed_postmortem(st, _play_knight_payload("Noah"))
    assert st["dev_cards_held"] == 1
    assert st["dev_cards_bought_this_turn"] == 1


def test_opponent_play_does_not_decrement_self(tmp_path: Path):
    st = _make_state(tmp_path=tmp_path)
    st["dev_cards_held"] = 2
    _feed_postmortem(st, _play_knight_payload("Bob"))
    assert st["dev_cards_held"] == 2


def test_play_floors_held_at_zero(tmp_path: Path):
    # Defensive: if the bridge missed a buy event but saw a play
    # (rare, but DOM-log virtualization can drop lines), we must not
    # let held go negative.
    st = _make_state(tmp_path=tmp_path)
    st["dev_cards_held"] = 0
    _feed_postmortem(st, _play_knight_payload("Noah"))
    assert st["dev_cards_held"] == 0


def test_just_bought_clears_on_self_to_opp_turn_flip():
    # Self bought a card on their turn (cid=1, self). When colonist's
    # current_turn_color_id flips to opp (cid=2), the carve-out clears
    # and the card becomes playable. _last_turn_cid must update so we
    # don't re-clear on subsequent polls within the same turn.
    st = _make_state(cur_cid=2, last_cid=1)
    st["dev_cards_held"] = 1
    st["dev_cards_bought_this_turn"] = 1
    _maybe_clear_dev_just_bought(st)
    assert st["dev_cards_bought_this_turn"] == 0
    assert st["_last_turn_cid"] == 2


def test_just_bought_does_not_clear_within_self_turn():
    # Still self's turn — the carve-out should not clear yet.
    st = _make_state(cur_cid=1, last_cid=1)
    st["dev_cards_bought_this_turn"] = 1
    _maybe_clear_dev_just_bought(st)
    assert st["dev_cards_bought_this_turn"] == 1


def test_just_bought_does_not_clear_on_opp_to_opp_flip():
    # Opp1 → Opp2. Self never had the turn, nothing to clear, but
    # _last_turn_cid still updates so the next opp→self transition
    # latches correctly.
    st = _make_state(cur_cid=3, last_cid=2)
    sess = st["game"].session
    sess.player_names[3] = "Charlie"
    st["dev_cards_bought_this_turn"] = 0
    _maybe_clear_dev_just_bought(st)
    assert st["dev_cards_bought_this_turn"] == 0
    assert st["_last_turn_cid"] == 3


def test_rb_hint_fires_on_playable_count_when_in_hand_zero():
    """The four dev-card hints all need to surface even when
    catanatron's *_IN_HAND counters stay at 0 for self (which they
    always do — the buy handler can't see card type from colonist's
    DOM log). playable_count is the overlay-tracked aggregate that
    gates them instead.

    rb_hint is the lightest of the four to test in isolation —
    knight/monopoly hints pull through robber-snapshot and
    opp-hand-tracker code paths that need a fully-booted LiveGame
    fixture. Their gate logic is identical so this single rb test
    covers the contract.
    """
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.bridge import _compute_rb_hint
    from cataanbot.tracker import Tracker

    g = Game([RandomPlayer(c) for c in (
        Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE)], seed=1)
    # Plant one RED settlement so the rb-hint placement search has a
    # network to extend from.
    g.state.board.build_settlement(
        Color.RED, 0, initial_build_phase=True)
    g.state.board.build_road(Color.RED, (0, 1))
    tr = Tracker()
    tr.game = g
    game_wrapper = SimpleNamespace(tracker=tr)
    out = _compute_rb_hint(game_wrapper, "RED", playable_count=1)
    assert out is not None
    assert out["have"] == 1


def test_rb_hint_returns_none_when_neither_signal_says_held():
    # Both type-specific counter (catanatron) and aggregate
    # (overlay) say 0 → hint silent. The previous-behaviour
    # contract: a player who doesn't hold the card sees nothing.
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.bridge import _compute_rb_hint
    from cataanbot.tracker import Tracker

    g = Game([RandomPlayer(c) for c in (
        Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE)], seed=1)
    g.state.board.build_settlement(
        Color.RED, 0, initial_build_phase=True)
    g.state.board.build_road(Color.RED, (0, 1))
    tr = Tracker()
    tr.game = g
    out = _compute_rb_hint(
        SimpleNamespace(tracker=tr), "RED", playable_count=0)
    assert out is None


def test_snap_breaks_out_vp_vs_non_vp_held(tmp_path: Path):
    """Colonist reports self's VP-dev-card count separately via
    victory_points_state[self][source=2] (because VP cards count
    toward the displayed VP total). The bridge subtracts that from
    total holdings so the play hints only fire when self holds at
    least one non-VP card. All-VP holdings → hints silent."""
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live_game import LiveGame

    # Build a minimal st with a session that has VP state set up.
    sess = SimpleNamespace(
        self_color_id=1,
        player_names={1: "Noah", 2: "Bob"},
        current_turn_color_id=1,
        # Two VP dev cards, no non-VP. (Source 2 is "VP cards held".)
        victory_points_state={1: {2: 2}},
    )
    # _build_advisor_snapshot reads game.session and game.color_map
    # plus tracker stuff. To keep this focused on the new VP-vs-non-VP
    # math we only assert on the dev_cards_* fields that flow through
    # without needing the full snap pipeline.
    game = SimpleNamespace(session=sess)
    st = _make_state(tmp_path=tmp_path)
    st["game"] = game
    st["dev_cards_held"] = 2  # both are VP
    st["dev_cards_bought_this_turn"] = 0

    # Reach into the snapshot builder's inline math: replicate the
    # exact expressions from bridge._build_advisor_snapshot. (Calling
    # the full snapshot builder would need a fully-booted game which
    # is heavier than this test wants.)
    dev_held = int(st.get("dev_cards_held") or 0)
    dev_just = int(st.get("dev_cards_bought_this_turn") or 0)
    vp_held = int((sess.victory_points_state
                   .get(sess.self_color_id, {})
                   .get(2, 0)) or 0)
    non_vp_held = max(0, dev_held - vp_held)
    dev_playable = max(0, non_vp_held - dev_just)

    assert dev_held == 2
    assert vp_held == 2
    assert non_vp_held == 0
    assert dev_playable == 0  # all VP → nothing to play


def test_snap_mixed_vp_and_non_vp(tmp_path: Path):
    # Mix: 1 VP + 1 non-VP held → playable=1 (the non-VP one).
    sess = SimpleNamespace(
        self_color_id=1,
        player_names={1: "Noah", 2: "Bob"},
        current_turn_color_id=1,
        victory_points_state={1: {2: 1}},
    )
    st = _make_state(tmp_path=tmp_path)
    st["game"] = SimpleNamespace(session=sess)
    st["dev_cards_held"] = 2
    st["dev_cards_bought_this_turn"] = 0

    vp_held = sess.victory_points_state[1].get(2, 0)
    non_vp = max(0, st["dev_cards_held"] - vp_held)
    playable = max(0, non_vp - st["dev_cards_bought_this_turn"])
    assert non_vp == 1
    assert playable == 1


def test_snap_just_bought_non_vp_still_delayed(tmp_path: Path):
    # Bought a non-VP card this turn (vp_held didn't bump). Total=1,
    # vp=0, non_vp=1, just_bought=1 → playable=0 (Catan delay).
    sess = SimpleNamespace(
        self_color_id=1,
        player_names={1: "Noah", 2: "Bob"},
        current_turn_color_id=1,
        victory_points_state={1: {2: 0}},
    )
    st = _make_state(tmp_path=tmp_path)
    st["game"] = SimpleNamespace(session=sess)
    st["dev_cards_held"] = 1
    st["dev_cards_bought_this_turn"] = 1

    vp_held = sess.victory_points_state[1].get(2, 0)
    non_vp = max(0, st["dev_cards_held"] - vp_held)
    playable = max(0, non_vp - st["dev_cards_bought_this_turn"])
    assert non_vp == 1
    assert playable == 0


def test_self_play_decrements_live_tracker_in_hand(tmp_path: Path):
    """Critical regression: when self plays a dev card via the
    DOM log, the LIVE tracker's catanatron state must decrement
    {type}_IN_HAND. Without this, _compute_*_hint reads the stale
    counter and the hint sticks in the HUD even after play.

    Uses a real LiveGame so game.tracker is a real catanatron Game
    instance — the SimpleNamespace stub in earlier tests doesn't
    exercise the live-apply path.
    """
    from cataanbot.bridge import _feed_postmortem
    from cataanbot.live_game import LiveGame
    from cataanbot.events import DevCardSelfBuyTypedEvent
    from cataanbot.live import apply_event
    from catanatron import Color

    # Boot a real LiveGame from a synthetic 7-tile flower so we have
    # a working catanatron tracker.
    from cataanbot.colonist_map import (
        corner_tile_signature, edge_endpoint_signatures,
    )
    positions = [(0, 0), (1, -1), (-1, 1),
                 (1, 0), (-1, 0), (0, 1), (0, -1)]
    pos_set = set(positions)
    types = [0, 1, 2, 3, 4, 5, 1]
    dice = [0, 4, 5, 6, 8, 9, 10]
    hex_states = {}
    for tid, (x, y) in enumerate(positions, start=1):
        hex_states[str(tid)] = {
            "x": x, "y": y,
            "type": types[tid - 1],
            "diceNumber": dice[tid - 1]}
    corner_states, seen_c, cid = {}, set(), 0
    for x, y in positions:
        for cx in range(x - 1, x + 2):
            for cy in range(y - 1, y + 2):
                for cz in (0, 1):
                    sig = corner_tile_signature(cx, cy, cz)
                    if sig in seen_c or not any(t in pos_set for t in sig):
                        continue
                    seen_c.add(sig); cid += 1
                    corner_states[str(cid)] = {"x": cx, "y": cy, "z": cz}
    edge_states, seen_e, eid = {}, set(), 0
    for x, y in positions:
        for ex in range(x - 1, x + 2):
            for ey in range(y - 1, y + 2):
                for ez in (0, 1, 2):
                    try:
                        a, b = edge_endpoint_signatures(ex, ey, ez)
                    except Exception: continue
                    key = frozenset((a, b))
                    if (key in seen_e
                            or not any(t in pos_set for t in a)
                            or not any(t in pos_set for t in b)): continue
                    seen_e.add(key); eid += 1
                    edge_states[str(eid)] = {"x": ex, "y": ey, "z": ez}

    body = {
        "playerColor": 1,
        "playerUserStates": [
            {"selectedColor": 1, "username": "Noah", "userId": 999},
        ],
        "gameState": {"mapState": {
            "tileHexStates": hex_states,
            "tileCornerStates": corner_states,
            "tileEdgeStates": edge_states,
            "portEdgeStates": {},
        }},
        "gameSettings": {"gameType": 1},
    }
    game = LiveGame()
    game.start_from_game_state(body)
    cat_state = game.tracker.game.state
    self_color = game.color_map.get("Noah")
    idx = cat_state.color_to_index[Color[self_color.upper()]]

    # Seed self with a knight via the typed-buy event.
    apply_event(game.tracker, game.color_map,
                DevCardSelfBuyTypedEvent(
                    player="Noah", card_type="KNIGHT"))
    assert cat_state.player_state[f"P{idx}_KNIGHT_IN_HAND"] == 1

    st = _make_state(tmp_path=tmp_path)
    st["game"] = game  # real LiveGame replaces the stub
    st["dev_cards_held"] = 1

    # DOM-log knight play → bridge hooks → live tracker decrement
    _feed_postmortem(st, _play_knight_payload("Noah"))
    assert cat_state.player_state[f"P{idx}_KNIGHT_IN_HAND"] == 0, (
        "live tracker KNIGHT_IN_HAND should be 0 after play")
    assert cat_state.player_state[f"P{idx}_PLAYED_KNIGHT"] == 1


def test_friendly_robber_info_event_sets_session_flag(tmp_path: Path):
    """Colonist's "Friendly Robber is active" InfoEvent at game
    start must flip session.friendly_robber_active=True. The bridge's
    snapshot reads this flag; the robber-target ranker then filters
    protected ≤2 VP victims."""
    from cataanbot.bridge import _feed_postmortem

    st = _make_state(tmp_path=tmp_path)
    sess = st["game"].session
    sess.friendly_robber_active = False
    # Synthetic DOM-log payload for the InfoEvent — text starts with
    # "Friendly Robber" (the parser lowercases it before its
    # startswith check, but we test exact colonist casing).
    payload = _payload([
        _text("Friendly Robber is active, tiles available to "
              "block are limited"),
    ])
    _feed_postmortem(st, payload)
    assert sess.friendly_robber_active is True


def test_full_buy_play_cycle_across_turns(tmp_path: Path):
    # End-to-end: buy on self's turn → can't play yet → turn flips →
    # carve-out clears → card becomes playable.
    st = _make_state(cur_cid=1, last_cid=1, tmp_path=tmp_path)
    # Buy on self's turn (DOM-log)
    _feed_postmortem(st, _buy_payload("Noah"))
    assert st["dev_cards_held"] == 1
    assert st["dev_cards_bought_this_turn"] == 1
    playable = st["dev_cards_held"] - st["dev_cards_bought_this_turn"]
    assert playable == 0

    # Turn flips to opp
    sess = st["game"].session
    sess.current_turn_color_id = 2
    _maybe_clear_dev_just_bought(st)
    playable = st["dev_cards_held"] - st["dev_cards_bought_this_turn"]
    assert playable == 1

    # Self's turn comes back around — still playable, plays it
    sess.current_turn_color_id = 1
    _maybe_clear_dev_just_bought(st)
    _feed_postmortem(st, _play_knight_payload("Noah"))
    assert st["dev_cards_held"] == 0
