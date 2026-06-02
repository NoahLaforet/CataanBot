# Manual smoke test (v0.42.0)

Automated coverage (pytest, the standalone JS tests via `node --test
tests/js/`, and `node --check`) is green, but a few things only show
their value in a live browser. Walk this list against a real
colonist.io game after loading the unpacked `extension/`. Run the
bridge with `./bin/catanbot live` for the bridge-path checks; stop it
(or set the panel to "extension only") for the no-bridge checks.

## Priority 0 bugs

- [ ] **Robber list pops immediately after a 7.** Roll a 7 on your turn.
  The robber-target ranking should appear within a beat, not after a
  visible delay.
- [ ] **Robber list pops immediately after a knight.** Play a knight.
  The target list should appear promptly with calm "placed" styling
  (not the urgent "forced" pulse a 7 uses).
- [ ] **Road recs never point into an opponent's settlement.** Mid-game
  with roads affordable, confirm no road rec (hero or alt) lands on or
  runs into an opponent's piece, even on a board where your corridors
  are sealed.

## Dice + dev cards (since v0.40)

- [ ] **Dice histogram matches colonist.** Open colonist's Dice Stats
  tab at game end; the HUD histogram counts should match it exactly
  (authoritative end-of-game stats, not an incremental tally).
- [ ] **Per-opponent played dev cards.** The PLAYERS section shows each
  rival's played dev-card breakdown (knights, monopolies, etc.), all
  from the public log.

## Variant boards

- [ ] **Gold Rush (fog board).** Start a Gold Rush game. Under
  restricted starting placement the first two settlements are limited to
  shown (non-fog) corners; roads into the fog ring surface in recs; when
  the gold hex reveals mid-game the snapshot names which resource to
  take.
- [ ] **Volcano.** Start a Volcano game (gold center + Black Forest fog
  rim). Opening picks and the recommender run (not suppressed), and a
  gold-adjacent corner is valued as a wildcard.
- [ ] **Pond / Twirl.** A weekly-rotation layout-only variant builds a
  fresh board and opening picks + 2:1 port rates work on the real
  geometry.

## No-bridge (standalone JS) path

- [ ] **HUD works with the bridge off.** Stop the bridge (or set the
  panel settings to "extension only"). After a few seconds the HUD
  keeps rendering: recommendations, dev-card hints, robber targets, and
  the strategy banner all populate from the JS recommender.
- [ ] **No-bridge mode is labelled experimental.** The panel settings
  drawer's "extension only" tip reads "experimental, reduced accuracy".
- [ ] **Incoming-trade verdict on the no-bridge path.** When an opponent
  offers a trade, the banner shows an ACCEPT / DECLINE / CONSIDER
  verdict with a reason, and a rebalanced counter when the ask is
  lopsided.
- [ ] **Bridge upgrade is graceful.** Start the bridge back up; the
  advanced panels (eval sparkline, full postmortem link) reappear with
  no broken UX.

## HUD layout

- [ ] **Grouped, collapsible panel.** The HUD groups into the pinned
  hero zone plus YOU, RECOMMENDATIONS, PLAYERS, and ROLLS & STATS.
  PLAYERS and ROLLS & STATS start collapsed; each group's open/closed
  state persists across a reload.

## Launcher + release hygiene

- [ ] **One-click launcher (macOS).** Run `./bin/catanbot-tray`. A
  CatanBot item appears in the menu bar; Start/Stop toggles the bridge,
  "Open colonist.io" brings it up and opens the game.
- [ ] **Extension loads with trimmed permissions.** Reload the unpacked
  extension; Chrome should not warn about `scripting`, and the HUD still
  captures frames and renders.
- [ ] **Quiet page console.** The colonist.io tab's own DevTools console
  has no `[catanbot]` banner noise (warn/error on real failure is fine).

## Hard to stage (note if you ever hit it)

- [ ] **5-6 player lobby.** In a 5 or 6 player game the HUD should show a
  "limited tracking" state rather than a blank or broken board.
