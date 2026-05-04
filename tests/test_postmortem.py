"""Smoke tests for the single-file HTML postmortem."""
from __future__ import annotations

from pathlib import Path

from catanbot.events import (
    BuildEvent, GameOverEvent, ProduceEvent, RollEvent, VPEvent,
)
from catanbot.live import ColorMap, DispatchResult
from catanbot.postmortem import render_postmortem_html


def _dr(event, status="applied"):
    return DispatchResult(event=event, status=status, message="")


def test_render_postmortem_html_writes_complete_file(tmp_path: Path):
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    events = [
        RollEvent(player="Alice", d1=3, d2=4),
        ProduceEvent(player="Alice", resources={"WOOD": 2}),
        BuildEvent(player="Alice", piece="settlement", vp_delta=1),
        RollEvent(player="Bob", d1=2, d2=3),
        ProduceEvent(player="Bob", resources={"WHEAT": 1}),
        VPEvent(player="Alice", reason="largest_army", vp_delta=2),
        GameOverEvent(winner="Alice"),
    ]
    results = [_dr(e) for e in events]
    timestamps = [float(i * 60) for i in range(len(events))]

    out = render_postmortem_html(
        events=events,
        dispatch_results=results,
        timestamps=timestamps,
        color_map=cm,
        final_vp={"RED": 10, "BLUE": 4},
        out_path=tmp_path / "pm.html",
        jsonl_path="tests/fixture.jsonl",
    )
    assert out.exists()
    body = out.read_text()
    assert body.startswith("<!doctype html>")
    assert body.count("<img ") == 4
    assert body.count("data:image/png;base64,") == 4
    # Report section included. v0.34.0 wrapped it in a styled card,
    # so the `<pre>` carries `class="report"` instead of being bare.
    assert "<pre class=\"report\"" in body
    assert "Winner: Alice" in body or "Alice" in body
    # jsonl path escaped into the source meta-row.
    assert "fixture.jsonl" in body


def test_render_postmortem_html_works_without_jsonl_path(tmp_path: Path):
    cm = ColorMap({"Alice": "RED"})
    events = [RollEvent(player="Alice", d1=4, d2=4)]
    out = render_postmortem_html(
        events=events,
        dispatch_results=[_dr(events[0])],
        timestamps=[None],
        color_map=cm,
        final_vp={"RED": 0},
        out_path=tmp_path / "pm.html",
    )
    assert out.exists()
    body = out.read_text()
    # v0.34.0 renders the source as "live game" when no jsonl_path is
    # supplied (live bridge runs don't have one). Old template used
    # "(unknown source)"; updated to match the new placeholder.
    assert "live game" in body
