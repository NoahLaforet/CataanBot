# Changelog

Notable user-facing changes to the userscript and bridge. Internal
refactors / test additions are in the git log; this captures what
landed in each tagged release.

## v0.29.1 — 2026-05-01

- **Restored the original slate dashboard as Style 1.** The v0.29.0
  redesign replaced it with the new "concierge" aesthetic; Noah
  wanted the slate kept too. Now there are 6 styles: original
  slate at position 1, with concierge / brutalist / casino / pixel
  / botanical at 2-6.
- **Monopoly hint clamps inferred opp counts.** "Drains 19 from
  blue" was firing on opps with 0 actual cards because the per-
  resource breakdown comes from tracker.hand() which drifts
  upward across hidden steals. Now scales every per-opp count
  down to the authoritative WS card total, then caps the
  cross-opp total by physical deck supply (19 - bank - self_held).
- **Discard banner gated on robber_pending.** The minimal-style HUD
  was firing the red "DISCARD 4 cards" alert just because the hand
  crossed the 7-card limit pre-roll. Now only fires when self
  actually owes a discard (just rolled a 7); the pre-roll spend-
  down warning still surfaces via the seven_prep amber tile.

## v0.29.0 — 2026-05-01

Each of the 5 styles redesigned from scratch with a unique
aesthetic direction — typography, color, ornamental treatment,
spatial composition. Not just "different DOM trees with the
same look" — five fundamentally different design languages:

- **Style 1 — Concierge.** Refined hospitality aesthetic. Warm
  cream paper, deep ink, a single brass rule (#a37b32) anchors
  the eye. Cormorant Garamond italic display, oldstyle figures.
  Single column, generous margins, no decorative chrome. Reads
  like an Aman or Aesop guest card.
- **Style 2 — Brutalist Editorial.** Pure black on bone-white,
  Helvetica Neue Black at 64px display. Hard 4px black borders
  with a 12px solid drop shadow. Uppercase headlines that span
  the panel. Two-column body with thick rules. Numbers ARE the
  ornament.
- **Style 3 — Casino Felt.** Deep felt green (#0d3a26) with
  gold (#d4af37) and crimson accents. Italianno script display
  for the title, Cormorant for body. Score numbers wrapped in
  round chip badges with white border. Radial-gradient baize
  texture, double-bordered frame.
- **Style 4 — Pixel Game Boy.** Authentic DMG 4-color sage
  palette (#9bbc0f → #0f380f). VT323 monospace at 22px.
  Pixel-art borders with hard 3px drop shadows. Solid
  blocky chrome, image-rendering:pixelated. Reads like a
  1989 handheld screen.
- **Style 5 — Botanical Manuscript.** Sepia parchment with
  subtle aging gradients. Spectral italic serif throughout,
  oldstyle figures, decorative `· · · ❦ · · ·` divider in the
  header. Each tile has a nested hairline border for the
  manuscript-margin feel. Audubon-plate caption typography.

Each style picks the renderer (default/newspaper/terminal/minimal)
that best matches its design language. Settings dropdown labels
updated to the new aesthetic names.

## v0.28.2 — 2026-05-01

Major HUD overhaul + critical bridge fixes after Noah's session
feedback ("the styles are still the same UI", "wrong road
predictions", "couldn't start game"):

- **Each non-default style is now a fully different RENDERER**, not
  just a CSS reskin. The dispatcher in renderOverlay swaps the
  entire DOM tree based on data-style:
  - **Style 1 (default)** — current dashboard, restored to emojis
  - **Style 2 terminal** — single scrolling text log, sparkline
    histogram, opp lines as `@ name vp/cards`, no cards/banners
  - **Style 3 newspaper** — front-page layout with a serif headline
    + 2-column body (standings on left, recent rolls on right)
  - **Style 4 tactical HUD** — stat-bar grid: self ammo readout
    with VP gauge at top, per-opp horizontal VP bars colored by
    their CSS color, top rec as a bracketed `[ TARGET ]` callout,
    pulsing klaxon for winning move
  - **Style 5 minimal tile** — one giant primary number on a single
    tile (Win, Discard, Robber, or Next move), tiny VP standings
    strip below; nothing else
- **Default HUD reverts to emojis.** The v0.27.0 SVG glyphs felt
  diagrammatic; emojis read at a glance. Non-default styles still
  use SVG glyphs when their aesthetic suits.
- **Fixed: opening-road follow-up recommended an edge I already
  owned.** The opening-road logic skipped opp-owned edges but not
  self-owned ones. Catan rejects double-roads, so the click failed
  in colonist. Now tracks `my_edges` alongside `opp_edges`.
- **Fixed: bridge crash on malformed type=4 frames.** Colonist
  sometimes ships type=4 (GameStart) without a usable gameState
  (auth handshakes, reconnect acks). LiveSession.from_game_start
  raised LiveSessionError, the bridge logged "decode error" and
  left the bot half-booted. Now silent no-op on malformed frames.
- **Fixed: tilesToHtml ReferenceError.** Helper was scoped inside
  the recs-flow if-block but called from the dev-card-hint
  placement section outside that scope. Hoisted to module scope.
- **Fixed: more SVG-rendering-as-text bugs.** The "1 brick from
  settlement" near-miss banner had the same escapeHtml(svg) bug
  pattern; same for the userscript fallback path.

## v0.28.1 — 2026-05-01

Critical bugfix + style overhaul after Noah's mid-session feedback:

- **Fixed SVG glyphs rendering as raw markup text.** Trade banners,
  robber-on-me, discard hint, and seven-prep all wrapped strings
  containing inline SVG glyphs in `escapeHtml(...)`, which turned
  the icons into literal `<svg class="res-glyph res-wood" viewBox=
  "0 0 24 24" ...>` text on screen. Visible in Noah's 2026-04-30
  screenshots; latent since v0.27.0 (when SVG replaced emojis), and
  the recent WS-side trade-offer fix made the banners fire often
  enough for Noah to actually see it.
- **5 HUD styles rebuilt as truly different layouts** — not just
  color/font reskins. Each style has distinct typography, density,
  decorative chrome, and structural treatment of cards/banners:
  - **1 operations console** — current dashboard (slate, dense,
    Inter)
  - **2 ASCII terminal** — pure black, JetBrains Mono, lowercase
    `$ ` prompt, dashed dividers, `> `/`[#] ` row prefixes
  - **3 broadsheet** — cream parchment, centered Charter masthead,
    double-rule dividers, oldstyle figures
  - **4 tactical HUD** — scan-line gradient bg, clipped polygon
    corners on opp cards, neon glow on top rec + magenta-tinted
    banners, JBM uppercase
  - **5 field notes** — pure white, generous padding (Apple Notes
    style), big confident VP numerals, hairline rules

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
- **VP target auto-detected from colonist's gameSettings.** Bot kept
  VP_TARGET=10 internally even when colonist was running a 15-VP game,
  so every endgame heuristic (close_to_win, leader_threat,
  win_proximity, recommender bias) fired at the wrong threshold. Now
  set_vp_target / set_discard_limit pick up colonist's real rules on
  GameStart. Logs the auto-detect to the bridge stdout for visibility.
- **5 toggleable HUD styles.** New "style" dropdown in the settings
  drawer cycles between: 1 slate dashboard (default), 2 terminal/CRT,
  3 newspaper/print, 4 cyberpunk neon, 5 minimal light. Each style
  also tweaks density, border weight, and one decorative detail (a
  "> " prompt prefix on terminal, a ❦ glyph on newspaper, neon glow
  on cyberpunk hero recs, etc.) so they feel genuinely different,
  not just recolors. Pure cosmetic — game logic, recs, and data are
  unchanged. Persisted in localStorage.
- **Opp inferred breakdown reads as a guess.** When an opp isn't fully
  tracked (any hidden steal in their hand), the per-resource breakdown
  renders italic + muted instead of the same weight as the
  authoritative total. Catches Noah's 2026-04-30 complaint that the
  inferred numbers "looked like ground truth" even when drifting.
- **Less jargon in the rec strip.** EV pill tooltip says "how much
  better this move scores than doing nothing" instead of "1-ply EV";
  move-quality gap pill says "−N.N pts" instead of "−N.N EV"; opp
  production tag says "0.42/roll" instead of "0.42p".

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
