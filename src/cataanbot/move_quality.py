"""Chess-style classification of recorded moves vs. the bot's recs.

Lifted from the per-game audit so the live HUD and the audit share one
code path. The classification scheme mirrors lichess/chess.com:

    !!  picked the bot's #1 rec (top recommendation)
    !   picked one of the top 3
    ?!  picked top 4-6 (acceptable but not best)
    ?   picked top 7-10 (questionable)
    ??  not in top 10 (likely a blunder relative to the bot's read)

"Top" is the rank within whatever rec list the bridge or audit
generated for that decision moment. The signal is "did Noah's move
agree with the engine's top picks" — same question the audit asks.
"""
from __future__ import annotations

from typing import Any

from cataanbot.events import BuildEvent

CLASSIFICATIONS = ("!!", "!", "?!", "?", "??")


def classify_rank(rank: int | None) -> str:
    """Map a rank-in-recs (1-indexed) to its chess-style label.

    ``None`` means the move wasn't in the recs at all (rank > top_n) and
    grades as a blunder. The thresholds match HUD_RESEARCH.md principle
    #7 and ``audit_missed_recs._classify`` so the audit JSONLs and the
    live HUD speak the same vocabulary."""
    if rank is None:
        return "??"
    if rank == 1:
        return "!!"
    if rank <= 3:
        return "!"
    if rank <= 6:
        return "?!"
    if rank <= 10:
        return "?"
    return "??"


def rec_matches_build(rec: dict[str, Any], ev: BuildEvent) -> bool:
    """True if ``rec`` is the same build action ``ev`` represents.

    Settlement/city: same kind + same node_id.
    Road: same kind + same unordered edge endpoints.
    Anything else (dev_card, trade, propose_trade): never matches a
    BuildEvent — the recs aren't directly comparable to a build.
    """
    if rec.get("kind") != ev.piece:
        return False
    if ev.piece in ("settlement", "city"):
        try:
            return int(rec.get("node_id") or -1) == int(ev.node_id or -2)
        except (TypeError, ValueError):
            return False
    if ev.piece == "road":
        edge = rec.get("edge")
        if not edge or len(edge) != 2:
            return False
        try:
            a, b = sorted(int(x) for x in edge)
            ea, eb = sorted(int(x) for x in (ev.edge_nodes or (-1, -1)))
        except (TypeError, ValueError):
            return False
        return (a, b) == (ea, eb)
    return False


def find_rank(recs: list[dict[str, Any]],
              ev: BuildEvent) -> int | None:
    """1-indexed rank of ``ev``'s matching rec in ``recs``, or None.

    Walks ``recs`` in order — the list comes from the recommender
    sorted best-first, so first match is the best rank. Returns None
    when no rec matches (i.e. the move wasn't in the bot's top-N at
    all)."""
    for i, rec in enumerate(recs, start=1):
        if rec_matches_build(rec, ev):
            return i
    return None


def classify_build_against_recs(ev: BuildEvent,
                                recs: list[dict[str, Any]]
                                ) -> tuple[str, int | None]:
    """Convenience: find rank, classify, return both.

    Used by the live bridge to produce the move-quality entry per
    self-build. ``rank=None`` means "not in the recs" → "??"; a
    1..N rank gets the matching label."""
    rank = find_rank(recs, ev)
    return classify_rank(rank), rank


__all__ = [
    "CLASSIFICATIONS",
    "classify_rank",
    "rec_matches_build",
    "find_rank",
    "classify_build_against_recs",
]
