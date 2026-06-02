"""Pure-function economy/board helpers extracted from bridge.py.

These functions don't touch the bridge `st` state — they take a `game`
plus simple args and return advisor-snapshot fragments. Kept here so
bridge.py stays focused on app construction and snapshot orchestration.

Re-exported by bridge.py for backwards compat with tests that import
from `catanbot.bridge` directly.
"""
from __future__ import annotations

from typing import Any


def _compute_production(
    game, color: str,
) -> dict[str, Any] | None:
    """Expected resource yield per roll given current builds.

    Sums ``map.node_production[node_id]`` across every settlement (×1)
    and city (×2) this color owns. ``per_roll`` is the total expected
    cards per dice roll — a rough pace indicator (1.0 = one card per
    roll, 2.5 = well-established). ``top_resource`` names the most-
    produced resource so Noah can tell "ore-heavy" from "sheep-heavy"
    at a glance.

    Color-generic: used for self (pace check) and each opp (threat
    ranking — informs robber target and trade-block priority).
    """
    try:
        from catanatron import Color
        my_enum = Color[color.upper()]
        board = game.tracker.game.state.board
        m = board.map
    except Exception:  # noqa: BLE001
        return None
    totals: dict[str, float] = {
        "WOOD": 0.0, "BRICK": 0.0, "SHEEP": 0.0, "WHEAT": 0.0, "ORE": 0.0,
    }
    for nid, (col, btype) in board.buildings.items():
        if col != my_enum:
            continue
        mult = 2.0 if str(btype).upper() == "CITY" else 1.0
        for res, pips in m.node_production.get(int(nid), {}).items():
            if res in totals:
                totals[res] += mult * float(pips)
    per_roll = sum(totals.values())
    top_res = max(totals, key=lambda r: totals[r]) if per_roll > 0 else None
    return {
        "per_roll": per_roll,
        "by_resource": totals,
        "top_resource": top_res if (top_res and totals[top_res] > 0) else None,
    }


def _owned_ports(game, color: str) -> list[str] | None:
    """Return a sorted list of ports this color has a coastal building
    on. Each entry is the port label as shown in ``advisor.player_ports``:
    a resource name (``"WHEAT"``, ``"SHEEP"``, etc.) for a 2:1, or
    ``"GENERIC"`` for the 3:1 port. Returns None on failure so the
    overlay can skip the render instead of guessing.
    """
    try:
        from catanbot.advisor import player_ports
        ports = player_ports(game.tracker.game, color)
    except Exception:  # noqa: BLE001
        return None
    # Stable order so the overlay doesn't flicker between refreshes.
    # GENERIC last so the specific 2:1s read first.
    specific = sorted(p for p in ports if p != "GENERIC")
    if "GENERIC" in ports:
        specific.append("GENERIC")
    return specific


def _knights_played(game, color: str) -> int:
    """Knights already played by `color` (for largest-army tracking).

    At 3+ this qualifies the player for largest army; holders at 2 are
    one knight away. Per-player visibility complements the single
    largest_army_race banner — shows *which* opp is actually the
    threat when multiple have dev cards in hand.
    """
    try:
        from catanatron import Color
        my_enum = Color[color.upper()]
        idx = game.tracker.game.state.color_to_index.get(my_enum)
        if idx is None:
            return 0
        return int(game.tracker.game.state.player_state.get(
            f"P{idx}_PLAYED_KNIGHT", 0))
    except Exception:  # noqa: BLE001
        return 0


# Played dev cards are public the moment they are played (colonist reveals
# the type), so catanatron tracks PLAYED_{type} per color. VP cards are
# excluded: they are never "played", they sit hidden in hand.
_PLAYED_DEV_KEYS = {
    "KNIGHT": "PLAYED_KNIGHT",
    "MONOPOLY": "PLAYED_MONOPOLY",
    "YEAR_OF_PLENTY": "PLAYED_YEAR_OF_PLENTY",
    "ROAD_BUILDING": "PLAYED_ROAD_BUILDING",
}


def _played_dev_by_type(game, color) -> dict[str, int]:
    """Per-type dev cards `color` has already PLAYED this game.

    All public info (a played card is revealed by the rules), so this is
    fair to surface: it shows what each opponent has burned and, by
    elimination against their bought count, what dev-card threats might
    still be in their hand. Accepts a catanatron Color or a color name.
    """
    out = {k: 0 for k in _PLAYED_DEV_KEYS}
    try:
        from catanatron import Color
        my_enum = color if isinstance(color, Color) else Color[str(color).upper()]
        idx = game.tracker.game.state.color_to_index.get(my_enum)
        if idx is None:
            return out
        ps = game.tracker.game.state.player_state
        for label, key in _PLAYED_DEV_KEYS.items():
            out[label] = int(ps.get(f"P{idx}_{key}", 0))
    except Exception:  # noqa: BLE001
        pass
    return out


# Build cost table used by _affordable_builds. Kept local to avoid
# coupling the opp-afford snapshot to discard-plan ordering, which is
# priority-sorted rather than impact-sorted. Order here is VP-impact
# descending so the overlay shows the most worrying afford tag first.
_AFFORD_COSTS: tuple[tuple[str, dict[str, int]], ...] = (
    ("city", {"WHEAT": 2, "ORE": 3}),
    ("settlement", {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1}),
    ("dev", {"WHEAT": 1, "SHEEP": 1, "ORE": 1}),
    ("road", {"WOOD": 1, "BRICK": 1}),
)


def _affordable_builds(
    inferred: dict[str, int], unknown: int = 0,
) -> list[str] | None:
    """Return the builds an opp's *definitely-known* hand can cover.

    Conservative by design: only flags builds whose every cost slot is
    fully covered by the inferred bucket. Unknowns don't count — they
    might be anything, so claiming affordability would over-alert Noah
    on buys that rely on hidden cards. Returns [] when the hand covers
    nothing, None on bad input (so overlay can silent-skip).

    When hand_tracked is false (unknown > 0) the answer is still useful:
    inferred is a *lower bound*, so "can: city" under unknowns still
    means they can city now, even if their hidden cards add more.
    """
    if not isinstance(inferred, dict):
        return None
    out: list[str] = []
    for name, cost in _AFFORD_COSTS:
        if all(inferred.get(r, 0) >= n for r, n in cost.items()):
            out.append(name)
    return out


def _closest_missing_build(
    hand: dict[str, int],
) -> dict[str, Any] | None:
    """The build with the smallest resource gap given ``hand``.

    When ``hand`` already covers every build, returns None — there's
    nothing to point at. Otherwise returns the nearest-miss: the build
    with the smallest sum of missing cards, with ties broken by VP
    impact (city > settlement > dev > road).

    The HUD uses this to turn "nothing buildable" into "1 brick from
    settle" — a direction of travel rather than a dead-end read.
    """
    if not isinstance(hand, dict):
        return None
    candidates: list[dict[str, Any]] = []
    # Preserves the VP-impact tie-break order — first-in-ties wins.
    BUILDS: tuple[tuple[str, dict[str, int]], ...] = (
        ("city", {"WHEAT": 2, "ORE": 3}),
        ("settlement", {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1}),
        ("dev card", {"WHEAT": 1, "SHEEP": 1, "ORE": 1}),
        ("road", {"WOOD": 1, "BRICK": 1}),
    )
    for name, cost in BUILDS:
        missing: dict[str, int] = {}
        for r, n in cost.items():
            have = int(hand.get(r, 0) or 0)
            if have < n:
                missing[r] = n - have
        if not missing:
            continue  # fully affordable — not the "next" build
        gap = sum(missing.values())
        candidates.append({
            "build": name, "missing": missing, "gap": gap,
        })
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["gap"])
    return candidates[0]


# Tie-break order when several resources are equally good to pull off the
# gold/volcano hex — wheat and ore carry the most builds (city + dev), so
# prefer them when nothing else decides it.
_GOLD_UTILITY_ORDER = ("WHEAT", "ORE", "SHEEP", "BRICK", "WOOD")


def _gold_resource_pick(
    hand: dict[str, int],
    production_by_resource: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    """Which resource to take when the gold/volcano hex rolls.

    Gold pays a resource of your choice, so the pick is "what unblocks the
    most valuable thing soonest". Priority:

    1. If you're short for a build, take the bottleneck resource of the
       nearest build (city > settlement > dev > road via
       ``_closest_missing_build``). Ties broken toward the resource you
       *produce* least, then by general utility.
    2. If every build is already affordable, bank your thinnest-produced
       resource (utility-biased) toward the next city.

    Returns ``{"resource", "reason", "toward"}`` or None on bad input.
    """
    if not isinstance(hand, dict):
        return None
    prod = production_by_resource or {}
    nb = _closest_missing_build(hand)
    if nb:
        missing = nb["missing"]
        build = nb["build"]
        gap = int(nb["gap"])
        res = sorted(
            missing,
            key=lambda r: (-missing[r], prod.get(r, 0.0),
                           _GOLD_UTILITY_ORDER.index(r)),
        )[0]
        reason = (f"completes your {build}" if gap == 1
                  else f"biggest step toward your {build}")
        return {"resource": res, "reason": reason, "toward": build}
    res = sorted(
        _GOLD_UTILITY_ORDER,
        key=lambda r: (prod.get(r, 0.0), _GOLD_UTILITY_ORDER.index(r)),
    )[0]
    return {"resource": res, "reason": "your thinnest resource · bank toward a city",
            "toward": None}


def _is_dev_stash_risk(
    vp: int, dev_cards: int, vp_target: int | None = None,
) -> bool:
    """Whether an opp's dev-card stash is a hidden-VP risk.

    True when dev_cards >= 2 AND (vp + dev_cards) >= (VP_TARGET - 1).
    The ``>= 2`` floor avoids false-positiving on every late-game opp
    holding a single knight. The sum threshold models "if they flipped
    every dev as a VP, they'd be within 1 of winning" — which is when
    holding onto them stops looking like a knight race and starts
    looking like a hidden-VP play.
    """
    from catanbot.config import VP_TARGET
    target = vp_target if vp_target is not None else VP_TARGET
    return dev_cards >= 2 and (vp + dev_cards) >= (target - 1)


def _one_short_vp_build(
    inferred: dict[str, int], unknown: int = 0,
    already_affordable: list[str] | None = None,
) -> dict | None:
    """The highest-VP build this opp is exactly 1 card short of.

    Scoped to city + settlement — the only builds worth tracking as
    threats, since road and dev rarely matter for a same-turn flip.
    When an opp is 1 ORE from a city, Noah can (a) withhold ORE in
    trades, (b) consider moving the robber onto an ORE tile, or (c)
    plan for an opp VP jump next turn. Actionable in a way that
    can_afford (already-flipped) is not.

    Skipped when the opp already has the build affordable — that's
    already surfaced by ``_affordable_builds``, showing "1 short"
    for the same opp would just be double-counting. Also skipped
    when ``unknown`` is high enough that the opp could already have
    the missing card (>=1 unknown): reporting "1 short" then would
    under-call the real risk.
    """
    if not isinstance(inferred, dict):
        return None
    already = set(already_affordable or [])
    best: dict | None = None
    # City outranks settlement for VP impact, so prefer it on ties.
    for name, cost in (("city", {"WHEAT": 2, "ORE": 3}),
                       ("settlement", {"WOOD": 1, "BRICK": 1,
                                       "SHEEP": 1, "WHEAT": 1})):
        if name in already:
            continue
        deficit = 0
        missing: str | None = None
        for r, n in cost.items():
            have = inferred.get(r, 0)
            if have < n:
                deficit += n - have
                missing = r
        if deficit == 1 and missing is not None:
            best = {"build": name, "need": missing,
                    "uncertain": unknown >= 1}
            break
    return best


def _pieces_for_color(game, color: str) -> dict[str, int]:
    """Settlement / city / road counts placed and remaining per color.

    Counts directly off the board (buildings dict + roads dict) since
    our tracker keeps those authoritative but doesn't decrement the
    catanatron ``Px_*_AVAILABLE`` pool keys. Base-game caps are 5/4/15
    for settlements/cities/roads. Roads in catanatron are stored with
    both edge directions, so we count unique frozenset edges.
    """
    try:
        from catanatron import Color
        my_enum = Color[color.upper()]
        board = game.tracker.game.state.board
    except Exception:  # noqa: BLE001
        return {"settle": 0, "settle_left": 5, "city": 0, "city_left": 4,
                "road": 0, "road_left": 15}
    settle = 0
    city = 0
    for nid, (col, btype) in board.buildings.items():
        if col != my_enum:
            continue
        if str(btype).upper() == "CITY":
            city += 1
        else:
            settle += 1
    seen_edges: set[frozenset] = set()
    for edge, col in board.roads.items():
        if col != my_enum:
            continue
        key = frozenset(edge) if not isinstance(edge, frozenset) else edge
        seen_edges.add(key)
    road = len(seen_edges)
    return {
        "settle": settle, "settle_left": max(0, 5 - settle),
        "city": city, "city_left": max(0, 4 - city),
        "road": road, "road_left": max(0, 15 - road),
    }


def _compute_bank_supply(game) -> dict[str, Any] | None:
    """Estimate how many of each resource remain in the bank.

    Uses the 19-per-resource Catan rule and the tracker's authoritative
    player hands to compute the difference. Returns None if we can't
    trust the math (e.g. a player has a totally inferred hand with
    unknowns, which would double-count against the bank). The `low`
    list calls out resources with ≤2 in the bank so the overlay can
    flash a warning — you can't port/4:1-trade into an empty resource
    and no one can receive it on a dice roll until someone pays back.
    """
    sess = game.session
    if sess is None:
        return None
    totals = {r: 0 for r in ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")}
    for user in sess.player_names.values():
        try:
            c = game.color_map.get(user)
        except Exception:  # noqa: BLE001
            continue
        h = game.tracker.hand(c)
        for r in totals:
            totals[r] += int(h.get(r, 0))
    remaining = {r: max(0, 19 - totals[r]) for r in totals}
    low = sorted(
        [(r, n) for r, n in remaining.items() if n <= 2],
        key=lambda kv: kv[1])
    return {
        "remaining": remaining,
        "low": [{"resource": r, "count": n} for r, n in low],
    }


def _compute_dev_deck_remaining(game) -> dict[str, Any] | None:
    """Estimate how many dev cards are left in the deck.

    Base game starts with 25: 14 knights, 5 VP, 2 monopoly, 2 YoP,
    2 road building. A dev card bought stays out of the deck forever
    (played or not), so ``remaining = 25 - total_ever_bought``. Total
    ever bought = sum across all players of (unplayed dev cards in
    hand) + (played knights + played specials). VP cards sit silently
    in hand so they're already covered by the unplayed count.

    Returns ``{remaining, drawn, low}`` where `low` is a bool flagged
    when ≤2 cards remain — buying a dev card becomes a gamble that
    can't happen at all once the deck is empty.
    """
    sess = game.session
    if sess is None:
        return None
    try:
        state = game.tracker.game.state
    except Exception:  # noqa: BLE001
        return None
    total_unplayed = 0
    total_played_actions = 0
    for cid in sess.player_names:
        total_unplayed += int(sess.dev_card_counts.get(cid, 0))
    action_keys = ("PLAYED_KNIGHT", "PLAYED_MONOPOLY",
                   "PLAYED_YEAR_OF_PLENTY", "PLAYED_ROAD_BUILDING")
    for _c, idx in state.color_to_index.items():
        for k in action_keys:
            total_played_actions += int(state.player_state.get(
                f"P{idx}_{k}", 0))
    drawn = total_unplayed + total_played_actions
    # Clamp — if tracking drift somehow outputs drawn > 25 we don't
    # want to surface a negative number.
    remaining = max(0, 25 - drawn)
    # Per-type breakdown: subtract total-played-across-all-players from
    # base deck composition (14 knights, 2 each of monopoly/YoP/road-
    # building). VP cards (5) sit hidden in hands and never log a
    # "played" action, so we can't infer remaining from plays — drop
    # VP from the breakdown to avoid showing a misleading number.
    # Reddit 36k-game finding #8 says LA is left on the table 29% of
    # games — Noah's request 2026-05-02 for a counter so he can spot
    # an under-contested LA push by knight scarcity.
    BASE_BY_TYPE = {
        "KNIGHT": 14,
        "MONOPOLY": 2,
        "YEAR_OF_PLENTY": 2,
        "ROAD_BUILDING": 2,
    }
    KEY_BY_TYPE = {
        "KNIGHT": "PLAYED_KNIGHT",
        "MONOPOLY": "PLAYED_MONOPOLY",
        "YEAR_OF_PLENTY": "PLAYED_YEAR_OF_PLENTY",
        "ROAD_BUILDING": "PLAYED_ROAD_BUILDING",
    }
    by_type: dict[str, dict[str, int]] = {}
    for type_name, base in BASE_BY_TYPE.items():
        played = 0
        key = KEY_BY_TYPE[type_name]
        for _c, idx in state.color_to_index.items():
            played += int(state.player_state.get(
                f"P{idx}_{key}", 0))
        by_type[type_name] = {
            "base": base,
            "played": played,
            # remaining = base - played (in deck OR held). We can't
            # split "in deck" from "held" without seeing opp hands.
            "remaining": max(0, base - played),
        }
    return {
        "remaining": remaining,
        "drawn": drawn,
        "low": remaining <= 2,
        "by_type": by_type,
    }


def _compute_largest_army_race(
    game, self_color: str | None,
) -> dict[str, Any] | None:
    """Flag a largest-army race once any player has ≥2 played knights.

    Largest-army qualifies at 3 played knights, so 2 = "one knight
    away." Same level structure as the longest-road race helper:
    self_push / opp_threat / contested / settled (silent).

    We look at PLAYED_KNIGHT (actual knights played) because that's
    the only authoritative count — knights in hand don't yet count
    toward the title.
    """
    from catanatron import Color

    state = game.tracker.game.state
    if self_color is None:
        return None
    try:
        my_enum = Color[self_color.upper()]
    except Exception:  # noqa: BLE001
        return None

    played: list[tuple[object, int, bool]] = []
    for col, idx in state.color_to_index.items():
        n = int(state.player_state.get(f"P{idx}_PLAYED_KNIGHT", 0))
        has_army = bool(state.player_state.get(f"P{idx}_HAS_ARMY", False))
        played.append((col, n, has_army))
    if not played:
        return None

    self_entry = next((e for e in played if e[0] == my_enum), None)
    opps = [e for e in played if e[0] != my_enum]
    if self_entry is None:
        return None
    self_n = self_entry[1]
    self_has = self_entry[2]
    top_opp = max(
        opps,
        key=lambda e: (e[1], 1 if e[2] else 0),
        default=None,
    )
    opp_max = top_opp[1] if top_opp else 0
    opp_holder_entry = next((e for e in opps if e[2]), None)
    opp_holder = opp_holder_entry is not None
    color_map = getattr(game, "color_map", None)

    def _name_for(entry) -> str:
        if entry is None or color_map is None:
            return "opp"
        col = entry[0]
        col_str = col.value if hasattr(col, "value") else str(col)
        uname = color_map.reverse(col_str)
        return uname or "opp"

    top_opp_name = _name_for(top_opp)
    holder_name = _name_for(opp_holder_entry) if opp_holder else None

    # Silent pre-race: need at least one side on 2 to matter.
    if self_n < 2 and opp_max < 2:
        return None
    # Settled: holder is 2+ ahead.
    if self_has and self_n >= opp_max + 2:
        return None
    if opp_holder and opp_max >= self_n + 2:
        return None

    # Contested (most specific): both sides ≥2 and within 1.
    if self_n >= 2 and opp_max >= 2 and abs(self_n - opp_max) <= 1:
        if self_has:
            holder = "you"
        elif holder_name:
            holder = holder_name
        else:
            holder = "nobody"
        return {
            "level": "contested",
            "self_n": self_n,
            "opp_n": opp_max,
            "opp_username": top_opp_name,
            "holder_username": holder_name,
            "message": (
                f"LA · you {self_n} / "
                f"{top_opp_name} {opp_max} · {holder} holds"),
        }
    if self_n >= 2 and not self_has and opp_max < self_n:
        return {
            "level": "self_push",
            "self_n": self_n,
            "opp_n": opp_max,
            "opp_username": top_opp_name,
            "message": f"1 knight → LA ({self_n})",
        }
    if opp_max >= 2 and opp_max >= self_n and not self_has:
        gap = opp_max - self_n
        if opp_holder:
            msg = (
                f"{holder_name or top_opp_name} has LA"
                f" · {opp_max} (+{gap})"
            )
        else:
            msg = f"{top_opp_name} 1 → LA ({opp_max})"
        return {
            "level": "opp_threat",
            "self_n": self_n,
            "opp_n": opp_max,
            "opp_username": top_opp_name,
            "holder_username": holder_name,
            "message": msg,
        }
    return None


def _compute_roll_yield(
    game, color: str, number: int,
) -> dict[str, Any] | None:
    """Break down what self would produce from a specific roll.

    Iterates every tile with ``tile.number == number``. For each, if
    the robber is parked there the buildings on that tile are blocked
    (tallied under ``blocked``); otherwise they contribute their
    resource to ``gained`` (×1 per settlement, ×2 per city). Returns
    None on bad input so the overlay can silent-skip.

    Used by the last-roll banner to surface what the dice actually
    delivered — and, more importantly, what the robber cost. A line
    like "+1 ore (3 ore blocked on the 8)" is worth more than just
    knowing a 7 didn't hit.
    """
    if number == 7 or not number:
        return None
    try:
        from catanatron import Color
        my_enum = Color[color.upper()]
        board = game.tracker.game.state.board
        m = board.map
    except Exception:  # noqa: BLE001
        return None
    robber_coord = board.robber_coordinate
    gained: dict[str, int] = {}
    blocked: dict[str, int] = {}
    tiles_touched = 0
    for coord, tile in m.land_tiles.items():
        if tile.number != number or not tile.resource:
            continue
        node_ids = set(tile.nodes.values())
        # Count self buildings on this tile. Settlement = ×1, city = ×2.
        for nid, (bcol, btype) in board.buildings.items():
            if bcol != my_enum or int(nid) not in node_ids:
                continue
            mult = 2 if str(btype).upper() == "CITY" else 1
            bucket = blocked if coord == robber_coord else gained
            bucket[tile.resource] = bucket.get(tile.resource, 0) + mult
            tiles_touched += 1
    if tiles_touched == 0:
        # Self's board has no exposure to this number — still useful to
        # report ("rolled 4 · nothing for you") so the banner is
        # informative rather than silent. Caller decides rendering.
        return {"gained": {}, "blocked": {}, "total": 0, "blocked_total": 0}
    return {
        "gained": gained,
        "blocked": blocked,
        "total": sum(gained.values()),
        "blocked_total": sum(blocked.values()),
    }


def _vp_breakdown(game, color: str) -> dict[str, int] | None:
    """Per-category VP breakdown from colonist's victoryPointsState.

    Returns ``{settle, city, vp_cards, longest_road, largest_army,
    total}`` or None when we don't have a live colonist session. Only
    works for self in the general case — VP cards are hidden for opps
    (colonist never ships key 2 for another player), so for opps the
    vp_cards slot is always 0 and total understates by their hidden
    VPs.
    """
    try:
        sess = getattr(game, "session", None)
        color_map = getattr(game, "color_map", None)
        if sess is None or color_map is None:
            return None
        username = color_map.reverse(color)
        if username is None:
            return None
        cid = None
        for c, name in sess.player_names.items():
            if name == username:
                cid = c
                break
        if cid is None:
            return None
        state = sess.victory_points_state.get(cid)
        if not state:
            return None
        # Keys: 0=settle count, 1=city count, 2=held VP cards,
        # 4=longest-road flag, 5=largest-army flag.
        settle = int(state.get(0, 0))
        city = int(state.get(1, 0))
        vp_cards = int(state.get(2, 0))
        lr = int(state.get(4, 0)) * 2
        la = int(state.get(5, 0)) * 2
        total = settle + city * 2 + vp_cards + lr + la
        return {
            "settle": settle, "city": city * 2, "vp_cards": vp_cards,
            "longest_road": lr, "largest_army": la, "total": total,
        }
    except Exception:  # noqa: BLE001
        return None


def _get_vp(game, color: str) -> int:
    """VP for `color` — prefer colonist's authoritative state.

    Colonist's victoryPointsState per color is what its UI displays
    (settles + cities + held VP cards + longest-road/largest-army
    flags). Using it directly avoids drift that would otherwise creep
    in when BuildEvents are missed on reconnect or when a knight-play
    doesn't reach our tracker. Falls back to catanatron's internal
    VICTORY_POINTS when we can't resolve the color to a colonist cid
    (e.g. ws-replay fixtures without a LiveSession).
    """
    try:
        color_map = getattr(game, "color_map", None)
        sess = getattr(game, "session", None)
        if sess is not None and color_map is not None:
            username = color_map.reverse(color)
            if username is not None:
                for cid, name in sess.player_names.items():
                    if name == username:
                        if sess.victory_points_state.get(cid):
                            return sess.vp_total(cid)
                        break
    except Exception:  # noqa: BLE001
        pass
    try:
        from catanatron import Color
        c = Color[color.upper()]
        idx = game.tracker.game.state.color_to_index.get(c)
        if idx is None:
            return 0
        return int(game.tracker.game.state.player_state.get(
            f"P{idx}_VICTORY_POINTS", 0))
    except Exception:  # noqa: BLE001
        return 0
