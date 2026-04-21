# Where we left off (2026-04-20)

## Shipped so far
- Ports around the coast (commit `5760fd0`)
- Settlements / cities / roads in player colors (commit `fca86d2`)
- Pip dots under number tokens + `bin/cataanbot` launcher (commit `60f9f16`)
- **Opening-settlement advisor** (commit `c55c32f`) — `cataanbot openings`
  prints the top-N pip-ranked nodes on a fresh map, with `--render` to
  overlay numbered gold markers on the board PNG. Upgraded in `1a8c9b7`
  (diversity + port), `39a5df3` (denial bonus for cluster-center picks),
  and `195ea7e` (forward-lookahead blocking bonus that measures how much
  your pick degrades the remaining top-K spots).
- **Manual-tracker REPL** (`cataanbot play`) — mirror a real game into
  a catanatron `Game` and render it live. Tracker is mirror-not-referee
  by design. Core mutations: settle/city/road/robber/roll/give/take/
  devbuy/devplay/trade/mtrade. Convenience layer (`d5d7f1f`) adds
  `discard` (multi-resource atomic 7-roll helper), `build` (auto-debits
  the cost after a successful placement), and a cosmetic `turn` pointer
  shown in the prompt.
- **Advisors that read live tracker state** — `robberadvice`,
  `tradeeval`, `secondadvice` with matching CLI subcommands that
  accept a `.json` save so the analysis can run off a live game.
- **Saved-state CLI advisors** (`7a9df27`) — all three above +
  `cataanbot stats` for dice-roll analysis.
- **Dice-roll stats** (`b64c9f9`) — `stats [path.png]` in the REPL and
  `cataanbot stats <save> [--histogram path.png]` on the CLI. Replays
  history to produce actual-vs-expected histogram, per-color resources
  delivered, per-tile production counts with a robbed column. PNG
  histogram available via flag. `a048f5f` embeds a compact 2-line
  version inside `show`.
- **Legend strip on every render** (`aed1bf6`) — per-color VP, building
  counts, LR/LA badges along the bottom. Backed by `ae43cd8` which
  recomputes `P{i}_VICTORY_POINTS` on every settle/city.
- **Longest road / largest army auto-detection** (`86e8dbf`) — every
  road build, opponent settlement (road-breakers), and knight play
  refreshes `HAS_ROAD` / `HAS_ARMY` with the standard ≥5 / ≥3
  thresholds and a "must strictly exceed holder" rule. Legend badges
  and VP totals update accordingly.
- **Visual polish v1** (`fe6d533`) — drop shadows on pieces and number
  tokens, rounded road end caps + shadow segment, vertical ocean
  gradient. No new dependencies — all pure PIL.

## Running the tool
The packaged `.venv/bin/cataanbot` entry point is unreliable on macOS
(`UF_HIDDEN` keeps getting re-applied to pip-written .pth files). Use the
repo-local launcher instead:

```
./bin/cataanbot doctor
./bin/cataanbot render -o board.png --ticks 60
./bin/cataanbot openings --top 10 --render board_openings.png
./bin/cataanbot stats game.json --histogram roll_hist.png
```

## Side notes (not blocking)
- Update the contributor script.
- Maybe send Karan an email.

## Natural next steps (pick any)
1. **Tile resource icons** (TODO_VISUAL) — swap the "WHEAT" / "WOOD"
   text label for a small geometric icon on each hex. Keep text behind
   a flag for debugging. Biggest remaining visual-polish win.
2. **Port resource icons** — same idea for the little port circles.
3. **Winner / near-winner callout** — `show` could loudly announce
   "RED at 10 VP — game over" or "BLUE at 9 — one turn from winning".
   Data is already there.
4. **Opening-advisor on live boards** — right now `openings` only works
   on fresh random games. A CLI variant that loads a tracker save and
   respects pieces already placed (filtering candidates by distance
   rule and removing taken spots) would make it useful during draft.
5. **Structural cleanup** — `repl.py` is getting long (~650 lines);
   moving command groups (mutations / advisors / history / meta) into
   mixins would make the file browsable again.

## Files
- `src/cataanbot/cli.py` — `doctor`, `render`, `openings`, `play`,
  `robberadvice`, `tradeeval`, `secondadvice`, `stats` subcommands.
- `src/cataanbot/render.py` — Pillow board renderer: board + legend
  strip, drop shadows, ocean gradient, `highlight_nodes` overlay for
  advisor recommendations.
- `src/cataanbot/advisor.py` — `score_opening_nodes` (base + diversity
  + port + denial + blocking), `score_robber_targets`,
  `evaluate_trade`, `score_second_settlements`, plus helpers.
- `src/cataanbot/stats.py` — `compute_stats` replays tracker history,
  `format_stats` / `format_mini_histogram` for terminal,
  `render_histogram` for the PNG bar chart.
- `src/cataanbot/tracker.py` — `Tracker` class, the core mirror layer.
  Seed + op-history architecture. `_recompute_vp`,
  `_recompute_longest_road`, `_recompute_largest_army` keep derived
  state honest across direct builds.
- `src/cataanbot/repl.py` — `TrackerRepl(cmd.Cmd)` with all commands.
- `bin/cataanbot` — launcher that sidesteps the macOS `.pth` quirk.
- `TODO_VISUAL.md` — visual-polish backlog.
