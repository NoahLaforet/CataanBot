# CatanBot

Live Settlers of Catan advisor for colonist.io. Chrome MV3 extension streams the
game (DOM log + WebSocket frames) to a local FastAPI bridge (127.0.0.1:8765)
running handcrafted heuristics + 1-ply eval over catanatron. In-page HUD in
colonist's log column; side panel is the fallback. No ML, by design.

## Surfaces
- extension/ is the LIVE surface. legacy/userscript/ is FROZEN: never edit it.
- Two engines kept at parity: src/catanbot/ (Python, primary) and extension/lib/
  (JS, standalone no-bridge mode, reduced accuracy). If you change scoring in one,
  mirror it or log the gap in docs/EXTENSION_PARITY.md.

## Commands
- Bridge: `./bin/catanbot live` (bootstraps .venv, serves 127.0.0.1:8765)
- Python tests: `pytest` (conftest injects src/; no install needed)
- JS tests: `node --test tests/js/*.test.mjs`; gate: `node --check extension/*.js`
- Harnesses: `PYTHONPATH=src .venv/bin/python scripts/arena.py` (also
  eval_player.py, smoke_all_maps.py, tune_eval.py)
- Extension: load unpacked from extension/; store zip via bin/build-extension-zip.sh

## Conventions
- One shared 1-10 heuristic scale across all move kinds; strategy archetype flows
  via snap["strategy"].
- Python/JS name mirroring is deliberate (recommend <-> recommendActions).
- HUD copy: bigger text, fewer words.
- Feedback data: only an explicit thumbs-down is negative; otherwise infer from
  whether the rec was played.

## Gotchas
- STATIC_GRAPH (catanatron) is module-global mutable state: variant maps mutate
  it in place (variant node ids offset at 1000). A classic game after a variant
  test can see stale edges; re-augment on fresh builds.
- Rules-changing variants (C&K, Seafarers) are NOT tuned; layout-only variants are.
- Bridge restart drops the content.js frame feed; reload the colonist tab after.
- docs/INPAGE_HUD_V3.md claims 9 test files are unreadable (June disk incident);
  they read cleanly on main as of 2026-07-06. Treat that warning as historical.
- Dev HUD mirror at ~/Desktop/catanbot-inpage-hud-dev must be re-synced after
  extension edits.

## Public repo (GPL-3.0)
Personal/gameplay data stays gitignored: feedback/, sessions/, ws_captures/,
postmortems/, .env, RESUME.md, graphify-out/. Never weaken those ignores; never
commit signing secrets (.env.example documents the keys).
