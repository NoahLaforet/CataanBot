# Where we left off (2026-04-20)

## Shipped so far
- Ports around the coast (commit `5760fd0`)
- Settlements / cities / roads in player colors (commit `fca86d2`)
- Pip dots under number tokens + `bin/cataanbot` launcher (commit `60f9f16`)
- **Opening-settlement advisor** (commit `c55c32f`) — `cataanbot openings`
  prints the top-N pip-ranked nodes on a fresh map, with `--render` to
  overlay numbered gold markers on the board PNG. Upgraded in `1a8c9b7`
  (diversity + port) and `39a5df3` (denial bonus for cluster-center picks).
- **Manual-tracker REPL** (`cataanbot play`) — mirror a real game into
  a catanatron `Game` and render it live. Commits:
    - `3992adb` — MVP: settle/city/road/robber, auto-render
    - `f00a291` — undo, save, load (seed + op-history replay)
    - `31177c4` — dice rolls (distributes via `yield_resources`)
    - `bb33938` — give/take for trades, steals, discards, monopoly
    - `45142ba` — dev card buy/play (with `_IN_HAND` / `PLAYED_` split)
    - `a2ffd57` — atomic `trade` + `mtrade` (maritime)
  The tracker is **mirror-not-referee**: no turn/phase enforcement, no
  auto-debit of build costs, no port eligibility checks. User drives,
  we record. That's the whole point — so weird edge cases (custom rules,
  someone miscounting, partial replays) don't block the tool.
- **Advisors that read live tracker state** — REPL commands that consume
  whatever the tracker currently has:
    - `877d797` — `robberadvice COLOR` scores every land tile by
      `opponent_pips_blocked - own_pips_blocked`, tiebreak on victim hand
      size (steal EV).
    - `e21f948` — `tradeeval COLOR N RES_OUT M RES_IN` computes marginal
      resource values from the color's production and port access, says
      favorable / even / unfavorable.
    - `fdf9487` — robber advisor now VP-weights each victim so a 9-VP
      leader's pips count ~3.4× a 3-VP trailing player's.
    - `985de41` — `secondadvice COLOR [first_node]` ranks complement
      picks for the 2nd settlement, paired with `3bfd044` which picks
      the best opening road direction beside each candidate.
- **Legend strip on every render** (`aed1bf6`) — per-color VP, building
  counts, LR/LA badges along the bottom. Backed by `ae43cd8` which
  recomputes `P{i}_VICTORY_POINTS` on every settle/city (catanatron only
  updates VP during tick-play; without this, legend + robber VP
  weighting were silently reading 0).
- **Saved-state CLI advisors** (`7a9df27`) — `cataanbot robberadvice`,
  `tradeeval`, `secondadvice` all take a `.json` tracker save as first
  positional arg, so you can run analysis on a position without
  re-entering the REPL.
- **Dice-roll stats** (`b64c9f9`) — `stats [path.png]` in the REPL and
  `cataanbot stats <save> [--histogram path.png]` on the CLI. Replays
  history to produce (a) actual-vs-expected histogram with a terminal
  bar chart, (b) per-color resources actually delivered by dice, (c)
  per-tile production counts with a robbed-count column. PNG output is
  a real bar chart with theoretical expected ticks overlaid.

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
1. **Blocking bonus in opening advisor** (only idea left from the early
   roadmap). Current denial is a per-node tiebreaker; a fuller version
   would score the top N-1 *remaining* spots after your hypothetical
   pick and favor picks that lock out opponents' best alternates.
2. **Tracker conveniences** (small) — `discard COLOR N1 RES1 N2 RES2...`
   wrapper for 7-rolls; `build COLOR settle|city|road ...` that auto-debits
   the cost; a turn pointer so `dev-per-turn` limits can be enforced.
   None are blocking — all doable today with the primitives we have.
3. **Visual polish** (see `TODO_VISUAL.md`) — resource icons instead of
   text labels, port icons, piece shading. Noah explicitly asked for this
   to look like a real interface, not a debug printout.
4. **Mini-histogram on `show`** — embed a compact one-line bar chart of
   roll frequencies inside the REPL's `show` summary so you get a
   luck-read at a glance without typing `stats`. The underlying compute
   is already there in `stats.compute_stats`.
5. **Longest-road / largest-army detection** — tracker doesn't currently
   set `HAS_ROAD` / `HAS_ARMY`, so the legend's LR/LA badges never
   light up. A simple pass after each road-build / knight-play would
   fix this; catanatron's graph utilities can do the LR walk.

## Files
- `src/cataanbot/cli.py` — `doctor`, `render`, `openings`, `play`,
  `robberadvice`, `tradeeval`, `secondadvice`, `stats` subcommands.
- `src/cataanbot/render.py` — Pillow board renderer, supports
  `highlight_nodes` for advisor overlays and `show_legend` for the
  bottom-panel scoreboard.
- `src/cataanbot/advisor.py` — `score_opening_nodes`,
  `score_robber_targets`, `evaluate_trade`, `score_second_settlements`,
  plus `player_production` and `player_ports` helpers. Each has a
  matching `format_*` printer.
- `src/cataanbot/stats.py` — `compute_stats` replays tracker history,
  `format_stats` prints the terminal view, `render_histogram` writes
  the PNG bar chart.
- `src/cataanbot/tracker.py` — `Tracker` class, the core mirror layer.
  Seed + op-history architecture: every mutation appends an op dict
  only on success, undo/save/load all go through `_replay`.
  `_recompute_vp()` keeps `VICTORY_POINTS` honest across direct builds.
- `src/cataanbot/repl.py` — `TrackerRepl(cmd.Cmd)` with all commands:
  mutation (settle/city/road/robber/roll/give/take/devbuy/devplay/
  trade/mtrade), advisors (robberadvice/tradeeval/secondadvice),
  stats, history (undo/save/load), meta (show/render/autorender/
  colors/quit).
- `bin/cataanbot` — launcher that sidesteps the macOS `.pth` quirk.
- `TODO_VISUAL.md` — visual-polish backlog.
