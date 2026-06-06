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
import { planBankTrades } from './trades.js';
import { computeStrategy } from './strategy.js';

const COSTS = {
    settlement: { WOOD: 1, BRICK: 1, SHEEP: 1, WHEAT: 1 },
    city: { WHEAT: 2, ORE: 3 },
    road: { WOOD: 1, BRICK: 1 },
    dev_card: { SHEEP: 1, WHEAT: 1, ORE: 1 },
};

// Flat dev-card score, mirrors recommender.py _DEV_CARD_SCORE. Used as
// the unlocked-build base for a bank trade that buys a dev card.
const _DEV_CARD_SCORE = 3.0;

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

/** Best settleable landing this road brings into reach. Mirrors
 *  recommender.py's best-landing walk: for each edge endpoint, scan
 *  its neighbours (the distance-2 nodes a follow-up road could settle)
 *  and keep the highest-ranked UNBLOCKED, on-land one. "Blocked" = any
 *  occupied node or a node adjacent to one (the distance-2 settlement
 *  rule), so blocked landings are EXCLUDED, not discounted. The old
 *  code scored the edge endpoint itself (distance-1, almost always
 *  blocked off your own settle) and halved it, which ran road scores
 *  low. Ranking uses raw production × diversity × a 2:1-port bonus on
 *  an owned resource, but the stored `total` is the raw production so
 *  the road score stays comparable to settlement scores. */
function _bestLanding(state, edgeId, blocked, ownedResources) {
    const board = state.map;
    const edge = board.edges[edgeId];
    if (!edge) return { prod: {}, total: 0, nodeId: null, rank: 0 };
    let best = { prod: {}, total: -1, nodeId: null, rank: 0 };
    let bestRank = -1;
    for (const end of [edge.a, edge.b]) {
        const endNode = board.nodes[end];
        if (!endNode) continue;
        for (const nb of endNode.neighbors) {
            if (blocked.has(nb)) continue;
            if (!board.landNodes.has(nb)) continue;
            const prod = nodeProduction(board, nb);
            const raw = Object.values(prod).reduce((s, v) => s + v, 0);
            let distinct = 0;
            for (const v of Object.values(prod)) if (v > 0) distinct += 1;
            const diversity = distinct >= 3 ? 1.15
                : distinct === 2 ? 1.05 : 1.0;
            let portBonus = 1.0;
            const port = board.nodes[nb] && board.nodes[nb].port;
            // Only a 2:1 port on a resource self produces is worth the
            // landing bump. The bridge reads node_to_port and explicitly
            // excludes the 3:1 generic port (recommender.py 1285-1296);
            // match that guard here rather than relying on a 3:1 having no
            // .resource, so a board model that ever stamps a resource on a
            // generic port can't accidentally inflate the rank.
            if (port && port.resource && port.kind !== '3:1'
                    && ownedResources.has(port.resource)) {
                portBonus = 1.4;
            }
            const rank = raw * diversity * portBonus;
            if (rank > bestRank) {
                bestRank = rank;
                best = { prod, total: raw, nodeId: nb, rank };
            }
        }
    }
    return best;
}

/** Resources self currently produces (>0 cards/roll), for the road
 *  landing's 2:1-port bonus. */
function _ownedResources(state) {
    const out = new Set();
    const board = state.map;
    for (const nid of _ownNodes(state)) {
        const prod = nodeProduction(board, nid);
        for (const [r, v] of Object.entries(prod)) if (v > 0) out.add(r);
    }
    return out;
}

/** Per-roll expected cards for a production map, formatted like the
 *  bridge's `+{prod:.2f}/roll` detail (the values are pip/36 shares, so
 *  their sum is the node's expected cards per roll). */
export function _perRoll(prodMap) {
    let s = 0;
    for (const v of Object.values(prodMap || {})) s += v;
    return s.toFixed(2);
}

// Single-letter resource labels for the per-resource rationale line.
// Mirrors recommender.py _RES_LETTER (Sh / Wh disambiguate sheep/wheat).
const _RES_LETTER = {
    WOOD: 'W', BRICK: 'B', SHEEP: 'Sh', WHEAT: 'Wh', ORE: 'O',
};

/** Per-resource /roll breakdown line, biggest resource first. Mirrors
 *  recommender.py _breakdown_per_roll: '+0.14 Wh +0.08 O +0.08 B /roll'.
 *  Empty when the node produces nothing. */
function _breakdownPerRoll(prodMap) {
    const items = Object.entries(prodMap || {})
        .map(([r, v]) => [r, Number(v) || 0])
        .filter(([, v]) => v > 0);
    if (!items.length) return '';
    items.sort((a, b) => b[1] - a[1]);
    const parts = items.map(
        ([r, v]) => `+${v.toFixed(2)} ${_RES_LETTER[r] || r[0]}`);
    return parts.join(' ') + ' /roll';
}

/** Settlement rationale: the per-resource breakdown plus a weak-fill hint
 *  when the spot's biggest resource is one self barely produces (self
 *  expected /roll <= 0.05). Mirrors recommender.py _settle_rationale.
 *  `selfExpected` is the per-resource /roll map across self's buildings. */
function _settleRationale(prodMap, selfExpected) {
    let line = _breakdownPerRoll(prodMap);
    if (!line) return '';
    if (selfExpected) {
        let topRes = null;
        let topVal = -1;
        for (const [r, v] of Object.entries(prodMap || {})) {
            if ((Number(v) || 0) > topVal) { topVal = Number(v) || 0; topRes = r; }
        }
        if (topRes != null
                && (Number(selfExpected[topRes]) || 0) <= 0.05) {
            line += ` · fills ${_RES_LETTER[topRes] || topRes[0]} (weak)`;
        }
    }
    return line;
}

/** City rationale: a city doubles the existing settle, so the marginal
 *  add equals the current yield. Framed 'adds +X /roll'. Mirrors
 *  recommender.py _city_rationale. */
function _cityRationale(prodMap) {
    const items = Object.entries(prodMap || {})
        .map(([r, v]) => [r, Number(v) || 0])
        .filter(([, v]) => v > 0);
    if (!items.length) return '';
    items.sort((a, b) => b[1] - a[1]);
    const parts = items.map(
        ([r, v]) => `+${v.toFixed(2)} ${_RES_LETTER[r] || r[0]}`);
    return 'adds ' + parts.join(' ') + ' /roll';
}

/** Road rationale: longest-road progression when the +1 road crosses a
 *  meaningful threshold (qualifies at 5, ties/beats the opp max), else
 *  the current chain length. Mirrors recommender.py _road_rationale over
 *  the public roadLength / hasRoad state JS already tracks. */
function _roadRationale(state) {
    const color = state.selfColor;
    const selfLen = (state.roadLength && state.roadLength[color]) || 0;
    const hasLr = state.hasRoad === color;
    let oppMax = 0;
    let oppHolds = false;
    for (const c of (state.colors || [])) {
        if (c === color) continue;
        const ol = (state.roadLength && state.roadLength[c]) || 0;
        if (ol > oppMax) oppMax = ol;
        if (state.hasRoad === c) oppHolds = true;
    }
    const nextLen = selfLen + 1;
    if (!hasLr && nextLen >= 5 && nextLen > oppMax) {
        return oppHolds
            ? `extends to ${nextLen} → FLIPS LR (+2 VP)`
            : `extends to ${nextLen} → claims LR (+2 VP)`;
    }
    if (!hasLr && nextLen >= 5 && nextLen === oppMax) {
        return `extends to ${nextLen} · ties LR`;
    }
    if (hasLr && nextLen > oppMax + 1) {
        return `extends to ${nextLen}, pads LR`;
    }
    if (selfLen >= 3) {
        return `extends ${selfLen}-chain to ${nextLen}`;
    }
    return '';
}

/** Per-resource /roll across all of self's buildings (city 2x), the
 *  weak-fill anchor for settle rationales. Mirrors
 *  recommender.py _compute_self_expected_per_roll. */
function _selfExpectedPerRoll(state) {
    const board = state.map;
    const out = {};
    if (!board || !board.nodes) return out;
    for (const [nid, b] of Object.entries(state.buildings || {})) {
        if (b.color !== state.selfColor) continue;
        const node = board.nodes[nid];
        if (!node) continue;
        const mult = b.kind === 'CITY' ? 2 : 1;
        for (const tid of node.tiles) {
            const t = board.tiles[tid];
            if (!t || !t.resource || !t.pip) continue;
            out[t.resource] = (out[t.resource] || 0) + (t.pip / 36) * mult;
        }
    }
    return out;
}

/** Rank own-settle nodes for city upgrade. Returns up to top-3 recs
 *  sorted desc by score. */
/** "need 1 sheep 1 ore" prefix for a soon-plan rec detail (text form,
 *  matching the standalone's detail convention; the bridge's
 *  _format_missing uses emoji). Empty when nothing is missing. */
function _formatMissing(missing) {
    const parts = Object.entries(missing || {})
        .map(([r, n]) => `${n} ${String(r).toLowerCase()}`);
    return parts.length ? `need ${parts.join(' ')}` : '';
}

// Soon-plans surface a blocked build only when it is one-to-two cards
// away (bridge _PLAN_MAX_MISSING). A 3+ card gap is too speculative to
// nudge toward, so the bridge emits nothing; the standalone now matches.
const _PLAN_MAX_MISSING = 2;

function _cityRecs(state, hand, opts) {
    const board = state.map;
    const cityBank = (state.bank[state.selfColor] || {}).cities;
    if (cityBank === 0) return [];
    const can = handCanAfford(hand, COSTS.city);
    if (can) {
        // Now path: every own settlement is a buildable city, ranked by
        // production, top-3 (recommender.py 1131-1144).
        const out = [];
        for (const nid of _ownNodes(state)) {
            const b = state.buildings[nid];
            if (!b || b.kind !== 'SETTLEMENT') continue;
            const prod = nodeProduction(board, nid);
            out.push({
                kind: 'city',
                when: 'now',
                score: _scoreCity(prod),
                detail: `+${_perRoll(prod)}/roll · +1 VP`,
                node_id: nid,
                tiles: _nodeTiles(board, nid),
                missing: null,
                resources: prod,
                rationale: _cityRationale(prod),
            });
        }
        out.sort((a, b) => b.score - a.score);
        return out;
    }
    // Soon path: a single "save for X" plan on the best owned settlement,
    // gated on a 1-to-2 card gap (recommender.py 1521-1537). Three-plus
    // cards short is not surfaced.
    const miss = _missing(hand, COSTS.city);
    const total = Object.values(miss).reduce((s, v) => s + v, 0);
    if (total <= 0 || total > _PLAN_MAX_MISSING) return [];
    const best = _bestOwnedSettleSpot(state);
    if (!best || best.nodeId == null) return [];
    const prod = best.prodMap;
    return [{
        kind: 'city',
        when: 'soon',
        score: _scoreCity(prod),
        detail: `${_formatMissing(miss)} · +${_perRoll(prod)}/roll · +1 VP`,
        node_id: best.nodeId,
        tiles: _nodeTiles(board, best.nodeId),
        missing: miss,
        resources: prod,
        rationale: _cityRationale(prod),
    }];
}

/** A 2:1-port detail suffix (" · wheat port") when the node carries a
 *  port for a resource self already produces; else "". Mirrors the
 *  bridge's _port_detail_suffix. */
function _portSuffix(state, nodeId, ownedResources) {
    if (nodeId == null || !state.map.nodes) return '';
    const node = state.map.nodes[nodeId];
    const port = node && node.port;
    if (!port || !port.resource || port.kind === '3:1') return '';
    if (!ownedResources.has(port.resource)) return '';
    return ` · ${String(port.resource).toLowerCase()} port`;
}

/** Rank legal new-settle nodes. Returns up to top-3 recs. */
function _settleRecs(state, hand, opts) {
    const board = state.map;
    const settleBank = (state.bank[state.selfColor] || {}).settles;
    if (settleBank === 0) return [];
    const legal = _legalSettleNodes(state);
    // At exactly two footprints the next settle is the 3rd, the single
    // biggest winner-vs-loser predictor (bridge tags it " · settle #3").
    const footprints = Object.values(state.buildings || {})
        .filter(b => b.color === state.selfColor).length;
    const settle3 = footprints === 2 ? ' · settle #3' : '';
    const selfExpected = _selfExpectedPerRoll(state);
    const can = handCanAfford(hand, COSTS.settlement);
    if (can) {
        // Now path: every legal spot, ranked by wheat-weighted production
        // (recommender.py 1100-1127), top-3.
        const recs = [];
        for (const nid of legal) {
            const prod = nodeProduction(board, nid);
            recs.push({
                kind: 'settlement',
                when: 'now',
                score: _scoreSettlement(prod),
                detail: `+${_perRoll(prod)}/roll${settle3}`,
                node_id: nid,
                tiles: _nodeTiles(board, nid),
                missing: null,
                resources: prod,
                port: board.nodes[nid]?.port || null,
                rationale: _settleRationale(prod, selfExpected),
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
    // Soon path: a single "save for X" plan on the best legal spot, gated
    // on a 1-to-2 card gap (recommender.py 1498-1520). Three-plus cards
    // short is not surfaced.
    const miss = _missing(hand, COSTS.settlement);
    const total = Object.values(miss).reduce((s, v) => s + v, 0);
    if (total <= 0 || total > _PLAN_MAX_MISSING) return [];
    const best = _bestSettleSpot(state);
    if (!best || best.nodeId == null) return [];
    const prod = best.prodMap;
    return [{
        kind: 'settlement',
        when: 'soon',
        score: _scoreSettlement(prod),
        detail: `${_formatMissing(miss)} · +${_perRoll(prod)}/roll${settle3}`,
        node_id: best.nodeId,
        tiles: _nodeTiles(board, best.nodeId),
        missing: miss,
        resources: prod,
        port: board.nodes[best.nodeId]?.port || null,
        rationale: _settleRationale(prod, selfExpected),
    }];
}

/** Rank legal new road edges. Returns up to top-3 recs. */
export function _roadRecs(state, hand, opts) {
    const board = state.map;
    const roadBank = (state.bank[state.selfColor] || {}).roads;
    if (roadBank === 0) return [];
    const legal = _legalRoadEdges(state);
    // Blocked = every occupied node plus its distance-1 neighbours (the
    // distance-2 settlement rule), across all colors. Computed once and
    // shared by every edge's best-landing search.
    const allNodes = _allNodes(state);
    const blocked = new Set(allNodes);
    for (const bnid of allNodes) {
        const bn = board.nodes[bnid];
        if (!bn) continue;
        for (const nb of bn.neighbors) blocked.add(nb);
    }
    const ownedResources = _ownedResources(state);
    // Self network nodes (building nodes + self road endpoints) for the
    // bridge's from->to edge labeling.
    const myNodes = new Set();
    for (const nid of _ownNodes(state)) myNodes.add(String(nid));
    for (const [e, col] of Object.entries(state.roads || {})) {
        if (col !== state.selfColor) continue;
        const p = String(e).split('||');
        myNodes.add(p[0]); myNodes.add(p[1]);
    }
    const can = handCanAfford(hand, COSTS.road);
    const recs = [];
    for (const eid of legal) {
        const landing = _bestLanding(state, eid, blocked, ownedResources);
        if (landing.total <= 0) continue;
        recs.push({
            kind: 'road',
            when: can ? 'now' : 'soon',
            score: _scoreRoad(landing.prod),
            detail: `→ ${_perRoll(landing.prod)}-prod spot`
                + _portSuffix(state, landing.nodeId, ownedResources),
            edge: eid,
            tiles: _edgeTiles(board, eid),
            missing: can ? null : _missing(hand, COSTS.road),
            resources: landing.prod,
            landing_node_id: landing.nodeId,
            landing_node: landing.nodeId,
            rationale: _roadRationale(state),
            _rank: landing.rank,   // diversity-weighted ordering key
            _rawProd: landing.total,
        });
    }
    // Order by the diversity-weighted rank, not the raw-prod score, so a
    // 3-resource interior corner doesn't lose to a 2-resource same-pip
    // corner and produce two recs pointing at conflicting directions off
    // the same settlement (recommender.py 1301: edge_scores.sort by rank).
    recs.sort((a, b) => b._rank - a._rank);
    // Landing-target alternates floor (recommender.py 1364-1367): keep the
    // primary plus alternates whose raw prod clears 30% of the top edge's
    // raw prod (or any positive prod when the top is itself weak — late
    // LR-push boards where every road only buys 0.1-0.2 prod).
    let out = recs.slice(0, 3);
    if (out.length > 1) {
        const topProd = out[0]._rawProd || 0;
        const minProd = topProd > 0.5 ? 0.3 * topProd : 0.0;
        out = [out[0], ...out.slice(1).filter(r => (r._rawProd || 0) > minProd)];
    }
    // Drop the internal ordering keys before returning.
    for (const r of out) { delete r._rank; delete r._rawProd; }
    // Sealed fallback (bridge fallback_candidates[0] path): every corridor
    // is blocked for settling, so no settle-spot rec exists. Emit one
    // degraded "extends network" rec pointing at the best-production far
    // end so a road direction still shows instead of nothing. Score is
    // floored at 1.0 (the 1-10 UI contract) with a 0.6x sealed multiplier.
    if (out.length === 0) {
        let best = null;
        for (const eid of legal) {
            const p = String(eid).split('||');
            const far = myNodes.has(p[0]) ? p[1]
                : (myNodes.has(p[1]) ? p[0] : p[1]);
            const prod = nodeProduction(board, far);
            const raw = Object.values(prod).reduce((s, v) => s + v, 0);
            if (!best || raw > best.raw) best = { eid, far, prod, raw };
        }
        if (best && best.raw > 0) {
            out = [{
                kind: 'road',
                when: can ? 'now' : 'soon',
                score: Math.max(1.0,
                    Math.round(_scoreRoad(best.prod) * 0.6 * 10) / 10),
                detail: 'extends network · no settle spot',
                edge: best.eid,
                tiles: _edgeTiles(board, best.eid),
                missing: can ? null : _missing(hand, COSTS.road),
                resources: best.prod,
                landing_node_id: best.far,
                landing_node: best.far,
                rationale: _roadRationale(state),
                sealed: true,
            }];
        }
    }
    // Flag alternates and label each edge from self's network outward.
    for (let i = 0; i < out.length; i += 1) {
        const rec = out[i];
        if (i > 0) rec.alt = true;
        const p = String(rec.edge).split('||');
        const [a, b] = p;
        if (myNodes.has(b) && !myNodes.has(a)) {
            rec.edge_from = b; rec.edge_to = a;
        } else {
            rec.edge_from = a; rec.edge_to = b;
        }
    }
    return out;
}

const _BANK_KIND_LABEL = {
    settlement: 'settlement', city: 'city', dev_card: 'dev card',
};

/** Best buildable settlement spot: ranked by wheat-weighted production
 *  (matching the bridge), but the returned raw production drives the
 *  score. Returns {nodeId, raw, prodMap} or null. */
function _bestSettleSpot(state) {
    const board = state.map;
    let best = null;
    let bestW = -1;
    for (const nid of _legalSettleNodes(state)) {
        const prod = nodeProduction(board, nid);
        const w = _weightedProd(prod);
        if (w > bestW) {
            bestW = w;
            const raw = Object.values(prod).reduce((s, v) => s + v, 0);
            best = { nodeId: nid, raw, prodMap: prod };
        }
    }
    return best;
}

/** Highest-production own settlement — the city-upgrade target. */
function _bestOwnedSettleSpot(state) {
    const board = state.map;
    let best = null;
    let bestRaw = -1;
    for (const nid of _ownNodes(state)) {
        const b = state.buildings[nid];
        if (!b || b.kind !== 'SETTLEMENT') continue;
        const prod = nodeProduction(board, nid);
        const raw = Object.values(prod).reduce((s, v) => s + v, 0);
        if (raw > bestRaw) {
            bestRaw = raw;
            best = { nodeId: nid, raw, prodMap: prod };
        }
    }
    return best;
}

/** Detail string for a bank-trade rec. Mirrors recommender.py's
 *  single-hop vs multi-hop formatting with the dot separator. */
function _bankTradeDetail(planned, kind) {
    const label = _BANK_KIND_LABEL[kind] || kind;
    const steps = planned.plan.map(([src, rate, tgt]) =>
        `${rate} ${src.toLowerCase()} → 1 ${tgt.toLowerCase()}`);
    if (planned.plan.length === 1) {
        return `${steps[0]} · unlocks ${label}`;
    }
    const fmtPack = (pack) => Object.entries(pack)
        .filter(([, n]) => n)
        .map(([r, n]) => `${n} ${r.toLowerCase()}`).join(' + ');
    return `${fmtPack(planned.give)} → ${fmtPack(planned.get)} `
        + `· unlocks ${label}`;
}

/** The single best bank/port trade that unlocks a blocked build this
 *  turn. Mirrors recommender.py: targets in priority order
 *  settlement > city > dev_card (road is NOT a bank-trade target); the
 *  FIRST reachable one is emitted, scored from the unlocked build's own
 *  production curve minus one (clamped to [2, 9]). The planning is the
 *  shared planBankTrades (trades.js). Returns a one-element array (or
 *  empty), matching the bridge's single bank_trade rec. The bridge also
 *  suppresses the dev_card target once the dev deck is empty; the
 *  standalone has no public deck count (colonist hides it), so that
 *  gate is intentionally absent. */
function _bankTradeRecs(state, hand, opts) {
    const board = state.map;
    if (!hand) return [];
    const bankSupply = opts && opts.bankSupply;
    const planOpts = bankSupply ? { bankSupply } : {};
    const targets = [
        // Settlement target scores off the WHEAT-WEIGHTED production of the
        // best buildable spot, mirroring the bridge's _score_settlement
        // input (recommender.py 1114,1618). The old JS scored sp.raw
        // (unweighted), so a wheat-heavy settle target read a touch low.
        ['settlement', COSTS.settlement, () => _bestSettleSpot(state),
            (sp) => _scoreSettlement(sp.prodMap)],
        ['city', COSTS.city, () => _bestOwnedSettleSpot(state),
            (sp) => _scoreCity(sp.prodMap)],
        ['dev_card', COSTS.dev_card, null, () => _DEV_CARD_SCORE],
    ];
    for (const [kind, cost, spotFn, scoreFn] of targets) {
        if (handCanAfford(hand, cost)) continue;
        let spot = null;
        if (spotFn) {
            spot = spotFn();
            if (!spot) continue;
        }
        const planned = planBankTrades(state, hand, cost, planOpts);
        if (!planned || !planned.plan.length) continue;
        const base = scoreFn(spot);
        const score = Math.round(_clip(base - 1.0, 2.0, 9.0) * 10) / 10;
        const rec = {
            kind: 'bank_trade',
            when: 'now',
            score,
            give: planned.give,
            get: planned.get,
            unlocks: kind,
            target_kind: kind,
            detail: _bankTradeDetail(planned, kind),
        };
        if (spot && spot.nodeId != null) {
            rec.node_id = spot.nodeId;
            rec.tiles = _nodeTiles(board, spot.nodeId);
        }
        return [rec];
    }
    return [];
}

/** Dev-card buy rec. Score scales with how stuck the player is
 *  (no settle/road/city better than this) and game phase — mid-late
 *  it's a serious play, opening it's noise. */
function _devCardRec(state, hand, otherRecs) {
    const can = handCanAfford(hand, COSTS.dev_card);
    // Soon-plan gate: a blocked dev card is only surfaced when it is
    // one-to-two cards away (recommender.py 1538-1547, _PLAN_MAX_MISSING).
    // A 3-card gap is too speculative, so emit nothing.
    if (!can) {
        const miss = _missing(hand, COSTS.dev_card);
        const total = Object.values(miss).reduce((s, v) => s + v, 0);
        if (total <= 0 || total > _PLAN_MAX_MISSING) return null;
    }
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
        detail: can ? 'buy dev card'
            : _formatMissing(_missing(hand, COSTS.dev_card)),
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
    // Public-info supply gates (the bridge gates the ask on
    // board_resources / bank-19 / known-holder). The standalone can check
    // the board even produces the resource, and that some opponent holds
    // at least one card (its proxy for "someone can supply it"; the
    // per-resource opp holder check needs the hand tracker, deferred).
    const boardResources = new Set();
    for (const t of Object.values(board.tiles || {})) {
        if (t && t.resource) boardResources.add(t.resource);
    }
    // Default permissive: only suppress on opponent supply when we
    // actually have opponent hand-total data (else we can't prove nobody
    // holds it). Mirrors the bridge falling through when it lacks info.
    const _opps = (state.colors || []).filter(c => c !== state.selfColor);
    const anyOppHasCards = (!_opps.length || !state.handTotal)
        ? true
        : _opps.some(c => (state.handTotal[c] || 0) > 0);
    // Per-resource bank-remaining map (panel.js passes opts.bankSupply =
    // { remaining: {res:n} }; n is 19 minus all cards inferred in play).
    // When the bank still holds all 19 of a resource, nobody owns a card,
    // so the ask is dead on arrival. Mirrors the bridge's bank-19 skip
    // (recommender.py 1738-1740). Only applied when the inference latched
    // (bankSupply.tracked); an untracked / missing map leaves the gate off.
    const bankSupply = opts && opts.bankSupply;
    const bankRemaining = (bankSupply && bankSupply.tracked
        && bankSupply.remaining) ? bankSupply.remaining : null;
    const wanted = [
        ['city', COSTS.city, 6.5, 'city'],
        ['settlement', COSTS.settlement, 6.0, 'settlement'],
        ['road', COSTS.road, 4.5, 'road'],
        ['dev_card', COSTS.dev_card, 4.0, 'dev card'],
    ];
    // Reserve, per resource, what any near-term build (<= 2 cards short)
    // already-held needs, so we never offer away a card another blocked
    // build is counting on. Mirrors recommender.py's reserved_across.
    const reservedAcross = {};
    for (const [, c2] of wanted) {
        const miss = _missing(hand, c2);
        const totalMiss = Object.values(miss).reduce((s, v) => s + v, 0);
        if (totalMiss > 2) continue;
        for (const [r, n] of Object.entries(c2)) {
            reservedAcross[r] = Math.max(reservedAcross[r] || 0,
                Math.min(n, hand[r] || 0));
        }
    }
    // Surplus held beyond what near-term builds reserve, per resource.
    // Mirrors the bridge's `surplus` map (hand minus reserved_across).
    const surplusMap = {};
    for (const r of RESOURCE_NAMES) {
        const spare = (hand[r] || 0) - (reservedAcross[r] || 0);
        if (spare > 0) surplusMap[r] = spare;
    }
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
        // Don't propose a trade for a resource the board never makes
        // (variant maps can omit one) or when no opponent holds a card.
        // Gate only when we actually have the board / opp info.
        if (boardResources.size > 0 && !boardResources.has(needRes)) continue;
        if (!anyOppHasCards) continue;
        // Bank-19 skip: if the bank still holds all 19 of needRes, no
        // opponent can be holding any, so the proposal can never land.
        if (bankRemaining && (bankRemaining[needRes] || 0) >= 19) continue;
        // Candidate variants (give_count, get_count, label, score_adj).
        // Mirrors recommender.py 1767-1772: 1:1 is the friendly offer,
        // 2:1 concede is the give-extra offer that gets a yes. The
        // need_n >= 2 "2:2 even" variant never fires here because the
        // standalone only proposes when the deficit is exactly 1 card.
        const variants = [
            [1, 1, '1:1 fair', 0.0],
            [2, 1, '2:1 concede', -0.6],
        ];
        let emittedForKind = 0;
        for (const [giveN, getN, label, adj] of variants) {
            // Pick the surplus with the most spare cards that can cover
            // this variant's give_n (mirrors the bridge's best_src walk).
            let bestSrc = null;
            let bestSpare = 0;
            for (const [src, spare] of Object.entries(surplusMap)) {
                if (src === needRes || spare < giveN) continue;
                if (spare > bestSpare) { bestSrc = src; bestSpare = spare; }
            }
            if (bestSrc == null) continue;
            // Bridge score: round(min(base - 0.3 + adj, 9.5), 1), floored
            // at 1.5, then clamped to our 1-10 UI band (<=8 so it never
            // outranks an affordable build).
            let score = Math.min(baseScore - 0.3 + adj, 9.5);
            score = Math.max(1.5, Math.min(8.0, score));
            const giveStr = giveN !== 1
                ? `${giveN} ${bestSrc.toLowerCase()}`
                : `1 ${bestSrc.toLowerCase()}`;
            const getStr = getN !== 1
                ? `${getN} ${needRes.toLowerCase()}`
                : `1 ${needRes.toLowerCase()}`;
            const detail = label === '2:1 concede'
                ? `offer ${giveStr} for ${getStr} (concede) · unlocks ${kindWord}`
                : `offer ${giveStr} for ${getStr} · unlocks ${kindWord}`;
            recs.push({
                kind: 'propose_trade',
                when: 'now',
                score: Math.round(score * 10) / 10,
                give: { [bestSrc]: giveN },
                get: { [needRes]: getN },
                unlocks: target,
                variant: label,
                detail,
            });
            emittedForKind += 1;
        }
        // One build's worth of trade proposals is enough — the next
        // blocked build still surfaces as a "save for X" plan (bridge
        // breaks the target loop after the first kind that emits).
        if (emittedForKind) break;
    }
    return recs.slice(0, 3);
}

/** Phase- and strategy-dependent score adjustments, applied to the
 *  assembled rec list in place before de-dupe/sort. Mirrors the three
 *  late passes in recommender.py recommend_actions:
 *    1. third-settle  — settlement recs ×1.25 at exactly 2 footprints.
 *    2. endgame       — VP-advancing "now" recs +2.5 (1 VP out) / +1.5,
 *                       dev cards halved (dropped entirely at gap 1),
 *                       once self is at the close-to-win threshold.
 *    3. per-archetype — OWS/LR_RUSH/PORT_TRADE/RB_CARVED_TILES nudges
 *                       from the JS strategy port (computeStrategy). */
function _applyPhaseBumps(state, recs) {
    const board = state.map;
    // Self footprint counts (settlements + cities).
    let settleCount = 0;
    let cityCount = 0;
    for (const b of Object.values(state.buildings || {})) {
        if (b.color !== state.selfColor) continue;
        if (b.kind === 'CITY') cityCount += 1;
        else if (b.kind === 'SETTLEMENT') settleCount += 1;
    }

    // 1. Third-settle bump.
    if (settleCount + cityCount === 2) {
        for (const r of recs) {
            if (r.kind !== 'settlement') continue;
            r.score = Math.round(r.score * 1.25 * 10) / 10;
            if (!/settle #3/.test(r.detail || '')) {
                r.detail = `${r.detail || ''} · settle #3`;
            }
        }
    }

    // 2. Endgame VP push.
    const vpTarget = state.vpTarget || 10;
    const selfVp = (state.vp && state.vp[state.selfColor]) || 0;
    const closeVp = Math.max(2, Math.round(vpTarget * 0.80));
    if (selfVp >= closeVp) {
        const gap = Math.max(1, vpTarget - selfVp);
        const bump = gap === 1 ? 2.5 : 1.5;
        const dropDev = gap === 1;
        for (let i = recs.length - 1; i >= 0; i--) {
            const r = recs[i];
            if (r.when !== 'now') continue;
            if (r.kind === 'dev_card') {
                if (dropDev) { recs.splice(i, 1); continue; }
                r.score = Math.round(r.score * 0.5 * 10) / 10;
                continue;
            }
            const advances = r.kind === 'settlement' || r.kind === 'city'
                || r.unlocks === 'settlement' || r.unlocks === 'city'
                || r.target_kind === 'settlement' || r.target_kind === 'city';
            if (advances) {
                r.score = Math.round(Math.min(r.score + bump, 10.0) * 10) / 10;
            }
        }
    }

    // 3. Per-archetype bias (from the JS strategy port).
    let strat = null;
    try { strat = computeStrategy(state); } catch (e) { strat = null; }
    const tag = strat && (strat.active || strat.primary);
    if (tag && tag !== 'BALANCED') {
        const late = strat.phase === 'late' || strat.phase === 'endgame';
        for (const r of recs) {
            if (r.when !== 'now') continue;
            if ((r.score || 0) <= 1.0) continue;
            let bump = 0;
            if (tag === 'OWS') {
                if (r.kind === 'dev_card') bump = 0.5;
                else if (r.kind === 'city') bump = 0.3;
            } else if (tag === 'LR_RUSH') {
                if (r.kind === 'road') bump = late ? 0.6 : 0.2;
            } else if (tag === 'PORT_TRADE') {
                if (r.kind === 'road' && r.landing_node_id != null
                        && board.nodes[r.landing_node_id]
                        && board.nodes[r.landing_node_id].port) {
                    bump = 0.4;
                }
            } else if (tag === 'RB_CARVED_TILES') {
                if (r.kind === 'road') bump = late ? 0.8 : -0.4;
            }
            if (bump !== 0) {
                r.score = Math.round(
                    Math.min(Math.max(r.score + bump, 1.0), 10.0) * 10) / 10;
            }
        }
    }
}

/** Build a ranked rec list. opts: { topK, includeSoon, bankSupply }. */
export function recommendActions(state, opts = {}) {
    if (!state || !state.selfColor || !state.map) return [];
    const hand = state.hands[state.selfColor] || newHand();
    const recs = [];
    recs.push(..._cityRecs(state, hand, opts));
    recs.push(..._settleRecs(state, hand, opts));
    recs.push(..._roadRecs(state, hand, opts));
    recs.push(..._bankTradeRecs(state, hand, opts));
    recs.push(..._proposeTradeRecs(state, hand, opts));
    const devRec = _devCardRec(state, hand, recs);
    if (devRec) recs.push(devRec);

    // Phase + strategy score bumps (third-settle, endgame VP push,
    // per-archetype bias) before de-dupe/sort so they re-order the list.
    _applyPhaseBumps(state, recs);

    // Filter out duplicates by (kind, node_id|edge|target). propose_trade
    // recs carry no node_id/edge, so include `unlocks` + `variant` in their
    // key — the bridge keeps the 1:1-fair and 2:1-concede variants of the
    // same build as distinct recs (recommender.py 1767-1828, no variant
    // dedup), and collapsing them on a bare `propose_trade|` key dropped the
    // concede offer entirely.
    const seen = new Set();
    const out = [];
    for (const r of recs) {
        const key = r.kind === 'propose_trade'
            ? `propose_trade|${r.unlocks || ''}|${r.variant || ''}`
            : `${r.kind}|${r.node_id || r.edge || r.target_kind || ''}`;
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
    // Plan-alignment annotation (recommender.py 2079-2108): when a "soon"
    // settlement plan is in the list AND a road rec's tiles overlap the
    // plan's tiles, tag that road's detail with "· supports plan" so Noah
    // can see which road advances the active plan vs. opens a new corridor.
    let soonSettleTiles = null;
    for (const r of filtered) {
        if (r.kind === 'settlement' && r.when === 'soon') {
            soonSettleTiles = r.tiles || [];
            break;
        }
    }
    if (soonSettleTiles && soonSettleTiles.length) {
        const soonSet = new Set(
            soonSettleTiles.map(t => `${t[0]}|${t[1]}`));
        for (const r of filtered) {
            if (r.kind !== 'road') continue;
            const rt = r.tiles || [];
            const overlap = rt.some(t => soonSet.has(`${t[0]}|${t[1]}`));
            if (overlap && !/supports plan/.test(r.detail || '')) {
                r.detail = `${r.detail || ''} · supports plan`;
            }
        }
    }
    const topK = opts.topK || 12;
    return filtered.slice(0, topK);
}

/** Robber-target ranking — bridge had this in advisor.score_robber_targets;
 *  the standalone advisor.js stub returns []. We fill it in here so the
 *  panel's robber-target list works after a 7. */
/** Detect an opponent who could win on their next turn via a Largest
 *  Army or Longest Road flip. Mirrors bridge_robber._detect_imminent_opp_color:
 *  conservative, only the LA/LR flips, each gated on the win landing
 *  within 2 VP. An opp's knight-in-hand is hidden public info (the
 *  standalone reads 0 for opps), so the LA path rarely fires for an
 *  opponent, matching the bridge. */
export function detectImminentOpp(state) {
    if (!state || !state.colors) return null;
    const target = state.vpTarget || 10;
    for (const c of state.colors) {
        if (c === state.selfColor) continue;
        const vp = (state.vp && state.vp[c]) || 0;
        if (vp >= target) continue;
        // LA path: +1 knight takes Largest Army.
        const played = (state.playedKnights && state.playedKnights[c]) || 0;
        const held = (state.devCardsByType && state.devCardsByType[c]
            && state.devCardsByType[c].KNIGHT) || 0;
        const hasArmy = state.hasArmy === c;
        let oppMaxPlayed = 0;
        for (const o of state.colors) {
            if (o === c) continue;
            oppMaxPlayed = Math.max(oppMaxPlayed,
                (state.playedKnights && state.playedKnights[o]) || 0);
        }
        if (!hasArmy && held >= 1
                && played + 1 >= Math.max(3, oppMaxPlayed + 1)
                && vp + 2 >= target) return c;
        // LR path: +1 road takes Longest Road.
        const ll = (state.roadLength && state.roadLength[c]) || 0;
        const hasRoad = state.hasRoad === c;
        let oppMaxRoads = 0;
        for (const o of state.colors) {
            if (o === c) continue;
            oppMaxRoads = Math.max(oppMaxRoads,
                (state.roadLength && state.roadLength[o]) || 0);
        }
        if (!hasRoad && ll + 1 >= Math.max(5, oppMaxRoads + 1)
                && vp + 2 >= target) return c;
    }
    return null;
}

/** Resources self owes for its cheapest unaffordable build. Blocking a
 *  tile of one of these denies an opp AND sets up a steal of the exact
 *  resource (mirrors the bridge's needed_resources input). */
function _closestMissingResources(state) {
    const hand = (state.hands && state.hands[state.selfColor]) || {};
    let best = null;
    let bestGap = Infinity;
    for (const cost of Object.values(COSTS)) {
        const miss = _missing(hand, cost);
        let gap = 0;
        for (const v of Object.values(miss)) gap += v;
        if (gap > 0 && gap < bestGap) { bestGap = gap; best = miss; }
    }
    return new Set(best ? Object.keys(best) : []);
}

/** Per-resource per-roll production for self and each opponent from
 *  public buildings (settlement 1x, city 2x). Feeds the monopoly-setup
 *  bonus. */
function _productionByResource(state) {
    const board = state.map;
    const selfProd = {};
    const oppProd = {};
    if (!board || !board.nodes) return { selfProd, oppProd };
    for (const [nid, b] of Object.entries(state.buildings || {})) {
        const node = board.nodes[nid];
        if (!node) continue;
        const mult = b.kind === 'CITY' ? 2 : 1;
        const bag = (b.color === state.selfColor)
            ? selfProd
            : (oppProd[b.color] = oppProd[b.color] || {});
        for (const tid of node.tiles) {
            const t = board.tiles[tid];
            if (!t || !t.resource) continue;
            bag[t.resource] = (bag[t.resource] || 0) + (t.pip / 36) * mult;
        }
    }
    return { selfProd, oppProd };
}

export function recommendRobberTargets(state, opts = {}) {
    if (!state || !state.map) return [];
    const imminentColor = (opts.imminentColor != null)
        ? opts.imminentColor : detectImminentOpp(state);
    // Resource-control inputs (Strategy v2 P1-5): resources self owes for
    // its next build, and per-resource production maps for self + opps.
    const neededResources = _closestMissingResources(state);
    const { selfProd, oppProd } = _productionByResource(state);
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
        // active, drop opps at or below the threshold from the victim
        // pool so the panel doesn't suggest moves the game won't
        // let you make. Threshold mirrors the bridge's configurable
        // friendly_robber_min_vp (config.get_friendly_robber_protected_vp,
        // default 2): protected = vp <= threshold. Override via
        // opts.friendlyRobberMinVp for house rules; default keeps the
        // standard colonist behavior unchanged.
        if (opts.friendlyRobber) {
            const protectVp = (opts.friendlyRobberMinVp != null)
                ? opts.friendlyRobberMinVp : 2;
            oppAdj = oppAdj.filter(b => {
                const vp = state.vp[b.color] || 0;
                return vp > protectVp;
            });
            if (oppAdj.length === 0) continue;
        }
        // Per-victim blocked pips (a city counts double), mirroring the
        // bridge's victims[color] = pip * weight accumulation.
        const pip = pipsForNumber(tile.number);
        const oppByColor = {};
        const victimPipsByColor = {};
        for (const a of oppAdj) {
            const w = a.kind === 'CITY' ? 2 : 1;
            oppByColor[a.color] = (oppByColor[a.color] || 0) + 1;
            victimPipsByColor[a.color] =
                (victimPipsByColor[a.color] || 0) + pip * w;
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
        // Suggested victim: card count dominates (best steal EV), a
        // near-winner is weighted up by VP tier, and pips are a small
        // nudge. Mirrors bridge_robber._compute_robber_snapshot's
        // _victim_priority. Prefer a victim holding >= 1 card.
        const _closeVp = Math.max(2, Math.round((state.vpTarget || 10) * 0.8));
        const _midVp = Math.max(1, Math.round((state.vpTarget || 10) * 0.6));
        const _victimPriority = (c) => {
            const cards = state.handTotal[c] || 0;
            const vp = state.vp[c] || 0;
            const pips = victimPipsByColor[c] || 0;
            const w = vp >= _closeVp ? 3.0 : (vp >= _midVp ? 1.8 : 1.0);
            return cards * w + pips * 0.3;
        };
        const _victimColors = Object.keys(oppByColor);
        const _withCards = _victimColors.filter(
            c => (state.handTotal[c] || 0) > 0);
        const _pool = _withCards.length ? _withCards : _victimColors;
        let bestVictim = null;
        let bestPriority = -Infinity;
        for (const c of _pool) {
            const pr = _victimPriority(c);
            if (pr > bestPriority) { bestPriority = pr; bestVictim = c; }
        }
        const bestVictimCards = bestVictim != null
            ? (state.handTotal[bestVictim] || 0) : -1;
        const victims = Object.keys(oppByColor).map((c) => ({
            username: COLONIST_COLOR_NAME[String(c)] || `P${c}`,
            color: COLONIST_COLOR_NAME[String(c)] || `P${c}`,
            color_css: COLONIST_COLOR_HEX[String(c)] || '#888',
            pips: victimPipsByColor[c] || (oppByColor[c] * pip),
            vp: state.vp[c] || 0,
            cards: state.handTotal[c] || 0,
            suggested: c === bestVictim,
        }));
        // VP-weighted blocking value (advisor.score_robber_targets):
        // each victim's blocked pips scale by how close they are to
        // winning (_vp_weight, baseline ~30% of target), times 2 when
        // they could win next turn (imminent). Card count is NOT in the
        // tile score; it only selects the suggested victim above.
        const target = state.vpTarget || 10;
        const baseline = Math.max(1, Math.round(target * 0.3));
        const vpWeight = (v) => 1.0 + 0.4 * Math.max(0, v - baseline);
        let weighted = 0;
        for (const c of Object.keys(victimPipsByColor)) {
            const vvp = (state.vp && state.vp[c]) || 0;
            const mult = (imminentColor != null
                && String(imminentColor) === String(c)) ? 2.0 : 1.0;
            weighted += victimPipsByColor[c] * vpWeight(vvp) * mult;
        }
        // Resource-control bonuses (advisor.score_robber_targets):
        // blocking a tile of a resource we owe is worth +1.0+0.2*pip;
        // locking a tile we already dominate concentrates it further.
        let resourceNeedBonus = 0;
        let monopolySetupBonus = 0;
        const tileRes = tile.resource;
        if (tileRes) {
            if (neededResources.has(tileRes)) {
                resourceNeedBonus = 1.0 + 0.2 * pip;
            }
            const selfP = selfProd[tileRes] || 0;
            let oppTotal = 0;
            for (const c of Object.keys(victimPipsByColor)) {
                oppTotal += (oppProd[c] && oppProd[c][tileRes]) || 0;
            }
            const tableTotal = selfP + oppTotal;
            if (tableTotal > 0) {
                const selfShare = selfP / tableTotal;
                const nPlayers = 1 + Object.keys(victimPipsByColor).length;
                const evenShare = 1.0 / Math.max(1, nPlayers);
                const surplus = Math.max(0, selfShare - evenShare);
                monopolySetupBonus = Math.min(1.0, surplus * pip * 0.6);
            }
        }
        const score = weighted - pip * selfPieceValue
            + resourceNeedBonus + monopolySetupBonus;
        // Tiebreak inputs (advisor.score_robber_targets sort key):
        // opponent_blocked is the raw (unweighted) blocked pips summed
        // across every victim on the tile; maxVictimHand is the largest
        // single-victim hand size (best steal EV). Both are derived from
        // data already gathered in this loop.
        let opponentBlocked = 0;
        for (const c of Object.keys(victimPipsByColor)) {
            opponentBlocked += victimPipsByColor[c];
        }
        let maxVictimHand = 0;
        for (const c of Object.keys(oppByColor)) {
            maxVictimHand = Math.max(maxVictimHand, state.handTotal[c] || 0);
        }
        targets.push({
            tile_id: tid,
            number: tile.number,
            resource: tile.resource,
            pip,
            score,
            resource_need_bonus: Math.round(resourceNeedBonus * 100) / 100,
            monopoly_setup_bonus: Math.round(monopolySetupBonus * 100) / 100,
            opp_pieces: oppAdj.length,
            opponent_blocked: opponentBlocked,
            max_victim_hand: maxVictimHand,
            steal_from_color: bestVictim,
            victim_hand_size: bestVictimCards >= 0 ? bestVictimCards : 0,
            victims,
        });
    }
    // Sort: higher score first; tiebreak by largest single-victim hand
    // size (more cards -> better steal EV), then by raw opponent pips.
    // Mirrors advisor.score_robber_targets' results.sort key.
    targets.sort((a, b) => {
        if (b.score !== a.score) return b.score - a.score;
        if (b.max_victim_hand !== a.max_victim_hand) {
            return b.max_victim_hand - a.max_victim_hand;
        }
        return b.opponent_blocked - a.opponent_blocked;
    });
    return targets.slice(0, opts.topK || 5);
}

export { COSTS, _scoreSettlement, _weightedProd, _proposeTradeRecs,
         _settleRecs };
