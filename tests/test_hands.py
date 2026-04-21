"""Hand-replay accounting: produced/spent/received + authoritative current."""
from __future__ import annotations

import pytest

from cataanbot.hands import estimate_hands, format_hands
from cataanbot.tracker import Tracker


@pytest.fixture
def tracker():
    return Tracker(seed=4242)


def test_estimate_returns_all_colors(tracker):
    per_color = estimate_hands(tracker)
    assert set(per_color.keys()) >= {"RED", "BLUE", "WHITE", "ORANGE"}
    for info in per_color.values():
        assert set(info.keys()) >= {"produced", "spent", "received", "current"}


def test_current_matches_tracker_state(tracker):
    tracker.give("RED", 3, "WOOD")
    tracker.give("BLUE", 2, "ORE")
    per_color = estimate_hands(tracker)
    assert per_color["RED"]["current"]["WOOD"] == 3
    assert per_color["BLUE"]["current"]["ORE"] == 2


def test_give_routes_into_received_bucket(tracker):
    tracker.give("RED", 3, "WOOD")
    per_color = estimate_hands(tracker)
    assert per_color["RED"]["received"]["WOOD"] == 3
    assert per_color["RED"]["spent"]["WOOD"] == 0


def test_take_routes_into_spent_bucket(tracker):
    tracker.give("RED", 3, "WOOD")
    tracker.take("RED", 2, "WOOD")
    per_color = estimate_hands(tracker)
    assert per_color["RED"]["received"]["WOOD"] == 3
    assert per_color["RED"]["spent"]["WOOD"] == 2
    assert per_color["RED"]["current"]["WOOD"] == 1


def test_trade_moves_both_buckets(tracker):
    tracker.give("RED", 2, "WOOD")
    tracker.give("BLUE", 1, "WHEAT")
    tracker.trade("RED", 2, "WOOD", "BLUE", 1, "WHEAT")
    per_color = estimate_hands(tracker)
    # RED spent wood, received wheat.
    assert per_color["RED"]["spent"]["WOOD"] == 2
    assert per_color["RED"]["received"]["WHEAT"] == 1
    # BLUE received wood, spent wheat.
    assert per_color["BLUE"]["received"]["WOOD"] == 2
    assert per_color["BLUE"]["spent"]["WHEAT"] == 1


def test_devbuy_accounts_for_cost(tracker):
    tracker.give("RED", 1, "SHEEP")
    tracker.give("RED", 1, "WHEAT")
    tracker.give("RED", 1, "ORE")
    tracker.devbuy("RED", "KNIGHT")
    per_color = estimate_hands(tracker)
    # devbuy adds 1 to each of sheep/wheat/ore in the spent bucket.
    assert per_color["RED"]["spent"]["SHEEP"] == 1
    assert per_color["RED"]["spent"]["WHEAT"] == 1
    assert per_color["RED"]["spent"]["ORE"] == 1


def test_roll_produces_for_owner(tracker):
    """A roll that hits RED's settlement should show up in produced bucket."""
    from catanatron import Color
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
    assert pick is not None
    tile, node_id = pick
    tracker.settle("RED", node_id)
    tracker.roll(tile.number)
    per_color = estimate_hands(tracker)
    assert per_color["RED"]["produced"][tile.resource] == 1


def test_format_hands_mentions_all_colors(tracker):
    out = format_hands(estimate_hands(tracker))
    for color in ("RED", "BLUE", "WHITE", "ORANGE"):
        assert color in out
