# Tampermonkey userscript (archived)

> **No longer maintained.** Use the Chrome extension at `extension/`
> instead — `chrome://extensions` → Load unpacked → pick `extension/`.
> Future bug fixes and HUD updates only land in the extension.

This folder is preserved as a snapshot of the last-working
Tampermonkey path (last updated alongside extension v0.30.1, head
`882059c`-ish, 2026-05-01). Everything below describes how to install
the snapshot if you need a non-Chrome HUD; nothing here will get
backported once the extension diverges.

## Why it's archived

The Chrome extension solves every problem the userscript was designed
for: same DOM/WS pipe, same bridge protocol, but the HUD lives in
Chrome's native side panel instead of a draggable overlay on top of
the colonist board. No more frame-rate jitter, no `:host` shadow-DOM
quirks, no "where did the panel go" after a page nav. And reloading
is one click in `chrome://extensions` instead of editing a
Tampermonkey script.

## Snapshot install (if you really need it)

1. Install [Tampermonkey](https://www.tampermonkey.net/) (or
   Violentmonkey) in your browser.
2. Open `colonist_cataanbot.user.js` in this folder, copy the
   contents, paste into a new Tampermonkey script. Save.
3. Confirm it's enabled on `colonist.io/*`.
4. Run the bridge as usual: `./bin/cataanbot bridge --advisor`.
5. Open colonist, start a game.

The userscript talks to the same bridge as the extension does (POST
`/log` and `/ws`, GET `/advisor`), so the bridge-side fixes that ship
in `main` will still work — only the HUD rendering layer is frozen.

## Files

- `colonist_cataanbot.user.js` — the user script itself (frozen).
- `board_probe.js` — diagnostic snippet for tile/edge inspection in
  the colonist DOM. Pasted into the Chrome devtools console; not
  loaded as a userscript.
