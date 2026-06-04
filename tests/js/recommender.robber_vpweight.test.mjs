// Standalone robber-target VP-weighting parity (the no-bridge path).
// Run with:  node --test tests/js/
//
// Mirrors advisor.score_robber_targets: the tile score is the sum of
// each victim's blocked pips scaled by _vp_weight(their VP), times 2
// for an opponent who could win next turn. Card count is NOT in the
// tile score (it only picks the suggested victim).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { recommendRobberTargets } from '../../extension/lib/recommender.js';

function twoTileState({ vp2, vp3, cards2 = 3, cards3 = 3,
                        road2 = 0, road3 = 0 }) {
  return {
    selfColor: '1',
    colors: ['1', '2', '3'],
    vpTarget: 10,
    vp: { '1': 3, '2': vp2, '3': vp3 },
    handTotal: { '2': cards2, '3': cards3 },
    playedKnights: {},
    devCardsByType: {},
    roadLength: { '2': road2, '3': road3 },
    hasArmy: null,
    hasRoad: null,
    robberTile: 'R0',
    buildings: {
      n2: { color: '2', kind: 'SETTLEMENT' },
      n3: { color: '3', kind: 'SETTLEMENT' },
    },
    map: {
      tiles: {
        R0: { number: null, resource: null, nodes: [] },
        T2: { number: 6, resource: 'WHEAT', nodes: ['n2'] },
        T3: { number: 6, resource: 'ORE', nodes: ['n3'] },
      },
    },
  };
}

test('a higher-VP victim ranks above an equal-pip lower-VP victim', () => {
  const t = recommendRobberTargets(twoTileState({ vp2: 8, vp3: 3 }));
  assert.equal(t[0].tile_id, 'T2', 'the vp-8 victim tile should rank first');
});

test('card count is no longer in the tile score (VP wins over a fat hand)', () => {
  // T2: low-VP victim with a huge hand. T3: high-VP victim, tiny hand.
  // Pre-parity the card term made T2 win; now T3 (high VP) must win.
  const t = recommendRobberTargets(
    twoTileState({ vp2: 3, vp3: 8, cards2: 10, cards3: 1 }));
  assert.equal(t[0].tile_id, 'T3', 'high-VP victim outranks a card-rich low-VP one');
});

test('an imminent winner gets the 2x blocking multiplier', () => {
  // Both victims at VP 8, but opp 2 could take Longest Road next turn
  // (road 5, nobody else near), so its tile gets doubled.
  const t = recommendRobberTargets(
    twoTileState({ vp2: 8, vp3: 8, road2: 5, road3: 0 }));
  assert.equal(t[0].tile_id, 'T2', 'the imminent winner tile should rank first');
});

test('explicit imminentColor opt overrides detection', () => {
  const state = twoTileState({ vp2: 8, vp3: 8 });
  const t = recommendRobberTargets(state, { imminentColor: '3' });
  assert.equal(t[0].tile_id, 'T3', 'opt-supplied imminent color should win');
});
