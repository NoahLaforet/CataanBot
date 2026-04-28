"""Game-state evaluation + 1-ply search rescoring.

The advisor's per-kind heuristics (in ``recommender.py``) are fast and
produce nice UX candidates, but they rank actions in isolation — "this
settlement has 0.83-prod" doesn't compare to "this road unlocks a
0.9-prod settlement next turn." A real player evaluates the *state
after* the move, not the move itself.

``evaluate_state`` scores an entire game state for a given color using
linear weights over VP, production, hand quality, dev cards, pieces,
and opponent pressure. ``search_rerank`` takes the heuristic recs,
simulates each against a copied game, evaluates the resulting state,
and attaches ``search_delta`` = post_eval − pre_eval so recs can be
sorted by actual lookahead value.

1-ply depth is where catanatron's speed (0.02ms per copy+execute)
lets us afford searching every candidate in <10ms — deep enough to
catch "this dev-card buy is better than the immediate settlement
because the settlement costs too many card slots" without the
branching-factor blowup of full minimax.
"""
from __future__ import annotations

from typing import Any

from cataanbot import config

_RESOURCES = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")
_DEV_PLAYABLE = ("KNIGHT", "MONOPOLY", "YEAR_OF_PLENTY", "ROAD_BUILDING")


def evaluate_state(game, my_color) -> float:
    """Overall state strength for ``my_color``, higher = better.

    Scale is relative: positive when we're ahead, negative when an
    opponent is. Terminal states return ±1000. Mid-game values
    typically land in [-150, 150].

    Linear combination of own-score minus the strongest opponent's
    score (weighted 0.8 so not every one-turn opponent gain is read as
    catastrophic). See ``_player_score`` for the component weights.
    """
    from catanatron import Color
    c = my_color if isinstance(my_color, Color) else Color[str(my_color)]
    winner = game.winning_color()
    if winner == c:
        return 1000.0
    if winner is not None:
        return -1000.0

    state = game.state
    board = state.board
    m = board.map

    own = _player_score(state, board, m, c)
    opp_scores = [
        _player_score(state, board, m, oc)
        for oc in state.color_to_index
        if oc != c
    ]
    max_opp = max(opp_scores) if opp_scores else 0.0
    return own - 0.8 * max_opp


def _player_score(state, board, m, color) -> float:
    """Component-weighted strength for one player. Weights are a hand
    tuning: VP dominates (direct progress to win), production is the
    second biggest (future VP), dev cards + hand + pieces round it out.

    Weights anchored to a 10-VP game — `VP_TARGET`-aware scaling would
    be a refinement but the per-component weights stay proportional.
    """
    idx = state.color_to_index[color]
    ps = state.player_state

    vp = int(ps.get(f"P{idx}_ACTUAL_VICTORY_POINTS", 0))
    # Quadratic VP emphasis so the last few VPs matter disproportionately.
    # At vp=0: contribution 0. At vp=target: contribution 20*target^2.
    # Between those the closer to target, the more every VP is worth.
    score = vp * 20.0 + vp * vp * 1.5

    # Total per-turn expected production (pips × building multiplier).
    # Sum over own buildings; city doubles pips.
    prod = 0.0
    for nid, (bcol, btype) in board.buildings.items():
        if bcol != color:
            continue
        mult = 2.0 if btype == "CITY" else 1.0
        for _res, pips in m.node_production.get(int(nid), {}).items():
            prod += mult * float(pips)
    score += prod * 10.0

    # Hand: capped value (each resource up to the discard line is worth
    # a flat amount; beyond triggers the 7-roll discard penalty).
    hand_total = sum(
        int(ps.get(f"P{idx}_{r}_IN_HAND", 0)) for r in _RESOURCES
    )
    cap = config.get_discard_limit()
    score += min(hand_total, cap) * 1.5
    if hand_total > cap:
        # Discard risk — each card above the limit is half-lost-value in
        # expectation (7-roll probability × half rounded down).
        score -= (hand_total - cap) * 3.0

    # Dev cards: playable dev cards are latent action potential, VP
    # cards are direct hidden VP.
    playable_dev = sum(
        int(ps.get(f"P{idx}_{kind}_IN_HAND", 0)) for kind in _DEV_PLAYABLE
    )
    dev_vp = int(ps.get(f"P{idx}_VICTORY_POINT_IN_HAND", 0))
    # Weight VP cards like actual VP since they count toward the win
    # target the moment you hit it.
    score += playable_dev * 2.5 + dev_vp * 20.0

    # Largest-army race: each played knight is worth half a VP in
    # expectation (3 knights unlock the +2 VP, but opponents can race).
    played_knights = int(ps.get(f"P{idx}_PLAYED_KNIGHT", 0))
    score += played_knights * 1.5

    # Longest-road race: once a player hits 5 road segments they're in
    # contention. Raw length past 4 is a proxy; actual +2 VP is already
    # reflected in ACTUAL_VICTORY_POINTS so avoid double-counting.
    road_len = int(ps.get(f"P{idx}_LONGEST_ROAD_LENGTH", 0))
    if road_len >= 4:
        score += (road_len - 3) * 1.0

    # Pieces in reserve — running out forces dead turns. Minor weight.
    settles_left = int(ps.get(f"P{idx}_SETTLEMENTS_AVAILABLE", 5))
    cities_left = int(ps.get(f"P{idx}_CITIES_AVAILABLE", 4))
    roads_left = int(ps.get(f"P{idx}_ROADS_AVAILABLE", 15))
    # The first few are free; deep into the game running out is a
    # problem.
    if settles_left <= 1:
        score -= (2 - settles_left) * 2.0
    if cities_left == 0:
        score -= 3.0
    if roads_left <= 2:
        score -= (3 - roads_left) * 0.5

    return score


def _rec_to_action(rec: dict[str, Any], color) -> Any | None:
    """Map a recommender output dict to a catanatron Action instance.

    Returns None for rec kinds we don't simulate (trade/propose_trade,
    opening_settlement — openings go through a different path because
    the game state needs initial_build_phase context). Missing
    identifiers (no node_id on a settlement rec, etc.) also return
    None rather than raising.
    """
    from catanatron.models.actions import Action, ActionType
    kind = rec.get("kind")
    if kind == "settlement" and rec.get("node_id") is not None:
        return Action(color, ActionType.BUILD_SETTLEMENT, int(rec["node_id"]))
    if kind == "city" and rec.get("node_id") is not None:
        return Action(color, ActionType.BUILD_CITY, int(rec["node_id"]))
    if kind == "road" and rec.get("edge"):
        edge = rec["edge"]
        return Action(color, ActionType.BUILD_ROAD,
                      (int(edge[0]), int(edge[1])))
    if kind == "dev_card":
        return Action(color, ActionType.BUY_DEVELOPMENT_CARD, None)
    return None


def search_rerank(game, my_color, recs: list[dict[str, Any]]) -> None:
    """Annotate each rec with ``search_delta`` and reorder in place.

    For each rec that maps to a simulatable catanatron action, copies
    the game, executes the action, evaluates the resulting state, and
    records ``post_eval − pre_eval`` as ``search_delta``. Recs are
    then sorted so search-scored picks come first (best delta first),
    followed by unsearchable picks (trade/propose_trade) ordered by
    their existing heuristic score.

    Safe to call with recs from ``recommend_actions`` — any rec whose
    action can't be constructed or executed keeps ``search_delta=None``
    and falls to the tail.
    """
    from catanatron import Color
    c = my_color if isinstance(my_color, Color) else Color[str(my_color)]
    try:
        pre = evaluate_state(game, c)
    except Exception:  # noqa: BLE001
        return
    for rec in recs:
        action = _rec_to_action(rec, c)
        if action is None:
            rec["search_delta"] = None
            continue
        try:
            gc = game.copy()
            gc.execute(action)
            post = evaluate_state(gc, c)
            rec["search_delta"] = post - pre
        except Exception:  # noqa: BLE001
            # Action wasn't legal in this state (opp-inferred game
            # drift, phase mismatch, etc.) — fall back to heuristic.
            rec["search_delta"] = None

    def _sort_key(rec: dict[str, Any]) -> tuple:
        sd = rec.get("search_delta")
        when = rec.get("when", "now")
        # Buckets: (0) search-scored "now" by delta desc — the real
        # 1-ply ranking. (1) unsimulatable "now" recs (propose_trade)
        # by heuristic score. (2) "soon" plans by heuristic score.
        # This keeps actionable picks ahead of save-for-X plans even
        # when the plan's score matches a now-rec's score.
        if sd is not None:
            return (0, -float(sd))
        if when == "now":
            return (1, -float(rec.get("score", 0.0)))
        return (2, -float(rec.get("score", 0.0)))

    recs.sort(key=_sort_key)


__all__ = ["evaluate_state", "search_rerank"]
