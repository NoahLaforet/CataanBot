# Where we left off (2026-04-20)

## Shipped so far
- Ports around the coast (commit `5760fd0`)
- Settlements / cities / roads in player colors (commit `fca86d2`)
- Pip dots under number tokens + `bin/cataanbot` launcher (commit `60f9f16`)
- **Opening-settlement advisor** (commit `c55c32f`) — `cataanbot openings`
  prints the top-N pip-ranked nodes on a fresh map, with `--render` to
  overlay numbered gold markers on the board PNG. Upgraded in `1a8c9b7`:
  score is now `raw * diversity + port_bonus`, so a brick+wheat+sheep
  spot beats a three-wheat spot at equal pips and coastal ports
  break ties among otherwise-similar candidates.
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
      leader's pips count ~3.4× a 3-VP trailing player's. Fixes the
      common case where the pip king isn't the right block target.

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
1. **Second-settlement advisor** — the opening advisor only ranks spots
   in isolation. Given that settlement #1 is already placed, settlement
   #2 should be scored for resource *complementarity* with #1 (fill in
   missing commodities, shoot for 3:1 port if you'd already produce it)
   and initial road placement for longest-road potential.
2. **Blocking bonus in opening advisor** — current score is per-node.
   A "best move" in opening is also "denying the rivals' best spot."
   Extend by scoring the top N-1 *remaining* spots after your pick and
   favoring picks that constrain opponents.
3. **CLI exposure of tracker advisors** — today `robberadvice` and
   `tradeeval` only run inside the REPL. Add `cataanbot robberadvice
   --save path.json COLOR` so they work against a saved state without
   a live REPL (useful for analysis after the game).
4. **Tracker conveniences** (small) — `discard COLOR N1 RES1 N2 RES2...`
   wrapper for 7-rolls; `build COLOR settle|city|road ...` that auto-debits
   the cost; a turn pointer so `dev-per-turn` limits can be enforced.
   None are blocking — all doable today with the primitives we have.
5. **Visual polish** (see `TODO_VISUAL.md`) — resource icons instead of
   text labels, port icons, piece shading. Noah explicitly asked for this
   to look like a real interface, not a debug printout.
6. **In-game dice-roll stats / analytics** — Noah asked 2026-04-20:
   while a game is running, show post-game-style stats (roll frequency
   histogram vs. theoretical, per-tile production actually delivered,
   resource totals distributed). Could be `stats` REPL command that
   prints text + optionally writes a PNG histogram, and/or embed a
   mini-histogram in `show`. The tracker already records every `roll`
   op in history, so the data's already there — just needs a reader.

## Files
- `src/cataanbot/cli.py` — `doctor`, `render`, `openings`, `play` subcommands.
- `src/cataanbot/render.py` — Pillow board renderer, supports
  `highlight_nodes` for advisor overlays.
- `src/cataanbot/advisor.py` — `score_opening_nodes`,
  `score_robber_targets`, `evaluate_trade`, plus `player_production`
  and `player_ports` helpers. Each has a matching `format_*` printer.
- `src/cataanbot/tracker.py` — `Tracker` class, the core mirror layer.
  Seed + op-history architecture: every mutation appends an op dict
  only on success, undo/save/load all go through `_replay`.
- `src/cataanbot/repl.py` — `TrackerRepl(cmd.Cmd)` with all commands:
  mutation (settle/city/road/robber/roll/give/take/devbuy/devplay/
  trade/mtrade), advisors (robberadvice/tradeeval), history
  (undo/save/load), and meta (show/render/autorender/colors/quit).
- `bin/cataanbot` — launcher that sidesteps the macOS `.pth` quirk.
- `TODO_VISUAL.md` — visual-polish backlog.
