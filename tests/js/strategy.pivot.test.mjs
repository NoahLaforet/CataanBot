// Standalone strategy pivot-trigger parity (the no-bridge path).
// Run with:  node --test tests/js/
//
// Covers the pivot_details string shape and the trigger thresholds:
// opp_close_to_win = round(target*0.8), opp_close_to_la = round(target*0.7)-1
// with no "opp knights > mine" gate, and the hot-number / seven-overdue
// detectors with no history-length floor (all matching bridge_strategy /
// strategy_select).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { computeStrategy } from '../../extension/lib/strategy.js';

function mk(over = {}) {
  return {
    selfColor: '1',
    colors: ['1', '2'],
    vpTarget: 10,
    vp: { '1': 3, '2': 3 },
    handTotal: { '1': 3, '2': 5 },
    discardLimit: 7,
    rollHistory: [],
    playedKnights: {},
    devCardsByType: {},
    roadLength: {},
    buildings: { n1: { color: '1', kind: 'SETTLEMENT' } },
    map: {
      nodes: { n1: { tiles: ['t1'], neighbors: [], port: null } },
      tiles: { t1: { resource: 'WHEAT', number: 6, pip: 5 } },
    },
    ...over,
  };
}

test('pivot_details is a flat list of strings, not objects', () => {
  const strat = computeStrategy(mk({ vp: { '1': 3, '2': 8 } }));
  assert.ok(strat.pivot_triggers.includes('opp_close_to_win'));
  for (const d of strat.pivot_details) assert.equal(typeof d, 'string');
  assert.ok(strat.pivot_details.some(d => d.includes('8 VP')));
});

test('opp_close_to_win fires at round(target*0.8)=8, not at 7', () => {
  assert.ok(!computeStrategy(mk({ vp: { '1': 3, '2': 7 } }))
    .pivot_triggers.includes('opp_close_to_win'), '7 VP must not fire');
  assert.ok(computeStrategy(mk({ vp: { '1': 3, '2': 8 } }))
    .pivot_triggers.includes('opp_close_to_win'), '8 VP must fire');
});

test('opp_close_to_la fires on vp>=6 even when self leads on knights', () => {
  // Bridge has no "opp knights > my knights" gate, so self being ahead
  // on knights must NOT suppress the warning.
  const strat = computeStrategy(mk({
    vp: { '1': 5, '2': 6 },
    playedKnights: { '1': 3, '2': 2 },
    hasArmy: null,
  }));
  assert.ok(strat.pivot_triggers.includes('opp_close_to_la'));
});

test('hot_number fires on 4 same rolls with no length-5 floor', () => {
  const strat = computeStrategy(mk({
    rollHistory: [{ total: 6 }, { total: 6 }, { total: 6 }, { total: 6 }],
  }));
  assert.ok(strat.pivot_triggers.includes('hot_number'));
});

test('seven_overdue fires with a heavy hand and few rolls (no length floor)', () => {
  const strat = computeStrategy(mk({
    handTotal: { '1': 9, '2': 5 },
    rollHistory: [{ total: 4 }, { total: 5 }, { total: 9 }],
  }));
  assert.ok(strat.pivot_triggers.includes('seven_overdue'));
});
