"""Structured events parsed out of colonist.io log payloads.

The bridge receives a raw DOM serialization from the Tampermonkey
userscript; `parser.parse_event` turns each payload into one of the
dataclasses below, or an `UnknownEvent` if nothing matches.

All player references use colonist.io *usernames* — color mapping to
catanatron happens later in the color-map layer, not here. Resource
strings use the catanatron convention (WHEAT / WOOD / SHEEP / ORE /
BRICK), since that's where they ultimately land.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union


# Resources map from colonist.io alt text → catanatron canonical name.
# `Grain` is colonist.io's label for WHEAT; everything else is a direct
# rename. Cards we've seen in logs: Lumber, Brick, Wool, Grain, Ore.
COLONIST_TO_CATAN_RESOURCE = {
    "Lumber": "WOOD",
    "Brick":  "BRICK",
    "Wool":   "SHEEP",
    "Grain":  "WHEAT",
    "Ore":    "ORE",
}

RESOURCE_NAMES = set(COLONIST_TO_CATAN_RESOURCE.values())


@dataclass
class RollEvent:
    player: str
    d1: int
    d2: int

    @property
    def total(self) -> int:
        return self.d1 + self.d2


@dataclass
class ProduceEvent:
    """Player collected resources from a roll."""
    player: str
    resources: dict[str, int]  # catanatron resource name → count


@dataclass
class BuildEvent:
    player: str
    piece: str           # 'settlement' | 'road' | 'city'
    vp_delta: int = 0
    # True for normal mid-game builds ("X built a ..." — debit resources).
    # False for free placements: setup-phase settlements/roads and the two
    # roads placed by the Road Building dev card ("X placed a ..."). The
    # hand tracker skips the cost debit when paid=False; VP still counts.
    paid: bool = True
    # Catanatron topology — filled when parsed from a WS diff (not the
    # DOM log, which doesn't carry coords). When set, the dispatcher can
    # resolve the placement directly; otherwise it stays 'unhandled'.
    node_id: int | None = None
    edge_nodes: tuple[int, int] | None = None


@dataclass
class DiscardEvent:
    """7-roll forced discard."""
    player: str
    resources: dict[str, int]


@dataclass
class RobberMoveEvent:
    player: str
    tile_label: str       # e.g. 'lumber tile', 'Desert', 'ore tile'
    prob: int | None      # red number on the tile; None for desert
    # Catanatron cube coord for the tile — set by the WS layer when the
    # topology mapping resolves colonist's tileIndex. Absent for DOM
    # parses, which only get a human label.
    coord: tuple[int, int, int] | None = None


@dataclass
class StealEvent:
    thief: str
    victim: str
    # Filled in when the log reveals the resource — happens when the
    # *current user* is either the thief or the victim. Stays None for
    # third-party steals, which we'll infer from hands later.
    resource: str | None = None


@dataclass
class NoStealEvent:
    """Robber moved somewhere with no adjacent opponents to rob."""


@dataclass
class TradeOfferEvent:
    """Player is asking the table for a trade; not yet accepted."""
    player: str
    give: dict[str, int]
    want: dict[str, int]  # may be {} if the offer is open-ended


@dataclass
class TradeCommitEvent:
    """A completed player-to-player or port/bank trade."""
    giver: str
    receiver: str
    gave: dict[str, int]
    got: dict[str, int]


@dataclass
class DevCardBuyEvent:
    player: str


@dataclass
class DevCardPlayEvent:
    player: str
    card: str            # 'knight' | 'road_building' | 'year_of_plenty' | 'monopoly' | 'vp' | 'unknown'
    # Year of Plenty fills `resources` with the two taken cards; Monopoly
    # fills `resource` with the claimed type. Everything else leaves them
    # empty.
    resources: dict[str, int] = field(default_factory=dict)
    resource: str | None = None


@dataclass
class MonopolyStealEvent:
    """Follow-up to a Monopoly dev card: total cards of a single resource
    pulled from every opponent. Colonist logs this as 'X stole N [res]'."""
    player: str
    resource: str
    count: int


@dataclass
class VPEvent:
    """Standalone VP callout (largest-army / longest-road / dev-card VP)."""
    player: str
    reason: str
    vp_delta: int
    # If this is a transfer (e.g. X took longest road from Y), the
    # previous holder who loses the bonus. None for first-time awards.
    previous_holder: str | None = None


@dataclass
class GameOverEvent:
    """End of game — someone won."""
    winner: str


@dataclass
class RollBlockedEvent:
    """A tile rolled its number but couldn't produce because the robber
    sits on it. No player attribution — the tile is the subject."""
    tile_label: str
    prob: int | None


@dataclass
class InfoEvent:
    """Skippable announcement — Friendly Robber active, bot is thinking, etc."""
    text: str


@dataclass
class DisconnectEvent:
    player: str
    reconnected: bool = False


@dataclass
class HandSyncEvent:
    """Ground-truth resource hand for one player, sourced from the WS
    `playerStates.{cid}.resourceCards` snapshot.

    Colonist ships the current user's hand as exact resource ints and
    everyone else's as zero-filled placeholders. The diff extractor only
    emits this event when the hand contents are fully known (i.e. for
    the self-player, whose cards come through with real type ints).
    Opponent count-only sync lives in the opponent-inference path, not
    here."""
    player: str
    resources: dict[str, int]


@dataclass
class UnknownEvent:
    """Parser couldn't classify — kept verbatim so we can add a rule later."""
    text: str
    icons: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)


Event = Union[
    RollEvent,
    ProduceEvent,
    BuildEvent,
    DiscardEvent,
    RobberMoveEvent,
    StealEvent,
    NoStealEvent,
    TradeOfferEvent,
    TradeCommitEvent,
    DevCardBuyEvent,
    DevCardPlayEvent,
    MonopolyStealEvent,
    VPEvent,
    GameOverEvent,
    RollBlockedEvent,
    InfoEvent,
    DisconnectEvent,
    HandSyncEvent,
    UnknownEvent,
]
