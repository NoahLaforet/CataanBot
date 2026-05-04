// recommender.js — in-game action recommender, JS port.
//
// Heuristic mirror of src/catanbot/recommender.py. Computes a ranked
// list of action recs (city / settlement / road / dev card / bank
// trade) from the snapshot-derived game state. Each rec carries a
// 1-10 score on the same scale recommender.py uses so the panel can
// render them through the same `renderRec` path that bridge mode
// produces.
//
// Score calibration (matches recommender.py):
//    * 10 = exceptional move; clear best
//    * 7-9 = strong, do this
//    * 4-6 = decent
//    * 1-3 = weak / last-resort
//
// We deliberately skip 1-ply search rerank (depends on catanatron's
// Game.copy() — bridge-only). Standalone is calibrated so the
// heuristic top pick lines up with the bridge's heuristic top pick;
// the search delta column in the panel just stays empty.

import { newHand, RESOURCE_NAMES } from './state.js';
import { nodeProduction, pipsForNumber } from './board.js';

const COSTS = {
    settlement: { WOOD: 1, BRICK: 1, SHEEP: 1, WHEAT: 1 },
    city: { WHEAT: 2, ORE: 3 },
    road: { WOOD: 1, BRICK: 1 },
    dev_card: { SHEEP: 1, WHEAT: 1, ORE: 1 },
};

function _clip(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

/** Settlement 1-10 score from corner production. */
function _scoreSettlement(prod) {
    const total = Object.values(prod).reduce((s, v) => s + v, 0);
    return Math.round(_clip(total * 12.0 + 2.0, 2.0, 10.0) * 10) / 10;
}

/** City 1-10 score: doubled production + 3 base. */
function _scoreCity(prod) {
    const total = Object.values(prod).reduce((s, v) => s + v, 0);
    return Math.round(_clip(total * 10.0 + 3.0, 4.0, 10.0) * 10) / 10;
}

/** Road 1-10 score from the production at its best landing node.
 *  Capped at 7 so a direct settle always outranks the road toward
 *  the same spot. */
function _scoreRoad(landingProd) {
    const total = Object.values(landingProd).reduce((s, v) => s + v, 0);
    return Math.round(_clip(total * 9.0, 1.0, 7.0) * 10) / 10;
}

/** Helper: can hand cover cost? */
export function handCanAfford(hand, cost) {
    if (!hand || !cost) return false;
    for (const [r, n] of Object.entries(cost)) {
        if ((hand[r] || 0) < n) return false;
    }
    return true;
}

/** Resources missing from hand to reach cost. */
function _missing(hand, cost) {
    const out = {};
    for (const [r, n] of Object.entries(cost)) {
        const have = hand[r] || 0;
        if (have < n) out[r] = n - have;
    }
    return out;
}

/** Tile descriptor list for a node (for the rec's tiles field). */
function _nodeTiles(board, nodeId) {
    const node = board.nodes[nodeId];
    if (!node) return [];
    return node.tiles
        .map(tid => board.tiles[tid])
        .filter(Boolean)
        .map(t => [t.resource || 'DESERT', t.number]);
}

/** Edge tiles — the 1-2 tiles that touch an edge between nodeA/nodeB. */
function _edgeTiles(board, edgeId) {
    const edge = board.edges[edgeId];
    if (!edge) return [];
    const a = board.nodes[edge.a], b = board.nodes[edge.b];
    if (!a || !b) return [];
    const tilesA = new Set(a.tiles);
    const tilesB = new Set(b.tiles);
    const out = [];
    for (const tid of tilesA) {
        if (tilesB.has(tid)) {
            const t = board.tiles[tid];
            if (t) out.push([t.resource || 'DESERT', t.number]);
        }
    }
    return out;
}

/** Set of nodes that own-color owns (settles or cities). */
function _ownNodes(state) {
    const out = new Set();
    if (!state.selfColor) return out;
    for (const [nid, b] of Object.entries(state.buildings)) {
        if (b.color === state.selfColor) out.add(nid);
    }
    return out;
}

/** Set of nodes ANY color owns — used for distance-rule legality. */
function _allNodes(state) {
    return new Set(Object.keys(state.buildings));
}

/** Set of edges owned by self. */
function _ownEdges(state) {
    const out = new Set();
    if (!state.selfColor) return out;
    for (const [eid, c] of Object.entries(state.roads)) {
        if (c === state.selfColor) out.add(eid);
    }
    return out;
}

/** Nodes reachable from own roads/settles for fresh settle placement
 *  (must be connected to your road network AND legal under the
 *  distance rule). */
function _legalSettleNodes(state) {
    const board = state.map;
    if (!board) return new Set();
    const ownEdges = _ownEdges(state);
    const allNodes = _allNodes(state);
    const reachable = new Set();
    // Endpoints of own edges + endpoints adjacent to own settles
    // count as reachable starts.
    for (const eid of ownEdges) {
        const e = board.edges[eid];
        if (!e) continue;
        reachable.add(e.a);
        reachable.add(e.b);
    }
    for (const nid of _ownNodes(state)) reachable.add(nid);
    const legal = new Set();
    for (const nid of reachable) {
        if (!board.landNodes.has(nid)) continue;
        if (allNodes.has(nid)) continue;
        // Distance rule: no neighbour can be built on.
        const node = board.nodes[nid];
        if (!node) continue;
        let blocked = false;
        for (const nb of node.neighbors) {
            if (allNodes.has(nb)) { blocked = true; break; }
        }
        if (!blocked) legal.add(nid);
    }
    return legal;
}

/** Edges adjacent to own road network where we could place a new
 *  road. Excludes edges already owned by anyone, and excludes edges
 *  whose only endpoint connection is broken by an opp settle (per
 *  Catan rules an opponent's settlement cuts your road). */
function _legalRoadEdges(state) {
    const board = state.map;
    if (!board) return new Set();
    const ownEdges = _ownEdges(state);
    const ownNodes = _ownNodes(state);
    const buildings = state.buildings;
    const out = new Set();
    // Anchor nodes: endpoints of own edges + own settle/city nodes.
    const anchors = new Set();
    for (const eid of ownEdges) {
        const e = board.edges[eid];
        if (!e) continue;
        anchors.add(e.a); anchors.add(e.b);
    }
    for (const nid of ownNodes) anchors.add(nid);
    for (const anchor of anchors) {
        // Opp settle on the anchor cuts the road extension out of
        // that node — skip if it isn't yours.
        const blockedByOpp = buildings[anchor]
            && buildings[anchor].color !== state.selfColor;
        if (blockedByOpp) continue;
        const node = board.nodes[anchor];
        if (!node) continue;
        for (const nb of node.neighbors) {
            const eid = anchor < nb
                ? `${anchor}||${nb}` : `${nb}||${anchor}`;
            if (state.roads[eid]) continue;  // taken
            out.add(eid);
        }
    }
    return out;
}

/** Compute the best landing-node production for an edge — used by
 *  the road score. The "landing" is whichever endpoint is currently
 *  not anchored to your network (the new ground you'd reach). */
function _bestLanding(state, edgeId) {
    const board = state.map;
    const edge = board.edges[edgeId];
    if (!edge) return { prod: {}, total: 0, nodeId: null };
    const ownNet = new Set();
    for (const nid of _ownNodes(state)) ownNet.add(nid);
    for (const eid of _ownEdges(state)) {
        const e = board.edges[eid];
        if (!e) continue;
        ownNet.add(e.a); ownNet.add(e.b);
    }
    const allNodes = _allNodes(state);
    const candidates = [edge.a, edge.b].filter(n => !ownNet.has(n));
    const lookAt = candidates.length ? candidates : [edge.a, edge.b];
    let best = { prod: {}, total: -1, nodeId: null };
    for (const nid of lookAt) {
        // Distance-rule blocked? If yes, the landing is worth less
        // (still place value on opening up the *next* hop).
        const node = board.nodes[nid];
        if (!node) continue;
        let nbBuilt = false;
        for (const nb of node.neighbors) {
            if (allNodes.has(nb) && nb !== nid) { nbBuilt = true; break; }
        }
        const prod = nodeProduction(board, nid);
        let total = Object.values(prod).reduce((s, v) => s + v, 0);
        if (allNodes.has(nid)) total = 0;
        if (nbBuilt) total *= 0.5;
        if (total > best.total) best = { prod, total, nodeId: nid };
    }
    return best;
}

/** Rank own-settle nodes for city upgrade. Returns up to top-3 recs
 *  sorted desc by score. */
function _cityRecs(state, hand, opts) {
    const board = state.map;
    const out = [];
    const cityBank = (state.bank[state.selfColor] || {}).cities;
    if (cityBank === 0) return out;
    for (const nid of _ownNodes(state)) {
        const b = state.buildings[nid];
        if (!b || b.kind !== 'SETTLEMENT') continue;
        const prod = nodeProduction(board, nid);
        const score = _scoreCity(prod);
        const tiles = _nodeTiles(board, nid);
        const can = handCanAfford(hand, COSTS.city);
        out.push({
            kind: 'city',
            when: can ? 'now' : 'soon',
            score,
            detail: 'upgrade settlement → city',
            node_id: nid,
            tiles,
            missing: can ? null : _missing(hand, COSTS.city),
            resources: prod,
        });
    }
    out.sort((a, b) => b.score - a.score);
    return out.slice(0, 3);
}

/** Rank legal new-settle nodes. Returns up to top-3 recs. */
function _settleRecs(state, hand, opts) {
    const board = state.map;
    const settleBank = (state.bank[state.selfColor] || {}).settles;
    if (settleBank === 0) return [];
    const legal = _legalSettleNodes(state);
    const recs = [];
    for (const nid of legal) {
        const prod = nodeProduction(board, nid);
        const score = _scoreSettlement(prod);
        const tiles = _nodeTiles(board, nid);
        const can = handCanAfford(hand, COSTS.settlement);
        recs.push({
            kind: 'settlement',
            when: can ? 'now' : 'soon',
            score,
            detail: 'place settlement',
            node_id: nid,
            tiles,
            missing: can ? null : _missing(hand, COSTS.settlement),
            resources: prod,
            port: board.nodes[nid]?.port || null,
        });
    }
    recs.sort((a, b) => b.score - a.score);
    return recs.slice(0, 3);
}

/** Rank legal new road edges. Returns up to top-3 recs. */
function _roadRecs(state, hand, opts) {
    const board = state.map;
    const roadBank = (state.bank[state.selfColor] || {}).roads;
    if (roadBank === 0) return [];
    const legal = _legalRoadEdges(state);
    const recs = [];
    for (const eid of legal) {
        const landing = _bestLanding(state, eid);
        if (landing.total <= 0) continue;
        const score = _scoreRoad(landing.prod);
        const tiles = _edgeTiles(board, eid);
        const can = handCanAfford(hand, COSTS.road);
        recs.push({
            kind: 'road',
            when: can ? 'now' : 'soon',
            score,
            detail: 'extend road',
            edge: eid,
            tiles,
            missing: can ? null : _missing(hand, COSTS.road),
            resources: landing.prod,
            landing_node_id: landing.nodeId,
        });
    }
    recs.sort((a, b) => b.score - a.score);
    return recs.slice(0, 3);
}

/** Bank/port trades that unlock a useful build this turn. Returns
 *  up to 2 recs. */
function _bankTradeRecs(state, hand, opts) {
    const board = state.map;
    const recs = [];
    if (!hand) return recs;
    // What ports do we touch?
    const ownPorts = new Set();   // resource → '2:1'
    let has31 = false;
    for (const nid of _ownNodes(state)) {
        const port = board.nodes[nid]?.port;
        if (!port) continue;
        if (port.kind === '3:1') { has31 = true; continue; }
        if (port.resource) ownPorts.add(port.resource);
    }
    const ratioFor = (res) => {
        if (ownPorts.has(res)) return 2;
        if (has31) return 3;
        return 4;
    };
    // Identify the nearest affordable build target.
    const wanted = [
        ['city', COSTS.city],
        ['settlement', COSTS.settlement],
        ['road', COSTS.road],
        ['dev_card', COSTS.dev_card],
    ];
    for (const [target, cost] of wanted) {
        if (handCanAfford(hand, cost)) continue;
        const need = _missing(hand, cost);
        if (Object.keys(need).length !== 1) continue; // need just one swap
        const [needRes, needCount] = Object.entries(need)[0];
        if (needCount > 1) continue;  // single-swap recs only for now
        // Find a resource we have a surplus of (ratio + 0 buffer).
        for (const surplus of RESOURCE_NAMES) {
            if (surplus === needRes) continue;
            const ratio = ratioFor(surplus);
            const have = hand[surplus] || 0;
            if (have < ratio) continue;
            // Don't trade away resources we still need for the cost.
            const stillNeed = (cost[surplus] || 0)
                - (hand[surplus] || 0);
            if (stillNeed > 0 && have - ratio < (cost[surplus] || 0)) continue;
            recs.push({
                kind: 'bank_trade',
                when: 'now',
                score: target === 'city' ? 7.5
                       : (target === 'settlement' ? 7.0
                       : (target === 'dev_card' ? 5.5 : 5.0)),
                detail: `trade ${ratio}:1 → unlock ${target}`,
                give: { [surplus]: ratio },
                get: { [needRes]: 1 },
                target_kind: target,
                ratio,
            });
            break;
        }
    }
    recs.sort((a, b) => b.score - a.score);
    return recs.slice(0, 2);
}

/** Dev-card buy rec — simple, fires when affordable + bank has cards. */
function _devCardRec(state, hand) {
    if (!handCanAfford(hand, COSTS.dev_card)) {
        return {
            kind: 'dev_card',
            when: 'soon',
            score: 4.5,
            detail: 'buy dev card',
            missing: _missing(hand, COSTS.dev_card),
        };
    }
    return {
        kind: 'dev_card',
        when: 'now',
        score: 4.5,
        detail: 'buy dev card',
    };
}

/** Build a ranked rec list. opts: { topK, includeSoon }. */
export function recommendActions(state, opts = {}) {
    if (!state || !state.selfColor || !state.map) return [];
    const hand = state.hands[state.selfColor] || newHand();
    const recs = [];
    recs.push(..._cityRecs(state, hand, opts));
    recs.push(..._settleRecs(state, hand, opts));
    recs.push(..._roadRecs(state, hand, opts));
    recs.push(..._bankTradeRecs(state, hand, opts));
    recs.push(_devCardRec(state, hand));

    // Filter out duplicates by (kind, node_id|edge|target).
    const seen = new Set();
    const out = [];
    for (const r of recs) {
        const key = `${r.kind}|${r.node_id || r.edge || r.target_kind || ''}`;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(r);
    }
    out.sort((a, b) => {
        // 'now' beats 'soon' in display order (panel splits by when
        // already, but keeping the global ranking sane is nice).
        if (a.when !== b.when) return a.when === 'now' ? -1 : 1;
        return b.score - a.score;
    });
    const topK = opts.topK || 12;
    return out.slice(0, topK);
}

/** Robber-target ranking — bridge had this in advisor.score_robber_targets;
 *  the standalone advisor.js stub returns []. We fill it in here so the
 *  panel's robber-target list works after a 7. */
export function recommendRobberTargets(state, opts = {}) {
    if (!state || !state.map) return [];
    const tiles = state.map.tiles || {};
    const targets = [];
    const buildings = state.buildings || {};
    for (const [tid, tile] of Object.entries(tiles)) {
        if (!tile.resource || !tile.number) continue;
        if (tid === state.robberTile) continue; // can't stay on same tile
        // Adjacent buildings (and their colors).
        const adj = [];
        for (const nid of tile.nodes) {
            const b = buildings[nid];
            if (!b) continue;
            if (b.color === state.selfColor) {
                // Self adjacency disqualifies (you'd be blocking
                // yourself); skip with 0 score.
                adj.push({ ...b, isSelf: true });
            } else {
                adj.push({ ...b, isSelf: false });
            }
        }
        const oppAdj = adj.filter(x => !x.isSelf);
        const hasSelfAdj = adj.some(x => x.isSelf);
        if (oppAdj.length === 0) continue;
        if (hasSelfAdj) continue;
        // Score: pip × oppPiecesValue × steal-EV
        const pip = pipsForNumber(tile.number);
        let pieceValue = 0;
        const oppByColor = {};
        for (const a of oppAdj) {
            pieceValue += a.kind === 'CITY' ? 2 : 1;
            oppByColor[a.color] = (oppByColor[a.color] || 0) + 1;
        }
        // Steal candidate: opp w/ most cards adj to this tile.
        let bestVictim = null;
        let bestVictimCards = 0;
        for (const c of Object.keys(oppByColor)) {
            const cards = state.handTotal[c] || 0;
            if (cards > bestVictimCards) {
                bestVictimCards = cards;
                bestVictim = c;
            }
        }
        const score = pip * pieceValue + (bestVictimCards || 0) * 1.5;
        targets.push({
            tile_id: tid,
            number: tile.number,
            resource: tile.resource,
            pip,
            score,
            opp_pieces: oppAdj.length,
            steal_from_color: bestVictim,
            victim_hand_size: bestVictimCards,
        });
    }
    targets.sort((a, b) => b.score - a.score);
    return targets.slice(0, opts.topK || 5);
}

export { COSTS };
