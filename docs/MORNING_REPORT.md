# Overnight run - morning report

## ⛔ READ THIS FIRST - your disk is actively failing

Not "had some bad blocks" - **failing in real time tonight.** Evidence:

- Files that PASSED tests earlier in the session were UNREADABLE hours later
  (`test_bridge_dev_cards.py` ran green, then its bytes stopped reading).
- A `tar` of the working tree, even excluding every known-bad file, **hung on
  a newly-corrupt file** - corruption is spreading faster than I can snapshot.
- ~30+ files now hang on read (metadata/`ls` fine, reading the content blocks
  forever). The set grew while I worked.

**`git push` is broken by it** too: the packer walks the object DB, hits a
corrupt object, and hangs. So your local commits could NOT be pushed normally.

### What you must do, in order
1. **Stop heavy work on this disk.** Every read/write risks more loss and may
   accelerate the failure. Ideally power down until you can image the drive.
2. **Back up / image now** to an EXTERNAL disk (Time Machine, or
   `ddrescue` for a failing-drive-aware clone). Don't trust this volume.
3. Run Disk Utility > First Aid + a SMART check (`smartctl -a`). Expect to
   **replace the drive**; this reads like end-of-life SSD/filesystem.

## ✅ Your work is preserved on GitHub (this is the important part)

`git push` was dead, so I preserved the current file contents straight to
GitHub via the API, bypassing the corrupt object DB:

- **Branch: `disk-recovery-2026-06-16`** on `NoahLaforet/CatanBot`.
- **75 files** = the entire readable source tree at its CURRENT state: all of
  `extension/` (loghud.js, content.js, background.js, panel.js, manifest...),
  all of `src/catanbot/` (bridge.py, colonist_diff.py, the knight-fixed
  bridge_postmortem.py, opp_inference.py, ...), `scripts/`, `docs/`, the
  Windows CI workflow. This is the cumulative result of **48 local commits that
  were never on GitHub** (the whole in-page-HUD project: v0.49 -> v0.51).
- It's the file CONTENT, not the 48-commit history. On a healthy machine:
  `git fetch`, branch off `disk-recovery-2026-06-16`, and reconcile. Your
  shipped releases (CWS extension, release binaries) are unaffected.

**Could NOT preserve** (corrupt + not in git, or corrupt-unreadable): the
`ws_captures/*.json` fixtures (local-only, gone), several `tests/*.py`,
`tests/fixtures/*.json`, `tray/assets/*.png`, a few `scripts/` and `docs/`.
Tracked ones can be restored from GitHub `main` history on a healthy disk.

## What got fixed tonight (in the preserved files)

1. **Knight-robber bug (commit 27ba77e, preserved).** Colonist logs a played
   knight as an icon whose alt-text lacks "knight", so the parser tags it
   `card='unknown'`; the robber handler only armed on `'knight'`. Now it arms
   on `'unknown'` too - this was your "knight ready, HUD says nothing" bug.
2. **Opening resources "?" - already fixed in code.** I traced the whole path:
   `colonist_diff.py` (line ~605) already emits a ProduceEvent from the 2nd
   settlement's adjacent tiles (skipping self), `live_game.feed` applies it to
   both the tracker AND the particle model, and the snapshot uses those
   minimums. So opponents' opening cards DO resolve to known resources - on a
   CURRENT bridge. You saw "?3" because **you were running a stale bridge you
   couldn't update** (the premise of this run). Nothing to fix; just run the
   rebuilt bridge. (I could not add a regression test: the capture fixture it
   needs is one of the disk-corrupted files.)
3. **Bridge-download CTA (a75ce68, preserved)** in the HUD offline state.
4. **All the live-play HUD fixes (preserved in loghud.js):** floating gear,
   dice histogram, affordable-only build glow, hex robber ring, dev-card glow,
   opponent reads on player rows, click-to-cycle recs, native trade badge.

## Task 6 (release) - DRAFT staged for you
A **draft** v0.51.0 release now exists on the repo (targets the recovery
branch as the build source; drafts aren't public and don't touch "latest", so
the download CTA still works). It has exact finish steps in its notes. To
complete on a healthy machine after disk recovery:
- Build/sign the Mac zip (`build-app.sh` + `sign_and_notarize.sh`, Team
  3A9BRYYWX4) -> `gh release upload v0.51.0 CatanBot-macos.zip --clobber`.
- Get `build-windows.yml` onto `main` (it's only on the recovery branch), then
  publishing the release fires the Windows CI (or `gh workflow run`). It builds
  the .exe on a GitHub Windows runner - no local build needed.
- DON'T build the Mac app on THIS disk. Only publish once BOTH zips are on.

## Still needs you
- **Board-overlay orientation** (lowest priority) - live-verify on a 7/knight
  (clean game). Code is in; if the bright hexagon is mirrored top<->bottom,
  flip the `py` sign in `boardCoordToPixel` (loghud.js). 2-second check.

## Source bridge
A source bridge (v0.51.0, with the knight fix) was running from this disk; it
may not survive the disk failure. After you recover, run from the healthy copy.
Diagnostic scripts (`.preserve.py`, `.verify_opening.py`, `.tar_excludes.txt`)
are throwaway - delete them.
