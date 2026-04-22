"""State evaluator + 1-ply search rerank — the bot-strength layer.

These tests pin down the contract for ``eval.py``: the evaluator must
return a number that meaningfully reflects "who's winning" (terminal
winners blow out, VP dominates, production matters), and
``search_rerank`` must reorder recs so the best-post-action option
sits at index 0 — that's what makes "just do the top pick" a winning
strategy rather than a heuristic gamble.

We lean on a fresh catanatron game with deterministic builds rather
than relying on random seeds — the evaluator's behavior is what we're
testing, not the engine's dice luck.
"""
from __future__ import annotations


def _fresh_game():
    from catanatron import Color, Game, RandomPlayer

    return Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=3,
    )


def _game_with_red_road_net():
    """Fresh game + RED settlement at node 0 with an extended road net
    so buildable_node_ids returns actual candidates. Stays in the
    initial-build phase — suitable for static recommender tests but
    not for executing actions."""
    from catanatron import Color

    g = _fresh_game()
    b = g.state.board
    b.build_settlement(Color.RED, 0, initial_build_phase=True)
    b.build_road(Color.RED, (0, 1))
    b.build_road(Color.RED, (1, 2))
    b.build_road(Color.RED, (2, 3))
    return g


def _red_mid_game(resources: dict[str, int] | None = None):
    """Advance the game through the initial phase, roll RED's dice, and
    optionally inject a resource hand. Returns a game where RED is the
    current player with ``ActionPrompt.PLAY_TURN`` and execute() accepts
    real builds — the state we need to integration-test search_rerank."""
    from catanatron import Color
    from catanatron.models.actions import ActionType
    from catanatron.state import generate_playable_actions

    g = _fresh_game()
    steps = 0
    while (g.state.is_initial_build_phase
           or g.state.current_color() != Color.RED) and steps < 200:
        g.play_tick()
        steps += 1
    for a in g.state.playable_actions:
        if a.action_type == ActionType.ROLL:
            g.execute(a)
            break
    if resources:
        idx = g.state.color_to_index[Color.RED]
        for res, n in resources.items():
            g.state.player_state[f"P{idx}_{res}_IN_HAND"] = int(n)
        g.state.playable_actions = generate_playable_actions(g.state)
    return g


def test_fresh_game_evaluation_is_near_zero():
    """A brand new game with no buildings for anyone — each player scores
    0 on every component, so own - 0.8*max_opp is 0.0. Acts as the
    "zero baseline" regression for the evaluator's normalization."""
    from cataanbot.eval import evaluate_state

    g = _fresh_game()
    score = evaluate_state(g, "RED")
    assert abs(score) < 0.001, f"expected 0.0 baseline, got {score}"


def test_winner_returns_positive_sentinel():
    """If ``my_color`` is the winning_color, evaluator shortcircuits to
    +1000 — a terminal dominates everything else so downstream search
    never "forgets" a winning line. Simulated by monkeypatching."""
    from catanatron import Color
    from cataanbot.eval import evaluate_state

    g = _fresh_game()
    g.winning_color = lambda: Color.RED  # type: ignore[method-assign]
    assert evaluate_state(g, "RED") == 1000.0


def test_loser_returns_negative_sentinel():
    """If an opponent has won, evaluator returns -1000 from our seat.
    The 1000 vs -1000 spread keeps winning/losing terminals cleanly
    separated in the search's sort order."""
    from catanatron import Color
    from cataanbot.eval import evaluate_state

    g = _fresh_game()
    g.winning_color = lambda: Color.BLUE  # type: ignore[method-assign]
    assert evaluate_state(g, "RED") == -1000.0


def test_settlement_beats_nothing():
    """Placing a RED settlement on a productive corner should evaluate
    strictly higher for RED than an empty board. This is the minimum
    behavior the evaluator needs — a build improves your position."""
    from catanatron import Color
    from cataanbot.eval import evaluate_state

    g = _fresh_game()
    pre = evaluate_state(g, "RED")
    g.state.board.build_settlement(Color.RED, 0, initial_build_phase=True)
    post = evaluate_state(g, "RED")
    assert post > pre, f"settlement should raise eval: {pre} → {post}"


def test_city_beats_settlement_on_same_spot():
    """Upgrading a settlement to a city adds 1 VP (+ direct VP weight)
    and doubles production. City eval must be strictly greater than
    settlement eval at the same node — the upgrade is unambiguously
    better in every component."""
    from catanatron import Color
    from cataanbot.eval import evaluate_state

    g_settle = _fresh_game()
    g_settle.state.board.build_settlement(
        Color.RED, 0, initial_build_phase=True)
    settle_eval = evaluate_state(g_settle, "RED")

    g_city = _fresh_game()
    g_city.state.board.build_settlement(
        Color.RED, 0, initial_build_phase=True)
    g_city.state.board.build_city(Color.RED, 0)
    city_eval = evaluate_state(g_city, "RED")

    assert city_eval > settle_eval, (
        f"city should beat settlement: {settle_eval} → {city_eval}")


def test_high_pip_settlement_outscores_low_pip():
    """Same color, same piece type, but a higher-pip corner must
    evaluate above a lower-pip corner — production matters, not just
    piece count. Picks two real catanatron nodes and compares."""
    from catanatron import Color
    from cataanbot.eval import evaluate_state

    g_a = _fresh_game()
    g_b = _fresh_game()
    m = g_a.state.board.map
    # Sort land nodes by raw pip production — grab a hot one and a cold one.
    nodes = list(m.land_nodes)
    pips = {n: sum(m.node_production.get(n, {}).values()) for n in nodes}
    hot = max(nodes, key=lambda n: pips[n])
    cold = min((n for n in nodes if pips[n] > 0), key=lambda n: pips[n])
    g_a.state.board.build_settlement(
        Color.RED, hot, initial_build_phase=True)
    g_b.state.board.build_settlement(
        Color.RED, cold, initial_build_phase=True)
    assert evaluate_state(g_a, "RED") > evaluate_state(g_b, "RED")


def test_opponent_buildings_subtract_from_own_score():
    """Placing a BLUE settlement without changing RED's pieces should
    lower RED's eval (max_opp goes up, own stays flat). Mirrors the
    0.8 opp-weighting — a move that helps the opp hurts us."""
    from catanatron import Color
    from cataanbot.eval import evaluate_state

    g = _fresh_game()
    g.state.board.build_settlement(Color.RED, 0, initial_build_phase=True)
    before = evaluate_state(g, "RED")
    g.state.board.build_settlement(Color.BLUE, 10, initial_build_phase=True)
    after = evaluate_state(g, "RED")
    assert after < before, f"opp build should drop RED eval: {before} → {after}"


def test_search_rerank_empty_list_is_noop():
    """Empty recs list should sort/return cleanly — no crashes, no
    mutation surprises."""
    from cataanbot.eval import search_rerank

    g = _game_with_red_road_net()
    recs: list = []
    search_rerank(g, "RED", recs)
    assert recs == []


def test_search_rerank_attaches_delta_and_promotes_city_over_road():
    """Feed search_rerank a mixed list (city, road) on a mid-game
    state and it must annotate each with ``search_delta`` and sort
    city first — +1 VP + doubled production beats a road with no VP
    and no immediate yield. Both actions are deterministic, so the
    ordering is stable across runs (unlike dev_card's random draw)."""
    from catanatron import Color
    from catanatron.models.actions import Action, ActionType
    from cataanbot.eval import search_rerank

    g = _red_mid_game({"WOOD": 1, "BRICK": 1, "WHEAT": 2, "ORE": 3})
    red_settle = next(
        n for n, (c, bt) in g.state.board.buildings.items()
        if c == Color.RED and bt == "SETTLEMENT"
    )
    # Pick a legal road edge from the playable_actions list — don't
    # guess board topology.
    road_edge = next(
        a.value for a in g.state.playable_actions
        if a.action_type == ActionType.BUILD_ROAD
    )
    recs = [
        {"kind": "road", "when": "now", "score": 5.0, "edge": list(road_edge)},
        {"kind": "city", "when": "now", "score": 9.0,
         "node_id": int(red_settle)},
    ]
    search_rerank(g, "RED", recs)
    assert recs[0]["kind"] == "city", (
        f"city should lead: "
        f"{[(r['kind'], r.get('search_delta')) for r in recs]}")
    for r in recs:
        assert r.get("search_delta") is not None


def test_search_rerank_puts_unsimulatable_at_tail():
    """A propose_trade rec can't be simulated directly (not a catanatron
    Action) — it must end up with search_delta=None and fall below any
    real simulated move in the ordering."""
    from cataanbot.eval import search_rerank

    g = _red_mid_game({"SHEEP": 1, "WHEAT": 1, "ORE": 1})
    recs = [
        {"kind": "propose_trade", "when": "now", "score": 8.0,
         "give": {"WOOD": 1}, "get": {"SHEEP": 1}},
        {"kind": "dev_card", "when": "now", "score": 3.0},
    ]
    search_rerank(g, "RED", recs)
    # dev_card simulates cleanly → search_delta is a float and it leads.
    assert recs[0]["kind"] == "dev_card", (
        f"dev_card should lead over unsimulatable propose_trade: "
        f"{[(r['kind'], r.get('search_delta')) for r in recs]}")
    assert recs[0].get("search_delta") is not None
    assert recs[1]["kind"] == "propose_trade"
    assert recs[1].get("search_delta") is None


def test_search_rerank_sorts_soon_plans_below_now_recs():
    """A 'soon' plan (not affordable this turn) must never displace an
    affordable 'now' rec — even if its heuristic score is higher. The
    evaluator can't simulate unaffordable actions, so those fall to
    bucket 2 behind simulated bucket-0 and unsimulatable-now bucket-1."""
    from catanatron import Color
    from cataanbot.eval import search_rerank

    g = _red_mid_game({"SHEEP": 1, "WHEAT": 1, "ORE": 1})
    red_settle = next(
        n for n, (c, bt) in g.state.board.buildings.items()
        if c == Color.RED and bt == "SETTLEMENT"
    )
    recs = [
        {"kind": "city", "when": "soon", "score": 9.5,
         "node_id": int(red_settle), "missing": {"ORE": 2}},
        {"kind": "dev_card", "when": "now", "score": 3.0},
    ]
    search_rerank(g, "RED", recs)
    # 'soon' city fails execute() (no ORE in hand) → search_delta=None
    # → bucket 2. 'now' dev_card simulates → bucket 0. Dev card leads.
    assert recs[0]["kind"] == "dev_card", (
        f"now should beat soon: {[(r['kind'], r.get('when')) for r in recs]}")


def test_recommend_actions_still_obeys_1_to_10_score_range():
    """Regression: wiring search_rerank in must not break the 1-10
    score contract with the UI — search_delta is a separate field, not
    a replacement for score."""
    from cataanbot.recommender import recommend_actions

    g = _game_with_red_road_net()
    hand = {"WOOD": 2, "BRICK": 2, "SHEEP": 2, "WHEAT": 3, "ORE": 3}
    out = recommend_actions(g, "RED", hand, top=10)
    for r in out:
        assert 1.0 <= float(r["score"]) <= 10.0, r


def test_recommend_actions_top_pick_has_search_delta():
    """Once wired, the top pick for an affordable hand on a real
    mid-game state should carry a ``search_delta`` float — that's the
    signal that the 1-ply rerank actually engaged. Propose-trade
    fallbacks may land at the tail without it, but at least one
    now-rec must have it."""
    from cataanbot.recommender import recommend_actions

    g = _red_mid_game({"WHEAT": 3, "ORE": 3})
    out = recommend_actions(g, "RED", {"WHEAT": 3, "ORE": 3}, top=6)
    assert out, "expected at least one rec"
    has_delta = any(r.get("search_delta") is not None for r in out)
    assert has_delta, f"at least one rec should be search-scored: {out}"
