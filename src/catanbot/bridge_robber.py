"""Robber-related advisor helpers extracted from bridge.py.

Both helpers depend on the live game state (board buildings, session
player metadata) but not the bridge ``st`` dict — they read directly
off `game`. Re-exported by bridge.py for backwards compat.
"""
from __future__ import annotations

from typing import Any


def _detect_imminent_opp_color(game) -> str | None:
    """Return the catanatron Color name of any opp who could win on
    their next turn from what we can see, or None.

    Mirrors the LR/LA branch of _compute_leader_threat without
    requiring the snap.opps[].can_afford slice — useful for callers
    that run before the snap is fully built (e.g. robber-target
    scoring on a fresh 7-roll). Conservative: only counts the
    LR / LA flips (no dev-stash heuristic, no build-affordability
    inference) since this helper is consumed by the robber-target
    weight, where over-bumping noise tiles is worse than under-
    bumping. Build-VP and dev-stash paths still flow through the
    snap-driven leader-threat detector for the banner.
    """
    try:
        from catanatron import Color  # noqa: F401
        from catanbot.config import VP_TARGET
        sess = game.session
        if sess is None or sess.self_color_id is None:
            return None
        try:
            self_user = sess.player_names.get(sess.self_color_id)
            self_color = (game.color_map.get(self_user)
                          if self_user else None)
        except Exception:  # noqa: BLE001
            self_color = None
        state = game.tracker.game.state
        ps = state.player_state
        for col, idx in state.color_to_index.items():
            col_str = (col.value if hasattr(col, "value")
                       else str(col)).upper()
            if self_color is not None and col_str == str(self_color).upper():
                continue
            vp = int(ps.get(f"P{idx}_VICTORY_POINTS", 0) or 0)
            if vp >= VP_TARGET:
                # Already at target — game is effectively over from
                # the threat-banner perspective; no point bumping
                # robber priority here.
                continue
            # LA path: +1 knight play takes LA?
            played = int(ps.get(f"P{idx}_PLAYED_KNIGHT", 0))
            held = int(ps.get(f"P{idx}_KNIGHT_IN_HAND", 0))
            has_army = bool(ps.get(f"P{idx}_HAS_ARMY", False))
            opp_max_played = 0
            for col2, idx2 in state.color_to_index.items():
                if idx2 == idx:
                    continue
                opp_max_played = max(opp_max_played, int(
                    ps.get(f"P{idx2}_PLAYED_KNIGHT", 0)))
            la_threshold = max(3, opp_max_played + 1)
            if (not has_army and held >= 1 and played + 1 >= la_threshold
                    and vp + 2 >= VP_TARGET):
                return col_str
            # LR path: +1 road takes LR?
            ll = int(ps.get(f"P{idx}_LONGEST_ROAD_LENGTH", 0))
            has_road = bool(ps.get(f"P{idx}_HAS_ROAD", False))
            opp_max_roads = 0
            for col2, idx2 in state.color_to_index.items():
                if idx2 == idx:
                    continue
                opp_max_roads = max(opp_max_roads, int(
                    ps.get(f"P{idx2}_LONGEST_ROAD_LENGTH", 0)))
            if (not has_road and ll + 1 >= max(5, opp_max_roads + 1)
                    and vp + 2 >= VP_TARGET):
                return col_str
        return None
    except Exception:  # noqa: BLE001
        return None


def _compute_robber_snapshot(
    game, display_colors: dict[str, str] | None = None, top: int = 5,
    imminent_color: str | None = None,
    needed_resources: list[str] | None = None,
) -> list[dict[str, Any]] | None:
    """Snapshot the top-N robber rankings for the overlay.

    Each target gets a ``suggested_victim`` color — the best single person
    to steal from when the tile has more than one adjacent opposing
    settlement/city. Scoring: card count dominates (biggest EV per steal,
    more cards = more likely to hold a needed resource), but a near-win
    opponent (VP ≥ ``mid_late_vp()``) gets boosted priority to deny them
    resources.
    """
    from catanbot.advisor import score_robber_targets

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
    # catanatron-color → username, so the victim pills can surface the
    # real colonist UI color for the robber ranking.
    reverse = {}
    for cid, user in sess.player_names.items():
        try:
            reverse[game.color_map.get(user)] = user
        except Exception:  # noqa: BLE001
            continue
    hand_size_override: dict[str, int] = {}
    for cid, count in sess.hand_card_counts.items():
        user = sess.player_names.get(cid)
        if not user:
            continue
        try:
            c = game.color_map.get(user)
        except Exception:  # noqa: BLE001
            continue
        hand_size_override[c] = int(count)
    # Friendly Robber threshold: when colonist announced the rule,
    # filter victims with VP ≤ the configured threshold (default 2,
    # matches colonist's behaviour — newly-placed players have 2 VP
    # from their two opening settlements; the rule protects them so
    # opps can't gang-robber a leader's first build attempt).
    # House-rules can override via CATANBOT_FRIENDLY_ROBBER_PROTECTED_VP.
    from catanbot.config import get_friendly_robber_protected_vp
    fr_min = (get_friendly_robber_protected_vp()
              if sess.friendly_robber_active else None)
    # Auto-detect imminent opp from game state when caller didn't
    # supply one — robber snapshot may be computed at any point
    # in the snap-build pipeline so we can't assume snap.threat is
    # available.
    if imminent_color is None:
        imminent_color = _detect_imminent_opp_color(game)
    # Strategy v2 P1-5: feed the resource-control inputs through.
    # opp_production_by_resource is a per-color cards-per-roll map
    # (excludes self); self_production_by_resource is self's own.
    # Without these, score_robber_targets falls back to the original
    # block-based scoring — backward compatible.
    self_prod_map: dict[str, float] = {}
    opp_prod_by_color: dict[str, dict[str, float]] = {}
    try:
        from catanbot.bridge_economy import _compute_production
        self_p = _compute_production(game, color)
        if self_p:
            self_prod_map = dict(self_p.get("by_resource") or {})
        for opp_color in reverse:
            opp_p = _compute_production(game, opp_color)
            if opp_p:
                opp_prod_by_color[opp_color] = dict(
                    opp_p.get("by_resource") or {})
    except Exception:  # noqa: BLE001
        # Production helpers can fail on early game state — fall back
        # to no resource-control inputs rather than crash the snapshot.
        self_prod_map = {}
        opp_prod_by_color = {}
    # Derive needed_resources from self's hand + closest missing build
    # when the caller didn't supply one. The robber landing on a tile
    # of a resource we owe for our next planned build is a worth-1pt
    # bump per pip — small enough to be a tiebreaker, big enough to
    # tilt against generic high-pip blocks when we're 1 ORE from a
    # city.
    if needed_resources is None:
        try:
            from catanbot.bridge_economy import _closest_missing_build
            self_hand = dict(game.tracker.hand(color))
            closest = _closest_missing_build(self_hand)
            if closest:
                needed_resources = list(
                    (closest.get("missing") or {}).keys())
        except Exception:  # noqa: BLE001
            needed_resources = None
    # Build vp_override from colonist's victoryPointsState — same
    # source the opps panel uses, so robber scoring agrees with the
    # numbers Noah sees in the HUD. _get_vp falls back to catanatron
    # when colonist data isn't available, so this is safe to apply
    # uniformly. Without this, the two paths drift on missed VP-card
    # buys and the robber scoring read 14 VP for an opp the panel
    # showed at 11 VP (Noah, 2026-05-04).
    vp_override: dict[str, int] = {}
    try:
        from catanbot.bridge_economy import _get_vp
        for opp_color in reverse:
            try:
                vp_override[opp_color] = int(_get_vp(game, opp_color))
            except Exception:  # noqa: BLE001
                continue
        # Self too, so own_blocked weighting (if it ever uses it) is
        # consistent. _vp_weight only applies to victim contributions
        # today, but no harm including self.
        try:
            vp_override[color] = int(_get_vp(game, color))
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        vp_override = {}
    try:
        scores = score_robber_targets(
            game.tracker.game, color,
            hand_size_override=hand_size_override or None,
            friendly_robber_min_vp=fr_min,
            imminent_color=imminent_color,
            needed_resources=needed_resources,
            opp_production_by_resource=opp_prod_by_color or None,
            self_production_by_resource=self_prod_map or None,
            vp_override=vp_override or None,
        )
    except Exception:  # noqa: BLE001
        return None
    display = display_colors or {}
    out = []
    for s in scores[:top]:
        # Pick the best victim: card count dominates (best steal EV), VP
        # pressure boosts near-winners, pip contribution is a small nudge.
        from catanbot.config import close_to_win_vp, mid_late_vp
        close_vp = close_to_win_vp()
        mid_vp = mid_late_vp()
        def _victim_priority(vcolor: str) -> float:
            cards = s.opponent_hand_size.get(vcolor, 0)
            vp = s.victim_vp.get(vcolor, 0)
            pips = s.victims.get(vcolor, 0)
            vp_weight = 3.0 if vp >= close_vp else (
                1.8 if vp >= mid_vp else 1.0)
            return cards * vp_weight + pips * 0.3
        suggested_color: str | None = None
        if s.victims:
            # Prefer a victim with >=1 card; all-empty-hands falls back to
            # the highest priority anyway, which is fine.
            with_cards = [
                c for c in s.victims
                if s.opponent_hand_size.get(c, 0) > 0
            ]
            pool = with_cards or list(s.victims.keys())
            suggested_color = max(pool, key=_victim_priority)
        out.append({
            "coord": list(s.coord),
            "resource": s.resource,
            "number": s.number,
            "score": round(s.score, 2),
            # Strategy v2 P1-5 — surface the resource-control bonuses
            # so the HUD can show "+1.0 we need this" or "+0.4 monopoly
            # setup" tags next to the rank, not just an opaque number.
            "resource_need_bonus": round(
                getattr(s, "resource_need_bonus", 0.0), 2),
            "monopoly_setup_bonus": round(
                getattr(s, "monopoly_setup_bonus", 0.0), 2),
            "suggested_victim": suggested_color,
            "victims": [
                {
                    "color": c,
                    "color_css": display.get(reverse.get(c, "")),
                    "username": reverse.get(c),
                    "pips": pips,
                    "vp": s.victim_vp.get(c, 0),
                    "cards": s.opponent_hand_size.get(c, 0),
                    "suggested": (c == suggested_color),
                }
                for c, pips in sorted(
                    s.victims.items(), key=lambda kv: -kv[1])
            ],
        })
    return out


def _compute_robber_on_me(game) -> dict[str, Any] | None:
    """Persistent "robber is blocking you" banner.

    Different from knight_hint: fires whenever the robber is parked on
    a self tile, regardless of whether a knight is in hand. Reports
    which tile and how many pips are being suppressed so the overlay
    can show the ongoing cost — a reminder to trade into dev cards or
    push for a knight.
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

    board = game.tracker.game.state.board
    robber = board.robber_coordinate
    if not robber:
        return None
    m = board.map
    robber_tile = m.land_tiles.get(robber)
    if robber_tile is None or not robber_tile.number:
        # Desert or uninit — robber parked here costs nothing.
        return None

    from catanbot.advisor import PIP_DOTS_BY_NUMBER
    robber_node_ids = set(robber_tile.nodes.values())
    pips = 0
    building_count = 0
    has_city = False
    for nid, (bcol, btype) in board.buildings.items():
        if bcol != my_enum or int(nid) not in robber_node_ids:
            continue
        per_building = PIP_DOTS_BY_NUMBER.get(robber_tile.number, 0)
        if str(btype).upper() == "CITY":
            per_building *= 2
            has_city = True
        pips += per_building
        building_count += 1
    if building_count == 0:
        return None
    # Probability-weighted card loss per dice roll. pips_blocked already
    # doubled cities, so dividing by 36 gives the expected cards denied
    # per roll — a figure Noah can reason about in "cards" rather than
    # translating dot-counts in his head.
    expected_per_roll = pips / 36.0
    return {
        "resource": robber_tile.resource,
        "number": robber_tile.number,
        "buildings": building_count,
        "has_city": has_city,
        "pips_blocked": pips,
        "expected_per_roll": round(expected_per_roll, 3),
    }
