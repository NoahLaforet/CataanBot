"""Post-game report generation from a captured event stream.

Given a list of `Event`s and the final `Tracker` + `ColorMap` state after
replaying them, `build_report` assembles a human-readable summary:
winner, final VP, per-player aggregates (rolls, builds, dev plays,
trades, 7s, monopolies), a dice-roll histogram, and a parser-quality
breakdown. Works purely offline — no live colonist session needed.

Keep this focused on *what the raw log tells us*. Anything that requires
the board topology (who produced from which tile, robber-tile history)
belongs downstream once the DOM→catanatron mapping is wired.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from cataanbot.events import (
    BuildEvent, DevCardBuyEvent, DevCardPlayEvent, DiscardEvent,
    Event, GameOverEvent, MonopolyStealEvent, ProduceEvent, RollEvent,
    StealEvent, TradeCommitEvent, VPEvent,
)
from cataanbot.live import ColorMap, DispatchResult


_RESOURCES = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")


@dataclass
class PlayerStats:
    """Per-player aggregates derived directly from the event stream."""
    username: str
    color: str
    rolls: int = 0
    sevens: int = 0
    produced: dict[str, int] = field(default_factory=dict)
    discarded: dict[str, int] = field(default_factory=dict)
    builds: Counter = field(default_factory=Counter)
    dev_buys: int = 0
    dev_plays: Counter = field(default_factory=Counter)
    monopolies: list[tuple[str, int]] = field(default_factory=list)
    steals_as_thief: int = 0
    steals_as_victim: int = 0
    trades_player: int = 0
    trades_bank: int = 0
    vp_awards: list[str] = field(default_factory=list)

    @property
    def produced_total(self) -> int:
        return sum(self.produced.values())

    @property
    def discarded_total(self) -> int:
        return sum(self.discarded.values())

    @property
    def builds_total(self) -> int:
        return sum(self.builds.values())


@dataclass
class ReplayReport:
    """Everything build_report collected for one game."""
    jsonl_path: str | None
    winner_username: str | None
    winner_color: str | None
    final_vp: dict[str, int]
    players: dict[str, PlayerStats]       # keyed by color
    roll_histogram: Counter                # dice total → count
    dispatch_counts: dict[str, int]       # applied/skipped/unhandled/error
    first_ts: float | None
    last_ts: float | None


def build_report(
    events: list[Event],
    dispatch_results: list[DispatchResult],
    color_map: ColorMap,
    final_vp: dict[str, int],
    timestamps: list[float] | None = None,
    jsonl_path: str | None = None,
) -> ReplayReport:
    """Aggregate a replay into a ReplayReport.

    `events` and `dispatch_results` are index-aligned — one result per
    parsed event. `timestamps` (optional) lets the header show game
    duration; if omitted the report just skips it."""
    stats_by_color = _init_stats(color_map)
    histogram: Counter = Counter()
    winner_username: str | None = None

    for event in events:
        if isinstance(event, RollEvent):
            histogram[event.total] += 1
            stats = _stats_for(stats_by_color, color_map, event.player)
            stats.rolls += 1
            if event.total == 7:
                stats.sevens += 1
        elif isinstance(event, ProduceEvent):
            stats = _stats_for(stats_by_color, color_map, event.player)
            for res, n in event.resources.items():
                stats.produced[res] = stats.produced.get(res, 0) + n
        elif isinstance(event, DiscardEvent):
            stats = _stats_for(stats_by_color, color_map, event.player)
            for res, n in event.resources.items():
                stats.discarded[res] = stats.discarded.get(res, 0) + n
        elif isinstance(event, BuildEvent):
            stats = _stats_for(stats_by_color, color_map, event.player)
            stats.builds[event.piece] += 1
        elif isinstance(event, DevCardBuyEvent):
            stats = _stats_for(stats_by_color, color_map, event.player)
            stats.dev_buys += 1
        elif isinstance(event, DevCardPlayEvent):
            stats = _stats_for(stats_by_color, color_map, event.player)
            stats.dev_plays[event.card] += 1
        elif isinstance(event, MonopolyStealEvent):
            stats = _stats_for(stats_by_color, color_map, event.player)
            stats.monopolies.append((event.resource, event.count))
        elif isinstance(event, StealEvent):
            thief_stats = _stats_for(stats_by_color, color_map, event.thief)
            victim_stats = _stats_for(stats_by_color, color_map, event.victim)
            thief_stats.steals_as_thief += 1
            victim_stats.steals_as_victim += 1
        elif isinstance(event, TradeCommitEvent):
            giver_stats = _stats_for(stats_by_color, color_map, event.giver)
            if event.receiver == "BANK":
                giver_stats.trades_bank += 1
            else:
                giver_stats.trades_player += 1
                recv_stats = _stats_for(
                    stats_by_color, color_map, event.receiver,
                )
                recv_stats.trades_player += 1
        elif isinstance(event, VPEvent):
            stats = _stats_for(stats_by_color, color_map, event.player)
            stats.vp_awards.append(event.reason)
        elif isinstance(event, GameOverEvent):
            winner_username = event.winner
            _stats_for(stats_by_color, color_map, event.winner)

    dispatch_counts = {"applied": 0, "skipped": 0, "unhandled": 0, "error": 0}
    for r in dispatch_results:
        dispatch_counts[r.status] = dispatch_counts.get(r.status, 0) + 1

    first_ts = last_ts = None
    if timestamps:
        valid = [t for t in timestamps if t is not None]
        if valid:
            first_ts = min(valid)
            last_ts = max(valid)

    winner_color = (
        color_map.get(winner_username) if winner_username else None
    )
    return ReplayReport(
        jsonl_path=jsonl_path,
        winner_username=winner_username,
        winner_color=winner_color,
        final_vp=dict(final_vp),
        players=stats_by_color,
        roll_histogram=histogram,
        dispatch_counts=dispatch_counts,
        first_ts=first_ts,
        last_ts=last_ts,
    )


def format_report(report: ReplayReport) -> str:
    """Render a ReplayReport as a readable multi-line string."""
    lines: list[str] = []
    bar = "=" * 64
    lines.append(bar)
    title = "CataanBot replay"
    if report.jsonl_path:
        title += f" — {report.jsonl_path}"
    lines.append(title)
    lines.append(bar)
    lines.append("")

    lines.extend(_format_players_block(report))
    lines.append("")
    lines.extend(_format_winner_block(report))
    lines.append("")
    lines.extend(_format_scoreboard(report))
    lines.append("")
    lines.extend(_format_histogram(report.roll_histogram))
    lines.append("")
    lines.extend(_format_per_player(report))
    lines.append("")
    lines.extend(_format_dispatch_quality(report))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_players_block(report: ReplayReport) -> list[str]:
    lines = ["Players:"]
    if not report.players:
        lines.append("  (none — empty log)")
        return lines
    for color, stats in _players_in_color_order(report.players):
        lines.append(f"  {color:<7} = {stats.username}")
    return lines


def _format_winner_block(report: ReplayReport) -> list[str]:
    if report.winner_username is None:
        out = ["Winner: (no GameOverEvent seen in log)"]
    else:
        out = [
            f"Winner: {report.winner_username} "
            f"({report.winner_color}) at "
            f"{report.final_vp.get(report.winner_color, 0)} VP",
        ]
    if report.first_ts is not None and report.last_ts is not None:
        minutes = (report.last_ts - report.first_ts) / 60.0
        out.append(f"Duration: ~{minutes:.1f} minutes")
    return out


def _format_scoreboard(report: ReplayReport) -> list[str]:
    lines = ["Final scores (tracker):"]
    ranking = sorted(
        report.players.items(),
        key=lambda kv: report.final_vp.get(kv[0], 0),
        reverse=True,
    )
    for color, stats in ranking:
        vp = report.final_vp.get(color, 0)
        tag = ""
        if stats.vp_awards:
            tag = f"  ({', '.join(sorted(set(stats.vp_awards)))})"
        lines.append(f"  {color:<7} — {vp:>2} VP  ({stats.username}){tag}")
    return lines


def _format_histogram(hist: Counter) -> list[str]:
    lines = ["Dice histogram:"]
    if not hist:
        lines.append("  (no rolls)")
        return lines
    max_count = max(hist.values())
    bar_width = 32
    for n in range(2, 13):
        c = hist.get(n, 0)
        bar = "█" * int(round(bar_width * c / max_count)) if max_count else ""
        marker = "  ← most" if c == max_count and c > 0 else ""
        lines.append(f"  {n:>2}: {bar:<{bar_width}} {c}{marker}")
    return lines


def _format_per_player(report: ReplayReport) -> list[str]:
    lines = ["Per-player activity:"]
    for color, s in _players_in_color_order(report.players):
        lines.append(f"  {s.username} ({color}):")
        lines.append(
            f"    rolls         {s.rolls} "
            f"(sevens {s.sevens})"
        )
        lines.append(
            f"    produced      {s.produced_total}  "
            f"{_fmt_res_counter(s.produced)}"
        )
        if s.discarded_total:
            lines.append(
                f"    discarded     {s.discarded_total}  "
                f"{_fmt_res_counter(s.discarded)}"
            )
        if s.builds_total:
            parts = [f"{n}x{p}" for p, n in s.builds.most_common()]
            lines.append(f"    builds        {', '.join(parts)}")
        if s.dev_buys or s.dev_plays:
            plays = ", ".join(
                f"{n}x{k}" for k, n in s.dev_plays.most_common()
            ) or "none played"
            lines.append(
                f"    dev cards     bought {s.dev_buys} / played {plays}"
            )
        if s.monopolies:
            mono_parts = [f"{n}x{r}" for r, n in s.monopolies]
            lines.append(f"    monopolies    {', '.join(mono_parts)}")
        if s.steals_as_thief or s.steals_as_victim:
            lines.append(
                f"    steals        took {s.steals_as_thief}, "
                f"lost {s.steals_as_victim}"
            )
        if s.trades_player or s.trades_bank:
            lines.append(
                f"    trades        player-to-player "
                f"{s.trades_player}, bank {s.trades_bank}"
            )
    return lines


def _format_dispatch_quality(report: ReplayReport) -> list[str]:
    c = report.dispatch_counts
    total = sum(c.values())
    return [
        "Parser / dispatcher quality:",
        f"  {c.get('applied', 0):>4} applied",
        f"  {c.get('skipped', 0):>4} skipped (informational)",
        f"  {c.get('unhandled', 0):>4} unhandled (needs board topology "
        f"or hand inference)",
        f"  {c.get('error', 0):>4} errors (tracker rejected — usually "
        f"hand desync from unknown-resource steals)",
        f"  {total:>4} total events",
    ]


def _init_stats(color_map: ColorMap) -> dict[str, PlayerStats]:
    # Pre-populate with whatever's already in the map so an explicit
    # --player mapping shows up even if that color never acted.
    return {
        color: PlayerStats(username=user, color=color)
        for user, color in color_map.as_dict().items()
    }


def _stats_for(
    stats_by_color: dict[str, PlayerStats],
    color_map: ColorMap,
    username: str,
) -> PlayerStats:
    color = color_map.get(username)
    if color not in stats_by_color:
        stats_by_color[color] = PlayerStats(username=username, color=color)
    return stats_by_color[color]


def _players_in_color_order(
    stats_by_color: dict[str, PlayerStats],
) -> list[tuple[str, PlayerStats]]:
    order = {"RED": 0, "BLUE": 1, "WHITE": 2, "ORANGE": 3}
    return sorted(
        stats_by_color.items(),
        key=lambda kv: order.get(kv[0], 99),
    )


def _fmt_res_counter(res: dict[str, int]) -> str:
    parts = []
    for r in _RESOURCES:
        n = res.get(r, 0)
        if n:
            parts.append(f"{n}x{r}")
    return " ".join(parts) if parts else "∅"
