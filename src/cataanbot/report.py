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

# Build costs by piece — used to estimate resource outflow even when the
# BuildEvent itself lands as "unhandled" (no board topology yet).
_BUILD_COSTS = {
    "settlement": {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1},
    "city":       {"WHEAT": 2, "ORE": 3},
    "road":       {"WOOD": 1, "BRICK": 1},
}
_DEV_BUY_COST = {"SHEEP": 1, "WHEAT": 1, "ORE": 1}

# 2d6 probabilities — how often each sum "should" appear.
_DICE_PROBABILITY = {
    2: 1 / 36, 3: 2 / 36, 4: 3 / 36, 5: 4 / 36, 6: 5 / 36,
    7: 6 / 36, 8: 5 / 36, 9: 4 / 36, 10: 3 / 36, 11: 2 / 36, 12: 1 / 36,
}


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
    trade_gave: dict[str, int] = field(default_factory=dict)
    trade_got: dict[str, int] = field(default_factory=dict)
    trade_partners: Counter = field(default_factory=Counter)
    bank_trades: list[tuple[dict[str, int], dict[str, int]]] = field(
        default_factory=list,
    )
    vp_awards: list[str] = field(default_factory=list)
    yop_gained: dict[str, int] = field(default_factory=dict)
    mono_gained: dict[str, int] = field(default_factory=dict)
    # Only populated when the resource is revealed in the log — in
    # colonist.io that happens when the current user is either side
    # of the steal. Third-party steals stay invisible here, which is
    # the whole reason hand-inference is a downstream problem.
    steal_gained: dict[str, int] = field(default_factory=dict)
    steal_lost: dict[str, int] = field(default_factory=dict)

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
    # Populated lazily by build_report when hand tracking is requested;
    # left as None for callers that don't need it to keep existing
    # tests of the report structure stable.
    reconstructed_hands: dict | None = None
    color_map: ColorMap | None = None


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
            if event.card == "year_of_plenty":
                for res, n in event.resources.items():
                    stats.yop_gained[res] = stats.yop_gained.get(res, 0) + n
        elif isinstance(event, MonopolyStealEvent):
            stats = _stats_for(stats_by_color, color_map, event.player)
            stats.monopolies.append((event.resource, event.count))
            stats.mono_gained[event.resource] = (
                stats.mono_gained.get(event.resource, 0) + event.count
            )
        elif isinstance(event, StealEvent):
            thief_stats = _stats_for(stats_by_color, color_map, event.thief)
            victim_stats = _stats_for(stats_by_color, color_map, event.victim)
            thief_stats.steals_as_thief += 1
            victim_stats.steals_as_victim += 1
            if event.resource:
                thief_stats.steal_gained[event.resource] = (
                    thief_stats.steal_gained.get(event.resource, 0) + 1
                )
                victim_stats.steal_lost[event.resource] = (
                    victim_stats.steal_lost.get(event.resource, 0) + 1
                )
        elif isinstance(event, TradeCommitEvent):
            giver_stats = _stats_for(stats_by_color, color_map, event.giver)
            if event.receiver == "BANK":
                giver_stats.trades_bank += 1
                giver_stats.bank_trades.append(
                    (dict(event.gave), dict(event.got)),
                )
            else:
                giver_stats.trades_player += 1
                recv_stats = _stats_for(
                    stats_by_color, color_map, event.receiver,
                )
                recv_stats.trades_player += 1
                giver_stats.trade_partners[recv_stats.color] += 1
                recv_stats.trade_partners[giver_stats.color] += 1
                for res, n in event.gave.items():
                    giver_stats.trade_gave[res] = (
                        giver_stats.trade_gave.get(res, 0) + n
                    )
                    recv_stats.trade_got[res] = (
                        recv_stats.trade_got.get(res, 0) + n
                    )
                for res, n in event.got.items():
                    giver_stats.trade_got[res] = (
                        giver_stats.trade_got.get(res, 0) + n
                    )
                    recv_stats.trade_gave[res] = (
                        recv_stats.trade_gave.get(res, 0) + n
                    )
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
    from cataanbot.hand_tracker import reconstruct_hands
    hands = reconstruct_hands(events, color_map)
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
        reconstructed_hands=hands,
        color_map=color_map,
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
    lines.extend(_format_trade_ledger(report))
    lines.append("")
    lines.extend(_format_known_flow(report))
    lines.append("")
    lines.extend(_format_reconstructed_hands(report))
    lines.append("")
    lines.extend(_format_dispatch_quality(report))
    return "\n".join(lines)


def _format_reconstructed_hands(report: ReplayReport) -> list[str]:
    if report.reconstructed_hands is None or report.color_map is None:
        return []
    from cataanbot.hand_tracker import format_hands_table
    return format_hands_table(report.reconstructed_hands, report.color_map)


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
    total_rolls = sum(report.roll_histogram.values())
    if report.first_ts is not None and report.last_ts is not None:
        minutes = (report.last_ts - report.first_ts) / 60.0
        out.append(
            f"Duration: ~{minutes:.1f} minutes over {total_rolls} rolls"
        )
    elif total_rolls:
        out.append(f"Total rolls: {total_rolls}")
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
    total = sum(hist.values())
    max_count = max(hist.values())
    bar_width = 32
    for n in range(2, 13):
        c = hist.get(n, 0)
        bar = "█" * int(round(bar_width * c / max_count)) if max_count else ""
        exp = total * _DICE_PROBABILITY[n]
        # Signed delta vs. 2d6 expectation — lets you eyeball "the dice god
        # hated BrickdDaddy's 8s" at a glance. Only shown once we have
        # enough rolls to make noise less misleading.
        if total >= 12:
            delta = c - exp
            luck = f"  (exp {exp:>4.1f}, {delta:+.1f})"
        else:
            luck = ""
        marker = "  ← most" if c == max_count and c > 0 else ""
        lines.append(
            f"  {n:>2}: {bar:<{bar_width}} {c}{luck}{marker}"
        )
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


def _format_trade_ledger(report: ReplayReport) -> list[str]:
    """Who traded what with whom. The ledger answers "is Felix dumping
    wheat on BrickdDaddy" at a glance — strategic signal that nothing
    else in the report surfaces."""
    lines = ["Trade ledger:"]
    players = _players_in_color_order(report.players)
    any_activity = any(
        s.trades_player or s.trades_bank for _, s in players
    )
    if not any_activity:
        lines.append("  (no trades in log)")
        return lines
    for color, s in players:
        if not (s.trades_player or s.trades_bank):
            continue
        lines.append(f"  {s.username} ({color}):")
        if s.trades_player:
            partners = ", ".join(
                f"{c}×{n}" for c, n in s.trade_partners.most_common()
            )
            lines.append(
                f"    partners      {s.trades_player} trades — {partners}"
            )
            lines.append(
                f"    gave          {_fmt_res_counter(s.trade_gave)}"
            )
            lines.append(
                f"    received      {_fmt_res_counter(s.trade_got)}"
            )
            net = _net_flow(s.trade_got, s.trade_gave)
            lines.append(
                f"    net           {_fmt_signed_counter(net)}"
            )
        if s.bank_trades:
            lines.append(
                f"    bank/port     {len(s.bank_trades)} trades — "
                f"{_fmt_bank_trades(s.bank_trades)}"
            )
    return lines


def _net_flow(
    got: dict[str, int], gave: dict[str, int],
) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in _RESOURCES:
        delta = got.get(r, 0) - gave.get(r, 0)
        if delta:
            out[r] = delta
    return out


def _fmt_signed_counter(res: dict[str, int]) -> str:
    parts = []
    for r in _RESOURCES:
        n = res.get(r, 0)
        if n:
            parts.append(f"{n:+d}x{r}")
    return " ".join(parts) if parts else "even"


def _fmt_bank_trades(
    trades: list[tuple[dict[str, int], dict[str, int]]],
) -> str:
    # Group identical gave→got shapes so "4xWOOD→1xWHEAT ×3" reads
    # cleaner than listing three identical entries.
    shape_counts: Counter = Counter()
    for gave, got in trades:
        key = (
            tuple(sorted(gave.items())),
            tuple(sorted(got.items())),
        )
        shape_counts[key] += 1
    parts = []
    for (gave_items, got_items), n in shape_counts.most_common():
        gave_str = " ".join(f"{v}x{k}" for k, v in gave_items)
        got_str = " ".join(f"{v}x{k}" for k, v in got_items)
        suffix = f" ×{n}" if n > 1 else ""
        parts.append(f"{gave_str}→{got_str}{suffix}")
    return ", ".join(parts)


def _known_flow(s: PlayerStats) -> tuple[
    dict[str, int], dict[str, int], dict[str, int],
]:
    """Return (sources, sinks, net) per resource.

    "Known" means we saw the resource explicitly in the log — so
    third-party steals and monopoly-victim losses don't appear here.
    Net can still be deceiving (hidden steals won't net out), but the
    sign tells you whether the player's visible activity had them
    generating or spending."""
    sources: dict[str, int] = {}
    sinks: dict[str, int] = {}

    def _add(bucket: dict[str, int], res: str, n: int) -> None:
        if n:
            bucket[res] = bucket.get(res, 0) + n

    for src in (s.produced, s.trade_got, s.yop_gained,
                s.mono_gained, s.steal_gained):
        for r, n in src.items():
            _add(sources, r, n)
    for snk in (s.discarded, s.trade_gave, s.steal_lost):
        for r, n in snk.items():
            _add(sinks, r, n)

    for piece, count in s.builds.items():
        cost = _BUILD_COSTS.get(piece)
        if not cost:
            continue
        for r, n in cost.items():
            _add(sinks, r, n * count)
    if s.dev_buys:
        for r, n in _DEV_BUY_COST.items():
            _add(sinks, r, n * s.dev_buys)

    net = {}
    for r in _RESOURCES:
        delta = sources.get(r, 0) - sinks.get(r, 0)
        if delta:
            net[r] = delta
    return sources, sinks, net


def _format_known_flow(report: ReplayReport) -> list[str]:
    """Show per-player net visible resource flow.

    Sources include dice production, trades received, monopoly hauls,
    YoP gains, and steals where the resource was revealed. Sinks
    include discards, trades given, inferred build/dev-buy costs, and
    steals lost with revealed resource."""
    lines = [
        "Known resource flow "
        "(sources - sinks; hidden steals / monopoly victims excluded):",
    ]
    players = _players_in_color_order(report.players)
    if not players:
        lines.append("  (no players)")
        return lines
    name_width = max(
        (len(s.username) for _, s in players), default=8,
    )
    name_width = max(name_width, 8)
    header = "  " + " " * name_width + "".join(
        f"{r[:3]:>5}" for r in _RESOURCES
    )
    lines.append(header)
    for _color, s in players:
        _sources, _sinks, net = _known_flow(s)
        cells = []
        for r in _RESOURCES:
            v = net.get(r, 0)
            cells.append(f"{v:>+5d}" if v else f"{'.':>5}")
        lines.append(f"  {s.username:<{name_width}}" + "".join(cells))
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
