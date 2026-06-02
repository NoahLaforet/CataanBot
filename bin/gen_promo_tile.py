#!/usr/bin/env python3
"""Generate the Chrome Web Store small promo tile (440x280).

A clean placeholder in the HUD palette: a Catan hex with a number
token on the left, the CatanBot wordmark and tagline on the right.
Writes docs/media/promo-tile-440x280.png. Replace with finished art
when there's time (see docs/STORE_LISTING.md).

Usage:  python3 bin/gen_promo_tile.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 440, 280
BG_TOP = (17, 21, 31)        # #11151f, HUD palette
BG_BOTTOM = (10, 14, 21)     # #0a0e15
GREEN = (74, 222, 128)       # --pos #4ade80
GREEN_DEEP = (22, 163, 74)   # #16a34a
LIGHT = (238, 241, 246)      # #eef1f6
DIM = (138, 147, 166)        # #8a93a6
TOKEN_BG = (235, 236, 240)
TOKEN_FG = (16, 20, 30)
PIP_RED = (239, 68, 68)      # #ef4444

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "media" / "promo-tile-440x280.png"

_FONT_CANDIDATES = {
    "bold": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "regular": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
}


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES[kind]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _vgradient(w: int, h: int, top, bottom) -> Image.Image:
    base = Image.new("RGB", (w, h), top)
    px = base.load()
    for y in range(h):
        t = y / max(1, h - 1)
        col = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = col
    return base


def _hexagon(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    # Pointy-top hexagon (vertex at top and bottom).
    pts = []
    for k in range(6):
        a = math.radians(-90 + 60 * k)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def main() -> None:
    img = _vgradient(W, H, BG_TOP, BG_BOTTOM).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # Subtle rounded border.
    draw.rounded_rectangle([2, 2, W - 3, H - 3], radius=14,
                           outline=(38, 44, 58), width=2)

    # Hex tile on the left.
    cx, cy, r = 104, 140, 76
    draw.polygon(_hexagon(cx, cy, r), fill=GREEN_DEEP, outline=GREEN)
    # Re-stroke the edge thicker.
    pts = _hexagon(cx, cy, r)
    draw.line(pts + [pts[0]], fill=GREEN, width=4, joint="curve")

    # Number token (a "8", the strongest roll).
    tr = 28
    draw.ellipse([cx - tr, cy - tr, cx + tr, cy + tr], fill=TOKEN_BG)
    num_font = _font("bold", 36)
    draw.text((cx, cy - 6), "8", font=num_font, fill=TOKEN_FG,
              anchor="mm")
    # Five pips under the number (8 = 5 dots).
    pip_y = cy + 17
    for i in range(5):
        px = cx - 16 + i * 8
        draw.ellipse([px - 1.6, pip_y - 1.6, px + 1.6, pip_y + 1.6],
                     fill=PIP_RED)

    # Wordmark + tagline on the right. Auto-shrink the wordmark so it
    # never clips against the right margin.
    left = 196
    right_margin = W - 22
    wsize = 44
    while wsize > 24:
        word_font = _font("bold", wsize)
        if draw.textlength("CatanBot", font=word_font) <= right_margin - left:
            break
        wsize -= 2
    draw.text((left, 104), "CatanBot", font=word_font, fill=LIGHT,
              anchor="lm")
    # Green accent underline.
    uw = min(draw.textlength("CatanBot", font=word_font), right_margin - left)
    draw.line([(left + 2, 132), (left + 2 + uw, 132)], fill=GREEN, width=3)
    tag_font = _font("regular", 15)
    draw.text((left + 2, 158), "live Catan advisor for colonist.io",
              font=tag_font, fill=DIM, anchor="lm")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(OUT, "PNG")
    print(f"wrote {OUT} ({W}x{H})")


if __name__ == "__main__":
    main()
