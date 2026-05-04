# CatanBot — Chrome Extension

The Chrome side-panel version of the CatanBot HUD. Same backend (the
local Python bridge on `localhost:8765`); difference is purely UX:
the HUD lives in Chrome's right-side panel instead of as an overlay
on top of the colonist board.

## Install (Developer mode — for testing)

1. Open `chrome://extensions` in Chrome.
2. Toggle **Developer mode** (top-right).
3. Click **Load unpacked**.
4. Pick the `extension/` folder in this repo.
5. Pin the extension to the toolbar (puzzle-piece icon → pin).
6. Click the green CatanBot icon — the side panel opens on the right.
7. Start the bridge from a terminal:

   ```bash
   cd path/to/CatanBot
   ./bin/catanbot bridge
   ```

8. Open a colonist.io game. The panel will show recommendations,
   plan, and threats live.

## Install (Chrome Web Store — for real)

Once published:

1. Visit the Web Store listing (after Noah uploads it).
2. Click "Add to Chrome".
3. Pin + click as above.

The Web Store path costs **$5 one-time** for developer registration.
After that, every push to the store auto-updates installed copies
within ~24 hours.

## What's NOT in v0.26.0 yet

The userscript HUD (v0.25.4) has more polish than this extension's
MVP panel:

- Full dragon's-tail of HUD principles (drag, opacity slider, fonts).
- Roll histogram, eval sparkline, move-quality strip.
- Robber target ranking with victim pills.
- Trade-offer evaluator banner.
- Strategic options + win-this-turn callout.
- Knight / monopoly / YoP / road-building hints.
- Per-opp ports + production breakdowns.

Those will land in 0.26.x patches once the basics are confirmed
working. The userscript continues to be the full-featured option for
now; both can run simultaneously since they talk to the same bridge.

## Architecture

```
extension/
├── manifest.json       MV3 manifest
├── inject.js          Runs in page world: patches WebSocket
├── content.js         Isolated world: relays WS frames + scrapes /log DOM
├── background.js      Service worker: localhost forwarding + side panel
├── panel.html         Side panel shell
├── panel.css          HUD styles (slate-blue dashboard)
├── panel.js           HUD render + /advisor polling
└── icons/             16/48/128 px green-on-slate icons
```

Communication flow:

```
colonist.io page (main world)        extension (isolated)         service worker
─────────────────────────────        ────────────────────         ──────────────
inject.js
  patches WebSocket
  postMessage(frame) ─────────────► content.js
                                    chrome.runtime.sendMessage ─► background.js
                                                                  fetch /ws POST
                                                                  to localhost:8765

DOM mutations on .game-log ─────► content.js
                                    chrome.runtime.sendMessage ─► background.js
                                                                  fetch /log POST

                                                                  side panel page
                                                                  fetch /advisor GET
                                                                  every 500ms
```

## Permissions justified

- `sidePanel` — the whole point.
- `storage` — reserved for future preference persistence.
- `scripting` — reserved (currently unused) for future page-script
  injection beyond the static `inject.js`.
- `host_permissions` for `colonist.io` — read game state from the page.
- `host_permissions` for `127.0.0.1:8765` / `localhost:8765` — talk
  to the bridge running on the user's own machine. **No data leaves
  the user's computer.**

## Privacy

The extension reads game-state messages from colonist.io and forwards
them to a Python bridge that runs **locally** on the user's machine
(default port 8765). Nothing is sent to a server we control. The
bridge is open source; users can audit what's sent.
