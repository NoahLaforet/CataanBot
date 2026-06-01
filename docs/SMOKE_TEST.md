# Manual smoke test (v0.39.0)

Automated coverage (pytest, the standalone JS tests, and `node --check`)
is green, but a few changes only show their value in a live browser.
Walk this list against a real colonist.io game (or `extension/pulse-test.html`
for the standalone path) after loading the unpacked `extension/` and
starting the bridge with `./bin/catanbot live`.

## Priority 0 bugs

- [ ] **Robber list pops immediately after a 7.** Roll a 7 on your turn.
  The robber-target ranking should appear within a beat, not after a
  visible delay. Try it a few times.
- [ ] **Robber list pops immediately after a knight.** Play a knight.
  The target list should appear promptly with calm "placed" styling
  (not the urgent "forced" pulse a 7 uses).
- [ ] **Road recs never point into an opponent's settlement.** Mid-game,
  with roads affordable, confirm no road recommendation (hero or alt)
  describes a road landing on or running into an opponent's piece.
  Worst case to check: a board where your expansion corridors are
  sealed by opponents (the rec should still suggest a sealed road toward
  open ground, or none, never one into an enemy corner).

## New this release

- [ ] **Collapsible stat panels.** Click the header of the roll
  histogram, eval sparkline, moves strip, and dev-deck strip. Each
  should collapse to just its header (caret flips to a right arrow) and
  expand again on a second click. Reload the panel: the collapsed/
  expanded state should persist per panel.
- [ ] **One-click launcher (macOS).** Run `./bin/catanbot-tray`. A
  CatanBot item appears in the menu bar. Start bridge turns the dot
  green; Stop bridge turns it grey. "Open colonist.io" brings the bridge
  up and opens the game. Quit stops the bridge.
- [ ] **No-bridge mode is labeled experimental.** In the panel settings
  drawer, the "extension only" mode tip reads "experimental, reduced
  accuracy."

## Release hygiene (quick checks)

- [ ] **Extension loads with the trimmed permissions.** Reload the
  unpacked extension; Chrome should not warn about `scripting` (removed)
  and the HUD should still capture frames and render.
- [ ] **Quiet page console.** Open the colonist.io tab's own DevTools
  console. The `[catanbot] WS interceptor installed` banner should be
  gone (error/warn messages may still appear if something fails).

## Hard to stage (note if you ever hit it)

- [ ] **5-6 player lobby.** In a 5 or 6 player game the HUD should show a
  "limited tracking" state rather than a blank or broken board.
