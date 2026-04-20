"""Board renderer — writes a PNG of a catanatron Game state.

Uses Pillow. Pointy-top hex layout. Cube coordinates (x, y, z) with x+y+z=0
map to pixel (px, py) via standard axial conversion.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from catanatron import Game


# Resource → fill color.
TILE_COLORS = {
    "WHEAT": (240, 199, 94),
    "WOOD": (90, 125, 58),
    "SHEEP": (167, 210, 107),
    "BRICK": (180, 82, 44),
    "ORE": (122, 122, 122),
    None: (226, 209, 164),  # desert
}

OCEAN = (62, 121, 159)
TOKEN_FILL = (250, 244, 224)
TOKEN_BORDER = (60, 40, 20)
RED_NUMBER = (180, 40, 40)
BLACK = (30, 22, 16)
ROBBER = (30, 22, 16)


def _axial_to_pixel(x: int, z: int, size: float) -> tuple[float, float]:
    """Pointy-top hex: convert axial (q=x, r=z) to pixel center."""
    px = size * math.sqrt(3) * (x + z / 2)
    py = size * 1.5 * z
    return px, py


def _hex_corners(cx: float, cy: float, size: float) -> list[tuple[float, float]]:
    """Six corners of a pointy-top hex centered at (cx, cy)."""
    pts = []
    for i in range(6):
        angle = math.radians(60 * i - 30)  # pointy-top: first corner at top
        pts.append((cx + size * math.cos(angle), cy + size * math.sin(angle)))
    return pts


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Best-available system font; falls back to Pillow default."""
    for path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_board(game: "Game", out_path: str | Path, hex_size: int = 60) -> Path:
    """Render the board to a PNG. Returns the output path."""
    out_path = Path(out_path)
    board = game.state.board
    land_tiles = board.map.land_tiles

    # Compute pixel extents so we can size the canvas.
    xs, ys = [], []
    for (x, _y, z) in land_tiles.keys():
        px, py = _axial_to_pixel(x, z, hex_size)
        xs.append(px)
        ys.append(py)
    pad = hex_size * 2
    minx, maxx = min(xs) - pad, max(xs) + pad
    miny, maxy = min(ys) - pad, max(ys) + pad
    w = int(maxx - minx)
    h = int(maxy - miny)

    img = Image.new("RGB", (w, h), OCEAN)
    draw = ImageDraw.Draw(img)

    # Offset so the board sits centered in the canvas.
    ox = -minx
    oy = -miny

    number_font = _load_font(int(hex_size * 0.42))
    resource_font = _load_font(int(hex_size * 0.22))
    robber_coord = board.robber_coordinate

    for coord, tile in land_tiles.items():
        x, _y, z = coord
        px, py = _axial_to_pixel(x, z, hex_size)
        cx, cy = px + ox, py + oy

        resource = tile.resource
        fill = TILE_COLORS.get(resource, TILE_COLORS[None])
        corners = _hex_corners(cx, cy, hex_size)
        draw.polygon(corners, fill=fill, outline=BLACK, width=2)

        # Resource label (small, top of tile).
        label = resource if resource else "DESERT"
        _draw_centered_text(draw, cx, cy - hex_size * 0.55, label,
                            resource_font, BLACK)

        # Number token (center of tile).
        number = tile.number
        if number is not None:
            _draw_number_token(draw, cx, cy, hex_size * 0.32, number,
                               number_font)

        # Robber marker.
        if coord == robber_coord:
            r = hex_size * 0.18
            draw.ellipse(
                (cx - r, cy + hex_size * 0.15 - r,
                 cx + r, cy + hex_size * 0.15 + r),
                fill=ROBBER, outline=TOKEN_FILL, width=2,
            )

    img.save(out_path)
    return out_path


def _draw_centered_text(draw, cx, cy, text, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text((cx - tw / 2, cy - th / 2), text, font=font, fill=fill)


def _draw_number_token(draw, cx, cy, radius, number, font) -> None:
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=TOKEN_FILL, outline=TOKEN_BORDER, width=2,
    )
    color = RED_NUMBER if number in (6, 8) else BLACK
    _draw_centered_text(draw, cx, cy, str(number), font, color)
