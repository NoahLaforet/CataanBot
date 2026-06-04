// Standalone Friendly Robber WS-latch parity (the no-bridge path).
// Run with:  node --test tests/js/
//
// The bridge latches friendly_robber_active from the WS gameSettings;
// the standalone now reads gameSettings.friendlyRobber into state so the
// robber ranker can drop protected (<=2 VP) victims authoritatively,
// instead of relying only on the chat path.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applySnapshot } from '../../extension/lib/events.js';
import { newGameState } from '../../extension/lib/state.js';

test('friendlyRobber is latched true from WS gameSettings', () => {
  const st = newGameState();
  assert.equal(st.friendlyRobber, false, 'defaults off');
  applySnapshot(st, {
    gameSettings: { friendlyRobber: true, victoryPointsToWin: 10 },
  });
  assert.equal(st.friendlyRobber, true);
});

test('a non-friendly-robber game leaves the flag false', () => {
  const st = newGameState();
  applySnapshot(st, { gameSettings: { friendlyRobber: false } });
  assert.equal(st.friendlyRobber, false);
});
