"""Single-file HTML postmortem stitching the text report and the four
PNG charts (VP timeline, production timeline, dice histogram, hand-size
timeline) into one shareable document with the images embedded as
base64 data URIs.

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

from catanbot.events import Event, RollEvent
from catanbot.live import ColorMap, DispatchResult
from catanbot.report import build_report, format_report


def render_postmortem_html(
    events: list[Event],
    dispatch_results: list[DispatchResult],
    timestamps: list[float | None],
    color_map: ColorMap,
    final_vp: dict[str, int],
    out_path: str | Path,
    jsonl_path: str | None = None,
    board_fingerprint: dict[str, object] | None = None,
) -> Path:
    """Build a combined text report + three embedded PNG charts and
    write them to a single self-contained HTML file.

    `board_fingerprint`, when provided, surfaces in the report header
    so postmortems on variant maps (Pond, Through-the-Desert, etc.) can
    be told apart at a glance from classic ones.
    """
    out_path = Path(out_path)

    report = build_report(
        events=events,
        dispatch_results=dispatch_results,
        color_map=color_map,
        final_vp=final_vp,
        timestamps=timestamps,
        jsonl_path=jsonl_path,
        board_fingerprint=board_fingerprint,
    )
    report_text = format_report(report)

    vp_png = _vp_png_bytes(events, timestamps, color_map)
    prod_png = _production_png_bytes(events, timestamps, color_map)
    dice_png = _dice_png_bytes(events)
    hand_png = _hand_png_bytes(events, timestamps, color_map)

    title = "CatanBot postmortem"
    if jsonl_path:
        title = f"CatanBot postmortem — {Path(jsonl_path).name}"

    # Hero header data — winner, VP table, and duration from timestamps.
    winner = ""
    if final_vp:
        winner = max(final_vp.items(), key=lambda kv: kv[1])[0]
    vp_rows_html = ""
    if final_vp:
        ranked = sorted(final_vp.items(), key=lambda kv: -kv[1])
        for name, vp in ranked:
            cls = "vp-row vp-winner" if name == winner else "vp-row"
            vp_rows_html += (
                f'<div class="{cls}">'
                f'<span class="vp-name">{_html.escape(name)}</span>'
                f'<span class="vp-score">{int(vp)}</span>'
                f'</div>'
            )
    duration_str = ""
    valid_ts = [t for t in (timestamps or []) if t]
    if len(valid_ts) >= 2:
        secs = max(valid_ts) - min(valid_ts)
        if secs > 0:
            mins = int(secs // 60)
            sec = int(secs % 60)
            duration_str = (
                f"{mins}m {sec:02d}s"
                if mins else f"{sec}s"
            )
    event_count = len(events or [])

    from catanbot.config import VP_TARGET
    html = _HTML_TEMPLATE.format(
        title=_html.escape(title),
        winner=_html.escape(winner or "—"),
        winner_class="hero-has-winner" if winner else "hero-no-winner",
        vp_rows=vp_rows_html or '<div class="vp-empty">no scoreboard data</div>',
        duration=_html.escape(duration_str or "—"),
        event_count=event_count,
        jsonl_path=_html.escape(jsonl_path or "live game"),
        report_text=_html.escape(report_text),
        vp_src=_data_uri(vp_png),
        prod_src=_data_uri(prod_png),
        dice_src=_data_uri(dice_png),
        hand_src=_data_uri(hand_png),
        vp_target=VP_TARGET,
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
    from catanbot.timeline import build_vp_timeline, render_vp_chart

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
    from catanbot.timeline import (
        build_production_timeline, render_production_chart,
    )

    samples = build_production_timeline(events, timestamps, color_map)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
        render_production_chart(samples, color_map, tmp.name)
        return Path(tmp.name).read_bytes()


def _hand_png_bytes(
    events: list[Event],
    timestamps: list[float | None],
    color_map: ColorMap,
) -> bytes:
    import tempfile
    from catanbot.timeline import build_hand_timeline, render_hand_chart

    samples = build_hand_timeline(events, timestamps, color_map)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
        render_hand_chart(samples, color_map, tmp.name)
        return Path(tmp.name).read_bytes()


def _dice_png_bytes(events: list[Event]) -> bytes:
    import tempfile
    from catanbot.dice_chart import render_dice_histogram

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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    color-scheme: dark;
    --bg-0: #0a0d14;
    --bg-1: #11151f;
    --bg-2: #161b29;
    --bg-3: #1f2434;
    --line: #232a3d;
    --line-strong: #2e364c;
    --fg: #eef1f6;
    --fg-mute: #a8b0bf;
    --fg-dim: #6b7180;
    --fg-label: #888ea1;
    --pos: #4ade80;
    --alert: #ef4444;
    --watch: #f59e0b;
    --info: #4aa7d4;
    --accent: #8b8ee6;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0;
    padding: 0;
    background: var(--bg-0);
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{
    max-width: 1080px;
    margin: 0 auto;
    padding: 2.5rem 1.5rem 4rem;
  }}

  /* ─── Hero header ──────────────────────────────────────── */
  .hero {{
    background: linear-gradient(135deg, var(--bg-1) 0%, var(--bg-2) 100%);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 1.75rem 2rem;
    margin-bottom: 2rem;
    box-shadow: 0 4px 24px rgba(0, 0, 0, 0.4);
  }}
  .hero-eyebrow {{
    color: var(--fg-label);
    font-size: 0.7rem;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    font-weight: 600;
    margin-bottom: 0.75rem;
  }}
  .hero-title {{
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    margin: 0 0 0.4rem 0;
    flex-wrap: wrap;
  }}
  .hero-title .label {{
    color: var(--fg-mute);
    font-size: 1rem;
    font-weight: 400;
  }}
  .hero-title .name {{
    color: var(--pos);
    font-size: 2rem;
    font-weight: 700;
    letter-spacing: -0.01em;
  }}
  .hero-no-winner .hero-title .name {{
    color: var(--fg-dim);
  }}
  .hero-meta {{
    display: flex;
    flex-wrap: wrap;
    gap: 1.5rem 2rem;
    margin-top: 1.25rem;
    color: var(--fg-mute);
    font-size: 0.85rem;
  }}
  .hero-meta-item .lbl {{
    display: block;
    color: var(--fg-label);
    font-size: 0.65rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    margin-bottom: 0.2rem;
  }}
  .hero-meta-item .val {{
    color: var(--fg);
    font-size: 1rem;
    font-weight: 500;
  }}

  /* ─── Scoreboard panel ─────────────────────────────────── */
  .scoreboard {{
    background: var(--bg-1);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1rem 1.25rem;
    margin-bottom: 2rem;
  }}
  .scoreboard h3 {{
    margin: 0 0 0.75rem 0;
    color: var(--fg-label);
    font-size: 0.7rem;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    font-weight: 600;
  }}
  .vp-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.45rem 0.6rem;
    border-radius: 6px;
    border-bottom: 1px solid var(--line);
  }}
  .vp-row:last-child {{ border-bottom: none; }}
  .vp-row.vp-winner {{
    background: rgba(74, 222, 128, 0.08);
    border-bottom-color: transparent;
  }}
  .vp-name {{
    font-family: ui-monospace, Menlo, Consolas, monospace;
    font-size: 0.9rem;
    color: var(--fg);
  }}
  .vp-winner .vp-name {{
    color: var(--pos);
    font-weight: 600;
  }}
  .vp-score {{
    font-family: ui-monospace, Menlo, Consolas, monospace;
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--fg);
  }}
  .vp-winner .vp-score {{ color: var(--pos); }}
  .vp-empty {{
    color: var(--fg-dim);
    font-style: italic;
    padding: 0.5rem;
  }}

  /* ─── Section header ───────────────────────────────────── */
  h2.section {{
    color: var(--fg);
    font-size: 1rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin: 2.5rem 0 1.25rem 0;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--line);
  }}

  /* ─── Chart grid ───────────────────────────────────────── */
  .charts {{
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 1.25rem;
  }}
  @media (max-width: 760px) {{
    .charts {{ grid-template-columns: 1fr; }}
  }}
  figure.chart {{
    margin: 0;
    background: var(--bg-1);
    border: 1px solid var(--line);
    border-radius: 12px;
    overflow: hidden;
    transition: border-color 0.15s ease;
  }}
  figure.chart:hover {{ border-color: var(--line-strong); }}
  figure.chart img {{
    display: block;
    width: 100%;
    height: auto;
    background: #fff;
  }}
  figure.chart figcaption {{
    color: var(--fg-mute);
    font-size: 0.8rem;
    padding: 0.7rem 1rem 0.85rem;
    border-top: 1px solid var(--line);
    background: var(--bg-2);
  }}

  /* ─── Report block ─────────────────────────────────────── */
  .report {{
    background: var(--bg-1);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1.25rem 1.5rem;
    font-family: ui-monospace, Menlo, Consolas, monospace;
    font-size: 0.78rem;
    line-height: 1.6;
    color: var(--fg);
    overflow-x: auto;
    white-space: pre;
    margin: 0;
  }}

  /* ─── Footer ───────────────────────────────────────────── */
  footer {{
    color: var(--fg-dim);
    font-size: 0.75rem;
    margin-top: 3rem;
    text-align: center;
    font-family: ui-monospace, Menlo, Consolas, monospace;
  }}
  footer a {{ color: var(--fg-mute); text-decoration: none; }}
  footer a:hover {{ color: var(--fg); }}
</style>
</head>
<body>
<div class="wrap">

  <header class="hero {winner_class}">
    <div class="hero-eyebrow">CatanBot postmortem</div>
    <div class="hero-title">
      <span class="label">winner</span>
      <span class="name">{winner}</span>
    </div>
    <div class="hero-meta">
      <div class="hero-meta-item">
        <span class="lbl">VP target</span>
        <span class="val">{vp_target}</span>
      </div>
      <div class="hero-meta-item">
        <span class="lbl">Duration</span>
        <span class="val">{duration}</span>
      </div>
      <div class="hero-meta-item">
        <span class="lbl">Events</span>
        <span class="val">{event_count}</span>
      </div>
      <div class="hero-meta-item">
        <span class="lbl">Source</span>
        <span class="val">{jsonl_path}</span>
      </div>
    </div>
  </header>

  <section class="scoreboard">
    <h3>Final scoreboard</h3>
    {vp_rows}
  </section>

  <h2 class="section">Charts</h2>
  <div class="charts">
    <figure class="chart">
      <img src="{vp_src}" alt="VP over time">
      <figcaption>Public VP over time. Dashed line marks the {vp_target}-VP win threshold.</figcaption>
    </figure>
    <figure class="chart">
      <img src="{prod_src}" alt="Cards received from rolls">
      <figcaption>Cumulative cards received from dice rolls (trades and dev-card effects excluded).</figcaption>
    </figure>
    <figure class="chart">
      <img src="{dice_src}" alt="Dice fairness">
      <figcaption>Actual vs. expected roll counts per value. Ghost outlines show the 2d6 expectation.</figcaption>
    </figure>
    <figure class="chart">
      <img src="{hand_src}" alt="Hand size over time">
      <figcaption>Reconstructed hand size per player. Dashed line marks the 7-card discard threshold.</figcaption>
    </figure>
  </div>

  <h2 class="section">Report</h2>
  <pre class="report">{report_text}</pre>

  <footer>Generated by CatanBot.</footer>
</div>
</body>
</html>
"""
