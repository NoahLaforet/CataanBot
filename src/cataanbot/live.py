"""Event → Tracker integration.

The parser (`parser.py`) turns colonist.io log payloads into structured
Events (`events.py`). The Tracker (`tracker.py`) exposes board / hand /
dev-card mutations keyed on catanatron colors. This module bridges them:

- `ColorMap` — maps colonist usernames to catanatron colors. Auto-assigns
  from `DEFAULT_COLORS` in order of first appearance unless a manual
  mapping is provided.
- `apply_event(tracker, color_map, event)` — dispatches an Event to the
  right Tracker call, returning a `DispatchResult` that says whether it
  was applied, skipped, unhandled, or errored.

Events that need board topology (building placements, robber tile coords)
are returned as UNHANDLED for now — we'll wire those up once the
colonist-DOM-to-catanatron-node mapping is built. Everything else (rolls,
produces, discards, trades, dev cards, monopoly, VP, steals with revealed
resource, game over) drives real tracker state.
"""
from __future__ import annotations

from dataclasses import dataclass

from cataanbot.events import (
    BuildEvent,
    DevCardBuyEvent,
    DevCardPlayEvent,
    DiscardEvent,
    DisconnectEvent,
    Event,
    GameOverEvent,
    HandSyncEvent,
    InfoEvent,
    MonopolyStealEvent,
    NoStealEvent,
    ProduceEvent,
    RobberMoveEvent,
    RollBlockedEvent,
    RollEvent,
    StealEvent,
    TradeCommitEvent,
    TradeOfferEvent,
    UnknownEvent,
    VPEvent,
)
from cataanbot.tracker import DEFAULT_COLORS, Tracker, TrackerError


class ColorMapError(ValueError):
    """Raised when a username/color mapping is inconsistent or exhausted."""


class ColorMap:
    """Bidirectional colonist-username ↔ catanatron-color map.

    First-appearance auto-assignment matches catanatron's default seat
    order (RED, BLUE, WHITE, ORANGE). Override explicitly via `add()` or
    the constructor when you know the real turn order up front."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._user_to_color: dict[str, str] = {}
        if mapping:
            for user, color in mapping.items():
                self.add(user, color)

    def add(self, username: str, color: str) -> None:
        color = color.upper()
        if color not in DEFAULT_COLORS:
            raise ColorMapError(
                f"unknown color {color!r}; use one of "
                f"{', '.join(DEFAULT_COLORS)}"
            )
        existing = self._user_to_color.get(username)
        if existing is not None and existing != color:
            raise ColorMapError(
                f"{username!r} already mapped to {existing}; "
                f"can't remap to {color}"
            )
        taken_by = next(
            (u for u, c in self._user_to_color.items() if c == color), None,
        )
        if taken_by is not None and taken_by != username:
            raise ColorMapError(
                f"color {color} already taken by {taken_by!r}"
            )
        self._user_to_color[username] = color

    def get(self, username: str) -> str:
        """Catanatron color for `username`, auto-assigning if first seen."""
        if username not in self._user_to_color:
            used = set(self._user_to_color.values())
            available = [c for c in DEFAULT_COLORS if c not in used]
            if not available:
                raise ColorMapError(
                    f"no colors left to assign to {username!r} "
                    f"(already mapped: {self._user_to_color})"
                )
            self._user_to_color[username] = available[0]
        return self._user_to_color[username]

    def has(self, username: str) -> bool:
        return username in self._user_to_color

    def reverse(self, color: str) -> str | None:
        color = color.upper()
        for u, c in self._user_to_color.items():
            if c == color:
                return u
        return None

    def as_dict(self) -> dict[str, str]:
        return dict(self._user_to_color)

    def __repr__(self) -> str:
        return f"ColorMap({self._user_to_color})"


@dataclass
class DispatchResult:
    """Outcome of dispatching one Event to the Tracker."""
    event: Event
    status: str  # 'applied' | 'skipped' | 'unhandled' | 'error'
    message: str = ""


# parser.py emits lowercase card names; tracker expects canonical uppercase.
_DEV_CARD_MAP = {
    "knight": "KNIGHT",
    "monopoly": "MONOPOLY",
    "year_of_plenty": "YEAR_OF_PLENTY",
    "road_building": "ROAD_BUILDING",
    "vp": "VICTORY_POINT",
}

# VPEvent.reason → the boolean column on player_state to flip.
_VP_REASON_TO_FLAG = {
    "longest_road": "HAS_ROAD",
    "largest_army": "HAS_ARMY",
}


def apply_event(
    tracker: Tracker, color_map: ColorMap, event: Event,
) -> DispatchResult:
    """Dispatch one Event to tracker mutations and report the outcome."""
    try:
        return _dispatch(tracker, color_map, event)
    except (TrackerError, ColorMapError, ValueError) as e:
        return DispatchResult(event, "error", str(e))


def _dispatch(
    tracker: Tracker, color_map: ColorMap, event: Event,
) -> DispatchResult:
    if isinstance(event, RollEvent):
        # The roll total is informational here — the per-color payouts
        # arrive as ProduceEvents right after. Calling tracker.roll()
        # would re-run catanatron's yield_resources against the (empty)
        # board and produce zero, or double-count if the board ever
        # gets placements filled in.
        return DispatchResult(
            event, "skipped",
            f"roll {event.total} — payouts via ProduceEvent",
        )

    if isinstance(event, ProduceEvent):
        color = color_map.get(event.player)
        for res, n in event.resources.items():
            tracker.give(color, n, res)
        return DispatchResult(
            event, "applied",
            f"{color} +{_fmt_res(event.resources)}",
        )

    if isinstance(event, DiscardEvent):
        color = color_map.get(event.player)
        for res, n in event.resources.items():
            tracker.take(color, n, res)
        return DispatchResult(
            event, "applied",
            f"{color} -{_fmt_res(event.resources)}",
        )

    if isinstance(event, TradeCommitEvent):
        return _apply_trade_commit(tracker, color_map, event)

    if isinstance(event, StealEvent):
        thief = color_map.get(event.thief)
        victim = color_map.get(event.victim)
        if event.resource:
            tracker.take(victim, 1, event.resource)
            tracker.give(thief, 1, event.resource)
            return DispatchResult(
                event, "applied",
                f"{thief} stole 1 {event.resource} from {victim}",
            )
        return DispatchResult(
            event, "unhandled",
            f"{thief} stole from {victim} (resource unknown — "
            f"third-party steal; needs hand inference)",
        )

    if isinstance(event, DevCardBuyEvent):
        color = color_map.get(event.player)
        # Debit the cost. Colonist doesn't reveal the card type on buy,
        # only on play, so we intentionally don't record a specific
        # {TYPE}_IN_HAND counter yet.
        for res in ("WHEAT", "SHEEP", "ORE"):
            tracker.take(color, 1, res)
        return DispatchResult(
            event, "applied",
            f"{color} bought dev card (cost debited; type unknown "
            f"until play)",
        )

    if isinstance(event, DevCardPlayEvent):
        return _apply_devcard_play(tracker, color_map, event)

    if isinstance(event, MonopolyStealEvent):
        return _apply_monopoly(tracker, color_map, event)

    if isinstance(event, VPEvent):
        return _apply_vp(tracker, color_map, event)

    if isinstance(event, BuildEvent):
        return _apply_build(tracker, color_map, event)

    if isinstance(event, HandSyncEvent):
        color = color_map.get(event.player)
        tracker.set_hand(color, event.resources)
        return DispatchResult(
            event, "applied",
            f"{color} hand sync → {_fmt_res(event.resources) or '∅'}",
        )

    if isinstance(event, RobberMoveEvent):
        return _apply_robber_move(tracker, color_map, event)

    if isinstance(
        event,
        (RollBlockedEvent, InfoEvent, DisconnectEvent,
         NoStealEvent, TradeOfferEvent),
    ):
        return DispatchResult(event, "skipped", "informational")

    if isinstance(event, GameOverEvent):
        return DispatchResult(
            event, "applied", f"GAME OVER — {event.winner} won",
        )

    if isinstance(event, UnknownEvent):
        return DispatchResult(event, "unhandled", "unknown event type")

    return DispatchResult(
        event, "unhandled",
        f"no dispatcher for {type(event).__name__}",
    )


def _apply_trade_commit(
    tracker: Tracker, color_map: ColorMap, event: TradeCommitEvent,
) -> DispatchResult:
    giver = color_map.get(event.giver)
    if event.receiver == "BANK":
        # Only one player moves cards; the bank absorbs the difference
        # automatically via tracker's give/take (they debit the freqdeck).
        for res, n in event.gave.items():
            tracker.take(giver, n, res)
        for res, n in event.got.items():
            tracker.give(giver, n, res)
        return DispatchResult(
            event, "applied",
            f"{giver} bank: {_fmt_res(event.gave)} "
            f"→ {_fmt_res(event.got)}",
        )

    receiver = color_map.get(event.receiver)
    # Swap resources hand-to-hand. tracker.give/take also touch the
    # bank freqdeck, but a matched pair (take A + give B) nets to zero
    # for the bank, so this is safe.
    for res, n in event.gave.items():
        tracker.take(giver, n, res)
        tracker.give(receiver, n, res)
    for res, n in event.got.items():
        tracker.take(receiver, n, res)
        tracker.give(giver, n, res)
    return DispatchResult(
        event, "applied",
        f"{giver} ⇄ {receiver}: {_fmt_res(event.gave)} "
        f"for {_fmt_res(event.got)}",
    )


def _apply_devcard_play(
    tracker: Tracker, color_map: ColorMap, event: DevCardPlayEvent,
) -> DispatchResult:
    color = color_map.get(event.player)
    canonical = _DEV_CARD_MAP.get(event.card)
    if canonical is None:
        return DispatchResult(
            event, "unhandled",
            f"unknown dev card type {event.card!r}",
        )
    # Because DevCardBuyEvent doesn't record a specific hand counter
    # (card type is hidden), we have to seed the hand here before play
    # so devplay doesn't fail. Net effect: buy and play cancel out on
    # the IN_HAND column, but PLAYED_{type} increments honestly —
    # which is what largest-army and advisor heuristics actually read.
    tracker.devbuy(color, canonical)
    tracker.devplay(color, canonical)
    if canonical == "YEAR_OF_PLENTY" and event.resources:
        for res, n in event.resources.items():
            tracker.give(color, n, res)
        extra = f" + picked {_fmt_res(event.resources)}"
    else:
        extra = ""
    return DispatchResult(
        event, "applied",
        f"{color} played {canonical.lower()}{extra}",
    )


def _apply_monopoly(
    tracker: Tracker, color_map: ColorMap, event: MonopolyStealEvent,
) -> DispatchResult:
    claimer = color_map.get(event.player)
    resource = event.resource
    remaining = event.count
    transferred = 0
    for _username, opp_color in color_map.as_dict().items():
        if opp_color == claimer:
            continue
        if remaining <= 0:
            break
        have = tracker.hand(opp_color).get(resource, 0)
        take_n = min(have, remaining)
        if take_n > 0:
            tracker.take(opp_color, take_n, resource)
            tracker.give(claimer, take_n, resource)
            transferred += take_n
            remaining -= take_n
    msg = f"{claimer} monopolied {transferred}x{resource}"
    if remaining > 0:
        # Opponents' tracked hands were short of what colonist says they
        # actually had — usually because we missed an earlier event
        # (steal with hidden resource, un-wired placement, etc.).
        msg += (f" (event reported {event.count}; "
                f"tracker short {remaining})")
    return DispatchResult(event, "applied", msg)


def _apply_build(
    tracker: Tracker, color_map: ColorMap, event: BuildEvent,
) -> DispatchResult:
    # Register the player either way so the color map is stable.
    color = color_map.get(event.player)

    if event.piece == "settlement" and event.node_id is not None:
        tracker.settle(color, event.node_id)
        return DispatchResult(
            event, "applied",
            f"{color} settled node {event.node_id}",
        )
    if event.piece == "city" and event.node_id is not None:
        tracker.city(color, event.node_id)
        return DispatchResult(
            event, "applied",
            f"{color} upgraded to city at node {event.node_id}",
        )
    if event.piece == "road" and event.edge_nodes is not None:
        a, b = event.edge_nodes
        tracker.road(color, a, b)
        return DispatchResult(
            event, "applied",
            f"{color} road {a}-{b}",
        )
    return DispatchResult(
        event, "unhandled",
        f"{event.player} built {event.piece} "
        f"— needs board topology to pick a node/edge",
    )


def _apply_robber_move(
    tracker: Tracker, color_map: ColorMap, event: RobberMoveEvent,
) -> DispatchResult:
    if event.player:
        color_map.get(event.player)
    if event.coord is not None:
        tracker.move_robber(event.coord)
        who = color_map.reverse(color_map.get(event.player)) if event.player \
            else "?"
        return DispatchResult(
            event, "applied",
            f"{who or event.player} moved robber → {event.coord}",
        )
    return DispatchResult(
        event, "unhandled",
        f"{event.player} moved robber → {event.tile_label} "
        f"— needs topology to resolve hex coord",
    )


def _apply_vp(
    tracker: Tracker, color_map: ColorMap, event: VPEvent,
) -> DispatchResult:
    flag = _VP_REASON_TO_FLAG.get(event.reason)
    if flag is None:
        return DispatchResult(
            event, "unhandled", f"unknown VP reason {event.reason!r}",
        )
    new_color = color_map.get(event.player)
    state = tracker.game.state
    if event.previous_holder:
        prev_color = color_map.get(event.previous_holder)
        prev_idx = state.color_to_index[tracker._color(prev_color)]
        state.player_state[f"P{prev_idx}_{flag}"] = False
    new_idx = state.color_to_index[tracker._color(new_color)]
    state.player_state[f"P{new_idx}_{flag}"] = True
    tracker._recompute_vp()
    src = f" (from {event.previous_holder})" if event.previous_holder else ""
    return DispatchResult(
        event, "applied",
        f"{new_color} got {event.reason}{src}",
    )


def _fmt_res(resources: dict[str, int]) -> str:
    if not resources:
        return "∅"
    return " ".join(f"{n}x{r}" for r, n in resources.items())
