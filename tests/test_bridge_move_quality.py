"""Bridge-side hook that grades self-builds (HUD principle #7).

Walks _record_self_build_quality through the cases that matter:
  * setup-phase placements → no entry (we don't grade openings)
  * post-setup self build matching the cached top rec → "!!"
  * post-setup self build off-list entirely → "??"
  * an opponent's build → no entry (only self gets graded)
  * empty cached recs → entry with classification=None (no_recs flag)
  * search_delta_gap math when both top + actual recs carry a delta
  * move_history capped at 30 so a long game can't unbounded-grow the snapshot
"""
from __future__ import annotations

from types import SimpleNamespace

from cataanbot.bridge import _record_self_build_quality
from cataanbot.events import BuildEvent
from cataanbot.live import ColorMap


def _make_state(*, recs, build_counts, self_name="Noah",
                opp_name="Bob"):
    """Build the smallest st dict that _record_self_build_quality reads.

    Sidesteps booting a real LiveGame off a colonist payload — that's
    covered by other tests. Here we just need .session.self_color_id +
    .session.player_names, .color_map, and .build_counts."""
    cm = ColorMap()
    cm.add(self_name, "RED")
    cm.add(opp_name, "BLUE")
    sess = SimpleNamespace(
        self_color_id=1,
        player_names={1: self_name, 2: opp_name},
    )
    game = SimpleNamespace(
        session=sess,
        color_map=cm,
        build_counts=build_counts,
    )
    return {
        "game": game,
        "last_recs_for_self": recs,
        "move_history": [],
        "total_rolls": 5,
    }


def test_setup_settlement_skipped():
    # build_counts post-state == 1 means this *was* the first opening
    # settlement. The hook must skip it — we don't grade openings.
    st = _make_state(
        recs=[{"kind": "settlement", "node_id": 7}],
        build_counts={"RED": {"settlement": 1, "city": 0, "road": 0}},
    )
    ev = BuildEvent(player="Noah", piece="settlement", node_id=7)
    _record_self_build_quality(st, ev)
    assert st["move_history"] == []


def test_setup_road_skipped():
    st = _make_state(
        recs=[{"kind": "road", "edge": (1, 2)}],
        build_counts={"RED": {"settlement": 2, "city": 0, "road": 2}},
    )
    ev = BuildEvent(player="Noah", piece="road", edge_nodes=(1, 2))
    _record_self_build_quality(st, ev)
    assert st["move_history"] == []


def test_post_setup_top_rec_grades_double_bang():
    # Third settlement → past setup. Matches cached top rec → "!!"
    st = _make_state(
        recs=[
            {"kind": "settlement", "node_id": 9},
            {"kind": "road", "edge": (1, 2)},
        ],
        build_counts={"RED": {"settlement": 3, "city": 0, "road": 2}},
    )
    ev = BuildEvent(player="Noah", piece="settlement", node_id=9)
    _record_self_build_quality(st, ev)

    assert len(st["move_history"]) == 1
    entry = st["move_history"][0]
    assert entry["classification"] == "!!"
    assert entry["rank"] == 1
    assert entry["piece"] == "settlement"
    assert entry["loc"] == 9


def test_post_setup_off_list_blunder():
    # Build doesn't match any rec → "??", rank=None.
    st = _make_state(
        recs=[{"kind": "settlement", "node_id": 9}],
        build_counts={"RED": {"settlement": 3, "city": 0, "road": 2}},
    )
    ev = BuildEvent(player="Noah", piece="settlement", node_id=42)
    _record_self_build_quality(st, ev)

    entry = st["move_history"][0]
    assert entry["classification"] == "??"
    assert entry["rank"] is None
    assert entry["top_kind"] == "settlement"
    assert entry["top_loc"] == 9


def test_opponent_build_not_graded():
    # Only self gets graded; an opponent's build must not append.
    st = _make_state(
        recs=[{"kind": "settlement", "node_id": 9}],
        build_counts={"BLUE": {"settlement": 3, "city": 0, "road": 2}},
    )
    ev = BuildEvent(player="Bob", piece="settlement", node_id=9)
    _record_self_build_quality(st, ev)
    assert st["move_history"] == []


def test_no_recs_yet_records_blank_entry():
    # Cached recs empty (e.g. build landed before a my_turn poll
    # cached anything). Hook still records the build but with
    # classification=None so the HUD can flag the missing comparison.
    st = _make_state(
        recs=[],
        build_counts={"RED": {"settlement": 3, "city": 0, "road": 2}},
    )
    ev = BuildEvent(player="Noah", piece="settlement", node_id=9)
    _record_self_build_quality(st, ev)

    entry = st["move_history"][0]
    assert entry["classification"] is None
    assert entry["rank"] is None
    assert entry["rec_count"] == 0


def test_search_delta_gap_computed():
    # When both top + actual recs carry a numeric search_delta, the
    # gap is (top - actual) — the EV the player left on the table.
    st = _make_state(
        recs=[
            {"kind": "settlement", "node_id": 1, "search_delta": 5.0},
            {"kind": "settlement", "node_id": 2, "search_delta": 3.5},
        ],
        build_counts={"RED": {"settlement": 3, "city": 0, "road": 2}},
    )
    ev = BuildEvent(player="Noah", piece="settlement", node_id=2)
    _record_self_build_quality(st, ev)

    entry = st["move_history"][0]
    assert entry["rank"] == 2
    assert entry["search_delta_gap"] == 1.5


def test_search_delta_gap_none_when_missing():
    # Recs without search_delta → gap stays None (don't fabricate a 0).
    st = _make_state(
        recs=[
            {"kind": "settlement", "node_id": 1},
            {"kind": "settlement", "node_id": 2},
        ],
        build_counts={"RED": {"settlement": 3, "city": 0, "road": 2}},
    )
    ev = BuildEvent(player="Noah", piece="settlement", node_id=2)
    _record_self_build_quality(st, ev)
    assert st["move_history"][0]["search_delta_gap"] is None


def test_move_history_capped_at_30():
    # Long game shouldn't grow move_history without bound — the cap
    # in _record_self_build_quality keeps the snapshot small. We
    # pre-fill 30 entries, push a 31st, expect the oldest to be
    # evicted.
    st = _make_state(
        recs=[{"kind": "settlement", "node_id": 1}],
        build_counts={"RED": {"settlement": 3, "city": 0, "road": 2}},
    )
    st["move_history"] = [
        {"ts": i, "marker": f"old-{i}"} for i in range(30)
    ]
    ev = BuildEvent(player="Noah", piece="settlement", node_id=1)
    _record_self_build_quality(st, ev)

    assert len(st["move_history"]) == 30
    # Oldest evicted; newest is the !!  entry we just appended.
    assert st["move_history"][0].get("marker") == "old-1"
    assert st["move_history"][-1]["classification"] == "!!"


def test_road_classified_with_unordered_edges():
    # Edge (3, 7) recorded by the WS diff in either order should
    # match the rec (3, 7). Earlier audit/live divergence on edge
    # order was the whole reason for the shared classifier.
    st = _make_state(
        recs=[{"kind": "road", "edge": (3, 7)}],
        build_counts={"RED": {"settlement": 2, "city": 0, "road": 3}},
    )
    ev = BuildEvent(player="Noah", piece="road", edge_nodes=(7, 3))
    _record_self_build_quality(st, ev)

    entry = st["move_history"][0]
    assert entry["classification"] == "!!"
    assert entry["loc"] == [7, 3]
