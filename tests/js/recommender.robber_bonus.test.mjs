// Standalone robber resource-control bonus parity (the no-bridge path).
// Run with:  node --test tests/js/
//
// Mirrors advisor.score_robber_targets' two bonuses: blocking a tile of
// a resource self owes for its next build (+1.0 + 0.2*pip), and locking
// a tile self already dominates production on (monopoly setup, capped 1).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { recommendRobberTargets } from '../../extension/lib/recommender.js';

test('robber prefers a tile of a resource self needs for its next build', () => {
  const state = {
    selfColor: '1', colors: ['1', '2'], vpTarget: 10,
    vp: { '1': 3, '2': 5 },
    handTotal: { '2': 4 },
    // 1 wheat short of a settlement -> WHEAT is a needed resource.
    hands: { '1': { WOOD: 1, BRICK: 1, SHEEP: 1, WHEAT: 0, ORE: 5 } },
    playedKnights: {}, devCardsByType: {}, roadLength: {},
    robberTile: 'R0',
    buildings: {
      nW: { color: '2', kind: 'SETTLEMENT' },
      nO: { color: '2', kind: 'SETTLEMENT' },
    },
    map: {
      tiles: {
        R0: { number: null, resource: null, nodes: [] },
        TW: { number: 6, resource: 'WHEAT', pip: 5, nodes: ['nW'] },
        TO: { number: 6, resource: 'ORE', pip: 5, nodes: ['nO'] },
      },
    },
  };
  const t = recommendRobberTargets(state);
  assert.equal(t[0].tile_id, 'TW', 'the needed-resource tile should rank first');
  const tw = t.find(x => x.tile_id === 'TW');
  assert.ok(tw.resource_need_bonus > 0, 'needed tile carries a need bonus');
  assert.equal(t.find(x => x.tile_id === 'TO').resource_need_bonus, 0);
});

test('robber adds a monopoly-setup bonus on a resource self dominates', () => {
  const state = {
    selfColor: '1', colors: ['1', '2'], vpTarget: 10,
    vp: { '1': 3, '2': 5 },
    handTotal: { '2': 4 },
    hands: { '1': { WOOD: 5, BRICK: 5, SHEEP: 5, WHEAT: 5, ORE: 5 } },  // owes nothing
    playedKnights: {}, devCardsByType: {}, roadLength: {},
    robberTile: 'R0',
    buildings: {
      s1: { color: '1', kind: 'SETTLEMENT' },
      s3: { color: '1', kind: 'SETTLEMENT' },
      o2: { color: '2', kind: 'SETTLEMENT' },
    },
    map: {
      nodes: { s1: { tiles: ['TW1'] }, s3: { tiles: ['TW3'] }, o2: { tiles: ['TW2'] } },
      tiles: {
        R0: { number: null, resource: null, pip: 0, nodes: [] },
        TW1: { number: 6, resource: 'WHEAT', pip: 5, nodes: ['s1'] },
        TW3: { number: 6, resource: 'WHEAT', pip: 5, nodes: ['s3'] },
        TW2: { number: 6, resource: 'WHEAT', pip: 5, nodes: ['o2'] }, // opp tile (target)
      },
    },
  };
  const t = recommendRobberTargets(state);
  const tw2 = t.find(x => x.tile_id === 'TW2');
  assert.ok(tw2, 'the opp WHEAT tile is a target');
  assert.ok(tw2.monopoly_setup_bonus > 0,
    'self dominating WHEAT production should add a monopoly-setup bonus');
});
