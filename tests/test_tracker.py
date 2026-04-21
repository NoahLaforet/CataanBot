"""Tracker mechanics: board ops, hand accounting, history replay, VP."""
from __future__ import annotations

import pytest

from cataanbot.tracker import Tracker, TrackerError


@pytest.fixture
def tracker():
    return Tracker(seed=4242)


def _any_legal_node(t: Tracker, color: str) -> int:
    """Grab any distance-legal opening node for `color` on the fixture map."""
    from catanatron import Color
    b = t.game.state.board
    return sorted(b.buildable_node_ids(Color[color], initial_build_phase=True))[0]


def _road_edge_from(t: Tracker, node_id: int) -> tuple[int, int]:
    m = t.game.state.board.map
    for tile in m.tiles.values():
        for edge in tile.edges.values():
            if node_id in edge:
                other = edge[0] if edge[1] == node_id else edge[1]
                return (node_id, other)
    raise AssertionError("no edge found from node")


def test_settle_places_building(tracker):
    node = _any_legal_node(tracker, "RED")
    tracker.settle("RED", node)
    buildings = tracker.game.state.board.buildings
    assert node in buildings
    color, kind = buildings[node]
    assert color.name == "RED"
    assert kind == "SETTLEMENT"


def test_settle_updates_vp(tracker):
    node = _any_legal_node(tracker, "RED")
    tracker.settle("RED", node)
    idx = tracker.game.state.color_to_index[tracker._color("RED")]
    assert tracker.game.state.player_state[f"P{idx}_VICTORY_POINTS"] == 1


def test_city_upgrades_settlement(tracker):
    node = _any_legal_node(tracker, "BLUE")
    tracker.settle("BLUE", node)
    tracker.city("BLUE", node)
    color, kind = tracker.game.state.board.buildings[node]
    assert color.name == "BLUE"
    assert kind == "CITY"
    idx = tracker.game.state.color_to_index[tracker._color("BLUE")]
    assert tracker.game.state.player_state[f"P{idx}_VICTORY_POINTS"] == 2


def test_settle_rejects_ocean_node(tracker):
    with pytest.raises(TrackerError):
        tracker.settle("RED", 9999)


def test_settle_rejects_invalid_color(tracker):
    node = _any_legal_node(tracker, "RED")
    with pytest.raises(TrackerError):
        tracker.settle("MAGENTA", node)


def test_undo_reverts_settle(tracker):
    node = _any_legal_node(tracker, "RED")
    tracker.settle("RED", node)
    dropped = tracker.undo()
    assert dropped["op"] == "settle"
    assert node not in tracker.game.state.board.buildings
    assert tracker.history == []


def test_undo_empty_history_is_noop(tracker):
    assert tracker.undo() is None


def test_save_load_roundtrip(tracker, tmp_path):
    node = _any_legal_node(tracker, "RED")
    tracker.settle("RED", node)
    edge = _road_edge_from(tracker, node)
    tracker.road("RED", edge[0], edge[1])

    path = tmp_path / "save.json"
    tracker.save(path)
    reloaded = Tracker.load(path)

    assert reloaded.seed == tracker.seed
    assert reloaded.history == tracker.history
    assert set(reloaded.game.state.board.buildings) == \
        set(tracker.game.state.board.buildings)


def test_give_and_take_adjust_hand(tracker):
    tracker.give("RED", 3, "WOOD")
    assert tracker.hand("RED")["WOOD"] == 3
    tracker.take("RED", 1, "WOOD")
    assert tracker.hand("RED")["WOOD"] == 2


def test_take_more_than_hand_raises(tracker):
    with pytest.raises(TrackerError):
        tracker.take("RED", 1, "ORE")


def test_trade_moves_both_sides(tracker):
    tracker.give("RED", 2, "WOOD")
    tracker.give("BLUE", 1, "WHEAT")
    tracker.trade("RED", 2, "WOOD", "BLUE", 1, "WHEAT")
    red = tracker.hand("RED")
    blue = tracker.hand("BLUE")
    assert red["WOOD"] == 0 and red["WHEAT"] == 1
    assert blue["WOOD"] == 2 and blue["WHEAT"] == 0


def test_trade_rejects_short_hand_atomically(tracker):
    tracker.give("RED", 1, "WOOD")
    # BLUE has no wheat to send back — trade must fail without moving RED's wood.
    with pytest.raises(TrackerError):
        tracker.trade("RED", 1, "WOOD", "BLUE", 1, "WHEAT")
    assert tracker.hand("RED")["WOOD"] == 1
    assert tracker.hand("BLUE")["WHEAT"] == 0


def test_mtrade_costs_and_returns(tracker):
    tracker.give("RED", 4, "WOOD")
    tracker.mtrade("RED", 4, "WOOD", "ORE")
    red = tracker.hand("RED")
    assert red["WOOD"] == 0 and red["ORE"] == 1


def test_devbuy_and_devplay_counts(tracker):
    tracker.devbuy("RED", "KNIGHT")
    counts = tracker.dev_counts("RED")
    assert counts["KNIGHT"]["hand"] == 1
    tracker.devplay("RED", "KNIGHT")
    counts = tracker.dev_counts("RED")
    assert counts["KNIGHT"]["hand"] == 0
    assert counts["KNIGHT"]["played"] == 1


def test_largest_army_threshold(tracker):
    for _ in range(3):
        tracker.devbuy("RED", "KNIGHT")
        tracker.devplay("RED", "KNIGHT")
    idx = tracker.game.state.color_to_index[tracker._color("RED")]
    assert tracker.game.state.player_state[f"P{idx}_HAS_ARMY"] is True
    # VP should include the +2 for largest army.
    assert tracker.game.state.player_state[f"P{idx}_VICTORY_POINTS"] == 2


def test_roll_distributes_resources(tracker):
    """A roll on a number that hits an owned tile should deliver resources."""
    # Find a land tile with a real number and an adjacent land node.
    m = tracker.game.state.board.map
    pick = None
    for _coord, tile in m.land_tiles.items():
        if tile.number is None or tile.resource is None:
            continue
        for node_id in tile.nodes.values():
            if node_id in m.land_nodes:
                pick = (tile, node_id)
                break
        if pick:
            break
    assert pick is not None, "no usable tile on fixture map"
    tile, node_id = pick

    # Place a settlement there and roll the tile's number.
    from catanatron import Color
    tracker.game.state.board.build_settlement(
        Color.RED, node_id, initial_build_phase=True
    )
    tracker._recompute_vp()
    before = tracker.hand("RED")[tile.resource]
    tracker.roll(tile.number)
    after = tracker.hand("RED")[tile.resource]
    assert after == before + 1


def test_roll_seven_distributes_nothing(tracker):
    before = tracker.hand("RED")
    tracker.roll(7)
    assert tracker.hand("RED") == before


def test_vp_callout_silent_in_early_game(tracker):
    node = _any_legal_node(tracker, "RED")
    tracker.settle("RED", node)
    status = tracker.vp_status()
    assert status["top"] == 1
    assert status["callout"] is None
    assert tracker.vp_callout_line() is None
