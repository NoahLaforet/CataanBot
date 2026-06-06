"""Probabilistic opponent-hand inference for the live bridge.

colonist.io hides opponents' exact cards: the WS frames carry only each
player's hand *size*, and the public game log narrates what changed
(who produced what on a roll, who built, who traded, who robbed whom).
From that narrative we can pin most of an opponent's hand down exactly,
but two transitions stay genuinely ambiguous:

* a third-party robber/knight steal, where the stolen card's type is
  hidden (we only see "A stole from B"), and
* any later play that depends on the still-unknown card.

The old bridge tracked opponents as a single per-resource *point
estimate* (``tracker.hand``) plus a scalar ``unknown`` bucket, then
guessed the unknown mass onto the victim's largest pile. That can't
answer "what is the chance B is holding ore", which is exactly what a
good robber-target or trade-partner pick needs.

This module replaces the point estimate with a small **weighted
particle filter** over opponent hands:

* Each particle is one concrete assignment of every opponent's cards.
* Deterministic events (produce, build cost, known trade, monopoly,
  year-of-plenty, dev-buy, known steal, discard) apply to every
  particle identically. A debit that would go negative makes that
  particle *impossible*, so it is dropped: a player who builds a road
  proves they held wood+brick, which prunes every hypothesis that said
  otherwise. This is the affordability constraint, for free.
* A hidden steal *branches* each particle, one child per resource the
  victim could have lost, weighted by that victim's composition.
* After each batch we **reconcile** against colonist's authoritative
  per-player hand sizes (``session.hand_card_counts``): any particle
  whose totals disagree is dropped. If every particle dies we *reseed*
  from the authoritative totals, so a missed log line or a page refresh
  self-heals instead of drifting forever.

The reference open-source counter (nickincardone/catan-counter) keeps a
full variant *tree* with a complete game snapshot per node and merges by
exact JSON; chained steals blow it up combinatorially. We avoid that by
(a) tracking only opponent hands (self is known exactly from the WS
self-hand), (b) merging identical particles, and (c) capping the
particle count, collapsing to the highest-weight hypotheses when a long
unresolved chain would otherwise explode. The authoritative-total
reconcile means the cap can never make us *wrong* about a total, only
slightly less sharp about a breakdown until the next observation.

Outputs, per opponent and resource: a guaranteed minimum, an expected
count, and the probability of holding at least one (and of holding more
than the guaranteed floor) so the HUD can render "2 (67%)" and the
advisors can weight a steal or trade by expected value instead of a
single guess.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from catanbot.events import (
    BankSyncEvent, BuildEvent, DevCardBuyEvent, DevCardPlayEvent,
    DiscardEvent, Event, HandSyncEvent, MonopolyStealEvent, ProduceEvent,
    StealEvent, TradeCommitEvent,
)

RESOURCES: tuple[str, ...] = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")
_OF_EACH = 19  # colonist standard deck: 19 of every resource

_BUILD_COSTS: dict[str, dict[str, int]] = {
    "settlement": {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1},
    "city": {"WHEAT": 2, "ORE": 3},
    "road": {"WOOD": 1, "BRICK": 1},
}
_DEV_BUY_COST: dict[str, int] = {"SHEEP": 1, "WHEAT": 1, "ORE": 1}

# Bounds. Particles past the cap are pruned to the highest-weight
# hypotheses; a desync that survives this many consecutive reconciles
# (totals matched by no particle) triggers a reseed.
_MAX_PARTICLES = 1500
_DESYNC_RESEED_AFTER = 2


def _zero_hand() -> dict[str, int]:
    return {r: 0 for r in RESOURCES}


class _Particle:
    """One hypothesis: a concrete hand for every opponent color."""

    __slots__ = ("hands", "w")

    def __init__(self, hands: dict[str, dict[str, int]], w: float) -> None:
        self.hands = hands
        self.w = w

    def clone(self, w: float | None = None) -> "_Particle":
        return _Particle(
            {c: dict(h) for c, h in self.hands.items()},
            self.w if w is None else w,
        )

    def total(self, color: str) -> int:
        return sum(self.hands[color].values())

    def key(self) -> tuple:
        return tuple(
            (c, tuple(self.hands[c][r] for r in RESOURCES))
            for c in sorted(self.hands)
        )


@dataclass
class ResourceBelief:
    """Per-(opponent, resource) marginal extracted from the particles."""

    minimum: int = 0          # held with certainty across every particle
    maximum: int = 0          # most they could be holding
    expected: float = 0.0     # probability-weighted mean count
    p_at_least_one: float = 0.0   # P(count >= 1)
    p_above_min: float = 0.0      # P(count > minimum) — the "+x%" tail

    @property
    def certain(self) -> bool:
        return self.minimum == self.maximum


class OppHandModel:
    """Particle-filter opponent-hand estimator for one live game.

    Fed the public game-log narrative (``apply``), the authoritative
    self hand (``set_self_hand``), the resource bank (``set_bank``), and
    colonist's per-player hand sizes (``reconcile``). Read back through
    ``beliefs`` / ``steal_expectation`` / ``expected_hand``.
    """

    def __init__(
        self,
        colors: Iterable[str],
        self_color: str | None = None,
        prior: dict[str, float] | None = None,
        max_particles: int = _MAX_PARTICLES,
    ) -> None:
        self.colors: list[str] = [c.upper() for c in colors]
        self.self_color: str | None = (
            self_color.upper() if self_color else None
        )
        self.opp_colors: list[str] = [
            c for c in self.colors if c != self.self_color
        ]
        self.self_hand: dict[str, int] = _zero_hand()
        self.bank: dict[str, int] | None = None
        # Prior over resource types for reseeds; events dominate quickly
        # so a flat prior is fine until a board-production prior is wired.
        self.prior: dict[str, float] = prior or {r: 1.0 for r in RESOURCES}
        self.max_particles = max_particles
        # The particle set. Starts as a single empty-hands hypothesis.
        self.particles: list[_Particle] = [
            _Particle({c: _zero_hand() for c in self.opp_colors}, 1.0)
        ]
        self.totals: dict[str, int] = {}      # last authoritative sizes
        self._desync_streak = 0
        self.drift = 0                          # impossible-event counter
        # Part 3 analytics fed off the same stream.
        self.steal_matrix: dict[tuple[str, str], int] = {}

    # -- ingestion ---------------------------------------------------------

    def set_self_hand(self, color: str, cards: dict[str, int]) -> None:
        """Pin the viewer's own exact hand (authoritative WS self-sync)."""
        if self.self_color is None:
            self.self_color = color.upper()
            self.opp_colors = [c for c in self.colors if c != self.self_color]
            self._rebuild_opp_dims()
        if color.upper() != self.self_color:
            return
        self.self_hand = {r: int(cards.get(r, 0)) for r in RESOURCES}

    def set_bank(self, resources: dict[str, int]) -> None:
        self.bank = {r: int(resources.get(r, 0)) for r in RESOURCES}

    def apply(self, event: Event, color_map: Any) -> None:
        """Fold one public-log event into every particle.

        ``color_map`` resolves colonist usernames to catanatron colors
        (same object the tracker uses). Events naming the bank or an
        unseated player resolve to ``None`` and are skipped on that side.
        """
        if isinstance(event, ProduceEvent):
            self._produce(self._color(color_map, event.player), event.resources)
        elif isinstance(event, BuildEvent):
            if event.paid:
                self._spend(
                    self._color(color_map, event.player),
                    _BUILD_COSTS.get(event.piece),
                )
        elif isinstance(event, DevCardBuyEvent):
            self._spend(self._color(color_map, event.player), _DEV_BUY_COST)
        elif isinstance(event, DevCardPlayEvent):
            if event.card == "year_of_plenty" and event.resources:
                self._produce(
                    self._color(color_map, event.player), event.resources)
        elif isinstance(event, DiscardEvent):
            self._spend(
                self._color(color_map, event.player), event.resources)
        elif isinstance(event, MonopolyStealEvent):
            self._monopoly(
                self._color(color_map, event.player),
                event.resource, event.count)
        elif isinstance(event, StealEvent):
            self._steal(
                self._color(color_map, event.thief),
                self._color(color_map, event.victim),
                event.resource)
        elif isinstance(event, TradeCommitEvent):
            self._trade(
                self._color(color_map, event.giver),
                self._color(color_map, event.receiver),
                event.gave, event.got)
        elif isinstance(event, HandSyncEvent):
            self.set_self_hand(
                self._color(color_map, event.player) or "", event.resources)
        elif isinstance(event, BankSyncEvent):
            self.set_bank(event.resources)

    # -- per-event particle ops -------------------------------------------

    def _produce(self, color: str | None, resources: dict[str, int]) -> None:
        if color is None or color == self.self_color or not resources:
            return
        for p in self.particles:
            hand = p.hands[color]
            for res, n in resources.items():
                if res in hand:
                    hand[res] += int(n)

    def _spend(self, color: str | None, cost: dict[str, int] | None) -> None:
        """Debit a known cost from one opponent; impossible particles die."""
        if color is None or color == self.self_color or not cost:
            return
        survivors: list[_Particle] = []
        for p in self.particles:
            hand = p.hands[color]
            ok = True
            for res, n in cost.items():
                if res not in hand:
                    continue
                if hand[res] < n:
                    ok = False
                    break
            if not ok:
                continue
            for res, n in cost.items():
                if res in hand:
                    hand[res] -= int(n)
            survivors.append(p)
        if survivors:
            self.particles = survivors
            self._renormalize()
        else:
            # No hypothesis could afford it — our breakdown drifted below
            # reality (a missed steal/trade). Clamp the floor to the cost
            # so the event still "happens", and flag drift; the next
            # reconcile re-anchors the totals.
            self.drift += 1
            for p in self.particles:
                hand = p.hands[color]
                for res, n in cost.items():
                    if res in hand:
                        hand[res] = max(0, hand[res] - int(n))

    def _monopoly(self, thief: str | None, resource: str, count: int) -> None:
        if resource not in RESOURCES:
            return
        for p in self.particles:
            for c in self.opp_colors:
                if c == thief:
                    continue
                p.hands[c][resource] = 0
            if thief is not None and thief != self.self_color:
                p.hands[thief][resource] += int(count)

    def _steal(
        self, thief: str | None, victim: str | None, resource: str | None
    ) -> None:
        if thief is not None and victim is not None:
            self.steal_matrix[(thief, victim)] = (
                self.steal_matrix.get((thief, victim), 0) + 1)
        if resource is not None:
            # Known steal — the viewer is thief or victim, so the type is
            # public. Apply the side(s) that are opponents.
            self._produce(thief, {resource: 1})
            self._spend(victim, {resource: 1})
            return
        # Hidden third-party steal: both sides are opponents (the log only
        # blurs steals not involving the viewer). Branch on the victim's
        # composition.
        if (victim is None or thief is None
                or victim == self.self_color or thief == self.self_color):
            return
        children: list[_Particle] = []
        for p in self.particles:
            vhand = p.hands[victim]
            vtotal = sum(vhand.values())
            if vtotal <= 0:
                # Victim looks empty in this hypothesis but was robbed —
                # this particle is inconsistent; drop it.
                continue
            for res in RESOURCES:
                have = vhand[res]
                if have <= 0:
                    continue
                child = p.clone(p.w * have / vtotal)
                child.hands[victim][res] -= 1
                child.hands[thief][res] += 1
                children.append(child)
        if children:
            self.particles = children
            self._compact()
        else:
            self.drift += 1

    def _trade(
        self, giver: str | None, receiver: str | None,
        gave: dict[str, int], got: dict[str, int],
    ) -> None:
        # gave flows giver -> receiver, got flows receiver -> giver.
        # Debits prove the giver/receiver held those cards (affordability).
        self._spend(giver, gave)
        self._produce(receiver, gave)
        self._spend(receiver, got)
        self._produce(giver, got)

    # -- reconciliation ----------------------------------------------------

    def reconcile(
        self, totals: dict[str, int] | None = None,
        bank: dict[str, int] | None = None,
    ) -> None:
        """Drop hypotheses that disagree with colonist's authoritative
        per-opponent hand sizes (and the 19-per-resource deck), reseeding
        if every hypothesis dies."""
        if bank is not None:
            self.set_bank(bank)
        if totals is not None:
            self.totals = {c.upper(): int(n) for c, n in totals.items()}
        if not self.totals:
            self._enforce_deck_cap()
            self._compact()
            return

        matching: list[_Particle] = []
        for p in self.particles:
            if not self._deck_ok(p):
                continue
            if all(
                p.total(c) == self.totals.get(c, p.total(c))
                for c in self.opp_colors
            ):
                matching.append(p)

        if matching:
            self.particles = matching
            self._desync_streak = 0
            self._compact()
            return

        # Nothing matches the authoritative totals. Could be a one-frame
        # lag between a log line and the WS size update, or a genuine
        # desync. Tolerate a lag; reseed only if it persists.
        self._desync_streak += 1
        if self._desync_streak >= _DESYNC_RESEED_AFTER:
            self._reseed()
            self._desync_streak = 0
        else:
            self._enforce_deck_cap()
            self._compact()

    def _deck_ok(self, p: _Particle) -> bool:
        for res in RESOURCES:
            in_play = self.self_hand[res] + sum(
                p.hands[c][res] for c in self.opp_colors)
            if in_play > _OF_EACH:
                return False
        return True

    def _enforce_deck_cap(self) -> None:
        survivors = [p for p in self.particles if self._deck_ok(p)]
        if survivors:
            self.particles = survivors
            self._renormalize()

    def _reseed(self) -> None:
        """Rebuild a single best-guess hypothesis straight from the
        authoritative totals when the particle set has fully desynced."""
        hands: dict[str, dict[str, int]] = {}
        # Remaining deck headroom per resource after the viewer's own hand.
        headroom = {r: max(0, _OF_EACH - self.self_hand[r]) for r in RESOURCES}
        for c in self.opp_colors:
            total = max(0, self.totals.get(c, 0))
            hands[c] = self._distribute(total, headroom)
            for r in RESOURCES:
                headroom[r] = max(0, headroom[r] - hands[c][r])
        self.particles = [_Particle(hands, 1.0)]
        self.drift += 1

    def _distribute(
        self, total: int, headroom: dict[str, int]
    ) -> dict[str, int]:
        """Spread ``total`` cards over resources by the prior, clamped to
        deck headroom. Largest-remainder rounding hits the exact total."""
        hand = _zero_hand()
        if total <= 0:
            return hand
        weights = {r: max(0.0, self.prior.get(r, 0.0)) for r in RESOURCES}
        wsum = sum(weights.values()) or 1.0
        # Ideal fractional allocation.
        ideal = {r: total * weights[r] / wsum for r in RESOURCES}
        floors = {r: min(headroom[r], int(ideal[r])) for r in RESOURCES}
        assigned = sum(floors.values())
        # Hand out the remaining cards to the largest fractional remainders
        # that still have headroom.
        remainder = sorted(
            RESOURCES,
            key=lambda r: (ideal[r] - floors[r]),
            reverse=True,
        )
        hand.update(floors)
        i = 0
        guard = 0
        while assigned < total and guard < total * len(RESOURCES) + len(RESOURCES):
            r = remainder[i % len(RESOURCES)]
            if hand[r] < headroom[r]:
                hand[r] += 1
                assigned += 1
            i += 1
            guard += 1
        return hand

    # -- particle-set housekeeping ----------------------------------------

    def _renormalize(self) -> None:
        wsum = sum(p.w for p in self.particles)
        if wsum <= 0:
            return
        for p in self.particles:
            p.w /= wsum

    def _merge(self) -> None:
        merged: dict[tuple, _Particle] = {}
        for p in self.particles:
            k = p.key()
            if k in merged:
                merged[k].w += p.w
            else:
                merged[k] = p
        self.particles = list(merged.values())

    def _cap(self) -> None:
        if len(self.particles) <= self.max_particles:
            return
        self.particles.sort(key=lambda p: p.w, reverse=True)
        self.particles = self.particles[: self.max_particles]

    def _compact(self) -> None:
        if not self.particles:
            return
        self._merge()
        self._cap()
        self._renormalize()

    # -- readouts ----------------------------------------------------------

    def expected_hand(self, color: str) -> dict[str, float]:
        """Probability-weighted mean per-resource count for one color."""
        color = color.upper()
        if color == self.self_color:
            return {r: float(self.self_hand[r]) for r in RESOURCES}
        out = {r: 0.0 for r in RESOURCES}
        wsum = sum(p.w for p in self.particles) or 1.0
        for p in self.particles:
            hand = p.hands.get(color)
            if hand is None:
                continue
            for r in RESOURCES:
                out[r] += p.w * hand[r]
        return {r: out[r] / wsum for r in RESOURCES}

    def beliefs(self, color: str) -> dict[str, ResourceBelief]:
        """Full per-resource belief (min/max/expected/probabilities)."""
        color = color.upper()
        if color == self.self_color:
            return {
                r: ResourceBelief(
                    minimum=self.self_hand[r], maximum=self.self_hand[r],
                    expected=float(self.self_hand[r]),
                    p_at_least_one=1.0 if self.self_hand[r] else 0.0,
                    p_above_min=0.0,
                )
                for r in RESOURCES
            }
        wsum = sum(p.w for p in self.particles) or 1.0
        out: dict[str, ResourceBelief] = {}
        for r in RESOURCES:
            counts = [
                (p.hands[color][r], p.w)
                for p in self.particles if color in p.hands
            ]
            if not counts:
                out[r] = ResourceBelief()
                continue
            mn = min(c for c, _ in counts)
            mx = max(c for c, _ in counts)
            exp = sum(c * w for c, w in counts) / wsum
            p_one = sum(w for c, w in counts if c >= 1) / wsum
            p_above = sum(w for c, w in counts if c > mn) / wsum
            out[r] = ResourceBelief(
                minimum=mn, maximum=mx, expected=exp,
                p_at_least_one=p_one, p_above_min=p_above,
            )
        return out

    def steal_expectation(self, victim: str) -> dict[str, float]:
        """Probability that robbing ``victim`` yields each resource — i.e.
        the expected fraction of their hand that is that resource."""
        color = victim.upper()
        exp = self.expected_hand(color)
        total = sum(exp.values())
        if total <= 0:
            return {r: 0.0 for r in RESOURCES}
        return {r: exp[r] / total for r in RESOURCES}

    def hand_total(self, color: str) -> int:
        color = color.upper()
        if color == self.self_color:
            return sum(self.self_hand.values())
        return int(self.totals.get(color, 0))

    def confidence(self, color: str) -> float:
        """Share of ``color``'s hand that is pinned down with certainty."""
        total = self.hand_total(color)
        if total <= 0:
            return 1.0
        bel = self.beliefs(color)
        known = sum(b.minimum for b in bel.values())
        return max(0.0, min(1.0, known / total))

    def _color(self, color_map: Any, username: str | None) -> str | None:
        if not username or username == "BANK":
            return None
        try:
            if hasattr(color_map, "has") and not color_map.has(username):
                color = color_map.get(username)
            else:
                color = color_map.get(username)
        except Exception:  # noqa: BLE001 — unseated/over-full table
            return None
        if color is None:
            return None
        color = str(color).upper()
        return color if color in self.colors else None

    def _rebuild_opp_dims(self) -> None:
        for p in self.particles:
            for c in self.opp_colors:
                p.hands.setdefault(c, _zero_hand())
            for c in list(p.hands):
                if c not in self.opp_colors:
                    del p.hands[c]
