// Standalone propose-trade supply-gating parity (the no-bridge path).
// Run with:  node --test tests/js/
//
// The bridge suppresses a propose_trade ask unless the board produces
// the resource and someone can supply it (recommender.py 1733-1759). The
// standalone now gates on board-produces and "any opponent holds a card"
// (the per-resource holder check needs the hand tracker, deferred).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { _proposeTradeRecs } from '../../extension/lib/recommender.js';

// Hand 1 ORE short of a city (WHEAT 2, ORE 3), everything else affordable
// so only the city drives a propose_trade for ORE.
const HAND = { WOOD: 3, BRICK: 1, SHEEP: 1, WHEAT: 2, ORE: 2 };

function mk({ boardHasOre = true, oppCards = 5 } = {}) {
  const tiles = {
    a: { resource: 'WOOD', pip: 4 }, b: { resource: 'BRICK', pip: 4 },
    c: { resource: 'SHEEP', pip: 4 }, d: { resource: 'WHEAT', pip: 4 },
  };
  if (boardHasOre) tiles.e = { resource: 'ORE', pip: 5 };
  return {
    selfColor: '1', colors: ['1', '2'],
    handTotal: { '2': oppCards },
    hands: { '1': HAND },
    map: { tiles },
  };
}

test('proposes an ORE trade when the board makes ORE and an opp has cards', () => {
  const recs = _proposeTradeRecs(mk(), HAND, {});
  const ore = recs.find(r => r.get && r.get.ORE);
  assert.ok(ore, 'a city-unlocking ORE proposal should appear');
});

test('no ORE proposal when the board produces no ORE (variant map)', () => {
  const recs = _proposeTradeRecs(mk({ boardHasOre: false }), HAND, {});
  assert.ok(!recs.some(r => r.get && r.get.ORE),
    'board-produces gate must suppress the ORE ask');
});

test('no proposal when every opponent is empty-handed', () => {
  const recs = _proposeTradeRecs(mk({ oppCards: 0 }), HAND, {});
  assert.ok(!recs.some(r => r.get && r.get.ORE),
    'nobody can supply it, so no ask');
});
