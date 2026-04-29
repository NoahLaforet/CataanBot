"""Audit a colonist WS capture against the recommender.

For each post-setup build Noah took, capture what ``recommend_actions``
would have suggested at that moment, and compute the rank of his actual
choice. Output a per-game summary plus a JSONL of decision-points for
further analysis.

Usage:
    python audit_missed_recs.py <capture.json> [<capture.json> ...]

For each capture, writes a sibling ``<capture>.audit.jsonl`` with one
JSON line per qualifying decision and prints a human-readable summary.

Move classification follows the chess-style scheme from
``HUD_RESEARCH.md`` principle #7:
    !!  picked the bot's #1 rec
    !   picked one of the top 3
    ?!  picked top 4-6
    ?   picked top 7-10
    ??  not in top 10
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

from cataanbot.colonist_diff import events_from_frame_payload
from cataanbot.colonist_proto import load_capture
from cataanbot.events import BuildEvent
from cataanbot.live import apply_event
from cataanbot.live_game import LiveGame


def _classify(rank: int | None) -> str:
    if rank is None:
        return "??"
    if rank == 1:
        return "!!"
    if rank <= 3:
        return "!"
    if rank <= 6:
        return "?!"
    return "?"


def _rec_match(rec: dict[str, Any], ev: BuildEvent) -> bool:
    if rec.get("kind") != ev.piece:
        return False
    if ev.piece in ("settlement", "city"):
        return int(rec.get("node_id") or -1) == int(ev.node_id or -2)
    if ev.piece == "road":
        edge = rec.get("edge")
        if not edge or len(edge) != 2:
            return False
        a, b = sorted(int(x) for x in edge)
        ea, eb = sorted(ev.edge_nodes or (-1, -1))
        return (a, b) == (ea, eb)
    return False


def _hand_from_game(game, color) -> dict[str, int]:
    from catanatron import Color
    from catanatron.state import RESOURCES
    c = color if isinstance(color, Color) else Color[str(color).upper()]
    state = game.state
    idx = state.color_to_index[c]
    return {
        r: int(state.player_state.get(f"P{idx}_{r}_IN_HAND", 0))
        for r in RESOURCES
    }


def _rec_summary(rec: dict[str, Any] | None) -> dict[str, Any] | None:
    if not rec:
        return None
    return {
        "kind": rec.get("kind"),
        "node_id": rec.get("node_id"),
        "edge": list(rec["edge"]) if rec.get("edge") else None,
        "score": rec.get("score"),
        "detail": rec.get("detail"),
        "search_delta": rec.get("search_delta"),
    }


def audit(capture_path: Path) -> dict[str, Any]:
    from cataanbot.recommender import recommend_actions

    game = LiveGame()
    decisions: list[dict[str, Any]] = []
    setup_seen = {"settlement": 0, "road": 0}

    for frame in load_capture(capture_path):
        if frame.error:
            continue
        payload = frame.payload
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        body = payload.get("payload") or {}

        if ptype == 4:
            if not game.started:
                game.start_from_game_state(body)
            else:
                game._resync_from_replay(body)
            continue

        if ptype != 91 or not game.started:
            continue

        events = events_from_frame_payload(game.session, payload)

        for ev in events:
            self_name = game.session.player_names.get(
                game.session.self_color_id) if game.session else None
            is_self_build = (
                isinstance(ev, BuildEvent)
                and self_name is not None
                and ev.player == self_name
            )

            decision_rec = None
            if is_self_build:
                color = game.color_map.get(ev.player)
                tally = game.build_counts.get(
                    color, {"settlement": 0, "city": 0, "road": 0})
                is_setup = (
                    (ev.piece == "settlement" and tally["settlement"] < 2)
                    or (ev.piece == "road" and tally["road"] < 2)
                )
                if not is_setup:
                    rec_error = None
                    try:
                        pre_game = copy.deepcopy(game.tracker.game)
                        pre_hand = _hand_from_game(pre_game, color)
                        recs = recommend_actions(
                            pre_game, color, pre_hand, top=10)
                    except Exception as e:  # noqa: BLE001
                        import traceback
                        traceback.print_exc()
                        recs = []
                        pre_hand = {}
                        rec_error = repr(e)

                    rank = None
                    for i, rec in enumerate(recs, start=1):
                        if _rec_match(rec, ev):
                            rank = i
                            break

                    decision_rec = {
                        "ts": getattr(frame, "ts", None),
                        "piece": ev.piece,
                        "actual_node": ev.node_id,
                        "actual_edge": (
                            list(ev.edge_nodes) if ev.edge_nodes else None),
                        "hand": pre_hand,
                        "rank": rank,
                        "rec_count": len(recs),
                        "top_rec": _rec_summary(recs[0] if recs else None),
                        "actual_rec": _rec_summary(
                            recs[rank - 1] if rank else None),
                        "classification": _classify(rank),
                        "rec_error": rec_error,
                    }
                    decisions.append(decision_rec)

            result = apply_event(game.tracker, game.color_map, ev)
            if (result.status == "applied"
                    and isinstance(result.event, BuildEvent)):
                game._debit_build(result.event)

    self_player = None
    self_color = None
    if game.started and game.session.self_color_id is not None:
        self_player = game.session.player_names.get(
            game.session.self_color_id)
        if self_player and game.color_map.has(self_player):
            self_color = game.color_map.get(self_player)
    return {
        "decisions": decisions,
        "self_player": self_player,
        "self_color": self_color,
    }


def summarize(audit_result: dict[str, Any]) -> str:
    decisions = audit_result["decisions"]
    if not decisions:
        return "no qualifying post-setup build decisions in capture"

    classes = {"!!": 0, "!": 0, "?!": 0, "?": 0, "??": 0}
    by_piece: dict[str, dict[str, int]] = {}
    for d in decisions:
        c = d["classification"]
        classes[c] = classes.get(c, 0) + 1
        piece = d["piece"]
        by_piece.setdefault(
            piece, {"!!": 0, "!": 0, "?!": 0, "?": 0, "??": 0})
        by_piece[piece][c] = by_piece[piece].get(c, 0) + 1

    total = len(decisions)
    lines = [
        f"player: {audit_result['self_player']}",
        f"total post-setup build decisions: {total}",
        "",
        "rank distribution:",
    ]
    labels = {
        "!!": "top rec    ",
        "!":  "top 3      ",
        "?!": "top 4-6    ",
        "?":  "top 7-10   ",
        "??": "not in top10",
    }
    for sym in ("!!", "!", "?!", "?", "??"):
        n = classes.get(sym, 0)
        pct = (100.0 * n / total) if total else 0
        lines.append(f"  {sym} {labels[sym]}: {n:3d}  {pct:5.1f}%")

    lines.append("")
    lines.append("by piece:")
    for piece, dist in by_piece.items():
        n = sum(dist.values())
        breakdown = " ".join(
            f"{sym}{dist.get(sym, 0)}" for sym in ("!!", "!", "?!", "?", "??"))
        lines.append(f"  {piece:12s} ({n}): {breakdown}")

    blunders = [d for d in decisions
                if d["classification"] in ("?", "??")]
    if blunders:
        lines.append("")
        lines.append(f"flagged moves ({len(blunders)} total — first 10):")
        for d in blunders[:10]:
            top = d.get("top_rec")
            actual = d.get("actual_rec")
            if top:
                top_loc = (top["node_id"] if top.get("node_id") is not None
                           else top.get("edge"))
                top_str = f"{top['kind']} @ {top_loc}"
                if top.get("score") is not None:
                    top_str += f" (score {top['score']:.1f}"
                    # EV gap: how much eval-bar Noah left on the table by
                    # picking his move over the bot's top. Only present
                    # when both recs are simulatable; trades and
                    # discards keep search_delta=None.
                    top_sd = top.get("search_delta")
                    actual_sd = actual.get("search_delta") if actual else None
                    if (isinstance(top_sd, (int, float))
                            and isinstance(actual_sd, (int, float))):
                        gap = top_sd - actual_sd
                        top_str += f", EV gap {gap:+.0f}"
                    top_str += ")"
            else:
                top_str = "(no recs)"
            actual_loc = (d["actual_node"] if d["actual_node"] is not None
                          else d["actual_edge"])
            lines.append(
                f"  [{d['classification']}] played {d['piece']} @ {actual_loc}"
                f"; rank={d['rank'] or '>10'}; top was {top_str}")

    return "\n".join(lines)


def _aggregate_summary(all_decisions: list[dict[str, Any]],
                       num_games: int) -> str:
    """Cross-game tally — how does Noah classify against the bot when
    you average over a session? Same buckets as ``summarize`` but the
    denominator is decisions across N captures."""
    if not all_decisions:
        return "no qualifying decisions across all captures"

    classes = {"!!": 0, "!": 0, "?!": 0, "?": 0, "??": 0}
    by_piece: dict[str, dict[str, int]] = {}
    for d in all_decisions:
        c = d["classification"]
        classes[c] = classes.get(c, 0) + 1
        piece = d["piece"]
        by_piece.setdefault(
            piece, {"!!": 0, "!": 0, "?!": 0, "?": 0, "??": 0})
        by_piece[piece][c] = by_piece[piece].get(c, 0) + 1

    total = len(all_decisions)
    lines = [
        f"games audited: {num_games}",
        f"total post-setup build decisions: {total}",
        "",
        "rank distribution (across all games):",
    ]
    labels = {
        "!!": "top rec    ",
        "!":  "top 3      ",
        "?!": "top 4-6    ",
        "?":  "top 7-10   ",
        "??": "not in top10",
    }
    for sym in ("!!", "!", "?!", "?", "??"):
        n = classes.get(sym, 0)
        pct = (100.0 * n / total) if total else 0
        lines.append(f"  {sym} {labels[sym]}: {n:3d}  {pct:5.1f}%")

    lines.append("")
    lines.append("by piece (across all games):")
    for piece, dist in by_piece.items():
        n = sum(dist.values())
        breakdown = " ".join(
            f"{sym}{dist.get(sym, 0)}" for sym in ("!!", "!", "?!", "?", "??"))
        lines.append(f"  {piece:12s} ({n}): {breakdown}")

    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "usage: python audit_missed_recs.py <capture.json> ...",
            file=sys.stderr)
        return 1

    all_decisions: list[dict[str, Any]] = []
    games_with_data = 0
    for arg in sys.argv[1:]:
        path = Path(arg).expanduser()
        if not path.exists():
            print(f"no such file: {path}", file=sys.stderr)
            continue
        print(f"=== {path.name} ===")
        try:
            result = audit(path)
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"error: {e}", file=sys.stderr)
            traceback.print_exc()
            continue
        print(summarize(result))
        out_path = path.with_suffix(".audit.jsonl")
        with out_path.open("w") as f:
            for d in result["decisions"]:
                f.write(json.dumps(d) + "\n")
        print(f"\nwrote per-decision JSONL: {out_path}")
        print()
        if result["decisions"]:
            all_decisions.extend(result["decisions"])
            games_with_data += 1

    # Cross-game aggregate, only when there's more than one capture with
    # qualifying decisions. Single-capture runs already show the same
    # numbers via ``summarize``, so this would just be duplication.
    if games_with_data > 1:
        print("=== aggregate across all captures ===")
        print(_aggregate_summary(all_decisions, games_with_data))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
