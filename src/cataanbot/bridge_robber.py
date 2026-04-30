"""Robber-related advisor helpers extracted from bridge.py.

Both helpers depend on the live game state (board buildings, session
player metadata) but not the bridge ``st`` dict — they read directly
off `game`. Re-exported by bridge.py for backwards compat.
"""
from __future__ import annotations

from typing import Any


def _compute_robber_snapshot(
    game, display_colors: dict[str, str] | None = None, top: int = 5,
) -> list[dict[str, Any]] | None:
    """Snapshot the top-N robber rankings for the overlay.

    Each target gets a ``suggested_victim`` color — the best single person
    to steal from when the tile has more than one adjacent opposing
    settlement/city. Scoring: card count dominates (biggest EV per steal,
    more cards = more likely to hold a needed resource), but a near-win
    opponent (VP ≥ ``mid_late_vp()``) gets boosted priority to deny them
    resources.
    """
    from cataanbot.advisor import score_robber_targets

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
    # House-rules can override via CATAANBOT_FRIENDLY_ROBBER_PROTECTED_VP.
    from cataanbot.config import get_friendly_robber_protected_vp
    fr_min = (get_friendly_robber_protected_vp()
              if sess.friendly_robber_active else None)
    try:
        scores = score_robber_targets(
            game.tracker.game, color,
            hand_size_override=hand_size_override or None,
            friendly_robber_min_vp=fr_min,
        )
    except Exception:  # noqa: BLE001
        return None
    display = display_colors or {}
    out = []
    for s in scores[:top]:
        # Pick the best victim: card count dominates (best steal EV), VP
        # pressure boosts near-winners, pip contribution is a small nudge.
        from cataanbot.config import close_to_win_vp, mid_late_vp
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

    from cataanbot.advisor import PIP_DOTS_BY_NUMBER
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
