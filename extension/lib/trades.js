// trades.js — bank/port trade unlocks + incoming-trade evaluator,
// JS port.
//
// Mirror of recommender.py's _plan_bank_trades and
// evaluate_incoming_trade. Both are pure heuristics over hand +
// bank/port state. evaluateIncomingTrade values the before/after hand
// with the in-game recommender; to keep the module graph acyclic
// (recommender.js imports planBankTrades from here) the recommender is
// INJECTED via opts.recommend rather than imported.

import { RESOURCE_NAMES } from './state.js';

// Resource icons, matching recommender.py _RES_TITLE so the standalone
// reason strings read exactly like the bridge's.
const _RES_TITLE = {
    WOOD: '🌲', BRICK: '🧱', SHEEP: '🐑', WHEAT: '🌾', ORE: '⛰️',
};

function _title(r) {
    return r ? r.charAt(0) + r.slice(1).toLowerCase() : r;
}

// Build-kind rank for the trade upgrade/downgrade test. Mirrors
// recommender.py _KIND_RANK; bank_trade / propose_trade resolve to the
// build they unlock before ranking.
const _KIND_RANK = { dev_card: 1, road: 2, settlement: 3, city: 4 };

const _KIND_LABEL = {
    settlement: 'settlement', city: 'city', road: 'road',
    dev_card: 'dev card', trade: 'trade',
    propose_trade: 'trade proposal', bank_trade: 'port trade',
    opening_settlement: 'settlement',
};

function _round(v, n = 1) {
    const f = Math.pow(10, n);
    return Math.round(v * f) / f;
}

function _signed(v) {
    return (v >= 0 ? '+' : '') + v.toFixed(1);
}

function _resolveKind(rec) {
    if (!rec) return null;
    if (rec.kind === 'bank_trade') return rec.target_kind || rec.unlocks || null;
    if (rec.kind === 'propose_trade') return rec.unlocks || null;
    return rec.kind || null;
}

function _kindRank(rec) {
    return _KIND_RANK[_resolveKind(rec)] || 0;
}

function _kindLabel(kind) {
    if (!kind) return 'build';
    return _KIND_LABEL[kind] || kind.replace(/_/g, ' ');
}

function _recLabel(rec) {
    // Mirrors the bridge's _kind_label(after_rank_kind or after_kind):
    // the resolved (unlocked) build kind, falling back to the raw kind.
    if (!rec) return 'build';
    return _kindLabel(_resolveKind(rec) || rec.kind);
}

/** Owned-port summary for sell-rate computation. Returns
 *  { specific: Set<resource> (2:1 ports owned), generic: bool (a 3:1
 *  port owned) }. Mirrors how recommender.py reads m.port_nodes against
 *  the player's settlement/city nodes. */
function _ownedPorts(state) {
    const out = { specific: new Set(), generic: false };
    const board = state.map;
    if (!board || !state.selfColor) return out;
    for (const [nid, b] of Object.entries(state.buildings || {})) {
        if (b.color !== state.selfColor) continue;
        const port = board.nodes[nid] && board.nodes[nid].port;
        if (!port) continue;
        if (port.kind === '3:1' || !port.resource) out.generic = true;
        else out.specific.add(port.resource);
    }
    return out;
}

/** Plan a sequence of bank/port trades that makes `cost` affordable
 *  from `hand`. Mirrors recommender.py _plan_bank_trades: greedy,
 *  cheapest sell-rate first, gated by bank supply when provided.
 *
 *  Returns { plan: [[srcRes, rate, getRes], ...], give: {res:cards},
 *  get: {res:count} }, an EMPTY plan when the build is already
 *  affordable (no trades needed), or null when it isn't reachable via
 *  trades this turn.
 *
 *  opts: { ports?: {specific,generic}, bankSupply?: {remaining}|{res:n} }.
 */
export function planBankTrades(state, hand, cost, opts = {}) {
    if (!state || !hand || !cost) return null;
    const ports = opts.ports || _ownedPorts(state);
    const bank = opts.bankSupply
        ? { ...(opts.bankSupply.remaining || opts.bankSupply) }
        : null;
    const sellRate = (res) => {
        if (ports.specific.has(res)) return 2;
        if (ports.generic) return 3;
        return 4;
    };
    // Reserve the cards the build itself consumes; record what's short.
    const available = {};
    for (const r of RESOURCE_NAMES) available[r] = hand[r] || 0;
    const needs = {};
    for (const [r, n] of Object.entries(cost)) {
        const have = available[r] || 0;
        if (have >= n) available[r] = have - n;
        else { needs[r] = n - have; available[r] = 0; }
    }
    if (Object.keys(needs).length === 0) {
        return { plan: [], give: {}, get: {} };
    }
    const plan = [];
    for (const [needRes, needCount] of Object.entries(needs)) {
        for (let i = 0; i < needCount; i++) {
            if (bank && (bank[needRes] || 0) <= 0) return null;
            let bestSrc = null;
            let bestRate = Infinity;
            for (const src of RESOURCE_NAMES) {
                if (src === needRes) continue;
                const surplus = available[src] || 0;
                if (surplus <= 0) continue;
                const rate = sellRate(src);
                if (surplus < rate) continue;
                if (rate < bestRate) { bestRate = rate; bestSrc = src; }
            }
            if (!bestSrc) return null;
            available[bestSrc] -= bestRate;
            plan.push([bestSrc, bestRate, needRes]);
            if (bank) bank[needRes] = Math.max(0, (bank[needRes] || 0) - 1);
        }
    }
    const give = {};
    const get = {};
    for (const [src, rate, tgt] of plan) {
        give[src] = (give[src] || 0) + rate;
        get[tgt] = (get[tgt] || 0) + 1;
    }
    return { plan, give, get };
}

/** Trim a resource pack down to `limit` total cards. Mirrors
 *  recommender.py _trim_pack exactly: peel ONE card from the current
 *  largest bucket at a time (max ties resolve to the first key in
 *  insertion order), dropping a bucket when it reaches zero, until the
 *  total is `limit`. This spreads the removal and keeps multiple
 *  resource types rather than concentrating on the tallest stacks. */
function _trimPack(pack, limit) {
    const out = {};
    for (const [r, n] of Object.entries(pack)) if (n > 0) out[r] = n;
    let total = Object.values(out).reduce((s, v) => s + v, 0);
    while (total > limit) {
        let topR = null;
        let topN = -Infinity;
        for (const [r, n] of Object.entries(out)) {
            if (n > topN) { topN = n; topR = r; }
        }
        if (topR == null) break;
        out[topR] -= 1;
        total -= 1;
        if (out[topR] <= 0) delete out[topR];
    }
    return out;
}

/** Evaluate an incoming player-to-player offer from self's seat. The
 *  offerer GIVES `give` and WANTS `want`; accepting sets
 *  hand += give - want. Mirrors recommender.py evaluate_incoming_trade.
 *
 *  opts: { recommend, oppVp, oppImminent, allowCounter, vpTarget }.
 *  `recommend` is recommendActions, injected to keep the module graph
 *  acyclic. Returns { verdict, score, reason, before, after, counter }
 *  with verdict in {accept, decline, consider}.
 */
export function evaluateIncomingTrade(state, hand, give, want, opts = {}) {
    if (!state || !hand) {
        return { verdict: 'consider', score: 0, reason: 'state not ready',
                 counter: null };
    }
    give = give || {};
    want = want || {};
    const oppVp = opts.oppVp || 0;
    const vpTarget = opts.vpTarget || state.vpTarget || 10;
    const closeVp = Math.max(2, Math.round(vpTarget * 0.80));
    const allowCounter = opts.allowCounter !== false;

    const wantKeys = Object.keys(want).filter(r => (want[r] || 0) > 0);
    // 1. No ask.
    if (wantKeys.length === 0) {
        return { verdict: 'consider', score: 0,
                 reason: 'open offer · no ask', counter: null };
    }
    // 2. Can't spare what they want.
    for (const r of wantKeys) {
        if ((hand[r] || 0) < want[r]) {
            return { verdict: 'decline', score: -10,
                     reason: `can't spare ${want[r]} `
                        + `${_RES_TITLE[r] || _title(r)}`,
                     counter: null };
        }
    }
    // 3. They give nothing.
    const giveKeys = Object.keys(give).filter(r => (give[r] || 0) > 0);
    if (giveKeys.length === 0) {
        return { verdict: 'decline', score: -10,
                 reason: 'they give nothing in return', counter: null };
    }
    // 4. Opp can win next turn.
    if (opts.oppImminent) {
        return { verdict: 'decline', score: -10,
                 reason: 'opp can win NEXT TURN · don\'t feed',
                 counter: null };
    }

    const recommend = opts.recommend;
    const bestNow = (h) => {
        if (typeof recommend !== 'function' || !state.selfColor) return null;
        const st = { ...state,
            hands: { ...state.hands, [state.selfColor]: h } };
        const recs = recommend(st, { topK: 4 }) || [];
        return recs.find(r => r.when === 'now') || null;
    };
    const newHand = { ...hand };
    for (const r of wantKeys) newHand[r] = (newHand[r] || 0) - want[r];
    for (const r of giveKeys) newHand[r] = (newHand[r] || 0) + give[r];

    const before = bestNow(hand);
    const after = bestNow(newHand);
    const sBefore = before ? (before.score || 0) : 0;
    const sAfter = after ? (after.score || 0) : 0;
    const delta = _round(sAfter - sBefore, 2);
    const kindUpgrade = _kindRank(after) > _kindRank(before);
    const kindDowngrade = _kindRank(after) < _kindRank(before);

    const wantTotal = wantKeys.reduce((s, r) => s + want[r], 0);
    const giveTotal = giveKeys.reduce((s, r) => s + give[r], 0);

    let verdict;
    let reason;
    if ((kindUpgrade || delta >= 1.0) && oppVp >= closeVp) {
        verdict = 'decline';
        reason = `opp at ${oppVp} VP · don't feed`;
    } else if (kindUpgrade || delta >= 1.0) {
        verdict = 'accept';
        reason = `unlocks ${_recLabel(after)} (${_signed(delta)})`;
    } else if (kindDowngrade || delta <= -1.0) {
        verdict = 'decline';
        reason = `blocks ${_recLabel(before)} (${delta.toFixed(1)})`;
    } else if (wantTotal > giveTotal) {
        verdict = 'decline';
        reason = `lopsided · give ${wantTotal}, get ${giveTotal}`;
    } else if (oppVp >= closeVp) {
        verdict = 'decline';
        reason = `opp at ${oppVp} VP · hold cards`;
    } else {
        verdict = 'consider';
        reason = 'neutral swap';
    }

    let counter = null;
    if (verdict !== 'accept' && allowCounter && oppVp < closeVp) {
        const trimmed = _trimPack(want, giveTotal);
        const tTotal = Object.values(trimmed).reduce((s, v) => s + v, 0);
        if (tTotal > 0 && tTotal < wantTotal) {
            const sub = evaluateIncomingTrade(state, hand, give, trimmed,
                { ...opts, allowCounter: false });
            if (sub.verdict === 'accept') {
                counter = { give: { ...give }, want: trimmed,
                            reason: `rebalance ${wantTotal}→${tTotal} for 1:1` };
            }
        }
    }

    // before/after as kind strings, mirroring the bridge contract
    // (before_kind / after_kind).
    return { verdict, score: delta, reason,
             before: before ? before.kind : null,
             after: after ? after.kind : null,
             counter };
}
