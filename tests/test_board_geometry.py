"""Board geometry on recs: exact node corners in fractional cube coords.

The in-page HUD draws placement markers by feeding these coords to
boardCoordToPixel (linear pointy-top layout, q=cube[0], r=cube[2],
south = +r as live-verified 2026-06-16). These tests pin the corner
offset table to that layout so a regression shows up here, not as a
misplaced circle in a live game.
"""
from __future__ import annotations

import pytest

from catanbot.recommender import (
    _attach_board_geometry,
    _node_board_pos,
    recommend_opening,
)
from catanbot.tracker import Tracker


@pytest.fixture
def tracker():
    return Tracker(seed=4242)


def _tile_coord(m, tile):
    return next(c for c, t in m.land_tiles.items() if t is tile)


def _nodes_by_ref(tile):
    return {getattr(ref, "name", str(ref)): int(nid)
            for ref, nid in tile.nodes.items()}


def test_every_board_node_resolves(tracker):
    m = tracker.game.state.board.map
    for node_id in m.adjacent_tiles:
        pos = _node_board_pos(m, node_id)
        assert pos is not None, f"node {node_id} unresolved"
        assert len(pos) == 3
        # Cube invariant q + s + r = 0 survives the fractional math.
        assert pos[0] + pos[1] + pos[2] == pytest.approx(0.0, abs=1e-3)


def test_interior_corner_equals_three_tile_mean(tracker):
    # On interior nodes the offset-table corner must agree with the mean
    # of the three adjacent tile centers: same point, two derivations.
    # This pins every offset pair against the neighbour topology.
    m = tracker.game.state.board.map
    checked = 0
    for node_id, tiles in m.adjacent_tiles.items():
        if len(tiles) != 3:
            continue
        coords = [_tile_coord(m, t) for t in tiles]
        pos = _node_board_pos(m, node_id)
        assert pos[0] == pytest.approx(
            sum(c[0] for c in coords) / 3, abs=1e-3)
        assert pos[2] == pytest.approx(
            sum(c[2] for c in coords) / 3, abs=1e-3)
        checked += 1
    assert checked > 0


def test_corner_orientation_matches_screen(tracker):
    # r grows SOUTH on colonist's screen: a tile's NORTH corner must land
    # at smaller r than its center, SOUTH at larger, and the east corners
    # at larger q. Catches a vertical or horizontal flip of the table.
    m = tracker.game.state.board.map
    for coord, tile in m.land_tiles.items():
        by_ref = _nodes_by_ref(tile)
        for name, node_id in by_ref.items():
            pos = _node_board_pos(m, node_id)
            if name == "NORTH":
                assert pos[2] < coord[2]
            elif name == "SOUTH":
                assert pos[2] > coord[2]
            elif name in ("NORTHEAST", "SOUTHEAST"):
                assert pos[0] > coord[0]
            elif name in ("NORTHWEST", "SOUTHWEST"):
                assert pos[0] < coord[0]


def test_opening_recs_carry_board_pos(tracker):
    recs = recommend_opening(tracker.game, None, top=5)
    assert recs
    for rec in recs:
        if rec.get("node_id") is None:
            continue
        pos = rec.get("board_pos")
        assert pos is not None and len(pos) == 3
        # Base-map coords live within a couple of rings of the origin.
        assert all(abs(v) <= 4.0 for v in pos)


def test_attach_stamps_road_edges(tracker):
    m = tracker.game.state.board.map
    tile = next(iter(m.land_tiles.values()))
    by_ref = _nodes_by_ref(tile)
    a, b = by_ref["NORTH"], by_ref["NORTHEAST"]
    recs = [
        {"kind": "road", "edge": [a, b]},
        {"kind": "opening_settlement", "node_id": a,
         "road": {"edge": [a, b]}},
    ]
    _attach_board_geometry(recs, m)
    edge = recs[0]["board_edge"]
    assert edge and len(edge) == 2 and edge[0] != edge[1]
    assert recs[1]["board_pos"] == edge[0]
    assert recs[1]["road"]["board_edge"] == edge
