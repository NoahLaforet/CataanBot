# Where we left off (2026-04-20, late-day pass)

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
- **Tile resource icons** (`7a8d8bc`) — replaced "WHEAT" / "WOOD" text
  with small geometric icons per resource (wheat stalk, two-tier pine,
  wool body + dark head, staggered brick pile, faceted crystal, sand
  sun for desert). `render --labels text` keeps the old debug mode.
- **Port resource icons** (`4292d7e`) — 2:1 port markers now contain
  the resource icon + a compact "2:1" ratio instead of a text label.
- **Winner / near-winner callout** (`c1a0b47`) — tracker summary prints
  a status line at 8+ VP ("RED WINS at 10 VP" / "one turn from
  winning" / "two from winning") and the rendered legend strip shows
  a color-coded banner along the top. Silent below 8 VP.
- **Live-board opening advisor** (`f2eddf5`) — `openings --save game.json
  --color WHITE` filters the candidate pool to nodes WHITE can legally
  place on right now. Blocking + denial recalculated against that
  restricted pool.
- **Second-settlement render overlay** (`415d573`) — `secondadvice
  --render path.png` gold-markers the top-N picks on the tracker's
  board, matching the `openings --render` pattern.
- **Openings alternatives view** (`4ec06ec`) — `openings --after N1 N2 …`
  prints the baseline top-N plus a second ranking assuming those nodes
  are already claimed (each removes itself + its neighbors). Makes
  denial/blocking math visible as a before/after.
- **Icon polish v2** (`20e3372`) — sheep rebuilt as a fluffy oval body
  with wool bumps, dark head, and legs (no more caterpillar look);
  brick icon now has visible mortar gaps so the 2-over-1 wall pattern
  reads cleanly instead of merging into a mound.
- **Deterministic seed flag** (`adf8c1f`) — `render --seed N` and
  `openings --seed N` pin the fresh-game map so `--after` can compare
  against the same board across runs.
- **Claimed-node X markers** (`1a227bb`) — `openings --after … --render`
  draws gray X badges on the claimed nodes and shifts the gold ranked
  markers to the post-pick top-N so the PNG stands alone.
- **Hand-estimation replay** (`25fb339`) — new `cataanbot hands <save>`
  CLI + `hands` REPL command. Replays tracker history through
  `yield_resources` to produce per-color produced/spent/received
  buckets alongside the authoritative current hand, so you can spot
  "red's been rolling 6s all game and hasn't built anything" at a
  glance.
- **Auto-offer secondadvice after first settle** (`01fc76a`) — placing
  a color's first settlement in the REPL now prints the top-5
  second-settlement ranking automatically, so you don't have to run
  the advisor manually during the opening draft.
- **Test coverage** (`18536eb`, `2d6d264`, `2bb037d`) — 59 pytest cases
  covering tracker board ops + save/load/undo, advisor scoring,
  hands replay buckets, stats histogram, every CLI subcommand, VP
  callout tiers across thresholds, and the openings --after flow.
  `tests/conftest.py` injects src/ onto sys.path so tests run without
  the macOS-flaky .pth file.
- **CLI polish** (`07592b4`, `e92fa15`, `980b311`) — `--version` flag
  via importlib.metadata, gitignore now blocks root-level `*.png`
  outputs cleanly, README documents the full command surface + the
  launcher caveat.

## Running the tool
The packaged `.venv/bin/cataanbot` entry point is unreliable on macOS
(`UF_HIDDEN` keeps getting re-applied to pip-written .pth files). Use the
repo-local launcher instead:

```
./bin/cataanbot doctor
./bin/cataanbot render -o board.png --ticks 60
./bin/cataanbot render -o board.png --labels text    # old text labels
./bin/cataanbot openings --top 10 --render board_openings.png
./bin/cataanbot openings --save game.json --color WHITE --top 5
./bin/cataanbot openings --seed 777 --top 5 --after 6 --render after.png
./bin/cataanbot secondadvice game.json RED --render second.png
./bin/cataanbot stats game.json --histogram roll_hist.png
```

## Side notes (not blocking)
- Update the contributor script.
- Maybe send Karan an email.

## Natural next steps (pick any)
1. **Smoke-test with a real game** — we've pushed as close to a "final
   product" as autonomous work can reach without a live session. Sit
   down for a full game, drive the REPL, and file whatever the real
   use reveals.
2. **Structural cleanup** — `repl.py` is now ~650 lines with ~30 `do_*`
   commands. Splitting mutations / advisors / history / meta into
   mixins would make the file browsable. Pure refactor with real
   breakage risk; best done when you can smoke-test each command in
   the REPL yourself, not in an autonomous run.
3. **Dev-card advisor** — `should-i-buy-dev` for the current color:
   trivial "is 14+ cards left and you're short on VP push" heuristic,
   but would round out the advisor set. Low value — dev cards are
   inherently low-info.
4. **Opponent-hand heuristic beyond hands.py** — hands.py now gives
   per-color produced/spent/received buckets but nothing consumes that
   in the advisor layer yet. Wiring it into `robberadvice` (steal-EV
   weighted by produced total) and `tradeeval` (opponent scarcity
   guess) is the next step once live play validates the hands numbers.

## Files
- `src/cataanbot/cli.py` — `doctor`, `render`, `openings`, `play`,
  `robberadvice`, `tradeeval`, `secondadvice`, `stats`, `hands`
  subcommands, plus `--version`.
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
- `src/cataanbot/hands.py` — history-replay per-color hand accounting.
- `bin/cataanbot` — launcher that sidesteps the macOS `.pth` quirk.
- `tests/` — conftest + test_tracker + test_advisor + test_hands +
  test_stats + test_cli + test_cli_integration (59 cases, <0.5s).
- `TODO_VISUAL.md` — visual-polish backlog.
