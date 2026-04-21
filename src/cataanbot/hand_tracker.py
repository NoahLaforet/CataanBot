"""Per-player resource-hand tracker driven by the parsed event stream.

Walks an Event list in order and maintains each seated player's
resource hand — `WOOD`, `BRICK`, `SHEEP`, `WHEAT`, `ORE` — by debiting
build / dev-card / trade costs and crediting produces, year-of-plenty
grants, monopoly hauls, and known steals. The thing this has to handle
that a naïve tracker doesn't:

* **Unknown-resource steals.** When a third-party steal happens (a
  steal between two opponents we're observing), colonist's log doesn't
  reveal which resource changed hands. We model that as an `unknown`
  bucket on each hand: the victim loses one card that we "blur" across
  their real resources (subtracting it when they next have to pay for
  something and the known pile can't cover it), and the thief gains
  one `unknown` card that resolves into a real resource the first time
  they spend something their known pile can't afford.

* **Overdrafts.** Even with unknown steals, our reconstruction will
  sometimes go into the red on a resource — the event stream is
  incomplete (we don't see everything on the colonist side, e.g. dev
  cards bought but never played, building costs paid in a way the log
  paraphrases). Overdrafts get clamped to zero and the running `drift`
  counter increments, so callers can flag hands as "approximate" when
  drift is non-trivial without the tracker crashing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from cataanbot.events import (
    BuildEvent, DevCardBuyEvent, DevCardPlayEvent, DiscardEvent, Event,
    MonopolyStealEvent, ProduceEvent, StealEvent, TradeCommitEvent,
)
from cataanbot.live import ColorMap


_RESOURCES = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")

_BUILD_COSTS: dict[str, dict[str, int]] = {
    "settlement": {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1},
    "city":       {"WHEAT": 2, "ORE": 3},
    "road":       {"WOOD": 1, "BRICK": 1},
}

_DEV_BUY_COST: dict[str, int] = {"SHEEP": 1, "WHEAT": 1, "ORE": 1}


@dataclass
class HandState:
    """One player's hand as inferred from the event stream.

    `cards` is the known per-resource count. `unknown` is the number of
    cards received from third-party (hidden) steals we haven't yet
    resolved into a specific resource. `drift` counts how many times we
    tried to debit more of a resource than we thought the player had —
    a rough "this hand is approximate" signal.
    """
    color: str
    cards: dict[str, int] = field(
        default_factory=lambda: {r: 0 for r in _RESOURCES}
    )
    unknown: int = 0
    drift: int = 0

    @property
    def total(self) -> int:
        return sum(self.cards.values()) + self.unknown

    def copy(self) -> "HandState":
        return HandState(
            color=self.color,
            cards=dict(self.cards),
            unknown=self.unknown,
            drift=self.drift,
        )


def _add(hand: HandState, resource: str, amount: int) -> None:
    if resource not in hand.cards:
        return
    hand.cards[resource] += amount


def _debit(hand: HandState, resource: str, amount: int) -> None:
    """Subtract `amount` of `resource`. Overdraft is absorbed by the
    unknown bucket when possible (an unknown-steal card that we now
    know was this resource), else it's clamped to zero and `drift` is
    bumped so the caller knows the hand is getting out of sync.
    """
    if resource not in hand.cards or amount <= 0:
        return
    have = hand.cards[resource]
    if have >= amount:
        hand.cards[resource] -= amount
        return
    short = amount - have
    hand.cards[resource] = 0
    if hand.unknown >= short:
        # The unknown-steal card(s) we were holding turn out to be this
        # resource — resolve them and move on.
        hand.unknown -= short
    else:
        # Still short after absorbing all unknowns — event stream gap.
        # Clamp and flag.
        remaining_short = short - hand.unknown
        hand.unknown = 0
        hand.drift += remaining_short


def _seated_colors(color_map: ColorMap) -> list[str]:
    return list(color_map.as_dict().values())


def init_hands(color_map: ColorMap) -> dict[str, HandState]:
    """Zero-initialized hand state for every seated color."""
    return {c: HandState(color=c) for c in _seated_colors(color_map)}


def _lookup(hands: dict[str, HandState], color_map: ColorMap,
            username: str | None) -> HandState | None:
    """Resolve a log-name to a seated HandState, or None.

    Returns None for BANK / non-seated entries without auto-assigning
    them into the ColorMap (which would crash once 4 real players are
    seated). We still fall through to color_map.get for real usernames
    because colonist spellings can shift case or spacing.
    """
    if not username or username == "BANK":
        return None
    if not color_map.has(username):
        # Try exact match via direct lookup; if the player isn't seated
        # yet we fall through to the auto-assigning get, which raises
        # when the table is full — in that case we give up.
        try:
            color = color_map.get(username)
        except Exception:
            return None
    else:
        color = color_map.get(username)
    return hands.get(color)


def apply_event(
    hands: dict[str, HandState],
    event: Event,
    color_map: ColorMap,
) -> bool:
    """Apply one event to the hand state in-place.

    Returns True if the event changed any hand (useful for the timeline
    builder to decide whether to emit a sample), False otherwise.
    """
    if isinstance(event, ProduceEvent):
        hand = _lookup(hands, color_map, event.player)
        if hand is None or not event.resources:
            return False
        for res, n in event.resources.items():
            _add(hand, res, n)
        return True

    if isinstance(event, DiscardEvent):
        hand = _lookup(hands, color_map, event.player)
        if hand is None or not event.resources:
            return False
        for res, n in event.resources.items():
            _debit(hand, res, n)
        return True

    if isinstance(event, BuildEvent):
        hand = _lookup(hands, color_map, event.player)
        if hand is None:
            return False
        # Free placements (setup-phase settlements/roads, Road Building
        # dev card) don't debit. `paid=False` skips the cost but we still
        # return True so the timeline treats the event as meaningful.
        if not event.paid:
            return False
        cost = _BUILD_COSTS.get(event.piece)
        if not cost:
            return False
        for res, n in cost.items():
            _debit(hand, res, n)
        return True

    if isinstance(event, DevCardBuyEvent):
        hand = _lookup(hands, color_map, event.player)
        if hand is None:
            return False
        for res, n in _DEV_BUY_COST.items():
            _debit(hand, res, n)
        return True

    if isinstance(event, DevCardPlayEvent):
        hand = _lookup(hands, color_map, event.player)
        if hand is None:
            return False
        # Year of Plenty grants the two resources from the bank.
        if event.card == "year_of_plenty" and event.resources:
            for res, n in event.resources.items():
                _add(hand, res, n)
            return True
        # Monopoly / Knight / Road-Building etc. have no hand delta here;
        # the follow-up MonopolyStealEvent / StealEvent / BuildEvents
        # carry the actual card movement.
        return False

    if isinstance(event, MonopolyStealEvent):
        hand = _lookup(hands, color_map, event.player)
        if hand is None:
            return False
        thief_color = hand.color
        # Monopoly pulls every copy of `resource` from every opponent.
        # Our per-opponent count may be off (unknown steals earlier),
        # but we trust the log's total `count` as authoritative.
        for opp_color, opp_hand in hands.items():
            if opp_color == thief_color:
                continue
            had = opp_hand.cards.get(event.resource, 0)
            if had > 0:
                opp_hand.cards[event.resource] = 0
        _add(hand, event.resource, event.count)
        return True

    if isinstance(event, StealEvent):
        thief = _lookup(hands, color_map, event.thief)
        victim = _lookup(hands, color_map, event.victim)
        if thief is None or victim is None:
            return False
        if event.resource is not None:
            _debit(victim, event.resource, 1)
            _add(thief, event.resource, 1)
            return True
        # Unknown-resource (third-party) steal. Move 1 card of unknown
        # type: the victim's hand size drops by 1 regardless, and the
        # thief's goes up by 1. We can't pinpoint which resource moved,
        # so we debit the victim's unknown bucket first if they have one
        # (most specific), otherwise pull from whatever resource they
        # have the most of (weighted-average-ish best guess), and the
        # thief picks up an unknown card that will resolve on next spend.
        if victim.unknown > 0:
            victim.unknown -= 1
        else:
            best_res = max(
                _RESOURCES, key=lambda r: victim.cards.get(r, 0),
            )
            if victim.cards.get(best_res, 0) > 0:
                victim.cards[best_res] -= 1
            else:
                # Victim has nothing we know about; drift.
                victim.drift += 1
        thief.unknown += 1
        return True

    if isinstance(event, TradeCommitEvent):
        giver = _lookup(hands, color_map, event.giver)
        receiver = _lookup(hands, color_map, event.receiver)
        # Bank trades have BANK as one side — apply only to the real
        # seated player, skip the phantom bank side. If neither side is
        # seated there's nothing to track.
        if giver is None and receiver is None:
            return False
        for res, n in event.gave.items():
            if giver is not None:
                _debit(giver, res, n)
            if receiver is not None:
                _add(receiver, res, n)
        for res, n in event.got.items():
            if receiver is not None:
                _debit(receiver, res, n)
            if giver is not None:
                _add(giver, res, n)
        return True

    return False


def reconstruct_hands(
    events: Iterable[Event],
    color_map: ColorMap,
) -> dict[str, HandState]:
    """Convenience: run the full event stream and return final hands."""
    hands = init_hands(color_map)
    for event in events:
        apply_event(hands, event, color_map)
    return hands


def format_hands_table(hands: dict[str, HandState], color_map: ColorMap) -> list[str]:
    """Pretty-print a per-color hand summary (used by the report)."""
    lines = ["Reconstructed hands (from event stream):", ""]
    users = {c: u for u, c in color_map.as_dict().items()}
    res_header = "".join(f"{r[:3]:>5}" for r in _RESOURCES)
    name_w = max((len(users.get(c, c)) for c in hands), default=8)
    name_w = max(name_w, 6)
    header = (
        f"  {'player':<{name_w}}  "
        f"{'color':<7}  {'total':>5}  {'?':>3}  |{res_header}  drift"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for color in color_map.as_dict().values():
        hand = hands.get(color)
        if hand is None:
            continue
        user = users.get(color, "?")
        cells = "".join(f"{hand.cards[r]:>5}" for r in _RESOURCES)
        lines.append(
            f"  {user:<{name_w}}  {color:<7}  {hand.total:>5}  "
            f"{hand.unknown:>3}  |{cells}  {hand.drift:>5}"
        )
    lines.append("")
    lines.append(
        "  ? = unresolved cards from third-party (hidden) steals"
    )
    lines.append(
        "  drift = times we had to clamp an overdraft (bigger = "
        "reconstruction is approximate)"
    )
    return lines
