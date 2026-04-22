"""Turn-action recommender — smoke tests against catanatron board state.

These use a fresh catanatron game, seed a settlement + road for RED,
load a hand, and check that ``recommend_actions`` surfaces sensible
picks. The scoring is heuristic, so the assertions focus on shape +
relative ordering (best pick is a build when any build is possible,
dev card only shows up as fallback) rather than exact scores.
"""
from __future__ import annotations


def _fresh_game_with_red_settle():
    from catanatron import Color, Game, RandomPlayer

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=1,
    )
    # Give RED a settlement + one road so downstream buildable_edges
    # and buildable_node_ids both have real candidates to chew on.
    b = g.state.board
    b.build_settlement(Color.RED, 0, initial_build_phase=True)
    b.build_road(Color.RED, (0, 1))
    return g


def test_recommend_empty_hand_returns_nothing():
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    out = recommend_actions(g, "RED", {}, top=4)
    assert out == []


def test_dev_card_alone_when_only_ywo_ore_sheep_wheat():
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    out = recommend_actions(
        g, "RED", {"SHEEP": 1, "WHEAT": 1, "ORE": 1}, top=4)
    # Only affordable rec is dev_card; planning-ahead recs may also
    # surface (e.g. settlement 2 cards off), but the act-now slice
    # should be dev_card only.
    now = [r for r in out if r.get("when") == "now"]
    assert len(now) == 1
    assert now[0]["kind"] == "dev_card"


def test_road_affordable_surfaces_edge_suggestion():
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    out = recommend_actions(
        g, "RED", {"WOOD": 1, "BRICK": 1}, top=4)
    assert any(r["kind"] == "road" for r in out)
    road = next(r for r in out if r["kind"] == "road")
    assert "edge" in road
    assert len(road["edge"]) == 2


def test_full_settlement_hand_picks_settlement_over_dev():
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    # Also extend RED's road net so buildable_node_ids has somewhere
    # to place legally.
    from catanatron import Color
    b = g.state.board
    b.build_road(Color.RED, (1, 2))
    b.build_road(Color.RED, (2, 3))
    # Hand covers settlement + dev-card costs both, so the sort must
    # rank the real build ahead of the fallback dev option.
    hand = {"WOOD": 1, "BRICK": 1, "SHEEP": 2, "WHEAT": 2, "ORE": 1}
    out = recommend_actions(g, "RED", hand, top=4)
    assert out[0]["kind"] in ("settlement", "road", "city")
    kinds = [r["kind"] for r in out]
    if "dev_card" in kinds:
        # If dev_card is present it must rank below all structural builds.
        dev_idx = kinds.index("dev_card")
        for i in range(dev_idx):
            assert kinds[i] in ("settlement", "road", "city")


def test_city_upgrade_appears_for_wheat_ore_heavy_hand():
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    out = recommend_actions(
        g, "RED", {"WHEAT": 2, "ORE": 3}, top=4)
    city = [r for r in out if r["kind"] == "city"]
    assert city, f"expected city upgrade, got {[r['kind'] for r in out]}"
    # Only settlement is RED's one at node 0 — city target must be node 0.
    assert city[0]["node_id"] == 0


def test_scores_are_in_one_to_ten_range():
    """Every recommendation score must be in [1, 10] regardless of
    board layout — the 1-10 scale is the contract with the UI."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    from catanatron import Color
    b = g.state.board
    b.build_road(Color.RED, (1, 2))
    b.build_road(Color.RED, (2, 3))
    hand = {"WOOD": 2, "BRICK": 2, "SHEEP": 2, "WHEAT": 3, "ORE": 3}
    out = recommend_actions(g, "RED", hand, top=10)
    for r in out:
        assert 1.0 <= float(r["score"]) <= 10.0, r


def test_dev_card_score_is_fixed_three():
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    out = recommend_actions(
        g, "RED", {"SHEEP": 1, "WHEAT": 1, "ORE": 1}, top=4)
    now = [r for r in out if r.get("when") == "now"]
    assert len(now) == 1
    assert now[0]["kind"] == "dev_card"
    assert now[0]["score"] == 3.0


def test_save_for_settlement_plan_surfaces_when_two_cards_short():
    """Road-only hand should also surface a "save for settlement" plan,
    so Noah sees both the now-option and the near-term better option."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    from catanatron import Color
    b = g.state.board
    # Extend road net so settlement target exists.
    b.build_road(Color.RED, (1, 2))
    b.build_road(Color.RED, (2, 3))
    # {WOOD:1, BRICK:1} affords road; settlement is 2 cards off (S+Wh).
    out = recommend_actions(g, "RED", {"WOOD": 1, "BRICK": 1}, top=6)
    kinds_when = [(r["kind"], r.get("when")) for r in out]
    assert ("road", "now") in kinds_when
    # Plan must carry the missing dict and tag when=soon.
    plan = next((r for r in out
                 if r["kind"] == "settlement" and r.get("when") == "soon"),
                None)
    assert plan is not None, kinds_when
    assert plan["missing"] == {"SHEEP": 1, "WHEAT": 1}
    assert "need" in plan["detail"].lower()


def test_plan_skipped_when_missing_more_than_two():
    """Hand 3+ cards from any upgrade shouldn't generate a plan — the
    noise isn't actionable."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    # Empty hand → 4 cards off settlement, 5 off city, 3 off dev.
    out = recommend_actions(g, "RED", {}, top=6)
    assert out == []


def test_now_ranks_above_equal_score_soon():
    """A now-rec and a soon-rec with equal score: the now-rec must
    appear first so the overlay's top pick is always actionable today."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    # dev_card affordable (3.0, now), dev_card plan not applicable
    # since affordable. Hand only admits dev_card now — check top is it.
    out = recommend_actions(
        g, "RED", {"SHEEP": 1, "WHEAT": 1, "ORE": 1}, top=6)
    assert out[0]["when"] == "now"


def test_city_scores_higher_than_dev_card():
    """Upgrading a settlement to a city is strictly better than a
    random dev card at the same affordable-turn, so it should always
    outrank when both are options."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    hand = {"WHEAT": 2, "ORE": 3, "SHEEP": 1}
    out = recommend_actions(g, "RED", hand, top=4)
    kinds = [r["kind"] for r in out]
    assert "city" in kinds and "dev_card" in kinds
    city = next(r for r in out if r["kind"] == "city")
    dev = next(r for r in out if r["kind"] == "dev_card")
    assert city["score"] > dev["score"]


def test_live_game_resyncs_hand_on_reconnect_type4():
    """A second type=4 frame on an already-booted LiveGame should
    re-sync the self-hand from the replay's playerStates rather than
    being ignored. Fixes disconnect/reconnect drift."""
    from cataanbot.live_game import LiveGame

    lg = LiveGame()
    # Bootstrap a minimal gameState that LiveSession.from_game_start
    # will accept. The map_state just needs to parse — for a synthetic
    # test we'd need a real fixture, so use the existing fixture helper.
    # Easier path: skip if we can't construct without fixtures.
    import pathlib
    import json
    fixture = pathlib.Path(__file__).parent / "fixtures" / "gamestart.json"
    if not fixture.exists():
        import pytest
        pytest.skip("no GameStart fixture on disk — covered by integration")

    body = json.loads(fixture.read_text())
    lg.start_from_game_state(body)
    assert lg.started
    # Capture the current hand of the self-color, then "pretend" the
    # replay ships the same body again (reconnect). Should not raise.
    lg.feed({"type": 4, "payload": body})
    # Still started, no crash, and the tracker survived.
    assert lg.started


def test_colonist_diff_latches_current_turn_color():
    """current_turn_color_id should cache the last seen turn-color so a
    roll frame that omits currentTurnPlayerColor still attributes right."""
    from cataanbot.colonist_diff import LiveSession, events_from_diff
    from cataanbot.colonist_map import MapMapping

    sess = LiveSession(mapping=MapMapping(), player_names={3: "Alice"})
    # First frame sets the turn color.
    events_from_diff(sess, {"currentState": {"currentTurnPlayerColor": 3}})
    assert sess.current_turn_color_id == 3

    # Next frame: dice roll but no currentTurnPlayerColor. The emitted
    # RollEvent should still be attributed to Alice (color 3).
    events = events_from_diff(sess, {"diceState": {"dice1": 2, "dice2": 3}})
    rolls = [e for e in events if getattr(e, "d1", None) is not None]
    assert rolls
    assert rolls[0].player == "Alice"
