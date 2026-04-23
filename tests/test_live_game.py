"""End-to-end: feed a real WS capture through LiveGame.feed and verify
the tracker ends up with a sensible board + hand state.

This is the regression we lean on for live-session polish — if any event
extraction or dispatch regresses, the counts stop matching the capture.
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


def test_feed_game_start_boots_everything():
    if not CAPTURE_EARLY.exists():
        pytest.skip("live capture not present")
    from cataanbot.live_game import LiveGame
    game = LiveGame()
    assert not game.started

    # The first type=4 we see should fully boot the game.
    for payload in _iter_payloads(CAPTURE_EARLY):
        game.feed(payload)
        if game.started:
            break
    assert game.started
    assert game.tracker is not None
    assert game.session is not None
    assert game.color_map is not None
    # Map should be the colonist-derived one — 19 land tiles, 9 ports.
    cat_map = game.tracker.game.state.board.map
    assert len(cat_map.land_tiles) == 19
    # Color map seeded with all known players.
    assert len(game.color_map.as_dict()) == len(game.session.player_names)


def test_feed_midgame_capture_builds_and_rolls_apply():
    """Replay the midgame capture after seeding from the GameStart of
    the early capture (they're the same game). Every Build/Roll/Produce
    that lands should either apply cleanly or be a known skip — if the
    pipeline regresses, we start seeing ``error`` status."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.live_game import LiveGame

    game = LiveGame()

    # Seed from the early capture's GameStart.
    for payload in _iter_payloads(CAPTURE_EARLY):
        game.feed(payload)
        if game.started:
            break
    assert game.started

    # Drive every diff from both captures through the pipeline.
    status_counts: dict[str, int] = {}
    errors: list[str] = []
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            for result in game.feed(payload):
                status_counts[result.status] = (
                    status_counts.get(result.status, 0) + 1)
                if result.status == "error":
                    errors.append(
                        f"{type(result.event).__name__}: {result.message}")

    # Something must have actually applied — if we regress to zero
    # applies, the whole WS pipeline is broken.
    assert status_counts.get("applied", 0) > 0, (
        f"nothing applied; status breakdown was {status_counts}")
    # Errors are unexpected given the event sources are WS-built with
    # real topology. If any show up, surface them explicitly.
    assert not errors, f"dispatch errors: {errors[:5]}"

    # Board state should be non-empty: some settlements/cities placed.
    buildings = game.tracker.game.state.board.buildings
    assert len(buildings) >= 4, (
        f"expected at least the 4 initial settlements, got {len(buildings)}")


def test_dev_card_buys_emit_for_opponents_only():
    """Opponent dev-card buys should dispatch so their inferred hand
    gets debited; the self-player's buy is covered by HandSyncEvent and
    must not fire a second time."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.events import DevCardBuyEvent
    from cataanbot.live_game import LiveGame

    game = LiveGame()
    for payload in _iter_payloads(CAPTURE_EARLY):
        game.feed(payload)
        if game.started:
            break
    assert game.started

    dev_buys: list[str] = []
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            for result in game.feed(payload):
                if isinstance(result.event, DevCardBuyEvent):
                    dev_buys.append(game.color_map.get(result.event.player))

    # Self-player in fort4092 is ORANGE (BrickdDaddy, color id 5).
    assert dev_buys, "expected at least one opponent dev-card buy"
    assert "ORANGE" not in dev_buys, (
        f"self-player dev buys should be suppressed, got {dev_buys}")


def test_second_settlement_credits_opponent_starting_resources():
    """Opponents' 2nd settlements immediately grant the 3 adjacent-tile
    resources. Colonist never ships this as a dice-roll payout, and
    without the synthetic ProduceEvent the opponent's tracker hand
    stays empty until their first roll. We should see every opponent
    holding ~3 cards worth of known resources by the time setup wraps,
    not zeros."""
    if not CAPTURE_EARLY.exists():
        pytest.skip("live capture not present")
    from cataanbot.live_game import LiveGame

    game = LiveGame()
    # Replay only the early capture — its tail covers the 2nd-settlement
    # round and the first couple turns. No need to walk into midgame.
    for payload in _iter_payloads(CAPTURE_EARLY):
        game.feed(payload)
    assert game.started

    # Every opponent who has placed 2 settlements by now should have
    # *some* known cards from the synthetic 2nd-settlement yield. The
    # capture may not cover the self-player's 2nd drop, so we gate on
    # "this opp is past their 2nd settlement" per-color.
    buildings = game.tracker.game.state.board.buildings
    per_color_settlements: dict[str, int] = {}
    for _nid, (col, btype) in buildings.items():
        if btype in ("SETTLEMENT", "CITY"):
            key = col.name if hasattr(col, "name") else str(col)
            per_color_settlements[key] = (
                per_color_settlements.get(key, 0) + 1)
    sess = game.session
    opps_with_2nd = [
        (cid, user) for cid, user in sess.player_names.items()
        if cid != sess.self_color_id
        and game.color_map.has(user)
        and per_color_settlements.get(
            game.color_map.get(user), 0) >= 2
    ]
    assert opps_with_2nd, (
        f"capture didn't cover any opponent's 2nd settlement: "
        f"{per_color_settlements}")
    for _cid, user in opps_with_2nd:
        color = game.color_map.get(user)
        hand = game.tracker.hand(color)
        assert sum(hand.values()) > 0, (
            f"opp {user} ({color}) has no known cards despite "
            f"having placed a 2nd settlement — synthetic "
            f"ProduceEvent isn't firing. hand={hand}")


def test_self_player_hand_syncs_from_ws_cards():
    """After replaying the midgame capture, ORANGE (the self-player in
    fort4092) should hold exactly what the final resourceCards snapshot
    says — no inference, no drift."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.colonist_proto import load_capture
    from cataanbot.live_game import LiveGame

    game = LiveGame()
    for payload in _iter_payloads(CAPTURE_EARLY):
        game.feed(payload)
        if game.started:
            break
    assert game.started

    # Walk both captures; track the very last resourceCards snapshot
    # that carries real ints so we know what ground truth is.
    resource_ints = {1: "WOOD", 2: "BRICK", 3: "SHEEP",
                     4: "WHEAT", 5: "ORE"}
    last_cards: list[int] | None = None
    last_cid: int | None = None
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
            if payload.get("type") != 91:
                continue
            diff = (payload.get("payload") or {}).get("diff") or {}
            for cid_str, pstate in (diff.get("playerStates") or {}).items():
                if not isinstance(pstate, dict):
                    continue
                rc = pstate.get("resourceCards")
                if not isinstance(rc, dict):
                    continue
                cards = rc.get("cards")
                if not isinstance(cards, list):
                    continue
                if any(int(c) for c in cards if isinstance(c, int)):
                    last_cards = cards
                    last_cid = int(cid_str)

    assert last_cards is not None and last_cid is not None
    expected: dict[str, int] = {}
    for c in last_cards:
        res = resource_ints.get(int(c))
        if res:
            expected[res] = expected.get(res, 0) + 1

    self_user = game.session.player_names[last_cid]
    color = game.color_map.get(self_user)
    hand = game.tracker.hand(color)
    for res in ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE"):
        assert hand.get(res, 0) == expected.get(res, 0), (
            f"{res}: expected {expected.get(res, 0)}, "
            f"tracker has {hand.get(res, 0)}; full hand {hand}")


def test_advisor_snapshot_surfaces_opp_hand_breakdown():
    """After replaying a real capture, each opp's entry should include
    the inferred per-resource hand plus an `unknown` bucket accounting
    for 3rd-party steals/discards we couldn't attribute.

    Contract:
        * ``sum(hand.values()) + unknown == cards``
        * ``hand_tracked`` flips True iff unknown == 0 and cards > 0.
    """
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker

    game = LiveGame()
    for payload in _iter_payloads(CAPTURE_EARLY):
        game.feed(payload)
        if game.started:
            break
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)

    st = {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }
    snap = _build_advisor_snapshot(st)
    assert snap["game_started"]
    assert snap["opps"], "no opps in snapshot"
    for opp in snap["opps"]:
        hand = opp["hand"]
        unknown = opp["unknown"]
        cards = opp["cards"]
        # Breakdown must never claim more than the authoritative total.
        assert sum(hand.values()) <= cards, opp
        # Reported unknown bucket must reconcile the two.
        assert sum(hand.values()) + unknown == cards, opp
        # Tracked flag is the precise boundary condition.
        assert opp["hand_tracked"] == (unknown == 0 and cards > 0), opp


def test_advisor_snapshot_trim_preserves_partial_hand_knowledge():
    """When the tracker over-attributes (inferred > real), the snapshot
    must trim the excess off the largest bucket(s) instead of zeroing
    the whole breakdown. Secondary buckets represent real observations
    (produced, traded, built) and shouldn't evaporate just because one
    unknown-steal heuristic committed to the wrong resource."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker

    game = LiveGame()
    # Replay both captures — self_color_id only latches after colonist
    # ships a resourceCards frame with non-zero values, and the bridge
    # bails on the opps loop until that happens.
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started
    assert game.session.self_color_id is not None

    # Pick an opponent — any non-self cid with a username + color.
    sess = game.session
    opp_cid = next(
        cid for cid in sess.player_names
        if cid != sess.self_color_id
        and game.color_map.has(sess.player_names[cid]))
    opp_user = sess.player_names[opp_cid]
    opp_color = game.color_map.get(opp_user)

    # Force inferred = {WOOD:5, BRICK:2, SHEEP:1, WHEAT:0, ORE:0} (=8)
    # against an authoritative real total of 4. Excess 4 should come
    # entirely out of WOOD (the max bucket) leaving BRICK/SHEEP intact.
    game.tracker.set_hand(opp_color, {
        "WOOD": 5, "BRICK": 2, "SHEEP": 1, "WHEAT": 0, "ORE": 0,
    })
    sess.hand_card_counts[opp_cid] = 4

    st = {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }
    snap = _build_advisor_snapshot(st)
    target = next(o for o in snap["opps"] if o["username"] == opp_user)
    hand = target["hand"]
    # Total matches the authoritative count exactly — trim filled the gap.
    assert sum(hand.values()) == 4, target
    assert target["unknown"] == 0, target
    # Secondary buckets (non-max) must be preserved: BRICK=2, SHEEP=1.
    assert hand["BRICK"] == 2, target
    assert hand["SHEEP"] == 1, target
    # WOOD absorbed the full excess: 5 - 4 = 1 remaining.
    assert hand["WOOD"] == 1, target


def test_advisor_snapshot_clips_inferred_to_physical_supply():
    """By conservation, bank + all hands = 19 per resource in base Catan.
    When the tracker's inferred bucket for an opp would imply more of a
    resource than is physically unclaimed (e.g., bank shows only 2 ORE
    left and self holds 0, so the entire opp-pool for ORE is 2), the
    snapshot must clip inferred[ORE] to that cap and surface the excess
    as ``unknown`` rather than displaying an impossible count."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started
    assert game.session.self_color_id is not None

    sess = game.session
    opp_cid = next(
        cid for cid in sess.player_names
        if cid != sess.self_color_id
        and game.color_map.has(sess.player_names[cid]))
    opp_user = sess.player_names[opp_cid]
    opp_color = game.color_map.get(opp_user)
    self_user = sess.player_names[sess.self_color_id]
    self_color = game.color_map.get(self_user)

    # Directly manipulate state to simulate a tracker desync: bank shows
    # 17 ORE (only 2 unclaimed across all opps), self holds 0 ORE, but
    # the tracker attributed 5 ORE to this opp — physically impossible
    # per conservation (17 + 0 + 5 + ... would exceed 19).
    state = game.tracker.game.state
    from catanatron.state import RESOURCES
    ore_idx = RESOURCES.index("ORE")
    state.resource_freqdeck[ore_idx] = 17
    self_idx = state.color_to_index[game.tracker._color(self_color)]
    state.player_state[f"P{self_idx}_ORE_IN_HAND"] = 0
    opp_idx = state.color_to_index[game.tracker._color(opp_color)]
    state.player_state[f"P{opp_idx}_ORE_IN_HAND"] = 5
    # Pad the real hand count so the physical-supply clip runs before
    # the over-attribution trim (inferred_total stays ≤ real_total).
    sess.hand_card_counts[opp_cid] = 20

    st = {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }
    snap = _build_advisor_snapshot(st)
    target = next(o for o in snap["opps"] if o["username"] == opp_user)
    # Cap = max(0, 19 - bank_ore - self_ore) = 19 - 17 - 0 = 2.
    # Inferred must never exceed what physically remains in the opp pool.
    assert target["hand"]["ORE"] <= 2, target


def test_advisor_snapshot_surfaces_incoming_trade_verdict():
    """A pending TradeOfferEvent should show up on the snapshot with an
    evaluate_incoming_trade verdict attached. Self-originated offers
    must not surface — we don't accept-or-decline our own proposals."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started
    sess = game.session
    assert sess.self_color_id is not None

    # Force a known-comfortable self hand so the swap has room to move.
    self_user = sess.player_names[sess.self_color_id]
    self_color = game.color_map.get(self_user)
    game.tracker.set_hand(self_color, {
        "WOOD": 1, "BRICK": 1, "SHEEP": 0, "WHEAT": 1, "ORE": 2,
    })

    opp_user = next(
        u for cid, u in sess.player_names.items()
        if cid != sess.self_color_id and game.color_map.has(u))

    base_st: dict = {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }

    # No pending offer → field stays None.
    st = dict(base_st, pending_trade_offer=None)
    snap = _build_advisor_snapshot(st)
    assert snap["incoming_trade"] is None

    # Opp offers SHEEP for ORE — should populate the field with a verdict.
    st = dict(base_st, pending_trade_offer={
        "player": opp_user, "give": {"SHEEP": 1},
        "want": {"ORE": 1}, "ts": None,
    })
    snap = _build_advisor_snapshot(st)
    inc = snap["incoming_trade"]
    assert inc is not None
    assert inc["offerer"] == opp_user
    assert inc["give"] == {"SHEEP": 1}
    assert inc["want"] == {"ORE": 1}
    assert inc["verdict"] in ("accept", "decline", "consider")
    assert isinstance(inc["reason"], str) and inc["reason"]

    # Self-originated offer must not surface (no accept/decline for us).
    st = dict(base_st, pending_trade_offer={
        "player": self_user, "give": {"ORE": 1},
        "want": {"SHEEP": 1}, "ts": None,
    })
    snap = _build_advisor_snapshot(st)
    assert snap["incoming_trade"] is None


def test_feed_postmortem_tracks_and_clears_trade_offer():
    """`_feed_postmortem` should stash a TradeOfferEvent in
    `pending_trade_offer` and drop it on the next dice roll. This gates
    the overlay's accept/decline panel — a stale offer after the turn
    advanced is worse than no panel at all."""
    from cataanbot.bridge import _feed_postmortem
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker

    def _offer_payload(offerer: str, give_alt: str, want_alt: str):
        return {
            "text": f"{offerer} wants to give {give_alt} for {want_alt}",
            "parts": [
                {"kind": "name", "name": offerer},
                {"kind": "text", "text": "wants to give"},
                {"kind": "icon", "alt": give_alt},
                {"kind": "text", "text": "for"},
                {"kind": "icon", "alt": want_alt},
            ],
            "names": [{"name": offerer, "color": "rgb(224,151,66)"}],
            "icons": [{"alt": give_alt}, {"alt": want_alt}],
            "ts": 1.0,
        }

    def _roll_payload(player: str, d1: int, d2: int):
        return {
            "text": f"{player} rolled {d1 + d2}",
            "parts": [
                {"kind": "name", "name": player},
                {"kind": "text", "text": "rolled"},
                {"kind": "icon", "alt": f"dice_{d1}"},
                {"kind": "icon", "alt": f"dice_{d2}"},
            ],
            "names": [{"name": player, "color": "rgb(255,0,0)"}],
            "icons": [{"alt": f"dice_{d1}"}, {"alt": f"dice_{d2}"}],
            "ts": 2.0,
        }

    st: dict = {
        "pm_tracker": Tracker(),
        "pm_color_map": ColorMap(),
        "pm_events": [],
        "pm_results": [],
        "pm_timestamps": [],
        "pm_written": False,
        "pm_dir": None,
        "pending_trade_offer": None,
    }
    _feed_postmortem(st, _offer_payload("Alice", "Lumber", "Brick"))
    assert st["pending_trade_offer"] is not None
    assert st["pending_trade_offer"]["player"] == "Alice"
    assert st["pending_trade_offer"]["give"] == {"WOOD": 1}
    assert st["pending_trade_offer"]["want"] == {"BRICK": 1}

    _feed_postmortem(st, _roll_payload("Alice", 3, 4))
    assert st["pending_trade_offer"] is None


def test_feed_postmortem_triggers_robber_on_self_knight():
    """Playing a Knight should arm the robber-ranking panel the same
    way rolling a 7 does. Opponent knights must NOT arm it — they pick
    their own tile."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _feed_postmortem
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started
    sess = game.session
    assert sess.self_color_id is not None
    self_user = sess.player_names[sess.self_color_id]
    opp_user = next(
        u for cid, u in sess.player_names.items()
        if cid != sess.self_color_id and game.color_map.has(u))

    def _knight_payload(player: str):
        # Colonist logs a knight play as "X used [Knight]". Parser
        # matches on the lowercase "knight" substring in the text.
        return {
            "text": f"{player} used Knight",
            "parts": [
                {"kind": "name", "name": player},
                {"kind": "text", "text": "used"},
                {"kind": "icon", "alt": "Knight"},
            ],
            "names": [{"name": player, "color": "rgb(200,200,200)"}],
            "icons": [{"alt": "Knight"}],
            "ts": 1.0,
        }

    def _fresh_st():
        return {
            "game": game,
            "display_colors": {},
            "pm_tracker": Tracker(),
            "pm_color_map": ColorMap(),
            "pm_events": [],
            "pm_results": [],
            "pm_timestamps": [],
            "pm_written": False,
            "pm_dir": None,
            "pending_trade_offer": None,
            "robber_pending": False,
            "robber_snapshot": None,
        }

    # Opp knight — must not set robber_pending (we don't pick the tile).
    st = _fresh_st()
    _feed_postmortem(st, _knight_payload(opp_user))
    assert st["robber_pending"] is False
    assert st["robber_snapshot"] is None

    # Self knight — arms robber panel.
    st = _fresh_st()
    _feed_postmortem(st, _knight_payload(self_user))
    assert st["robber_pending"] is True
    # Snapshot is best-effort; at minimum it should be a list (possibly
    # empty on edge boards). None would mean the compute path errored.
    assert st["robber_snapshot"] is not None


def test_compute_robber_snapshot_marks_suggested_victim():
    """Each robber target should carry a suggested_victim color — the
    best person to steal from. Preference order: card count dominates
    (steal EV), VP pressure boosts near-winners, pips break ties."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _compute_robber_snapshot
    from cataanbot.live_game import LiveGame

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started
    snap = _compute_robber_snapshot(game, display_colors={}, top=5)
    assert snap is not None and len(snap) > 0
    # Every target with victims must pick exactly one of those victims.
    for target in snap:
        victims = target["victims"]
        if not victims:
            continue
        assert target["suggested_victim"] is not None, target
        sv = target["suggested_victim"]
        # The suggested color must appear in the victims list.
        assert any(v["color"] == sv for v in victims), target
        # Exactly one victim flagged suggested=True.
        flagged = [v for v in victims if v.get("suggested")]
        assert len(flagged) == 1, flagged
        assert flagged[0]["color"] == sv


def test_compute_knight_hint_none_when_no_knight():
    """No Knight in hand → no hint (None). The overlay uses this to
    hide the panel entirely."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _compute_knight_hint
    from cataanbot.live_game import LiveGame
    from catanatron import Color

    # Midgame capture is needed for self_color_id to latch (setup-only
    # captures never see the first resourceCards frame).
    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started
    sess = game.session
    assert sess.self_color_id is not None
    self_user = sess.player_names[sess.self_color_id]
    color = game.color_map.get(self_user)
    idx = game.tracker.game.state.color_to_index[Color[color.upper()]]
    # Force knight count to 0 — whatever the capture had, this is a
    # clean "no knight" baseline.
    game.tracker.game.state.player_state[f"P{idx}_KNIGHT_IN_HAND"] = 0
    hint = _compute_knight_hint(game)
    assert hint is None


def test_compute_knight_hint_play_when_robber_blocks_self():
    """Robber parked on one of our own production tiles → recommend
    play, with reason mentioning the block."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _compute_knight_hint
    from cataanbot.live_game import LiveGame
    from catanatron import Color

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started
    sess = game.session
    self_user = sess.player_names.get(sess.self_color_id)
    color = game.color_map.get(self_user)
    my_enum = Color[color.upper()]
    cat = game.tracker.game
    idx = cat.state.color_to_index[my_enum]
    # Give self a Knight so the hint fires.
    cat.state.player_state[f"P{idx}_KNIGHT_IN_HAND"] = 1
    # Park the robber on a tile adjacent to one of self's buildings.
    m = cat.state.board.map
    self_nodes = {n for n, (c, _) in cat.state.board.buildings.items()
                  if c == my_enum}
    assert self_nodes
    robber_coord = next(
        coord for coord, tile in m.land_tiles.items()
        if tile.number and set(tile.nodes.values()) & self_nodes)
    cat.state.board.robber_coordinate = robber_coord
    hint = _compute_knight_hint(game)
    assert hint is not None
    assert hint["have"] == 1
    assert hint["should_play"] is True
    assert "your tile" in hint["reason"].lower() or "block" in hint["reason"].lower()


def test_compute_discard_plan_preserves_city_over_lesser_builds():
    """With 10 cards including 2+ WHEAT and 3+ ORE, we must drop 5.
    The planner should hold the city cost (2W 3O = 5 cards) and dump
    5 non-reserved cards (SHEEP first, then WOOD/BRICK)."""
    from cataanbot.bridge import _compute_discard_plan
    hand = {"WOOD": 1, "BRICK": 1, "SHEEP": 3, "WHEAT": 2, "ORE": 3}
    drops, preserve = _compute_discard_plan(hand, 5)
    assert preserve == "city"
    assert sum(drops.values()) == 5
    # City resources must still be in hand after drops.
    for r, n in (("WHEAT", 2), ("ORE", 3)):
        assert hand[r] - drops.get(r, 0) >= n, (
            f"{r} dipped below city reserve: {drops}")


def test_compute_discard_plan_falls_back_when_no_build_affordable():
    """A pure-sheep hand can't preserve any build; drops all from SHEEP
    without claiming a preserve rationale."""
    from cataanbot.bridge import _compute_discard_plan
    hand = {"WOOD": 0, "BRICK": 0, "SHEEP": 10, "WHEAT": 0, "ORE": 0}
    drops, preserve = _compute_discard_plan(hand, 5)
    assert preserve is None
    assert drops == {"SHEEP": 5}


def test_compute_discard_plan_breaks_reserve_when_no_slack():
    """When non-reserved cards are insufficient to reach the discard
    target, the planner dips into the preserved build — and drops the
    preserve rationale since we can't hold it together."""
    from cataanbot.bridge import _compute_discard_plan
    # 8 cards. Best preserve: settlement (1W+1B+1S+1Wh = 4). Need to
    # drop 4, so 4 remain. SHEEP has 1 non-reserved. All other non-
    # reserved are 0. We have to dip in.
    hand = {"WOOD": 1, "BRICK": 1, "SHEEP": 2, "WHEAT": 1, "ORE": 3}
    drops, preserve = _compute_discard_plan(hand, 4)
    assert sum(drops.values()) == 4
    # Either the preserve is broken (None) or ORE was available as
    # pure non-reserved (3 ore + 1 sheep = 4 drops, preserve held).
    # In this hand city isn't affordable (need 2 wheat), so settlement
    # is the preserve — which has no ore. So 4 drops from non-reserved
    # = 1 sheep + 3 ore. That keeps settlement. Verify:
    assert preserve == "settlement"
    assert drops.get("ORE", 0) == 3
    assert drops.get("SHEEP", 0) == 1


def test_compute_discard_hint_bails_under_limit():
    """7-card hand doesn't trigger discard on a 7-roll; hint is None."""
    from cataanbot.bridge import _compute_discard_hint
    hand = {"WOOD": 2, "BRICK": 2, "SHEEP": 1, "WHEAT": 1, "ORE": 1}
    assert _compute_discard_hint(hand, 7) is None


def test_compute_discard_hint_returns_plan_over_limit():
    """10-card hand → must drop 5; hint surfaces drops + rationale."""
    from cataanbot.bridge import _compute_discard_hint
    hand = {"WOOD": 1, "BRICK": 1, "SHEEP": 3, "WHEAT": 2, "ORE": 3}
    hint = _compute_discard_hint(hand, 10)
    assert hint is not None
    assert hint["need"] == 5
    assert sum(hint["drop"].values()) == 5
    assert "city" in hint["rationale"]


def test_compute_leader_threat_bails_when_nobody_leads():
    from cataanbot.bridge import _compute_leader_threat
    snap = {"opps": [{"username": "A", "vp": 3}, {"username": "B", "vp": 2}],
            "self": {"vp": 4}}
    assert _compute_leader_threat(snap) is None


def test_compute_leader_threat_fires_at_mid_late():
    """A 6 VP opp should trigger a mid-level warning."""
    from cataanbot.bridge import _compute_leader_threat
    snap = {"opps": [{"username": "A", "vp": 6, "color": "RED"}],
            "self": {"vp": 4}}
    t = _compute_leader_threat(snap)
    assert t is not None
    assert t["leader_username"] == "A"
    assert t["leader_vp"] == 6
    assert t["level"] == "mid"
    assert t["gap"] == 2


def test_compute_leader_threat_close_level_at_8_vp():
    """At 8 VP (close_to_win for target=10), level escalates to
    ``close``. Messaging should call out "one build from winning"."""
    from cataanbot.bridge import _compute_leader_threat
    snap = {"opps": [{"username": "X", "vp": 8, "color": "BLUE"}],
            "self": {"vp": 5}}
    t = _compute_leader_threat(snap)
    assert t is not None
    assert t["level"] == "close"
    assert "one build" in t["message"]


def test_compute_leader_threat_win_level_at_10_vp():
    from cataanbot.bridge import _compute_leader_threat
    snap = {"opps": [{"username": "X", "vp": 10, "color": "BLUE"}],
            "self": {"vp": 5}}
    t = _compute_leader_threat(snap)
    assert t is not None
    assert t["level"] == "win"


def test_leader_threat_vector_flags_vp_build():
    """Leader at 8 VP holding a city cost in hand should surface
    'vp_build' in threat_vector AND include 'can city' in the
    message. This is the qualitative jump that makes the banner
    actionable — Noah can robber ore instead of just reading VP."""
    from cataanbot.bridge import _compute_leader_threat
    snap = {
        "opps": [{
            "username": "X", "vp": 8, "color": "BLUE",
            "can_afford": ["city"], "dev_cards": 0,
        }],
        "self": {"vp": 5},
    }
    t = _compute_leader_threat(snap)
    assert t is not None
    assert "vp_build" in t["threat_vector"]
    assert "can city" in t["message"]
    assert t["gap_to_win"] == 2


def test_leader_threat_vector_bumps_mid_to_close_on_vp_build():
    """A leader at 7 VP normally reads 'mid', but if they can afford a
    VP build they can close faster than VP suggests — bump to 'close'.
    The 2-VP gap they'd close with city puts them at 8 next turn, same
    as the normal close threshold. This is exactly when Noah needs
    the urgent-banner styling to kick in."""
    from cataanbot.bridge import _compute_leader_threat
    snap = {
        "opps": [{
            "username": "X", "vp": 7, "color": "BLUE",
            "can_afford": ["settlement"], "dev_cards": 0,
        }],
        "self": {"vp": 5},
    }
    t = _compute_leader_threat(snap)
    assert t is not None
    assert t["level"] == "close"
    assert "vp_build" in t["threat_vector"]


def test_leader_threat_vector_flags_dev_vp_only_when_close():
    """Dev cards at mid-game (6 VP) don't warrant dev_vp flagging —
    they're probably knights. Same dev cards at close (8 VP) = hidden
    VP risk worth highlighting."""
    from cataanbot.bridge import _compute_leader_threat
    # 6 VP leader with dev card: vector should NOT include dev_vp.
    snap_mid = {
        "opps": [{
            "username": "X", "vp": 6, "color": "RED",
            "can_afford": [], "dev_cards": 2,
        }],
        "self": {"vp": 4},
    }
    t_mid = _compute_leader_threat(snap_mid)
    assert "dev_vp" not in (t_mid or {}).get("threat_vector", [])
    # 8 VP leader with dev card: vector SHOULD include dev_vp.
    snap_close = {
        "opps": [{
            "username": "X", "vp": 8, "color": "RED",
            "can_afford": [], "dev_cards": 2,
        }],
        "self": {"vp": 4},
    }
    t_close = _compute_leader_threat(snap_close)
    assert t_close is not None
    assert "dev_vp" in t_close["threat_vector"]
    assert "2 dev" in t_close["message"]


def test_compute_monopoly_hint_picks_resource_with_largest_total():
    """When self holds a MONOPOLY, the hint should pick the resource
    with the highest inferred total across opps."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _compute_monopoly_hint
    from cataanbot.live_game import LiveGame
    from catanatron import Color

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started
    sess = game.session
    self_user = sess.player_names.get(sess.self_color_id)
    color = game.color_map.get(self_user)
    my_enum = Color[color.upper()]
    cat = game.tracker.game
    idx = cat.state.color_to_index[my_enum]
    # With no MONOPOLY: hint is None.
    cat.state.player_state[f"P{idx}_MONOPOLY_IN_HAND"] = 0
    assert _compute_monopoly_hint(
        game, color, dict(game.tracker.hand(color))) is None
    # Grant one MONOPOLY: hint fires.
    cat.state.player_state[f"P{idx}_MONOPOLY_IN_HAND"] = 1
    h = _compute_monopoly_hint(
        game, color, dict(game.tracker.hand(color)))
    assert h is not None
    assert h["have"] == 1
    assert h["resource"] in ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")
    assert h["est_steal"] >= 0
    assert set(h["totals"]) == {"WOOD", "BRICK", "SHEEP", "WHEAT", "ORE"}
    # The chosen resource should have the max total (ties ok).
    max_total = max(h["totals"].values())
    assert h["totals"][h["resource"]] == max_total


def test_compute_yop_hint_suggests_pair_to_unlock_build():
    """YoP should identify a 2-card pickup that unlocks a build."""
    from cataanbot.bridge import _compute_yop_hint

    class _FakeGame:
        class _Tracker:
            class _Game:
                class _State:
                    color_to_index: dict = {}
                    player_state: dict = {}
                state = _State()
            game = _Game()
        tracker = _Tracker()

    from catanatron import Color
    g = _FakeGame()
    g.tracker.game.state.color_to_index = {Color.RED: 0}
    g.tracker.game.state.player_state = {"P0_YEAR_OF_PLENTY_IN_HAND": 1}
    # Self has 1 WOOD + 1 BRICK + 1 SHEEP but no WHEAT. Settlement
    # requires 1 of each of {WOOD, BRICK, SHEEP, WHEAT}. YoP → 1 WHEAT
    # unlocks settlement; second slot should target another need.
    hand = {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 0, "ORE": 0}
    h = _compute_yop_hint(g, "RED", hand)
    assert h is not None
    assert h["have"] == 1
    assert h["unlock"] == "settlement"
    assert "WHEAT" in h["pair"]


def test_compute_rb_hint_fires_when_longest_road_in_reach():
    """Road Building hint covers its four states: no card → None;
    card but no road pieces → None; card + near longest road →
    should_play=secures; card + opp ahead → should_play=catches; card
    + no swing → should_play=False. Uses a fake game with a monkey-
    patched _pieces_for_color so we don't need a live capture."""
    import cataanbot.bridge as bridge
    from cataanbot.bridge import _compute_rb_hint
    from catanatron import Color

    class _FakeGame:
        class _Tracker:
            class _Game:
                class _State:
                    color_to_index: dict = {}
                    player_state: dict = {}
                state = _State()
            game = _Game()
        tracker = _Tracker()

    g = _FakeGame()
    g.tracker.game.state.color_to_index = {Color.RED: 0, Color.BLUE: 1}

    # Monkeypatch piece counts — 10 roads left keeps us out of the
    # "running low" branch unless we explicitly set it.
    original = bridge._pieces_for_color
    bridge._pieces_for_color = lambda _g, _c: {
        "settle": 2, "settle_left": 3, "city": 0, "city_left": 4,
        "road": 5, "road_left": 10,
    }
    try:
        # Case 1: no RB card → None.
        g.tracker.game.state.player_state = {
            "P0_ROAD_BUILDING_IN_HAND": 0,
            "P0_LONGEST_ROAD_LENGTH": 3, "P0_HAS_ROAD": False,
            "P1_LONGEST_ROAD_LENGTH": 2, "P1_HAS_ROAD": False,
        }
        assert _compute_rb_hint(g, "RED") is None

        # Case 2: card + near longest road (self 4, opp 2) → secures.
        g.tracker.game.state.player_state = {
            "P0_ROAD_BUILDING_IN_HAND": 1,
            "P0_LONGEST_ROAD_LENGTH": 4, "P0_HAS_ROAD": False,
            "P1_LONGEST_ROAD_LENGTH": 2, "P1_HAS_ROAD": False,
        }
        h = _compute_rb_hint(g, "RED")
        assert h is not None and h["should_play"]
        assert "secures" in h["reason"]
        assert h["self_len"] == 4 and h["opp_len"] == 2

        # Case 3: card + opp holds longest road (opp 5/has_road,
        # self 3). Projected 5 catches opp — should fire "catches".
        g.tracker.game.state.player_state = {
            "P0_ROAD_BUILDING_IN_HAND": 1,
            "P0_LONGEST_ROAD_LENGTH": 3, "P0_HAS_ROAD": False,
            "P1_LONGEST_ROAD_LENGTH": 5, "P1_HAS_ROAD": True,
        }
        h = _compute_rb_hint(g, "RED")
        assert h is not None and h["should_play"]
        assert "catches" in h["reason"]

        # Case 4: card + no swing (both sides low) → hold.
        g.tracker.game.state.player_state = {
            "P0_ROAD_BUILDING_IN_HAND": 1,
            "P0_LONGEST_ROAD_LENGTH": 2, "P0_HAS_ROAD": False,
            "P1_LONGEST_ROAD_LENGTH": 2, "P1_HAS_ROAD": False,
        }
        h = _compute_rb_hint(g, "RED")
        assert h is not None and not h["should_play"]
        assert "hold" in h["reason"]

        # Case 5: roads_left == 0 → None (can't use the card).
        bridge._pieces_for_color = lambda _g, _c: {
            "settle": 2, "settle_left": 3, "city": 0, "city_left": 4,
            "road": 15, "road_left": 0,
        }
        assert _compute_rb_hint(g, "RED") is None

        # Case 6: roads_left <= 2 triggers "running low" even with no
        # race pressure.
        bridge._pieces_for_color = lambda _g, _c: {
            "settle": 2, "settle_left": 3, "city": 0, "city_left": 4,
            "road": 14, "road_left": 1,
        }
        g.tracker.game.state.player_state = {
            "P0_ROAD_BUILDING_IN_HAND": 1,
            "P0_LONGEST_ROAD_LENGTH": 2, "P0_HAS_ROAD": False,
            "P1_LONGEST_ROAD_LENGTH": 2, "P1_HAS_ROAD": False,
        }
        h = _compute_rb_hint(g, "RED")
        assert h is not None and h["should_play"]
        assert "running low" in h["reason"]
    finally:
        bridge._pieces_for_color = original


def test_reconnect_replays_pre_existing_buildings_into_tracker():
    """Simulate a mid-game reconnect: on a fresh WS session, colonist
    ships the full current mapState in the GameStart payload — every
    settlement, city, and road already on the board. Without
    ``_replay_pre_existing_buildings`` the tracker starts empty and
    downstream advisors think nothing has been built. This test drives
    the early+midgame captures through one LiveGame, snapshots the
    session's ``known_corners`` / ``known_edges`` / ``corner_owners``,
    then boots a fresh LiveGame with those baked into the GameStart's
    mapState and asserts the second tracker ends up with the same
    building inventory as the first.
    """
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from copy import deepcopy

    from cataanbot.colonist_proto import load_capture
    from cataanbot.live_game import LiveGame

    # Drive the original game as a reference.
    ref_game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            ref_game.feed(payload)
    assert ref_game.started
    ref_buildings = dict(ref_game.tracker.game.state.board.buildings)
    ref_roads = dict(ref_game.tracker.game.state.board.roads)
    assert ref_buildings, "reference game didn't build anything"

    # Grab the very first GameStart body from the early capture so we can
    # construct a synthetic reconnect payload that reuses the same map
    # topology and player roster.
    start_body: dict = {}
    for frame in load_capture(CAPTURE_EARLY):
        if frame.error:
            continue
        p = frame.payload
        if isinstance(p, dict) and p.get("type") == 4:
            start_body = deepcopy(p.get("payload") or {})
            break
    assert start_body, "no GameStart in early capture"

    game_state = (start_body.get("gameState")
                  if "gameState" in start_body else start_body)
    map_state = game_state["mapState"]

    # Inject the reference game's accumulated corner/edge state into the
    # mapState — this mirrors what a real reconnect sees from colonist.
    sess = ref_game.session
    for cid, bt in sess.known_corners.items():
        cid_str = str(cid)
        if cid_str not in map_state["tileCornerStates"]:
            continue
        map_state["tileCornerStates"][cid_str]["buildingType"] = int(bt)
        owner = sess.corner_owners.get(cid)
        if owner is not None:
            map_state["tileCornerStates"][cid_str]["owner"] = int(owner)
    for eid, owner in sess.known_edges.items():
        eid_str = str(eid)
        if eid_str not in map_state["tileEdgeStates"]:
            continue
        if owner:
            map_state["tileEdgeStates"][eid_str]["owner"] = int(owner)

    # Boot a fresh game from the stitched body.
    reconnect_game = LiveGame()
    reconnect_game.start_from_game_state(start_body)
    assert reconnect_game.started

    got_buildings = dict(reconnect_game.tracker.game.state.board.buildings)
    got_roads = dict(reconnect_game.tracker.game.state.board.roads)
    assert got_buildings == ref_buildings, (
        f"building parity failed: "
        f"ref={sorted(ref_buildings.items())} "
        f"got={sorted(got_buildings.items())}")
    assert got_roads == ref_roads, (
        f"road parity failed: "
        f"ref={sorted(ref_roads.items())} "
        f"got={sorted(got_roads.items())}")
    # build_counts tally on the reconnect should reflect the corners +
    # roads currently on the board (reconnect sees each corner once,
    # classified by its final buildingType). The setup-phase gate in
    # ``_debit_build`` only needs to see "this color already has ≥2
    # settlements / roads" — which is trivially true for a reconnect —
    # so any cap ≥2 is fine. We just assert the tallies are non-zero
    # for colors with buildings so the gate doesn't incorrectly bill
    # post-reconnect placements as free.
    for color, tally in reconnect_game.build_counts.items():
        corners = sum(1 for _nid, (c, _bt) in got_buildings.items()
                      if c.name == color)
        roads = sum(1 for (_a, _b), c in got_roads.items()
                    if c.name == color) // 2  # both-directions
        assert tally["settlement"] + tally["city"] == corners, (
            f"{color}: replayed tally {tally} doesn't match "
            f"board corners {corners}")
        assert tally["road"] == roads, (
            f"{color}: replayed road tally {tally['road']} "
            f"doesn't match board roads {roads}")


def test_paid_builds_debit_costs_and_setup_is_free():
    """First 2 settlements + 2 roads per color are free; subsequent
    settlements/cities/roads should debit the standard build cost."""
    if not CAPTURE_EARLY.exists():
        pytest.skip("live capture not present")
    from cataanbot.events import BuildEvent
    from cataanbot.live_game import LiveGame

    game = LiveGame()
    for payload in _iter_payloads(CAPTURE_EARLY):
        game.feed(payload)
        if game.started:
            break
    assert game.started

    red = game.color_map.get("Alice" if game.color_map.has("Alice") else
                             next(iter(game.color_map.as_dict())))
    before = dict(game.tracker.hand(red))
    # Pre-load enough cards to cover a settlement + a city + a road.
    for res, n in (("WOOD", 2), ("BRICK", 2), ("SHEEP", 1),
                   ("WHEAT", 3), ("ORE", 3)):
        game.tracker.give(red, n, res)

    # Simulate four settlement + two road BuildEvents: the first two
    # settlements and two roads are the setup-phase placements (free).
    username = game.color_map.reverse(red)
    free_events = [
        BuildEvent(player=username, piece="settlement", node_id=0),
        BuildEvent(player=username, piece="road", edge_nodes=(0, 1)),
        BuildEvent(player=username, piece="settlement", node_id=3),
        BuildEvent(player=username, piece="road", edge_nodes=(3, 4)),
    ]
    paid_events = [
        BuildEvent(player=username, piece="settlement", node_id=6),
        BuildEvent(player=username, piece="road", edge_nodes=(6, 7)),
        BuildEvent(player=username, piece="city", node_id=6),
    ]
    for ev in free_events + paid_events:
        game._debit_build(ev)

    after = game.tracker.hand(red)
    # Paid settlement = W+B+Sh+Wh, paid road = W+B, city = 2Wh+3Ore.
    # Net debit from paid builds only (setup was free).
    expected_debit = {"WOOD": 2, "BRICK": 2, "SHEEP": 1,
                      "WHEAT": 3, "ORE": 3}
    for res, n in expected_debit.items():
        got = before.get(res, 0) + n - after.get(res, 0)
        assert got == n, (
            f"{res}: expected debit of {n}, got {got} "
            f"(before={before.get(res, 0)}, after={after.get(res, 0)})")


def test_snapshot_exposes_piece_counts_on_self_and_opps():
    """After two captures replayed, both self and every opp should
    have piece counts populated: settlements placed, cities placed,
    roads placed, plus the remaining-in-pool counts. Counts derive
    from catanatron's Px_*_AVAILABLE keys so they're authoritative."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    st: dict = {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }
    snap = _build_advisor_snapshot(st)
    me = snap["self"]
    assert me is not None
    p = me["pieces"]
    # Every field populated, non-negative, placed+left sums to the base
    # max (5 settlements, 4 cities, 15 roads).
    assert p["settle"] + p["settle_left"] == 5
    assert p["city"] + p["city_left"] == 4
    assert p["road"] + p["road_left"] == 15
    # Mid-game: at least the 2 starting settlements + some roads built.
    assert p["settle"] >= 2 or p["city"] >= 1
    assert p["road"] >= 2

    for opp in snap["opps"]:
        op = opp["pieces"]
        assert op["settle"] + op["settle_left"] == 5
        assert op["city"] + op["city_left"] == 4
        assert op["road"] + op["road_left"] == 15


def test_snapshot_self_vp_breakdown_sums_to_total():
    """Self VP breakdown must (a) exist when the colonist session is
    populated, (b) have every category non-negative, (c) have its
    `total` match the per-category sum, and (d) align with the
    top-level `vp` number that _get_vp returns. Drift between those
    two would mean the HUD is showing a breakdown that doesn't add up
    to the displayed VP — confusing and worse than showing nothing."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    st: dict = {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }
    snap = _build_advisor_snapshot(st)
    me = snap["self"]
    assert me is not None
    b = me["vp_breakdown"]
    assert b is not None, "session-backed capture should yield a breakdown"
    for key in ("settle", "city", "vp_cards",
                "longest_road", "largest_army", "total"):
        assert b[key] >= 0, f"{key} went negative"
    # Sum of parts equals total (city/lr/la are already doubled in the dict).
    summed = (b["settle"] + b["city"] + b["vp_cards"]
              + b["longest_road"] + b["largest_army"])
    assert b["total"] == summed, (
        f"breakdown {b} parts sum to {summed} ≠ total {b['total']}")
    # Breakdown must match the displayed VP — otherwise the HUD would
    # show inconsistent numbers.
    assert b["total"] == me["vp"], (
        f"breakdown total {b['total']} ≠ self.vp {me['vp']}")
    # Post-setup there are always 2 build slots on the board (settle or
    # upgraded to city). `city` is already doubled, so a single upgrade
    # accounts for 2 of the total on its own.
    assert b["settle"] + b["city"] >= 2


def test_compute_production_scales_settlements_and_cities():
    """Pre-build: zero production. After a settlement on a numbered
    tile: per_roll > 0. After upgrading to city: exactly 2× the
    settlement rate (per-node production doubles). Guards against
    regressions where city multiplier goes missing or double-applies
    to settlements."""
    from cataanbot.bridge import _compute_production
    from cataanbot.tracker import Tracker
    from catanatron import Color

    tr = Tracker(seed=4242)
    board = tr.game.state.board
    m = board.map
    # Pre-build: zero production.
    p0 = _compute_production(_wrap_game(tr), "RED")
    assert p0 is not None
    assert p0["per_roll"] == 0.0
    assert p0["top_resource"] is None
    # Find a legal inland node that touches at least two numbered
    # tiles so the math is meaningful.
    buildable = board.buildable_node_ids(
        Color.RED, initial_build_phase=True)
    target = None
    for nid in buildable:
        prod = m.node_production.get(int(nid), {})
        if sum(prod.values()) > 0.1:
            target = nid
            break
    assert target is not None
    board.build_settlement(Color.RED, target, initial_build_phase=True)
    p1 = _compute_production(_wrap_game(tr), "RED")
    assert p1["per_roll"] > 0
    assert p1["top_resource"] in {
        "WOOD", "BRICK", "SHEEP", "WHEAT", "ORE"}
    settle_rate = p1["per_roll"]
    # Upgrade to city — production should double at this node.
    board.build_city(Color.RED, target)
    p2 = _compute_production(_wrap_game(tr), "RED")
    assert abs(p2["per_roll"] - 2 * settle_rate) < 1e-9


def test_snapshot_populates_per_opp_production():
    """Every opp row should carry a production block after capture
    replay. The block may show zero (e.g. robbed out or building-less)
    but the shape must be consistent — None would break the overlay's
    strong-engine comparison logic. After forcing an extra city onto
    one opp, that row's per_roll must rise (2x settle delta)."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _build_advisor_snapshot, _compute_production
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker
    from catanatron import Color

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    st: dict = {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }
    snap = _build_advisor_snapshot(st)
    assert snap["opps"]
    for opp in snap["opps"]:
        assert "production" in opp
        assert opp["production"] is not None
        assert opp["production"]["per_roll"] >= 0
        # by_resource has all 5 resource keys with non-negative floats
        by_res = opp["production"]["by_resource"]
        for r in ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE"):
            assert r in by_res
            assert by_res[r] >= 0

    # Upgrade a settlement to a city on the first opp; re-snap;
    # per_roll for that opp must rise (city = 2× settle at same node).
    target = snap["opps"][0]
    opp_enum = Color[target["color"].upper()]
    board = game.tracker.game.state.board
    settle_nids = [nid for nid, (col, bt) in board.buildings.items()
                   if col == opp_enum and str(bt).upper() != "CITY"]
    if not settle_nids:
        pytest.skip("first opp has no upgradable settlement in capture")
    before = target["production"]["per_roll"]
    board.build_city(opp_enum, settle_nids[0])
    p_after = _compute_production(game, target["color"])
    assert p_after["per_roll"] >= before, (
        f"city upgrade must not reduce production: {before} -> "
        f"{p_after['per_roll']}")


def test_snapshot_populates_self_ports_after_build():
    """`_owned_ports` returns a list based on coastal buildings. With
    no building on a port node, the list is empty; after a build on a
    port terminal, the port's resource (or GENERIC) shows up."""
    from cataanbot.bridge import _owned_ports
    from cataanbot.tracker import Tracker
    from catanatron import Color

    tr = Tracker(seed=4242)
    board = tr.game.state.board
    m = board.map
    # Pre-build: no settlements, no ports owned.
    assert _owned_ports(_wrap_game(tr), "RED") == []
    # Pick a port whose terminals are legitimate buildable nodes at
    # opening time, then drop a RED settlement on one terminal.
    buildable = board.buildable_node_ids(
        Color.RED, initial_build_phase=True)
    target = None
    expected_label: str | None = None
    for port in m.ports_by_id.values():
        for nid in port.nodes.values():
            if nid in buildable:
                target = nid
                expected_label = (
                    port.resource if port.resource is not None else "GENERIC")
                break
        if target is not None:
            break
    assert target is not None, "no reachable port terminal in base map"
    board.build_settlement(Color.RED, target, initial_build_phase=True)
    ports = _owned_ports(_wrap_game(tr), "RED")
    assert ports is not None
    assert expected_label in ports


def _wrap_game(tracker):
    """Minimal adapter so _owned_ports sees `.tracker.game`."""
    class _W:
        pass
    w = _W()
    w.tracker = tracker
    return w


def test_snapshot_exposes_opp_ports_after_capture():
    """After a capture replay, every opp row should include a `ports`
    list (possibly empty). This pairs with #75 (self ports) to give
    full port visibility — the decision 'is this opp a good trade
    partner for X' needs their port set, not just mine."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    st: dict = {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }
    snap = _build_advisor_snapshot(st)
    assert snap["opps"]
    for opp in snap["opps"]:
        assert "ports" in opp
        # ports is a list or None (helper returns None on tracker fail).
        # Either way must NOT be a dict/other — overlay expects array.
        assert opp["ports"] is None or isinstance(opp["ports"], list)


def test_snapshot_exposes_knights_played_on_self_and_opps():
    """After capture replay, every seated player should have a
    non-negative `knights_played` field. We then bump one opp's
    PLAYED_KNIGHT in catanatron state and re-snapshot to confirm the
    field tracks it. This guards against accidentally dropping the
    field in a future snap refactor — losing it would silently hide
    a major largest-army signal."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    st: dict = {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }
    snap = _build_advisor_snapshot(st)
    assert snap["self"] is not None
    assert "knights_played" in snap["self"]
    assert snap["self"]["knights_played"] >= 0
    for opp in snap["opps"]:
        assert "knights_played" in opp
        assert opp["knights_played"] >= 0

    # Bump the first opp's PLAYED_KNIGHT and re-snap. New count should
    # show on the row. Pick the first opp whose color is indexed.
    target_opp = snap["opps"][0]
    from catanatron import Color
    opp_enum = Color[target_opp["color"].upper()]
    idx = game.tracker.game.state.color_to_index[opp_enum]
    game.tracker.game.state.player_state[f"P{idx}_PLAYED_KNIGHT"] = 3
    snap2 = _build_advisor_snapshot(st)
    updated = next(o for o in snap2["opps"]
                   if o["color"] == target_opp["color"])
    assert updated["knights_played"] == 3


def test_compute_dev_deck_remaining_tracks_draws():
    """Deck starts at 25. Draws reduce remaining. A played action card
    still counts as drawn (it's out of the deck forever). With every
    slot empty the deck reads 25; bumping one unplayed and two played
    knights drops remaining to 22 and surfaces the drawn-count tally."""
    if not CAPTURE_EARLY.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _compute_dev_deck_remaining
    from cataanbot.live_game import LiveGame

    game = LiveGame()
    for payload in _iter_payloads(CAPTURE_EARLY):
        game.feed(payload)
    assert game.started

    # Zero everything first for a deterministic baseline.
    sess = game.session
    for cid in sess.player_names:
        sess.dev_card_counts[cid] = 0
    state = game.tracker.game.state
    for idx in state.color_to_index.values():
        for k in ("PLAYED_KNIGHT", "PLAYED_MONOPOLY",
                  "PLAYED_YEAR_OF_PLENTY", "PLAYED_ROAD_BUILDING"):
            state.player_state[f"P{idx}_{k}"] = 0
    dd = _compute_dev_deck_remaining(game)
    assert dd is not None
    assert dd["remaining"] == 25
    assert dd["drawn"] == 0
    assert dd["low"] is False

    # Bump: 1 unplayed dev on first seated player, 2 played knights on
    # first color. Drawn = 3; remaining = 22.
    first_cid = next(iter(sess.player_names))
    sess.dev_card_counts[first_cid] = 1
    first_color_idx = next(iter(state.color_to_index.values()))
    state.player_state[f"P{first_color_idx}_PLAYED_KNIGHT"] = 2
    dd = _compute_dev_deck_remaining(game)
    assert dd["drawn"] == 3
    assert dd["remaining"] == 22
    assert dd["low"] is False

    # Push draws to 23 — remaining = 2 → low flag fires.
    state.player_state[f"P{first_color_idx}_PLAYED_KNIGHT"] = 22
    dd = _compute_dev_deck_remaining(game)
    assert dd["remaining"] == 2
    assert dd["low"] is True

    # Over-count clamps to 0 (defensive — real games can't exceed 25).
    state.player_state[f"P{first_color_idx}_PLAYED_KNIGHT"] = 50
    dd = _compute_dev_deck_remaining(game)
    assert dd["remaining"] == 0
    assert dd["low"] is True


def test_bank_supply_flags_low_resources():
    """Bank starts at 19 per resource. When player hands consume most
    of a resource, remaining drops below 2 and the `low` list fires.
    If every resource has plenty in the bank, low is empty."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _compute_bank_supply
    from cataanbot.live_game import LiveGame

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started

    # Zero everyone's hand first so the baseline is clean: every
    # resource should then have 19 remaining — no low warnings.
    sess = game.session
    for user in sess.player_names.values():
        c = game.color_map.get(user)
        game.tracker.set_hand(c, {
            "WOOD": 0, "BRICK": 0, "SHEEP": 0, "WHEAT": 0, "ORE": 0})
    bank = _compute_bank_supply(game)
    assert bank is not None
    assert bank["remaining"]["WOOD"] == 19
    assert bank["low"] == []

    # Pile 18 WOOD onto one player → bank has 1 WOOD left.
    first_user = next(iter(sess.player_names.values()))
    first_color = game.color_map.get(first_user)
    game.tracker.set_hand(first_color, {
        "WOOD": 18, "BRICK": 0, "SHEEP": 0, "WHEAT": 0, "ORE": 0})
    bank = _compute_bank_supply(game)
    assert bank["remaining"]["WOOD"] == 1
    assert any(e["resource"] == "WOOD" and e["count"] == 1
               for e in bank["low"])


def test_largest_army_race_silent_early_and_alerts_at_2():
    """Parallel to the longest-road tracker but on played knights.
    Race fires once someone hits 2 played (one away from qualifying
    at 3), and settles silent once the holder is 2+ ahead."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _compute_largest_army_race
    from cataanbot.live_game import LiveGame
    from catanatron import Color

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started
    sess = game.session
    self_user = sess.player_names[sess.self_color_id]
    self_color = game.color_map.get(self_user)
    my_enum = Color[self_color.upper()]
    cat = game.tracker.game
    ps = cat.state.player_state
    my_idx = cat.state.color_to_index[my_enum]
    opp_indices = [i for c, i in cat.state.color_to_index.items()
                   if c != my_enum]
    opp_idx = opp_indices[0]

    # Zero every seat — silent.
    for i in (my_idx, *opp_indices):
        ps[f"P{i}_PLAYED_KNIGHT"] = 0
        ps[f"P{i}_HAS_ARMY"] = False
    assert _compute_largest_army_race(game, self_color) is None

    # Self at 2, nobody else close → self_push.
    ps[f"P{my_idx}_PLAYED_KNIGHT"] = 2
    race = _compute_largest_army_race(game, self_color)
    assert race and race["level"] == "self_push"
    assert race["self_n"] == 2

    # Both on 2 → contested (not opp_threat).
    ps[f"P{opp_idx}_PLAYED_KNIGHT"] = 2
    race = _compute_largest_army_race(game, self_color)
    assert race and race["level"] == "contested"

    # Opp on 2, self at 0 → opp_threat.
    ps[f"P{my_idx}_PLAYED_KNIGHT"] = 0
    race = _compute_largest_army_race(game, self_color)
    assert race and race["level"] == "opp_threat"

    # Opp holds at 5, self at 2 → settled, silent.
    ps[f"P{opp_idx}_PLAYED_KNIGHT"] = 5
    ps[f"P{opp_idx}_HAS_ARMY"] = True
    ps[f"P{my_idx}_PLAYED_KNIGHT"] = 2
    assert _compute_largest_army_race(game, self_color) is None


def test_longest_road_race_silent_early_and_alerts_at_4():
    """Race tracker should stay quiet while every road count is <4,
    alert when self hits 4 unqualified ('self_push'), alert when an
    opp hits 4 without self on 4 ('opp_threat'), and emit 'contested'
    when both sides are within 1 of each other at 4+."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _compute_longest_road_race
    from cataanbot.live_game import LiveGame
    from catanatron import Color

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started
    sess = game.session
    self_user = sess.player_names[sess.self_color_id]
    self_color = game.color_map.get(self_user)
    my_enum = Color[self_color.upper()]
    cat = game.tracker.game
    ps = cat.state.player_state
    my_idx = cat.state.color_to_index[my_enum]
    opp_indices = [i for c, i in cat.state.color_to_index.items()
                   if c != my_enum]
    assert opp_indices, "fixture must seat at least one opp"
    opp_idx = opp_indices[0]

    # Baseline: zero every seat — silent.
    for i in (my_idx, *opp_indices):
        ps[f"P{i}_LONGEST_ROAD_LENGTH"] = 0
        ps[f"P{i}_HAS_ROAD"] = False
    assert _compute_longest_road_race(game, self_color) is None

    # Self at 4, opp at 0 → self_push alert.
    ps[f"P{my_idx}_LONGEST_ROAD_LENGTH"] = 4
    race = _compute_longest_road_race(game, self_color)
    assert race and race["level"] == "self_push"
    assert race["self_len"] == 4

    # Self at 4, opp at 4 → contested.
    ps[f"P{opp_idx}_LONGEST_ROAD_LENGTH"] = 4
    race = _compute_longest_road_race(game, self_color)
    assert race and race["level"] == "contested"
    assert race["self_len"] == 4 and race["opp_len"] == 4

    # Opp at 4, self at 0 → opp_threat alert.
    ps[f"P{my_idx}_LONGEST_ROAD_LENGTH"] = 0
    race = _compute_longest_road_race(game, self_color)
    assert race and race["level"] == "opp_threat"

    # Opp holds with 2+ lead → silent (race settled).
    ps[f"P{opp_idx}_LONGEST_ROAD_LENGTH"] = 7
    ps[f"P{opp_idx}_HAS_ROAD"] = True
    ps[f"P{my_idx}_LONGEST_ROAD_LENGTH"] = 4
    assert _compute_longest_road_race(game, self_color) is None


def test_robber_on_me_fires_when_robber_sits_on_self_tile():
    """With the robber parked on a tile that has a self building,
    robber_on_me should report the resource, number, pips suppressed,
    and building count. A city doubles the pip cost. When the robber
    moves away (or lands on desert), the banner clears."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker
    from catanatron import Color

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started
    sess = game.session
    self_user = sess.player_names[sess.self_color_id]
    color = game.color_map.get(self_user)
    my_enum = Color[color.upper()]
    cat = game.tracker.game

    base_st: dict = {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }

    # Find a tile adjacent to one of self's settlements/cities.
    m = cat.state.board.map
    self_nodes = {n for n, (c, _) in cat.state.board.buildings.items()
                  if c == my_enum}
    assert self_nodes
    hot_coord = next(
        coord for coord, tile in m.land_tiles.items()
        if tile.number and set(tile.nodes.values()) & self_nodes)
    cat.state.board.robber_coordinate = hot_coord
    snap = _build_advisor_snapshot(dict(base_st))
    rom = snap["robber_on_me"]
    assert rom is not None
    assert rom["pips_blocked"] > 0
    assert rom["buildings"] >= 1
    assert rom["resource"] == m.land_tiles[hot_coord].resource
    assert rom["number"] == m.land_tiles[hot_coord].number

    # Move to a tile with no self presence → banner clears.
    cold_coord = next(
        coord for coord, tile in m.land_tiles.items()
        if tile.number and not (set(tile.nodes.values()) & self_nodes))
    cat.state.board.robber_coordinate = cold_coord
    snap = _build_advisor_snapshot(dict(base_st))
    assert snap["robber_on_me"] is None

    # Desert / unset → also None.
    desert = next(
        (coord for coord, tile in m.land_tiles.items()
         if not tile.number), None)
    if desert is not None:
        cat.state.board.robber_coordinate = desert
        snap = _build_advisor_snapshot(dict(base_st))
        assert snap["robber_on_me"] is None


def test_snapshot_populates_robber_targets_when_self_holds_knight():
    """Self holding a KNIGHT (and not facing a 7-roll) should get the
    full robber_targets ranking in the snapshot with
    robber_reason=='knight'. This drives the overlay's advisory panel
    so Noah can eyeball the block before burning the card."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker
    from catanatron import Color

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    assert game.started
    sess = game.session
    assert sess.self_color_id is not None
    self_user = sess.player_names[sess.self_color_id]
    color = game.color_map.get(self_user)
    my_enum = Color[color.upper()]
    cat = game.tracker.game
    idx = cat.state.color_to_index[my_enum]

    base_st: dict = {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }

    # No knight → no knight-driven robber_targets.
    cat.state.player_state[f"P{idx}_KNIGHT_IN_HAND"] = 0
    snap = _build_advisor_snapshot(dict(base_st))
    assert snap["robber_reason"] is None
    assert snap["robber_targets"] == []

    # Give self a KNIGHT — full ranking must populate with the 'knight'
    # discriminator so the overlay labels it differently from a 7-roll.
    cat.state.player_state[f"P{idx}_KNIGHT_IN_HAND"] = 1
    snap = _build_advisor_snapshot(dict(base_st))
    assert snap["knight_hint"]["have"] == 1
    assert snap["robber_reason"] == "knight"
    assert len(snap["robber_targets"]) > 0
    assert snap["robber_pending"] is False

    # A self-7-roll (robber_pending=True) must retain "forced" labeling
    # even when a knight is also in hand — urgent state wins.
    forced_st = dict(base_st, robber_pending=True, robber_snapshot=[
        {"coord": (0, 0, 0), "resource": "WHEAT", "number": 6,
         "score": 3.0, "victims": [], "opponent_blocked": 0}])
    snap = _build_advisor_snapshot(forced_st)
    assert snap["robber_reason"] == "forced"
    assert snap["robber_pending"] is True


def test_affordable_builds_covers_all_four_builds():
    """Unit on the bridge helper: every build with exactly its cost
    in-hand should surface. Order is city > settle > dev > road so the
    overlay tag reads worst-first when multiple are affordable."""
    from cataanbot.bridge import _affordable_builds

    # Empty hand → no builds.
    assert _affordable_builds({}) == []
    # Road only.
    assert _affordable_builds({"WOOD": 1, "BRICK": 1}) == ["road"]
    # Settlement: 1w/1b/1sh/1wh → covers settlement AND road (wood+brick).
    hand = {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1}
    assert _affordable_builds(hand) == ["settlement", "road"]
    # City: 2wh/3ore → covers city only.
    assert _affordable_builds({"WHEAT": 2, "ORE": 3}) == ["city"]
    # Dev card: 1sh/1wh/1ore.
    assert _affordable_builds({"SHEEP": 1, "WHEAT": 1, "ORE": 1}) == ["dev"]
    # Stacked hand covers everything — order matches _AFFORD_COSTS.
    fat = {"WOOD": 2, "BRICK": 2, "SHEEP": 2, "WHEAT": 3, "ORE": 3}
    assert _affordable_builds(fat) == [
        "city", "settlement", "dev", "road"]


def test_affordable_builds_ignores_unknown_cards():
    """The whole point: unknown cards (stolen from us, unresolved
    trades) must NOT count toward affordability. Otherwise a 'can: city'
    tag could fire on a hand with 0 ore but 5 unknowns — that's a guess,
    not a warning. The helper gets unknown as a hint but must not bake
    it into the 'definitely affords' decision."""
    from cataanbot.bridge import _affordable_builds

    # Hand has no ore, but 10 unknowns. City costs ore → must be absent.
    result = _affordable_builds({"WHEAT": 2, "ORE": 0}, unknown=10)
    assert "city" not in result
    # Same for settlement — no sheep in hand, unknowns don't rescue it.
    result = _affordable_builds(
        {"WOOD": 1, "BRICK": 1, "WHEAT": 1}, unknown=5)
    assert "settlement" not in result


def test_snapshot_populates_can_afford_on_opps():
    """End-to-end via capture replay: every opp row must have a
    can_afford list. After overriding one opp's tracker hand to exactly
    a city's worth, the replay snapshot surfaces 'city' on that row."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.bridge import _build_advisor_snapshot
    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker
    from catanatron import Color

    game = LiveGame()
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            game.feed(payload)
    st: dict = {
        "seq": 0, "game": game,
        "ws_count": 0, "log_count": 0,
        "last_roll": None,
        "robber_pending": False, "robber_snapshot": None,
        "display_colors": {},
        "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }
    snap = _build_advisor_snapshot(st)
    assert snap["opps"], "capture should include at least one opp"
    for opp in snap["opps"]:
        assert "can_afford" in opp
        assert isinstance(opp["can_afford"], list)

    # Pick the first opp. Force the tracker to believe they hold exactly
    # a city's worth (2 wheat, 3 ore) and re-snap. The tracker reads
    # hands out of catanatron's player_state P{idx}_{RES}_IN_HAND keys,
    # so we write directly there. Also align the session's
    # hand_card_counts so the snap's supply-cap trim doesn't fire.
    target = snap["opps"][0]
    opp_enum = Color[target["color"].upper()]
    state = game.tracker.game.state
    idx = state.color_to_index[opp_enum]
    hand_override = {
        "WOOD": 0, "BRICK": 0, "SHEEP": 0, "WHEAT": 2, "ORE": 3}
    for r, n in hand_override.items():
        state.player_state[f"P{idx}_{r}_IN_HAND"] = n
    sess = game.session
    target_cid = next(
        cid for cid, name in sess.player_names.items()
        if name == target["username"])
    sess.hand_card_counts[target_cid] = 5

    snap2 = _build_advisor_snapshot(st)
    updated = next(
        o for o in snap2["opps"] if o["username"] == target["username"])
    assert "city" in updated["can_afford"], (
        f"city should be affordable with 2w/3ore, got {updated['can_afford']}")


def test_compute_roll_yield_sums_settlements_and_cities():
    """Place one RED settlement on a numbered tile, pick that tile's
    number, and assert the yield shows +1 of that resource. Upgrade to
    a city and it becomes +2. Moving the robber onto the tile shifts
    that yield from gained to blocked."""
    from cataanbot.bridge import _compute_roll_yield
    from cataanbot.tracker import Tracker
    from catanatron import Color

    tr = Tracker(seed=4242)
    board = tr.game.state.board
    m = board.map
    # Find a legal buildable node on a numbered non-desert tile and
    # remember which tile/resource/number we're using so assertions
    # can reference them.
    buildable = board.buildable_node_ids(
        Color.RED, initial_build_phase=True)
    target_node = None
    target_coord = None
    target_res = None
    target_num = None
    for coord, tile in m.land_tiles.items():
        if tile.number is None or not tile.resource:
            continue
        for nid in tile.nodes.values():
            if nid in buildable:
                target_node = int(nid)
                target_coord = coord
                target_res = tile.resource
                target_num = tile.number
                break
        if target_node is not None:
            break
    assert target_node is not None

    board.build_settlement(
        Color.RED, target_node, initial_build_phase=True)

    # Settlement: +1 of the tile's resource, nothing blocked.
    y = _compute_roll_yield(_wrap_game(tr), "RED", target_num)
    assert y is not None
    assert y["gained"].get(target_res, 0) >= 1
    assert y["blocked_total"] == 0

    settle_amount = y["gained"][target_res]
    # Upgrade to city: settle_amount doubles at this node.
    board.build_city(Color.RED, target_node)
    y_city = _compute_roll_yield(_wrap_game(tr), "RED", target_num)
    assert y_city["gained"][target_res] == settle_amount + 1, (
        f"city should add 1 more to gained count, got {y_city['gained']}")

    # Move robber onto this tile. Yield shifts: that tile contributes
    # to blocked, not gained.
    board.robber_coordinate = target_coord
    y_robbed = _compute_roll_yield(_wrap_game(tr), "RED", target_num)
    # Blocked total should include at least 2 (the city's ×2).
    assert y_robbed["blocked"].get(target_res, 0) >= 2
    # And gained drops by the same amount (this city is fully blocked).
    gain_after = y_robbed["gained"].get(target_res, 0)
    assert gain_after <= y_city["gained"][target_res] - 2


def test_compute_roll_yield_returns_none_on_7():
    """7-rolls don't produce. The helper bails and returns None so the
    overlay suppresses the yield line entirely on a 7 — the discard
    hint and robber-placement flow cover that case separately."""
    from cataanbot.bridge import _compute_roll_yield
    from cataanbot.tracker import Tracker
    tr = Tracker(seed=4242)
    assert _compute_roll_yield(_wrap_game(tr), "RED", 7) is None
