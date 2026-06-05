#!/usr/bin/env python3
"""Generate the menu-bar status-icon frames for the CatanBot tray app.

Derived from the brand art (extension/icons/icon-128.png). Run once; the
frames are committed under src/catanbot/tray/assets/ so the app has them
at runtime without a Pillow dependency. Re-run after changing the brand
icon.

Each frame is the brand "C" silhouette recolored from the source ALPHA
mask, so green (live) and gray (off) read cleanly instead of muddying a
saturation knob. Files are emitted at a 2x retina pixel size tagged with a
144 dpi resolution, so NSImage (what rumps loads) reports a ~22pt logical
size and renders crisp on Retina menu bars.

  running      : vivid brand green C (bridge up)
  stopped      : clean neutral gray C (bridge off)
  starting_a   : dim green pulse frame (bridge coming up)
  starting_b   : bright green pulse frame with a soft glow
  golive_1/2/3 : one-shot bloom played once when the bridge first goes live
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "extension" / "icons" / "icon-128.png"
OUT = ROOT / "src" / "catanbot" / "tray" / "assets"

PT = 22                 # logical menu-bar point size
SCALE = 2               # retina backing
PX = PT * SCALE         # physical pixels (44)
DPI = (72 * SCALE, 72 * SCALE)   # tag as 2x so NSImage logical size == PT

GREEN = (74, 222, 128)  # brand green #4ADE80
GRAY = (150, 150, 150)  # clean neutral


def _mask() -> Image.Image:
    """The brand C as a single-channel alpha mask at the target size."""
    src = Image.open(SRC).convert("RGBA").resize((PX, PX), Image.LANCZOS)
    return src.getchannel("A")


def _recolor(mask: Image.Image, rgb: tuple[int, int, int],
             brightness: float = 1.0, glow: float = 0.0) -> Image.Image:
    """Fill `mask` with `rgb` (optionally dimmed by `brightness`), with an
    optional soft outer glow of the same color under the crisp glyph."""
    r, g, b = (max(0, min(255, int(c * brightness))) for c in rgb)
    glyph = Image.new("RGBA", mask.size, (r, g, b, 0))
    glyph.putalpha(mask)
    if glow <= 0:
        return glyph
    blur = mask.filter(ImageFilter.GaussianBlur(radius=max(1.0, PX * 0.06)))
    halo = Image.new("RGBA", mask.size, (r, g, b, 0))
    halo.putalpha(blur.point(lambda a: int(a * glow)))
    return Image.alpha_composite(halo, glyph)


# state -> (rgb, brightness, glow)
STATES = {
    "running": (GREEN, 1.0, 0.0),
    "stopped": (GRAY, 0.9, 0.0),
    "starting_a": (GREEN, 0.7, 0.0),
    "starting_b": (GREEN, 1.0, 0.25),
    "golive_1": (GREEN, 1.0, 0.18),
    "golive_2": (GREEN, 1.0, 0.50),
    "golive_3": (GREEN, 1.0, 0.0),
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    mask = _mask()
    for name, (rgb, bri, glow) in STATES.items():
        path = OUT / f"tray_{name}.png"
        _recolor(mask, rgb, bri, glow).save(path, dpi=DPI)
        print("wrote", path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
