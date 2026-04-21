"""Roll statistics: histogram + per-color + per-tile production counts."""
from __future__ import annotations

import pytest

from cataanbot.stats import compute_stats, format_stats
from cataanbot.tracker import Tracker


@pytest.fixture
def tracker():
    return Tracker(seed=4242)


def test_empty_history_has_zero_rolls(tracker):
    s = compute_stats(tracker)
    assert sum(s["histogram"].values()) == 0
    assert s["total_rolls"] == 0


def test_histogram_counts_rolls(tracker):
    for n in (6, 6, 8, 7, 10):
        tracker.roll(n)
    s = compute_stats(tracker)
    assert s["histogram"][6] == 2
    assert s["histogram"][8] == 1
    assert s["histogram"][7] == 1
    assert s["histogram"][10] == 1
    assert s["total_rolls"] == 5


def test_per_color_matches_yielded_resources(tracker):
    """Resources produced via roll should match the per-color bucket."""
    m = tracker.game.state.board.map
    pick = None
    for _coord, tile in m.land_tiles.items():
        if tile.number is None or tile.resource is None:
            continue
        for node_id in tile.nodes.values():
            if node_id in m.land_nodes:
                pick = (tile, node_id)
                break
        if pick:
            break
    assert pick is not None
    tile, node_id = pick
    tracker.settle("RED", node_id)
    tracker.roll(tile.number)
    tracker.roll(tile.number)
    s = compute_stats(tracker)
    assert s["per_color_resources"]["RED"][tile.resource] == 2


def test_format_stats_renders(tracker):
    for n in (6, 8, 10):
        tracker.roll(n)
    out = format_stats(compute_stats(tracker))
    assert "histogram" in out.lower() or "rolls" in out.lower()
