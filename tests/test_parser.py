"""Parser tests driven by real log payloads from a colonist.io bot game."""
from __future__ import annotations

from cataanbot.events import (
    BuildEvent,
    DevCardBuyEvent,
    DevCardPlayEvent,
    DiscardEvent,
    DisconnectEvent,
    GameOverEvent,
    InfoEvent,
    MonopolyStealEvent,
    NoStealEvent,
    ProduceEvent,
    RobberMoveEvent,
    RollBlockedEvent,
    RollEvent,
    StealEvent,
    TradeCommitEvent,
    TradeOfferEvent,
    VPEvent,
)
from cataanbot.parser import parse_event


def _make(parts, self_name=None):
    """Build a minimal payload around a parts array."""
    return {"ts": 0, "text": "", "parts": parts, "names": [], "icons": [],
            "self": self_name}


def _name(n):
    return {"kind": "name", "name": n, "color": ""}


def _text(t):
    return {"kind": "text", "text": t}


def _icon(alt):
    return {"kind": "icon", "alt": alt, "src_tail": ""}


# ---------------------------------------------------------------------------
# Rolls
# ---------------------------------------------------------------------------

def test_roll_parses_both_dice():
    ev = parse_event(_make([
        _name("Hans"), _text("rolled"), _icon("dice_3"), _icon("dice_4"),
    ]))
    assert isinstance(ev, RollEvent)
    assert ev.player == "Hans"
    assert (ev.d1, ev.d2) == (3, 4)
    assert ev.total == 7


def test_roll_snake_eyes():
    ev = parse_event(_make([
        _name("Hans"), _text("rolled"), _icon("dice_1"), _icon("dice_1"),
    ]))
    assert isinstance(ev, RollEvent)
    assert ev.total == 2


# ---------------------------------------------------------------------------
# Production
# ---------------------------------------------------------------------------

def test_production_single_resource():
    ev = parse_event(_make([
        _name("BrickdDaddy"), _text("got"), _icon("Lumber"),
    ]))
    assert isinstance(ev, ProduceEvent)
    assert ev.player == "BrickdDaddy"
    assert ev.resources == {"WOOD": 1}


def test_production_multiple_resources():
    ev = parse_event(_make([
        _name("Hans"), _text("got"),
        _icon("Ore"), _icon("Ore"), _icon("Wool"),
    ]))
    assert isinstance(ev, ProduceEvent)
    assert ev.resources == {"ORE": 2, "SHEEP": 1}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def test_build_city_awards_vp():
    ev = parse_event(_make([
        _name("Hans"), _text("built a City"), _text("(+1 VP)"), _icon("city"),
    ]))
    assert isinstance(ev, BuildEvent)
    assert ev.piece == "city"
    assert ev.vp_delta == 1


def test_build_road_no_vp():
    ev = parse_event(_make([
        _name("BrickdDaddy"), _text("built a Road"), _icon("road"),
    ]))
    assert isinstance(ev, BuildEvent)
    assert ev.piece == "road"
    assert ev.vp_delta == 0


def test_build_settlement_awards_vp():
    ev = parse_event(_make([
        _name("Nona"), _text("built a Settlement"), _text("(+1 VP)"),
        _icon("settlement"),
    ]))
    assert isinstance(ev, BuildEvent)
    assert ev.piece == "settlement"
    assert ev.vp_delta == 1


# ---------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------

def test_discard_counts_icons():
    ev = parse_event(_make([
        _name("Hans"), _text("discarded"),
        _icon("Wool"), _icon("Grain"), _icon("Grain"), _icon("Wool"),
    ]))
    assert isinstance(ev, DiscardEvent)
    assert ev.resources == {"SHEEP": 2, "WHEAT": 2}


# ---------------------------------------------------------------------------
# Robber move + steal
# ---------------------------------------------------------------------------

def test_robber_move_to_numbered_tile():
    ev = parse_event(_make([
        _name("Grega"), _text("moved Robber  to"),
        _icon("robber"), _icon("prob_9"), _icon("ore tile"),
    ]))
    assert isinstance(ev, RobberMoveEvent)
    assert ev.tile_label == "ore tile"
    assert ev.prob == 9


def test_robber_move_to_desert():
    ev = parse_event(_make([
        _name("Hans"), _text("moved Robber  to Desert"), _icon("robber"),
    ]))
    assert isinstance(ev, RobberMoveEvent)
    assert ev.tile_label == "Desert"
    assert ev.prob is None


def test_steal_names_both_players():
    ev = parse_event(_make([
        _name("Grega"), _text("stole  from"), _name("Hans"),
        _icon("Resource Card"),
    ]))
    assert isinstance(ev, StealEvent)
    assert ev.thief == "Grega"
    assert ev.victim == "Hans"


def test_no_player_to_steal():
    ev = parse_event(_make([_text("No player to steal from")]))
    assert isinstance(ev, NoStealEvent)


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

def test_bank_trade_splits_give_and_take():
    ev = parse_event(_make([
        _name("Hans"), _text("gave bank"),
        _icon("Lumber"), _icon("Lumber"), _icon("Lumber"), _icon("Lumber"),
        _text("and took"), _icon("Ore"),
    ]))
    assert isinstance(ev, TradeCommitEvent)
    assert ev.giver == "Hans"
    assert ev.receiver == "BANK"
    assert ev.gave == {"WOOD": 4}
    assert ev.got == {"ORE": 1}


def test_player_trade_splits_both_sides():
    # "Hans gave [Grain Wool] and got [Ore Ore] from Grega"
    ev = parse_event(_make([
        _name("Hans"), _text("gave"),
        _icon("Grain"), _icon("Wool"),
        _text("and got"),
        _icon("Ore"), _icon("Ore"),
        _text("from"), _name("Grega"),
    ]))
    assert isinstance(ev, TradeCommitEvent)
    assert ev.giver == "Hans"
    assert ev.receiver == "Grega"
    assert ev.gave == {"WHEAT": 1, "SHEEP": 1}
    assert ev.got == {"ORE": 2}


def test_trade_offer_captures_both_sides():
    # "Hans wants to give [Wool Wool Ore] for [Grain]"
    ev = parse_event(_make([
        _name("Hans"), _text("wants to give"),
        _icon("Wool"), _icon("Wool"), _icon("Ore"),
        _text("for"), _icon("Grain"),
    ]))
    assert isinstance(ev, TradeOfferEvent)
    assert ev.player == "Hans"
    assert ev.give == {"SHEEP": 2, "ORE": 1}
    assert ev.want == {"WHEAT": 1}


# ---------------------------------------------------------------------------
# Info / disconnect
# ---------------------------------------------------------------------------

def test_friendly_robber_is_info():
    ev = parse_event(_make([
        _text("Friendly Robber is active, tiles available to block are limited"),
        _icon("robber"),
    ]))
    assert isinstance(ev, InfoEvent)


def test_bot_selecting_discard_is_info():
    ev = parse_event(_make([
        _text("Bot is selecting cards to discard for"), _name("Hans"),
    ]))
    assert isinstance(ev, InfoEvent)


def test_self_steal_from_opponent_reveals_resource():
    ev = parse_event(_make([
        _text("You stole from"), _name("Hans"), _icon("Brick"),
    ], self_name="BrickdDaddy"))
    assert isinstance(ev, StealEvent)
    assert ev.thief == "BrickdDaddy"
    assert ev.victim == "Hans"
    assert ev.resource == "BRICK"


def test_opponent_steal_from_self_reveals_resource():
    ev = parse_event(_make([
        _name("Hans"), _text("stole from you"), _icon("Wool"),
    ], self_name="BrickdDaddy"))
    assert isinstance(ev, StealEvent)
    assert ev.thief == "Hans"
    assert ev.victim == "BrickdDaddy"
    assert ev.resource == "SHEEP"


def test_self_steal_without_session_falls_back():
    ev = parse_event(_make([
        _text("You stole from"), _name("Hans"), _icon("Brick"),
    ]))
    assert isinstance(ev, StealEvent)
    assert ev.thief == "YOU"
    assert ev.resource == "BRICK"


def test_roll_blocked_by_robber():
    ev = parse_event(_make([
        _icon("prob_8"), _icon("lumber tile"),
        _text("is blocked by the Robber. No resources produced"),
    ]))
    assert isinstance(ev, RollBlockedEvent)
    assert ev.tile_label == "lumber tile"
    assert ev.prob == 8


def test_dev_card_buy_via_icon():
    ev = parse_event(_make([
        _name("BrickdDaddy"), _text("bought"), _icon("Development Card"),
    ]))
    assert isinstance(ev, DevCardBuyEvent)
    assert ev.player == "BrickdDaddy"


def test_dev_card_played_generic():
    ev = parse_event(_make([
        _name("BrickdDaddy"), _text("used"),
    ]))
    assert isinstance(ev, DevCardPlayEvent)
    assert ev.player == "BrickdDaddy"
    assert ev.card == "unknown"


def test_year_of_plenty_takes_two_from_bank():
    ev = parse_event(_make([
        _name("Brit"), _text("took from bank"), _icon("Grain"), _icon("Ore"),
    ]))
    assert isinstance(ev, DevCardPlayEvent)
    assert ev.card == "year_of_plenty"
    assert ev.resources == {"WHEAT": 1, "ORE": 1}


def test_longest_road_with_transfer_names_previous_holder():
    ev = parse_event(_make([
        _name("Brit"), _text("took Longest Road from"),
        _name("BrickdDaddy"),
    ]))
    assert isinstance(ev, VPEvent)
    assert ev.player == "Brit"
    assert ev.reason == "longest_road"
    assert ev.previous_holder == "BrickdDaddy"
    assert ev.vp_delta == 2


def test_longest_road_passed_from_names_new_holder_second():
    # Colonist's "passed from X to Y" rendering — Y is the new holder.
    ev = parse_event(_make([
        _text("Longest Road"), _icon("longest road"), _text("passed from"),
        _name("Burck"), _text("to"), _name("BrickdDaddy"),
        _text("("), _text("+2 VPs"), _text(")"),
    ]))
    assert isinstance(ev, VPEvent)
    assert ev.player == "BrickdDaddy"
    assert ev.previous_holder == "Burck"
    assert ev.reason == "longest_road"
    assert ev.vp_delta == 2


def test_largest_army_first_time_has_no_previous():
    ev = parse_event(_make([
        _name("BrickdDaddy"), _text("has Largest Army"),
    ]))
    assert isinstance(ev, VPEvent)
    assert ev.reason == "largest_army"
    assert ev.previous_holder is None


def test_game_over():
    ev = parse_event(_make([
        _name("Hans"), _text("won the game!"), _icon("trophy"), _icon("trophy"),
    ]))
    assert isinstance(ev, GameOverEvent)
    assert ev.winner == "Hans"


def test_disconnect_and_reconnect():
    d = parse_event(_make([
        _name("BrickdDaddy"),
        _text("has disconnected. A bot will take over next turn"),
    ]))
    assert isinstance(d, DisconnectEvent)
    assert d.reconnected is False

    r = parse_event(_make([
        _name("BrickdDaddy"), _text("has reconnected"),
    ]))
    assert isinstance(r, DisconnectEvent)
    assert r.reconnected is True


def test_disconnect_with_trailing_unless_clause():
    # Real-game text: name rendered as plain text, not a colored span.
    d = parse_event(_make([
        _text("BrickdDaddy has disconnected. A bot will take over next "
              "turn unless BrickdDaddy reconnects."),
    ]))
    assert isinstance(d, DisconnectEvent)
    assert d.player == "BrickdDaddy"
    assert d.reconnected is False


def test_setup_placement_counts_as_build():
    ev = parse_event(_make([
        _name("Kitti"), _text("placed a Settlement"), _icon("settlement"),
    ]))
    assert isinstance(ev, BuildEvent)
    assert ev.piece == "settlement"
    assert ev.vp_delta == 1
    # Setup placements are free — don't charge the hand.
    assert ev.paid is False


def test_dev_card_placed_road():
    ev = parse_event(_make([
        _name("BrickdDaddy"), _text("placed a Road"), _icon("road"),
    ]))
    assert isinstance(ev, BuildEvent)
    assert ev.piece == "road"
    assert ev.vp_delta == 0
    # Road Building dev card places roads for free too.
    assert ev.paid is False


def test_built_a_sets_paid_true():
    ev = parse_event(_make([
        _name("BrickdDaddy"), _text("built a Road"), _icon("road"),
    ]))
    assert isinstance(ev, BuildEvent)
    assert ev.paid is True


def test_starting_resources_are_production():
    ev = parse_event(_make([
        _name("Marja"), _text("received starting resources"),
        _icon("Brick"), _icon("Wool"), _icon("Lumber"),
    ]))
    assert isinstance(ev, ProduceEvent)
    assert ev.player == "Marja"
    assert ev.resources == {"BRICK": 1, "SHEEP": 1, "WOOD": 1}


def test_monopoly_claim_pulls_total_from_opponents():
    # Icon alt comes back lowercase ('lumber') in this row even though
    # the "Lumber" (title) alt is used elsewhere; rule is case-insensitive.
    ev = parse_event(_make([
        _name("Afrika"), _text("stole 10"), _icon("lumber"),
    ]))
    assert isinstance(ev, MonopolyStealEvent)
    assert ev.player == "Afrika"
    assert ev.resource == "WOOD"
    assert ev.count == 10


def test_happy_settling_is_info():
    ev = parse_event(_make([
        _text("Happy settling! Learn how to play in the rulebook ."
              " List of commands: /help"),
    ]))
    assert isinstance(ev, InfoEvent)


def test_insufficient_bank_distribute_is_info():
    """Bank-shortage notice ("Insufficient in bank to distribute: 5 in
    bank when 7 were required") has no player name, so it used to fall
    through to UnknownEvent and pollute the userscript log scan as "?".
    The actual partial yield already fires as a separate ProduceEvent —
    this row is purely informational."""
    ev = parse_event(_make([
        _text("Insufficient in bank to distribute: 5 in bank when 7"
              " were required"),
        _icon("Grain"),
    ]))
    assert isinstance(ev, InfoEvent)
