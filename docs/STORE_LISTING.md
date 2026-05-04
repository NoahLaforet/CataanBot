# Chrome Web Store listing — CatanBot

Reference doc for the CWS submission. Everything in here is ready
to paste into the developer dashboard at
https://chrome.google.com/webstore/devconsole.

---

## Listing fields

### Item name
`CatanBot — Colonist Advisor HUD`

### Short description (132 char max)
`Live Catan advisor for colonist.io: move recs, robber targets, dev-card timing, opponent inference, post-game analysis.`

### Detailed description (suggested)
```
CatanBot is a side-panel HUD for colonist.io that runs alongside
your Catan game and surfaces decision-support information in real
time — the way a chess engine's analysis bar sits next to a chess
board.

WHAT IT DOES
• Opening picks — ranks every legal corner for both your 1st and
  2nd settlement using complement-aware production scoring,
  port adjacency, denial value, and resource diversity. Pairs
  each pick with a follow-up road suggestion.
• In-game recommendations — top-N action ranking with score
  breakdowns. Settlement, city, road, dev card, port trade,
  player-trade proposal. Each rec carries a 1-ply lookahead
  delta showing how much better the bot's pick is than the
  alternative.
• Dev-card play hints — Knight, Monopoly, Year of Plenty, Road
  Building each get a typed PLAY/HOLD verdict with conversational
  reasoning ("an opp is close to Largest Army — play to deny",
  "robber's on you — play to clear it").
• Robber & 7-roll telemetry — top robber targets ranked by
  blocking value × victim VP × hand size × resource scarcity.
  Auto-detects colonist's optional Friendly Robber rule.
• Strategy archetype tracker — once your opening settlements
  land, the bot detects which archetype you're playing
  (Ore-Wheat-Sheep, Longest Road rush, Port trader, Road
  Builder, or Balanced) and biases its recommendations
  accordingly. Mid-game pivot triggers (hot numbers, dev cards
  drawn, opponents closing on Largest Army) keep the strategy
  responsive.
• Live HUD — roll histogram with 36-roll baseline, eval
  sparkline (chess-style position graph), per-build
  move-quality grading, opponent hand inference, production
  rate.
• Variant board support — works on layout-only variants like
  Pond and Twirl, not just the standard board.
• Auto-postmortem — when a game ends, a self-contained HTML
  report opens automatically: winner, final VP breakdown, dice
  fairness, hand dynamics, trade quality, 7-roll impact, and
  the full game-progression charts.

PRIVACY
CatanBot is local-only. The extension talks to a Python bridge
running on your own machine at 127.0.0.1:8765 and nothing else.
Your colonist.io game data never leaves your computer.

REQUIREMENTS
You need to install and run the open-source Python bridge once
on your own machine — full instructions at the project page.
Python 3.11+, macOS or Linux. The bridge handles all the heavy
lifting; the extension is the rendering surface.

WHAT IT IS NOT
CatanBot is a decision-support tool, not an autoplay bot. It
reads the game (via the colonist DOM and WebSocket frames) and
shows recommendations. It does not click for you, it does not
interact with colonist's game state, and it does not bypass any
game rules. Every action still happens through your own clicks.
Think of it as a chess-engine analysis bar in a side panel.

OPEN SOURCE
GPL-3.0. Full source on GitHub. Issues and contributions welcome.
```

### Category
`Productivity` (primary) — `Tools` works as a fallback.

### Language
`English`

---

## Required assets to upload

| Asset | Size | Path | Status |
|-------|------|------|--------|
| Small icon | 128×128 | `extension/icons/icon-128.png` | ✅ already in repo |
| Promotional tile (small) | 440×280 | — | ⏳ needs creation |
| Promotional tile (marquee, optional) | 1400×560 | — | ⏳ optional |
| Screenshot 1 | 1280×800 or 640×400 | — | ⏳ HUD on a real game |
| Screenshot 2 | same | — | ⏳ Opening picks view |
| Screenshot 3 | same | — | ⏳ Strategy banner + ranking |
| Screenshot 4 | same | — | ⏳ Robber targets + dev hints |
| Screenshot 5 | same | — | ⏳ Auto-postmortem |

### Screenshot capture checklist
1. Start a fresh game on colonist.io
2. Disable streamer mode for screenshots (the listing should
   show the real HUD; streamer mode is a runtime toggle)
3. Cmd+Shift+4 in macOS to crop to the panel; resize to
   1280×800 in Preview before upload
4. Cover: opening, mid-game with strategy banner, robber
   placement, dev-card play, post-game

### Promotional tile (440×280) suggestion
- Dark background matching HUD palette (#11151f)
- Hex tile graphic on the left, "CATANBOT" wordmark on the
  right with tagline "live Catan advisor for colonist.io"
- Can ship a placeholder for v1; replace when there's time

---

## Required policy assets

### Privacy policy
The CWS requires a privacy policy URL. Since the extension is
local-only and ships nothing offsite, the policy is short:

```
CatanBot does not collect, transmit, or store any user data
remotely. The extension communicates only with a Python bridge
running on the user's own machine (127.0.0.1:8765) and the
colonist.io game tab. No telemetry, no analytics, no third-
party requests. All game state, strategy logs, and postmortems
remain on the user's machine.

Source code is available at https://github.com/NoahLaforet/CatanBot
under the GPL-3.0 license.

Questions: noah.laforet@icloud.com
```

Host this at e.g. `noahlaforet.github.io/catanbot-privacy`
(GitHub Pages, free) or as a section in the repo README and
link to that anchor.

### Permission justifications
The dev-console asks for a one-line justification per permission:

| Permission | Justification |
|-----------|---------------|
| `sidePanel` | Renders the HUD in Chrome's native side panel adjacent to the colonist.io game. |
| `storage` | Persists user preferences (streamer mode, font scale, panel toggles) across browser sessions. |
| `scripting` | Injects the page-world WebSocket hook into the colonist.io tab. |
| `tabs` | Auto-opens the post-game HTML report in a new tab adjacent to the game. |
| `colonist.io host permission` | Reads the game DOM and WebSocket frames. The extension's only data source. |
| `127.0.0.1:8765` host permission | Talks to the local Python bridge. Required for all decision-support features. |

### Single-purpose declaration
> CatanBot is a single-purpose extension that surfaces decision-
> support information for the Catan board game on colonist.io.

---

## Build + submit checklist

1. Run `bin/build-extension-zip.sh` to produce `dist/catanbot-extension-v{X.Y.Z}.zip`
2. In the dev console: Items → New item → upload the zip
3. Fill in listing fields from this doc
4. Upload screenshots + promo tile
5. Paste privacy policy URL
6. Save draft, hit "Submit for review"
7. Reviews typically take 1-3 business days

---

## After approval

- Update README to point at the CWS install link instead of
  unpacked install instructions (keep both — power users want
  unpacked for git updates)
- Bump version on every repo release; the CWS auto-updates within
  ~24h of a new package upload
