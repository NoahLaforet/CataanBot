# V3 orchestration plan: Opus 4.8 drives, Fable 5 on call

Companion to `docs/INPAGE_HUD_V3.md` (the WHAT). This file is the HOW:
how an Opus 4.8 session should orchestrate the v3 work the way a Fable 5
session would, and exactly when to stop and pull Fable 5 in. Written because
Fable 5 usage limits are the binding constraint; Opus time is cheap by
comparison and most of v3 is well-specified execution, not frontier judgment.

## Roles

- **Opus 4.8** - the session driver. Runs every stage below end to end,
  orchestrates subagents/workflows, does the live browser work, commits.
- **Haiku 4.5 / Sonnet 5** - subagent grunt work inside workflows (mechanical
  sweeps, file reads, screenshot description, test triage). Use the workflow
  `model`/`effort` opts to downgrade; never downgrade the verify/judge stages.
- **Fable 5** - surgical escalation ONLY, at the gates defined below. The
  goal is single-digit Fable turns per gate: Opus prepares an evidence
  packet first, Fable decides, Opus implements the decision.

## Posture (how Fable would run this - Opus should copy it)

- Autonomous: do not ask permission for reversible steps; stop only for
  destructive actions or scope changes. Noah is usually away.
- Verify before claiming done: node --check, the runnable pytest subset,
  and a live screenshot for anything visual. "Tests pass" means you ran them.
- Fan out where work is independent (bridge lane vs extension lane), stay
  serial where it is not (live calibration is one browser, one game).
- Commit as you go, real human messages, Noah sole author, NO AI
  attribution, no em-dashes, no /Users/<name> paths (public repo). Push at
  breakpoints. Sync the dev mirror after every extension change.
- When stuck twice on the same failure, stop grinding: that is a Fable gate
  (F3), not a third attempt.

## The Fable protocol (token-cheap escalation)

1. Opus hits a gate. It writes a packet to `~/Desktop/catanbot-fable-packet.md`
   (OUTSIDE the repo - packets carry local paths and screenshots). Contents,
   in under ~150 lines: one-paragraph context; the exact decision needed;
   evidence (measurement table, screenshot file paths, code anchors); what
   was already tried; 2-3 candidate options with tradeoffs; the constraint
   list (a wrong board marker is worse than none, native style bar, etc).
2. Noah switches the session to Fable (`/model fable5`) or opens a fresh
   Fable session and says: "Read ~/Desktop/catanbot-fable-packet.md and
   decide. Write the decision + reasoning back into the packet, then stop."
3. Switch back to Opus, which implements the decision from the packet.

Keep Fable turns to deciding and (rarely) writing the one hardest function.
Everything readable, runnable, or mechanical stays on Opus.

## Stages, with model tags

### Stage 0 - preflight (OPUS, no gates)
Read `docs/INPAGE_HUD_V3.md` fully. Confirm: main up to date with origin,
dev mirror in sync, bridge launchable, the 9 corrupt test files excluded
from any pytest invocation. Nothing here needs judgment.

### Stage 1 - placement visuals + de-abbreviation (OPUS, no gates)
Fully specified in V3 stage 1, including file:line anchors and the decided
design (recommender.py attaches `board_pos`/`board_edge` fractional cube
coords at the source; loghud.js draws circle + segment in updateBoardOverlay
under the existing zoom-safety gates). Suggested shape: two parallel lanes
(bridge lane: coords helper + rec attachment + "prob" de-abbrev + tests;
extension lane: overlay drawing + styles + node --check), then an
integration check on the /advisor JSON before the single bridge restart.
A workflow is optional; two Agent-tool lanes are enough. Downgradeable:
the de-abbreviation sweep and test-writing (Sonnet-tier). NOT downgradeable:
the coord math helper (verify the LandTile coord caveat from V3 first).
Escalate only via F3 if something loops.

### Stage 2 - zoom/pan tracking (OPUS measures, FABLE GATE F2 decides)
Opus runs the entire live calibration protocol from V3 stage 2 itself
(bot game via claude-in-chrome, wheel ticks, drag tests, clamp probing,
resize, trackpad events) and records a measurement table: scale factor per
tick, anchor point, clamp bounds, pan ratio, event shapes seen.

- **Clean results** (consistent multiplicative factor, stable anchor,
  detectable clamps, 1:1 pan or no pan): Opus implements the decided
  transform shape (running similarity transform applied in
  boardCoordToPixel, hide-fallback on any low-confidence event) WITHOUT
  Fable. The design is already chosen; clean data makes it execution.
- **GATE F2 - messy results** (smoothing/inertia, cursor-vs-center
  inconsistency, undetectable clamps, fractional pinch that does not map):
  STOP. Do not improvise a transform. Packet up the measurements +
  screenshots and let Fable pick: full reconstruction / partial (for
  example pan-only tracking + zoom hide) / hardened hide gate only. This is
  the one place in v3 where the wrong call ships misaligned markers, which
  is the explicit worst outcome.

Regardless of branch: add drag detection to the hide gate (the current
pan hole) - that part is unconditional and Opus-only.

### Stage 3 - live verification runsheet (OPUS, GATE F3 on loops)
Run the V3 stage 3 runsheet by screenshot in a bot game. Selector rot or
a cue misfiring gets two independent fix attempts (different hypotheses,
not retries); a third failure is GATE F3: packet with DOM captures +
screenshots, Fable diagnoses, Opus patches. Screenshot description and
DOM-capture summarization are downgradeable to Haiku subagents.

### Stage 4 - ship 0.52.0 (OPUS, no gates)
Version bump across the four sync points, CHANGELOG, full runnable suite,
fold or delete the stale v0.51.0 draft release, Mac zip + Windows CI per
V3 stage 4. Mechanical. Signing needs Noah's machine unlocked; ask once.

## Gate summary (the answer to "when does Fable 5 get used")

| Gate | Trigger | Fable's job | Expected cost |
|------|---------|-------------|---------------|
| F2 | Zoom calibration data is messy/inconsistent | Pick reconstruction vs partial vs hide-only; design the transform if reconstruction | 1-3 turns |
| F3 | Same failure survives 2 distinct fix attempts, anywhere | Diagnose from packet, prescribe the fix | 1-2 turns |
| (none) | Stages 0, 1, 4, and clean-data stage 2 | - | 0 |

If neither gate fires, v3 ships entirely on Opus 4.8.

## Kickoff prompt for the Opus 4.8 session

"Read docs/INPAGE_HUD_V3_ORCHESTRATION.md and docs/INPAGE_HUD_V3.md in the
CatanBot repo and execute the plan as the Opus driver, stage 0 through 4,
following the posture section. Respect the Fable gates: if F2 or F3 fires,
write the packet to ~/Desktop/catanbot-fable-packet.md and stop with a clear
message telling me to switch models. I will reload the unpacked extension
and approve the bridge restart when you ask."
