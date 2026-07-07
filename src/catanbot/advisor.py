"""Opening-placement advisor.

Ranks every land node on a fresh Catan map by expected resource production,
so the player can see which opening settlement spots are strongest on this
particular board layout.

Production comes from catanatron's `map.node_production[node_id]` — a Counter
of resource → expected yield per dice roll. Summing it gives the classic
"total pip value" of a spot. We also note adjacent tiles (resource + number)
and any port access.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from catanatron import Game


@dataclass
class NodeScore:
    node_id: int
    raw_production: float            # sum of per-roll yields across tiles
    diversity_factor: float          # multiplier based on distinct resources
    port_bonus: float                # additive bonus for port access
    base_score: float                # raw_production * diversity + port_bonus
    denial_bonus: float              # bonus for being adjacent to high-value nodes
    blocking_bonus: float            # lookahead: how much the pick degrades
                                     # opponents' top-K remaining options
    score: float                     # base + denial + blocking
    resources: dict[str, float]      # resource name → per-roll yield
    tiles: list[tuple[str, int | None]]  # (resource_or_"DESERT", number)
    port: str | None                 # "3:1", "WHEAT 2:1", etc., or None

    # Kept for backwards compat with callers inspecting `production`.
    @property
    def production(self) -> float:
        return self.raw_production


# Diversity multiplier: 1 resource = 1.0 (no bonus), 2 = 1.08, 3 = 1.22.
# Encourages spots that give you flexibility, not just volume.
# Reddit 36k-game finding #3: raw pip count barely predicts wins —
# the highest-win-rate placement in the data (56.4%) had only 16 pips.
# Composition trumps volume, so the 3-distinct boost was bumped from
# 1.15 to 1.22 (a 3-resource node now reads about a third of a tile
# stronger than a same-pip 2-resource one). Still calibrated to leave
# a clear lower-pip 3-distinct vs higher-pip 1-distinct boundary
# rather than steamroll it.
_DIVERSITY_BY_COUNT = {0: 1.0, 1: 1.0, 2: 1.08, 3: 1.22}


# Per-resource weight applied to ``node_production`` cards-per-roll
# before raw is summed. Reddit 36k-game finding #2: wheat is the #1
# winning resource — used in every major build (settle, city, dev
# card). The flat sum treats it as equal-value-per-card with brick
# and ore, which under-weights wheat-bearing corners against ore-
# heavy stacks that benefit from the standard layout's clustering of
# 6/8 tiles on ore. The 1.10× boost is intentionally small: a real
# tiebreaker but never enough to flip across the diversity boundary
# on its own.
_RESOURCE_WEIGHT: dict[str, float] = {
    "WHEAT": 1.10,
    "WOOD": 1.0,
    "BRICK": 1.0,
    "SHEEP": 1.0,
    "ORE": 1.0,
}

# Wildcard weight for a gold/volcano hex (Volcano map). Gold pays a
# resource of your choice on its number, so a gold card is strictly more
# useful than any single fixed resource — you can always turn it into the
# scarcest thing you need. Weighted just above wheat (the best fixed
# resource) and counted as a distinct resource for diversity, since it
# covers whatever your other tiles miss. Read off ``map.gold_node_ids`` /
# ``map.gold_number`` annotated by colonist_map.annotate_gold_nodes().
_GOLD_WEIGHT = 1.25

# Fog-reveal weight for an unrevealed fog hex (Black Forest / Gold Rush).
# A road whose far end lands on a fog corner reveals the hex into a free
# resource. Capture analysis of real Gold Rush games: reveals skew toward
# scarce/absent resources (~77%) on strong numbers (~65% land on 5/6/8/9),
# so a fog corner behaves like a wildcard that fills whatever you're
# missing. We model each adjacent fog tile as a ~5/9 wildcard yield
# (4 pip-dots out of 36) weighted just like gold, since the reveal pays a
# resource of (effectively) your choice. Read off ``map.fog_node_ids``
# annotated by colonist_map.annotate_fog_nodes / refresh_fog_nodes.
_FOG_REVEAL_NUMBER = 5  # 5/6/8/9-class strong number; 4 pip-dots
_FOG_WEIGHT = 1.25


# Pip-dot table: each Catan number's odds out of 36 = "pips" — referenced
# both by the robber scorer (further down) and the port-pip-alignment
# guard in ``_port_bonus`` (just below). Hoisted to module top so the
# port helper can import it without a forward reference.
PIP_DOTS_BY_NUMBER = {
    2: 1, 12: 1,
    3: 2, 11: 2,
    4: 3, 10: 3,
    5: 4, 9: 4,
    6: 5, 8: 5,
}


def _weighted_raw_production(counter: dict) -> float:
    """raw production with the WHEAT bias applied. Equivalent to
    ``sum(counter.values())`` when all weights are 1.0."""
    return float(sum(
        v * _RESOURCE_WEIGHT.get(r, 1.0) for r, v in counter.items()))


def compute_table_scarcity(game: "Game") -> dict[str, float]:
    """Per-resource "table scarcity" score in [0, 1].

    Strategy v2 P2-11 (Gaminkid05): a port for a resource the rest of
    the table is short on is less valuable than the same port for an
    abundant resource — if opponents can't produce it, we can extract
    1:1 player trades anyway. Conversely, when the table is FLUSH on
    a resource, a port helps because no one will trade for it.

    Scarcity = 1 - (table_total_production_for_resource /
                    sum_table_total_production).

    With even production, scarcity = 4/5 = 0.8 for every resource. A
    resource with 50% of the table's production scores 0.5; a resource
    with 0 production scores 1.0 (truly scarce).

    Returns ``{}`` on failure so callers can default to a 0-bias.
    """
    out: dict[str, float] = {}
    try:
        m = game.state.board.map
        totals: dict[str, float] = {
            "WOOD": 0.0, "BRICK": 0.0, "SHEEP": 0.0,
            "WHEAT": 0.0, "ORE": 0.0,
        }
        for nid, (_col, btype) in game.state.board.buildings.items():
            mult = 2.0 if btype == "CITY" else 1.0
            for res, v in m.node_production.get(int(nid), {}).items():
                if res in totals:
                    totals[res] += mult * float(v)
        grand = sum(totals.values())
        if grand <= 0:
            return out
        for r, v in totals.items():
            out[r] = max(0.0, min(1.0, 1.0 - v / grand))
    except Exception:  # noqa: BLE001
        return {}
    return out


def _port_bonus(port_label: str | None, resources: dict[str, float],
                tiles: list[tuple[str, int | None]] | None = None,
                table_scarcity: dict[str, float] | None = None) -> float:
    """Additive bonus for port access, scaled by production alignment.

    All values are in cards-per-roll units to match raw_production
    (which sums ``map.node_production[node_id]`` — itself in cards-per-
    roll). The previous tuning was calibrated as if ``resources`` were
    pip counts (1-5), but the function is always called with cards-per-
    roll probabilities (0.028-0.139). That made the 3:1 generic bonus
    (0.10) the same scale as an entire tile of raw production, which
    let a 2-tile coastal corner with a 3:1 port outrank a 3-tile
    interior corner — a real misranking on any board with coastal 3:1s.

    Calibration:

    * **3:1 generic** — 0.005, a near-tiebreaker. Useless turn 1 (no
      cards to trade) and saves only 1 card per trade later. Should
      never tier-flip across the diversity boundary.
    * **2:1 on an unproduced resource** — 0.015, slightly above 3:1.
      Only valuable if you expand toward the matching resource, but
      strictly more option value than a generic 3:1.
    * **2:1 on a produced resource** — 0.30 × cards-per-roll on that
      resource. A 5-pip-tile wheat corner on a WHEAT 2:1 (0.139
      cards/roll) picks up ~0.042, comparable to a low-pip extra tile.
      A 1-pip-tile (0.028) picks up ~0.008. Linear in production so
      richer corners on matching ports keep their edge.

    ``tiles`` (optional, strategy v2 P1-7): the (resource, number)
    pairs touching this node. When provided, the produced-resource
    bonus is HALVED if the best matching-tile has pip <= 2 (numbers
    2/3/11/12) — chalks777's note that real port plays only happen
    on strong-pip alignment. Calls that pre-date the strategy v2 work
    pass tiles=None and get the original (uniform) curve.
    """
    if not port_label:
        return 0.0
    if port_label == "3:1":
        return 0.005
    # "WHEAT 2:1", "ORE 2:1", etc.
    port_resource = port_label.split(" ", 1)[0]
    res_prod = float(resources.get(port_resource, 0.0))
    if res_prod > 0:
        bonus = 0.30 * res_prod
        # Pip-alignment penalty: a 2:1 wheat port is much weaker when
        # the only wheat tile in reach is a 2/3/11/12. Halve when the
        # best pip on the matching resource's adjacent tile is <= 2.
        if tiles:
            best_match_pip = 0
            for res, num in tiles:
                if res != port_resource or num is None:
                    continue
                p = PIP_DOTS_BY_NUMBER.get(int(num), 0)
                if p > best_match_pip:
                    best_match_pip = p
            if 0 < best_match_pip <= 2:
                bonus *= 0.5
        # Strategy v2 P2-11: table-scarcity dampening. Even-distribution
        # baseline is 0.8 (1 - 1/5). When the port resource is *more*
        # scarce table-wide than that — i.e. opponents barely produce
        # it — a port loses some value because we can extract 1:1
        # player trades for our surplus instead. The damp scales
        # linearly past the 0.8 baseline up to a cap of 0.7× at fully
        # absent (scarcity == 1.0). Below 0.8 (resource is abundant
        # table-wide) the bonus stays at full strength.
        if table_scarcity:
            sc = float(table_scarcity.get(port_resource, 0.0))
            if sc > 0.8:
                bonus *= 1.0 - (sc - 0.8) * 1.5  # 0.8→1.0 ; 1.0→0.7
        return bonus
    return 0.015


# Per-adjacent-node weight for the denial bonus. Kept small so denial is a
# tiebreaker among comparable spots, not a primary driver. At w=0.04, a
# cluster-center spot that locks out two ~0.45-scoring neighbors picks up
# ~0.036, enough to float past isolated peers but not to flip the top tier.
_DENIAL_WEIGHT = 0.04

# Blocking-bonus tuning. Blocking asks a sharper question than denial: if
# I take this node (and its neighbors become illegal by distance rule),
# how much worse is the opponent's best remaining option? Measured as the
# drop in the top-K base-score sum caused by removing my pick + its
# neighbors from the candidate pool.
_BLOCKING_TOP_K = 3
_BLOCKING_WEIGHT = 0.05


def score_opening_nodes(game: "Game",
                        legal_nodes: set[int] | None = None) -> list[NodeScore]:
    """Return every land node scored for opening placement, best first.

    Score = base_score + denial_bonus + blocking_bonus, where:
        base_score   = raw_production × diversity_factor + port_bonus
        denial_bonus = _DENIAL_WEIGHT × Σ base_score(neighbor)
        blocking_bonus = _BLOCKING_WEIGHT × (baseline_top_K − remaining_top_K)

    Denial reflects the distance-rule consequence: taking node N locks out
    every node one edge away. A spot surrounded by other high-value spots
    is strictly more valuable than an equally-scoring isolated spot because
    claiming it denies opponents the cluster.

    Diversity rewards nodes that touch 3 distinct resources over ones that
    stack on a single resource even at equal pip sum — early-game you want
    access to building materials, not volume of one commodity. Port bonus
    is small; it breaks ties among otherwise similar spots.

    `legal_nodes`, when given, restricts the candidate pool, the blocking
    baseline, and the denial neighbor set. Use this when advising on a
    live game where some spots are already taken or distance-blocked —
    denying an already-taken neighbor isn't worth anything, so dropping
    them from the denial sum is consistent.
    """
    m = game.state.board.map
    node_to_port = _build_node_port_labels(m)
    neighbors = _build_node_neighbors(m)
    all_land_nodes = set(m.land_nodes)
    land_nodes = (all_land_nodes & legal_nodes
                  if legal_nodes is not None else all_land_nodes)
    # Strategy v2 P2-11: table scarcity is computed once per scoring
    # pass (cheap — single iteration over all buildings) and passed
    # into _port_bonus to dampen ports for resources opponents won't
    # have. Empty during fully empty boards (no buildings yet).
    table_scarcity = compute_table_scarcity(game)

    # Gold/volcano wildcard hex (Volcano map). Nodes touching it get a
    # wildcard yield valued just above wheat and an extra diversity slot,
    # since gold pays whatever resource you choose. Empty on classic maps.
    gold_node_ids = getattr(m, "gold_node_ids", frozenset())
    gold_number = getattr(m, "gold_number", None)
    gold_yield = (PIP_DOTS_BY_NUMBER.get(int(gold_number), 0) / 36.0
                  if gold_number else 0.0)

    # Fog-reveal wildcard (Black Forest / Gold Rush). Nodes touching an
    # unrevealed fog hex get a per-fog-tile wildcard yield, since a road
    # there reveals a free, scarce-biased resource. Empty on classic maps
    # and shrinks to empty as the fog reveals over the game.
    fog_node_ids = getattr(m, "fog_node_ids", frozenset())
    fog_yield = PIP_DOTS_BY_NUMBER.get(_FOG_REVEAL_NUMBER, 0) / 36.0
    # Tile ids of the unrevealed fog hexes. A fog hex builds as a resource-
    # less, number-less LandTile whose every corner was added to
    # fog_node_ids by annotate_fog_nodes, so we recover the set by matching
    # that signature. Lets a single node count >1 reveal when it sits on
    # the seam between two fog hexes.
    fog_tile_ids: set[int] = set()
    if fog_node_ids:
        for tile in m.land_tiles.values():
            if (getattr(tile, "resource", None) is None
                    and getattr(tile, "number", None) is None
                    and set(int(n) for n in tile.nodes.values())
                    <= fog_node_ids):
                tid = getattr(tile, "id", None)
                if tid is not None:
                    fog_tile_ids.add(int(tid))

    # Pass 1: compute base_score per node.
    base_by_node: dict[int, float] = {}
    scratch: dict[int, dict] = {}
    for node_id in land_nodes:
        counter = m.node_production.get(node_id, {})
        raw = _weighted_raw_production(counter)
        resources = {r: float(v) for r, v in counter.items()}

        distinct = sum(1 for v in resources.values() if v > 0)
        port_label = node_to_port.get(node_id)
        tiles: list[tuple[str, int | None]] = []
        for tile in m.adjacent_tiles.get(node_id, []):
            label = tile.resource if tile.resource else "DESERT"
            tiles.append((label, tile.number))
        # Gold adjacency: add the wildcard yield to raw production, expose
        # it as a GOLD resource, bump diversity by one (it covers any
        # missing resource), and relabel one DESERT tile slot as GOLD.
        if node_id in gold_node_ids and gold_yield > 0:
            raw += gold_yield * _GOLD_WEIGHT
            resources["GOLD"] = resources.get("GOLD", 0.0) + gold_yield
            distinct += 1
            for i, (lbl, num) in enumerate(tiles):
                if lbl == "DESERT" and num is None:
                    tiles[i] = ("GOLD", gold_number)
                    break
        # Fog adjacency: each unrevealed fog tile this node touches is a
        # wildcard reveal. Add its yield to raw, expose a FOG resource,
        # bump diversity once (a reveal covers a missing resource), and
        # relabel a DESERT slot (fog hexes build resource-less) per fog
        # tile. catanatron has no fog tile, so adjacency comes off the
        # annotated fog_node_ids rather than the tile labels.
        if node_id in fog_node_ids and fog_yield > 0:
            fog_tiles_here = sum(
                1 for tile in m.adjacent_tiles.get(node_id, [])
                if getattr(tile, "id", None) in fog_tile_ids
            )
            # Fall back to a single reveal when we can't pin tile ids: the
            # node is in fog_node_ids so at least one fog tile is adjacent.
            if fog_tiles_here <= 0:
                fog_tiles_here = 1
            raw += fog_yield * _FOG_WEIGHT * fog_tiles_here
            resources["FOG"] = (resources.get("FOG", 0.0)
                                + fog_yield * fog_tiles_here)
            distinct += 1
            relabelled = 0
            for i, (lbl, num) in enumerate(tiles):
                if relabelled >= fog_tiles_here:
                    break
                if lbl == "DESERT" and num is None:
                    tiles[i] = ("FOG", _FOG_REVEAL_NUMBER)
                    relabelled += 1
        diversity = _DIVERSITY_BY_COUNT.get(distinct, 1.22)
        # Pass tiles into _port_bonus so the pip-alignment guard fires
        # — a 2:1 wheat port adjacent only to a wheat 2/3/11/12 tile
        # gets half the bonus, matching the strategy v2 P1-7 plan.
        # Plus the table-scarcity dampening from P2-11.
        port_bonus = _port_bonus(
            port_label, resources, tiles=tiles,
            table_scarcity=table_scarcity or None,
        )
        base = raw * diversity + port_bonus
        base_by_node[node_id] = base

        scratch[node_id] = dict(
            raw=raw, diversity=diversity, port_bonus=port_bonus,
            resources=resources, tiles=tiles, port_label=port_label,
        )

    # Baseline top-K base scores across the whole board, for blocking.
    baseline_sorted = sorted(base_by_node.values(), reverse=True)
    baseline_top_k = sum(baseline_sorted[:_BLOCKING_TOP_K])

    # Pass 2: add denial + blocking, assemble final NodeScores.
    scores: list[NodeScore] = []
    for node_id, fields in scratch.items():
        denial = _DENIAL_WEIGHT * sum(
            base_by_node[n]
            for n in neighbors.get(node_id, ())
            if n in base_by_node
        )
        # Blocking: simulate the pick by removing node_id + its neighbors
        # (distance rule) and see how much the top-K remaining drops.
        excluded = {node_id} | neighbors.get(node_id, set())
        remaining_sorted = sorted(
            (v for n, v in base_by_node.items() if n not in excluded),
            reverse=True,
        )
        remaining_top_k = sum(remaining_sorted[:_BLOCKING_TOP_K])
        blocking = _BLOCKING_WEIGHT * max(0.0, baseline_top_k - remaining_top_k)

        base = base_by_node[node_id]
        scores.append(NodeScore(
            node_id=node_id,
            raw_production=fields["raw"],
            diversity_factor=fields["diversity"],
            port_bonus=fields["port_bonus"],
            base_score=base,
            denial_bonus=denial,
            blocking_bonus=blocking,
            score=base + denial + blocking,
            resources=fields["resources"],
            tiles=fields["tiles"],
            port=fields["port_label"],
        ))

    scores.sort(key=lambda s: s.score, reverse=True)
    return scores


def _build_node_port_labels(m) -> dict[int, str]:
    """Map each port-adjacent node_id to a short label like "WHEAT 2:1" or "3:1".

    Uses the same intersection logic as the renderer: a port's terminal nodes
    are the ones in the port's own hex that also belong to `port_nodes[resource]`.
    For 3:1 ports where the generic set includes all 8 nodes, fall back to the
    port's ocean-facing edge.
    """
    from catanatron.models.map import EdgeRef
    port_nodes = m.port_nodes
    labels: dict[int, str] = {}
    for port in m.ports_by_id.values():
        resource = port.resource
        generic = resource is None
        candidates = set(port.nodes.values())
        terminals = [n for n in candidates if n in port_nodes.get(resource, set())]
        if len(terminals) != 2:
            try:
                edge_ref = EdgeRef[port.direction.name]
                edge = port.edges.get(edge_ref)
                terminals = list(edge) if edge else []
            except (KeyError, AttributeError):
                terminals = []
        label = "3:1" if generic else f"{resource} 2:1"
        for n in terminals:
            labels[n] = label
    return labels


# --- robber advisor ------------------------------------------------------
# (PIP_DOTS_BY_NUMBER lives near the top of this module so the port
# helper can use it without a forward reference.)


@dataclass
class RobberScore:
    coord: tuple[int, int, int]
    resource: str | None
    number: int | None
    pip_dots: int
    own_blocked: int           # pip dots belonging to my buildings
    opponent_blocked: int      # raw pip dots belonging to every other color
    victims: dict[str, int]    # opponent color → pip dots blocked on them
    victim_vp: dict[str, int]  # opponent color → current public VP
    opponent_hand_size: dict[str, int]  # opponent color → total cards in hand
    weighted_opponent_blocked: float  # opponent_blocked with VP weighting
    # Strategy v2 P1-5 — robber as resource-control tool. These three
    # additive bonuses extend the scoring beyond "block opp pips":
    resource_need_bonus: float = 0.0  # bump when this tile produces a
                                      # resource we owe for the next build
    monopoly_setup_bonus: float = 0.0  # bump when locking the tile makes
                                       # our share of the resource much
                                       # bigger than the table average —
                                       # sets up a future trade monopoly
    score: float = 0.0          # weighted_opponent_blocked - own_blocked
                                # + resource_need + monopoly_setup


def _vp_weight(vp: int, vp_target: int | None = None) -> float:
    """Scale blocking value by how close the victim is to winning.

    At vp = early-game baseline (~30% of target) the weight is 1.0.
    Above that the weight ramps by 0.4 per VP, so in a 10 VP game:
    3 → 1.0, 6 → 2.2, 9 → 3.4 — the old calibration. For a 12 VP game
    the baseline shifts to 4 VP, so 4 → 1.0, 7 → 2.2, etc. Linear
    above baseline is simple and matches the intuition that each extra
    VP past the opening phase makes the player more urgent to stop."""
    from catanbot.config import early_game_baseline_vp
    baseline = early_game_baseline_vp(vp_target) if vp_target else \
        early_game_baseline_vp()
    return 1.0 + 0.4 * max(0, vp - baseline)


def score_robber_targets(
    game: "Game", my_color: str,
    hand_size_override: dict[str, int] | None = None,
    friendly_robber_min_vp: int | None = None,
    imminent_color: str | None = None,
    needed_resources: list[str] | None = None,
    opp_production_by_resource: dict[str, dict[str, float]] | None = None,
    self_production_by_resource: dict[str, float] | None = None,
    vp_override: dict[str, int] | None = None,
) -> list[RobberScore]:
    """Rank every land tile (except where the robber is now) for blocking value.

    Score is `opponent_pips_blocked - own_pips_blocked`, where a settlement
    on an adjacent node contributes 1× the tile's pip dots and a city
    contributes 2×. The desert (no number) scores 0 but is still a valid
    "unblock yourself" target if the robber is currently hurting you.

    ``hand_size_override`` replaces the catanatron-derived opponent hand
    sizes on a per-color basis. The live bridge passes in the WS-derived
    card counts, which are ground truth even when per-resource tracking
    has drifted (trades, steals, discards we didn't fully see).

    ``friendly_robber_min_vp`` (when given) filters victims to only those
    with VP > the threshold. Colonist's "Friendly Robber" optional rule
    protects players at or below the threshold (typically 2 — newly
    placed players have exactly 2 from setup). Tiles whose only victims
    are protected are dropped from the ranking entirely; tiles with a
    mix score only the un-protected victims' pips.

    ``imminent_color`` (when given) flags an opp whose threat-level is
    "imminent" — they could win on their next turn. Tiles adjacent to
    this color get a 2× extra VP-weight multiplier so robber-target
    ranking strongly prefers shutting them down over equal-pip tiles
    of other opps. Live bridge passes this when leader-threat detector
    fires imminent.
    """
    from catanatron import Color
    from catanatron.state import RESOURCES

    board = game.state.board
    m = board.map
    my_color_enum = Color[my_color.upper()]
    current_robber = board.robber_coordinate

    # Precompute every opponent's hand size + public VP from player_state.
    state = game.state
    hand_sizes: dict[str, int] = {}
    vp_by_color: dict[str, int] = {}
    for color, idx in state.color_to_index.items():
        hand_sizes[color.name] = sum(
            int(state.player_state.get(f"P{idx}_{r}_IN_HAND", 0))
            for r in RESOURCES
        )
        vp_by_color[color.name] = int(
            state.player_state.get(f"P{idx}_VICTORY_POINTS", 0)
        )
    if hand_size_override:
        for c, n in hand_size_override.items():
            hand_sizes[c.upper()] = int(n)
    # VP_OVERRIDE: prefer the colonist victoryPointsState figure
    # over catanatron's player_state when the bridge passes one
    # through. Catanatron's count drifts on missed VP-card buys
    # and on settle/city events the tracker silently skips —
    # leaving the robber scorer with stale numbers that compound
    # through _vp_weight (Noah saw an opp at 14 VP per catanatron
    # but 11 VP per colonist on 2026-05-04, which made every
    # robber score a +100 outlier).
    if vp_override:
        for c, n in vp_override.items():
            vp_by_color[c.upper()] = int(n)

    results: list[RobberScore] = []
    for coord, tile in m.land_tiles.items():
        if coord == current_robber:
            continue  # rule: robber must actually move
        pip_dots = PIP_DOTS_BY_NUMBER.get(tile.number, 0)
        own_blocked = 0
        victims: dict[str, int] = {}
        for node_id in tile.nodes.values():
            entry = board.buildings.get(node_id)
            if entry is None:
                continue
            color, kind = entry
            weight = 2 if kind == "CITY" else 1
            contribution = pip_dots * weight
            if color == my_color_enum:
                own_blocked += contribution
            else:
                # Friendly Robber: skip victims whose VP is at or below
                # the protection threshold. Colonist's optional rule
                # makes those tiles unblockable, so a tile whose only
                # adjacent opps are protected scores 0 — same effect as
                # an unbuilt tile, just by drop-out.
                if (friendly_robber_min_vp is not None
                        and vp_by_color.get(color.name, 0)
                        <= friendly_robber_min_vp):
                    continue
                victims[color.name] = victims.get(color.name, 0) + contribution
        # Tiles where every adjacent opp is protected drop out
        # entirely (no victims → score 0 → filtered below).
        opponent_blocked = sum(victims.values())
        imminent_norm = (imminent_color.upper()
                         if imminent_color else None)
        weighted = sum(
            pips * _vp_weight(vp_by_color.get(c, 0))
            * (2.0 if imminent_norm == c.upper() else 1.0)
            for c, pips in victims.items()
        )
        # Strategy v2 P1-5 — robber as resource-control tool.
        #
        # resource_need_bonus: when this tile produces a resource we
        # owe for our next planned build, blocking it (a) denies an
        # opponent the production AND (b) sets up a steal of that
        # exact resource. Worth more than blocking a generic high-
        # pip resource we don't care about.
        #
        # monopoly_setup_bonus: when (my production share of this
        # resource) is already high relative to the table's average
        # share, locking the tile concentrates the resource further
        # — opponents will have to come to us to trade for it. Caps
        # at +1.0 so it can't dwarf the base block score.
        resource_need_bonus = 0.0
        monopoly_setup_bonus = 0.0
        tile_res = tile.resource
        if tile_res:
            if needed_resources and tile_res in needed_resources:
                resource_need_bonus = 1.0 + 0.2 * pip_dots
            if (opp_production_by_resource is not None
                    and self_production_by_resource is not None):
                self_p = float(self_production_by_resource.get(
                    tile_res, 0.0))
                opp_total = sum(
                    float(opp_production_by_resource.get(c, {})
                          .get(tile_res, 0.0))
                    for c in victims)
                table_total = self_p + opp_total
                if table_total > 0:
                    self_share = self_p / table_total
                    # 1/(N+1) is the "even split" baseline among self
                    # plus victims-on-this-tile. If we're well above
                    # that, locking the tile concentrates further.
                    n_players = 1 + len(victims)
                    even_share = 1.0 / max(1, n_players)
                    surplus = max(0.0, self_share - even_share)
                    monopoly_setup_bonus = min(
                        1.0, surplus * pip_dots * 0.6)
        score = (weighted - own_blocked
                 + resource_need_bonus + monopoly_setup_bonus)
        results.append(RobberScore(
            coord=coord,
            resource=tile.resource,
            number=tile.number,
            pip_dots=pip_dots,
            own_blocked=own_blocked,
            opponent_blocked=opponent_blocked,
            victims=victims,
            victim_vp={c: vp_by_color.get(c, 0) for c in victims},
            opponent_hand_size={c: hand_sizes.get(c, 0) for c in victims},
            weighted_opponent_blocked=weighted,
            resource_need_bonus=resource_need_bonus,
            monopoly_setup_bonus=monopoly_setup_bonus,
            score=score,
        ))

    # Sort: higher score first; tiebreak by largest single-victim hand size
    # (more cards → better steal EV), then by raw (unweighted) opponent pips.
    results.sort(key=lambda r: (
        -r.score,
        -max(r.opponent_hand_size.values(), default=0),
        -r.opponent_blocked,
    ))
    return results


def format_robber_ranking(scores: list[RobberScore], my_color: str,
                          top: int = 8) -> str:
    my_color = my_color.upper()
    header = (f"{'rank':>4}  {'coord':<12} {'tile':<10} {'pips':>4}  "
              f"{'score':>6}  victims (pips / VP / hand)")
    lines = [
        f"Best robber moves for {my_color} "
        f"(score = VP-weighted opponent pips blocked - your own):",
        "",
        header,
        "-" * len(header),
    ]
    if not scores:
        lines.append("  (no legal targets, board has no land tiles off the robber?)")
        return "\n".join(lines)
    for i, r in enumerate(scores[:top], start=1):
        coord_str = f"({r.coord[0]},{r.coord[1]},{r.coord[2]})"
        if r.resource is None:
            tile_str = "DESERT"
        else:
            tile_str = f"{r.resource[:3]}{'' if r.number is None else r.number}"
        if r.victims:
            victim_str = ", ".join(
                f"{c} {r.victims[c]}p/{r.victim_vp.get(c, 0)}vp/"
                f"{r.opponent_hand_size.get(c, 0)}c"
                for c in sorted(r.victims, key=lambda c: -r.victims[c])
            )
        else:
            victim_str = "(no opponents adjacent)"
        lines.append(
            f"{i:>4}  {coord_str:<12} {tile_str:<10} {r.pip_dots:>4}  "
            f"{r.score:>6.1f}  {victim_str}"
        )
    return "\n".join(lines)


# --- second-settlement advisor ------------------------------------------
# How many distinct resources the (F, N) pair covers → small flat bonus.
# Covering 4+ resources opens up the most build options; 5 is jackpot.
_COMBINED_DIVERSITY_BONUS = {0: 0.0, 1: 0.0, 2: 0.0,
                             3: 0.05, 4: 0.15, 5: 0.25}


@dataclass
class OpeningRoad:
    """A recommended direction for the settlement-paired opening road."""
    edge: tuple[int, int]       # (second_settlement_node, adjacent_node)
    far_node: int               # the road's non-settlement endpoint
    landing_node: int | None    # best prospective 3rd-settlement spot beyond
    landing_score: float        # production value of that prospective spot
    landing_tiles: list[tuple[str, int | None]]


@dataclass
class SecondSettleScore:
    node_id: int
    raw_production: float                       # N's total per-roll yield
    resources: dict[str, float]                 # N's per-roll yield, by resource
    complement_value: float                     # Σ N.yield(r) × marginal_at_F(r)
    combined_distinct: int                      # distinct resources in F ∪ N
    diversity_bonus: float
    port: str | None
    port_bonus: float
    tiles: list[tuple[str, int | None]]
    score: float
    best_road: OpeningRoad | None               # best direction for the free road


def legal_nodes_after_picks(game: "Game",
                            picks: list[int]) -> set[int]:
    """Return the land-node set that remains legal after pretending `picks`
    are all placed (each pick removes itself + its distance-rule neighbors).

    This is a *hypothetical* tool — it doesn't touch the game state, just
    does the math so callers can re-score with `score_opening_nodes(game,
    legal_nodes=...)` and see how the top-N shifts."""
    m = game.state.board.map
    neighbors = _build_node_neighbors(m)
    legal = set(m.land_nodes)
    for pick in picks:
        if pick in legal:
            legal.discard(pick)
        for nb in neighbors.get(pick, ()):
            legal.discard(nb)
    return legal


def _build_node_neighbors(m) -> dict[int, set[int]]:
    """Undirected node graph, built from every tile's hex-edge cycle.

    Two land nodes are neighbors iff they share an edge on some tile.
    Walking across ocean tiles is fine for the graph — catanatron's tile
    edges are shared between adjacent hexes so the result is connected."""
    neighbors: dict[int, set[int]] = {}
    for tile in m.tiles.values():
        for edge in tile.edges.values():
            a, b = edge
            neighbors.setdefault(a, set()).add(b)
            neighbors.setdefault(b, set()).add(a)
    return neighbors


def _fog_reveal_nodes(m) -> frozenset:
    """The node ids that touch an unrevealed fog hex, defaulting to empty."""
    return getattr(m, "fog_node_ids", frozenset()) or frozenset()


def _fog_reveal_value(m, node_id: int,
                      fog_nodes: frozenset | None = None) -> float:
    """Reveal EV for a node that sits on the fog ring.

    A road landing on a fog corner reveals each adjacent fog hex into a
    free, scarce-biased resource, so we treat every fog tile this node
    touches as a ~5/9 wildcard yield. Returns 0.0 when the node touches no
    fog or the board has no fog. ``fog_nodes`` may be passed to avoid the
    repeated attribute read in a hot loop.
    """
    if fog_nodes is None:
        fog_nodes = _fog_reveal_nodes(m)
    if not fog_nodes or int(node_id) not in fog_nodes:
        return 0.0
    fog_yield = PIP_DOTS_BY_NUMBER.get(_FOG_REVEAL_NUMBER, 0) / 36.0
    # Count fog hexes adjacent to this node: a fog hex builds resource-less
    # and number-less with every corner inside fog_nodes.
    count = 0
    for tile in m.adjacent_tiles.get(int(node_id), []):
        if (getattr(tile, "resource", None) is None
                and getattr(tile, "number", None) is None
                and set(int(n) for n in tile.nodes.values()) <= fog_nodes):
            count += 1
    if count <= 0:
        count = 1
    return fog_yield * _FOG_WEIGHT * count


def _best_opening_road(
    m, first_node: int, second_node: int,
    neighbors: dict[int, set[int]],
    land_nodes: set[int],
) -> OpeningRoad | None:
    """For each edge outward from `second_node`, pick the best landing spot.

    The "landing" is a neighbor of the road's far end — a prospective 3rd
    settlement spot. We score it by raw_production and also require it
    satisfies the distance rule against both F and N (i.e. not adjacent
    to either)."""
    out_edges = []
    for far in neighbors.get(second_node, ()):
        if far == first_node:
            continue  # road to F is legal but pointless for expansion
        out_edges.append((second_node, far))

    fn_neighbors = neighbors.get(first_node, set()) | {first_node}
    sn_neighbors = neighbors.get(second_node, set()) | {second_node}
    fog_nodes = _fog_reveal_nodes(m)

    best: OpeningRoad | None = None
    for edge in out_edges:
        far = edge[1]
        # Reveal EV the road itself buys: pushing into a fog hex flips it,
        # so a road whose far end touches fog is worth the wildcard reveal
        # even before any 3rd-settle landing. Added to every candidate (and
        # the no-landing fallback) so fog-bound roads surface in recs.
        far_fog = _fog_reveal_value(m, far, fog_nodes)
        candidates = []
        for landing in neighbors.get(far, ()):
            if landing == second_node or landing in fn_neighbors \
                    or landing in sn_neighbors or landing not in land_nodes:
                continue
            prod = float(sum(m.node_production.get(landing, {}).values()))
            prod += far_fog + _fog_reveal_value(m, landing, fog_nodes)
            tiles = []
            for tile in m.adjacent_tiles.get(landing, []):
                label = tile.resource if tile.resource else "DESERT"
                tiles.append((label, tile.number))
            candidates.append((prod, landing, tiles))
        if candidates:
            candidates.sort(key=lambda c: -c[0])
            prod, landing, tiles = candidates[0]
        elif far_fog > 0:
            # No legal 3rd-settle landing past this far node, but the road
            # still reveals fog, so keep it as a direction with reveal value.
            prod, landing, tiles = far_fog, None, []
        else:
            prod, landing, tiles = 0.0, None, []
        road = OpeningRoad(edge=edge, far_node=far, landing_node=landing,
                           landing_score=prod, landing_tiles=tiles)
        if best is None or road.landing_score > best.landing_score:
            best = road
    return best


def score_second_settlements(
    game: "Game", first_node_id: int, color: str = "RED",
    legal_nodes: set[int] | None = None,
) -> list[SecondSettleScore]:
    """Rank legal second-settlement nodes given first settlement at `first_node_id`.

    The main term is *complement value*: each candidate's per-resource yield
    weighted by its marginal value to F (rarer-at-F resources are worth more).
    This way a candidate giving ORE+BRICK next to a WHEAT-heavy F outranks
    a candidate with slightly higher raw pips that mostly stacks wheat.

    Adds small bonuses for combined resource diversity (F ∪ N covering 4–5
    distinct resources) and port access — ports are most valuable when the
    combined F+N production of the ported resource is high, since excess is
    what feeds maritime trades.

    Only nodes legal under the distance rule are returned. ``legal_nodes``
    overrides the default (catanatron's ``buildable_node_ids``), so callers
    can score a hypothetical pairing on a board where ``first_node_id``
    hasn't actually been placed yet — e.g. the round-1 overlay wanting to
    preview "if I settle here, my best round-2 N would be..." before any
    settlement exists."""
    from catanatron import Color
    from catanatron.state import RESOURCES

    b = game.state.board
    m = b.map
    if first_node_id not in m.land_nodes:
        raise ValueError(f"node {first_node_id} is not a land node")

    c = Color[color.upper()]
    if legal_nodes is None:
        legal = set(b.buildable_node_ids(c, initial_build_phase=True))
    else:
        legal = set(legal_nodes)
    node_to_port = _build_node_port_labels(m)
    neighbors = _build_node_neighbors(m)
    land_nodes = set(m.land_nodes)

    F_prod = {r: float(m.node_production.get(first_node_id, {}).get(r, 0.0))
              for r in RESOURCES}
    marginal_at_F = {r: 1.0 / (0.5 + F_prod[r]) for r in RESOURCES}
    # Strategy v2 P2-11: compute table scarcity once for the pass and
    # reuse across every candidate's port_bonus call.
    table_scarcity = compute_table_scarcity(game)

    results: list[SecondSettleScore] = []
    for node_id in m.land_nodes:
        if node_id not in legal or node_id == first_node_id:
            continue
        N_prod = {r: float(m.node_production.get(node_id, {}).get(r, 0.0))
                  for r in RESOURCES}
        raw = sum(N_prod.values())
        complement = sum(N_prod[r] * marginal_at_F[r] for r in RESOURCES)

        combined = {r: F_prod[r] + N_prod[r] for r in RESOURCES}
        combined_distinct = sum(1 for v in combined.values() if v > 0)
        diversity_bonus = _COMBINED_DIVERSITY_BONUS.get(combined_distinct, 0.25)

        # Share the first-settle port-bonus curve (scales with produced
        # pips on the port resource). Second-settle plugs in the combined
        # pair production so a port pick only blossoms when the two-node
        # plan actually feeds it.
        port_label = node_to_port.get(node_id)
        tiles: list[tuple[str, int | None]] = []
        for tile in m.adjacent_tiles.get(node_id, []):
            label = tile.resource if tile.resource else "DESERT"
            tiles.append((label, tile.number))
        # Pass N's tiles to the port helper so the pip-alignment guard
        # fires on a port whose matching resource only sits on a weak
        # 2/3/11/12 tile of N. (Strategy v2 P1-7.)
        # Add table_scarcity from P2-11 — same dampening as the first-
        # settle pass uses.
        port_bonus = _port_bonus(
            port_label, combined, tiles=tiles,
            table_scarcity=table_scarcity or None,
        )

        best_road = _best_opening_road(m, first_node_id, node_id,
                                       neighbors, land_nodes)

        results.append(SecondSettleScore(
            node_id=node_id,
            raw_production=raw,
            resources=N_prod,
            complement_value=complement,
            combined_distinct=combined_distinct,
            diversity_bonus=diversity_bonus,
            port=port_label,
            port_bonus=port_bonus,
            tiles=tiles,
            score=complement + diversity_bonus + port_bonus,
            best_road=best_road,
        ))

    results.sort(key=lambda r: -r.score)
    return results


def format_second_settlement_ranking(
    scores: list[SecondSettleScore], first_node_id: int, top: int = 10,
) -> str:
    header = (f"{'rank':>4}  {'node':>4}  {'score':>5}  {'comp':>5}  "
              f"{'raw':>5}  {'#res':>4}  {'tiles':<28}{'port':<12}road → landing")
    lines = [
        f"Top {min(top, len(scores))} second-settlement picks "
        f"given first at node {first_node_id} "
        f"(score = complement + diversity + port):",
        "",
        header,
        "-" * len(header),
    ]
    if not scores:
        lines.append("  (no legal nodes, check the first placement is on the board)")
        return "\n".join(lines)
    for i, s in enumerate(scores[:top], start=1):
        tiles_str = ", ".join(
            f"{res[:3]}{'' if num is None else num}"
            for res, num in s.tiles
        )
        port_str = s.port or ""
        if s.best_road and s.best_road.landing_node is not None:
            landing_tiles = ", ".join(
                f"{res[:3]}{'' if num is None else num}"
                for res, num in s.best_road.landing_tiles
            )
            road_str = (f"{s.node_id}-{s.best_road.far_node} → "
                        f"{s.best_road.landing_node} "
                        f"({s.best_road.landing_score:.2f}: {landing_tiles})")
        elif s.best_road:
            road_str = f"{s.node_id}-{s.best_road.far_node} (no landing spot)"
        else:
            road_str = "(no outgoing edges)"
        lines.append(
            f"{i:>4}  {s.node_id:>4}  {s.score:>5.2f}  "
            f"{s.complement_value:>5.2f}  {s.raw_production:>5.2f}  "
            f"{s.combined_distinct:>4}  {tiles_str:<28}{port_str:<12}{road_str}"
        )
    return "\n".join(lines)


# --- trade evaluator -----------------------------------------------------
@dataclass
class TradeEval:
    color: str
    give: tuple[int, str]            # (amount, resource) you give up
    get: tuple[int, str]             # (amount, resource) you receive
    production: dict[str, float]     # expected yield per roll, per resource
    ports: set[str]                  # resources with a 2:1 port, plus "GENERIC" for 3:1
    marginal_values: dict[str, float]
    give_value: float
    get_value: float
    delta: float                     # get_value - give_value; positive = favorable


def player_production(game: "Game", color: str) -> dict[str, float]:
    """Expected per-roll yield for a color, weighted by settlement=1 / city=2."""
    from catanatron import Color
    from catanatron.state import RESOURCES
    c = Color[color.upper()]
    board = game.state.board
    m = board.map
    prod = {r: 0.0 for r in RESOURCES}
    for node_id, (bc, kind) in board.buildings.items():
        if bc != c:
            continue
        weight = 2 if kind == "CITY" else 1
        for resource, yield_ in m.node_production.get(node_id, {}).items():
            if resource in prod:
                prod[resource] += weight * float(yield_)
    return prod


def player_ports(game: "Game", color: str) -> set[str]:
    """Return the set of resources this color has a 2:1 port on.

    A generic 3:1 port contributes the sentinel string "GENERIC". Returns
    an empty set if the color has no coastal buildings yet."""
    from catanatron import Color
    from catanatron.models.map import EdgeRef
    c = Color[color.upper()]
    board = game.state.board
    m = board.map
    port_nodes = m.port_nodes
    owned: set[str] = set()
    for port in m.ports_by_id.values():
        resource = port.resource
        candidates = set(port.nodes.values())
        terminals = [n for n in candidates
                     if n in port_nodes.get(resource, set())]
        if len(terminals) != 2:
            try:
                edge_ref = EdgeRef[port.direction.name]
                edge = port.edges.get(edge_ref)
                terminals = list(edge) if edge else []
            except (KeyError, AttributeError):
                terminals = []
        if any(board.buildings.get(n, (None,))[0] == c for n in terminals):
            owned.add(resource if resource is not None else "GENERIC")
    return owned


def _marginal_value(resource: str, prod: dict[str, float],
                    ports: set[str]) -> float:
    """Value of one more card of `resource` to this player, on the margin.

    Scarcer resources are worth more (1 / (floor + production)). Port
    ownership on the resource counts as extra effective production, since
    excess can be converted at a good rate."""
    p = prod.get(resource, 0.0)
    if resource in ports:       # 2:1 on this resource
        p += 1.0
    elif "GENERIC" in ports:    # 3:1 any
        p += 0.5
    return 1.0 / (0.5 + p)


def evaluate_trade(game: "Game", color: str,
                   give_amount: int, give_resource: str,
                   get_amount: int, get_resource: str) -> TradeEval:
    """Evaluate a trade from `color`'s perspective."""
    from catanatron.state import RESOURCES
    give_resource = give_resource.upper()
    get_resource = get_resource.upper()
    for r in (give_resource, get_resource):
        if r not in RESOURCES:
            raise ValueError(f"unknown resource {r!r}; use one of "
                             f"{', '.join(RESOURCES)}")

    prod = player_production(game, color)
    ports = player_ports(game, color)
    marginal = {r: _marginal_value(r, prod, ports) for r in RESOURCES}

    give_value = marginal[give_resource] * give_amount
    get_value = marginal[get_resource] * get_amount

    return TradeEval(
        color=color.upper(),
        give=(give_amount, give_resource),
        get=(get_amount, get_resource),
        production=prod,
        ports=ports,
        marginal_values=marginal,
        give_value=give_value,
        get_value=get_value,
        delta=get_value - give_value,
    )


def format_trade_eval(e: TradeEval) -> str:
    verdict = (
        "favorable" if e.delta > 0.05 else
        "unfavorable" if e.delta < -0.05 else
        "roughly even"
    )
    give_n, give_r = e.give
    get_n, get_r = e.get
    port_note = ""
    if e.ports:
        labels = []
        for p in sorted(e.ports):
            labels.append("3:1 any" if p == "GENERIC" else f"{p} 2:1")
        port_note = f"  (ports: {', '.join(labels)})"
    lines = [
        f"Trade eval for {e.color}: give {give_n} {give_r.lower()} "
        f"→ get {get_n} {get_r.lower()}",
        f"  production snapshot{port_note}:",
    ]
    prod_cells = "   ".join(
        f"{r.lower()}={e.production[r]:.2f}" for r in e.production
    )
    lines.append(f"    {prod_cells}")
    lines.append(
        f"  your {give_r.lower()} marginal value: "
        f"{e.marginal_values[give_r]:.2f}  × {give_n}  = "
        f"{e.give_value:.2f}"
    )
    lines.append(
        f"  your {get_r.lower()} marginal value:  "
        f"{e.marginal_values[get_r]:.2f}  × {get_n}  = "
        f"{e.get_value:.2f}"
    )
    lines.append(f"  delta: {e.delta:+.2f}  ({verdict} for {e.color})")
    return "\n".join(lines)


def format_opening_ranking(scores: list[NodeScore], top: int = 10) -> str:
    """Human-readable ranked list for the CLI."""
    header = (f"{'rank':>4}  {'node':>4}  {'score':>5}  {'base':>5}  "
              f"{'deny':>5}  {'block':>5}  {'raw':>5}  {'tiles':<28}port")
    lines = [
        f"Top {min(top, len(scores))} opening settlement spots "
        f"(score = base + denial + blocking):",
        "",
        header,
        "-" * len(header),
    ]
    for i, s in enumerate(scores[:top], start=1):
        tiles_str = ", ".join(
            f"{res[:3]}{'' if num is None else num}"
            for res, num in s.tiles
        )
        port_str = s.port or ""
        lines.append(
            f"{i:>4}  {s.node_id:>4}  {s.score:>5.2f}  "
            f"{s.base_score:>5.2f}  {s.denial_bonus:>5.2f}  "
            f"{s.blocking_bonus:>5.2f}  {s.raw_production:>5.2f}  "
            f"{tiles_str:<28}{port_str}"
        )
    return "\n".join(lines)
