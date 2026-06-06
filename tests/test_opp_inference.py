"""Probabilistic opponent-hand inference (src/catanbot/opp_inference.py).

Drives the particle filter with the same structured events the live
bridge feeds it and asserts on the per-resource beliefs, the
affordability + hand-count pruning, and the reseed/refresh recovery.

Events carry colonist *usernames* (Me / Bob / Cara); the readout methods
are keyed by catanatron *color* (RED / BLUE / WHITE), the same way the
live bridge calls them. So fixtures build events by name and assert by
color: Me=RED, Bob=BLUE, Cara=WHITE.
"""
from __future__ import annotations

import pytest

from catanbot.events import (
    BuildEvent, DevCardBuyEvent, DevCardPlayEvent, DiscardEvent,
    HandSyncEvent, MonopolyStealEvent, ProduceEvent, StealEvent,
    TradeCommitEvent,
)
from catanbot.live import ColorMap
from catanbot.opp_inference import RESOURCES, OppHandModel

ME, BOB, CARA = "RED", "BLUE", "WHITE"


def make(mapping: dict[str, str], self_user: str | None = None):
    cm = ColorMap(mapping=mapping)
    colors = list(mapping.values())
    self_color = mapping[self_user] if self_user else None
    return OppHandModel(colors, self_color=self_color), cm


def total_expected(model: OppHandModel, color: str) -> float:
    return sum(model.expected_hand(color).values())


# -- no impossible state -------------------------------------------------

def test_no_negative_or_impossible_counts():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    m.apply(ProduceEvent(player="Bob", resources={"WOOD": 1}), cm)
    # Bob discards more wood than he could have — the floor clamps at 0,
    # never negative.
    m.apply(DiscardEvent(player="Bob", resources={"WOOD": 3}), cm)
    for r in RESOURCES:
        assert m.beliefs(BOB)[r].minimum >= 0
        assert m.expected_hand(BOB)[r] >= 0.0


# -- produce + reconcile -------------------------------------------------

def test_produce_then_reconcile_keeps_total():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    m.apply(ProduceEvent(player="Bob", resources={"WOOD": 2, "ORE": 1}), cm)
    m.reconcile({BOB: 3})
    assert m.hand_total(BOB) == 3
    assert m.beliefs(BOB)["WOOD"].minimum == 2
    assert m.beliefs(BOB)["ORE"].minimum == 1
    assert m.beliefs(BOB)["WOOD"].certain


# -- hidden steal branches into a probability ----------------------------

def test_hidden_steal_yields_min_plus_probability():
    m, cm = make({"Me": ME, "Bob": BOB, "Cara": CARA}, self_user="Me")
    # Bob holds 2 wood + 1 brick. Cara robs Bob with the type hidden.
    m.apply(ProduceEvent(player="Bob", resources={"WOOD": 2, "BRICK": 1}), cm)
    m.apply(StealEvent(thief="Cara", victim="Bob", resource=None), cm)
    m.reconcile({BOB: 2, CARA: 1})

    cara = m.beliefs(CARA)
    # 2/3 chance the stolen card was wood, 1/3 brick.
    assert cara["WOOD"].minimum == 0
    assert cara["WOOD"].maximum == 1
    assert cara["WOOD"].p_at_least_one == pytest.approx(2 / 3, abs=1e-6)
    assert cara["BRICK"].p_at_least_one == pytest.approx(1 / 3, abs=1e-6)
    assert total_expected(m, CARA) == pytest.approx(1.0, abs=1e-6)

    bob = m.beliefs(BOB)
    assert bob["WOOD"].minimum == 1          # at least one wood left for sure
    assert bob["WOOD"].p_above_min == pytest.approx(1 / 3, abs=1e-6)


def test_single_type_victim_steal_is_certain():
    m, cm = make({"Me": ME, "Bob": BOB, "Cara": CARA}, self_user="Me")
    m.apply(ProduceEvent(player="Bob", resources={"ORE": 2}), cm)
    m.apply(StealEvent(thief="Cara", victim="Bob", resource=None), cm)
    m.reconcile({BOB: 1, CARA: 1})
    # Bob only had ore, so the steal is fully resolved.
    assert m.beliefs(CARA)["ORE"].minimum == 1
    assert m.beliefs(CARA)["ORE"].certain
    assert m.beliefs(BOB)["ORE"].minimum == 1


# -- affordability prunes the ambiguity ----------------------------------

def test_build_resolves_hidden_steal_by_affordability():
    m, cm = make({"Me": ME, "Bob": BOB, "Cara": CARA}, self_user="Me")
    m.apply(ProduceEvent(player="Cara", resources={"BRICK": 1}), cm)
    m.apply(ProduceEvent(player="Bob", resources={"WOOD": 2, "SHEEP": 1}), cm)
    # Cara robs Bob (wood 2/3, sheep 1/3), then builds a road. Only the
    # branch where she stole wood can afford wood+brick, so the steal is
    # retroactively pinned to wood.
    m.apply(StealEvent(thief="Cara", victim="Bob", resource=None), cm)
    m.apply(BuildEvent(player="Cara", piece="road", paid=True), cm)
    m.reconcile({BOB: 2, CARA: 0})
    assert m.beliefs(BOB)["WOOD"].minimum == 1
    assert m.beliefs(BOB)["WOOD"].certain     # the stolen card was wood
    assert m.beliefs(BOB)["SHEEP"].minimum == 1
    assert m.hand_total(CARA) == 0


# -- hand-count cross-check ----------------------------------------------

def test_reconcile_drops_inconsistent_particles():
    m, cm = make({"Me": ME, "Bob": BOB, "Cara": CARA}, self_user="Me")
    m.apply(ProduceEvent(player="Bob", resources={"WOOD": 1, "BRICK": 1}), cm)
    m.apply(StealEvent(thief="Cara", victim="Bob", resource=None), cm)
    # Two branches: Bob {wood0,brick1} or {wood1,brick0}; both total 1.
    # Tell the model Bob actually shows 1 card — both still valid.
    m.reconcile({BOB: 1, CARA: 1})
    assert len(m.particles) == 2
    # Now an authoritative total that no branch satisfies twice in a row
    # forces a reseed rather than a permanent lie.
    m.reconcile({BOB: 4, CARA: 1})
    m.reconcile({BOB: 4, CARA: 1})
    assert m.hand_total(BOB) == 4
    assert sum(m.beliefs(BOB)[r].minimum for r in RESOURCES) <= 4


# -- monopoly ------------------------------------------------------------

def test_monopoly_sweeps_resource_and_credits_thief():
    m, cm = make({"Me": ME, "Bob": BOB, "Cara": CARA}, self_user="Me")
    m.apply(ProduceEvent(player="Bob", resources={"WHEAT": 2, "ORE": 1}), cm)
    m.apply(ProduceEvent(player="Cara", resources={"WHEAT": 3}), cm)
    # Cara monopolies wheat — 2 from Bob, 3 already hers, plus whatever the
    # viewer held. Log says she pulled 2 from opponents.
    m.apply(MonopolyStealEvent(player="Cara", resource="WHEAT", count=2), cm)
    assert m.beliefs(BOB)["WHEAT"].maximum == 0      # Bob wheat wiped
    assert m.beliefs(BOB)["ORE"].minimum == 1
    assert m.beliefs(CARA)["WHEAT"].minimum == 5     # 3 held + 2 taken


# -- year of plenty ------------------------------------------------------

def test_year_of_plenty_credits_two():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    m.apply(
        DevCardPlayEvent(
            player="Bob", card="year_of_plenty",
            resources={"ORE": 1, "WHEAT": 1}),
        cm)
    assert m.beliefs(BOB)["ORE"].minimum == 1
    assert m.beliefs(BOB)["WHEAT"].minimum == 1
    assert m.hand_total(BOB) == 0  # no reconcile yet; totals unknown
    m.reconcile({BOB: 2})
    assert m.hand_total(BOB) == 2


# -- road building (free placement does not debit) -----------------------

def test_road_building_free_roads_do_not_debit():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    m.apply(ProduceEvent(player="Bob", resources={"WOOD": 1, "BRICK": 1}), cm)
    # Road Building plays two FREE roads — colonist logs them as "placed",
    # so paid=False and the hand is untouched.
    m.apply(BuildEvent(player="Bob", piece="road", paid=False), cm)
    m.apply(BuildEvent(player="Bob", piece="road", paid=False), cm)
    assert m.beliefs(BOB)["WOOD"].minimum == 1
    assert m.beliefs(BOB)["BRICK"].minimum == 1


# -- dev-card buy --------------------------------------------------------

def test_dev_buy_debits_sheep_wheat_ore():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    m.apply(
        ProduceEvent(
            player="Bob",
            resources={"SHEEP": 1, "WHEAT": 1, "ORE": 1, "WOOD": 1}),
        cm)
    m.apply(DevCardBuyEvent(player="Bob"), cm)
    b = m.beliefs(BOB)
    assert b["SHEEP"].minimum == 0
    assert b["WHEAT"].minimum == 0
    assert b["ORE"].minimum == 0
    assert b["WOOD"].minimum == 1
    m.reconcile({BOB: 1})
    assert m.hand_total(BOB) == 1


# -- port vs bank trade ratios -------------------------------------------

def test_bank_trade_4_to_1():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    m.apply(ProduceEvent(player="Bob", resources={"WOOD": 4}), cm)
    # 4 wood to the bank for 1 ore.
    m.apply(
        TradeCommitEvent(
            giver="Bob", receiver="BANK",
            gave={"WOOD": 4}, got={"ORE": 1}),
        cm)
    b = m.beliefs(BOB)
    assert b["WOOD"].minimum == 0
    assert b["ORE"].minimum == 1
    m.reconcile({BOB: 1})
    assert m.hand_total(BOB) == 1


def test_port_trade_2_to_1():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    m.apply(ProduceEvent(player="Bob", resources={"SHEEP": 2}), cm)
    m.apply(
        TradeCommitEvent(
            giver="Bob", receiver="BANK",
            gave={"SHEEP": 2}, got={"BRICK": 1}),
        cm)
    b = m.beliefs(BOB)
    assert b["SHEEP"].minimum == 0
    assert b["BRICK"].minimum == 1


def test_player_trade_moves_both_hands():
    m, cm = make({"Me": ME, "Bob": BOB, "Cara": CARA}, self_user="Me")
    m.apply(ProduceEvent(player="Bob", resources={"WOOD": 2}), cm)
    m.apply(ProduceEvent(player="Cara", resources={"ORE": 1}), cm)
    # Bob gives 2 wood, gets 1 ore from Cara.
    m.apply(
        TradeCommitEvent(
            giver="Bob", receiver="Cara",
            gave={"WOOD": 2}, got={"ORE": 1}),
        cm)
    assert m.beliefs(BOB)["WOOD"].minimum == 0
    assert m.beliefs(BOB)["ORE"].minimum == 1
    assert m.beliefs(CARA)["ORE"].minimum == 0
    assert m.beliefs(CARA)["WOOD"].minimum == 2


# -- 7 discards ----------------------------------------------------------

def test_seven_discard_debits_known_cards():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    m.apply(
        ProduceEvent(
            player="Bob",
            resources={"WOOD": 3, "BRICK": 2, "SHEEP": 2, "ORE": 1}),
        cm)
    # 8 cards -> discards 4 (colonist reveals the icons in the log).
    m.apply(
        DiscardEvent(
            player="Bob", resources={"WOOD": 2, "BRICK": 1, "SHEEP": 1}),
        cm)
    m.reconcile({BOB: 4})
    assert m.hand_total(BOB) == 4
    b = m.beliefs(BOB)
    assert b["WOOD"].minimum == 1
    assert b["BRICK"].minimum == 1
    assert b["SHEEP"].minimum == 1
    assert b["ORE"].minimum == 1


# -- self attribution ("you stole" / "stole from you") -------------------

def test_self_steals_from_opponent_known_resource():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    m.apply(ProduceEvent(player="Bob", resources={"ORE": 2}), cm)
    # The viewer robs Bob and sees it was ore. Only Bob's side is tracked
    # here; the viewer's own hand comes from the authoritative self-sync.
    m.apply(StealEvent(thief="Me", victim="Bob", resource="ORE"), cm)
    m.reconcile({BOB: 1})
    assert m.beliefs(BOB)["ORE"].minimum == 1
    assert m.hand_total(BOB) == 1


def test_opponent_steals_from_self_credits_thief():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    # Bob robs the viewer and the log reveals it was wheat (the viewer is
    # the victim, so the type is public). Bob gains a known wheat.
    m.apply(StealEvent(thief="Bob", victim="Me", resource="WHEAT"), cm)
    m.reconcile({BOB: 1})
    assert m.beliefs(BOB)["WHEAT"].minimum == 1


def test_self_hand_is_exact_from_sync():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    m.set_self_hand(ME, {"WOOD": 3, "ORE": 2})
    b = m.beliefs(ME)
    assert b["WOOD"].minimum == 3 and b["WOOD"].certain
    assert b["ORE"].minimum == 2
    assert m.hand_total(ME) == 5


# -- robber steal expectation --------------------------------------------

def test_steal_expectation_tracks_composition():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    m.apply(
        ProduceEvent(player="Bob", resources={"WOOD": 3, "ORE": 1}), cm)
    m.reconcile({BOB: 4})
    ev = m.steal_expectation(BOB)
    assert ev["WOOD"] == pytest.approx(0.75, abs=1e-6)
    assert ev["ORE"] == pytest.approx(0.25, abs=1e-6)
    assert sum(ev.values()) == pytest.approx(1.0, abs=1e-6)


# -- reseed / refresh recovery -------------------------------------------

def test_reseed_recovers_from_total_desync():
    m, cm = make({"Me": ME, "Bob": BOB}, self_user="Me")
    m.apply(ProduceEvent(player="Bob", resources={"WOOD": 1}), cm)
    # Simulate a long missed stretch: colonist now says Bob holds 6 cards.
    # Two reconciles with no matching particle force a fresh, consistent
    # best-guess hand rather than a stuck wrong breakdown.
    m.reconcile({BOB: 6})
    m.reconcile({BOB: 6})
    assert m.hand_total(BOB) == 6
    assert sum(m.expected_hand(BOB).values()) == pytest.approx(6.0, abs=1e-6)
    for r in RESOURCES:
        assert m.expected_hand(BOB)[r] >= 0.0


def test_deck_cap_never_exceeds_nineteen():
    m, cm = make({"Me": ME, "Bob": BOB, "Cara": CARA}, self_user="Me")
    m.set_self_hand(ME, {"WOOD": 10})
    # Bob + Cara cannot collectively hold more than 9 more wood.
    m.reconcile({BOB: 15, CARA: 15})
    m.reconcile({BOB: 15, CARA: 15})
    in_play = (
        m.self_hand["WOOD"]
        + m.expected_hand(BOB)["WOOD"]
        + m.expected_hand(CARA)["WOOD"])
    assert in_play <= 19 + 1e-6


# -- end-to-end through the real DOM-log parser --------------------------

def _log(parts, self_name=None):
    return {"ts": 0, "text": "", "parts": parts, "names": [], "icons": [],
            "self": self_name}


def _name(n):
    return {"kind": "name", "name": n, "color": ""}


def _text(t):
    return {"kind": "text", "text": t}


def _icon(alt):
    return {"kind": "icon", "alt": alt}


def test_parser_feeds_model_end_to_end():
    """The same path the live bridge uses: raw colonist log payloads run
    through parse_event, then OppHandModel.apply. Confirms the model
    tracks real parsed events, not just hand-built dataclasses."""
    from catanbot.parser import parse_event

    m, cm = make({"Me": ME, "Bob": BOB, "Cara": CARA}, self_user="Me")
    # Bob collects 2 lumber + 1 grain on a roll.
    m.apply(parse_event(_log([
        _name("Bob"), _text("got"),
        _icon("Lumber"), _icon("Lumber"), _icon("Grain")])), cm)
    # Cara robs Bob, type hidden (third-party steal).
    m.apply(parse_event(_log([
        _name("Cara"), _text("stole  from"), _name("Bob"),
        _icon("Resource Card")])), cm)
    m.reconcile({BOB: 2, CARA: 1})

    assert m.is_synced()
    # Cara holds one of Bob's cards: 2/3 wood, 1/3 wheat.
    cara = m.beliefs(CARA)
    assert cara["WOOD"].p_at_least_one == pytest.approx(2 / 3, abs=1e-6)
    assert cara["WHEAT"].p_at_least_one == pytest.approx(1 / 3, abs=1e-6)
    assert m.steal_matrix[(CARA, BOB)] == 1


# -- steal matrix (Part 3 analytics) -------------------------------------

def test_steal_matrix_accumulates():
    m, cm = make({"Me": ME, "Bob": BOB, "Cara": CARA}, self_user="Me")
    m.apply(ProduceEvent(player="Bob", resources={"WOOD": 2}), cm)
    m.apply(StealEvent(thief="Cara", victim="Bob", resource=None), cm)
    m.apply(StealEvent(thief="Cara", victim="Bob", resource=None), cm)
    assert m.steal_matrix[(CARA, BOB)] == 2
