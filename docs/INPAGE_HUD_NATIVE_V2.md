# CatanBot in-page HUD — native v2 (performance + fully built-in, good style)

Paste this whole file as the kickoff prompt for the next session. The previous
session's environment was slow (git porcelain hung; commits had to go through
plumbing) so this is a clean handoff.

## State at handoff
- Branch `main`. Commits this session:
  - `f348ca1` loghud: native action cues, kill the floating panels, opening road
  - `4463505` loghud: full side-panel parity sections in the CatanBot tab
- Version still `0.50.0` on all four files (manifest / pyproject / __init__ /
  CHANGELOG top) so `test_extension_integrity.py` version-sync stays green. Bump
  to `0.51.0` as the final ship step once this build is verified live.
- The dev folder `~/Desktop/catanbot-inpage-hud-dev` is rebuilt from `extension/`.
  Noah must reload it in `chrome://extensions` to get these changes — the game
  he just played was likely the OLD v0.50.0 still loaded.

## What already changed in code (verify live after a reload, don't redo blind)
- The floating notification panel (`#cbo-notify`) and on-board side-read strips
  (`.cbo-side-read`) are DELETED. Discard guidance moved into the HUD body.
- Opponent reads inject as a strip under each colonist player row again
  (`injectPlayerReads`, sibling in the scroll content).
- Action cues unified into `applyActionCues`: glow the bottom build button on
  your turn, glow the central "Place" button in setup; knight-card + discard-
  card glows are wired through `findDevCard` / `findHandResourceCards` stubs
  that currently return null/[] (NO-OP until the hand DOM is reconned).
- Opening road FIXED: `topRecHtml` reads `rec.road.edge_tiles` with a `→`
  arrow when the rec is `kind=opening_settlement, action=road`.
- Parity sections added to the CatanBot tab: strategy / board-affinity banner,
  LR + LA race, "you can win this turn", knight nudge, bank-low + dev-deck,
  dice tempo, post-game. (Steal info = the robber-targets section; histogram +
  eval sparkline intentionally left to the full side panel.)

## Noah's feedback from the game he just played (THE work list)
1. **SLOW / LAGGY.** The HUD makes the page feel sluggish. This is now top
   priority. Likely causes: the 1000ms poll does a full `root.innerHTML =
   renderBody(snap)` every tick PLUS three full-document `querySelectorAll`
   sweeps each tick (`applyActionCues`, `injectTradeBadge`, `injectPlayerReads`)
   PLUS content.js's 500ms MutationObserver re-anchor PLUS the streamer sweep.
   Fix: (a) only re-render when the snapshot actually changed (compare a cheap
   hash / a seq field; skip innerHTML churn when identical); (b) CACHE the
   colonist element handles (build bar, trade panel, player-panel container)
   and invalidate them from the existing MutationObserver instead of re-querying
   the whole document every tick; (c) batch DOM reads then writes (no layout
   thrash); (d) consider an adaptive poll (faster on your turn, slower idle).
   Measure before/after; the page must feel native-fast.
2. **EVERYTHING BUILT-IN, NOT OVERLAY** ("resources and popups are overlay not
   built in like the chat log"). The `Log | CatanBot` tab IS the gold standard
   of built-in. Resolve the tension from last round: the per-player ROW strips
   (`injectPlayerReads`) read as tacked-on overlays. EITHER make them visually
   seamless (match colonist's own row markup/!beige styling exactly so they look
   native) OR drop them and keep opponent resources only in the CatanBot tab's
   OPPONENTS section (which is genuinely built-in). Lean toward the tab unless
   the row strips can be made indistinguishable from colonist's UI. The ONLY
   things allowed outside a colonist panel are (a) glows ON colonist's real
   elements (build buttons, dev card, place button) and (b) the trade verdict
   badge anchored to the trade panel — both are "on a real element," not
   floaters. Kill anything that reads as a loose popup.
3. **Missing reads** (he saw these gone in the old build; confirm they fire in
   the new one and add board cues): opening-road pick, robber recommendation.
   Robber: `robberHtml` shows ranked target tiles + suggested victim when
   `robber_pending` / knight — verify it actually renders live; the board is a
   WebGL canvas so the tile cue stays as the tab's text read (no DOM node to
   glow), but make the robber read prominent.
4. **Strategy + settings menu.** Strategy banner now renders (verify). The
   settings menu should become a real in-page interactive menu (Noah: "fully
   making an interactive menu inside of the site itself"), not just the tiny
   gear dropdown: streamer mode, pause recs, replace-log, plus VP target /
   discard limit etc., styled native, living in the CatanBot tab or a clean
   in-panel menu.
5. **Full functionality + good style.** Bring the rest of the side panel
   (game_plan, monopoly_hint, yop_hint, rb_hint, milestone, standings,
   yield_summary, gold_pick/fog for variants) and make the whole thing LOOK
   good and native (colonist beige, good type scale, fits the column, no
   clutter). "Good style" is an explicit ask.

## Still-deferred recon (needs a mid-game live capture, not setup)
- The dev-card HAND element (to light up the knight card) -> wire `findDevCard`.
- The hand RESOURCE-card elements (to glow the cards to discard on a 7) ->
  wire `findHandResourceCards`. Both currently no-op safely.
- Capture by driving Comet to a game WHERE a dev card / a 7-discard is live,
  then read the DOM around the hand.

## Key files / anchors
- `extension/loghud.js` — the HUD. `renderBody` (now with the parity helpers
  `strategyHtml/raceHtml/bankDevHtml/tempoHtml/winningMoveHtml/knightHintHtml/
  gameOverHtml`), `applyActionCues` + `highlightBuildButton/highlightCentralAction
  /highlightKnightCard/highlightDiscardCards` + the `findDevCard/findHandResourceCards`
  stubs, `injectTradeBadge`, `injectPlayerReads`, `fetchAndRender` (the poll),
  `buildSettingsPanel` (the gear).
- `extension/content.js` — `findLogContainer`, `window.__catanbot`, the 500ms
  re-anchor MutationObserver (reuse it to cache/invalidate element handles).
- `extension/background.js` — `get-advisor` (data path; in-page fetch is blocked
  in Comet).
- `extension/panel.js` — the side-panel reference renderer for any section not
  yet ported (search `renderOverlay`).
- Bridge snapshot fields (authoritative shapes): `src/catanbot/bridge.py`
  `_build_advisor_snapshot`; helpers in `bridge_strategy.py` (strategy_tag,
  longest_road_race, winning_move), `bridge_economy.py` (bank_supply,
  dev_deck_remaining, largest_army_race), `bridge_hints.py` (knight_hint,
  game_plan). Snapshot keys list: strategy, longest_road_race, largest_army_race,
  bank_supply, dev_deck, hot_numbers, sevens_hot, production_stall,
  engine_deficit, winning_move, game_over, knight_hint, game_plan, monopoly_hint,
  yop_hint, rb_hint, milestone, standings, yield_summary, robber_targets,
  discard_hint, incoming_trade, win_proximity, threat. (There is NO steal_matrix
  key; robber_targets carries the steal info.)

## Build + verify
- Run the bridge app (or `./bin/catanbot bridge`); HUD shows "bridge offline"
  without it. Reload `~/Desktop/catanbot-inpage-hud-dev` in chrome://extensions,
  open colonist Play-vs-Bots, reload the tab. HUD = the CatanBot tab in the log
  panel. Claude can drive Comet (claude-in-chrome MCP) to recon + screenshot but
  CANNOT reload the unpacked extension; Noah reloads, Claude verifies by shot.
- Tests: `node --check extension/loghud.js`, `pytest tests/test_extension_integrity.py`.

## Git note (important for this repo in this environment)
`git commit` PORCELAIN HANGS here (some post-commit housekeeping never returns).
`git write-tree` / `commit-tree` / `update-ref` all work instantly. Use the
helper `/tmp/cbo_commit.sh <repo> <msgfile> <paths...>` (plumbing + per-step
watchdog) or build commits by hand with plumbing. The harness also runs a
periodic `git status` that briefly holds `.git/index.lock`; ignore transient
lock errors and retry. Do NOT batch many `git`/`grep` calls — they auto-background
and truncate output in this session; run one focused command and wait.

## House rules (unchanged)
Extension only, never `legacy/userscript/`. No em-dashes anywhere. Commit as we
go, human messages, Noah sole author, no Claude/AI attribution. Never commit
`/Users/noah` paths or secrets to the public repo. Keep tests green. HUD prefs:
fewer words, obvious reading, info where it's actionable, NATIVE (built-in like
the chat log) not floating, and GOOD STYLE.
