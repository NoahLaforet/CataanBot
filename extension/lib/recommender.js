// recommender.js — in-game action recommender, JS port.
//
// Mirror of src/catanbot/recommender.py minus the 1-ply search
// rerank (that stays bridge-only). Ranks every legal action by a
// 1-10 heuristic score: settlement > city > road > dev card,
// modulated by production + VP gain.
//
// Phase 3 implementation pending. Stub returns empty list so the
// panel can call this unconditionally.

import { newHand } from './state.js';

const COSTS = {
    settlement: { WOOD: 1, BRICK: 1, SHEEP: 1, WHEAT: 1 },
    city: { WHEAT: 2, ORE: 3 },
    road: { WOOD: 1, BRICK: 1 },
    dev_card: { SHEEP: 1, WHEAT: 1, ORE: 1 },
};

/** Return up to `top` recommendations for self's current state.
 *  Each rec: {kind, when, score, detail, node_id?, edge?, tiles?,
 *  missing?}.
 *  `when` ∈ {"now", "soon"} mirrors the bridge contract.  */
export function recommendActions(state, opts = {}) {
    if (!state || !state.selfColor) return [];
    // TODO Phase 3.
    return [];
}

/** Helper: can `hand` cover `cost`? */
export function handCanAfford(hand, cost) {
    if (!hand || !cost) return false;
    for (const [r, n] of Object.entries(cost)) {
        if ((hand[r] || 0) < n) return false;
    }
    return true;
}

export { COSTS };
