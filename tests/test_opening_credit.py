"""Opponents' second-settlement opening resources resolve to KNOWN, not '?'.

Catan's setup rule hands a player one card per adjacent tile when they place
their SECOND settlement. Colonist never logs this as a roll payout, so the
bridge synthesizes a ProduceEvent for it (colonist_diff, opponents only) and
folds it into the particle model. Without that, opponents' opening hands read
as unknown '?' on the HUD.

These are deliberately capture-free: the WS-capture fixtures that the
colonist_diff integration tests rely on were lost to disk corruption on
2026-06-16, so this locks the mechanism at the model layer instead.
"""
from __future__ import annotations

from catanbot.events import ProduceEvent
from catanbot.live import ColorMap
from catanbot.opp_inference import OppHandModel


def _model_with_two_seats():
    cm = ColorMap()
    me = cm.get("Alice")     # self
    opp = cm.get("Bob")      # opponent
    return OppHandModel(colors=[me, opp], self_color=me), cm, me, opp


def test_opening_produce_resolves_to_known_minimums():
    """An opponent's opening haul pins per-resource minimums, so the snapshot
    reports zero unknown mass (unknown = total - sum(minimums))."""
    model, cm, _me, opp = _model_with_two_seats()
    haul = {"WHEAT": 1, "ORE": 1, "SHEEP": 1}
    model.apply(ProduceEvent(player="Bob", resources=haul), cm)

    bel = model.beliefs(opp)
    mins = {r: bel[r].minimum for r in bel}
    assert mins["WHEAT"] >= 1
    assert mins["ORE"] >= 1
    assert mins["SHEEP"] >= 1
    # Three known cards out of a three-card hand => no '?' on the HUD.
    assert sum(mins.values()) >= 3


def test_no_produce_leaves_opening_hand_unknown():
    """Sanity floor: with no opening produce folded in, the opponent has no
    pinned minimums, which is exactly the '?' state we fix above."""
    model, _cm, _me, opp = _model_with_two_seats()
    bel = model.beliefs(opp)
    assert sum(b.minimum for b in bel.values()) == 0
