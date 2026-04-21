"""Tests for the per-event VP timeline + PNG chart."""
from __future__ import annotations

from pathlib import Path

from cataanbot.events import BuildEvent, ProduceEvent, RollEvent, VPEvent
from cataanbot.live import ColorMap
from cataanbot.timeline import (
    build_production_timeline,
    build_vp_timeline,
    render_production_chart,
    render_vp_chart,
)


def test_build_vp_timeline_has_zero_baseline():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    samples = build_vp_timeline([], None, cm)
    assert len(samples) == 1
    assert samples[0].event_index == -1
    assert samples[0].vp == {"RED": 0, "BLUE": 0}


def test_build_vp_timeline_tracks_build_vp_deltas():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        BuildEvent(player="Alice", piece="settlement", vp_delta=1),
        BuildEvent(player="Alice", piece="road", vp_delta=0),   # no sample
        BuildEvent(player="Bob",   piece="settlement", vp_delta=1),
        BuildEvent(player="Alice", piece="city",       vp_delta=1),
    ]
    samples = build_vp_timeline(events, None, cm)
    # baseline + 3 VP-changing events (road with vp_delta=0 is skipped).
    assert len(samples) == 4
    assert samples[-1].vp == {"RED": 2, "BLUE": 1}
    # Samples only emitted for the events that actually moved VP.
    assert [s.event_index for s in samples] == [-1, 0, 2, 3]


def test_build_vp_timeline_transfers_largest_army():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        VPEvent(player="Alice", reason="largest_army", vp_delta=2),
        # Bob steals it — Alice loses 2, Bob gains 2.
        VPEvent(player="Bob", reason="largest_army", vp_delta=2,
                previous_holder="Alice"),
    ]
    samples = build_vp_timeline(events, None, cm)
    assert samples[1].vp == {"RED": 2, "BLUE": 0}
    assert samples[-1].vp == {"RED": 0, "BLUE": 2}


def test_build_vp_timeline_uses_relative_timestamps():
    cm = ColorMap({"Alice": "RED"})
    events = [
        BuildEvent(player="Alice", piece="settlement", vp_delta=1),
        BuildEvent(player="Alice", piece="city", vp_delta=1),
    ]
    samples = build_vp_timeline(events, [1000.0, 1090.5], cm)
    # Baseline grounds at 0 when timestamps are present.
    assert samples[0].t == 0.0
    assert samples[1].t == 0.0          # first event is the zero reference
    assert samples[2].t == 90.5


def test_build_vp_timeline_no_timestamps_leaves_t_none():
    cm = ColorMap({"Alice": "RED"})
    events = [BuildEvent(player="Alice", piece="settlement", vp_delta=1)]
    samples = build_vp_timeline(events, None, cm)
    assert all(s.t is None for s in samples)


def test_render_vp_chart_writes_png(tmp_path: Path):
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        BuildEvent(player="Alice", piece="settlement", vp_delta=1),
        BuildEvent(player="Bob",   piece="settlement", vp_delta=1),
        BuildEvent(player="Alice", piece="city",       vp_delta=1),
        VPEvent(player="Alice", reason="largest_army", vp_delta=2),
    ]
    samples = build_vp_timeline(events, None, cm)
    out = render_vp_chart(samples, cm, tmp_path / "vp.png", title="T")
    assert out.exists()
    # PNG magic bytes — guards against silently writing an empty file.
    assert out.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert out.stat().st_size > 500


def test_render_vp_chart_handles_timestamps(tmp_path: Path):
    cm = ColorMap({"Alice": "RED"})
    events = [
        BuildEvent(player="Alice", piece="settlement", vp_delta=1),
        BuildEvent(player="Alice", piece="city",       vp_delta=1),
    ]
    samples = build_vp_timeline(events, [1000.0, 1600.0], cm)
    out = render_vp_chart(samples, cm, tmp_path / "vp.png")
    assert out.exists() and out.stat().st_size > 500


def test_render_vp_chart_rejects_empty_color_map(tmp_path: Path):
    cm = ColorMap({})
    samples = build_vp_timeline([], None, cm)
    try:
        render_vp_chart(samples, cm, tmp_path / "vp.png")
    except ValueError as exc:
        assert "no seated players" in str(exc)
    else:
        raise AssertionError("expected ValueError for empty color_map")


def test_build_vp_timeline_ignores_non_vp_events():
    cm = ColorMap({"Alice": "RED"})
    events = [
        RollEvent(player="Alice", d1=3, d2=4),
        RollEvent(player="Alice", d1=1, d2=2),
    ]
    samples = build_vp_timeline(events, None, cm)
    # Only the baseline survives.
    assert len(samples) == 1
    assert samples[0].event_index == -1


# ---------------------------------------------------------------------------
# Production timeline
# ---------------------------------------------------------------------------

def test_build_production_timeline_has_zero_baseline():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    samples = build_production_timeline([], None, cm)
    assert len(samples) == 1
    assert samples[0].cards == {"RED": 0, "BLUE": 0}


def test_build_production_timeline_sums_resources_cumulatively():
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        ProduceEvent(player="Alice", resources={"WOOD": 2, "BRICK": 1}),
        ProduceEvent(player="Bob",   resources={"WHEAT": 1}),
        ProduceEvent(player="Alice", resources={"ORE": 2}),
    ]
    samples = build_production_timeline(events, None, cm)
    assert len(samples) == 4  # baseline + 3
    assert samples[1].cards == {"RED": 3, "BLUE": 0}
    assert samples[2].cards == {"RED": 3, "BLUE": 1}
    assert samples[-1].cards == {"RED": 5, "BLUE": 1}


def test_build_production_timeline_skips_empty_produces():
    # A ProduceEvent with no resources shouldn't emit a sample — it
    # would just duplicate the previous row and pollute the chart.
    cm = ColorMap({"Alice": "RED"})
    events = [
        ProduceEvent(player="Alice", resources={}),
        ProduceEvent(player="Alice", resources={"WOOD": 1}),
    ]
    samples = build_production_timeline(events, None, cm)
    assert len(samples) == 2  # baseline + one real produce
    assert samples[-1].cards == {"RED": 1}


def test_build_production_timeline_ignores_non_produce_events():
    cm = ColorMap({"Alice": "RED"})
    events = [
        BuildEvent(player="Alice", piece="settlement", vp_delta=1),
        RollEvent(player="Alice", d1=3, d2=4),
    ]
    samples = build_production_timeline(events, None, cm)
    assert len(samples) == 1


def test_render_production_chart_writes_png(tmp_path: Path):
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        ProduceEvent(player="Alice", resources={"WOOD": 2}),
        ProduceEvent(player="Bob",   resources={"WHEAT": 3}),
        ProduceEvent(player="Alice", resources={"ORE": 1}),
    ]
    samples = build_production_timeline(events, [1000.0, 1100.0, 1250.0], cm)
    out = render_production_chart(samples, cm, tmp_path / "prod.png")
    assert out.exists()
    assert out.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert out.stat().st_size > 500
