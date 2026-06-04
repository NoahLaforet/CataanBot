// Standalone strategy pivot_details shape parity (the no-bridge path).
// Run with:  node --test tests/js/
//
// The panel escapeHtml()s each pivot_details entry directly, so they
// must be plain strings (bridge_strategy.py: pivot_details = [t.detail
// for t in triggers]). Emitting {name, detail} objects rendered every
// fired trigger as the literal "[object Object]".
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { computeStrategy } from '../../extension/lib/strategy.js';

test('pivot_details is a flat list of strings, not objects', () => {
  const state = {
    selfColor: '1',
    colors: ['1', '2'],
    vpTarget: 10,
    vp: { '1': 3, '2': 7 },        // opp at 7 VP fires opp_close_to_win
    handTotal: { '1': 3, '2': 5 },
    rollHistory: [],
    playedKnights: {},
    devCardsByType: {},
    roadLength: {},
    buildings: { n1: { color: '1', kind: 'SETTLEMENT' } },
    map: {
      nodes: { n1: { tiles: ['t1'], neighbors: [], port: null } },
      tiles: { t1: { resource: 'WHEAT', number: 6, pip: 5 } },
    },
  };
  const strat = computeStrategy(state);
  assert.ok(strat, 'strategy snapshot should be built when self has a node');
  assert.ok(strat.pivot_triggers.includes('opp_close_to_win'),
    'the opp-close-to-win trigger should fire at 7 VP');
  assert.ok(Array.isArray(strat.pivot_details));
  for (const d of strat.pivot_details) {
    assert.equal(typeof d, 'string', 'each pivot detail must be a string');
  }
  assert.ok(strat.pivot_details.some(d => d.includes('7 VP')),
    'the human detail line should carry through');
});
