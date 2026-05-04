// index.js — single import surface for the standalone JS
// recommender. panel.js / content.js use:
//
//   import { bridgeReachable, runStandalone } from './lib/index.js';
//
// instead of pulling individual modules. Keeps the integration
// point narrow so swapping bridge ↔ standalone is a one-line
// change in the panel.

export { bridgeReachable, BRIDGE_URL, _resetBridgeCache }
    from './bridge_probe.js';
export { newGameState, newHand, newDevCardCounts, edgeKey,
         initSeats, RESOURCE_NAMES }
    from './state.js';
export { buildBoardFromColonistMap, pipsForNumber,
         PIP_DOTS_BY_NUMBER }
    from './board.js';
export { applyEvent, applyAll } from './events.js';
export { scoreOpeningNodes, scoreSecondSettlements,
         scoreRobberTargets,
         DIVERSITY_BY_COUNT, RESOURCE_WEIGHT }
    from './advisor.js';
export { recommendActions, handCanAfford, COSTS }
    from './recommender.js';
export { knightHint, monopolyHint, yopHint, rbHint }
    from './hints.js';
export { planBankTrades, evaluateIncomingTrade }
    from './trades.js';

import { newGameState, initSeats } from './state.js';
import { buildBoardFromColonistMap } from './board.js';
import { applyAll } from './events.js';
import { scoreOpeningNodes, scoreSecondSettlements,
         scoreRobberTargets } from './advisor.js';
import { recommendActions } from './recommender.js';
import { knightHint, monopolyHint, yopHint, rbHint } from './hints.js';

/** Top-level "give me a snap" entry point for the standalone path.
 *  Mirrors the shape of the bridge's /advisor snapshot so the
 *  panel renderer doesn't have to branch on bridge-vs-JS for the
 *  fields it consumes. Returns null when the board isn't built
 *  yet (still in setup), so the panel can skip rendering until
 *  the first GameStart frame lands.
 */
export function buildStandaloneSnap({ state, hand, opts } = {}) {
    if (!state || !state.selfColor) return null;
    const myHand = hand
        || state.hands[state.selfColor]
        || {};
    return {
        // Mirrored fields (bridge naming preserved):
        seq: 0,  // standalone has no bridge seq; bumped by panel
        game_started: !!state.started,
        vp_target: state.vpTarget,
        discard_limit: state.discardLimit,
        self: null,  // panel composes from state directly
        opps: [],
        last_roll: state.rollHistory[state.rollHistory.length - 1]
                    || null,
        roll_history: state.rollHistory.slice(),
        total_rolls: state.totalRolls,
        roll_histogram: { ...state.rollHistogram },
        recommendations: recommendActions(state, opts),
        knight_hint: knightHint(state, opts),
        monopoly_hint: monopolyHint(state, opts),
        yop_hint: yopHint(state, opts),
        rb_hint: rbHint(state, opts),
        robber_targets: scoreRobberTargets(state, opts),
        // Bridge-only fields stay null so the panel hides those
        // sections cleanly:
        strategy: null,
        game_plan: null,
        strategic_options: null,
        threat: null,
        win_proximity: null,
        winning_move: null,
        longest_road_race: null,
        largest_army_race: null,
        latest_postmortem: { seq: 0, available: false, written_at: 0 },
        game_over: state.gameOver,
        // Marker so the panel knows this snap came from the JS path,
        // not the bridge. Lets it skip bridge-specific operations
        // (e.g. POST /feedback for thumbs-down) gracefully.
        _source: 'standalone',
    };
}
