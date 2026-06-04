# Extension to bridge parity tracker

Live audit of what the standalone JS recommender (`extension/lib/`, the
no-bridge path) produces vs the Python bridge (`src/catanbot/`, the
source of truth). The bridge is primary and supported; the standalone
mirrors its heuristics so the HUD works with zero local install, and
when a bridge is reachable the advanced panels light up on top.

Last refresh: 2026-06-03 (v0.44.1 parity catch-up, in progress).

## How the two engines relate

- The Chrome side panel polls the local bridge at `127.0.0.1:8765`.
  When it answers, the panel renders the bridge snapshot.
- When no bridge answers (after the `bridge_probe` fail streak), the
  panel builds the snapshot itself from `extension/lib/` and renders
  the same shape, tagged `_source: 'standalone'` and labelled
  "experimental, reduced accuracy".
- The standalone uses only public information (the colonist DOM log and
  the WS frames colonist sends to the client). It never reads
  rules-hidden state. Opponent hands are chat-inferred totals plus
  drift, never the bridge's authoritative per-resource tracker.

## At parity (standalone mirrors the bridge)

- Opening picks: 1st and 2nd settlement ranking with the follow-up road
  (`scoreOpeningNodes`, `scoreSecondSettlements`, `bestOpeningRoad`).
- In-game recommender: settlement / city / road / dev card / bank trade
  / propose trade, scored on the same 1 to 10 curve
  (`recommendActions`).
- Bank/port trade planning and scoring: greedy cheapest-rate plan with
  2:1 / 3:1 / 4:1 sell rates, scored from the unlocked build's
  production curve minus one, one rec in settlement > city > dev_card
  order (`planBankTrades`, `_bankTradeRecs`).
- Road scoring: best unblocked distance-2 landing (raw production times
  diversity times a 2:1-port bonus), blocked landings excluded
  (`_bestLanding`).
- Phase and archetype bumps: third-settle x1.25 at two footprints,
  endgame +2.5 / +1.5 VP push, and the OWS / LR_RUSH / PORT_TRADE /
  RB_CARVED_TILES nudges (`_applyPhaseBumps` + `computeStrategy`).
- Dev-card play hints: Knight / Monopoly / Year of Plenty / Road
  Building PLAY/HOLD verdicts (`knightHint`, `monopolyHint`, `yopHint`,
  `rbHint`).
- Robber target ranking, friendly-robber aware
  (`recommendRobberTargets`).
- Incoming-trade evaluation: EV from the best-now rec before vs after,
  build-kind upgrade/downgrade, close-to-win and lopsided guards, plus a
  rebalanced counter (`evaluateIncomingTrade`).
- Strategy archetype banner + ranking + pivot triggers
  (`computeStrategy`).
- Move-quality grading, roll histogram, opponent production and
  played-dev counts, bank supply, win-this-turn callout, threat and
  win-proximity banners.

## Bridge-only by design (the standalone does not attempt these)

- `eval_history` and the 1-ply state-eval search rerank: needs
  catanatron `Game.copy()`. No JS equivalent.
- `strategic_options`: multi-archetype alt-plan ranking with heavy
  lookahead. The flat rec list plus the game-plan banner cover the live
  HUD use case.
- `latest_postmortem`: the bridge renders an HTML report to disk; the
  extension has no filesystem.
- Variant boards (Pond, Twirl, Gold Rush fog, Volcano): the bridge
  builds a fresh catanatron map from colonist's tile/edge data. The
  standalone is classic-only and falls back to bridge mode on variants.
- Authoritative opponent hand tracking: the bridge runs a hand tracker
  with drift; the standalone has chat-inferred totals only.

## v0.44.x parity catch-up (in progress)

The two bridge commits since v0.43.0 (`38e6546`, `6837c1f`) were audited
against `extension/lib/`:

- **Monopoly tiny-pot hold: closed.** The bridge stopped recommending PLAY
  to steal a 1-card pot even when it unlocks a build (a one-shot card is
  wasted on 1 card). Ported to `monopolyHint`: PLAY now needs an estimated
  2+ cards on an unlock and 4+ for a no-unlock tempo swing, else HOLD with
  "small pot . save it". Locked by `tests/js/hints.monopoly.test.mjs`.
- **Knight played-count double-count: not applicable to the standalone.**
  The bridge bug came from catanatron's `PLAYED_KNIGHT` being incremented
  by two event sources (DOM-log parser + WS), which the bridge now snaps to
  `mechanicKnightState.knightsPlayed`. The standalone already reads that
  authoritative count as an absolute value (`events.js`, the
  `state.playedKnights[key] = k` write) and never had an incremental path,
  so both engines land on colonist's number. No port needed.
- **Road Building expansion naming: open.** The bridge HOLD reason now names
  the settle spot two free roads would open ("hold for LR . or play to open
  a settle spot (...)"). The standalone `rbHint` is structurally different
  (it already gives actionable reasons and uses different should_play
  semantics) and is classic-only, so this is tracked as an open gap for the
  full rec/hint parity pass, not a one-line port.

## Closed in v0.43.0 (Phase 1 standalone parity)

- Bank/port trade scoring moved from flat base scores to the unlocked
  build's production curve minus one, in the bridge's target order with
  a single rec.
- Road best-landing now scores the distance-2 settleable spot and
  excludes blocked landings, instead of halving the distance-1 endpoint
  (road scores no longer run low).
- Third-settle, endgame, and per-archetype score bumps applied in the
  recommender (were bridge-only).
- Incoming-trade evaluation ported from a hand-rolled inline check to
  the shared `evaluateIncomingTrade`.
- WS frame parsing walks each frame once (`_collectKeys`) instead of
  ~17 independent tree searches.

## Known standalone gaps (accepted, public-info-only limits)

- No dev-deck-empty gate on the bank-trade-for-dev-card rec: colonist
  hides the deck count from a public-info advisor, so the standalone
  cannot suppress that rec when the deck is exhausted.
- Road landing omits the fog-reveal bonus: fog boards are bridge-only.
- `Math.round` (round half up) vs Python's round half to even: only
  diverges on exact x.5 boundaries, never reached at a 10 or 12 VP
  target.
- `bankSupply` is not gated into `recommendActions`: the standalone
  estimate is chat-inferred and best-effort, so the bank-empty gate
  stays off (matching the bridge when the bank is unconstrained).
