// Standalone Year of Plenty hint shape parity (the no-bridge path).
// Run with:  node --test tests/js/
//
// The panel reads yop_hint.pair / .unlock / .bank_ok (bridge names); the
// standalone emitted take / target_kind, so the YoP card rendered empty.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { yopHint } from '../../extension/lib/hints.js';

function mk(hand) {
  return {
    selfColor: '1',
    devCardsByType: { '1': { YEAR_OF_PLENTY: 1 } },
    hands: { '1': hand },
  };
}

test('YoP PLAY emits pair / unlock / bank_ok when 2 cards unlock a build', () => {
  // 1 ORE short of a city (WHEAT 2, ORE 3): +2 ORE unlocks it.
  const h = yopHint(mk({ WOOD: 0, BRICK: 0, SHEEP: 0, WHEAT: 2, ORE: 1 }));
  assert.equal(h.should_play, true);
  assert.equal(h.unlock, 'city');
  assert.ok(Array.isArray(h.pair) && h.pair.length === 2);
  assert.ok(h.pair.includes('ORE'));
  assert.equal(h.bank_ok, true);
  assert.equal(h.take, undefined, 'old field name must be gone');
});

test('YoP from an empty hand unlocks the cheapest build (road) with a pair', () => {
  // Empty hand: only the 2-card road is within reach, so PLAY unlocks
  // road with pair = WOOD + BRICK (city/settlement/dev need 3+).
  const h = yopHint(mk({ WOOD: 0, BRICK: 0, SHEEP: 0, WHEAT: 0, ORE: 0 }));
  assert.equal(h.should_play, true);
  assert.equal(h.unlock, 'road');
  assert.deepEqual([...h.pair].sort(), ['BRICK', 'WOOD']);
  assert.equal(h.bank_ok, true);
  assert.equal(h.take, undefined, 'old field name must be gone');
});
