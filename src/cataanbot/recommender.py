"""Turn-action recommender — the "what should I do" output.

Given the tracker's catanatron game plus the self-color's current hand,
returns a ranked list of actionable recommendations. Each recommendation
is a concrete move (build settlement at node N, build road on edge E,
upgrade city at node N, buy dev card) with a heuristic **1-10 score** so
the overlay can surface the top pick.

Score calibration (all kinds share the same 1-10 scale so they're
directly comparable):
    * 10 = exceptional move (best spot on the board, big VP swing)
    * 7-9 = strong, clearly worth doing this turn
    * 4-6 = decent, solid progress
    * 1-3 = weak, usually a last-resort

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


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _score_settlement(prod: float) -> float:
    """Settlement 1-10 score. A ~0.83-prod corner (a pristine 6/8/10
    triangle) pins at 10; a 0.1-prod wasteland sits at ~2."""
    return round(_clip(prod * 12.0 + 2.0, 2.0, 10.0), 1)


def _score_city(prod: float) -> float:
    """City 1-10 score. Adds a +3 base for the VP pop on top of the
    doubled production — a decent city at 0.4 prod scores ~7."""
    return round(_clip(prod * 10.0 + 3.0, 4.0, 10.0), 1)


def _score_road(landing_prod: float) -> float:
    """Road is a means-to-end: you still need to save for the settlement.
    Caps at 7 so a direct settle always ranks above a road toward the
    same spot."""
    return round(_clip(landing_prod * 9.0, 1.0, 7.0), 1)


_DEV_CARD_SCORE = 3.0

def _best_trade_rate(resource: str, owned_nodes: set[int],
                     port_nodes) -> int:
    """Cheapest bank/port trade rate the player can access for this
    resource. ``port_nodes`` is catanatron's ``map.port_nodes`` — a
    ``None`` key holds the set of generic (3:1) port nodes, each
    resource key holds its 2:1 port nodes."""
    specific = port_nodes.get(resource) or set()
    if owned_nodes & set(specific):
        return 2
    generic = port_nodes.get(None) or set()
    if owned_nodes & set(generic):
        return 3
    return 4


def _find_trade_source(hand: dict[str, int], need_resource: str,
                       rate: int,
                       reserved: dict[str, int] | None = None) -> str | None:
    """Pick a resource whose *excess* over what this build needs is ≥
    the trade rate. ``reserved`` is the build's cost dict — we must
    leave that much behind so the build itself is still possible.

    Picks the largest excess stockpile to minimize disruption to future
    turns. Returns None if nothing's tradeable."""
    candidates = []
    reserved = reserved or {}
    for res, n in hand.items():
        if res == need_resource:
            continue
        excess = n - reserved.get(res, 0)
        if excess >= rate:
            candidates.append((res, excess))
    if not candidates:
        return None
    candidates.sort(key=lambda rn: -rn[1])
    return candidates[0][0]


def _hand_can_afford(hand: dict[str, int], cost: dict[str, int]) -> bool:
    return all(hand.get(r, 0) >= n for r, n in cost.items())


_SETTLEMENT_COST = {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1}
_CITY_COST = {"WHEAT": 2, "ORE": 3}
_ROAD_COST = {"WOOD": 1, "BRICK": 1}
_DEV_COST = {"SHEEP": 1, "WHEAT": 1, "ORE": 1}

# Max missing-card budget for a "save for X" plan. Two-off is a realistic
# 1-2 turn away target; anything further is noise.
_PLAN_MAX_MISSING = 2

_RES_TITLE = {
    "WOOD": "Wood", "BRICK": "Brick", "SHEEP": "Sheep",
    "WHEAT": "Wheat", "ORE": "Ore",
}


def _missing_for(hand: dict[str, int],
                 cost: dict[str, int]) -> dict[str, int]:
    return {r: n - hand.get(r, 0) for r, n in cost.items()
            if hand.get(r, 0) < n}


def _format_missing(missing: dict[str, int]) -> str:
    parts = [f"{n} {_RES_TITLE.get(r, r.title())}"
             for r, n in missing.items()]
    return "need " + ", ".join(parts)


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

    Each rec carries a ``when`` tag:
        "now"  — affordable this turn
        "soon" — 1-2 cards off; surface as a "save for X" planning hint
    UI typically groups by ``when``.

    Returns up to ``top`` dicts, sorted by heuristic score descending:
        {kind, when, score, detail, node_id?, edge?, tiles?, missing?}
    where ``kind`` ∈ {settlement, city, road, dev_card}.
    """
    from catanatron import Color

    c = color if isinstance(color, Color) else Color[str(color).upper()]
    m = game.state.board.map
    recs: list[dict[str, Any]] = []

    def _best_settlement_spot() -> tuple[int, float] | None:
        try:
            nodes = game.state.board.buildable_node_ids(
                c, initial_build_phase=False)
        except Exception:  # noqa: BLE001
            return None
        scored = [(node, _node_pip_production(m, node)) for node in nodes]
        scored.sort(key=lambda s: -s[1])
        return (int(scored[0][0]), scored[0][1]) if scored else None

    def _best_owned_settlement() -> tuple[int, float] | None:
        best = None
        for node_id, (bcol, btype) in game.state.board.buildings.items():
            if bcol != c or btype != "SETTLEMENT":
                continue
            prod = _node_pip_production(m, int(node_id))
            if best is None or prod > best[1]:
                best = (int(node_id), prod)
        return best

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
            recs.append({
                "kind": "settlement",
                "when": "now",
                "node_id": int(node),
                "score": _score_settlement(prod),
                "detail": f"prod {prod:.2f}/roll",
                "tiles": _tile_label(m, int(node)),
            })

    # --- City upgrades ---------------------------------------------------
    # Any settlement I own, ranked by production (city doubles yield).
    if _hand_can_afford(hand, _CITY_COST):
        for node_id, (bcol, btype) in game.state.board.buildings.items():
            if bcol != c or btype != "SETTLEMENT":
                continue
            prod = _node_pip_production(m, int(node_id))
            recs.append({
                "kind": "city",
                "when": "now",
                "node_id": int(node_id),
                "score": _score_city(prod),
                "detail": f"2× prod ({prod:.2f}/roll) + 1 VP",
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
            recs.append({
                "kind": "road",
                "when": "now",
                "edge": list(edge),
                "landing_node": landing,
                "score": _score_road(prod),
                "detail": f"→ {prod:.2f}-prod spot",
                "tiles": _tile_label(m, landing) if landing else [],
            })

    # --- Dev card --------------------------------------------------------
    # Always a sane fallback. Fixed score of 3 on the 1-10 scale — real
    # builds usually outrank it, but it surfaces when nothing else fits.
    if _hand_can_afford(hand, _DEV_COST):
        recs.append({
            "kind": "dev_card",
            "when": "now",
            "score": _DEV_CARD_SCORE,
            "detail": "knight / VP / road-building / YoP / monopoly",
        })

    # --- "Save for X" plans ---------------------------------------------
    # When a bigger purchase is 1-2 cards away, surface it so the user can
    # decide to hold rather than spend on whatever's affordable now.
    # e.g. road is affordable but a settlement is 1 Sheep away → the
    # overlay shows both and the user picks.
    if not _hand_can_afford(hand, _SETTLEMENT_COST):
        missing = _missing_for(hand, _SETTLEMENT_COST)
        if 0 < sum(missing.values()) <= _PLAN_MAX_MISSING:
            best = _best_settlement_spot()
            if best is not None:
                node, prod = best
                recs.append({
                    "kind": "settlement",
                    "when": "soon",
                    "node_id": node,
                    "score": _score_settlement(prod),
                    "missing": missing,
                    "detail": (f"{_format_missing(missing)} "
                               f"· {prod:.2f}/roll target"),
                    "tiles": _tile_label(m, node),
                })
    if not _hand_can_afford(hand, _CITY_COST):
        missing = _missing_for(hand, _CITY_COST)
        if 0 < sum(missing.values()) <= _PLAN_MAX_MISSING:
            best = _best_owned_settlement()
            if best is not None:
                node, prod = best
                recs.append({
                    "kind": "city",
                    "when": "soon",
                    "node_id": node,
                    "score": _score_city(prod),
                    "missing": missing,
                    "detail": (f"{_format_missing(missing)} "
                               f"· 2×{prod:.2f}/roll + 1 VP"),
                    "tiles": _tile_label(m, node),
                })
    if not _hand_can_afford(hand, _DEV_COST):
        missing = _missing_for(hand, _DEV_COST)
        if 0 < sum(missing.values()) <= _PLAN_MAX_MISSING:
            recs.append({
                "kind": "dev_card",
                "when": "soon",
                "score": _DEV_CARD_SCORE,
                "missing": missing,
                "detail": (f"{_format_missing(missing)} "
                           f"· knight / VP / road / YoP / mono"),
            })

    # --- Bank/port trades ------------------------------------------------
    # If a build is blocked by exactly 1 missing card and we're sitting
    # on enough of some other resource to bank-trade (4:1) or port-trade
    # (3:1 generic, 2:1 specific), suggest the trade. The trade rec is
    # tagged ``when: "now"`` because bank trades execute on the same
    # turn — it unlocks the build right now. Score matches the unlocked
    # build minus a small efficiency penalty so a direct build always
    # edges ahead of "trade + build".
    owned_building_nodes = {
        int(n) for n, (bcol, _) in game.state.board.buildings.items()
        if bcol == c
    }
    port_nodes = getattr(m, "port_nodes", {}) or {}
    _trade_targets = [
        ("settlement", _SETTLEMENT_COST, _score_settlement,
         _best_settlement_spot),
        ("city",       _CITY_COST,       _score_city,
         _best_owned_settlement),
        ("dev_card",   _DEV_COST,        lambda _p: _DEV_CARD_SCORE,
         lambda: (0, 0.0)),
    ]
    for kind, cost, score_fn, target_fn in _trade_targets:
        if _hand_can_afford(hand, cost):
            continue
        missing = _missing_for(hand, cost)
        if sum(missing.values()) != 1:
            continue  # Multi-trade plans get too expensive to be useful.
        need_res = next(iter(missing))
        rate_needed = _best_trade_rate(need_res, owned_building_nodes,
                                       port_nodes)
        # Reserve the build's cost; any excess beyond that is tradeable.
        source = _find_trade_source(hand, need_res, rate_needed, cost)
        if source is None:
            continue
        target = target_fn()
        if target is None:
            continue
        node_or_none, prod = (target if isinstance(target, tuple)
                              else (None, 0.0))
        base_score = score_fn(prod)
        # Small penalty for trade inefficiency — a direct build next
        # turn often nets more cards back. Cap at 9.5.
        trade_score = round(min(base_score - 0.5, 9.5), 1)
        rate_label = {2: "2:1 port", 3: "3:1 port",
                      4: "4:1 bank"}[rate_needed]
        rec = {
            "kind": "trade",
            "when": "now",
            "score": trade_score,
            "give": {source: rate_needed},
            "get": {need_res: 1},
            "unlocks": kind,
            "detail": (f"{rate_needed} {_RES_TITLE.get(source, source)} "
                       f"→ 1 {_RES_TITLE.get(need_res, need_res)} "
                       f"· {rate_label} · unlocks {kind}"),
        }
        if node_or_none is not None:
            rec["node_id"] = int(node_or_none)
            rec["tiles"] = _tile_label(m, int(node_or_none))
        recs.append(rec)
        # One trade suggestion per turn is plenty — don't flood the
        # overlay. The highest-impact one (settlement > city > dev)
        # wins by virtue of being considered first.
        break

    # Sort by score descending. Ties break with "now" before "soon" so the
    # act-now option ranks above an equally-scored plan.
    recs.sort(key=lambda r: (-float(r.get("score", 0)),
                             0 if r.get("when") == "now" else 1))
    return recs[:top]
