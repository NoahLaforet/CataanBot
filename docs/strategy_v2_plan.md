# Strategy v2 plan — improvements from competitive-Catan feedback

Source: r/boardgames thread "I simulated 36,000 games of Catan…"
(u/Hot-Rooster1675, Apr 2026), with the highest-signal critique coming
from u/chalks777, a multi-time Catan World Tournament competitor whose
extended family regularly places top-20 worldwide.

The OP's findings are useful as **bias targets** (already captured in
prior tuning notes). This document is about the *agent-behavior critique*
in the comments — what competitive humans actually do that the heuristic
agents (and, by extension, our recommender) get wrong.

CatanBot is an *advisor* for one human seat, not a 4-bot simulator, but
every critique below maps onto a recommender heuristic we ship. The
question is just: when our HUD recommends a move, is it recommending
what a tournament player would do, or what a naive heuristic would?

---

## TL;DR — the five things that matter most

1. **The strategy isn't chosen on turn 1; it's chosen by the placements.**
   Pick the strategy *after* both opening settlements land, based on what
   was actually claimed.
2. **The strategy must keep adapting** — re-evaluate when the dice run
   hot on a number, when a development card lands that enables a pivot,
   or when an opponent visibly commits to LR/LA.
3. **Knight timing is the biggest single bot tell.** Don't burn the first
   knight unless the robber is on you and stealing something important.
   Holding knights is concealment, not laziness.
4. **The robber is a resource-control tool, not a VP-rank tool.** Steal
   what you need, set up your own monopoly, deny acceleration — not just
   "hit the leader."
5. **Port placement is a trap on random boards.** Only worth it with
   perfect tile alignment, or as a 4th-seat fallback. Settling *near* a
   relevant port and reaching it on settle #2 is the real port play.

Everything else is supporting detail.

---

## Tier P0 — ship before anything else

### 1. Strategy selection happens *after* opening placements

**Insight (chalks777, also MJamesRead, Razmodius33, Gaminkid05):** No
serious player decides "I'm an OWS player today" and then scores nodes
to fit. They score nodes for *general* quality, take the best two, and
*then* derive the strategy from what those two settlements actually
enable.

**Current behavior:** [`advisor.score_opening_nodes`][advisor.py L132]
weights nodes by a single composite score; the recommender exposes one
ranked list. There is no "and now what strategy do these two seats
support?" step before the bridge starts giving in-game advice.

**Proposed change:**
- Add a `strategy_after_placements()` pass that runs once both opening
  settlements are committed.
- Inputs: the two nodes' production map, port adjacency, expansion
  reachability, and the opponents' claimed nodes (so we know what's
  blocked).
- Outputs: a primary strategy tag (`OWS`, `LR_RUSH`, `PORT_TRADE`,
  `BALANCED`, `RB_CARVED_TILES`) and a secondary "fallback" tag.
- Feed that tag into the existing `bridge_strategy.py` weighting so
  recommendations downstream are coherent.

**Files:** `advisor.py`, `bridge_strategy.py`, new
`src/catanbot/strategy_select.py`.

**Complexity:** Medium. Most of the inputs already exist in the
recommender; this is a new selection layer, not a rewrite.

---

### 2. RoadBuilder, PortRusher, OreWheatSheep — fix the archetype definitions

**Insight (chalks777, comment ohmyywz):** The Reddit study's
implementations of these three archetypes are not how a competitive
player would play them. Our recommender currently echoes those same
naive definitions in places.

| Archetype | Naive (what we do) | Competitive (what we should do) |
|-----------|--------------------|----------------------------------|
| **RB** (RoadBuilder) | Roads-first, rush LR | Only viable when the placements *carve out* 4–5 tiles. Play normally for settlements/cities; use roads to *block* opponents inside your zone; rush LR in the **last 2 rounds**, not turn 5. |
| **PR** (PortRusher) | Settle on the port node | Settle on the **best tile** for the resource you'll trade; route roads so settle #2 is on the port (3:1 acceptable). Settling *on* the port is bad except in extreme layouts. |
| **OWS** | Buy dev cards, play knights | Buy dev cards for **flexibility and concealment**, not for knights. Pivot when you draw road-building or monopoly. Cities are a side-effect; appearing weak (no settlements visible from your hand) is the actual edge. |

**Current behavior:** [`advisor._port_bonus`][advisor.py L79] rewards
port-adjacent nodes with no requirement that the port resource match
the node's tiles strongly. [`recommender._opening_road_followup`][rec.py L401]
biases road #1 toward LR-style expansion regardless of layout.

**Proposed change:**
- `RB` archetype only fires when an "isolation score" is high (desert or
  weak-number neighborhood blocking opponents from a tile cluster).
- `PR` archetype splits into:
  - `PR_settle_on_port` — only with strong tile alignment + good numbers.
  - `PR_settle_near_port` — settle on tile, route road #1 toward port.
- `OWS` no longer surfaces "buy dev card → play knight" as the default
  next move; surfaces "buy dev card → hold for pivot" with a pivot
  trigger list (road-building drawn → switch to LR; monopoly drawn → use
  on the most-rolled wheat/ore in the last 5 turns).

**Files:** `advisor.py`, `recommender.py`, `bridge_strategy.py`.

**Complexity:** Medium. The `PR_settle_near_port` split is the largest
chunk — needs road-routing logic from settlement to port node.

---

### 3. Strategy adaptation triggers — re-evaluate mid-game

**Insight (chalks777 #1 in his prioritized list):** The single biggest
gap between heuristic agents and competitive players. A locked-in
strategy is a losing strategy.

**Triggers that should force a re-evaluation:**

| Trigger | Pivot |
|---------|-------|
| A single number rolls 4+ times in a 10-turn window and we're on it | Re-weight resource priorities; consider a wheat/ore monopoly setup |
| Road-building dev card drawn | Re-score whether `LR_RUSH` is now reachable in 2 turns |
| Monopoly drawn | Hold; flag the most-rolled resource of the last 5 turns as the trigger condition |
| An opponent crosses 6 VP | Tighten leader-aversion in trades; re-score robber for resource denial |
| An opponent plays their 2nd knight | Either commit to beating them to LA in 3 turns, or stop investing in dev cards |
| We're at 7 cards and the robber is overdue (no 7 in 10+ turns) | Surface "trade or build now to dodge a 7" |

**Current behavior:** No re-evaluation cadence. The advisor recomputes
every poll but the *strategic frame* is implicit and never named, so
there's nothing to pivot.

**Proposed change:**
- Add `bridge_strategy.detect_pivot_triggers(state, history)` returning
  a list of fired triggers per advisor poll.
- Each trigger maps to a strategy-tag override that flows into the same
  weighting layer added in (1).
- Surface fired triggers in the HUD as a one-line "why this changed"
  banner so the human user understands the pivot.

**Files:** `bridge_strategy.py`, `recommender.py`,
`extension/panel.js` (HUD banner).

**Complexity:** Medium-large. The history tracking is the new piece;
trigger detection itself is shallow logic.

---

## Tier P1 — high-impact follow-ups

### 4. Knight-timing rules

**Insight (chalks777 #2):** With one knight in hand, you almost never
play it proactively early/mid game. You play it *only* when the robber
is on you stealing an important resource (robber on a 2/3/11/12 can
stay there). At two knights you may play proactively. Late game, only
play immediately if there's a clear leader, a 2+ city stack on a
single number, or LA is contested.

**Current behavior:** [`bridge_robber.py`][bridge_robber.py] scores
knight plays based on production and VP rank. There's no "hold knight
unless conditions met" logic.

**Proposed change:**
- Add a `should_hold_knight()` function with the rule table above.
- HUD shows "hold knight — waiting for [trigger]" when applicable.
- Override only when: (a) we're racing for LA and ahead by 1 knight,
  (b) opponent is on 9+ VP and a knight steal denies a key resource,
  (c) the robber is on one of our 4–10 hexes.

**Files:** `bridge_robber.py`, `recommender.py`.

**Complexity:** Small. Mostly a guard layer in front of existing logic.

---

### 5. Robber as a resource-control tool

**Insight (chalks777 #3, Nucaranlaeg, Gaminkid05):** Current logic is
"highest production opponent + steal from VP leader." Real placement
considers:
- **Resource I need next turn** — steal from the player most likely to
  hold it.
- **Monopoly setup** — if I'm rich on wheat and one opponent feeds the
  table's wheat from a 10, lock the 10. My wheat trades become 1:1.
- **Acceleration denial** — an 8 that three opponents touch hurts more
  than a leader's 4 that only they touch.
- **Don't move it from a "stuck" placement** — if it's already on a
  high-roll tile that no one can shake, sometimes leaving it is better
  than moving it onto your own potential target.

**Current behavior:** [`advisor.score_robber_targets`][advisor.py L299]
weights by VP and production, no resource-need or monopoly model.

**Proposed change:**
- Extend `RobberScore` with three new components:
  - `resource_need_bonus` — based on what we owe for our next planned build.
  - `monopoly_setup_bonus` — based on `(my_share_of_resource - 1/N) × table_demand`.
  - `acceleration_denial_bonus` — sum of pip-share for each opponent on the hex.
- Tunable weights that sum to 1.0 with the existing VP-leader weight.

**Files:** `advisor.py` (RobberScore + scoring), `bridge_robber.py`.

**Complexity:** Medium. The monopoly-setup signal needs a "who can
produce this resource" view that probably already exists in
`hand_tracker.py`.

---

### 6. Player-trade volume and quality

**Insight (Nucaranlaeg, mxzf, chalks777):** Reddit's sim ran ~1.2
player-to-player trades per game; mxzf reports real-game tables run
8–15. Aggressive-trading tables score 11–13 VP in time-limited play vs
8–9 for trade-averse tables. Trading is *not* optional.

**Caveats:** chalks777 also notes that trading with the leader is
usually a mistake; the existing leader-aversion rule should stay.

**Current behavior:** [`recommender.evaluate_incoming_trade`][rec.py L1837]
exists. No proactive *outgoing* trade recommendations beyond bank/port.
The recommender doesn't model whether a 1:1 trade with a non-leader
opponent is good for both sides — and so doesn't suggest one.

**Proposed change:**
- Add `recommend_outgoing_trades(state)` that surveys our hand for
  surpluses (3+ of one resource we don't immediately need) and looks
  for opponents whose recent production thin-spots match.
- Filter: never propose a trade to a 7+ VP opponent unless our marginal
  benefit exceeds theirs by a tunable margin.
- HUD surfaces top 1–2 proposed outgoing trades with rationale.

**Files:** `recommender.py`, `bridge_economy.py`, possibly
`hand_tracker.py` for opponent-need modeling.

**Complexity:** Medium-large. Opponent-need modeling is the hard part;
the trade-suggestion surface is small.

---

### 7. Port-bonus penalty on random boards

**Insight (Reddit finding #7, chalks777):** Settling on a port with
weak tile alignment is a net-negative move on random boards (~10pp
win-rate hit in the sim). Humans skip bad ports; the heuristic doesn't.

**Current behavior:** [`advisor._port_bonus`][advisor.py L79] adds a
positive bonus for any port adjacency.

**Proposed change:**
- Require *both* of these for a positive port bonus:
  - The port-resource appears on at least one adjacent tile with pip ≥ 4.
  - OR we're picking 4th and the remaining nodes are all weaker than
    `pip_threshold` (configurable).
- Otherwise the bonus is 0 or negative.
- Consider a separate "port reachability" bonus for nodes 1–2 roads
  away from a relevant port — that's the *good* port play.

**Files:** `advisor.py`.

**Complexity:** Small. Mostly tightening a guard.

---

### 8. Hand management — proactive 7-dodge

**Insight (chalks777 #6):** Getting hit with a 7-discard is often worse
than proactively burning resources at 4:1 with the bank or 2:1 to a
worse-off opponent. Hand size > 7 with a 7 overdue is a yellow flag.

**Current behavior:** No 7-dodge logic.

**Proposed change:**
- Add `_seven_dodge_pressure(state)`: returns a 0–1 score based on
  hand size, turns since last 7, and number of opponents holding 7+ cards.
- When pressure > 0.6, surface a "consider trading down" recommendation
  even if the trade is mildly suboptimal.

**Files:** `recommender.py`, possibly a small new module
`src/catanbot/seven_pressure.py`.

**Complexity:** Small.

---

## Tier P2 — nice-to-haves once P0/P1 are stable

### 9. Largest Army as a *defensible* race, not a goal

**Insight (chalks777 #8 finding, Civil_Walker, psymunn):** LA takes a
minimum of 3 turns to claim from nothing (one knight per turn). Once
held, it's nearly impossible to steal because the holder can react with
one extra knight. Going for LA from behind is rarely correct; defending
LA you already have, or sniping when an opponent is at 2 visible
knights, is.

**Current behavior:** Existing `largest_army_race` field surfaces the
race generically.

**Proposed change:**
- Replace generic "push LA" with three states:
  - `LA_DEFEND` — we hold LA; recommend buying dev cards proactively if
    any opponent is at 2 knights.
  - `LA_SNIPE` — an opponent is one knight from LA; we have 1+ knights
    in hand and can race in 2 turns. Push hard.
  - `LA_PASS` — neither of the above. Don't recommend dev cards just
    for LA.

**Files:** `bridge_strategy.py`, `recommender.py`.

**Complexity:** Small.

---

### 10. Longest Road — late-game burst, not early commitment

**Insight (chalks777 #1 finding):** Even though LR wins 56–61% of
games, competitive players don't *invest* in roads early. They place
settlements with future LR potential and rush the last X roads in 1–2
turns to claim it. Investing road materials in turn 5 is usually wrong.

**Current behavior:** `_compute_longest_road_race` in
`bridge_strategy.py` likely surfaces LR pushes whenever we're within a
few roads.

**Proposed change:**
- Add a "LR setup vs LR commit" distinction.
- Setup phase (early/mid game): recommend road placements that *enable*
  a future LR but only when they double as expansion roads.
- Commit phase (late game, score ≥ 7 or opponent ≥ 8): recommend the
  full LR rush only if it can be completed in ≤ 2 turns from current
  resources + 1 expected production turn.

**Files:** `bridge_strategy.py`, `recommender.py`.

**Complexity:** Medium.

---

### 11. Opponent modeling for trade and port valuation

**Insight (Gaminkid05):** A port that trades a resource you have in
abundance is *less* valuable if opponents won't have access to that
resource — you can extract a 1:1 or 2:1 from them instead. Same logic
inverts trade margins generally.

**Current behavior:** Trade evals look only at our own marginal value
(`advisor._marginal_value`).

**Proposed change:**
- Track per-opponent production estimates (probably already in
  `hand_tracker.py`).
- For each resource, compute a "table scarcity" score = mean opponent
  production deficit.
- High-scarcity resources we hold extra of → don't take a port for
  trading them; use them for player trades instead.

**Files:** `advisor.py`, `hand_tracker.py`, `bridge_economy.py`.

**Complexity:** Medium.

---

### 12. Brick-port early pickup (existing tuning request)

This was already flagged separately as user-feedback: when settle is on
a high-pip brick tile and a 2:1 brick port is ≤ 2 roads away, surface
that road as the top recommendation. This is the *good* version of
port play (settle near, route to port) and slots into improvement #2
above.

---

## Sequencing recommendation

The first three items (P0 1–3) are tightly coupled and should land
together in a single push. Without strategy selection (1) the archetype
fixes (2) have no surface, and without adaptation triggers (3) the
selection from (1) decays as the game progresses.

A reasonable order:

1. P0-1 (strategy selection after placement) — establishes the
   strategy-tag plumbing the rest of the work plugs into.
2. P0-3 (adaptation triggers) — keeps the tag fresh.
3. P0-2 (archetype redefinitions) — uses the tag to drive better
   recommendations.
4. P1-4 (knight timing) — small, high-visibility win once strategy tags
   exist.
5. P1-7 (port-bonus tightening) — small, can ship in parallel.
6. P1-5 (robber as resource control) — medium effort, large impact.
7. P1-6 (player-trade volume) — largest single piece in P1; benefits
   from the opponent-modeling foundation needed for P2-11.
8. P1-8 (7-dodge), P2-9 (LA states), P2-10 (LR phases) — independent
   small wins, ship as time permits.
9. P2-11 (opponent modeling for trade/port) — last; builds on every
   prior piece.

---

## Out of scope (acknowledged but not actionable)

- **Social/charisma trading dynamics.** chalks777's "I drank a beer
  and won the embargoed semi-finals" anecdote is real and matters at the
  top of the field, but it's not modelable from a side-panel advisor.
- **Bluffing and information concealment as a primary axis.** OWS as
  "appear weak so opponents trade with you" is partially captured by
  the dev-card-as-pivot-tool reframing in P0-2, but the deception
  layer itself is a human skill we're not trying to coach.
- **The beginner board.** Tournament-relevant work assumes random
  boards; beginner-board tuning isn't a priority.

---

## How to validate each change

For every shipped item:

1. Run the existing thumbs-down corpus regression (no recs that were
   previously thumbs-down should now appear).
2. Spot-check 3–5 saved postmortems where the change would have
   applied; the new recommendation should make sense in hindsight.
3. Watch the next 5 live games and tag any rec that the user would
   not actually play. Use the thumbs-down stream as the durable
   feedback loop.

Per the project's feedback model: only thumbs-down is explicit. A
silent acceptance counts as a pass.
