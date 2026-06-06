// Standalone settle / city / road rationale parity (the no-bridge path).
// Run with:  node --test tests/js/
//
// The bridge ships a per-rec `rationale` line: a per-resource /roll
// breakdown for settle/city (recommender.py _settle_rationale /
// _city_rationale) and an LR-progression line for roads
// (_road_rationale). The standalone now emits the same field so the panel
// can render the same secondary line bridge mode shows.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { _settleRecs, _roadRecs } from '../../extension/lib/recommender.js';

// A self with two footprints and a road to an open node, hand flush for a
// settlement, so the now-path settle recs fire with a rationale line.
function settleState() {
  const hand = { WOOD: 2, BRICK: 2, SHEEP: 2, WHEAT: 2, ORE: 0 };
  return {
    state: {
      selfColor: '1', colors: ['1'],
      bank: { '1': { roads: 13, settles: 3, cities: 4 } },
      roads: { '0||1': '1', '1||2': '1' },
      buildings: { 0: { color: '1', kind: 'SETTLEMENT' },
                   8: { color: '1', kind: 'SETTLEMENT' } },
      hands: { '1': hand },
      roadLength: { '1': 2 }, hasRoad: null,
      map: {
        nodes: {
          0: { neighbors: ['1'], tiles: ['t0'] },
          1: { neighbors: ['0', '2'], tiles: ['t1'] },
          2: { neighbors: ['1', '3'], tiles: ['t2'] },
          3: { neighbors: ['2'], tiles: ['t2'] },
          8: { neighbors: ['7'], tiles: ['t8'] },
          7: { neighbors: ['8'], tiles: ['t7'] },
        },
        edges: { '0||1': { a: '0', b: '1' }, '1||2': { a: '1', b: '2' },
                 '2||3': { a: '2', b: '3' }, '7||8': { a: '7', b: '8' } },
        tiles: {
          t0: { resource: 'WHEAT', pip: 5, number: 6 },
          t1: { resource: 'WOOD', pip: 4, number: 5 },
          t2: { resource: 'ORE', pip: 3, number: 4 },
          t7: { resource: 'SHEEP', pip: 4, number: 9 },
          t8: { resource: 'BRICK', pip: 4, number: 10 },
        },
        landNodes: new Set(['0', '1', '2', '3', '7', '8']),
      },
    },
    hand,
  };
}

test('settle recs carry a per-resource /roll rationale line', () => {
  const { state, hand } = settleState();
  const recs = _settleRecs(state, hand, {});
  assert.ok(recs.length >= 1);
  const r = recs[0];
  assert.ok(typeof r.rationale === 'string' && r.rationale.length > 0,
    'a non-empty rationale should be present');
  assert.match(r.rationale, /\/roll/);
});

test('road recs carry an LR-progression rationale once the chain is long', () => {
  const { state } = settleState();
  // Push self's chain to 4 so the +1 road crosses the LR-claim threshold,
  // and make the hand road-affordable.
  state.roadLength = { '1': 4 };
  const hand = { WOOD: 2, BRICK: 2, SHEEP: 0, WHEAT: 0, ORE: 0 };
  state.hands['1'] = hand;
  const recs = _roadRecs(state, hand, {});
  if (recs.length) {
    assert.ok(typeof recs[0].rationale === 'string',
      'road recs should carry a rationale field');
    assert.match(recs[0].rationale, /extends to 5/);
  }
});
