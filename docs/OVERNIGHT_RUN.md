# CatanBot overnight run — playbook (2026-06-16)

Self-contained handoff for an autonomous overnight session (a context
compact wiped working memory; this is the source of truth). Noah is
asleep, machine caffeinated + plugged in, full permission to drive the
live colonist bot game and to cut the distribution release on his repo.

## Environment / how to work here

- Repo: `~/Desktop/Github/CatanBot`. Extension in `extension/`.
  Dev-folder mirror the extension loads from: `~/Desktop/catanbot-inpage-hud-dev`.
  After every extension commit, `cp` the changed files there. Only Noah
  can reload an unpacked extension, so live extension changes won't show
  until he reloads in the morning — verify via the render harness / JS
  injection instead.
- Git porcelain HANGS in this repo. Commit with
  `bash /tmp/cbo_commit.sh <repo> <msgfile> <paths...>` (plumbing +
  watchdog). Rules: no em-dashes anywhere, Noah is sole author, no
  Claude/AI attribution.
- Sandbox: any `.venv/bin/python` or port-binding or writing outside the
  repo needs `dangerouslyDisableSandbox: true`. `/tmp` is blocked — use
  `$TMPDIR`. Commands auto-background; run one, wait, read its output file.
- Tests: `node --check extension/loghud.js`; integrity tests run with the
  SYSTEM python (the venv pytest hangs for the same reason as the bridge,
  see below) via:
  `python3 -c "import importlib.util; spec=importlib.util.spec_from_file_location('t','tests/test_extension_integrity.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); [getattr(m,n)() for n in dir(m) if n.startswith('test_')]"`
- MCP browser (`claude-in-chrome`, tab id changes per session — call
  `tabs_context_mcp` first) IS Noah's live colonist window. You can
  screenshot, inject JS (`javascript_tool`), and drive the game
  (`computer` tool: click/key). The bridge is `127.0.0.1:8765`;
  `curl -s http://127.0.0.1:8765/advisor` for the live snapshot.

## State as of this handoff

- v0.51.0 released (tag/release on GitHub is still v0.48.0 — see
  Distribution). Many post-release extension commits; latest = `0cca694`.
  All extension fixes are committed + synced to the dev folder; Noah
  reloads in the morning to get them.
- Running bridge = Noah's Mac app `~/Applications/CatanBot.app` (I
  relaunched it; it reports `bridge_version: null`, serves every field
  the v0.51.0 HUD needs). It handles 7-roll robbers but NOT knight-robbers
  (bug) and shows opponents' opening cards as "?" (bug). These are the two
  bridge fixes below; they need a RUNNABLE SOURCE bridge to test, which
  needs the import hang fixed first.

## TASK 1 (do first) — fix the venv import hang

`import catanatron` hangs forever: networkx, imported by
`catanatron/models/board.py`, scans `importlib.metadata.entry_points()`
on import and `pathlib.read_text` stalls on stale editable-install
metadata left by the `CataanBot` -> `CatanBot` repo rename. Confirmed via
`faulthandler.dump_traceback_later`.

Stale files in `.venv/lib/python3.12/site-packages/`:
`cataanbot.pth` (old spelling), `__editable__.catanbot-0.25.0 2.pth`
(macOS dup), `__editable__.catanbot-0.39.0.pth`, plus their
`__editable___catanbot_*_finder.py` and stale `catanbot-*.dist-info`.

Fix: move those stale catanbot editable `.pth` + finder + dist-info files
to a backup dir (don't delete blindly), then test
`dangerouslyDisableSandbox` :
`.venv/bin/python -u -c "import faulthandler,sys; sys.path.insert(0,'src'); faulthandler.dump_traceback_later(25,exit=True); import catanatron; print('OK')"`.
Iterate (remove the offending artifact) until catanatron imports fast.
Then confirm catanbot imports via the launcher's path:
`PYTHONPATH=src .venv/bin/python -c "import catanbot.bridge; print('bridge import OK')"`.
`bin/catanbot` uses `PYTHONPATH=src`, so it does NOT need the editable
install — removing it is safe.

Then run the SOURCE bridge to test fixes: stop the Mac app first (it owns
8765 — `pkill -f "CatanBot.app"` or `osascript -e 'quit app "CatanBot"'`),
then `nohup ./bin/catanbot bridge > "$TMPDIR/bridge.log" 2>&1 &` (sandbox
off). Wait for `curl :8765/` to return. Now the extension talks to YOUR
bridge and you can test bridge code changes by restarting it. (Restart
loses the board until the next GameStart frame — drive the game to start a
fresh game after restart.) When done overnight, you may relaunch the Mac
app so Noah's morning is normal, OR leave the source bridge running.

## TASK 2 — bridge: knight -> robber detection

When a knight is played, `my_turn:True` but `robber_pending:False` and
`robber_targets:[]` (verified live). 7-rolls work. So the bridge sets the
robber decision on a 7 but not on a played knight. Find where
`robber_pending` / `_compute_robber_snapshot` is triggered in `bridge.py`
(grep `robber_pending`, `robber_snapshot`); compare the 7-roll path to the
knight path. The colonist log/WS for a played knight needs to flip the
same robber-pending state. Test live: play a knight in the game, curl
`/advisor`, confirm `robber_pending:True` + `robber_targets` populated +
`robber_targets[].coord` present.

## TASK 3 — bridge: opening resources as known, not "?"

Opponents' second-settlement resources are deterministic (the 3 tiles the
2nd settlement touches) but the HUD shows them as unknown "?N". The opp
hand model (`opp_inference.py` / `hand_tracker.py`) should credit each
player's opening hand from their 2nd-settlement adjacency at game start.
Find where the opening deal is handled; seed the known cards. Test: start a
fresh game, place openings, confirm opponents show concrete resources, not
"?".

## TASK 4 — board overlay: verify orientation + tune calibration

Extension (`extension/loghud.js`, `boardCoordToPixel` +
`updateBoardOverlay`). It draws a pulsing green HEXAGON on the recommended
robber tile (top pick bright; 2nd/3rd dim). `BOARD_CALIB =
{fx:0.339, fy:0.474, fdE:0.0684, fdV:0.0739}` as fractions of the
`#game-canvas` rect; `px = cx + q*dE + r*dE/2`, `py = cy - r*dV` (q=coord[0],
r=coord[2], catanatron cube). The vertical FLIP (`- r`) was a code-analysis
guess, NOT verified. Geometry is calibrated (hexagons trace the tiles) but
has ~half-tile perspective drift (colonist tilts the board in 3D).

Verify live: drive the game to a 7-roll robber (the bridge handles those),
`curl /advisor` for `robber_targets` (coord + resource + number), compute
`boardCoordToPixel(coord)` with the live `#game-canvas` rect, screenshot,
and check the BRIGHT hexagon sits on the tile whose visible resource+number
match target #1. If it's on the mirror tile (top<->bottom), flip back to
`py = cy + r*dV`. Tune `fx/fy/fdE/fdV` to reduce offset. Optional: add a
per-row perspective correction (scale dV + hex size slightly by row) for
precision. Inject test hexagons with the production math, screenshot,
iterate (pattern used during initial calibration). Don't drive Noah's game
into a bad state he can't recover; you can play full games to test.

## TASK 5 — in-page bridge-download CTA ("new pop-up")

The HUD's "bridge offline" placeholder has no download link; that CTA only
lived in the retired side panel (`panel.js`, `_bridgeCtaHtml`,
`BRIDGE_DOWNLOAD_URL` = `.../releases/latest/download/CatanBot-macos.zip`,
`_WIN` = `...CatanBot-windows.zip`, platform via `_isMac`/`_isWindows`).
Mirror a compact, native-beige download CTA into loghud's bridge-offline
state (platform-aware button to the right asset). Test by forcing the
bridge-offline render.

## TASK 6 — distribution: cut the v0.51.0 bridge release

`gh` is authed as NoahLaforet (Noah asked me to do this). Pipeline:
publishing a GitHub release fires `.github/workflows/build-windows.yml`
(builds + attaches `CatanBot-windows.zip` on a Windows runner ~5 min).
Mac `.app` is built locally: `build-app.sh` then `sign_and_notarize.sh`
(notarization needs Apple Team `3A9BRYYWX4` creds — likely keychain/env;
if unavailable, build an unsigned app and FLAG that Noah must notarize).
Asset names the extension expects: `CatanBot-macos.zip`,
`CatanBot-windows.zip`. Current latest release is v0.48.0; making v0.51.0
"latest" with BOTH assets points `/releases/latest/download/` at it.
CAUTION: don't publish a v0.51.0 that becomes "latest" missing an asset
(download 404). Safer: build Mac asset first; create the release;
publish triggers Windows CI; verify both assets land. Rebuild the bridge
AFTER Tasks 2+3 so the 0.51.0 binaries include the knight-robber +
opening-resource fixes. If notarization is blocked, leave the release as a
DRAFT/pre-release and write exactly what Noah must run.

## Smaller / verify

- Dev-card glow (`0cca694`): verify `findDevCard` targets the bottom-left
  hotbar dev stack (not the player-panel dev-count icon) when you hold a
  dev card. `_inHotbar` = not in `gamePlayerInformationContainer`,
  `y > 0.78*innerHeight`, `x < 0.5*innerWidth`.
- Floating gear (`ce8c311`), dice histogram (`5a7b643`), affordability +
  hexagon + tab-bar footgun (`33b54a4`): all committed; confirm they
  render via injection/harness.

## Morning report

Leave a concise status: what shipped (commits to reload), what's verified
live, what's blocked + why, and any one-action asks (e.g. Apple
notarization). Keep [[project_catanbot_inpage_hud]] memory updated.

## Snapshot field cheat-sheet

`robber_targets[].{coord:[x,y,z], resource, number, suggested_victim}`;
`roll_histogram{2..12}`, `total_rolls`, `dice_expected`; `self.{afford,
hand, vp, next_build}`; `recommendations[]`; `game_plan`,
`strategic_options`, `knight_hint/monopoly_hint/yop_hint/rb_hint
{have,should_play,...}`, `standings`, `game_progress`, `yield_summary`,
`bank_supply`, `dev_deck`, `robber_pending`, `my_turn`, `setup_phase`.
Hotbar cards: `.cardContainer-*` with `.cardImage-*` img =
`card_<lumber|brick|wool|grain|ore|devcardback>`. Board = `#game-canvas`
(WebGL, no DOM per tile).
