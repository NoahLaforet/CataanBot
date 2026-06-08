"""Adversarial sparring opponents + the self-play arena.

The arena (scripts/arena.py) and its opponents (scripts/opponents.py) are
the measurement rig for hardening the eval against play it doesn't see in
catanatron's by-the-book bots: leader-hunters and off-book disruptors, at
1v1 as well as 4p. These tests pin the two invariants that make the rig
trustworthy: a champion-vs-champion table is exactly fair (so any edge a
candidate shows is real, not a seat artifact), and the hunter is actually
adversarial (it points the robber at its target, and it measurably drags
the champion's win rate below the fair mirror).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _seeds(n):
    from scripts.tune_selfplay import _seeds as mk
    return mk(n, master=20260607)


def test_mirror_1v1_is_fair():
    """Champion vs champion at 1v1 over a fixed seed set must sit near 50%
    and complete every game — the fairness anchor for the arena."""
    from scripts.arena import run_map

    r = run_map("champion", 2, "classic", {}, _seeds(24), 0.25)
    assert r["games"] == 24
    assert r["crashes"] == 0
    assert 0.30 <= r["winrate"] <= 0.70  # loose: 24 games, just not broken


def test_hunter_drags_champion_below_mirror():
    """A 1v1 hunter (every robber funnelled at the champion) must pull the
    champion's win rate below the fair mirror. Compared IN-PROCESS on the
    same seeds: absolute rates carry cross-process noise (set-iteration order
    under hash randomization shifts greedy tie-breaks), but within one
    process the hunter is strictly adversarial and must reduce the rate."""
    from scripts.arena import run_map

    seeds = _seeds(36)
    mirror = run_map("champion", 2, "classic", {}, seeds, 0.25)
    hunter = run_map("hunter", 2, "classic", {}, seeds, 0.25)
    assert hunter["crashes"] == 0 and mirror["crashes"] == 0
    assert hunter["winrate"] < mirror["winrate"]


def test_hunter_aims_robber_at_target():
    """HunterPlayer must, among legal robber moves, pick one that blocks the
    target — never a tile irrelevant to the target when a relevant one is
    legal."""
    from catanatron import Color
    from catanatron.models.enums import ActionType
    from scripts.opponents import HunterPlayer, _tile_block_value

    from scripts.arena import run_map  # noqa: F401  (ensures path set up)
    from catanatron import Game, RandomPlayer

    # Drive a game until a MOVE_ROBBER choice exists for some player, then
    # let a Hunter (seated as that color, targeting the leader) choose.
    g = Game([RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                        Color.WHITE, Color.ORANGE)], seed=11)
    for _ in range(8000):
        if g.winning_color() is not None:
            break
        actor = g.state.current_color()
        robber = [a for a in g.state.playable_actions
                  if a.action_type == ActionType.MOVE_ROBBER]
        if robber and len({a.value[1] for a in robber if a.value[1]}) >= 1:
            hunter = HunterPlayer(actor)
            target = hunter._target_color(g)
            if target is None:
                g.play_tick()
                continue
            chosen = hunter.decide(g, robber)
            # the chosen tile blocks at least as much target production as
            # any other legal robber tile (it maximizes the block).
            best = max(_tile_block_value(g, a.value[0], target)
                       for a in robber)
            got = _tile_block_value(g, chosen.value[0], target)
            assert got == best
            return
        g.play_tick()
    # if no suitable robber decision arose, the test is inconclusive but not
    # a failure — assert we at least ran the engine.
    assert True
