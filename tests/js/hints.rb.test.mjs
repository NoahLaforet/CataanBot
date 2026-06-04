// Standalone Road Building hint regression (the no-bridge path).
// Run with:  node --test tests/js/
//
// Mirrors bridge_hints.py _compute_rb_hint: PLAY on a longest-road swing
// (secures LR / catches opp LR) or when low on road pieces, else HOLD
// with a clear reason instead of "hold forever".
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rbHint } from '../../extension/lib/hints.js';

function roadsPlaced(n, color = '1') {
  const r = {};
  for (let i = 0; i < n; i += 1) r[`e${i}`] = color;
  return r;
}

function rbState({ selfLen, oppLen, selfHasRoad = false, oppHasRoad = false,
                   placed = 3 }) {
  return {
    selfColor: '1',
    colors: ['1', '2'],
    devCardsByType: { '1': { ROAD_BUILDING: 1 } },
    roadLength: { '1': selfLen, '2': oppLen },
    hasRoad: selfHasRoad ? '1' : (oppHasRoad ? '2' : null),
    roads: roadsPlaced(placed),
    buildings: {},
    map: { tiles: {}, nodes: {}, edges: {} },
  };
}

test('secures LR -> PLAY with the projection in the reason', () => {
  const h = rbHint(rbState({ selfLen: 4, oppLen: 3, placed: 4 }));
  assert.equal(h.should_play, true);
  assert.match(h.reason, /secures LR/);
  assert.match(h.reason, /4→6 vs 3/);
});

test('catches an opp about to take LR -> PLAY', () => {
  const h = rbHint(rbState({ selfLen: 4, oppLen: 6, oppHasRoad: true,
                             placed: 4 }));
  assert.equal(h.should_play, true);
  assert.match(h.reason, /catches opp LR/);
});

test('low on road pieces -> PLAY before the card loses value', () => {
  const h = rbHint(rbState({ selfLen: 2, oppLen: 2, placed: 13 }));
  assert.equal(h.should_play, true);
  assert.match(h.reason, /low on roads · 2 left/);
});

test('no swing and plenty of roads -> HOLD with a clear reason', () => {
  const h = rbHint(rbState({ selfLen: 2, oppLen: 2, placed: 3 }));
  assert.equal(h.should_play, false);
  assert.match(h.reason, /no clear swing yet/);
  assert.doesNotMatch(h.reason, /forever/);
});
