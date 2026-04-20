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
PORT_FILL = (245, 238, 215)
PORT_LINE = (60, 40, 20)

# Player piece colors — Catan-ish, tuned for readability on the tile palette.
PLAYER_COLORS = {
    "RED": (200, 45, 45),
    "BLUE": (45, 90, 180),
    "WHITE": (245, 245, 240),
    "ORANGE": (230, 140, 40),
}
PIECE_OUTLINE = (20, 14, 10)

# NodeRef order must match the _hex_corners angle order (pointy-top, starting
# at NORTH, going clockwise).
NODE_REF_ORDER = ("NORTH", "NORTHEAST", "SOUTHEAST", "SOUTH", "SOUTHWEST", "NORTHWEST")


def _axial_to_pixel(x: int, z: int, size: float) -> tuple[float, float]:
    """Pointy-top hex: convert axial (q=x, r=z) to pixel center."""
    px = size * math.sqrt(3) * (x + z / 2)
    py = size * 1.5 * z
    return px, py


def _hex_corners(cx: float, cy: float, size: float) -> list[tuple[float, float]]:
    """Six corners of a pointy-top hex centered at (cx, cy), ordered
    N, NE, SE, S, SW, NW to match catanatron's NodeRef enum."""
    pts = []
    for i in range(6):
        # i=0 at NORTH (270°), then clockwise: NE (330°), SE (30°), S (90°),
        # SW (150°), NW (210°).
        angle = math.radians(270 + 60 * i)
        pts.append((cx + size * math.cos(angle), cy + size * math.sin(angle)))
    return pts


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
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


def render_board(game: "Game", out_path: str | Path, hex_size: int = 60,
                 highlight_nodes: list[int] | None = None) -> Path:
    """Render the board to a PNG. Returns the output path.

    `highlight_nodes`, if given, is a ranked list of node_ids to mark with
    numbered circles — use this to visualize advisor recommendations.
    """
    out_path = Path(out_path)
    board = game.state.board
    land_tiles = board.map.land_tiles

    # Compute pixel extents so we can size the canvas.
    xs, ys = [], []
    for (x, _y, z) in land_tiles.keys():
        px, py = _axial_to_pixel(x, z, hex_size)
        xs.append(px)
        ys.append(py)
    pad = hex_size * 2.8  # extra room for ports
    minx, maxx = min(xs) - pad, max(xs) + pad
    miny, maxy = min(ys) - pad, max(ys) + pad
    w = int(maxx - minx)
    h = int(maxy - miny)

    img = Image.new("RGB", (w, h), OCEAN)
    draw = ImageDraw.Draw(img)

    ox = -minx
    oy = -miny
    board_cx = (maxx + minx) / 2 + ox - ox  # board center in canvas coords
    board_cy = (maxy + miny) / 2 + oy - oy
    # Equivalent: board center at canvas midpoint.
    board_cx = w / 2
    board_cy = h / 2

    number_font = _load_font(int(hex_size * 0.42))
    resource_font = _load_font(int(hex_size * 0.22))
    port_font = _load_font(int(hex_size * 0.22))
    robber_coord = board.robber_coordinate

    # Build a global node_id → pixel position map so ports and (later) buildings
    # can locate themselves.
    node_pos: dict[int, tuple[float, float]] = {}
    for coord, tile in land_tiles.items():
        x, _y, z = coord
        px, py = _axial_to_pixel(x, z, hex_size)
        cx, cy = px + ox, py + oy
        corners = _hex_corners(cx, cy, hex_size)
        for i, ref in enumerate(NODE_REF_ORDER):
            nid = tile.nodes[_node_ref(ref)]
            node_pos[nid] = corners[i]

    # Draw tiles.
    for coord, tile in land_tiles.items():
        x, _y, z = coord
        px, py = _axial_to_pixel(x, z, hex_size)
        cx, cy = px + ox, py + oy

        resource = tile.resource
        fill = TILE_COLORS.get(resource, TILE_COLORS[None])
        corners = _hex_corners(cx, cy, hex_size)
        draw.polygon(corners, fill=fill, outline=BLACK, width=2)

        label = resource if resource else "DESERT"
        _draw_centered_text(draw, cx, cy - hex_size * 0.55, label,
                            resource_font, BLACK)

        if tile.number is not None:
            _draw_number_token(draw, cx, cy, hex_size * 0.32, tile.number,
                               number_font)

        if coord == robber_coord:
            r = hex_size * 0.18
            draw.ellipse(
                (cx - r, cy + hex_size * 0.15 - r,
                 cx + r, cy + hex_size * 0.15 + r),
                fill=ROBBER, outline=TOKEN_FILL, width=2,
            )

    # Draw ports. Each port has 2 coastal node terminals — find them via the
    # intersection of the port's 6 hex nodes with map.port_nodes[resource].
    port_nodes_map = board.map.port_nodes
    for port in board.map.ports_by_id.values():
        resource = port.resource
        port_node_ids = port_nodes_map.get(resource, set())
        terminals = [nid for nid in port.nodes.values()
                     if nid in port_node_ids and nid in node_pos]
        # For 3:1 (None resource), port_nodes[None] contains 8 nodes for all
        # 4 3:1 ports — we need to pick just the 2 that belong to THIS port.
        # The port's "direction" edge is the ocean-facing edge; its 2 nodes
        # are the terminals. Use port.edges + port.direction for precision.
        if len(terminals) != 2:
            terminals = _port_terminals_via_direction(port)
            terminals = [nid for nid in terminals if nid in node_pos]
        if len(terminals) != 2:
            continue  # skip if we can't locate it
        n1 = node_pos[terminals[0]]
        n2 = node_pos[terminals[1]]
        mx, my = (n1[0] + n2[0]) / 2, (n1[1] + n2[1]) / 2
        # Push the port marker outward from the board center.
        dx, dy = mx - board_cx, my - board_cy
        mag = math.hypot(dx, dy) or 1.0
        push = hex_size * 0.9
        pmx = mx + dx / mag * push
        pmy = my + dy / mag * push
        # Dock lines from each terminal to the marker.
        draw.line([n1, (pmx, pmy)], fill=PORT_LINE, width=2)
        draw.line([n2, (pmx, pmy)], fill=PORT_LINE, width=2)
        # Marker circle.
        r = hex_size * 0.30
        draw.ellipse((pmx - r, pmy - r, pmx + r, pmy + r),
                     fill=PORT_FILL, outline=PORT_LINE, width=2)
        label = f"{resource[:3]} 2:1" if resource else "3:1"
        _draw_centered_text(draw, pmx, pmy, label, port_font, BLACK)

    # Roads first so buildings draw on top at the endpoints.
    drawn_edges: set[tuple[int, int]] = set()
    for edge, color in board.roads.items():
        key = tuple(sorted(edge))
        if key in drawn_edges:
            continue
        drawn_edges.add(key)
        a, b = edge
        if a not in node_pos or b not in node_pos:
            continue
        _draw_road(draw, node_pos[a], node_pos[b], color.name, hex_size)

    for node_id, (color, kind) in board.buildings.items():
        if node_id not in node_pos:
            continue
        cx, cy = node_pos[node_id]
        if kind == "SETTLEMENT":
            _draw_settlement(draw, cx, cy, hex_size, color.name)
        elif kind == "CITY":
            _draw_city(draw, cx, cy, hex_size, color.name)

    if highlight_nodes:
        highlight_font = _load_font(int(hex_size * 0.28))
        for rank, nid in enumerate(highlight_nodes, start=1):
            if nid not in node_pos:
                continue
            cx, cy = node_pos[nid]
            r = hex_size * 0.22
            # Gold-ish marker so it stands out on every tile color.
            draw.ellipse(
                (cx - r, cy - r, cx + r, cy + r),
                fill=(250, 220, 90), outline=PIECE_OUTLINE, width=2,
            )
            _draw_centered_text(draw, cx, cy, str(rank),
                                highlight_font, PIECE_OUTLINE)

    img.save(out_path)
    return out_path


def _node_ref(name: str):
    from catanatron.models.map import NodeRef
    return NodeRef[name]


def _port_terminals_via_direction(port) -> list[int]:
    """Return the 2 node IDs on the port's ocean-facing edge."""
    from catanatron.models.map import EdgeRef
    try:
        edge_ref = EdgeRef[port.direction.name]
    except KeyError:
        return []
    edge = port.edges.get(edge_ref)
    return list(edge) if edge else []


def _draw_centered_text(draw, cx, cy, text, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text((cx - tw / 2, cy - th / 2), text, font=font, fill=fill)


_PIPS_BY_NUMBER = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5,
                   8: 5, 9: 4, 10: 3, 11: 2, 12: 1}


def _draw_number_token(draw, cx, cy, radius, number, font) -> None:
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=TOKEN_FILL, outline=TOKEN_BORDER, width=2,
    )
    color = RED_NUMBER if number in (6, 8) else BLACK
    _draw_centered_text(draw, cx, cy - radius * 0.08, str(number), font, color)

    pips = _PIPS_BY_NUMBER.get(number, 0)
    if pips == 0:
        return
    # Row of dots under the number, color-matched (red for 6/8).
    dot_r = max(1.5, radius * 0.09)
    gap = dot_r * 2.4
    total_w = gap * (pips - 1)
    dy = radius * 0.55
    for i in range(pips):
        dx = -total_w / 2 + i * gap
        draw.ellipse(
            (cx + dx - dot_r, cy + dy - dot_r,
             cx + dx + dot_r, cy + dy + dot_r),
            fill=color,
        )


def _draw_road(draw, p1, p2, color_name: str, hex_size: float) -> None:
    """Thick colored segment between two node pixels, with dark outline so it
    reads on any tile color."""
    fill = PLAYER_COLORS.get(color_name, (180, 180, 180))
    outline_w = max(2, int(hex_size * 0.16))
    inner_w = max(1, int(hex_size * 0.10))
    draw.line([p1, p2], fill=PIECE_OUTLINE, width=outline_w)
    draw.line([p1, p2], fill=fill, width=inner_w)


def _draw_settlement(draw, cx: float, cy: float, hex_size: float,
                     color_name: str) -> None:
    """Small house at (cx, cy): square base + triangular roof."""
    fill = PLAYER_COLORS.get(color_name, (180, 180, 180))
    s = hex_size * 0.22  # half-width
    roof = hex_size * 0.18
    pts = [
        (cx - s, cy + s),
        (cx - s, cy - s * 0.2),
        (cx, cy - s * 0.2 - roof),
        (cx + s, cy - s * 0.2),
        (cx + s, cy + s),
    ]
    draw.polygon(pts, fill=fill, outline=PIECE_OUTLINE)


def _draw_city(draw, cx: float, cy: float, hex_size: float,
               color_name: str) -> None:
    """Wider L-shape to distinguish from a settlement: short tower on the left,
    tall tower on the right, single baseline."""
    fill = PLAYER_COLORS.get(color_name, (180, 180, 180))
    w = hex_size * 0.34
    h = hex_size * 0.26
    tall = hex_size * 0.40
    # L-profile polygon (points go clockwise from bottom-left).
    pts = [
        (cx - w, cy + h * 0.6),
        (cx - w, cy - h * 0.1),
        (cx - w * 0.1, cy - h * 0.1),
        (cx - w * 0.1, cy - tall * 0.6),
        (cx + w * 0.05, cy - tall),
        (cx + w, cy - tall * 0.6),
        (cx + w, cy + h * 0.6),
    ]
    draw.polygon(pts, fill=fill, outline=PIECE_OUTLINE)
