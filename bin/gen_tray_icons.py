#!/usr/bin/env python3
"""Generate the menu-bar status-icon frames for the CatanBot tray app.

Derived from the brand art (extension/icons/icon-128.png). Run once; the
frames are committed under src/catanbot/tray/assets/ so the app has them
at runtime without a Pillow dependency. Re-run after changing the brand
icon.

  tray_running    : full-color, slightly vivid (bridge up)
  tray_stopped    : desaturated + dimmed (bridge off)
  tray_starting_a : dim pulse frame (bridge coming up)
  tray_starting_b : bright pulse frame
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageEnhance

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "extension" / "icons" / "icon-128.png"
OUT = ROOT / "src" / "catanbot" / "tray" / "assets"
SIZE = 44  # rumps scales to the menu-bar height; this keeps it crisp


def _adjust(base: Image.Image, saturation: float,
            brightness: float) -> Image.Image:
    rgb = ImageEnhance.Color(base.convert("RGB")).enhance(saturation)
    rgb = ImageEnhance.Brightness(rgb).enhance(brightness)
    out = rgb.convert("RGBA")
    out.putalpha(base.getchannel("A"))
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    base = Image.open(SRC).convert("RGBA").resize(
        (SIZE, SIZE), Image.LANCZOS)
    frames = {
        "running": (1.15, 1.0),
        "stopped": (0.0, 0.55),
        "starting_a": (0.55, 0.78),
        "starting_b": (1.1, 1.0),
    }
    for name, (sat, bri) in frames.items():
        path = OUT / f"tray_{name}.png"
        _adjust(base, sat, bri).save(path)
        print("wrote", path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
