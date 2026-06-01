# Extension ↔ Bridge parity tracker

Live audit of every snap surface the bridge ships vs. what the
JS recommender (extension-only / standalone path) populates. Pull
this open when picking up cross-context — line items here
describe the *target* (bridge behaviour from `src/catanbot/`) and
the *current state* of the standalone pipeline.

Last refresh: 2026-05-05 07:30 UTC · extension v0.37.35

## Core game state

| field | bridge | extension | notes |
|---|---|---|---|
| `seq` | int (++ on every state change) | `-2` constant | not load-bearing — auto path always re-renders on bridge fail |
| `game_started` | bool | bool | latched on tileHexStates seen |
| `setup_phase` | bool | bool | gates "→ opening picks" header |
| `game_progress` | `{phase, round, total_rolls}` | `{phase, round, total_rolls}` | round=0 in standalone |
| `game_settings` | dict | n/a | not consumed by renderer |
| `variant` | str | n/a (classic only) | variant boards build, but flag not surfaced |
| `variant_recs_disabled` | bool | n/a | always classic-mode |
| `last_roll` | `{total, d1, d2, isYou, ts}` | same | live |
| `roll_history` | array | array | capped at 200 |
| `total_rolls` | int | int | gated on ≥1 building (skip play-order dice) |
| `roll_histogram` | `{2..12: count}` | same | live |
| `vp_target` | int | int | from `gameSettings.victoryPointsToWin` |
| `discard_limit` | int | int | from `gameSettings.cardDiscardLimit` |
| `friendly_robber_active` | bool | bool | latched via chat "friendly robber" |
| `current_turn_username` | str | str | RGB-distance match to chat names |
| `current_turn_color` | str | str | catanatron-shaped (RED/BLUE/...) |
| `my_turn` | bool | bool | currentTurnPlayerColor === selfColorId |

## Player blocks

| field | bridge | extension | notes |
|---|---|---|---|
| `self.username` | str | str | from chat scraper |
| `self.color` | str | str | catanatron-shaped |
| `self.color_css` | hex | hex | from chat or fallback |
| `self.cards` | int (total) | int | sum of typed |
| `self.hand` | `{res: int}` | `{res: int}` | typed (authoritative for self) |
| `self.afford` | `[name, ...]` | same | what's currently buildable |
| `self.next_build` | `{build, missing, gap}` | same | nearest-miss target |
| `self.vp` | int | int | from victoryPointsState |
| `self.vp_breakdown` | `{settle, city, vp_cards, longest_road, largest_army, total}` | same | computed from buildings + flags |
| `self.knights_played` | int | int | from mechanicKnightState (or 0) |
| `self.production` | `{per_roll, top_resource, per_resource}` | same | live |
| `self.ports` | `[name|GENERIC, ...]` | same | from owned settle/city ports |
| `self.monopoly_risk` | `{resource, count}` | same | flags 6+ stack when opp holds dev |
| `self.pieces` | `{settles, cities, roads}` | same | from bank-remaining |
| `self.dev_cards` | `{KNIGHT, MONOPOLY, ..., VP}` | same | typed for self |
| `opp.username/color/color_css` | same | same | matched from chat |
| `opp.cards` | int (authoritative WS total) | int (WS total) | reliable |
| `opp.hand` | `{res: int}` (inferred + drift) | `{res: int}` (chat-inferred only) | bridge has hand_tracker drift; standalone is best-effort |
| `opp.unknown` | int (3rd-party-steal residue) | int (rough) | standalone tracks via WS-total minus inferred sum |
| `opp.hand_tracked` | bool | bool | true when |inferred − WS| ≤ 1 |
| `opp.card_delta` / `card_delta_window` | int / int | int / int | live (5-roll ring buffer in events.js) |
| `opp.vp` | int | int | live |
| `opp.dev_cards` | int (total) | int (total) | live |
| `opp.dev_stash_risk` | bool | bool | live (`dev≥2 AND vp+dev≥target-1`) |
| `opp.knights_played` | int | int | live |
| `opp.pieces` | `{settles, cities, roads}` | same | live |
| `opp.prod` | float (per-roll) | float | live |
| `opp.ports` | array | array | live (walks opp's settles/cities for `node.port`) |
| `opps[]` order | seat order | sorted by colonist color id | matches colonist UI |

## Recommendations

| field | bridge | extension | notes |
|---|---|---|---|
| `recommendations[]` (opening) | `kind=opening_settlement` with `{tiles, port, road, plan?}` | same shape, **road field NOT yet shipped** | bridge attaches `_best_opening_road` per pick; standalone needs port |
| `recommendations[]` (mid) | top-4 from `recommend_actions` (kind=settlement/city/road/dev_card/bank_trade/trade/propose_trade) | top from `recommendActions` (city/settlement/road/bank_trade/dev_card) | **propose_trade not ported**; **search_delta not ported** |
| rec `score` | 1-10 | 1-10 | calibrations match |
| rec `when` | now/soon | now/soon | live |
| rec `search_delta` | 1-ply EV (catanatron Game.copy) | n/a | bridge-only by design |
| rec `tiles` | `[(res, num), ...]` | same | live |
| rec `missing` | `{res: count}` | same | live |
| rec `give/get` (trades) | dict pairs | dict pairs (bank_trade only) | player-trade proposals not ported |
| rec `road` (opening_settlement) | `{edge_tiles, sealed?, contested?}` | **MISSING** | needs `_bestOpeningRoad` port |
| rec `plan` (opening, round-1) | `{second, archetype}` | n/a | bridge predicts 2nd settle; standalone uses scoreSecondSettlements at round-2 only |

## Banners + verdicts

| field | bridge | extension | notes |
|---|---|---|---|
| `strategy` | `{active, primary, fallback, rationale, phase, scores, ranking, pivot_triggers, override_tag}` | same | five archetypes + 4 pivot triggers |
| `knight_hint` | `{have, should_play, reason, played, has_la, robber_on_us}` | same | PLAY/HOLD verdict |
| `monopoly_hint` | `{have, target_resource, est_total, should_play, reason, unlock}` | same | PLAY/HOLD with resource pick |
| `yop_hint` | `{have, take, should_play, reason, target_kind}` | same | PLAY/HOLD with pair pick |
| `rb_hint` | `{have, edges, should_play, reason, self_len, opp_len}` | same | PLAY/HOLD with edge pair |
| `winning_move` | `{message, confidence, kind, alternatives}` | same | confidence=high only |
| `threat` | `{level, leader_color, leader_username, leader_vp, message}` | same | live |
| `win_proximity` | `{level, self_vp, target, gap, message}` | same | live |
| `engine_deficit` | `{leader_username, leader_per_roll, self_per_roll, ratio}` | same | live |
| `robber_on_me` | `{tile_id, resource, number, buildings, has_city, rolls_recent, blocks_recent}` | same | live |
| `discard_hint` | `{need, drop, rationale}` | same | live |
| `seven_prep` | `{level, cards, limit, message, would_drop?}` | same | live — would_drop preview now shipped |
| `hot_numbers` | `[{number, count}, ...]` | same | top-2 |
| `sevens_hot` | `{count, window, message}` | same | live |
| `production_stall` | per-opp dry-streak | n/a | requires per-roll hand-history tracking |
| `milestone` | `{kind=third_settle, headline, missing}` | same | gated on 2 settles + no cities + ≥5 rolls |
| `standings` | `{leader, rows, self_vp, self_is_leader, gap_to_leader}` | same | live |
| `longest_road_race` | `{level, message}` | same | contested/safe/behind/open |
| `largest_army_race` | `{level, message}` | same | live |
| `incoming_trade` | `{offerer, color, color_css, vp, give, want, verdict, reason, counter?}` | same minus counter | basic ACCEPT/DECLINE/CONSIDER |
| `bank_supply` | per-resource deck remaining | n/a | not surfaced in renderer |
| `dev_deck.by_type` | `{KNIGHT.remaining, ...}` | all 5 types | knights via mechanicKnightState; mono / yop / rb via chat parser; vp from self typed counts |

## Bridge-only by design (skip)

- `eval_history` — per-roll catanatron evaluator output. Catanatron-tier;
  no JS equivalent.
- `latest_postmortem` — full HTML postmortem at /postmortem. Bridge writes
  to disk; standalone has no file system.
- `strategic_options` — multi-archetype alt-plan ranking
  (`_compute_strategic_options`). Heavy lookahead.

Now ported (v0.37.35):
- `move_history` — chess-style !! / ! / ?! / ? / ?? grading vs the rec
  list cached the previous tick.
- `game_plan` — slim heuristic version surfacing the top-priority
  near-term goal + one-line summary.

## Outstanding gaps to close

Only catanatron-tier lookahead is left:

1. **eval_history** — per-roll catanatron evaluator output. No JS
   equivalent without a JS port of catanatron.
2. **strategic_options** — multi-archetype alt-plan ranking. Heavy
   lookahead; the flat rec list + game_plan banner cover the live
   HUD use case.
3. **latest_postmortem** — bridge writes HTML to disk; standalone
   has no filesystem.

## Standalone recommender divergences (2026-05-31 audit)

The bridge is the source of truth; these are places the standalone JS
recommender scored or ranked a move differently. Closing one means the
no-bridge score badge and #1 pick match what the bridge would show.

Closed in v0.39.0:
- Settlement display score and ranking now use wheat-weighted production
  with no diversity multiplier, matching recommender.py
  `_score_settlement(_node_pip_production_weighted)`. The old JS baked a
  1.0 / 1.08 / 1.22 diversity factor into the score and dropped the
  wheat weight, so the same corner showed a different score (and
  sometimes a different #1 pick), which also skewed move-quality grading.

Open (want a live bridge-vs-standalone comparison to tune safely):
- Third-settle (x1.25 at 2 footprints), endgame (+1.5/+2.5 past
  close-to-win), and per-archetype score bumps are applied by the bridge
  in `recommend_actions` but not by `recommendActions`, so the rec order
  can differ in those phases.
- `_bestLanding` halves a road landing's production whenever any
  neighbor is built (including the near anchor), where the bridge
  excludes blocked landings instead, so standalone road scores run low.
- `strategy.js` PORT_TRADE near-port thresholds. DONE (v0.40.0): the
  post-placement near branch now matches `strategy_select.py` (produced-
  resource gate 0.20, near score 0.85, no 3:1-near credit). The
  JS-only pre-placement board-affinity branch is left as is.
- `_proposeTradeRecs` resource reservation. DONE (v0.40.0): it now
  computes reservedAcross over near-term builds (<= 2 cards short) and
  only offers a surplus held beyond that, matching the bridge's
  reserved_across so it won't trade away a card another blocked build
  needs.
- `_bankTradeRecs` uses fixed base scores rather than the unlocked
  build's production curve minus one.
- The standalone knight robber window relies on the chat-log "used
  Knight" line; a WS-frame backstop (a mechanicKnightState increment)
  would make it fire even when that chat line is missed.
- `events.js` `_findKey` runs about 17 independent tree walks per WS
  frame; a single-pass key collector would cut the per-frame cost
  (behavior-identical refactor).

Closed in v0.37.35:
- ~~move_history~~ — chess-style grading via `lib/move_quality.js`
  + per-tick rec cache.
- ~~bank_supply~~ — per-resource Catan deck remaining.
- ~~production_stall~~ — self-side dry-streak detection.
- ~~game_plan banner~~ — slim heuristic of `_compute_game_plan`.
- ~~knight `robber_reason`~~ — chat-detected self knight play
  fires the urgent banner.
- ~~dev_cards_just_bought~~ — chat-detected dev buy blocks
  same-turn play of that card.

Closed in v0.37.34:
- ~~opening_settlement `road` field~~ (v0.37.32 — `_best_opening_road`
  ported to advisor.js)
- ~~propose_trade recs~~ (v0.37.33 — `_propose_trades` ported to
  recommender.js)
- ~~opp_card_delta~~ (v0.37.34 — 5-roll ring buffer in events.js)
- ~~dev_deck per-type for non-knight~~ (v0.37.34 — chat parser for
  monopoly / yop / rb plays)
- ~~seven_prep `would_drop`~~ (v0.37.34)
- ~~opp.dev_stash_risk + opp.ports~~ (v0.37.34)
- ~~opening-rec routing bug~~ (v0.37.34 — building/road-counted
  setup_phase)
- ~~trade-modal stuck~~ (v0.37.34 — WS-driven `tradeState` lifecycle)
- ~~robber recs intermittent~~ (v0.37.34 — robberPending lifecycle)
