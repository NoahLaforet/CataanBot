// advisor.js — opening picks + robber target ranking, JS port.
//
// Heuristic-only port of src/catanbot/advisor.py. No 1-ply search
// (that stays bridge-only — depends on catanatron's Game.copy()).
// Ranks every land node for opening settlement, every land tile
// for robber placement.
//
// Phase 2/4 implementation pending. Stubs return empty arrays so
// downstream callers can ship the import without a runtime crash.

import { pipsForNumber } from './board.js';

const DIVERSITY_BY_COUNT = { 0: 1.0, 1: 1.0, 2: 1.08, 3: 1.22 };
const RESOURCE_WEIGHT = {
    WHEAT: 1.10, WOOD: 1.0, BRICK: 1.0, SHEEP: 1.0, ORE: 1.0,
};

/** Rank every legal opening node by complement-aware production
 *  + port + denial + diversity. Mirror of
 *  advisor.score_opening_nodes. Returns array of NodeScore-shaped
 *  dicts sorted descending by score. */
export function scoreOpeningNodes(state, opts = {}) {
    if (!state || !state.map) return [];
    // TODO Phase 2.
    return [];
}

/** Rank a hypothetical 2nd settle pair against the placed first
 *  settle. Mirror of advisor.score_second_settlements. */
export function scoreSecondSettlements(state, firstNodeId, opts = {}) {
    if (!state || !state.map || firstNodeId == null) return [];
    // TODO Phase 2.
    return [];
}

/** Rank every land tile for robber placement value. Mirror of
 *  advisor.score_robber_targets. */
export function scoreRobberTargets(state, opts = {}) {
    if (!state || !state.map) return [];
    // TODO Phase 4.
    return [];
}

export { DIVERSITY_BY_COUNT, RESOURCE_WEIGHT };
