"""Tests for the dice-histogram PNG chart."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from cataanbot.dice_chart import render_dice_histogram


def _png_header(p: Path) -> bytes:
    return p.read_bytes()[:8]


def test_render_dice_histogram_writes_png(tmp_path: Path):
    # Small game — deltas should not be drawn (total < 12).
    hist = Counter({6: 2, 7: 1, 8: 1})
    out = render_dice_histogram(hist, tmp_path / "dice.png")
    assert out.exists()
    assert _png_header(out) == b"\x89PNG\r\n\x1a\n"
    assert out.stat().st_size > 500


def test_render_dice_histogram_handles_empty_histogram(tmp_path: Path):
    # No rolls at all — chart should still render (all ghost bars at 0).
    out = render_dice_histogram(Counter(), tmp_path / "dice.png")
    assert out.exists()
    assert _png_header(out) == b"\x89PNG\r\n\x1a\n"


def test_render_dice_histogram_large_game(tmp_path: Path):
    # Big enough to trigger the signed-delta labels.
    hist = Counter({
        2: 2, 3: 2, 4: 5, 5: 5, 6: 8, 7: 10,
        8: 9, 9: 6, 10: 4, 11: 3, 12: 2,
    })
    out = render_dice_histogram(
        hist, tmp_path / "dice.png", title="custom title",
    )
    assert out.exists()
    assert _png_header(out) == b"\x89PNG\r\n\x1a\n"
    # Bigger canvas data means bigger file; sanity floor.
    assert out.stat().st_size > 1000


def test_render_dice_histogram_accepts_plain_dict(tmp_path: Path):
    # Not just Counter — a plain dict should also work.
    hist = {7: 3, 6: 2}
    out = render_dice_histogram(hist, tmp_path / "dice.png")
    assert out.exists()
