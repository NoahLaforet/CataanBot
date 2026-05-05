// move_quality.js — chess-style classification of self builds against
// the bot's recs at decision time.
//
// Mirrors src/catanbot/move_quality.py:
//   !!  picked the bot's #1 rec
//   !   top 2-3
//   ?!  top 4-6
//   ?   top 7-10
//   ??  not in the top-N at all
//
// The signal is "did Noah's move agree with the engine's top picks?"
// — same question the python audit asks.

export const CLASSIFICATIONS = ['!!', '!', '?!', '?', '??'];

/** 1-indexed rank → chess-style label. Null means "not in the recs". */
export function classifyRank(rank) {
    if (rank == null) return '??';
    if (rank === 1) return '!!';
    if (rank <= 3) return '!';
    if (rank <= 6) return '?!';
    if (rank <= 10) return '?';
    return '??';
}

/** Whether a rec describes the same build a (piece, nodeId/edgeKey) tuple
 *  represents. Roads use an unordered edge-key match; settle/city use
 *  exact node-id match. */
export function recMatchesBuild(rec, ev) {
    if (!rec || !ev) return false;
    const recKind = rec.kind;
    if (recKind !== ev.piece) return false;
    if (ev.piece === 'settlement' || ev.piece === 'city'
            || ev.piece === 'opening_settlement') {
        return String(rec.node_id ?? rec.nodeId ?? '')
            === String(ev.node_id ?? '');
    }
    if (ev.piece === 'road') {
        // rec.edge can be a string (edgeKey, the recommender's output)
        // or [a, b]; ev.edge_key is the canonical "min-max" form set
        // by the diff detector. rec.edge_key wins if both present.
        let recEdge = rec.edge_key || null;
        if (!recEdge && typeof rec.edge === 'string') {
            recEdge = rec.edge;
        } else if (!recEdge && Array.isArray(rec.edge)) {
            const [a, b] = rec.edge.map(Number);
            recEdge = a < b ? `${a}-${b}` : `${b}-${a}`;
        }
        return recEdge === ev.edge_key;
    }
    return false;
}

/** First matching rec (best rank). Returns 1-indexed rank or null. */
export function findRank(recs, ev) {
    if (!Array.isArray(recs)) return null;
    for (let i = 0; i < recs.length; i++) {
        if (recMatchesBuild(recs[i], ev)) return i + 1;
    }
    return null;
}

/** Convenience: classify against a rec list. Returns {label, rank}. */
export function classifyBuildAgainstRecs(ev, recs) {
    const rank = findRank(recs, ev);
    return { label: classifyRank(rank), rank };
}
