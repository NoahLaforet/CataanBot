"""Tests for the colonist → Event extractor and the WS dispatcher path."""

from __future__ import annotations

from pathlib import Path

import pytest

from cataanbot.colonist_diff import (
    LiveSession, LiveSessionError,
    events_from_diff, events_from_frame_payload, produce_events_for_roll,
)
from cataanbot.colonist_proto import load_capture
from cataanbot.events import (
    BuildEvent, ProduceEvent, RobberMoveEvent, RollEvent, VPEvent,
)
from cataanbot.live import ColorMap, apply_event
from cataanbot.tracker import Tracker


CAPTURE_EARLY = (Path(__file__).parent.parent
                 / "ws_captures"
                 / "cataanbot-ws-fort4092-early-2026-04-21T23-23-22.json")
CAPTURE_MID = (Path(__file__).parent.parent
               / "ws_captures"
               / "cataanbot-ws-fort4092-midgame-2026-04-21T23-34-04.json")


def _game_start_body(path: Path) -> dict:
    if not path.exists():
        pytest.skip(f"live capture not present at {path}")
    frames = list(load_capture(path))
    gs = next(f for f in frames if f.raw_length == 5156)
    return gs.payload["payload"]


# ---------------------------------------------------------------------------
# LiveSession construction
# ---------------------------------------------------------------------------

def test_from_game_start_resolves_usernames():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    # fort4092 saw BrickdDaddy (color 5), Elissa (1), Vtarj (2)
    assert sess.player_for(5) == "BrickdDaddy"
    assert sess.player_for(1) == "Elissa"
    assert sess.player_for(2) == "Vtarj"
    # Unknown color id fallbacks to a stable placeholder.
    assert sess.player_for(99) == "player99"


def test_from_game_start_requires_map_state():
    with pytest.raises(LiveSessionError):
        LiveSession.from_game_start({"playerUserStates": []})


def test_from_game_start_seeds_existing_placements():
    body = _game_start_body(CAPTURE_EARLY)
    # Mutate a corner as if a settlement already sits there.
    game_state = {**body["gameState"], "mapState": {**body["gameState"]["mapState"]}}
    corners = dict(game_state["mapState"]["tileCornerStates"])
    some_cid = next(iter(corners))
    corners[some_cid] = {**corners[some_cid], "owner": 5, "buildingType": 1}
    game_state["mapState"]["tileCornerStates"] = corners
    body = {**body, "gameState": game_state}

    sess = LiveSession.from_game_start(body)
    # Replaying the same state as a diff shouldn't emit a build event —
    # we already knew about it.
    events = events_from_diff(sess, {"mapState": {
        "tileCornerStates": {some_cid: {"owner": 5, "buildingType": 1}}}})
    assert events == []


# ---------------------------------------------------------------------------
# Diff → Event translation
# ---------------------------------------------------------------------------

def test_diff_settlement_becomes_build_event_with_node_id():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    pick_cid = next(cid for cid in sess.mapping.node_id
                    if cid not in sess.known_corners)
    events = events_from_diff(sess, {"mapState": {
        "tileCornerStates": {
            str(pick_cid): {"owner": 5, "buildingType": 1}}}})
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, BuildEvent)
    assert ev.piece == "settlement"
    assert ev.player == "BrickdDaddy"
    assert ev.node_id == sess.mapping.node_id[pick_cid]


def test_diff_city_upgrade_emits_city_event():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    pick_cid = next(iter(sess.mapping.node_id))
    # First a settlement, then the upgrade.
    events_from_diff(sess, {"mapState": {
        "tileCornerStates": {
            str(pick_cid): {"owner": 1, "buildingType": 1}}}})
    events = events_from_diff(sess, {"mapState": {
        "tileCornerStates": {
            str(pick_cid): {"owner": 1, "buildingType": 2}}}})
    assert [e.piece for e in events] == ["city"]
    assert events[0].node_id == sess.mapping.node_id[pick_cid]


def test_diff_road_becomes_build_event_with_edge_nodes():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    pick_eid = next(eid for eid in sess.mapping.edge_nodes
                    if eid not in sess.known_edges)
    events = events_from_diff(sess, {"mapState": {
        "tileEdgeStates": {
            str(pick_eid): {"owner": 2, "type": 1}}}})
    assert len(events) == 1
    ev = events[0]
    assert ev.piece == "road"
    assert ev.player == "Vtarj"
    pair = sess.mapping.edge_nodes[pick_eid]
    assert set(ev.edge_nodes) == set(pair)


def test_diff_robber_becomes_move_event_with_coord():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    any_tid = next(iter(sess.mapping.tile_coord))
    events = events_from_diff(sess, {
        "mechanicRobberState": {"locationTileIndex": any_tid}})
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, RobberMoveEvent)
    assert ev.coord == sess.mapping.tile_coord[any_tid]


def test_diff_dice_roll_emits_roll_event():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    events = events_from_diff(sess, {
        "diceState": {"dice1": 3, "dice2": 4, "diceThrown": True},
        "currentState": {"currentTurnPlayerColor": 5},
    })
    assert any(isinstance(e, RollEvent) for e in events)
    roll = next(e for e in events if isinstance(e, RollEvent))
    assert (roll.d1, roll.d2) == (3, 4)
    assert roll.player == "BrickdDaddy"


def test_diff_without_fresh_dice_emits_no_roll():
    """diceThrown: False alone (roll-consumed frame) isn't a new roll."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    events = events_from_diff(sess, {
        "diceState": {"diceThrown": False}})
    assert not any(isinstance(e, RollEvent) for e in events)


def test_produce_events_for_roll_skips_robber_tile():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    # Put a settlement on the first corner of some tile that rolls on 6.
    six_tid = next(tid for tid, d in sess.mapping.tile_dice.items()
                   if d == 6)
    any_cid = next(iter(sess.mapping.tile_corners[six_tid]))
    sess.known_corners[any_cid] = 1
    sess.corner_owners[any_cid] = 5

    got = produce_events_for_roll(sess, 6)
    assert got and got[0].player == "BrickdDaddy"

    sess.robber_tile_id = six_tid
    # With the robber on the only 6-tile that corner touches, yields may
    # still appear from *other* 6-tiles if the corner is at a junction,
    # but the robbed tile itself never contributes — so the count drops.
    without_robber = sum(v for ev in produce_events_for_roll(sess, 6)
                         for v in ev.resources.values())
    assert without_robber < sum(v for ev in got for v in ev.resources.values())


def test_produce_events_for_roll_handles_seven():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    assert produce_events_for_roll(sess, 7) == []


def test_produce_events_for_roll_skips_self_player():
    """Once we've latched onto the self-player's color id, their yield
    must NOT be emitted as a ProduceEvent — HandSync from the resource-
    cards snapshot already captures it absolutely, so a delta on top
    would double-count. Opponents still get their deltas."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    six_tid = next(tid for tid, d in sess.mapping.tile_dice.items()
                   if d == 6)
    corners = list(sess.mapping.tile_corners[six_tid])
    # Self player's corner (latched below).
    sess.known_corners[corners[0]] = 1
    sess.corner_owners[corners[0]] = 5  # BrickdDaddy
    # Opponent's corner on the same tile.
    sess.known_corners[corners[1]] = 1
    sess.corner_owners[corners[1]] = 1  # Elissa

    sess.self_color_id = 5
    events = produce_events_for_roll(sess, 6)
    players = {ev.player for ev in events}
    assert "BrickdDaddy" not in players, (
        f"self-player leaked into produce events: {players}")
    assert "Elissa" in players, (
        "opponent yield should still be emitted")


def test_events_from_frame_payload_emits_roll_plus_produce():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    six_tid = next(tid for tid, d in sess.mapping.tile_dice.items() if d == 6)
    any_cid = next(iter(sess.mapping.tile_corners[six_tid]))
    sess.known_corners[any_cid] = 1
    sess.corner_owners[any_cid] = 1  # Elissa

    events = events_from_frame_payload(sess, {
        "type": 91,
        "payload": {"diff": {
            "diceState": {"dice1": 3, "dice2": 3, "diceThrown": True},
            "currentState": {"currentTurnPlayerColor": 1},
        }},
        "sequence": 1,
    })
    assert any(isinstance(e, RollEvent) for e in events)
    assert any(isinstance(e, ProduceEvent) and e.player == "Elissa"
               for e in events)


def test_empty_or_unrelated_diff_emits_nothing():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    assert events_from_diff(sess, {}) == []
    # A roll-consumed frame carries only diceThrown=False, not a fresh
    # pair of dice values — no new RollEvent.
    assert events_from_diff(
        sess, {"diceState": {"diceThrown": False}}) == []


def test_events_from_frame_payload_filters_non_diff_frames():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    # Not a type=91 frame.
    assert events_from_frame_payload(sess, {"type": 4, "payload": {}}) == []
    # Type=91 but empty diff.
    assert events_from_frame_payload(
        sess, {"type": 91, "payload": {"diff": {}}, "sequence": 1}) == []


# ---------------------------------------------------------------------------
# Dispatcher: BuildEvent / RobberMoveEvent with topology fields → tracker
# ---------------------------------------------------------------------------

def test_build_event_with_node_id_settles_on_tracker():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    node_id = next(iter(t.game.state.board.map.land_nodes))
    result = apply_event(t, cm, BuildEvent(
        player="Alice", piece="settlement", node_id=node_id))
    assert result.status == "applied"
    assert t.game.state.board.buildings[node_id][1] == "SETTLEMENT"


def test_build_event_city_upgrades_on_tracker():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    node_id = next(iter(t.game.state.board.map.land_nodes))
    apply_event(t, cm, BuildEvent(
        player="Alice", piece="settlement", node_id=node_id))
    result = apply_event(t, cm, BuildEvent(
        player="Alice", piece="city", node_id=node_id))
    assert result.status == "applied"
    assert t.game.state.board.buildings[node_id][1] == "CITY"


def test_build_event_road_places_road_on_tracker():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    board_map = t.game.state.board.map
    # Pick any land tile and grab an incident edge as (a, b) node ids.
    land_coord = next(iter(board_map.land_tiles))
    tile = board_map.land_tiles[land_coord]
    _, (a, b) = next(iter(tile.edges.items()))
    apply_event(t, cm, BuildEvent(
        player="Alice", piece="settlement", node_id=a))
    result = apply_event(t, cm, BuildEvent(
        player="Alice", piece="road", edge_nodes=(a, b)))
    assert result.status == "applied"
    assert (a, b) in t.game.state.board.roads \
        or (b, a) in t.game.state.board.roads


def test_robber_move_event_with_coord_updates_tracker():
    t = Tracker()
    cm = ColorMap()
    land_coord = next(iter(t.game.state.board.map.land_tiles))
    result = apply_event(t, cm, RobberMoveEvent(
        player="", tile_label="", prob=None, coord=land_coord))
    assert result.status == "applied"
    assert t.game.state.board.robber_coordinate == land_coord


def test_build_event_without_topology_stays_unhandled():
    """Regression guard for the DOM-parse code path."""
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    result = apply_event(t, cm, BuildEvent(
        player="Alice", piece="settlement"))
    assert result.status == "unhandled"


# ---------------------------------------------------------------------------
# End-to-end: stream every type=91 frame from the midgame capture and
# make sure every produced event applies cleanly to the tracker.
# ---------------------------------------------------------------------------

def test_midgame_capture_streams_into_tracker_without_errors():
    if not CAPTURE_MID.exists():
        pytest.skip(f"midgame capture not present at {CAPTURE_MID}")

    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    tracker = Tracker()
    cm = ColorMap()

    # Seed tracker with the initial placements we snapshotted in
    # known_corners/known_edges so the midgame diffs play out against a
    # realistic board.
    map_state = _game_start_body(CAPTURE_EARLY)["gameState"]["mapState"]
    for cid_str, c in map_state["tileCornerStates"].items():
        bt = int(c.get("buildingType") or 0)
        owner = c.get("owner")
        if not bt or owner is None:
            continue
        node_id = sess.mapping.node_id[int(cid_str)]
        player = sess.player_for(int(owner))
        color = cm.get(player)
        tracker.settle(color, node_id)
        if bt == 2:
            tracker.city(color, node_id)
    for eid_str, e in map_state["tileEdgeStates"].items():
        owner = e.get("owner")
        if not owner:
            continue
        pair = sess.mapping.edge_nodes[int(eid_str)]
        a, b = tuple(pair)
        player = sess.player_for(int(owner))
        color = cm.get(player)
        try:
            tracker.road(color, a, b)
        except Exception:
            # Setup roads sometimes fail catanatron's connectivity check
            # when replayed out of order; skip silently for this smoke
            # test — the diff stream is what we're actually validating.
            pass

    applied = unhandled = errored = 0
    frames = list(load_capture(CAPTURE_MID))
    for frame in frames:
        if frame.error:
            continue
        for event in events_from_frame_payload(sess, frame.payload or {}):
            result = apply_event(tracker, cm, event)
            if result.status == "applied":
                applied += 1
            elif result.status == "error":
                errored += 1
            else:
                unhandled += 1

    assert applied > 0, "expected at least one applied build/robber event"
    assert errored == 0, (
        f"{errored} events raised tracker errors in midgame replay")


def _minimal_session() -> LiveSession:
    """LiveSession wired with stable per-color usernames — enough for the
    diff extractor to attribute VPEvents without needing a full capture."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    return sess


def test_diff_emits_longest_road_vpevent_on_has_flag_true():
    """``hasLongestRoad: true`` appearing on a color for the first time
    must produce a VPEvent(reason='longest_road') with that color as
    the new player. Without this, the tracker never flips HAS_ROAD
    true for the authoritative award and the +2 VP is invisible on
    the HUD even though colonist itself painted the bonus."""
    sess = _minimal_session()
    diff = {
        "mechanicLongestRoadState": {
            "1": {"longestRoad": 5, "hasLongestRoad": True},
        },
    }
    events = events_from_diff(sess, diff)
    vp_events = [e for e in events if isinstance(e, VPEvent)]
    assert len(vp_events) == 1, f"expected 1 VPEvent, got {events}"
    ev = vp_events[0]
    assert ev.reason == "longest_road"
    assert ev.player == sess.player_for(1)
    assert ev.previous_holder is None
    assert sess.has_longest_road_cid == 1


def test_diff_emits_largest_army_vpevent_with_previous_holder_on_transfer():
    """When LA transfers, colonist sets ``hasLargestArmy: true`` on the
    new holder; we must emit a VPEvent that names the *old* holder as
    previous_holder so ``_apply_vp`` strips HAS_ARMY from them before
    granting it — otherwise both flags end up true simultaneously and
    the VP counter double-counts."""
    sess = _minimal_session()
    sess.has_largest_army_cid = 2  # pretend Vtarj already held it
    diff = {
        "mechanicLargestArmyState": {
            "5": {"hasLargestArmy": True},
        },
    }
    events = events_from_diff(sess, diff)
    vp_events = [e for e in events if isinstance(e, VPEvent)]
    assert len(vp_events) == 1
    ev = vp_events[0]
    assert ev.reason == "largest_army"
    assert ev.player == sess.player_for(5)
    assert ev.previous_holder == sess.player_for(2)
    assert sess.has_largest_army_cid == 5


def test_diff_skips_vpevent_when_already_current_holder():
    """A later diff that re-ships ``hasLongestRoad: true`` on the same
    cid (e.g. the player keeps extending their road) must NOT emit a
    duplicate VPEvent — the bonus was already applied on the first
    transition, and double-firing would add another +2 VP each road."""
    sess = _minimal_session()
    sess.has_longest_road_cid = 1
    diff = {
        "mechanicLongestRoadState": {
            "1": {"longestRoad": 6, "hasLongestRoad": True},
        },
    }
    events = events_from_diff(sess, diff)
    assert not [e for e in events if isinstance(e, VPEvent)]


def test_diff_vpevent_wires_into_tracker_vp_with_bonus():
    """End-to-end: a diff awarding longest_road to a color whose
    internal road count is *below* 5 must still leave that color with
    HAS_ROAD=True and VP credited +2 after ``apply_event`` runs. This
    is the exact live bug — our tracker's own road count can lag
    colonist, and the HUD was losing the +2 because _recompute_longest
    _road was stripping the flag on the next build."""
    sess = _minimal_session()
    tracker = Tracker()
    # Start fresh — no roads on the board, nobody holds longest road.
    cm = ColorMap()
    # Pre-register so apply_event doesn't tack on a new color.
    cm.get(sess.player_for(1))

    diff = {
        "mechanicLongestRoadState": {
            "1": {"longestRoad": 5, "hasLongestRoad": True},
        },
    }
    for ev in events_from_diff(sess, diff):
        apply_event(tracker, cm, ev)

    color = cm.get(sess.player_for(1))
    state = tracker.game.state
    idx = state.color_to_index[tracker._color(color)]
    assert state.player_state[f"P{idx}_HAS_ROAD"] is True
    assert state.player_state[f"P{idx}_VICTORY_POINTS"] == 2, (
        f"VP should include +2 for longest road, got "
        f"{state.player_state[f'P{idx}_VICTORY_POINTS']}")


def test_tracker_recompute_longest_road_does_not_strip_without_displacer():
    """Regression for the stripping bug: if a color holds HAS_ROAD (set
    via VPEvent) and our internal road count is 0 (missed road diffs),
    a subsequent _recompute_longest_road must leave HAS_ROAD alone. In
    the old logic, the absence of any qualifier set new_holder=None and
    the code stripped the existing flag — silently wiping the +2 VP
    every time any build happened."""
    from catanatron import Color

    tracker = Tracker()
    state = tracker.game.state
    idx = state.color_to_index[Color.RED]
    state.player_state[f"P{idx}_HAS_ROAD"] = True

    tracker._recompute_longest_road()
    assert state.player_state[f"P{idx}_HAS_ROAD"] is True, (
        "HAS_ROAD was stripped despite no displacer — colonist's award "
        "would be silently erased")


def test_tracker_recompute_largest_army_preserves_flag_under_undercount():
    """Same guarantee for largest army: if HAS_ARMY is true from a
    colonist VPEvent but our PLAYED_KNIGHT counter is below the
    threshold (missed ``used [knight]`` DOM log lines), recompute must
    not strip the flag."""
    from catanatron import Color

    tracker = Tracker()
    state = tracker.game.state
    idx = state.color_to_index[Color.RED]
    state.player_state[f"P{idx}_HAS_ARMY"] = True

    tracker._recompute_largest_army()
    assert state.player_state[f"P{idx}_HAS_ARMY"] is True


def test_tracker_recompute_longest_road_still_transfers_on_real_displacer():
    """The conservative recompute must not become a black hole — when
    another color's actual internal road count strictly exceeds the
    current holder's, HAS_ROAD must move. This protects the replay /
    offline analysis path (no VPEvent stream) from freezing on the
    first holder forever."""
    from catanatron import Color

    tracker = Tracker()
    state = tracker.game.state
    # Current holder is RED per prior VPEvent but their internal count
    # will show 0 — BLUE builds a continuous 6-road and should displace.
    red_idx = state.color_to_index[Color.RED]
    blue_idx = state.color_to_index[Color.BLUE]
    state.player_state[f"P{red_idx}_HAS_ROAD"] = True

    # Stub in per-color lengths without actually building — patch the
    # board method so we can drive the recompute deterministically.
    tracker.game.state.board.continuous_roads_by_player = lambda c: (
        [[1, 2, 3, 4, 5, 6]] if c == Color.BLUE else [])
    tracker._recompute_longest_road()
    assert state.player_state[f"P{red_idx}_HAS_ROAD"] is False
    assert state.player_state[f"P{blue_idx}_HAS_ROAD"] is True
