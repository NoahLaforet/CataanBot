# colonist.io DOM recon (2026-04-20)

Captured live from an in-progress bot game via Chrome-extension
JavaScript probe. All class names are the hashed build IDs colonist.io
currently ships — they are stable within a deploy but WILL rot across
deploys. Selectors must be coded defensively (fall back to structural
matches if class hashes disappear).

## Log panel structure

```
div#ui-game
  div.containerLandscape-TGQeBgol
    div.responsiveContainer-AS9_lPrM.gameFeedsContainer-jBSxvDCV
      div.container-Phl3P_ZR.beige-RdMs0LF_        ← log container
        div.virtualContainer-Y9hPMC2i              ← virtualized list
          div.virtualScroller-lSkdkGJi             ← scroller (watch this)
            div.scrollItemContainer-WXX2rkzf       ← one log entry  (×N)
              <entry body>
                span.messagePart-XeUsOgLX          ← text + inline pills
                  "Gratia stole  from Nona"        ← message text
                  span[style*="color:#HEX"] Gratia ← colored name pill
                  span[style*="color:#HEX"] Nona   ← colored name pill
                  img.lobby-chat-text-icon[alt]    ← resource / action icons
                  span.vp-text  "+1 VP"            ← VP callouts
                img.avatarImage-JNCoQelY           ← bot/player avatar
```

**Virtualization note:** The scroller recycles DOM nodes when they
leave the viewport. A MutationObserver on `virtualScroller-lSkdkGJi`
will fire on new entries added, but historical entries may disappear
if the user scrolls. The userscript needs to record each seen entry
at insertion time — don't try to re-scan the panel later.

## Player identity

- Players are labeled by **username**, not color name.
  Colors are encoded in `style="color:#HEX"` on the name span.
- Observed color assignments in this game (4-player):

  | Player         | Color style                          |
  | -------------- | ------------------------------------ |
  | Rush           | `#223697` (blue)                     |
  | Nona           | `#E09742` (orange)                   |
  | Gratia         | `#E27174` (red)                      |
  | BrickdDaddy    | `#3e3e3e` (gray; disconnected bot)   |

  Standard Catan colors suggest the full palette is blue / orange /
  red / white — BrickdDaddy's gray tone is probably a disconnect
  dimming; confirm by watching reconnect.

## Event catalog (raw `innerText` per entry)

From a single 13-entry window of the live log:

| # | Text (trimmed)                                                             | Key icons (alt=)                       |
| - | -------------------------------------------------------------------------- | -------------------------------------- |
| 0 | `Rush got`                                                                 | Ore                                    |
| 1 | `Nona got`                                                                 | Lumber                                 |
| 2 | `Nona built a Settlement  (+1 VP)`                                         | settlement                             |
| 3 | (empty)                                                                    | —                                      |
| 4 | `Gratia rolled`                                                            | dice_3, dice_4                         |
| 5 | `BrickdDaddy discarded`                                                    | Lumber, Lumber, Ore, Lumber            |
| 6 | `Friendly Robber is active, tiles available to block are limited`          | robber                                 |
| 7 | `Gratia moved Robber  to`                                                  | robber, prob_3, ore tile               |
| 8 | `Gratia stole  from Nona`                                                  | Resource Card (hidden)                 |
| 9 | `Gratia built a Road`                                                      | road                                   |
| 10| `Gratia wants to give  for`                                                | Ore, Lumber                            |
| 11| `BrickdDaddy has disconnected. A bot will take over next turn…`            | (avatar only)                          |
| 12| `Gratia gave  and got  from Rush`                                          | Ore, Lumber                            |

## Icon alt-text vocabulary (so far)

Resource cards (exact alt values):

- `Ore`
- `Lumber`
- `Wool` (not yet seen in sample — inferred from standard set)
- `Wheat` (not yet seen — inferred)
- `Brick` (not yet seen — inferred)
- `Resource Card` — used when the card is **hidden** (steals)

Actions / pieces:

- `settlement`, `road`, `city` (last inferred)
- `robber`
- `dice_N` where N ∈ 1..6 (two per roll)
- `prob_N` where N is the tile's red-number (e.g. `prob_3`)
- `<resource> tile` (e.g. `ore tile`) — tile type target for robber

## Parser implications

Good news:

- **Every event has an `alt`-tagged icon** identifying the semantic
  content. No OCR, no guessing from emoji.
- **Resource quantities are the icon count**, not a number in text.
  `discarded` with four Lumber icons = 4 cards discarded.
- **Robber destination is fully encoded**: `prob_3` + `ore tile` =
  the 3-dot ore hex.
- **Colors are in hex**, so a single map `hex → catanatron-color`
  covers multi-game sessions.

Watch out for:

- Steals don't leak the resource (good for opponents, bad for you)
  — we'll need inference later from what the stealer has visible.
- `wants to give` is an *offer*, not a commit. Only `gave … and got …`
  represents a completed trade.
- The `Friendly Robber` line is metadata, not a player action —
  parser must skip.
- Virtualized list means late-joiners can't see the start. Userscript
  must attach before the game begins, or accept a partial history.

## Recommended selectors for the userscript

Primary (class-based, fastest):

```js
const CONTAINER = 'div.container-Phl3P_ZR.beige-RdMs0LF_';
const SCROLLER  = 'div.virtualScroller-lSkdkGJi';
const ENTRY     = 'div.scrollItemContainer-WXX2rkzf';
const TEXT      = 'span.messagePart-XeUsOgLX';
const NAME_PILL = 'span[style*="color:#"]';
const ICON      = 'img.lobby-chat-text-icon';
```

Fallback (structural, survives class-hash rotation):

- Find any `<div>` whose innerText contains ≥3 of
  `["rolled", "built", "got", "stole", "moved Robber"]`. That's the
  log container.
- Entries are its scroller's immediate children.

## Next step

With this in hand the Day 1 userscript is a ~100-line
MutationObserver that:

1. Polls every 500ms until `SCROLLER` exists.
2. Attaches a MutationObserver to `SCROLLER`.
3. On each added node, serialises `{text, colorNames, iconAlts}` and
   POSTs to `http://localhost:8765/log` on a FastAPI bridge.
4. Bridge prints the structured events to stdout — no parsing yet.

Once that pipe is proven, Day 2 is the regex/alt-text parser.
