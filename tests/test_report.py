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
