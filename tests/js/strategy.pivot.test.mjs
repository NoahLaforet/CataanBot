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

// --- post-placement rationale (live numbers, not a static blurb) ----
// strategy_select._rationale_for emits real per-roll production for OWS,
// e.g. "ore 0.39/r + wheat 0.39/r · city + dev engine".
function mkOWS(over = {}) {
  return {
    selfColor: '1', colors: ['1', '2'], vpTarget: 10,
    vp: { '1': 3, '2': 3 }, handTotal: { '1': 3, '2': 3 },
    discardLimit: 7, rollHistory: [], playedKnights: {}, devCardsByType: {},
    roadLength: {}, totalRolls: 12,
    buildings: {
      n1: { color: '1', kind: 'CITY' },
      n2: { color: '1', kind: 'SETTLEMENT' },
    },
    map: {
      nodes: {
        n1: { tiles: ['t1', 't2', 't3'], neighbors: [], port: null },
        n2: { tiles: ['t1', 't2', 't4'], neighbors: [], port: null },
      },
      tiles: {
        t1: { resource: 'ORE', number: 8, pip: 5 },
        t2: { resource: 'WHEAT', number: 6, pip: 5 },
        t3: { resource: 'ORE', number: 5, pip: 4 },
        t4: { resource: 'WHEAT', number: 9, pip: 4 },
      },
    },
    ...over,
  };
}

test('rationale carries live per-roll numbers, not a static blurb', () => {
  const strat = computeStrategy(mkOWS());
  assert.equal(strat.primary, 'OWS');
  // Matches the bridge format: "ore X.XX/r + wheat X.XX/r · city + dev engine".
  assert.match(strat.rationale,
    /^ore \d+\.\d{2}\/r \+ wheat \d+\.\d{2}\/r · city \+ dev engine$/);
});

test('BALANCED rationale reports the resource count', () => {
  // Single low-production city: no archetype clears its floor -> BALANCED.
  const strat = computeStrategy(mk({
    buildings: { n1: { color: '1', kind: 'CITY' } },
    map: {
      nodes: { n1: { tiles: ['t1', 't2'], neighbors: [], port: null } },
      tiles: {
        t1: { resource: 'ORE', number: 8, pip: 5 },
        t2: { resource: 'WHEAT', number: 6, pip: 5 },
      },
    },
  }));
  assert.equal(strat.primary, 'BALANCED');
  assert.match(strat.rationale, /^balanced base \(\d\/5 resources\) · /);
});

// --- stickiness / anti-flicker (1.15x guard) ------------------------
// strategy_select.select_strategy :604-619 keeps the prior primary unless
// the new top score beats the prior primary's current score by >= 1.15x.
test('stickiness keeps prior primary on a small score wobble', () => {
  const base = mkOWS();
  const top = computeStrategy(base);  // OWS, some score S
  const sOWS = top.scores.OWS;
  // Prior was PORT_TRADE scoring just under OWS (within 1.15x) -> hold it.
  const strat = computeStrategy({
    ...base,
    prevStrategy: { primary: 'PORT_TRADE', scores: { PORT_TRADE: sOWS * 0.95 } },
  });
  assert.equal(strat.primary, 'PORT_TRADE');
  assert.equal(strat.active, 'PORT_TRADE');
});

test('stickiness flips primary when the new top clears 1.15x', () => {
  const base = mkOWS();
  // Prior PORT_TRADE far below the new OWS top -> flip to OWS.
  const strat = computeStrategy({
    ...base,
    prevStrategy: { primary: 'PORT_TRADE', scores: { PORT_TRADE: 0.1 } },
  });
  assert.equal(strat.primary, 'OWS');
});

test('no prevStrategy means the stateless pick (no regression)', () => {
  const strat = computeStrategy(mkOWS());
  assert.equal(strat.primary, 'OWS');
  // set_at_rolls defaults to the current roll count when nothing carries.
  assert.equal(strat.set_at_rolls, 12);
});

test('set_at_rolls carries forward while primary is unchanged', () => {
  const base = mkOWS({ totalRolls: 20 });
  const strat = computeStrategy({
    ...base,
    prevStrategy: { primary: 'OWS', scores: { OWS: 0.9 }, set_at_rolls: 8 },
  });
  assert.equal(strat.primary, 'OWS');
  assert.equal(strat.set_at_rolls, 8);
});
