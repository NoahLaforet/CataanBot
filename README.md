# CataanBot

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
cataanbot --help
```

## License

GPL-3.0. catanatron is GPL-3.0; this project depends on it, so the derivative
license applies. See [LICENSE](LICENSE).
