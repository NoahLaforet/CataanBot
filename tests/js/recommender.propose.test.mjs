// Node-based regression test for the standalone propose-trade resource
// reservation (parity with the bridge). Run with: node --test tests/js/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { _proposeTradeRecs } from '../../extension/lib/recommender.js';

test('propose-trade withholds a resource a near-term build still needs', () => {
  // A road is one BRICK short, so the recommender wants to trade for it.
  // ORE looks like a fat surplus (3 in hand, the road needs none) and the
  // OLD code would have offered it. But a city is two WHEAT short and
  // needs ORE:3, so it is a near-term build that reserves all three ORE.
  // The fixed code computes reservedAcross over near-term builds and must
  // NOT give that ORE away.
  const hand = { SHEEP: 1, ORE: 3, WOOD: 1 };
  const recs = _proposeTradeRecs({ map: {} }, hand, {});
  for (const r of recs) {
    assert.ok(
      !('ORE' in (r.give || {})),
      `offered ORE that a near-term city reserves: ${JSON.stringify(r)}`,
    );
  }
});

test('propose-trade still offers a genuinely spare resource', () => {
  // Settlement is one WHEAT short; ORE (2 held) is only reserved 1 by the
  // near-term dev card, so 1 ORE is genuinely spare and should be offered.
  const hand = { WOOD: 1, BRICK: 1, SHEEP: 1, ORE: 2 };
  const recs = _proposeTradeRecs({ map: {} }, hand, {});
  const settleTrade = recs.find((r) => r.unlocks === 'settlement');
  assert.ok(settleTrade, 'expected a propose-trade unlocking the settlement');
  assert.equal(settleTrade.get.WHEAT, 1);
});
