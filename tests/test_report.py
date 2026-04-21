"""Tests for the offline replay postmortem report."""
from __future__ import annotations

from cataanbot.events import (
    BuildEvent, DevCardBuyEvent, DevCardPlayEvent, DiscardEvent,
    GameOverEvent, MonopolyStealEvent, ProduceEvent, RollEvent,
    StealEvent, TradeCommitEvent, VPEvent,
)
from cataanbot.live import ColorMap, DispatchResult
from cataanbot.report import build_report, format_report


def _result(event, status="applied", message=""):
    return DispatchResult(event=event, status=status, message=message)


def test_build_report_counts_rolls_and_sevens():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        RollEvent(player="Alice", d1=3, d2=4),   # 7
        RollEvent(player="Alice", d1=2, d2=3),   # 5
        RollEvent(player="Bob",   d1=6, d2=6),   # 12
        RollEvent(player="Bob",   d1=3, d2=4),   # 7
    ]
    results = [_result(e) for e in events]
    rep = build_report(events, results, cm, final_vp={"RED": 0, "BLUE": 0})

    assert rep.roll_histogram[7] == 2
    assert rep.roll_histogram[5] == 1
    assert rep.roll_histogram[12] == 1
    assert rep.players["RED"].rolls == 2
    assert rep.players["RED"].sevens == 1
    assert rep.players["BLUE"].rolls == 2
    assert rep.players["BLUE"].sevens == 1


def test_build_report_winner_and_final_vp():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [GameOverEvent(winner="Alice")]
    rep = build_report(
        events, [_result(events[0])], cm,
        final_vp={"RED": 10, "BLUE": 6},
    )
    assert rep.winner_username == "Alice"
    assert rep.winner_color == "RED"
    assert rep.final_vp == {"RED": 10, "BLUE": 6}


def test_build_report_no_game_over_has_no_winner():
    cm = ColorMap({"Alice": "RED"})
    rep = build_report([], [], cm, final_vp={"RED": 0})
    assert rep.winner_username is None
    assert rep.winner_color is None


def test_build_report_aggregates_produced_and_discarded():
    cm = ColorMap({"Alice": "RED"})
    events = [
        ProduceEvent(player="Alice", resources={"WOOD": 2, "BRICK": 1}),
        ProduceEvent(player="Alice", resources={"WOOD": 1}),
        DiscardEvent(player="Alice", resources={"WOOD": 2}),
    ]
    rep = build_report(events, [_result(e) for e in events], cm,
                       final_vp={"RED": 0})
    p = rep.players["RED"]
    assert p.produced == {"WOOD": 3, "BRICK": 1}
    assert p.produced_total == 4
    assert p.discarded == {"WOOD": 2}
    assert p.discarded_total == 2


def test_build_report_counts_builds_and_dev_cards():
    cm = ColorMap({"Alice": "RED"})
    events = [
        BuildEvent(player="Alice", piece="settlement"),
        BuildEvent(player="Alice", piece="road"),
        BuildEvent(player="Alice", piece="road"),
        BuildEvent(player="Alice", piece="city"),
        DevCardBuyEvent(player="Alice"),
        DevCardBuyEvent(player="Alice"),
        DevCardPlayEvent(player="Alice", card="knight"),
        DevCardPlayEvent(player="Alice", card="knight"),
        DevCardPlayEvent(player="Alice", card="year_of_plenty"),
    ]
    rep = build_report(events, [_result(e) for e in events], cm,
                       final_vp={"RED": 0})
    p = rep.players["RED"]
    assert p.builds["road"] == 2
    assert p.builds["settlement"] == 1
    assert p.builds["city"] == 1
    assert p.builds_total == 4
    assert p.dev_buys == 2
    assert p.dev_plays["knight"] == 2
    assert p.dev_plays["year_of_plenty"] == 1


def test_build_report_trades_bank_and_player():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        TradeCommitEvent(
            giver="Alice", receiver="BANK",
            gave={"WOOD": 4}, got={"WHEAT": 1},
        ),
        TradeCommitEvent(
            giver="Alice", receiver="Bob",
            gave={"WOOD": 1}, got={"WHEAT": 1},
        ),
    ]
    rep = build_report(events, [_result(e) for e in events], cm,
                       final_vp={"RED": 0, "BLUE": 0})
    assert rep.players["RED"].trades_bank == 1
    assert rep.players["RED"].trades_player == 1
    assert rep.players["BLUE"].trades_player == 1
    assert rep.players["BLUE"].trades_bank == 0


def test_build_report_steals_both_sides():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        StealEvent(thief="Alice", victim="Bob", resource="WOOD"),
        StealEvent(thief="Alice", victim="Bob", resource=None),
        StealEvent(thief="Bob", victim="Alice", resource="ORE"),
    ]
    rep = build_report(events, [_result(e) for e in events], cm,
                       final_vp={"RED": 0, "BLUE": 0})
    assert rep.players["RED"].steals_as_thief == 2
    assert rep.players["RED"].steals_as_victim == 1
    assert rep.players["BLUE"].steals_as_thief == 1
    assert rep.players["BLUE"].steals_as_victim == 2


def test_build_report_monopolies_and_vp():
    cm = ColorMap({"Alice": "RED"})
    events = [
        MonopolyStealEvent(player="Alice", resource="WHEAT", count=5),
        VPEvent(player="Alice", reason="largest_army", vp_delta=2),
    ]
    rep = build_report(events, [_result(e) for e in events], cm,
                       final_vp={"RED": 0})
    p = rep.players["RED"]
    assert p.monopolies == [("WHEAT", 5)]
    assert p.vp_awards == ["largest_army"]


def test_build_report_dispatch_counts():
    cm = ColorMap({"Alice": "RED"})
    ev = RollEvent(player="Alice", d1=1, d2=1)
    results = [
        _result(ev, "applied"),
        _result(ev, "applied"),
        _result(ev, "skipped"),
        _result(ev, "unhandled"),
        _result(ev, "error"),
    ]
    rep = build_report([ev], [results[0]], cm, final_vp={"RED": 0})
    # build_report uses dispatch_results list independently of events
    rep2 = build_report([ev], results, cm, final_vp={"RED": 0})
    assert rep2.dispatch_counts == {
        "applied": 2, "skipped": 1, "unhandled": 1, "error": 1,
    }


def test_build_report_timestamps_yield_duration():
    cm = ColorMap({"Alice": "RED"})
    rep = build_report(
        [], [], cm, final_vp={"RED": 0},
        timestamps=[1000.0, 1600.0, None, 1300.0],
    )
    assert rep.first_ts == 1000.0
    assert rep.last_ts == 1600.0


def test_format_report_renders_without_crashing():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        RollEvent(player="Alice", d1=3, d2=4),
        ProduceEvent(player="Alice", resources={"WOOD": 2}),
        BuildEvent(player="Alice", piece="settlement"),
        TradeCommitEvent(
            giver="Alice", receiver="Bob",
            gave={"WOOD": 1}, got={"WHEAT": 1},
        ),
        DevCardBuyEvent(player="Alice"),
        DevCardPlayEvent(player="Alice", card="knight"),
        StealEvent(thief="Alice", victim="Bob", resource="ORE"),
        VPEvent(player="Alice", reason="largest_army", vp_delta=2),
        GameOverEvent(winner="Alice"),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm,
        final_vp={"RED": 10, "BLUE": 4},
        timestamps=[1000.0 + i * 60 for i in range(len(events))],
        jsonl_path="/tmp/sample.jsonl",
    )
    out = format_report(rep)
    assert "CataanBot replay" in out
    assert "/tmp/sample.jsonl" in out
    assert "Alice" in out and "Bob" in out
    assert "Winner: Alice (RED) at 10 VP" in out
    assert "Dice histogram" in out
    assert "Per-player activity" in out
    assert "Parser / dispatcher quality" in out
    # Duration line only shows when timestamps are present.
    assert "Duration:" in out


def test_format_report_empty_log():
    cm = ColorMap()
    rep = build_report([], [], cm, final_vp={})
    out = format_report(rep)
    assert "no GameOverEvent" in out
    assert "(no rolls)" in out


def test_format_histogram_shows_expected_and_delta_once_enough_rolls():
    cm = ColorMap({"Alice": "RED"})
    # 24 rolls total — above the 12-roll threshold that gates the luck column.
    events = [RollEvent(player="Alice", d1=1, d2=1) for _ in range(24)]
    rep = build_report(events, [_result(e) for e in events], cm,
                       final_vp={"RED": 0})
    out = format_report(rep)
    # 2 has expectation 24/36 ≈ 0.67; we rolled 24 of them, delta +23.3.
    assert "exp  0.7" in out
    assert "+23.3" in out


def test_format_histogram_hides_luck_column_for_short_games():
    cm = ColorMap({"Alice": "RED"})
    events = [RollEvent(player="Alice", d1=3, d2=4)]  # only 1 roll
    rep = build_report(events, [_result(events[0])], cm,
                       final_vp={"RED": 0})
    out = format_report(rep)
    assert "exp" not in out


def test_trade_ledger_aggregates_resources_and_partners():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE", "Carol": "WHITE"})
    events = [
        TradeCommitEvent(
            giver="Alice", receiver="Bob",
            gave={"WOOD": 2}, got={"WHEAT": 1},
        ),
        TradeCommitEvent(
            giver="Alice", receiver="Carol",
            gave={"SHEEP": 1}, got={"ORE": 1},
        ),
        TradeCommitEvent(
            giver="Bob", receiver="Alice",
            gave={"BRICK": 1}, got={"WOOD": 1},
        ),
    ]
    rep = build_report(events, [_result(e) for e in events], cm,
                       final_vp={"RED": 0, "BLUE": 0, "WHITE": 0})
    alice = rep.players["RED"]
    assert alice.trades_player == 3
    # Trades 1+2 give; trade 3 she's on the receive side and sends WOOD back.
    assert alice.trade_gave == {"WOOD": 3, "SHEEP": 1}
    assert alice.trade_got == {"WHEAT": 1, "ORE": 1, "BRICK": 1}
    assert alice.trade_partners["BLUE"] == 2
    assert alice.trade_partners["WHITE"] == 1


def test_trade_ledger_tracks_bank_trade_shapes():
    cm = ColorMap({"Alice": "RED"})
    events = [
        TradeCommitEvent(
            giver="Alice", receiver="BANK",
            gave={"WOOD": 4}, got={"WHEAT": 1},
        ),
        TradeCommitEvent(
            giver="Alice", receiver="BANK",
            gave={"WOOD": 4}, got={"WHEAT": 1},
        ),
        TradeCommitEvent(
            giver="Alice", receiver="BANK",
            gave={"BRICK": 3}, got={"ORE": 1},
        ),
    ]
    rep = build_report(events, [_result(e) for e in events], cm,
                       final_vp={"RED": 0})
    alice = rep.players["RED"]
    assert alice.trades_bank == 3
    assert len(alice.bank_trades) == 3
    # Bank trades should NOT feed into player-trade gave/got totals.
    assert alice.trade_gave == {}
    assert alice.trade_got == {}


def test_format_report_renders_trade_ledger():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        TradeCommitEvent(
            giver="Alice", receiver="Bob",
            gave={"WOOD": 2}, got={"WHEAT": 1},
        ),
        TradeCommitEvent(
            giver="Alice", receiver="BANK",
            gave={"SHEEP": 4}, got={"ORE": 1},
        ),
        TradeCommitEvent(
            giver="Alice", receiver="BANK",
            gave={"SHEEP": 4}, got={"ORE": 1},
        ),
    ]
    rep = build_report(events, [_result(e) for e in events], cm,
                       final_vp={"RED": 0, "BLUE": 0})
    out = format_report(rep)
    assert "Trade ledger" in out
    # Partner line: Alice traded once with Bob (BLUE).
    assert "BLUE×1" in out
    # Duplicate bank-trade shape should coalesce with a ×2 suffix.
    assert "4xSHEEP→1xORE ×2" in out
    # Net flow: Alice gave 2xWOOD, got 1xWHEAT.
    assert "-2xWOOD" in out
    assert "+1xWHEAT" in out


def test_format_report_ledger_empty_when_no_trades():
    cm = ColorMap({"Alice": "RED"})
    events = [RollEvent(player="Alice", d1=3, d2=4)]
    rep = build_report(events, [_result(events[0])], cm,
                       final_vp={"RED": 0})
    out = format_report(rep)
    assert "(no trades in log)" in out


def test_known_flow_sources_and_sinks():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        ProduceEvent(player="Alice", resources={"WOOD": 3, "BRICK": 2}),
        TradeCommitEvent(
            giver="Alice", receiver="Bob",
            gave={"WOOD": 1}, got={"WHEAT": 1},
        ),
        DiscardEvent(player="Alice", resources={"BRICK": 1}),
        # A settlement costs WOOD+BRICK+SHEEP+WHEAT; should subtract from sinks.
        BuildEvent(player="Alice", piece="settlement"),
        DevCardBuyEvent(player="Alice"),  # SHEEP+WHEAT+ORE
        DevCardPlayEvent(
            player="Alice", card="year_of_plenty",
            resources={"ORE": 2},
        ),
        MonopolyStealEvent(player="Alice", resource="SHEEP", count=4),
        StealEvent(thief="Alice", victim="Bob", resource="BRICK"),
        StealEvent(thief="Bob", victim="Alice", resource="WHEAT"),
    ]
    rep = build_report(events, [_result(e) for e in events], cm,
                       final_vp={"RED": 0, "BLUE": 0})
    # Pull the private helper via the module to assert the math.
    from cataanbot.report import _known_flow
    alice = rep.players["RED"]
    sources, sinks, net = _known_flow(alice)
    # Sources: WOOD 3 from produce, BRICK 2 from produce + 1 from steal,
    #          WHEAT 1 from trade, SHEEP 4 from monopoly, ORE 2 from YoP.
    assert sources == {
        "WOOD": 3, "BRICK": 3, "SHEEP": 4, "WHEAT": 1, "ORE": 2,
    }
    # Sinks: WOOD 1 trade + 1 settle = 2; BRICK 1 discard + 1 settle = 2;
    #        SHEEP 1 settle + 1 dev = 2; WHEAT 1 settle + 1 dev + 1 steal = 3;
    #        ORE 1 dev.
    assert sinks == {
        "WOOD": 2, "BRICK": 2, "SHEEP": 2, "WHEAT": 3, "ORE": 1,
    }
    assert net == {
        "WOOD": 1, "BRICK": 1, "SHEEP": 2, "WHEAT": -2, "ORE": 1,
    }


def test_known_flow_unknown_steals_do_not_register():
    # Resource=None steals shouldn't touch steal_gained/lost — keeps us
    # honest about what's actually observable from the log.
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        StealEvent(thief="Alice", victim="Bob", resource=None),
    ]
    rep = build_report(events, [_result(events[0])], cm,
                       final_vp={"RED": 0, "BLUE": 0})
    assert rep.players["RED"].steal_gained == {}
    assert rep.players["BLUE"].steal_lost == {}
    # Count-level counters still bump.
    assert rep.players["RED"].steals_as_thief == 1
    assert rep.players["BLUE"].steals_as_victim == 1


def test_format_report_renders_known_flow():
    cm = ColorMap({"Alice": "RED"})
    events = [
        ProduceEvent(player="Alice", resources={"WOOD": 5}),
        BuildEvent(player="Alice", piece="road"),
    ]
    rep = build_report(events, [_result(e) for e in events], cm,
                       final_vp={"RED": 0})
    out = format_report(rep)
    assert "Known resource flow" in out
    # Alice: +5 WOOD produced, -1 WOOD road cost, -1 BRICK road cost.
    # The row should show "+4" under WOOD and "-1" under BRI.
    # Just check the line's shape is present.
    assert "Alice" in out
    assert "+4" in out and "-1" in out


def test_build_report_registers_winner_color():
    # Even if the winner never produced/rolled, GameOverEvent should
    # make sure they land in players/ so the scoreboard isn't blank.
    cm = ColorMap()
    events = [GameOverEvent(winner="Zoe")]
    rep = build_report(events, [_result(events[0])], cm,
                       final_vp={"RED": 10})
    assert rep.winner_username == "Zoe"
    assert rep.winner_color == "RED"
    assert "RED" in rep.players


def test_hand_dynamics_tracks_peak_and_vulnerable_events():
    cm = ColorMap({"Alice": "RED"})
    events = [
        ProduceEvent(player="Alice", resources={"WOOD": 4}),   # hand=4
        ProduceEvent(player="Alice", resources={"BRICK": 5}),  # hand=9 (8+)
        ProduceEvent(player="Alice", resources={"SHEEP": 1}),  # hand=10 (8+)
        DiscardEvent(player="Alice", resources={"WOOD": 4, "BRICK": 1}),
                                                               # hand=5
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    d = rep.hand_dynamics["RED"]
    assert d.peak_size == 10
    assert d.peak_event_index == 2
    # Two samples had hand ≥ 8: after event 1 (9 cards) and event 2 (10).
    assert d.vulnerable_events == 2
    assert d.final_drift == 0


def test_hand_dynamics_reports_drift_on_overdraft():
    cm = ColorMap({"Alice": "RED"})
    # Alice discards without ever producing — every discard underflows,
    # bumping drift. Hand never reaches 8, so vulnerable_events stays 0.
    events = [
        DiscardEvent(player="Alice", resources={"WOOD": 2}),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    d = rep.hand_dynamics["RED"]
    assert d.final_drift == 2
    assert d.vulnerable_events == 0
    assert d.peak_size == 0


def test_format_report_includes_hand_dynamics_section():
    cm = ColorMap({"Alice": "RED"})
    events = [
        ProduceEvent(player="Alice", resources={"WOOD": 8}),  # hand=8
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    out = format_report(rep)
    assert "Hand dynamics" in out
    # "Alice" appears in several sections; check the dynamics row has
    # the peak number (8) on the same line.
    dyn_line = next(
        ln for ln in out.splitlines()
        if "Alice" in ln and " 8 " in ln and "#0" in ln
    )
    assert dyn_line
