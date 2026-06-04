// Standalone rec-detail per-roll formatting parity.
// Run with:  node --test tests/js/
//
// The bridge rec detail carries the node's expected cards per roll
// (`+{prod:.2f}/roll`); the standalone used static placeholders. _perRoll
// sums the pip/36 production shares and formats to 2 decimals.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { _perRoll } from '../../extension/lib/recommender.js';

test('_perRoll sums production shares to a 2-decimal per-roll string', () => {
  assert.equal(_perRoll({ WHEAT: 5 / 36, ORE: 4 / 36, BRICK: 3 / 36 }), '0.33');
  assert.equal(_perRoll({ WHEAT: 9 / 36 }), '0.25');
});

test('_perRoll tolerates an empty or missing map', () => {
  assert.equal(_perRoll({}), '0.00');
  assert.equal(_perRoll(null), '0.00');
});
