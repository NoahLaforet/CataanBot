# Overnight run - morning report

Read with docs/OVERNIGHT_RUN.md. TL;DR: the night's headline turned out to be
a **disk-corruption recovery** (your drive had unreadable blocks on ~17 files);
I fully recovered the repo + venv from git/GitHub, then fixed the knight-robber
bug and added the bridge-download CTA. The board-overlay live-verify and the
distribution release need you (they require live game placement and Apple
signing I can't do unattended).

## !! READ FIRST - possible failing disk

Your volume threw unreadable-content errors (file metadata fine, reading the
bytes hung forever) on ~17 scattered files - the old `.venv`, repo source,
two git OBJECTS, and `.pyc` caches. That pattern (static bad blocks) is an
early sign of a failing SSD/filesystem. I recovered everything, but please:
**back up, then run Disk Utility > First Aid on the volume** (and consider
`smartctl`/a drive health check). If it recurs, the drive may need replacing.

## DONE + committed

1. **Disk recovery (the import hang).** Recreated `.venv` fresh (old at
   `.venv.corrupt.bak`), reinstalled bridge deps. Restored every corrupted
   tracked source file from local git; two files whose git OBJECTS were also
   corrupt (`bridge_robber.py`, `tracker.py`) were re-fetched from GitHub via
   `gh api`. Cleared corrupt `.pyc` caches. Re-scan: all 171 source files
   readable, full pytest suite passes. `import catanbot.bridge` = 0.0s.
2. **Source bridge running v0.51.0** (`./bin/catanbot bridge`, pid in
   `$TMPDIR/srcbridge2.log`). The Mac app reported `bridge_version: null`; this
   reports `0.51.0`. If it's down in the morning, just relaunch
   `~/Applications/CatanBot.app` (works fine, just an older version string).
3. **`27ba77e` bridge: knight-robber fix.** Root cause: colonist logs a played
   knight as "X used [icon]" whose alt text lacks "knight", so the parser emits
   `DevCardPlayEvent(card='unknown')`; the robber handler only matched
   `card=='knight'`, so it never armed `robber_pending` on a knight - exactly
   your "nothing for the robber" symptom. Fix: arm the robber rec on `unknown`
   too. 22 dev-card tests pass. NEEDS LIVE CONFIRM: play a knight, check the HUD
   shows ROBBER TARGETS (and the green hexagon).
4. **`a75ce68` loghud: bridge-download CTA** in the HUD's offline state (the
   "new pop-up" - that CTA only lived in the retired side panel). Platform-aware
   download button + launch hint.
5. Earlier tonight (reload `catanbot-inpage-hud-dev` to get all): floating gear
   (`ce8c311`), dice histogram (`5a7b643`), affordability glow + hex robber ring
   + tab-bar footgun (`33b54a4`), dev-card stack glow (`0cca694`).

## NEEDS YOU (couldn't finish unattended)

- **Task 4 - board-overlay orientation.** Code is in (green hexagon on the
  robber tile). I could NOT live-verify because driving a fresh game's opening
  placement is canvas clicks I can't do reliably. On your next 7 / knight: does
  the BRIGHT hexagon sit on the #1 target tile the HUD names? If mirrored
  top<->bottom, flip `py = cy - rr*dV` back to `+` in `boardCoordToPixel`
  (loghud.js). If just offset, nudge `BOARD_CALIB`.
- **Task 3 - opening resources as "?".** The hand tracker credits resources from
  public ProduceEvents (`hand_tracker.py`). Whether opponents' 2nd-settlement
  cards show as known depends on whether colonist emits a public "X got [..]"
  ProduceEvent for the opening deal. Capture a fresh game's first-turn /log
  POSTs (or chat log) to confirm; if colonist is silent, seed the opening hand
  from the 2nd-settlement adjacency in the BuildEvent handler. Needs a clean
  game capture I couldn't get.
- **Task 6 - v0.51.0 bridge release.** Now that imports work, the Mac build is
  unblocked. Steps: build the app (`build-app.sh`) -> `sign_and_notarize.sh`
  (needs your Apple Team 3A9BRYYWX4 creds - I can't sign). Then `gh release
  create v0.51.0` (publishing fires `.github/workflows/build-windows.yml` which
  builds + attaches `CatanBot-windows.zip`). Asset names the extension expects:
  `CatanBot-macos.zip`, `CatanBot-windows.zip`. DON'T publish until BOTH assets
  exist or `/releases/latest/download/` 404s (your CTA points at macos.zip). I
  did not auto-publish (outward-facing + needs signing).

## Cleanup left in the tree (safe to delete)
`.venv.corrupt.bak`, `.venv_stale_backup`, and the dot diagnostic files
(`.import_test.txt`, `.fh*.txt`, `.trace*.txt`, `.fullscan.txt`,
`.corrupt_scan.txt`, `.restore_all.txt`, `.fix_result.txt`, `.pip_*.txt`,
`.pytest_out.txt`). I remove these at the end of the run; listed here in case
any linger.
