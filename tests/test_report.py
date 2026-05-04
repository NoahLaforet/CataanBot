"""Tests for the offline replay postmortem report."""
from __future__ import annotations

from catanbot.events import (
    BuildEvent, DevCardBuyEvent, DevCardPlayEvent, DiscardEvent,
    GameOverEvent, InfoEvent, MonopolyStealEvent, NoStealEvent,
    ProduceEvent, RobberMoveEvent, RollEvent, StealEvent,
    TradeCommitEvent, VPEvent,
)
from catanbot.live import ColorMap, DispatchResult
from catanbot.report import build_report, format_report


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
    assert "CatanBot replay" in out
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
    from catanbot.report import _known_flow
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


def test_trade_impact_scores_lopsided_trade():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    # Alice has produced 10 wood already — wood is cheap for her.
    # Bob has produced nothing — everything is scarce for him.
    # So Alice giving wood for Bob's ore should look great for Alice
    # and terrible for Bob (from Bob's perspective).
    events = [
        ProduceEvent(player="Alice", resources={"WOOD": 10}),
        ProduceEvent(player="Bob", resources={"ORE": 1}),
        TradeCommitEvent(
            giver="Alice", receiver="Bob",
            gave={"WOOD": 1}, got={"ORE": 1},
        ),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm,
        final_vp={"RED": 0, "BLUE": 0},
    )
    assert len(rep.trade_impacts) == 1
    t = rep.trade_impacts[0]
    assert t.giver == "Alice"
    assert t.receiver == "Bob"
    # Alice got ORE (0 produced for her → marginal 2.0), gave WOOD
    # (10 produced → marginal ~0.095). Delta strongly positive.
    assert t.giver_delta > 1.5
    # Bob gave up ORE (1 produced → marginal ~0.67), got WOOD (0
    # produced → marginal 2.0). Delta also positive for Bob since
    # his scarcity profile inverts it.
    assert t.receiver_delta > 1.0


def test_trade_impact_skips_bank_trades():
    cm = ColorMap({"Alice": "RED"})
    events = [
        TradeCommitEvent(
            giver="Alice", receiver="BANK",
            gave={"WOOD": 4}, got={"ORE": 1},
        ),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    assert rep.trade_impacts == []


def test_format_report_includes_trade_quality_section():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        ProduceEvent(player="Alice", resources={"WOOD": 10}),
        TradeCommitEvent(
            giver="Alice", receiver="Bob",
            gave={"WOOD": 1}, got={"ORE": 1},
        ),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm,
        final_vp={"RED": 0, "BLUE": 0},
    )
    out = format_report(rep)
    assert "Trade quality" in out


def test_format_report_handles_no_trades_gracefully():
    cm = ColorMap({"Alice": "RED"})
    rep = build_report([], [], cm, final_vp={"RED": 0})
    out = format_report(rep)
    assert "Trade quality" in out
    assert "no player-to-player trades in log" in out


def test_seven_impact_captures_roller_discards_robber_steal():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        RollEvent(player="Alice", d1=3, d2=4),  # 7
        DiscardEvent(player="Bob", resources={"WOOD": 3, "WHEAT": 1}),
        RobberMoveEvent(player="Alice", tile_label="ore tile", prob=8),
        StealEvent(thief="Alice", victim="Bob", resource="ORE"),
        RollEvent(player="Bob", d1=1, d2=1),  # 2 — closes the 7 window
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm,
        final_vp={"RED": 0, "BLUE": 0},
    )
    assert len(rep.sevens) == 1
    s = rep.sevens[0]
    assert s.roller == "Alice"
    assert s.discards == {"Bob": 4}
    assert s.total_discards == 4
    assert s.robber_tile == "ore tile"
    assert s.robber_prob == 8
    assert s.steal_victim == "Bob"
    assert s.steal_resource == "ORE"


def test_seven_impact_no_steal_marks_victim_blank():
    cm = ColorMap({"Alice": "RED"})
    events = [
        RollEvent(player="Alice", d1=3, d2=4),
        RobberMoveEvent(player="Alice", tile_label="Desert", prob=None),
        NoStealEvent(),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    s = rep.sevens[0]
    assert s.steal_victim == ""
    assert s.steal_resource is None
    assert s.robber_prob is None


def test_seven_impact_closes_on_next_roll():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    # Two 7s back to back. Discards for the second 7 must NOT bleed into
    # the first's record.
    events = [
        RollEvent(player="Alice", d1=3, d2=4),
        DiscardEvent(player="Bob", resources={"WOOD": 2}),
        RollEvent(player="Bob", d1=3, d2=4),
        DiscardEvent(player="Alice", resources={"ORE": 5}),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm,
        final_vp={"RED": 0, "BLUE": 0},
    )
    assert len(rep.sevens) == 2
    assert rep.sevens[0].discards == {"Bob": 2}
    assert rep.sevens[1].discards == {"Alice": 5}


def test_format_report_includes_seven_impacts_section():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        RollEvent(player="Alice", d1=3, d2=4),
        DiscardEvent(player="Bob", resources={"WOOD": 3}),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm,
        final_vp={"RED": 0, "BLUE": 0},
    )
    out = format_report(rep)
    assert "7-roll impacts" in out
    # The row should name Bob with his discard count.
    assert "Bob 3" in out


def test_format_report_handles_no_sevens_gracefully():
    cm = ColorMap({"Alice": "RED"})
    rep = build_report([], [], cm, final_vp={"RED": 0})
    out = format_report(rep)
    assert "7-roll impacts" in out
    assert "no 7s in log" in out


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


# --- Move annotations ------------------------------------------------------


def test_move_annotations_brilliant_monopoly():
    cm = ColorMap({"Alice": "RED"})
    events = [
        DevCardPlayEvent(player="Alice", card="monopoly"),
        MonopolyStealEvent(player="Alice", resource="WHEAT", count=7),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    anns = [a for a in rep.move_annotations if a.move_kind == "monopoly"]
    assert len(anns) == 1
    assert anns[0].glyph == "!!"
    assert "7" in anns[0].summary
    assert anns[0].player == "Alice"


def test_move_annotations_whiffed_monopoly_is_blunder():
    cm = ColorMap({"Alice": "RED"})
    events = [
        MonopolyStealEvent(player="Alice", resource="WHEAT", count=0),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    ann = rep.move_annotations[0]
    assert ann.glyph == "??"
    assert "whiffed" in ann.note


def test_move_annotations_self_inflicted_seven_is_blunder():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        # Fatten Alice to 9 cards before she rolls a 7
        ProduceEvent(player="Alice", resources={"WOOD": 9}),
        RollEvent(player="Alice", d1=3, d2=4),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    seven_anns = [
        a for a in rep.move_annotations if a.move_kind == "rolled_7"
    ]
    assert len(seven_anns) == 1
    assert seven_anns[0].glyph == "??"
    assert seven_anns[0].player == "Alice"
    assert "9" in seven_anns[0].summary


def test_move_annotations_seven_against_fat_opp_is_brilliant():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        # Bob is fat with 9 cards; Alice rolls a 7 and stays clean
        ProduceEvent(player="Bob", resources={"WOOD": 9}),
        ProduceEvent(player="Alice", resources={"WOOD": 2}),
        RollEvent(player="Alice", d1=3, d2=4),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    seven_anns = [
        a for a in rep.move_annotations if a.move_kind == "rolled_7"
    ]
    assert len(seven_anns) == 1
    assert seven_anns[0].glyph == "!!"
    assert seven_anns[0].player == "Alice"


def test_move_annotations_mutually_lopsided_trade_flags_both_sides():
    """When both sides swap their abundance for their scarcity (classic
    'you have wood, I have wheat, let's fix both our gaps' trade), the
    marginal-value delta is positive for both players, so both get a
    brilliant-tier glyph. This is the common healthy-trade case."""
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        ProduceEvent(player="Alice", resources={"WHEAT": 10}),
        ProduceEvent(player="Bob", resources={"BRICK": 10}),
        TradeCommitEvent(
            giver="Alice", receiver="Bob",
            gave={"WHEAT": 1}, got={"BRICK": 1},
        ),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    trade_anns = [
        a for a in rep.move_annotations if a.move_kind == "trade"
    ]
    # Both sides flagged (both got a scarce resource for their abundance).
    assert len(trade_anns) == 2
    players = {a.player: a.glyph for a in trade_anns}
    assert players["Alice"] == "!!"
    assert players["Bob"] == "!!"


def test_move_annotations_bank_trade_not_flagged():
    cm = ColorMap({"Alice": "RED"})
    events = [
        ProduceEvent(player="Alice", resources={"WOOD": 4}),
        TradeCommitEvent(
            giver="Alice", receiver="BANK",
            gave={"WOOD": 4}, got={"WHEAT": 1},
        ),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    trade_anns = [
        a for a in rep.move_annotations if a.move_kind == "trade"
    ]
    assert trade_anns == []


def test_format_report_includes_move_annotations_section():
    cm = ColorMap({"Alice": "RED"})
    events = [
        MonopolyStealEvent(player="Alice", resource="ORE", count=6),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    out = format_report(rep)
    assert "Move annotations" in out
    assert "!!" in out
    assert "Monopoly" in out
    # Per-player glyph tally line on the player header (e.g.
    # "Alice (RED): !!:1") so the annotated-moves section reads as a
    # quick at-a-glance summary before drilling into per-move detail.
    assert "!!:1" in out


def test_move_annotations_knight_steals_from_fat_opp():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        ProduceEvent(player="Bob", resources={"WOOD": 9}),   # Bob fat
        DevCardPlayEvent(player="Alice", card="knight"),
        RobberMoveEvent(player="Alice", tile_label="wheat tile", prob=6),
        StealEvent(thief="Alice", victim="Bob", resource="WOOD"),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    knight_anns = [a for a in rep.move_annotations if a.move_kind == "knight"]
    assert len(knight_anns) == 1
    assert knight_anns[0].glyph == "!!"
    assert knight_anns[0].player == "Alice"
    assert "fat" in knight_anns[0].note.lower()


def test_move_annotations_knight_fizzles_on_empty_tile():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        DevCardPlayEvent(player="Alice", card="knight"),
        RobberMoveEvent(player="Alice", tile_label="ore tile", prob=8),
        NoStealEvent(),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    knight_anns = [a for a in rep.move_annotations if a.move_kind == "knight"]
    assert len(knight_anns) == 1
    assert knight_anns[0].glyph == "?!"


def test_move_annotations_fat_hand_victim_flagged_on_seven():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        # Bob carries 9 cards. Alice (roller) has 2 — not her blunder,
        # but Bob should have spent down. We flag Bob even though the
        # roll annotation is the "!!" brilliant case for Alice.
        ProduceEvent(player="Bob", resources={"WOOD": 9}),
        ProduceEvent(player="Alice", resources={"WOOD": 2}),
        RollEvent(player="Alice", d1=3, d2=4),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    fat_anns = [
        a for a in rep.move_annotations if a.move_kind == "fat_hand"
    ]
    assert len(fat_anns) == 1
    assert fat_anns[0].glyph == "?"
    assert fat_anns[0].player == "Bob"
    assert "9" in fat_anns[0].summary


def test_format_report_move_annotations_empty_graceful():
    cm = ColorMap({"Alice": "RED"})
    events = [
        RollEvent(player="Alice", d1=2, d2=3),  # a 5, nothing interesting
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    out = format_report(rep)
    assert "Move annotations" in out
    assert "no flagged moves" in out


def test_dev_card_timeline_records_buys_and_plays_with_rounds():
    """The dev-card timeline tags each buy/play with a round counter.

    Round 0 = before any roll lands; round n = after the nth RollEvent.
    Card type for opaque opponent buys is None (colonist hides it until
    play); play events always carry the type."""
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        DevCardBuyEvent(player="Alice"),         # round 0 — pre-roll buy
        RollEvent(player="Alice", d1=3, d2=2),   # advance to round 1
        DevCardBuyEvent(player="Bob"),           # round 1
        DevCardPlayEvent(player="Alice", card="knight"),
        RollEvent(player="Alice", d1=4, d2=3),   # advance to round 2 (a 7)
        DevCardPlayEvent(player="Bob", card="monopoly", resource="WOOD"),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm,
        final_vp={"RED": 0, "BLUE": 0},
    )
    tl = rep.dev_card_timeline
    assert len(tl) == 4
    assert tl[0].round == 0 and tl[0].action == "buy" and tl[0].player == "Alice"
    assert tl[1].round == 1 and tl[1].action == "buy" and tl[1].player == "Bob"
    assert (tl[2].round == 1 and tl[2].action == "play"
            and tl[2].card == "knight")
    assert (tl[3].round == 2 and tl[3].action == "play"
            and tl[3].card == "monopoly" and tl[3].player == "Bob")


def test_friendly_robber_info_event_flips_flag():
    """The InfoEvent that announces 'Friendly Robber active' flips the
    report flag, and the format_report meta block surfaces it. Other
    InfoEvents (rule reminders, bot-thinking notices) leave it off."""
    cm = ColorMap({"Alice": "RED"})
    events = [
        InfoEvent(text="Some bot thinking notice"),
        RollEvent(player="Alice", d1=3, d2=4),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    assert rep.friendly_robber_active is False
    assert "Friendly Robber" not in format_report(rep)

    events_fr = [
        InfoEvent(text="Friendly Robber is active, "
                       "victims with low VP are protected"),
        RollEvent(player="Alice", d1=3, d2=4),
    ]
    rep_fr = build_report(
        events_fr, [_result(e) for e in events_fr], cm, final_vp={"RED": 0},
    )
    assert rep_fr.friendly_robber_active is True
    out = format_report(rep_fr)
    assert "Friendly Robber active" in out


def test_board_fingerprint_renders_in_report_header():
    """When build_report is given a board_fingerprint dict the meta
    block prints a Board: line with label + counts. Legacy callers that
    pass nothing get a fully silent meta block."""
    cm = ColorMap({"Alice": "RED"})
    events = [RollEvent(player="Alice", d1=3, d2=4)]
    fp = {
        "label": "pond",
        "tile_count": 24, "corner_count": 76,
        "edge_count": 100, "port_count": 8,
    }
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
        board_fingerprint=fp,
    )
    assert rep.board_fingerprint == fp
    out = format_report(rep)
    assert "Board: pond" in out
    assert "24 tiles" in out
    assert "8 ports" in out

    # Without a fingerprint, no Board: line at all.
    rep2 = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    assert rep2.board_fingerprint is None
    assert "Board:" not in format_report(rep2)


def test_dev_card_timeline_format_renders_grouped_rounds():
    """format_report's dev card timeline section groups by round and
    shows player + verb + card type. Empty timeline shows the skip line."""
    cm = ColorMap({"Alice": "RED"})
    rep = build_report(
        [], [], cm, final_vp={"RED": 0},
    )
    assert "Dev card timeline" in format_report(rep)
    assert "no dev cards" in format_report(rep)

    events = [
        RollEvent(player="Alice", d1=3, d2=2),
        DevCardBuyEvent(player="Alice"),
        DevCardPlayEvent(player="Alice", card="knight"),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm, final_vp={"RED": 0},
    )
    out = format_report(rep)
    assert "R 1" in out
    assert "Alice" in out and "knight" in out


def test_longest_road_transfer_strips_previous_holder():
    """When colonist announces a Longest Road transfer (X took LR from
    Y), the previous holder must lose the award in the report. Without
    this strip, vp_awards is append-only — both players end up
    rendered with `longest_road`, which is impossible.

    Regression for the BrickdDaddy game where the postmortem credited
    both RED and BLUE with longest_road simultaneously."""
    from catanbot.events import VPEvent
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        # Alice gets longest road first.
        VPEvent(player="Alice", reason="longest_road", vp_delta=2),
        # Then Bob takes it from Alice.
        VPEvent(player="Bob", reason="longest_road", vp_delta=2,
                previous_holder="Alice"),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm,
        final_vp={"RED": 2, "BLUE": 4},
    )
    assert "longest_road" not in rep.players["RED"].vp_awards, (
        "Alice (RED) should no longer hold longest_road after Bob took it")
    assert "longest_road" in rep.players["BLUE"].vp_awards


def test_first_time_award_does_not_strip():
    """A first-time LA/LR award has no previous_holder — should not
    accidentally strip from anyone."""
    from catanbot.events import VPEvent
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        VPEvent(player="Alice", reason="largest_army", vp_delta=2,
                previous_holder=None),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm,
        final_vp={"RED": 2, "BLUE": 0},
    )
    assert "largest_army" in rep.players["RED"].vp_awards
    assert "largest_army" not in rep.players["BLUE"].vp_awards


def test_longest_road_transfer_strips_without_previous_holder():
    """Colonist's parser doesn't always populate previous_holder on a
    transfer — the announcement may just say "X has Longest Road"
    without naming who lost it. The report must still strip the prior
    holder by virtue of LR being a single-holder award.

    Regression for the 2026-04-30 opp postmortem where the
    parser missed previous_holder on the LR transfer and both players
    rendered with longest_road in the final scoreboard."""
    from catanbot.events import VPEvent
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        VPEvent(player="Alice", reason="longest_road", vp_delta=2),
        # Bob takes LR — but parser missed previous_holder.
        VPEvent(player="Bob", reason="longest_road", vp_delta=2,
                previous_holder=None),
    ]
    rep = build_report(
        events, [_result(e) for e in events], cm,
        final_vp={"RED": 2, "BLUE": 4},
    )
    assert "longest_road" not in rep.players["RED"].vp_awards, (
        "Alice (RED) must lose LR even when previous_holder is None")
    assert "longest_road" in rep.players["BLUE"].vp_awards


def test_scoreboard_flags_loser_who_had_enough_vp():
    """When a non-winner's tracker VP at game end is at/above the
    win target, the postmortem should flag it — they had enough but
    the opp closed first. Came out of Noah's 2026-05-03 vs an opp
    loss: he ended at 12 effective VP (target 10) but opp crossed
    on their own turn first.
    """
    from catanbot.report import build_report, format_report
    from catanbot.live import ColorMap
    from catanbot.events import GameOverEvent
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [GameOverEvent(winner="Bob")]
    rep = build_report(
        events, [_result(e) for e in events], cm,
        final_vp={"RED": 12, "BLUE": 10},
    )
    out = format_report(rep)
    # Loser RED at 12 VP (target 10 default) → flagged.
    assert "had enough VP, opp closed first" in out
    # Winner line should NOT carry the flag.
    blue_line = next(line for line in out.splitlines()
                     if "BLUE" in line and "VP" in line and "(Bob)" in line)
    assert "had enough VP" not in blue_line


def test_scoreboard_no_flag_when_loser_under_target():
    """Normal loss case (loser ended below target) — no flag, just
    the regular score line."""
    from catanbot.report import build_report, format_report
    from catanbot.live import ColorMap
    from catanbot.events import GameOverEvent
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [GameOverEvent(winner="Bob")]
    rep = build_report(
        events, [_result(e) for e in events], cm,
        final_vp={"RED": 7, "BLUE": 10},
    )
    out = format_report(rep)
    assert "had enough VP" not in out
