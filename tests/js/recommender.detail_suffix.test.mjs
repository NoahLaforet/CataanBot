// Standalone rec-detail suffix parity: the " · settle #3" tag and the
// road " · <res> port" suffix. Run with:  node --test tests/js/
//
// Mirrors bridge recommender.py: settle detail gets " · settle #3" at
// two footprints, and a road landing on a 2:1 port for a resource self
// already produces gets " · <res> port" (_port_detail_suffix).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { _settleRecs, _roadRecs } from '../../extension/lib/recommender.js';

const ROAD_HAND = { WOOD: 1, BRICK: 1, SHEEP: 0, WHEAT: 0, ORE: 0 };
const SETTLE_HAND = { WOOD: 1, BRICK: 1, SHEEP: 1, WHEAT: 1, ORE: 0 };

test('settle detail gets " · settle #3" at two footprints', () => {
  // Self owns settlements at 0 and 8 (2 footprints) and a road 0-1-2, so
  // node 2 is the only legal settle spot; the next settle is the 3rd.
  const state = {
    selfColor: '1', colors: ['1'],
    bank: { '1': { roads: 13, settles: 3, cities: 4 } },
    roads: { '0||1': '1', '1||2': '1' },
    buildings: { 0: { color: '1', kind: 'SETTLEMENT' },
                 8: { color: '1', kind: 'SETTLEMENT' } },
    hands: { '1': SETTLE_HAND },
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
  const recs = _settleRecs(state, SETTLE_HAND, {});
  assert.ok(recs.length >= 1, 'node 2 should be a legal settle spot');
  assert.match(recs[0].detail, /settle #3/);
});

test('road detail gets a " · <res> port" suffix on a matching 2:1 port', () => {
  // 0(self) - 1 - 2 line: road 0->1 lands a settle spot at 2, which holds
  // a 2:1 WHEAT port; self produces WHEAT (node 0), so the suffix shows.
  const state = {
    selfColor: '1', colors: ['1'],
    bank: { '1': { roads: 15, settles: 5, cities: 4 } },
    roads: {},
    buildings: { 0: { color: '1', kind: 'SETTLEMENT' } },
    hands: { '1': ROAD_HAND },
    map: {
      nodes: {
        0: { neighbors: ['1'], tiles: ['t0'] },
        1: { neighbors: ['0', '2'], tiles: ['t0'] },
        2: { neighbors: ['1', '3'], tiles: ['t2'],
             port: { kind: '2:1', resource: 'WHEAT' } },
        3: { neighbors: ['2'], tiles: ['t2'] },
      },
      edges: { '0||1': { a: '0', b: '1' }, '1||2': { a: '1', b: '2' },
               '2||3': { a: '2', b: '3' } },
      tiles: { t0: { resource: 'WHEAT', pip: 5, number: 6 },
               t2: { resource: 'ORE', pip: 4, number: 5 } },
      landNodes: new Set(['0', '1', '2', '3']),
    },
  };
  const out = _roadRecs(state, ROAD_HAND, {});
  assert.ok(out.length >= 1);
  assert.match(out[0].detail, /· wheat port/);
});
