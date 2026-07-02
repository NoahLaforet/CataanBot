# In-page board visuals - autonomous run playbook

Self-contained playbook for making CatanBot's board markers good and
zoom-stable, plus the rest of Noah's visual backlog. Read this start to finish
before working.

## Environment + hard constraints
- Repo: `~/Desktop/Github/CatanBot`. Extension dev folder mirror:
  `~/Desktop/catanbot-inpage-hud-dev` (cp `extension/*` after each change; Noah
  reloads the unpacked extension - you cannot).
- The live colonist tab is the MCP tab (full permission to drive: zoom, click,
  screenshot, inject JS). It's a bot game, offline, not against ToS.
- Bridge runs at `127.0.0.1:8765`. Source bridge launch:
  `cd repo && PYTHONPATH=src nohup .venv/bin/python -m catanbot.cli bridge &`.
  It REPLAYS `sessions/active.jsonl` on startup to restore game state, so a
  restart is state-safe BUT drops content.js's frame feed (content.js does NOT
  auto-reconnect; Noah must reload the colonist tab to reconnect). Prefer
  loghud-only changes (no restart) where possible.
- DISK IS FAILING (2026-06-16, corruption spreads in real time). After EVERY
  working change, preserve the file to GitHub branch `disk-recovery-2026-06-16`
  via the contents API (git push is broken by corrupt objects). Pattern: read
  file -> base64 -> PUT `repos/NoahLaforet/CatanBot/contents/<path>` with the
  branch + current sha. Commit locally too via `/tmp/cbo_commit.sh` (watchdog;
  it may hang - the recovery-branch copy is the durable one). No em-dashes,
  Noah sole author. If a file read hangs, it's newly corrupt - skip + note it.

## The core problem: zoom/pan tracking
The board overlay (`#cbo-board-overlay` in loghud.js) positions hexagons with
`boardCoordToPixel`, which maps catanatron cube coords to pixels using
`BOARD_CALIB` = fractions of the `#game-canvas` rect. That assumes a FIXED
board position+scale inside the canvas. Colonist draws the whole board in a
WebGL canvas (direct child of body, internal camera) and zoom/pan moves the
board WITHOUT any CSS transform - so the overlay detaches on zoom. You cannot
style colonist's tiles (canvas pixels, not DOM).

Tasks, in order:

### 1. Robber hexagon: outline-only + default-zoom alignment (started)
- Fill removed already (outline only). Confirm it reads clean.
- Alignment: at default zoom the hex should trace the named #1 target tile.
  `fx=0.373` centers it, but extreme north/south rows drift (colonist's 3D
  tilt). Tune empirically: inject markers at known coords, screenshot, compare;
  consider a mild non-linear vertical term (north rows compressed). Verify
  across center + top + bottom target tiles, not just one.

### 2. Make board visuals follow zoom/pan (the hard one)
Goal: every board marker tracks when Noah scroll-zooms or pans. Approaches, in
order of preference:
- (a) Read colonist's WebGL camera. Hook the canvas's WebGL context in
  content.js at injection time (wrap `HTMLCanvasElement.prototype.getContext`
  before colonist initializes; capture the view/projection matrix passed to
  `uniformMatrix4fv`, or the gl viewport). Derive scale+translation, apply to
  `boardCoordToPixel`. Needs a content.js change + a fresh page load to hook
  pre-init. Investigate first whether colonist exposes camera/zoom state on any
  global, the canvas, or an event - cheaper if so.
- (b) Derive the transform from a reference each frame (e.g. detect a known
  board feature) - only if (a) fails.
- (c) FALLBACK if tracking is genuinely infeasible after real effort: detect
  zoom/pan (wheel + pointer listeners on the canvas) and HIDE the board overlay
  while zoomed away from the calibrated default, showing a small "reset zoom to
  see board markers" hint. Never show a misaligned marker - a wrong highlight is
  worse than none. The HUD's ranked text read stays the reliable backup.
Pick the simplest thing that actually follows zoom; document what you tried.

### 3. Settlement/opener circles + road lines on the board
- Bridge already gives recs `node_id` (settlement) and `edge_nodes` (road) but
  no screen position. Add to the snapshot, for the top settle/opening rec and
  top road rec: each node's position as a FRACTIONAL cube coord = mean of
  `m.adjacent_tiles[node_id]` cube coords (3-tile nodes -> exact corner; 2-tile
  edge nodes -> edge midpoint, acceptable). Roads: the two endpoint node coords.
- Extension: `boardCoordToPixel` already takes [q,s,r] and is linear, so a
  fractional coord gives the corner pixel. Draw a pulsing CIRCLE on the
  settlement node and a thick SEGMENT between the road's two node pixels. Same
  zoom-tracking treatment as task 2.
- Needs a bridge restart (replay-safe) - batch it with task 4 so Noah reloads
  once.

### 4. De-abbreviate everywhere
No abbreviations unless universally standard (VP is fine). Expand in BOTH:
- loghud.js: dev names already done (monopoly / year of plenty / road builder);
  sweep for others (e.g. "dev cards" header is borderline - "development cards").
- bridge snapshot text (bridge.py / bridge_hints.py): "need", "biggest
  predictor", "kn" (knights), "LR/LA" (longest road / largest army), "prob",
  "dev deck", concede, etc. Expand at the source strings.

### 5. Dev card outline (verify/extend)
`findDevCard` + `cbo-cue-knight` already red-outlines the face-down dev card in
the hotbar when one should be played. Verify it fires for ALL playable dev
recs (knight, monopoly, year of plenty, road builder), not just knight. Fix
`findDevCard`/the gate if it misses.

### 6. Any other board visuals that help and follow zoom.

## Workflow
Test live: drive the game / zoom via injected JS (`wheel` events or colonist's
zoom) + screenshot to verify tracking. Commit + preserve + dev-sync each piece.
Keep the runnable test subset green (the 9 disk-corrupt test files hang - ignore
them: test_colonist_diff/map, test_gold_rush, test_volcano, test_tray_process,
test_parser, test_bridge_dev_cards, test_dice_chart, test_colonist_proto). Leave
a status report in docs/MORNING_REPORT.md. Don't bug Noah unless blocked; he's
away.
