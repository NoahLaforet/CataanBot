# Where we left off (2026-04-20)

## Shipped so far
- Ports around the coast (commit `5760fd0`)
- Settlements / cities / roads in player colors (commit `fca86d2`)
- Pip dots under number tokens + `bin/cataanbot` launcher (commit `60f9f16`)
- **Opening-settlement advisor** (commit `c55c32f`) — `cataanbot openings`
  prints the top-N pip-ranked nodes on a fresh map, with `--render` to
  overlay numbered gold markers on the board PNG.

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
1. **Track all players** (core roadmap phase 3) — manual state input so the
   tool can score the actual board in front of Noah, not just a random sim.
   Could be a tiny REPL that takes "settle RED @ 10", "road BLUE @ 8-27",
   "roll 8", etc., and updates a real `Game` via catanatron's action API.
2. **Better opening heuristic** — current score is raw pip sum. Add:
    - Resource diversity bonus (a spot giving brick+wheat+sheep beats one
      giving three wheats of equal pips — early-game flexibility).
    - Port bonus weighted by the player's production of that resource.
    - Blocking bonus (deny rivals the best adjacent spot).
3. **Advisor for the second settlement** — given the first settlement is
   placed, rank the second (and opening road) considering longest-road
   potential and resource complementarity.
4. **Robber recommendation** — when a 7 is rolled or a knight played,
   which tile should we block?
5. **Visual polish** (see `TODO_VISUAL.md`) — resource icons instead of
   text labels, port icons, piece shading. Noah explicitly asked for this
   to look like a real interface, not a debug printout.

## Files
- `src/cataanbot/cli.py` — `doctor`, `render`, `openings` subcommands.
- `src/cataanbot/render.py` — Pillow board renderer, supports
  `highlight_nodes` for advisor overlays.
- `src/cataanbot/advisor.py` — `score_opening_nodes`, `format_opening_ranking`.
- `bin/cataanbot` — launcher that sidesteps the macOS `.pth` quirk.
- `TODO_VISUAL.md` — visual-polish backlog.
