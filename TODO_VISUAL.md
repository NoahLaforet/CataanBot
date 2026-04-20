# Visual polish backlog

Current renderer is functional but bare — colored hexes, number tokens, text
labels on tiles and ports, basic piece shapes. Noah wants this to look like a
real interface, not a debug printout. Plan below; tackle after core advisor
features land.

## Tiles — use imagery, not text
- Resource icons per tile: sheep, wheat stalks, brick wall, wood/tree, ore
  rock, desert sand. Drop the "WHEAT" / "WOOD" text label.
- Source: public-domain tile SVG/PNG set, or hand-drawn pixel art baked into
  `assets/`. Commit license alongside.
- Subtle texture / gradient on each hex (not flat fill) so tiles read as
  physical pieces.

## Ports — visual, not "WOO 2:1"
- Little resource icon inside the port circle instead of 3-letter text.
- 3:1 ports get a "?" or generic harbor icon.
- Dock lines styled as little wooden planks, not plain 2px strokes.

## Number tokens
- Classic pip dots under the number (task #3).
- Circle has a soft drop shadow.
- Serif font for the number itself.

## Pieces
- Settlements / cities currently simple polygons. Later: slight 3D shading,
  consistent lighting direction, subtle drop shadow so they don't blend into
  the hex.
- Roads: rounded end caps, maybe a wood-grain gradient.

## Background / framing
- Ocean gets a subtle wave texture or gradient (lighter near land).
- Optional title bar / legend strip showing player colors + scores.

## Implementation notes
- Prefer Pillow for the MVP (no new deps). If polish needs real SVG, consider
  cairosvg or switch rendering to HTML/canvas down the road.
- Keep the text-only mode available behind a flag for debugging.
