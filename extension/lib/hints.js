// hints.js — dev-card play timing hints, JS port.
//
// Mirror of bridge_hints.py's _compute_{knight,monopoly,yop,rb}_hint
// functions. Each returns null when the card type isn't held, or a
// {have, should_play, reason, ...} dict otherwise. Naturally
// self-contained: no Python-only deps beyond the state model.
//
// Phase 4 implementation pending.

/** When self has a Knight: play (robber on us / opp closing on LA /
 *  strong block / would secure LA) or hold (concealment, no urgent
 *  trigger, weak-pip robber tile + only 1 knight, etc.). */
export function knightHint(state, opts = {}) {
    if (!state || !state.selfColor) return null;
    // TODO Phase 4.
    return null;
}

/** When self has a Monopoly: which resource to target. */
export function monopolyHint(state, opts = {}) {
    if (!state || !state.selfColor) return null;
    return null;
}

/** When self has Year of Plenty: which two resources to take. */
export function yopHint(state, opts = {}) {
    if (!state || !state.selfColor) return null;
    return null;
}

/** When self has Road Building: which two edges to place. */
export function rbHint(state, opts = {}) {
    if (!state || !state.selfColor) return null;
    return null;
}
