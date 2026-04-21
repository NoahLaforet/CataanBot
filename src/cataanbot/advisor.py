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


# Diversity multiplier: 1 resource = 1.0 (no bonus), 2 = 1.05, 3 = 1.15.
# Encourages spots that give you flexibility, not just volume.
_DIVERSITY_BY_COUNT = {0: 1.0, 1: 1.0, 2: 1.05, 3: 1.15}


def _port_bonus(port_label: str | None, resources: dict[str, float]) -> float:
    """Small additive bonus for port access.

    2:1 on a resource the node itself produces is worth the most — you can
    immediately offload your excess. 2:1 on an unrelated resource and 3:1
    generic ports are both mild bonuses since they require cards you don't
    yet have."""
    if not port_label:
        return 0.0
    if port_label == "3:1":
        return 0.02
    # "WHEAT 2:1", "ORE 2:1", etc.
    port_resource = port_label.split(" ", 1)[0]
    if resources.get(port_resource, 0.0) > 0:
        return 0.06
    return 0.02


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

    # Pass 1: compute base_score per node.
    base_by_node: dict[int, float] = {}
    scratch: dict[int, dict] = {}
    for node_id in land_nodes:
        counter = m.node_production.get(node_id, {})
        raw = float(sum(counter.values()))
        resources = {r: float(v) for r, v in counter.items()}

        distinct = sum(1 for v in resources.values() if v > 0)
        diversity = _DIVERSITY_BY_COUNT.get(distinct, 1.15)
        port_label = node_to_port.get(node_id)
        port_bonus = _port_bonus(port_label, resources)
        base = raw * diversity + port_bonus
        base_by_node[node_id] = base

        tiles = []
        for tile in m.adjacent_tiles.get(node_id, []):
            label = tile.resource if tile.resource else "DESERT"
            tiles.append((label, tile.number))

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
PIP_DOTS_BY_NUMBER = {
    2: 1, 12: 1,
    3: 2, 11: 2,
    4: 3, 10: 3,
    5: 4, 9: 4,
    6: 5, 8: 5,
}


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
    score: float               # weighted_opponent_blocked - own_blocked


def _vp_weight(vp: int) -> float:
    """Scale blocking value by how close the victim is to winning.

    3 VP → 1.0 (baseline early game), 6 → 2.2, 9 → 3.4. Linear above 3
    is simple and matches the intuition that each extra VP past the
    opening phase makes the player more urgent to stop."""
    return 1.0 + 0.4 * max(0, vp - 3)


def score_robber_targets(game: "Game", my_color: str) -> list[RobberScore]:
    """Rank every land tile (except where the robber is now) for blocking value.

    Score is `opponent_pips_blocked - own_pips_blocked`, where a settlement
    on an adjacent node contributes 1× the tile's pip dots and a city
    contributes 2×. The desert (no number) scores 0 but is still a valid
    "unblock yourself" target if the robber is currently hurting you.
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
                victims[color.name] = victims.get(color.name, 0) + contribution
        opponent_blocked = sum(victims.values())
        weighted = sum(
            pips * _vp_weight(vp_by_color.get(c, 0))
            for c, pips in victims.items()
        )
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
            score=weighted - own_blocked,
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
        lines.append("  (no legal targets — board has no land tiles off the robber?)")
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

    best: OpeningRoad | None = None
    for edge in out_edges:
        far = edge[1]
        candidates = []
        for landing in neighbors.get(far, ()):
            if landing == second_node or landing in fn_neighbors \
                    or landing in sn_neighbors or landing not in land_nodes:
                continue
            prod = float(sum(m.node_production.get(landing, {}).values()))
            tiles = []
            for tile in m.adjacent_tiles.get(landing, []):
                label = tile.resource if tile.resource else "DESERT"
                tiles.append((label, tile.number))
            candidates.append((prod, landing, tiles))
        if candidates:
            candidates.sort(key=lambda c: -c[0])
            prod, landing, tiles = candidates[0]
        else:
            prod, landing, tiles = 0.0, None, []
        road = OpeningRoad(edge=edge, far_node=far, landing_node=landing,
                           landing_score=prod, landing_tiles=tiles)
        if best is None or road.landing_score > best.landing_score:
            best = road
    return best


def score_second_settlements(
    game: "Game", first_node_id: int, color: str = "RED",
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

    Only nodes legal under the distance rule are returned."""
    from catanatron import Color
    from catanatron.state import RESOURCES

    b = game.state.board
    m = b.map
    if first_node_id not in m.land_nodes:
        raise ValueError(f"node {first_node_id} is not a land node")

    c = Color[color.upper()]
    legal = set(b.buildable_node_ids(c, initial_build_phase=True))
    node_to_port = _build_node_port_labels(m)
    neighbors = _build_node_neighbors(m)
    land_nodes = set(m.land_nodes)

    F_prod = {r: float(m.node_production.get(first_node_id, {}).get(r, 0.0))
              for r in RESOURCES}
    marginal_at_F = {r: 1.0 / (0.5 + F_prod[r]) for r in RESOURCES}

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

        port_label = node_to_port.get(node_id)
        if port_label is None:
            port_bonus = 0.0
        elif port_label == "3:1":
            port_bonus = 0.03
        else:
            port_resource = port_label.split(" ", 1)[0]
            combined_r = combined.get(port_resource, 0.0)
            # Port is most valuable when you already produce that resource;
            # a 2:1 port on a resource you don't touch is near-useless.
            port_bonus = 0.03 + combined_r * 0.3

        tiles = []
        for tile in m.adjacent_tiles.get(node_id, []):
            label = tile.resource if tile.resource else "DESERT"
            tiles.append((label, tile.number))

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
        lines.append("  (no legal nodes — check the first placement is on the board)")
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
