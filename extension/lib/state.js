// state.js — Game state container for the standalone JS recommender.
//
// Mirrors the subset of catanatron's Game state that our heuristics
// actually read. Built up from colonist's WS frames as they arrive
// (see events.js). Designed to be cheap to mutate and cheap to clone
// for hypothetical scoring passes.
//
// Shape decisions:
// * Colors are catanatron strings ('RED', 'BLUE', 'WHITE', 'ORANGE').
//   colonist's int color ids map onto these via a board-level table.
// * Nodes / edges / tiles are integer ids matching catanatron's
//   numbering for the standard board. Variant maps that don't map
//   cleanly fall back to bridge mode (graceful upgrade).
// * Hands track per-player resource counts; unknown bucket holds
//   3rd-party-steal residue same as the Python tracker.

const RESOURCES = ['WOOD', 'BRICK', 'SHEEP', 'WHEAT', 'ORE'];

export function newGameState() {
    return {
        // map: built externally (board.js) and assigned on the
        // panel side once the GameStart mapState arrives.
        map: null,
        // colors: list of color keys (colonist color id strings)
        // we've seen seated. Order is insertion order (first frame
        // wins).
        colors: [],
        // self color: legacy string field (set to selfColorId.toString
        // by events.js), kept for code that branches on `selfColor`.
        selfColor: null,
        // self color id (1..6) per colonist's playerColor field.
        selfColorId: null,
        // username ↔ color (left empty in standalone — colonist's WS
        // frames don't carry usernames; the panel reads them from
        // the lobby DOM if needed).
        usernameToColor: {},
        colorToUsername: {},
        // {nodeId: {color, kind: 'SETTLEMENT'|'CITY'}}
        buildings: {},
        // {edgeKey: color}
        roads: {},
        // current robber tile id (string), or null pre-placement.
        robberTile: null,
        // current turn — colonist color id of the player whose turn
        // it is right now, or null pre-game-start.
        currentTurn: null,
        // raw colonist gameState int (currentState.gameState). Phase
        // boundaries can be read off this; we don't hard-decode.
        phaseRaw: null,
        // per-color counts.
        playedKnights: {},
        roadLength: {},   // longest single chain length per color
        hasArmy: null,    // color holding LA, or null
        hasRoad: null,    // color holding LR, or null
        // public VP per color (settles + cities + LR/LA flags +
        // self-only VP cards).
        vp: {},
        vpCardsInHand: {},  // self only typically
        // per-color hand. Each: {WOOD,BRICK,SHEEP,WHEAT,ORE: int,
        //                       unknown: int, drift: int}.
        // For opponents the typed counts stay 0; the total lives in
        // handTotal[color].
        hands: {},
        // {color: int} — total cards in hand. For self this equals
        // sum(hands[self][r]); for opps this is the only ground
        // truth.
        handTotal: {},
        // {color: {KNIGHT,MONOPOLY,YEAR_OF_PLENTY,ROAD_BUILDING,
        //          VICTORY_POINT: int}} — typed dev cards in hand
        // (self only; opps stay all zero).
        devCardsByType: {},
        // total dev cards in hand per color (aggregate; opps too).
        devCardsTotal: {},
        // {color: {settles:int, cities:int, roads:int}} — bank
        // remaining counts per player from mechanic*State.
        bank: {},
        // ring buffer of recent rolls; each {total, d1, d2,
        // isYou, rollerColor, ts}.
        rollHistory: [],
        // monotonic roll counter; used by phase boundary heuristics.
        totalRolls: 0,
        // {2..12: count} of every dice total seen.
        rollHistogram: Object.fromEntries(
            Array.from({length: 11}, (_, i) => [i + 2, 0])),
        // game configuration. Pulled from gameSettings; defaults to
        // standard Catan.
        vpTarget: 10,
        discardLimit: 7,
        // True after GameStart; flips false on game over until next
        // start.
        started: false,
        // True between game-over and next GameStart. Drives the
        // "waiting for next game" HUD frame.
        gameOver: null,  // { winnerColor, winnerUsername }
        // Per-color hand-total history — last 5 samples taken on
        // every roll (mirrors python's opp_card_hist in
        // bridge.py:883-888). Powers the "+3 in 5" annotation on
        // opp rows.
        oppCardHist: {},
        // Robber lifecycle — mirrors python's robber_pending /
        // robber_moved_at_rolls in src/catanbot/bridge.py:160-162.
        // robberPending: true between self rolling a 7 and the
        //   robber being placed (robberTile change). Cleared when:
        //   * robber moves (any tile change),
        //   * an opponent rolls a 7,
        //   * a non-7 roll fires while not pending.
        // robberMovedAtRolls: totalRolls value when robberTile last
        //   changed. Drives the "placed" review-window display.
        robberPending: false,
        robberMovedAtRolls: null,
        // WS-frame backstop for the self-knight robber window: set when
        // mechanicKnightState shows self's played-knight count rise by 1
        // on our turn (events.js), cleared on turn change. Complements
        // the chat-log knight detection in panel.js so the robber-target
        // list still opens when the chat line is missed.
        knightRobberPending: false,
        knightRobberTurn: null,
        // Active trade offers, keyed by offer_id. Each entry:
        // {creator: cidStr, give: {res:int}, want: {res:int}, ts}.
        // Mirrors Python's pending_trade_offer flow — populated from
        // tradeState.activeOffers, cleared when an offer goes null
        // or appears in tradeState.closedOffers. Authoritative
        // source for the incoming-trade banner; chat-based offers
        // are a fallback only.
        tradeOffers: {},
        // Internal: dedup fingerprint for the most-recent emitted
        // RollEvent. Prevents re-counting on resync frames.
        _lastRollFp: null,
    };
}

export function newHand() {
    return {
        WOOD: 0, BRICK: 0, SHEEP: 0, WHEAT: 0, ORE: 0,
        unknown: 0, drift: 0,
    };
}

export function newDevCardCounts() {
    return {
        KNIGHT: 0, MONOPOLY: 0, YEAR_OF_PLENTY: 0,
        ROAD_BUILDING: 0, VICTORY_POINT: 0,
    };
}

/** Edge key for the roads dict — order-independent. */
export function edgeKey(a, b) {
    a = Number(a); b = Number(b);
    return a < b ? `${a}-${b}` : `${b}-${a}`;
}

/** Initialize per-player buckets when the seat list is known. */
export function initSeats(state, colors) {
    state.colors = colors.slice();
    for (const c of colors) {
        state.hands[c] = newHand();
        state.handTotal[c] = 0;
        state.devCardsByType[c] = newDevCardCounts();
        state.devCardsTotal[c] = 0;
        state.bank[c] = { settles: 5, cities: 4, roads: 15 };
        state.playedKnights[c] = 0;
        state.roadLength[c] = 0;
        state.vp[c] = 0;
        state.vpCardsInHand[c] = 0;
    }
}

export const RESOURCE_NAMES = RESOURCES;
