# CatanBot

[![tests](https://github.com/NoahLaforet/CatanBot/actions/workflows/tests.yml/badge.svg)](https://github.com/NoahLaforet/CatanBot/actions/workflows/tests.yml)

A live Settlers of Catan advisor for [colonist.io](https://colonist.io).
A Chrome extension streams the in-browser game (DOM log + raw
WebSocket frames) to a local FastAPI bridge that runs the strategy
engine; the HUD renders in Chrome's right-side panel — no overlap
with the game board.

Built on top of [catanatron](https://github.com/bcollazo/catanatron)
(Python Catan engine) — handcrafted heuristics + 1-ply state-eval
search, no ML.

## Architecture

```
   ┌──────────────────────────┐
   │      colonist.io         │
   │   (browser game tab)     │
   └──────────┬───────────────┘
              │
       inject.js           content.js
   (page-world WS hook)  (DOM-log scraper)
              │                 │
              └────────┬────────┘
                       ▼
              chrome runtime
                       │
                       ▼
              background.js   ← Chrome MV3 service worker
                       │
                       ▼
              POST /ws  +  POST /log
                       │
                       ▼
              ┌────────────────────────┐
              │   FastAPI bridge       │  127.0.0.1:8765
              │   (Python, local)      │
              │                        │
              │  • event parser        │
              │  • catanatron tracker  │
              │  • recommender         │
              │  • postmortem render   │
              └────────┬───────────────┘
                       │
              GET /advisor  (snapshot, polled ~1Hz)
                       │
                       ▼
              ┌────────────────────────┐
              │   Chrome side panel    │  panel.js / panel.css
              │   (the HUD)            │
              └────────────────────────┘
```

The bridge runs locally — your game state never leaves your
machine. The Chrome extension's only network destination is
`127.0.0.1:8765`.

---

## What it does, today

- **Live opening picks (1st + 2nd settlement).** Ranks every legal
  corner by complement-aware production, port adjacency, denial value
  of distance-2 neighbours, and resource diversity. Pairs each pick
  with a follow-up road suggestion.
- **In-game build recommender.** Top-N action ranking with score
  breakdown — settlement / city / road / dev card / propose-trade /
  port trade. Each rec carries a 1-ply EV delta (state-eval rerank)
  so you can see *how much better* the bot's pick is than the
  alternative.
- **Dev-card play hints.** Knight / Monopoly / Year of Plenty /
  Road-Building each get a typed PLAY/HOLD verdict with conversational
  reasoning ("an opp is close to Largest Army — play to deny", "robber's
  on you — play to clear it"). VP cards are tracked but not surfaced
  as playable. Catan's no-play-on-buy-turn rule is respected.
- **Robber & 7-roll telemetry.** Top robber targets ranked by
  blocking value × victim VP × hand size. Auto-detects colonist's
  optional **Friendly Robber** rule and filters protected (≤2 VP)
  victims out of the suggestions. Auto-detects when the robber sits
  on one of your own tiles.
- **Live HUD overlay.** Roll histogram with 36-roll baseline, eval
  sparkline (chess-style position graph), per-build move-quality
  annotation (`!! / ! / ?! / ? / ??` chess grading vs the bot's top
  picks at decision time), opponent hand inference + production rate.
- **Variant board support.** Same Catan rules, different layouts —
  weekly-rotation maps like **Pond** (24 tiles, interior lake) and
  **Twirl** (42 tiles, swirl-shape) build a fresh catanatron CatanMap
  from colonist's authoritative tile/edge data. Opening picks,
  recommender, and 2:1 port trade rates all work on the actual
  geometry. Auto-detects colonist's dual-GameStart pattern (placeholder
  19-tile frame followed by the real shape) and rebuilds cleanly.
- **Strategy biases backed by data.** Five tunings in the recommender
  inspired by [u/Hot-Rooster1675's 36k-game simulation](https://www.reddit.com/r/boardgames/comments/1ssk2y0/i_simulated_36000_games_of_catan_some/):
  3rd-settle expansion bump (winners build #3 ~7 turns earlier),
  wheat-priority weight on opening eval (wheat is in every major
  build), composition-over-pips diversity bonus, longest-road push
  surfaced 1–2 roads from qualifying, brick-port early-pickup bonus
  on 2:1 ports for owned resources.
- **Strategy archetype tracker (v2).** Once both opening settlements
  land, the bot detects which archetype your placements actually
  enable — Ore-Wheat-Sheep, Longest Road rush, Port trader, Road
  Builder, or Balanced — and biases recommendations toward it.
  Each tag carries a one-line rationale; the HUD also surfaces a
  *strategy ranking* showing all 5 archetype scores so you can see
  how close the runners-up came. Pre-placement (before settles
  drop) the same ranking renders as a *board affinity* read so you
  can pick your first settle to align with whatever the board
  favors. Driven by tournament-player feedback on the Reddit thread
  above; full plan in [`docs/strategy_v2_plan.md`](docs/strategy_v2_plan.md).
- **Mid-game pivot triggers.** Hot-number streaks on your tiles,
  road-building dev card drawn, monopoly drawn, an opp closing on
  Largest Army, an opp crossing the close-to-win VP threshold, or
  a 7 going overdue with a heavy hand — each fires a named trigger
  that surfaces below the strategy banner so you see *why* the bot
  is shifting its bias mid-game. Some triggers (road-building
  drawn → Longest Road rush) carry a strategy override.
- **Tournament-grade nuance.** Knight-hold rules don't burn your
  first knight on a weak (2/3/11/12) robber tile; the robber
  scorer rewards moves that steal a resource you actually need
  next turn or set up a monopoly; port valuation halves on
  weak-pip tile alignment and dampens further when the table is
  short on the resource (you can extract player trades for it
  instead); Largest Army splits into defend / snipe / pass states;
  Longest Road splits into setup phase (fire only at 1 road out)
  vs commit phase (fire at ≤2 roads out, late game). All grounded
  in the same Reddit thread's top reply from a Catan World
  Tournament competitor.
- **Auto-postmortem.** When a game ends, a self-contained HTML report
  is written to `postmortems/` — winner, final VP breakdown, dice
  fairness, hand-dynamics, trade quality, 7-roll impact, plus the
  charts (VP timeline, dice distribution, hand size, cumulative
  production) embedded as base64 PNGs.

---

## Quick start

Requires Python 3.11+ (catanatron constraint). macOS / Linux / WSL.
Three commands to get a friend playing:

```bash
git clone https://github.com/NoahLaforet/CatanBot.git
cd CatanBot
./bin/catanbot live
```

The launcher script auto-creates a `.venv`, installs everything
from `pyproject.toml` (including the FastAPI bridge), and starts
the bridge on `127.0.0.1:8765` with the live advisor on. First
run takes ~30s; subsequent runs start in <2s.

If `./bin/catanbot` complains about Python, install **Python 3.11+**
first:

- macOS: `brew install python@3.12`
- Ubuntu/Debian: `sudo apt install python3.12 python3.12-venv`
- Windows: install from [python.org](https://www.python.org/downloads/)
  and run from WSL (the launcher is bash-only)

Then load the Chrome extension:

1. Open `chrome://extensions` in Chrome.
2. Toggle **Developer mode** (top-right).
3. Click **Load unpacked** and pick the `extension/` folder.
4. Pin the green CatanBot icon to the toolbar (puzzle-piece →
   pin) and click it — the side panel opens.
5. Open colonist.io, start a game. The bridge terminal logs each
   event; the panel renders the HUD live.

To update: `git pull`, then reload ⟳ on the CatanBot card in
`chrome://extensions`.

> **Heads-up for friends:** the bridge runs entirely on your
> machine. Game state never leaves `127.0.0.1`. The extension's
> only network destination is the local bridge.

## Manual install (if the launcher gives you trouble)

```bash
git clone https://github.com/NoahLaforet/CatanBot.git
cd CatanBot
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[bridge]'
./bin/catanbot live           # or: catanbot live
```

The `[bridge]` extras pull in FastAPI + uvicorn. If you want
**uv** instead of `pip` + `venv`:

```bash
git clone https://github.com/NoahLaforet/CatanBot.git
cd CatanBot
uv sync --extra bridge
uv run catanbot live
```

> On macOS the packaged `.venv/bin/catanbot` entry point can flake
> when the editable-install `.pth` file picks up an `UF_HIDDEN` flag
> from APFS. The repo-local `./bin/catanbot` launcher sidesteps that
> by setting `PYTHONPATH=src/` explicitly — use it instead of the
> packaged entry point.

## Other ways to run the bridge

```bash
# Live advisor with custom WS-frame mirror (lets you replay
# the game offline later via `catanbot replay`)
./bin/catanbot bridge --advisor --ws-jsonl ws_captures/$(date +%Y-%m-%d).jsonl
```

A Chrome Web Store listing is in preparation (see
`docs/STORE_LISTING.md` for the submission checklist; build a
fresh upload zip with `./bin/build-extension-zip.sh`). Once the
listing is live, users will get automatic updates within ~24h of
each tagged release without needing the manual reload step above.

> The Tampermonkey userscript that used to ship alongside the
> extension is archived under `legacy/userscript/`. It's no longer
> maintained — the Chrome extension is the only supported HUD path.

## Offline tools

```bash
# Render a fresh random board
./bin/catanbot render -o board.png

# Rank opening settlement spots
./bin/catanbot openings --top 10 --render openings.png

# Manual-tracker REPL
./bin/catanbot play

# Replay a captured WS jsonl
./bin/catanbot replay capture.jsonl --report --postmortem game.html
```

## Repo layout

```
src/catanbot/        bridge, recommender, tracker, render, advisor
extension/            Chrome side-panel extension (Manifest V3)
tests/                pytest, ~690 tests covering parsing, dispatch,
                      tracker arithmetic, recommender heuristics,
                      strategy selector + pivot triggers,
                      bridge snapshot shapes
docs/                 design notes — DOM/WS protocol recon (colonist),
                      HUD design principles
legacy/userscript/    archived Tampermonkey HUD — frozen snapshot,
                      no longer maintained; use the extension
ws_captures/          local WS jsonl mirrors (gitignored, big files)
postmortems/          auto-generated game-end HTML (gitignored)
```

## Development

```bash
.venv/bin/python -m pytest        # ~2s, ~690 tests
node --check extension/panel.js
```

CI runs `pytest` on every push (see `.github/workflows/tests.yml`).

## What this project is — and isn't

CatanBot is a **decision support tool** for live colonist.io games.
It reads the page (DOM log + WebSocket frames) and renders
recommendations into a side panel. It does not click for you, it
does not interact with colonist's game state, it does not bypass
any UI rules — every action still happens through your own clicks.
Think of it as a chess-engine analysis bar in the side panel, not
a bot that plays autonomously.

The strategy engine is heuristic, not ML, not perfect. The
move-quality classifier (`!! / ! / ?! / ? / ??`) compares your
actual play against the engine's top recommendation at decision
time — useful as honest feedback after the fact, not as a claim
of objective truth.

## Acknowledgements

- [catanatron](https://github.com/bcollazo/catanatron) — the Python
  Catan engine that does the heavy lifting under the recommender.
- [u/Hot-Rooster1675's 36,000-game simulation](https://www.reddit.com/r/boardgames/comments/1ssk2y0/i_simulated_36000_games_of_catan_some/)
  — five of the recommender's strategy biases come from that data
  (3rd-settle timing, wheat priority, composition over pips, LR
  push, port-bonus tempering).
- **u/chalks777**, multi-time Catan World Tournament competitor —
  the [extended top reply](https://www.reddit.com/r/boardgames/comments/1ssk2y0/comment/ohmyywz/)
  on that thread drove the strategy v2 work (archetype selector,
  pivot triggers, knight-hold rules, robber-as-resource-control,
  port pip-alignment, LA defend/snipe states, LR setup-vs-commit
  phases, table-scarcity dampening).

## License

GPL-3.0. catanatron is GPL-3.0; CatanBot depends on it, so the
derivative license applies. See [LICENSE](LICENSE).
