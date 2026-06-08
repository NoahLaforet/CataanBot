# Build prompt: in-page HUD full parity + native per-player reads

## Goal

Bring CatanBot's in-page HUD to **full parity with the side panel**, and add
**per-player reads injected next to each colonist player row**. The CatanBot
tab should show everything the side panel does (recs + sub-lines, robber
targets, self card, opponents, dev deck, eval/move-quality, banners,
post-game), and each colonist player row (Peony / Webber / Hett ...) gets
CatanBot's live hand read next to it. Keep it resilient: background-routed
data, React-survival, streamer-safe, floating-overlay fail-safe. Tests green,
extension only, never legacy/userscript.

## Where we are (v0.50.0, shipped + working live in Comet)

The in-page HUD is injected into colonist's log column: a `Log | CatanBot`
tab bar + a SLIM CatanBot tab (top rec + opponent hand reads + a 1-line
urgency footer). Anchored via `content.js` `findLogContainer()` (selectors
confirmed current by live capture). Data comes through the background
worker's `get-advisor` message (a content-script direct fetch to
`http://127.0.0.1` is blocked in Comet: ERR_BLOCKED_BY_CLIENT). Streamer-safe,
survives React re-renders, falls back to the floating overlay if the anchor
ever fails. Side panel retired to an icon-reachable fallback.

The gap Noah called out: the CatanBot tab is too minimal. He wants the FULL
side-panel information in it, plus each player's resource read shown next to
the player.

## Approach

### Track 1 — Full parity in the CatanBot tab (embed the panel, don't re-port)

Render the ACTUAL side panel inside the CatanBot tab via an **iframe of
`panel.html`**, instead of re-implementing its renderer. This gives exact
parity instantly AND stays caught up forever (one renderer, no drift).

- Add `panel.html` + its assets (`panel.js`, `panel.css`, `lib/*`, `icons/*`)
  to `web_accessible_resources` for `https://colonist.io/*`.
- `loghud.js` injects `<iframe src="<chrome.runtime.getURL('panel.html')>"
  id="cbo-loghud-frame">` as the CatanBot-tab body (replacing the slim
  render). The iframe runs `panel.js`, which already fetches via the
  background worker and renders the full HUD.
- Size the iframe to fill the log column; the panel's own CSS handles internal
  layout + scroll.
- Keep the existing slim render as a lightweight fallback if the iframe fails
  to load (onerror).
- TRADEOFF: the embedded panel keeps CatanBot's own (dark) styling, not
  colonist's beige. That is the fast, sustainable path to "fully caught up." A
  full native-beige restyle of every section is a much larger, drift-prone
  effort (two renderers diverging) and is NOT recommended for parity. The
  native touches stay the tab bar + the player-row reads below.

### Track 3 — Contextual injection at every interaction point

Beyond the log column, weave CatanBot's read into colonist's own UI wherever
the player acts, each piece SIZED TO FIT its spot (Noah: "make sure it all
fits in each overlay part, that's very important"):

- **Trade accept/decline prompt** -> inject CatanBot's trade evaluation right
  at the incoming-trade UI (is this a good trade for you, net resource value,
  whether it helps an opponent's known plan), from the snapshot's
  trade/incoming_trade fields.
- **Robber / 7-roll** -> the robber-target read at the robber-placement UI.
- **Discard prompt** -> the discard recommendation at the discard UI.
- **Dev-card / build bar** -> the relevant hint where the action is.

Each is its own small injected overlay: recon the colonist element, anchor to
it (resilient selector + re-anchor), render the matching snapshot slice, fit
it inside that element's bounds, streamer-stamp, skip-on-miss. This is the
"inject into every single actual use thing" goal. Build them one interaction
at a time; each needs a live DOM recon of that colonist element.

### Track 2 — Native per-player reads ("next to each player")

Inject CatanBot's live hand read next to each player in colonist's own player
panel (the Peony / Webber / Hett rows that already show dev/knight counts).

- **R0 recon:** capture colonist's player-panel DOM live (the rows + how each
  maps to a username/color). Prior research says colonist exposes
  `[data-player-information-container]`; confirm with a live capture command.
- For each player row, append a compact chip strip from `/advisor`
  `opps[].hand_probs` (the "wood 2 +67%" read), color-matched to the player;
  self row from `self.hand`.
- Update on the same 1s poll (via the same `get-advisor` message, parsed in
  loghud). Streamer-stamp the injected nodes. Re-inject after React re-renders
  (reuse the `ensureHudAttached` pattern).
- Fail-safe: if the player panel can't be located, skip silently (never break
  colonist's UI).

### Track 4 — In-page settings + interactive menu (use the blank space)

Noah: "make a full interactive menu inside the site itself." Put CatanBot's
settings in-page, in the empty blue margin (where ads were) or a left bar:

- All the side-panel settings, in-page: streamer mode (hide usernames), pause
  banners/recs, in-page overlay toggle, log-HUD on/off + replace mode, VP
  target / discard limit, the keyboard shortcuts. Wire them to the same flags
  the panel uses (localStorage `cataan.streamer`, `catanbot.log_hud`,
  `catanbot.overlay`, etc.) so they stay in sync.
- Placement: a small gear that opens a menu, or a thin vertical bar pinned in
  the left/blank margin. Noah left placement to "whatever you think" — use the
  dead blue space, keep it out of the board's way.

### Cleaner hand reads (shipped first)

Per Noah, the raw "+49% / 49%" was noise. New format: CONFIRMED cards first
(icon + count), a separator, then only the LIKELY-but-unsure resources as dim
icons (no count, no percent). Applies wherever reads show (log HUD, player
rows, future contextual panels).

### Track 3 additions (latest notes)

- **Trades:** when CatanBot says "offer a trade to rebalance your hand," also
  give an **"if they deny, do this instead"** fallback line. Inject the trade
  eval at the accept/decline UI.
- **Robber:** highlight the recommended tile on the board (or a popup over it),
  or a custom CatanBot notification panel. Use whatever reads cleanest.

## Phases (each shippable + testable)

- **R0** — live recon of the player panel (one capture command) -> selectors
  + the player-row -> username/color match strategy.
- **P6** — `web_accessible_resources` entry + iframe `panel.html` into the
  CatanBot tab. Verify every section renders, data flows through the worker,
  and the iframe sizes/scrolls in the column. Log tab + replace mode still
  work.
- **P7** — player-row read injection (Track 2): inject, update on poll,
  streamer-safe, re-anchored, skip-on-miss.
- **P8** — polish: iframe sizing to the column width, scroll, slim-render
  fallback on iframe error, and consolidate to ONE bridge poll (don't let the
  iframe panel AND a loghud poll both hammer the bridge).
- **P9** — tests: contract guards (iframe src uses runtime.getURL, the
  web_accessible_resources entry exists, the player-row injection contract),
  recon fixtures for the player panel, version bump + CHANGELOG.

## Risks + mitigations

- `web_accessible_resources` makes `panel.html` loadable from colonist.io.
  It's already a local-only UI with no secrets; acceptable, but scope the
  match to `https://colonist.io/*` only.
- iframe sizing/scroll in a narrow column — give it an explicit
  height (fill the feed column) and let panel.css scroll internally.
- player-row selectors rot — recon + a structural fallback + skip-on-miss.
- double bridge load — the iframe's panel.js polls AND loghud polls for the
  player reads. Consolidate: have loghud reuse the snapshot it already fetches
  for `get-advisor` and not double-poll; or gate the iframe's poll. Profile.

## Constraints (house rules)

Extension only, never `legacy/userscript/`. No em-dashes. Commit as we go with
human messages, Noah sole author, no Claude attribution. Never commit
`/Users/noah` paths, Apple/Team IDs, or secrets to the public repo. Keep all
tests green (version-sync, JS `node --check`, pytest).

## Open decision

Iframe-the-panel (RECOMMENDED: instant full parity, auto-synced, CatanBot's
own styling) vs a native-beige restyle of every section (much larger, two
renderers drift). Default: iframe.
