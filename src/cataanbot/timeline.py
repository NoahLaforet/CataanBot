"""Per-event VP timeline extraction and PNG chart rendering.

Given the event stream and per-event timestamps captured during replay,
`build_vp_timeline` walks the stream and emits a sample every time any
player's *publicly-visible* VP changes — from settlement / city builds
(via `BuildEvent.vp_delta`) and largest-army / longest-road flips (via
`VPEvent`, which handles the previous-holder debit).

`render_vp_chart` rasterises the resulting samples into a PNG step chart
with one colored line per seated player, a dashed 10-VP win line, a
minute-axis when timestamps are present, and a right-hand legend
showing each player's final VP.

Hidden dev-card VP is *not* reconstructed here — those stay face-down
until the game ends, so the line the chart draws is "what the table
could see," which is the more useful narrative anyway. The winner will
typically appear to end at 8-10 visible VP and the tracker-side
final_vp covers the reveal separately.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cataanbot.events import BuildEvent, Event, VPEvent
from cataanbot.live import ColorMap

if TYPE_CHECKING:
    from PIL.Image import Image  # noqa: F401


# Order seats left-to-right / top-to-bottom everywhere in the project.
_SEAT_ORDER = ("RED", "BLUE", "WHITE", "ORANGE")


@dataclass
class VpSample:
    """One point on the VP timeline.

    `t` is seconds from the first event's timestamp; None when the JSONL
    didn't carry timestamps for the relevant events (in which case callers
    should fall back to `event_index` for the x-axis).
    """
    t: float | None
    vp: dict[str, int]      # color → visible VP at this sample
    event_index: int        # position in the input `events` list


def build_vp_timeline(
    events: list[Event],
    timestamps: list[float | None] | None,
    color_map: ColorMap,
) -> list[VpSample]:
    """Walk the event stream and emit a sample each time VP changes.

    The returned list always starts with a baseline (t=0, all colors 0)
    so the chart grounds at zero even if the log's first VP-changing
    event is several minutes in.
    """
    if timestamps is None:
        timestamps = [None] * len(events)
    first_ts: float | None = None
    for ts in timestamps:
        if ts is not None:
            first_ts = ts
            break

    vp: dict[str, int] = {c: 0 for c in color_map.as_dict().values()}
    samples: list[VpSample] = [
        VpSample(t=0.0 if first_ts is not None else None,
                 vp=dict(vp), event_index=-1),
    ]

    for i, event in enumerate(events):
        ts = timestamps[i] if i < len(timestamps) else None
        changed = False
        if isinstance(event, BuildEvent) and event.vp_delta:
            color = color_map.get(event.player)
            vp.setdefault(color, 0)
            vp[color] += event.vp_delta
            changed = True
        elif isinstance(event, VPEvent):
            color = color_map.get(event.player)
            vp.setdefault(color, 0)
            vp[color] += event.vp_delta
            if event.previous_holder:
                prev = color_map.get(event.previous_holder)
                vp.setdefault(prev, 0)
                vp[prev] -= event.vp_delta
            changed = True
        if not changed:
            continue
        t_rel: float | None
        if ts is not None and first_ts is not None:
            t_rel = ts - first_ts
        else:
            t_rel = None
        samples.append(VpSample(t=t_rel, vp=dict(vp), event_index=i))
    return samples


def _samples_xy(
    samples: list[VpSample], color: str,
) -> tuple[list[float], list[int]]:
    """Return parallel (x, vp) arrays for one color.

    x is minutes when timestamps are present, event_index otherwise.
    Colors missing from a sample's dict read as 0 (pre-seat).
    """
    xs: list[float] = []
    ys: list[int] = []
    for s in samples:
        x = s.t / 60.0 if s.t is not None else float(max(s.event_index, 0))
        xs.append(x)
        ys.append(s.vp.get(color, 0))
    return xs, ys


def render_vp_chart(
    samples: list[VpSample],
    color_map: ColorMap,
    out_path: str | Path,
    title: str | None = None,
) -> Path:
    """Draw a PNG step chart of VP over time.

    One line per seated color, in seat order (RED/BLUE/WHITE/ORANGE).
    A dashed horizontal line at 10 VP marks the win threshold. If any
    sample has a real timestamp the x-axis is minutes; otherwise it's
    event index.
    """
    from PIL import Image, ImageDraw
    from cataanbot.render import (
        PIECE_OUTLINE, PLAYER_COLORS, _load_font,
    )

    out_path = Path(out_path)

    users_by_color = {c: u for u, c in color_map.as_dict().items()}
    seated = [c for c in _SEAT_ORDER if c in users_by_color]
    if not seated:
        raise ValueError("no seated players in color_map — nothing to chart")

    has_time = any(s.t is not None for s in samples)

    title_font = _load_font(18)
    label_font = _load_font(13)
    legend_font = _load_font(14)

    # Legend sizing — measure the actual pixel width of every legend
    # row with PIL rather than guess at char widths (em-dash + final
    # VP at the end of a long username will otherwise clip off the
    # right edge of the canvas). Grow the canvas, not the margin,
    # so margin_r widening actually gains us room instead of just
    # shrinking the plot.
    final_sample = samples[-1] if samples else None
    _measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    max_label_w = 0
    for color in seated:
        username = users_by_color[color]
        final = final_sample.vp.get(color, 0) if final_sample else 0
        w = _measure.textlength(
            f"{username} ({color}) — {final}", font=legend_font,
        )
        if w > max_label_w:
            max_label_w = int(w)
    # 22px swatch-plus-gap on the left, 16px breathing room on the right.
    legend_px = 22 + max_label_w + 16
    margin_l, margin_r = 64, max(200, legend_px)
    margin_t, margin_b = 56, 56
    plot_w = 680
    plot_h = 328
    W = margin_l + plot_w + margin_r
    H = margin_t + plot_h + margin_b

    img = Image.new("RGB", (W, H), (245, 245, 240))
    draw = ImageDraw.Draw(img)

    if title is None:
        title = "VP over time"
    draw.text((margin_l, 16), title, font=title_font, fill=PIECE_OUTLINE)

    # --- axis ranges ------------------------------------------------------
    max_x = max(
        (s.t / 60.0 if s.t is not None else float(max(s.event_index, 0))
         for s in samples),
        default=1.0,
    )
    if max_x <= 0:
        max_x = 1.0
    # Pad x so the last point isn't glued to the legend.
    max_x *= 1.02

    final_max_vp = max(
        (max(s.vp.values()) if s.vp else 0 for s in samples),
        default=0,
    )
    max_y = max(11, final_max_vp + 1)

    def xpx(x: float) -> float:
        return margin_l + (x / max_x) * plot_w

    def ypx(y: float) -> float:
        return margin_t + plot_h - (y / max_y) * plot_h

    # --- gridlines + y labels --------------------------------------------
    for y in range(0, int(max_y) + 1, 2):
        gy = ypx(y)
        draw.line([(margin_l, gy), (margin_l + plot_w, gy)],
                  fill=(215, 215, 210), width=1)
        draw.text((margin_l - 28, gy - 8), f"{y:>2}",
                  font=label_font, fill=PIECE_OUTLINE)

    # --- x-axis ticks -----------------------------------------------------
    if has_time:
        tick_step = _nice_minute_step(max_x)
        t = 0.0
        while t <= max_x + 1e-6:
            gx = xpx(t)
            draw.line([(gx, margin_t), (gx, margin_t + plot_h)],
                      fill=(230, 230, 225), width=1)
            label = f"{int(t)}m" if abs(t - int(t)) < 1e-6 else f"{t:.1f}m"
            draw.text((gx - 10, margin_t + plot_h + 6), label,
                      font=label_font, fill=PIECE_OUTLINE)
            t += tick_step
    else:
        # No timestamps — label every 20 events or so.
        step = max(1, int(math.ceil(max_x / 8)))
        x = 0
        while x <= max_x + 1e-6:
            gx = xpx(x)
            draw.line([(gx, margin_t), (gx, margin_t + plot_h)],
                      fill=(230, 230, 225), width=1)
            draw.text((gx - 10, margin_t + plot_h + 6), f"#{int(x)}",
                      font=label_font, fill=PIECE_OUTLINE)
            x += step

    # Axis frame (drawn after the gridlines so it sits on top).
    draw.rectangle(
        [margin_l, margin_t, margin_l + plot_w, margin_t + plot_h],
        outline=PIECE_OUTLINE, width=2,
    )

    # --- 10-VP win line (dashed) -----------------------------------------
    # Label the line inside the plot area to leave the right margin
    # clean for the legend.
    win_y = ypx(10)
    _dashed_hline(
        draw,
        margin_l, margin_l + plot_w,
        win_y,
        color=(180, 40, 40), width=2, dash=8, gap=6,
    )
    draw.text(
        (margin_l + 6, win_y - 16), "10 VP (win)",
        font=label_font, fill=(180, 40, 40),
    )

    # --- per-color step lines --------------------------------------------
    # Draw in a stable order so overlapping lines are deterministic.
    for color in seated:
        xs, ys = _samples_xy(samples, color)
        rgb = PLAYER_COLORS.get(color, (120, 120, 120))
        outline = (
            (40, 40, 40) if color == "WHITE" else PIECE_OUTLINE
        )
        # Step plot — horizontal segment then a vertical jump at each
        # sample i > 0.
        pts: list[tuple[float, float]] = []
        for i, (x, y) in enumerate(zip(xs, ys)):
            if i == 0:
                pts.append((xpx(x), ypx(y)))
                continue
            # Move horizontally to new x at previous y.
            prev_y = ys[i - 1]
            pts.append((xpx(x), ypx(prev_y)))
            # Then vertical jump to new y.
            pts.append((xpx(x), ypx(y)))
        # Extend the final value out to the right edge so the line doesn't
        # appear to end early.
        pts.append((xpx(max_x), ypx(ys[-1])))
        # Outline pass for WHITE visibility.
        if color == "WHITE":
            _polyline(draw, pts, fill=outline, width=4)
        _polyline(draw, pts, fill=rgb, width=3)
        # Markers at each sample.
        for x, y in zip(xs[1:], ys[1:]):
            cx, cy = xpx(x), ypx(y)
            draw.ellipse(
                [cx - 3, cy - 3, cx + 3, cy + 3],
                fill=rgb, outline=outline, width=1,
            )

    # --- legend ----------------------------------------------------------
    legend_x = margin_l + plot_w + 16
    legend_y = margin_t
    draw.text((legend_x, legend_y - 4), "Final VP",
              font=legend_font, fill=PIECE_OUTLINE)
    row_y = legend_y + 18
    for color in seated:
        username = users_by_color[color]
        final = final_sample.vp.get(color, 0) if final_sample else 0
        rgb = PLAYER_COLORS.get(color, (120, 120, 120))
        outline = (40, 40, 40) if color == "WHITE" else PIECE_OUTLINE
        draw.rectangle(
            [legend_x, row_y + 3, legend_x + 16, row_y + 14],
            fill=rgb, outline=outline, width=1,
        )
        label = f"{username} ({color}) — {final}"
        draw.text((legend_x + 22, row_y), label,
                  font=legend_font, fill=PIECE_OUTLINE)
        row_y += 22

    # --- footnote --------------------------------------------------------
    footer = (
        "Public VP only — hidden dev-card VP stays off this chart "
        "until a winner is declared."
    )
    draw.text((margin_l, H - 22), footer,
              font=label_font, fill=(120, 118, 112))

    img.save(out_path)
    return out_path


# ---------------------------------------------------------------------------
# PIL helpers
# ---------------------------------------------------------------------------

def _polyline(draw, pts, fill, width=1) -> None:
    """Draw a polyline — PIL has `draw.line` but its joins look jagged
    for 3px widths; stitching consecutive segments gives a cleaner read."""
    for p1, p2 in zip(pts, pts[1:]):
        draw.line([p1, p2], fill=fill, width=width)


def _dashed_hline(draw, x0, x1, y, color, width=1, dash=6, gap=4) -> None:
    x = x0
    while x < x1:
        seg_end = min(x + dash, x1)
        draw.line([(x, y), (seg_end, y)], fill=color, width=width)
        x = seg_end + gap


def _nice_minute_step(span_minutes: float) -> float:
    """Pick a human-friendly tick step for an x-axis of `span_minutes`."""
    if span_minutes <= 4:
        return 0.5
    if span_minutes <= 10:
        return 1.0
    if span_minutes <= 25:
        return 2.0
    if span_minutes <= 60:
        return 5.0
    return 10.0
