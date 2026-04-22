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
