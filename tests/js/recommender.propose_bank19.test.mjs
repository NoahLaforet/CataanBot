// Standalone propose-trade bank-19 supply gate (the no-bridge path).
// Run with:  node --test tests/js/
//
// The bridge skips a propose_trade ask when the bank still holds all 19
// of the needed resource: nobody can be holding a card of it, so the ask
// is dead on arrival (recommender.py 1738-1740). The standalone now honors
// the same guard when panel.js threads a tracked bankSupply.remaining map.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { _proposeTradeRecs } from '../../extension/lib/recommender.js';

// Hand 1 ORE short of a city (WHEAT 2, ORE 3); everything else affordable
// so only the city drives a propose_trade for ORE.
const HAND = { WOOD: 3, BRICK: 1, SHEEP: 1, WHEAT: 2, ORE: 2 };

function mk() {
  return {
    selfColor: '1', colors: ['1', '2'],
    handTotal: { '2': 5 },
    hands: { '1': HAND },
    map: {
      tiles: {
        a: { resource: 'WOOD', pip: 4 }, b: { resource: 'BRICK', pip: 4 },
        c: { resource: 'SHEEP', pip: 4 }, d: { resource: 'WHEAT', pip: 4 },
        e: { resource: 'ORE', pip: 5 },
      },
    },
  };
}

test('no ORE proposal when the bank still holds all 19 ORE', () => {
  const opts = { bankSupply: { tracked: true, remaining: { ORE: 19 } } };
  const recs = _proposeTradeRecs(mk(), HAND, opts);
  assert.ok(!recs.some(r => r.get && r.get.ORE),
    'bank-19 gate must suppress an ask nobody can supply');
});

test('ORE proposal survives when some ORE is in play (remaining < 19)', () => {
  const opts = { bankSupply: { tracked: true, remaining: { ORE: 15 } } };
  const recs = _proposeTradeRecs(mk(), HAND, opts);
  assert.ok(recs.some(r => r.get && r.get.ORE),
    'with ORE out of the bank, the ask is plausible and should appear');
});

test('untracked bankSupply leaves the bank-19 gate off', () => {
  // remaining says 19 but tracked is false (opaque opp), so we cannot
  // prove nobody holds it; the gate must not fire.
  const opts = { bankSupply: { tracked: false, remaining: { ORE: 19 } } };
  const recs = _proposeTradeRecs(mk(), HAND, opts);
  assert.ok(recs.some(r => r.get && r.get.ORE),
    'an untracked supply map must not suppress the ask');
});
