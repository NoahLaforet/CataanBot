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
