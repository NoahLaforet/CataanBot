# Volcano map support (mapSetting 34)

The Volcano weekly map is a 71-tile variant: an ore/desert-heavy visible
board with a single **gold/volcano hex** in the centre (the robber starts
on it) and ~18 **Black Forest fog hexes** around the rim.

A representative board scan (capture 2026-05-23):

```
14 sea/desert · 18 ORE · 6 WOOD · 6 BRICK · 4 SHEEP · 4 WHEAT · 1 GOLD · 18 FOG
```

## Rules (colonist.io)

- **Gold hex** — when its number rolls, every settlement/city touching it
  produces a resource of the owner's choice (Seafarers Gold River rule).
  Robber starts on it, so it pays nothing until the robber moves.
- **Fog hexes** — start hidden, reveal when a road/settlement lands
  adjacent. On reveal they flip to a real tile and the revealing player
  gets one free resource of that type.

## How CatanBot handles it

Tile types decode the same as classic Catan — empirically confirmed
against live production (see `scripts/volcano_calibrate.py`): the
ore-heavy reading is the real board, not a mapping bug.

- **Gold hex** (tile type 6) builds as a non-producing tile in catanatron
  (it has no "gold" resource, and `yield_resources` chokes on a numbered
  None-resource tile). Instead `colonist_map.annotate_gold_nodes` records
  the gold hex's nodes + number on the map, and the opening scorer values
  a gold-adjacent corner as a **wildcard** — yield weighted just above
  wheat, counted as an extra diversity slot. See `_GOLD_WEIGHT` in
  `advisor.py`.
- **Fog** (type 7) reveal is handled by the existing Black Forest
  reveal-event path.
- **Variant gate** — `variant_label()` returns `"volcano"` for a
  gold+fog board on a mapSetting; the recs gate (`_RECS_SAFE_VARIANTS`)
  lets it through, so opening picks + recommender run instead of being
  suppressed.

Regression tests in `tests/test_volcano.py` pin all of the above against
the real capture `ws_captures/volcano-2026-05-23.jsonl`.

### Known limitation

Replaying several *different* old games' GameStart frames in one bridge
process (e.g. a stale autosave that spans a Black Forest game and then a
Volcano game) can transiently drop a few mid-game build events on the
intermediate game instance, because the variant builds share
catanatron's module-level `STATIC_GRAPH`. A single clean game tracks
perfectly (verified: catanatron buildings == colonist's authoritative
corner ownership), and the graph self-heals on the latest GameStart, so
this only affects stacked stale-history replays, not live play. Start the
bridge on a clean autosave for a pristine current game.

## Fog placement pattern

Mining every fog reveal we have captured (43 reveals across Black Forest +
Volcano, `scripts/fog_patterns.py`):

- **Fog hides the resources the visible board is short on.** On the
  all-wood Black Forest boards, 31/32 reveals were non-wood; aggregate
  **77%** of reveals landed on a resource *absent* from the visible board.
  On Volcano (ore-heavy) the same logic points at the scarce
  wheat/sheep being hidden in fog.
- **Fog tiles carry strong numbers.** **65%** of reveals were on 5/6/8/9
  (vs ~40% if uniform), and 2/3/11/12 are rare. Fog is disproportionately
  *good* production.

Takeaway for play: on Volcano, **building roads into the fog ring is
positive-EV** — you tend to uncover well-numbered tiles of the scarce
(non-ore) resources you actually need, plus a free resource on reveal.
Sample sizes are small per board; treat as a strong lean, not a law.
