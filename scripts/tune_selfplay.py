"""Head-to-head weight tuning against the bot's own champion eval.

The previous tuning effort (scripts/tune_eval.py, 2026-06-01) saturated:
the engine already beats every catanatron bot (random ~86%, weighted/vp
~71%), so single-weight sweeps vs those bots can't see further gains. To
find real headroom you need an opponent the bot can actually lose to, and
the strongest one available is the bot itself.

This script seats ONE candidate (the champion's EVAL_WEIGHTS patched by an
override dict) at a rotating seat against THREE copies of the champion,
over a fixed set of seeded games (same board + dice + deck order for every
config). If candidate == champion the win rate is ~25% by symmetry, so a
candidate whose Wilson 95%% lower bound clears 25 is genuinely stronger,
not lucky. A regression mode also checks the candidate still beats the
weak bots (a change that wins the mirror but tanks vs weighted/vp is
over-fit and gets rejected).

Per-seat weights are possible because evaluate_state takes an optional
``weights`` override (added for exactly this).

Usage:
  PYTHONPATH=src .venv/bin/python scripts/tune_selfplay.py \
      --override '{"prod": 12.0}' --games 300
  PYTHONPATH=src .venv/bin/python scripts/tune_selfplay.py --mirror --games 300
  PYTHONPATH=src .venv/bin/python scripts/tune_selfplay.py \
      --override '{"prod": 12.0}' --regress --games 120
  ... add --json for a machine-readable line (for agents).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys

from catanatron import Color, Game, Player, RandomPlayer
from catanbot import eval as ce

SEATS = 4
COLORS = [Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE]
CHAMPION = dict(ce.EVAL_WEIGHTS)  # snapshot of the shipped defaults


class WeightedEvalPlayer(Player):
    """Greedy 1-ply player driven by evaluate_state with its OWN weights,
    so a candidate and the champion can share one game."""

    def __init__(self, color, weights):
        super().__init__(color)
        self.weights = weights

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
            except Exception:  # noqa: BLE001
                continue
            v = ce.evaluate_state(g2, self.color, self.weights)
            if v > best_v:
                best_v, best = v, a
        return best


def _wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """Win rate + Wilson score 95%% interval (low, high). Robust at the
    small-N counts a per-candidate run produces."""
    if n == 0:
        return 0.0, 0.0, 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def _opponent(kind: str, color):
    if kind == "weighted":
        try:
            from catanatron.players.weighted_random import WeightedRandomPlayer
            return WeightedRandomPlayer(color)
        except Exception:  # noqa: BLE001
            pass
    if kind == "vp":
        try:
            from catanatron.players.search import VictoryPointPlayer
            return VictoryPointPlayer(color)
        except Exception:  # noqa: BLE001
            pass
    return RandomPlayer(color)


def _seeds(n: int, master: int) -> list[int]:
    rng = random.Random(master)
    return [rng.randint(0, 10**9) for _ in range(n)]


def head_to_head(override: dict, seeds: list[int], vps: int = 10) -> dict:
    """Candidate (champion patched by ``override``) at a rotating seat vs
    three champions. Returns win rate + Wilson CI over completed games."""
    cand_w = dict(CHAMPION)
    cand_w.update(override)
    wins = 0
    done = 0
    for i, s in enumerate(seeds):
        random.seed(s)
        cand_seat = i % SEATS
        players = [
            WeightedEvalPlayer(c, cand_w if seat == cand_seat else CHAMPION)
            for seat, c in enumerate(COLORS)
        ]
        cand_color = COLORS[cand_seat]
        try:
            g = Game(players, seed=s, vps_to_win=vps)
            winner = g.play()
        except Exception:  # noqa: BLE001
            continue
        done += 1
        if winner == cand_color:
            wins += 1
    rate, lo, hi = _wilson(wins, done)
    return {"wins": wins, "games": done, "winrate": rate,
            "ci_low": lo, "ci_high": hi, "baseline": 1.0 / SEATS}


def vs_pool(override: dict, kind: str, seeds: list[int], vps: int = 10) -> dict:
    """Candidate (rotating seat) vs three ``kind`` bots — the weak-bot
    regression check. Baseline 25%; the champion sits ~86/71/71."""
    cand_w = dict(CHAMPION)
    cand_w.update(override)
    wins = 0
    done = 0
    for i, s in enumerate(seeds):
        random.seed(s)
        cand_seat = i % SEATS
        players = [
            WeightedEvalPlayer(c, cand_w) if seat == cand_seat
            else _opponent(kind, c)
            for seat, c in enumerate(COLORS)
        ]
        cand_color = COLORS[cand_seat]
        try:
            g = Game(players, seed=s, vps_to_win=vps)
            winner = g.play()
        except Exception:  # noqa: BLE001
            continue
        done += 1
        if winner == cand_color:
            wins += 1
    rate, lo, hi = _wilson(wins, done)
    return {"kind": kind, "wins": wins, "games": done, "winrate": rate,
            "ci_low": lo, "ci_high": hi}


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--override", default="{}",
                    help="JSON dict of EVAL_WEIGHTS overrides")
    ap.add_argument("--games", type=int, default=300)
    ap.add_argument("--seed", type=int, default=20260607,
                    help="master seed for the fixed game set")
    ap.add_argument("--vps", type=int, default=10)
    ap.add_argument("--mirror", action="store_true",
                    help="force override={} (symmetry sanity, expect ~25%%)")
    ap.add_argument("--regress", action="store_true",
                    help="also run vs random/weighted/vp regression")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    override = {} if args.mirror else json.loads(args.override)
    seeds = _seeds(args.games, args.seed)
    h2h = head_to_head(override, seeds, vps=args.vps)

    out = {"override": override, "h2h": h2h}
    if args.regress:
        # fewer games for the weak-bot check — it just needs to confirm
        # no collapse, not a tight CI.
        reg_seeds = _seeds(max(60, args.games // 3), args.seed + 1)
        out["regress"] = {
            k: vs_pool(override, k, reg_seeds, vps=args.vps)
            for k in ("random", "weighted", "vp")
        }

    if args.json:
        print(json.dumps(out))
        return 0

    print(f"override: {override or '(mirror / champion)'}")
    print(f"fixed seeds: {args.games} (master {args.seed})\n")
    print(f"candidate vs 3x champion: {h2h['wins']}/{h2h['games']} = "
          f"{h2h['winrate']*100:.1f}%  "
          f"(95% CI {h2h['ci_low']*100:.1f}–{h2h['ci_high']*100:.1f}, "
          f"baseline 25.0)")
    verdict = ("BETTER than champion" if h2h["ci_low"] > 0.25 else
               "WORSE than champion" if h2h["ci_high"] < 0.25 else
               "indistinguishable from champion")
    print(f"verdict: {verdict}")
    if args.regress:
        print("\nweak-bot regression (candidate vs 3x bot, baseline 25):")
        for k, r in out["regress"].items():
            print(f"  vs {k:<9} {r['wins']:>3}/{r['games']:<3} = "
                  f"{r['winrate']*100:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
