# Where we left off (2026-04-20)

## Done this session
- **Port rendering** — commit `5760fd0`, pushed. Dock lines + circle markers
  around the coast labeled "WOO 2:1", "ORE 2:1", "3:1", etc.
- **Settlement / city / road rendering** — code written, not yet committed.
  Pieces draw in player colors (RED/BLUE/WHITE/ORANGE). Roads are outlined
  thick colored segments; settlements are small houses; cities are L-profile
  towers (distinct from settlements).
- **`--ticks N` option** on `cataanbot render` so we can preview a non-empty
  board (otherwise a fresh Game has no buildings).
- **Visual polish backlog** — `TODO_VISUAL.md` captures Noah's ask to make
  this look like a real interface later (icons instead of text, textures,
  shadows, etc.). Tackle after core advisor features.

## Known issue to fix next session
- `.venv/bin/cataanbot` entry point is broken because all `.pth` files in
  `.venv/lib/python3.12/site-packages/` have the macOS `UF_HIDDEN` flag set,
  so site.py skips them. Running `chflags nohidden` + `xattr -c` didn't
  clear it — something (likely Apple's provenance/quarantine system) keeps
  re-flagging them, and a real fix may need a fresh venv outside of
  `~/Desktop/` or `sudo chflags`.
- **Workaround used this session**: `PYTHONPATH=./src .venv/bin/python -m
  cataanbot.cli render -o board.png --ticks 60` — this works.

## Next tasks (in order)
1. Resolve the `UF_HIDDEN` venv issue (or recreate venv elsewhere, or ship
   a wrapper script that sets PYTHONPATH).
2. Commit settlements/cities/roads + `--ticks` flag + `TODO_VISUAL.md`.
3. Task #3 from the original list: **pip dots under number tokens** (classic
   Catan visual — 2 and 12 get one dot, 3/11 two, up to 6/8 five dots).
4. First real advisor feature: **score legal initial settlement spots** by
   summing pip value of adjacent tiles, deduplicating port access. Print the
   top 5 with node IDs.

## Sample renders from this session
- `board_initial.png` — post-initial-placement (`--ticks 60`) — 8 settlements,
  8 roads, all 4 player colors visible.
- `board_midgame.png` — deeper into a random game (`--ticks 400`) — robber
  has moved off desert, but random players don't upgrade to cities often.

## Files touched (uncommitted)
- `src/cataanbot/render.py` — added `PLAYER_COLORS`, `_draw_road`,
  `_draw_settlement`, `_draw_city`, and the building/road draw loop.
- `src/cataanbot/cli.py` — added `--ticks` option to `render`.
- `TODO_VISUAL.md` — new.
