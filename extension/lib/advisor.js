// advisor.js — opening picks + robber target ranking, JS port.
//
// Heuristic-only port of src/catanbot/advisor.py. No 1-ply search
// (that stays bridge-only — depends on catanatron's Game.copy()).
// Ranks every land node for opening settlement, every land tile
// for robber placement.

import { nodeProduction, pipsForNumber } from './board.js';

// Diversity multiplier — 1 resource = 1.0, 2 = 1.08, 3 = 1.22.
// 3-resource node reads about a third of a tile stronger than
// a same-pip 2-resource one. Reddit 36k-game finding #3:
// composition trumps volume.
const DIVERSITY_BY_COUNT = { 0: 1.0, 1: 1.0, 2: 1.08, 3: 1.22 };

// Per-resource weight — wheat is the #1 winning resource per the
// Reddit data; small tiebreaker bias on wheat-bearing corners.
const RESOURCE_WEIGHT = {
    WHEAT: 1.10, WOOD: 1.0, BRICK: 1.0, SHEEP: 1.0, ORE: 1.0,
};

const DENIAL_WEIGHT = 0.04;
const BLOCKING_TOP_K = 3;
const BLOCKING_WEIGHT = 0.05;

function weightedRawProduction(prod) {
    let total = 0;
    for (const [r, v] of Object.entries(prod)) {
        total += v * (RESOURCE_WEIGHT[r] || 1.0);
    }
    return total;
}

/** Port-bonus curve, mirroring advisor._port_bonus.
 *  port: { kind: '2:1'|'3:1', resource: str|null }
 *  resources: { resourceName → cards-per-roll }
 *  tiles: optional list of (resource, number) pairs for the
 *    pip-alignment guard (P1-7).
 */
function portBonus(port, resources, tiles, tableScarcity) {
    if (!port) return 0;
    if (port.kind === '3:1') return 0.005;
    const portResource = port.resource;
    const resProd = Number(resources[portResource] || 0);
    if (resProd > 0) {
        let bonus = 0.30 * resProd;
        // P1-7: halve when matching tile is on weak pip.
        if (tiles && tiles.length) {
            let bestPip = 0;
            for (const [res, num] of tiles) {
                if (res !== portResource || num == null) continue;
                const p = pipsForNumber(num);
                if (p > bestPip) bestPip = p;
            }
            if (bestPip > 0 && bestPip <= 2) bonus *= 0.5;
        }
        // P2-11: dampen when table scarcity > 0.8 baseline.
        if (tableScarcity) {
            const sc = Number(tableScarcity[portResource] || 0);
            if (sc > 0.8) bonus *= 1.0 - (sc - 0.8) * 1.5;
        }
        return bonus;
    }
    return 0.015;
}

/** Pre-placement table scarcity. Without any settlements down,
 *  scarcity is per-resource based on how many tiles produce it
 *  on the board (more tiles = lower scarcity). With settlements,
 *  fold in current production. Compute once per scoring pass. */
export function computeTableScarcity(board) {
    const totals = { WOOD: 0, BRICK: 0, SHEEP: 0, WHEAT: 0, ORE: 0 };
    for (const t of Object.values(board.tiles || {})) {
        if (!t.resource) continue;
        totals[t.resource] = (totals[t.resource] || 0) + (t.pip / 36);
    }
    const grand = Object.values(totals).reduce((s, v) => s + v, 0);
    if (grand <= 0) return {};
    const out = {};
    for (const [r, v] of Object.entries(totals)) {
        out[r] = Math.max(0, Math.min(1, 1 - v / grand));
    }
    return out;
}

/** Rank every legal opening node by complement-aware production
 *  + port + denial + diversity. Mirror of
 *  advisor.score_opening_nodes.
 *
 *  Args:
 *    board — from buildBoardFromColonistMap.
 *    opts.legalNodes — optional Set restricting candidate pool.
 *    opts.tableScarcity — optional precomputed scarcity map.
 *
 *  Returns array of NodeScore dicts sorted descending by score:
 *    {
 *      nodeId, rawProduction, diversityFactor, portBonus, baseScore,
 *      denialBonus, blockingBonus, score,
 *      resources: {res → cards/roll},
 *      tiles: [{resource, number, pip}],
 *      port: {kind, resource} | null,
 *    }
 */
export function scoreOpeningNodes(board, opts = {}) {
    if (!board || !board.nodes) return [];
    const legal = opts.legalNodes
        ? new Set([...board.landNodes].filter(n => opts.legalNodes.has(n)))
        : new Set(board.landNodes);
    const scarcity = opts.tableScarcity || computeTableScarcity(board);

    // Pass 1: base scores per node.
    const scratch = {};
    const baseByNode = {};
    for (const nodeId of legal) {
        const node = board.nodes[nodeId];
        if (!node) continue;
        const prod = nodeProduction(board, nodeId);
        const raw = weightedRawProduction(prod);
        const distinct = Object.values(prod).filter(v => v > 0).length;
        const diversity = DIVERSITY_BY_COUNT[distinct] || 1.15;
        const tiles = node.tiles
            .map(tid => board.tiles[tid])
            .filter(Boolean)
            .map(t => [t.resource || 'DESERT', t.number]);
        const pBonus = portBonus(node.port, prod, tiles, scarcity);
        const base = raw * diversity + pBonus;
        baseByNode[nodeId] = base;
        scratch[nodeId] = {
            raw, diversity, portBonus: pBonus,
            resources: prod, tiles, port: node.port || null,
        };
    }

    // Baseline top-K base scores for the blocking computation.
    const baselineSorted = Object.values(baseByNode)
        .sort((a, b) => b - a);
    const baselineTopK = baselineSorted.slice(0, BLOCKING_TOP_K)
        .reduce((s, v) => s + v, 0);

    // Pass 2: denial + blocking + assemble.
    const scores = [];
    for (const nodeId of legal) {
        const node = board.nodes[nodeId];
        if (!node) continue;
        const fields = scratch[nodeId];
        if (!fields) continue;
        let denial = 0;
        for (const nb of node.neighbors) {
            if (nb in baseByNode) denial += baseByNode[nb];
        }
        denial *= DENIAL_WEIGHT;
        // Blocking: exclude this node + its neighbours, recompute
        // top-K, take the gap.
        const excluded = new Set([nodeId, ...node.neighbors]);
        const remainingSorted = Object.entries(baseByNode)
            .filter(([nid]) => !excluded.has(nid))
            .map(([, v]) => v)
            .sort((a, b) => b - a);
        const remainingTopK = remainingSorted.slice(0, BLOCKING_TOP_K)
            .reduce((s, v) => s + v, 0);
        const blocking = BLOCKING_WEIGHT *
            Math.max(0, baselineTopK - remainingTopK);

        const base = baseByNode[nodeId];
        scores.push({
            nodeId,
            rawProduction: fields.raw,
            diversityFactor: fields.diversity,
            portBonus: fields.portBonus,
            baseScore: base,
            denialBonus: denial,
            blockingBonus: blocking,
            score: base + denial + blocking,
            resources: fields.resources,
            tiles: fields.tiles,
            port: fields.port,
        });
    }

    scores.sort((a, b) => b.score - a.score);
    return scores;
}

/** Tile descriptors for the edge between two nodes — the 1-2
 *  resource-bearing tiles that touch the edge. Mirrors
 *  recommender._edge_tiles. Used by opening-road suggestion to
 *  surface "↳ road: between BR3 SHE10" style hints. */
export function edgeTiles(board, nodeA, nodeB) {
    const a = board.nodes[nodeA];
    const b = board.nodes[nodeB];
    if (!a || !b) return [];
    const tilesA = new Set(a.tiles);
    const tilesB = new Set(b.tiles);
    const out = [];
    for (const tid of tilesA) {
        if (!tilesB.has(tid)) continue;
        const t = board.tiles[tid];
        if (!t) continue;
        out.push([t.resource || 'DESERT', t.number]);
    }
    return out;
}

/** Best opening road for a proposed settlement. Picks one of the
 *  three adjacent edges by what 2-hop expansion target the road
 *  opens up, with fallback to the highest-prod adjacent direction
 *  when every corridor is sealed. JS port of
 *  recommender._best_opening_road, slimmed for the standalone
 *  pipeline (no opp pieces during opening so no contested /
 *  sealed-by-opp logic).
 *
 *  Returns `{edge, toward_node, edge_tiles, sealed?}` matching the
 *  bridge shape the panel renderer reads, or `null` if the node
 *  has no neighbours.
 *
 *  `scoredByNode` is a dict of {nodeId: score} from a prior
 *  scoreOpeningNodes pass — caller reuses the existing scoring
 *  rather than re-computing.
 *
 *  `placedNodes` (Set) marks nodes that already have a settlement
 *  on them (and their neighbours, distance-rule). Edges into those
 *  are degraded.
 */
export function bestOpeningRoad(board, settlementNodeId, opts = {}) {
    if (!board || !board.nodes) return null;
    const settle = board.nodes[settlementNodeId];
    if (!settle) return null;
    const scoredByNode = opts.scoredByNode || {};
    const placedNodes = opts.placedNodes || new Set();
    const myEdges = opts.myEdges || new Set();   // 'a||b' strings we own
    let best = null;       // {combined, far, expansion}
    let fallback = null;   // {prod, far}
    for (const far of settle.neighbors) {
        // Skip edges we already own a road on (round-3/4 follow-up
        // flow shouldn't suggest a road we built in round 1).
        const eid = settlementNodeId < far
            ? `${settlementNodeId}||${far}`
            : `${far}||${settlementNodeId}`;
        if (myEdges.has(eid)) continue;
        // Far-node production for the fallback ranking.
        const farNode = board.nodes[far];
        if (!farNode) continue;
        let farProd = 0;
        for (const tid of farNode.tiles) {
            const t = board.tiles[tid];
            if (t && t.resource && t.number) farProd += (t.pip || 0);
        }
        if (fallback === null || farProd > fallback.prod) {
            fallback = { prod: farProd, far };
        }
        // Best 2-hop expansion via (settle → far → x).
        let expScore = 0;
        let expNode = null;
        for (const x of farNode.neighbors) {
            if (x === settlementNodeId) continue;
            // Distance-rule blocked? Skip.
            if (placedNodes.has(x)) continue;
            const xs = scoredByNode[x];
            if (xs == null) continue;
            if (xs > expScore) {
                expScore = xs;
                expNode = x;
            }
        }
        if (expNode === null) continue;
        const combined = expScore * 100.0 + farProd;
        if (best === null || combined > best.combined) {
            best = { combined, far, expansion: expNode };
        }
    }
    if (best !== null) {
        return {
            edge: [settlementNodeId, best.far],
            toward_node: best.expansion,
            edge_tiles: edgeTiles(board, settlementNodeId, best.far),
        };
    }
    if (fallback !== null) {
        return {
            edge: [settlementNodeId, fallback.far],
            toward_node: fallback.far,
            edge_tiles: edgeTiles(board, settlementNodeId, fallback.far),
            sealed: true,
        };
    }
    return null;
}

/** Distance-rule legality: nodes that remain legal after the
 *  given placements claim themselves + their neighbours. */
export function legalNodesAfterPicks(board, picks) {
    if (!board) return new Set();
    const legal = new Set(board.landNodes);
    for (const pick of picks || []) {
        legal.delete(pick);
        const node = board.nodes[pick];
        if (!node) continue;
        for (const nb of node.neighbors) legal.delete(nb);
    }
    return legal;
}

/** Rank a hypothetical 2nd settle pair against the placed first
 *  settle. Mirror of advisor.score_second_settlements.
 *  Returns array of {nodeId, complementValue, diversityBonus,
 *  portBonus, score, port, resources, tiles} sorted desc. */
export function scoreSecondSettlements(board, firstNodeId, opts = {}) {
    if (!board || !firstNodeId) return [];
    const firstNode = board.nodes[firstNodeId];
    if (!firstNode) return [];
    const RESOURCES = ['WOOD', 'BRICK', 'SHEEP', 'WHEAT', 'ORE'];
    const fProd = nodeProduction(board, firstNodeId);
    // Marginal value at F: 1 / (0.5 + production). Rare resources
    // are worth more.
    const marginal = {};
    for (const r of RESOURCES) {
        marginal[r] = 1.0 / (0.5 + (fProd[r] || 0));
    }
    const COMBINED_DIVERSITY_BONUS = {
        0: 0.0, 1: 0.0, 2: 0.0, 3: 0.05, 4: 0.15, 5: 0.25,
    };
    const scarcity = opts.tableScarcity || computeTableScarcity(board);
    const legal = opts.legalNodes
        || legalNodesAfterPicks(board, [firstNodeId]);
    const scores = [];
    for (const nid of legal) {
        if (nid === firstNodeId) continue;
        const node = board.nodes[nid];
        if (!node) continue;
        const nProd = nodeProduction(board, nid);
        const raw = Object.values(nProd).reduce((s, v) => s + v, 0);
        let complement = 0;
        for (const r of RESOURCES) {
            complement += (nProd[r] || 0) * marginal[r];
        }
        const combined = {};
        for (const r of RESOURCES) {
            combined[r] = (fProd[r] || 0) + (nProd[r] || 0);
        }
        const distinct = Object.values(combined).filter(v => v > 0).length;
        const dBonus = COMBINED_DIVERSITY_BONUS[distinct] || 0.25;
        const tiles = node.tiles
            .map(tid => board.tiles[tid])
            .filter(Boolean)
            .map(t => [t.resource || 'DESERT', t.number]);
        const pBonus = portBonus(node.port, combined, tiles, scarcity);
        scores.push({
            nodeId: nid,
            rawProduction: raw,
            resources: nProd,
            complementValue: complement,
            combinedDistinct: distinct,
            diversityBonus: dBonus,
            port: node.port || null,
            portBonus: pBonus,
            tiles,
            score: complement + dBonus + pBonus,
        });
    }
    scores.sort((a, b) => b.score - a.score);
    return scores;
}

/** Rank every land tile for robber placement value. Mirror of
 *  advisor.score_robber_targets. Phase 4 work — stub for now. */
export function scoreRobberTargets(state, opts = {}) {
    if (!state || !state.map) return [];
    return [];
}

export { DIVERSITY_BY_COUNT, RESOURCE_WEIGHT };
