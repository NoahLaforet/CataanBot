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

from catanbot import config

_RESOURCES = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")
_DEV_PLAYABLE = ("KNIGHT", "MONOPOLY", "YEAR_OF_PLENTY", "ROAD_BUILDING")

# Component weights for _player_score. Pulled out of the function body so
# they can be measured + tuned via self-play (scripts/eval_player.py).
# Defaults are the original hand-tuned values — changing one here changes
# both the live HUD's search_rerank and the self-play player, so only
# adjust against a measured win-rate improvement on fixed-seed games.
EVAL_WEIGHTS: dict[str, float] = {
    "vp_linear": 20.0,    # per-VP linear term
    "vp_quad": 1.5,       # per-VP^2 (endgame emphasis)
    "prod": 10.0,         # per expected pip/roll
    "hand": 1.5,          # per card up to discard cap
    "hand_over_cap": 3.0, # penalty per card above cap
    "dev": 2.5,           # per playable dev card
    "knight": 1.5,        # per played knight (largest-army race)
    "road_past3": 1.0,    # per road segment beyond 3 (longest-road race)
    "gold_premium": 1.25, # wildcard multiplier on gold-hex yield (Volcano)
    "robber_aware": 0.0,  # discount on the robber-blocked tile's production
                          # (0 = shipped/blind; 1 = full, correct discount).
                          # Off by default until the arena proves it; see
                          # scripts/arena.py and the robber-awareness study.
}

# Dice-number → pip count (ways to roll it out of 36). Used to value the
# gold/volcano hex, whose number is kept off the catanatron tile (so
# yield_resources can't choke on a None-resource numbered tile) and lives
# on cat_map.gold_number instead.
_PIP_BY_NUMBER = {2: 1, 12: 1, 3: 2, 11: 2, 4: 3, 10: 3, 5: 4, 9: 4, 6: 5, 8: 5}


def evaluate_state(game, my_color, weights: dict[str, float] | None = None) -> float:
    """Overall state strength for ``my_color``, higher = better.

    Scale is relative: positive when we're ahead, negative when an
    opponent is. Terminal states return ±1000. Mid-game values
    typically land in [-150, 150].

    Linear combination of own-score minus the strongest opponent's
    score (weighted 0.8 so not every one-turn opponent gain is read as
    catastrophic). See ``_player_score`` for the component weights.

    ``weights`` overrides the module-global ``EVAL_WEIGHTS`` for this
    call only (defaults to it). This lets two weight-sets coexist in one
    game — a candidate seat scored with its own weights while the rest
    use the champion's — which is what fixed-seed head-to-head tuning
    (scripts/tune_selfplay.py) needs. None preserves the shipped HUD
    behaviour exactly.
    """
    from catanatron import Color
    w = weights if weights is not None else EVAL_WEIGHTS
    c = my_color if isinstance(my_color, Color) else Color[str(my_color)]
    winner = game.winning_color()
    if winner == c:
        return 1000.0
    if winner is not None:
        return -1000.0

    state = game.state
    board = state.board
    m = board.map

    own = _player_score(state, board, m, c, w)
    opp_scores = [
        _player_score(state, board, m, oc, w)
        for oc in state.color_to_index
        if oc != c
    ]
    max_opp = max(opp_scores) if opp_scores else 0.0
    return own - 0.8 * max_opp


def _player_score(state, board, m, color, w: dict[str, float] = EVAL_WEIGHTS) -> float:
    """Component-weighted strength for one player. Weights are a hand
    tuning: VP dominates (direct progress to win), production is the
    second biggest (future VP), dev cards + hand + pieces round it out.

    Weights anchored to a 10-VP game — `VP_TARGET`-aware scaling would
    be a refinement but the per-component weights stay proportional.
    """
    idx = state.color_to_index[color]
    ps = state.player_state

    # Read public VP + hidden VP cards separately because the colonist→
    # catanatron tracker only maintains ``VICTORY_POINTS`` (public:
    # buildings + LR + LA); ``ACTUAL_VICTORY_POINTS`` is never updated
    # in tracker-driven games and stays 0, which silently breaks every
    # eval that relied on it.
    vp_public = int(ps.get(f"P{idx}_VICTORY_POINTS", 0))
    hidden_vp = int(ps.get(f"P{idx}_VICTORY_POINT_IN_HAND", 0))
    vp = vp_public + hidden_vp
    # Quadratic VP emphasis so the last few VPs matter disproportionately.
    # At vp=0: contribution 0. At vp=target: contribution 20*target^2.
    # Between those the closer to target, the more every VP is worth.
    score = vp * w["vp_linear"] + vp * vp * w["vp_quad"]

    # Total per-turn expected production (pips × building multiplier).
    # Sum over own buildings; city doubles pips.
    prod = 0.0
    for nid, (bcol, btype) in board.buildings.items():
        if bcol != color:
            continue
        mult = 2.0 if btype == "CITY" else 1.0
        for _res, pips in m.node_production.get(int(nid), {}).items():
            prod += mult * float(pips)
    # Gold/volcano hex (Volcano map): it's built as a non-producing tile
    # in catanatron (no gold resource), so node_production credits it 0.
    # Add its yield by hand for buildings on a gold node — a wildcard, so
    # weighted just above a fixed resource. Without this, search_rerank
    # undervalues building/upgrading on the gold hex mid-game, the same
    # blind spot the opening scorer fixes via gold_node_ids.
    gold_nodes = getattr(m, "gold_node_ids", frozenset())
    gold_number = getattr(m, "gold_number", None)
    if gold_nodes and gold_number:
        gold_pips = _PIP_BY_NUMBER.get(int(gold_number), 0) / 36.0
        if gold_pips:
            for nid, (bcol, btype) in board.buildings.items():
                if bcol == color and int(nid) in gold_nodes:
                    mult = 2.0 if btype == "CITY" else 1.0
                    prod += mult * gold_pips * w["gold_premium"]
    # Robber awareness: the production sum above credits EVERY owned tile,
    # but the tile under the robber yields nothing. The shipped eval is blind
    # to this — when an opponent (a hunter especially) parks the robber on our
    # best tile, the eval still scores us as if it produced, so the bot never
    # learns to avoid robber-exposed lines or to value robbing the leader
    # back. Subtract the robber-blocked tile's contribution. Symmetric:
    # applied to every color, it makes the eval value the robber as both
    # defense and offense. Gated by w["robber_aware"] (default 0.0 = the
    # original behaviour) until measured; 1.0 = full, correct discount.
    robber_aware = w.get("robber_aware", 0.0)
    if robber_aware:
        rc = getattr(board, "robber_coordinate", None)
        rt = m.land_tiles.get(rc) if rc is not None else None
        if rt is not None and rt.number is not None:
            frac = _PIP_BY_NUMBER.get(int(rt.number), 0) / 36.0
            if frac:
                blocked = 0.0
                for nid in rt.nodes.values():
                    owner = board.buildings.get(nid)
                    if owner and owner[0] == color:
                        blocked += 2.0 if owner[1] == "CITY" else 1.0
                prod -= robber_aware * frac * blocked
    score += prod * w["prod"]

    # Hand: capped value (each resource up to the discard line is worth
    # a flat amount; beyond triggers the 7-roll discard penalty).
    hand_total = sum(
        int(ps.get(f"P{idx}_{r}_IN_HAND", 0)) for r in _RESOURCES
    )
    cap = config.get_discard_limit()
    score += min(hand_total, cap) * w["hand"]
    if hand_total > cap:
        # Discard risk — each card above the limit is half-lost-value in
        # expectation (7-roll probability × half rounded down).
        score -= (hand_total - cap) * w["hand_over_cap"]

    # Dev cards: playable dev cards are latent action potential. VP
    # cards already count via ``hidden_vp`` above (linear + quadratic).
    playable_dev = sum(
        int(ps.get(f"P{idx}_{kind}_IN_HAND", 0)) for kind in _DEV_PLAYABLE
    )
    score += playable_dev * w["dev"]

    # Largest-army race: each played knight is worth half a VP in
    # expectation (3 knights unlock the +2 VP, but opponents can race).
    played_knights = int(ps.get(f"P{idx}_PLAYED_KNIGHT", 0))
    score += played_knights * w["knight"]

    # Longest-road race: once a player hits 5 road segments they're in
    # contention. Raw length past 4 is a proxy; the actual +2 VP for
    # holding the card is already reflected in ``vp_public`` (tracker
    # awards HAS_ROAD which feeds VICTORY_POINTS) so avoid double-count.
    road_len = int(ps.get(f"P{idx}_LONGEST_ROAD_LENGTH", 0))
    if road_len >= 4:
        score += (road_len - 3) * w["road_past3"]

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
    # Deliberately do NOT simulate a dev-card buy. Executing
    # BUY_DEVELOPMENT_CARD on the game copy draws the actual next card
    # from the deterministic, seed-fixed deck, and evaluate_state then
    # credits that specific card (a drawn VP card adds ~+21 pts), which
    # let a blind dev buy out-rank real builds in the 1-ply search. Leave
    # it unsearched (search_delta stays None) so it keeps its heuristic
    # score and ranks against roads, not against a peeked-at deck.
    return None


def _force_our_play_turn(state, c) -> None:
    """Pin a copied state to "our color's regular play turn" so
    ``state.execute(action)`` won't reject our recs as out-of-turn.

    The colonist→catanatron tracker mutates buildings and hands directly
    but never advances the turn/phase machinery — ``current_player_index``
    and ``is_initial_build_phase`` stay frozen at game-start values. Without
    this normalization, ``playable_actions`` is locked to whatever the
    state was before ``start_from_game_state`` returned (typically RED's
    initial-settlement set), and every simulation in ``search_rerank``
    raises ``ValueError("not in playable actions")`` — which silently
    drops every rec to the heuristic-only tail bucket.

    Side-effect: regenerates ``state.playable_actions`` for our color
    in PLAY_TURN.
    """
    from catanatron.models.enums import ActionPrompt
    from catanatron.state import generate_playable_actions
    state.is_initial_build_phase = False
    state.is_discarding = False
    state.is_moving_knight = False
    state.is_road_building = False
    state.free_roads_available = 0
    idx = state.color_to_index[c]
    state.current_player_index = idx
    state.current_turn_index = idx
    state.current_prompt = ActionPrompt.PLAY_TURN
    # Mark our color as already-rolled so playable_actions exposes the
    # full build menu instead of just ROLL.
    state.player_state[f"P{idx}_HAS_ROLLED"] = True
    state.playable_actions = generate_playable_actions(state)


_RERANK_RESOURCES = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")


def search_rerank(
    game, my_color, recs: list[dict[str, Any]],
    hand: dict[str, int] | None = None,
) -> None:
    """Annotate each rec with ``search_delta`` and reorder in place.

    For each rec that maps to a simulatable catanatron action, copies
    the game, normalizes the state to our regular play turn, executes
    the action, evaluates the resulting state, and records
    ``post_eval − pre_eval`` as ``search_delta``. Recs are then sorted
    so search-scored picks come first (best delta first), followed by
    unsearchable picks (trade/propose_trade) ordered by their existing
    heuristic score.

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
            _force_our_play_turn(gc.state, c)
            # Use the live (colonist-derived) hand the recommender saw, not
            # the tracker's player_state hand, which can under-count ore /
            # wheat after steals or inference drift. Without this, an
            # affordable city failed to simulate (search_delta=None ->
            # tail) while a cheaper road simulated (a real float -> top),
            # so a road outranked a buildable city. (bug: road over city)
            if hand is not None:
                gidx = gc.state.color_to_index[c]
                for r in _RERANK_RESOURCES:
                    gc.state.player_state[f"P{gidx}_{r}_IN_HAND"] = int(
                        hand.get(r, 0))
                from catanatron.state import generate_playable_actions
                gc.state.playable_actions = generate_playable_actions(gc.state)
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
