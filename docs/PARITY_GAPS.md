# Standalone parity gap tracker

Generated from the v0.44.1 parity audit (bridge `src/catanbot/` vs
standalone `extension/lib/`). The bridge is the source of truth; these
are places the no-bridge JS path diverges. Check items off as they close.

Counts: 61 portable, 21 needs-design, 5 hard-blocked.


## Portable (close these, bridge is truth)

### boards-and-bridge-only

- [ ] **[high]** Restricted opening placement (Gold Rush fog-adjacent corners barred from first two settlements)
  - bridge: colonist_map.py:313-317 latches cat_map.restricted_starting_placement from colonist corner restrictedStartingPlacement flags; recommender.py:219-224 drops fog-adjacent nodes from the legal opening pool when that flag is set.
  - standalone: absent. buildBoardFromColonistMap never reads the restrictedStartingPlacement corner field, sets no flag, and the JS legal pool (legalNodesAfterPicks -> scoreOpeningNodes opts.legalNodes, advisor.js:106-108) only applies the distance rule. 
  - files: colonist_map.py:313-317, recommender.py:219-224 / board.js:210-222 (cornerStates loop ignores the flag) + advisor.js:104-108
- [ ] **[medium]** Gold/volcano wildcard hex valuation in opening picks & build scoring
  - bridge: annotate_gold_nodes/refresh_gold_nodes (colonist_map.py:336-392) record cat_map.gold_node_ids + gold_number; advisor.py:281-331 adds a wildcard yield (_GOLD_WEIGHT) to every gold-adjacent node and exposes a GOLD resource slot; recommender.p
  - standalone: absent. board.js maps colonist tile type 6 (gold) to resource null via COLONIST_TILE_RESOURCE (board.js:38-45,:179-180), so a gold hex is treated exactly like desert: nodeProduction (board.js:348-363) yields nothing and scoreOpeningNodes (a
  - files: colonist_map.py:336-392, advisor.py:281-331 / board.js:38-45,:179-187 + advisor.js scoreOpeningNodes
- [ ] **[medium]** Fog-reveal wildcard EV (Black Forest / Gold Rush expansion-into-fog value)
  - bridge: annotate_fog_nodes/refresh_fog_nodes (colonist_map.py:394-448) record cat_map.fog_node_ids from colonist fog tile types 7/8; advisor.py:290-355 adds a per-fog-tile wildcard reveal yield (_FOG_WEIGHT) to fog-adjacent nodes and shrinks it as 
  - standalone: absent. board.js maps fog tile types 7/8 to resource null (treated as desert, board.js:179-180); there is no FOG_TILE_TYPES set, no fog_node_ids, and no reveal-EV term in advisor.js or recommender.js. A fog corner scores as a dead/desert co
  - files: colonist_map.py:394-448, advisor.py:290-355 / board.js:179-187 + advisor.js
- [ ] **[medium]** Gold-pick advisor (which resource to take when the gold hex rolls)
  - bridge: bridge.py:1813-1839 emits snap['gold_pick'] = {resource, owned, number} via _gold_resource_pick whenever gold_node_ids is non-empty, telling the user which resource to choose on a gold roll.
  - standalone: absent. The standalone snapshot (index.js:50-93) has no gold_pick field and no gold-node tracking to derive one from.
  - files: bridge.py:1813-1839 / index.js buildStandaloneSnapshot (no gold_pick)
- [ ] **[low]** Variant board geometry construction (Pond / Twirl / Gold Rush / Volcano shapes)
  - bridge: colonist_map.py builds a real catanatron CatanMap from colonist tileHexStates: classic uses BASE_MAP_TEMPLATE (_is_classic_shape, colonist_map.py:206-212,:631-632), variants go through _build_variant_catanatron_map (colonist_map.py:215-333)
  - standalone: board.js buildBoardFromColonistMap (board.js:158-342) builds a signature-based node/edge graph from the SAME colonist tileCornerStates/tileEdgeStates with IDENTICAL cornerSig/edgeEndpoints math (board.js:77-100 mirrors colonist_map.py:143-1
  - files: colonist_map.py:215-333 / board.js:158-342 buildBoardFromColonistMap
- [ ] **[low]** fog_hint banner (N fog tiles unrevealed, road into fog is +EV)
  - bridge: bridge.py:2057-2073 emits snap['fog_hint'] = {fog_remaining, message} on black_forest/volcano while fog tiles remain, counted off sess.mapping.tile_types.
  - standalone: absent. No fog_hint field in the standalone snapshot (index.js:50-93) and no fog-tile-remaining count in board.js.
  - files: bridge.py:2057-2073 / index.js (no fog_hint)
- [ ] **[low]** players_unsupported flag (5-6 player boards exceed catanatron's 4 colors)
  - bridge: bridge.py:2046-2049 emits snap['players_unsupported'] = sess.too_many_players() so the HUD shows 'limited tracking'.
  - standalone: absent from the standalone snapshot (index.js:50-93). The 4-color limit is a catanatron constraint the JS engine does NOT share, so the standalone could in principle track 5-6 players, but it neither tracks them specially nor emits the flag
  - files: bridge.py:2046-2049 / index.js (no players_unsupported)
- [ ] **[low]** Variant-board ports / 2:1 trade-rate suggestions
  - bridge: Classic: build_catanatron_map_from_colonist (colonist_map.py:741-852) wires ports into catanatron port_nodes; variants: _attach_variant_ports (colonist_map.py:484-568) reconstructs Port tiles on the synthesized water ring so port adjacency/
  - standalone: board.js Pass 4 (board.js:282-303) attaches ports to nodes by edge-endpoint signature directly from colonist portEdgeStates INCLUDING on variant shapes (no classic-only gate), so the standalone has BETTER variant-port coverage than the brid
  - files: colonist_map.py:484-568 / board.js:282-303

### dev-card-hints

- [ ] **[high]** Road Building HOLD reason naming the settle spot two free roads open (opens_settle branch)
  - bridge: bridge_hints.py _compute_rb_hint:1001-1008   when there is no LR swing and no fog, it calls _suggest_rb_placement, and if the placement's placement_reason startswith 'unlocks' it sets reason = `hold for LR · or play to open a settle spot ({
  - standalone: hints.js _rbHintImpl:236-340   never computes an 'open a settle spot' HOLD reason. With no good landing it emits the flat reason 'no high-value landing · hold'; with a good landing it says 'opens up a strong landing'. It never names which n
  - files: bridge: src/catanbot/bridge_hints.py:892-1031 (+ _suggest_rb_placement:605-843); JS: extension/lib/hints.js:236-340 _rbHintImpl. JS already has board.nodes/edges/tiles/pip and a landing scorer, so a d
- [x] **[high]** Road Building 'secures LR' / 'catches opp LR' / 'low on roads' PLAY reasons and projected-length math  (closed: tests/js/hints.rb.test.mjs)
  - bridge: _compute_rb_hint:954-985 computes projected = self_len + min(2, roads_left), qualify=5, and emits three distinct reasons: 'secures LR · {self_len}→{projected} vs {opp_max}' (when !self_has and projected>=max(5,opp_max+1)); 'catches opp LR ·
  - standalone: hints.js _rbHintImpl:320-331 only has one LR branch: should_play=True with reason 'race for Longest Road' when hasRoad!=self AND myLen+2>=oppMax+1 AND oppMax>=4. Different threshold (oppMax>=4 vs bridge qualify=5 / opp_max>=5), different pr
  - files: bridge: src/catanbot/bridge_hints.py:954-1031; JS: extension/lib/hints.js:320-339. roadLength/hasRoad are already in JS state; only road-piece-supply (for the 'low on roads' case) needs a public-info 
- [x] **[high]** Knight weak-robber-tile hold guard (pip<=2) and knight-stack/late-game rule  (closed: tests/js/hints.knight.test.mjs)
  - bridge: _compute_knight_hint:1707-1734: with only one knight in hand and not late-game (self_vp < close_to_win_vp-2), it will NOT fire PLAY just because the robber is on you if the tile is weak (robber_tile_pip<=2, i.e. 2/3/11/12)   reason becomes 
  - standalone: knightHint:61-106 fires PLAY for any robberOnUs regardless of tile pip or knight count (reason 'robber on us · clear it'), has no weak-tile guard, no knight-stack/late-game gate, and no robber-target-score branch at all.
  - files: bridge: src/catanbot/bridge_hints.py:1718-1760; JS: extension/lib/hints.js:67-99. JS has state.vp, state.robberTile, tile pips (pipsForNumber) and playedKnights, so the pip<=2 weak-tile and stack/late
- [x] **[high]** Knight 'strong block available' PLAY trigger from robber-target score  (closed: tests/js/hints.knight.test.mjs)
  - bridge: _compute_knight_hint:1654-1760 calls _compute_robber_snapshot (top=1), takes top_score, and at top_score>=4.0 (gated by knight_stack_ok) fires PLAY with reason `a strong block on {resource} {number} is available`, and ships best_target (the
  - standalone: knightHint never scores robber targets. It has no top_score branch and never emits best_target. The standalone has recommendRobberTargets (recommender.js:727) producing a score, but knightHint does not consult it, so the 'strong block avail
  - files: bridge: src/catanbot/bridge_hints.py:1654-1766; JS: extension/lib/hints.js:60-106 (knightHint emits no best_target), scorer exists at extension/lib/recommender.js:727-835. Note score scales differ (br
- [x] **[medium]** Knight 'steal/secure Largest Army' verdict with strict-exceed rule  (closed: tests/js/hints.knight.test.mjs)
  - bridge: _compute_knight_hint:1665-1745 computes knight_secures_la accounting for an already-held LA (must strictly exceed the holder's played count: self_played>=la_holder_played) vs nobody holding LA (self_played>=2 and self+1>opp_max_played), emi
  - standalone: knightHint:88-91 uses willClaimLA = !haveLA && (myKnights+1>=3)   a flat threshold-3 check that ignores opponents' played-knight counts entirely (doesn't enforce strict-exceed, doesn't handle stealing an opp's held LA), and emits a single r
  - files: bridge: src/catanbot/bridge_hints.py:1685-1745; JS: extension/lib/hints.js:88-96. JS tracks playedKnights and hasArmy per color, so the strict-exceed and steal-vs-claim logic is reproducible.
- [x] **[medium]** Knight deny-LA threat trigger thresholds  (closed: tests/js/hints.knight.test.mjs)
  - bridge: _compute_knight_hint:1639-1737 fires 'an opp is close to Largest Army · play to deny' when any opp has played>=3 OR (played>=2 AND vp>=largest_army_threat_vp config). Independent of how many knights self holds.
  - standalone: knightHint:82-98 sets oppCloseToLA when an opp has k>=2 AND k>=myKnights+1, but only fires PLAY ('opp closing on LA · race') when self also has have>=2 knights; the bridge has no such self-stack requirement and uses a played>=3-or-(>=2&vp) 
  - files: bridge: src/catanbot/bridge_hints.py:1639-1737; JS: extension/lib/hints.js:80-98. JS has state.vp and playedKnights; the only missing input is the largest_army_threat_vp config constant, which can be 
- [ ] **[low]** YoP no-build-within-reach HOLD pair selection and pad logic
  - bridge: _compute_yop_hint:546-573 (no unlock) picks the pair by demand-weighted-by-priority across all builds (defaulting ORE+WHEAT); when a single-resource unlock needs a 2nd slot it fills toward the next-best build (priority-ordered, ORE default)
  - standalone: yopHint:205-211 hardcodes the HOLD pair to ['WHEAT','ORE'] with reason 'no build within reach', and when padding a single-need pair it always appends 'WHEAT' (line 198) regardless of what the next build needs.
  - files: bridge: src/catanbot/bridge_hints.py:507-573; JS: extension/lib/hints.js:186-211. Pure hand+cost logic, fully reproducible in JS.
- [ ] **[low]** YoP field name and ordering: 'pair'/'unlock'/'bank_ok' vs JS 'take'/'target_kind'
  - bridge: returns keys have, should_play, reason, pair, unlock, bank_ok (:595-602).
  - standalone: yopHint returns have, take, should_play, reason, target_kind (:212-218)   different key names (take vs pair, target_kind vs unlock) and no bank_ok/unlock key. Any consumer expecting the bridge field names diverges.
  - files: bridge: src/catanbot/bridge_hints.py:595-602; JS: extension/lib/hints.js:206-218.

### events-state

- [ ] **[high]** Opponent 2nd-settlement starting yield (ProduceEvent on placement)
  - bridge: events_from_diff detects an opponent's 2nd settlement (corner count == 2 after the build) and emits a ProduceEvent for the 3 adjacent tile resources, seeding opp hands before their first real roll (colonist_diff.py:605-618, _starting_resour
  - standalone: absent   events.js processes tileCornerStates only to set state.buildings (events.js:296-345); it never computes a starting-resource bag and opp hands are never built from per-resource production. Opp hands stay total-only via handTotal.
  - files: src/catanbot/colonist_diff.py:605-618 / extension/lib/events.js applySnapshot tileCornerStates loop (296-345)
- [ ] **[high]** Per-roll opponent resource distribution (produce_events_for_roll)
  - bridge: On every non-7 roll the bridge walks the actual colonist resource layout (tile_dice/tile_types) and tracked corner ownership to compute per-opponent per-resource yield, skipping the robber tile and self, emitting one ProduceEvent per opp (c
  - standalone: absent   events.js only counts the roll into rollHistogram/totalRolls/rollHistory and pushes a card-total history sample (events.js:651-709). It never credits per-resource production to any color; opp typed hands (state.hands[opp]) remain a
  - files: src/catanbot/colonist_diff.py:1164-1206 / extension/lib/events.js diceState block (651-709)
- [ ] **[high]** Authoritative resource bank tracking (bankState -> BankSyncEvent)
  - bridge: LiveSession seeds bank_resources from GameStart bankState.resourceCards and patches it from every partial bankState delta in a diff, emitting a merged BankSyncEvent that overwrites the tracker's freqdeck (colonist_diff.py:247, 364-373, 674-
  - standalone: absent for the resource bank   events.js _WANTED_KEYS (events.js:52-58) does not include bankState and applySnapshot never reads it. state has no resource-bank field. The panel approximates a bank as 19 - sum(self hand + chat-inferred opp h
  - files: src/catanbot/colonist_diff.py:674-698 / extension/lib/events.js _WANTED_KEYS (52-58) + panel.js:1323-1368
- [ ] **[medium]** Friendly Robber rule latched from WS gameSettings
  - bridge: from_game_start latches friendly_robber_active = bool(gameSettings.friendlyRobber) authoritatively from the WS GameStart payload (colonist_diff.py:230, 320-329); the robber-target ranker filters protected (<=2 VP) victims out using this WS-
  - standalone: absent from WS path   events.js reads gameSettings only for victoryPointsToWin and cardDiscardLimit (events.js:231-241); it never reads gameSettings.friendlyRobber. The standalone robber ranker (recommender.js:765-771) does honor friendly r
  - files: src/catanbot/colonist_diff.py:320-329 / extension/lib/events.js gameSettings block (231-241)
- [ ] **[medium]** Self dev-card PLAY detection from WS developmentCardsUsed
  - bridge: _dev_card_buy_events multiset-diffs colonist's authoritative self developmentCardsUsed history each frame and emits a DevCardPlayEvent for every newly-played non-VP card (colonist_diff.py:872-906), so a self knight/monopoly/RB/YOP play is d
  - standalone: partial   events.js has no developmentCardsUsed parsing at all (no DevCardPlayEvent analog). It derives knightRobberPending only from the nested-under-playerStates mechanicKnightState.knightsPlayed +1 on self's turn (events.js:474-500). The
  - files: src/catanbot/colonist_diff.py:872-906 + src/catanbot/live_game.py:476-506 / extension/lib/events.js playerStates knight block (474-500)
- [ ] **[low]** Self developmentCardsBoughtThisTurn carve-out
  - bridge: LiveSession mirrors colonist's authoritative self developmentCardsBoughtThisTurn (typed list, clears to null on turn flip) into self_dev_bought_this_turn (colonist_diff.py:204, 859-865), letting the advisor exclude a just-bought card from p
  - standalone: absent   events.js never reads developmentCardsBoughtThisTurn and state has no field for it. Standalone dev-card hints cannot compute the just-bought carve-out from WS data; they only see the aggregate typed dev-card counts in devCardsByTyp
  - files: src/catanbot/colonist_diff.py:859-865 / extension/lib/events.js developmentCards block (426-458)
- [ ] **[low]** Authoritative end-game dice distribution (endGameState.diceStats)
  - bridge: LiveSession captures colonist's authoritative diceStats (11-element per-number distribution) to override the incremental roll tally that drifts on missed/reconnect frames (colonist_diff.py:248-254; commit e329b65 'use colonist's authoritati
  - standalone: absent   events.js builds rollHistogram purely incrementally from observed diceState frames (events.js:684-685) with the same drift-on-missed-frames problem the bridge fixed; it never reads endGameState.diceStats. (diceState is also gated b
  - files: src/catanbot/colonist_diff.py:248-254 / extension/lib/events.js diceState rollHistogram (684-685)
- [ ] **[low]** Roll dedup keyed on roller+dice (resync-rebroadcast guard)
  - bridge: events_from_diff dedups a roll on (cid, d1, d2) via last_roll_emitted so a state-resync rebroadcast of an identical recent roll diff doesn't double-bump roll_histogram/total_rolls (colonist_diff.py:168, 773-780).
  - standalone: present but weaker   events.js dedups on fingerprint `${roller}|${d1}|${d2}` stored in _lastRollFp (events.js:671-672). Equivalent for back-to-back identical frames, but it only remembers the single most recent fingerprint (same as bridge),
  - files: src/catanbot/colonist_diff.py:773-780 / extension/lib/events.js diceState dedup (664-672)
- [ ] **[low]** Too-many-players (5-6 seat) limited-tracking guard
  - bridge: LiveSession.too_many_players() flags lobbies seating more than catanatron's 4 colors so LiveGame refuses to seat a corrupt color map and surfaces 'limited tracking' (colonist_diff.py:484-486; live_game.py:220-223).
  - standalone: absent   the JS keys everything by colonist color id 1..6 with no 4-color cap (state.colors grows freely, _ensureColor accepts any cid). There is no players_unsupported flag, so a 5-6 player game silently produces recs against a partial mod
  - files: src/catanbot/colonist_diff.py:484-486 + live_game.py:220-223 / extension/lib/events.js _ensureColor (158-171)

### recommender

- [ ] **[high]** settlement / city / road rec detail strings + rationale line
  - bridge: Detail carries concrete production: settlement '+{prod:.2f}/roll' (recommender.py:1116) optionally '· settle #3', city '+{prod:.2f}/roll · +1 VP' (recommender.py:1141), road '→ {prod:.2f}-prod spot' with '· {res} port' / '· reveals fog' / '
  - standalone: Static placeholder details: 'place settlement' (recommender.js:325), 'upgrade settlement → city' (recommender.js:299), 'extend road' (recommender.js:372). No per-roll number, no port/fog/plan suffix, and no 'rationale' field is emitted anyw
  - note: nodeProduction and tile data are already available in JS (used for scoring); formatting the same '+X.XX/roll' detail and a per-resource rationale is pure presentation logic over public board info.
  - files: bridge src/catanbot/recommender.py:1116,1141,1352-1354,887-988; JS extension/lib/recommender.js:299,325,372 (_cityRecs/_settleRecs/_roadRecs)
- [ ] **[medium]** Road landing-target alternates, fallback/sealed roads, edge_from/edge_to, landing_node
  - bridge: Builds a primary road rec plus up to ~4 alternates: landing-target alts from edge_scores[1:4] gated at 30% of top prod (recommender.py:1365-1379), LR-extension top-ups from fallback_candidates at 0.6x score with detail 'extends network' (re
  - standalone: _roadRecs (recommender.js:344-381) emits up to 3 road recs each from _bestLanding, but every rec requires landing.total>0 (recommender.js:363) so there is NO sealed/fallback/no-settle-spot road rec and no 'extends network' wording; no alt f
  - note: Buildable edges, neighbor graph, and production are all public and already computed in JS; the alternates, sealed-fallback, edge ordering, and field rename are pure logic ports.
  - files: bridge src/catanbot/recommender.py:1331-1476; JS extension/lib/recommender.js:344-381 (_roadRecs), 236-267 (_bestLanding)
- [ ] **[medium]** propose_trade ask-supply gating (board_produces / bank-19 / known-holder)
  - bridge: Suppresses a propose_trade ask unless the resource physically exists on the board (recommender.py:1733), the bank doesn't still hold all 19 (recommender.py:1738-1740), and an opp is known to hold it or it is plausibly in an unknown pile (re
  - standalone: _proposeTradeRecs (recommender.js:527-589) applies none of these guards   it offers a trade for the missing resource purely from self's surplus, regardless of whether any opponent could supply it or whether the board even produces it.
  - note: opp hand estimates, bank supply, and the board tile set are all public info the standalone already tracks (used elsewhere in the panel); porting the three guards is pure logic.
  - files: bridge src/catanbot/recommender.py:1733-1759; JS extension/lib/recommender.js:550-587 (_proposeTradeRecs loop)
- [ ] **[medium]** endgame VP-push close-to-win threshold
  - bridge: close_to_win_vp() (config) drives the endgame bump; default is VP_TARGET - 2 (e.g. 8 on a 10 game) per the docstring at recommender.py:1939-1941 ('10 VP target -> >= 8 VP').
  - standalone: _applyPhaseBumps (recommender.js:625) computes closeVp = max(2, round(vpTarget*0.80)) = 8 on a 10-target, which matches for 10 but diverges on other targets   e.g. a 12-VP game gives round(9.6)=10 in JS but the bridge docstring says 12->10,
  - note: VP target is public; this is just two different threshold formulas. Aligning JS to the bridge's close_to_win_vp (VP_TARGET - 2) is a one-line change.
  - files: bridge src/catanbot/recommender.py:1939-1962 (and config.close_to_win_vp); JS extension/lib/recommender.js:625-629 (_applyPhaseBumps)
- [ ] **[medium]** 'save for X' soon-plans (settlement/city/dev one-to-two cards away)
  - bridge: When a build is unaffordable but only 1-2 cards short, emits a when='soon' rec with missing-cards detail (recommender.py:1498-1547): settlement, city, and dev_card soon-plans, each with _format_missing detail and rationale.
  - standalone: _settleRecs/_cityRecs set when='soon' with a missing field when unaffordable (recommender.js:297,321), but they ONLY appear if the node is already in the legal/owned set; there is no dedicated single best 'save for X' rec with the bridge's 
  - note: Missing-card math and _format_missing emoji formatting are pure logic over public hand+cost; _missing already exists in recommender.js:92-99.
  - files: bridge src/catanbot/recommender.py:1493-1547; JS extension/lib/recommender.js:283-341 (_cityRecs/_settleRecs soon branch)
- [ ] **[low]** bank_trade score derivation (multi-step base score)
  - bridge: Bank-trade score = score_fn(prod) of the best target spot minus 1, clamped [2,9] (recommender.py:1618,1624). For settlement it uses _score_settlement on the weighted prod of the best buildable spot; city uses _score_city; dev uses 3.0.
  - standalone: _bankTradeRecs (recommender.js:450-491) mirrors this closely but the settlement branch scores with sp.raw (raw production) not the wheat-weighted production (recommender.js:457-458)   _clip(sp.raw*12+2,...)   whereas the bridge's _score_set
  - note: _weightedProd already exists in recommender.js:48-54; feeding it instead of raw is a one-line fix over public info.
  - files: bridge src/catanbot/recommender.py:1114,1592,1618; JS extension/lib/recommender.js:457-458 (_bankTradeRecs settlement target)
- [ ] **[low]** bank-empty gating of bank/port trades
  - bridge: _plan_bank_trades returns None (no trade rec) when bank_supply has 0 of a needed resource (recommender.py:722-723), and propose_trade also skips asks when the bank holds all 19 (recommender.py:1738-1740).
  - standalone: planBankTrades (trades.js:122,136) correctly returns null when the bank is empty of the needed resource, matching the bridge for bank_trade. But the propose_trade path (recommender.js:527-589) has no bank-19 guard, so the standalone can sti
  - note: bank supply is public-inferred and already passed into _bankTradeRecs via opts.bankSupply; threading the same into _proposeTradeRecs is pure logic.
  - files: bridge src/catanbot/recommender.py:722-723,1738-1740; JS extension/lib/trades.js:122,136 (planBankTrades), extension/lib/recommender.js:527-589 (_proposeTradeRecs)
- [ ] **[low]** plan-alignment annotation ('· supports plan')
  - bridge: After assembling recs, if a 'soon' settlement plan exists and a road rec's tiles overlap the plan's tiles, the road's detail gets '· supports plan' appended (recommender.py:2079-2108).
  - standalone: Absent. recommender.js has no soon-settle plan detection and never annotates road recs with 'supports plan' (the grep for 'supports plan' returns no hits in extension/lib).
  - note: Requires soon-settle plans to exist in JS first (also absent, see below), but the overlap-and-annotate logic itself is pure tile comparison over public info.
  - files: bridge src/catanbot/recommender.py:2079-2108; JS extension/lib/recommender.js (no equivalent)
- [ ] **[low]** settle/city rec count and ordering (top-3 vs sorted-after-bump)
  - bridge: Settlements: top-3 of buildable nodes sorted by wheat-weighted prod, third-settle bump applied inline before append (recommender.py:1100-1127). City: ALL owned settlements emitted (no slice), each scored (recommender.py:1131-1144).
  - standalone: Settlements: _settleRecs slices to top-3 sorted by weighted prod (recommender.js:338-340)   matches. City: _cityRecs sorts by score and slices to top-3 (recommender.js:306-307), so on a board where self owns 4+ settlements the standalone dr
  - note: Pure logic; remove the slice(0,3) in _cityRecs to match, or accept the cap as a product choice.
  - files: bridge src/catanbot/recommender.py:1131-1144; JS extension/lib/recommender.js:306-307 (_cityRecs slice)
- [ ] **[low]** _bestLanding / road blocked-set port-bonus source
  - bridge: Port-match bonus reads node_to_port labels built from m via _build_node_port_labels and only applies for a 2:1 (non-3:1) port whose resource self produces (recommender.py:1285-1296).
  - standalone: _bestLanding reads board.nodes[nb].port and applies a 1.4 bonus when port.resource is in ownedResources (recommender.js:254-258); it does NOT explicitly exclude 3:1 generic ports there (it relies on a 3:1 port having no .resource). If the J
  - note: Pure logic; add the explicit 3:1 exclusion to match the bridge guard.
  - files: bridge src/catanbot/recommender.py:1285-1296; JS extension/lib/recommender.js:254-258 (_bestLanding port bonus)
- [ ] **[low]** dedup key and propose/bank conflict filter
  - bridge: The bridge does not run a generic (kind,node) dedup pass; it caps each category at the source (one bank trade via break at recommender.py:1652, one build's worth of propose trades via break at recommender.py:1838) and relies on category emi
  - standalone: recommendActions (recommender.js:695-713) adds an explicit dedup by `${kind}|${node_id||edge||target_kind}` and a filter dropping propose_trade whose unlocks matches an existing bank_trade target. This is extra JS-side logic with no bridge 
  - note: Pure logic divergence; both are public-info, but the standalone added a filter the bridge lacks, so outputs can differ in which trade recs survive.
  - files: bridge src/catanbot/recommender.py:1649-1652,1835-1838; JS extension/lib/recommender.js:695-713 (recommendActions dedup/filter)

### robber

- [x] **[high]** VP-weighted blocking value (per-victim continuous weight vs flat single-victim bonus)  (closed: tests/js/recommender.robber_vpweight.test.mjs)
  - bridge: score_robber_targets weights each victim's blocked pips by _vp_weight(vp) = 1.0 + 0.4*max(0, vp - early_game_baseline_vp) (advisor.py:593-597, _vp_weight at 475-487). So a near-win opponent's tile can score 3.4x an equal-pip early-game oppo
  - standalone: recommendRobberTargets ignores victim VP entirely in the block term. It computes score = pip*pieceValue + bestVictimCards*1.5 - pip*selfPieceValue (recommender.js:819). VP only enters via the friendly-robber >2 filter (line 768); a 9-VP lea
  - files: src/catanbot/advisor.py:475-487,593-597,636; extension/lib/recommender.js:819 (recommendRobberTargets). state.vp is already populated from colonist victoryPointsState (events.js:461-469), same public 
- [x] **[high]** Imminent-winner 2x tile-weight multiplier  (closed: tests/js/recommender.robber_vpweight.test.mjs)
  - bridge: _detect_imminent_opp_color (bridge_robber.py:12-79) flags any opp who could take Largest Army (+1 knight in hand reaches max(3, opp_max_played+1) and vp+2>=target) or Longest Road (+1 road reaches max(5, opp_max_roads+1) and vp+2>=target). 
  - standalone: absent. recommendRobberTargets has no imminent detection and no per-color multiplier; an opp about to flip LA/LR for the win gets no extra robber priority over any other opp.
  - files: src/catanbot/bridge_robber.py:12-79 (_detect_imminent_opp_color), advisor.py:591-596; extension/lib/recommender.js:727-835. All inputs (played knights, knight-in-hand, longest-road length, has-army/ha
- [ ] **[medium]** resource_need_bonus and monopoly_setup_bonus (resource-control scoring)
  - bridge: Adds resource_need_bonus = 1.0 + 0.2*pip_dots when the tile produces a resource self owes for its next planned build (advisor.py:615-616; needed_resources derived in bridge_robber.py:169-178 from _closest_missing_build), and monopoly_setup_
  - standalone: absent. recommendRobberTargets emits no resource_need_bonus or monopoly_setup_bonus fields and the score never accounts for self's build needs or production share, so the panel can show no resource-control tags in standalone mode.
  - files: src/catanbot/advisor.py:611-635, bridge_robber.py:146-178,249-252; extension/lib/recommender.js:819-831. JS already has per-node production (nodeProduction/board.js) and hand state, so closest-missing
- [ ] **[medium]** suggested_victim / steal-from selection
  - bridge: _victim_priority = cards * vp_weight + pips * 0.3, where vp_weight = 3.0 if vp>=close_to_win_vp else 1.8 if vp>=mid_late_vp else 1.0 (bridge_robber.py:224-240). It also restricts the pool to victims holding >=1 card before taking the max (l
  - standalone: recommendRobberTargets picks the victim with strictly the most cards (state.handTotal[c]), ties broken by iteration order, with no VP or pip weighting and no >=1-card pool restriction (recommender.js:801-809). bestVictim drives steal_from_c
  - files: src/catanbot/bridge_robber.py:221-240; extension/lib/recommender.js:801-809. close_to_win_vp = round(vpTarget*0.80), mid_late_vp = round(vpTarget*0.60) are pure config; handTotal and vp are already in
- [ ] **[low]** Friendly-robber protection threshold (hardcoded vs configurable, and comparison boundary)
  - bridge: Uses get_friendly_robber_protected_vp() (default 2, env-overridable via CATANBOT_FRIENDLY_ROBBER_PROTECTED_VP) and filters with vp <= threshold (advisor.py:583-586, bridge_robber.py:132-134). Protected victims are dropped per-victim, so a m
  - standalone: recommendRobberTargets hardcodes `vp > 2` (recommender.js:766-768). Same default boundary (2 VP, strictly-greater keeps the same set as bridge's <=2 drop), and it also filters per-opp before dropping empty tiles, so behavior matches at the 
  - files: src/catanbot/config.py:58-59,100-113 (get_friendly_robber_protected_vp), advisor.py:583-586; extension/lib/recommender.js:766-768. House-rule override is a product decision/config plumbing, not a logi
- [ ] **[low]** own-block penalty magnitude (interaction with weighted base, not just the subtraction)
  - bridge: own_blocked accumulates pip_dots*weight for each self building (city=2x) and is subtracted from the VP-weighted opponent term (advisor.py:566-576,636). Because the opponent term is VP-weighted while own_blocked is raw pips, the relative pen
  - standalone: score = pip*pieceValue + bestVictimCards*1.5 - pip*selfPieceValue (recommender.js:784-787,819-820). The self penalty (pip*selfPieceValue, city=2x) is structurally analogous, but since the JS opponent term is unweighted the trade-off between
  - files: src/catanbot/advisor.py:566-576,636; extension/lib/recommender.js:784-787,819-820. Falls out for free once the VP-weighting (gap 1) is ported; no new data needed.
- [ ] **[low]** sort tiebreakers
  - bridge: results.sort by (-score, -max single-victim hand size, -raw opponent_blocked pips) (advisor.py:656-660), giving a stable, EV-aware ordering when scores tie.
  - standalone: targets.sort((a,b) => b.score - a.score) only (recommender.js:833); ties resolve by Array.sort stability / insertion order, which is tile-iteration order, not hand size or pips.
  - files: src/catanbot/advisor.py:656-660; extension/lib/recommender.js:833. Both tiebreak inputs (per-victim hand size, raw opponent pips) are already computed in the JS loop.

### snapshot-fields

- [ ] **[high]** Opponent per-roll production field shape: opp block uses scalar `prod`, renderer reads `o.production.per_roll`
  - bridge: Each opp row carries `production` = full dict {per_roll, top_resource, by_resource} from _compute_production (bridge.py:2374). Opp-row renderer panel.js:4937 reads `o.production.per_roll` for the `0.42/roll` tag.
  - standalone: oppsBlock sets `prod: oppPerRoll` (bare number) at panel.js:1284, NOT `production`. So in standalone mode the opp `x/roll` tag at panel.js:4937 never renders (o.production is undefined). engine_deficit was separately patched to read `o.prod
  - files: bridge.py:2374 + bridge_economy._compute_production; JS panel.js:1266-1285 oppsBlock builder sets `prod` not `production`; renderer panel.js:4937
- [ ] **[medium]** strategic_options (long-game LR/LA/dev-dive options with vp_swing)
  - bridge: bridge.py:2623 sets `strategic_options` from _compute_strategic_options (bridge_hints)   list of {label, detail, vp_swing} long-horizon plays. Renderer panel.js:4518-4530 prints them under a `→ long game` header.
  - standalone: absent; standalone snap sets game_plan but never strategic_options, so the `→ long game` section never appears in no-bridge mode.
  - files: bridge.py:2623 + bridge_hints._compute_strategic_options; JS panel.js standalone snap ~2090 omits the field; renderer panel.js:4518
- [ ] **[medium]** yield_summary (actual vs expected cards across roll window)
  - bridge: bridge.py:1897-1904 sets `yield_summary` = {window, got, blocked, expected} by aggregating per-entry gained_total/blocked_total in roll_history against production.per_roll. Renderer panel.js:5173-5184 prints `got X/Y (N rolls)` with a `behi
  - standalone: absent; standalone roll_history entries are not enriched with per-roll gained_total/blocked_total (bridge computes those in _track_overlay_state via _compute_roll_yield), and the standalone snap never sets yield_summary, so the yield-summar
  - files: bridge.py:1879-1904, 884-918 (roll-time yield enrichment); JS panel.js standalone snap ~2090 omits yield_summary; renderer panel.js:5173
- [ ] **[medium]** Per-opp can_afford and one_short (next-VP-build pre-warnings)
  - bridge: Each opp row carries `can_afford` = _affordable_builds(inferred, unknown) (bridge.py:2366) and `one_short` = _one_short_vp_build (bridge.py:2370). Renderer panel.js:4960-4981 prints `can: city, settlement` and `1 <res> → city` tags per opp.
  - standalone: absent from oppsBlock (panel.js:1266-1285); the standalone computes per-opp inferred hands (_chatHands) and unknown buckets but never derives can_afford/one_short, so those opp warning tags never render in no-bridge mode.
  - files: bridge.py:2366,2370 + _affordable_builds/_one_short_vp_build; JS oppsBlock panel.js:1266-1285; renderer panel.js:4960
- [ ] **[low]** last_roll.opponent_yields and last_roll.yield (per-roll who-gained breakdown)
  - bridge: bridge.py:1844-1878 enriches snap.last_roll with `yield` (self gained/blocked) and `opponent_yields` (per-opp {color, gained_total, blocked_total}) via _compute_roll_yield. Renderer panel.js:5140-5164 prints `they: <color +n / blk>` from la
  - standalone: absent; standalone lastRoll is just `st.rollHistory[last]` (panel.js:1288, 2242) with no `.yield`/`.opponent_yields`, so the per-roll opponent-yield line never renders in no-bridge mode.
  - files: bridge.py:1844-1878; JS panel.js:1288/2242 lastRoll; renderer panel.js:5140
- [ ] **[low]** production_stall / hot_numbers / sevens_hot detection method drift
  - bridge: bridge.py:1954-1971 production_stall counts non-7 rolls since last gain via roll_history gained_total. hot_numbers (bridge.py:1928-1948) ranks by ratio vs 36-roll baseline, top-2; sevens_hot over the live window (bridge.py:1911-1919).
  - standalone: Present but with method/threshold drift: standalone production_stall (panel.js:2155-2197) re-derives self-tile gain from roll total + self buildings (standalone roll_history has no gained_total); hot_numbers (panel.js:1762-1790) uses last-1
  - files: bridge.py:1905-1971; JS panel.js:1762-1790 (hot/sevens), 2155-2197 (stall)
- [ ] **[low]** index.js buildStandaloneSnap stub is stale/unused and mis-shapes fields
  - bridge: n/a   this is a JS-internal inconsistency. The bridge snap is the contract both JS surfaces claim to mirror.
  - standalone: extension/lib/index.js:53-94 exports `buildStandaloneSnap` documented as the standalone snap entry point, but the panel does not call it (panel.js builds its own inline snap at ~2090). The stub sets self:null, opps:[], strategy/game_plan/st
  - files: extension/lib/index.js:46-94 (buildStandaloneSnap, unused); the live builder is panel.js:2090-2299

### strategy

- [ ] **[high]** pivot_details payload shape breaks pivot-trigger text in the HUD
  - bridge: bridge_strategy.py:79 sets pivot_details = [t.detail for t in triggers], a list of strings. Panel reads triggerDetails[i] (panel.js:3686-3694) and escapeHtml()s it directly, rendering the human detail line.
  - standalone: strategy.js:420-422 sets pivot_details: triggers.map(t => ({name, detail})), a list of objects. Panel escapeHtml(detail) (panel.js:3693; escapeHtml :5704 does String(obj)) renders each fired trigger as the literal [object Object].
  - note: One-line fix: emit pivot_details as plain detail strings (triggers.map(t => t.detail)) to match the bridge contract the panel expects.
  - files: bridge src/catanbot/bridge_strategy.py:79; JS extension/lib/strategy.js:419-422; consumer extension/panel.js:3686-3694,5704
- [ ] **[medium]** opp_close_to_win trigger VP threshold
  - bridge: _detect_opp_close (strategy_select.py:749-756) fires when an opp VP >= close_to_win_vp() = round(VP_TARGET*0.80) = 8 for a 10-VP game (config.py:124-133).
  - standalone: _detectOppCloseToWin (strategy.js:314-329) uses closeAt = (vpTarget||10)-4 = 6 and its comment wrongly claims it matches the bridge; fires two VP earlier (6 vs 8) and scales as target-4 not target*0.80.
  - note: Replace target-4 with Math.max(2, Math.round(vpTarget*0.80)). Pure constant fix.
  - files: bridge src/catanbot/strategy_select.py:749-756, src/catanbot/config.py:124-133; JS extension/lib/strategy.js:314-329
- [ ] **[medium]** opp_close_to_la trigger gate (VP floor plus extra knight-lead condition)
  - bridge: _detect_opp_close (strategy_select.py:757-771) fires when opp played_knights >= 2 AND not has_army AND vp >= largest_army_threat_vp()-1 = round(VP_TARGET*0.70)-1 = 6 for a 10-VP game; no opp-knights>my-knights condition.
  - standalone: _detectOppCloseToLA (strategy.js:297-313) fires when k>=2 AND k>myK AND vp>=4: hard-coded VP floor of 4 instead of the scaled 6, plus an added k>myK gate the bridge lacks that suppresses the warning when self already matches the opp knight 
  - note: Use the 0.70-ratio VP floor and drop or align the k>myK gate. Constant/condition fix.
  - files: bridge src/catanbot/strategy_select.py:757-771, src/catanbot/config.py:136-143; JS extension/lib/strategy.js:297-313
- [ ] **[low]** Strategy stickiness / anti-flicker on the active-archetype banner
  - bridge: select_strategy (strategy_select.py:604-619) takes the previous tag and will not flip primary unless the new top score >= 1.15x the previously chosen tag's current score; set_at_rolls carries forward for stability across WS frames.
  - standalone: computeStrategy is stateless (header :26-27): each call recomputes primary from the current ranking with no previous-tag memory, so the banner can flip between similarly scored archetypes on consecutive frames; no set_at_rolls equivalent.
  - note: Pure logic: persist prior snap.strategy.primary on state, pass it in, apply the same 1.15x guard. Public-info only.
  - files: bridge src/catanbot/strategy_select.py:604-619; JS extension/lib/strategy.js:382-396
- [ ] **[low]** Post-placement rationale text (live production numbers vs static blurb)
  - bridge: _rationale_for (strategy_select.py:630-648) emits rationale with live numbers, e.g. 'ore 0.20/r + wheat 0.25/r city + dev engine' or RB 'corridor carved out (~73% isolation)', rendered in strat-why (panel.js:3618-3620).
  - standalone: computeStrategy sets rationale from the static RATIONALE map (strategy.js:251-257,410), e.g. 'ore + wheat lean city-rush w/ dev-card flex'; no live cards/roll figures, no isolation %. Same render path, so the standalone shows a generic blur
  - note: Standalone already computes prod (combinedProd) and an isolation-proxy reach; formatting them into the rationale string is trivial, all public-info.
  - files: bridge src/catanbot/strategy_select.py:630-648; JS extension/lib/strategy.js:251-257,410
- [ ] **[low]** hot_number and seven_overdue detector minimum-roll floors
  - bridge: _detect_hot_number (strategy_select.py:677-700) fires on count>=4 over the last 10 rolls with no history-length floor. _detect_seven_overdue (:775-793) fires whenever hand>limit and no 7 in the last 10 rolls, also with no length floor.
  - standalone: _detectHotNumber (strategy.js:277-296) additionally requires recent.length>=5; _detectSevenOverdue (strategy.js:330-343) additionally requires recent.length>=8. Early-game the standalone suppresses both triggers where the bridge already fir
  - note: Adjust or remove the length floors to match the bridge. Pure logic, public-info.
  - files: bridge src/catanbot/strategy_select.py:677-700,775-793; JS extension/lib/strategy.js:277-296,330-343

### trades

- [x] **[high]** opp_imminent (can-win-next-turn) decline short-circuit is never triggered in JS  (closed: tests/js/trades.imminent.test.mjs)
  - bridge: evaluate_incoming_trade takes opp_imminent and, when set by the leader-threat detector, returns verdict=decline score=-10 reason='opp can win NEXT TURN · don't feed' before any scoring (recommender.py:2227-2230).
  - standalone: trades.js:215-219 implements the identical short-circuit on opts.oppImminent, but the only caller (panel.js:1944-1948) never passes oppImminent, so it is dead code and an imminent-win opponent is scored as an ordinary swap (likely landing o
  - files: src/catanbot/recommender.py:2227-2230 vs extension/lib/trades.js:215-219 (present) + extension/panel.js:1944-1948 (caller omits oppImminent)
- [ ] **[medium]** propose_trade recommendations have no opponent-supply / board-resource / bank guards in JS
  - bridge: _propose path (recommender.py:1733-1759) gates every propose_trade on: need_res in board_resources (variant maps can omit a resource), bank not holding all 19 of need_res (else nobody owns it), AND a known holder (opp_resource_total>0) or u
  - standalone: _proposeTradeRecs (recommender.js:527-589) gates only on handCanAfford, exactly-1-card-short, and self surplus beyond reservedAcross. It never inspects opponent hands, board resources, or bank supply, so it can recommend proposing a trade f
  - files: src/catanbot/recommender.py:1733-1759 vs extension/lib/recommender.js:550-587 (_proposeTradeRecs)
- [ ] **[medium]** propose_trade variants and the proactive rebalance trade are absent in JS
  - bridge: Bridge emits up to 3 variants per blocked build: '1:1 fair', '2:1 concede' (-0.6 score), and '2:2 even' (-0.2) when need_n>=2 (recommender.py:1767-1832), plus a separate proactive 'rebalance' propose_trade (2-for-1, score 3.5, recommender.p
  - standalone: _proposeTradeRecs (recommender.js:550-588) emits only the '1:1 fair' variant (single surplus, single 1-card deficit), no 2:1/2:2, and there is no rebalance-trade block at all. Fewer/different propose recs change which rec is 'best now' for 
  - files: src/catanbot/recommender.py:1767-1832 + 1840-1928 vs extension/lib/recommender.js:550-588
- [ ] **[low]** close-to-win VP threshold rounding differs (Python round vs JS Math.round)
  - bridge: _CLOSE_TO_WIN_VP = close_to_win_vp() = max(2, round(VP_TARGET * 0.80)) using Python's banker's rounding (config.py:124-133, _CLOSE_TO_WIN_RATIO=0.80). Drives the 'opp at N VP · don't feed' / 'hold cards' guards and the counter-offer gate (r
  - standalone: closeVp = Math.max(2, Math.round(vpTarget * 0.80)) (trades.js:190) using JS half-up rounding. For VP_TARGET=10 both give 8, but for targets where target*0.8 lands on .5 (e.g. ties) Python round-half-to-even and JS round-half-up diverge by 1
  - files: src/catanbot/config.py:124-133 vs extension/lib/trades.js:190
- [ ] **[low]** counter-offer gate: bridge has explicit want_total<=give_total and want_total<=1 short-circuits
  - bridge: _suggest_counter_offer (recommender.py:2149-2167) returns None when want_total <= give_total OR want_total <= 1, then trims want to give_total and only surfaces a counter if the trimmed offer re-evaluates to 'accept'. Counter is also fully 
  - standalone: trades.js:266-278 inlines the counter: skips on accept/!allowCounter/oppVp>=closeVp, trims want to giveTotal, and only emits if tTotal>0 && tTotal<wantTotal && the sub-eval is 'accept'. There is no explicit want_total<=give_total guard befo
  - files: src/catanbot/recommender.py:2149-2167 + 2288-2293 vs extension/lib/trades.js:266-278


## Needs-design (product decision or large port)

### boards-and-bridge-only

- [ ] **[medium]** variant label + variant_recs_disabled guard + scan_eligible (non-classic warning banner / rec suppression)
  - bridge: sess.variant_label() (colonist_diff.py:425-475) classifies classic/twirl/scanned/black_forest/volcano/'variant: ...'; bridge.py:1698-1705 suppresses opening recs (variant_recs_disabled=True) for unsafe variants, bridge.py:2038-2042 emits sn
  - standalone: absent. The standalone snapshot (index.js:50-93) emits none of variant, variant_recs_disabled, game_settings, scan_eligible, or map_setting. grep of extension/lib finds no non_classic / variant-warning logic. The standalone silently scores 
  - note: Requires porting variant_label classification (gameSettings flag table + non-classic tile-type detection) plus a product decision on which variants the standalone trusts; colonist_diff.py:425-475 is a sizeable classifier and the scan allow-
  - files: colonist_diff.py:425-475, bridge.py:1698-1705,:2038-2042,:2081-2093 / index.js buildStandaloneSnapshot
- [ ] **[medium]** End-of-game postmortem report (auto-open replay analysis)
  - bridge: report.py build_report (report.py:229-470) aggregates the parsed Event stream + DispatchResults + color_map + final VP into a ReplayReport (per-player stats, trade impacts, seven impacts, dev-card timeline, move annotations) and format_repo
  - standalone: absent. index.js:87 hardcodes latest_postmortem:{seq:0, available:false, written_at:0}; there is no JS report builder in extension/lib. The standalone never produces a postmortem.
  - note: Feasible in pure JS+public-info: build_report (report.py:229-470) is pure aggregation over the same Event stream the standalone already parses (no Game.copy, no forward search) plus _score_trade_delta/_score_monopoly_haul heuristics, all po
  - files: report.py:229-470 build_report, bridge_postmortem.py / index.js:87 (latest_postmortem stub)

### dev-card-hints

- [ ] **[medium]** Road Building fog-reveal PLAY trigger
  - bridge: _compute_rb_hint:990-1000 calls _free_road_reaches_fog (:846-889): on fog boards (Black Forest / Gold Rush) it counts distinct unrevealed fog tiles reachable by 1-2 free roads and, if >0, sets should_play=True with reason `reveals fog · {n}
  - standalone: absent   hints.js has no fog awareness at all and state.js carries no fog_node_ids. The fog-reveal PLAY verdict and its reason never fire in the standalone.
  - note: Fog tile/corner geometry is public (visible on the DOM board) but the standalone state model has no fog_node_ids and no notion of unrevealed fog hexes; porting requires adding fog board parsing + a reachability pass.
  - files: bridge: src/catanbot/bridge_hints.py:846-1000; JS: extension/lib/hints.js _rbHintImpl (no fog code), extension/lib/state.js (no fog field).
- [ ] **[medium]** Road Building mid-play 'PLACE   N free roads left' verdict and free_roads_pending telemetry
  - bridge: _compute_rb_hint:919-1028 reads state.free_roads_available; while >0 (card already played) it keeps the hint up, sets should_play=True and reason `place {N} free road(s)`, and ships free_roads_pending in the snap so the HUD reads PLACE not 
  - standalone: absent   hints.js rbHint/_rbHintImpl never tracks free roads pending; it only emits the pre-play PLAY/HOLD verdict and self_len/opp_len/edges. No free_roads_pending field, and the banner would vanish the instant road #1 is placed.
  - note: Requires the standalone to detect RB-in-progress and the count of free roads remaining from the WS/DOM (colonist signals this), then thread a freeRoadsPending field into state   a state-model addition, not pure logic.
  - files: bridge: src/catanbot/bridge_hints.py:923-1028; JS: extension/lib/hints.js:221-340, extension/lib/state.js (no freeRoadsPending).
- [ ] **[medium]** Knight just-bought-this-turn exclusion and non_vp_held drift cap
  - bridge: _compute_knight_hint:1581-1608 subtracts KNIGHT-type cards bought this turn (sess.self_dev_bought_this_turn, type int 11) so a just-bought knight never recommends PLAY, and hard-caps playable_knights by snap.dev_cards_non_vp_held (returns N
  - standalone: knightHint:62-65 reads have = own.KNIGHT directly from devCardsByType with no this-turn-purchase subtraction and no authoritative non-VP cap. hints.js header (lines 14-18) explicitly notes the standalone errs toward showing the hint and doe
  - note: colonist does ship developmentCardsBoughtThisTurn (the bridge consumes it), so this is public; porting needs that field threaded into the standalone state model plus a non-VP authoritative count, both currently absent.
  - files: bridge: src/catanbot/bridge_hints.py:1581-1608; JS: extension/lib/hints.js:62-65, header note 14-18; extension/lib/state.js devCardsByType (no bought-this-turn list).
- [ ] **[medium]** YoP piece-supply guard (don't suggest a build you can't place)
  - bridge: _compute_yop_hint:488-511 reads SETTLEMENTS/CITIES/ROADS_AVAILABLE and skips any unlock target whose piece supply is exhausted (placeable map), fixing the 'unlocks settlement at 5 settles placed' no-op bug (2026-05-02).
  - standalone: yopHint:177-204 iterates a fixed targets list [city,settlement,dev_card,road] with no piece-supply check, so it can recommend 'unlocks settlement' when self has 0 settlements left to place.
  - note: Remaining piece counts per player are public (colonist mechanic state / visible board), but the standalone state model carries no settlements/cities/roads-available counts; needs a state-field addition.
  - files: bridge: src/catanbot/bridge_hints.py:488-511; JS: extension/lib/hints.js:177-204; extension/lib/state.js (no piece-supply fields).
- [ ] **[medium]** YoP bank-supply guard (bank_ok / 'bank short on X')
  - bridge: _compute_yop_hint:577-601 checks bank_supply['remaining']; if the bank is out of either picked resource it sets bank_ok=False, flips should_play to False, and rewrites reason to `bank short on {res}`. Ships bank_ok in the hint.
  - standalone: yopHint:212-218 always returns should_play=true with reason `unlocks {target}` when a pair is found, never emits bank_ok, and never checks bank remaining   it can recommend PLAY for a resource the bank is empty of.
  - note: Bank remaining per resource is public (derivable from initial 19 minus all known holdings/board), but the standalone has no bank-supply remaining model wired into hints; needs that derived count threaded in.
  - files: bridge: src/catanbot/bridge_hints.py:577-601; JS: extension/lib/hints.js:205-219; extension/lib/state.js (no bank remaining).
- [ ] **[medium]** Monopoly per-opp inference, top_holder spotlight, totals, and physical/WS caps
  - bridge: _compute_monopoly_hint:281-453 builds per-opp counts (from snap opp_hands, capped by WS-authoritative opp_card_totals and by physical bank max 19-bank-self), ranks resources by (count, unlock, _MONOPOLY_RES_WEIGHT ORE/WHEAT 5, BRICK/WOOD 3,
  - standalone: monopolyHint:108-166 estimates opp holdings with _estOppResource (splits each opp's known total across resources by production weight), uses a different tie-break weight (WHEAT 1.05, ORE 1.03, else 1.00, +0.5 unlock), caps only at a flat 19
  - note: The per-opp WS-authoritative totals and bank-remaining caps depend on state the standalone doesn't track (opp_card_totals, bank_supply); the production-weighted estimate is the standalone's documented public-info substitute, so exact per-op
  - files: bridge: src/catanbot/bridge_hints.py:281-453; JS: extension/lib/hints.js:29-166.
- [ ] **[low]** Monopoly unlock piece-supply guard
  - bridge: _compute_monopoly_hint:376-402 applies the same placeable settlement/city/road supply guard as YoP before naming unlock_reason, so it won't say 'unlocks settlement' when self has 0 settlements left.
  - standalone: monopolyHint:124-128 / 151-152 computes unlock purely from handCanAfford with no piece-supply check, so it can claim an unlock for a build self cannot place.
  - note: Same missing piece-supply state as the YoP guard.
  - files: bridge: src/catanbot/bridge_hints.py:376-402; JS: extension/lib/hints.js:124-152; extension/lib/state.js (no piece-supply fields).

### events-state

- [ ] **[medium]** Variant-board detection (non_classic_tiles + variant_label + scan/whitelist gating)
  - bridge: from_game_start sweeps mapping.tile_types and records any non-classic tile int in non_classic_tiles, and stores the full variant flag set (gameType/modeSetting/extensionSetting/scenarioSetting/mapSetting) in game_settings (colonist_diff.py:
  - standalone: absent   events.js captures none of these. _WANTED_KEYS includes tileHexStates but only to set state.started (events.js:721-724); it never records non-classic tile ints, never stores the variant flags (only vpTarget/discardLimit), and there
  - note: Large port plus a product decision on which variant labels gate recs and how to surface the warning when running standalone.
  - files: src/catanbot/colonist_diff.py:331-339,412-475 / extension/lib/events.js tileHexStates handling (721-724) + gameSettings (231-241)
- [ ] **[low]** Black Forest fog-tile reveal (TileRevealEvent) updating board topology
  - bridge: events_from_diff parses tileHexStates fog->real transitions and emits TileRevealEvent that rewrites mapping.tile_types/tile_dice and the catanatron map (colonist_diff.py:643-672; applied via tracker.reveal_tile in live.py:283-289), so produ
  - standalone: absent in events.js   tileHexStates is read only to flip state.started (events.js:721-724). There is no fog-reveal handler that updates the JS board's tile resource/number from a WS hex diff. (board.js may parse hexes at GameStart, but mid-
  - note: Requires the JS board (board.js topology) to support in-place tile mutation mid-game and re-derivation of pip/production; a non-trivial board-model change, plus only matters on Black Forest/Volcano maps.
  - files: src/catanbot/colonist_diff.py:643-672 / extension/lib/events.js tileHexStates (721-724)

### recommender

- [ ] **[high]** Dev-card buy score curve
  - bridge: Flat _DEV_CARD_SCORE = 3.0 (recommender.py:79, 1489) for the now-rec, with a single phase/endgame adjustment applied later (halved or dropped near win, recommender.py:1980-1984).
  - standalone: _devCardRec (recommender.js:496-515) computes a variable base of 4.5, +0.5 if totalRolls>=8, +1.5 if nothing else is affordable, -0.5 if self holds Largest Army, clamped to [2.5, 8.0]. So the standalone dev-card rec routinely scores 4.5-6.5
  - note: All inputs (total rolls, affordability, who holds army) are public info, so it is portable   but it is a deliberate product divergence (a richer curve) that someone must decide to reconcile to the bridge's flat 3.0 or vice-versa.
  - files: bridge src/catanbot/recommender.py:79,1485-1491; JS extension/lib/recommender.js:496-515 (_devCardRec)
- [ ] **[high]** propose_trade variants and reserved-across logic
  - bridge: For a build short by exactly 1 card, emits multiple variants: '1:1 fair' (adj 0.0), '2:1 concede' (adj -0.6), and '2:2 even' (adj -0.2) when need>=2, scored base_score-0.3+adj (recommender.py:1767-1827). Adds a separate 'rebalance' proactiv
  - standalone: _proposeTradeRecs (recommender.js:527-589) only ever emits a single '1:1 fair' variant per blocked build (no 2:1 concede, no 2:2 even, no rebalance trade), with a fixed per-target base score (city 6.5 / settle 6.0 / road 4.5 / dev 4.0) rath
  - note: opp_hands/bank/board are public and the JS already receives opp hand estimates, so the variant ladder and rebalance trade are portable in principle; but the JS scoring model and target set were deliberately simplified, so reconciling is a p
  - files: bridge src/catanbot/recommender.py:1654-1928; JS extension/lib/recommender.js:527-589 (_proposeTradeRecs)
- [ ] **[medium]** opening_settlement recs (round-1/round-2 plan, archetype, score curve, road followup) emitted from recommendActions path
  - bridge: recommend_opening (recommender.py:165-472) emits opening_settlement recs with _score_opening / _score_second_settle curves, detail strings ('+X/roll · adds 🌾 · covers 3/5 · 2nd pick'), plan.second + archetype labels (_label_archetype, recom
  - standalone: recommendActions (recommender.js) emits no opening_settlement recs at all; the standalone opening path is the separate scoreOpeningNodes/scoreSecondSettlements/bestOpeningRoad in advisor.js, which return raw NodeScore objects, not the bridg
  - note: All inputs are public board info and the JS already ports the underlying scorers, but wrapping them into the bridge's opening_settlement rec shape (score curve, plan/archetype, road-followup gating) is a substantial port and a product decis
  - files: bridge src/catanbot/recommender.py:165-472 (recommend_opening), 82-93 (score curves), 114-162 (_label_archetype); JS extension/lib/advisor.js:104-182 (scoreOpeningNodes)   no opening_settlement emitte

### robber

- [ ] **[low]** victim pill metadata: real colonist username/color vs synthetic color-id table; is_placeholder flag
  - bridge: Per-victim fields use the reverse catanatron-color->username map and the live display color CSS, plus is_placeholder = sess.is_placeholder_username(...) so the panel can render a 'P{N}' initial and streamer-mode can anonymize (bridge_robber
  - standalone: recommendRobberTargets hardcodes COLONIST_COLOR_NAME/COLONIST_COLOR_HEX keyed by color id 1-6 (recommender.js:792-800) and sets username=color=COLOR_NAME, color_css=fixed hex, with no is_placeholder field. So victim pills show generic color
  - files: src/catanbot/bridge_robber.py:254-272; extension/lib/recommender.js:792-818. Real usernames are public (colonist player roster) and the panel already has _bestUsernameFor/_colorName helpers, but threa

### snapshot-fields

- [ ] **[medium]** eval_history per-roll sparkline samples
  - bridge: bridge.py:1490 ships `eval_history` (list of {roll, eval}) sampled once per roll from catanbot.eval.evaluate_state (own − 0.8*max_opp) in _track_overlay_state (bridge.py:953-967). renderEvalGraph (panel.js:5524) draws the signed SVG sparkli
  - standalone: absent; the inline standalone snap (panel.js:2090-2299) never sets eval_history, so renderEvalGraph hides the eval graph permanently in no-bridge mode.
  - note: evaluate_state is a pure heuristic over public board state (VP, production, pieces) so it is reproducible in JS, but there is no JS port of catanbot/eval.py and the standalone keeps no per-roll eval ring-buffer; porting the evaluator plus a
  - files: bridge.py:1490, 953-967; eval.py evaluate_state; JS panel.js standalone snap ~2090 (no eval_history); renderEvalGraph panel.js:5524
- [ ] **[low]** Per-opp played_dev (public per-type played dev-card counts)
  - bridge: Each opp row carries `played_dev` = _played_dev_by_type(game, c) (bridge.py:2362)   public per-type tally of dev cards that opp has revealed by playing (added in commit bdb4a8a).
  - standalone: absent from oppsBlock (panel.js:1266-1285); the standalone tracks per-opp knights_played (mechanicKnightState) but not the full per-type played-dev breakdown. No opp-row renderer consumes played_dev directly yet, but it is part of the commi
  - note: Knights are observable in standalone (mechanicKnightState ships knightsPlayed per color), but monopoly/YoP/RB plays are seen only via DOM chat-log parsing, which the standalone aggregates globally (_chatDevPlays) rather than per-opponent. A
  - files: bridge.py:2362 + bridge_economy._played_dev_by_type; JS oppsBlock panel.js:1266-1285 (no played_dev)
- [ ] **[low]** Variant / scan-map / postmortem scaffolding fields
  - bridge: bridge.py ships players_unsupported (1573), variant + game_settings (2038-2042), scan_eligible/map_setting (2081-2093), variant_recs_disabled (1705/2181), ws_frames/log_events (1474-1475), dev_cards_played_by_type (2493), dev_cards_just_bou
  - standalone: Mostly absent: standalone omits players_unsupported, variant/game_settings, scan_eligible/map_setting, variant_recs_disabled, ws_frames/log_events, dev_cards_played_by_type, dev_cards_just_bought_authoritative, and ships latest_postmortem a
  - note: variant_label/scan-eligibility depend on parsing colonist gameSettings flags the standalone state container does not retain; latest_postmortem is inherently bridge-only (postmortem HTML is written server-side to disk and served via GET /pos
  - files: bridge.py:1573,2038-2093,2452-2496,2844-2853; JS panel.js standalone snap ~2090-2299; renderers 3974/3998/2274

### strategy

- [ ] **[high]** RB_CARVED_TILES (Road Builder) archetype scoring and gating
  - bridge: _score_rb_carved (strategy_select.py:371-407) gates RB on >=4 opponent footprints, a natural corridor anchor (desert/board-edge/4+ weak-number tiles in the 2-hop ring, :410-445), and isolation>=0.5; final score = (iso-0.5)*2 from _isolation
  - standalone: _scoreRBCarvedTiles (strategy.js:199-237) is an explicitly slim proxy (header :21-23): distinct numbered tiles within 2 hops minus 1 per opp building in that ring, score = clamp(reach/8). No footprint gate, no corridor anchor, no (iso-0.5)*
  - note: Reproducible from public state.map/state.buildings, but porting the corridor anchor + isolation transform + >=4-footprint gate is sizable and a product decision vs keeping the simpler proxy.
  - files: bridge src/catanbot/strategy_select.py:371-445,188-234; JS extension/lib/strategy.js:199-237
- [ ] **[medium]** Dev-card-drawn pivot triggers (road_builder_drawn, monopoly_drawn) and the LR_RUSH override
  - bridge: _detect_dev_card_drawn (strategy_select.py:703-733) fires road_builder_drawn with override_tag=LR_RUSH and monopoly_drawn (no override) when self just bought those dev types this turn. merge_triggers_into_tag (:847-872) folds the first over
  - standalone: absent. _detectPivots (strategy.js:344-355) runs only hot_number/opp_close_to_la/opp_close_to_win/seven_overdue, and every JS trigger hard-codes override_tag:null (:291,308,324,341), so computeStrategy never sets override_tag (loop :402-408
  - note: Portable in principle (state.devCardsByType exists, state.js:68-71) but needs a per-turn just-bought delta threaded from events.js into computeStrategy plus the override flip wired through; modest port and a product call, all public WS data
  - files: bridge src/catanbot/strategy_select.py:703-733,847-872; JS extension/lib/strategy.js:344-355,362-425
- [ ] **[medium]** Pre-placement board-affinity ranking method
  - bridge: compute_board_affinity (strategy_select.py:461-530) takes top-15 opening candidates, enumerates non-conflicting 2-node pairs, scores each archetype against each pair's real combined production, and takes the per-archetype MAX across pairs.
  - standalone: computeStrategy pre-placement uses _boardProd (strategy.js:82-101): sum every tile pip/36 over the whole board, divide each resource by a tuned 3 as a pair proxy, score once. Structurally different, so the preview board-affinity ranking (pa
  - note: Public board data only and an opening ranking exists in JS (panel.js:1369-1378), but porting pair-enumeration + per-archetype-max is sizable and a product call.
  - files: bridge src/catanbot/strategy_select.py:461-530; JS extension/lib/strategy.js:82-101,362-396


## Hard-blocked (impossible in JS under public-info-only)

### boards-and-bridge-only

- [ ] **[medium]** 1-ply search rerank (search_delta column + reordering of recs)
  - bridge: eval.py search_rerank (eval.py:248-299) copies the game (game.copy()), forces our play turn (generate_playable_actions, eval.py:215-245), executes each candidate Action (state.execute), evaluates the resulting state (evaluate_state, eval.py
  - standalone: absent by design. recommender.js:16-19 states it deliberately skips 1-ply rerank; recs ship with an empty search_delta column and are ordered by heuristic score only, calibrated so the heuristic top pick matches the bridge's heuristic top p
  - note: search_rerank requires catanatron's Game.copy() + state.execute(action) + generate_playable_actions forward simulator (eval.py:215-299). The standalone has no catanatron and no rules simulator, only a static board graph + tracker, so it can
  - files: eval.py:248-299 / recommender.js:16-19 (rerank intentionally omitted)

### recommender

- [ ] **[medium]** 1-ply search rerank (search_delta column + bucket ordering)
  - bridge: recommend_actions calls search_rerank(game, c, recs) (recommender.py:1936-1937) which simulates each affordable build on a Game.copy() and reorders recs into searched-now -> other-now -> soon buckets, stamping a search_delta on the searched
  - standalone: recommendActions (recommender.js:680-722) never reranks by search; the file header (recommender.js:16-19) explicitly states it skips 1-ply search because it depends on catanatron Game.copy(). Final sort is a plain when-then-score sort (reco
  - note: The bridge rerank runs a real forward simulation via catanatron Game.copy() + state evaluator (eval.py). There is no catanatron engine in JS, so a true 1-ply lookahead over the full game state cannot be reproduced. This is documented as int
  - files: bridge src/catanbot/recommender.py:1936-1937, 1998-2005; JS extension/lib/recommender.js:680-722 (recommendActions)
- [ ] **[low]** dev-deck-empty gating of dev_card and bank/propose dev trades
  - bridge: recommend_actions takes dev_deck_remaining; when the deck is known empty it suppresses the dev_card now-rec and soon-plan (recommender.py:1483-1491,1538-1547), the dev_card bank trade (recommender.py:1604-1605), and the dev_card propose tra
  - standalone: No dev-deck gate anywhere. The JS bank-trade comment is explicit (recommender.js:447-449): colonist hides the deck count so the gate is intentionally absent. _devCardRec always emits, and _bankTradeRecs always considers the dev_card target.
  - note: colonist masks the remaining dev-deck count (rules-hidden info); the standalone has no public source for dev_deck_remaining, so it genuinely cannot reproduce this gate. Acknowledged in the JS comment.
  - files: bridge src/catanbot/recommender.py:1483-1485,1604-1605,1695-1696; JS extension/lib/recommender.js:447-449 (_bankTradeRecs comment), 496-515 (_devCardRec)

### trades

- [ ] **[high]** before/after EV uses 1-ply search_rerank in the bridge but raw heuristic scores in JS
  - bridge: evaluate_incoming_trade (recommender.py:2238-2252) calls _best_now_rec -> recommend_actions, which runs search_rerank (recommender.py:1936-1937, eval.py:248 search_rerank) doing a catanatron Game.copy() 1-ply forward search that can change 
  - standalone: evaluateIncomingTrade (trades.js:222-237) calls opts.recommend = recommendActions, which has NO rerank (recommender.js:16-17, 680-722 assemble + sort only; advisor.js:3-4, bridge_probe.js:8 confirm 1-ply search is bridge-only). before/after
  - note: search_rerank requires catanatron Game.copy() to simulate executing each affordable build and re-score the resulting state. That forward-search engine does not exist in JS under public-info-only; the standalone explicitly documents it as br
  - files: src/catanbot/recommender.py:1936-1937 + eval.py:248 vs extension/lib/trades.js:222-237 (bestNow) / recommender.js:680-722 (recommendActions, no rerank)
- [ ] **[low]** bank_trade dev_card target suppressed when dev deck empty (bridge only)
  - bridge: Bank-trade loop skips the dev_card target when dev_deck_empty (recommender.py:1604-1605, mirrored in propose at 1695-1696), so it never recommends trading toward a dev card that can't be bought.
  - standalone: _bankTradeRecs (recommender.js:461-471) has no dev-deck-empty gate; the docstring at recommender.js:446-449 calls this out as intentional because colonist hides the public deck count. So the standalone can recommend a port trade to buy a de
  - note: Colonist masks the dev-deck remaining count from the client; the bridge derives dev_deck_empty from catanatron's tracked deck state. Under public-info-only the standalone cannot know the deck is empty (it can only infer played cards, not un
  - files: src/catanbot/recommender.py:1604-1605 vs extension/lib/recommender.js:461-471 (+446-449 rationale)
