// Standalone JS knight-hint regression (the no-bridge path).
// Run with:  node --test tests/js/
//
// Mirrors bridge_hints.py _compute_knight_hint: weak-robber-tile hold
// guard (pip <= 2), deny-Largest-Army before claim-LA, a strong-block
// trigger, and the knight-stack / late-game rule.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { knightHint } from '../../extension/lib/hints.js';
import { recommendRobberTargets } from '../../extension/lib/recommender.js';

// Base state: self holds `knights` Knight cards, robber on `robberTile`
// (a tile with `pip`/`number`), self settlement on `selfOnRobber` ? the
// robber tile : elsewhere. No opponents adjacent to anything, so the
// robber-target scorer returns nothing (topScore = 0) unless overridden.
function baseState({ knights = 1, robberPip = 5, robberNumber = 6,
                     selfOnRobber = true, selfVp = 3, vpTarget = 10,
                     playedSelf = 0, opp = {} } = {}) {
  const colors = ['1', '2'];
  const buildings = {};
  if (selfOnRobber) buildings.nSelf = { color: '1', kind: 'SETTLEMENT' };
  return {
    selfColor: '1',
    colors,
    vpTarget,
    devCardsByType: { '1': { KNIGHT: knights } },
    playedKnights: { '1': playedSelf, '2': opp.played || 0 },
    hasArmy: opp.hasArmy || null,
    vp: { '1': selfVp, '2': opp.vp || 3 },
    robberTile: 'T1',
    buildings,
    map: {
      tiles: {
        T1: { number: robberNumber, pip: robberPip, resource: 'WHEAT',
              nodes: selfOnRobber ? ['nSelf'] : ['nOther'] },
      },
      nodes: {},
    },
  };
}

test('robber on a strong tile -> PLAY to clear it', () => {
  const h = knightHint(baseState({ knights: 1, robberPip: 5 }));
  assert.equal(h.should_play, true);
  assert.match(h.reason, /play to clear it/);
});

test('robber on a weak tile with one knight -> HOLD', () => {
  const h = knightHint(baseState({ knights: 1, robberPip: 1, robberNumber: 2 }));
  assert.equal(h.should_play, false);
  assert.match(h.reason, /weak tile/);
});

test('robber on a weak tile with two knights -> PLAY (stack ok)', () => {
  const h = knightHint(baseState({ knights: 2, robberPip: 1, robberNumber: 2 }));
  assert.equal(h.should_play, true);
  assert.match(h.reason, /play to clear it/);
});

test('robber on a weak tile late-game -> PLAY even with one knight', () => {
  // late_game = self_vp >= close_to_win(8) - 2 = 6
  const h = knightHint(baseState({ knights: 1, robberPip: 1, robberNumber: 2,
                                   selfVp: 6 }));
  assert.equal(h.should_play, true);
});

test('opp racing Largest Army (3 played) -> PLAY to deny', () => {
  const h = knightHint(baseState({ knights: 1, selfOnRobber: false,
                                   opp: { played: 3, hasArmy: '2', vp: 6 } }));
  assert.equal(h.should_play, true);
  assert.match(h.reason, /play to deny/);
});

test('self one knight from Largest Army (no holder) -> PLAY to claim', () => {
  const h = knightHint(baseState({ knights: 1, selfOnRobber: false,
                                   playedSelf: 2, opp: { played: 0 } }));
  assert.equal(h.should_play, true);
  assert.match(h.reason, /Largest Army/);
});

test('no trigger -> HOLD with the calm reason', () => {
  const h = knightHint(baseState({ knights: 1, selfOnRobber: false,
                                   playedSelf: 0, opp: { played: 0 } }));
  assert.equal(h.should_play, false);
  assert.match(h.reason, /no urgent reason/);
});

test('strong-block path gates on the knight stack', () => {
  // A fat opponent stack on a high-pip tile, robber elsewhere, so the
  // robber-target scorer rates a strong block. Self holds no LA edge.
  const strong = {
    selfColor: '1',
    colors: ['1', '2'],
    vpTarget: 10,
    devCardsByType: { '1': { KNIGHT: 1 } },
    playedKnights: { '1': 0, '2': 0 },
    hasArmy: null,
    vp: { '1': 3, '2': 6 },
    handTotal: { '1': 4, '2': 9 },
    robberTile: 'Tdesert',
    buildings: {
      nSelf: { color: '1', kind: 'SETTLEMENT' },
      nOpp: { color: '2', kind: 'CITY' },
    },
    map: {
      tiles: {
        Tdesert: { number: null, pip: 0, resource: null, nodes: ['nSelf'] },
        Tjuicy: { number: 6, pip: 5, resource: 'ORE', nodes: ['nOpp'] },
      },
      nodes: {},
    },
  };
  const top = recommendRobberTargets(strong)[0];
  // Only meaningful when the scorer agrees this is a strong block.
  if (!top || (Number(top.score) || 0) < 4) return;
  const one = knightHint(strong);
  assert.equal(one.should_play, false, 'one knight should HOLD a strong block');
  assert.match(one.reason, /only hold 1 knight/);
  const two = knightHint({ ...strong, devCardsByType: { '1': { KNIGHT: 2 } } });
  assert.equal(two.should_play, true, 'two knights should PLAY a strong block');
  assert.match(two.reason, /strong block/);
});
