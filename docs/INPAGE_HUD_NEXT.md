# CatanBot in-page HUD — next build (NATIVE integration, no floaters)

Paste this whole file as the kickoff prompt. State: commit `73e237c`, v0.50.0,
working tree clean.

## Design principle (READ FIRST — this is the whole point)
CatanBot should feel like **colonist's OWN HUD got smarter**, not like a
separate app laid on top. **NO standalone floating CatanBot panels/overlays.**
Noah's exact note: "it's just laying on top... I only want it integrated into
the actual current HUD. I don't want weird floaters."

Every piece of CatanBot output goes one of two ways:
1. **Into an existing colonist panel** — the CatanBot tab already lives in the
   log slot (good); opponent resource reads go INTO the player-panel rows;
   counts go where colonist shows counts.
2. **As a visual CUE on the real colonist element you act on** — glow / blink /
   color-change / a `!` badge ON the actual thing to click.

What was RIGHT vs WRONG last round (Noah's words):
- RIGHT: the trade verdict badge anchored to colonist's trade panel ("the
  accept trade one was okay") — because it's attached to a real element.
- RIGHT: the build-button highlight (glow the bottom build button) — that IS
  the cue pattern; keep + extend it.
- WRONG: the floating notification panel and the floating per-player resource
  strips ("the resources for the other players was bad", "weird floaters") —
  DELETE these and re-home them natively.

## /goal (one-liner)
Make CatanBot integrate natively into colonist's HUD with no floaters: put
opponent reads into the player rows, light up the exact element to click for
every action (build/dev/knight/place/robber/discard with glow/blink/color/`!`),
show the opening-road pick, and add the missing parity info. Verify live.

## Work items
1. **KILL THE FLOATERS.** Remove the floating notification panel (`#cbo-notify`)
   and the on-board side-read strips (`#cbo-side-read`) from loghud.js. Re-home
   their info: robber/discard → a cue + the CatanBot tab; opponent reads → the
   player rows (below). Keep the trade badge (anchored) and the build-button
   highlight.
2. **ACTION CUES on the real elements (headline feature).** When CatanBot
   recommends an action, light up the exact colonist control with a clear cue
   (glow + subtle blink + optional `!` badge):
   - build settle/road/city/dev → the bottom build buttons
     (`highlightActionButton` already does road/settle/city/dev; make it
     blink/`!`, not just a static glow).
   - **play a knight (or any dev)** → glow/`!` on THAT dev card in the hand.
     Recon the dev-card hand live while holding a knight.
   - place a settlement/road/city, move the robber → cue the board spot
     (the board is a WebGL canvas, so a cue is an overlay positioned at that
     element/tile's screen rect) AND/OR the relevant button.
   - discard on a 7 → glow the specific cards to drop in your hand.
3. **OPPONENT READS INTO THE PLAYER ROWS.** Integrate each opponent's inferred
   resources into colonist's own player panel rows, not floating. The rows are
   `playerRow-*` inside `opponentsScrollContainer` (a simplebar list); each row
   is `display:block; position:relative; ~86px`. Find the clean in-row spot
   (e.g. a styled line inside `playerInformation`, or an absolutely-positioned
   strip within the row's own bounds) so it reads as part of the row. Last
   attempt fought the simplebar / appended loose; do it right this time.
4. **OPENING ROAD.** After the opening settlement, show WHERE the road goes
   (+ direction). The bridge computes it (`recommend_opening` /
   `_opening_road_followup` / `_best_opening_road` in recommender.py; bridge.py
   ~1805; the side panel surfaces it). Trace why the in-page `renderBody` /
   `topRecHtml` drops it and surface it in NEXT MOVE.
5. **FULL SIDEBAR PARITY** in the CatanBot tab (it's integrated, so this is the
   right home). Noah: "same functionality available and readability as the
   sidebar HUD version." Bring over EVERYTHING the side panel shows, at the
   same readability: the strategy / board-affinity banner (`snap.strategy`),
   longest-road + largest-army race banners, win-proximity / leader-threat,
   bank supply (remaining resources), dev-deck count + odds, the roll
   histogram, eval/move-quality, the steal matrix, and the post-game summary.
   `panel.js renderOverlay` (and the section field shapes it reads) is the
   reference renderer — port section by section, keeping it readable in the
   column width.
6. **VERIFY LIVE** every cue lands on the right element and fits.

## Confirmed by Noah (locked)
- Robber rec → a CUE on the board tile (where to rob), since the board is a
  canvas (overlay positioned at the tile's screen rect). The text read can also
  live in the CatanBot tab.
- Discard on a 7 → GLOW the specific cards in your hand to get rid of.
- Parity = "all of the above" (every missing side-panel feature) at sidebar
  readability.

## DOM recon already captured (do NOT re-scrape)
- **Log anchor:** `div.container-Phl3P_ZR.beige-RdMs0LF_`; inside:
  `virtualContainer-Y9hPMC2i` > `virtualScroller-lSkdkGJi`.
- **Player panel:** `gamePlayerInformationContainer-oiaBsFwL` >
  `opponentsScrollContainer-Q5zNU1ru` (simplebar) >
  `opponentPlayerRow-AYNGolhx playerRow-RMhJ5mpg` (each row: child
  `container-...playerInformation-JA0qNVrB` + a hidden `diceGroup-`) + a
  sibling self `playerRow-`.
- **Trade panel:** accept/decline = `IMG.tradeResponseStatus-Wa78ni7p`,
  collapse = `IMG.showHideTradeIcon-Ei0woPsb`.
- **Bottom build bar:** cards = `[class*="actionButton-"]` (inner
  `root-fipXCgRS`), left-to-right [trade, dev, road, settle, city, end-turn];
  road/settle/city carry a numeric piece-count badge. Central status button =
  `actionButtonContainer-Qb77cVe3`. In-game viewport 854x837, dpr 2.
- **Still to recon:** the dev-card HAND (for the knight cue) and the board
  tile/spot rects (for placement/robber cues). Drive Comet to capture these.
- **Bridge fetch:** content-script fetch to `http://127.0.0.1:8765` is BLOCKED
  in Comet; loghud routes `/advisor` via background.js's `get-advisor` message.

## Key files
- `extension/loghud.js` — the HUD + cues (renderBody, injectTradeBadge,
  highlightActionButton, the floaters to delete, settings gear).
- `extension/content.js` — findLogContainer + window.__catanbot + off-screen
  log-hide (scroll-flood fix).
- `extension/background.js` — `get-advisor` handler.
- `src/catanbot/bridge.py` (~1805) + `recommender.py` — opening rec logic.
- Memory: [[project_catanbot_inpage_hud]]; plan: docs/INPAGE_HUD_PARITY_PROMPT.md.

## Run + verify workflow
- Run: start the CatanBot bridge app (or `./bin/catanbot bridge`), reload
  `~/Desktop/catanbot-inpage-hud-dev` in `chrome://extensions`, open colonist.io
  Play vs Bots, reload the tab. HUD = the CatanBot tab in the log panel.
- Verify: Claude drives Noah's Comet via the claude-in-chrome MCP (bot games,
  recon, inject test DOM, screenshot) but CANNOT reload the unpacked extension
  (Chromium blocks `chrome://extensions`) and bot games disconnect while Noah
  is also on colonist. So: Claude builds + commits, Noah reloads, Claude
  verifies by screenshot. Don't run a competing bot game on the single bridge.

## Constraints (house rules)
Extension only, never `legacy/userscript/`. No em-dashes. Commit as we go,
human messages, Noah sole author, no Claude/AI attribution. Never commit
`/Users/noah` paths or secrets to the public repo. Tests green (`node --check`,
pytest, `test_extension_integrity`). HUD prefs: fewer words, obvious reading,
info where it's actionable, NATIVE not floating. Rebuild
`~/Desktop/catanbot-inpage-hud-dev` after each chunk for Noah to reload.

## Delivery
Phase by phase (kill floaters + re-home → action cues incl. knight/placement →
opponent reads in rows → opening road → more info → verify). Commit each,
rebuild the dev folder, report at each checkpoint.
