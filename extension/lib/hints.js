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
import { COSTS, handCanAfford, recommendRobberTargets } from './recommender.js';

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

/** Knight hint. Mirrors bridge_hints.py _compute_knight_hint: a
 *  weak-robber-tile hold guard (pip <= 2), deny-Largest-Army before
 *  claim-LA ordering, a strong-block trigger off the top robber-target
 *  score, and a knight-stack / late-game rule that gates proactive
 *  plays when self holds only one knight. The standalone keeps the
 *  bridge's "just bought this turn" exclusion as a known simplification
 *  (self per-turn dev buys are not tracked here), erring toward showing
 *  the hint. */
export function knightHint(state, opts) {
    if (!state || !state.selfColor) return null;
    const own = state.devCardsByType[state.selfColor];
    if (!own) return null;
    const have = own.KNIGHT || 0;
    if (have <= 0) return null;

    // Robber physically on one of our tiles, and the pip-weighted
    // production it blocks. A weak tile (pip <= 2, i.e. 2/3/11/12)
    // hurts so little it is not worth a lone knight to clear.
    const board = state.map;
    let robberOnUs = false;
    let robberTilePip = 0;
    let selfBlockedPips = 0;
    if (state.robberTile && board) {
        const tile = board.tiles[state.robberTile];
        if (tile) {
            if (tile.number) robberTilePip = tile.pip || 0;
            for (const nid of tile.nodes) {
                const b = state.buildings[nid];
                if (b && b.color === state.selfColor) {
                    robberOnUs = true;
                    selfBlockedPips += robberTilePip;
                }
            }
        }
    }
    const weakRobberTile = robberTilePip <= 2;

    const myKnights = state.playedKnights[state.selfColor] || 0;
    const haveLA = state.hasArmy === state.selfColor;
    const target = state.vpTarget || 10;
    // Config ratios from bridge config.py: threat at round(target*0.7)
    // (10 -> 7), close-to-win at round(target*0.8) (10 -> 8).
    const laThreatVp = Math.max(2, Math.round(target * 0.7));
    const closeToWin = Math.max(2, Math.round(target * 0.8));

    // Opp racing to Largest Army: played >= 3, OR played >= 2 at a
    // threat-level VP.
    let largestArmyThreat = false;
    for (const c of state.colors) {
        if (c === state.selfColor) continue;
        const played = state.playedKnights[c] || 0;
        const vp = (state.vp && state.vp[c]) || 0;
        if (played >= 3 || (played >= 2 && vp >= laThreatVp)) {
            largestArmyThreat = true; break;
        }
    }

    // Would playing this knight CLAIM Largest Army? Must strictly exceed
    // every other player; stealing a held LA needs self+1 > the holder.
    const laHeldBySomeone = state.hasArmy != null;
    const laHolderPlayed = laHeldBySomeone
        ? (state.playedKnights[state.hasArmy] || 0) : 0;
    let oppMaxPlayed = 0;
    for (const c of state.colors) {
        if (c === state.selfColor) continue;
        oppMaxPlayed = Math.max(oppMaxPlayed, state.playedKnights[c] || 0);
    }
    let knightSecuresLa;
    if (haveLA) knightSecuresLa = false;
    else if (laHeldBySomeone) knightSecuresLa = myKnights >= laHolderPlayed;
    else knightSecuresLa = (myKnights >= 2 && (myKnights + 1) > oppMaxPlayed);

    // Top robber-target score drives the "strong block available" path.
    let topTarget = null;
    let topScore = 0;
    try {
        const tt = recommendRobberTargets(state, opts || {}) || [];
        topTarget = tt[0] || null;
        topScore = topTarget ? (Number(topTarget.score) || 0) : 0;
    } catch (_) { /* robber scoring needs a board; tolerate absence */ }

    const selfVp = (state.vp && state.vp[state.selfColor]) || 0;
    const lateGame = selfVp >= closeToWin - 2;
    const knightStackOk = (have >= 2 || lateGame);

    let should_play = false;
    let reason = 'no urgent reason · hold for now';
    if (selfBlockedPips > 0) {
        if (weakRobberTile && !knightStackOk) {
            reason = `robber's on you but on a weak tile `
                + `(pip ${robberTilePip}) · hold knight`;
        } else {
            should_play = true;
            const cardsPerRoll = selfBlockedPips / 36;
            reason = `robber's on you · play to clear it `
                + `(~${cardsPerRoll.toFixed(2)} cards/roll blocked)`;
        }
    } else if (largestArmyThreat) {
        should_play = true;
        reason = 'an opp is close to Largest Army · play to deny';
    } else if (knightSecuresLa) {
        should_play = true;
        reason = laHeldBySomeone
            ? 'playing this knight steals Largest Army from the '
                + 'current holder (+2 VP)'
            : "you're 1 knight from Largest Army · play it to grab "
                + 'the +2 VP';
    } else if (topScore >= 4.0) {
        if (!knightStackOk) {
            reason = 'a block exists but you only hold 1 knight · '
                + 'hold for a clearer trigger';
        } else {
            should_play = true;
            if (topTarget && topTarget.resource) {
                const tileLbl = (`${String(topTarget.resource).toLowerCase()} `
                    + `${topTarget.number || ''}`).trim();
                reason = `a strong block on ${tileLbl} is available`;
            } else {
                reason = 'a strong block is available';
            }
        }
    }

    return {
        have,
        should_play,
        reason,
        played: myKnights,
        has_la: haveLA,
        robber_on_us: robberOnUs,
        best_target: topTarget,
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
        // No unlock within reach: HOLD, but point the pair at the two
        // resources most in demand across all builds (priority-weighted),
        // matching bridge_hints.py's demand fallback. Default ORE+WHEAT.
        const demand = { WOOD: 0, BRICK: 0, SHEEP: 0, WHEAT: 0, ORE: 0 };
        for (const [, cost, val] of targets) {
            for (const [r, n] of Object.entries(cost)) {
                const d = n - (selfHand[r] || 0);
                if (d > 0) demand[r] += val * d;
            }
        }
        const ranked = Object.entries(demand).sort((a, b) => b[1] - a[1]);
        const topR = (ranked[0] && ranked[0][1] > 0) ? ranked[0][0] : 'ORE';
        const secondR = (ranked[1] && ranked[1][1] > 0)
            ? ranked[1][0] : 'WHEAT';
        const holdPair = (topR !== secondR) ? [topR, secondR] : [topR, topR];
        return {
            have,
            should_play: false,
            reason: 'no build within reach',
            pair: holdPair,
            unlock: null,
            bank_ok: true,
        };
    }
    // Field shape matches bridge_hints.py (pair / unlock / bank_ok); the
    // panel renderer reads those names. Bank-supply gating stays off in
    // the standalone (the bank estimate is chat-inferred and best-effort),
    // so bank_ok is always true here, matching the bridge when the bank
    // is unconstrained.
    return {
        have,
        should_play: true,
        reason: `unlocks ${bestTarget}`,
        pair: bestPair,
        unlock: bestTarget,
        bank_ok: true,
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
    // No legal extension still gets a verdict: the LR / road-supply
    // logic below does not depend on a placement (the bridge computes
    // should_play independently of its placement search).
    const pick = ranked.slice(0, 2);
    const have = state.devCardsByType[state.selfColor]?.ROAD_BUILDING || 0;
    // should_play / reason mirror bridge_hints.py _compute_rb_hint: the
    // value is in a longest-road swing (qualifying self, or catching an
    // opp about to take it), or playing before you run out of road
    // pieces. The landing pick above stands in for the bridge's
    // placement search; fog-reveal and the settle-opening HOLD naming
    // are bridge-only / a later wave.
    const self_len = state.roadLength[state.selfColor] || 0;
    const self_has = state.hasRoad === state.selfColor;
    let opp_max = 0;
    let opp_has = false;
    for (const c of state.colors) {
        if (c === state.selfColor) continue;
        const ln = state.roadLength[c] || 0;
        if (ln > opp_max) opp_max = ln;
        if (state.hasRoad === c) opp_has = true;
    }
    // Roads left is public: 15 starting pieces minus self's placements.
    let placedRoads = 0;
    for (const col of Object.values(state.roads || {})) {
        if (col === state.selfColor) placedRoads += 1;
    }
    const roads_left = Math.max(0, 15 - placedRoads);
    const projected = self_len + Math.min(2, roads_left);
    const qualify = 5;  // base-game longest-road threshold

    let should_play = false;
    let reason = 'no clear swing yet';
    if (roads_left <= 0) {
        reason = 'no road pieces left · hold';
    } else if (!self_has && projected >= Math.max(qualify, opp_max + 1)) {
        should_play = true;
        reason = `secures LR · ${self_len}→${projected} vs ${opp_max}`;
    } else if (opp_has && opp_max >= qualify && projected >= opp_max) {
        should_play = true;
        reason = `catches opp LR · proj ${projected} ≥ ${opp_max}`;
    } else if (roads_left <= 2) {
        should_play = true;
        reason = `low on roads · ${roads_left} left`;
    }
    return {
        have,
        edges: pick.map(p => p.edge),
        should_play,
        reason,
        self_len,
        opp_len: opp_max,
    };
}
