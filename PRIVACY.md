# Privacy Policy — CatanBot

_Last updated: 2026-05-04_

## TL;DR

CatanBot does not collect, transmit, or store any user data
remotely. The Chrome extension communicates only with a Python
bridge running on the user's own machine and the colonist.io
game tab — that's it. No analytics, no telemetry, no third-party
requests, no tracking pixels, no remote logging.

## What the extension reads

While you have a colonist.io game tab open and the CatanBot side
panel pinned, the extension's content scripts:

- Inject a hook into the colonist.io page world that observes
  the WebSocket frames colonist's own JavaScript already sends
  and receives. Frames are forwarded over `chrome.runtime` to
  the extension's service worker.
- Walk the colonist.io chat log DOM to extract event text
  (rolls, builds, trades, dev card plays) for the local strategy
  engine.

The extension does not modify colonist's game state, does not
click for you, and does not bypass any colonist UI.

## What gets transmitted

Every frame and DOM event observed by the extension is forwarded
to a single endpoint:

```
http://127.0.0.1:8765
```

This is the loopback address — your own machine, never leaves
your computer. The Python bridge that listens on that port is
also open-source and runs locally. You install it once on your
machine; the extension cannot function without it.

The extension makes no requests to any other host. There is no
fallback, no telemetry endpoint, no analytics service, no remote
logging. You can verify this by inspecting the extension's
network activity in Chrome's DevTools.

## What gets stored

Locally, on your machine only:

- Browser-side: a small `chrome.storage.local` blob of UI
  preferences (font scale, streamer-mode toggle, panel section
  collapse states, opacity).
- Bridge-side: per-game WebSocket frame mirrors in
  `~/Desktop/CataanBot/sessions/active.jsonl` (rotates per
  game, capped at the active session), and an HTML
  post-mortem of each finished game in `postmortems/`. These
  files are written by the bridge process to your own disk.

Nothing is uploaded anywhere.

## Permissions

The extension requests the following Chrome permissions:

| Permission | Why |
|------------|-----|
| `sidePanel` | Renders the HUD in Chrome's native side panel. |
| `storage` | Persists UI preferences across browser sessions. |
| `scripting` | Injects the WebSocket observer into colonist.io. |
| `tabs` | Auto-opens the post-game HTML report in a new tab. |
| Host: `colonist.io/*` | Reads the game DOM and WebSocket frames. The extension's only data source. |
| Host: `127.0.0.1:8765` | Talks to your local Python bridge. The only outbound destination. |

## Open source

CatanBot is GPL-3.0 licensed. Full source is on GitHub:
[https://github.com/NoahLaforet/CatanBot](https://github.com/NoahLaforet/CatanBot)

You are free to inspect, audit, and modify the code.

## Contact

Questions, issues, or audit requests: noah.laforet@icloud.com

GitHub issues: [https://github.com/NoahLaforet/CatanBot/issues](https://github.com/NoahLaforet/CatanBot/issues)
