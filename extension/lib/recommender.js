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

// Per-resource weight for settlement scoring and ranking. Mirrors
// _RESOURCE_WEIGHT in advisor.py (Reddit finding #2: wheat is in every
// major build, so a wheat corner edges out an equal-pip non-wheat one).
const _RESOURCE_WEIGHT = {
    WHEAT: 1.10, WOOD: 1.0, BRICK: 1.0, SHEEP: 1.0, ORE: 1.0,
};

/** Wheat-weighted production sum. Mirrors recommender.py's
 *  _node_pip_production_weighted. */
function _weightedProd(prod) {
    let w = 0;
    for (const [r, v] of Object.entries(prod || {})) {
        w += (v || 0) * (_RESOURCE_WEIGHT[r] || 1.0);
    }
    return w;
}

/** Settlement 1-10 score. Mirrors recommender.py: _score_settlement
 *  applied to the WHEAT-WEIGHTED production, with no diversity
 *  multiplier. The bridge has no diversity term here, so the old JS
 *  (raw total times a 1.0/1.08/1.22 diversity factor) showed a
 *  different score, and sometimes a different #1 pick, for the same
 *  corner, which also skewed move-quality grading against it.
 *  clip(weighted * 12 + 2, 2, 10). */
function _scoreSettlement(prod) {
    return Math.round(
        _clip(_weightedProd(prod) * 12.0 + 2.0, 2.0, 10.0) * 10) / 10;
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
    // Rank by wheat-weighted production, matching recommender.py's
    // scored.sort(key=-weighted). The displayed score is monotonic in
    // the weighted prod, but sorting on the weight directly keeps ties
    // at the clip bounds ordered the way the bridge orders them.
    recs.sort((a, b) =>
        _weightedProd(b.resources) - _weightedProd(a.resources));
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
 *  up to 3 recs. Now handles multi-swap trades (e.g. 2 separate
 *  port/bank trades to get 2 missing resources). */
function _bankTradeRecs(state, hand, opts) {
    const board = state.map;
    const recs = [];
    if (!hand) return recs;
    // What ports do we touch? Pull from owned settlements + cities.
    const ownPorts = new Set();   // resource set with 2:1 access
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
    // Cheapest swaps to satisfy `need` from `hand`. Returns null if
    // no combination of port/bank trades can cover the missing
    // resources, or {give, get, totalSpent} when feasible.
    function planSwaps(hand, need) {
        const give = {};
        const get = { ...need };
        const remaining = { ...hand };
        for (const [r, n] of Object.entries(need)) {
            let stillNeed = n;
            // Each iteration spends `ratio` of some surplus to gain 1.
            while (stillNeed > 0) {
                let bestSurplus = null;
                let bestRatio = Infinity;
                for (const sr of RESOURCE_NAMES) {
                    if (sr === r) continue;
                    const have = remaining[sr] || 0;
                    const rt = ratioFor(sr);
                    if (have >= rt && rt < bestRatio) {
                        bestSurplus = sr;
                        bestRatio = rt;
                    }
                }
                if (!bestSurplus) return null;
                give[bestSurplus] = (give[bestSurplus] || 0) + bestRatio;
                remaining[bestSurplus] -= bestRatio;
                stillNeed -= 1;
            }
        }
        return { give, get,
                 totalSpent: Object.values(give)
                    .reduce((s, v) => s + v, 0) };
    }
    // Identify the highest-VP build target reachable via trades.
    const wanted = [
        ['city', COSTS.city, 7.5],
        ['settlement', COSTS.settlement, 7.0],
        ['dev_card', COSTS.dev_card, 5.5],
        ['road', COSTS.road, 5.0],
    ];
    for (const [target, cost, baseScore] of wanted) {
        if (handCanAfford(hand, cost)) continue;
        const need = _missing(hand, cost);
        // Verify trading away resources doesn't leave us short on
        // anything still needed for the build itself.
        const plan = planSwaps(hand, need);
        if (!plan) continue;
        // Confirm post-trade hand can pay the build cost.
        const postHand = { ...hand };
        for (const [r, n] of Object.entries(plan.give)) {
            postHand[r] = (postHand[r] || 0) - n;
        }
        for (const [r, n] of Object.entries(plan.get)) {
            postHand[r] = (postHand[r] || 0) + n;
        }
        if (!handCanAfford(postHand, cost)) continue;
        // Score: base − 0.5 per extra trade beyond the first.
        const swaps = plan.totalSpent;
        const score = Math.max(2.0,
            baseScore - 0.5 * (swaps - Object.keys(plan.give).length));
        recs.push({
            kind: 'bank_trade',
            when: 'now',
            score: Math.round(score * 10) / 10,
            detail: swaps === Object.keys(plan.give).length
                ? `trade → unlock ${target}`
                : `${swaps}-card trade → ${target}`,
            give: plan.give,
            get: plan.get,
            target_kind: target,
        });
    }
    recs.sort((a, b) => b.score - a.score);
    return recs.slice(0, 3);
}

/** Dev-card buy rec. Score scales with how stuck the player is
 *  (no settle/road/city better than this) and game phase — mid-late
 *  it's a serious play, opening it's noise. */
function _devCardRec(state, hand, otherRecs) {
    const can = handCanAfford(hand, COSTS.dev_card);
    // Best non-dev now-rec score, if any. If no settle/road/city
    // is even close, dev card becomes the primary play.
    const nonDevTop = (otherRecs || [])
        .filter(r => r.kind !== 'dev_card' && r.when === 'now')
        .reduce((m, r) => Math.max(m, r.score || 0), 0);
    let score = 4.5;
    if (state.totalRolls >= 8) score += 0.5;
    if (nonDevTop === 0) score += 1.5; // nothing else affordable
    if (state.hasArmy === state.selfColor) score -= 0.5;
    score = Math.min(8.0, Math.max(2.5, score));
    return {
        kind: 'dev_card',
        when: can ? 'now' : 'soon',
        score: Math.round(score * 10) / 10,
        detail: 'buy dev card',
        missing: can ? null : _missing(hand, COSTS.dev_card),
    };
}

/** Player-to-player trade proposals. When self is exactly 1 card
 *  short of a build target AND we have a surplus of another
 *  resource, suggest a 1-for-1 fair trade with whichever opp
 *  produces the missing resource (or fallback to "any opp").
 *  Mirrors recommender._propose_trades in slim form.
 *
 *  Ships `kind=propose_trade` with `give`, `get`, `unlocks`,
 *  `variant` ("1:1 fair") so the panel's existing trade-rec
 *  renderer at panel.js line ~2070 picks them up unchanged.
 */
function _proposeTradeRecs(state, hand, opts) {
    const board = state.map;
    if (!board) return [];
    const recs = [];
    const wanted = [
        ['city', COSTS.city, 6.5, 'city'],
        ['settlement', COSTS.settlement, 6.0, 'settlement'],
        ['road', COSTS.road, 4.5, 'road'],
        ['dev_card', COSTS.dev_card, 4.0, 'dev card'],
    ];
    for (const [target, cost, baseScore, kindWord] of wanted) {
        if (handCanAfford(hand, cost)) continue;
        const need = _missing(hand, cost);
        const needKeys = Object.keys(need);
        // Only emit propose-trade when we're short by exactly 1
        // resource type AND the deficit is just 1 card. Anything
        // bigger is better served by bank/port trades or saving up.
        if (needKeys.length !== 1) continue;
        const needRes = needKeys[0];
        if (need[needRes] !== 1) continue;
        // Find a surplus we'd offer. Has to NOT be needed by the
        // build itself.
        for (const surplus of RESOURCE_NAMES) {
            if (surplus === needRes) continue;
            const have = hand[surplus] || 0;
            const reservedByCost = cost[surplus] || 0;
            if (have - 1 < reservedByCost) continue;
            // Score floor at 1.5 so we don't outrank affordable
            // builds, but high enough to clearly beat the dev-card
            // fallback when a real trade unlocks a build.
            const score = Math.max(1.5,
                Math.min(8.0, baseScore));
            recs.push({
                kind: 'propose_trade',
                when: 'now',
                score: Math.round(score * 10) / 10,
                give: { [surplus]: 1 },
                get: { [needRes]: 1 },
                unlocks: target,
                variant: '1:1 fair',
                detail: `offer 1 ${surplus.toLowerCase()} for `
                    + `1 ${needRes.toLowerCase()} · `
                    + `unlocks ${kindWord}`,
            });
            break;  // one surplus offer per blocked build
        }
    }
    return recs.slice(0, 3);
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
    recs.push(..._proposeTradeRecs(state, hand, opts));
    recs.push(_devCardRec(state, hand, recs));

    // Filter out duplicates by (kind, node_id|edge|target).
    const seen = new Set();
    const out = [];
    for (const r of recs) {
        const key = `${r.kind}|${r.node_id || r.edge || r.target_kind || ''}`;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(r);
    }
    // Filter dups: a propose_trade for `target=settlement` shouldn't
    // co-exist with a bank_trade for the same target. Bank/port is
    // preferred when affordable since it doesn't depend on opp
    // accepting; player trade only fires when no bank-trade rec
    // already covered the same build.
    const bankTargets = new Set(
        out.filter(r => r.kind === 'bank_trade')
            .map(r => r.target_kind));
    const filtered = out.filter(r =>
        !(r.kind === 'propose_trade' && bankTargets.has(r.unlocks)));
    filtered.sort((a, b) => {
        // 'now' beats 'soon' in display order (panel splits by when
        // already, but keeping the global ranking sane is nice).
        if (a.when !== b.when) return a.when === 'now' ? -1 : 1;
        return b.score - a.score;
    });
    const topK = opts.topK || 12;
    return filtered.slice(0, topK);
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
        let oppAdj = adj.filter(x => !x.isSelf);
        const selfAdj = adj.filter(x => x.isSelf);
        // No opponent building on this tile means no steal value, so it
        // can't be a robber target. But do NOT drop a tile just because
        // we also touch it: the bridge keeps self-adjacent tiles
        // (own_blocked is a score penalty, not a drop), and dropping
        // them here meant that on boards where every opp-adjacent
        // productive tile also touches one of our own settlements the
        // whole list came back empty and the panel rendered no robber
        // table at all (Bug 1, standalone). Keep the tile and penalize
        // its score below instead.
        if (oppAdj.length === 0) continue;
        // Friendly Robber filter: colonist's optional rule protects
        // any player at ≤ 2 VP from being the rob target. When
        // active, drop opps below the threshold from the victim
        // pool so the panel doesn't suggest moves the game won't
        // let you make.
        if (opts.friendlyRobber) {
            oppAdj = oppAdj.filter(b => {
                const vp = state.vp[b.color] || 0;
                return vp > 2;
            });
            if (oppAdj.length === 0) continue;
        }
        // Score: pip × oppPiecesValue × steal-EV
        const pip = pipsForNumber(tile.number);
        let pieceValue = 0;
        const oppByColor = {};
        for (const a of oppAdj) {
            pieceValue += a.kind === 'CITY' ? 2 : 1;
            oppByColor[a.color] = (oppByColor[a.color] || 0) + 1;
        }
        // Self-block penalty: parking the robber on a tile we also
        // produce from costs us pips, so a self-adjacent tile sinks in
        // the ranking (mirrors the bridge's own_blocked weighting) while
        // still appearing rather than vanishing from the list.
        let selfPieceValue = 0;
        for (const a of selfAdj) {
            selfPieceValue += a.kind === 'CITY' ? 2 : 1;
        }
        // Build the victims array — each adjacent opp w/ vp + card count
        // + suggested flag set on the highest-card holder. Mirrors the
        // bridge's bridge_robber.score_robber_targets output so the
        // panel's robber-target table renders unchanged.
        const COLONIST_COLOR_NAME = {
            '1': 'RED', '2': 'BLUE', '3': 'ORANGE',
            '4': 'WHITE', '5': 'GREEN', '6': 'BROWN',
        };
        const COLONIST_COLOR_HEX = {
            '1': '#e8715f', '2': '#4aa7d4',
            '3': '#e29a4a', '4': '#f0f0f0',
            '5': '#7ac74f', '6': '#a07045',
        };
        let bestVictim = null;
        let bestVictimCards = -1;
        for (const c of Object.keys(oppByColor)) {
            const cards = state.handTotal[c] || 0;
            if (cards > bestVictimCards) {
                bestVictimCards = cards;
                bestVictim = c;
            }
        }
        const victims = Object.keys(oppByColor).map((c) => ({
            username: COLONIST_COLOR_NAME[String(c)] || `P${c}`,
            color: COLONIST_COLOR_NAME[String(c)] || `P${c}`,
            color_css: COLONIST_COLOR_HEX[String(c)] || '#888',
            pips: oppByColor[c] * pip,
            vp: state.vp[c] || 0,
            cards: state.handTotal[c] || 0,
            suggested: c === bestVictim,
        }));
        const score = pip * pieceValue + (bestVictimCards || 0) * 1.5
            - pip * selfPieceValue;
        targets.push({
            tile_id: tid,
            number: tile.number,
            resource: tile.resource,
            pip,
            score,
            opp_pieces: oppAdj.length,
            steal_from_color: bestVictim,
            victim_hand_size: bestVictimCards >= 0 ? bestVictimCards : 0,
            victims,
        });
    }
    targets.sort((a, b) => b.score - a.score);
    return targets.slice(0, opts.topK || 5);
}

export { COSTS, _scoreSettlement, _weightedProd };
