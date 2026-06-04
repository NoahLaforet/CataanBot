// strategy.js — archetype scoring, JS port (slim).
//
// Heuristic mirror of the core scoring in src/catanbot/strategy_select.py.
// Computes a 0-1 score for each of the five archetypes —
// RB_CARVED_TILES, OWS, LR_RUSH, PORT_TRADE, BALANCED — and packages
// them into the {active, primary, fallback, rationale, scores,
// ranking, phase} shape the panel's `renderStrategyBanner` consumes.
//
// Two modes:
//   * Pre-placement (board affinity): no settles down yet. We score
//     the whole board's production distribution as if a hypothetical
//     player owned everything; the resulting ranking tells the user
//     which archetype the board *enables* most strongly. Active is
//     left null so the panel renders the "🧭 board affinity" headline
//     instead of an active-archetype banner.
//   * Post-placement: read self's settlements + cities, compute their
//     combined production, and score archetypes against that footprint.
//     active = primary tag.
//
// Skipped vs the Python version:
//   * RB_CARVED_TILES isolation scoring (the corridor walk) — we use a
//     much simpler tile-cluster proxy that's directionally correct.
//   * Pivot-trigger detection (hot_number, road_builder_drawn,
//     opp_close_to_la). Worth porting later — the basic banner is
//     usable without it.
//   * Override / fallback bookkeeping across multiple snaps. Each
//     `computeStrategy` call is stateless; the panel re-derives.

import { nodeProduction, pipsForNumber } from './board.js';

const TAGS = ['RB_CARVED_TILES', 'OWS', 'LR_RUSH', 'PORT_TRADE', 'BALANCED'];

const TAG_MIN = {
    RB_CARVED_TILES: 0.65,
    OWS: 0.45,
    LR_RUSH: 0.45,
    PORT_TRADE: 0.45,
    BALANCED: 0.0,
};

const PHASE_BOUNDARIES = [
    [0, 'opening'], [5, 'early'], [15, 'mid'],
    [30, 'late'], [50, 'endgame'],
];

function _phaseFor(rolls) {
    let label = 'opening';
    for (const [t, n] of PHASE_BOUNDARIES) {
        if (rolls >= t) label = n;
    }
    return label;
}

function _selfNodes(state) {
    if (!state.selfColor) return [];
    const out = [];
    for (const [nid, b] of Object.entries(state.buildings)) {
        if (b.color === state.selfColor
                && (b.kind === 'SETTLEMENT' || b.kind === 'CITY')) {
            out.push(nid);
        }
    }
    return out;
}

function _combinedProd(board, nodeIds) {
    const out = { WOOD: 0, BRICK: 0, SHEEP: 0, WHEAT: 0, ORE: 0 };
    for (const nid of nodeIds) {
        const p = nodeProduction(board, nid);
        for (const [r, v] of Object.entries(p)) {
            if (r in out) out[r] += v;
        }
    }
    return out;
}

/** Whole-board production (every node produces 1× from each adjacent
 *  tile). Used as the "what does this board enable" baseline for
 *  pre-placement scoring. Normalized so it's comparable to a
 *  2-settle footprint: divide by total land nodes / typical-pair
 *  ratio (~25). */
function _boardProd(board) {
    const out = { WOOD: 0, BRICK: 0, SHEEP: 0, WHEAT: 0, ORE: 0 };
    for (const tile of Object.values(board.tiles || {})) {
        if (!tile.resource || !tile.number) continue;
        const yield_ = tile.pip / 36;
        // Each tile contributes to 6 corners; for board-affinity we
        // approximate by weighting raw tile yield directly. The
        // archetype thresholds below were tuned on per-pair sums in
        // the [0.10, 0.45] range; we scale the board sum to match
        // by dividing through tile-count baseline.
        out[tile.resource] += yield_;
    }
    // Scale to "what a typical 2-settle pair would produce." Empirically
    // dividing by 3 lands per-resource around 0.15-0.20 on a classic
    // board — comfortably above the 0.10 archetype-eligibility floor
    // for every resource so pre-placement ranking has signal across
    // all archetypes, not just PORT_TRADE.
    for (const r of Object.keys(out)) out[r] /= 3;
    return out;
}

function _scoreOWS(prod) {
    const ore = prod.ORE || 0;
    const wheat = prod.WHEAT || 0;
    const sheep = prod.SHEEP || 0;
    if (ore < 0.10 || wheat < 0.10) return 0.0;
    const base = 0.5 * wheat + 0.35 * ore + 0.20 * sheep;
    return Math.min(1.0, base * 4.0);
}

function _scoreLRRush(prod, state, nodeIds) {
    const wood = prod.WOOD || 0;
    const brick = prod.BRICK || 0;
    if (wood < 0.10 || brick < 0.10) return 0.0;
    const base = wood + brick;
    // Runway: count free road-extension neighbours from self nodes.
    // Pre-placement (no nodeIds) we assume full runway = 1.0.
    let runwayFactor = 1.0;
    if (nodeIds.length && state.map) {
        const blocked = new Set();
        for (const [nid, b] of Object.entries(state.buildings)) {
            if (b.kind !== 'SETTLEMENT' && b.kind !== 'CITY') continue;
            blocked.add(nid);
            const n = state.map.nodes[nid];
            if (!n) continue;
            for (const nb of n.neighbors) blocked.add(nb);
        }
        let runway = 0;
        for (const nid of nodeIds) {
            const node = state.map.nodes[nid];
            if (!node) continue;
            for (const nb of node.neighbors) {
                if (!blocked.has(nb)) runway += 1;
            }
        }
        runwayFactor = Math.min(1.0, runway / 4.0);
    }
    return Math.min(1.0, base * 1.6 * (0.5 + 0.5 * runwayFactor));
}

function _scorePortTrade(state, nodeIds, prod) {
    const board = state.map;
    if (!board) return 0.0;
    let direct = 0.0;
    let near = 0.0;
    // Pre-placement: any high-pip tile next to a 2:1 port whose
    // resource lands on it counts as "available"; pick the best.
    if (nodeIds.length === 0) {
        for (const port of board.ports || []) {
            if (port.kind === '3:1' || !port.resource) continue;
            for (const portNid of port.nodes) {
                const n = board.nodes[portNid];
                if (!n) continue;
                for (const tid of n.tiles) {
                    const t = board.tiles[tid];
                    if (!t || t.resource !== port.resource) continue;
                    if ((t.pip || 0) >= 4) {
                        direct = Math.max(direct, 0.55);
                    }
                }
            }
        }
        return Math.min(1.0, direct);
    }
    // Post-placement: settle-on-port w/ strong adjacent or settle-
    // near-port (1 hop) on a resource we produce.
    for (const nid of nodeIds) {
        const n = board.nodes[nid];
        if (!n) continue;
        const port = n.port;
        if (port && port.kind === '2:1' && port.resource) {
            // direct: port res produced on adjacent tile w/ pip >= 3
            for (const tid of n.tiles) {
                const t = board.tiles[tid];
                if (!t || t.resource !== port.resource) continue;
                if ((t.pip || 0) >= 3) direct = Math.max(direct, 0.6);
            }
        }
        // Near: a 2:1 port on a neighbour node (1-hop expansion target)
        // for a resource we already produce. Mirrors strategy_select.py
        // _score_port_trade so the standalone archetype banner agrees
        // with the bridge: 3:1 ports get no near credit, the produced-
        // resource gate is 0.20 (not 0.10), and the near score is 0.85
        // (not 0.55).
        for (const nb of n.neighbors) {
            const nbn = board.nodes[nb];
            if (!nbn || !nbn.port) continue;
            const p = nbn.port;
            if (p.kind === '3:1' || !p.resource) continue;
            if ((prod[p.resource] || 0) >= 0.20) {
                near = Math.max(near, 0.85);
            }
        }
    }
    return Math.min(1.0, Math.max(direct, near));
}

function _scoreRBCarvedTiles(state, nodeIds) {
    // Slim version: count distinct number-bearing tiles within 2 hops
    // of self nodes (via own-edge graph), subtract 1 per opp building
    // in that ring. Score = clamp((reach - opps) / 8, 0, 1).
    if (nodeIds.length === 0) return 0.0;
    const board = state.map;
    if (!board) return 0.0;
    const reachableTiles = new Set();
    const oppPenalty = (() => {
        let p = 0;
        for (const [nid, b] of Object.entries(state.buildings)) {
            if (b.color === state.selfColor) continue;
            // Within 2 hops of any self node?
            for (const sn of nodeIds) {
                if (_within2Hops(board, sn, nid)) { p += 1; break; }
            }
        }
        return p;
    })();
    for (const sn of nodeIds) {
        const node = board.nodes[sn];
        if (!node) continue;
        for (const tid of node.tiles) {
            const t = board.tiles[tid];
            if (t && t.number) reachableTiles.add(tid);
        }
        // 1-hop and 2-hop expansion
        for (const nb of node.neighbors) {
            const n2 = board.nodes[nb];
            if (!n2) continue;
            for (const tid of n2.tiles) {
                const t = board.tiles[tid];
                if (t && t.number) reachableTiles.add(tid);
            }
        }
    }
    const reach = Math.max(0, reachableTiles.size - oppPenalty);
    return Math.min(1.0, reach / 8.0);
}

function _within2Hops(board, a, b) {
    if (a === b) return true;
    const start = board.nodes[a];
    if (!start) return false;
    if (start.neighbors.has(b)) return true;
    for (const nb of start.neighbors) {
        const n2 = board.nodes[nb];
        if (n2 && n2.neighbors.has(b)) return true;
    }
    return false;
}

const RATIONALE = {
    OWS: 'ore + wheat lean · city-rush w/ dev-card flex.',
    LR_RUSH: 'wood + brick footprint w/ expansion runway · push roads.',
    PORT_TRADE: 'port aligned with produced resource · leverage trades.',
    RB_CARVED_TILES: 'isolated cluster of producing tiles · RoadBuilder.',
    BALANCED: 'no dominant archetype · keep options open.',
};

/** Pivot trigger detectors. Each fires at most one trigger; the
 *  list is folded into snap.strategy.pivot_triggers + may produce
 *  an override_tag that flips the active archetype as long as the
 *  condition holds. Slim port of strategy_select._detect_*.
 */
function _selfTileNumbers(state, nodeIds) {
    if (!state.map || !nodeIds.length) return new Set();
    const out = new Set();
    for (const nid of nodeIds) {
        const node = state.map.nodes[nid];
        if (!node) continue;
        for (const tid of node.tiles) {
            const t = state.map.tiles[tid];
            if (t && t.number) out.add(t.number);
        }
    }
    return out;
}
function _detectHotNumber(state, nodeIds) {
    const recent = (state.rollHistory || []).slice(-10);
    // No history-length floor (matches bridge): a count >= 4 already
    // requires at least 4 rolls of the number.
    const counts = {};
    for (const r of recent) {
        const n = r.total;
        if (n && n !== 7) counts[n] = (counts[n] || 0) + 1;
    }
    const myNumbers = _selfTileNumbers(state, nodeIds);
    for (const [num, c] of Object.entries(counts)) {
        if (c >= 4 && myNumbers.has(Number(num))) {
            return {
                name: 'hot_number',
                detail: `${num} rolled ${c}× in last 10 · lean in`,
                override_tag: null,
            };
        }
    }
    return null;
}
function _detectOppCloseToLA(state) {
    if (state.hasArmy) return null;  // someone already holds it
    // Bridge (_detect_opp_close): opp played_knights >= 2, not has_army,
    // and vp >= largest_army_threat_vp() - 1 = round(target*0.7) - 1 (6 at
    // a 10-VP target). There is no "opp knights > my knights" gate; that
    // suppressed the warning whenever self was already ahead on knights.
    const target = state.vpTarget || 10;
    const laFloor = Math.max(2, Math.round(target * 0.7) - 1);
    for (const c of state.colors) {
        if (c === state.selfColor) continue;
        const k = state.playedKnights[c] || 0;
        const vp = state.vp[c] || 0;
        if (k >= 2 && vp >= laFloor) {
            return {
                name: 'opp_close_to_la',
                detail: `opp on ${k} knights · race to LA or commit to denial`,
                override_tag: null,
            };
        }
    }
    return null;
}
function _detectOppCloseToWin(state) {
    const target = state.vpTarget || 10;
    // Bridge close_to_win_vp() = round(target*0.8) (8 at a 10-VP target),
    // not target-4. The old value fired this trigger two VP too early.
    const closeAt = Math.max(2, Math.round(target * 0.8));
    for (const c of state.colors) {
        if (c === state.selfColor) continue;
        const vp = state.vp[c] || 0;
        if (vp >= closeAt) {
            return {
                name: 'opp_close_to_win',
                detail: `opp at ${vp} VP · tighten trades, deny resources`,
                override_tag: null,
            };
        }
    }
    return null;
}
function _detectSevenOverdue(state) {
    const recent = (state.rollHistory || []).slice(-10);
    // No history-length floor (matches bridge): the hand>limit guard
    // below already keeps this from firing in the opening.
    const limit = state.discardLimit || 7;
    const myHand = state.handTotal[state.selfColor] || 0;
    if (myHand <= limit) return null;
    if (recent.some(r => r.total === 7)) return null;
    return {
        name: 'seven_overdue',
        detail: `hand at ${myHand}, no 7 in 10 rolls · trade down `
            + 'before the next 7',
        override_tag: null,
    };
}
function _detectPivots(state, nodeIds) {
    const triggers = [];
    const hot = _detectHotNumber(state, nodeIds);
    if (hot) triggers.push(hot);
    const oppLA = _detectOppCloseToLA(state);
    if (oppLA) triggers.push(oppLA);
    const oppWin = _detectOppCloseToWin(state);
    if (oppWin) triggers.push(oppWin);
    const seven = _detectSevenOverdue(state);
    if (seven) triggers.push(seven);
    return triggers;
}

/** Compute the strategy snap for the current state.
 *  Returns the shape `panel.renderStrategyBanner` reads:
 *  { active, primary, fallback, rationale, phase, scores, ranking,
 *    pivot_triggers, override_tag }
 */
export function computeStrategy(state) {
    if (!state || !state.map) return null;
    const nodeIds = _selfNodes(state);
    const isPre = nodeIds.length === 0;
    const prod = isPre
        ? _boardProd(state.map)
        : _combinedProd(state.map, nodeIds);
    const scores = {
        OWS: _scoreOWS(prod),
        LR_RUSH: _scoreLRRush(prod, state, nodeIds),
        PORT_TRADE: _scorePortTrade(state, nodeIds, prod),
        RB_CARVED_TILES: _scoreRBCarvedTiles(state, nodeIds),
        BALANCED: 0.50,  // floor — always present
    };
    const ranking = TAGS
        .map(tag => ({
            tag,
            score: Math.round(scores[tag] * 100) / 100,
            eligible: scores[tag] >= TAG_MIN[tag],
        }))
        .sort((a, b) => b.score - a.score);
    let primary = null;
    let fallback = null;
    let active = null;
    if (!isPre) {
        // Pick first eligible from ranking; BALANCED is the floor.
        for (const r of ranking) {
            if (r.eligible) {
                if (!primary) primary = r.tag;
                else if (!fallback) { fallback = r.tag; break; }
            }
        }
        if (!primary) primary = 'BALANCED';
        active = primary;
    }
    // Pivot triggers — informational signals that surface below the
    // banner. Most are non-overriding; an explicit override_tag flips
    // active as long as the trigger fires (matches the bridge).
    const triggers = isPre ? [] : _detectPivots(state, nodeIds);
    let overrideTag = null;
    for (const t of triggers) {
        if (t.override_tag && t.override_tag !== primary) {
            overrideTag = t.override_tag;
            active = overrideTag;
            break;
        }
    }
    const phase = _phaseFor(state.totalRolls || 0);
    const rationale = RATIONALE[active] || RATIONALE.BALANCED;
    return {
        active,
        primary,
        fallback,
        rationale,
        phase,
        scores,
        ranking,
        pivot_triggers: triggers.map(t => t.name),
        // Flat list of detail strings, matching bridge_strategy.py
        // (pivot_details = [t.detail for t in triggers]). The panel
        // escapeHtml()s each entry directly, so emitting {name, detail}
        // objects rendered every fired trigger as "[object Object]".
        pivot_details: triggers.map(t => t.detail),
        override_tag: overrideTag,
    };
}
