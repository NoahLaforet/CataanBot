// board.js — colonist mapState → standalone JS board model.
//
// Parses colonist's `mapState` (the same payload catanatron's
// colonist_map.py consumes on the bridge) into a node/edge graph
// the JS recommender can score against. Self-contained: no
// catanatron, no external libs.
//
// Coord conventions (mirror src/catanbot/colonist_map.py):
//
//   * Tiles live at colonist axial (x, y). We translate to cube
//     (q, s, r) with q+s+r=0 so distance / neighbour math is the
//     usual hex stuff.
//   * Each tile has 6 corners. Colonist numbers 2 of them per
//     tile (NORTH=z0, SOUTH=z1); the other 4 belong to neighbour
//     tiles. We resolve duplicates via "tile signature" — the
//     unordered set of axials touching the corner.
//   * Each tile has 6 edges. Colonist numbers 3 per tile (NW=z0,
//     W=z1, SW=z2); the other 3 belong to neighbour tiles. Same
//     dedup trick via endpoint signatures.
//
// Node IDs are strings: "x1,y1|x2,y2[|x3,y3]" with axials sorted.
// 2-tile signatures appear on the land/water boundary; interior
// nodes have 3-tile signatures. Edge IDs are "nodeA||nodeB" with
// nodes sorted lexicographically.

import { RESOURCE_NAMES } from './state.js';

const PIP_DOTS_BY_NUMBER = {
    2: 1, 12: 1,
    3: 2, 11: 2,
    4: 3, 10: 3,
    5: 4, 9: 4,
    6: 5, 8: 5,
};

// Colonist tile.type → catanatron resource name. Mirror of
// COLONIST_TILE_RESOURCE in src/catanbot/colonist_map.py.
const COLONIST_TILE_RESOURCE = {
    0: null,        // desert
    1: 'WOOD',
    2: 'BRICK',
    3: 'SHEEP',
    4: 'WHEAT',
    5: 'ORE',
};

// Colonist port.type → resource name. Type 1 = generic 3:1.
const COLONIST_PORT_RESOURCE = {
    1: null,        // generic 3:1
    2: 'WOOD',
    3: 'BRICK',
    4: 'SHEEP',
    5: 'WHEAT',
    6: 'ORE',
};

// Cube neighbour offsets for the six hex directions.
const HEX_NEIGHBOURS = [
    [1, -1, 0], [-1, 1, 0],   // EAST, WEST
    [1, 0, -1], [-1, 0, 1],   // NORTHEAST, SOUTHWEST
    [0, 1, -1], [0, -1, 1],   // NORTHWEST, SOUTHEAST
];

/** Colonist axial → cube (q, s, r). */
function axialToCube(ax, ay) {
    return [ax, -ax - ay, ay];
}

/** Stable signature for a tile coord — used as map key inside
 *  node signatures. Just the axial pair. */
function tileSig(ax, ay) {
    return `${ax},${ay}`;
}

/** Three-tile signature for a corner at colonist (cx, cy, cz).
 *  Returns sorted "ax,ay|ax,ay|ax,ay" string. */
function cornerSig(cx, cy, cz) {
    let trio;
    if (cz === 0) {
        // NORTH corner: tile + N tile + NE tile
        trio = [[cx, cy], [cx, cy - 1], [cx + 1, cy - 1]];
    } else {
        // SOUTH corner: tile + S tile + SW tile
        trio = [[cx, cy], [cx, cy + 1], [cx - 1, cy + 1]];
    }
    return trio.map(([x, y]) => tileSig(x, y)).sort().join('|');
}

/** Endpoint signatures for a colonist edge at (ex, ey, ez).
 *  Returns [nodeSigA, nodeSigB]. */
function edgeEndpoints(ex, ey, ez) {
    const north     = cornerSig(ex,     ey,     0);
    const northwest = cornerSig(ex,     ey - 1, 1);
    const southwest = cornerSig(ex - 1, ey + 1, 0);
    const south     = cornerSig(ex,     ey,     1);
    if (ez === 0) return [north, northwest];
    if (ez === 1) return [northwest, southwest];
    if (ez === 2) return [southwest, south];
    throw new Error(`unknown edge z-slot: ${ez}`);
}

/** Stable edge id from two node sig strings. */
function edgeKeyFromSigs(a, b) {
    return a < b ? `${a}||${b}` : `${b}||${a}`;
}

/** Resolve a node sig string to an existing node id, or null.
 *  When a corner sits on the land/water boundary, only 2 of its
 *  trio of tiles are land; the third may be missing entirely
 *  from a variant map. We accept any node whose sig is a SUPERSET
 *  of the queried sig — that's the catanatron convention.
 *
 *  Returns the canonical node id (which may be a 2-tile sub-sig)
 *  or null if no match.
 */
function resolveNodeSig(nodes, sig) {
    if (nodes[sig]) return sig;
    // Sub-signatures: if the queried sig is a 3-tile signature but
    // an existing 2-tile node intersects with it (because the third
    // tile is water/missing), use that one.
    const wanted = new Set(sig.split('|'));
    for (const candidate of Object.keys(nodes)) {
        const have = new Set(candidate.split('|'));
        // Match when every tile in the existing node's sig is
        // present in the queried sig, AND the existing sig has
        // at least 2 tiles (single-tile nodes are noise).
        if (have.size >= 2) {
            let allPresent = true;
            for (const t of have) {
                if (!wanted.has(t)) { allPresent = false; break; }
            }
            if (allPresent) return candidate;
        }
    }
    return null;
}

/** Build a board model from a colonist mapState payload.
 *
 *  Accepts the mapState dict as it arrives in the GameStart frame.
 *  Returns:
 *
 *    {
 *      tiles: { tileId → {id, coord, axial, resource, number,
 *                          pip, nodes:[], edges:[]} },
 *      nodes: { nodeId → {id, tiles:[], neighbors:Set,
 *                          port: {kind:'2:1'|'3:1', resource} | null} },
 *      edges: { edgeId → {id, a, b, tiles:[]} },
 *      landNodes: Set<nodeId>,
 *      landTiles: Set<tileId>,
 *      desertTile: tileId | null,
 *      ports: [ {kind, resource, nodes:[]} ],
 *    }
 *
 *  Returns null when mapState is missing or empty (caller falls
 *  back to bridge mode).
 */
export function buildBoardFromColonistMap(mapState) {
    if (!mapState) return null;
    const hexStates = mapState.tileHexStates || {};
    const portStates = mapState.portEdgeStates || {};
    if (Object.keys(hexStates).length === 0) return null;

    const tiles = {};
    const nodes = {};
    const edges = {};

    // Pass 1: tiles. Pre-allocate so subsequent passes can
    // reference each tile by id.
    const axialByTid = {};
    for (const [tid, t] of Object.entries(hexStates)) {
        const ax = Number(t.x);
        const ay = Number(t.y);
        const typeInt = Number(t.type) || 0;
        const dice = Number(t.diceNumber) || null;
        const resource = (typeInt in COLONIST_TILE_RESOURCE)
            ? COLONIST_TILE_RESOURCE[typeInt] : null;
        tiles[tid] = {
            id: tid,
            coord: axialToCube(ax, ay),
            axial: [ax, ay],
            resource,
            number: resource ? dice : null,
            pip: resource ? (PIP_DOTS_BY_NUMBER[dice] || 0) : 0,
            nodes: [],
            edges: [],
        };
        axialByTid[tid] = [ax, ay];
    }

    // Pass 2: corners. For every tile, register its NORTH (z=0)
    // and SOUTH (z=1) corners by signature. Dedup happens
    // naturally because two tiles share the same corner
    // signature for their shared corner.
    for (const [tid, t] of Object.entries(hexStates)) {
        const [ax, ay] = [Number(t.x), Number(t.y)];
        for (const cz of [0, 1]) {
            const sig = cornerSig(ax, ay, cz);
            if (!nodes[sig]) {
                nodes[sig] = {
                    id: sig,
                    tiles: [],
                    neighbors: new Set(),
                    port: null,
                };
            }
            if (!nodes[sig].tiles.includes(tid)) {
                nodes[sig].tiles.push(tid);
            }
            tiles[tid].nodes.push(sig);
        }
    }

    // For tiles on the inner board, the other 4 corners belong
    // to neighbour tiles. Walk neighbours and back-link any
    // signatures that include this tile.
    const tileSigToId = {};
    for (const [tid, [ax, ay]] of Object.entries(axialByTid)) {
        tileSigToId[tileSig(ax, ay)] = tid;
    }
    for (const node of Object.values(nodes)) {
        for (const ts of node.id.split('|')) {
            const ttid = tileSigToId[ts];
            if (ttid && !tiles[ttid].nodes.includes(node.id)) {
                tiles[ttid].nodes.push(node.id);
            }
        }
    }

    // Pass 3: edges. Each tile owns its NW/W/SW (z=0/1/2). The
    // other 3 belong to neighbours' z=0/1/2 entries — they get
    // registered when those neighbours iterate. Endpoint sigs
    // may resolve to existing node ids or to sub-sigs (boundary
    // corners).
    for (const [tid, t] of Object.entries(hexStates)) {
        const [ax, ay] = [Number(t.x), Number(t.y)];
        for (const ez of [0, 1, 2]) {
            const [sigA, sigB] = edgeEndpoints(ax, ay, ez);
            const a = resolveNodeSig(nodes, sigA);
            const b = resolveNodeSig(nodes, sigB);
            if (!a || !b) continue;  // boundary edge; node missing
            const eid = edgeKeyFromSigs(a, b);
            if (!edges[eid]) {
                edges[eid] = { id: eid, a, b, tiles: [] };
                nodes[a].neighbors.add(b);
                nodes[b].neighbors.add(a);
            }
            if (!edges[eid].tiles.includes(tid)) {
                edges[eid].tiles.push(tid);
            }
            if (!tiles[tid].edges.includes(eid)) {
                tiles[tid].edges.push(eid);
            }
        }
    }

    // Pass 3b: back-link non-owned edges to tiles. An edge that
    // tile T touches but doesn't own (T's NE/E/SE edges, owned
    // by NE/E/SE neighbour tiles) should still appear in T's
    // edges[] so distance/longest-road computations can iterate
    // T's full edge set. An edge belongs to T iff both of its
    // endpoint nodes are in T's nodes list.
    for (const tile of Object.values(tiles)) {
        const tileNodeSet = new Set(tile.nodes);
        for (const eid of Object.keys(edges)) {
            const e = edges[eid];
            if (tile.edges.includes(eid)) continue;
            if (tileNodeSet.has(e.a) && tileNodeSet.has(e.b)) {
                tile.edges.push(eid);
                if (!e.tiles.includes(tile.id)) {
                    e.tiles.push(tile.id);
                }
            }
        }
    }

    // Pass 4: ports. Each port lives on an edge; we attach the
    // port spec to both endpoint nodes so the recommender can
    // ask "is this node a port node?" in O(1).
    const ports = [];
    for (const p of Object.values(portStates)) {
        const ex = Number(p.x), ey = Number(p.y), ez = Number(p.z);
        const typeInt = Number(p.type) || 1;
        const resource = (typeInt in COLONIST_PORT_RESOURCE)
            ? COLONIST_PORT_RESOURCE[typeInt] : null;
        const kind = resource === null ? '3:1' : '2:1';
        const [sigA, sigB] = edgeEndpoints(ex, ey, ez);
        const a = resolveNodeSig(nodes, sigA);
        const b = resolveNodeSig(nodes, sigB);
        const portNodes = [a, b].filter(Boolean);
        const port = { kind, resource, nodes: portNodes };
        ports.push(port);
        for (const nid of portNodes) {
            // Most-specific port wins if a node sits on multiple
            // (shouldn't happen on classic, can on variants).
            if (!nodes[nid].port
                    || (nodes[nid].port.kind === '3:1' && kind === '2:1')) {
                nodes[nid].port = port;
            }
        }
    }

    // Land sets: nodes touching at least one resource-bearing
    // tile, tiles with a resource. Desert is "land" but doesn't
    // produce.
    const landTiles = new Set();
    let desertTile = null;
    for (const [tid, t] of Object.entries(tiles)) {
        if (t.resource !== null || t.number !== null) {
            landTiles.add(tid);
        } else if (Number(hexStates[tid].type) === 0) {
            // explicit desert (not just unknown)
            landTiles.add(tid);
            desertTile = tid;
        }
    }
    const landNodes = new Set();
    for (const [nid, n] of Object.entries(nodes)) {
        if (n.tiles.some(t => landTiles.has(t))) {
            landNodes.add(nid);
        }
    }

    return {
        tiles,
        nodes,
        edges,
        landNodes,
        landTiles,
        desertTile,
        ports,
    };
}

/** Per-roll cards-per-roll yield map for a node. Mirrors
 *  catanatron's `node_production[node_id]` — sum of (1/36 ×
 *  pip_count_for_number) per resource across the node's
 *  adjacent producing tiles. */
export function nodeProduction(board, nodeId) {
    const out = {};
    const node = board.nodes[nodeId];
    if (!node) return out;
    for (const tid of node.tiles) {
        const tile = board.tiles[tid];
        if (!tile || !tile.resource || !tile.number) continue;
        const pip = tile.pip;
        if (!pip) continue;
        // Probability of this number on a roll = pip/36; one card
        // per hit per settlement. So expected = pip/36 cards/roll.
        const yield_ = pip / 36;
        out[tile.resource] = (out[tile.resource] || 0) + yield_;
    }
    return out;
}

/** Pip dot count for a Catan number (5-pip = 6/8, 1-pip = 2/12). */
export function pipsForNumber(n) {
    return PIP_DOTS_BY_NUMBER[Number(n) || 0] || 0;
}

export {
    PIP_DOTS_BY_NUMBER,
    COLONIST_TILE_RESOURCE,
    COLONIST_PORT_RESOURCE,
    HEX_NEIGHBOURS,
    axialToCube,
};
