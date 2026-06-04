// Standalone soon-plan missing-cards detail parity (the no-bridge path).
// Run with:  node --test tests/js/
//
// The bridge "save for X" soon-plans lead their detail with the missing
// cards (_format_missing -> "need ..."). The standalone now prefixes
// soon settle/city/dev details the same way (text form).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { _settleRecs } from '../../extension/lib/recommender.js';

test('an unaffordable settle soon-plan leads with the missing cards', () => {
  // Two footprints + a road to an open node 2, but the hand is 1 sheep
  // short of a settlement -> a "soon" rec detailed "need 1 sheep · ...".
  const hand = { WOOD: 1, BRICK: 1, SHEEP: 0, WHEAT: 1, ORE: 0 };
  const state = {
    selfColor: '1', colors: ['1'],
    bank: { '1': { roads: 13, settles: 3, cities: 4 } },
    roads: { '0||1': '1', '1||2': '1' },
    buildings: { 0: { color: '1', kind: 'SETTLEMENT' },
                 8: { color: '1', kind: 'SETTLEMENT' } },
    hands: { '1': hand },
    map: {
      nodes: {
        0: { neighbors: ['1'], tiles: ['t'] },
        1: { neighbors: ['0', '2'], tiles: ['t'] },
        2: { neighbors: ['1', '3'], tiles: ['t'] },
        3: { neighbors: ['2'], tiles: ['t'] },
        8: { neighbors: ['7'], tiles: ['t'] },
        7: { neighbors: ['8'], tiles: ['t'] },
      },
      edges: { '0||1': { a: '0', b: '1' }, '1||2': { a: '1', b: '2' },
               '2||3': { a: '2', b: '3' }, '7||8': { a: '7', b: '8' } },
      tiles: { t: { resource: 'WHEAT', pip: 5, number: 6 } },
      landNodes: new Set(['0', '1', '2', '3', '7', '8']),
    },
  };
  const recs = _settleRecs(state, hand, {});
  assert.ok(recs.length >= 1);
  assert.equal(recs[0].when, 'soon');
  assert.match(recs[0].detail, /^need 1 sheep/);
  assert.match(recs[0].detail, /\/roll/);
});
