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
  inspired by [u/Hot-Rooster1675's 36k-game simulation]([https://www.reddit.com/r/boardgames/comments/1ssk2y0/i_simulated_36000_games_of_catan_some/]):
  3rd-settle expansion bump (winners build #3 ~7 turns earlier),
  wheat-priority weight on opening eval (wheat is in every major
  build), composition-over-pips diversity bonus, longest-road push
  surfaced 1–2 roads from qualifying, brick-port early-pickup bonus
  on 2:1 ports for owned resources.
- **Auto-postmortem.** When a game ends, a self-contained HTML report
  is written to `postmortems/` — winner, final VP breakdown, dice
  fairness, hand-dynamics, trade quality, 7-roll impact, plus the
  charts (VP timeline, dice distribution, hand size, cumulative
  production) embedded as base64 PNGs.

---

## Install

Requires Python 3.11+ (catanatron constraint). macOS / Linux.

```bash
git clone https://github.com/NoahLaforet/CatanBot.git
cd CatanBot
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[bridge]'
./bin/catanbot --help
```

The `[bridge]` extras pull in FastAPI + uvicorn for the live bridge.
Skip if you only want the offline replay / advisor CLIs.

> On macOS the packaged `.venv/bin/catanbot` entry point can flake
> when the editable-install `.pth` file picks up an `UF_HIDDEN` flag
> from APFS. The repo-local `./bin/catanbot` launcher sidesteps
> that by setting `PYTHONPATH=src/` explicitly — use it instead of
> the packaged entry point.

## Live play on colonist.io

```bash
# Start the bridge with the live advisor on
./bin/catanbot live

# Or the lower-level bridge command, with custom WS mirror path
./bin/catanbot bridge --advisor --ws-jsonl ws_captures/$(date +%Y-%m-%d).jsonl
```

### Install the Chrome extension (recommended)

The extension renders the HUD in Chrome's native side panel (same
mechanism the Claude extension uses) — no overlap with the colonist
board, no draggable pop-out window to manage.

1. Open `chrome://extensions` in Chrome.
2. Toggle **Developer mode** (top-right).
3. Click **Load unpacked** and pick the `extension/` folder in this
   repo.
4. Pin the green CatanBot icon to the toolbar (puzzle-piece menu →
   pin) and click it — the side panel opens on the right.
5. Open colonist.io and start a game. The bridge terminal logs each
   event; the panel shows the HUD live.

To pull updates: `git pull`, then click the reload ⟳ icon on the
CatanBot card in `chrome://extensions`.

When the extension is ready for the Chrome Web Store, listing it
there ($5 one-time developer fee) gives users automatic updates
within ~24h of each push, no manual reload needed.

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
tests/                pytest, ~650 tests covering parsing, dispatch,
                      tracker arithmetic, recommender heuristics,
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
.venv/bin/python -m pytest        # ~2s, ~650 tests
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
- [u/Hot-Rooster1675's 36,000-game simulation]([https://www.reddit.com/r/boardgames/comments/1ssk2y0/i_simulated_36000_games_of_catan_some/])
  — five of the recommender's strategy biases come from that data
  (3rd-settle timing, wheat priority, composition over pips, LR
  push, port-bonus tempering).

## License

GPL-3.0. catanatron is GPL-3.0; CatanBot depends on it, so the
derivative license applies. See [LICENSE](LICENSE).
