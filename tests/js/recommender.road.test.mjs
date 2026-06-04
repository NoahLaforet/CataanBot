// Standalone road-rec parity: sealed-fallback + from->to edge labeling.
// Run with:  node --test tests/js/
//
// Mirrors the bridge road-rec path (recommender.py 1331-1476): when every
// corridor is blocked for settling, emit a degraded "extends network ·
// no settle spot" rec (sealed:true) instead of dropping the road
// entirely; every road carries edge_from / edge_to (self network outward)
// and a landing_node field.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { _roadRecs } from '../../extension/lib/recommender.js';

const ROAD_HAND = { WOOD: 1, BRICK: 1, SHEEP: 0, WHEAT: 0, ORE: 0 };

function baseState(map, buildings) {
  return {
    selfColor: '1',
    colors: ['1'],
    bank: { '1': { roads: 15, settles: 5, cities: 4 } },
    roads: {},
    buildings,
    hands: { '1': ROAD_HAND },
    map,
  };
}

test('sealed corridor -> a degraded "extends network" rec, not nothing', () => {
  // Self settlement at 0; its only neighbour 1 has no unblocked settle
  // spot of its own, so the settle-spot search finds nothing.
  const map = {
    nodes: { 0: { neighbors: ['1'], tiles: ['t1'] },
             1: { neighbors: ['0'], tiles: ['t1'] } },
    edges: { '0||1': { a: '0', b: '1' } },
    tiles: { t1: { resource: 'WHEAT', pip: 5, number: 6 } },
    landNodes: new Set(['0', '1']),
  };
  const out = _roadRecs(baseState(map, { 0: { color: '1', kind: 'SETTLEMENT' } }),
    ROAD_HAND, {});
  assert.equal(out.length, 1);
  assert.equal(out[0].sealed, true);
  assert.match(out[0].detail, /extends network/);
  assert.ok(out[0].score >= 1.0, 'sealed score honours the 1-10 floor');
  assert.equal(out[0].edge_from, '0');
  assert.equal(out[0].edge_to, '1');
});

test('a settle-spot road carries landing_node and from->to labels', () => {
  // 0(self) - 1 - 2 - 3 line: road 0->1 opens a settle spot at 2.
  const map = {
    nodes: {
      0: { neighbors: ['1'], tiles: ['t0'] },
      1: { neighbors: ['0', '2'], tiles: ['t0'] },
      2: { neighbors: ['1', '3'], tiles: ['t2'] },
      3: { neighbors: ['2'], tiles: ['t2'] },
    },
    edges: { '0||1': { a: '0', b: '1' }, '1||2': { a: '1', b: '2' },
             '2||3': { a: '2', b: '3' } },
    tiles: { t0: { resource: 'WHEAT', pip: 5, number: 6 },
             t2: { resource: 'ORE', pip: 4, number: 5 } },
    landNodes: new Set(['0', '1', '2', '3']),
  };
  const out = _roadRecs(baseState(map, { 0: { color: '1', kind: 'SETTLEMENT' } }),
    ROAD_HAND, {});
  assert.ok(out.length >= 1);
  assert.ok(!out[0].sealed, 'a settle-spot road is not sealed');
  assert.equal(out[0].landing_node, '2');
  assert.equal(out[0].edge_from, '0');
  assert.equal(out[0].edge_to, '1');
});
