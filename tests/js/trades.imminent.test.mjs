// Standalone incoming-trade "don't feed an imminent winner" parity.
// Run with:  node --test tests/js/
//
// The trades.js short-circuit already existed; the gap was that the
// panel never passed oppImminent. detectImminentOpp is now exported so
// the panel can flag an offerer who could win next turn.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { evaluateIncomingTrade } from '../../extension/lib/trades.js';
import { detectImminentOpp } from '../../extension/lib/recommender.js';

test('detectImminentOpp flags an opp about to take Longest Road', () => {
  const state = {
    selfColor: '1', colors: ['1', '2'], vpTarget: 10,
    vp: { '1': 5, '2': 8 },
    playedKnights: {}, devCardsByType: {}, roadLength: { '2': 5 },
    hasArmy: null, hasRoad: null,
  };
  assert.equal(detectImminentOpp(state), '2');
});

test('detectImminentOpp returns null when nobody is one move from winning', () => {
  const state = {
    selfColor: '1', colors: ['1', '2'], vpTarget: 10,
    vp: { '1': 5, '2': 5 },
    playedKnights: {}, devCardsByType: {}, roadLength: { '2': 3 },
    hasArmy: null, hasRoad: null,
  };
  assert.equal(detectImminentOpp(state), null);
});

test('evaluateIncomingTrade declines feeding an imminent winner', () => {
  const state = {
    selfColor: '1', colors: ['1', '2'], vpTarget: 10,
    vp: { '1': 5, '2': 8 }, hands: {}, buildings: {}, map: null,
  };
  const hand = { WOOD: 2, BRICK: 0, SHEEP: 0, WHEAT: 0, ORE: 1 };
  const ev = evaluateIncomingTrade(state, hand, { WOOD: 1 }, { ORE: 1 },
    { recommend: () => [], oppVp: 8, vpTarget: 10, oppImminent: true });
  assert.equal(ev.verdict, 'decline');
  assert.match(ev.reason, /NEXT TURN/);
});
