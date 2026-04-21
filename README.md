# CataanBot

[![tests](https://github.com/NoahLaforet/CataanBot/actions/workflows/tests.yml/badge.svg)](https://github.com/NoahLaforet/CataanBot/actions/workflows/tests.yml)

A personal Settlers of Catan advisor. Renders a live board, tracks all players
as the game progresses, and suggests strong moves backed by expected-value
reasoning over dice probabilities and catanatron's simulation engine.

Built on top of [catanatron](https://github.com/bcollazo/catanatron) (Python
Catan engine) + [JSettlers](https://github.com/jdmonin/JSettlers2)-inspired
heuristics. No ML — strong handcrafted evaluation + search.

---

## Goals

- **Visual board** — render the full hex grid, robber, ports, roads,
  settlements, cities.
- **All-player tracking** — the advisor watches every move and updates the
  model of the game state, not just yours.
- **Actionable suggestions** — top-N ranked moves with an EV/score breakdown so
  you learn *why*, not just *what*.
- **General-purpose** — works for physical-board games (manual input),
  online games, and eventually colonist.io (scraped or extension-read).

## Non-goals (for now)

- ML personalization (no training on own games)
- Full Seafarers / Cities & Knights / custom maps (base-game 3–4 players only)
- Competing with JSettlers on strength — realistic aim is "removes dumb
  mistakes, quantifies EV."

---

## Roadmap

1. **Phase 1 — CLI + catanatron integration.** Load a game state, print it,
   generate legal moves, score them with a simple EV heuristic. No UI yet.
2. **Phase 2 — Visual board.** Python renderer (likely pygame or a small
   Flask + canvas frontend) showing the live state.
3. **Phase 3 — Manual state input.** Click tiles / edges / vertices to mirror
   a real game into the app turn-by-turn.
4. **Phase 4 — Stronger advisor.** Port JSettlers heuristics or wire MCTS over
   catanatron's state machine.
5. **Phase 5 — Screenshot CV path.** OpenCV + HSV classification for
   automatic board ingestion.
6. **Phase 6 — Chrome extension.** Read colonist.io DOM, talk to local
   advisor over `localhost`.

---

## Install

Requires Python 3.11+ (catanatron constraint). macOS/Linux.

```bash
git clone https://github.com/NoahLaforet/CataanBot.git
cd CataanBot
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
./bin/cataanbot --help
```

On macOS the packaged `.venv/bin/cataanbot` entry point can flake out when the
editable-install `.pth` file picks up an `UF_HIDDEN` flag from APFS. The
repo-local `./bin/cataanbot` launcher sidesteps that by setting
`PYTHONPATH=src/` explicitly — use it instead of the packaged entry point.

## Usage

```bash
# Render a fresh random board (with optional --seed N for reproducibility)
./bin/cataanbot render -o board.png

# Rank opening settlement spots on a fresh board
./bin/cataanbot openings --top 10 --render openings.png

# Manual-tracker REPL for mirroring a live game
./bin/cataanbot play
# inside the REPL: settle / city / road / roll / give / take / trade /
# mtrade / devbuy / devplay / robber / discard / build / undo / save / load
# advisors: openings-after, secondadvice, robberadvice, tradeeval, hands, stats

# Advisors over a saved game (produced by `save path.json` in the REPL)
./bin/cataanbot openings  --save game.json --color WHITE
./bin/cataanbot secondadvice game.json RED --render second.png
./bin/cataanbot robberadvice game.json RED
./bin/cataanbot tradeeval   game.json RED give 2 WOOD get 1 WHEAT
./bin/cataanbot hands       game.json
./bin/cataanbot stats       game.json --histogram hist.png
```

## colonist.io bridge (Phase 6, Day 1)

Stream colonist.io's in-game log to a local FastAPI bridge so the advisor
can eventually consume live play. Day 1 just proves the pipe — the bridge
prints each event to stdout.

```bash
# Install bridge deps (fastapi + uvicorn)
pip install -e '.[bridge]'

# Start the bridge
./bin/cataanbot bridge                         # 127.0.0.1:8765
./bin/cataanbot bridge --jsonl ~/cataan.jsonl  # also mirror to disk
```

Install the userscript once in Tampermonkey (or Violentmonkey):

1. Install the Tampermonkey browser extension.
2. Open `userscript/colonist_cataanbot.user.js` in the repo, copy the
   contents, and paste into a new Tampermonkey script. Save.
3. Confirm it's enabled on `colonist.io/*`.
4. Start a game. The bridge terminal should print events as they happen.

The userscript watches `div.virtualScroller-lSkdkGJi` (the log panel's
virtualized list) via a MutationObserver and POSTs each new entry to
`http://127.0.0.1:8765/log` as structured JSON — text, colored name
pills, and icon `alt` values. See `COLONIST_RECON.md` for the DOM spec.

## Replaying a captured JSONL (offline)

The bridge's `--jsonl` mirror gives you a replayable log of any captured
game. `cataanbot replay` walks that file through the Event → Tracker
dispatcher — useful for auditing past games without booting colonist.

```bash
# Auto-assign colors in first-appearance order
./bin/cataanbot replay ~/Desktop/cataanbot-game5.jsonl

# Pin colors explicitly, save final state, render the board
./bin/cataanbot replay game.jsonl \
  --player BrickdDaddy=BLUE --player Thorin=ORANGE \
  --save replayed.json --render replayed.png -v

# Postmortem: winner, final VP, per-player aggregates, dice histogram,
# and parser-quality breakdown — all derived from the event stream.
./bin/cataanbot replay game.jsonl --report

# Write the same postmortem to a file instead of stdout.
./bin/cataanbot replay game.jsonl --report-out game5-report.txt

# Render a per-event VP timeline PNG (step chart, one line per seat,
# dashed 10-VP win line, minute x-axis when the JSONL has timestamps).
./bin/cataanbot replay game.jsonl --vp-chart vp.png

# Render a cumulative-production PNG — total cards received from rolls
# per player, over time. Shows who had the economic lead and when
# the dice shifted it.
./bin/cataanbot replay game.jsonl --production-chart prod.png

# Render the dice-fairness bar chart — actual vs. expected roll counts
# per value 2-12. Ghost outlines show the 2d6 expectation; filled bars
# are actual; signed delta labels above each bar.
./bin/cataanbot replay game.jsonl --dice-chart dice.png

# One-shot postmortem: single self-contained HTML file with the
# full text report and all three charts embedded as base64 PNGs.
./bin/cataanbot replay game.jsonl --postmortem game5.html
```

Events that need board topology (settlement / city / road placements and
robber moves) are currently reported as `unhandled` — once the
DOM-to-catanatron-node mapping lands, they'll flow through too.

## Development

Tests are plain pytest; the `tests/conftest.py` shim puts `src/` on the path so
they run without an editable install.

```bash
.venv/bin/python -m pytest
```

## License

GPL-3.0. catanatron is GPL-3.0; this project depends on it, so the derivative
license applies. See [LICENSE](LICENSE).
