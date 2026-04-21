# Where we left off (2026-04-21)

Previous milestones (visual board, REPL, advisors, saved-game CLIs, icon
polish, hand replay, VP/longest-road/largest-army tracking) all remain
shipped — see git log for the full trail. This pass focused on making
the colonist.io → JSONL → postmortem pipeline trustworthy and laying
the groundwork for topology mapping.

## Shipped this session

- **Drift-dedup on the bridge** (`b260201`) — virtualized scroller was
  echoing already-captured log rows when nodes recycled in/out of view.
  Multi-layer dedup (text + 60s TTL) on the userscript side plus a
  replay-time backfill cut final drift on game5 from ~18 cards to 9
  total across all 4 players.
- **Hand-dynamics postmortem section** (`7fea54f`) — per-color peak
  hand size + event index, count of vulnerable (8+ card) events that
  weren't resolved by a 7 or trade, and final drift vs. reconstructed
  hand. Lives in `report.py`.
- **7-roll impact section** (`0adbec3`) — for every `RollEvent(total=7)`
  captures the roller, per-player discards, robber destination tile,
  steal victim + resource. Event window opens on the 7 and closes on
  the next roll.
- **Trade-quality section** (`aecf9ae`) — scores player-to-player
  trades with a 2d6 marginal-value heuristic (`1 / (0.5 + produced)`
  as scarcity proxy), reports giver/receiver delta so lopsided trades
  surface in the postmortem. Bank trades skipped.
- **Board DOM probe** (`503a2ea`) — new `userscript/board_probe.js`,
  paste into DevTools Console during an active colonist game. Walks
  SVG/polygon/circle/image + positioned divs, downloads
  `cataanbot-board-probe.json`. Read-only, no network. This is the
  artifact that unblocks topology mapping.

## What's next (when you reopen)

**The blocker for the live advisor is board topology.** BuildEvent and
RobberMoveEvent dispatch as `unhandled` today because colonist's DOM
coords don't map to catanatron node IDs. Can't write the mapping
without real board DOM samples.

Numbered next-session plan:

1. **Capture** — paste `userscript/board_probe.js` into Chrome DevTools
   Console during an active colonist game. Drop the downloaded
   `cataanbot-board-probe.json` in the repo root.
2. **Parse** — write a parser for the probe JSON (tile polygons +
   number pips + port markers).
3. **Map** — build a catanatron `Map` from the probe, compute the
   DOM-coord → catanatron node-id + edge-id mapping.
4. **Module** — `src/cataanbot/dom_to_catanatron.py` exposes
   `resolve_build(dom_x, dom_y) -> (kind, node_or_edge_id)` and
   `resolve_robber_tile(dom_x, dom_y) -> tile_coordinate`.
5. **Userscript** — extend `userscript/colonist_cataanbot.user.js` to
   attach DOM coordinates for settlement / city / road / robber events
   when it observes them.
6. **Dispatcher** — wire BuildEvent / RobberMoveEvent in live.py (or
   wherever the Event→Tracker dispatcher lives) so tracker state
   finally reflects builds and robber moves from the stream.
7. **Advisor hookup** — once the tracker is complete, wire the existing
   advisors (openings / secondadvice / robberadvice / tradeeval) to
   run against the live state in real time.

## Running the tool

```
./bin/cataanbot --help
./bin/cataanbot bridge --jsonl ~/cataan.jsonl
./bin/cataanbot replay game.jsonl --postmortem game.html
./bin/cataanbot replay game.jsonl --hand-chart hand.png
./bin/cataanbot openings --seed 777 --top 5
./bin/cataanbot secondadvice game.json RED --render second.png
```

210 pytest cases passing. `main` is clean at `503a2ea`, all pushed to
origin. Use `./bin/cataanbot`, not `.venv/bin/cataanbot` (macOS
`UF_HIDDEN` on .pth flakes).

## Files

- `src/cataanbot/report.py` — postmortem report builder. Sections:
  scoreboard, trade ledger, known resource flow, reconstructed hands,
  hand dynamics, 7-roll impacts, trade quality, dispatch quality.
- `src/cataanbot/live.py` + `src/cataanbot/bridge.py` — FastAPI bridge
  ingesting colonist userscript POSTs.
- `src/cataanbot/replay.py` — walks a JSONL through the Event→Tracker
  dispatcher. `--postmortem`, `--hand-chart`, `--vp-chart`,
  `--production-chart`, `--dice-chart`, `--report-out`.
- `userscript/colonist_cataanbot.user.js` — Tampermonkey script,
  v0.4.3 with 60s dedup TTL.
- `userscript/board_probe.js` — one-shot DOM probe (this session).
- `bin/cataanbot` — launcher that sidesteps macOS `.pth` quirk.
- `tests/` — 210 cases covering tracker, advisors, hands, stats, CLI,
  replay parser, report sections.
