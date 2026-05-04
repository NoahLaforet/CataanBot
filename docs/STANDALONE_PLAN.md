# Standalone extension — JS-side recommender

## Decision

The public Chrome Web Store extension must work with **zero local
install**. No Python bridge, no installer, no terminal. A user
clicks "Add to Chrome," opens colonist.io, and the HUD works.

Noah's existing Python bridge stays in the repo and stays
maintained — it powers his personal training, tracking, and
advanced postmortem work. The extension auto-detects whether the
bridge is reachable on `127.0.0.1:8765`:

- **No bridge present (default for store users):** runs the
  built-in JS recommender. Covers the core HUD.
- **Bridge present (Noah + power users):** feature panels
  light up — strategy archetype tracker, mid-game pivot
  triggers, full postmortems, advanced trade evaluation,
  move-quality grading.

This is a **graceful upgrade**, not a downgrade — the bridge is
unambiguously a "more features" option, not a required dependency.

## What the JS recommender needs to do (MVP scope)

| Feature | JS port? | Effort | Notes |
|---------|----------|--------|-------|
| Hand tracking from WS frames | yes | small | port `hand_tracker.py` (~300 lines) |
| VP / production tracking | yes | small | already partially in panel.js |
| Opening picks ranking | yes | medium | port `advisor.score_opening_nodes` (~150 lines) |
| 2nd settlement complement-aware | yes | medium | port `advisor.score_second_settlements` (~120 lines) |
| In-game rec list (settle/city/road/dev) | yes | medium | port `recommender.recommend_actions` simple paths (~400 lines) |
| Robber target ranking | yes | small | port `advisor.score_robber_targets` (~150 lines) |
| Dev card play hints | yes | small | each is ~80 lines of straightforward heuristic |
| Roll histogram + recent rolls | yes | trivial | already JS-side |
| Bank/port trade unlocks | yes | medium | port `_plan_bank_trades` (~80 lines) |
| Player-trade proposals | yes | medium | port `propose_trade` block (~150 lines) |
| Move quality grading | partial | medium | needs comparison-against-recs path |
| **Strategy archetype tracker** | **bridge-only** | — | too much state; bridge feature |
| **Mid-game pivot triggers** | **bridge-only** | — | depends on strategy tracker |
| **Auto-postmortem HTML** | **bridge-only** | — | matplotlib + Python templating |
| **1-ply state-eval search rerank** | **bridge-only** | — | needs catanatron Game.copy() |

Total JS port estimate: ~2000-2500 lines across maybe 8 files.
Heuristic-only — no ML, no external deps beyond what the
extension already loads.

## Directory layout

```
extension/
├── manifest.json
├── background.js          ← service worker, MV3
├── content.js             ← DOM observer + WS forwarder
├── inject.js              ← page-world WS hook
├── panel.js               ← HUD render
├── panel.css
└── lib/                   ← NEW: standalone JS recommender
    ├── board.js           ← parse colonist mapState → graph
    ├── state.js           ← Game state container + mutations
    ├── events.js          ← apply WS events to state
    ├── advisor.js         ← opening picks, robber, port helpers
    ├── recommender.js     ← in-game action recs
    ├── hints.js           ← dev card play hints
    └── trades.js          ← bank/port + propose-trade logic
```

content.js / panel.js shift from "forward to bridge" to
"maintain JS state, render directly." If `state.bridgeReachable
=== true` (probe at startup), additional panels are appended.

## Phasing

### Phase 0 — design + scaffolding (today)
- This doc. Decision documented.
- Stub `extension/lib/` with module skeletons.
- Add a "bridge probe" helper that extension uses to detect
  presence at boot.

### Phase 1 — board + state model
- Port `board.js`: parse colonist's `mapState` into a node
  graph (land_nodes, neighbors, ports, adjacent_tiles per
  node). Same shape as catanatron's `CatanMap` but tighter.
- Port `state.js`: Game state container holding buildings,
  roads, robber, hands, played_knights, vp_per_color.
- Port `events.js`: take a parsed WS event, mutate state.
  This is the analog of catanatron's tracker.

### Phase 2 — opening recommender
- Port `score_opening_nodes` + `score_second_settlements`.
- Wire into panel: when in opening phase + no bridge,
  render JS-side picks.
- Smoke test against the same captures the Python tests use.

### Phase 3 — in-game recs
- Port `recommend_actions` minus the 1-ply search rerank
  (that stays bridge-only). Simple kind ranking + bank
  trade unlock + propose-trade.
- Port the per-kind score helpers.
- Wire into panel.

### Phase 4 — dev card hints + robber targets
- Port `_compute_knight_hint`, `_compute_monopoly_hint`,
  `_compute_yop_hint`, `_compute_rb_hint`.
- Port `score_robber_targets`.

### Phase 5 — trade evaluation
- Port `evaluate_incoming_trade`.
- Wire into the existing trade-offer banner.

### Phase 6 — bridge upgrade panels
- Detect bridge presence at boot via `fetch /advisor` probe.
- When present, render the existing strategy banner +
  ranking + pivot triggers + game-plan + advanced postmortem
  link.
- When absent, those panels stay hidden — no broken UX.

## Open questions / risks

- **catanatron's mapState parsing.** colonist ships their own
  mapState format which catanatron's `colonist_map.py`
  already parses on the bridge. We need to mirror that
  parser in JS. Could be 100-200 lines depending on how
  many variant maps we need to handle for v1.
- **Distance rule + longest-road computation.** Both are
  graph algorithms. Distance is trivial (BFS to depth 2).
  Longest road is harder (longest path on a multigraph) —
  catanatron does it iteratively; we'd port that.
- **Move quality grading.** Currently compares Noah's actual
  build against the bot's top picks at decision time.
  Without the full recommender we can still grade against
  the JS-side recommender's picks. Acceptable downgrade.
- **Test infrastructure.** Python tests don't help here.
  Need Jest or similar for the JS modules. Plus golden-file
  tests against captured WS sessions.

## Bridge-only features stay valuable

For Noah personally and for the small group who will install
the bridge, keeping the bridge means:

- Continuing all the strategy v2 work (archetype tracker,
  pivot triggers, port-pip alignment, etc.) — these stay
  active and surface as the "advanced" panel set.
- Postmortems with full chart rendering (VP timeline, dice
  fairness, hand size over time, cumulative production).
- Move quality with the 1-ply search delta.
- Training data collection (autosave files, postmortems
  for replay analysis).
- Future: strategy-engine improvements that are too heavy
  for in-extension JS land here first.

The bridge becomes the "lab" where new heuristics get
prototyped; once stable enough, port to JS for the public
extension.

## Timeline

Realistic: 1-2 weeks of focused work to get phases 0-4 in a
shippable state. Phases 5-6 add another few days. Doesn't
block the current CWS submission — the unlisted v0.35.0 ships
with the bridge dependency clearly stated, then we update the
extension once the standalone build is ready.
