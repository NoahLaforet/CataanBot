# Changelog

Notable user-facing changes to the userscript and bridge. Internal
refactors / test additions are in the git log; this captures what
landed in each tagged release.

## v0.44.0 (2026-06-03)

Playtest bugfixes from two live 1v1 games.

- **Monopoly holds a tiny pot.** It no longer recommends PLAY to steal a
  1-card stack even when that card would unlock a build; PLAY now needs
  2+ cards on an unlock, 4+ otherwise.
- **Road Building names the expansion option.** Instead of "hold forever
  / no clear swing", when two free roads would open a new settlement
  spot the hint surfaces that option.
- **Knight count fixed.** A self knight play was counted twice (a
  DevCardPlayEvent from both the DOM-log and the WS path), doubling the
  displayed knight count and skewing the recommender. PLAYED_KNIGHT is
  now reconciled to colonist's authoritative count each frame.
- **Opening banner.** The board-affinity headline reads "second settle"
  on the second opening placement instead of always "first settle".
- **Streamer startup leak.** The name anonymizer now applies before
  colonist's first paint; it read the toggle async before, so real names
  could flash for a turn at game start.

## v0.43.0 (2026-06-02)

Standalone no-bridge parity, a one-download bridge installer, and a
pre-launch security pass.

- **Standalone JS recommender at bridge parity.** The no-bridge HUD now
  matches the Python bridge on bank/port trade planning and scoring,
  incoming-trade evaluation (accept / decline / counter with a
  rebalanced counter), road best-landing scoring, and the third-settle /
  endgame / per-archetype score bumps. WS frames are parsed in a single
  pass instead of one tree walk per field.
- **One-download bridge installer.** A PyInstaller recipe builds a
  self-contained bridge binary that needs no Python install (verified on
  macOS); see docs/BRIDGE_INSTALLER.md and SIGNING.md.
- **Privacy and security hardening.** Dropped the Google Fonts fetch, so
  the panel's only network destinations are colonist.io and the local
  bridge; restricted the bridge CORS to colonist.io and the extension;
  and tightened the page-message and web-accessible-resource scopes.
- **Docs.** Rewrote the extension-to-bridge parity tracker, refreshed
  the smoke test for the Gold Rush, Volcano, and no-bridge paths, and
  added a launch-readiness report.

## v0.42.0 (2026-06-02)

Gold Rush (fog board) strategy support, an authoritative dice-stats
fix, and a per-opponent played dev-card breakdown.

- **Gold Rush fog board.** Full support for colonist's fog variant.
  Roads into the fog ring are credited with fog-reveal value so they
  surface in recs (you pay to uncover free, scarce-biased resources on
  strong numbers); under restricted starting placement the first two
  settlements are held to the shown corners; Road Building is timed to
  crack open the fog when a free road can reach it; and the gold hex
  that surfaces mid-game is valued as a wildcard, with the snapshot
  naming which resource to take.
- **Dice histogram matches colonist exactly.** The roll counts were
  tallied incrementally, so a frame missed on a reconnect or restart
  dropped a roll for good and the distribution drifted low over a game.
  The histogram now reads colonist's authoritative end-of-game dice
  stats, so it matches colonist's Dice Stats tab exactly.
- **Per-opponent played dev cards.** The PLAYERS section now breaks
  down which dev cards each rival has played (knights, monopolies, and
  the rest), all public information from the game log.
- **Trade sanity fix.** The live recommendation path was not passing
  opponent hands, so the propose-trade supply guard never ran and could
  surface a trade for a resource nobody holds. It now passes hands and
  filters those out (verified zero unsuppliable propose-trades across a
  full captured game).

## v0.41.0 (2026-06-01)

HUD layout: a grouped, collapsible side panel that trades one long
scroll for a few tidy sections.

- **Grouped + collapsible HUD.** The sections now sort into a pinned
  hero zone (the win-this-turn call and any urgent alert stay always
  visible) plus four collapsible groups: YOU, RECOMMENDATIONS, PLAYERS,
  and ROLLS & STATS. PLAYERS and ROLLS & STATS start collapsed, and each
  group's open/closed state persists. The render path is unchanged: the
  grouping is a post-render pass, so nothing is dropped and it degrades
  to the flat layout if anything goes wrong.
- **Opponent count** on the PLAYERS header.
- **Cleaner extension name and description** (removed em-dashes from the
  user-facing manifest text).

## v0.40.0 (2026-06-01)

Second audit pass: a visible dice-stats fix, three recommender bug
fixes, a branded clickable launcher, and HUD polish.

- **Dice histogram no longer looks squashed.** The roll histogram scaled
  every bar against the busiest column including 7, and 7 is the single
  most likely total (6/36), so it always pinned the scale and crushed
  the 2-12 distribution into the bottom of the chart. The 7 column is
  now excluded from the scale (and its bar clamped), so the real spread
  uses the full height.
- **Three recommender fixes.** An incoming trade that strips a resource
  unlocking a higher build (when the best move was a trade toward a
  city) is no longer mis-read as an upgrade and wrongly accepted. The
  1-ply search no longer simulates a dev-card buy, which had been
  peeking at the next deck card and ranking a blind buy above real
  builds. The endgame re-sort keeps the search ordering instead of
  discarding it.
- **Engine weights re-validated.** Swept the eval weights over 200
  fixed-seed self-play games against every available bot; no change beat
  noise, so the defaults stand (the engine already wins 67-80%).
- **Branded, clickable launcher.** The menu-bar app now uses a custom
  icon derived from the brand art that reflects bridge status (dim when
  off, a pulse while starting, full-color when up), and
  `bin/build-app.sh` assembles a double-clickable CatanBot.app so it
  launches from Finder without a terminal.
- **HUD readability.** Bumped the dim and label text colors to pass WCAG
  AA contrast on the dark panel.

## v0.39.0 (2026-05-31)

Audit-and-fix pass: two long-standing bugs squashed, plus a one-click
launcher, a tidier HUD, and release hygiene across the repo.

- **Robber targets appear instantly.** The HUD's push-refresh hook was
  built but never wired, so the panel only redrew on the 500ms poll and
  the robber-target list could lag up to a full poll behind a 7 or a
  played knight. The hook now fires on every WebSocket frame and chat
  line, cutting mid-turn latency from up to 500ms to about 30ms for
  both the bridge and the no-bridge paths.
- **Road recs never point into an opponent's settlement.** The bridge's
  sealed-road fallback picked a far endpoint by network reach and only
  checked production, so it could surface a road running into an
  opponent's piece. It now skips occupied far nodes (both the mid-game
  fallback and the opening-road follow-up) while still emitting a sealed
  rec toward open ground.
- **5-6 player lobbies degrade cleanly.** A 5th or 6th seat exceeds
  catanatron's four colors and used to leave a half-booted, corrupt
  game that re-raised on every later frame. The bridge now detects this
  up front and shows "limited tracking" instead of failing.
- **Friendly Robber auto-detects from the WS game settings.** The
  authoritative `friendlyRobber` flag in the GameStart payload is now
  read directly, so protected (low-VP) victims are filtered out even
  when the chat-log announcement is missed or scrolled past.
- **No-bridge robber list no longer goes empty.** On boards where every
  opponent-adjacent productive tile also touched one of your own
  settlements, the standalone ranking dropped every candidate and the
  table vanished. Self-adjacent tiles are now kept with a score penalty.
- **No-bridge settlement scores match the bridge.** The standalone
  recommender now scores and ranks settlement spots by wheat-weighted
  production with no diversity multiplier, matching the Python engine,
  so the same corner reads the same score and the same number-one pick
  on both paths.
- **Collapsible HUD panels.** The roll histogram, eval sparkline,
  move-quality strip, and dev-deck strip each get a click-to-collapse
  header (persisted) so you can trim the panel's height to taste.
- **One-click bridge launcher (macOS).** `./bin/catanbot-tray` starts a
  menu-bar app that starts/stops the local bridge with a status dot,
  opens colonist.io, and exposes a couple of settings. `bin/catanbot`
  stays the cross-platform launch path.
- **Release hygiene.** Dropped the unused `scripting` permission and the
  dead `localhost` host grant; synced the version across manifest,
  pyproject, and this changelog; added the first standalone JS tests
  and a node --check syntax pass for the extension; corrected the
  privacy/store/README permission notes; removed a stray duplicate
  test file; and
  quieted the page-console logging. The no-bridge mode is now labeled
  experimental in the panel.

## v0.38.0 — 2026-05-18

Black Forest variant support. The all-wood-centre map with fog hexes
around the rim now plays end to end: fog tiles (colonist tile types 7
and 8) are detected at GameStart, and a road that reveals one flips it
to a real tile live.

- **Fog reveal engine.** A `tileHexStates` diff flipping a fog hex to
  a real resource emits a `TileRevealEvent`; the tracker mutates the
  live catanatron map so the hex pays out on the next matching roll,
  and rebuilds `node_production` so opening-pick scoring picks it up.
- **Recs stay on.** A board whose only non-classic tiles are fog
  hexes labels as `black_forest` and clears the recs gate, so the HUD
  keeps recommending instead of going quiet on the variant.
- **Bank sync.** The tracker's resource bank now resyncs from
  colonist's authoritative `bankState` on every diff that carries one.
  Give/take accounting alone drifted the bank, badly on wood-heavy
  Black Forest boards where it ran negative and silently zeroed out
  roll payouts.
- **Scan-map button.** Unrecognized weekly maps (Scramble and the
  like) get a "map not recognized: scan" button that confirms the
  board uses classic tiles and rules so geometry-scored recs turn
  back on.

## v0.37.36 — 2026-05-10

Opening-pick recs now react to placements. The standalone snap was
computing `scoreOpeningNodes` against the empty board exactly once
per tick and never passing the `legalNodes` filter, so the top 8
picks stayed frozen on the same nodes regardless of who placed
where. As opponents claimed top spots they kept appearing in your
list; as you placed your 1st settle the round-2 ranking still
included your own node. Now the placed-and-neighbour set is built
once at the top of the snap, inverted into a `legalNodes` Set, and
passed to both `scoreOpeningNodes` and `scoreSecondSettlements`.
The downstream road-suggestion `placedNodes` reuses the same set
instead of rebuilding it.

## v0.37.35 — 2026-05-05

Deeper python parity: chess-style move grading, bank-supply
display, dev-card timing, and a multi-step plan banner all now
ship in the extension snap.

- **move_history (new lib/move_quality.js)** — every self build
  graded `!!` / `!` / `?!` / `?` / `??` against the rec list
  cached at the previous tick. Mirrors python's `move_quality.py`
  + `bridge.py:962-979`. Tracked via diffing self's
  `state.buildings` and `state.roads` per snap; capped at 30
  entries. Opening picks are pre-seeded into the graded set so the
  first 2 settles + roads don't flag '??'.
- **bank_supply (panel.js)** — per-resource cards remaining in the
  19-per-resource Catan deck. Mirrors
  `bridge_economy._compute_bank_supply`. Self.hand is authoritative;
  opps come from chat inference (best-effort). `tracked` field
  marks when an opp's drift would make the math unreliable.
- **production_stall (panel.js)** — self-side dry-streak detector.
  Walks rollHistory backwards counting non-7 rolls where no
  self-owned tile produced. Surfaces at 3+ dry rolls. Same
  threshold as bridge.
- **knight robber_reason (panel.js)** — chat-detected self knight
  play sets `_selfKnightPlayedThisTurn`, which the snap reads to
  ship `robber_reason='knight'` so the urgent banner fires the
  moment you click the knight. Cleared on turn change.
- **dev_cards_just_bought (panel.js)** — chat-detected "X bought
  development card" where X is self bumps a per-turn counter.
  `dev_cards_playable` now subtracts that counter so the dev-card
  hint block stops suggesting plays for cards we just bought
  (Catan rule: can't play a dev card the turn you bought it).
  Cleared on turn change.
- **game_plan banner (panel.js)** — heuristic port of python's
  `_compute_game_plan`. Picks the top-priority rec (city > settle
  > road) and writes a one-line plan summary like "ready to city"
  or "2 short — need 1 brick + 1 wheat". Renderer at panel.js:4228
  was already wired; standalone just never set the field.

## v0.37.34 — 2026-05-05

Python parity catch-up: extension snap now mirrors every bridge
surface Noah uses live. Closes the "stuck on waiting for game" /
"trade modal stuck" / "robber recs not always firing" reports.

- **Setup-phase routing (panel.js)** — `inOpeningPhase` now derives
  from `state.buildings` + `state.roads` per color, mirroring
  `bridge.py:1517-1549`. Earlier bank-derived path failed at
  GameStart (mechanic*State events haven't fired yet → `playersTotal=0`
  → `inOpeningPhase=false` → panel routed to mid-game with no
  opening picks). Opening picks now appear from the first frame.
- **WS-driven trade lifecycle (events.js, panel.js)** — added
  `state.tradeOffers` populated from `tradeState.activeOffers`,
  cleared on `null` payload OR `tradeState.closedOffers`. Mirrors
  python's `_trade_offer_events`. Banner no longer sticks on
  cancel / decline / counter-offer / timeout. Chat-based detection
  remains as a 30-second-window fallback.
- **Robber lifecycle (events.js, panel.js)** — `robberPending` and
  `robberMovedAtRolls` track the same forced/placed window the
  bridge does. Snap now ships `robber_pending` + `robber_reason`
  ('forced' or 'placed'), so the renderer's gate at panel.js:4108
  actually fires in standalone — robber rec table now appears
  every time you roll a 7.
- **opp_card_delta (events.js, panel.js)** — 5-roll ring buffer of
  per-color hand totals. Each opp row now ships `card_delta` /
  `card_delta_window` so the "+3 in 5" annotation renders.
- **dev_deck per-type (panel.js)** — chat parses "X used Monopoly /
  Road Building / Year of Plenty" and "X took from bank" so the
  dev-deck strip shows all four non-knight types decreasing.
  Previously knight was the only deck count tracked.
- **seven_prep would_drop (panel.js)** — pre-roll fat-hand warning
  now previews the exact discard plan (`would_drop` map) just like
  the bridge's `_compute_seven_prep_hint`.
- **opp.dev_stash_risk + opp.ports + opp.prod (panel.js)** —
  hidden-VP risk flag (`dev_cards >= 2 AND vp + dev >= target - 1`),
  per-opp port list, and per-opp production rate now ship in each
  opp block.

## v0.37.0 — 2026-05-04

Standalone Phase 4: full mid-game state from the extension alone.

The bridge is no longer required for mid-game recs. Colonist ships
authoritative game state with every WS frame; the standalone path
now reads it directly into a JS state container and runs the same
heuristic recommender + dev-card hints + robber-target ranker the
bridge used to produce. The Python script becomes optional —
"download from web store, click play" now actually works.

- **Snapshot applier (`lib/events.js`).** New `applySnapshot()`
  walks each decoded WS frame and updates a `newGameState()`
  container in-place: buildings (settlements + cities) from
  `tileCornerStates`, roads from `tileEdgeStates`, robber tile
  from `mechanicRobberState`, per-color hand from
  `playerStates.{cid}.resourceCards` (typed for self, count-only
  for opps), dev cards from `developmentCards.cards`, VP from
  `victoryPointsState`, longest-road / largest-army holders from
  `mechanic{LongestRoad,LargestArmy}State`, dice from `diceState`.
  Idempotent — same frame applied twice is a no-op.
- **Mid-game recommender (`lib/recommender.js`).** Heuristic port
  of `recommender.py` minus the 1-ply search rerank: city upgrades
  ranked by doubled production, settlement placements legal under
  road network + distance rule, road extensions scored by best
  landing node, single-swap bank/port trades that unlock a build,
  dev-card buy. Same 1-10 calibration as the bridge so the panel
  renders them through the same `renderRec` path.
- **Robber target ranker.** New `recommendRobberTargets()` picks
  the highest-EV tile by pip × adjacent-opp pieces × victim hand
  size; skips self-adjacent tiles per Catan rules.
- **Dev-card hints (`lib/hints.js`).** `knightHint` flags
  robber-on-us / opp closing on LA / claim-LA-now;
  `monopolyHint` picks the best resource via inferred opp totals
  (production-weighted from settlements + cities) and surfaces
  unlocks; `yopHint` finds the cheapest pair that unlocks a
  build; `rbHint` ranks legal road-pair extensions and PLAY-flags
  when they open a strong landing or tighten a Longest Road race.
- **Standalone snap upgrade.** `_makeNoBridgeSnap()` in
  `panel.js` now produces a full mid-game snapshot —
  recommendations, knight/monopoly/yop/rb hints, robber targets,
  per-player VP/hand totals, last roll, roll histogram, game-over
  detection — populated entirely from the JS state container.
  When the bridge is unreachable, the panel renders the same
  surfaces it did under bridge mode (modulo strategy v2 archetype
  scoring + 1-ply search-delta annotations, which stay
  bridge-only for now).

## v0.35.0 — 2026-05-04

Strategy v2: archetype tracker, mid-game pivots, tournament-grade
tuning, post-game frame.

The biggest behavioural shift since the bot stopped using a single
implicit "good move" notion. The recommender now reads the game
through five named archetypes, biases its picks toward whichever
the placements actually enable, and re-evaluates as the game
progresses. Driven by tournament-player feedback in the
[36k-game Reddit thread](https://www.reddit.com/r/boardgames/comments/1ssk2y0/i_simulated_36000_games_of_catan_some/);
full plan in `docs/strategy_v2_plan.md`.

- **Strategy archetype tracker.** New `strategy_select` module
  scores every legal-pair-of-settlements against five archetypes
  — Ore-Wheat-Sheep, Longest Road rush, Port trader, Road
  Builder, Balanced. Pre-placement it ships board-affinity scores
  so the user can pick their first settle to align with whatever
  the board favors; post-placement it locks in a primary +
  fallback tag and biases `recommend_actions` accordingly.
- **HUD strategy banner.** Top of every snap shows the active
  archetype with full plain-English label, a one-line rationale,
  and the full ranked list (mini-bars, eligible vs ineligible
  states). Pre-placement banner reads "🧭 board affinity — pick
  your first settle to align with a strong archetype below."
  Hover any archetype label for a "what this means" tooltip.
- **Mid-game pivot triggers.** Hot-number streaks on self tiles,
  road-building dev card drawn, monopoly drawn, opp closing on
  Largest Army, opp crossing the close-to-win VP threshold, or a
  7 going overdue with a heavy hand — each fires a named trigger
  that surfaces below the banner. `road_builder_drawn` carries an
  override that flips the active tag to Longest Road rush so
  downstream rec biasing reacts immediately.
- **Knight-hold rules.** Don't burn the first knight to clear a
  weak (2/3/11/12, pip ≤ 2) robber tile; loosen at 2+ knights or
  late game. "Strong block available" recommendations now hold
  the 1st knight by default for concealment.
- **Robber as resource-control tool.** `score_robber_targets`
  adds two new bonuses on top of the block-score: +1.0 + 0.2×pip
  when the tile produces a resource we owe for our next planned
  build, plus a monopoly-setup bonus that scales with self's
  share of the resource vs. the table baseline.
- **Port-pip alignment.** `_port_bonus` halves on weak-pip
  matches (a 2:1 wheat port whose only matching wheat tile is on
  a 2/3/11/12 gets half the bonus), and dampens further when
  table scarcity says opps barely produce the resource (you can
  extract 1:1 player trades for it instead).
- **Largest Army defend / snipe / pass.** New `la_defend` option
  fires when self holds LA and any opp is at 2+ played knights;
  the existing `largest_army_push` gains a "snipe" annotation
  when an opp sits exactly 1 knight from claiming.
- **Longest Road setup vs commit.** `longest_road_push` splits
  by phase — fires only at roads_needed == 1 in setup phase
  (self VP < 6, no opp at 8+ VP), at roads_needed ≤ 2 in commit
  phase (self VP ≥ 7 OR any opp at 8+ VP). chalks777's note that
  real LR plays come from late-game commits, not early road
  investment.
- **Proactive rebalance trades.** When self has 4+ of a single
  resource AND a non-leader opp holds something we have ≤1 of
  AND no build-targeted propose_trade already fired, surface a
  2:1 rebalance trade rec.
- **Seven-dodge pressure.** New "consider" tier on
  `seven_prep_hint` that fires at hand size = limit + 1 when
  pressure crosses 0.6 (hand overshoot + rolls-since-last-7 +
  opp hands ≥ 7). Catches the soft case where you're "fine right
  now" but a 7 is statistically overdue.
- **Game-over frame.** When the session flags a winner, the HUD
  shows "🏁 GAME OVER · {winner} won — waiting for next game" at
  the top and suppresses in-game banners. Self-win gets the
  positive accent (🏆). Clears automatically on the next
  GameStart.
- **Plain-English labels everywhere.** The bare archetype tags
  (OWS, LR_RUSH, RB_CARVED_TILES) used to leak into the HUD as
  insider jargon; replaced with full names and hover tooltips.
  "fb:" → "fallback:" etc.

## v0.34.0 — 2026-05-04

Auto-open postmortem + dark-mode redesign.

Postmortems used to land in `~/Desktop/postmortems/<stamp>.html`
and Noah had to fish them out manually. Now when a game ends the
panel detects the new postmortem and asks the service worker to
pop it open in a new tab adjacent to the colonist tab — no
download dialog, no manual click.

- **Bridge** — new `GET /postmortem` serves the most recently
  written file inline (HTMLResponse). State carries
  `last_postmortem_path` + `last_postmortem_seq`; the snap
  exposes `latest_postmortem: {seq, available}` so the panel
  can diff seq across polls.
- **Panel** — `_maybeOpenPostmortem` runs on every advisor tick.
  First seq seen is recorded silently (a page-reload mid-session
  doesn't re-pop a stale postmortem); subsequent bumps fire an
  `open-postmortem` runtime message.
- **Background** — handles `open-postmortem` by calling
  `chrome.tabs.create` with `index = colonist.index + 1` and
  `active: false`. Doesn't steal focus mid-game.
- **Postmortem template** — full dark-mode redesign matching the
  HUD palette (--bg-0/--bg-1, --pos for the winner, etc.). Hero
  header with winner name + duration + event count + VP target;
  scoreboard panel with ranked VP rows (winner highlighted
  green); 2x2 chart grid that collapses to single-column under
  760px; report block now sits in a styled card instead of a
  bare `<pre>`.

## v0.33.14 — 2026-05-04

LiveGame reboots on player-set change in GameStart.

Why orange-shows-white kept persisting across the v0.33.9–v0.33.13
fixes: when colonist's "quit and start new" path bypasses the
GameOver frame, `game_over_emitted` stays False and the next
GameStart slides into `_resync_from_replay` instead of a fresh
boot. The OLD game's color_map + player_names live on, so
catanatron emits stale opponent usernames (Vlad/Budd/Wiburg) for
events that the chat scraper logs with the new game's real
usernames (Calan/Hamlet/Kara). `display_colors` lookup keys never
match `opps[i].username` and the panel falls back to catanatron-
enum hex (Wiburg → catanatron WHITE → `#f0f0f0` pill).

Confirmed live in the bridge log:
```
[01:06:46 #0050] RollEvent: Hamlet rolled 8   ← chat (fresh)
[ws #00159]      RollEvent: Vlad rolled 8     ← catanatron (stale)
```
Same 8-roll, two different usernames.

- **`_gamestart_player_set_changed`** new helper compares the new
  GameStart body's `playerStates[*].username` set against the
  current session's `player_names`. Any difference forces the
  same reboot path that game-over and shape-change use, which
  rebuilds color_map + tracker + clears display_colors /
  streamer_anon downstream.

## v0.33.13 — 2026-05-04

Second half of the orange-pill fix: bridge was storing colors
keyed by the anonymized name, but the panel queries by real
username — perpetual miss in streamer mode.

content.js's anonymizer rewrites textContent in-place ("Wiburg"
→ "Cyrus"), and serializeEntry then reads `innerText` to build
the `names` array. So the bridge was getting `{name: "Cyrus",
bg: orange}` and storing `display_colors["Cyrus"]`, but the
panel does `display_colors.get("Wiburg")` — `display_colors`
keys by catanatron-side real usernames everywhere else.

- **content.js** stashes the original username on
  `el.dataset.cataanonReal` when rewriting; serializeEntry now
  prefers it over `innerText`.

## v0.33.12 — 2026-05-04

Fix the orange-pill-renders-white bug for real this time.

content.js's chat serializer captured `{name, color, bg}` per
name span but the final `names` array sent to the bridge dropped
`bg` and only sent `{name, color}`. The bridge's
`_harvest_display_colors` had a bg-fallback path coded since
forever — explicitly for the case "colonist ships WHITE-player
names without inline color styles ... and instead uses a colored
background" — but it never received bg data because content.js
was filtering it out at the wire.

Confirmed live: snap showed `color_css: null` for every player
including the orange-in-colonist player who maps to catanatron
WHITE → COLOR_HEX[WHITE] = `#f0f0f0` → white pill. With bg now
flowing through, the harvester can latch the colonist orange.

## v0.33.11 — 2026-05-04

Defensive panel fix: kill the local fantasy-name counter entirely.
After v0.33.10, the panel still showed Dara/Elin/Fynn while chat
showed Aria/Bran/Cyrus — the side panel persists across colonist
tab reloads, so its `_anonSeq` counter carried forward to the
next game and assigned slots 3+ on top of stale entries from a
previous game. The bridge map fix only helps if the bridge map
is populated; with the bridge running stale code (no
`/streamer-anon` endpoint), the panel was falling back to its
poisoned local counter.

- **panel.js** drops the local `_name_to_anon` / `_anonSeq` /
  `_FANTASY_NAMES`. When the bridge map has the username, use it.
  Otherwise return a positional `Opp 1` / `Opp 2` / `Opp 3`
  derived from this snap's opps order — never a fantasy name.
  This guarantees panel labels can only ever match chat or
  fall back to a label that's obviously different (so the user
  knows the bridge sync hasn't landed).

## v0.33.10 — 2026-05-04

Fix the panel-vs-chat name desync. content.js (colonist tab) and
panel.js (side panel) each maintained their own
`_FANTASY_NAMES` counter, so when both saw the same set of real
usernames in different orders the panel ended up labelling
opps as "Elin / Dara / Fynn" while colonist chat / banners
showed "Aria / Bran / Cyrus". Also explained the orange-pill
confusion from v0.33.9: the bridge had latched a transparent
color, AND the player labelled "Cyrus" in chat was actually
labelled "Fynn" in the panel — same root cause for both halves.

- **`POST /streamer-anon`** new bridge endpoint takes
  `{self, names: {real → anon}}` and stores it on bridge state.
  Cleared on the new-game reboot path alongside `display_colors`.
- **content.js** schedules a debounced sync to the bridge every
  time `_name_to_anon` grows or `_anonSelfName` flips. Background
  service worker forwards `streamer-anon` messages to the bridge.
- **panel.js** `anonName()` now consults `snap.streamer_anon`
  first; the local counter only fires as a pre-content-POST
  fallback. Self detection also honours
  `snap.streamer_self_username` so the "You" pill works even
  before catanatron has booted self_color.

## v0.33.9 — 2026-05-04

Fix the orange-pill regression introduced by v0.33.8's hard-blank
CSS rule. With streamer mode on, `color: transparent !important`
was overriding the colonist player-name spans, so the harvester's
`getComputedStyle` fallback was returning `rgba(0, 0, 0, 0)` for
any seat whose username span lacked an inline `color:` (orange,
in Noah's game with Cyrus). The bridge then latched "transparent"
as the player's color and the panel pill rendered white.

- **content.js harvester** filters out `transparent` /
  `rgba(...,0)` color values before storing on the `name` part —
  inline `style.color` still reads correctly, only the computed
  fallback was poisoned.
- **bridge `_harvest_display_colors`** belt-and-suspenders: rejects
  transparent strings on both `color` and `bg` so any payload
  caught mid-flight from older clients can't latch a bad value.

## v0.33.0 — 2026-05-03 (later same day)

Reactive layer over the v0.32 strategy work. Mostly: imminent-tier
threat detection, stronger pulses, and a content-script reliability
pass after Noah caught Chrome surfacing "Extension context
invalidated" + "gave up waiting for scroller" errors.

### Threat / win detection
- **Imminent threat tier** — new "WIN NEXT TURN" banner for
  leader-threat + a louder pulse animation. Fires when leader.vp +
  visible-VP path (city/settle build, dev stash, LR/LA flip)
  ≥ VP_TARGET. Reads catanatron player_state directly so LR/LA
  flips are caught even when the build-VP path alone wouldn't get
  there.
- **Dev-card storm vector** in threat_vector — fires when an opp
  has ≥3 unplayed dev cards regardless of VP. The build-up itself
  is the signal; by the time vp catches up to close_vp, the cards
  are already in their hand.
- **Trade evaluator** auto-declines when offerer is imminent (was
  only checking opp_vp >= close_to_win_vp threshold).
- **Win-this-turn claim path** — fires when self.vp ≥ target with
  held VP cards covering the gap and it's self's turn. Came out of
  the 2026-05-03 vs an opp loss where Noah was at 8 visible +
  2 held VP cards but never claimed.
- **Knight-hint LA-deny** widened — fires when any opp has played
  ≥3 knights, regardless of their visible VP (was: ≥2 played AND
  vp ≥ largest_army_threat_vp). Earlier trigger gives Noah the
  knight-asap nudge before it's too late.

### Robber
- **Robber-target ranker** gets a 2× weight multiplier on tiles
  adjacent to an imminent opp. Captures lower-VP imminent threats
  (e.g. 7 VP + can-city = imminent at vp=7 — currently linear-VP
  weighting alone was too soft).
- **Robber pulses** — `.robber-on-me` and the robber-targets
  section pulse while the placement decision is active.

### HUD polish
- **Pulses bumped** from 8-14px / 0.30-0.45 opacity glow to
  18-22px / 0.65-0.75 with background-color swing — the original
  pulses were too subtle to register as "blinking" against the
  dim panel.
- **Robber-target pill** shows the player's username initial
  ("P" for opp) instead of catanatron's internal color
  letter ("R" when opp was remapped to RED internally).
- **Road Building** keeps both placements visible while
  free_roads_available > 0 (was: hint vanished after the card
  played, leaving the second free-road suggestion on the floor).
- **Engine-deficit alarm** — banner when an opp's per-roll
  production is ≥1.5× self's mid/late game.

### Postmortem
- **Per-player glyph tally** on the Move Annotations header
  ("BrickdDaddy (BLUE): !!:1 !:2 ?!:1") for at-a-glance read
  before scanning per-move detail.
- **Loser had-enough-VP flag** — non-winner whose tracker VP at
  game-end ≥ VP_TARGET gets "← had enough VP, opp closed first"
  inline on the score line.
- **Fingerprint** sources edge + port counts from session.mapping
  (catanatron's CatanMap reads them as 0 / 6).

### Reliability
- **content.js**: wrapped `chrome.runtime.sendMessage` in try/catch
  so the synchronous "Extension context invalidated" throw after
  an extension reload doesn't spam Chrome's error log.
- **Scroller selector** now falls back to `[class^="virtualScroller-"]`
  prefix matching when the exact CSS-module hash misses (colonist
  redeploys reshuffle hashes; the prefix is stable). Same fallback
  on the entry selector for log-row matching.

## v0.32.0 — 2026-05-03

Twirl variant support, Reddit-36k-game-finding tunings, and a real
"win this turn" path that fired the loss case from earlier today.

### Variant maps
- **Twirl** (mapSetting=31, 42 tiles / 126 corners / 168 edges /
  12 ports) plays end-to-end. The bridge auto-detects colonist's
  dual-GameStart pattern (placeholder 19/54/72/9 frame followed
  by the real Twirl shape) and rebuilds the catanatron CatanMap
  on the second frame; without that, every diff with a corner id
  past the placeholder range silently dropped and the next valid
  road build hit "Invalid Road Placement".
- **Recs gate** widened to a `_RECS_SAFE_VARIANTS` whitelist so
  layout-only variants (Twirl, classic) flow through; rules-
  changing variants (5-6 ext, Cities & Knights, Seafarers) stay
  suppressed because the recommender doesn't model their state
  machine.
- **Postmortem fingerprint** labels twirl + pond by tile/corner
  count (was generic "variant") and pulls edge + port counts
  from the colonist mapping (catanatron's CatanMap reads them
  back as 0 and 6 regardless of map shape).
- **Variant builder self-clean** — every variant boot strips the
  prior variant's slice of catanatron's STATIC_GRAPH before
  augmenting with the current map's edges. Without it, two
  variants in the same process collide at the same node-id range
  and settle-distance discards pull in stale neighbors, rejecting
  valid placements.

### Recommender / strategy tunes (Reddit 36k-game findings)
- **3rd-settle expansion bias (#1)** — settlement recs get a 1.25×
  rank bump while self has only the opening 2 footprints, with
  "· settle #3" appended to detail; clears the moment a 3rd
  footprint lands. Paired with a milestone banner ("settle #3 —
  biggest predictor · need 🌲 1 + 🐑 1") that's now actually
  rendered in the HUD; previously the bridge computed it but
  panel.js dropped it on the floor.
- **Wheat priority (#2)** — opening-eval raw production weights
  wheat at 1.10× (every other resource at 1.0×). Wheat is the
  one resource used in every major build; the flat sum was
  under-weighting it against ore-on-6/8 stacks.
- **Composition over pips (#3)** — diversity multiplier bumped
  from 1.05/1.15 to 1.08/1.22 for 2-distinct / 3-distinct nodes.
- **LR push surfaces earlier (#5)** — strategic-options strip now
  flags the longest-road push 1-2 roads from qualifying (was
  1 road only). Past 2 stays quiet to avoid speculative noise.
- **Brick-port early pickup (in-flight before today)** — 1.4×
  rank bump on roads landing on a 2:1 port for a resource self
  already produces; 3:1 ports skipped (Reddit data flagged
  generic-port chasing as net-negative).

### Win detection
- **Claim path for held VP cards** — winning-move banner now fires
  when self.vp ≥ target with held VP cards covering the gap and
  it's self's turn. Came directly out of a 2026-05-03 loss vs an opp where Noah was at 8 visible + 2 held VP cards but
  never claimed before the opp won on their next turn. Banner
  reads "WIN THIS TURN — VP cards in hand bring you to N · play
  any move to claim".

### HUD
- **Resource cells icon-first with vertical divider bars** —
  switched from "N icon" to "icon N" across self hand, opp hand,
  trade pills, near-build missing line, roll-yield strip, discard
  hint, seven-prep. Each cell has padding + a left-border so the
  boundary between resources is unambiguous at a glance. Replaces
  the gap-only spacing that left "1🧱 2🐑" reading as one blob.
- **Dev-deck per-type strip backend** — knight / monopoly / YoP /
  road-building remaining counts ship in `dev_deck.by_type`,
  driving the HUD strip Noah requested for spotting under-
  contested LA pushes by knight scarcity (Reddit finding #4).
  HUD render + CSS shipped in earlier sessions; this completes
  the loop.

### Hint hygiene
- **Piece-supply guard** on monopoly + YoP unlock checks: skip a
  build when the player is out of that piece. Was telling Noah
  "WOOD+WHEAT unlocks settlement" at 5 settlements placed.
- **TradeCloseEvent + offer_id** on TradeOfferEvent so the HUD's
  incoming-trade banner clears the moment colonist marks the
  offer null/closed instead of lingering until the next snapshot.
- **Content scraper** drops the isAtBottom() gate so log events
  keep flowing when Noah scrolls up to read chat.

## v0.31.0 — 2026-05-01

Two extension fixes for the "panel must be open before Start Game"
bug Noah hit on 2026-05-01:

- **inject.js now loads via `world: "MAIN"` content script**, not
  via dynamic `<script>` injection. The async `<script>` tag
  appended at document_start was racing colonist's bundle —
  colonist could create its WebSocket before our patch installed,
  missing the GameStart frame entirely. Loading inject.js as a
  proper MV3 main-world content script runs it synchronously
  before colonist's app code. `web_accessible_resources` removed
  (no longer needed).
- **Toolbar badge** now shows "ON" in green when the active tab
  is on colonist.io. Chrome doesn't allow programmatic side-panel
  open without a user gesture, so the badge is the discoverability
  fix — Noah can spot at a glance whether the extension is
  active. Title text also updates per-tab.

Net effect: WS frames are captured even when the side panel is
closed (the bot's bridge sees GameStart immediately). The HUD
itself only renders once the panel is opened, but the bridge
state is correct from the moment colonist boots the game.

## v0.30.1 — 2026-05-01

Robustness pass — no UI changes, three bridge-side fixes:

- **`UNHANDLED BuildEvent` lines now logged.** DOM-log road events
  arrive without edge coordinates, fall through `_apply_build` to
  "unhandled" status, and silently never apply to catanatron's
  tracker. The recommender then keeps suggesting the same road
  forever because it doesn't know it was placed. Diagnostic-only
  change — every unhandled BuildEvent now prints
  `[ws #N] UNHANDLED BuildEvent: ...` so the gap is visible
  instead of mystery-stale recs.
- **Per-event isolation in `LiveGame.feed`.** Pre-fix, a single
  raising `apply_event` (e.g. `tracker.road` rejecting an invalid
  placement) short-circuited the whole frame's list comprehension
  and silently dropped every later event in that diff. Now each
  event is wrapped in try/except; failures surface as `error`
  DispatchResult; the rest of the frame still applies.
- **Same isolation in `_replay_pre_existing_buildings`.** Mid-game
  reconnect replay used to crash on a single bad seed (corner
  already taken, off-board node, disconnected road). Now catches
  per-build for settlements/cities and defers per-build for roads
  (the retry-until-stable loop still works).

## v0.30.0 — 2026-05-01

Soft revert of the style switcher. The 5/6-style cycler shipped
in v0.28.0–v0.29.1 caused more problems than it solved (style
numbering shifted between releases, alt renderers introduced
runtime errors like the tilesToHtml ReferenceError, monopoly
hint over-counted in untested paths). Removed entirely:

- `<select id="style">` from panel.html
- `bindStyleSelector()` + the renderOverlay dispatcher + the
  four alternate renderers (terminal, newspaper, tactical HUD,
  minimal) in panel.js
- All `html[data-style="N"] .panel { ... }` blocks in panel.css
- The Google Fonts `@import` for the redesigned aesthetics
- `.drawer-select` CSS class

`iconFor()` simplified to always prefer emoji for the resource
glyphs (Noah's pref) with the SVG line-art retained as fallback
when an emoji isn't mapped.

What's KEPT from this session — the actual game-logic wins:

- Postmortem 2/2 final scores fix (build-derived VP fallback)
- Postmortem double longest_road fix (always strip prior holder)
- Knight robber-rec retry on empty snapshot (+ 30-attempt cap)
- WS-side trade-offer parser (tradeState.activeOffers)
- Bridge wiring of TradeOfferEvent → pending_trade_offer
- Recommender: 1:2 longshot trade variant dropped
- Recommender: dev_card suppressed at win-this-turn, halved at
  endgame
- Opp inferred breakdown italic when not fully tracked
- Jargon strings replaced with plain English
- Bridge: silent no-op on malformed type=4 frames
- tilesToHtml hoisted to module scope
- Several escapeHtml-of-svg bugs fixed in trade banner / robber-
  on-me / discard hint / seven-prep / near-miss
- Opening road follow-up: skip edges I already own
- Bridge: auto-detect VP target + discard limit from colonist
  gameSettings
- Monopoly hint: clamp inferred opp counts against authoritative
  totals + physical deck supply
- Discard banner gated on robber_pending (no false-alarm pre-roll)

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

Mid-game bug fixes from Noah's 2026-04-30 opp game, plus a
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
