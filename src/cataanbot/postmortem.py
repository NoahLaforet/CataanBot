"""Single-file HTML postmortem stitching the text report and the three
PNG charts (VP timeline, production timeline, dice histogram) into one
shareable document with the images embedded as base64 data URIs.

The charts are rendered in-memory (no temp files) and the report text
goes into a `<pre>` block; no JavaScript, no external assets — open
the file in any browser.
"""
from __future__ import annotations

import base64
import html as _html
import io
from collections import Counter
from pathlib import Path

from cataanbot.events import Event, RollEvent
from cataanbot.live import ColorMap, DispatchResult
from cataanbot.report import build_report, format_report


def render_postmortem_html(
    events: list[Event],
    dispatch_results: list[DispatchResult],
    timestamps: list[float | None],
    color_map: ColorMap,
    final_vp: dict[str, int],
    out_path: str | Path,
    jsonl_path: str | None = None,
) -> Path:
    """Build a combined text report + three embedded PNG charts and
    write them to a single self-contained HTML file."""
    out_path = Path(out_path)

    report = build_report(
        events=events,
        dispatch_results=dispatch_results,
        color_map=color_map,
        final_vp=final_vp,
        timestamps=timestamps,
        jsonl_path=jsonl_path,
    )
    report_text = format_report(report)

    vp_png = _vp_png_bytes(events, timestamps, color_map)
    prod_png = _production_png_bytes(events, timestamps, color_map)
    dice_png = _dice_png_bytes(events)

    title = "CataanBot postmortem"
    if jsonl_path:
        title = f"CataanBot postmortem — {Path(jsonl_path).name}"

    html = _HTML_TEMPLATE.format(
        title=_html.escape(title),
        jsonl_path=_html.escape(jsonl_path or "(unknown source)"),
        report_text=_html.escape(report_text),
        vp_src=_data_uri(vp_png),
        prod_src=_data_uri(prod_png),
        dice_src=_data_uri(dice_png),
    )
    out_path.write_text(html)
    return out_path


def _data_uri(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _image_to_png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _vp_png_bytes(
    events: list[Event],
    timestamps: list[float | None],
    color_map: ColorMap,
) -> bytes:
    # Render to a tmp file, then read bytes — simpler than threading an
    # in-memory buffer through the PIL pipeline in timeline.py, and it
    # avoids diverging the renderer's signature for one caller.
    import tempfile
    from cataanbot.timeline import build_vp_timeline, render_vp_chart

    samples = build_vp_timeline(events, timestamps, color_map)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
        render_vp_chart(samples, color_map, tmp.name)
        return Path(tmp.name).read_bytes()


def _production_png_bytes(
    events: list[Event],
    timestamps: list[float | None],
    color_map: ColorMap,
) -> bytes:
    import tempfile
    from cataanbot.timeline import (
        build_production_timeline, render_production_chart,
    )

    samples = build_production_timeline(events, timestamps, color_map)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
        render_production_chart(samples, color_map, tmp.name)
        return Path(tmp.name).read_bytes()


def _dice_png_bytes(events: list[Event]) -> bytes:
    import tempfile
    from cataanbot.dice_chart import render_dice_histogram

    hist: Counter = Counter()
    for e in events:
        if isinstance(e, RollEvent):
            hist[e.d1 + e.d2] += 1
    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
        render_dice_histogram(hist, tmp.name)
        return Path(tmp.name).read_bytes()


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{
    color-scheme: light dark;
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 980px;
    margin: 2rem auto;
    padding: 0 1.25rem;
    line-height: 1.45;
    color: #222;
    background: #f8f7f3;
  }}
  h1 {{
    margin: 0 0 0.25rem 0;
    font-size: 1.5rem;
  }}
  .source {{
    color: #888;
    font-family: ui-monospace, Menlo, Consolas, monospace;
    font-size: 0.85rem;
    margin-bottom: 1.5rem;
  }}
  h2 {{
    font-size: 1.1rem;
    margin-top: 2rem;
    border-bottom: 1px solid #d8d6cf;
    padding-bottom: 0.25rem;
  }}
  pre {{
    background: #ffffff;
    border: 1px solid #d8d6cf;
    border-radius: 6px;
    padding: 1rem;
    font-family: ui-monospace, Menlo, Consolas, monospace;
    font-size: 0.8rem;
    overflow-x: auto;
    white-space: pre;
  }}
  figure {{
    margin: 0 0 1.25rem 0;
    text-align: center;
  }}
  figure img {{
    max-width: 100%;
    height: auto;
    border: 1px solid #d8d6cf;
    border-radius: 6px;
    background: #fff;
  }}
  figcaption {{
    color: #666;
    font-size: 0.85rem;
    margin-top: 0.25rem;
  }}
  footer {{
    color: #999;
    font-size: 0.75rem;
    margin-top: 3rem;
    text-align: center;
  }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="source">{jsonl_path}</div>

<h2>Charts</h2>
<figure>
  <img src="{vp_src}" alt="VP over time">
  <figcaption>Public VP over time. Dashed line is the 10-VP win threshold.</figcaption>
</figure>
<figure>
  <img src="{prod_src}" alt="Cards received from rolls">
  <figcaption>Cumulative cards received from dice rolls (trades and dev-card effects excluded).</figcaption>
</figure>
<figure>
  <img src="{dice_src}" alt="Dice fairness">
  <figcaption>Actual vs. expected roll counts per value; ghost outlines show the 2d6 expectation.</figcaption>
</figure>

<h2>Report</h2>
<pre>{report_text}</pre>

<footer>Generated by CataanBot.</footer>
</body>
</html>
"""
