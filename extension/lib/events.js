// events.js — apply colonist WS snapshots to the JS game state.
//
// Colonist ships authoritative game state with every WS broadcast
// (mapState + playerStates + mechanic*State). Rather than porting
// the full diff parser from src/catanbot/colonist_diff.py
// line-for-line, we read the snapshot directly and update state
// in-place. This is ~80% of the bridge's value at ~10% of the code.
//
// Color identity: we key everything by colonist color id (an int
// 1..6), not by catanatron's color strings. The standalone path
// stays self-contained; the panel maps id → display label at the
// last moment. A "self color" string ("self") and a numeric
// selfColorId both live on state for convenience.
//
// Reverse-engineering reference for snapshot field names lives in
// src/catanbot/colonist_diff.py — that file's _CARD_RESOURCE,
// _DEV_CARD_TYPE, and _VP_WEIGHTS tables are mirrored below.

import { newHand, newDevCardCounts, edgeKey, RESOURCE_NAMES } from './state.js';

// Resource type ints inside playerStates.{cid}.resourceCards.cards.
const _CARD_RESOURCE = {
    1: 'WOOD', 2: 'BRICK', 3: 'SHEEP', 4: 'WHEAT', 5: 'ORE',
};

// Dev card type ints inside mechanicDevelopmentCardsState.players.{cid}.
//   10 = opp placeholder (hidden type)
//   11 = KNIGHT, 12 = VICTORY_POINT, 13 = MONOPOLY,
//   14 = ROAD_BUILDING, 15 = YEAR_OF_PLENTY
const _DEV_CARD_TYPE = {
    11: 'KNIGHT',
    12: 'VICTORY_POINT',
    13: 'MONOPOLY',
    14: 'ROAD_BUILDING',
    15: 'YEAR_OF_PLENTY',
};

// VP weights (matches _VP_WEIGHTS in colonist_diff.py).
//   0 = settlements (1 VP each)
//   1 = cities (2 VPs each)
//   2 = VP dev cards (self only)
//   4 = has-longest-road flag (2 VPs)
//   5 = has-largest-army flag (2 VPs)
const _VP_WEIGHTS = { 0: 1, 1: 2, 2: 1, 4: 2, 5: 2 };

const _BUILDING_BY_TYPE = { 1: 'SETTLEMENT', 2: 'CITY' };

// Top-level snapshot keys applySnapshot pulls out of a decoded frame.
// They're harvested in ONE depth-bounded tree walk per frame
// (_collectKeys) instead of one independent DFS per key (the old
// _findKey ran ~17 walks per frame).
const _WANTED_KEYS = new Set([
    'playerColor', 'playerStates', 'gameSettings', 'currentState',
    'mechanicRobberState', 'tileCornerStates', 'tileEdgeStates',
    'mechanicLongestRoadState', 'mechanicLargestArmyState',
    'mechanicSettlementState', 'mechanicCityState', 'mechanicRoadState',
    'tradeState', 'diceState', 'winnerPlayerColor', 'tileHexStates',
]);

/** Walk a decoded msgpack tree ONCE and harvest the first non-null
 *  value for each wanted key. Pre-order (a dict's own keys are checked
 *  before descending into its values), depth-bounded at 8, recurses
 *  into both objects and arrays, and skips ArrayBuffer views. Resolves
 *  to the same value as the old per-key _findKey DFS on every real
 *  colonist frame (each key lives at one canonical spot). It differs
 *  only on a shape colonist never emits: a wanted key present as a null
 *  placeholder shadowing a deeper non-null copy, where this version
 *  recovers the deeper value instead of stopping at the null. Returns a
 *  Map<key, value>; absent keys are simply missing. */
function _collectKeys(root, wanted) {
    const out = new Map();
    let remaining = wanted.size;
    const visit = (o, depth) => {
        if (remaining === 0 || depth > 8) return;
        if (o && typeof o === 'object' && !Array.isArray(o)
                && !ArrayBuffer.isView(o)) {
            for (const k of Object.keys(o)) {
                if (wanted.has(k) && !out.has(k) && o[k] != null) {
                    out.set(k, o[k]);
                    if (--remaining === 0) return;
                }
            }
            for (const v of Object.values(o)) {
                visit(v, depth + 1);
                if (remaining === 0) return;
            }
        } else if (Array.isArray(o)) {
            for (const v of o) {
                visit(v, depth + 1);
                if (remaining === 0) return;
            }
        }
    };
    visit(root, 0);
    return out;
}

/** Resolve a colonist tile (x, y, z) to a node id in the JS board.
 *  Mirrors the cornerSig math in board.js — duplicated to avoid an
 *  export cycle. Returns the canonical node id (a 3-tile signature
 *  string) or null if the corner sits outside the parsed board. */
function _cornerNodeId(board, cx, cy, cz) {
    let trio;
    if (cz === 0) {
        trio = [[cx, cy], [cx, cy - 1], [cx + 1, cy - 1]];
    } else {
        trio = [[cx, cy], [cx, cy + 1], [cx - 1, cy + 1]];
    }
    const sig = trio.map(([x, y]) => `${x},${y}`).sort().join('|');
    if (board.nodes[sig]) return sig;
    // Boundary corner: 2-tile sub-signature.
    const want = new Set(sig.split('|'));
    for (const candidate of Object.keys(board.nodes)) {
        const have = new Set(candidate.split('|'));
        if (have.size < 2) continue;
        let ok = true;
        for (const t of have) if (!want.has(t)) { ok = false; break; }
        if (ok) return candidate;
    }
    return null;
}

/** Resolve a colonist edge (x, y, z) to an edge id in the JS board. */
function _edgeId(board, ex, ey, ez) {
    let endpoints;
    if (ez === 0) {
        endpoints = [
            _cornerNodeId(board, ex, ey, 0),
            _cornerNodeId(board, ex, ey - 1, 1),
        ];
    } else if (ez === 1) {
        endpoints = [
            _cornerNodeId(board, ex, ey - 1, 1),
            _cornerNodeId(board, ex - 1, ey + 1, 0),
        ];
    } else {
        endpoints = [
            _cornerNodeId(board, ex - 1, ey + 1, 0),
            _cornerNodeId(board, ex, ey, 1),
        ];
    }
    const [a, b] = endpoints;
    if (!a || !b) return null;
    return a < b ? `${a}||${b}` : `${b}||${a}`;
}

/** Resolve a colonist tile id (the key in tileHexStates) to a JS
 *  board tile id. They're identical strings in practice; this stays
 *  a function so a future variant could swap in a remapping. */
function _tileId(board, tid) {
    return tid != null && board.tiles[String(tid)]
        ? String(tid) : null;
}

/** Ensure per-color buckets exist on state. Keyed by colonist
 *  color id (string-ified — Object keys are strings anyway, and
 *  this keeps Set/Map use consistent). */
function _ensureColor(state, cid) {
    const key = String(cid);
    if (!state.hands[key]) state.hands[key] = newHand();
    if (!state.devCardsByType[key]) {
        state.devCardsByType[key] = newDevCardCounts();
    }
    if (state.devCardsTotal[key] == null) state.devCardsTotal[key] = 0;
    if (state.playedKnights[key] == null) state.playedKnights[key] = 0;
    if (state.roadLength[key] == null) state.roadLength[key] = 0;
    if (state.vp[key] == null) state.vp[key] = 0;
    if (state.vpCardsInHand[key] == null) state.vpCardsInHand[key] = 0;
    if (!state.colors.includes(key)) state.colors.push(key);
    return key;
}

/** Apply a single decoded msgpack frame to state in-place.
 *  Returns true when the snapshot meaningfully changed state.
 *
 *  Idempotent: applying the same frame twice has no effect after
 *  the first. State is keyed by colonist color id strings ('1'..'6')
 *  rather than catanatron color names.
 */
export function applySnapshot(state, decoded) {
    if (!state || !decoded) return false;
    let dirty = false;

    // One pass harvests every top-level key this function reads; K(k)
    // returns the first non-null value found, or null (drop-in for the
    // old K(k)).
    const _keys = _collectKeys(decoded, _WANTED_KEYS);
    const K = (k) => {
        const v = _keys.get(k);
        return v == null ? null : v;
    };

    // --- Self color id (latched once on GameStart, or via the
    //     resourceCards-typed sniff if we joined mid-game and
    //     missed the GameStart frame). The bridge mirrors this:
    //     colonist ships real type ints in resourceCards.cards
    //     for the viewer's own slot only; opps see zero-fills,
    //     so any slot with a non-zero card int is necessarily
    //     the self-player (see colonist_diff.py:818-822).
    const pc = K('playerColor');
    if (typeof pc === 'number' && state.selfColorId == null) {
        state.selfColorId = pc;
        state.selfColor = String(pc);
        _ensureColor(state, pc);
        dirty = true;
    } else if (state.selfColorId == null) {
        const ps = K('playerStates');
        if (ps && typeof ps === 'object') {
            for (const [cidStr, pstate] of Object.entries(ps)) {
                if (!pstate || typeof pstate !== 'object') continue;
                const rc = pstate.resourceCards;
                if (!rc || !Array.isArray(rc.cards)) continue;
                for (const ci of rc.cards) {
                    if (Number(ci) > 0) {
                        const cid = Number(cidStr);
                        if (cid) {
                            state.selfColorId = cid;
                            state.selfColor = String(cid);
                            _ensureColor(state, cid);
                            dirty = true;
                        }
                        break;
                    }
                }
                if (state.selfColorId != null) break;
            }
        }
    }

    // --- Game settings (VP target, discard limit). ----------------
    const gs = K('gameSettings');
    if (gs && typeof gs === 'object') {
        const vpw = Number(gs.victoryPointsToWin);
        if (vpw && vpw !== state.vpTarget) {
            state.vpTarget = vpw; dirty = true;
        }
        const dl = Number(gs.cardDiscardLimit);
        if (dl && dl !== state.discardLimit) {
            state.discardLimit = dl; dirty = true;
        }
    }

    // --- Whose turn is it? ----------------------------------------
    const cs = K('currentState');
    if (cs && typeof cs === 'object'
            && cs.currentTurnPlayerColor != null) {
        const ctp = Number(cs.currentTurnPlayerColor);
        if (ctp !== state.currentTurn) {
            // A turn change ends the self-knight robber window (mirrors
            // the chat path, which also clears on turn change).
            if (state.knightRobberTurn != null
                    && ctp !== state.knightRobberTurn) {
                state.knightRobberPending = false;
                state.knightRobberTurn = null;
            }
            state.currentTurn = ctp;
            dirty = true;
        }
    }
    // Phase from currentState — colonist uses ints; opening = 0..2,
    // mid-game = 3+. We don't hard-decode; just stash the raw value.
    if (cs && typeof cs === 'object' && cs.gameState != null) {
        const ph = Number(cs.gameState);
        if (ph !== state.phaseRaw) { state.phaseRaw = ph; dirty = true; }
    }

    // --- GameStart marker. ----------------------------------------
    if (state.map == null) {
        // map is built externally (board.js) and assigned on the
        // panel side. Don't try to rebuild here — applySnapshot is
        // about state, not topology.
    }

    // --- Robber tile. ---------------------------------------------
    const robber = K('mechanicRobberState');
    if (robber && typeof robber === 'object'
            && robber.locationTileIndex != null) {
        const tid = String(Number(robber.locationTileIndex));
        if (tid !== state.robberTile) {
            // Robber just moved — clear the "must place" flag and
            // anchor the review window. Mirrors RobberMoveEvent path
            // in bridge.py:950-961.
            state.robberTile = tid;
            state.robberPending = false;
            state.robberMovedAtRolls = state.totalRolls || 0;
            dirty = true;
        }
    }

    // --- Buildings (settlements + cities) from tileCornerStates. --
    // The full mapState lives on GameStart; later diffs ship just
    // the touched corners. Both are handled by the same loop —
    // mutated corners get owner/buildingType, untouched stay.
    if (state.map) {
        const corners = K('tileCornerStates');
        if (corners && typeof corners === 'object') {
            for (const [cid, c] of Object.entries(corners)) {
                if (!c || typeof c !== 'object') continue;
                const bt = Number(c.buildingType) || 0;
                const owner = c.owner == null ? 0 : Number(c.owner);
                // Mid-game delta frames omit x/y/z and ship only
                // {owner, buildingType}. Resolve via the GameStart-
                // built cornerIdToNodeId map. Full GameStart frames
                // ship coords; we use them when present so a re-
                // sync frame from a different layout still
                // resolves correctly.
                let nodeId = null;
                if (c.x != null && c.y != null && c.z != null) {
                    nodeId = _cornerNodeId(state.map,
                        Number(c.x), Number(c.y), Number(c.z));
                }
                if (!nodeId && state.map.cornerIdToNodeId) {
                    nodeId = state.map.cornerIdToNodeId[cid] || null;
                }
                if (!nodeId) continue;
                const prev = state.buildings[nodeId];
                if (bt === 0) {
                    if (prev) {
                        delete state.buildings[nodeId];
                        dirty = true;
                    }
                    continue;
                }
                if (!owner) {
                    // Diff with no owner = upgrade-in-place; reuse
                    // prior color.
                    if (!prev) continue;
                    const kind = _BUILDING_BY_TYPE[bt] || prev.kind;
                    if (kind !== prev.kind) {
                        state.buildings[nodeId] = { ...prev, kind };
                        dirty = true;
                    }
                    continue;
                }
                _ensureColor(state, owner);
                const kind = _BUILDING_BY_TYPE[bt] || 'SETTLEMENT';
                if (!prev || prev.color !== String(owner)
                        || prev.kind !== kind) {
                    state.buildings[nodeId] = {
                        color: String(owner), kind, nodeId,
                    };
                    dirty = true;
                }
            }
        }

        // --- Roads from tileEdgeStates. ---------------------------
        const edges = K('tileEdgeStates');
        if (edges && typeof edges === 'object') {
            for (const [eidColonist, e] of Object.entries(edges)) {
                if (!e || typeof e !== 'object') continue;
                const owner = e.owner == null ? 0 : Number(e.owner);
                let eid = null;
                if (e.x != null && e.y != null && e.z != null) {
                    eid = _edgeId(state.map, Number(e.x),
                        Number(e.y), Number(e.z));
                }
                if (!eid && state.map.edgeIdToEdgeId) {
                    eid = state.map.edgeIdToEdgeId[eidColonist] || null;
                }
                if (!eid) continue;
                const prev = state.roads[eid];
                if (!owner) {
                    if (prev) { delete state.roads[eid]; dirty = true; }
                    continue;
                }
                _ensureColor(state, owner);
                if (prev !== String(owner)) {
                    state.roads[eid] = String(owner);
                    dirty = true;
                }
            }
        }
    }

    // --- Per-player resource hands + dev cards + VP. --------------
    const playerStates = K('playerStates');
    if (playerStates && typeof playerStates === 'object') {
        for (const [cidStr, pstate] of Object.entries(playerStates)) {
            if (!pstate || typeof pstate !== 'object') continue;
            const cid = Number(cidStr);
            if (!cid) continue;
            const key = _ensureColor(state, cid);

            // Hands. self gets typed counts, opps get total only.
            const rc = pstate.resourceCards;
            if (rc && typeof rc === 'object') {
                const cards = rc.cards;
                if (Array.isArray(cards)) {
                    let total = 0;
                    let typedTotal = 0;
                    const tmp = newHand();
                    for (const ci of cards) {
                        const ciNum = Number(ci);
                        const res = _CARD_RESOURCE[ciNum];
                        if (res) {
                            tmp[res] = (tmp[res] || 0) + 1;
                            typedTotal += 1;
                        }
                        total += 1;
                    }
                    if (typedTotal > 0) {
                        // self snapshot: real per-resource breakdown
                        const cur = state.hands[key];
                        for (const r of RESOURCE_NAMES) {
                            if (cur[r] !== tmp[r]) {
                                cur[r] = tmp[r];
                                dirty = true;
                            }
                        }
                        cur.unknown = 0;
                        if (state.handTotal[key] !== total) {
                            state.handTotal[key] = total;
                            dirty = true;
                        }
                    } else {
                        // opp snapshot: total only.
                        if (state.handTotal[key] !== total) {
                            state.handTotal[key] = total;
                            dirty = true;
                        }
                    }
                }
            }

            // Dev cards. Self ships typed ints; opps ship 10s.
            const dev = pstate.developmentCards;
            if (dev && typeof dev === 'object'
                    && Array.isArray(dev.cards)) {
                const counts = newDevCardCounts();
                let total = 0;
                for (const di of dev.cards) {
                    const t = _DEV_CARD_TYPE[Number(di)];
                    if (t) counts[t] = (counts[t] || 0) + 1;
                    total += 1;
                }
                const prev = state.devCardsByType[key];
                let changed = false;
                for (const t of Object.keys(counts)) {
                    if (prev[t] !== counts[t]) {
                        prev[t] = counts[t];
                        changed = true;
                    }
                }
                if (state.devCardsTotal[key] !== total) {
                    state.devCardsTotal[key] = total;
                    changed = true;
                }
                // Self VP cards in hand for the snap.
                if (cid === state.selfColorId) {
                    const vps = counts.VICTORY_POINT || 0;
                    if (state.vpCardsInHand[key] !== vps) {
                        state.vpCardsInHand[key] = vps;
                        changed = true;
                    }
                }
                if (changed) dirty = true;
            }

            // VP breakdown.
            const vps = pstate.victoryPointsState;
            if (vps && typeof vps === 'object') {
                let total = 0;
                for (const [k, v] of Object.entries(vps)) {
                    const w = _VP_WEIGHTS[Number(k)] || 0;
                    total += w * (Number(v) || 0);
                }
                if (state.vp[key] !== total) {
                    state.vp[key] = total;
                    dirty = true;
                }
            }

            // Played-knight count from mechanicKnightState (when
            // colonist ships it inside playerStates) — used by hint
            // logic. Falls back to dev-card-used count below.
            if (pstate.mechanicKnightState
                    && typeof pstate.mechanicKnightState === 'object'
                    && pstate.mechanicKnightState.knightsPlayed != null) {
                const k = Number(pstate.mechanicKnightState.knightsPlayed);
                const prevK = state.playedKnights[key];
                if (prevK !== k) {
                    // Self just played a knight this frame (exactly +1
                    // while it's our turn) means we owe a robber move.
                    // Set a WS-driven flag so the robber-target list
                    // surfaces even when the chat-log "X used a Knight"
                    // line is missed (the chat path stays the fast
                    // signal; this is the reliability backstop). Require
                    // exactly +1 so a mid-game join (0 -> N in one sync
                    // frame) does not false-fire. Cleared on turn change.
                    if (cid === state.selfColorId
                            && k === prevK + 1
                            && state.currentTurn === state.selfColorId) {
                        state.knightRobberPending = true;
                        state.knightRobberTurn = state.currentTurn;
                    }
                    state.playedKnights[key] = k;
                    dirty = true;
                }
            }
        }
    }

    // --- Longest Road / Largest Army holders. ---------------------
    const lr = K('mechanicLongestRoadState');
    if (lr && typeof lr === 'object') {
        let holder = null;
        for (const [cidStr, st] of Object.entries(lr)) {
            if (st && typeof st === 'object' && st.hasLongestRoad) {
                holder = String(Number(cidStr));
            }
            if (st && typeof st === 'object'
                    && st.longestRoadCount != null) {
                const key = _ensureColor(state, Number(cidStr));
                const n = Number(st.longestRoadCount) || 0;
                if (state.roadLength[key] !== n) {
                    state.roadLength[key] = n;
                    dirty = true;
                }
            }
        }
        if (state.hasRoad !== holder) { state.hasRoad = holder; dirty = true; }
    }
    const la = K('mechanicLargestArmyState');
    if (la && typeof la === 'object') {
        let holder = null;
        for (const [cidStr, st] of Object.entries(la)) {
            if (st && typeof st === 'object' && st.hasLargestArmy) {
                holder = String(Number(cidStr));
            }
        }
        if (state.hasArmy !== holder) { state.hasArmy = holder; dirty = true; }
    }

    // --- Bank counts (settlements / cities / roads remaining). ----
    const ms2 = K('mechanicSettlementState');
    if (ms2 && typeof ms2 === 'object') {
        for (const [cidStr, st] of Object.entries(ms2)) {
            if (!st || typeof st !== 'object') continue;
            const key = _ensureColor(state, Number(cidStr));
            if (st.bankSettlementAmount != null) {
                const n = Number(st.bankSettlementAmount);
                if (state.bank[key]?.settles !== n) {
                    state.bank[key] = state.bank[key] || {};
                    state.bank[key].settles = n;
                    dirty = true;
                }
            }
        }
    }
    const mc2 = K('mechanicCityState');
    if (mc2 && typeof mc2 === 'object') {
        for (const [cidStr, st] of Object.entries(mc2)) {
            if (!st || typeof st !== 'object') continue;
            const key = _ensureColor(state, Number(cidStr));
            if (st.bankCityAmount != null) {
                const n = Number(st.bankCityAmount);
                if (state.bank[key]?.cities !== n) {
                    state.bank[key] = state.bank[key] || {};
                    state.bank[key].cities = n;
                    dirty = true;
                }
            }
        }
    }
    const mr2 = K('mechanicRoadState');
    if (mr2 && typeof mr2 === 'object') {
        for (const [cidStr, st] of Object.entries(mr2)) {
            if (!st || typeof st !== 'object') continue;
            const key = _ensureColor(state, Number(cidStr));
            if (st.bankRoadAmount != null) {
                const n = Number(st.bankRoadAmount);
                if (state.bank[key]?.roads !== n) {
                    state.bank[key] = state.bank[key] || {};
                    state.bank[key].roads = n;
                    dirty = true;
                }
            }
        }
    }

    // --- Active trade offers (tradeState). -----------------------
    // Mirrors python's _trade_offer_events in colonist_diff.py.
    // activeOffers: {offer_id: payload | null} — null means the
    //   offer was withdrawn / declined / expired; drop it.
    // closedOffers: {offer_id: ...} — offer resolved (committed,
    //   countered, or timed out); drop it.
    // We only stash offers from non-self creators with both
    // offeredResources + wantedResources populated; partial-update
    // frames (just playerResponses) reuse the cached payload.
    if (state.tradeOffers == null) state.tradeOffers = {};
    const tradeState = K('tradeState');
    if (tradeState && typeof tradeState === 'object') {
        const active = tradeState.activeOffers;
        if (active && typeof active === 'object') {
            for (const [offerId, payload] of Object.entries(active)) {
                if (payload === null) {
                    if (state.tradeOffers[offerId]) {
                        delete state.tradeOffers[offerId];
                        dirty = true;
                    }
                    continue;
                }
                if (!payload || typeof payload !== 'object') continue;
                const offered = payload.offeredResources;
                const wanted = payload.wantedResources;
                const creator = payload.creator;
                if (offered == null || wanted == null
                        || creator == null) {
                    // Partial-update frame; ignore unless we have it cached.
                    continue;
                }
                if (state.selfColorId != null
                        && Number(creator) === Number(state.selfColorId)) {
                    // Self-created offer — banner not for the sender.
                    continue;
                }
                const give = {};
                for (const ci of (Array.isArray(offered) ? offered : [])) {
                    const r = _CARD_RESOURCE[Number(ci)];
                    if (r) give[r] = (give[r] || 0) + 1;
                }
                const want = {};
                for (const ci of (Array.isArray(wanted) ? wanted : [])) {
                    const r = _CARD_RESOURCE[Number(ci)];
                    if (r) want[r] = (want[r] || 0) + 1;
                }
                if (!Object.keys(give).length
                        && !Object.keys(want).length) continue;
                const prev = state.tradeOffers[offerId];
                if (!prev || prev.creator !== String(creator)) {
                    state.tradeOffers[offerId] = {
                        creator: String(creator),
                        give, want, ts: Date.now(),
                    };
                    dirty = true;
                }
            }
        }
        const closed = tradeState.closedOffers;
        if (closed && typeof closed === 'object') {
            for (const offerId of Object.keys(closed)) {
                if (state.tradeOffers[offerId]) {
                    delete state.tradeOffers[offerId];
                    dirty = true;
                }
            }
        }
    }

    // --- Roll detection from diceState. ---------------------------
    // Colonist's setup-phase ships a play-order-determination dice
    // BEFORE anyone places settlements (it's part of the GameStart
    // payload). That dice is NOT a game roll — counting it
    // incorrectly bumps totalRolls to 1 right at GameStart, which
    // breaks the opening-phase trigger (panel skipped opening
    // picks because "first roll already happened"). Real game rolls
    // only fire AFTER both opening rounds are complete, by which
    // point at least one player has a settlement on the board.
    // Gate on Object.keys(state.buildings).length > 0 so the
    // pre-placement order roll is silently dropped.
    const dice = K('diceState');
    const anyBuildings = Object.keys(state.buildings || {}).length > 0;
    if (dice && typeof dice === 'object'
            && dice.dice1 != null && dice.dice2 != null
            && anyBuildings) {
        const d1 = Number(dice.dice1);
        const d2 = Number(dice.dice2);
        const total = d1 + d2;
        const roller = state.currentTurn;
        const fp = `${roller}|${d1}|${d2}`;
        if (state._lastRollFp !== fp && total >= 2 && total <= 12) {
            state._lastRollFp = fp;
            state.rollHistory.push({
                total, d1, d2,
                isYou: roller === state.selfColorId,
                rollerColor: roller != null ? String(roller) : null,
                ts: Date.now(),
            });
            // Cap history at 200 to keep memory flat across long games.
            if (state.rollHistory.length > 200) {
                state.rollHistory.shift();
            }
            state.totalRolls += 1;
            state.rollHistogram[total] = (state.rollHistogram[total] || 0) + 1;
            // Per-color card-total history sample (mirrors bridge.py:883-888).
            // 5-deep ring buffer — captures swings even across a few
            // rolls of churn. Done on every roll including 7s; a robber
            // steal that drops a victim's count is itself signal.
            if (state.oppCardHist == null) state.oppCardHist = {};
            for (const c of state.colors) {
                const series = state.oppCardHist[c] || [];
                series.push(state.handTotal[c] || 0);
                if (series.length > 5) series.shift();
                state.oppCardHist[c] = series;
            }
            // Robber-pending lifecycle (mirrors bridge.py:917-949):
            // self rolls 7 → pending=true; opp rolls 7 → reset; any
            // other roll → reset only when no review window owed.
            if (total === 7) {
                if (roller === state.selfColorId) {
                    state.robberPending = true;
                } else {
                    state.robberPending = false;
                }
            }
            dirty = true;
        }
    }

    // --- Game-over detection. -------------------------------------
    const winner = K('winnerPlayerColor');
    if (typeof winner === 'number' && winner > 0) {
        if (!state.gameOver || state.gameOver.winnerColor !== String(winner)) {
            state.gameOver = { winnerColor: String(winner) };
            dirty = true;
        }
    }

    // GameStart latch — once we've seen mapState we're started.
    if (!state.started && K('tileHexStates')) {
        state.started = true;
        dirty = true;
    }

    return dirty;
}

/** Apply a stream of decoded frames in order. */
export function applyAll(state, decodedFrames) {
    let changed = false;
    for (const d of decodedFrames || []) {
        if (applySnapshot(state, d)) changed = true;
    }
    return changed;
}

/** Legacy entry point — kept so existing imports of `applyEvent`
 *  don't break. Treats `event` as a decoded msgpack frame and calls
 *  applySnapshot. */
export function applyEvent(state, event) {
    return applySnapshot(state, event);
}
