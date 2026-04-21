"""Tests for the colonist → Event extractor and the WS dispatcher path."""

from __future__ import annotations

from pathlib import Path

import pytest

from cataanbot.colonist_diff import (
    LiveSession, LiveSessionError,
    events_from_diff, events_from_frame_payload,
)
from cataanbot.colonist_proto import load_capture
from cataanbot.events import BuildEvent, RobberMoveEvent
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


def test_empty_or_unrelated_diff_emits_nothing():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    assert events_from_diff(sess, {}) == []
    assert events_from_diff(sess, {"diceState": {"dice1": 3, "dice2": 4}}) == []


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
