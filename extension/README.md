# CatanBot — Chrome Extension

The CatanBot HUD for colonist.io, driven by the local Python bridge on
`localhost:8765`. As of v0.50 the HUD lives **in the page**: it takes over
colonist's own log column with a `Log | CatanBot` tab bar (the CatanBot tab
shows the recommendation, opponent hand reads, and an urgency footer; the Log
tab flips back to the game log), styled to blend with the native site. The
old Chrome side panel is still there as a full-view fallback (click the
toolbar icon), but you no longer need it. Set
`localStorage 'catanbot.log_hud'='0'` on colonist.io to turn the in-page HUD
off and use the side panel only.

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

## What it renders

The side panel (toolbar icon) is the full-detail fallback view; the in-page
HUD is the default day-to-day surface. (The Tampermonkey userscript under
`legacy/userscript/` is archived and frozen.) Everything the old userscript
did renders in the panel, all driven by the same local bridge:

- Roll histogram (colonist's authoritative dice counts), eval
  sparkline, and a per-build move-quality strip.
- Robber target ranking with victim pills, friendly-robber aware.
- Trade-offer evaluator with an accept / reject verdict and the EV
  delta in line.
- Win-this-turn callout plus the ranked build recommendations.
- Knight / Monopoly / Year of Plenty / Road-Building play-or-hold
  hints.
- Per-opponent ports, production rates, and a played dev-card
  breakdown.
- Variant boards (Pond, Twirl) and the Gold Rush fog board.

The HUD also groups into pinned + collapsible sections (YOU,
RECOMMENDATIONS, PLAYERS, ROLLS & STATS) so it stays compact during a
game.

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
- `storage`: persists UI preferences (streamer mode, font scale, panel
  collapse states) plus badge/turn state across browser sessions.
- `host_permissions` for `colonist.io`: read game state from the page.
  The page-world WebSocket hook ships as a static `content_scripts`
  entry (`inject.js`, `world: MAIN`), which needs no extra permission.
- `host_permissions` for `127.0.0.1:8765`: talk to the bridge running
  on the user's own machine. **No data leaves the user's computer.**

## Privacy

The extension reads game-state messages from colonist.io and forwards
them to a Python bridge that runs **locally** on the user's machine
(default port 8765). Nothing is sent to a server we control. The
bridge is open source; users can audit what's sent.
