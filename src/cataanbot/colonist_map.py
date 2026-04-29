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


# Colonist tile.type int → catanatron resource name. Type 0 is desert
# (no resource). Types 1..5 align with catanatron's canonical resource
# order (WOOD, BRICK, SHEEP, WHEAT, ORE) — verified empirically: the
# fort4092 tile-count histogram is {1: 4, 2: 3, 3: 4, 4: 4, 5: 3}, which
# matches base Catan's resource distribution under this ordering.
# Variant tiles (gold, ocean/water, fog, etc.) live at type ints we
# haven't decoded yet. They're detected and flagged via
# ``LiveSession.non_classic_tiles`` so the HUD can warn the user that
# strategy isn't tuned for this map; tile_resource() returns None for
# them (treats as a non-producing tile, same as desert) so a variant
# game still parses without crashing.
COLONIST_TILE_RESOURCE = {
    0: None,        # desert
    1: "WOOD",
    2: "BRICK",
    3: "SHEEP",
    4: "WHEAT",
    5: "ORE",
}

# Tile type ints we recognize. Anything outside this set is a variant
# tile (gold-hex, ocean/water on Seafarers maps, fog/cloud on Cities &
# Knights, etc.) — kept in a set so the diff parser can detect a
# variant board even when the gameSettings flags don't fire.
KNOWN_CLASSIC_TILE_TYPES = frozenset(COLONIST_TILE_RESOURCE)

# Port type int → resource name. Type 1 is the generic 3:1 port (no
# resource lock); types 2..6 are the resource-specific 2:1 ports,
# offset-by-one against tiles (2:1 wood = port type 2 etc.).
COLONIST_PORT_RESOURCE = {
    1: None,        # generic 3:1
    2: "WOOD",
    3: "BRICK",
    4: "SHEEP",
    5: "WHEAT",
    6: "ORE",
}


def tile_resource(type_int: int) -> str | None:
    """Catanatron resource name for a colonist tile type int.

    Returns None for desert (type 0) AND for any unrecognized type
    (variant tiles we haven't decoded yet — gold, ocean, fog, etc.).
    The unknown-type case is intentionally soft: we'd rather under-
    score a variant tile than crash the whole game-state parser when
    Noah opens a Seafarers/Black Forest map. Detection of those
    types lives in the diff parser so the HUD can warn separately.
    """
    return COLONIST_TILE_RESOURCE.get(type_int)


def port_resource(type_int: int) -> str | None:
    """Catanatron resource name for a colonist port type int (None = 3:1 generic)."""
    if type_int not in COLONIST_PORT_RESOURCE:
        raise ValueError(f"unknown colonist port type {type_int!r}")
    return COLONIST_PORT_RESOURCE[type_int]


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


# Cube-coord neighbour offsets — same six directions catanatron uses.
# Used to walk outward from each land tile when inferring water-ring
# positions for variant maps where colonist doesn't ship water tiles.
_HEX_NEIGHBOURS = (
    (1, -1, 0), (-1, 1, 0),       # EAST, WEST
    (1, 0, -1), (-1, 0, 1),       # NORTHEAST, SOUTHWEST
    (0, 1, -1), (0, -1, 1),       # NORTHWEST, SOUTHEAST
)


def _is_classic_shape(hex_states: dict, corner_states: dict,
                      edge_states: dict, port_states: dict) -> bool:
    """The 19/54/72/9 shape that BASE_MAP_TEMPLATE was designed for.
    Anything else is a variant map (Pond, weekly rotation, etc.) and
    needs a custom catanatron CatanMap built from colonist's data."""
    return (len(hex_states) == 19 and len(corner_states) == 54
            and len(edge_states) == 72 and len(port_states) == 9)


def _build_variant_catanatron_map(map_state: dict[str, Any]):
    """Build a catanatron CatanMap that matches colonist's actual map
    layout — for any shape. Colonist plays normal Catan rules but the
    weekly rotation cycles through different board layouts (Pond, etc.);
    BASE_MAP_TEMPLATE only fits the classic 19-tile board, so we
    construct a fresh tiles dict from colonist's authoritative data.

    First pass: LandTiles at exact colonist cube coords plus a Water
    ring of synthesized tiles around the land perimeter (so corners on
    the land boundary are 3-way and the signature-based corner matcher
    in build_mapping still works). No Port tiles in this pass — that
    means port trade-rate suggestions are quiet on variant boards, but
    opening-pick scoring and build/placement logic work on the right
    geometry. Port reconstruction is a follow-up.
    """
    from catanatron.models.map import (
        CatanMap, LandTile, Water, get_nodes_and_edges,
    )

    hex_states = map_state.get("tileHexStates", {})
    if not hex_states:
        raise MapMappingError("variant map: tileHexStates is empty")

    # Cube-coord ordering: tile id ascending. Determinism matters for
    # node id assignment (catanatron walks the dict and stitches as it
    # goes); a stable order means the same map gives the same node ids
    # across runs, which our corner/edge signature matching relies on.
    sorted_tiles = sorted(
        ((int(tid), t) for tid, t in hex_states.items()),
        key=lambda kv: kv[0],
    )

    tiles: dict[tuple[int, int, int], Any] = {}
    node_autoinc = 0
    tile_autoinc = 0

    # Pass 1: LandTiles at colonist's exact positions.
    land_coords: set[tuple[int, int, int]] = set()
    for col_tid, t in sorted_tiles:
        coord = axial_to_cube(t["x"], t["y"])
        land_coords.add(coord)

    for col_tid, t in sorted_tiles:
        coord = axial_to_cube(t["x"], t["y"])
        nodes, edges, node_autoinc = get_nodes_and_edges(
            tiles, coord, node_autoinc)
        type_int = int(t.get("type", 0))
        resource = tile_resource(type_int)  # None for desert/variant
        dice = int(t.get("diceNumber", 0)) or None
        tiles[coord] = LandTile(
            tile_autoinc, resource, dice if resource is not None else None,
            nodes, edges)
        tile_autoinc += 1

    # Pass 2: Water tiles around the perimeter. Without these, land
    # corners on the outer edge of the map would have only 2 adjacent
    # tiles, but build_mapping's corner-signature matcher expects 3
    # (the catanatron convention treats land-water junctions as 3-way
    # nodes). We synthesize a Water ring 1 cell out from every land
    # tile; doesn't affect gameplay since Water tiles produce nothing.
    water_coords: set[tuple[int, int, int]] = set()
    for coord in land_coords:
        for dx, dy, dz in _HEX_NEIGHBOURS:
            nbr = (coord[0] + dx, coord[1] + dy, coord[2] + dz)
            if nbr not in land_coords:
                water_coords.add(nbr)
    # Sort for determinism
    for coord in sorted(water_coords):
        nodes, edges, node_autoinc = get_nodes_and_edges(
            tiles, coord, node_autoinc)
        tiles[coord] = Water(nodes, edges)

    return CatanMap.from_tiles(tiles)


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
    # colonist tile id → set of colonist corner ids on that tile's 6
    # vertices. Built during build_mapping so yield computation on a
    # roll doesn't have to re-scan corner signatures.
    tile_corners: dict[int, set[int]] = field(default_factory=dict)


class MapMappingError(RuntimeError):
    pass


def build_mapping(map_state: dict[str, Any]) -> MapMapping:
    """Build a ``MapMapping`` from a colonist ``mapState`` dict.

    Handles classic (19/54/72/9) AND variant maps (Pond, Random,
    weekly rotation). Same Catan rules in both cases — colonist
    only changes the BOARD SHAPE, not the gameplay. For classic
    we use catanatron's BASE_MAP_TEMPLATE so port positions land
    on the canonical water-ring slots; for variants we build a
    custom CatanMap from colonist's tileHexStates directly.

    Variant ports / water tiles aren't supported in this first
    pass: the custom CatanMap is land-only, so port-2:1 trade
    suggestions stay quiet on variant boards. Bot still plays the
    real geometry (opening picks, build placement, recommender)
    so most of the value is intact.
    """
    from catanatron.models.map import BASE_MAP_TEMPLATE, CatanMap

    hex_states = map_state.get("tileHexStates", {})
    corner_states = map_state.get("tileCornerStates", {})
    edge_states = map_state.get("tileEdgeStates", {})
    port_states = map_state.get("portEdgeStates", {})

    is_classic = _is_classic_shape(
        hex_states, corner_states, edge_states, port_states)

    colonist_tiles = {(t["x"], t["y"]): int(tid)
                      for tid, t in hex_states.items()}

    if is_classic:
        cat_map = CatanMap.from_template(BASE_MAP_TEMPLATE)
    else:
        # Variant board — build a CatanMap matching colonist's
        # actual tile layout. May fail if the cube coords have
        # disconnected components (extreme variants); let that
        # surface so the bridge can degrade rather than crash.
        cat_map = _build_variant_catanatron_map(map_state)
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
    axial_to_tile_id = {(t["x"], t["y"]): int(tid)
                        for tid, t in hex_states.items()}
    for cid, c in corner_states.items():
        sig = corner_tile_signature(c["x"], c["y"], c["z"])
        nid = node_by_signature.get(sig)
        if nid is None:
            raise MapMappingError(
                f"corner {cid} at {c} has no matching catanatron node "
                f"(signature {sorted(sig)})")
        cid_i = int(cid)
        m.node_id[cid_i] = nid
        for ax, ay in sig:
            tid = axial_to_tile_id.get((ax, ay))
            if tid is not None:
                m.tile_corners.setdefault(tid, set()).add(cid_i)

    expected_nodes = 54 if is_classic else len(corner_states)
    if len(set(m.node_id.values())) != expected_nodes:
        raise MapMappingError(
            f"corner mapping is not bijective "
            f"(want {expected_nodes}, got {len(set(m.node_id.values()))})")

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

    expected_edges = 72 if is_classic else len(edge_states)
    if len(set(m.edge_nodes.values())) != expected_edges:
        raise MapMappingError(
            f"edge mapping is not bijective "
            f"(want {expected_edges}, got {len(set(m.edge_nodes.values()))})")

    # --- Ports -------------------------------------------------------------
    # Classic maps: ports map cleanly to catanatron's water-ring
    # template positions. Variant maps have ports too but the
    # land-only CatanMap we built doesn't have water/port tiles to
    # attach them to; collect the port edge-corner pairs anyway so
    # downstream code can still see *which* edges have ports (useful
    # for "don't waste a settle there" logic), even if the trade
    # rates aren't wired through catanatron's port_nodes.
    for pid, p in port_states.items():
        a_sig, b_sig = edge_endpoint_signatures(p["x"], p["y"], p["z"])
        a = node_by_signature.get(a_sig)
        b = node_by_signature.get(b_sig)
        if a is None or b is None:
            if is_classic:
                raise MapMappingError(
                    f"port {pid} at {p} has endpoints outside catanatron's graph")
            # Variant: silently skip ports we can't anchor.
            continue
        pair = frozenset({a, b})
        if pair not in cat_edges:
            if is_classic:
                raise MapMappingError(
                    f"port {pid} at {p} maps to non-catanatron edge")
            continue
        m.port_edges[int(pid)] = pair
        m.port_types[int(pid)] = int(p["type"])

    return m


def build_catanatron_map_from_colonist(
    map_state: dict[str, Any],
    mapping: "MapMapping | None" = None,
):
    """Return a CatanMap that mirrors colonist's resource / dice / port layout.

    We walk ``BASE_MAP_TEMPLATE.topology`` in iteration order and
    hand-build the tile dict ourselves:

    * For the 19 land coords, we use colonist's actual tile type + dice
      number (``mapping.tile_types`` / ``mapping.tile_dice``). Iterating
      them in template order preserves catanatron's 0..53 land node IDs,
      so ``MapMapping.node_id`` / ``edge_nodes`` remain valid on the
      returned map.
    * For the 18 water-ring coords, we check each outward-facing direction
      for a colonist port sitting on that edge (matched against
      ``mapping.port_edges`` land-node pairs). Matches become
      ``Port`` tiles with the right resource and direction; everything
      else is plain ``Water``. This is critical: colonist and catanatron
      place ports at *different* 9 edges of the water ring, so we can't
      just reuse the template's port positions.

    Result: ``yield_resources`` off catanatron's board produces the exact
    payout the live game would, and port-adjacency queries see the real
    port layout.
    """
    from catanatron.models.map import (
        BASE_MAP_TEMPLATE, CatanMap, Direction, LandTile, Port, Water,
        PORT_DIRECTION_TO_NODEREFS, UNIT_VECTORS, get_nodes_and_edges,
    )

    if mapping is None:
        mapping = build_mapping(map_state)

    hex_states = map_state.get("tileHexStates", {})
    corner_states = map_state.get("tileCornerStates", {})
    edge_states = map_state.get("tileEdgeStates", {})
    port_states = map_state.get("portEdgeStates", {})
    is_classic = _is_classic_shape(
        hex_states, corner_states, edge_states, port_states)
    if not is_classic:
        # Variant board — delegate to the land-only builder. Ports +
        # water tiles aren't reconstructed for variants in this pass,
        # but the resource/dice/adjacency layout matches colonist
        # exactly so opening picks and produce events work.
        return _build_variant_catanatron_map(map_state)

    cube_to_colonist_tid = {
        axial_to_cube(t["x"], t["y"]): int(tid)
        for tid, t in hex_states.items()
    }
    pair_to_colonist_pid = {
        pair: pid for pid, pair in mapping.port_edges.items()
    }

    tiles: dict[tuple[int, int, int], Any] = {}
    node_autoinc = 0
    tile_autoinc = 0
    port_autoinc = 0

    # Land tiles first — same order as BASE_MAP_TEMPLATE so the 0..53 land
    # node IDs come out identical to what build_mapping saw.
    for coord, tt in BASE_MAP_TEMPLATE.topology.items():
        if tt is not LandTile:
            continue
        nodes, edges, node_autoinc = get_nodes_and_edges(
            tiles, coord, node_autoinc)
        col_tid = cube_to_colonist_tid.get(coord)
        if col_tid is None:
            raise MapMappingError(
                f"colonist map missing land tile at {coord}")
        type_int = mapping.tile_types[col_tid]
        resource = tile_resource(type_int)
        dice = mapping.tile_dice.get(col_tid, 0)
        tiles[coord] = LandTile(
            tile_autoinc, resource, dice if dice else None, nodes, edges)
        tile_autoinc += 1

    # Ring tiles: Port at the direction matching a colonist port edge,
    # Water everywhere else. Ring positions themselves match catanatron's
    # template (base Catan water ring is fixed); only port slots move.
    for coord, tt in BASE_MAP_TEMPLATE.topology.items():
        if tt is LandTile:
            continue
        nodes, edges, node_autoinc = get_nodes_and_edges(
            tiles, coord, node_autoinc)

        port_direction = None
        port_pid = None
        for direction in Direction:
            nbr = tuple(c + v for c, v in zip(coord, UNIT_VECTORS[direction]))
            nbr_tile = tiles.get(nbr)
            if not isinstance(nbr_tile, LandTile):
                continue
            a_ref, b_ref = PORT_DIRECTION_TO_NODEREFS[direction]
            pair = frozenset({nodes[a_ref], nodes[b_ref]})
            pid = pair_to_colonist_pid.get(pair)
            if pid is not None:
                port_direction = direction
                port_pid = pid
                break

        if port_direction is not None:
            type_int = mapping.port_types[port_pid]
            tiles[coord] = Port(
                port_autoinc, port_resource(type_int),
                port_direction, nodes, edges)
            port_autoinc += 1
        else:
            tiles[coord] = Water(nodes, edges)

    return CatanMap.from_tiles(tiles)
