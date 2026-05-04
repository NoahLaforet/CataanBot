"""Dev-card and discard hint computers extracted from bridge.py.

These functions take a `game` plus advisor inputs (hand, self_color)
and return advisor-snapshot fragments. They depend on bridge_economy
(`_pieces_for_color`) and bridge_robber (`_compute_robber_snapshot`),
not on the bridge `st` dict.

Re-exported by bridge.py for backwards compat.
"""
from __future__ import annotations

from typing import Any

from cataanbot.bridge_economy import _pieces_for_color
from cataanbot.bridge_robber import _compute_robber_snapshot


# Cost tables mirror the build_costs used elsewhere in the bot. Keys
# are ordered by "preserve this build over the others" priority — a
# city beats a settlement beats a dev card beats a road when we can
# only keep one of them post-discard.
_DISCARD_PRESERVE_PLANS: tuple[tuple[str, dict[str, int]], ...] = (
    ("city", {"WHEAT": 2, "ORE": 3}),
    ("settlement", {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1}),
    ("dev card", {"WHEAT": 1, "SHEEP": 1, "ORE": 1}),
    ("road", {"WOOD": 1, "BRICK": 1}),
)
# Drop priority: resources on the left go first. SHEEP's the cheapest
# to re-acquire (3:1 bank trade still leaves an edge) and isn't the
# bottleneck for cities the way WHEAT/ORE are. WOOD/BRICK slot in
# between — roads are cheaper than cities. WHEAT/ORE last: losing an
# ore delays city tempo more than losing any other card.
_DISCARD_PRIORITY: tuple[str, ...] = (
    "SHEEP", "WOOD", "BRICK", "WHEAT", "ORE")


def _compute_discard_plan(
    hand: dict[str, int], need: int,
) -> tuple[dict[str, int], str | None]:
    """Return {resource: count} to discard plus a rationale string.

    Strategy: try to preserve the most valuable build we can afford
    *after* the discard. Drop from resources not needed for that build,
    lowest-priority first; only break into the preserved build if the
    remaining non-reserved cards aren't enough.
    """
    if need <= 0:
        return {}, None
    total = sum(hand.values())
    after = total - need
    preserve_name: str | None = None
    reserved: dict[str, int] = {}
    for name, cost in _DISCARD_PRESERVE_PLANS:
        cost_total = sum(cost.values())
        if cost_total > after:
            continue
        if all(hand.get(r, 0) >= n for r, n in cost.items()):
            preserve_name = name
            reserved = dict(cost)
            break
    drops: dict[str, int] = {}
    remaining = need
    for r in _DISCARD_PRIORITY:
        if remaining == 0:
            break
        droppable = hand.get(r, 0) - reserved.get(r, 0)
        if droppable <= 0:
            continue
        take = min(droppable, remaining)
        drops[r] = drops.get(r, 0) + take
        remaining -= take
    if remaining > 0:
        # Reserved cards weren't enough slack — we have to dip into the
        # preserved build. Drop from its cheapest resource first.
        preserve_name = None
        for r in _DISCARD_PRIORITY:
            if remaining == 0:
                break
            available = hand.get(r, 0) - drops.get(r, 0)
            if available <= 0:
                continue
            take = min(available, remaining)
            drops[r] = drops.get(r, 0) + take
            remaining -= take
    return drops, preserve_name


def _compute_discard_hint(
    hand: dict[str, int], cards: int,
) -> dict[str, Any] | None:
    """Recommend which cards to discard when self must discard on a 7.

    Fires only when the authoritative card count exceeds the discard
    limit (default 7). Returns None otherwise so the overlay can hide
    the banner. Uses the authoritative ``cards`` total — the tracker's
    per-resource breakdown is trusted for the *shape* but the total is
    ``cards``; a drift between the two surfaces elsewhere already.
    """
    from cataanbot.config import DISCARD_LIMIT
    if cards <= DISCARD_LIMIT:
        return None
    need = cards // 2
    if need <= 0:
        return None
    drops, preserve = _compute_discard_plan(hand, need)
    if not drops:
        return None
    if preserve:
        rationale = f"keep enough to {preserve}"
    else:
        rationale = "drop spares"
    return {
        "need": need,
        "drop": drops,
        "rationale": rationale,
    }


def _compute_seven_prep_hint(
    hand: dict[str, int], cards: int,
) -> dict[str, Any] | None:
    """Pre-roll warning: spend down before the next 7 hits.

    Distinct from ``_compute_discard_hint`` (which fires reactively
    AFTER a 7 forces a discard). This one fires PROACTIVELY when self
    is sitting at risky card counts:

    * **danger** (cards >= DISCARD_LIMIT + 3, default 10): expected
      discard on a 7 would be 5+ cards — game-changing damage. Banner
      should dominate the HUD.
    * **warn**   (cards >= DISCARD_LIMIT + 2, default 9): expected
      discard would be 4+ cards. Worth a clear "spend down" prompt.

    Returns None when self holds DISCARD_LIMIT + 1 or fewer — at 8 cards
    you'd lose 4 to a 7 but the risk : opportunity-cost ratio doesn't
    yet justify forcing a spend-down. The proactive warning kicks in
    one card above that.

    Noah's BrickdDaddy game: caught at 11+ cards 48 times with 4 self-
    discard blunder annotations. The reactive discard_hint shows up
    AFTER a 7 lands; this warning is meant to prevent the situation in
    the first place.
    """
    from cataanbot.config import DISCARD_LIMIT
    if cards < DISCARD_LIMIT + 2:
        return None
    expected_discard = cards // 2
    level = "danger" if cards >= DISCARD_LIMIT + 3 else "warn"
    # Plan a hypothetical discard so the user sees what they'd lose
    # if a 7 hit RIGHT NOW. Same logic as the reactive discard_hint —
    # gives them a concrete handle on what's at stake.
    drops, _preserve = _compute_discard_plan(hand, expected_discard)
    return {
        "level": level,
        "cards": cards,
        "expected_discard": expected_discard,
        "would_drop": drops,
        "message": (
            f"DUMP TO {DISCARD_LIMIT} BEFORE YOU ROLL — "
            f"a 7 right now costs you {expected_discard} cards"),
    }


# Resource tie-breaker weights when a Monopoly would net equal totals
# across resources — prefer stealing the strategically scarcer cards.
_MONOPOLY_RES_WEIGHT = {
    "ORE": 5, "WHEAT": 5, "BRICK": 3, "WOOD": 3, "SHEEP": 2,
}
_BUILD_COSTS_MONOPOLY = {
    "city": {"WHEAT": 2, "ORE": 3},
    "settlement": {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1},
    "dev card": {"WHEAT": 1, "SHEEP": 1, "ORE": 1},
    "road": {"WOOD": 1, "BRICK": 1},
}


def _compute_monopoly_hint(
    game, self_color: str, self_hand: dict[str, int],
    display_colors: dict[str, str] | None = None,
    playable_count: int = 0,
    opp_card_totals: dict[str, int] | None = None,
    bank_supply: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Pick the best resource to steal when self plays Monopoly.

    Fires when self has at least one playable dev card in hand. Since
    colonist hides the card type from the DOM log, we don't know if
    it's actually a Monopoly — the user reads our four hint blocks
    side-by-side and picks the one that matches what's in their
    actual dev card panel. Ranks each resource by the inferred total
    held across opps; ties break toward resources that would unlock
    an immediate build for self. Carries a PLAY/HOLD verdict (unlock
    or big-pot → PLAY; small pot w/ no unlock → HOLD).
    """
    from catanatron import Color
    try:
        my_enum = Color[self_color.upper()]
    except Exception:  # noqa: BLE001
        return None
    state = game.tracker.game.state
    idx = state.color_to_index.get(my_enum)
    if idx is None:
        return None
    # Prefer the type-specific catanatron counter when it's been
    # populated (real-game flow keeps it at 0 for self, but tests
    # poke values directly so this respects either signal). Fall
    # back to the aggregate playable_count tracked in overlay state.
    held = int(state.player_state.get(f"P{idx}_MONOPOLY_IN_HAND", 0))
    if held <= 0:
        held = int(playable_count or 0)
    if held <= 0:
        return None
    # Aggregate inferred counts across opps via the tracker. We also
    # remember the per-opp split so we can spotlight the top holder —
    # monopoly steals from everyone, but Noah wants to know whose stack
    # he's draining the most (it informs follow-up trade/robber calls).
    totals: dict[str, int] = {
        "WOOD": 0, "BRICK": 0, "SHEEP": 0, "WHEAT": 0, "ORE": 0,
    }
    per_opp: dict[str, dict[str, int]] = {}
    for opp_color in state.color_to_index:
        if opp_color == my_enum:
            continue
        try:
            opp_hand = game.tracker.hand(opp_color.value)
        except Exception:  # noqa: BLE001
            continue
        # Authoritative cap: opp's total card count from the WS
        # (not the inferred per-resource sum). When tracker.hand()
        # disagrees with the authoritative total — which it will
        # whenever a hidden steal moved cards we couldn't attribute
        # — the per-resource breakdown gets scaled down so it sums
        # to no more than the real total. Catches the "drains 19
        # from blue" bug on opps with 0 actual cards.
        cap = None
        if opp_card_totals is not None:
            cap = opp_card_totals.get(opp_color.value)
        counts: dict[str, int] = {}
        raw_sum = sum(int(n) for n in opp_hand.values())
        scale = 1.0
        if cap is not None and raw_sum > cap and raw_sum > 0:
            # Inferred breakdown overstates this opp's hand. Scale
            # all per-resource counts down proportionally so they
            # sum to cap. Cap=0 (opp has no cards) drops to all-zero.
            scale = cap / raw_sum
        for r, n in opp_hand.items():
            if r in totals:
                clamped = int(round(int(n) * scale)) if scale < 1.0 else int(n)
                if cap == 0:
                    clamped = 0
                totals[r] += clamped
                counts[r] = clamped
        per_opp[opp_color.value] = counts
    # Final physical cap: total of any resource across all opps can
    # never exceed (deck_max - bank_remaining - self_held). 19 max
    # per resource in classic Catan.
    if bank_supply:
        for r in list(totals.keys()):
            phys_max = max(
                0,
                19
                - int(bank_supply.get(r, 0))
                - int(self_hand.get(r, 0)),
            )
            if totals[r] > phys_max:
                # Distribute the cap proportionally across opps so
                # the per-opp split stays meaningful.
                if totals[r] > 0:
                    factor = phys_max / totals[r]
                    for opp_val, counts in per_opp.items():
                        counts[r] = int(round(counts.get(r, 0) * factor))
                totals[r] = phys_max
    if not any(totals.values()):
        return None
    # Rank: (count, unlock-bonus, resource-weight).
    def _unlock(res: str) -> int:
        # 1 if grabbing `res` unlocks any build we couldn't afford.
        gained = dict(self_hand)
        gained[res] = gained.get(res, 0) + totals[res]
        for name, cost in _BUILD_COSTS_MONOPOLY.items():
            if all(gained.get(r, 0) >= n for r, n in cost.items()):
                if not all(self_hand.get(r, 0) >= n for r, n in cost.items()):
                    return 1
        return 0
    ranked = sorted(
        totals.items(),
        key=lambda kv: (kv[1], _unlock(kv[0]),
                        _MONOPOLY_RES_WEIGHT.get(kv[0], 0)),
        reverse=True,
    )
    best_res, best_count = ranked[0]
    if best_count <= 0:
        return None
    # Unlock reason: which build does this unlock (if any)? Skip a
    # build the player can't physically place (out of pieces) — same
    # piece-supply guard the YoP hint uses, since "unlocks settlement"
    # at 5 settles placed is a play that does nothing.
    settles_left = int(state.player_state.get(
        f"P{idx}_SETTLEMENTS_AVAILABLE", 5))
    cities_left = int(state.player_state.get(
        f"P{idx}_CITIES_AVAILABLE", 4))
    roads_left = int(state.player_state.get(
        f"P{idx}_ROADS_AVAILABLE", 15))
    placeable = {
        "settlement": settles_left > 0,
        "city": cities_left > 0,
        "road": roads_left > 0,
        "dev card": True,
    }
    unlock_reason: str | None = None
    gained = dict(self_hand)
    gained[best_res] = gained.get(best_res, 0) + best_count
    for name, cost in _BUILD_COSTS_MONOPOLY.items():
        if not placeable.get(name, True):
            continue
        if (all(gained.get(r, 0) >= n for r, n in cost.items())
                and not all(self_hand.get(r, 0) >= n
                            for r, n in cost.items())):
            unlock_reason = f"unlocks {name}"
            break

    # Verdict: PLAY when it unlocks or when the pot is large enough to
    # swing tempo (4+ cards is a full settlement's worth of resources).
    # HOLD when the pot is small AND no unlock — you'll get more value
    # letting opps accumulate. The 4-card threshold is intentionally
    # slightly above a single-opp production spike so we don't fire
    # PLAY on a one-roll lucky stack.
    should_play = False
    if unlock_reason:
        should_play = True
        reason = unlock_reason
    elif best_count >= 4:
        should_play = True
        reason = f"large pot · {best_count} cards"
    else:
        reason = f"small pot · {best_count}"

    # Top holder: the single opp contributing the most to best_count.
    # Used by the overlay to render "drains 4 from noah" as a sub-line.
    top_holder_color: str | None = None
    top_holder_count = 0
    for color_val, counts in per_opp.items():
        n = counts.get(best_res, 0)
        if n > top_holder_count:
            top_holder_count = n
            top_holder_color = color_val
    top_holder: dict[str, Any] | None = None
    if top_holder_color is not None and top_holder_count > 0:
        dc = (display_colors or {}).get(top_holder_color, top_holder_color)
        top_holder = {
            "color": top_holder_color,
            "display": dc,
            "count": top_holder_count,
        }

    return {
        "have": held,
        "should_play": should_play,
        "reason": reason,
        "resource": best_res,
        "est_steal": best_count,
        "totals": totals,
        "unlock": unlock_reason,
        "top_holder": top_holder,
    }


def _compute_yop_hint(
    game, self_color: str, self_hand: dict[str, int],
    bank_supply: dict[str, Any] | None = None,
    playable_count: int = 0,
) -> dict[str, Any] | None:
    """Suggest which pair to pick with Year-of-Plenty.

    Fires only when self holds at least one YEAR_OF_PLENTY card. Picks
    the pair that unlocks the most valuable buildable; falls back to
    the pair that aligns with the costliest build closest to complete.

    When no pair would unlock anything this turn, still surface the
    hint with should_play=False so the overlay can render a HOLD
    verdict rather than silently hiding the card. If the bank is
    completely out of a resource in the chosen pair, the YoP play
    can't actually grant that card — flag bank_ok=False so Noah knows
    before spending the card.
    """
    from catanatron import Color
    try:
        my_enum = Color[self_color.upper()]
    except Exception:  # noqa: BLE001
        return None
    state = game.tracker.game.state
    idx = state.color_to_index.get(my_enum)
    if idx is None:
        return None
    held = int(state.player_state.get(f"P{idx}_YEAR_OF_PLENTY_IN_HAND", 0))
    if held <= 0:
        held = int(playable_count or 0)
    if held <= 0:
        return None
    # Piece-supply guard. When the player has placed all 5 settlements
    # (or 4 cities, or 15 roads) the hint must not recommend a YoP pair
    # whose unlock target is a build they physically can't place. Bug
    # Noah hit on 2026-05-02: at 5 settles placed, hint suggested
    # WOOD+WHEAT "unlocks settlement" — a play that does nothing.
    settles_left = int(state.player_state.get(
        f"P{idx}_SETTLEMENTS_AVAILABLE", 5))
    cities_left = int(state.player_state.get(
        f"P{idx}_CITIES_AVAILABLE", 4))
    roads_left = int(state.player_state.get(
        f"P{idx}_ROADS_AVAILABLE", 15))
    placeable = {
        "settlement": settles_left > 0,
        "city": cities_left > 0,
        "road": roads_left > 0,
        "dev card": True,
    }
    # For each target build, compute deficit in self_hand. A pick is
    # "unlocking" iff total_deficit <= 2 (YoP grants exactly 2 cards).
    best: tuple[int, str, list[str]] | None = None  # (priority, build, [r1, r2])
    priority = {"city": 4, "settlement": 3, "dev card": 2, "road": 1}
    for name, cost in _BUILD_COSTS_MONOPOLY.items():
        if not placeable.get(name, True):
            continue
        deficit: dict[str, int] = {}
        for r, n in cost.items():
            d = n - self_hand.get(r, 0)
            if d > 0:
                deficit[r] = d
        total = sum(deficit.values())
        if total == 0:
            # Already affordable; YoP would be wasted on this target.
            continue
        if total > 2:
            continue
        pick: list[str] = []
        for r, d in deficit.items():
            pick.extend([r] * d)
        if len(pick) < 2:
            # Fill the second slot with a resource toward the next-
            # best build (city takes priority if YoP is generous).
            needs_next = None
            for n2, cost2 in _BUILD_COSTS_MONOPOLY.items():
                if n2 == name:
                    continue
                for r2, need in cost2.items():
                    have = self_hand.get(r2, 0)
                    if name != n2 and have + pick.count(r2) < need:
                        needs_next = r2
                        break
                if needs_next:
                    break
            pick.append(needs_next or "ORE")  # ORE as safe default
        pick = pick[:2]
        p = priority.get(name, 0)
        if best is None or p > best[0]:
            best = (p, name, pick)

    # No unlock within reach: surface a HOLD verdict pointed at the
    # cheapest build's deficit resource so Noah still sees the card.
    # Pair: two of the single resource most in demand across all builds
    # (weighted by priority). Default to ORE+WHEAT (city pair) as a
    # safe-ish hoard pick when we can't infer anything.
    if best is None:
        demand: dict[str, float] = {r: 0.0 for r in (
            "WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")}
        for name, cost in _BUILD_COSTS_MONOPOLY.items():
            w = priority.get(name, 1)
            for r, n in cost.items():
                d = n - self_hand.get(r, 0)
                if d > 0:
                    demand[r] += float(w * d)
        ranked = sorted(demand.items(),
                        key=lambda kv: kv[1], reverse=True)
        top_r = ranked[0][0] if ranked and ranked[0][1] > 0 else "ORE"
        second_r = (ranked[1][0] if len(ranked) > 1 and ranked[1][1] > 0
                    else "WHEAT")
        pair = [top_r, second_r] if top_r != second_r else [top_r, top_r]
        return {
            "have": held,
            "should_play": False,
            "reason": "no build within reach",
            "pair": pair,
            "unlock": None,
            "bank_ok": True,
        }

    _, build_name, pair = best

    # Bank-supply guard: YoP can't grant a resource the bank is out of.
    # If either pick is unavailable, flag it — Noah should trade/port
    # or pick a different pair.
    bank_ok = True
    if bank_supply and isinstance(bank_supply.get("remaining"), dict):
        remaining = bank_supply["remaining"]
        needed: dict[str, int] = {}
        for r in pair:
            needed[r] = needed.get(r, 0) + 1
        for r, n in needed.items():
            if int(remaining.get(r, 0)) < n:
                bank_ok = False
                break

    reason = f"unlocks {build_name}"
    if not bank_ok:
        reason = f"bank short on {' or '.join(sorted(set(pair)))}"

    return {
        "have": held,
        "should_play": bank_ok,  # If bank can't grant the pair, don't PLAY yet
        "reason": reason,
        "pair": pair,
        "unlock": build_name,
        "bank_ok": bank_ok,
    }


def _suggest_rb_placement(
    game, self_color_enum, *, max_edges: int = 2,
) -> dict[str, Any] | None:
    """Pick the best pair of free roads to lay when Road Building plays.

    Search strategy is intentionally local (no full minimax): walk
    out-edges from self's road network, then the out-edges after
    hypothetically laying each first pick. Pairs that land on a
    settlement-buildable node get ranked by that node's opening-score;
    a single-edge unlock is preferred over a 2-edge reach (less
    commitment, same reward). If no unlock is available, fall back to
    the pair that extends the longest continuous chain the most.

    Returns ``{edges, toward_node, toward_tiles, direction,
    placement_reason}`` or None when self has no legal road build.
    """
    from catanatron import Color  # noqa: F401 — only for typing clarity
    from cataanbot.advisor import (
        _build_node_neighbors, score_opening_nodes,
    )
    from cataanbot.recommender import _tile_label

    board = game.state.board
    m = board.map
    neighbors = _build_node_neighbors(m)
    try:
        first_edges = list(board.buildable_edges(self_color_enum))
    except Exception:  # noqa: BLE001
        return None
    if not first_edges:
        return None

    # Distance-2 legal-settlement filter mirrors the opening scorer.
    blocked: set[int] = set()
    for nid, (col, bt) in board.buildings.items():
        if bt in ("SETTLEMENT", "CITY"):
            blocked.add(int(nid))
            blocked |= {int(x) for x in neighbors.get(int(nid), set())}
    scored = {ns.node_id: ns for ns in score_opening_nodes(game)}

    def node_is_buildable(nid: int) -> bool:
        return nid not in blocked and nid in scored

    # My network endpoints. We need this to score "longest-path gain" as
    # a fallback when no unlock is available. Chain length is just the
    # count of consecutive edges reachable from any of my network nodes
    # including the two new ones.
    my_edges: set[frozenset[int]] = set()
    my_nodes: set[int] = set()
    for (a, b), col in board.roads.items():
        if col == self_color_enum:
            my_edges.add(frozenset((int(a), int(b))))
            my_nodes.add(int(a))
            my_nodes.add(int(b))
    for nid, (col, bt) in board.buildings.items():
        if col == self_color_enum:
            my_nodes.add(int(nid))

    enemy_bld_nodes: set[int] = {
        int(nid) for nid, (col, bt) in board.buildings.items()
        if col != self_color_enum
    }

    def step2_edges_from(far_node: int, first_edge: tuple[int, int]):
        """Legal edges to build on a board where ``first_edge`` has been
        laid. Rules: can't step through an enemy settle/city; can't reuse
        an existing road."""
        out: list[tuple[int, int]] = []
        if far_node in enemy_bld_nodes:
            return out
        for nb in neighbors.get(int(far_node), ()):
            e2 = (int(far_node), int(nb))
            if nb == first_edge[0]:
                continue
            existing = (board.roads.get(e2)
                        or board.roads.get((e2[1], e2[0])))
            if existing is not None:
                continue
            out.append(e2)
        return out

    def longest_path_from(new_edges: set[frozenset[int]]) -> int:
        """Rough longest continuous chain on (my_edges ∪ new_edges).
        Not topology-perfect — we just DFS from each endpoint of a new
        edge and count the longest simple path. Good enough to rank
        extension candidates relative to each other."""
        g = my_edges | new_edges
        if not g:
            return 0
        adj: dict[int, set[int]] = {}
        for e in g:
            a, b = tuple(e)
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        # Enemy nodes break chains the same way they break road-legality.
        best_len = 0
        seeds = set()
        for e in new_edges:
            seeds |= set(e)
        if not seeds:
            seeds = set(adj)
        for start in seeds:
            stack = [(start, frozenset(), 0)]
            while stack:
                node, used, length = stack.pop()
                if length > best_len:
                    best_len = length
                for nb in adj.get(node, ()):
                    if nb in enemy_bld_nodes and nb != start:
                        continue
                    edge = frozenset((node, nb))
                    if edge in used:
                        continue
                    stack.append((nb, used | {edge}, length + 1))
        return best_len

    # (score, tag, edges, toward_node). Higher score wins.
    candidates: list[tuple[float, str, list[tuple[int, int]], int]] = []
    for (a1, b1) in first_edges:
        new1 = {frozenset((int(a1), int(b1)))}
        # Case A: single-edge unlock at b1.
        if node_is_buildable(int(b1)):
            sc = float(scored[int(b1)].score)
            candidates.append((
                sc * 10.0 + 5.0,  # +5 bonus: 1-edge cost beats 2-edge
                "unlocks settlement",
                [(int(a1), int(b1))],
                int(b1),
            ))
        # Case B: 2-edge unlock at b2.
        for (a2, b2) in step2_edges_from(int(b1), (int(a1), int(b1))):
            if node_is_buildable(int(b2)):
                sc = float(scored[int(b2)].score)
                candidates.append((
                    sc * 10.0,
                    "unlocks 2-hop settle",
                    [(int(a1), int(b1)), (int(a2), int(b2))],
                    int(b2),
                ))
        # Case C (fallback): pure longest-road extension. Gets ranked
        # below any unlock — unlock score starts at >= _score_opening(0)
        # ≈ 2, so chain-only scores cap below 2.
        for (a2, b2) in step2_edges_from(int(b1), (int(a1), int(b1))):
            new2 = new1 | {frozenset((int(a2), int(b2)))}
            chain = longest_path_from(new2)
            candidates.append((
                0.05 * float(chain),
                f"extends chain to {chain}",
                [(int(a1), int(b1)), (int(a2), int(b2))],
                int(b2),
            ))
        # Single-edge chain extension (when no second edge is legal).
        chain1 = longest_path_from(new1)
        candidates.append((
            0.04 * float(chain1),
            f"extends chain to {chain1}",
            [(int(a1), int(b1))],
            int(b1),
        ))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    _, tag, edges, toward = candidates[0]

    # Always show both Road Building placements when the primary plan
    # is a single-edge unlock — the player is paying for two free
    # roads, not one, and the second placement still matters (LR
    # extension, 2-hop reach, or reserving a corridor). Without this,
    # the hint shows the unlock road and silently leaves the second
    # placement up to the player to figure out from scratch.
    second_edges: list[tuple[int, int]] = []
    second_reason: str | None = None
    second_toward: int | None = None
    if len(edges) == 1 and max_edges >= 2:
        # Hypothetically place the primary edge, then re-evaluate
        # the best follow-up edge from the expanded network.
        primary = edges[0]
        primary_set = {frozenset((int(primary[0]), int(primary[1])))}
        followups: list[tuple[float, str, tuple[int, int]]] = []
        # Out-edges from the just-placed primary's far node.
        for (a2, b2) in step2_edges_from(int(primary[1]),
                                         (int(primary[0]),
                                          int(primary[1]))):
            edge = frozenset((int(a2), int(b2)))
            if edge in primary_set:
                continue
            # Prefer landing on a buildable corner ("setup the next
            # settle"); fall back to chain extension.
            if node_is_buildable(int(b2)):
                sc = float(scored[int(b2)].score)
                followups.append(
                    (sc * 10.0, f"sets up settle at {int(b2)}",
                     (int(a2), int(b2))))
            chain = longest_path_from(primary_set
                                      | {edge})
            followups.append(
                (0.05 * float(chain),
                 f"extends chain to {chain}",
                 (int(a2), int(b2))))
        # Out-edges from elsewhere in the network (in case the best
        # second placement isn't adjacent to the primary).
        try:
            other_edges = list(board.buildable_edges(self_color_enum))
        except Exception:  # noqa: BLE001
            other_edges = []
        for (a2, b2) in other_edges:
            edge = frozenset((int(a2), int(b2)))
            if edge in primary_set:
                continue
            if node_is_buildable(int(b2)):
                sc = float(scored[int(b2)].score)
                followups.append(
                    (sc * 10.0, f"unlocks settle at {int(b2)}",
                     (int(a2), int(b2))))
            chain = longest_path_from(primary_set | {edge})
            followups.append(
                (0.04 * float(chain),
                 f"extends chain to {chain}",
                 (int(a2), int(b2))))
        if followups:
            followups.sort(key=lambda c: c[0], reverse=True)
            _, second_reason, e2 = followups[0]
            second_edges = [e2]
            second_toward = int(e2[1])

    full_edges = list(edges) + second_edges
    out: dict[str, Any] = {
        "edges": [list(e) for e in full_edges],
        "toward_node": int(toward),
        "toward_tiles": _tile_label(m, int(toward)),
        "placement_reason": tag,
    }
    if second_reason:
        out["second_placement_reason"] = second_reason
    if second_toward is not None:
        out["second_toward_node"] = second_toward
        out["second_toward_tiles"] = _tile_label(m, second_toward)
    return out


def _compute_rb_hint(game, self_color: str,
                     playable_count: int = 0,
                     ) -> dict[str, Any] | None:
    """Recommend whether to play Road Building this turn.

    Fires only when self holds a ROAD_BUILDING card AND has at least
    one road piece left to place. Two free roads is worth the most
    when it swings longest road — either qualifying self or catching
    an opp who's about to. Secondary case: road supply is almost
    exhausted, so play while the cards are still useful.

    Returns ``{have, should_play, reason, self_len, opp_len, placement?}``
    or None when we shouldn't surface a hint. The projected length is a
    naive +2 to self's current chain — catanatron recomputes
    topology-aware length after play, so this is a hint upper bound,
    not a promise. ``placement`` carries the concrete pair of edges to
    lay when we can compute one.
    """
    from catanatron import Color
    try:
        my_enum = Color[self_color.upper()]
    except Exception:  # noqa: BLE001
        return None
    state = game.tracker.game.state
    idx = state.color_to_index.get(my_enum)
    if idx is None:
        return None
    held = int(state.player_state.get(
        f"P{idx}_ROAD_BUILDING_IN_HAND", 0))
    if held <= 0:
        held = int(playable_count or 0)
    # Free roads pending mid-RB: catanatron decrements
    # state.free_roads_available from 2 → 1 → 0 as the player lays
    # each road. While > 0 we should keep the hint up so the player
    # can see WHERE to place the remaining free road(s) — without
    # this, the banner vanishes the instant they place road #1 and
    # the "road #2" suggestion drops on the floor mid-play.
    free_pending = int(getattr(state, "free_roads_available", 0) or 0)
    if held <= 0 and free_pending <= 0:
        return None
    # Need at least 1 road piece left to get any value. (The card
    # still plays with 0 roads available but grants nothing — treat
    # as a non-hint in that case to avoid nudging a wasted play.)
    pieces = _pieces_for_color(game, self_color)
    roads_left = int(pieces.get("road_left", 0))
    if roads_left <= 0:
        return None

    self_len = int(state.player_state.get(
        f"P{idx}_LONGEST_ROAD_LENGTH", 0))
    self_has = bool(state.player_state.get(
        f"P{idx}_HAS_ROAD", False))
    opp_max = 0
    opp_has = False
    for c, oidx in state.color_to_index.items():
        if c == my_enum:
            continue
        ln = int(state.player_state.get(
            f"P{oidx}_LONGEST_ROAD_LENGTH", 0))
        opp_max = max(opp_max, ln)
        if state.player_state.get(f"P{oidx}_HAS_ROAD", False):
            opp_has = True
    projected = self_len + min(2, roads_left)
    qualify = 5  # base-game longest-road threshold

    should = False
    reason = "no clear swing yet"
    if not self_has and projected >= max(qualify, opp_max + 1):
        should = True
        reason = (f"secures LR · "
                  f"{self_len}→{projected} vs {opp_max}")
    elif opp_has and opp_max >= qualify and projected >= opp_max:
        should = True
        reason = (f"catches opp LR · "
                  f"proj {projected} ≥ {opp_max}")
    elif roads_left <= 2:
        # Almost out of roads — card loses value the longer you hold it.
        should = True
        reason = f"low on roads · {roads_left} left"

    out: dict[str, Any] = {
        "have": held,
        "should_play": should,
        "reason": reason,
        "self_len": self_len,
        "opp_len": opp_max,
        # Free roads still pending after the card was played. Drives
        # the HUD's "PLACE — N free road(s) left" copy so Noah knows
        # the recs apply mid-RB, not just the pre-play hint.
        "free_roads_pending": free_pending,
    }
    # Mid-play override: card already played, just need placement
    # guidance for the remaining free road(s). Override should/reason
    # so the HUD reads PLACE rather than PLAY.
    if held <= 0 and free_pending > 0:
        out["should_play"] = True
        out["reason"] = (
            f"place {free_pending} free road"
            + ("s" if free_pending > 1 else ""))
    try:
        # Limit the placement suggestion to the number of roads still
        # pending — when only one is left, returning a 2-edge plan
        # would mis-describe the situation.
        placement = _suggest_rb_placement(
            game.tracker.game, my_enum,
            max_edges=(free_pending if free_pending > 0 else 2),
        )
        if placement is not None:
            out["placement"] = placement
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] rb placement failed: {e!r}", flush=True)
    return out


def _compute_game_plan(
    game, self_color: str, hand: dict[str, int],
) -> dict[str, Any] | None:
    """Compose a multi-step plan toward the next meaningful goal.

    Reads like a chess principal variation — "2 roads then settle · 4
    wood→brick if stuck" — so Noah can mid-turn stay on a plan instead
    of picking from a flat list each time.

    Search finds the highest pip-prod settlement spot within 2 road
    hops of self's network (0-hop = already connected, 1-hop = one
    road away, 2-hop = two roads away). Costs out the full plan
    (roads + settlement), diffs against self's hand, and if short
    picks a trade-fallback the user could lean on: prefers a port 2:1
    or 3:1 when self owns one, falling back to 4:1 bank. Falls back
    to a city plan when no settle is reachable.

    Returns ``None`` during setup or when we can't compute anything
    meaningful. Otherwise ``{goal_kind, goal_label, goal_node?,
    goal_tiles, roads_needed, missing, trade_plan?, summary}``.
    """
    from catanatron import Color
    try:
        my_enum = (self_color if isinstance(self_color, Color)
                   else Color[str(self_color).upper()])
    except Exception:  # noqa: BLE001
        return None

    # Setup phase plans live in the opening recs, not here.
    try:
        my_idx = game.tracker.game.state.color_to_index.get(my_enum)
        if my_idx is None:
            return None
        placed = int(game.tracker.game.state.player_state.get(
            f"P{my_idx}_SETTLEMENTS_AVAILABLE", 5))
        # Fewer than 3 means we've played at least 2 settles (opening
        # done). If we still have 4+ available, we're mid-setup.
        if placed >= 4:
            return None
    except Exception:  # noqa: BLE001
        pass

    from cataanbot.advisor import _build_node_neighbors, player_ports
    from cataanbot.recommender import (
        _SETTLEMENT_COST, _CITY_COST, _ROAD_COST,
        _node_pip_production, _tile_label,
    )

    cat = game.tracker.game
    board = cat.state.board
    m = board.map
    neighbors = _build_node_neighbors(m)
    land = set(m.land_nodes)

    # Distance-2 blocked nodes — can't settle adjacent to any building.
    buildings = board.buildings
    blocked: set[int] = {int(x) for x in buildings.keys()}
    for nid in list(buildings.keys()):
        blocked |= {int(x) for x in neighbors.get(int(nid), set())}

    # My road-network nodes + my building nodes; enemy settles/cities
    # break road-legality the same way they block adjacent settlement
    # placement.
    my_nodes: set[int] = set()
    for (a, b), rc in board.roads.items():
        if rc == my_enum:
            my_nodes.add(int(a)); my_nodes.add(int(b))
    for nid, (col, _bt) in buildings.items():
        if col == my_enum:
            my_nodes.add(int(nid))
    enemy_bld_nodes: set[int] = {
        int(nid) for nid, (col, _bt) in buildings.items()
        if col != my_enum
    }
    my_edges: set[frozenset[int]] = {
        frozenset((int(a), int(b))) for (a, b), rc in board.roads.items()
        if rc == my_enum
    }

    def reach_hops(target: int) -> int | None:
        """BFS from my network to target — minimum roads needed to
        reach it. Returns 0 if already connected, 1 or 2 for roads
        needed, None when further than 2 hops. Stops at enemy buildings
        (they block road-legality)."""
        if target in my_nodes:
            return 0
        frontier: list[tuple[int, int]] = [(n, 0) for n in my_nodes]
        visited = set(my_nodes)
        while frontier:
            node, hops = frontier.pop(0)
            if hops >= 2:
                continue
            for nb in neighbors.get(node, ()):
                if nb in visited:
                    continue
                visited.add(nb)
                if nb == target:
                    return hops + 1
                if nb in enemy_bld_nodes:
                    continue
                frontier.append((nb, hops + 1))
        return None

    # Rank candidate settlement targets: prefer fewer hops, then higher
    # pip production. Filter to reachable-in-2 land nodes that aren't
    # distance-2 blocked and aren't already my own building.
    best: tuple[int, int, float] | None = None  # (hops, node, prod)
    for nid in land:
        if nid in blocked:
            continue
        hops = reach_hops(int(nid))
        if hops is None:
            continue
        prod = _node_pip_production(m, int(nid))
        if prod <= 0:
            continue
        # Sort key: (hops, -prod). Lower hops win ties go to higher prod.
        if best is None:
            best = (hops, int(nid), prod)
        else:
            if (hops, -prod) < (best[0], -best[2]):
                best = (hops, int(nid), prod)

    # No reachable settle within 2 hops → fall back to city goal.
    if best is None:
        # Pick my highest-prod settlement as the city target.
        city_best: tuple[int, float] | None = None
        for nid, (col, bt) in buildings.items():
            if col != my_enum or bt != "SETTLEMENT":
                continue
            prod = _node_pip_production(m, int(nid))
            if city_best is None or prod > city_best[1]:
                city_best = (int(nid), prod)
        if city_best is None:
            return None
        node, prod = city_best
        cost = _CITY_COST
        missing = {r: max(0, cost.get(r, 0) - hand.get(r, 0))
                   for r in ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")}
        missing = {r: n for r, n in missing.items() if n > 0}
        trade_plan = _plan_trade_fallback(cat, my_enum, hand, cost, missing)
        tiles = _tile_label(m, node)
        # The goal_tiles chips render right after the summary in the
        # HUD, so the location is already shown — drop "city at {tiles}"
        # prose here to avoid duplicating the chips. Keep just the
        # missing/ready state and any stuck-trade tail.
        if missing:
            summary = _format_missing_short(missing)
        else:
            summary = "ready"
        if trade_plan:
            summary += (f" · {trade_plan['ratio']}:1 "
                        f"{_emoji_for(trade_plan['from_res'])}"
                        f"→{_emoji_for(trade_plan['to_res'])} if stuck")
        return {
            "goal_kind": "city",
            "goal_label": f"city at {_short_tile_label(tiles)}",
            "goal_node": node,
            "goal_tiles": tiles,
            "roads_needed": 0,
            "missing": missing,
            "trade_plan": trade_plan,
            "summary": summary,
        }

    hops, node, prod = best
    # Total plan cost = hops × road + 1 settlement.
    cost: dict[str, int] = {
        k: 0 for k in ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")}
    for r, n in _ROAD_COST.items():
        cost[r] += n * hops
    for r, n in _SETTLEMENT_COST.items():
        cost[r] += n
    missing = {r: max(0, cost[r] - hand.get(r, 0)) for r in cost}
    missing = {r: n for r, n in missing.items() if n > 0}
    trade_plan = _plan_trade_fallback(cat, my_enum, hand, cost, missing)

    tiles = _tile_label(m, node)
    # The goal_tiles chips render right after the summary in the HUD,
    # so location is already shown — drop "settle at {tiles}" prose to
    # avoid duplicating the chips. Lead with the road hops when any,
    # otherwise fall back to "ready"/missing-resource state.
    parts: list[str] = []
    if hops > 0:
        parts.append(f"{hops} road{'s' if hops > 1 else ''}")
    if missing:
        parts.append(_format_missing_short(missing))
    elif hops == 0:
        parts.append("ready")
    summary = " · ".join(parts)
    if trade_plan:
        summary += (f" · {trade_plan['ratio']}:1 "
                    f"{_emoji_for(trade_plan['from_res'])}"
                    f"→{_emoji_for(trade_plan['to_res'])} if stuck")

    return {
        "goal_kind": "settlement",
        "goal_label": f"settle at {_short_tile_label(tiles)}",
        "goal_node": node,
        "goal_tiles": tiles,
        "roads_needed": hops,
        "missing": missing,
        "trade_plan": trade_plan,
        "summary": summary,
    }


def _short_tile_label(tiles: list[tuple[str, int]] | None) -> str:
    """One-line tile label: "wheat 6 + ore 11". Skip desert (no num)."""
    if not tiles:
        return "?"
    parts = []
    for t in tiles:
        if not t or t[0] == "DESERT":
            continue
        res, num = t[0], t[1]
        parts.append(f"{res.lower()[:3]}{num}" if num else res.lower()[:3])
    return "+".join(parts) if parts else "?"


def _format_missing_short(missing: dict[str, int]) -> str:
    """Compact missing-cards string: "need 1🧱 1🐑".

    Uses resource emojis to match the rest of the HUD (opp ports, prod
    top resource, trade fallback on the same banner). The old "1b 1s"
    letter abbreviations were tight but inconsistent with the icon
    convention used elsewhere.
    """
    if not missing:
        return ""
    parts = [f"{n}{_emoji_for(r)}" for r, n in missing.items()]
    return "need " + " ".join(parts)


_RES_EMOJI = {
    "WOOD": "🌲", "BRICK": "🧱", "SHEEP": "🐑",
    "WHEAT": "🌾", "ORE": "⛰️",
}


def _emoji_for(res: str | None) -> str:
    """Resource → emoji used across game-plan + banner trade strings."""
    if not res:
        return "?"
    return _RES_EMOJI.get(res.upper(), res[:3].lower())


def _plan_trade_fallback(
    cat_game, my_enum, hand: dict[str, int], cost: dict[str, int],
    missing: dict[str, int],
) -> dict[str, Any] | None:
    """Pick a single best trade plan to cover the first missing resource.

    Chooses the cheapest ratio available given self's port ownership —
    2:1 specific port, 3:1 generic port, otherwise 4:1 bank. The trade
    source must be a resource we hold in excess (not needed for the
    current plan). Returns None when no legal trade can bridge the gap.
    """
    if not missing:
        return None
    try:
        from cataanbot.advisor import player_ports
        ports = set(player_ports(cat_game, my_enum))
    except Exception:  # noqa: BLE001
        ports = set()
    # "Excess" = hand minus what this plan needs.
    surplus: dict[str, int] = {}
    for r in ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE"):
        excess = hand.get(r, 0) - cost.get(r, 0)
        if excess > 0:
            surplus[r] = excess
    missing_r = next(iter(missing.keys()))
    best_from: str | None = None
    best_ratio = 99
    for from_r, excess in surplus.items():
        if from_r in ports:
            ratio = 2
        elif "GENERIC" in ports:
            ratio = 3
        else:
            ratio = 4
        if excess >= ratio and ratio < best_ratio:
            best_ratio = ratio
            best_from = from_r
    if best_from is None:
        return None
    return {
        "from_res": best_from,
        "from_count": best_ratio,
        "to_res": missing_r,
        "ratio": best_ratio,
    }


def _compute_strategic_options(
    game, self_color: str, hand: dict[str, int],
) -> list[dict[str, Any]] | None:
    """Surface riskier / longer-horizon plays that the flat rec list
    doesn't cover.

    The default recommender ranks what's affordable **right now** and
    fans out "save for X" plans for 1-2 cards away. That's tight but
    conservative — it misses VP-swing plays that take pieces and turns
    but materially change the endgame:

        * **Longest road push** — when self is at 4 roads (1 away from
          qualifying) and the race is open.
        * **Largest army push** — when self has knights played + held
          ≥ 3 and the LA holder is within 1.
        * **Dev-card dive** — when self is flush on ore+wheat+sheep and
          no higher-value build fits, surface a multi-card buy toward
          hidden VP + the dev-card engine.

    Returns a list of ``{kind, label, detail, vp_swing, pieces}``
    options ordered by expected VP impact. ``None`` when nothing is
    actionable so the overlay can hide the section silently.
    """
    from catanatron import Color
    try:
        my_enum = (self_color if isinstance(self_color, Color)
                   else Color[str(self_color).upper()])
    except Exception:  # noqa: BLE001
        return None

    state = game.tracker.game.state
    my_idx = state.color_to_index.get(my_enum)
    if my_idx is None:
        return None

    # Stay quiet during setup.
    try:
        placed = int(state.player_state.get(
            f"P{my_idx}_SETTLEMENTS_AVAILABLE", 5))
        if placed >= 4:
            return None
    except Exception:  # noqa: BLE001
        pass

    ps = state.player_state
    options: list[dict[str, Any]] = []

    # ---- Longest road push -------------------------------------------
    self_len = int(ps.get(f"P{my_idx}_LONGEST_ROAD_LENGTH", 0))
    self_has_lr = bool(ps.get(f"P{my_idx}_HAS_ROAD", False))
    opp_lr_max = 0
    opp_lr_holder = False
    for col, idx in state.color_to_index.items():
        if col == my_enum:
            continue
        ol = int(ps.get(f"P{idx}_LONGEST_ROAD_LENGTH", 0))
        oh = bool(ps.get(f"P{idx}_HAS_ROAD", False))
        if ol > opp_lr_max:
            opp_lr_max = ol
        if oh:
            opp_lr_holder = True
    if self_len >= 3 and not self_has_lr:
        target_len = max(5, opp_lr_max + 1)
        roads_needed = target_len - self_len
        # Reddit 36k-game finding #5: LR wins 56-61% of games and is
        # sticky (85% of 3p games never see LR change hands once
        # claimed). Surface the push when within 2 roads of qualifying
        # so the player can plan ahead, not just on the 1-road victory
        # lap. Past 2 roads it dilutes — too speculative against the
        # other strategic options on the strip.
        if 1 <= roads_needed <= 2:
            vp_swing = 2 if not opp_lr_holder else 4  # take + denial
            options.append({
                "kind": "longest_road_push",
                "label": "LR push",
                "detail": (f"+{roads_needed} road"
                           f"{'s' if roads_needed > 1 else ''}"
                           + (" · denies opp" if opp_lr_holder else "")),
                "vp_swing": vp_swing,
                "pieces": roads_needed,
            })

    # ---- Largest army push -------------------------------------------
    knights_played = int(ps.get(f"P{my_idx}_PLAYED_KNIGHT", 0))
    knights_held = int(ps.get(f"P{my_idx}_KNIGHT_IN_HAND", 0))
    self_has_la = bool(ps.get(f"P{my_idx}_HAS_ARMY", False))
    opp_knights_max = 0
    opp_la_holder = False
    for col, idx in state.color_to_index.items():
        if col == my_enum:
            continue
        ok = int(ps.get(f"P{idx}_PLAYED_KNIGHT", 0))
        oh = bool(ps.get(f"P{idx}_HAS_ARMY", False))
        if ok > opp_knights_max:
            opp_knights_max = ok
        if oh:
            opp_la_holder = True
    la_threshold = max(3, opp_knights_max + 1)
    needed_plays = max(0, la_threshold - knights_played)
    if (not self_has_la and knights_held >= 1
            and knights_played + knights_held >= la_threshold
            and needed_plays > 0):
        vp_swing = 2 if not opp_la_holder else 4
        options.append({
            "kind": "largest_army_push",
            "label": "LA push",
            "detail": (f"play {needed_plays} knight"
                       f"{'s' if needed_plays > 1 else ''}"
                       + (" · denies opp" if opp_la_holder else "")),
            "vp_swing": vp_swing,
            "pieces": needed_plays,
        })

    # ---- Dev-card dive ------------------------------------------------
    # When self has multiple dev-card buys stacked (3+ full bundles of
    # ore+wheat+sheep) and the board has nothing better to spend them
    # on — worth surfacing as a hidden-VP play.
    bundles = min(hand.get("ORE", 0),
                  hand.get("WHEAT", 0),
                  hand.get("SHEEP", 0))
    if bundles >= 3:
        options.append({
            "kind": "dev_card_dive",
            "label": "dev-card dive",
            "detail": f"buy {min(bundles, 4)} dev · hidden VP",
            "vp_swing": 1,
            "pieces": 0,
        })

    if not options:
        return None
    # Higher VP swing first, then by fewer pieces needed (cheaper path).
    options.sort(key=lambda o: (-o["vp_swing"], o["pieces"]))
    return options


def _compute_knight_hint(
    game, display_colors: dict[str, str] | None = None,
    playable_count: int = 0,
) -> dict[str, Any] | None:
    """Recommend whether to play a Knight dev card this turn.

    Fires only when self has at least one **playable** KNIGHT in hand.
    Catan's just-bought rule: dev cards bought this turn can't be
    played until next turn — colonist ships
    ``developmentCardsBoughtThisTurn`` (a list of type ints) so we
    subtract any KNIGHT (type 11) bought this turn from the hand
    count. If every knight in hand was just bought, the hint stays
    silent.

    The "should play" logic weighs:
        * Robber currently on one of self's tiles → urgent remove
        * Top robber target score >= 4 → meaningful block
        * An opp at 7+ VP with 2+ played knights → deny largest-army

    Returns {have, should_play, reason, best_target} or None if self
    has no playable Knight or we can't determine self color.
    """
    from catanatron import Color

    sess = game.session
    if sess is None or sess.self_color_id is None:
        return None
    username = sess.player_names.get(sess.self_color_id)
    if not username:
        return None
    try:
        color = game.color_map.get(username)
    except Exception:  # noqa: BLE001
        return None
    try:
        my_enum = Color[color.upper()]
    except Exception:  # noqa: BLE001
        return None

    state = game.tracker.game.state
    idx = state.color_to_index.get(my_enum)
    if idx is None:
        return None
    knight_in_hand = int(
        state.player_state.get(f"P{idx}_KNIGHT_IN_HAND", 0))
    if knight_in_hand <= 0:
        knight_in_hand = int(playable_count or 0)
    # Subtract knights bought this turn — Catan rule: can't play a
    # dev card the same turn it was bought. Colonist's WS ships the
    # type list, so we count exact KNIGHT-type buys (type int 11).
    bought_knights_this_turn = 0
    try:
        bought = list(getattr(sess, "self_dev_bought_this_turn", []) or [])
        # KNIGHT int = 11 (decoded from devcard-decode capture, see
        # colonist_diff._DEV_CARD_TYPE).
        bought_knights_this_turn = sum(
            1 for tid in bought if int(tid) == 11)
    except Exception:  # noqa: BLE001
        pass
    playable_knights = knight_in_hand - bought_knights_this_turn
    if playable_knights <= 0:
        return None
    knight_in_hand = playable_knights

    board = game.tracker.game.state.board
    robber = board.robber_coordinate
    # Robber currently blocking me? Find self buildings on the robber tile.
    self_blocked_pips = 0
    m = board.map
    robber_tile = m.land_tiles.get(robber) if robber else None
    if robber_tile is not None and robber_tile.number:
        from cataanbot.advisor import PIP_DOTS_BY_NUMBER
        robber_node_ids = set(robber_tile.nodes.values())
        for nid, (bcol, _bt) in board.buildings.items():
            if bcol != my_enum or int(nid) not in robber_node_ids:
                continue
            self_blocked_pips += PIP_DOTS_BY_NUMBER.get(robber_tile.number, 0)

    # Opp closing in on largest army? Two trigger paths:
    #   (a) played >= 2 AND vp >= largest_army_threat_vp — "they're
    #       racing to close out" (existing condition).
    #   (b) played >= 3 — "they're explicitly playing for LA" even
    #       at low VP. By the time the (a) trigger fires the race
    #       is often already lost; (b) catches the build-up phase.
    # Came out of Noah's 2026-05-03 loss vs an opp: opp played 5
    # knights total but the deny-LA hint never fired loudly enough
    # to push Noah's own knights into play.
    from cataanbot.config import largest_army_threat_vp
    la_threat_vp = largest_army_threat_vp()
    largest_army_threat = False
    for opp_color, opp_idx in state.color_to_index.items():
        if opp_color == my_enum:
            continue
        played = int(state.player_state.get(
            f"P{opp_idx}_PLAYED_KNIGHT", 0))
        vp = int(state.player_state.get(
            f"P{opp_idx}_VICTORY_POINTS", 0))
        if (played >= 3
                or (played >= 2 and vp >= la_threat_vp)):
            largest_army_threat = True
            break

    # Best robber target score (reuses the existing ranker).
    top_targets = _compute_robber_snapshot(
        game, display_colors=display_colors, top=1) or []
    top_target = top_targets[0] if top_targets else None
    top_score = float(top_target["score"]) if top_target else 0.0

    # Self's own progress toward Largest Army — needed for the
    # "you're close to LA" copy variant. Also figure out who currently
    # holds LA (if anyone) so the suggestion accounts for "stealing"
    # an already-held LA (which requires EXCEEDING the holder's count,
    # not just tying or reaching 3).
    self_played_knights = int(state.player_state.get(
        f"P{idx}_PLAYED_KNIGHT", 0))
    self_has_la = bool(state.player_state.get(f"P{idx}_HAS_ARMY", False))
    la_holder_played = 0
    la_held_by_someone = False
    for c, c_idx in state.color_to_index.items():
        if state.player_state.get(f"P{c_idx}_HAS_ARMY", False):
            la_held_by_someone = True
            la_holder_played = max(la_holder_played, int(
                state.player_state.get(f"P{c_idx}_PLAYED_KNIGHT", 0)))
    # Find the highest opp played_knight count — needed to know if
    # this play actually CLAIMS Largest Army or just ties someone.
    # Catan rule: must STRICTLY EXCEED every other player to hold LA.
    opp_max_played = 0
    for c, c_idx in state.color_to_index.items():
        if c == my_enum:
            continue
        opp_max_played = max(opp_max_played, int(
            state.player_state.get(f"P{c_idx}_PLAYED_KNIGHT", 0)))

    # Knights needed to grab LA after this play (which adds 1 to
    # self's played count):
    if self_has_la:
        # Already hold LA — playing knight is for blocking value.
        knight_secures_la = False
    elif la_held_by_someone:
        # Held by opp at la_holder_played. Need self+1 > holder.
        knight_secures_la = self_played_knights >= la_holder_played
    else:
        # Nobody holds LA. Need self+1 >= 3 (the LA threshold) AND
        # self+1 > every opp's played count.
        knight_secures_la = (
            self_played_knights >= 2
            and (self_played_knights + 1) > opp_max_played)

    should = False
    # Reason copy is intentionally conversational — Noah said the old
    # "strong block · score +10" stat-string didn't tell him *why* to
    # play. The reasons below name a concrete situation (robber on
    # you / opp close to LA / you close to LA / a tile worth blocking)
    # so the verdict reads as advice, not a stat dump.
    reason = "no urgent reason — hold for now"
    if self_blocked_pips > 0:
        should = True
        # Translate "pips" (Catan-jargon for the dots under each tile
        # number, summing to 6 max per tile and predicting how often
        # it rolls) into something a normal player understands:
        # expected cards blocked per roll. pips/36 ≈ cards per dice
        # roll. A player who reads "blocks ~0.42 cards/roll" gets
        # the magnitude immediately; "10 pips blocked" was opaque
        # to anyone who hadn't memorized the dot table.
        cards_per_roll = self_blocked_pips / 36.0
        reason = (f"robber's on you — play to clear it "
                  f"(~{cards_per_roll:.2f} cards/roll blocked)")
    elif largest_army_threat:
        should = True
        reason = "an opp is close to Largest Army — play to deny"
    elif knight_secures_la:
        should = True
        if la_held_by_someone:
            reason = ("playing this knight steals Largest Army "
                      "from the current holder (+2 VP)")
        else:
            reason = ("you're 1 knight from Largest Army — "
                      "play it to grab the +2 VP")
    elif top_score >= 4.0:
        should = True
        # Name the tile so it's actionable without the score number.
        if top_target and top_target.get("resource"):
            tile_lbl = (f"{top_target['resource'].lower()} "
                        f"{top_target.get('number') or ''}").strip()
            reason = f"a strong block on {tile_lbl} is available"
        else:
            reason = "a strong block is available"

    return {
        "have": knight_in_hand,
        "should_play": should,
        "reason": reason,
        "best_target": top_target,
    }
