"""Tests for the colonist → catanatron topology mapping."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cataanbot.colonist_map import (
    COLONIST_PORT_RESOURCE, COLONIST_TILE_RESOURCE,
    MapMapping, MapMappingError, axial_to_cube,
    build_catanatron_map_from_colonist,
    build_mapping, corner_tile_signature, edge_endpoint_signatures,
    port_resource, tile_resource,
)


# ---- Geometry primitives ---------------------------------------------------

def test_axial_to_cube_preserves_invariant():
    for ax in range(-2, 3):
        for ay in range(-2, 3):
            x, y, z = axial_to_cube(ax, ay)
            assert x + y + z == 0
            assert (x, y) == (ax, ay)


def test_corner_signature_z0_and_z1_match_adjacency():
    # z=0 (NORTH) corner of (0, 0) sits between (0,0), (0,-1), (1,-1).
    assert corner_tile_signature(0, 0, 0) == frozenset(
        [(0, 0), (0, -1), (1, -1)])
    # z=1 (SOUTH) corner of (0, 0) sits between (0,0), (0,1), (-1,1).
    assert corner_tile_signature(0, 0, 1) == frozenset(
        [(0, 0), (0, 1), (-1, 1)])


def test_edge_endpoints_all_three_z_slots():
    # NW edge endpoints are NORTH and NORTHWEST corners.
    n, nw = edge_endpoint_signatures(0, 0, 0)
    assert n == corner_tile_signature(0, 0, 0)
    # NORTHWEST corner of (0,0) == SOUTH corner of (0,-1).
    assert nw == corner_tile_signature(0, -1, 1)

    # W edge: NORTHWEST and SOUTHWEST.
    a, b = edge_endpoint_signatures(0, 0, 1)
    assert a == corner_tile_signature(0, -1, 1)
    assert b == corner_tile_signature(-1, 1, 0)

    # SW edge: SOUTHWEST and SOUTH.
    a, b = edge_endpoint_signatures(0, 0, 2)
    assert a == corner_tile_signature(-1, 1, 0)
    assert b == corner_tile_signature(0, 0, 1)


def test_edge_endpoints_rejects_unknown_z():
    with pytest.raises(ValueError):
        edge_endpoint_signatures(0, 0, 3)


# ---- Full mapping from a live capture --------------------------------------

CAPTURE_PATH = (Path(__file__).parent.parent
                / "ws_captures"
                / "cataanbot-ws-fort4092-early-2026-04-21T23-23-22.json")


def _load_fort4092_map_state() -> dict:
    """Pull the GameStart mapState from the live fort4092 capture.

    Skipped cleanly if the capture dump isn't on disk (ws_captures/ is
    gitignored, so CI won't have it).
    """
    if not CAPTURE_PATH.exists():
        pytest.skip(f"live capture not present at {CAPTURE_PATH}")
    from cataanbot.colonist_proto import load_capture
    frames = list(load_capture(CAPTURE_PATH))
    gs = next(f for f in frames if f.raw_length == 5156)
    return gs.payload["payload"]["gameState"]["mapState"]


def test_build_mapping_from_live_capture_is_bijective():
    m = build_mapping(_load_fort4092_map_state())
    assert len(m.tile_coord) == 19
    assert len(m.node_id) == 54
    assert len(m.edge_nodes) == 72
    assert len(m.port_edges) == 9
    # Injectivity
    assert len(set(m.node_id.values())) == 54
    assert len(set(m.edge_nodes.values())) == 72


def test_mapped_edges_are_catanatron_edges():
    from catanatron.models.map import BASE_MAP_TEMPLATE, CatanMap
    m = build_mapping(_load_fort4092_map_state())
    cat_map = CatanMap.from_template(BASE_MAP_TEMPLATE)
    all_edges = set()
    for tile in cat_map.tiles.values():
        if hasattr(tile, "edges"):
            for _, (a, b) in tile.edges.items():
                all_edges.add(frozenset({a, b}))
    for eid, pair in m.edge_nodes.items():
        assert pair in all_edges, f"edge {eid} {sorted(pair)} not in catanatron"


def test_ports_are_subset_of_edges():
    m = build_mapping(_load_fort4092_map_state())
    edge_pairs = set(m.edge_nodes.values())
    for pid, pair in m.port_edges.items():
        assert pair in edge_pairs, f"port {pid} not on a known edge"


def test_port_type_distribution_matches_base_catan():
    m = build_mapping(_load_fort4092_map_state())
    from collections import Counter
    counts = Counter(m.port_types.values())
    # base Catan: 4 generic (type 1), 5 resource-specific (types 2..6)
    assert counts[1] == 4
    assert sum(v for k, v in counts.items() if k >= 2) == 5


def test_tile_type_distribution_matches_base_catan():
    m = build_mapping(_load_fort4092_map_state())
    from collections import Counter
    type_counts = Counter(m.tile_types.values())
    # 1 desert, 4+4+4+3+3 resource mix
    assert type_counts[0] == 1                # desert
    non_desert = sorted(
        [v for k, v in type_counts.items() if k != 0], reverse=True)
    assert non_desert == [4, 4, 4, 3, 3]


# ---- Error paths -----------------------------------------------------------

# ---- Resource mapping ------------------------------------------------------

def test_tile_resource_covers_desert_and_five_resources():
    assert tile_resource(0) is None
    assert {tile_resource(i) for i in range(1, 6)} == {
        "WOOD", "BRICK", "SHEEP", "WHEAT", "ORE"}


def test_port_resource_covers_generic_and_five_resources():
    assert port_resource(1) is None
    assert {port_resource(i) for i in range(2, 7)} == {
        "WOOD", "BRICK", "SHEEP", "WHEAT", "ORE"}


def test_tile_resource_rejects_unknown():
    with pytest.raises(ValueError):
        tile_resource(9)


def test_tile_resource_distribution_matches_base_catan():
    """Under the assumed mapping, fort4092's tiles give base-Catan counts."""
    from collections import Counter
    m = build_mapping(_load_fort4092_map_state())
    counts = Counter(tile_resource(t) for t in m.tile_types.values())
    assert counts == {
        None: 1, "WOOD": 4, "BRICK": 3, "SHEEP": 4, "WHEAT": 4, "ORE": 3}


MIDGAME_PATH = (Path(__file__).parent.parent
                / "ws_captures"
                / "cataanbot-ws-fort4092-midgame-2026-04-21T23-34-04.json")


def _live_session():
    from cataanbot.colonist_diff import LiveSession
    from cataanbot.colonist_proto import load_capture
    body = next(f.payload["payload"] for f in load_capture(CAPTURE_PATH)
                if isinstance(f.payload, dict)
                and f.payload.get("type") == 4)
    return LiveSession.from_game_start(body)


def _iter_diffs(path):
    """Yield each type=91 diff body from a capture, with a running
    snapshot of the previous bank state so callers can compute deltas."""
    from cataanbot.colonist_proto import load_capture
    for frame in load_capture(path):
        if frame.error:
            continue
        p = frame.payload
        if not isinstance(p, dict) or p.get("type") != 91:
            continue
        diff = (p.get("payload") or {}).get("diff") or {}
        if diff:
            yield diff


def test_tile_int_matches_card_int_across_rolls():
    """For every fresh roll, the bank-state resource ints that changed
    are a subset of the tile.type ints on rolled hexes. Proves the
    int-level mapping is consistent between tiles, cards, and bank."""
    if not CAPTURE_PATH.exists() or not MIDGAME_PATH.exists():
        pytest.skip("live captures not present")
    sess = _live_session()

    tiles_by_dice: dict[int, set[int]] = {}
    for tid, dice in sess.mapping.tile_dice.items():
        if dice:
            tiles_by_dice.setdefault(dice, set()).add(
                sess.mapping.tile_types[tid])

    verified = 0
    for diff in _iter_diffs(MIDGAME_PATH):
        dice_state = diff.get("diceState") or {}
        # Strict filter: only the frame that carries the fresh roll
        # itself has both dice1 and dice2 in its diff.
        if "dice1" not in dice_state or "dice2" not in dice_state:
            continue
        total = dice_state["dice1"] + dice_state["dice2"]
        if total == 7 or total not in tiles_by_dice:
            continue
        bank_rc = (diff.get("bankState") or {}).get("resourceCards") or {}
        if not bank_rc:
            continue
        rolled_types = tiles_by_dice[total]
        for res_key in bank_rc:
            assert int(res_key) in rolled_types, (
                f"roll {total}: bank delta touched type {res_key}, "
                f"but rolled tiles only have types {sorted(rolled_types)}")
            verified += 1
    assert verified > 0, "expected at least one roll delivery to verify"


def test_build_costs_fix_resource_names():
    """Validate the absolute name mapping against known Catan build costs.

    Roads cost {WOOD, BRICK}; a road-only frame's bank delta always
    grows exactly in types {1, 2}. Cities cost {2 WHEAT, 3 ORE}; their
    delta is always {4: +2, 5: +3}. Dev-card buys cost {SHEEP, WHEAT,
    ORE}; their delta is {3: +1, 4: +1, 5: +1}. Only one assignment of
    names to ints satisfies all three simultaneously, and it matches
    COLONIST_TILE_RESOURCE."""
    if not CAPTURE_PATH.exists() or not MIDGAME_PATH.exists():
        pytest.skip("live captures not present")
    sess = _live_session()

    initial_corners = set(
        sess.known_corners.keys())
    initial_edges = set(sess.known_edges.keys())

    # Seed prev-bank from GameStart.
    from cataanbot.colonist_proto import load_capture
    gs_body = next(f.payload["payload"] for f in load_capture(CAPTURE_PATH)
                   if isinstance(f.payload, dict)
                   and f.payload.get("type") == 4)
    prev_bank = {
        int(k): v for k, v in
        gs_body["gameState"]["bankState"]["resourceCards"].items()
    }

    road_positive_types: set[int] = set()
    city_positive_counts: dict[int, set[int]] = {}  # type → set of deltas seen
    devbuy_positive_types: set[int] = set()

    known_bt: dict[int, int] = {}  # cid -> buildingType seen

    for diff in _iter_diffs(MIDGAME_PATH):
        bank_rc = (diff.get("bankState") or {}).get("resourceCards") or {}
        if not bank_rc:
            continue
        ms = diff.get("mapState") or {}
        corners = ms.get("tileCornerStates") or {}
        edges = ms.get("tileEdgeStates") or {}

        new_cities = []
        for cid, c in corners.items():
            cid_i = int(cid)
            bt = c.get("buildingType")
            prev = known_bt.get(cid_i, 1 if cid_i in initial_corners else 0)
            if bt == 2 and prev == 1:
                new_cities.append(cid_i)
            if bt:
                known_bt[cid_i] = bt

        new_roads = [int(e) for e in edges if int(e) not in initial_edges]
        for e in new_roads:
            initial_edges.add(e)

        dev_bought = "mechanicDevelopmentCardsState" in diff
        dice_active = "dice1" in (diff.get("diceState") or {})

        deltas = {int(k): v - prev_bank.get(int(k), 0)
                  for k, v in bank_rc.items()}
        prev_bank.update({int(k): v for k, v in bank_rc.items()})
        positive = {k: v for k, v in deltas.items() if v > 0}
        if not positive or dice_active:
            continue

        if new_cities and set(positive) == {4, 5}:
            for k, v in positive.items():
                city_positive_counts.setdefault(k, set()).add(v)
        elif new_roads and not dev_bought:
            # Road-only frames: exactly {1: 1, 2: 1}.
            if positive == {1: 1, 2: 1}:
                road_positive_types.update(positive)
        elif dev_bought and set(positive) == {3, 4, 5}:
            devbuy_positive_types.update(positive)

    # Road cost = WOOD + BRICK  → types {1, 2}
    assert road_positive_types == {1, 2}
    # City cost = 2 WHEAT + 3 ORE. Type 4 always returned 2, type 5 always 3.
    assert city_positive_counts.get(4) == {2}, "type 4 should refund 2 (WHEAT)"
    assert city_positive_counts.get(5) == {3}, "type 5 should refund 3 (ORE)"
    # Dev card = SHEEP + WHEAT + ORE → types {3, 4, 5}
    assert devbuy_positive_types == {3, 4, 5}

    # Together: 4=WHEAT, 5=ORE (city); dev-buy + city fixes 3=SHEEP;
    # road cost leaves {1, 2} = {WOOD, BRICK}; fort4092 type-count
    # distribution (4,3,4,4,3) then pins 1=WOOD (4 tiles) and 2=BRICK
    # (3 tiles). The constant table encodes exactly that.
    assert tile_resource(4) == "WHEAT"
    assert tile_resource(5) == "ORE"
    assert tile_resource(3) == "SHEEP"
    assert tile_resource(1) == "WOOD"
    assert tile_resource(2) == "BRICK"


def test_port_ratio_changes_match_port_resource_offset():
    """When a player first builds onto a 2:1 port, their
    bankTradeRatiosState flips exactly one resource key to 2. That key
    is the colonist resource int (1..5) for the port's resource, which
    must match ``COLONIST_PORT_RESOURCE[port_type]`` for the port they
    settled on."""
    if not CAPTURE_PATH.exists() or not MIDGAME_PATH.exists():
        pytest.skip("live captures not present")
    verified = 0
    for diff in _iter_diffs(MIDGAME_PATH):
        ps_diff = diff.get("playerStates") or {}
        for _pid, pdiff in ps_diff.items():
            ratios = pdiff.get("bankTradeRatiosState") or {}
            # Only look at single-resource 2:1 changes.
            two_for_ones = {int(k) for k, v in ratios.items() if v == 2}
            if len(two_for_ones) == 1:
                res_int = next(iter(two_for_ones))
                # port offset: port_type = res_int + 1
                assert port_resource(res_int + 1) == tile_resource(res_int)
                verified += 1
    assert verified >= 1, "expected at least one 2:1 port claim"


# ---- Error paths -----------------------------------------------------------

def test_build_mapping_rejects_wrong_shape():
    with pytest.raises(MapMappingError):
        build_mapping({
            "tileHexStates": {},
            "tileCornerStates": {},
            "tileEdgeStates": {},
            "portEdgeStates": {},
        })


def test_build_mapping_rejects_corner_off_board():
    base = _load_fort4092_map_state()
    corrupt = {k: dict(v) for k, v in base.items()} if False else {
        "tileHexStates": dict(base["tileHexStates"]),
        "tileCornerStates": dict(base["tileCornerStates"]),
        "tileEdgeStates": dict(base["tileEdgeStates"]),
        "portEdgeStates": dict(base["portEdgeStates"]),
    }
    # Shift corner 0 way off the map.
    corrupt["tileCornerStates"]["0"] = {"x": 99, "y": 99, "z": 0}
    with pytest.raises(MapMappingError):
        build_mapping(corrupt)


# ---- build_catanatron_map_from_colonist ------------------------------------

def test_colonist_built_map_preserves_mapping_node_ids():
    """Node IDs from ``build_mapping`` must remain valid on the custom
    map, otherwise every WS build would write to the wrong node."""
    ms = _load_fort4092_map_state()
    mapping = build_mapping(ms)
    cm = build_catanatron_map_from_colonist(ms, mapping=mapping)
    # Every colonist corner's catanatron node id must be an actual land node.
    for nid in mapping.node_id.values():
        assert nid in cm.land_nodes


def test_colonist_built_map_resource_and_dice_per_tile():
    """Each land tile's (resource, number) on the catanatron side matches
    the colonist tile sitting at the same cube coord."""
    ms = _load_fort4092_map_state()
    mapping = build_mapping(ms)
    cm = build_catanatron_map_from_colonist(ms, mapping=mapping)
    hex_states = ms["tileHexStates"]
    for tid_str, t in hex_states.items():
        cube = axial_to_cube(t["x"], t["y"])
        land = cm.land_tiles[cube]
        assert land.resource == tile_resource(int(t["type"]))
        dice = int(t.get("diceNumber", 0))
        expected_num = dice if dice else None
        assert land.number == expected_num


def test_colonist_built_map_desert_has_no_resource_or_number():
    ms = _load_fort4092_map_state()
    cm = build_catanatron_map_from_colonist(ms)
    deserts = [t for t in cm.land_tiles.values() if t.resource is None]
    assert len(deserts) == 1
    assert deserts[0].number is None


def test_colonist_built_map_port_resources_match_mapping():
    """Each port's resource on the catanatron side matches what the
    colonist port type integer translates to via ``port_resource``."""
    from catanatron.models.map import PORT_DIRECTION_TO_NODEREFS, Port
    ms = _load_fort4092_map_state()
    mapping = build_mapping(ms)
    cm = build_catanatron_map_from_colonist(ms, mapping=mapping)
    pair_to_pid = {pair: pid for pid, pair in mapping.port_edges.items()}
    seen = 0
    for _coord, tile in cm.tiles.items():
        if not isinstance(tile, Port):
            continue
        a_ref, b_ref = PORT_DIRECTION_TO_NODEREFS[tile.direction]
        pair = frozenset({tile.nodes[a_ref], tile.nodes[b_ref]})
        pid = pair_to_pid[pair]
        assert tile.resource == port_resource(mapping.port_types[pid])
        seen += 1
    assert seen == 9


def test_colonist_built_map_has_base_catan_resource_histogram():
    """A sanity check: the custom map's resource/number layout must
    match what base Catan actually produced in the capture."""
    from collections import Counter
    ms = _load_fort4092_map_state()
    cm = build_catanatron_map_from_colonist(ms)
    res_hist = Counter(t.resource for t in cm.land_tiles.values())
    num_hist = Counter(t.number for t in cm.land_tiles.values()
                       if t.number is not None)
    assert res_hist == {
        None: 1, "WOOD": 4, "BRICK": 3, "SHEEP": 4, "WHEAT": 4, "ORE": 3}
    # 18 non-desert tiles covering 2..12 with expected pair counts.
    assert sum(num_hist.values()) == 18
    for n in (2, 12):
        assert num_hist[n] == 1
    for n in (3, 4, 5, 6, 8, 9, 10, 11):
        assert num_hist[n] == 2


def test_tracker_with_colonist_map_uses_ws_node_ids():
    """End-to-end: build the custom map, hand it to the Tracker, then
    run settle/city/road/move_robber at IDs supplied by ``MapMapping``.
    This is the exact path the live dispatcher takes."""
    from cataanbot.tracker import Tracker
    ms = _load_fort4092_map_state()
    mapping = build_mapping(ms)
    cm = build_catanatron_map_from_colonist(ms, mapping=mapping)

    tracker = Tracker(seed=1, catan_map=cm)

    # Grab a corner + one of its adjacent edges.
    cid = next(iter(mapping.node_id))
    node_id = mapping.node_id[cid]
    # Find an edge containing node_id.
    eid, edge_pair = next((eid, pair) for eid, pair in mapping.edge_nodes.items()
                          if node_id in pair)
    other = next(n for n in edge_pair if n != node_id)

    tracker.settle("RED", node_id)
    tracker.city("RED", node_id)
    tracker.road("RED", node_id, other)

    board = tracker.game.state.board
    owner, kind = board.buildings[node_id]
    assert kind == "CITY" and owner.name == "RED"
    assert board.roads[(node_id, other)].name == "RED"

    # Robber at a known tile.
    tid = next(iter(mapping.tile_coord))
    coord = mapping.tile_coord[tid]
    tracker.move_robber(coord)
    assert board.robber_coordinate == coord


def test_tracker_with_colonist_map_roll_yields_real_resources():
    """With the real layout loaded, tracker.roll(N) distributes exactly
    what the live colonist game would pay out — no more random-map hack."""
    from cataanbot.tracker import Tracker
    ms = _load_fort4092_map_state()
    mapping = build_mapping(ms)
    cm = build_catanatron_map_from_colonist(ms, mapping=mapping)

    tracker = Tracker(seed=1, catan_map=cm)

    # Pick a tile with a real number, settle its NORTH corner, then roll.
    tid, coord = next(
        (tid, c) for tid, c in mapping.tile_coord.items()
        if mapping.tile_dice.get(tid))
    dice = mapping.tile_dice[tid]
    resource = tile_resource(mapping.tile_types[tid])

    # Find a colonist corner touching this tile (via tile_corners).
    cid = next(iter(mapping.tile_corners[tid]))
    node_id = mapping.node_id[cid]

    tracker.settle("BLUE", node_id)
    # Robber defaults to the desert, so the rolled tile pays out.
    payout = tracker.roll(dice)
    assert payout.get("BLUE", {}).get(resource, 0) >= 1


def test_tracker_with_colonist_map_survives_reset():
    """``reset`` should reuse the custom map — same node IDs, same layout."""
    from cataanbot.tracker import Tracker
    ms = _load_fort4092_map_state()
    mapping = build_mapping(ms)
    cm = build_catanatron_map_from_colonist(ms, mapping=mapping)

    tracker = Tracker(seed=1, catan_map=cm)
    cid = next(iter(mapping.node_id))
    node_id = mapping.node_id[cid]
    tracker.settle("WHITE", node_id)
    tracker.reset()
    # After reset the custom map is still in effect — the same node id
    # is a land node and a fresh settle succeeds on it.
    assert node_id in tracker.game.state.board.map.land_nodes
    tracker.settle("WHITE", node_id)
