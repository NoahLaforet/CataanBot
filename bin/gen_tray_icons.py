#!/usr/bin/env python3
"""Generate the menu-bar status-icon frames for the CatanBot tray app.

Derived from the brand art (extension/icons/icon-128.png), which is the
CatanBot "C" logo: a green C and green rounded border on a near-black
rounded tile. The frames keep that FULL logo and vary saturation +
brightness for the status states. (Recoloring a silhouette mask instead
would flatten the tile into a solid square, since the art's alpha channel
is the whole tile, not the C.) Run once; the frames are committed under
src/catanbot/tray/assets/ so the app has them at runtime without a Pillow
dependency. Re-run after changing the brand icon.

Files are emitted at a 2x retina pixel size tagged 144 dpi, so NSImage
(what rumps loads) reports a ~22pt logical size and renders crisp on
Retina menu bars.

  running      : the full-color logo (bridge up)
  stopped      : desaturated gray (bridge off)
  starting_a   : dim pulse frame (bridge coming up)
  starting_b   : bright pulse frame
  golive_1/2/3 : one-shot brighten-and-settle pop when the bridge goes live
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageEnhance

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "extension" / "icons" / "icon-128.png"
OUT = ROOT / "src" / "catanbot" / "tray" / "assets"

PT = 22                 # logical menu-bar point size
SCALE = 2               # retina backing
PX = PT * SCALE         # physical pixels (44)
DPI = (72 * SCALE, 72 * SCALE)   # tag as 2x so NSImage logical size == PT


def _adjust(base: Image.Image, saturation: float,
            brightness: float) -> Image.Image:
    """Recolor the full logo by saturation + brightness, preserving the
    original alpha (the rounded-tile shape)."""
    rgb = ImageEnhance.Color(base.convert("RGB")).enhance(saturation)
    rgb = ImageEnhance.Brightness(rgb).enhance(brightness)
    out = rgb.convert("RGBA")
    out.putalpha(base.getchannel("A"))
    return out


# state -> (saturation, brightness)
STATES = {
    "running": (1.1, 1.0),
    "stopped": (0.0, 0.85),
    "starting_a": (0.7, 0.7),
    "starting_b": (1.1, 1.0),
    "golive_1": (1.2, 1.15),
    "golive_2": (1.35, 1.3),
    "golive_3": (1.1, 1.0),
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    base = Image.open(SRC).convert("RGBA").resize((PX, PX), Image.LANCZOS)
    for name, (sat, bri) in STATES.items():
        path = OUT / f"tray_{name}.png"
        _adjust(base, sat, bri).save(path, dpi=DPI)
        print("wrote", path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
