// Standalone JS monopoly-hint regression (the no-bridge path).
// Run with:  node --test tests/js/
//
// Mirrors the bridge fix in bridge_hints.py _compute_monopoly_hint:
// a one-shot Monopoly is wasted on a tiny pot even when it technically
// unlocks a build, so PLAY now needs >= 2 cards on an unlock (and 4+
// for a no-unlock tempo swing). A 1-card pot must read HOLD.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { monopolyHint } from '../../extension/lib/hints.js';

// One opponent who produces ONLY wheat, so _estOppResource(state, 'WHEAT')
// resolves to that opp's full hand total (share = 1.0). Self is exactly
// one wheat short of a settlement, so +1 wheat "unlocks" a build.
function wheatPotState(oppHandTotal) {
  return {
    selfColor: '1',
    colors: ['1', '2'],
    handTotal: { '2': oppHandTotal },
    hands: { '1': { WOOD: 1, BRICK: 1, SHEEP: 1, WHEAT: 0, ORE: 0 } },
    devCardsByType: { '1': { MONOPOLY: 1 } },
    buildings: { oppNode: { color: '2', kind: 'SETTLEMENT' } },
    map: {
      nodes: { oppNode: { tiles: ['t1'] } },
      tiles: { t1: { resource: 'WHEAT', pip: 5 } },
    },
  };
}

test('monopoly HOLDs a 1-card unlock pot (one-shot card not worth 1 card)', () => {
  const hint = monopolyHint(wheatPotState(1));
  assert.equal(hint.target_resource, 'WHEAT');
  assert.equal(hint.est_total, 1);
  assert.equal(hint.should_play, false, '1-card pot must be HOLD even on an unlock');
  assert.match(hint.reason, /small pot/);
  assert.match(hint.reason, /save it/);
});

test('monopoly PLAYs when an unlock pot has >= 2 cards', () => {
  const hint = monopolyHint(wheatPotState(2));
  assert.equal(hint.target_resource, 'WHEAT');
  assert.equal(hint.est_total, 2);
  assert.equal(hint.should_play, true, '2-card unlock pot should PLAY');
  assert.match(hint.reason, /unlocks/);
});

test('monopoly PLAYs a large pot (>= 4) even without an unlock', () => {
  // Self already affords nothing extra here, but a 4-card pot is a
  // tempo swing worth the card on its own.
  const state = wheatPotState(4);
  // Give self a full hand so +wheat does not unlock anything new, forcing
  // the no-unlock branch to be exercised by the large-pot threshold.
  state.hands['1'] = { WOOD: 1, BRICK: 1, SHEEP: 1, WHEAT: 1, ORE: 3 };
  const hint = monopolyHint(state);
  assert.equal(hint.should_play, true);
  assert.match(hint.reason, /large pot|unlocks/);
});
