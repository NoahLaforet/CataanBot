// Node-based regression tests for trades.js — bank/port trade planning
// and incoming-trade evaluation, the JS mirror of recommender.py's
// _plan_bank_trades and evaluate_incoming_trade. Run with:
//   node --test tests/js/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { planBankTrades, evaluateIncomingTrade }
    from '../../extension/lib/trades.js';

// Minimal state stub — planBankTrades only reaches into state.map /
// state.buildings when opts.ports is absent, so a bare object is enough
// and ports are injected explicitly where they matter.
const baseState = () => ({ map: null, selfColor: '1', buildings: {} });

// --- planBankTrades --------------------------------------------------

test('planBankTrades returns an empty plan when already affordable', () => {
    const out = planBankTrades(baseState(), { WHEAT: 2, ORE: 3 },
        { WHEAT: 2, ORE: 3 });
    assert.deepEqual(out, { plan: [], give: {}, get: {} });
});

test('planBankTrades uses the 4:1 bank when no ports are owned', () => {
    const out = planBankTrades(baseState(), { WOOD: 4 }, { ORE: 1 });
    assert.deepEqual(out.plan, [['WOOD', 4, 'ORE']]);
    assert.deepEqual(out.give, { WOOD: 4 });
    assert.deepEqual(out.get, { ORE: 1 });
});

test('planBankTrades drops to 3:1 with a generic port', () => {
    const out = planBankTrades(baseState(), { WOOD: 3 }, { ORE: 1 },
        { ports: { specific: new Set(), generic: true } });
    assert.deepEqual(out.plan, [['WOOD', 3, 'ORE']]);
});

test('planBankTrades drops to 2:1 with a matching specific port', () => {
    const out = planBankTrades(baseState(), { WOOD: 2 }, { ORE: 1 },
        { ports: { specific: new Set(['WOOD']), generic: false } });
    assert.deepEqual(out.plan, [['WOOD', 2, 'ORE']]);
});

test('planBankTrades spends the cheapest-rate surplus first', () => {
    // SHEEP is a 2:1 port, WOOD is 4:1 — the trade should sell SHEEP.
    const out = planBankTrades(baseState(), { WOOD: 4, SHEEP: 2 },
        { ORE: 1 }, { ports: { specific: new Set(['SHEEP']), generic: false } });
    assert.deepEqual(out.plan, [['SHEEP', 2, 'ORE']]);
});

test('planBankTrades returns null when no surplus can cover a trade', () => {
    const out = planBankTrades(baseState(), { WOOD: 1 }, { ORE: 1 });
    assert.equal(out, null);
});

test('planBankTrades respects the bank-supply gate', () => {
    const out = planBankTrades(baseState(), { WOOD: 4 }, { ORE: 1 },
        { bankSupply: { remaining: { ORE: 0 } } });
    assert.equal(out, null, 'bank out of ORE means the trade is impossible');
});

test('planBankTrades plans one trade per missing card', () => {
    const out = planBankTrades(baseState(), { WOOD: 8 }, { ORE: 2 });
    assert.deepEqual(out.plan, [['WOOD', 4, 'ORE'], ['WOOD', 4, 'ORE']]);
    assert.deepEqual(out.give, { WOOD: 8 });
    assert.deepEqual(out.get, { ORE: 2 });
});

// --- evaluateIncomingTrade ------------------------------------------

const evalState = () => ({ selfColor: '1', vpTarget: 10, hands: { '1': {} } });

test('evaluateIncomingTrade: an empty ask is a consider', () => {
    const r = evaluateIncomingTrade(evalState(), { WHEAT: 2 },
        { ORE: 1 }, {});
    assert.equal(r.verdict, 'consider');
    assert.match(r.reason, /no ask/);
});

test('evaluateIncomingTrade: declines what you cannot spare', () => {
    const r = evaluateIncomingTrade(evalState(), { WHEAT: 0 },
        { ORE: 1 }, { WHEAT: 1 });
    assert.equal(r.verdict, 'decline');
    assert.match(r.reason, /can't spare 1 🌾/);
});

test('evaluateIncomingTrade: declines a one-sided gift-for-nothing', () => {
    const r = evaluateIncomingTrade(evalState(), { WHEAT: 2 }, {},
        { WHEAT: 1 });
    assert.equal(r.verdict, 'decline');
    assert.match(r.reason, /give nothing/);
});

test('evaluateIncomingTrade: refuses to feed an opp who can win next turn', () => {
    const r = evaluateIncomingTrade(evalState(), { WHEAT: 1 },
        { ORE: 1 }, { WHEAT: 1 }, { oppImminent: true });
    assert.equal(r.verdict, 'decline');
    assert.match(r.reason, /NEXT TURN/);
});

test('evaluateIncomingTrade: accepts a trade that unlocks a better build', () => {
    // After gaining 3 ORE the best-now rec jumps from 3 to 8.
    const recommend = (st) => [{ kind: 'city', when: 'now',
        score: (st.hands['1'].ORE || 0) >= 3 ? 8 : 3 }];
    const r = evaluateIncomingTrade(evalState(), { WOOD: 2, ORE: 0 },
        { ORE: 3 }, { WOOD: 1 }, { recommend });
    assert.equal(r.verdict, 'accept');
    assert.match(r.reason, /unlocks city \(\+5\.0\)/);
});

test('evaluateIncomingTrade: still declines an unlock when the opp is close to win', () => {
    const recommend = (st) => [{ kind: 'city', when: 'now',
        score: (st.hands['1'].ORE || 0) >= 3 ? 8 : 3 }];
    const r = evaluateIncomingTrade(evalState(), { WOOD: 2, ORE: 0 },
        { ORE: 3 }, { WOOD: 1 }, { recommend, oppVp: 8 });
    assert.equal(r.verdict, 'decline');
    assert.match(r.reason, /opp at 8 VP/);
});

test('evaluateIncomingTrade: declines a swap that blocks your build', () => {
    const recommend = (st) => [{ kind: 'city', when: 'now',
        score: (st.hands['1'].WHEAT || 0) * 4 }];
    const r = evaluateIncomingTrade(evalState(), { WHEAT: 2, ORE: 0 },
        { ORE: 1 }, { WHEAT: 2 }, { recommend });
    assert.equal(r.verdict, 'decline');
    assert.match(r.reason, /blocks city/);
});

test('evaluateIncomingTrade: declines a lopsided give-more-than-you-get', () => {
    const recommend = () => [{ kind: 'road', when: 'now', score: 5 }];
    const r = evaluateIncomingTrade(evalState(), { WHEAT: 1, SHEEP: 1 },
        { ORE: 1 }, { WHEAT: 1, SHEEP: 1 }, { recommend });
    assert.equal(r.verdict, 'decline');
    assert.match(r.reason, /lopsided · give 2, get 1/);
});

test('evaluateIncomingTrade: a fair neutral swap is a consider', () => {
    const recommend = () => [{ kind: 'road', when: 'now', score: 5 }];
    const r = evaluateIncomingTrade(evalState(), { WHEAT: 1 },
        { ORE: 1 }, { WHEAT: 1 }, { recommend });
    assert.equal(r.verdict, 'consider');
    assert.equal(r.reason, 'neutral swap');
});

test('evaluateIncomingTrade: suggests a rebalanced counter on a lopsided ask', () => {
    // Best-now score = wheat held. The full 3-card ask nets 0 and is
    // lopsided; trimming it to 2 cards (peel one wheat, _trim_pack style)
    // makes the swap a +1 accept, so a rebalanced counter is surfaced.
    const recommend = (st) => [{ kind: 'city', when: 'now',
        score: (st.hands['1'].WHEAT || 0) }];
    const r = evaluateIncomingTrade(evalState(),
        { WHEAT: 2, SHEEP: 1 }, { WHEAT: 2 },
        { WHEAT: 2, SHEEP: 1 }, { recommend });
    assert.equal(r.verdict, 'decline');
    assert.match(r.reason, /lopsided/);
    assert.ok(r.counter, 'a counter should be suggested');
    assert.deepEqual(r.counter.want, { WHEAT: 1, SHEEP: 1 });
    assert.match(r.counter.reason, /rebalance 3→2 for 1:1/);
});
