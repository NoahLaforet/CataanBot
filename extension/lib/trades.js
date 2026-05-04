// trades.js — bank/port trade unlocks + propose-trade evaluator,
// JS port.
//
// Mirror of recommender.py's _plan_bank_trades and the propose-
// trade block + the evaluate_incoming_trade function. Both are
// pure heuristics over hand + opp-hands + bank state.
//
// Phase 5 implementation pending.

/** Plan a sequence of bank/port trades that makes `cost`
 *  affordable from `hand`. Returns null when impossible. */
export function planBankTrades(state, hand, cost, opts = {}) {
    if (!state || !hand || !cost) return null;
    // TODO Phase 5.
    return null;
}

/** Evaluate an incoming player-to-player offer from self's seat. */
export function evaluateIncomingTrade(state, hand, give, want, opts = {}) {
    if (!state || !hand) {
        return { verdict: 'consider', score: 0,
                 reason: 'state not ready' };
    }
    // TODO Phase 5.
    return { verdict: 'consider', score: 0, reason: 'JS port pending' };
}
