"""Build a colonist.io → catanatron topology mapping from a GameStart frame.

Colonist's base-board map state is delivered in the type=4 ``GameStart``
WebSocket frame and has the shape::

    mapState = {
        "tileHexStates":    {id -> {x, y, type, diceNumber}},   # 19 tiles
        "tileCornerStates": {id -> {x, y, z}},                   # 54 corners
        "tileEdgeStates":   {id -> {x, y, z}},                   # 72 edges
        "portEdgeStates":   {id -> {x, y, z, type}},             #  9 ports
    }

catanatron uses:

* Tiles on cube ``(x, y, z)`` with ``x + y + z = 0``.
* Corner/edge integer ids rooted per ``LandTile`` with ``NodeRef``
  (NORTH, NORTHEAST, ...) and ``EdgeRef`` labels.

Empirically (from fort4092 and cross-checked game5), the coordinate
systems align perfectly under these transforms:

* Tile axial ``(ax, ay)`` ↔ catanatron cube ``(ax, ay, -ax-ay)``.
* Corner ``z`` slot:
    - ``z=0`` is the NORTH corner of tile ``(x, y)``, 3-tile adjacency
      ``{(x, y), (x, y-1), (x+1, y-1)}``.
    - ``z=1`` is the SOUTH corner of tile ``(x, y)``, adjacency
      ``{(x, y), (x, y+1), (x-1, y+1)}``.
* Edge ``z`` slot:
    - ``z=0`` = NW edge (NORTH-NORTHWEST corners)
    - ``z=1`` = W edge  (NORTHWEST-SOUTHWEST corners)
    - ``z=2`` = SW edge (SOUTHWEST-SOUTH corners)

The colonist map numbers 3 edges per hex (z=0/1/2), so each edge has a
unique owning tile even on the ocean boundary — the remaining 3 edges
of a tile are owned by its NE/E/SE neighbors (which may be phantom).

Colonist's tile ``type`` (0..5) and port ``type`` (1..6) integers
encode resources, but the mapping varies by build and has to be
inferred from live play (see ``calibrate_resource_types``). We keep
``tile.type`` raw in ``MapMapping.tile_types`` so downstream code can
translate when the mapping is known.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---- Colonist geometry -----------------------------------------------------

def corner_tile_signature(cx: int, cy: int, cz: int) -> frozenset[tuple[int, int]]:
    """3-tile adjacency signature for a colonist corner coord."""
    if cz == 0:
        return frozenset([(cx, cy), (cx, cy - 1), (cx + 1, cy - 1)])
    return frozenset([(cx, cy), (cx, cy + 1), (cx - 1, cy + 1)])


def edge_endpoint_signatures(
    ex: int, ey: int, ez: int,
) -> tuple[frozenset[tuple[int, int]], frozenset[tuple[int, int]]]:
    """Return the two corner signatures that bound a colonist edge.

    The two endpoints depend on ``z``:
      z=0 NW: NORTH and NORTHWEST
      z=1 W : NORTHWEST and SOUTHWEST
      z=2 SW: SOUTHWEST and SOUTH
    where *WEST corners are expressed as the neighbouring tile's
    N/S corner (see module docstring).
    """
    north     = corner_tile_signature(ex,     ey,     0)
    northwest = corner_tile_signature(ex,     ey - 1, 1)
    southwest = corner_tile_signature(ex - 1, ey + 1, 0)
    south     = corner_tile_signature(ex,     ey,     1)
    if ez == 0: return (north, northwest)
    if ez == 1: return (northwest, southwest)
    if ez == 2: return (southwest, south)
    raise ValueError(f"unknown edge z-slot: {ez}")


def axial_to_cube(ax: int, ay: int) -> tuple[int, int, int]:
    """Colonist axial (x, y) → catanatron cube (x, y, z)."""
    return (ax, ay, -ax - ay)


# ---- Mapping build ---------------------------------------------------------

@dataclass
class MapMapping:
    """Bijective mapping between a colonist mapState and a catanatron map.

    ``tile_coord``: colonist tile id (int) → catanatron cube coord.
    ``node_id``:    colonist corner id (int) → catanatron node id (int).
    ``edge_nodes``: colonist edge id (int) → frozenset of 2 catanatron node ids.
    ``port_edges``: colonist port id (int) → frozenset of 2 catanatron node ids.
    ``tile_types``: colonist tile id → raw colonist ``type`` int.
    ``tile_dice``:  colonist tile id → number token (0 for desert).
    ``port_types``: colonist port id → raw colonist ``type`` int.
    """
    tile_coord: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    node_id:    dict[int, int] = field(default_factory=dict)
    edge_nodes: dict[int, frozenset[int]] = field(default_factory=dict)
    port_edges: dict[int, frozenset[int]] = field(default_factory=dict)
    tile_types: dict[int, int] = field(default_factory=dict)
    tile_dice:  dict[int, int] = field(default_factory=dict)
    port_types: dict[int, int] = field(default_factory=dict)


class MapMappingError(RuntimeError):
    pass


def build_mapping(map_state: dict[str, Any]) -> MapMapping:
    """Build a ``MapMapping`` from a colonist ``mapState`` dict.

    Raises ``MapMappingError`` if the map shape doesn't match base Catan
    (19 hex tiles, 54 corners, 72 edges, 9 ports).
    """
    from catanatron.models.map import BASE_MAP_TEMPLATE, CatanMap

    hex_states = map_state.get("tileHexStates", {})
    corner_states = map_state.get("tileCornerStates", {})
    edge_states = map_state.get("tileEdgeStates", {})
    port_states = map_state.get("portEdgeStates", {})

    if len(hex_states) != 19 or len(corner_states) != 54 \
       or len(edge_states) != 72 or len(port_states) != 9:
        raise MapMappingError(
            f"unexpected mapState shape: "
            f"tiles={len(hex_states)} corners={len(corner_states)} "
            f"edges={len(edge_states)} ports={len(port_states)}")

    colonist_tiles = {(t["x"], t["y"]): int(tid)
                      for tid, t in hex_states.items()}

    cat_map = CatanMap.from_template(BASE_MAP_TEMPLATE)
    cat_tiles_by_axial = {(c[0], c[1]): tile
                          for c, tile in cat_map.tiles.items()
                          if hasattr(tile, "nodes")}

    # node id → set of adjacent catanatron tile axials
    node_tiles: dict[int, set[tuple[int, int]]] = {}
    for (ax, ay), tile in cat_tiles_by_axial.items():
        for _, nid in tile.nodes.items():
            node_tiles.setdefault(nid, set()).add((ax, ay))
    node_by_signature = {frozenset(v): k
                         for k, v in node_tiles.items()
                         if len(v) == 3}

    # All valid catanatron edges (as frozensets of 2 node ids)
    cat_edges: set[frozenset[int]] = set()
    for tile in cat_tiles_by_axial.values():
        for _, (a, b) in tile.edges.items():
            cat_edges.add(frozenset({a, b}))

    m = MapMapping()

    # --- Tiles -------------------------------------------------------------
    for tid, t in hex_states.items():
        tid_i = int(tid)
        m.tile_coord[tid_i] = axial_to_cube(t["x"], t["y"])
        m.tile_types[tid_i] = int(t["type"])
        m.tile_dice[tid_i]  = int(t.get("diceNumber", 0))

    # --- Corners -----------------------------------------------------------
    for cid, c in corner_states.items():
        sig = corner_tile_signature(c["x"], c["y"], c["z"])
        nid = node_by_signature.get(sig)
        if nid is None:
            raise MapMappingError(
                f"corner {cid} at {c} has no matching catanatron node "
                f"(signature {sorted(sig)})")
        m.node_id[int(cid)] = nid

    if len(set(m.node_id.values())) != 54:
        raise MapMappingError("corner mapping is not bijective")

    # --- Edges -------------------------------------------------------------
    for eid, e in edge_states.items():
        a_sig, b_sig = edge_endpoint_signatures(e["x"], e["y"], e["z"])
        a = node_by_signature.get(a_sig)
        b = node_by_signature.get(b_sig)
        if a is None or b is None:
            raise MapMappingError(
                f"edge {eid} at {e} has endpoints outside catanatron's graph")
        pair = frozenset({a, b})
        if pair not in cat_edges:
            raise MapMappingError(
                f"edge {eid} at {e} maps to pair {sorted(pair)} which is "
                f"not a catanatron edge")
        m.edge_nodes[int(eid)] = pair

    if len(set(m.edge_nodes.values())) != 72:
        raise MapMappingError("edge mapping is not bijective")

    # --- Ports -------------------------------------------------------------
    for pid, p in port_states.items():
        a_sig, b_sig = edge_endpoint_signatures(p["x"], p["y"], p["z"])
        a = node_by_signature.get(a_sig)
        b = node_by_signature.get(b_sig)
        if a is None or b is None:
            raise MapMappingError(
                f"port {pid} at {p} has endpoints outside catanatron's graph")
        pair = frozenset({a, b})
        if pair not in cat_edges:
            raise MapMappingError(
                f"port {pid} at {p} maps to non-catanatron edge")
        m.port_edges[int(pid)] = pair
        m.port_types[int(pid)] = int(p["type"])

    return m
