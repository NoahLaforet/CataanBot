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
    Event, GameOverEvent, MonopolyStealEvent, NoStealEvent, ProduceEvent,
    RobberMoveEvent, RollEvent, StealEvent, TradeCommitEvent, VPEvent,
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
class TradeImpact:
    """One player-to-player trade scored by per-side marginal-value delta.

    Marginal value of a resource for a given player is approximated as
    `1 / (0.5 + total_produced_so_far[resource])`, using the event-stream
    cumulative produce counts as a scarcity proxy. Rarer-for-you = higher
    marginal, so `giver_delta > 0` means what you received was worth more
    than what you gave up (from your perspective).

    Bank / port trades are excluded — those land in PlayerStats.bank_trades
    with their own implicit 2:1/3:1/4:1 EV that doesn't map cleanly to
    this scoring rubric.
    """
    event_index: int
    giver: str
    receiver: str
    gave: dict[str, int]
    got: dict[str, int]
    giver_delta: float
    receiver_delta: float


@dataclass
class SevenImpact:
    """One 7-roll and the damage it did.

    `roller` is the username who rolled. `discards` is a dict of
    username → total cards lost to the forced discard (summed across
    any resources). `robber_tile` is the colonist tile label (e.g.
    'ore tile') the robber moved to; may be None if the log didn't
    include a RobberMoveEvent before the next roll. `steal_victim`
    and `steal_resource` capture the post-robber steal, both optional
    (resource stays None on third-party steals).
    """
    event_index: int
    roller: str
    discards: dict[str, int] = field(default_factory=dict)
    discard_details: dict[str, dict[str, int]] = field(default_factory=dict)
    robber_tile: str | None = None
    robber_prob: int | None = None
    steal_victim: str | None = None
    steal_resource: str | None = None

    @property
    def total_discards(self) -> int:
        return sum(self.discards.values())


@dataclass
class HandDynamics:
    """Time-series hand stats per color, derived from the hand_tracker walk.

    `peak_size` is the max total cards (known + unknown) held at any point.
    `vulnerable_events` counts distinct hand-change samples where the total
    was 8 or more — a rough "how often were you exposed to discard-on-7"
    gauge. `final_drift` is the end-of-game overdraft-clamp count for this
    color — anything above ~3 means the reconstruction for that hand
    missed events and the other numbers are approximate.
    """
    peak_size: int = 0
    peak_event_index: int = -1
    vulnerable_events: int = 0
    final_drift: int = 0


@dataclass
class MoveAnnotation:
    """Chess-style quality glyph attached to a single decision in the log.

    `glyph` uses the chess-annotation vocabulary:
      !!  brilliant   — exceptional outcome (e.g. monopoly haul ≥ 5)
      !   good        — positive outcome
      !?  interesting — worth noting, slightly favorable
      ?!  dubious     — slightly bad; could have done better
      ?   mistake     — clearly negative outcome
      ??  blunder     — self-inflicted damage (e.g. rolled a 7 on a fat hand)

    These are *heuristic* and log-derived — we score each move on the
    observable consequences (what opps actually held for a monopoly,
    your own hand size when you rolled a 7, the trade-value delta),
    not on a rerun of recommend_actions. Still enough signal to spot
    the standout plays and mistakes at a glance.
    """
    event_index: int
    player: str
    color: str
    move_kind: str
    glyph: str
    summary: str
    note: str


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
    hand_dynamics: dict[str, HandDynamics] | None = None
    sevens: list[SevenImpact] = field(default_factory=list)
    trade_impacts: list[TradeImpact] = field(default_factory=list)
    move_annotations: list[MoveAnnotation] = field(default_factory=list)


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
    sevens: list[SevenImpact] = []
    trade_impacts: list[TradeImpact] = []
    # Per-username cumulative produce counts — snapshotted at each
    # player-to-player TradeCommitEvent to score trade quality.
    produced_so_far: dict[str, dict[str, int]] = {}
    # When the current roll is a 7, this points at the SevenImpact being
    # filled in. A new RollEvent closes the window (discards/robber/steal
    # attributed to that 7 must appear before the next roll).
    current_seven: SevenImpact | None = None

    for i, event in enumerate(events):
        if isinstance(event, RollEvent):
            histogram[event.total] += 1
            stats = _stats_for(stats_by_color, color_map, event.player)
            stats.rolls += 1
            # New roll closes any in-flight 7 window.
            if current_seven is not None:
                sevens.append(current_seven)
                current_seven = None
            if event.total == 7:
                stats.sevens += 1
                current_seven = SevenImpact(
                    event_index=i, roller=event.player,
                )
        elif isinstance(event, ProduceEvent):
            stats = _stats_for(stats_by_color, color_map, event.player)
            for res, n in event.resources.items():
                stats.produced[res] = stats.produced.get(res, 0) + n
            psf = produced_so_far.setdefault(event.player, {})
            for res, n in event.resources.items():
                psf[res] = psf.get(res, 0) + n
        elif isinstance(event, DiscardEvent):
            stats = _stats_for(stats_by_color, color_map, event.player)
            for res, n in event.resources.items():
                stats.discarded[res] = stats.discarded.get(res, 0) + n
            if current_seven is not None:
                total = sum(event.resources.values())
                current_seven.discards[event.player] = (
                    current_seven.discards.get(event.player, 0) + total
                )
                details = current_seven.discard_details.setdefault(
                    event.player, {},
                )
                for res, n in event.resources.items():
                    details[res] = details.get(res, 0) + n
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
            # A robber-triggered steal attaches to the in-flight 7. Only
            # the first one; a Knight-dev-card steal that happens to land
            # mid-7-window is rare but would otherwise overwrite this.
            if (current_seven is not None
                    and current_seven.steal_victim is None):
                current_seven.steal_victim = event.victim
                current_seven.steal_resource = event.resource
        elif isinstance(event, RobberMoveEvent):
            if current_seven is not None and current_seven.robber_tile is None:
                current_seven.robber_tile = event.tile_label
                current_seven.robber_prob = event.prob
        elif isinstance(event, NoStealEvent):
            # Mark the seven as "robber moved but nobody stole" so the
            # formatter can say so explicitly instead of leaving the
            # victim field blank-and-ambiguous.
            if (current_seven is not None
                    and current_seven.steal_victim is None):
                current_seven.steal_victim = ""
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
                giver_psf = produced_so_far.get(event.giver, {})
                recv_psf = produced_so_far.get(event.receiver, {})
                giver_d = _score_trade_delta(
                    gave=event.gave, got=event.got, produced=giver_psf,
                )
                recv_d = _score_trade_delta(
                    gave=event.got, got=event.gave, produced=recv_psf,
                )
                trade_impacts.append(TradeImpact(
                    event_index=i,
                    giver=event.giver, receiver=event.receiver,
                    gave=dict(event.gave), got=dict(event.got),
                    giver_delta=giver_d, receiver_delta=recv_d,
                ))
        elif isinstance(event, VPEvent):
            stats = _stats_for(stats_by_color, color_map, event.player)
            stats.vp_awards.append(event.reason)
        elif isinstance(event, GameOverEvent):
            winner_username = event.winner
            _stats_for(stats_by_color, color_map, event.winner)

    # Close the last in-flight 7 window (no RollEvent follows to flush it).
    if current_seven is not None:
        sevens.append(current_seven)

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
    hands, dynamics = _walk_hands_with_dynamics(events, color_map)
    annotations = _collect_move_annotations(events, trade_impacts, color_map)
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
        hand_dynamics=dynamics,
        sevens=sevens,
        trade_impacts=trade_impacts,
        move_annotations=annotations,
    )


def _score_trade_delta(
    gave: dict[str, int], got: dict[str, int], produced: dict[str, int],
) -> float:
    """Delta in marginal-value terms: got_value - gave_value for a
    player whose cumulative resource production so far is `produced`.

    Marginal = 1 / (0.5 + produced_count). Early in the game everything
    is scarce, so deltas are large; later produces compress the scale,
    which matches the intuition that a single brick is life-or-death
    turn 1 and nearly fungible turn 30.
    """
    def _marginal(res: str) -> float:
        return 1.0 / (0.5 + produced.get(res, 0))
    got_value = sum(_marginal(r) * n for r, n in got.items())
    gave_value = sum(_marginal(r) * n for r, n in gave.items())
    return got_value - gave_value


# First hand size that triggers discard on a 7 is DISCARD_LIMIT + 1
# (colonist rounds down, so "more than N" = N+1 cards). Resolved at
# call time from the live config so a mid-session change to a custom
# discard limit (e.g., Cities & Knights' 9-card rule) propagates here.
def _discard_threshold() -> int:
    from cataanbot.config import get_discard_limit
    return get_discard_limit() + 1


def _walk_hands_with_dynamics(
    events: list[Event], color_map: ColorMap,
) -> tuple[dict, dict[str, HandDynamics]]:
    """Run the event stream through hand_tracker and also track per-color
    peak size + discard-vulnerability counts along the way.

    Single pass so we don't double-walk. Peak is the max total (known +
    unknown) seen at any step; vulnerable_events counts distinct event
    samples where the total was ≥ 8 (the real discard threshold) — it's
    a loose proxy for "how often did the player sit on a big hand".
    """
    from cataanbot.hand_tracker import apply_event, init_hands
    hands = init_hands(color_map)
    dynamics = {c: HandDynamics() for c in hands}
    threshold = _discard_threshold()
    for i, event in enumerate(events):
        changed = apply_event(hands, event, color_map)
        if not changed:
            continue
        for color, hand in hands.items():
            total = hand.total
            d = dynamics[color]
            if total > d.peak_size:
                d.peak_size = total
                d.peak_event_index = i
            if total >= threshold:
                d.vulnerable_events += 1
    for color, hand in hands.items():
        dynamics[color].final_drift = hand.drift
    return hands, dynamics


def _score_monopoly_haul(haul: int) -> tuple[str, str]:
    if haul >= 5:
        return "!!", f"{haul}-card haul — huge swing"
    if haul >= 3:
        return "!", f"{haul}-card haul"
    if haul >= 1:
        return "?!", f"only {haul} card — thin value"
    return "??", "whiffed — opps held none"


def _score_trade_side(delta: float) -> tuple[str, str] | None:
    """Map a trade-delta to a glyph/note, or None if near-even.

    Uses the same 0.15 cutoff that _format_trade_impacts uses to suppress
    near-even noise, so a move annotation fires iff the trade was a
    visibly lopsided one from that side's perspective.
    """
    if delta >= 0.5:
        return "!!", f"got +{delta:.2f} scarcity delta"
    if delta >= 0.15:
        return "!", f"got +{delta:.2f}"
    if delta <= -0.5:
        return "??", f"gave up {-delta:.2f} scarcity delta"
    if delta <= -0.15:
        return "?", f"gave up {-delta:.2f}"
    return None


def _username_for(color_map: ColorMap, color: str) -> str:
    for user, c in color_map.as_dict().items():
        if c == color:
            return user
    return color


def _knight_outcome(
    events: list[Event], play_idx: int, hands_at_play: dict,
    color_map: ColorMap,
) -> tuple[str, str]:
    """Walk forward from a Knight play to find the steal outcome, then
    score based on what the steal did to opps at that moment.

    Returns a (glyph, note) tuple. We stop scanning at the next
    DevCardPlayEvent or RollEvent — a knight always resolves before
    the next action, so there's no ambiguity."""
    for j in range(play_idx + 1, len(events)):
        ev = events[j]
        if isinstance(ev, (DevCardPlayEvent, RollEvent)):
            break
        if isinstance(ev, NoStealEvent):
            return "?!", "robber moved but nothing to steal"
        if isinstance(ev, StealEvent):
            victim_color = color_map.get(ev.victim) or ""
            victim_size = (
                hands_at_play[victim_color].total
                if victim_color in hands_at_play else 0
            )
            if victim_size >= 8:
                return "!!", f"stole from fat opp ({victim_size} cards)"
            if victim_size >= 4:
                return "!", f"stole from {ev.victim} ({victim_size} cards)"
            return "?!", f"stole from thin hand ({victim_size} cards)"
    return "?!", "knight with no resolved outcome"


def _collect_move_annotations(
    events: list[Event],
    trade_impacts: list[TradeImpact],
    color_map: ColorMap,
) -> list[MoveAnnotation]:
    """Heuristic per-move quality ratings for the postmortem.

    Walks the event stream alongside a live hand_tracker replay so
    each RollEvent / MonopolyStealEvent / TradeCommitEvent / knight
    play can be scored against the actual observable state at that
    moment — no catanatron state reconstruction required.
    """
    from cataanbot.hand_tracker import apply_event, init_hands
    hands = init_hands(color_map)
    out: list[MoveAnnotation] = []
    trade_by_idx = {t.event_index: t for t in trade_impacts}

    for i, event in enumerate(events):
        if isinstance(event, MonopolyStealEvent):
            glyph, note = _score_monopoly_haul(event.count)
            color = color_map.get(event.player) or ""
            out.append(MoveAnnotation(
                event_index=i,
                player=event.player,
                color=color,
                move_kind="monopoly",
                glyph=glyph,
                summary=f"Monopoly {event.resource} → {event.count}",
                note=note,
            ))
        elif (isinstance(event, DevCardPlayEvent)
                and event.card == "knight"):
            color = color_map.get(event.player) or ""
            # Use a shallow snapshot of hand sizes at the moment of the
            # play so the outcome scorer sees the victim's hand size
            # *before* the steal has applied.
            snapshot = {
                c: type(h)(color=h.color, cards=dict(h.cards),
                           unknown=h.unknown, drift=h.drift)
                for c, h in hands.items()
            }
            glyph, note = _knight_outcome(
                events, i, snapshot, color_map,
            )
            out.append(MoveAnnotation(
                event_index=i,
                player=event.player,
                color=color,
                move_kind="knight",
                glyph=glyph,
                summary="played Knight",
                note=note,
            ))
        elif isinstance(event, RollEvent) and event.total == 7:
            color = color_map.get(event.player) or ""
            my_total = hands[color].total if color in hands else 0
            # Self-blunder: rolling a 7 while you're sitting on 8+ cards
            # forces your own discard. Small hands mean the 7 is a free
            # shot at opps, so we only flag the own-goal case.
            if my_total >= 8:
                out.append(MoveAnnotation(
                    event_index=i,
                    player=event.player,
                    color=color,
                    move_kind="rolled_7",
                    glyph="??",
                    summary=f"rolled 7 holding {my_total} cards",
                    note="self-discard blunder",
                ))
            else:
                # Opposite case — you rolled a 7 with a light hand while
                # an opp was sitting fat. Free damage, worth flagging.
                opp_fat = [
                    (c, h.total)
                    for c, h in hands.items()
                    if c != color and h.total >= 8
                ]
                if opp_fat:
                    biggest = max(opp_fat, key=lambda kv: kv[1])
                    out.append(MoveAnnotation(
                        event_index=i,
                        player=event.player,
                        color=color,
                        move_kind="rolled_7",
                        glyph="!!",
                        summary=f"rolled 7 vs fat opp ({biggest[1]} cards)",
                        note="free discard damage",
                    ))
            # Also annotate anyone ELSE carrying a fat hand into the
            # 7-roll — they chose not to spend down and now have to
            # discard half. This is about the victim's prior bad
            # planning, not the roller's play.
            for c, h in hands.items():
                if c == color or h.total < 8:
                    continue
                victim_user = _username_for(color_map, c)
                out.append(MoveAnnotation(
                    event_index=i,
                    player=victim_user,
                    color=c,
                    move_kind="fat_hand",
                    glyph="?",
                    summary=f"caught holding {h.total} cards",
                    note="should have spent down before a 7",
                ))
        elif isinstance(event, TradeCommitEvent) and event.receiver != "BANK":
            t = trade_by_idx.get(i)
            if t is not None:
                for player, delta in (
                    (t.giver, t.giver_delta),
                    (t.receiver, t.receiver_delta),
                ):
                    scored = _score_trade_side(delta)
                    if scored is None:
                        continue
                    glyph, base_note = scored
                    color = color_map.get(player) or ""
                    counterparty = t.receiver if player == t.giver else t.giver
                    gave_pack = t.gave if player == t.giver else t.got
                    got_pack = t.got if player == t.giver else t.gave
                    swap = (f"{_fmt_res_counter(gave_pack)} → "
                            f"{_fmt_res_counter(got_pack)}")
                    out.append(MoveAnnotation(
                        event_index=i,
                        player=player,
                        color=color,
                        move_kind="trade",
                        glyph=glyph,
                        summary=f"trade w/ {counterparty}: {swap}",
                        note=base_note,
                    ))
        apply_event(hands, event, color_map)
    return out


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
    lines.extend(_format_hand_dynamics(report))
    lines.append("")
    lines.extend(_format_seven_impacts(report))
    lines.append("")
    lines.extend(_format_trade_impacts(report))
    lines.append("")
    lines.extend(_format_move_annotations(report))
    lines.append("")
    lines.extend(_format_dispatch_quality(report))
    return "\n".join(lines)


def _format_move_annotations(report: ReplayReport) -> list[str]:
    """Chess-style move annotations section — shows glyph + summary per
    flagged decision, grouped by player so you can scan each side's
    standout plays and mistakes at a glance.

    Only surfaces annotated moves (no noise rows for neutral plays);
    if a player had no flagged moves they're omitted entirely.
    """
    lines = ["Move annotations (heuristic; !=good ?=mistake):"]
    if not report.move_annotations:
        lines.append("  (no flagged moves)")
        return lines
    by_color: dict[str, list[MoveAnnotation]] = {}
    for a in report.move_annotations:
        by_color.setdefault(a.color, []).append(a)
    players = _players_in_color_order(report.players)
    any_shown = False
    for color, stats in players:
        anns = by_color.get(color)
        if not anns:
            continue
        any_shown = True
        lines.append(f"  {stats.username} ({color}):")
        for a in anns:
            lines.append(
                f"    {a.glyph:<3} #{a.event_index:<4}  "
                f"{a.summary:<36}  {a.note}"
            )
    if not any_shown:
        lines.append("  (no flagged moves)")
    return lines


def _format_reconstructed_hands(report: ReplayReport) -> list[str]:
    if report.reconstructed_hands is None or report.color_map is None:
        return []
    from cataanbot.hand_tracker import format_hands_table
    return format_hands_table(report.reconstructed_hands, report.color_map)


def _format_trade_impacts(report: ReplayReport) -> list[str]:
    """Rank player-to-player trades by how lopsided they looked at the
    moment they happened. Uses cumulative-produce-to-date as the scarcity
    proxy, so the scoring adapts to each player's board reality without
    needing topology.

    Surfaces the top 8 most lopsided; omits near-even trades entirely
    (|delta| < 0.15 for both sides) since those aren't interesting.
    """
    lines = ["Trade quality (delta from each side's perspective):"]
    if not report.trade_impacts:
        lines.append("  (no player-to-player trades in log)")
        return lines
    scored = [
        t for t in report.trade_impacts
        if abs(t.giver_delta) >= 0.15 or abs(t.receiver_delta) >= 0.15
    ]
    if not scored:
        lines.append("  (all trades were roughly even given production so far)")
        return lines
    scored.sort(key=lambda t: max(
        abs(t.giver_delta), abs(t.receiver_delta),
    ), reverse=True)
    shown = scored[:8]
    header = (
        f"  {'#':>3}  {'event':>6}  {'giver':<12} {'gave':<20} "
        f"{'got':<20} {'giver Δ':>8}  {'receiver':<12} {'recv Δ':>7}"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for idx, t in enumerate(shown, start=1):
        gave_str = _fmt_res_counter(t.gave)
        got_str = _fmt_res_counter(t.got)
        lines.append(
            f"  {idx:>3}  #{t.event_index:>5}  "
            f"{t.giver:<12} {gave_str:<20} {got_str:<20} "
            f"{t.giver_delta:>+8.2f}  {t.receiver:<12} "
            f"{t.receiver_delta:>+7.2f}"
        )
    if len(scored) > len(shown):
        lines.append(
            f"  (+ {len(scored) - len(shown)} more lopsided trades not shown)"
        )
    lines.append("")
    lines.append(
        "  Δ > 0 = that side came out ahead given their scarcity at the time; "
        "|Δ| < 0.15 hidden"
    )
    return lines


def _format_seven_impacts(report: ReplayReport) -> list[str]:
    """Per-7-roll damage summary: roller, discards, robber tile, steal.

    Shows the most costly 7s first (by total cards discarded + 1 for a
    successful steal). Caps at 10 rows so a discard-heavy game doesn't
    blow up the report — the full list lives in `report.sevens` for
    downstream consumers who want it.
    """
    lines = ["7-roll impacts:"]
    if not report.sevens:
        lines.append("  (no 7s in log)")
        return lines

    def _cost(s: SevenImpact) -> int:
        return s.total_discards + (1 if s.steal_victim else 0)

    ranked = sorted(report.sevens, key=_cost, reverse=True)
    shown = ranked[:10]
    header = (
        f"  {'#':>3}  {'event':>6}  {'roller':<14}  {'discards':<28}  "
        f"{'tile':<15}  steal"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for idx, s in enumerate(shown, start=1):
        if s.discards:
            parts = [
                f"{user} {n}"
                for user, n in sorted(
                    s.discards.items(), key=lambda kv: -kv[1],
                )
            ]
            discard_str = ", ".join(parts)
        else:
            discard_str = "—"
        tile = s.robber_tile or "?"
        if s.robber_prob is not None:
            tile = f"{tile} ({s.robber_prob})"
        if s.steal_victim == "":
            steal_str = "(no target)"
        elif s.steal_victim:
            res = s.steal_resource or "?"
            steal_str = f"from {s.steal_victim} ({res})"
        else:
            steal_str = "—"
        lines.append(
            f"  {idx:>3}  #{s.event_index:>5}  {s.roller:<14}  "
            f"{discard_str:<28}  {tile:<15}  {steal_str}"
        )
    if len(ranked) > len(shown):
        lines.append(f"  (+ {len(ranked) - len(shown)} more 7s not shown)")
    return lines


def _format_hand_dynamics(report: ReplayReport) -> list[str]:
    if not report.hand_dynamics or report.color_map is None:
        return []
    users = {c: u for u, c in report.color_map.as_dict().items()}
    players = _players_in_color_order(report.players)
    if not players:
        return []
    name_w = max((len(s.username) for _, s in players), default=8)
    name_w = max(name_w, 6)
    header = (
        f"  {'player':<{name_w}}  "
        f"{'peak':>4}  {'at event':>9}  "
        f"{'8+ events':>10}  {'drift':>5}"
    )
    lines = [
        "Hand dynamics (from the event-stream reconstruction):",
        "",
        header,
        "  " + "-" * (len(header) - 2),
    ]
    for color, stats in players:
        d = report.hand_dynamics.get(color)
        if d is None:
            continue
        user = users.get(color, stats.username)
        peak_idx = (
            f"#{d.peak_event_index}" if d.peak_event_index >= 0 else "—"
        )
        lines.append(
            f"  {user:<{name_w}}  "
            f"{d.peak_size:>4}  {peak_idx:>9}  "
            f"{d.vulnerable_events:>10}  {d.final_drift:>5}"
        )
    lines.append("")
    lines.append(
        "  peak = max total cards held; 8+ events = hand-change samples at "
        "8+ cards"
    )
    lines.append(
        "  (a 7 roll at 8+ forces a discard); drift ≥3 means the hand for "
        "that color is approximate"
    )
    return lines


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
