"""Race / endgame VP computers extracted from bridge.py.

These functions read the live game state directly (board, player_state)
and don't touch the bridge `st` dict. Re-exported by bridge.py for
backwards compat.
"""
from __future__ import annotations

from typing import Any


def _compute_longest_road_race(
    game, self_color: str | None,
) -> dict[str, Any] | None:
    """Flag a longest-road race when either side is 1 segment away.

    Returns a banner dict (level + message) or None. Levels:
        * "self_push" — self is 1 road away from qualifying (5 segs)
        * "opp_threat" — an opp is 1 road away from qualifying
        * "contested" — both sides are within 1 of current holder
    The banner is noise-free once the race is settled (holder is 2+
    ahead of everyone). We deliberately don't alert on "self just
    won longest road" because the VP banner already handles that.
    """
    from catanatron import Color

    state = game.tracker.game.state
    if self_color is None:
        return None
    try:
        my_enum = Color[self_color.upper()]
    except Exception:  # noqa: BLE001
        return None

    # Build (color, length, has_road) per seated player.
    lengths: list[tuple[object, int, bool]] = []
    for col, idx in state.color_to_index.items():
        length = int(state.player_state.get(
            f"P{idx}_LONGEST_ROAD_LENGTH", 0))
        has_road = bool(state.player_state.get(
            f"P{idx}_HAS_ROAD", False))
        lengths.append((col, length, has_road))
    if not lengths:
        return None

    self_entry = next((e for e in lengths if e[0] == my_enum), None)
    opps = [e for e in lengths if e[0] != my_enum]
    if self_entry is None:
        return None
    self_len = self_entry[1]
    self_has = self_entry[2]
    # Name the leading opp (by length) so messages say "alice" not "opp".
    # Ties broken by whoever currently holds the title, then by iteration
    # order — same across calls so the banner doesn't flip-flop.
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

    # Nobody's close yet — don't spam early game.
    if self_len < 4 and opp_max < 4:
        return None

    # Already held + lead by 2+: race is over, no alert.
    if self_has and self_len >= opp_max + 2:
        return None
    if opp_holder and opp_max >= self_len + 2:
        return None

    # Contested first (most specific): both sides ≥4 and within 1.
    # Keeps the contested banner from being drowned out by the plain
    # opp_threat path when we're neck-and-neck at 4.
    if self_len >= 4 and opp_max >= 4 and abs(self_len - opp_max) <= 1:
        if self_has:
            holder = "you"
        elif holder_name:
            holder = holder_name
        else:
            holder = "nobody"
        return {
            "level": "contested",
            "self_len": self_len,
            "opp_len": opp_max,
            "opp_username": top_opp_name,
            "holder_username": holder_name,
            "message": (
                f"LR · you {self_len} / "
                f"{top_opp_name} {opp_max} · {holder} holds"),
        }
    # Self pushing: we're on 4+, nobody else is close.
    if self_len >= 4 and not self_has and opp_max < self_len:
        return {
            "level": "self_push",
            "self_len": self_len,
            "opp_len": opp_max,
            "opp_username": top_opp_name,
            "message": f"1 road → LR ({self_len})",
        }
    # Opp threat: someone else is on 4+ and ahead of us.
    if opp_max >= 4 and opp_max >= self_len and not self_has:
        gap = opp_max - self_len
        if opp_holder:
            msg = (
                f"{holder_name or top_opp_name} has LR"
                f" · {opp_max} (+{gap})"
            )
        else:
            msg = f"{top_opp_name} 1 → LR ({opp_max})"
        return {
            "level": "opp_threat",
            "self_len": self_len,
            "opp_len": opp_max,
            "opp_username": top_opp_name,
            "holder_username": holder_name,
            "message": msg,
        }
    return None


def _compute_leader_threat(snap: dict[str, Any]) -> dict[str, Any] | None:
    """Flag the highest-VP opp and label the urgency.

    Returns a dict or None when nobody's ahead enough to warrant a
    banner. Close-to-win and mid-late thresholds track config so the
    warning scales with the game's VP_TARGET (default 10 → 8 = close).

    Enrichment — ``threat_vector`` lists *how* the leader could close
    the gap right now: an affordable VP-granting build ("city"/
    "settlement") bumps urgency because VP can be claimed this turn,
    and unplayed dev cards flag hidden-VP risk (hidden VP cards count
    toward the win total the moment their total hits target). These
    convert the banner from "watch the leader" to "the leader can
    actually end it now" — which is a different decision for Noah.
    """
    from cataanbot.config import close_to_win_vp, mid_late_vp, VP_TARGET
    opps = snap.get("opps") or []
    if not opps:
        return None
    leader = max(opps, key=lambda o: o.get("vp", 0))
    leader_vp = int(leader.get("vp", 0))
    if leader_vp < mid_late_vp():
        return None
    self_snap = snap.get("self") or {}
    my_vp = int(self_snap.get("vp", 0))
    close_vp = close_to_win_vp()
    gap_to_win = max(0, VP_TARGET - leader_vp)

    # Threat vector: what tools does the leader have right now?
    # 'vp_build' = can afford city or settlement (+1 VP next turn).
    # 'dev_vp' = holds dev cards, any of which could be a hidden VP.
    vector: list[str] = []
    can_afford = leader.get("can_afford") or []
    vp_builds = [b for b in can_afford if b in ("city", "settlement")]
    if vp_builds:
        vector.append("vp_build")
    # Dev cards are only urgent when leader is genuinely close — at
    # mid_late VP they might be knights, and a knight is less scary
    # than a hidden VP at 9 VP.
    dev_cards = int(leader.get("dev_cards", 0) or 0)
    if dev_cards > 0 and leader_vp >= close_vp:
        vector.append("dev_vp")

    # Max VP the leader could plausibly add THIS TURN from what we
    # can see. Cities (+2) outrank settlements (+1); ties broken by
    # vp_build availability. Hidden VP cards stack additively but
    # only when there are enough unplayed dev cards to plausibly
    # contain them — and even then, conservative: at most +1 from
    # the stash because we can't know how many of N cards are VPs.
    max_immediate_vp = 0
    if "city" in vp_builds:
        max_immediate_vp += 2
    elif "settlement" in vp_builds:
        max_immediate_vp += 1
    if leader.get("dev_stash_risk"):
        max_immediate_vp += 1

    # Level maps to overlay styling: "win" is effectively over,
    # "imminent" = leader could close on their NEXT turn (vp + visible
    # +VP path ≥ target — the loudest pre-game-over alarm), "close" =
    # one build from winning, "mid" = worth noticing but not yet
    # urgent. A leader at "mid" with a VP-build in hand gets bumped to
    # "close" — they can actually close faster than their VP suggests.
    if leader_vp >= VP_TARGET:
        level = "win"
    elif leader_vp + max_immediate_vp >= VP_TARGET:
        level = "imminent"
    elif leader_vp >= close_vp:
        level = "close"
    elif vp_builds and leader_vp >= close_vp - 1:
        level = "close"
    else:
        level = "mid"
    gap = leader_vp - my_vp

    # Build a means-tag for the message. Order: vp_build first (most
    # concrete), dev_vp second. Empty string when no vector present.
    means_parts = []
    if "vp_build" in vector:
        means_parts.append(f"can {'/'.join(vp_builds)}")
    if "dev_vp" in vector:
        means_parts.append(f"{dev_cards} dev")
    means = f" ({', '.join(means_parts)})" if means_parts else ""

    if level == "imminent":
        msg = (f"{leader.get('username')} can WIN NEXT TURN — "
               f"{leader_vp} VP{means}")
    elif level == "close":
        msg = (f"{leader.get('username')} at {leader_vp} VP — "
               f"one build away{means}")
    elif level == "win":
        msg = f"{leader.get('username')} at {leader_vp} VP — game over"
    else:
        msg = f"{leader.get('username')} leads at {leader_vp} VP{means}"
    return {
        "leader_username": leader.get("username"),
        "leader_color": leader.get("color"),
        "leader_color_css": leader.get("color_css"),
        "leader_vp": leader_vp,
        "my_vp": my_vp,
        "gap": gap,
        "gap_to_win": gap_to_win,
        "threat_vector": vector,
        "level": level,
        "message": msg,
    }


def _compute_win_proximity(
    snap: dict[str, Any], dev_cards_held: int = 0,
) -> dict[str, Any] | None:
    """Self-side mirror of ``_compute_leader_threat``.

    Fires when self hits ``close_to_win_vp()`` so Noah snaps into close-
    out mode: the marginal value of a VP build leaps, bank/port trades
    that unlock one become worth lopsided ratios, and any unplayed dev
    card might already be a hidden VP that closes the game the instant
    total hits target. Returns None when self is still building up —
    banner stays out of the way until it's decision-shifting.

    Levels:
      * ``win``    — self VP >= target (game effectively over).
      * ``close-1`` — 1 VP from winning. Every decision should close.
      * ``close``  — 2 VP from winning. Start pruning non-VP spending.

    ``dev_cards_held`` is accepted as a parameter instead of fished out
    of snap because the dev count lives on the session, not the snap
    payload — callers pass it through from the session.
    """
    from cataanbot.config import close_to_win_vp, VP_TARGET
    self_snap = snap.get("self") or {}
    vp = int(self_snap.get("vp", 0) or 0)
    close_vp = close_to_win_vp()
    if vp < close_vp:
        return None
    gap_to_win = max(0, VP_TARGET - vp)
    afford = self_snap.get("afford") or []
    # Only city + settlement flip VP same-turn. Road/dev-card don't.
    vp_builds = [b for b in afford if b in ("city", "settlement")]
    if vp >= VP_TARGET:
        level = "win"
    elif gap_to_win == 1:
        level = "close-1"
    else:
        level = "close"
    if level == "win":
        msg = f"you reached {vp} VP — game over"
    elif level == "close-1":
        if vp_builds:
            msg = f"1 VP to win — {'/'.join(vp_builds)} ready"
        elif dev_cards_held > 0:
            msg = f"1 VP to win — {dev_cards_held} dev in hand"
        else:
            msg = "1 VP to win"
    else:
        if vp_builds:
            msg = (f"{gap_to_win} VP to win — "
                   f"{'/'.join(vp_builds)} ready")
        else:
            msg = f"{gap_to_win} VP to win"
    return {
        "vp": vp,
        "gap_to_win": gap_to_win,
        "vp_builds_affordable": vp_builds,
        "dev_cards_held": int(dev_cards_held),
        "level": level,
        "message": msg,
    }


def _compute_winning_move(
    game, self_color, hand: dict[str, int], snap: dict[str, Any],
) -> dict[str, Any] | None:
    """Detect when a single immediate action reaches VP_TARGET.

    Fires only when self is exactly 1 or 2 VP short and a concrete
    same-turn action closes the gap:

    * **+1 VP (settle / city)** — affordable and a legal spot exists.
    * **+2 VP (road → LR)** — self is 1 segment shy of qualifying (5
      segs minimum, strictly more than any opp), holds road cost, and
      has at least one buildable edge.
    * **+2 VP (knight → LA)** — self is 1 played knight shy of
      qualifying (3 min, strictly more than any opp), holds a KNIGHT
      in hand, not yet played this turn.

    Returns the highest-confidence option (single-build wins preferred
    over conditional LR/LA flips) or ``None`` when no winning move is
    reachable. Game_plan/win_proximity stay responsible for multi-step
    narrative; this is the **"press the button now"** banner.
    """
    from cataanbot.config import VP_TARGET
    from cataanbot.recommender import (
        _SETTLEMENT_COST, _CITY_COST, _ROAD_COST,
        _hand_can_afford,
    )
    from catanatron import Color

    self_snap = snap.get("self") or {}
    vp = int(self_snap.get("vp", 0) or 0)
    gap = VP_TARGET - vp
    if gap > 2:
        return None

    try:
        my_enum = (self_color if isinstance(self_color, Color)
                   else Color[str(self_color).upper()])
    except Exception:  # noqa: BLE001
        return None
    try:
        state = game.tracker.game.state
        board = state.board
    except Exception:  # noqa: BLE001
        return None
    my_idx = state.color_to_index.get(my_enum)
    if my_idx is None:
        return None

    # Already-won path: vp already ≥ target AND held VP cards account
    # for the difference (i.e. our visible VP is short, but the cards
    # in hand close the gap on reveal). Fires only on self's turn —
    # off-turn this just adds noise. This is the case Noah lost on
    # 2026-05-03 vs Plunder101: 8 visible + 2 VP cards = 10 effective,
    # but he never claimed before Plunder hit 10 first.
    if gap <= 0:
        vp_held = int(snap.get("dev_cards_vp_held") or 0)
        if vp_held > 0 and snap.get("my_turn"):
            return {
                "kind": "claim_with_vp_cards",
                "vp": vp,
                "vp_after": vp,
                "confidence": "high",
                "detail": (f"VP cards in hand bring you to {vp} — "
                           f"play any move to claim"),
                "message": "WIN THIS TURN",
                "alternatives": [],
            }
        return None
    ps = state.player_state
    # Setup phase is self-filtering via the gap check above: VP=0-2 in
    # setup means gap=8-10, always >2 and already rejected. No extra
    # SETTLEMENTS_AVAILABLE guard — LiveGame doesn't track that key.

    candidates: list[dict[str, Any]] = []

    # +1 VP path: affordable settlement on a legal spot.
    if gap == 1 and _hand_can_afford(hand, _SETTLEMENT_COST):
        try:
            spots = list(board.buildable_node_ids(my_enum))
        except Exception:  # noqa: BLE001
            spots = []
        if spots:
            candidates.append({
                "kind": "settle",
                "confidence": "high",
                "vp_after": vp + 1,
                "detail": "settle now — +1 VP",
            })

    # +1 VP path: city upgrade on an existing self settlement.
    if gap == 1 and _hand_can_afford(hand, _CITY_COST):
        own_settles = [
            int(nid) for nid, (col, bt) in board.buildings.items()
            if col == my_enum and str(bt).upper() == "SETTLEMENT"
        ]
        if own_settles:
            candidates.append({
                "kind": "city",
                "confidence": "high",
                "vp_after": vp + 1,
                "detail": "upgrade to city — +1 VP",
            })

    # +2 VP path: road that flips longest road.
    if gap == 2 and _hand_can_afford(hand, _ROAD_COST):
        self_len = int(ps.get(f"P{my_idx}_LONGEST_ROAD_LENGTH", 0))
        self_has_lr = bool(ps.get(f"P{my_idx}_HAS_ROAD", False))
        opp_max = 0
        for col, idx in state.color_to_index.items():
            if col == my_enum:
                continue
            ol = int(ps.get(f"P{idx}_LONGEST_ROAD_LENGTH", 0))
            if ol > opp_max:
                opp_max = ol
        # +1 road must qualify us (>= 5) and strictly beat opp max.
        qualifies = self_len + 1 >= max(5, opp_max + 1)
        if qualifies and not self_has_lr:
            try:
                edges = list(board.buildable_edges(my_enum))
            except Exception:  # noqa: BLE001
                edges = []
            if edges:
                # Confidence "medium": +1 road usually extends the LR
                # chain when we're already leading, but a branch off the
                # tail won't grow it. Noah can eyeball placement — we'd
                # need a full LR recompute to be certain.
                candidates.append({
                    "kind": "road_to_lr",
                    "confidence": "medium",
                    "vp_after": vp + 2,
                    "detail": (f"+1 road on your {self_len}-chain → "
                               "LR (+2 VP)"),
                })

    # +2 VP path: knight play that flips largest army.
    if gap == 2:
        knights_played = int(ps.get(f"P{my_idx}_PLAYED_KNIGHT", 0))
        knights_in_hand = int(ps.get(f"P{my_idx}_KNIGHT_IN_HAND", 0))
        played_this_turn = bool(ps.get(
            f"P{my_idx}_HAS_PLAYED_DEVELOPMENT_CARD_IN_TURN", False))
        self_has_la = bool(ps.get(f"P{my_idx}_HAS_ARMY", False))
        opp_knights_max = 0
        for col, idx in state.color_to_index.items():
            if col == my_enum:
                continue
            ok = int(ps.get(f"P{idx}_PLAYED_KNIGHT", 0))
            if ok > opp_knights_max:
                opp_knights_max = ok
        la_threshold = max(3, opp_knights_max + 1)
        qualifies = knights_played + 1 >= la_threshold
        if (qualifies and not self_has_la
                and knights_in_hand >= 1 and not played_this_turn):
            candidates.append({
                "kind": "knight_to_la",
                "confidence": "high",
                "vp_after": vp + 2,
                "detail": (f"play Knight ({knights_played+1}/"
                           f"{la_threshold}) → LA (+2 VP)"),
            })

    if not candidates:
        return None

    # High confidence first (direct +1 builds, knight play), then road.
    conf_rank = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: conf_rank.get(c["confidence"], 9))
    top = candidates[0]
    return {
        "kind": top["kind"],
        "vp": vp,
        "vp_after": top["vp_after"],
        "confidence": top["confidence"],
        "detail": top["detail"],
        "alternatives": [
            {"kind": c["kind"], "detail": c["detail"]}
            for c in candidates[1:]
        ],
        "message": "WIN THIS TURN — " + top["detail"],
    }
