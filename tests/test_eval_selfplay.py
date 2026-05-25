"""Self-play guard: the eval must keep beating random bots handily.

evaluate_state drives both the self-play player and the live HUD's
search_rerank, so a regression that quietly weakens it (a bad weight, a
broken term) is otherwise invisible to the suite. This pins a wide margin
over chance on a small, fast game count — loose enough not to flake on
dice variance, tight enough to catch a real break.
"""
from __future__ import annotations

from catanatron import Color, Game, Player, RandomPlayer
from catanbot.eval import evaluate_state


class _EvalPlayer(Player):
    def decide(self, game, playable_actions):
        if not playable_actions:
            return None
        if len(playable_actions) == 1:
            return playable_actions[0]
        best, best_v = playable_actions[0], float("-inf")
        for a in playable_actions:
            g2 = game.copy()
            try:
                g2.execute(a)
            except Exception:
                continue
            v = evaluate_state(g2, self.color)
            if v > best_v:
                best_v, best = v, a
        return best


def test_eval_player_beats_random_by_wide_margin():
    """Over 24 games (EvalPlayer rotating seats), the eval should win far
    more than the 25% random baseline. Assert >=50% — a regression that
    breaks the eval drops this toward chance."""
    colors = [Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE]
    wins = 0
    completed = 0
    for i in range(24):
        seat = i % 4
        players = [
            _EvalPlayer(c) if s == seat else RandomPlayer(c)
            for s, c in enumerate(colors)
        ]
        try:
            g = Game(players, seed=1000 + i)
            winner = g.play()
        except Exception:
            continue
        completed += 1
        if winner == colors[seat]:
            wins += 1
    assert completed >= 20, f"too many games failed to complete: {completed}/24"
    rate = wins / completed
    assert rate >= 0.50, (
        f"eval win rate vs random collapsed to {rate:.0%} "
        f"({wins}/{completed}) — expected >=50%, well above 25% chance")
