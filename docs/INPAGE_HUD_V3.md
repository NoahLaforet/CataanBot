# In-page HUD v3 - full functionality run (continuation playbook)

Self-contained kickoff for the next session. Written 2026-07-02 after a
recon-and-housekeeping pass; no v3 implementation code exists yet. Read this
top to bottom, then execute in order. Ultracode/workflow orchestration is
fine for the implementation stages; the live calibration stage is inherently
serial (one browser, one game).

## State at handoff (2026-07-02, all verified this session)

- `main` is PUSHED through `33259ee`. The disk-era push blockage is GONE:
  normal `git commit` and `git push` both work again. The `/tmp/cbo_commit.sh`
  plumbing workaround and the contents-API preservation flow are RETIRED.
  Commit normally, push at breakpoints.
- Recovery-era junk cleaned: the seven `* 2.*` Finder-duplicate files are
  deleted, `scripts/sign_and_notarize.sh` exec bit restored,
  `tests/test_opening_credit.py` + final `docs/MORNING_REPORT.md` +
  `docs/INPAGE_BOARD_VISUALS.md` committed, `graphify-out/` gitignored.
- Disk: the 9 June-corrupt test files STILL hang on read and are permanent
  scar tissue (test_colonist_diff, test_colonist_map, test_gold_rush,
  test_volcano, test_tray_process, test_parser, test_bridge_dev_cards,
  test_dice_chart, test_colonist_proto). Never read or run them; exclude via
  `--ignore` flags. The machine has otherwise been stable since mid-June.
- Dev mirror `~/Desktop/catanbot-inpage-hud-dev` is in sync with `extension/`.
  After every extension change: `cp extension/<file> ~/Desktop/catanbot-inpage-hud-dev/`.
  Noah reloads the unpacked extension in chrome://extensions; Claude cannot.
- Bridge was NOT running at handoff. Launch:
  `cd <repo> && PYTHONPATH=src nohup .venv/bin/python -m catanbot.cli bridge &`
  It replays `sessions/active.jsonl` on startup (state-safe restart) BUT a
  restart drops content.js's frame feed; the colonist tab must be reloaded to
  reconnect. Batch all bridge-side changes into ONE restart.

## What already works (do not redo)

- Full side-panel parity in the CatanBot tab: renderBody has milestone,
  game_plan, strategic options, dev cluster (monopoly/yop/rb), seven prep,
  yield_summary, engine deficit, standings strip, variant (gold/fog), dice
  histogram, strategy banner, LR/LA race, winning move, game over.
- Perf pass (snapshot signature skip, cached handles, adaptive poll).
- Robber board overlay: hexagon rings on top-3 targets, live-calibrated
  (BOARD_CALIB fx 0.373, perspective K=0.10), zoom-safety hide via wheel-tick
  counting with a "board markers paused" note.
- `findDevCard` / `findHandResourceCards` are IMPLEMENTED (hotbar heuristics:
  `.cardContainer-*` + img src devcardback / card_<resource>, bottom-left
  screen gate `_inHotbar`). Dev-stack glow fires for all four dev hint types.
  The discard-card glow has NOT been seen live yet (needs a 7 in a game).
- Trade verdict badge with instant dismiss; affordable-only build glow;
  click-to-cycle recs; in-page gear settings (streamer / pause / replace-log /
  VP target / discard limit).

## The v3 gap list (the actual work, in build order)

### 1. Board placement visuals: settlement circle + road line (task 3)

Bridge and extension halves. Design decided this session:

Bridge half - attach coords at the SOURCE in `src/catanbot/recommender.py`
so every rec path (opening, midgame, followups) gets them for free:
- Helper `_node_frac_coord(m, node_id)`: mean of the cube coords of
  `m.adjacent_tiles[node_id]` tiles, rounded 4dp, as `[q, s, r]`. Three-tile
  nodes give the exact corner; two-tile edge nodes give the edge midpoint
  (acceptable). CAVEAT to verify first: whether the LandTile objects in
  `adjacent_tiles` carry their own cube coord attribute. The robber path
  (advisor.score_robber_targets, emitted at bridge_robber.py:295 as
  `list(s.coord)`) gets coords while iterating the map; if LandTile has no
  `.coord`, build a reverse map from `m.land_tiles.items()` (coord is the
  dict key) and look tiles up by identity or `t.id`.
- Attach `board_pos` (frac coord) to every rec dict that carries `node_id`
  (opening build ~recommender.py:326/395, midgame ~:1148/:1539, road landing
  :460). Attach `board_edge = [frac(a), frac(b)]` to every road dict; the
  endpoints are already there as `road["edge"] = [a, b]` node ids
  (recommender.py:312) and on kind=road recs (~:1373-1446, verify shape).
- De-abbreviate while in there (task 4 remainder): user-facing "prob" in
  bridge.py:3422 and :3451 event text becomes "probability"; sweep other
  user-facing snapshot strings (rg for short tokens in headline/detail/text
  fields; comments do not count). Extension dev names were already done.
- Tests: extend an existing recommender test to assert board_pos/board_edge
  presence and sane ranges; run the suite minus the 9 corrupt files.
- ONE bridge restart after all of it.

Extension half - in `extension/loghud.js`:
- `boardCoordToPixel` (line ~1514) is linear in the coord, so fractional
  coords work as-is. Add to `updateBoardOverlay` (~1573): when the top rec
  has `board_pos` and placement is live (setup phase, or my_turn and the
  build is affordable - reuse `canAffordBuild`), draw a pulsing CIRCLE at the
  node pixel; when the rec's road (or a kind=road top rec) has `board_edge`,
  draw a thick line segment between the two endpoint pixels (SVG line in the
  same overlay layer, same rank styling idea as the robber hexes).
- Same gates as the robber hexes: `enabled() && !_paused()`,
  `boardViewDefault()` zoom-safety, `stampStreamer(layer)`, cleared when the
  decision ends. Never show a misaligned marker.
- `node --check extension/loghud.js`, sync the dev mirror, then live verify.

### 2. Zoom/pan tracking for all board markers (task 2, the hard one)

Dead end confirmed and documented at loghud.js:1544: colonist uploads its
camera via a UBO / data texture, NOT uniformMatrix4fv, so reading the WebGL
matrix is out. The wheel-tick zoom-hide fallback is live today.

MEASURED LIVE 2026-07-03 (bot game, colonist v311, synthetic-event probes).
GATE F2 FIRED: the full findings + design options are in the Fable packet
at `~/Desktop/catanbot-fable-packet.md` (local, not in the repo). Summary:
- Plain wheel over the canvas does NOTHING. The current hide gate counts
  plain-wheel ticks: spurious hides, and net-0 restore rarely happens.
- Zoom = ctrl+wheel ONLY (Mac trackpad pinch). Synthetic WheelEvents are
  accepted, so the extension can DRIVE the camera (resync primitive).
- Scale is exact and deterministic: factor 1.828 per 400 units of
  accumulated deltaY (k=0.00151/unit), delta-proportional (8x-50 == 4x-100),
  perfectly invertible mid-range, uniform on both axes.
- Drag-pan is EXACT 1:1 translation with the cursor (and it exists, so the
  old markers were silently wrong after any pan: the recon hole confirmed).
- Zoom anchor: cursor-dependent but NOT the cursor; fixed point lands
  between cursor and board center (suspected viewport clamp on the camera
  translation). This is the one unmodeled component.
- Min-zoom CLAMP is deterministic and ABSORBS excess ticks (no debt), so
  blind tick integration breaks at any clamp touch. Burst-out-to-clamp
  reaches a known camera state from anywhere.
- Page reload / reconnect re-fits the board deterministically. Canvas
  resizes when banners toggle (837->878 seen); BOARD_CALIB fractions
  already absorb pure resizes.
- colonist drops the WS after ~3-4 min in a throttled background tab;
  reconnect preserves an active game.
Decision pending from the packet (options A-D). Implement whichever the
packet's DECISION section says; do not pick without it.

### 3. Live verification runsheet (task 8)

Bridge up, Noah reloads the unpacked extension once, bot game via
claude-in-chrome (offline, full drive permission). Already verified
2026-07-03 on the OLD build (game text side): opening settle rec fires and
re-ranks adaptively as opponents place, the road follow-up rec appears
after settling with the right edge, the offline CTA toast fires on
non-game pages, source bridge 0.51.0 serves /advisor (a stale 0.48 app
bridge had been squatting on 8765 since June; killed). Remaining items
below need the NEW build (Noah reloads the unpacked extension first).
Watch the clock: colonist drops a backgrounded tab after ~3-4 min;
reconnect right away, and prefer bot rooms (5h timers). Verify by
screenshot:
- Robber hexagon on a 7 and on a played knight (the 'unknown' card fix).
- NEW settlement circle during setup; road line on the opening followup and
  on a midgame road rec.
- Dev-stack glow for each hint type that comes up; discard glow on a 7
  (first-ever live check of findHandResourceCards).
- Zoom off default hides markers + shows the note; return restores. If v3
  zoom tracking landed: markers track through zoom and pan.
- Trade badge verdict + dismiss; affordability gate on the build glow.
- Style pass against Noah's bar: bigger text, fewer words, native beige,
  nothing reads as a floater ("built-in like the chat log").

### 4. Ship

- Bump 0.51.0 -> 0.52.0 in manifest.json / pyproject / __init__ / CHANGELOG
  top (test_extension_integrity checks the sync).
- Full runnable pytest suite green + `node --check` on changed JS.
- The v0.51.0 DRAFT GitHub release from the disk night still exists and is
  stale relative to main; fold it into the 0.52.0 ship or delete the draft.
  Mac zip needs build-app.sh + sign_and_notarize.sh (Team 3A9BRYYWX4);
  publishing fires the Windows CI (build-windows.yml is on main now).

## House rules (unchanged, load-bearing)

Extension only, never legacy/userscript/. No em-dashes anywhere. Commit as
we go with real human messages, Noah sole author, NO AI attribution. Never
commit /Users/<name> paths or secrets (public repo). Keep tests green. HUD
bar: fewer words, obvious reading, info where it is actionable, native not
floating, good style. Bridge restarts are batched; colonist tab reload after.

## Kickoff prompt for the next session

"Continue CatanBot in-page HUD v3: read docs/INPAGE_HUD_V3.md and execute it
top to bottom. Start with stage 1 (bridge board_pos/board_edge + extension
placement visuals + de-abbreviation, one bridge restart), then stage 2 zoom
calibration live with a bot game, then the verification runsheet. Ultracode
is fine for stage 1. I will reload the unpacked extension when you ask."
