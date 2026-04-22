"""Turn-action recommender — the "what should I do" output.

Given the tracker's catanatron game plus the self-color's current hand,
returns a ranked list of actionable recommendations. Each recommendation
is a concrete move (build settlement at node N, build road on edge E,
upgrade city at node N, buy dev card) with a heuristic score so the
overlay can surface the top pick.

Scope is deliberately narrow: this is a *heuristic* advisor, not a full
AlphaBeta/ValueFunction search. It reuses the opening-placement scoring
in ``advisor.py`` to rank settlement and city spots by pip-production,
and extends that to roads by scoring each buildable edge by the best
settlement spot it opens up.

Callers pass ``my_turn`` from colonist's ``currentTurnPlayerColor`` —
recommendations off-turn are actionable state (e.g. ports you could
trade into), but the initial cut only fires when it's actually your turn.
"""
from __future__ import annotations

from typing import Any


def _hand_can_afford(hand: dict[str, int], cost: dict[str, int]) -> bool:
    return all(hand.get(r, 0) >= n for r, n in cost.items())


_SETTLEMENT_COST = {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1}
_CITY_COST = {"WHEAT": 2, "ORE": 3}
_ROAD_COST = {"WOOD": 1, "BRICK": 1}
_DEV_COST = {"SHEEP": 1, "WHEAT": 1, "ORE": 1}


def _node_pip_production(m, node_id: int) -> float:
    """Sum of pip-weighted resource yield for a node, including desert (0)."""
    return float(sum(m.node_production.get(node_id, {}).values()))


def _tile_label(m, node_id: int) -> list[tuple[str, int | None]]:
    out = []
    for tile in m.adjacent_tiles.get(node_id, []):
        label = tile.resource if tile.resource else "DESERT"
        out.append((label, tile.number))
    return out


def recommend_actions(
    game, color, hand: dict[str, int], *, top: int = 4,
) -> list[dict[str, Any]]:
    """Rank what to do with the current hand.

    ``color`` is a ``catanatron.Color`` enum or the string name (RED/etc).
    ``hand`` is a ``{resource: count}`` dict in catanatron canonical
    names (WOOD/BRICK/SHEEP/WHEAT/ORE).

    Returns up to ``top`` dicts, sorted by heuristic score descending:
        {kind, score, detail, node_id?, edge?, tiles?}
    where ``kind`` ∈ {settlement, city, road, dev_card}.
    """
    from catanatron import Color

    c = color if isinstance(color, Color) else Color[str(color).upper()]
    m = game.state.board.map
    recs: list[dict[str, Any]] = []

    # --- Settlements -----------------------------------------------------
    # buildable_node_ids respects distance-2 + road-connectivity rules.
    if _hand_can_afford(hand, _SETTLEMENT_COST):
        try:
            nodes = game.state.board.buildable_node_ids(
                c, initial_build_phase=False)
        except Exception:  # noqa: BLE001
            nodes = []
        scored = [
            (node, _node_pip_production(m, node)) for node in nodes
        ]
        scored.sort(key=lambda s: -s[1])
        for node, prod in scored[:3]:
            # node_production is per-roll probability (~0.05-1.5). Scale by
            # 10 so a 0.5-prod settlement scores ~5 — clearly ahead of a
            # dev-card (1.5) but not so inflated that weak spots swamp it.
            recs.append({
                "kind": "settlement",
                "node_id": int(node),
                "score": round(prod * 10.0, 2),
                "detail": f"prod {prod:.2f}",
                "tiles": _tile_label(m, int(node)),
            })

    # --- City upgrades ---------------------------------------------------
    # Any settlement I own, ranked by production (city doubles yield).
    if _hand_can_afford(hand, _CITY_COST):
        for node_id, (bcol, btype) in game.state.board.buildings.items():
            if bcol != c or btype != "SETTLEMENT":
                continue
            prod = _node_pip_production(m, int(node_id))
            # City doubles yield on an existing corner — upgrade value ≈
            # extra production (one more copy) + 1 VP. Same prod scale as
            # settlement but with a +2 base to reflect the VP pop.
            recs.append({
                "kind": "city",
                "node_id": int(node_id),
                "score": round(prod * 8.0 + 2.0, 2),
                "detail": f"2× prod at {prod:.2f}",
                "tiles": _tile_label(m, int(node_id)),
            })

    # --- Roads -----------------------------------------------------------
    # Each buildable edge scored by the best settlement spot its far end
    # opens up (ignoring distance-2 against the player's own building at
    # the near end — catanatron handles that for actual placement).
    if _hand_can_afford(hand, _ROAD_COST):
        try:
            edges = list(game.state.board.buildable_edges(c))
        except Exception:  # noqa: BLE001
            edges = []
        land = set(m.land_nodes)
        existing_buildings = set(game.state.board.buildings.keys())
        # Distance-2 neighbors of any existing building are blocked.
        from cataanbot.advisor import _build_node_neighbors
        neighbors = _build_node_neighbors(m)
        blocked = set(existing_buildings)
        for b in existing_buildings:
            blocked |= neighbors.get(b, set())
        edge_scores: list[tuple[tuple[int, int], float, int | None]] = []
        for (a, b) in edges:
            far = b if a in existing_buildings or a in blocked else b
            # Look at both endpoints' neighbors for new reachable spots.
            best_land_prod = 0.0
            best_land_node: int | None = None
            for end in (a, b):
                for nb in neighbors.get(end, ()):
                    if nb in blocked or nb not in land:
                        continue
                    p = _node_pip_production(m, nb)
                    if p > best_land_prod:
                        best_land_prod = p
                        best_land_node = nb
            if best_land_prod > 0 and best_land_node is not None:
                edge_scores.append(((int(a), int(b)),
                                    best_land_prod, best_land_node))
        edge_scores.sort(key=lambda s: -s[1])
        if edge_scores:
            (edge, prod, landing) = edge_scores[0]
            # Road reaches a settle spot eventually — lower score than a
            # direct build since you still have to save for the settle.
            # Roads are a means — you still have to save for the settle.
            # Score at ~40% of settling there outright.
            recs.append({
                "kind": "road",
                "edge": list(edge),
                "landing_node": landing,
                "score": round(prod * 4.0, 2),
                "detail": f"→ {prod:.2f}-prod spot",
                "tiles": _tile_label(m, landing) if landing else [],
            })

    # --- Dev card --------------------------------------------------------
    # Always a sane fallback. Score low so real builds outrank it, but
    # non-zero so it surfaces when nothing else is affordable.
    if _hand_can_afford(hand, _DEV_COST):
        recs.append({
            "kind": "dev_card",
            "score": 1.2,
            "detail": "knight / VP / road-building / YoP / monopoly",
        })

    recs.sort(key=lambda r: -float(r.get("score", 0)))
    return recs[:top]
