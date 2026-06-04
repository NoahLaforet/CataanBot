// Standalone robber suggested-victim VP-priority parity.
// Run with:  node --test tests/js/
//
// Mirrors bridge_robber._compute_robber_snapshot._victim_priority:
// on a multi-opp tile the suggested victim weights card count by a VP
// tier (3.0 at close-to-win, 1.8 at mid-late, else 1.0) plus a small
// pip nudge, so a near-winner can be preferred over a card-rich laggard.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { recommendRobberTargets } from '../../extension/lib/recommender.js';

test('suggested victim prefers a near-winner over a card-rich low-VP opp', () => {
  const state = {
    selfColor: '1', colors: ['1', '2', '3'], vpTarget: 10,
    vp: { '1': 3, '2': 8, '3': 3 },        // opp 2 is at close-to-win (>=8)
    handTotal: { '2': 2, '3': 5 },         // opp 3 holds more cards
    playedKnights: {}, devCardsByType: {}, roadLength: {},
    robberTile: 'R0',
    buildings: {
      n2: { color: '2', kind: 'SETTLEMENT' },
      n3: { color: '3', kind: 'SETTLEMENT' },
    },
    map: {
      tiles: {
        R0: { number: null, resource: null, nodes: [] },
        T: { number: 6, resource: 'WHEAT', pip: 5, nodes: ['n2', 'n3'] },
      },
    },
  };
  const t = recommendRobberTargets(state);
  assert.equal(t[0].steal_from_color, '2',
    'the near-winner should be suggested despite holding fewer cards');
});

test('with everyone empty-handed, the higher-VP victim is still suggested', () => {
  const state = {
    selfColor: '1', colors: ['1', '2', '3'], vpTarget: 10,
    vp: { '1': 3, '2': 8, '3': 3 },
    handTotal: { '2': 0, '3': 0 },
    playedKnights: {}, devCardsByType: {}, roadLength: {},
    robberTile: 'R0',
    buildings: {
      n2: { color: '2', kind: 'SETTLEMENT' },
      n3: { color: '3', kind: 'SETTLEMENT' },
    },
    map: {
      tiles: {
        R0: { number: null, resource: null, nodes: [] },
        T: { number: 6, resource: 'WHEAT', pip: 5, nodes: ['n2', 'n3'] },
      },
    },
  };
  const t = recommendRobberTargets(state);
  assert.equal(t[0].steal_from_color, '2');
});
