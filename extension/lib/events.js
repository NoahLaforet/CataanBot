// events.js — apply parsed colonist WS events to the JS game state.
//
// Mirror of the bridge's catanatron tracker + hand_tracker.py
// pipeline. Each handler is small (10-40 lines); the file's job is
// to dispatch a parsed event to the right mutator.
//
// Phase 1 implementation pending. Stubs in place so the panel can
// import this and call `applyEvent` unconditionally; non-handled
// events become no-ops and the existing bridge fallback path takes
// over.

import { newHand, newDevCardCounts, edgeKey } from './state.js';

/** Apply one parsed event to the game state in-place.
 *  Returns true when the event modified state, false otherwise.
 *
 *  Event shape mirrors the bridge's parser output:
 *    {kind, ...kind-specific fields}
 *
 *  Example kinds: 'roll', 'build_settlement', 'build_road',
 *  'build_city', 'buy_dev_card', 'play_knight', 'play_monopoly',
 *  'play_year_of_plenty', 'play_road_building', 'trade',
 *  'discard', 'steal', 'monopoly_steal', 'robber_move',
 *  'game_start', 'game_over'.
 */
export function applyEvent(state, event) {
    if (!state || !event || !event.kind) return false;
    // TODO Phase 1 — port the relevant handlers from
    // src/catanbot/{tracker,hand_tracker,events,parser}.py.
    return false;
}

/** Convenience: apply a stream of events. */
export function applyAll(state, events) {
    let changed = false;
    for (const ev of events || []) {
        if (applyEvent(state, ev)) changed = true;
    }
    return changed;
}
