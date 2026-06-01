// Node-based regression tests for standalone settlement scoring parity
// with the bridge. Run with:  node --test tests/js/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { _scoreSettlement, _weightedProd }
  from '../../extension/lib/recommender.js';

const clip = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const round1 = (v) => Math.round(v * 10) / 10;

test('settlement score matches the bridge formula (wheat-weighted, no diversity)', () => {
  // A 3-distinct corner: WHEAT on a 5, ORE on a 4, BRICK on a 3 (pips/36).
  const prod = { WHEAT: 5 / 36, ORE: 4 / 36, BRICK: 3 / 36 };
  const weighted = (5 / 36) * 1.10 + (4 / 36) * 1.0 + (3 / 36) * 1.0;
  assert.equal(_weightedProd(prod), weighted);
  // recommender.py: _score_settlement(weighted) = clip(weighted*12+2, 2, 10).
  assert.equal(_scoreSettlement(prod), round1(clip(weighted * 12 + 2, 2, 10)));
});

test('settlement score is no longer inflated by resource diversity', () => {
  // Two corners with the SAME wheat-weighted production but different
  // diversity must now score identically. The old JS multiplied by a
  // 1.0 / 1.08 / 1.22 diversity factor, so a 3-distinct corner scored
  // ~1.0 higher than an equal-production single-resource corner, which
  // the bridge never did.
  const threeWay = { WOOD: 3 / 36, SHEEP: 3 / 36, ORE: 3 / 36 };
  const oneWay = { WOOD: 9 / 36 };
  assert.equal(_weightedProd(threeWay), _weightedProd(oneWay));
  assert.equal(_scoreSettlement(threeWay), _scoreSettlement(oneWay));
});
