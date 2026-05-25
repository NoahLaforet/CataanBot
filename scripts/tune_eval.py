"""Measured tuning of EVAL_WEIGHTS via fixed-seed self-play.

For each candidate weight value, runs the SAME set of seeded games (same
board + dice + deck order) so configs are compared on identical matches,
not independent luck. Sweeps one weight at a time vs an opponent pool and
reports win rate + delta from the current default.

Conservative by design: a change is only worth keeping if it improves
robustly (ideally vs more than one opponent type), since over-tuning to a
single weak bot doesn't transfer to real play.

Usage: python scripts/tune_eval.py [games] [opponent]
"""
from __future__ import annotations

import random
import sys

from catanatron import Color, Game
from catanbot import eval as ce
from scripts.eval_player import EvalPlayer, _opponent

SEATS = 4
COLORS = [Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE]


def run_config(overrides: dict, seeds: list[int], opponent: str) -> float:
    """Win rate for EvalPlayer with EVAL_WEIGHTS patched by `overrides`,
    over the given fixed seeds (EvalPlayer rotates seats by seed index)."""
    base = dict(ce.EVAL_WEIGHTS)
    ce.EVAL_WEIGHTS.update(overrides)
    try:
        wins = 0
        done = 0
        for i, s in enumerate(seeds):
            random.seed(s)                      # seed opponent RNG too
            eval_seat = i % SEATS
            players = [
                EvalPlayer(c) if seat == eval_seat else _opponent(opponent, c)
                for seat, c in enumerate(COLORS)
            ]
            eval_color = COLORS[eval_seat]
            try:
                g = Game(players, seed=s)
                w = g.play()
            except Exception:
                continue
            done += 1
            if w == eval_color:
                wins += 1
        return wins / done if done else 0.0
    finally:
        ce.EVAL_WEIGHTS.clear()
        ce.EVAL_WEIGHTS.update(base)


def sweep(weight: str, factors: list[float], seeds: list[int],
          opponent: str) -> None:
    base_val = ce.EVAL_WEIGHTS[weight]
    base_rate = run_config({}, seeds, opponent)
    print(f"\n== sweep '{weight}' (default {base_val}) vs {opponent}, "
          f"{len(seeds)} fixed-seed games ==")
    print(f"   default {base_val:<7} -> {base_rate*100:5.1f}%  (baseline)")
    for f in factors:
        if f == 1.0:
            continue
        val = base_val * f
        rate = run_config({weight: val}, seeds, opponent)
        delta = (rate - base_rate) * 100
        flag = "  <-- better" if delta >= 4 else ("  (worse)" if delta <= -4 else "")
        print(f"   {weight}={val:<7.2f} -> {rate*100:5.1f}%  "
              f"({delta:+5.1f} pts){flag}")


def main(argv):
    games = int(argv[0]) if argv else 120
    opponent = argv[1] if len(argv) > 1 else "vp"
    rng = random.Random(20260524)
    seeds = [rng.randint(0, 10**9) for _ in range(games)]
    print(f"fixed-seed tuning: {games} games/config vs {opponent} bots")
    for weight, factors in (
        ("prod", [0.6, 0.8, 1.3, 1.6]),
        ("vp_quad", [0.5, 0.75, 1.5, 2.0]),
        ("dev", [0.4, 0.7, 1.4, 2.0]),
    ):
        sweep(weight, factors, seeds, opponent)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
