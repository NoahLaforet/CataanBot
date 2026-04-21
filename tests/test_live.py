"""Tests for the Event → Tracker dispatcher."""
from __future__ import annotations

import pytest

from cataanbot.events import (
    BuildEvent, DevCardBuyEvent, DevCardPlayEvent, DiscardEvent,
    DisconnectEvent, GameOverEvent, InfoEvent, MonopolyStealEvent,
    NoStealEvent, ProduceEvent, RobberMoveEvent, RollBlockedEvent,
    RollEvent, StealEvent, TradeCommitEvent, TradeOfferEvent,
    UnknownEvent, VPEvent,
)
from cataanbot.live import (
    ColorMap, ColorMapError, apply_event,
)
from cataanbot.tracker import Tracker


# ---------------------------------------------------------------------------
# ColorMap
# ---------------------------------------------------------------------------

def test_color_map_auto_assigns_in_order():
    cm = ColorMap()
    assert cm.get("Alice") == "RED"
    assert cm.get("Bob") == "BLUE"
    assert cm.get("Carol") == "WHITE"
    assert cm.get("Dan") == "ORANGE"


def test_color_map_get_is_idempotent():
    cm = ColorMap()
    assert cm.get("Alice") == "RED"
    assert cm.get("Alice") == "RED"


def test_color_map_manual_mapping_wins():
    cm = ColorMap({"Alice": "BLUE"})
    assert cm.get("Alice") == "BLUE"
    # Next auto-assign skips the taken color.
    assert cm.get("Bob") == "RED"


def test_color_map_rejects_duplicate_color():
    cm = ColorMap({"Alice": "RED"})
    with pytest.raises(ColorMapError):
        cm.add("Bob", "RED")


def test_color_map_allows_reassigning_same_color_to_same_user():
    cm = ColorMap({"Alice": "RED"})
    cm.add("Alice", "RED")  # idempotent — no error
    assert cm.get("Alice") == "RED"


def test_color_map_rejects_remap_of_user():
    cm = ColorMap({"Alice": "RED"})
    with pytest.raises(ColorMapError):
        cm.add("Alice", "BLUE")


def test_color_map_rejects_unknown_color():
    with pytest.raises(ColorMapError):
        ColorMap({"Alice": "PURPLE"})


def test_color_map_exhausted_raises():
    cm = ColorMap()
    for name in ("A", "B", "C", "D"):
        cm.get(name)
    with pytest.raises(ColorMapError):
        cm.get("E")


def test_color_map_reverse_lookup():
    cm = ColorMap({"Alice": "RED"})
    assert cm.reverse("RED") == "Alice"
    assert cm.reverse("red") == "Alice"
    assert cm.reverse("BLUE") is None


# ---------------------------------------------------------------------------
# Informational / skipped events
# ---------------------------------------------------------------------------

def test_roll_event_is_skipped():
    t = Tracker()
    result = apply_event(t, ColorMap(), RollEvent(player="Alice", d1=3, d2=4))
    assert result.status == "skipped"
    assert "7" in result.message


def test_info_event_is_skipped():
    t = Tracker()
    result = apply_event(t, ColorMap(), InfoEvent(text="happy settling"))
    assert result.status == "skipped"


def test_disconnect_event_is_skipped():
    t = Tracker()
    result = apply_event(t, ColorMap(),
                         DisconnectEvent(player="Alice", reconnected=False))
    assert result.status == "skipped"


def test_no_steal_is_skipped():
    t = Tracker()
    result = apply_event(t, ColorMap(), NoStealEvent())
    assert result.status == "skipped"


def test_trade_offer_is_skipped():
    t = Tracker()
    result = apply_event(
        t, ColorMap(),
        TradeOfferEvent(player="Alice", give={"WOOD": 1}, want={"WHEAT": 1}),
    )
    assert result.status == "skipped"


def test_roll_blocked_is_skipped():
    t = Tracker()
    result = apply_event(
        t, ColorMap(),
        RollBlockedEvent(tile_label="grain tile", prob=9),
    )
    assert result.status == "skipped"


# ---------------------------------------------------------------------------
# Produce / discard
# ---------------------------------------------------------------------------

def test_produce_event_gives_resources():
    t = Tracker()
    cm = ColorMap()
    result = apply_event(
        t, cm,
        ProduceEvent(player="Alice", resources={"WOOD": 2, "SHEEP": 1}),
    )
    assert result.status == "applied"
    hand = t.hand("RED")
    assert hand["WOOD"] == 2
    assert hand["SHEEP"] == 1
    assert hand["WHEAT"] == 0


def test_discard_event_takes_from_hand():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    t.give("RED", 3, "WOOD")
    t.give("RED", 2, "SHEEP")
    result = apply_event(
        t, cm,
        DiscardEvent(player="Alice", resources={"WOOD": 2, "SHEEP": 1}),
    )
    assert result.status == "applied"
    assert t.hand("RED")["WOOD"] == 1
    assert t.hand("RED")["SHEEP"] == 1


def test_discard_more_than_held_is_error():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    result = apply_event(
        t, cm,
        DiscardEvent(player="Alice", resources={"WOOD": 2}),
    )
    assert result.status == "error"


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

def test_bank_trade_commits_to_tracker():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    t.give("RED", 4, "WOOD")
    result = apply_event(
        t, cm,
        TradeCommitEvent(
            giver="Alice", receiver="BANK",
            gave={"WOOD": 4}, got={"WHEAT": 1},
        ),
    )
    assert result.status == "applied"
    hand = t.hand("RED")
    assert hand["WOOD"] == 0
    assert hand["WHEAT"] == 1


def test_player_trade_moves_both_hands():
    t = Tracker()
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    t.give("RED", 2, "WOOD")
    t.give("BLUE", 1, "WHEAT")
    result = apply_event(
        t, cm,
        TradeCommitEvent(
            giver="Alice", receiver="Bob",
            gave={"WOOD": 2}, got={"WHEAT": 1},
        ),
    )
    assert result.status == "applied"
    assert t.hand("RED")["WOOD"] == 0
    assert t.hand("RED")["WHEAT"] == 1
    assert t.hand("BLUE")["WOOD"] == 2
    assert t.hand("BLUE")["WHEAT"] == 0


def test_multi_resource_player_trade():
    t = Tracker()
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    t.give("RED", 1, "SHEEP")
    t.give("RED", 1, "ORE")
    t.give("BLUE", 1, "WHEAT")
    # BrickdDaddy gave 1xSHEEP 1xORE and got 1xWHEAT from German
    result = apply_event(
        t, cm,
        TradeCommitEvent(
            giver="Alice", receiver="Bob",
            gave={"SHEEP": 1, "ORE": 1}, got={"WHEAT": 1},
        ),
    )
    assert result.status == "applied"
    assert t.hand("RED")["WHEAT"] == 1
    assert t.hand("RED")["SHEEP"] == 0
    assert t.hand("RED")["ORE"] == 0
    assert t.hand("BLUE")["SHEEP"] == 1
    assert t.hand("BLUE")["ORE"] == 1
    assert t.hand("BLUE")["WHEAT"] == 0


# ---------------------------------------------------------------------------
# Steals
# ---------------------------------------------------------------------------

def test_steal_with_revealed_resource():
    t = Tracker()
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    t.give("BLUE", 1, "WOOD")
    result = apply_event(
        t, cm,
        StealEvent(thief="Alice", victim="Bob", resource="WOOD"),
    )
    assert result.status == "applied"
    assert t.hand("RED")["WOOD"] == 1
    assert t.hand("BLUE")["WOOD"] == 0


def test_steal_without_resource_is_unhandled():
    t = Tracker()
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    result = apply_event(
        t, cm,
        StealEvent(thief="Alice", victim="Bob", resource=None),
    )
    assert result.status == "unhandled"


# ---------------------------------------------------------------------------
# Dev cards
# ---------------------------------------------------------------------------

def test_devcard_buy_debits_cost():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    for r in ("WHEAT", "SHEEP", "ORE"):
        t.give("RED", 1, r)
    result = apply_event(t, cm, DevCardBuyEvent(player="Alice"))
    assert result.status == "applied"
    hand = t.hand("RED")
    assert hand["WHEAT"] == 0
    assert hand["SHEEP"] == 0
    assert hand["ORE"] == 0


def test_devcard_play_knight_bumps_played_count():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    result = apply_event(
        t, cm, DevCardPlayEvent(player="Alice", card="knight"),
    )
    assert result.status == "applied"
    state = t.game.state
    idx = state.color_to_index[t._color("RED")]
    assert state.player_state[f"P{idx}_PLAYED_KNIGHT"] == 1


def test_three_knights_grants_largest_army():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    for _ in range(3):
        apply_event(
            t, cm, DevCardPlayEvent(player="Alice", card="knight"),
        )
    state = t.game.state
    idx = state.color_to_index[t._color("RED")]
    assert state.player_state[f"P{idx}_PLAYED_KNIGHT"] == 3
    assert state.player_state[f"P{idx}_HAS_ARMY"] is True


def test_year_of_plenty_gives_picked_resources():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    result = apply_event(
        t, cm,
        DevCardPlayEvent(
            player="Alice", card="year_of_plenty",
            resources={"WHEAT": 1, "ORE": 1},
        ),
    )
    assert result.status == "applied"
    hand = t.hand("RED")
    assert hand["WHEAT"] == 1
    assert hand["ORE"] == 1


def test_unknown_dev_card_is_unhandled():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    result = apply_event(
        t, cm, DevCardPlayEvent(player="Alice", card="unknown"),
    )
    assert result.status == "unhandled"


# ---------------------------------------------------------------------------
# Monopoly
# ---------------------------------------------------------------------------

def test_monopoly_drains_sheep_from_opponents():
    t = Tracker()
    cm = ColorMap({
        "Alice": "RED", "Bob": "BLUE",
        "Carol": "WHITE", "Dan": "ORANGE",
    })
    t.give("BLUE", 2, "SHEEP")
    t.give("WHITE", 3, "SHEEP")
    t.give("ORANGE", 1, "SHEEP")
    result = apply_event(
        t, cm,
        MonopolyStealEvent(player="Alice", resource="SHEEP", count=6),
    )
    assert result.status == "applied"
    assert t.hand("RED")["SHEEP"] == 6
    assert t.hand("BLUE")["SHEEP"] == 0
    assert t.hand("WHITE")["SHEEP"] == 0
    assert t.hand("ORANGE")["SHEEP"] == 0


def test_monopoly_short_tracker_reports_mismatch():
    t = Tracker()
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    # Event says 6 sheep but tracker only thinks Bob has 2 — hand
    # accounting is behind reality. We apply what we can and flag it.
    t.give("BLUE", 2, "SHEEP")
    result = apply_event(
        t, cm,
        MonopolyStealEvent(player="Alice", resource="SHEEP", count=6),
    )
    assert result.status == "applied"
    assert t.hand("RED")["SHEEP"] == 2
    assert "short" in result.message


# ---------------------------------------------------------------------------
# VP
# ---------------------------------------------------------------------------

def test_first_longest_road_sets_has_road_flag():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    result = apply_event(
        t, cm, VPEvent(player="Alice", reason="longest_road", vp_delta=2),
    )
    assert result.status == "applied"
    state = t.game.state
    idx = state.color_to_index[t._color("RED")]
    assert state.player_state[f"P{idx}_HAS_ROAD"] is True
    # VP total reflects the +2 bonus.
    assert state.player_state[f"P{idx}_VICTORY_POINTS"] == 2


def test_longest_road_transfer_flips_flags():
    t = Tracker()
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    apply_event(t, cm,
                VPEvent(player="Bob", reason="longest_road", vp_delta=2))
    apply_event(
        t, cm,
        VPEvent(player="Alice", reason="longest_road",
                vp_delta=2, previous_holder="Bob"),
    )
    state = t.game.state
    red_idx = state.color_to_index[t._color("RED")]
    blue_idx = state.color_to_index[t._color("BLUE")]
    assert state.player_state[f"P{red_idx}_HAS_ROAD"] is True
    assert state.player_state[f"P{blue_idx}_HAS_ROAD"] is False


def test_largest_army_sets_has_army_flag():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    result = apply_event(
        t, cm, VPEvent(player="Alice", reason="largest_army", vp_delta=2),
    )
    assert result.status == "applied"
    state = t.game.state
    idx = state.color_to_index[t._color("RED")]
    assert state.player_state[f"P{idx}_HAS_ARMY"] is True


# ---------------------------------------------------------------------------
# Unhandled — needs topology
# ---------------------------------------------------------------------------

def test_build_event_is_unhandled():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    result = apply_event(
        t, cm,
        BuildEvent(player="Alice", piece="settlement", vp_delta=1),
    )
    assert result.status == "unhandled"
    assert "topology" in result.message


def test_robber_move_is_unhandled():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    result = apply_event(
        t, cm,
        RobberMoveEvent(player="Alice", tile_label="grain tile", prob=9),
    )
    assert result.status == "unhandled"


# ---------------------------------------------------------------------------
# Game over + fallthroughs
# ---------------------------------------------------------------------------

def test_game_over_reports_winner():
    t = Tracker()
    result = apply_event(t, ColorMap(), GameOverEvent(winner="Alice"))
    assert result.status == "applied"
    assert "Alice" in result.message


def test_unknown_event_is_unhandled():
    t = Tracker()
    result = apply_event(
        t, ColorMap(),
        UnknownEvent(text="???", icons=[], names=[]),
    )
    assert result.status == "unhandled"
