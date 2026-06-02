// Node-based regression tests for the standalone knight-robber WS
// backstop (events.js). Run with: node --test tests/js/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applySnapshot } from '../../extension/lib/events.js';
import { newGameState } from '../../extension/lib/state.js';

function frame(currentTurn, selfKnights) {
  return {
    currentState: { currentTurnPlayerColor: currentTurn },
    playerStates: {
      1: { mechanicKnightState: { knightsPlayed: selfKnights } },
    },
  };
}

test('a self knight played on our turn sets the WS robber backstop', () => {
  const st = newGameState();
  st.selfColorId = 1;
  st.currentTurn = 1;
  st.playedKnights['1'] = 2; // established baseline
  applySnapshot(st, frame(1, 3)); // 2 -> 3, exactly +1, our turn
  assert.equal(st.knightRobberPending, true);
  assert.equal(st.knightRobberTurn, 1);
});

test('a mid-game join (0 -> N in one sync) does not false-fire', () => {
  const st = newGameState();
  st.selfColorId = 1;
  st.currentTurn = 1;
  applySnapshot(st, frame(1, 3)); // 0 -> 3 in one frame: not a +1
  assert.equal(!!st.knightRobberPending, false);
});

test('the knight window clears on the next turn change', () => {
  const st = newGameState();
  st.selfColorId = 1;
  st.currentTurn = 1;
  applySnapshot(st, frame(1, 1)); // fresh game: 0 -> 1, our turn -> fires
  assert.equal(st.knightRobberPending, true);
  applySnapshot(st, frame(2, 1)); // turn passes to an opponent
  assert.equal(st.knightRobberPending, false);
  assert.equal(st.knightRobberTurn, null);
});
