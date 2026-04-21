"""PNG bar chart of actual vs expected roll counts.

The ASCII histogram in the `--report` output already shows per-value
delta against the 2d6 distribution; this renders the same data as a
graphic — one "ghost" outline bar at the expected count plus a filled
bar at the actual count, so you can read overshoot/shortfall at a
glance. 6 and 8 are tinted red (classic Catan "hot" numbers), 7 is
neutral grey (robber, not income), everything else is a muted blue.
"""
from __future__ import annotations

from pathlib import Path

from cataanbot.report import _DICE_PROBABILITY


# Per-value bar color. 6/8 hot red; 7 neutral grey; rest steel blue.
_BAR_COLOR: dict[int, tuple[int, int, int]] = {
    2:  (100, 130, 180),
    3:  (100, 130, 180),
    4:  (100, 130, 180),
    5:  (100, 130, 180),
    6:  (200, 60, 60),
    7:  (140, 140, 150),
    8:  (200, 60, 60),
    9:  (100, 130, 180),
    10: (100, 130, 180),
    11: (100, 130, 180),
    12: (100, 130, 180),
}


def _tint(rgb: tuple[int, int, int], amount: float = 0.55) -> tuple[int, int, int]:
    """Lighten toward white by `amount` (0 = unchanged, 1 = white)."""
    r, g, b = rgb
    return (
        int(r + (255 - r) * amount),
        int(g + (255 - g) * amount),
        int(b + (255 - b) * amount),
    )


def render_dice_histogram(
    roll_histogram: dict[int, int],
    out_path: str | Path,
    title: str | None = None,
) -> Path:
    """Draw actual-vs-expected roll counts as a PNG bar chart.

    `roll_histogram` is value → count (e.g. `report.roll_histogram`).
    Values with zero rolls still show their expected ghost bar so the
    whole 2-12 range is always visible.
    """
    from PIL import Image, ImageDraw
    from cataanbot.render import PIECE_OUTLINE, _load_font

    out_path = Path(out_path)
    title_font = _load_font(18)
    label_font = _load_font(13)
    legend_font = _load_font(13)
    value_font = _load_font(12)

    total = sum(roll_histogram.values())
    actuals = {v: roll_histogram.get(v, 0) for v in range(2, 13)}
    expecteds = {v: total * _DICE_PROBABILITY[v] for v in range(2, 13)}

    # y-max: whichever is bigger (actual or expected), with some headroom.
    peak = max(
        max(actuals.values(), default=0),
        max(expecteds.values(), default=0.0),
    )
    if peak <= 0:
        peak = 1.0
    # Round up to the next multiple of 2 (or 5 for big peaks) for a
    # clean gridline above the tallest bar.
    if peak <= 10:
        max_y = float(int(peak) + 2)
        y_tick_step = 2
    else:
        step = 5 if peak <= 40 else 10
        max_y = float(((int(peak) // step) + 1) * step)
        y_tick_step = step

    # --- canvas ---------------------------------------------------------
    W, H = 820, 440
    margin_l, margin_r = 56, 200
    margin_t, margin_b = 56, 72
    plot_w = W - margin_l - margin_r
    plot_h = H - margin_t - margin_b

    img = Image.new("RGB", (W, H), (245, 245, 240))
    draw = ImageDraw.Draw(img)
    if title is None:
        title = "Dice rolls — actual vs. expected"
    draw.text((margin_l, 16), title, font=title_font, fill=PIECE_OUTLINE)

    # --- y gridlines + labels ------------------------------------------
    def ypx(y: float) -> float:
        return margin_t + plot_h - (y / max_y) * plot_h

    for y in range(0, int(max_y) + 1, y_tick_step):
        gy = ypx(y)
        draw.line([(margin_l, gy), (margin_l + plot_w, gy)],
                  fill=(215, 215, 210), width=1)
        draw.text((margin_l - 32, gy - 8), f"{y:>3}",
                  font=label_font, fill=PIECE_OUTLINE)

    # --- axis frame -----------------------------------------------------
    draw.rectangle(
        [margin_l, margin_t, margin_l + plot_w, margin_t + plot_h],
        outline=PIECE_OUTLINE, width=2,
    )

    # --- bars -----------------------------------------------------------
    n_values = 11  # 2..12
    group_w = plot_w / n_values
    bar_w = group_w * 0.6
    for v in range(2, 13):
        group_left = margin_l + (v - 2) * group_w
        bar_left = group_left + (group_w - bar_w) / 2.0
        bar_right = bar_left + bar_w

        actual = actuals[v]
        expected = expecteds[v]
        color = _BAR_COLOR[v]
        ghost = _tint(color, 0.65)

        # Expected "ghost" outline. Drawn first so the filled actual
        # bar sits on top; when actual > expected, the actual bar
        # extends above the outline (easy overshoot read).
        if expected > 0:
            ey = ypx(expected)
            draw.rectangle(
                [bar_left, ey, bar_right, margin_t + plot_h],
                outline=ghost, width=2,
            )

        if actual > 0:
            ay = ypx(actual)
            draw.rectangle(
                [bar_left, ay, bar_right, margin_t + plot_h],
                fill=color,
            )

        # Value label under the axis.
        draw.text(
            (group_left + group_w / 2.0 - 5, margin_t + plot_h + 6),
            str(v), font=label_font, fill=PIECE_OUTLINE,
        )

        # Signed delta above the bar (only once enough rolls to be
        # meaningful — noise dominates otherwise).
        if total >= 12:
            delta = actual - expected
            label = f"{delta:+.1f}" if abs(delta) >= 0.1 else "±0"
            top_y = ypx(max(actual, expected))
            draw.text(
                (group_left + group_w / 2.0 - 12, max(top_y - 16, margin_t)),
                label, font=value_font,
                fill=(80, 80, 80) if abs(delta) < 1 else color,
            )

    # --- x-axis title ---------------------------------------------------
    draw.text(
        (margin_l + plot_w / 2.0 - 30, margin_t + plot_h + 26),
        "Roll value", font=label_font, fill=PIECE_OUTLINE,
    )

    # --- legend ---------------------------------------------------------
    legend_x = margin_l + plot_w + 16
    legend_y = margin_t
    draw.text((legend_x, legend_y - 4), f"{total} total rolls",
              font=legend_font, fill=PIECE_OUTLINE)

    # Sample swatch: filled = actual, outlined = expected.
    sample_color = (100, 130, 180)
    row_y = legend_y + 24
    draw.rectangle(
        [legend_x, row_y, legend_x + 16, row_y + 14],
        fill=sample_color,
    )
    draw.text((legend_x + 22, row_y), "Actual count",
              font=legend_font, fill=PIECE_OUTLINE)

    row_y += 22
    draw.rectangle(
        [legend_x, row_y, legend_x + 16, row_y + 14],
        outline=_tint(sample_color, 0.65), width=2,
    )
    draw.text((legend_x + 22, row_y), "Expected (2d6)",
              font=legend_font, fill=PIECE_OUTLINE)

    row_y += 30
    draw.text((legend_x, row_y), "Hot:", font=legend_font,
              fill=PIECE_OUTLINE)
    row_y += 18
    draw.rectangle(
        [legend_x, row_y, legend_x + 16, row_y + 14],
        fill=(200, 60, 60),
    )
    draw.text((legend_x + 22, row_y), "6, 8",
              font=legend_font, fill=PIECE_OUTLINE)

    row_y += 22
    draw.rectangle(
        [legend_x, row_y, legend_x + 16, row_y + 14],
        fill=(140, 140, 150),
    )
    draw.text((legend_x + 22, row_y), "7 (robber)",
              font=legend_font, fill=PIECE_OUTLINE)

    # --- footer ---------------------------------------------------------
    footer = (
        "Deltas above each bar show actual − expected counts "
        "(shown once ≥12 rolls)."
    )
    draw.text((margin_l, H - 22), footer,
              font=label_font, fill=(120, 118, 112))

    img.save(out_path)
    return out_path
