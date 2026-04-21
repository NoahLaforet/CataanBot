"""Tests for the event-stream hand tracker."""
from __future__ import annotations

from cataanbot.events import (
    BuildEvent, DevCardBuyEvent, DevCardPlayEvent, DiscardEvent,
    MonopolyStealEvent, ProduceEvent, StealEvent, TradeCommitEvent,
)
from cataanbot.hand_tracker import (
    apply_event, init_hands, reconstruct_hands,
)
from cataanbot.live import ColorMap


def test_init_hands_seats_zero_cards_for_each_color():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    hands = init_hands(cm)
    assert set(hands.keys()) == {"RED", "BLUE"}
    assert hands["RED"].total == 0
    assert all(v == 0 for v in hands["RED"].cards.values())


def test_produce_event_credits_resources():
    cm = ColorMap({"Alice": "RED"})
    hands = init_hands(cm)
    apply_event(
        hands, ProduceEvent(player="Alice", resources={"WOOD": 2, "BRICK": 1}),
        cm,
    )
    assert hands["RED"].cards == {
        "WOOD": 2, "BRICK": 1, "SHEEP": 0, "WHEAT": 0, "ORE": 0,
    }


def test_build_settlement_debits_four_resources():
    cm = ColorMap({"Alice": "RED"})
    hands = init_hands(cm)
    apply_event(hands, ProduceEvent(
        player="Alice",
        resources={"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1},
    ), cm)
    apply_event(hands, BuildEvent(
        player="Alice", piece="settlement", vp_delta=1,
    ), cm)
    assert hands["RED"].total == 0


def test_build_city_debits_wheat_and_ore():
    cm = ColorMap({"Alice": "RED"})
    hands = init_hands(cm)
    apply_event(hands, ProduceEvent(
        player="Alice", resources={"WHEAT": 3, "ORE": 4},
    ), cm)
    apply_event(hands, BuildEvent(
        player="Alice", piece="city", vp_delta=1,
    ), cm)
    assert hands["RED"].cards["WHEAT"] == 1
    assert hands["RED"].cards["ORE"] == 1


def test_dev_buy_debits_sheep_wheat_ore():
    cm = ColorMap({"Alice": "RED"})
    hands = init_hands(cm)
    apply_event(hands, ProduceEvent(
        player="Alice", resources={"SHEEP": 1, "WHEAT": 1, "ORE": 1},
    ), cm)
    apply_event(hands, DevCardBuyEvent(player="Alice"), cm)
    assert hands["RED"].total == 0


def test_trade_commit_moves_cards_both_ways():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    hands = init_hands(cm)
    apply_event(hands, ProduceEvent(
        player="Alice", resources={"WOOD": 3},
    ), cm)
    apply_event(hands, ProduceEvent(
        player="Bob", resources={"WHEAT": 2},
    ), cm)
    apply_event(hands, TradeCommitEvent(
        giver="Alice", receiver="Bob",
        gave={"WOOD": 2}, got={"WHEAT": 1},
    ), cm)
    assert hands["RED"].cards["WOOD"] == 1
    assert hands["RED"].cards["WHEAT"] == 1
    assert hands["BLUE"].cards["WOOD"] == 2
    assert hands["BLUE"].cards["WHEAT"] == 1


def test_discard_event_debits_resources():
    cm = ColorMap({"Alice": "RED"})
    hands = init_hands(cm)
    apply_event(hands, ProduceEvent(
        player="Alice", resources={"WOOD": 4, "ORE": 4},
    ), cm)
    apply_event(hands, DiscardEvent(
        player="Alice", resources={"WOOD": 2, "ORE": 2},
    ), cm)
    assert hands["RED"].cards["WOOD"] == 2
    assert hands["RED"].cards["ORE"] == 2


def test_year_of_plenty_adds_two_cards_from_bank():
    cm = ColorMap({"Alice": "RED"})
    hands = init_hands(cm)
    apply_event(hands, DevCardPlayEvent(
        player="Alice", card="year_of_plenty",
        resources={"ORE": 1, "WHEAT": 1},
    ), cm)
    assert hands["RED"].cards["ORE"] == 1
    assert hands["RED"].cards["WHEAT"] == 1


def test_monopoly_pulls_resource_from_everyone():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE", "Carol": "WHITE"})
    hands = init_hands(cm)
    apply_event(hands, ProduceEvent(
        player="Bob", resources={"SHEEP": 2, "WOOD": 1},
    ), cm)
    apply_event(hands, ProduceEvent(
        player="Carol", resources={"SHEEP": 1},
    ), cm)
    apply_event(hands, MonopolyStealEvent(
        player="Alice", resource="SHEEP", count=3,
    ), cm)
    assert hands["RED"].cards["SHEEP"] == 3
    assert hands["BLUE"].cards["SHEEP"] == 0
    assert hands["BLUE"].cards["WOOD"] == 1  # untouched
    assert hands["WHITE"].cards["SHEEP"] == 0


def test_known_steal_moves_one_card():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    hands = init_hands(cm)
    apply_event(hands, ProduceEvent(
        player="Bob", resources={"WHEAT": 2},
    ), cm)
    apply_event(hands, StealEvent(
        thief="Alice", victim="Bob", resource="WHEAT",
    ), cm)
    assert hands["RED"].cards["WHEAT"] == 1
    assert hands["BLUE"].cards["WHEAT"] == 1


def test_unknown_steal_creates_thief_unknown_bucket():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    hands = init_hands(cm)
    # Bob has 3 cards, all sheep (only resource we know about).
    apply_event(hands, ProduceEvent(
        player="Bob", resources={"SHEEP": 3},
    ), cm)
    # Alice steals unknown from Bob.
    apply_event(hands, StealEvent(thief="Alice", victim="Bob"), cm)
    assert hands["RED"].unknown == 1
    assert hands["RED"].total == 1
    # Bob lost 1 from his largest pile (sheep).
    assert hands["BLUE"].cards["SHEEP"] == 2
    assert hands["BLUE"].total == 2


def test_unknown_steal_then_debit_resolves_unknown():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    hands = init_hands(cm)
    # Alice has 0 cards. Steals 1 unknown from Bob.
    apply_event(hands, ProduceEvent(
        player="Bob", resources={"WOOD": 1},
    ), cm)
    apply_event(hands, StealEvent(thief="Alice", victim="Bob"), cm)
    assert hands["RED"].unknown == 1
    # Now Alice builds a road (costs 1 WOOD + 1 BRICK). She has no known
    # resources, but she has 1 unknown — and she also produces 1 BRICK.
    apply_event(hands, ProduceEvent(
        player="Alice", resources={"BRICK": 1},
    ), cm)
    apply_event(hands, BuildEvent(
        player="Alice", piece="road", vp_delta=0,
    ), cm)
    # Unknown was consumed to cover the WOOD debit.
    assert hands["RED"].unknown == 0
    # Drift for the "WOOD" shortfall should be 0 — unknown covered it.
    assert hands["RED"].drift == 0


def test_overdraft_increments_drift_and_clamps():
    cm = ColorMap({"Alice": "RED"})
    hands = init_hands(cm)
    # Alice has 0 cards but the event stream says she discarded 2 WOOD.
    # (This can happen if we missed an earlier produce.)
    apply_event(hands, DiscardEvent(
        player="Alice", resources={"WOOD": 2},
    ), cm)
    assert hands["RED"].cards["WOOD"] == 0
    assert hands["RED"].drift == 2


def test_reconstruct_hands_full_stream():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        ProduceEvent(player="Alice", resources={"WOOD": 1, "BRICK": 1}),
        ProduceEvent(player="Bob",   resources={"SHEEP": 1, "WHEAT": 1}),
        BuildEvent(player="Alice", piece="road", vp_delta=0),
        TradeCommitEvent(
            giver="Bob", receiver="Alice",
            gave={"SHEEP": 1}, got={"WOOD": 0},  # free gift edge case
        ),
    ]
    hands = reconstruct_hands(events, cm)
    assert hands["RED"].total == 1   # started 2, spent 2 on road, got 1 sheep
    assert hands["RED"].cards["SHEEP"] == 1
    assert hands["BLUE"].total == 1
    assert hands["BLUE"].cards["WHEAT"] == 1
