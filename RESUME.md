# Where we left off (2026-04-20)

## Shipped so far
- Ports around the coast (commit `5760fd0`)
- Settlements / cities / roads in player colors (commit `fca86d2`)
- Pip dots under number tokens + `bin/cataanbot` launcher (commit `60f9f16`)
- **Opening-settlement advisor** (commit `c55c32f`) — `cataanbot openings`
  prints the top-N pip-ranked nodes on a fresh map, with `--render` to
  overlay numbered gold markers on the board PNG.
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

## Running the tool
The packaged `.venv/bin/cataanbot` entry point is unreliable on macOS
(`UF_HIDDEN` keeps getting re-applied to pip-written .pth files). Use the
repo-local launcher instead:

```
./bin/cataanbot doctor
./bin/cataanbot render -o board.png --ticks 60
./bin/cataanbot openings --top 10 --render board_openings.png
```

## Side notes (not blocking)
- Update the contributor script.
- Maybe send Karan an email.

## Natural next steps (pick any)
1. **Advisors that read the tracker** — the whole point of the tracker
   was to feed real state into advisors. First candidate: robber-move
   recommendation (score each tile by `opponent_pips_blocked − own_pips_blocked`,
   with a tiebreak on leader VP / largest hand). Second: trade-evaluator
   ("is this trade good for me?") using marginal pip value per resource.
2. **Better opening heuristic** — current score is raw pip sum. Add:
    - Resource diversity bonus (a spot giving brick+wheat+sheep beats one
      giving three wheats of equal pips — early-game flexibility).
    - Port bonus weighted by the player's production of that resource.
    - Blocking bonus (deny rivals the best adjacent spot).
3. **Second-settlement advisor** — given the first settlement is placed,
   rank the second (and opening road) considering longest-road potential
   and resource complementarity.
4. **Tracker conveniences** (small) — `discard COLOR N1 RES1 N2 RES2...`
   wrapper for 7-rolls; `build COLOR settle|city|road ...` that auto-debits
   the cost; a turn pointer so `dev-per-turn` limits can be enforced.
   None are blocking — all doable today with the primitives we have.
5. **Visual polish** (see `TODO_VISUAL.md`) — resource icons instead of
   text labels, port icons, piece shading. Noah explicitly asked for this
   to look like a real interface, not a debug printout.

## Files
- `src/cataanbot/cli.py` — `doctor`, `render`, `openings`, `play` subcommands.
- `src/cataanbot/render.py` — Pillow board renderer, supports
  `highlight_nodes` for advisor overlays.
- `src/cataanbot/advisor.py` — `score_opening_nodes`, `format_opening_ranking`.
- `src/cataanbot/tracker.py` — `Tracker` class, the core mirror layer.
  Seed + op-history architecture: every mutation appends an op dict
  only on success, undo/save/load all go through `_replay`.
- `src/cataanbot/repl.py` — `TrackerRepl(cmd.Cmd)` with all commands
  (settle/city/road/robber/roll/give/take/devbuy/devplay/trade/mtrade/
  undo/save/load/show/render/autorender/colors/quit).
- `bin/cataanbot` — launcher that sidesteps the macOS `.pth` quirk.
- `TODO_VISUAL.md` — visual-polish backlog.
