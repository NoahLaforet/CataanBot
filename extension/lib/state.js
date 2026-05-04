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
        // map: { tiles: { id: {resource, number, coord, nodes:[], edges:[]} },
        //        nodes: { id: {tiles:[], edges:[], port: {kind, resource}} },
        //        edges: { 'a-b': {nodes:[a,b], tiles:[]} },
        //        ports: [...], landNodes: Set, landTiles: Set }
        map: null,
        // colors: list of player color strings in seat order.
        colors: [],
        // self color string, or null until latched.
        selfColor: null,
        // username -> color and reverse.
        usernameToColor: {},
        colorToUsername: {},
        // {nodeId: {color, kind: 'SETTLEMENT'|'CITY'}}
        buildings: {},
        // {edgeKey: color} where edgeKey is 'min-max' of the node ids.
        roads: {},
        // current robber tile id, or null pre-placement.
        robberTile: null,
        // per-color counts.
        playedKnights: {},
        roadLength: {},   // longest single chain length per color
        hasArmy: null,    // color holding LA, or null
        hasRoad: null,    // color holding LR, or null
        // public VP per color (settles + cities + LR/LA flags).
        // VP cards in hand stay hidden for opps; for self we know.
        vp: {},
        vpCardsInHand: {},  // self only typically
        // per-color hand. Each: {WOOD,BRICK,SHEEP,WHEAT,ORE: int,
        //                       unknown: int, drift: int}.
        hands: {},
        // {color: {KNIGHT,MONOPOLY,YEAR_OF_PLENTY,ROAD_BUILDING,
        //          VICTORY_POINT: int}} — typed dev cards in hand.
        // We only see types for self; opps stay aggregate.
        devCardsByType: {},
        // total dev cards in hand per color (aggregate).
        devCardsTotal: {},
        // ring buffer of recent rolls; each {total, isYou, ts}.
        rollHistory: [],
        // monotonic roll counter; used by phase boundary heuristics.
        totalRolls: 0,
        // {2..12: count} of every dice total seen.
        rollHistogram: Object.fromEntries(
            Array.from({length: 11}, (_, i) => [i + 2, 0])),
        // game configuration. Pulled from GameStart frame; defaults
        // to standard Catan.
        vpTarget: 10,
        discardLimit: 7,
        // True after GameStart; flips false on game over until next
        // start.
        started: false,
        // True between game-over and next GameStart. Drives the
        // "waiting for next game" HUD frame.
        gameOver: null,  // { winnerColor, winnerUsername }
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
        state.devCardsByType[c] = newDevCardCounts();
        state.devCardsTotal[c] = 0;
        state.playedKnights[c] = 0;
        state.roadLength[c] = 0;
        state.vp[c] = 0;
        state.vpCardsInHand[c] = 0;
    }
}

export const RESOURCE_NAMES = RESOURCES;
