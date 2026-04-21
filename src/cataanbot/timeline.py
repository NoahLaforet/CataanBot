"""Per-event timeline extraction and PNG step-chart rendering.

Two timelines are built from the replay event stream:

* `build_vp_timeline` tracks *publicly-visible* VP — settlement / city
  builds (via `BuildEvent.vp_delta`) and largest-army / longest-road
  flips (via `VPEvent`, which handles the previous-holder debit).
  Hidden dev-card VP stays off this chart; those cards stay face-down
  until the winner is declared, so the line the chart draws is "what
  the table could see," which is the more useful narrative anyway.

* `build_production_timeline` tracks cumulative resource cards each
  player has collected from rolls (`ProduceEvent`). It's the economic
  counterpart to the VP timeline — who had the income lead and when
  the dice shifted the economy.

Both feed `_render_step_chart`, which handles canvas sizing, axis
rendering, step-plotted lines, and the right-hand legend. The two
public renderers (`render_vp_chart`, `render_production_chart`) are
thin adapters that package up their series data and chart config.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from cataanbot.events import BuildEvent, Event, ProduceEvent, VPEvent
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


@dataclass
class ProductionSample:
    """One point on the cumulative-production timeline.

    Same time semantics as `VpSample`. `cards` is total resource cards
    received from rolls (sum across WOOD/BRICK/SHEEP/WHEAT/ORE) — dev
    cards, trades, and monopoly/year-of-plenty grants are NOT counted
    here, since this chart is specifically about dice luck and placement
    quality, not overall card throughput.
    """
    t: float | None
    cards: dict[str, int]   # color → cumulative cards from rolls
    event_index: int


@dataclass
class HandSample:
    """One point on the hand-size timeline.

    `size` is each seated player's total inferred cards-in-hand (known
    + unknown bucket from hidden steals) at this event, as reconstructed
    by `hand_tracker`. Emitted every time any player's hand size
    changes.
    """
    t: float | None
    size: dict[str, int]    # color → total cards in hand
    event_index: int


def _first_ts(timestamps: list[float | None]) -> float | None:
    for ts in timestamps:
        if ts is not None:
            return ts
    return None


def _rel_t(ts: float | None, first: float | None) -> float | None:
    if ts is None or first is None:
        return None
    return ts - first


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
    first_ts = _first_ts(timestamps)

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
        samples.append(VpSample(
            t=_rel_t(ts, first_ts), vp=dict(vp), event_index=i,
        ))
    return samples


def build_production_timeline(
    events: list[Event],
    timestamps: list[float | None] | None,
    color_map: ColorMap,
) -> list[ProductionSample]:
    """Walk the event stream and emit a sample each time a player
    collects cards from a roll (`ProduceEvent`).

    The returned list always starts with a baseline at 0 cards so the
    chart grounds cleanly; values are cumulative across the game.
    """
    if timestamps is None:
        timestamps = [None] * len(events)
    first_ts = _first_ts(timestamps)

    cards: dict[str, int] = {c: 0 for c in color_map.as_dict().values()}
    samples: list[ProductionSample] = [
        ProductionSample(
            t=0.0 if first_ts is not None else None,
            cards=dict(cards), event_index=-1,
        ),
    ]

    for i, event in enumerate(events):
        if not isinstance(event, ProduceEvent):
            continue
        color = color_map.get(event.player)
        cards.setdefault(color, 0)
        gained = sum(event.resources.values())
        if gained == 0:
            continue
        cards[color] += gained
        ts = timestamps[i] if i < len(timestamps) else None
        samples.append(ProductionSample(
            t=_rel_t(ts, first_ts), cards=dict(cards), event_index=i,
        ))
    return samples


def build_hand_timeline(
    events: list[Event],
    timestamps: list[float | None] | None,
    color_map: ColorMap,
) -> list[HandSample]:
    """Walk the event stream and emit a sample each time any player's
    reconstructed hand size changes. Uses `hand_tracker.apply_event` as
    the source of truth so the timeline and the report's hand table
    can't drift apart.
    """
    from cataanbot.hand_tracker import apply_event as _apply_hand
    from cataanbot.hand_tracker import init_hands

    if timestamps is None:
        timestamps = [None] * len(events)
    first_ts = _first_ts(timestamps)

    hands = init_hands(color_map)
    def _snapshot() -> dict[str, int]:
        return {c: h.total for c, h in hands.items()}

    samples: list[HandSample] = [
        HandSample(
            t=0.0 if first_ts is not None else None,
            size=_snapshot(), event_index=-1,
        ),
    ]
    prev = _snapshot()
    for i, event in enumerate(events):
        if not _apply_hand(hands, event, color_map):
            continue
        now = _snapshot()
        if now == prev:
            continue  # applied but no hand-size movement
        prev = now
        ts = timestamps[i] if i < len(timestamps) else None
        samples.append(HandSample(
            t=_rel_t(ts, first_ts), size=dict(now), event_index=i,
        ))
    return samples


def _sample_x(t: float | None, event_index: int) -> float:
    """x-coord for a sample: minutes when timestamped, else event index."""
    return t / 60.0 if t is not None else float(max(event_index, 0))


def _vp_series(
    samples: list[VpSample], color: str,
) -> tuple[list[float], list[int]]:
    xs = [_sample_x(s.t, s.event_index) for s in samples]
    ys = [s.vp.get(color, 0) for s in samples]
    return xs, ys


def _production_series(
    samples: list[ProductionSample], color: str,
) -> tuple[list[float], list[int]]:
    xs = [_sample_x(s.t, s.event_index) for s in samples]
    ys = [s.cards.get(color, 0) for s in samples]
    return xs, ys


def _hand_series(
    samples: list[HandSample], color: str,
) -> tuple[list[float], list[int]]:
    xs = [_sample_x(s.t, s.event_index) for s in samples]
    ys = [s.size.get(color, 0) for s in samples]
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
    users_by_color = {c: u for u, c in color_map.as_dict().items()}
    seated = [c for c in _SEAT_ORDER if c in users_by_color]
    if not seated:
        raise ValueError("no seated players in color_map — nothing to chart")

    has_time = any(s.t is not None for s in samples)
    series = {c: _vp_series(samples, c) for c in seated}
    final_sample = samples[-1] if samples else None

    final_vp = {
        c: (final_sample.vp.get(c, 0) if final_sample else 0)
        for c in seated
    }
    final_max = max(final_vp.values(), default=0)
    max_y = float(max(11, final_max + 1))

    return _render_step_chart(
        seated=seated,
        users_by_color=users_by_color,
        series=series,
        has_time=has_time,
        max_y=max_y,
        y_tick_step=2,
        threshold=(10.0, "10 VP (win)", (180, 40, 40)),
        legend_heading="Final VP",
        legend_value=lambda c: str(final_vp[c]),
        title=title or "VP over time",
        footer=(
            "Public VP only — hidden dev-card VP stays off this chart "
            "until a winner is declared."
        ),
        out_path=out_path,
    )


def render_hand_chart(
    samples: list[HandSample],
    color_map: ColorMap,
    out_path: str | Path,
    title: str | None = None,
) -> Path:
    """Draw a PNG step chart of reconstructed hand size over time.

    One line per seated color. Dashed line at 7 cards marks the 7-roll
    discard threshold (you get forced to discard half when a 7 is
    rolled and you hold 8+). Excellent for spotting "this player was
    sitting on 8 cards all game and never got 7-hit" vs. "they dumped
    on every 7."
    """
    users_by_color = {c: u for u, c in color_map.as_dict().items()}
    seated = [c for c in _SEAT_ORDER if c in users_by_color]
    if not seated:
        raise ValueError("no seated players in color_map — nothing to chart")

    has_time = any(s.t is not None for s in samples)
    series = {c: _hand_series(samples, c) for c in seated}
    final_sample = samples[-1] if samples else None

    final_sizes = {
        c: (final_sample.size.get(c, 0) if final_sample else 0)
        for c in seated
    }
    peak = 0
    for _xs, ys in series.values():
        for y in ys:
            if y > peak:
                peak = y
    max_y = float(max(10, peak + 2))

    return _render_step_chart(
        seated=seated,
        users_by_color=users_by_color,
        series=series,
        has_time=has_time,
        max_y=max_y,
        y_tick_step=2,
        threshold=(7.0, "7+ cards → discard on 7", (180, 40, 40)),
        legend_heading="Final hand size",
        legend_value=lambda c: str(final_sizes[c]),
        title=title or "Hand size over time",
        footer=(
            "Reconstructed from the event stream — drift grows with "
            "hidden steals and setup-phase gaps."
        ),
        out_path=out_path,
    )


def render_production_chart(
    samples: list[ProductionSample],
    color_map: ColorMap,
    out_path: str | Path,
    title: str | None = None,
) -> Path:
    """Draw a PNG step chart of cumulative cards received from rolls.

    Tracks gross cards from `ProduceEvent` only — trades, monopolies,
    year-of-plenty, and dev-card buys are NOT counted. This makes it
    a reasonably clean read on dice-luck + placement quality.
    """
    users_by_color = {c: u for u, c in color_map.as_dict().items()}
    seated = [c for c in _SEAT_ORDER if c in users_by_color]
    if not seated:
        raise ValueError("no seated players in color_map — nothing to chart")

    has_time = any(s.t is not None for s in samples)
    series = {c: _production_series(samples, c) for c in seated}
    final_sample = samples[-1] if samples else None

    final_cards = {
        c: (final_sample.cards.get(c, 0) if final_sample else 0)
        for c in seated
    }
    final_max = max(final_cards.values(), default=0)
    # Round max_y up to the next multiple of 10 for a cleaner grid.
    max_y = float(max(10, ((final_max + 9) // 10) * 10))
    y_tick_step = max(5, int(max_y // 8))
    y_tick_step = int(round(y_tick_step / 5) * 5) or 5

    return _render_step_chart(
        seated=seated,
        users_by_color=users_by_color,
        series=series,
        has_time=has_time,
        max_y=max_y,
        y_tick_step=y_tick_step,
        threshold=None,
        legend_heading="Cards from rolls",
        legend_value=lambda c: str(final_cards[c]),
        title=title or "Cards received from rolls",
        footer=(
            "Roll production only — trades, monopolies, year-of-plenty, "
            "and dev-card buys are excluded."
        ),
        out_path=out_path,
    )


# ---------------------------------------------------------------------------
# Shared step-chart renderer
# ---------------------------------------------------------------------------

def _render_step_chart(
    *,
    seated: list[str],
    users_by_color: dict[str, str],
    series: dict[str, tuple[list[float], list[int]]],
    has_time: bool,
    max_y: float,
    y_tick_step: int,
    threshold: tuple[float, str, tuple[int, int, int]] | None,
    legend_heading: str,
    legend_value: Callable[[str], str],
    title: str,
    footer: str,
    out_path: str | Path,
) -> Path:
    from PIL import Image, ImageDraw
    from cataanbot.render import (
        PIECE_OUTLINE, PLAYER_COLORS, _load_font,
    )

    out_path = Path(out_path)
    title_font = _load_font(18)
    label_font = _load_font(13)
    legend_font = _load_font(14)

    # Measure every legend row so the canvas grows to fit the longest
    # label. Previously we hand-sized margin_r and clipped long names
    # when the label ran past the image's right edge.
    _measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    max_label_w = 0
    legend_labels: dict[str, str] = {}
    for color in seated:
        username = users_by_color[color]
        val = legend_value(color)
        label = (
            f"{username} ({color}) — {val}" if val
            else f"{username} ({color})"
        )
        legend_labels[color] = label
        w = int(_measure.textlength(label, font=legend_font))
        if w > max_label_w:
            max_label_w = w
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
    draw.text((margin_l, 16), title, font=title_font, fill=PIECE_OUTLINE)

    # --- axis ranges ------------------------------------------------------
    max_x = 1.0
    for xs, _ in series.values():
        for x in xs:
            if x > max_x:
                max_x = x
    # Pad x so the last sample isn't glued to the legend.
    max_x *= 1.02

    def xpx(x: float) -> float:
        return margin_l + (x / max_x) * plot_w

    def ypx(y: float) -> float:
        return margin_t + plot_h - (y / max_y) * plot_h

    # --- gridlines + y labels --------------------------------------------
    for y in range(0, int(max_y) + 1, y_tick_step):
        gy = ypx(y)
        draw.line([(margin_l, gy), (margin_l + plot_w, gy)],
                  fill=(215, 215, 210), width=1)
        draw.text((margin_l - 32, gy - 8), f"{y:>3}",
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

    # --- optional threshold line ----------------------------------------
    if threshold is not None:
        thr_y_val, thr_label, thr_color = threshold
        # Only draw if the threshold is actually on-plot; otherwise skip
        # to avoid a dashed line sitting on the top/bottom edge.
        if 0 <= thr_y_val <= max_y:
            thr_y = ypx(thr_y_val)
            _dashed_hline(
                draw, margin_l, margin_l + plot_w, thr_y,
                color=thr_color, width=2, dash=8, gap=6,
            )
            draw.text(
                (margin_l + 6, thr_y - 16), thr_label,
                font=label_font, fill=thr_color,
            )

    # --- per-color step lines --------------------------------------------
    for color in seated:
        xs, ys = series[color]
        rgb = PLAYER_COLORS.get(color, (120, 120, 120))
        outline = (40, 40, 40) if color == "WHITE" else PIECE_OUTLINE
        pts: list[tuple[float, float]] = []
        for i, (x, y) in enumerate(zip(xs, ys)):
            if i == 0:
                pts.append((xpx(x), ypx(y)))
                continue
            prev_y = ys[i - 1]
            pts.append((xpx(x), ypx(prev_y)))
            pts.append((xpx(x), ypx(y)))
        if ys:
            pts.append((xpx(max_x), ypx(ys[-1])))
        if color == "WHITE":
            _polyline(draw, pts, fill=outline, width=4)
        _polyline(draw, pts, fill=rgb, width=3)
        for x, y in zip(xs[1:], ys[1:]):
            cx, cy = xpx(x), ypx(y)
            draw.ellipse(
                [cx - 3, cy - 3, cx + 3, cy + 3],
                fill=rgb, outline=outline, width=1,
            )

    # --- legend ----------------------------------------------------------
    legend_x = margin_l + plot_w + 16
    legend_y = margin_t
    draw.text((legend_x, legend_y - 4), legend_heading,
              font=legend_font, fill=PIECE_OUTLINE)
    row_y = legend_y + 18
    for color in seated:
        rgb = PLAYER_COLORS.get(color, (120, 120, 120))
        outline = (40, 40, 40) if color == "WHITE" else PIECE_OUTLINE
        draw.rectangle(
            [legend_x, row_y + 3, legend_x + 16, row_y + 14],
            fill=rgb, outline=outline, width=1,
        )
        draw.text((legend_x + 22, row_y), legend_labels[color],
                  font=legend_font, fill=PIECE_OUTLINE)
        row_y += 22

    # --- footnote --------------------------------------------------------
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
