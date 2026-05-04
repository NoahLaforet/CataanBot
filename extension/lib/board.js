// board.js — colonist mapState → standalone JS board model.
//
// Parses colonist's `mapState` (the same payload catanatron's
// colonist_map.py consumes on the bridge) into a node/edge graph
// the JS recommender can score against. Mirrors catanatron's
// CatanMap shape but only the fields our heuristics actually read.
//
// Phase 1 implementation pending — for now, exports the function
// signature so call sites can import unconditionally and get a
// "build skeleton" object back.

import { RESOURCE_NAMES } from './state.js';

const PIP_DOTS_BY_NUMBER = {
    2: 1, 12: 1,
    3: 2, 11: 2,
    4: 3, 10: 3,
    5: 4, 9: 4,
    6: 5, 8: 5,
};

/** Build a board model from a colonist mapState payload.
 *  Returns null when the variant isn't supported by the JS port —
 *  the caller falls back to bridge mode in that case.
 *
 *  Shape:
 *    {
 *      tiles: { id → {resource, number, coord, nodes:[ids], edges:[keys]} },
 *      nodes: { id → {tiles:[ids], neighbors:Set, port: {kind, resource}|null} },
 *      edges: { 'a-b' → {a, b, tiles:[ids]} },
 *      landNodes: Set<id>,
 *      landTiles: Set<id>,
 *      desertTile: id | null,
 *      ports: [ {kind: '3:1'|'2:1', resource: str|null, nodes:[ids]} ]
 *    }
 */
export function buildBoardFromColonistMap(mapState) {
    // TODO Phase 1 — actual implementation. Stub returns null so
    // any caller that imports today falls back to bridge mode.
    if (!mapState) return null;
    return null;
}

export function pipsForNumber(n) {
    return PIP_DOTS_BY_NUMBER[Number(n) || 0] || 0;
}

export { PIP_DOTS_BY_NUMBER };
