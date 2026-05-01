# Changelog

Notable user-facing changes to the userscript and bridge. Internal
refactors / test additions are in the git log; this captures what
landed in each tagged release.

## v0.28.0 — 2026-04-30

Mid-game bug fixes from Noah's 2026-04-30 ToucherOfKid game, plus a
playful HUD-style toggle:

- **Postmortem final scores no longer collapse to 2/2.** When colonist's
  victoryPointsState wasn't usable at game end, the resolver fell to
  the catanatron live tracker — which reads frozen at 2 if its internal
  state never advanced. Now derives VP from the pm_events stream
  (settles + 2*cities + LR/LA flags) before that fallback.
- **Postmortem no longer credits both players with longest_road.**
  The old strip-prior-holder logic only fired when previous_holder was
  set on the VPEvent; the parser doesn't always populate it. Now LR/LA
  always strip from every other player on each new award.
- **Knight play retries an empty robber rec.** When _compute_robber_snapshot
  returned None (game state not ready), the HUD silently dropped the
  ranking. Now sets a retry flag the snap builder honors on each poll
  until a non-empty snapshot lands.
- **Trade offer banner fires on WS-only offers.** Incoming offers came
  ONLY through the DOM-log "X wants to give ... for ..." pattern, which
  colonist doesn't always emit. Now also parses tradeState.activeOffers
  from the WS diff so offers sent through colonist's UI button alone
  still trigger the banner.
- **5 toggleable HUD styles.** New "style" dropdown in the settings
  drawer cycles between: 1 slate dashboard (default), 2 terminal/CRT,
  3 newspaper/print, 4 cyberpunk neon, 5 minimal light. Pure cosmetic —
  game logic, recs, and data are unchanged. Persisted in localStorage.

## v0.24.2 — 2026-04-29

Polish round after the first variant-map (Pond) test:

- **Knight hint copy is conversational.** Replaces the old stat-string
  ("strong block · score +10, top brick 6 +10") with situational
  reasoning: "robber's on you — play to clear it", "an opp is close
  to Largest Army — play to deny", "you're 1 knight from Largest
  Army — play it to grab the +2 VP", "a strong block on brick 6
  is available."
- **Monopoly hint clears immediately on play.** The DOM-log
  DevCardPlayEvent now applies to the live tracker too, so
  `MONOPOLY_IN_HAND` decrements correctly and the hint stops
  rendering. Same fix benefits knight / YoP / road-building.
- **Robber-targets hidden until knight is played.** Pre-play
  rankings showed which tile the bot would target, which gave away
  intentions in streamed games. Targets only render after the
  knight actually plays (or after a 7-roll forced placement).
- **Friendly Robber detection + filter.** Auto-detects colonist's
  optional rule from the in-game InfoEvent and filters protected
  ≤2 VP victims out of the robber-target ranking. Header pill
  surfaces the rule status.
- **Render orientation matches colonist.** Render PNGs (board
  preview, openings, postmortem) now share colonist's screen
  orientation — desert in the same place, top-row tiles on the
  same row, ports on the same edges. Fixes both classic and variant
  renders.
- **Monopoly "drains N from white" swatch fallback.** When colonist
  hasn't yet harvested the player's CSS color, falls back to the
  catanatron color hex so the swatch always renders.

## v0.24.1 — 2026-04-29

Variant-board geometry support — the weekly-rotation maps Noah
plays (Pond, etc.) now boot end-to-end:

- **Bridge boots on non-classic shapes.** `build_mapping` is
  shape-aware: classic uses BASE_MAP_TEMPLATE, variants get a
  fresh CatanMap built from colonist's authoritative tile data
  with synthesized water-ring + interior-lake support.
- **Ports anchor on variant maps.** Port tiles get reconstructed
  at the correct water positions so 2:1 trade rates work on Pond
  and other variant layouts.
- **Road-placement validation works on variants.** Augments
  catanatron's module-global STATIC_GRAPH so `Board.build_road`
  recognises variant edges. Variant node IDs are offset to 1000+
  to avoid collision with classic 0..53.
- **Variant header pill.** "VARIANT MAP" badge with tooltip
  describing which colonist gameSettings flag flagged.

## v0.23.50 — 2026-04-29

Decoded self's dev-card type from colonist's WS frames:

- KNIGHT = 11, VICTORY_POINT = 12, ROAD_BUILDING = 14 confirmed
  against a real capture; MONOPOLY = 13 (alphabetical guess) was
  later confirmed in a Pond game; YEAR_OF_PLENTY = 15 by elimination.
- When the bot knows the actual card type, only the matching hint
  block renders (instead of all four). Catanatron's
  `{type}_IN_HAND` counters get populated from a typed buy event.
- HUD push-refreshes within ~30ms of any WS frame instead of
  waiting up to 500ms for the next periodic poll, so resource
  counts track live during builds and trades.

## v0.23.48 — 2026-04-29

Dev-card play hints fire for self for the first time. The DOM-log
buy handler couldn't see the card type, so catanatron's
`*_IN_HAND` counters stayed at 0 forever and every play-timing
hint silently no-op'd. Tracks self's dev-card holdings as an
aggregate count instead and gates the hints on this. Includes
Catan's no-play-on-buy-turn delay.

## v0.23.46 — 2026-04-28

Live chess-style move-quality annotation (HUD principle #7).
Each post-setup self build is graded against the bot's top-10
recs at decision time and shown as `!!` / `!` / `?!` / `?` / `??`
in the HUD with a running tally and EV-gap pill.

## v0.23.45 — 2026-04-28

Live eval sparkline (HUD principle #6). The bridge's per-roll
state-eval samples render as a small line chart under the roll
histogram so you can see momentum across the game in one glance.

## Earlier

Phase 1-6 milestones (CLI advisor, visual board, REPL tracker,
catanatron integration, colonist DOM/WS bridge, full Phase 7
HUD overlay) shipped through v0.23.x. See git log for the
detailed trail.
