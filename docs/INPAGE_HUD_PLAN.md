# Build prompt: in-page HUD (take over the colonist.io log column)

## Goal

Replace the Chrome **side panel** with a native-feeling HUD injected into
colonist.io's right-hand log/feed column, so the site looks like it just got
better. Noah's design calls:

- **Layout:** a `[ Log ] [ CatanBot ]` tab bar pinned to the log header,
  defaulting to the CatanBot tab (HUD owns the visible area), one click back
  to the native log. A setting (`catanbot.loghud.replace`) hides the tab bar
  and shows the HUD only (full replace).
- **Content:** curated essentials only (top rec + opponent hand reads +
  1-line threat footer). Not a full panel.js port.
- **Side panel:** in-page becomes the only experience; retire the side-panel
  entry. Keep panel.js/overlay.js as fail-safe code, not a user surface.
- **Look:** blend native. Match colonist's beige/wood log styling, their
  font, hairline dividers. It should read as part of the site.

## Why this is a rendering job, not a data job

The competitor "Catan Card Tracker for Colonist.io" (Jishnu) does exactly
this: content script + MutationObserver on the log, injects DOM that replaces
native panels and pins a tab bar to the log container, reconciles against
colonist's on-screen counts. CatanBot already has a **stronger** foundation:
it reads colonist **WebSocket frames** (structured, wording-proof) plus the
DOM log via `content.js`, has the `/advisor` snapshot (~40 fields), and a
slim in-page renderer in `overlay.js`. So the work is injection + render +
native styling, not tracking.

## Architecture

- **No Shadow DOM, no iframe.** Plain DOM card with an ID-scoped injected
  `<style>` under `#cbo-loghud`. (overlay.js's CSS is already ID-scoped and
  leak-proof; a shadow root would block the streamer sweep and complicate
  skip-marking.)
- **content.js owns the selectors.** Add `findLogContainer()` beside the
  existing `findScroller()`, exposed on `window.__catanbot` (content.js and
  overlay.js share one isolated world via the single `content_scripts`
  entry). `log-hud.js` (new, or folded into overlay.js) owns the renderer.
- **Anchor = the beige log container** `div.container-Phl3P_ZR.beige-RdMs0LF_`,
  NOT the `virtualScroller` (it recycles nodes). 3-tier selector ladder:
  (1) exact; (2) `[class*="container-"][class*="beige-"]`; (3) structural:
  parent of `findScroller()`'s result that also holds `virtualContainer`.
- **Tab bar** injected into the container header. CatanBot tab -> show HUD,
  `display:none` the scroller. Log tab -> show scroller, hide HUD. Replace
  mode hides the tab bar and always shows the HUD.
- **Render set (curated essentials):** top rec (1 line + sublines), opponent
  hand reads (1 line/opp, color pills), 1-line threat/priority footer
  (robber-on-me / monopoly risk / win proximity / LR + LA race). Reuse
  `topRecHtml` / `handReadHtml` / `pillColor` / `contrastText` / `iconFor`
  from overlay.js for STRUCTURE, but write NEW native-beige CSS (overlay's is
  dark). Gate recs on `my_turn || setup_phase` and `variant_recs_disabled`
  ("recs off (variant)" instead of blank). Drop placeholder seats. Poll
  `/advisor` at 1000ms.
- **Survival across React re-renders:** reuse content.js's EXISTING scroller
  MutationObserver + 500ms safety setInterval; both call
  `ensureHudAttached()` (re-prepend the cached HUD node if it left the DOM;
  appendChild moves, never duplicates). Add a `#ui-game`-level observer for
  whole-container unmount. Cache the container ref; never `getComputedStyle`
  per mutation.
- **Streamer mode:** content.js sweeps `document.querySelectorAll('div,span,
  ...')` rewriting any leaf matching a username. Stamp every HUD node + text
  leaf with the `STREAMER_DATA_FLAG` dataset (`cataanonymized`) so the sweep
  skips them; the HUD already gets correct names via `snap.streamer_anon`.
- **Fail-safe:** if the container can't be found after the retry budget, flip
  on the floating `overlay.js` + a one-time toast + `console.warn`, so advice
  never disappears and Noah knows the recon rotted.

## Phased plan (each phase shippable + testable)

- **P0 (no UI):** `findLogContainer()` + a `window.__catanbotProbe()` dev
  probe (logs which tier matched, the container className, a 5-entry log
  sample). Verify tier-1 binds on a live/bot colonist game. Zero render risk.
- **P1 (static):** inject the empty native-beige `#cbo-loghud` card + the
  `Log | CatanBot` tab bar into the container, behind `catanbot.log_hud`
  (Noah flips it on). HUD owns the column; tab flips to native log; survives
  scroll/turns/recycling.
- **P2 (slim render):** wire top rec + opponent hand reads on the 1000ms
  poll. Rec matches the side panel; placeholders dropped; variant suppression
  shown.
- **P3 (survival hardening):** `ensureHudAttached()` in the existing observer
  tail + the 500ms interval; `#ui-game` observer. Force re-renders (reload,
  phase change, resize); re-attach < 500ms, no duplicate cards.
- **P4 (threat footer + gates):** 1-line threat/priority footer; game-over
  gate (hide recs in post-game lobby); green/amber/red card border from
  `my_turn` + robber-on-me + threat. Verify the urgency cascade live.
- **P5 (fail-safe + correctness):** null-container fail-safe to the floating
  overlay; verify streamer mode does NOT rewrite the HUD's name pills; commit
  a colonist DOM snapshot to `tests/` + a CI check that `findLogContainer()`
  binds against it (catch selector rot offline). Retire the side-panel entry
  (keep panel.js as fallback). Then get Noah's eyes before adding any
  panel-only sections.

## Top risks (mitigations are in the phases)

1. **Streamer name clobber** (highest, non-obvious): stamp HUD leaves with
   `STREAMER_DATA_FLAG`.
2. **React unmount orphans the card:** reuse the existing dual observer +
   interval calling `ensureHudAttached()`; `#ui-game` observer for full
   unmount. Self-heals < 500ms.
3. **`beige-` selector rot on deploy** (no human-readable part): 3-tier
   ladder ending in a structural walk; on total failure, fail-safe to the
   floating overlay.
4. **Polling contention:** HUD at 1000ms; back off to 1500ms if `/advisor`
   latency > 100ms.
5. **Reflow cost:** cache the container ref; re-resolve only on the 500ms
   fallback, never per mutation.
6. **TOS / perception:** additive read-only decision support; document in
   PRIVACY.md; for the PUBLISHED store build decide default-on vs opt-in
   (Noah runs it on; other users may want opt-in).

## Constraints (house rules)

- Only touch `extension/` and `src/catanbot/`; never `legacy/userscript/`.
- No em-dashes anywhere. Commit as we go, human messages, Noah sole author,
  no Claude attribution.
- Never commit `/Users/noah` paths, Apple/Team IDs, or secrets to the public
  repo.
- Keep all tests green (version-sync, JS `node --check`, pytest). Match the
  HUD design prefs: bigger text, fewer words, obvious reading.

## One open question for v0

Beyond top rec + opponent hand reads + threat footer, is there ONE thing you
check the side panel for every game that must be in v0 to actually retire the
panel (robber-target ranking? bank supply? dev-deck count?)? Default MVP
omits those; name it and it goes in P2.
