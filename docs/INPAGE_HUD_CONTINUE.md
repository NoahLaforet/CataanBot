# In-page HUD — continue here

Paste this as the prompt to resume. The in-page HUD (replaces the side panel,
lives in colonist's log column) is built and working; this is the polish
backlog from Noah's live feedback on 2026-06-08.

## How to run / test
1. Start the CatanBot bridge app (or `./bin/catanbot bridge`) — the HUD shows
   "bridge offline" without it.
2. `chrome://extensions` → Load unpacked (or reload) `~/Desktop/catanbot-inpage-hud-dev`.
3. colonist.io → Play vs Bots → reload the tab. The HUD is the `CatanBot` tab
   in the top-right log panel; ⚙ gear = settings.
- I (Claude) can drive Noah's Comet via the claude-in-chrome MCP and play bot
  games to recon/verify, but I CANNOT reload the unpacked extension
  (Chromium blocks scripting chrome://extensions) and bot games disconnect
  while Noah is also on colonist. So Noah reloads; I verify by injecting test
  DOM + screenshotting.

## TODO (priority order, from Noah's notes)
1. **Opening road not shown.** After placing the opening settlement, the HUD
   doesn't show WHERE the road should go. The bridge already has the
   road-direction followup (bridge.py ~1805 `recommend_opening`,
   recommender.py `_opening_road_followup`/`_best_opening_road`; the SIDE
   PANEL surfaces road direction). Find why the in-page `topRecHtml` /
   `renderBody` doesn't show the opening road + its direction — likely the
   road follow-up is a rec sub-field the slim render drops, or during the
   road sub-phase `recommendations[0]` isn't the road. Surface it.
2. **"All around less info."** Add more side-panel parity sections to the
   CatanBot tab: the strategy / board-affinity banner (the "STRATEGY RANKING"
   block, snap.strategy), dev-deck count, maybe the roll histogram. panel.js
   `renderOverlay` is the reference renderer; port the high-value ones,
   keeping the clean compact style.
3. **Highlight the knight play** like the build buttons. Knight is played from
   the dev-card HAND (a different element than the bottom build bar). Recon
   the dev-card hand while holding a knight, then glow the knight card when
   `knight_hint.should_play` / a knight rec fires. (Build-button highlight for
   road/settle/city/dev already ships: `highlightActionButton` in loghud.js.)
4. **Verify live** (needs Noah's eyes): the build-button glow lands on the
   right button; the trade badge color (CONSIDER now blue) + flush position;
   the opening-road fix.

## DOM recon already captured (don't re-recon)
- **Log anchor:** `div.container-Phl3P_ZR.beige-RdMs0LF_` (beige log container);
  inside: `virtualContainer-Y9hPMC2i` > `virtualScroller-lSkdkGJi`.
- **Player panel:** `gamePlayerInformationContainer-oiaBsFwL` >
  `opponentsScrollContainer-Q5zNU1ru` (a **simplebar** custom scrollbar —
  injecting into rows is finicky) > `opponentPlayerRow-AYNGolhx playerRow-RMhJ5mpg`
  (opponents) + a sibling self `playerRow-*`.
- **Trade panel:** accept/decline = `IMG.tradeResponseStatus-Wa78ni7p`,
  collapse = `IMG.showHideTradeIcon-Ei0woPsb`. Walk up from the icon to the
  panel; the verdict badge anchors to it.
- **Bottom build bar:** cards are `[class*="actionButton-"]` (inner
  `root-fipXCgRS`), left-to-right = [trade, dev, road, settle, city, end-turn];
  road/settle/city carry a numeric piece-count badge (used to identify them).
  The central status button ("Place Settlement"/"End Turn") is
  `actionButtonContainer-Qb77cVe3`. In-game viewport was 854x837, dpr 2.
- **Bridge fetch:** a content-script fetch to `http://127.0.0.1:8765` is
  BLOCKED in Comet (`ERR_BLOCKED_BY_CLIENT`); loghud routes `/advisor` through
  background.js's `get-advisor` message (service worker, host permission).

## Key files
- `extension/loghud.js` — the in-page HUD. renderBody (rec+fallback/self/
  robber/opponents/footer), injectTradeBadge, highlightActionButton,
  updateNotify (robber/discard/trade), injectSideReads (off by default),
  attachRowHovers, the settings gear.
- `extension/content.js` — findLogContainer + window.__catanbot + the
  off-screen log-hide (scroll-flood fix).
- `extension/background.js` — the `get-advisor` handler.
- `src/catanbot/bridge.py` (~1805) — opening rec logic.
- `docs/INPAGE_HUD_PARITY_PROMPT.md` — the broader parity plan.
- Memory: [[project_catanbot_inpage_hud]].

## Shipped this session (commits aa1284c..dd5a100 area)
In-page HUD default-on with the side panel retired to a fallback; CatanBot tab
(rec + if-denied trade fallback, YOU card, robber targets, opponents, footer);
contextual panels (trade verdict badge on colonist's trade panel + robber/
discard notification in the left margin); settings gear (streamer/pause/
replace/reads-on-board); cleaner reads (confirmed-first, no %); per-player
reads (OPPONENTS section + hover; on-board side strips behind a toggle);
build-button highlight; background-routed data; React-survival; streamer-safe;
floating-overlay fail-safe; fixed a scrollToIndex console flood. Tests green.
