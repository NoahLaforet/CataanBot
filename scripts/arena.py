"""Self-play arena: the champion bot vs adversarial tables, any map, 2p or 4p.

The 2026-06-07 weight search (scripts/tune_selfplay.py) proved the eval is
already optimal against by-the-book opponents. This arena is the next rung:
it seats the champion-under-test against the adversaries in
scripts/opponents.py (anti-book disruption, leader-hunters) and on 1v1
tables, across every map geometry, so we can SEE where the by-the-book eval
gets exploited and then prove a fix.

One champion-under-test rotates through all seats (so seat luck cancels)
against a table of the chosen opponent type, over a fixed seed set (same
board + dice for every config), reporting win rate + Wilson CI. Baseline is
1/players (25% at a 4p table, 50% at 1v1), so "is the bot beating chance
against people actively gunning for it" is a clean read.

The champion-under-test can be weight-overridden (--override) so the SAME
arena measures whether a candidate change does better against these harder
opponents than the shipped eval does.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/arena.py \
      --opponent hunter --players 4 --map classic --games 200
  ... --opponent antibook|hunter|mixed|champion|random|weighted|vp
  ... --players 2   (1v1, a different game entirely)
  ... --map classic|twirl|volcano|all
  ... --override '{"prod": 12.0}'   (test a candidate champion)
  ... --json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

# Make the `scripts` package importable whether this is run as
# `python scripts/arena.py` (script dir on path) or imported as a module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catanatron import Color, Game, RandomPlayer  # noqa: E402
from catanbot import eval as ce  # noqa: E402
from scripts.opponents import (  # noqa: E402
    AntiBookPlayer, ChampionPlayer, HunterPlayer)
from scripts.selfplay_smoke import _build_map_from_capture  # noqa: E402
from scripts.tune_selfplay import _seeds, _wilson  # noqa: E402

COLORS = [Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE]
CHAMP = dict(ce.EVAL_WEIGHTS)

# label -> (capture filename or None for classic, vps_to_win)
MAP_SPECS = {
    "classic": (None, 10),
    "twirl": ("twirl-win-2026-05-03.json", 10),
    "volcano": ("volcano-2026-05-23.jsonl", 14),
}


def _opponent(kind, color, champ_color, epsilon, seed):
    if kind == "champion":
        return ChampionPlayer(color, weights=dict(CHAMP))
    if kind == "antibook":
        return AntiBookPlayer(color, epsilon=epsilon, seed=seed,
                              weights=dict(CHAMP))
    if kind == "hunter":
        # hunt the champion-under-test specifically: the "everyone targets
        # our bot" stress test.
        return HunterPlayer(color, target=champ_color, weights=dict(CHAMP))
    if kind == "random":
        return RandomPlayer(color)
    if kind == "weighted":
        try:
            from catanatron.players.weighted_random import WeightedRandomPlayer
            return WeightedRandomPlayer(color)
        except Exception:  # noqa: BLE001
            return RandomPlayer(color)
    if kind == "vp":
        try:
            from catanatron.players.search import VictoryPointPlayer
            return VictoryPointPlayer(color)
        except Exception:  # noqa: BLE001
            return RandomPlayer(color)
    raise SystemExit(f"unknown opponent kind: {kind}")


def _fill_seats(kind, colors, champ_seat, epsilon, seed):
    """Build the player list. The champ-under-test sits at champ_seat; the
    rest are the opponent type. 'mixed' cycles hunter/antibook/champion so a
    realistic table has a hunter AND a disruptor AND a clean racer."""
    champ_color = colors[champ_seat]
    mixed_cycle = ["hunter", "antibook", "champion"]
    players = []
    fill_i = 0
    for seat, c in enumerate(colors):
        if seat == champ_seat:
            players.append(None)  # placeholder, set by caller
            continue
        k = kind
        if kind == "mixed":
            k = mixed_cycle[fill_i % len(mixed_cycle)]
            fill_i += 1
        players.append(_opponent(k, c, champ_color, epsilon, seed))
    return players


def run_map(kind, players_n, map_label, override, seeds, epsilon):
    cap, vps = MAP_SPECS[map_label]
    colors = COLORS[:players_n]
    champ_w = dict(CHAMP)
    champ_w.update(override)
    wins = 0
    done = 0
    crashes = 0
    for i, s in enumerate(seeds):
        random.seed(s)
        champ_seat = i % players_n
        champ_color = colors[champ_seat]
        table = _fill_seats(kind, colors, champ_seat, epsilon, s)
        table[champ_seat] = ChampionPlayer(champ_color, weights=champ_w)
        cmap = None
        if cap is not None:
            cmap = _build_map_from_capture(cap)
            if cmap is None:
                return {"map": map_label, "skipped": "no capture",
                        "games": 0, "winrate": 0.0}
        try:
            g = Game(table, seed=s, vps_to_win=vps, catan_map=cmap)
            winner = g.play()
        except Exception:  # noqa: BLE001
            crashes += 1
            continue
        done += 1
        if winner == champ_color:
            wins += 1
    rate, lo, hi = _wilson(wins, done)
    return {"map": map_label, "opponent": kind, "players": players_n,
            "wins": wins, "games": done, "crashes": crashes,
            "winrate": rate, "ci_low": lo, "ci_high": hi,
            "baseline": 1.0 / players_n}


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--opponent", default="hunter",
                    choices=["champion", "antibook", "hunter", "mixed",
                             "random", "weighted", "vp"])
    ap.add_argument("--players", type=int, default=4, choices=[2, 4])
    ap.add_argument("--map", default="classic",
                    choices=["classic", "twirl", "volcano", "all"])
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20260607)
    ap.add_argument("--epsilon", type=float, default=0.25,
                    help="anti-book off-book probability")
    ap.add_argument("--override", default="{}",
                    help="JSON EVAL_WEIGHTS overrides for the champion-under-test")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    override = json.loads(args.override)
    seeds = _seeds(args.games, args.seed)
    maps = (["classic", "twirl", "volcano"] if args.map == "all"
            else [args.map])
    results = [run_map(args.opponent, args.players, mp, override, seeds,
                       args.epsilon) for mp in maps]

    out = {"opponent": args.opponent, "players": args.players,
           "override": override, "results": results}
    if args.json:
        print(json.dumps(out))
        return 0

    print(f"champion-under-test vs {args.opponent} x{args.players - 1}, "
          f"{args.players}p table, override={override or '(champion)'}")
    print(f"fixed seeds: {args.games} (master {args.seed})\n")
    for r in results:
        if r.get("skipped"):
            print(f"  {r['map']:8s}  SKIPPED ({r['skipped']})")
            continue
        base = r["baseline"] * 100
        edge = (r["winrate"] - r["baseline"]) * 100
        print(f"  {r['map']:8s}  {r['wins']:>3}/{r['games']:<3} = "
              f"{r['winrate']*100:5.1f}%  (95% CI "
              f"{r['ci_low']*100:.1f}-{r['ci_high']*100:.1f}, base {base:.0f}, "
              f"edge {edge:+.1f}){'  CRASHES:' + str(r['crashes']) if r['crashes'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
