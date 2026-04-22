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
    assert len(out) == 1
    assert out[0]["kind"] == "dev_card"


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
