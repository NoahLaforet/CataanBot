"""Pit CatanBot's state-eval as a catanatron Player against the bots.

Wraps `catanbot.eval.evaluate_state` in a greedy 1-ply player: for each
legal action, copy the game, apply it, score the resulting state, and play
the highest-scoring action. Running it against catanatron's RandomPlayer
gives a real win-rate signal — the training/tuning metric that was missing.

Usage:
  python scripts/eval_player.py [games] [opponent]
    games     number of games (default 60)
    opponent  random | weighted   (default random)
"""
from __future__ import annotations

import random
import sys
from collections import Counter

from catanatron import Color, Game, Player, RandomPlayer
from catanbot.eval import evaluate_state


class EvalPlayer(Player):
    """Greedy 1-ply player driven by catanbot.eval.evaluate_state."""

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


def _opponent(kind, color):
    if kind == "weighted":
        try:
            from catanatron.players.weighted_random import WeightedRandomPlayer
            return WeightedRandomPlayer(color)
        except Exception:
            pass
    if kind == "vp":
        try:
            from catanatron.players.search import VictoryPointPlayer
            return VictoryPointPlayer(color)
        except Exception:
            pass
    return RandomPlayer(color)


def tournament(games: int, opponent: str, seats: int = 4,
               vps: int = 10, catan_map=None) -> dict:
    """Run `games` games; EvalPlayer rotates through every seat so seat
    bias doesn't skew the win rate. Returns wins-by-player-type."""
    colors = [Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE][:seats]
    wins = Counter()
    eval_wins = 0
    completed = 0
    for i in range(games):
        eval_seat = i % seats           # rotate EvalPlayer's seat
        players = []
        for s, c in enumerate(colors):
            players.append(EvalPlayer(c) if s == eval_seat
                           else _opponent(opponent, c))
        eval_color = colors[eval_seat]
        try:
            g = Game(players, catan_map=catan_map, vps_to_win=vps,
                     seed=random.randint(0, 10**9))
            winner = g.play()
        except Exception as e:
            print(f"  game {i} crashed: {e!r}")
            continue
        completed += 1
        if winner is None:
            wins["draw/cap"] += 1
        elif winner == eval_color:
            eval_wins += 1
            wins["EvalPlayer"] += 1
        else:
            wins["opponent"] += 1
    rate = eval_wins / completed if completed else 0.0
    expected = 1.0 / seats
    return {"completed": completed, "eval_wins": eval_wins,
            "eval_winrate": rate, "expected_random": expected,
            "breakdown": dict(wins)}


def main(argv):
    games = int(argv[0]) if argv else 60
    opponent = argv[1] if len(argv) > 1 else "random"
    print(f"EvalPlayer vs {opponent} bots — {games} games, "
          f"EvalPlayer rotating all 4 seats")
    r = tournament(games, opponent)
    print(f"\ncompleted:      {r['completed']}")
    print(f"EvalPlayer wins: {r['eval_wins']}  "
          f"({r['eval_winrate']*100:.1f}%)")
    print(f"random baseline: {r['expected_random']*100:.1f}%")
    print(f"breakdown:       {r['breakdown']}")
    edge = r['eval_winrate'] - r['expected_random']
    print(f"edge over chance: {edge*100:+.1f} points")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
