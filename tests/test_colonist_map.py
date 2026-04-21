"""Tests for the colonist → catanatron topology mapping."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cataanbot.colonist_map import (
    MapMapping, MapMappingError, axial_to_cube,
    build_mapping, corner_tile_signature, edge_endpoint_signatures,
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
