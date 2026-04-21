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
OCEAN_TOP = (48, 96, 136)        # slightly darker at the top
OCEAN_BOTTOM = (78, 142, 180)    # lighter at the bottom for a vertical gradient
TOKEN_FILL = (250, 244, 224)
TOKEN_BORDER = (60, 40, 20)
RED_NUMBER = (180, 40, 40)
BLACK = (30, 22, 16)
ROBBER = (30, 22, 16)
PORT_FILL = (245, 238, 215)
PORT_LINE = (60, 40, 20)
# Solid dark gray for piece/token drop shadows. Full opacity keeps the
# renderer in RGB-only space (no alpha-compositing dance); the eye reads
# a solid ~2-3px dark shape under a colored piece as a shadow just fine.
SHADOW_COLOR = (32, 24, 18)

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
                 highlight_nodes: list[int] | None = None,
                 show_legend: bool = True,
                 label_style: str = "icon") -> Path:
    """Render the board to a PNG. Returns the output path.

    `highlight_nodes`, if given, is a ranked list of node_ids to mark with
    numbered circles — use this to visualize advisor recommendations.

    `show_legend` (default True) adds a bottom strip showing per-player
    VP, buildings, roads, and longest-road / largest-army badges so the
    render is self-contained — no need to cross-reference a summary.

    `label_style` is "icon" (default) for a small geometric resource icon,
    or "text" for the older "WHEAT"/"WOOD" string — useful for debugging.
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
    board_h = int(maxy - miny)
    legend_h = int(hex_size * 1.5) if show_legend else 0
    h = board_h + legend_h

    img = Image.new("RGB", (w, h), OCEAN)
    # Paint a vertical ocean gradient on the board region so the water
    # doesn't read as one flat slab. Each row linearly interpolates
    # between OCEAN_TOP and OCEAN_BOTTOM.
    _paint_vertical_gradient(img, 0, 0, w, board_h if show_legend else h,
                             OCEAN_TOP, OCEAN_BOTTOM)
    draw = ImageDraw.Draw(img)

    ox = -minx
    oy = -miny
    board_cx = (maxx + minx) / 2 + ox - ox  # board center in canvas coords
    board_cy = (maxy + miny) / 2 + oy - oy
    # Board occupies the top portion of the canvas; legend (if any) sits
    # below. Keep the board vertically centered within its own region.
    board_cx = w / 2
    board_cy = board_h / 2

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

        icon_y = cy - hex_size * 0.55
        if label_style == "text":
            label = resource if resource else "DESERT"
            _draw_centered_text(draw, cx, icon_y, label,
                                resource_font, BLACK)
        else:
            _draw_resource_icon(draw, cx, icon_y, hex_size * 0.22, resource)

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
        # Port contents: 2:1 ports get the resource icon + a small "2:1" ratio
        # tag below; 3:1 ports show just "3:1" centered.
        if resource:
            _draw_resource_icon(draw, pmx, pmy - r * 0.2, r * 0.6, resource)
            ratio_font = _load_font(max(10, int(hex_size * 0.17)))
            _draw_centered_text(draw, pmx, pmy + r * 0.55, "2:1",
                                ratio_font, BLACK)
        else:
            _draw_centered_text(draw, pmx, pmy, "3:1", port_font, BLACK)

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

    if show_legend:
        _draw_legend(draw, game, w, board_h, legend_h, hex_size)

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
    # Soft drop shadow: slightly larger, offset, solid dark. Sits below the
    # token so it reads as the token floating just above the hex.
    sx, sy = 2, 3
    draw.ellipse(
        (cx - radius + sx, cy - radius + sy,
         cx + radius + sx, cy + radius + sy),
        fill=SHADOW_COLOR,
    )
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


LEGEND_BG = (38, 82, 112)       # slightly darker than ocean so it reads as a panel
LEGEND_DIVIDER = (20, 40, 60)
LEGEND_TEXT = (245, 245, 240)
LEGEND_ACCENT = (250, 220, 90)  # for longest-road / largest-army badges
LEGEND_COLOR_ORDER = ("RED", "BLUE", "WHITE", "ORANGE")


def _draw_legend(draw, game, w: int, board_h: int, legend_h: int,
                 hex_size: float) -> None:
    """Per-color summary strip along the bottom of the render.

    Shows a color swatch, current public VP, settlement/city/road counts,
    and small pill badges for longest road and largest army. Skips colors
    that aren't seated in the game (shouldn't happen in our 4-player setup
    but stays robust).
    """
    state = game.state
    board = state.board

    # Count settlements/cities per color from the buildings dict.
    s_count = {c: 0 for c in LEGEND_COLOR_ORDER}
    c_count = {c: 0 for c in LEGEND_COLOR_ORDER}
    for _nid, (color, kind) in board.buildings.items():
        if color.name not in s_count:
            continue
        if kind == "CITY":
            c_count[color.name] += 1
        else:
            s_count[color.name] += 1
    # Roads: each edge appears twice in `board.roads` (both orderings).
    r_count = {c: 0 for c in LEGEND_COLOR_ORDER}
    for _edge, color in board.roads.items():
        if color.name in r_count:
            r_count[color.name] += 1
    for c in r_count:
        r_count[c] //= 2

    # Panel background.
    draw.rectangle((0, board_h, w, board_h + legend_h), fill=LEGEND_BG)
    draw.line([(0, board_h), (w, board_h)], fill=LEGEND_DIVIDER, width=2)

    colors_seated = [c for c in LEGEND_COLOR_ORDER
                     if _find_player_index(state, c) is not None]
    if not colors_seated:
        return

    # Winner / near-winner callout overlaid across the top of the strip when
    # someone is close enough that the game could end soon. Silent for early
    # game (everyone under 8 VP). Returns the vertical offset the per-color
    # columns must shift down so they don't overlap the banner.
    banner_h = _draw_vp_callout(draw, state, colors_seated, w, board_h,
                                legend_h, hex_size)

    col_w = w / len(colors_seated)
    name_font = _load_font(int(hex_size * 0.28))
    line_font = _load_font(int(hex_size * 0.22))
    badge_font = _load_font(int(hex_size * 0.18))

    # Per-color columns start below the banner (if any) and use the remaining
    # legend area. All relative offsets are taken from `content_top` and
    # scaled by `content_h` so the layout stays consistent at any banner height.
    content_top = board_h + banner_h
    content_h = legend_h - banner_h

    for i, cname in enumerate(colors_seated):
        col_x = i * col_w
        idx = _find_player_index(state, cname)
        vp = int(state.player_state.get(f"P{idx}_VICTORY_POINTS", 0))
        has_road = bool(state.player_state.get(f"P{idx}_HAS_ROAD", False))
        has_army = bool(state.player_state.get(f"P{idx}_HAS_ARMY", False))

        # Color swatch on the left of the column.
        swatch_x = col_x + hex_size * 0.30
        swatch_y = content_top + content_h * 0.18
        swatch_size = content_h * 0.32
        draw.rectangle(
            (swatch_x, swatch_y, swatch_x + swatch_size, swatch_y + swatch_size),
            fill=PLAYER_COLORS.get(cname, (180, 180, 180)),
            outline=PIECE_OUTLINE, width=2,
        )

        # Name + VP on the first line.
        text_x = swatch_x + swatch_size + hex_size * 0.18
        line1_y = content_top + content_h * 0.12
        draw.text((text_x, line1_y),
                  f"{cname}  {vp} VP", font=name_font, fill=LEGEND_TEXT)

        # Building counts line.
        line2_y = content_top + content_h * 0.48
        counts = f"{s_count[cname]}s  {c_count[cname]}c  {r_count[cname]}r"
        draw.text((text_x, line2_y), counts, font=line_font, fill=LEGEND_TEXT)

        # Badges row, only if earned.
        badge_y = content_top + content_h * 0.74
        badge_x = text_x
        if has_road:
            badge_x = _draw_badge(draw, badge_x, badge_y, "LR",
                                  badge_font, hex_size)
        if has_army:
            badge_x = _draw_badge(draw, badge_x, badge_y, "LA",
                                  badge_font, hex_size)

        # Divider between columns (skip after last).
        if i < len(colors_seated) - 1:
            dx = col_x + col_w
            draw.line(
                [(dx, content_top + content_h * 0.15),
                 (dx, content_top + content_h * 0.85)],
                fill=LEGEND_DIVIDER, width=1,
            )


def _find_player_index(state, color_name: str) -> int | None:
    from catanatron import Color
    try:
        c = Color[color_name]
    except KeyError:
        return None
    return state.color_to_index.get(c)


def _draw_badge(draw, x: float, y: float, label: str,
                font, hex_size: float) -> float:
    """Small pill-shaped badge. Returns the x-offset where the next badge
    should start (so callers can chain them)."""
    bbox = draw.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad_x = hex_size * 0.12
    pad_y = hex_size * 0.06
    x2 = x + tw + 2 * pad_x
    y2 = y + th + 2 * pad_y
    draw.rounded_rectangle((x, y, x2, y2), radius=int(pad_y * 1.2),
                           fill=LEGEND_ACCENT, outline=PIECE_OUTLINE, width=1)
    draw.text((x + pad_x, y + pad_y), label, font=font, fill=PIECE_OUTLINE)
    return x2 + hex_size * 0.12


CALLOUT_WINNER_BG = (210, 70, 60)      # alarming red for game-over
CALLOUT_ONE_AWAY_BG = (230, 150, 40)   # amber — next-turn threat
CALLOUT_TWO_AWAY_BG = (90, 130, 200)   # muted blue — heads-up
CALLOUT_TEXT = (250, 245, 230)


def _draw_vp_callout(draw, state, colors_seated, w: int, board_h: int,
                     legend_h: int, hex_size: float) -> int:
    """Render a single-line banner across the top of the legend strip when
    some player is at 8+ VP. Returns the banner height in pixels (0 when
    silent, so the caller can shift per-color columns down accordingly)."""
    per_color: dict[str, int] = {}
    for cname in colors_seated:
        idx = _find_player_index(state, cname)
        if idx is None:
            continue
        per_color[cname] = int(state.player_state.get(f"P{idx}_VICTORY_POINTS", 0))
    if not per_color:
        return 0
    top = max(per_color.values())
    if top < 8:
        return 0
    leaders = "/".join(c for c, v in per_color.items() if v == top)
    if top >= 10:
        bg = CALLOUT_WINNER_BG
        text = f"*  {leaders} WINS at {top} VP  *"
    elif top >= 9:
        bg = CALLOUT_ONE_AWAY_BG
        text = f"!  {leaders} at {top} VP — one turn from winning  !"
    else:
        bg = CALLOUT_TWO_AWAY_BG
        text = f"{leaders} at {top} VP — two from winning"

    # Thin banner pinned to the top edge of the legend strip.
    banner_h = int(legend_h * 0.32)
    draw.rectangle((0, board_h, w, board_h + banner_h), fill=bg)
    font = _load_font(max(12, int(hex_size * 0.22)))
    _draw_centered_text(draw, w / 2, board_h + banner_h / 2, text,
                        font, CALLOUT_TEXT)
    return banner_h


def _draw_road(draw, p1, p2, color_name: str, hex_size: float) -> None:
    """Thick colored segment between two node pixels, with dark outline and
    rounded end caps so the road doesn't butt into buildings with a harsh
    square edge."""
    fill = PLAYER_COLORS.get(color_name, (180, 180, 180))
    outline_w = max(2, int(hex_size * 0.16))
    inner_w = max(1, int(hex_size * 0.10))
    # Drop-shadow segment underneath.
    sx, sy = 2, 3
    draw.line([(p1[0] + sx, p1[1] + sy), (p2[0] + sx, p2[1] + sy)],
              fill=SHADOW_COLOR, width=outline_w)
    # Outline + inner fill.
    draw.line([p1, p2], fill=PIECE_OUTLINE, width=outline_w)
    draw.line([p1, p2], fill=fill, width=inner_w)
    # Rounded end caps — two filled circles, outlined then filled.
    r_out = outline_w / 2
    r_in = inner_w / 2
    for p in (p1, p2):
        draw.ellipse((p[0] - r_out, p[1] - r_out,
                      p[0] + r_out, p[1] + r_out),
                     fill=PIECE_OUTLINE)
        draw.ellipse((p[0] - r_in, p[1] - r_in,
                      p[0] + r_in, p[1] + r_in),
                     fill=fill)


def _draw_settlement(draw, cx: float, cy: float, hex_size: float,
                     color_name: str) -> None:
    """Small house at (cx, cy): square base + triangular roof, with drop
    shadow offset so it lifts off the hex."""
    fill = PLAYER_COLORS.get(color_name, (180, 180, 180))
    highlight = _lighten(fill, 0.18)
    s = hex_size * 0.22
    roof = hex_size * 0.18
    pts = [
        (cx - s, cy + s),
        (cx - s, cy - s * 0.2),
        (cx, cy - s * 0.2 - roof),
        (cx + s, cy - s * 0.2),
        (cx + s, cy + s),
    ]
    sx, sy = 2, 3
    shadow_pts = [(x + sx, y + sy) for x, y in pts]
    draw.polygon(shadow_pts, fill=SHADOW_COLOR)
    draw.polygon(pts, fill=fill, outline=PIECE_OUTLINE)
    # Diagonal highlight streak: short polygon along the upper-left of the
    # base so the piece reads as lit from upper-left.
    streak = [
        (cx - s + 1, cy + s - 1),
        (cx - s + 1, cy - s * 0.2 + 1),
        (cx - s * 0.4, cy - s * 0.2 + 1),
    ]
    draw.polygon(streak, fill=highlight)


def _draw_city(draw, cx: float, cy: float, hex_size: float,
               color_name: str) -> None:
    """Wider L-shape to distinguish from a settlement: short tower on the left,
    tall tower on the right, single baseline. Drop shadow + highlight streak
    match the settlement styling."""
    fill = PLAYER_COLORS.get(color_name, (180, 180, 180))
    highlight = _lighten(fill, 0.18)
    w = hex_size * 0.34
    h = hex_size * 0.26
    tall = hex_size * 0.40
    pts = [
        (cx - w, cy + h * 0.6),
        (cx - w, cy - h * 0.1),
        (cx - w * 0.1, cy - h * 0.1),
        (cx - w * 0.1, cy - tall * 0.6),
        (cx + w * 0.05, cy - tall),
        (cx + w, cy - tall * 0.6),
        (cx + w, cy + h * 0.6),
    ]
    sx, sy = 2, 3
    shadow_pts = [(x + sx, y + sy) for x, y in pts]
    draw.polygon(shadow_pts, fill=SHADOW_COLOR)
    draw.polygon(pts, fill=fill, outline=PIECE_OUTLINE)
    # Highlight streak on the left wall of the tall tower.
    streak = [
        (cx - w + 1, cy + h * 0.6 - 1),
        (cx - w + 1, cy - h * 0.1 + 1),
        (cx - w + hex_size * 0.04, cy - h * 0.1 + 1),
        (cx - w + hex_size * 0.04, cy + h * 0.6 - 1),
    ]
    draw.polygon(streak, fill=highlight)


def _lighten(rgb: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """Blend `rgb` toward white by `amount` ∈ [0, 1]. Used for piece highlights."""
    r, g, b = rgb
    return (
        int(r + (255 - r) * amount),
        int(g + (255 - g) * amount),
        int(b + (255 - b) * amount),
    )


# Darker accent variants of each tile color, used for the geometric icons so
# the icon reads clearly against its own tile instead of blending in.
_ICON_STALK = (80, 50, 14)         # wheat stalk and grain — dark brown
_ICON_WHEAT_GRAIN = (120, 80, 20)  # slightly lighter brown for grain tips
_ICON_TREE = (34, 68, 32)          # pine body — darker green than the tile
_ICON_TREE_SHADE = (22, 48, 20)
_ICON_TRUNK = (80, 50, 24)
_ICON_SHEEP_BODY = (250, 248, 238) # off-white wool
_ICON_SHEEP_HEAD = (48, 40, 32)    # dark head/face
_ICON_BRICK_FILL = (130, 52, 28)   # dark red brick on the tile's lighter red
_ICON_BRICK_MORTAR = (50, 20, 10)
_ICON_ORE_FILL = (200, 205, 215)   # pale blue-gray crystal on gray tile
_ICON_ORE_SHADE = (100, 110, 125)
_ICON_DESERT = (196, 162, 96)      # warm sand dot


def _draw_resource_icon(draw, cx: float, cy: float, size: float,
                        resource: str | None) -> None:
    """Draw a small geometric icon for the resource at (cx, cy).

    `size` is the icon half-width — icons are drawn roughly within
    [cx-size, cx+size] × [cy-size, cy+size]. Picked to match the old text
    label's position near the top of the hex."""
    if resource == "WHEAT":
        _draw_wheat_icon(draw, cx, cy, size)
    elif resource == "WOOD":
        _draw_wood_icon(draw, cx, cy, size)
    elif resource == "SHEEP":
        _draw_sheep_icon(draw, cx, cy, size)
    elif resource == "BRICK":
        _draw_brick_icon(draw, cx, cy, size)
    elif resource == "ORE":
        _draw_ore_icon(draw, cx, cy, size)
    else:
        _draw_desert_icon(draw, cx, cy, size)


def _draw_wheat_icon(draw, cx: float, cy: float, size: float) -> None:
    """A single wheat stalk — central stem plus three paired grain kernels
    branching up and outward."""
    stem_w = max(1, int(size * 0.12))
    draw.line([(cx, cy + size * 0.85), (cx, cy - size * 0.35)],
              fill=_ICON_STALK, width=stem_w)
    # Three pairs of oblique grain marks, highest pair at the top.
    for i, t in enumerate((0.0, 0.35, 0.7)):
        offset = size * (0.25 + t * 0.35)
        base_y = cy - size * 0.35 + size * t * 0.9
        # Left grain
        lg = [
            (cx - stem_w, base_y + size * 0.08),
            (cx - offset, base_y - size * 0.05),
            (cx - offset, base_y - size * 0.22),
            (cx - stem_w, base_y - size * 0.08),
        ]
        # Right grain mirrored
        rg = [(cx + (cx - x), y) for x, y in lg]
        draw.polygon(lg, fill=_ICON_WHEAT_GRAIN, outline=_ICON_STALK)
        draw.polygon(rg, fill=_ICON_WHEAT_GRAIN, outline=_ICON_STALK)
    # Crown grain at the very top.
    crown = [
        (cx, cy - size * 0.9),
        (cx - size * 0.18, cy - size * 0.55),
        (cx + size * 0.18, cy - size * 0.55),
    ]
    draw.polygon(crown, fill=_ICON_WHEAT_GRAIN, outline=_ICON_STALK)


def _draw_wood_icon(draw, cx: float, cy: float, size: float) -> None:
    """Stylized pine: brown trunk + two-tier dark-green triangular canopy."""
    trunk = [
        (cx - size * 0.12, cy + size * 0.95),
        (cx - size * 0.12, cy + size * 0.55),
        (cx + size * 0.12, cy + size * 0.55),
        (cx + size * 0.12, cy + size * 0.95),
    ]
    draw.polygon(trunk, fill=_ICON_TRUNK, outline=PIECE_OUTLINE)
    # Lower (wider) canopy tier
    lower = [
        (cx, cy - size * 0.05),
        (cx - size * 0.75, cy + size * 0.55),
        (cx + size * 0.75, cy + size * 0.55),
    ]
    draw.polygon(lower, fill=_ICON_TREE, outline=_ICON_TREE_SHADE)
    # Upper (narrower) canopy tier
    upper = [
        (cx, cy - size * 0.85),
        (cx - size * 0.55, cy - size * 0.05),
        (cx + size * 0.55, cy - size * 0.05),
    ]
    draw.polygon(upper, fill=_ICON_TREE, outline=_ICON_TREE_SHADE)


def _draw_sheep_icon(draw, cx: float, cy: float, size: float) -> None:
    """Woolly body as three overlapping off-white circles, with a small dark
    head on the right and two tiny legs."""
    r = size * 0.42
    # Body: three overlapping circles form the fluffy back.
    for dx, dy, rr in ((-size * 0.35, size * 0.05, r),
                        (0,            -size * 0.05, r * 1.05),
                        (size * 0.3,  size * 0.05, r)):
        draw.ellipse(
            (cx + dx - rr, cy + dy - rr, cx + dx + rr, cy + dy + rr),
            fill=_ICON_SHEEP_BODY, outline=PIECE_OUTLINE,
        )
    # Head on the right — small dark oval overlapping the rightmost wool bump.
    hx = cx + size * 0.6
    hy = cy + size * 0.1
    hr = size * 0.22
    draw.ellipse((hx - hr, hy - hr, hx + hr, hy + hr),
                 fill=_ICON_SHEEP_HEAD, outline=PIECE_OUTLINE)
    # Legs.
    for lx in (cx - size * 0.3, cx + size * 0.2):
        draw.line([(lx, cy + size * 0.45), (lx, cy + size * 0.9)],
                  fill=PIECE_OUTLINE, width=max(1, int(size * 0.1)))


def _draw_brick_icon(draw, cx: float, cy: float, size: float) -> None:
    """Three bricks in a staggered 2-over-1 pattern."""
    bw = size * 0.45
    bh = size * 0.32
    # Bottom row: two bricks side by side.
    bricks = [
        (cx - bw - size * 0.03, cy + size * 0.2, cx - size * 0.03, cy + size * 0.2 + bh),
        (cx + size * 0.03, cy + size * 0.2, cx + size * 0.03 + bw, cy + size * 0.2 + bh),
        # Top brick: centered, slightly offset so it bridges the two below.
        (cx - bw * 0.8, cy + size * 0.2 - bh - size * 0.04,
         cx + bw * 0.8, cy + size * 0.2 - size * 0.04),
    ]
    for x0, y0, x1, y1 in bricks:
        draw.rectangle((x0, y0, x1, y1),
                       fill=_ICON_BRICK_FILL, outline=_ICON_BRICK_MORTAR, width=2)


def _draw_ore_icon(draw, cx: float, cy: float, size: float) -> None:
    """Angular crystal: kite-shaped polygon with an internal facet line for
    a simple faceted look."""
    pts = [
        (cx, cy - size * 0.95),
        (cx + size * 0.7, cy - size * 0.1),
        (cx + size * 0.35, cy + size * 0.85),
        (cx - size * 0.35, cy + size * 0.85),
        (cx - size * 0.7, cy - size * 0.1),
    ]
    draw.polygon(pts, fill=_ICON_ORE_FILL, outline=PIECE_OUTLINE)
    # Highlight facet — polygon down the upper-left side.
    facet = [
        (cx, cy - size * 0.95),
        (cx - size * 0.7, cy - size * 0.1),
        (cx - size * 0.1, cy - size * 0.2),
    ]
    draw.polygon(facet, fill=_lighten(_ICON_ORE_FILL, 0.25),
                 outline=_ICON_ORE_SHADE)
    # Inner shade facet on the lower right so the crystal reads 3D.
    shade = [
        (cx + size * 0.7, cy - size * 0.1),
        (cx + size * 0.35, cy + size * 0.85),
        (cx + size * 0.05, cy + size * 0.1),
    ]
    draw.polygon(shade, fill=_ICON_ORE_SHADE, outline=PIECE_OUTLINE)


def _draw_desert_icon(draw, cx: float, cy: float, size: float) -> None:
    """Small sun dot — an earthy cue without the "DESERT" text."""
    r = size * 0.45
    draw.ellipse((cx - r, cy - r, cx + r, cy + r),
                 fill=_ICON_DESERT, outline=PIECE_OUTLINE, width=2)
    # Radial tick marks to suggest a sun.
    for ang_deg in range(0, 360, 45):
        a = math.radians(ang_deg)
        x1 = cx + math.cos(a) * r * 1.15
        y1 = cy + math.sin(a) * r * 1.15
        x2 = cx + math.cos(a) * r * 1.55
        y2 = cy + math.sin(a) * r * 1.55
        draw.line([(x1, y1), (x2, y2)], fill=_ICON_DESERT, width=2)


def _paint_vertical_gradient(img, x0: int, y0: int, x1: int, y1: int,
                             top_rgb, bottom_rgb) -> None:
    """Fill the rectangle (x0,y0)-(x1,y1) with a vertical color gradient.

    Uses row-by-row rectangles — slower than numpy but keeps dependencies
    at PIL only. The board canvas is small enough that this is fine."""
    draw = ImageDraw.Draw(img)
    height = max(1, y1 - y0)
    for row in range(y0, y1):
        t = (row - y0) / height
        color = tuple(int(top_rgb[i] + (bottom_rgb[i] - top_rgb[i]) * t)
                      for i in range(3))
        draw.rectangle((x0, row, x1, row + 1), fill=color)
