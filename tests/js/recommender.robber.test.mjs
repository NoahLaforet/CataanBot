// Node-based regression tests for the standalone JS recommender (the
// no-bridge / extension-only path). Run with:  node --test tests/js/
// These import the pure lib/ ES modules directly; they have no Chrome
// or DOM dependencies, so they run headless in CI.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { recommendRobberTargets } from '../../extension/lib/recommender.js';

test('robber list keeps a self-adjacent tile instead of returning empty (Bug 1)', () => {
  // One productive tile touched by BOTH a self settlement and an opp
  // settlement. Pre-fix the tile was dropped (a `hasSelfAdj continue`),
  // so on boards where every opp-adjacent productive tile also touched
  // one of our own settlements the list came back empty and the panel
  // rendered no robber table at all even though a placement was owed.
  const state = {
    selfColor: '1',
    robberTile: '99',
    vp: { '1': 3, '2': 4 },
    handTotal: { '1': 5, '2': 6 },
    buildings: {
      nSelf: { color: '1', kind: 'SETTLEMENT' },
      nOpp: { color: '2', kind: 'SETTLEMENT' },
    },
    map: {
      tiles: {
        5: { resource: 'WHEAT', number: 6, nodes: ['nSelf', 'nOpp'] },
      },
    },
  };
  const targets = recommendRobberTargets(state, {});
  assert.ok(targets.length >= 1, 'self-adjacent tile must still be listed');
  assert.equal(String(targets[0].tile_id), '5');
  assert.equal(targets[0].steal_from_color, '2');
});

test('robber list still drops a tile with no opponent adjacency', () => {
  // A tile only our own settlement touches has no steal value, so it
  // must NOT be offered as a robber target.
  const state = {
    selfColor: '1',
    robberTile: '99',
    vp: { '1': 3 },
    handTotal: { '1': 5 },
    buildings: { nSelf: { color: '1', kind: 'SETTLEMENT' } },
    map: {
      tiles: { 5: { resource: 'WHEAT', number: 6, nodes: ['nSelf'] } },
    },
  };
  const targets = recommendRobberTargets(state, {});
  assert.equal(targets.length, 0);
});
