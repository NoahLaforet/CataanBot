"""In-game dice-roll statistics, computed by replaying tracker history.

Every `roll` op in the tracker records only the dice sum, but because the
tracker already exposes seed + ordered op history, we can replay the game
step-by-step and know exactly which tiles produced and which colors received
cards at each roll. That gives us three useful views:

- A classic 2d6 goodness-of-fit histogram — was the dice god kind?
- Per-color resource totals actually delivered via dice (not trades).
- Per-tile production counts — revealing dead tiles the robber kept locked.

This is expensive-ish because we rebuild a fresh Game at `seed` and walk
the whole history, but game-sized histories (~200 ops) stay sub-second.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cataanbot.tracker import Tracker


# Probability of each dice sum with two fair six-sided dice. Multiplying by
# total roll count gives the expected frequency for chi-squared-ish eyeballing.
_EXPECTED_PROBABILITY = {
    2: 1 / 36, 3: 2 / 36, 4: 3 / 36, 5: 4 / 36, 6: 5 / 36,
    7: 6 / 36, 8: 5 / 36, 9: 4 / 36, 10: 3 / 36, 11: 2 / 36, 12: 1 / 36,
}


def compute_stats(tracker: "Tracker") -> dict[str, Any]:
    """Replay the tracker's history with instrumentation.

    Returns a dict with roll histogram, per-color resource totals from
    dice rolls (trades/gives excluded), and per-tile production counts.
    """
    from catanatron.state import yield_resources, RESOURCES
    from cataanbot.tracker import Tracker

    replay = Tracker(seed=tracker.seed)
    m = replay.game.state.board.map

    histogram = {n: 0 for n in range(2, 13)}
    per_color: dict[str, dict[str, int]] = {}
    per_tile: dict[tuple[int, int, int], dict[str, Any]] = {}
    for coord, tile in m.land_tiles.items():
        if tile.number is not None:
            per_tile[coord] = {
                "number": tile.number,
                "resource": tile.resource,
                "produced": 0,
                "robbed": 0,
            }

    for op in tracker.history:
        name = op["op"]
        args = op["args"]

        if name == "roll":
            n = args[0]
            histogram[n] += 1
            if n != 7:
                state = replay.game.state
                board = state.board
                robber = board.robber_coordinate
                # Per-tile: mark every tile with this number as "produced" if
                # not robbed AND has at least one adjacent building; bump a
                # separate "robbed" counter for visibility into wasted rolls.
                for coord, tile in m.land_tiles.items():
                    if tile.number != n:
                        continue
                    entry = per_tile.get(coord)
                    if entry is None:
                        continue
                    if coord == robber:
                        entry["robbed"] += 1
                        continue
                    if any(board.buildings.get(nid) is not None
                           for nid in tile.nodes.values()):
                        entry["produced"] += 1
                # Per-color: use yield_resources on the pre-roll state so the
                # bank-depletion cap is honored the same way the live roll did.
                payout, _ = yield_resources(
                    board, state.resource_freqdeck, n
                )
                for color, freqdeck in payout.items():
                    bucket = per_color.setdefault(
                        color.name, {r: 0 for r in RESOURCES}
                    )
                    for i, r in enumerate(RESOURCES):
                        bucket[r] += freqdeck[i]

        # Now progress replay by applying this op, mirroring Tracker._replay.
        if name == "settle":
            replay._apply_settle(args[0], args[1])
        elif name == "city":
            replay._apply_city(args[0], args[1])
        elif name == "road":
            replay._apply_road(args[0], args[1], args[2])
        elif name == "robber":
            replay._apply_robber(tuple(args))
        elif name == "roll":
            replay._apply_roll(args[0])
        elif name == "give":
            replay._apply_adjust(args[0], args[1], args[2], sign=+1)
        elif name == "take":
            replay._apply_adjust(args[0], args[1], args[2], sign=-1)
        elif name == "devbuy":
            replay._apply_devbuy(args[0], args[1])
        elif name == "devplay":
            replay._apply_devplay(args[0], args[1])
        elif name == "trade":
            replay._apply_trade(args[0], args[1], args[2],
                                args[3], args[4], args[5])
        elif name == "mtrade":
            replay._apply_mtrade(args[0], args[1], args[2], args[3])

    total = sum(histogram.values())
    return {
        "seed": tracker.seed,
        "total_rolls": total,
        "histogram": histogram,
        "expected_probability": dict(_EXPECTED_PROBABILITY),
        "per_color_resources": per_color,
        "per_tile_production": per_tile,
    }


def format_stats(stats: dict[str, Any], bar_width: int = 24) -> str:
    """Human-readable multi-section summary for the REPL/CLI."""
    total = stats["total_rolls"]
    histogram = stats["histogram"]
    expected_prob = stats["expected_probability"]

    lines = [
        f"Dice-roll stats — {total} rolls recorded (seed={stats['seed']}).",
        "",
    ]
    if total == 0:
        lines.append("  (no `roll` ops in history yet)")
        return "\n".join(lines)

    max_count = max(histogram.values()) or 1

    lines.append("Roll frequencies (bar = actual, ^ = expected):")
    header = f"  {'n':>2}  {'cnt':>3}  {'exp':>5}  {'Δ':>5}  distribution"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for n in range(2, 13):
        cnt = histogram[n]
        exp = expected_prob[n] * total
        delta = cnt - exp
        fill = int(round(cnt / max_count * bar_width))
        expected_col = int(round(exp / max_count * bar_width))
        bar_chars = []
        for i in range(bar_width):
            if i < fill:
                bar_chars.append("█")
            else:
                bar_chars.append("·")
        # Overlay expected marker — replaces the char at that column.
        if 0 <= expected_col < bar_width:
            marker = "^" if expected_col >= fill else "┃"
            bar_chars[expected_col] = marker
        bar = "".join(bar_chars)
        lines.append(
            f"  {n:>2}  {cnt:>3}  {exp:>5.1f}  {delta:>+5.1f}  {bar}"
        )

    per_color = stats["per_color_resources"]
    if per_color:
        lines.append("")
        lines.append("Resources delivered by dice (trades/gives excluded):")
        from catanatron.state import RESOURCES
        header = (f"  {'color':<7} "
                  + " ".join(f"{r.lower()[:5]:>5}" for r in RESOURCES)
                  + f"  {'total':>5}")
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 2))
        for color in ("RED", "BLUE", "WHITE", "ORANGE"):
            bucket = per_color.get(color)
            if bucket is None:
                continue
            cells = " ".join(f"{bucket[r]:>5}" for r in RESOURCES)
            total_cards = sum(bucket.values())
            lines.append(f"  {color:<7} {cells}  {total_cards:>5}")

    per_tile = stats["per_tile_production"]
    if per_tile:
        lines.append("")
        lines.append("Per-tile production (times actually paid out):")
        # Sort by produced descending, then by number (closer to 7 first).
        rows = sorted(
            per_tile.items(),
            key=lambda kv: (-kv[1]["produced"], abs(kv[1]["number"] - 7)),
        )
        header = (f"  {'tile':<10} {'coord':<12} {'#':>3} "
                  f"{'paid':>4} {'robbed':>6}")
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 2))
        for coord, info in rows[:12]:
            resource = info["resource"] or "DESERT"
            tile_str = f"{resource[:3]}{info['number']}"
            coord_str = f"({coord[0]},{coord[1]},{coord[2]})"
            lines.append(
                f"  {tile_str:<10} {coord_str:<12} {info['number']:>3} "
                f"{info['produced']:>4} {info['robbed']:>6}"
            )

    return "\n".join(lines)


def render_histogram(stats: dict[str, Any], out_path) -> "Path":
    """Write a PNG bar chart of roll frequencies vs. expected 2d6.

    Actual counts are solid bars; the theoretical expected value over the
    same total rolls is drawn as a thin horizontal line per column, so
    you can see at a glance which numbers ran hot or cold.
    """
    from pathlib import Path
    from PIL import Image, ImageDraw

    from cataanbot.render import _load_font, PIECE_OUTLINE, OCEAN

    out_path = Path(out_path)
    histogram = stats["histogram"]
    expected_prob = stats["expected_probability"]
    total = stats["total_rolls"]

    W, H = 720, 360
    margin_l, margin_r = 60, 24
    margin_t, margin_b = 36, 60
    plot_w = W - margin_l - margin_r
    plot_h = H - margin_t - margin_b

    img = Image.new("RGB", (W, H), (245, 245, 240))
    draw = ImageDraw.Draw(img)
    title_font = _load_font(18)
    label_font = _load_font(13)

    max_count = max(max(histogram.values()), 1)
    # Pad the y-axis a little so the expected marker never clips the top.
    y_max = max(max_count, max(p * total for p in expected_prob.values()) + 1)

    draw.text((margin_l, 8),
              f"Dice rolls ({total} total) — bars=actual, line=expected",
              font=title_font, fill=PIECE_OUTLINE)

    # Axis baseline.
    baseline_y = margin_t + plot_h
    draw.line([(margin_l, baseline_y), (margin_l + plot_w, baseline_y)],
              fill=PIECE_OUTLINE, width=2)

    # Gridlines + y-axis labels every ~max/4 counts.
    step = max(1, int(round(y_max / 4)))
    y_tick = 0
    while y_tick <= y_max:
        y = baseline_y - int(y_tick / y_max * plot_h)
        draw.line([(margin_l - 4, y), (margin_l + plot_w, y)],
                  fill=(210, 210, 210), width=1)
        draw.text((margin_l - 30, y - 8), str(y_tick),
                  font=label_font, fill=PIECE_OUTLINE)
        y_tick += step

    slot_w = plot_w / 11       # one slot per dice sum 2..12
    bar_w = slot_w * 0.7
    for i, n in enumerate(range(2, 13)):
        cx = margin_l + slot_w * (i + 0.5)
        x0 = cx - bar_w / 2
        x1 = cx + bar_w / 2
        count = histogram[n]
        expected_count = expected_prob[n] * total

        bar_top = baseline_y - int(count / y_max * plot_h)
        # 7 gets a distinct color since it's the robber roll, not a producer.
        bar_color = (200, 80, 70) if n == 7 else (70, 120, 180)
        draw.rectangle((x0, bar_top, x1, baseline_y),
                       fill=bar_color, outline=PIECE_OUTLINE, width=1)

        exp_y = baseline_y - int(expected_count / y_max * plot_h)
        draw.line([(x0 - 2, exp_y), (x1 + 2, exp_y)],
                  fill=(40, 40, 40), width=2)

        draw.text((cx - 6, baseline_y + 8), str(n),
                  font=label_font, fill=PIECE_OUTLINE)
        if count > 0:
            draw.text((cx - 6, bar_top - 16), str(count),
                      font=label_font, fill=PIECE_OUTLINE)

    img.save(out_path)
    return out_path
