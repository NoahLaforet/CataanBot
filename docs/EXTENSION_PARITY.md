# Extension ↔ Bridge parity tracker

Live audit of every snap surface the bridge ships vs. what the
JS recommender (extension-only / standalone path) populates. Pull
this open when picking up cross-context — line items here
describe the *target* (bridge behaviour from `src/catanbot/`) and
the *current state* of the standalone pipeline.

Last refresh: 2026-05-04 22:00 UTC · extension v0.37.31

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
| `opp.card_delta` / `card_delta_window` | int / int | n/a | bridge tracks 5-roll delta |
| `opp.vp` | int | int | live |
| `opp.dev_cards` | int (total) | int (total) | live |
| `opp.dev_stash_risk` | bool | n/a | not ported |
| `opp.knights_played` | int | int | live |
| `opp.pieces` | `{settles, cities, roads}` | same | live |
| `opp.prod` | float (per-roll) | float | live |
| `opp.ports` | array | n/a | not ported |
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
| `seven_prep` | `{level, cards, limit, message, would_drop?}` | partial — `would_drop` not shipped | bridge previews specific cards to drop |
| `hot_numbers` | `[{number, count}, ...]` | same | top-2 |
| `sevens_hot` | `{count, window, message}` | same | live |
| `production_stall` | per-opp dry-streak | n/a | requires per-roll hand-history tracking |
| `milestone` | `{kind=third_settle, headline, missing}` | same | gated on 2 settles + no cities + ≥5 rolls |
| `standings` | `{leader, rows, self_vp, self_is_leader, gap_to_leader}` | same | live |
| `longest_road_race` | `{level, message}` | same | contested/safe/behind/open |
| `largest_army_race` | `{level, message}` | same | live |
| `incoming_trade` | `{offerer, color, color_css, vp, give, want, verdict, reason, counter?}` | same minus counter | basic ACCEPT/DECLINE/CONSIDER |
| `bank_supply` | per-resource deck remaining | n/a | not surfaced in renderer |
| `dev_deck.by_type` | `{KNIGHT.remaining, ...}` | KNIGHT only | bridge tracks plays for all types via chat |

## Bridge-only by design (skip)

- `eval_history` — per-roll catanatron evaluator output
- `move_history` — chess-style !! / ! / ?! / ? / ?? grading vs bot top-10 at decision time
- `latest_postmortem` — full HTML postmortem at /postmortem
- `game_plan` — multi-step planning (bridge runs `_compute_game_plan` over catanatron state)
- `plan` — persistent multi-turn plan banner
- `strategic_options` — alt game plans (`_compute_strategic_options`)

## Outstanding gaps to close

1. **opening_settlement `road` field** — port `_best_opening_road` to advisor.js. Picks the best of 3 adjacent edges per opening pick by what landing it opens. Required for the "↳ road: between BR3 SHE10" sub-line under each opening pick. ← user's "road recommendations" complaint
2. **propose_trade recs** — port `_propose_trades` from recommender.py. When self short on a build target, suggest a port-bank trade OR a propose-trade to whichever opp produces the surplus. Bridge-only currently.
3. **opp_card_delta** — track per-opp card-total over a 5-roll window so the "+3 in 5" / "−2 in 5" annotations on opp rows can fire.
4. **dev_deck per-type for non-knight** — port the chat "X played Y" parsing so we know plays of monopoly / yop / rb (not just knights). Used by the dev-deck strip at the bottom of the panel.
5. **game_plan / plan / strategic_options** — multi-step lookahead. Lower priority; the flat rec list works without.
6. **production_stall** — per-opp roll-by-roll hand-history tracking.

Items 1-2 are the user-visible gaps named in the bridge. 3-6 are nice-to-have or bridge-only.
