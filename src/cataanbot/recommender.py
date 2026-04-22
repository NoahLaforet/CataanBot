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


def _score_opening(raw_score: float) -> float:
    """Opening-settlement 1-10 calibration. base_score+denial+blocking
    typically lives in [0.0, 0.6] — a ~0.5 top-of-board spot pins near
    10, a ~0.1 leftover lands around 3.5."""
    return round(_clip(raw_score * 15.0 + 2.0, 2.0, 10.0), 1)


def _score_second_settle(raw_score: float) -> float:
    """Round-2 pick 1-10 calibration. complement + diversity + port for
    the top paired candidate typically sits in [0.3, 1.5] — a strong
    complement pairs toward 10, a meh filler lands around 3."""
    return round(_clip(raw_score * 6.0 + 1.5, 2.0, 10.0), 1)


def _resources_covered(*prod_maps: dict[str, float]) -> set[str]:
    """Union of resource names with positive production across inputs."""
    out: set[str] = set()
    for pm in prod_maps:
        for r, v in pm.items():
            if v > 0.0:
                out.add(r)
    return out


def _resources_added(base: dict[str, float],
                     addend: dict[str, float]) -> list[str]:
    """Resources ``addend`` produces that ``base`` doesn't cover yet."""
    base_covered = {r for r, v in base.items() if v > 0.0}
    return [r for r, v in addend.items()
            if v > 0.0 and r not in base_covered]


def _label_archetype(tiles_f: list, tiles_n: list,
                     f_port: str | None,
                     n_port: str | None) -> str | None:
    """Tag a coordinated F+N plan with a strategic archetype.

    Labels are based on tile-count across F ∪ N (not production magnitude —
    count mirrors how players pick, e.g. "I have 2 ore tiles so I'm going
    ore-city"). Returns one of "ore-city", "wood-first", "dev-card",
    "balanced", "port", or ``None`` if the combo doesn't fit a profile.

    Priority: a 2:1 port on a produced resource trumps other labels, since
    it reshapes how you'll convert surplus across the whole game. Then
    ore-city (2+ ore + wheat), wood-first (heavy wood/brick), balanced
    (4+ distinct resources), dev-card (the three dev-buy resources
    sheep+wheat+ore all present without enough wood/brick to road-spam
    or ore to city-rush — the fallback "pivot to knights + VP cards"
    path when a more aggressive archetype isn't available).
    """
    counts: dict[str, int] = {}
    for res, _num in list(tiles_f) + list(tiles_n):
        if res == "DESERT":
            continue
        counts[res] = counts.get(res, 0) + 1
    for port in (f_port, n_port):
        if not port or port == "3:1":
            continue
        port_res = port.split(" ", 1)[0]
        if counts.get(port_res, 0) > 0:
            return "port"
    ore = counts.get("ORE", 0)
    wheat = counts.get("WHEAT", 0)
    wood = counts.get("WOOD", 0)
    brick = counts.get("BRICK", 0)
    sheep = counts.get("SHEEP", 0)
    if ore >= 2 and wheat >= 1:
        return "ore-city"
    if (wood + brick) >= 3 or (wood >= 2 and brick >= 1):
        return "wood-first"
    if len(counts) >= 4:
        return "balanced"
    # Dev-card pivot: all three dev-card ingredients produced, but not
    # enough wood/brick to road-spam, not enough ore to city-rush, and
    # coverage too narrow to call balanced. The "mediocre settlement,
    # best option is dev cards" fork — a deliberate fallback rather than
    # a first pick.
    if (sheep >= 1 and wheat >= 1 and ore >= 1
            and (wood + brick) <= 2 and ore < 2):
        return "dev-card"
    return None


def recommend_opening(game, color, *, top: int = 5) -> list[dict[str, Any]]:
    """Rank remaining opening settlement spots during the setup phase.

    Adaptive by construction: each call re-reads the current buildings
    and re-filters ``legal_nodes_after_picks``, so the ranking shifts
    automatically as opponents place. Tied into the live bridge, this
    means the overlay's opening picks update on every WS frame without
    any state of its own.

    Returns up to ``top`` dicts with the normal rec shape
    (``kind="opening_settlement"``, ``score``, ``detail``, ``node_id``,
    ``tiles``) so the overlay renders them through the same path as
    mid-game recs.

    Callers are expected to only invoke this during setup — passing a
    mid-game state just returns an empty list because buildable_node_ids
    plus distance-2 already rules out every "opening" spot by then.
    """
    from catanatron import Color
    from catanatron.state import RESOURCES
    from cataanbot.advisor import (
        _build_node_neighbors, legal_nodes_after_picks,
        score_opening_nodes, score_second_settlements,
    )

    # Color is optional — during round-1 of the opening the bridge calls
    # in with None because self_color_id hasn't latched yet. The picks
    # themselves are public board info, so we just skip the round-2 hint.
    if color is None:
        c = None
    elif isinstance(color, Color):
        c = color
    else:
        try:
            c = Color[str(color).upper()]
        except KeyError:
            c = None
    placed = [
        int(nid) for nid, (_col, btype)
        in game.state.board.buildings.items()
        if btype == "SETTLEMENT"
    ]
    # If every color has already placed both opening settlements, the
    # pick loop is moot — distance-2 legal nodes may still exist on
    # paper but colonist won't let anyone drop another opening
    # settlement. Skip straight to the road-followup so the overlay
    # surfaces the "finish your road" hint instead of stale picks.
    num_players = len(game.state.colors)
    m = game.state.board.map
    neighbors = _build_node_neighbors(m)
    # Opening-road scoring reuses the settlement scores: the best road
    # points toward an expansion corridor. Score per-node via the full
    # board (not restricted to `legal`) so we can weigh the 2-hop
    # reachable node even when it's currently blocked by the proposed
    # settlement's distance rule — it'll reopen once someone moves.
    full_scored = {ns.node_id: ns for ns in score_opening_nodes(game)}
    if len(placed) >= 2 * num_players:
        if c is None:
            return []
        return _opening_road_followup(
            game=game, c=c, neighbors=neighbors,
            scored_by_node=full_scored, m=m,
        )
    # Note whether I already have a settlement down (round-2 context).
    my_placed = 0 if c is None else sum(
        1 for nid, (col, bt) in game.state.board.buildings.items()
        if col == c and bt == "SETTLEMENT"
    )
    recs: list[dict[str, Any]] = []

    # --- Round 2: complement-aware ranking against my placed F -----------
    # Once my first settlement is down, "best 2nd pick" isn't about pips
    # in isolation — it's about what F is missing. Defer to the paired
    # scorer so a sheep/ore corner edges out a higher-pip but overlapping
    # wheat pick.
    if my_placed == 1 and c is not None:
        my_first = next(
            int(nid) for nid, (col, bt)
            in game.state.board.buildings.items()
            if col == c and bt == "SETTLEMENT"
        )
        F_prod = {r: float(m.node_production.get(my_first, {}).get(r, 0.0))
                  for r in RESOURCES}
        pair_scored = score_second_settlements(game, my_first, color=c.name)
        for s in pair_scored[:top]:
            new_res = _resources_added(F_prod, s.resources)
            coverage = len(_resources_covered(F_prod, s.resources))
            detail_parts = [f"pip {s.raw_production:.2f}/roll"]
            if new_res:
                added_abbrev = "+".join(r[:3].lower() for r in new_res)
                detail_parts.append(f"adds {added_abbrev}")
            detail_parts.append(f"covers {coverage}/5")
            if s.port:
                detail_parts.append(f"port {s.port}")
            detail_parts.append("2nd pick")
            road: dict[str, Any] | None = None
            if (s.best_road is not None
                    and s.best_road.landing_node is not None):
                road = {
                    "edge": [int(s.node_id), int(s.best_road.far_node)],
                    "toward_node": int(s.best_road.landing_node),
                    "toward_tiles": s.best_road.landing_tiles,
                }
            recs.append({
                "kind": "opening_settlement",
                "when": "now",
                "node_id": int(s.node_id),
                "score": _score_second_settle(s.score),
                "detail": " · ".join(detail_parts),
                "tiles": s.tiles,
                "port": s.port,
                "road": road,
            })
        if not recs:
            recs.extend(_opening_road_followup(
                game=game, c=c, neighbors=neighbors,
                scored_by_node=full_scored, m=m,
            ))
        return recs

    # --- Round 1: rank F, attach best paired N as plan.second ------------
    legal = legal_nodes_after_picks(game, placed)
    if not legal:
        return []
    scored = score_opening_nodes(game, legal_nodes=legal)
    pair_color = c.name if c is not None else "RED"
    for s in scored[:top]:
        detail_parts = [f"pip {s.raw_production:.2f}/roll"]
        if s.port:
            detail_parts.append(f"port {s.port}")
        F_prod = {r: s.resources.get(r, 0.0) for r in RESOURCES}
        # Best hypothetical 2nd settle paired with this F. The legality
        # override tells score_second_settlements to pretend F is placed
        # even though the board is still empty at this point in round 1.
        legal_after_f = legal_nodes_after_picks(
            game, placed + [int(s.node_id)])
        pair_scored = score_second_settlements(
            game, int(s.node_id), color=pair_color,
            legal_nodes=legal_after_f,
        )
        plan_second: dict[str, Any] | None = None
        archetype: str | None = None
        n_neighbors: set[int] = set()
        if pair_scored:
            n = pair_scored[0]
            new_res = _resources_added(F_prod, n.resources)
            coverage = len(_resources_covered(F_prod, n.resources))
            plan_second = {
                "node_id": int(n.node_id),
                "tiles": n.tiles,
                "port": n.port,
                "covers": coverage,
                "adds": new_res,
            }
            archetype = _label_archetype(s.tiles, n.tiles, s.port, n.port)
            n_neighbors = (neighbors.get(int(n.node_id), set())
                           | {int(n.node_id)})
            # The overlay renders plan.second as its own sub-line, so
            # the detail string stays terse — just pips + port.
        road = _best_opening_road(
            settlement=int(s.node_id),
            neighbors=neighbors,
            scored_by_node=full_scored,
            m=m,
            game=game,
            my_color=c,
            planned_blocked=n_neighbors,
        )
        rec: dict[str, Any] = {
            "kind": "opening_settlement",
            "when": "now",
            "node_id": int(s.node_id),
            "score": _score_opening(s.score),
            "detail": " · ".join(detail_parts),
            "tiles": s.tiles,
            "port": s.port,
            "road": road,
        }
        if plan_second is not None:
            plan: dict[str, Any] = {"second": plan_second}
            if archetype is not None:
                plan["archetype"] = archetype
            rec["plan"] = plan
        recs.append(rec)
    # All opening settlements placed but self still owes a matching road?
    # Emit a "finish the road" rec so the overlay doesn't go blank during
    # that window. Fires only when we know self's color — otherwise we
    # can't tell whose settlement needs a road.
    if not recs and c is not None:
        recs.extend(_opening_road_followup(
            game=game, c=c, neighbors=neighbors,
            scored_by_node=full_scored, m=m,
        ))
    return recs


def _opening_road_followup(*, game, c, neighbors, scored_by_node, m):
    """One-off road hint for self's most-recently-placed opening
    settlement that doesn't yet have a self-owned adjacent road.

    Used when the main opening settlement recs are exhausted (all
    settlements placed) but at least one player still owes their
    opening road. Returning a non-empty list keeps the overlay showing
    something useful between settlement placement and road placement."""
    out: list[dict[str, Any]] = []
    roads = game.state.board.roads
    # Highest-pip self settlement without a road is the likely target —
    # it's usually the one just placed, and if not it's still the more
    # important of the two to cover.
    candidates: list[tuple[float, int]] = []
    for nid, (col, btype) in game.state.board.buildings.items():
        if col != c or btype != "SETTLEMENT":
            continue
        has_self_road = False
        for x in neighbors.get(int(nid), set()):
            if (roads.get((int(nid), int(x))) == c
                    or roads.get((int(x), int(nid))) == c):
                has_self_road = True
                break
        if has_self_road:
            continue
        prod = _node_pip_production(m, int(nid))
        candidates.append((prod, int(nid)))
    if not candidates:
        return out
    candidates.sort(reverse=True)
    _, nid = candidates[0]
    road = _best_opening_road(
        settlement=nid, neighbors=neighbors,
        scored_by_node=scored_by_node, m=m, game=game, my_color=c,
    )
    if not road:
        return out
    out.append({
        "kind": "opening_settlement",
        "when": "now",
        "node_id": nid,
        "score": _score_opening(scored_by_node[nid].score)
                 if nid in scored_by_node else 5.0,
        "detail": "lay your matching road",
        "tiles": _tile_label(m, nid),
        "port": None,
        "road": road,
    })
    return out


def _best_opening_road(*, settlement: int, neighbors, scored_by_node,
                       m, game=None, my_color=None,
                       planned_blocked: set[int] | None = None,
                       ) -> dict[str, Any] | None:
    """For a proposed opening settlement, pick the best adjacent edge.

    "Best" = the edge whose far-end leads toward the highest-scoring
    legal 2-hop expansion spot. The road itself doesn't collect
    resources; it's a commitment to where your settlement network
    extends. Tiebreaker is the far-node's own pip production — a road
    toward a 6/8 tile is better than toward a 3/4 corner at equal
    2-hop target.

    Blocking-risk filter: expansion candidates that are already
    distance-2 blocked by any existing building get dropped, and the
    edge `(far, x)` being owned by an opponent road drops that branch
    outright — the opp has already sealed the corridor, so pointing our
    road at it is wasted. When ``game`` isn't given we skip these
    checks (the unit tests hit the no-board path).

    ``planned_blocked`` (optional) treats extra nodes as distance-blocked
    even though they aren't built yet — used in round-1 to reserve the
    planned 2nd-settlement and its neighbors so the round-1 road doesn't
    aim at a corridor that will be sealed once N is placed.
    """
    # Precompute the danger set from the live game: distance-2 blocks
    # from any settlement/city, and opponent-owned edges we can't cross.
    blocked_nodes: set[int] = set()
    opp_edges: set[frozenset[int]] = set()
    if game is not None:
        for nid, (col, btype) in game.state.board.buildings.items():
            if btype not in ("SETTLEMENT", "CITY"):
                continue
            blocked_nodes.add(int(nid))
            blocked_nodes |= {int(n) for n in neighbors.get(int(nid), set())}
        for edge, col in game.state.board.roads.items():
            if col == my_color:
                continue
            a, b = edge
            opp_edges.add(frozenset((int(a), int(b))))
    if planned_blocked:
        blocked_nodes |= {int(n) for n in planned_blocked}
    adj = neighbors.get(settlement, set())
    best: tuple[float, int, int, bool] | None = None
    # (score, far, expansion, contested)
    for far in adj:
        # Best reachable 2-hop settlement spot via (settlement -> far -> x).
        exp_score = 0.0
        exp_node: int | None = None
        exp_contested = False
        for x in neighbors.get(far, set()):
            if x == settlement:
                continue
            # Skip expansions already sealed by distance-2 rule.
            if x in blocked_nodes:
                continue
            ns = scored_by_node.get(x)
            if ns is None:
                continue
            # Opp road on (far, x) — physically blocks the extension.
            # Drop it from consideration rather than soft-penalize;
            # a sealed corridor is worse than pointing elsewhere.
            if frozenset((far, x)) in opp_edges:
                continue
            # Soft contested signal: opp pieces already close to the
            # expansion target. Doesn't filter the edge, just flags it
            # so the overlay can warn.
            contested = False
            if game is not None:
                for nb in neighbors.get(x, set()):
                    if nb in blocked_nodes and nb != settlement:
                        contested = True
                        break
            if ns.score > exp_score:
                exp_score = ns.score
                exp_node = x
                exp_contested = contested
        # Skip far-candidates that lead nowhere legal — the whole point
        # of the opening road is to point at a future settlement spot,
        # and `far` itself is distance-1 from our new settlement so it
        # can never be one. Better to return no road than to recommend
        # one that breaks the distance-2 rule.
        if exp_node is None:
            continue
        # Tiebreaker: far-node's own pip production.
        far_prod = _node_pip_production(m, far)
        combined = exp_score * 100.0 + far_prod
        if best is None or combined > best[0]:
            best = (combined, far, exp_node, exp_contested)
    if best is None:
        return None
    _, far, expansion, contested = best
    out: dict[str, Any] = {
        "edge": [int(settlement), int(far)],
        "toward_node": int(expansion),
        "toward_tiles": _tile_label(m, expansion),
    }
    if contested:
        out["contested"] = True
    return out

def _sell_rate(resource: str, owned_nodes: set[int], port_nodes) -> int:
    """Cheapest rate at which the player can SELL this resource. A
    settlement on a matching 2:1 port returns 2; on any 3:1 generic
    port returns 3; otherwise 4:1 bank. Ports apply to the resource
    you're giving up, not the one you're getting."""
    specific = port_nodes.get(resource) or set()
    if owned_nodes & set(specific):
        return 2
    generic = port_nodes.get(None) or set()
    if owned_nodes & set(generic):
        return 3
    return 4


def _best_trade_offer(hand: dict[str, int], need_resource: str,
                      owned_nodes: set[int], port_nodes,
                      reserved: dict[str, int] | None = None,
                      ) -> tuple[str, int] | None:
    """Pick the best (source, rate) trade we can make to get 1 of
    ``need_resource``. For each resource we have surplus of (beyond
    ``reserved`` — the build's own cost), compute our sell rate for it
    and check whether we have ``rate`` excess cards to cover the trade.
    Prefer the cheapest rate; break ties on largest excess so the trade
    minimizes future-turn impact."""
    reserved = reserved or {}
    best: tuple[str, int, int] | None = None  # (res, rate, excess)
    for res, n in hand.items():
        if res == need_resource:
            continue
        excess = n - reserved.get(res, 0)
        if excess <= 0:
            continue
        rate = _sell_rate(res, owned_nodes, port_nodes)
        if excess < rate:
            continue
        # Cheaper rate wins; ties broken by larger excess.
        if (best is None or rate < best[1]
                or (rate == best[1] and excess > best[2])):
            best = (res, rate, excess)
    if best is None:
        return None
    return (best[0], best[1])


def _owned_port_nodes(game, c) -> set[int]:
    """Return the set of node_ids where color ``c`` sits on a port
    corner — i.e. has any settlement or city on a port node. Used to
    gate port sell rates in ``_sell_rate``."""
    port_nodes = set()
    m = game.state.board.map
    for resource_key, nodes in m.port_nodes.items():
        port_nodes |= set(nodes)
    out: set[int] = set()
    for nid, (bcol, btype) in game.state.board.buildings.items():
        if bcol == c and btype in ("SETTLEMENT", "CITY"):
            out.add(int(nid))
    return out & port_nodes


def _plan_bank_trades(hand: dict[str, int], cost: dict[str, int],
                      owned_nodes: set[int],
                      port_nodes,
                      bank_supply: dict[str, int] | None = None,
                      ) -> list[tuple[str, int, str]] | None:
    """Plan a sequence of port/bank trades that makes ``cost`` affordable.

    Returns a list of ``(source_resource, rate, target_resource)`` tuples
    if the build is reachable via trades this turn, else None. Greedy by
    cheapest rate: we always pay the fewest cards per missing card.

    ``bank_supply`` (when given) caps trades — if the bank has 0 of a
    needed resource the trade won't land at the window, so we fail the
    plan rather than emit an undoable rec.
    """
    available = dict(hand)
    needs: dict[str, int] = {}
    for r, n in cost.items():
        have = available.get(r, 0)
        if have >= n:
            available[r] = have - n
        else:
            available[r] = 0
            needs[r] = n - have
    if not needs:
        return []
    trades: list[tuple[str, int, str]] = []
    for need_res, need_count in needs.items():
        for _ in range(need_count):
            if bank_supply is not None and bank_supply.get(need_res, 0) <= 0:
                return None
            best_src: str | None = None
            best_rate = 99
            for src, surplus in available.items():
                if src == need_res or surplus <= 0:
                    continue
                rate = _sell_rate(src, owned_nodes, port_nodes)
                if surplus < rate:
                    continue
                if rate < best_rate:
                    best_src, best_rate = src, rate
            if best_src is None:
                return None
            available[best_src] -= best_rate
            trades.append((best_src, best_rate, need_res))
            if bank_supply is not None:
                bank_supply = dict(bank_supply)
                bank_supply[need_res] = max(
                    0, bank_supply.get(need_res, 0) - 1)
    return trades


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

# Human-friendly labels for rec kinds — used anywhere a `kind` key gets
# rendered into overlay text. Without this, raw snake_case identifiers
# like "dev_card" and "propose_trade" leak into detail strings and read
# as Python variable names to the user.
_KIND_LABEL = {
    "settlement": "settlement",
    "city": "city",
    "road": "road",
    "dev_card": "dev card",
    "trade": "trade",
    "propose_trade": "trade proposal",
    "bank_trade": "port trade",
    "opening_settlement": "settlement",
}


def _kind_label(kind: str | None) -> str:
    if not kind:
        return "build"
    return _KIND_LABEL.get(kind, kind.replace("_", " "))


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
    opp_hands: dict[str, dict[str, int]] | None = None,
    bank_supply: dict[str, int] | None = None,
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

    # --- Bank / port trade unlocks --------------------------------------
    # When a build is missing cards but the player has enough spare
    # cards to pay through a 2:1 port, 3:1 port, or 4:1 bank, emit a
    # concrete trade rec that completes the build. This is the rec the
    # user asked for when they said "I have tons of ore and a 2:1 port
    # but the bot won't tell me to trade for a settlement".
    #
    # Skipped when bank_supply signals 0 of the needed resource — a
    # bank/port trade against an empty pool is a no-op in colonist.
    port_nodes_map = m.port_nodes
    my_owned_nodes: set[int] = set()
    for nid, (bcol, btype) in game.state.board.buildings.items():
        if bcol == c and btype in ("SETTLEMENT", "CITY"):
            my_owned_nodes.add(int(nid))

    def _resource_title(r: str) -> str:
        return _RES_TITLE.get(r, r.title())

    def _fmt_pack(pack: dict[str, int]) -> str:
        return ", ".join(f"{n} {_resource_title(r)}"
                         for r, n in pack.items() if n)

    def _collapse_trade_plan(plan: list[tuple[str, int, str]]) -> tuple[
            dict[str, int], dict[str, int], str]:
        """Collapse a multi-step port plan into net give/get packs plus a
        human description."""
        give: dict[str, int] = {}
        get: dict[str, int] = {}
        steps: list[str] = []
        for src, rate, tgt in plan:
            give[src] = give.get(src, 0) + rate
            get[tgt] = get.get(tgt, 0) + 1
            steps.append(f"{rate} {_resource_title(src)} → "
                         f"1 {_resource_title(tgt)}")
        return give, get, " + ".join(steps)

    # Roads deliberately left out: they're cheap (2 cards) and their
    # value depends on *where* they'd extend — trading for a generic
    # road with no target node produces a suggestion Noah can't act on.
    # Settlement / city / dev-card trades unlock concrete, high-value
    # moves, so those are the ones worth surfacing.
    _trade_targets = [
        ("settlement", _SETTLEMENT_COST, _score_settlement,
         _best_settlement_spot),
        ("city",       _CITY_COST,       _score_city,
         _best_owned_settlement),
        ("dev_card",   _DEV_COST,        lambda _p: _DEV_CARD_SCORE,
         None),
    ]
    for kind, cost, score_fn, target_fn in _trade_targets:
        if _hand_can_afford(hand, cost):
            continue
        plan = _plan_bank_trades(
            hand, cost, my_owned_nodes, port_nodes_map,
            bank_supply=bank_supply)
        if not plan:
            continue
        node_or_none: int | None = None
        prod = 0.0
        if target_fn is not None:
            target = target_fn()
            if target is None:
                continue
            node_or_none, prod = target
        base_score = score_fn(prod)
        give, get, steps = _collapse_trade_plan(plan)
        # Bank trades guarantee their result (vs. propose_trade which may
        # not land), but cost more cards. Score 1.0 below the direct
        # build so affording-now stays the top pick when possible.
        rate_sum = sum(r for _, r, _ in plan)
        trade_score = round(min(max(base_score - 1.0, 2.0), 9.0), 1)
        label_word = _kind_label(kind)
        rec: dict[str, Any] = {
            "kind": "bank_trade",
            "when": "now",
            "score": trade_score,
            "give": give,
            "get": get,
            "unlocks": kind,
            "detail": (f"{steps} · then build {label_word}"
                       if len(plan) == 1
                       else f"trade {_fmt_pack(give)} → {_fmt_pack(get)} "
                            f"({rate_sum} cards) · then build {label_word}"),
        }
        if node_or_none is not None:
            rec["node_id"] = int(node_or_none)
            rec["tiles"] = _tile_label(m, int(node_or_none))
        recs.append(rec)
        # One port/bank trade per rec cycle keeps the overlay focused on
        # the single best unlock path. Higher-priority builds win by
        # virtue of coming first in _trade_targets.
        break

    # --- Player-to-player trade proposals -------------------------------
    # For each blocked build, emit a few propose_trade variants at
    # different denominations: 1:1 (fair), 2:1 (concede to get a yes),
    # and 2:2 (even swap when we need two of a thing). Skipped when no
    # opponent is known to hold the resource we'd be asking for — a
    # proposal for a wheat nobody has is dead on arrival.
    opp_resource_total: dict[str, int] = {}
    opp_has_unknown = False
    if opp_hands is not None:
        for opp_hand in opp_hands.values():
            for r, n in opp_hand.items():
                if r == "unknown":
                    if n > 0:
                        opp_has_unknown = True
                    continue
                opp_resource_total[r] = opp_resource_total.get(r, 0) + int(n)
        # Any unknown card among opponents means we can't rule a resource
        # out entirely. Only skip when we know the board cold.
    # Reserve resources across every build we'd still plausibly make
    # this turn — affordable now, or one-to-two cards off. Trading a
    # resource away that some higher-priority blocked build needs (e.g.
    # offering our only WOOD for a dev-card unlock while the settlement
    # is also blocked on WOOD) would move us further from the real goal.
    reserved_across: dict[str, int] = {}
    for _k, _c, _s, _t in _trade_targets:
        _missing = _missing_for(hand, _c)
        if sum(_missing.values()) > 2:
            continue
        for r, n in _c.items():
            reserved_across[r] = max(
                reserved_across.get(r, 0), min(n, hand.get(r, 0)))

    for kind, cost, score_fn, target_fn in _trade_targets:
        if _hand_can_afford(hand, cost):
            continue
        missing = _missing_for(hand, cost)
        total_missing = sum(missing.values())
        # Only propose when a single trade closes the gap. Two-missing
        # needs two trades to actually unlock the build, and emitting
        # a "trade 1→1" rec that alone can't unlock is misleading —
        # the "save for X" plan path surfaces those instead.
        if total_missing != 1:
            continue
        if target_fn is None and kind != "dev_card":
            continue
        target = target_fn() if target_fn is not None else None
        if target is None and kind in ("settlement", "city"):
            continue
        node_or_none, prod = (target if target is not None else (None, 0.0))
        base_score = score_fn(prod)
        need_pairs = list(missing.items())
        # Surplus = hand minus every resource reserved by an affordable-
        # or-reachable build (incl. this one). If the last WOOD in hand
        # is needed for a blocked settlement, it's not spare — trading
        # it to unlock a dev card would only dig the settlement hole
        # deeper.
        surplus: dict[str, int] = {}
        for res, n in hand.items():
            spare = n - reserved_across.get(res, 0)
            if spare > 0:
                surplus[res] = spare
        if not surplus:
            continue
        emitted_for_kind = 0
        for need_res, need_n in need_pairs:
            if opp_hands is not None and not opp_has_unknown:
                if opp_resource_total.get(need_res, 0) <= 0:
                    # Nobody has this — no point asking for it.
                    continue
            # Candidate variants (give_count, get_count, label, score_adj).
            # 1:1 is the friendliest; 2:1 is a concession offer; 2:2 is
            # useful when we need two of something (e.g. city); 1:2 is
            # a longshot but shows up last.
            variants: list[tuple[int, int, str, float]] = [
                (1, 1, "1:1 fair", 0.0),
                (2, 1, "2:1 concede", -0.6),
            ]
            if need_n >= 2:
                variants.append((2, 2, "2:2 even", -0.2))
            variants.append((1, 2, "1:2 longshot", -1.2))
            for give_n, get_n, label, adj in variants:
                best_src = None
                best_spare = 0
                for src, spare in surplus.items():
                    if src == need_res or spare < give_n:
                        continue
                    if spare > best_spare:
                        best_src = src
                        best_spare = spare
                if best_src is None:
                    continue
                propose_score = round(
                    min(base_score - 0.3 + adj, 9.5), 1)
                propose_score = max(propose_score, 1.5)
                kind_word = _kind_label(kind)
                rec = {
                    "kind": "propose_trade",
                    "when": "now",
                    "score": propose_score,
                    "give": {best_src: give_n},
                    "get": {need_res: get_n},
                    "unlocks": kind,
                    "variant": label,
                    "detail": (
                        f"propose {label}: {give_n} "
                        f"{_resource_title(best_src)} → "
                        f"{get_n} {_resource_title(need_res)} · "
                        f"ask the table · toward {kind_word}"),
                }
                if node_or_none is not None:
                    rec["node_id"] = int(node_or_none)
                    rec["tiles"] = _tile_label(m, int(node_or_none))
                recs.append(rec)
                emitted_for_kind += 1
                # Cap variants per missing-resource so the overlay doesn't
                # fill up with trade suggestions.
                if emitted_for_kind >= 3:
                    break
            if emitted_for_kind >= 3:
                break
        if emitted_for_kind:
            # One build's worth of trade proposals is enough — the next
            # blocked build will still surface as a "save for X" plan.
            break

    # 1-ply search rerank: for each affordable build, simulate executing
    # it on a game copy and score the resulting state. The rec with the
    # best post-action evaluation wins — actual lookahead value, not just
    # the per-kind heuristic. Falls back to heuristic score for recs that
    # can't be simulated (propose_trade, soon-plans) or if the engine
    # state is malformed. See eval.py for the state evaluator.
    from cataanbot.eval import search_rerank
    search_rerank(game, c, recs)
    return recs[:top]


def _fmt_trade_side(pack: dict[str, int]) -> str:
    parts = [f"{n} {_RES_TITLE.get(r, r.title())}"
             for r, n in pack.items() if n]
    return ", ".join(parts) if parts else "∅"


def _trim_pack(pack: dict[str, int], target_total: int) -> dict[str, int]:
    """Shrink ``pack`` down to ``target_total`` cards, dropping from the
    largest bucket first. Buckets that hit zero are removed entirely so
    the caller doesn't end up with ``{ORE: 0}`` noise."""
    out = {r: int(n) for r, n in pack.items() if n > 0}
    remaining = sum(out.values())
    while remaining > target_total and out:
        top = max(out, key=out.get)
        out[top] -= 1
        if out[top] <= 0:
            del out[top]
        remaining -= 1
    return out


def _suggest_counter_offer(
    game, self_color, self_hand: dict[str, int],
    give: dict[str, int], want: dict[str, int], *, opp_vp: int,
) -> dict[str, Any] | None:
    """Suggest a fairer version of an incoming offer.

    The heuristic is intentionally narrow: trim ``want`` down to at most
    the size of ``give`` (so the counter is at worst 1:1 in our favor)
    and re-evaluate. If that subset turns into an "accept", surface it
    as the counter. Anything else is noise — a counter we'd still
    decline isn't worth proposing, and a counter the opponent would
    obviously refuse (e.g. doubling ``give``) wastes a turn.

    Returns ``{give, want, reason}`` or None.
    """
    want_total = sum(int(n) for n in want.values())
    give_total = sum(int(n) for n in give.values())
    if want_total <= give_total or want_total <= 1:
        return None
    counter_want = _trim_pack(want, give_total)
    if not counter_want:
        return None
    sub = evaluate_incoming_trade(
        game, self_color, self_hand, give, counter_want,
        opp_vp=opp_vp, _allow_counter=False,
    )
    if sub.get("verdict") != "accept":
        return None
    return {
        "give": dict(give),
        "want": counter_want,
        "reason": f"rebalance {want_total}→{sum(counter_want.values())} "
                  f"for 1:1",
    }


def evaluate_incoming_trade(
    game, self_color, self_hand: dict[str, int],
    give: dict[str, int], want: dict[str, int],
    *, opp_vp: int = 0, _allow_counter: bool = True,
) -> dict[str, Any]:
    """Rate an incoming player-to-player offer.

    The offerer proposes: they give ``give``, they want ``want``.
    From our seat, accepting means ``hand += give - want``.

    Returns ``{verdict, score, reason, before, after, counter}`` where:
        verdict ∈ {"accept", "decline", "consider"}
        score   float — delta of best affordable-now rec before → after;
                positive leans accept
        reason  short human-readable string for the overlay
        before  top "now" rec kind at current hand, or None
        after   top "now" rec kind after the swap, or None
        counter {give, want, reason} suggestion for decline/consider
                offers that rebalance into an accept, else None

    Affordability comes first — if we can't spare ``want``, auto-decline.
    Then we compare what's buildable this turn before and after the swap.
    A build unlocked → accept; a build lost → decline. Neutral deltas
    fall to a fairness check (giving more cards than we get) and an
    opp-close-to-win guard (VP ≥ ``close_to_win_vp()``) before landing
    on "consider".

    ``_allow_counter`` gates the counter-offer search; internal recursion
    (from ``_suggest_counter_offer``) sets it False to avoid infinite
    fan-out when a counter itself would spawn another counter.
    """
    if not want:
        return {"verdict": "consider", "score": 0.0,
                "reason": "open offer — no ask", "before": None,
                "after": None, "counter": None}
    for r, n in want.items():
        if self_hand.get(r, 0) < int(n):
            return {
                "verdict": "decline",
                "score": -10.0,
                "reason": f"can't spare {n} {_RES_TITLE.get(r, r.title())}",
                "before": None,
                "after": None,
                "counter": None,
            }
    if not give:
        return {"verdict": "decline", "score": -10.0,
                "reason": "they give nothing in return",
                "before": None, "after": None, "counter": None}

    new_hand = dict(self_hand)
    for r, n in want.items():
        new_hand[r] = new_hand.get(r, 0) - int(n)
    for r, n in give.items():
        new_hand[r] = new_hand.get(r, 0) + int(n)

    def _best_now_rec(h: dict[str, int]) -> dict[str, Any] | None:
        try:
            recs = recommend_actions(game, self_color, h, top=4)
        except Exception:  # noqa: BLE001
            return None
        for r in recs:
            if r.get("when") == "now":
                return r
        return None

    before = _best_now_rec(self_hand)
    after = _best_now_rec(new_hand)
    s_before = float(before.get("score", 0.0)) if before else 0.0
    s_after = float(after.get("score", 0.0)) if after else 0.0
    delta = round(s_after - s_before, 2)

    before_kind = before.get("kind") if before else None
    after_kind = after.get("kind") if after else None
    give_total = sum(int(n) for n in give.values())
    want_total = sum(int(n) for n in want.values())
    from cataanbot.config import close_to_win_vp
    _CLOSE_TO_WIN_VP = close_to_win_vp()
    # Rank of build types so we can detect a kind upgrade. Score deltas
    # alone undersell e.g. "road → settlement" (raw gap is only ~0.5)
    # even though it's a real upgrade in the type of move available.
    _KIND_RANK = {None: 0, "dev_card": 1, "road": 2, "trade": 2,
                  "settlement": 3, "city": 4}
    kind_upgrade = (_KIND_RANK.get(after_kind, 0)
                    > _KIND_RANK.get(before_kind, 0))
    kind_downgrade = (_KIND_RANK.get(after_kind, 0)
                      < _KIND_RANK.get(before_kind, 0))

    # Counter is only meaningful on non-accept paths. Skip entirely when
    # the opp is close to winning — any deal feeds them closer to 10 VP.
    def _maybe_counter() -> dict[str, Any] | None:
        if not _allow_counter or opp_vp >= _CLOSE_TO_WIN_VP:
            return None
        return _suggest_counter_offer(
            game, self_color, self_hand, give, want, opp_vp=opp_vp,
        )

    if kind_upgrade or delta >= 1.0:
        if opp_vp >= _CLOSE_TO_WIN_VP:
            return {"verdict": "decline", "score": delta,
                    "reason": f"opp at {opp_vp} VP — don't feed",
                    "before": before_kind, "after": after_kind,
                    "counter": None}
        label = _kind_label(after_kind)
        return {"verdict": "accept", "score": delta,
                "reason": f"unlocks {label} (+{delta:.1f})",
                "before": before_kind, "after": after_kind,
                "counter": None}
    if kind_downgrade or delta <= -1.0:
        label = _kind_label(before_kind)
        return {"verdict": "decline", "score": delta,
                "reason": f"blocks {label} ({delta:.1f})",
                "before": before_kind, "after": after_kind,
                "counter": _maybe_counter()}
    if want_total > give_total:
        return {"verdict": "decline", "score": delta,
                "reason": f"lopsided — give {want_total}, "
                          f"get {give_total}",
                "before": before_kind, "after": after_kind,
                "counter": _maybe_counter()}
    if opp_vp >= _CLOSE_TO_WIN_VP:
        return {"verdict": "decline", "score": delta,
                "reason": f"opp at {opp_vp} VP — hold cards",
                "before": before_kind, "after": after_kind,
                "counter": None}
    return {"verdict": "consider", "score": delta,
            "reason": "neutral swap",
            "before": before_kind, "after": after_kind,
            "counter": _maybe_counter()}
