"""Post-placement strategy selector.

The recommender historically scored every move in isolation, with one
implicit notion of "good" baked into the per-resource weights and the
opening archetype labeler. That works for "what should I build NOW?"
but ignores the question competitive players answer first: *given the
two settlements I actually claimed, which strategy am I now playing?*

This module answers that. After both opening settlements land, it
inspects the claimed footprint and returns a tag describing the
strategy the placements actually enable:

    OWS              — ore-wheat-sheep dev-card engine (city-rush + flex)
    LR_RUSH          — wood/brick footprint with expansion runway
    PORT_TRADE       — strong tile + relevant port reachable in 1-2 roads
    RB_CARVED_TILES  — isolated cluster of 4-5 tiles (the *real* RB)
    BALANCED         — default fallback; every other tag inherits from this

The tag flows through ``bridge_strategy`` into the snap as
``snap["strategy"]``, where downstream weighting (in ``recommender``
and ``bridge_hints``) can read it to bias scores. The selector also
exposes ``detect_pivot_triggers`` which compares current state against
a small history rolling buffer and fires named triggers
(``hot_number``, ``road_builder_drawn``, ``opp_close_to_la``, etc.)
when conditions warrant a re-evaluation mid-game.

Per the competitive-Catan feedback driving the v2 plan: the strategy
isn't chosen on turn 1; it's chosen by the placements, and it must
keep adapting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from catanatron import Game


# Tag ordering — when scores tie, earlier wins. BALANCED is intentionally
# last so it surfaces only when nothing else clears its threshold.
_TAGS: tuple[str, ...] = (
    "RB_CARVED_TILES",
    "OWS",
    "LR_RUSH",
    "PORT_TRADE",
    "BALANCED",
)


# Phase boundaries by total roll count (full-table dice rolls). Used to
# label the strategy's current phase so downstream consumers can pick
# different bias profiles per phase (LR_RUSH in setup phase only
# recommends roads that double as expansion; in commit phase it goes
# all-in on chain extension).
_PHASE_BOUNDARIES = (
    (0, "opening"),
    (5, "early"),
    (15, "mid"),
    (30, "late"),
    (50, "endgame"),
)


def _phase_for(rolls: int) -> str:
    """Map an absolute roll count to a phase label. Monotonic — once a
    phase is left it doesn't come back."""
    label = "opening"
    for threshold, name in _PHASE_BOUNDARIES:
        if rolls >= threshold:
            label = name
    return label


@dataclass
class StrategyTag:
    """The chosen strategy plus context for downstream consumers.

    ``primary`` is the active tag; ``fallback`` is the runner-up so the
    rec layer can offer an alternate read on close calls. ``rationale``
    is a one-line user-facing description of *why* this tag was picked
    — surfaces on the HUD as the "why this strategy" hint.

    ``scores`` is the full per-tag score map (every archetype's 0-1
    score from this evaluation pass). The HUD uses this to render a
    "ranked strategies" view so the user sees not just the pick but
    how close the runners-up came.

    ``pivot_triggers`` is the list of fired trigger names (string
    constants from ``_PIVOT_TRIGGERS``). Empty list means no pivot
    pressure right now. ``override_tag`` is set by the trigger detector
    when a trigger should force a temporary re-tag.
    """
    primary: str
    fallback: str | None
    rationale: str
    phase: str
    set_at_rolls: int = 0
    pivot_triggers: list[str] = field(default_factory=list)
    override_tag: str | None = None
    scores: dict[str, float] = field(default_factory=dict)

    def to_snap(self) -> dict[str, Any]:
        """Serialize for the advisor snap. Keys mirror the names the
        HUD will read; flatten so the front-end doesn't need to know
        about dataclasses."""
        # Full ranked list (best score first) so the HUD can render
        # the bot's whole assessment, not just the winner. Each entry
        # is {tag, score, eligible} where eligible reflects whether
        # the tag cleared its threshold and was a real contender.
        ranking = []
        if self.scores:
            sorted_tags = sorted(
                self.scores.items(), key=lambda kv: -kv[1])
            for tag, score in sorted_tags:
                ranking.append({
                    "tag": tag,
                    "score": round(float(score), 2),
                    "eligible": (score
                                 >= _TAG_MIN_SCORE.get(tag, 0.0)),
                })
        # Preview mode (pre-placement, scores populated but no chosen
        # primary): snap.active stays falsy so the HUD renders the
        # board-affinity headline instead of an archetype banner.
        active_tag = (self.override_tag or self.primary) or None
        return {
            "primary": self.primary or None,
            "fallback": self.fallback,
            "rationale": self.rationale,
            "phase": self.phase,
            "set_at_rolls": self.set_at_rolls,
            "pivot_triggers": list(self.pivot_triggers),
            "override_tag": self.override_tag,
            "active": active_tag,
            "scores": dict(self.scores),
            "ranking": ranking,
        }


# --- placement inspection --------------------------------------------

def _self_settlement_nodes(game: "Game", my_color) -> list[int]:
    """Node ids of self's placed settlements + cities (cities count
    because they sit on a former settlement node — same tile coverage)."""
    out: list[int] = []
    for nid, (col, btype) in game.state.board.buildings.items():
        if col == my_color and btype in ("SETTLEMENT", "CITY"):
            out.append(int(nid))
    return out


def _combined_production(game: "Game", node_ids: list[int]) -> dict[str, float]:
    """Per-resource cards-per-roll across all owned nodes (settlements
    only; cities double individually but for the purpose of strategy
    detection we treat all footprints as 1×, since we want to see what
    the *placements* enable, not the current city upgrades)."""
    m = game.state.board.map
    out: dict[str, float] = {
        "WOOD": 0.0, "BRICK": 0.0, "SHEEP": 0.0, "WHEAT": 0.0, "ORE": 0.0,
    }
    for nid in node_ids:
        for res, v in m.node_production.get(int(nid), {}).items():
            if res in out:
                out[res] += float(v)
    return out


def _adjacent_tiles(game: "Game", node_id: int) -> list[tuple[str | None, int | None]]:
    """List of (resource, number) for every tile touching ``node_id``.
    Desert reads as (None, None)."""
    m = game.state.board.map
    out: list[tuple[str | None, int | None]] = []
    for tile in m.adjacent_tiles.get(int(node_id), []):
        out.append((tile.resource, tile.number))
    return out


def _node_port_label(game: "Game", node_id: int) -> str | None:
    """Return the port label for a node (``"WHEAT 2:1"``, ``"3:1"``,
    or None). Reuses the advisor's port-label mapping."""
    from catanbot.advisor import _build_node_port_labels
    labels = _build_node_port_labels(game.state.board.map)
    return labels.get(int(node_id))


# --- isolation score (for RB_CARVED_TILES) ---------------------------

def _isolation_score(game: "Game", my_color, node_ids: list[int]) -> float:
    """How "carved out" is the cluster around self's settlements?

    A true RoadBuilder play needs 4–5 tiles that are reachable through
    your own settlements without going through opponents — the desert
    being on the edge or a weak-number ring on one side typically
    produces this. We score it as:

        (tiles within 2 hops of self's nodes that have a number) /
        (max possible — capped at 12)

    When opponents' settlements lie inside that 2-hop ring, each one
    subtracts 1.0 from the numerator (their pieces eat directly into
    the corridor). The result floors at 0 and tops out at ~1.0.

    A score >= 0.7 is "carved-out enough to play RB"; below that, the
    RB tag should not fire.
    """
    if not node_ids:
        return 0.0
    m = game.state.board.map
    from catanbot.advisor import _build_node_neighbors
    neighbors = _build_node_neighbors(m)
    # 2-hop reachable set from any of my nodes.
    reachable: set[int] = set()
    for nid in node_ids:
        reachable.add(int(nid))
        first_ring = neighbors.get(int(nid), set())
        reachable |= {int(n) for n in first_ring}
        for f in first_ring:
            reachable |= {int(n) for n in neighbors.get(int(f), set())}
    # Tiles touched by the reachable set, with a number (skip desert).
    tile_ids: set[int] = set()
    for nid in reachable:
        for tile in m.adjacent_tiles.get(int(nid), []):
            if tile.number is not None and tile.resource is not None:
                tile_ids.add(tile.id)
    tile_count = min(len(tile_ids), 12)
    # Opponent footprints inside the corridor reduce isolation.
    opp_inside = 0
    for nid, (col, btype) in game.state.board.buildings.items():
        if col == my_color or btype not in ("SETTLEMENT", "CITY"):
            continue
        if int(nid) in reachable:
            opp_inside += 1
    score = (tile_count - opp_inside) / 12.0
    return max(0.0, min(1.0, score))


# --- per-tag scoring -------------------------------------------------

# Threshold a tag must clear (after weighting) to be considered eligible.
# BALANCED has no threshold — it's the floor. The numbers reflect
# minimum signal: e.g. PORT_TRADE needs a port that aligns with at
# least one produced resource on a 4+ pip tile.
_TAG_MIN_SCORE = {
    "RB_CARVED_TILES": 0.65,
    "OWS": 0.45,
    "LR_RUSH": 0.45,
    "PORT_TRADE": 0.45,
    "BALANCED": 0.0,
}


def _score_ows(prod: dict[str, float]) -> float:
    """OWS score: city-rush plus dev-card flexibility.

    Strong when the placements lean on ore + wheat + sheep — those are
    the three resources that fund both city upgrades AND dev-card buys.
    A pure ore+wheat play (no sheep) still scores well; sheep is the
    flexibility multiplier.

    Scale: ``prod`` values are cards-per-roll (e.g. a 5-pip wheat tile
    contributes 0.139). Two strong ore+wheat settlements typically
    yield wheat ~0.25, ore ~0.20, sheep ~0.05 — base subscore ~0.21,
    multiplied by 3.0 → ~0.63, comfortably above the 0.45 threshold.
    """
    ore = prod.get("ORE", 0.0)
    wheat = prod.get("WHEAT", 0.0)
    sheep = prod.get("SHEEP", 0.0)
    # Floor of 0.10 ore AND wheat blocks the tag from firing on
    # placements that are OWS in name only (one weak corner of each).
    if ore < 0.10 or wheat < 0.10:
        return 0.0
    base = 0.5 * wheat + 0.35 * ore + 0.20 * sheep
    # Multiplier of 4.0: a weak-but-real OWS pair (ore 0.16 + wheat
    # 0.16) lands ~0.57, clearing the 0.45 threshold; a strong pair
    # caps at 1.0.
    return min(1.0, base * 4.0)


def _score_lr_rush(prod: dict[str, float],
                   node_ids: list[int],
                   game: "Game") -> float:
    """LR_RUSH score: wood + brick footprint plus expansion runway.

    Strong when wood AND brick are both produced (you can spam roads
    AND afford settlements without leaning on dev cards), and at least
    one settlement sits on a node with 2+ buildable neighbors (room to
    extend).
    """
    wood = prod.get("WOOD", 0.0)
    brick = prod.get("BRICK", 0.0)
    # Floor of 0.10 each — a single 4-pip tile in each scores 0.111,
    # which qualifies. Below that, neither resource is producing
    # enough to road-spam.
    if wood < 0.10 or brick < 0.10:
        return 0.0
    base = wood + brick
    # Runway: count buildable corridors out of self's nodes. A node
    # with all 3 first-ring neighbors blocked has no corridor and
    # shouldn't count.
    from catanbot.advisor import _build_node_neighbors
    m = game.state.board.map
    neighbors = _build_node_neighbors(m)
    blocked: set[int] = set()
    for nid, (_col, btype) in game.state.board.buildings.items():
        if btype not in ("SETTLEMENT", "CITY"):
            continue
        blocked.add(int(nid))
        blocked |= {int(n) for n in neighbors.get(int(nid), set())}
    runway = 0
    for nid in node_ids:
        free = sum(
            1 for n in neighbors.get(int(nid), set())
            if int(n) not in blocked
        )
        runway += free
    runway_factor = min(1.0, runway / 4.0)
    # Multiplier sized to put a 0.4 wood+brick (two ~5-pip tiles) base
    # at ~0.6 with full runway — clears the 0.45 threshold but leaves
    # headroom for OWS to outscore on equally-strong ore+wheat boards.
    return min(1.0, base * 1.6 * (0.5 + 0.5 * runway_factor))


def _score_port_trade(game: "Game", my_color, node_ids: list[int],
                      prod: dict[str, float]) -> float:
    """PORT_TRADE score: a relevant port reachable from settlements.

    Port-on-settlement is the *bad* PR play and scores zero unless the
    port resource is on a strong adjacent tile. Settle-near-port (port
    on a 1-hop expansion node) scores high when the port's resource
    is one we already produce in surplus.
    """
    from catanbot.advisor import (
        _build_node_neighbors, _build_node_port_labels,
    )
    m = game.state.board.map
    port_labels = _build_node_port_labels(m)
    neighbors = _build_node_neighbors(m)

    # Direct settle-on-port: only counts if the port resource is on an
    # adjacent tile with pip >= 4 (the 4/5/6/8/9/10 tier).
    direct_score = 0.0
    for nid in node_ids:
        label = port_labels.get(int(nid))
        if not label or label == "3:1":
            continue
        port_res = label.split(" ", 1)[0]
        for res, num in _adjacent_tiles(game, int(nid)):
            if res != port_res:
                continue
            from catanbot.advisor import PIP_DOTS_BY_NUMBER
            pip = PIP_DOTS_BY_NUMBER.get(int(num) if num else 0, 0)
            if pip >= 3:
                direct_score = max(direct_score, 0.6)

    # Settle-near-port: port within 1 hop of a settlement and the
    # port resource is one we produce. Stronger than direct because
    # it doesn't sacrifice a tile-adjacent corner.
    near_score = 0.0
    for nid in node_ids:
        for nb in neighbors.get(int(nid), set()):
            label = port_labels.get(int(nb))
            if not label or label == "3:1":
                continue
            port_res = label.split(" ", 1)[0]
            if prod.get(port_res, 0.0) >= 0.20:
                near_score = max(near_score, 0.85)

    return max(direct_score, near_score)


def _score_rb_carved(game: "Game", my_color, node_ids: list[int]) -> float:
    """RB_CARVED_TILES score.

    The original RB strategy requires the corridor to be bounded by
    *natural* constraints — desert/edge/weak-number ring — AND for at
    least some opponents to have committed, since otherwise a fresh
    game with only self placements reads as 100% isolated even though
    the rest of the table will fill in next round.

    Gating logic:

    * Require >= 4 opponent footprints (placed). Less than that and
      the rest of the table hasn't expressed where they're heading.
    * Require an "edge anchor" — at least one of self's tiles within
      2 hops touches a desert, the map boundary, OR a 2/3/11/12 weak
      number ring (4+ such tiles inside the corridor).

    Only when both conditions hold do we derive the actual score from
    the isolation metric.
    """
    # Opponent commitment guard — RB makes no sense before opponents
    # have placed.
    opp_footprints = 0
    for _nid, (col, btype) in game.state.board.buildings.items():
        if col != my_color and btype in ("SETTLEMENT", "CITY"):
            opp_footprints += 1
    if opp_footprints < 4:
        return 0.0

    # Edge anchor — does the corridor terminate naturally?
    if not _has_natural_corridor_anchor(game, my_color, node_ids):
        return 0.0

    iso = _isolation_score(game, my_color, node_ids)
    if iso < 0.5:
        return 0.0
    return min(1.0, (iso - 0.5) * 2.0)


def _has_natural_corridor_anchor(game: "Game", my_color,
                                 node_ids: list[int]) -> bool:
    """True when the 2-hop corridor includes a natural terminator:
    a desert tile, a board-edge node (a node touching ocean — fewer
    than 3 land tiles), or 4+ weak-number tiles (pip <= 2)."""
    from catanbot.advisor import (
        _build_node_neighbors, PIP_DOTS_BY_NUMBER,
    )
    m = game.state.board.map
    neighbors = _build_node_neighbors(m)
    reachable: set[int] = set()
    for nid in node_ids:
        reachable.add(int(nid))
        first_ring = neighbors.get(int(nid), set())
        reachable |= {int(n) for n in first_ring}
        for f in first_ring:
            reachable |= {int(n) for n in neighbors.get(int(f), set())}

    weak_tiles = 0
    for nid in reachable:
        tiles = m.adjacent_tiles.get(int(nid), [])
        # Edge node: fewer than 3 numbered land tiles attached.
        land_tiles = [t for t in tiles if t.resource is not None]
        if len(land_tiles) < 3:
            return True
        for t in tiles:
            if t.resource is None and t.number is None:
                # Desert.
                return True
            pip = PIP_DOTS_BY_NUMBER.get(t.number or 0, 0)
            if pip and pip <= 2:
                weak_tiles += 1
    # Every weak tile gets counted from each adjacent node, so 4 unique
    # weak tiles can produce up to 12 hits. Threshold of 8 keeps this
    # honest — at least 3 unique weak tiles in the corridor.
    return weak_tiles >= 8


def _score_balanced(prod: dict[str, float]) -> float:
    """BALANCED score: distinct resources covered. 4-5 distinct =
    strong baseline, 3 = floor."""
    distinct = sum(1 for v in prod.values() if v > 0.05)
    if distinct >= 4:
        return 0.55
    if distinct >= 3:
        return 0.40
    return 0.20


# --- public API ------------------------------------------------------

def compute_board_affinity(game: "Game") -> dict[str, float]:
    """Pre-placement archetype scoring — "what does this board favor?"

    Without committed settlements, the standard scoring functions
    (which need owned nodes to compute combined production) all return
    zero. To guide the user's *first* settle decision, this function
    asks a different question: for each archetype, what's the BEST 2-
    node pair on this board that fits it? It uses the existing
    opening scorer to find candidate top nodes, then for each
    archetype it locates the best-fitting pair and scores the
    archetype against that hypothetical placement.

    Returns the same {tag → 0..1 score} shape as the post-placement
    selector, so the snap layer can render the ranking identically.
    """
    from catanatron import Color
    from catanbot.advisor import (
        _build_node_neighbors, score_opening_nodes,
    )

    try:
        m = game.state.board.map
    except Exception:  # noqa: BLE001
        return {}
    # Top-15 opening candidates is a wide enough net to find a good
    # archetype-aligned pair without scoring every land node.
    try:
        top_nodes = [s.node_id for s in
                     score_opening_nodes(game)[:15]]
    except Exception:  # noqa: BLE001
        return {}
    if not top_nodes:
        return {}
    neighbors = _build_node_neighbors(m)

    # Generate non-conflicting pairs from the top candidates.
    pairs: list[tuple[int, int]] = []
    for i, a in enumerate(top_nodes):
        a_block = {a} | set(neighbors.get(a, set()))
        for b in top_nodes[i + 1:]:
            if b in a_block:
                continue
            pairs.append((a, b))
    if not pairs:
        return {}

    # For each pair, score each archetype as if those were the
    # committed settlements. Take the max per archetype across all
    # pairs — that's the "best-case board affinity."
    best: dict[str, float] = {t: 0.0 for t in _TAGS}
    color_for_scoring = next(iter(game.state.colors))
    for a, b in pairs:
        node_ids = [a, b]
        prod = _combined_production(game, node_ids)
        scores = {
            "OWS": _score_ows(prod),
            "LR_RUSH": _score_lr_rush(prod, node_ids, game),
            "PORT_TRADE": _score_port_trade(
                game, color_for_scoring, node_ids, prod),
            # RB_CARVED_TILES is gated on real opponent placements
            # AND a natural anchor; pre-placement it's noisy. Skip
            # for board affinity — it'll surface post-placement when
            # it actually matters.
            "RB_CARVED_TILES": 0.0,
            "BALANCED": _score_balanced(prod),
        }
        for tag, score in scores.items():
            if score > best[tag]:
                best[tag] = score
    return best


def select_strategy(
    game: "Game",
    my_color,
    *,
    rolls_so_far: int = 0,
    previous: StrategyTag | None = None,
) -> StrategyTag | None:
    """Pick the active strategy tag from the current placements.

    During setup (fewer than 2 settlements placed), returns a "preview"
    StrategyTag with ``primary=None`` but populated ``scores`` from
    ``compute_board_affinity`` — the HUD uses this to guide the
    user's first settle. Once both opening settlements are down,
    scores every tag against the actual placements and picks the
    highest-scoring eligible tag as primary, second-highest as
    fallback.

    ``previous`` is the last-emitted tag; the selector is mostly
    monotonic — primary won't flip unless the new top tag's score is
    at least 15% higher than the previously-chosen tag's current
    score, OR a fired pivot trigger explicitly demands the change.
    The HUD doesn't need to flicker between similar reads on every
    snap.
    """
    from catanatron import Color

    try:
        my_enum = (my_color if isinstance(my_color, Color)
                   else Color[str(my_color).upper()])
    except Exception:  # noqa: BLE001
        return None

    nodes = _self_settlement_nodes(game, my_enum)
    if len(nodes) < 2:
        # Pre-placement preview — board-affinity ranking only.
        affinity = compute_board_affinity(game)
        if not affinity:
            return None
        return StrategyTag(
            primary="",  # empty string signals preview mode to the snap
            fallback=None,
            rationale="",
            phase=_phase_for(rolls_so_far),
            set_at_rolls=rolls_so_far,
            scores=affinity,
        )

    prod = _combined_production(game, nodes)
    scores: dict[str, float] = {
        "OWS": _score_ows(prod),
        "LR_RUSH": _score_lr_rush(prod, nodes, game),
        "PORT_TRADE": _score_port_trade(game, my_enum, nodes, prod),
        "RB_CARVED_TILES": _score_rb_carved(game, my_enum, nodes),
        "BALANCED": _score_balanced(prod),
    }

    eligible = [
        (tag, score) for tag, score in scores.items()
        if score >= _TAG_MIN_SCORE.get(tag, 0.0)
    ]
    if not eligible:
        # Defensive — BALANCED's threshold is 0 so this shouldn't
        # happen, but guard against scoring bugs.
        return None
    # Tag-order tiebreak: when two tags tie on score, earlier in
    # ``_TAGS`` wins. RB > OWS > LR > PORT > BALANCED — biases toward
    # the more-specific tag when scores are equal.
    eligible.sort(key=lambda kv: (-kv[1], _TAGS.index(kv[0])))
    primary = eligible[0][0]
    fallback = eligible[1][0] if len(eligible) > 1 else None

    # Stickiness: don't flip primary on a small score wobble.
    if previous is not None and previous.primary != primary:
        prev_score = scores.get(previous.primary, 0.0)
        new_score = eligible[0][1]
        if new_score < prev_score * 1.15:
            primary = previous.primary
            # Recompute fallback as the highest non-primary eligible.
            non_primary = [t for t, _ in eligible if t != primary]
            fallback = non_primary[0] if non_primary else None

    rationale = _rationale_for(primary, prod, scores, nodes, game, my_enum)
    phase = _phase_for(rolls_so_far)

    set_at = (previous.set_at_rolls
              if previous is not None and previous.primary == primary
              else rolls_so_far)
    return StrategyTag(
        primary=primary,
        fallback=fallback,
        rationale=rationale,
        phase=phase,
        set_at_rolls=set_at,
        scores=dict(scores),
    )


def _rationale_for(tag: str, prod: dict[str, float],
                   scores: dict[str, float],
                   nodes: list[int], game: "Game", my_color) -> str:
    """One-line explanation for why ``tag`` was chosen. Surfaces in the
    HUD so the user understands the strategy framing."""
    if tag == "OWS":
        return (f"ore {prod.get('ORE', 0):.2f}/r + wheat "
                f"{prod.get('WHEAT', 0):.2f}/r · city + dev engine")
    if tag == "LR_RUSH":
        return (f"wood {prod.get('WOOD', 0):.2f}/r + brick "
                f"{prod.get('BRICK', 0):.2f}/r · road runway open")
    if tag == "PORT_TRADE":
        return "relevant port reachable; settle near, route on settle #2"
    if tag == "RB_CARVED_TILES":
        iso = _isolation_score(game, my_color, nodes)
        return (f"corridor carved out (~{int(iso*100)}% isolation) · "
                f"hold roads, claim LR late")
    distinct = sum(1 for v in prod.values() if v > 0.05)
    return f"balanced base ({distinct}/5 resources) · keep options open"


# --- pivot triggers --------------------------------------------------

# Named pivot triggers. Order matters — the first trigger that fires and
# carries an override tag wins (pivot triggers can also be informational,
# in which case they fire without overriding the active tag).
_PIVOT_TRIGGERS = (
    "hot_number",
    "road_builder_drawn",
    "monopoly_drawn",
    "opp_close_to_win",
    "opp_close_to_la",
    "seven_overdue",
)


@dataclass
class PivotTrigger:
    """Single fired trigger. ``override_tag`` is None for informational
    triggers (the HUD shows them as 'consider …' without forcing the
    active tag). ``override_tag`` set means the strategy layer should
    flip the active tag for as long as the trigger condition holds."""
    name: str
    detail: str
    override_tag: str | None = None


def _detect_hot_number(roll_history: list[dict[str, Any]],
                       my_settlement_numbers: set[int]) -> PivotTrigger | None:
    """Hot-number streak: a single number rolled 4+ times in the last
    10 rolls AND it lands on one of self's tiles. Pivot — start
    leaning into whatever resource that tile produces (e.g. setup for
    a monopoly call later)."""
    if not roll_history or not my_settlement_numbers:
        return None
    recent = roll_history[-10:]
    counts: dict[int, int] = {}
    for entry in recent:
        n = entry.get("total")
        if n and n != 7:
            counts[int(n)] = counts.get(int(n), 0) + 1
    for num, count in counts.items():
        if count >= 4 and num in my_settlement_numbers:
            return PivotTrigger(
                name="hot_number",
                detail=f"{num} rolled {count}× in last 10 · lean in",
                # Informational only — we don't know which strategy
                # leverages this without knowing the resource.
                override_tag=None,
            )
    return None


def _detect_dev_card_drawn(self_dev_just_bought: list[int] | None,
                           ) -> list[PivotTrigger]:
    """Road-building (type 14) or monopoly (type 12) freshly drawn?
    Each fires its own trigger with a strategy override.

    Type ints are colonist's: KNIGHT=11, MONOPOLY=12, YEAR_OF_PLENTY=13,
    ROAD_BUILDING=14, VICTORY_POINT=15. See colonist_diff._DEV_CARD_TYPE.
    """
    out: list[PivotTrigger] = []
    if not self_dev_just_bought:
        return out
    for tid in self_dev_just_bought:
        try:
            tid_i = int(tid)
        except Exception:  # noqa: BLE001
            continue
        if tid_i == 14:  # ROAD_BUILDING
            out.append(PivotTrigger(
                name="road_builder_drawn",
                detail="road-building drawn · LR rush now reachable",
                override_tag="LR_RUSH",
            ))
        elif tid_i == 12:  # MONOPOLY
            out.append(PivotTrigger(
                name="monopoly_drawn",
                detail="monopoly drawn · hold for hot resource",
                # No override — monopoly doesn't change the active
                # strategy, just tells the player to hold the card.
                override_tag=None,
            ))
    return out


def _detect_opp_close(game: "Game", my_color) -> list[PivotTrigger]:
    """Opp at 6+ VP, opp at 2+ played knights without LA holder, or
    opp 1 road from LR. Each is a different pivot."""
    from catanatron import Color
    from catanbot.config import close_to_win_vp, largest_army_threat_vp
    out: list[PivotTrigger] = []
    state = game.state
    ps = state.player_state
    close_vp = close_to_win_vp()
    la_threat_vp = largest_army_threat_vp()
    for col, idx in state.color_to_index.items():
        if col == my_color:
            continue
        vp = int(ps.get(f"P{idx}_VICTORY_POINTS", 0))
        if vp >= close_vp:
            out.append(PivotTrigger(
                name="opp_close_to_win",
                detail=f"opp at {vp} VP · tighten trades, deny resources",
                override_tag=None,
            ))
            break  # one is enough
    for col, idx in state.color_to_index.items():
        if col == my_color:
            continue
        played = int(ps.get(f"P{idx}_PLAYED_KNIGHT", 0))
        has_army = bool(ps.get(f"P{idx}_HAS_ARMY", False))
        vp = int(ps.get(f"P{idx}_VICTORY_POINTS", 0))
        if (played >= 2 and not has_army
                and vp >= la_threat_vp - 1):
            out.append(PivotTrigger(
                name="opp_close_to_la",
                detail=(f"opp on {played} knights · race to LA "
                        f"or commit to denial"),
                override_tag=None,
            ))
            break
    return out


def _detect_seven_overdue(roll_history: list[dict[str, Any]],
                          self_hand_size: int) -> PivotTrigger | None:
    """No 7 in the last 10 rolls AND self holds > discard_limit. The
    next 7 will hurt; pivot toward spending or trading down."""
    from catanbot.config import get_discard_limit
    limit = get_discard_limit()
    if self_hand_size <= limit:
        return None
    if not roll_history:
        return None
    recent = roll_history[-10:]
    if any(int(e.get("total") or 0) == 7 for e in recent):
        return None
    return PivotTrigger(
        name="seven_overdue",
        detail=(f"hand at {self_hand_size}, no 7 in 10 rolls · "
                "trade down before the next 7"),
        override_tag=None,
    )


def detect_pivot_triggers(
    game: "Game",
    my_color,
    *,
    roll_history: list[dict[str, Any]] | None = None,
    self_dev_just_bought: list[int] | None = None,
    self_hand_size: int = 0,
) -> list[PivotTrigger]:
    """Run every detector and return the fired list.

    Caller passes:
        ``roll_history``  — list of {"total": int, "blocked_you": bool, ...}
                            entries (the bridge keeps this in ``st``).
        ``self_dev_just_bought`` — type ints for dev cards bought THIS
                            turn (from ``sess.self_dev_bought_this_turn``).
        ``self_hand_size`` — total resource cards in hand.

    Each detector returns at most one trigger; the dev-draw detector
    can return multiple (one per drawn card). The result list keeps
    insertion order so the HUD renders the most-relevant pivot first.
    """
    from catanatron import Color
    try:
        my_enum = (my_color if isinstance(my_color, Color)
                   else Color[str(my_color).upper()])
    except Exception:  # noqa: BLE001
        return []
    out: list[PivotTrigger] = []

    # Hot number — needs the set of numbers self's settlements touch.
    nodes = _self_settlement_nodes(game, my_enum)
    my_numbers: set[int] = set()
    m = game.state.board.map
    for nid in nodes:
        for tile in m.adjacent_tiles.get(int(nid), []):
            if tile.number is not None:
                my_numbers.add(int(tile.number))
    hot = _detect_hot_number(roll_history or [], my_numbers)
    if hot:
        out.append(hot)

    out.extend(_detect_dev_card_drawn(self_dev_just_bought))
    out.extend(_detect_opp_close(game, my_enum))

    seven = _detect_seven_overdue(roll_history or [], self_hand_size)
    if seven:
        out.append(seven)

    return out


def merge_triggers_into_tag(
    tag: StrategyTag,
    triggers: list[PivotTrigger],
) -> StrategyTag:
    """Fold the trigger list into the tag's pivot_triggers + override_tag
    fields. Returns a NEW StrategyTag (immutable feel, easier to test).

    When multiple triggers carry an override_tag, the first one wins —
    matches the order in ``_PIVOT_TRIGGERS`` since detect_pivot_triggers
    yields in that order.
    """
    names = [t.name for t in triggers]
    override = next(
        (t.override_tag for t in triggers if t.override_tag is not None),
        None,
    )
    return StrategyTag(
        primary=tag.primary,
        fallback=tag.fallback,
        rationale=tag.rationale,
        phase=tag.phase,
        set_at_rolls=tag.set_at_rolls,
        pivot_triggers=names,
        override_tag=override,
        scores=dict(tag.scores),
    )
