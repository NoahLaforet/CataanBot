// hints.js — dev-card play timing hints, JS port.
//
// Heuristic mirrors of bridge_hints.py for the four playable dev
// cards. Each function returns null when self holds zero of that
// type, otherwise:
//
//   {
//     have: int,
//     should_play: bool,
//     reason: string,
//     ...kind-specific fields
//   }
//
// Calibration is intentionally simpler than the bridge: we don't have
// a 1-ply search, and self's dev cards bought *this* turn ship as a
// separate counter we'd need to track explicitly. The standalone
// version errs toward "show the hint, let the user decide" rather
// than gating it on per-turn purchase recency.

import { newHand, RESOURCE_NAMES } from './state.js';
import { COSTS, handCanAfford } from './recommender.js';

/** Total inferred opponent hand size on a given resource — for the
 *  standalone path this has to be a guess (opps' resourceCards
 *  ship as zeros). We approximate by splitting each opp's known
 *  total evenly across resources, weighted by which tiles they
 *  produce on. Way better than 0 and avoids the "monopoly drains
 *  19 wheat" overestimate by capping at the real bank-side max. */
function _estOppResource(state, resource) {
    const board = state.map;
    if (!board) return 0;
    let total = 0;
    for (const color of state.colors) {
        if (color === state.selfColor) continue;
        const handSize = state.handTotal[color] || 0;
        if (handSize === 0) continue;
        // Per-resource production weight for this opp's settles+cities.
        const pw = { WOOD: 0, BRICK: 0, SHEEP: 0, WHEAT: 0, ORE: 0 };
        for (const [nid, b] of Object.entries(state.buildings)) {
            if (b.color !== color) continue;
            const node = board.nodes[nid];
            if (!node) continue;
            const mult = b.kind === 'CITY' ? 2 : 1;
            for (const tid of node.tiles) {
                const t = board.tiles[tid];
                if (!t || !t.resource) continue;
                pw[t.resource] = (pw[t.resource] || 0) + (t.pip * mult);
            }
        }
        const grand = Object.values(pw).reduce((s, v) => s + v, 0);
        if (grand <= 0) continue;
        const share = (pw[resource] || 0) / grand;
        total += handSize * share;
    }
    // Cap: max 19 of any resource minus what's in our hand and the
    // bank (if known). For now just clamp at 19.
    return Math.min(19, Math.round(total));
}

/** Knight hint. */
export function knightHint(state) {
    if (!state || !state.selfColor) return null;
    const own = state.devCardsByType[state.selfColor];
    if (!own) return null;
    const have = own.KNIGHT || 0;
    if (have <= 0) return null;
    // Robber sitting on us? PLAY immediately.
    let robberOnUs = false;
    if (state.robberTile && state.map) {
        const tile = state.map.tiles[state.robberTile];
        if (tile) {
            for (const nid of tile.nodes) {
                const b = state.buildings[nid];
                if (b && b.color === state.selfColor) {
                    robberOnUs = true; break;
                }
            }
        }
    }
    // Opponent 1 knight away from Largest Army?
    const myKnights = state.playedKnights[state.selfColor] || 0;
    let oppCloseToLA = false;
    for (const c of state.colors) {
        if (c === state.selfColor) continue;
        const k = state.playedKnights[c] || 0;
        if (k >= 2 && k >= myKnights + 1) { oppCloseToLA = true; break; }
    }
    // Would playing this Knight win us LA outright?
    const haveLA = state.hasArmy === state.selfColor;
    const willClaimLA = !haveLA && (myKnights + 1 >= 3);

    let should_play = false;
    let reason = 'hold · no urgent trigger';
    if (robberOnUs) { should_play = true; reason = 'robber on us · clear it'; }
    else if (willClaimLA) {
        should_play = true; reason = `take Largest Army (+2 VP)`;
    } else if (oppCloseToLA && have >= 2) {
        should_play = true; reason = 'opp closing on LA · race';
    }
    return {
        have, should_play, reason,
        played: myKnights,
        has_la: haveLA,
        robber_on_us: robberOnUs,
    };
}

/** Monopoly hint — pick the best resource to claim. */
export function monopolyHint(state) {
    if (!state || !state.selfColor) return null;
    const own = state.devCardsByType[state.selfColor];
    if (!own) return null;
    const have = own.MONOPOLY || 0;
    if (have <= 0) return null;
    const selfHand = state.hands[state.selfColor] || newHand();
    let bestRes = null;
    let bestCount = 0;
    let bestUnlock = null;
    for (const r of RESOURCE_NAMES) {
        const est = _estOppResource(state, r);
        if (est <= 0) continue;
        // Unlock: with `est` more of `r`, can we afford a build?
        const sim = { ...selfHand, [r]: (selfHand[r] || 0) + est };
        let unlock = null;
        for (const [target, cost] of Object.entries(COSTS)) {
            if (handCanAfford(selfHand, cost)) continue;
            if (handCanAfford(sim, cost)) { unlock = target; break; }
        }
        // Resource weight: wheat>ore>others (tracking competitive
        // win-rate ranking); ties break high.
        const weight =
            r === 'WHEAT' ? 1.05
                : r === 'ORE' ? 1.03
                : 1.00;
        const score = est * weight + (unlock ? 0.5 : 0);
        if (score > bestCount) {
            bestCount = score;
            bestRes = r;
            bestUnlock = unlock;
        }
    }
    if (!bestRes) return null;
    const realEst = _estOppResource(state, bestRes);
    let should_play, reason;
    // Monopoly takes ALL of one resource from every opponent and is a
    // one-shot card, so a tiny pot is a waste even when it technically
    // unlocks a build (a single 4:1 trade or one more turn gets there
    // without burning the card). Require >= 2 cards to PLAY on an unlock,
    // and 4+ for a no-unlock tempo swing; a 1-card pot is never worth it.
    // Mirrors bridge_hints.py _compute_monopoly_hint.
    if (bestUnlock && realEst >= 2) {
        should_play = true; reason = `unlocks ${bestUnlock} · ~${realEst} cards`;
    } else if (realEst >= 4) {
        should_play = true; reason = `large pot · ~${realEst} cards`;
    } else {
        should_play = false; reason = `small pot · ~${realEst} · save it`;
    }
    return {
        have,
        target_resource: bestRes,
        est_total: realEst,
        should_play,
        reason,
        unlock: bestUnlock,
    };
}

/** YoP hint — pick the cheapest 2-resource pair that unlocks a build. */
export function yopHint(state) {
    if (!state || !state.selfColor) return null;
    const own = state.devCardsByType[state.selfColor];
    if (!own) return null;
    const have = own.YEAR_OF_PLENTY || 0;
    if (have <= 0) return null;
    const selfHand = state.hands[state.selfColor] || newHand();
    // Find the highest-value build that 2 added cards can unlock.
    const targets = [
        ['city', COSTS.city, 9],
        ['settlement', COSTS.settlement, 8],
        ['dev_card', COSTS.dev_card, 5],
        ['road', COSTS.road, 4],
    ];
    let bestTarget = null;
    let bestPair = null;
    let bestVal = 0;
    for (const [name, cost, val] of targets) {
        if (handCanAfford(selfHand, cost)) continue;
        const need = [];
        for (const [r, n] of Object.entries(cost)) {
            const have2 = selfHand[r] || 0;
            const short = n - have2;
            for (let i = 0; i < short; i += 1) need.push(r);
            if (need.length > 2) break;
        }
        if (need.length === 0 || need.length > 2) continue;
        const pair = need.length === 2
            ? need
            : [need[0], 'WHEAT'];  // pad with a flex resource
        if (val > bestVal) {
            bestVal = val;
            bestTarget = name;
            bestPair = pair;
        }
    }
    if (!bestTarget) {
        return {
            have, take: ['WHEAT', 'ORE'],
            should_play: false,
            reason: 'no build within reach',
        };
    }
    return {
        have,
        take: bestPair,
        should_play: true,
        reason: `unlocks ${bestTarget}`,
        target_kind: bestTarget,
    };
}

/** Road Building hint — pick the two best legal road edges. */
export function rbHint(state) {
    if (!state || !state.selfColor) return null;
    const own = state.devCardsByType[state.selfColor];
    if (!own) return null;
    const have = own.ROAD_BUILDING || 0;
    if (have <= 0) return null;
    // Reuse recommender's road ranking.
    const tmpState = { ...state, hands: { [state.selfColor]: {
        WOOD: 1, BRICK: 1, SHEEP: 0, WHEAT: 0, ORE: 0,
    } } };
    // Lazy-import to avoid the cycle at module load.
    return _rbHintImpl(state);
}

function _rbHintImpl(state) {
    const board = state.map;
    if (!board) return { have: 0, should_play: false,
                          reason: 'no board' };
    // Borrow recommender's edge enumeration via a small inline copy.
    const ownEdges = new Set();
    for (const [eid, c] of Object.entries(state.roads)) {
        if (c === state.selfColor) ownEdges.add(eid);
    }
    const ownNodes = new Set();
    for (const [nid, b] of Object.entries(state.buildings)) {
        if (b.color === state.selfColor) ownNodes.add(nid);
    }
    const buildings = state.buildings;
    const anchors = new Set();
    for (const eid of ownEdges) {
        const e = board.edges[eid];
        if (!e) continue;
        anchors.add(e.a); anchors.add(e.b);
    }
    for (const nid of ownNodes) anchors.add(nid);
    const candidateEdges = [];
    for (const anchor of anchors) {
        const blocked = buildings[anchor]
            && buildings[anchor].color !== state.selfColor;
        if (blocked) continue;
        const node = board.nodes[anchor];
        if (!node) continue;
        for (const nb of node.neighbors) {
            const eid = anchor < nb
                ? `${anchor}||${nb}` : `${nb}||${anchor}`;
            if (state.roads[eid]) continue;
            candidateEdges.push(eid);
        }
    }
    // Score each by best-landing production.
    const allBuilt = new Set(Object.keys(buildings));
    function landingScore(edgeId) {
        const e = board.edges[edgeId];
        if (!e) return 0;
        const candidates = [e.a, e.b];
        let best = 0;
        for (const nid of candidates) {
            if (allBuilt.has(nid)) continue;
            const node = board.nodes[nid];
            if (!node) continue;
            // Distance-rule check for landing settle viability.
            let nbBuilt = false;
            for (const nb of node.neighbors) {
                if (allBuilt.has(nb)) { nbBuilt = true; break; }
            }
            let s = 0;
            for (const tid of node.tiles) {
                const t = board.tiles[tid];
                if (!t || !t.resource) continue;
                s += t.pip / 36;
            }
            if (nbBuilt) s *= 0.4;
            if (s > best) best = s;
        }
        return best;
    }
    const ranked = candidateEdges
        .map(e => ({ edge: e, score: landingScore(e) }))
        .sort((a, b) => b.score - a.score);
    if (ranked.length === 0) {
        return {
            have: state.devCardsByType[state.selfColor]?.ROAD_BUILDING || 0,
            should_play: false,
            reason: 'no legal road extensions',
        };
    }
    const pick = ranked.slice(0, 2);
    const have = state.devCardsByType[state.selfColor]?.ROAD_BUILDING || 0;
    // PLAY when the pair lands at a node we could plausibly settle —
    // either a landing scores >= 0.30 (decent corner) or we already
    // know the road race matters (longest-road threat).
    const goodLanding = pick[0]?.score >= 0.25;
    let should_play = goodLanding;
    let reason = goodLanding
        ? 'opens up a strong landing'
        : 'no high-value landing · hold';
    // Longest-road race: if we're 1 road behind LR holder + LR is
    // contested, prefer to play.
    const myLen = state.roadLength[state.selfColor] || 0;
    let oppMax = 0;
    for (const c of state.colors) {
        if (c === state.selfColor) continue;
        const l = state.roadLength[c] || 0;
        if (l > oppMax) oppMax = l;
    }
    if (state.hasRoad !== state.selfColor && myLen + 2 >= oppMax + 1
            && oppMax >= 4) {
        should_play = true;
        reason = 'race for Longest Road';
    }
    return {
        have,
        edges: pick.map(p => p.edge),
        should_play,
        reason,
        self_len: myLen,
        opp_len: oppMax,
    };
}
