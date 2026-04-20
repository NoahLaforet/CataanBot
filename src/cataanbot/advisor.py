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
    production: float
    resources: dict[str, float]  # resource name → per-roll yield
    tiles: list[tuple[str, int | None]]  # (resource_or_"DESERT", number)
    port: str | None  # "3:1", "WHEAT 2:1", etc., or None


def score_opening_nodes(game: "Game") -> list[NodeScore]:
    """Return every land node scored by total expected production, best first."""
    m = game.state.board.map
    scores: list[NodeScore] = []

    node_to_port = _build_node_port_labels(m)

    for node_id in m.land_nodes:
        counter = m.node_production.get(node_id, {})
        production = float(sum(counter.values()))
        resources = {r: float(v) for r, v in counter.items()}

        tiles = []
        for tile in m.adjacent_tiles.get(node_id, []):
            label = tile.resource if tile.resource else "DESERT"
            tiles.append((label, tile.number))

        scores.append(NodeScore(
            node_id=node_id,
            production=production,
            resources=resources,
            tiles=tiles,
            port=node_to_port.get(node_id),
        ))

    scores.sort(key=lambda s: s.production, reverse=True)
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
    opponent_blocked: int      # pip dots belonging to every other color
    victims: dict[str, int]    # opponent color → pip dots blocked on them
    opponent_hand_size: dict[str, int]  # opponent color → total cards in hand
    score: int                 # opponent_blocked - own_blocked


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

    # Precompute every opponent's hand size from player_state.
    state = game.state
    hand_sizes: dict[str, int] = {}
    for color, idx in state.color_to_index.items():
        total = sum(int(state.player_state.get(f"P{idx}_{r}_IN_HAND", 0))
                    for r in RESOURCES)
        hand_sizes[color.name] = total

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
        results.append(RobberScore(
            coord=coord,
            resource=tile.resource,
            number=tile.number,
            pip_dots=pip_dots,
            own_blocked=own_blocked,
            opponent_blocked=opponent_blocked,
            victims=victims,
            opponent_hand_size={c: hand_sizes.get(c, 0) for c in victims},
            score=opponent_blocked - own_blocked,
        ))

    # Sort: higher score first; tiebreak by largest single-victim hand size
    # (more cards → better steal EV), then by highest opponent_blocked.
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
              f"{'score':>5}  victims (pips blocked / cards in hand)")
    lines = [
        f"Best robber moves for {my_color} "
        f"(score = opponent pips blocked - your own):",
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
                f"{c} {r.victims[c]}p/{r.opponent_hand_size.get(c, 0)}c"
                for c in sorted(r.victims, key=lambda c: -r.victims[c])
            )
        else:
            victim_str = "(no opponents adjacent)"
        lines.append(
            f"{i:>4}  {coord_str:<12} {tile_str:<10} {r.pip_dots:>4}  "
            f"{r.score:>5}  {victim_str}"
        )
    return "\n".join(lines)


def format_opening_ranking(scores: list[NodeScore], top: int = 10) -> str:
    """Human-readable ranked list for the CLI."""
    header = f"{'rank':>4}  {'node':>4}  {'prod':>5}  {'tiles':<28}port"
    lines = [
        f"Top {min(top, len(scores))} opening settlement spots (by expected "
        f"production per roll):",
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
            f"{i:>4}  {s.node_id:>4}  {s.production:>5.2f}  "
            f"{tiles_str:<28}{port_str}"
        )
    return "\n".join(lines)
