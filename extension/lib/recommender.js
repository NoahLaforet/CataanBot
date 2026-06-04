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
    if (!edge) return { prod: {}, total: 0, nodeId: null };
    let best = { prod: {}, total: -1, nodeId: null };
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
            if (port && port.resource && ownedResources.has(port.resource)) {
                portBonus = 1.4;
            }
            const rank = raw * diversity * portBonus;
            if (rank > bestRank) {
                bestRank = rank;
                best = { prod, total: raw, nodeId: nb };
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
            detail: `+${_perRoll(prod)}/roll · +1 VP`,
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
            detail: `+${_perRoll(prod)}/roll`,
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
    const recs = [];
    for (const eid of legal) {
        const landing = _bestLanding(state, eid, blocked, ownedResources);
        if (landing.total <= 0) continue;
        const score = _scoreRoad(landing.prod);
        const tiles = _edgeTiles(board, eid);
        const can = handCanAfford(hand, COSTS.road);
        recs.push({
            kind: 'road',
            when: can ? 'now' : 'soon',
            score,
            detail: `→ ${_perRoll(landing.prod)}-prod spot`,
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
        ['settlement', COSTS.settlement, () => _bestSettleSpot(state),
            (sp) => Math.round(
                _clip(sp.raw * 12.0 + 2.0, 2.0, 10.0) * 10) / 10],
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
            // Only offer a card held beyond what near-term builds reserve
            // (this build included, via reservedAcross).
            if (have - (reservedAcross[surplus] || 0) < 1) continue;
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
    recs.push(_devCardRec(state, hand, recs));

    // Phase + strategy score bumps (third-settle, endgame VP push,
    // per-archetype bias) before de-dupe/sort so they re-order the list.
    _applyPhaseBumps(state, recs);

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
        targets.push({
            tile_id: tid,
            number: tile.number,
            resource: tile.resource,
            pip,
            score,
            resource_need_bonus: Math.round(resourceNeedBonus * 100) / 100,
            monopoly_setup_bonus: Math.round(monopolySetupBonus * 100) / 100,
            opp_pieces: oppAdj.length,
            steal_from_color: bestVictim,
            victim_hand_size: bestVictimCards >= 0 ? bestVictimCards : 0,
            victims,
        });
    }
    targets.sort((a, b) => b.score - a.score);
    return targets.slice(0, opts.topK || 5);
}

export { COSTS, _scoreSettlement, _weightedProd, _proposeTradeRecs };
