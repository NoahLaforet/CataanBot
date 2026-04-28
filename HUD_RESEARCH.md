# HUD Research — what to steal from prior art

Source survey for task #137 (HUD redesign in a visually different
direction). Synthesis is at the bottom — the rest is the case-by-case
notes that backed it.

## Catan-specific tools

Catan's overlay scene is small and almost entirely about **resource
counting**, not advice.

* **Colonist IQ** (Chrome) and **Colonist Resource Tracker** parse the
  in-game chat log and surface inferred opp hands. No recommendations,
  no win-prob. A robber icon next to a player whose hand contents got
  scrambled.
* **colonist-enhancer** (Firefox, unmaintained) injects a side menu with
  the same parsed-log info.
* **Catan Board Analyzer**, **CSSettlers**, **CatanCalculator** are all
  pre-game tools — feed them a board picture / config and they
  calculate dot-pip totals or settlement scores. Static images, not
  overlays.
* **Catanatron web UI** (the project we already use as our engine) is a
  bare board + action list. No analysis surfacing.

**Takeaway:** the existing Colonist tooling is _resource trackers_.
There is no real "advisor that tells you what to do" overlay in the
Catan scene. CataanBot is unique in trying to render `recommend` /
winning-move output live. There's no Catan advisor UI to crib from
directly — the prior art is in adjacent scenes.

## Chess engines (lichess / chess.com)

Closest analogue for "AI advisor on a live game." The genre is mature
and the UI patterns are well-converged.

* **Eval bar** is the dominant visual. Vertical bar, single float
  number, white-vs-black share. One glance answers "who's winning."
* **Best move arrows** drawn directly on the board.
* **Top-N variations panel** (top 3 lines, score for each) sits to the
  right.
* **Move-quality classification** post-hoc: `??` blunder, `?` mistake,
  `?!` inaccuracy, `!` good, `!!` brilliant. Tiny inline icons next to
  moves.
* **Eval graph** along the bottom — game progression, who was ahead
  when. Clickable to jump to that move.
* **Game phase tag** ("opening / middlegame / endgame") in the top bar.

**Takeaway:** ONE dominant evaluation visual that says who's winning,
plus the move recommendation as ONE highlighted action. Everything
else is layers underneath, expandable on demand.

## Hearthstone Deck Tracker (HDT)

Closest tech analogue: in-game overlay on a live video game, parsed
from a log stream. (Same architectural shape as CataanBot.)

* **Two main panels**: your deck (cards remaining, draw probabilities)
  and opp's hand (cards seen, where they came from).
* **Iconic markers** on cards: coin, mulligan'd, returned, created.
  No words, all icons.
* **Independent draggable panels** — every element repositionable, with
  a "lock layout" toggle.
* **Probability numbers** prominent (e.g. "32% to draw lethal").
* **Per-element on/off** so users can run a minimal HUD if they want.

**Takeaway:** symbolic vocabulary > text. If there's a recurring state
(monopoly risk, near-discard, can-buy), it should have a single icon
that means it, not a sentence.

## Poker HUDs (PokerTracker, DriveHUD, Hand2Note)

Per-player stat overlay on a live game.

* **Tiny dense stat boxes** — 4-6 numbers per opp, no labels (you learn
  what `VPIP / PFR / AGG / 3B / WTSD` mean).
* **Color gradient on numbers** — same number means different things
  per stat (high VPIP = red loose, high PFR = green tight-aggro).
* **Hover for popup** with full stat detail and ranges.
* **Effective stack auto-coloring** — turns red when below 10BB
  (push/fold range).

**Takeaway:** numbers in cells with semantic color do enormous work.
Don't write "8 cards (at risk)" — write `8` in red. The color IS the
warning. We do this in pieces (VP shading, monopoly banner) but
inconsistently.

## Catan analytics (Duddhawork blog)

Data-driven what-actually-wins lessons that feed the recommender, not
the UI per se — but worth keeping near at hand.

* Average game = ~64 rolls, ~16 turns per player → ~10 meaningful
  decisions per player. The HUD has a small budget of "important
  moments" to flag.
* Wheat is king. Lumber and clay are the resources you can survive
  short on. Recommender should weight wheat-blocking and wheat-targeted
  monopolies higher.
* 20+ starting dot pips → meaningfully higher win rate. Make the
  opening-pick scoring's pip total VERY visible (it currently is, but
  nothing else on the screen is sized by importance).
* Cities and dev cards correlate with winning; lumber/clay accumulation
  doesn't. Suggests reweighting the `production_per_roll` display by
  resource importance.

## Synthesis — design principles for v0.24 HUD

Ordered by how much they'd change the user experience, biggest first.

### 1. One dominant "current verdict" element

The chess eval-bar lesson. Currently the recommendation list is a
sequence of equal-weight rows. Instead: one big slot at the top that
says **"the bot's read of the position right now"** — could be the
winning-move, the top recommendation, or a phase-specific guidance
("you're at 5 VP behind a 7-VP leader, push city now"). Everything
else gets visually demoted.

### 2. Symbolic state vocabulary

A small alphabet of icons for recurring HUD states, used consistently:

| state | icon | currently shown as |
| --- | --- | --- |
| monopoly risk | 🚨 | text banner |
| near-discard (≥8 cards) | 🎲 | text + bold |
| winning move | 👑 | green box |
| can-afford new build | ✅ | text "→ X" |
| 1-card-short | ⏳ | text "→ X from Y" |
| robber on me | 🚫 | banner |
| longest road threat | 🛣️ | banner |
| largest army threat | ⚔️ | banner |

Unify these so the HUD has 8-10 reusable visual tokens, not 30 unique
text banners. Train the eye once.

### 3. Color-as-warning, not text-as-warning

Adopt poker-HUD palette:

* numbers always have a heat color: red (danger), amber (watch), green
  (good), grey (neutral).
* opp `cards-in-hand` = red at ≥8, amber at 6-7, grey at ≤5. No
  "fat-hand" word marker — the color IS the marker.
* opp `vp` = red at ≥(VP_TARGET - 2), amber at mid-late threshold,
  grey otherwise. Already mostly there; make it the rule.
* self resources display where the resource you're short of for your
  next build flashes amber.

### 4. Phase-aware default layout

Show different rows in different game phases — chess-style:

* **Opening (rounds 1-2)**: opening picks dominate. Recommender list
  hidden. Production/standings irrelevant.
* **Early (rounds 3-8)**: production + closest-build + recommender
  list. Standings collapsed.
* **Mid-late (rounds 9+)**: standings + winning-move + threats. Hand
  detail collapses.

The HUD currently shows everything always. Phase-aware hiding cuts
visible content by ~half in any single phase.

### 5. Hover-to-expand, click-to-pin (chess.com pattern)

Most rows should be 1 line dense. Hover shows the detail (currently
inline as `· detail`); click pins it open. The HUD currently shows
ALL detail ALL the time, which is the "wall of text" problem.

### 6. Eval-graph for postmortem

After-game (in `postmortems/`): a small VP-over-time chart per
player. Already shipped per game as HTML; consider also mounting it
LIVE in the HUD as a sidebar after round 5 — gives "the arc of the
game" at a glance, lets Noah see when momentum shifted.

### 7. Move-quality annotation (chess `?!` / `!!`)

Post-hoc, annotate Noah's moves vs the recommender's top pick:

* `!!` = picked the search-rerank top move
* `!` = picked one of the top 3
* `?!` = picked a positive-EV move that wasn't the best
* `?` = picked a move the bot rated zero-or-negative
* `??` = picked a move it actively flagged as a blunder

Show inline in the postmortem and as a running tally in the HUD ("12
moves: 3 !!, 5 !, 4 ?!"). This is the chess.com feature people stay
for; it would feed task #122 (missed-rec patterns) directly.

## Implementation order

If we tackle #137, recommend this sequence:

1. **#3 first (color-as-warning)** — smallest visual change, biggest
   density reduction. Removes 5-6 text banners outright.
2. **#2 (symbolic vocabulary)** — second smallest, locks in the new
   visual language so #1 has consistent codomain.
3. **#1 (dominant verdict)** — bigger structural change, needs the
   first two for the rest of the HUD to demote cleanly.
4. **#4 (phase-aware)** — depends on #1; the demotion from #1 makes
   conditional hiding cheap.
5. **#5 (hover-to-expand)** — interaction polish; punt unless #1-#4
   leave the HUD feeling "right."
6. **#6 (live eval graph)** + **#7 (move quality)** — separate,
   bigger projects; reasonable as their own tasks, not part of #137.

## Sources

* [catanatron](https://github.com/bcollazo/catanatron)
* [colonist-enhancer](https://github.com/movcmpret/colonist-enhancer)
* [Colonist IQ](https://chromewebstore.google.com/detail/colonist-iq/ebljmhhfeffkimlfkmhjjahcicmdlcaj)
* [Catan Board Analyzer](https://catan-analyzer.netlify.app/)
* [Catan analytics blog](https://duddhawork.com/blog/catan-analytics-how-to-win-with-data-driven-strategies/)
* [Hearthstone Deck Tracker overlay wiki](https://github.com/HearthSim/Hearthstone-Deck-Tracker/wiki/Overlay)
* [chee — chess analysis overlay](https://github.com/hong4rc/chee)
* [eval.bar — chess analysis tool](https://github.com/goodvibs/eval.bar)
* [PokerTracker basic HUD guide](https://www.pokertracker.com/guides/PT4/hud/basic-hud-guide)
