"""Chess-style move classification — shared by the live HUD and the
per-game audit. The thresholds need to stay locked: any drift here
silently changes how `audit_missed_recs.py` grades old captures and how
the live overlay grades the current game. Hence: explicit test per band
(top-1, top-3, top-6, top-10, beyond) plus the rec-matching edge cases
(unordered road edges, mismatched piece kinds, missing fields)."""
from __future__ import annotations

from cataanbot.events import BuildEvent
from cataanbot.move_quality import (
    classify_build_against_recs,
    classify_rank,
    find_rank,
    rec_matches_build,
)


def test_classify_rank_bands():
    # Locked thresholds: 1=!!, 2-3=!, 4-6=?!, 7-10=?, >10/None=??
    assert classify_rank(1) == "!!"
    assert classify_rank(2) == "!"
    assert classify_rank(3) == "!"
    assert classify_rank(4) == "?!"
    assert classify_rank(6) == "?!"
    assert classify_rank(7) == "?"
    assert classify_rank(10) == "?"
    assert classify_rank(11) == "??"
    assert classify_rank(None) == "??"


def test_rec_matches_settlement_by_node():
    rec = {"kind": "settlement", "node_id": 42}
    ev = BuildEvent(player="Noah", piece="settlement", node_id=42)
    assert rec_matches_build(rec, ev)

    ev_other = BuildEvent(player="Noah", piece="settlement", node_id=7)
    assert not rec_matches_build(rec, ev_other)


def test_rec_matches_city_by_node():
    rec = {"kind": "city", "node_id": 12}
    ev = BuildEvent(player="Noah", piece="city", node_id=12)
    assert rec_matches_build(rec, ev)


def test_rec_kind_must_match():
    # Same node but different piece kind = not a match. Otherwise the
    # classifier would grade a city upgrade as agreeing with a
    # settlement rec on the same node.
    rec = {"kind": "settlement", "node_id": 5}
    ev = BuildEvent(player="Noah", piece="city", node_id=5)
    assert not rec_matches_build(rec, ev)


def test_rec_matches_road_unordered_edges():
    # The recommender stores edges as (a, b); BuildEvent records them
    # as parsed from the WS diff which can land in either order. The
    # match is unordered so (3, 7) ↔ (7, 3) grades as the same road.
    rec = {"kind": "road", "edge": (3, 7)}
    ev_forward = BuildEvent(
        player="Noah", piece="road", edge_nodes=(3, 7))
    ev_reverse = BuildEvent(
        player="Noah", piece="road", edge_nodes=(7, 3))
    assert rec_matches_build(rec, ev_forward)
    assert rec_matches_build(rec, ev_reverse)


def test_rec_road_no_match_when_disjoint():
    rec = {"kind": "road", "edge": (3, 7)}
    ev = BuildEvent(player="Noah", piece="road", edge_nodes=(3, 8))
    assert not rec_matches_build(rec, ev)


def test_rec_road_missing_edge_field_is_no_match():
    # Defensive: malformed rec (no edge, edge length != 2) shouldn't
    # crash — should just grade as "doesn't match this event."
    assert not rec_matches_build({"kind": "road"}, BuildEvent(
        player="Noah", piece="road", edge_nodes=(1, 2)))
    assert not rec_matches_build(
        {"kind": "road", "edge": (1,)},
        BuildEvent(player="Noah", piece="road", edge_nodes=(1, 2)))


def test_rec_dev_card_and_trade_never_match_build():
    # dev_card / trade / propose_trade recs aren't directly comparable
    # to a BuildEvent — the BuildEvent is a settle/city/road action, so
    # those rec kinds always fail the match.
    ev = BuildEvent(player="Noah", piece="settlement", node_id=10)
    assert not rec_matches_build({"kind": "dev_card"}, ev)
    assert not rec_matches_build({"kind": "trade"}, ev)
    assert not rec_matches_build({"kind": "propose_trade"}, ev)


def test_find_rank_returns_first_match():
    # Recs are best-first, so first matching rec is the rank we want.
    # If a duplicate appeared later it shouldn't override.
    recs = [
        {"kind": "road", "edge": (1, 2)},
        {"kind": "settlement", "node_id": 5},
        {"kind": "settlement", "node_id": 10},
    ]
    ev = BuildEvent(player="Noah", piece="settlement", node_id=10)
    assert find_rank(recs, ev) == 3


def test_find_rank_none_when_not_in_list():
    recs = [{"kind": "settlement", "node_id": 5}]
    ev = BuildEvent(player="Noah", piece="settlement", node_id=99)
    assert find_rank(recs, ev) is None


def test_find_rank_none_on_empty_recs():
    ev = BuildEvent(player="Noah", piece="settlement", node_id=5)
    assert find_rank([], ev) is None


def test_classify_build_against_recs_top_pick():
    # Bot's top rec matches what the player did → "!!"
    recs = [
        {"kind": "settlement", "node_id": 7},
        {"kind": "road", "edge": (1, 2)},
    ]
    ev = BuildEvent(player="Noah", piece="settlement", node_id=7)
    classification, rank = classify_build_against_recs(ev, recs)
    assert classification == "!!"
    assert rank == 1


def test_classify_build_against_recs_blunder():
    # Move not in recs at all → "??", rank=None
    recs = [{"kind": "settlement", "node_id": 7}]
    ev = BuildEvent(player="Noah", piece="settlement", node_id=99)
    classification, rank = classify_build_against_recs(ev, recs)
    assert classification == "??"
    assert rank is None


def test_classify_build_against_recs_acceptable_alt():
    # 4th in the list is the player's pick → "?!" (acceptable but not
    # best). Catches off-by-one errors in the band boundary.
    recs = [
        {"kind": "settlement", "node_id": 1},
        {"kind": "settlement", "node_id": 2},
        {"kind": "settlement", "node_id": 3},
        {"kind": "settlement", "node_id": 4},
    ]
    ev = BuildEvent(player="Noah", piece="settlement", node_id=4)
    classification, rank = classify_build_against_recs(ev, recs)
    assert classification == "?!"
    assert rank == 4
