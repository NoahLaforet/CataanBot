"""Ground-truth accuracy harness for the opponent-hand particle filter.

The live bridge never sees an opponent's exact cards. ``OppHandModel``
(``catanbot.opp_inference``) reconstructs them from the public colonist
game log plus colonist's authoritative per-player hand *sizes*. This
script measures how good that reconstruction is, against a source of
truth the live game can never give us: a fully observable catanatron
self-play game, where ``State.player_state`` carries every player's
exact hand.

How it works, per game:

* Drive a catanatron RandomPlayer game one action at a time with
  ``Game.play_tick``. Catanatron is fully deterministic given its
  ``seed``, so the whole harness is reproducible from one integer.
* Pick one seat as the *viewer*. Its hand is fed to the model exactly
  (``set_self_hand``), mirroring colonist's self-hand WS frame. The
  other three seats are the opponents the model must infer.
* Translate each catanatron action into the PUBLIC colonist event the
  bridge would actually see, and ``apply`` it to the model. A
  third-party robber/knight steal is emitted with ``resource=None``
  (hidden), exactly as colonist blurs it; a steal touching the viewer
  carries its real resource (public). Roll payouts become one
  ``ProduceEvent`` per producing seat, builds become paid
  ``BuildEvent``, dev buys/plays, monopolies, year-of-plenty,
  maritime trades and 7-discards all map to their event dataclass.
* After every step, anchor the model with ``reconcile`` against the
  true per-opponent hand totals (colonist ships these sizes), then
  read ``beliefs`` for every opponent and resource and score them
  against the true counts.

Metrics, accumulated across every (step, opponent, resource):

* floor-invariant violations: ``beliefs(opp)[res].minimum`` must never
  exceed the opponent's true count. A single violation is a
  correctness bug in the model, captured with its reproducing seed and
  the offending event. The script exits non-zero if any occur.
* MAE: mean absolute error of ``beliefs.expected`` vs the true count.
* resolution: fraction of all opponent cards pinned to the floor
  (sum of minimums / sum of true totals), i.e. how much of each hand the
  model nails down rather than leaving as untyped unknown mass.
* calibration: among (opp,res) cells the model claims it holds at
  least one of (``p_at_least_one >= 0.5``), the fraction that truly do.
* sync rate: fraction of steps where ``is_synced()`` is True.

Usage::

    PYTHONPATH=src .venv/bin/python scripts/infer_accuracy.py \\
        --games 300 --variant classic --seed 0 --profile mixed
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# When run as a script, pin the hash seed before catanatron is imported
# below (see ensure_hashseed_pinned for why). On import this is a no-op,
# so a test can set the env var itself and import the module safely.
if __name__ == "__main__" and os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.execv(sys.executable, [sys.executable] + sys.argv)


def ensure_hashseed_pinned() -> None:
    """Pin PYTHONHASHSEED and re-exec once if it is not already fixed.

    catanatron builds ``playable_actions`` out of sets/dicts whose
    iteration order depends on Python's per-process hash seed, so
    ``RandomPlayer``-style action picks diverge across runs even with an
    identical Game seed. Pinning the hash seed makes a given ``--seed``
    always reproduce the same games, the same metrics, and the same
    floor-invariant violations. Callers must invoke this before importing
    catanatron (the CLI does so at startup; tests set the env var in their
    own re-exec guard before importing this module)."""
    if os.environ.get("PYTHONHASHSEED") != "0":
        os.environ["PYTHONHASHSEED"] = "0"
        os.execv(sys.executable, [sys.executable] + sys.argv)


from catanatron import Color, Game
from catanatron.models.player import Player

from catanbot.events import (
    BuildEvent, DevCardBuyEvent, DevCardPlayEvent, DiscardEvent,
    MonopolyStealEvent, ProduceEvent, StealEvent, TradeCommitEvent,
)
from catanbot.live import ColorMap
from catanbot.opp_inference import OppHandModel

ROOT = Path(__file__).resolve().parent.parent
RESOURCES: tuple[str, ...] = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")
SEAT_COLORS = [Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE]

# catanatron dev-card-type int/name -> the catanbot DevCardPlayEvent card
# string and the BUILD_* piece names map straight across.
_PIECE = {
    "BUILD_SETTLEMENT": "settlement",
    "BUILD_CITY": "city",
    "BUILD_ROAD": "road",
}
_DEV_PLAY_CARD = {
    "PLAY_KNIGHT_CARD": "knight",
    "PLAY_YEAR_OF_PLENTY": "year_of_plenty",
    "PLAY_MONOPOLY": "monopoly",
    "PLAY_ROAD_BUILDING": "road_building",
}

# Which event family each --profile leans on. The biased player upweights
# the matching catanatron action types when they are playable.
_PROFILE_ACTIONS = {
    "mixed": (),
    "steal-heavy": ("PLAY_KNIGHT_CARD", "BUY_DEVELOPMENT_CARD"),
    "monopoly-heavy": ("PLAY_MONOPOLY", "BUY_DEVELOPMENT_CARD"),
    "trade-heavy": ("MARITIME_TRADE",),
    "discard-heavy": ("BUY_DEVELOPMENT_CARD",),  # bigger hands -> more 7-discards
}


class BiasedRandomPlayer(Player):
    """Deterministic random player with an optional bias toward a set of
    action types, so a ``--profile`` can stress one event family. Carries
    its own RNG so the harness is reproducible without touching global
    ``random`` state."""

    def __init__(self, color, rng, favor: tuple[str, ...] = (), strength: float = 0.85):
        super().__init__(color)
        self.rng = rng
        self.favor = set(favor)
        self.strength = strength

    def decide(self, game, playable_actions):
        actions = list(playable_actions)
        if len(actions) == 1:
            return actions[0]
        if self.favor:
            favored = [a for a in actions if a.action_type.name in self.favor]
            if favored and self.rng.random() < self.strength:
                return favored[self.rng.randrange(len(favored))]
        return actions[self.rng.randrange(len(actions))]


@dataclass
class Metrics:
    games: int = 0
    steps: int = 0
    cells: int = 0                 # (step, opp, resource) tuples scored
    abs_err_sum: float = 0.0
    true_total: int = 0
    floor_sum: int = 0             # sum of minimums (resolution numerator)
    synced_steps: int = 0
    # calibration of p_at_least_one >= 0.5
    claim_one: int = 0
    claim_one_correct: int = 0
    violations: list[dict] = field(default_factory=list)

    @property
    def mae(self) -> float:
        return self.abs_err_sum / self.cells if self.cells else 0.0

    @property
    def resolution(self) -> float:
        return self.floor_sum / self.true_total if self.true_total else 1.0

    @property
    def sync_rate(self) -> float:
        return self.synced_steps / self.steps if self.steps else 0.0

    @property
    def calibration(self) -> float:
        return self.claim_one_correct / self.claim_one if self.claim_one else 1.0


def _build_classic_map():
    """Classic board needs no capture; ``catan_map=None`` is the default."""
    return None


def _build_variant_map(variant: str):
    """Build a variant board from a ws_capture, mirroring selfplay_smoke."""
    from catanbot.colonist_proto import load_capture
    from catanbot.live_game import LiveGame

    captures = {
        "twirl": "twirl-win-2026-05-03.json",
        "volcano": "volcano-2026-05-23.jsonl",
    }
    name = captures.get(variant)
    if name is None:
        raise SystemExit(f"unknown variant {variant!r}")
    path = ROOT / "ws_captures" / name
    if not path.exists():
        raise SystemExit(f"no capture for variant {variant!r} at {path}")
    lg = LiveGame()
    last = None
    for fr in load_capture(path):
        if fr.error or not isinstance(fr.payload, dict):
            continue
        p = fr.payload
        if p.get("type") == 4:
            b = p.get("payload")
            if isinstance(b, dict):
                gs = b.get("gameState") if "gameState" in b else b
                if isinstance(gs, dict) and isinstance(gs.get("mapState"), dict):
                    last = p
    if last is None:
        raise SystemExit(f"no usable gameState frame in capture for {variant!r}")
    lg.feed(last)
    if not lg.started:
        raise SystemExit(f"capture for {variant!r} did not start a game")
    return lg.tracker.game.state.board.map


class _GameDriver:
    """Plays one catanatron game tick by tick, reading ground-truth hands
    and feeding the matching public events to an ``OppHandModel``."""

    def __init__(self, game: Game, viewer_idx: int):
        self.game = game
        self.state = game.state
        self.viewer_idx = viewer_idx
        # P-index -> catanatron Color, fixed for the whole game.
        self.idx_color = {i: self.state.colors[i] for i in range(len(self.state.colors))}
        self.color_name = {i: self.idx_color[i].value for i in range(len(self.idx_color))}
        self.viewer_color = self.color_name[viewer_idx]
        # username == color name keeps the ColorMap a pure identity map, so
        # the model resolves "RED" -> "RED" exactly as it would a real seat.
        self.color_map = ColorMap({self.color_name[i]: self.color_name[i]
                                   for i in range(len(self.idx_color))})
        self.model = OppHandModel(
            colors=[self.color_name[i] for i in range(len(self.idx_color))],
            self_color=self.viewer_color,
        )

    # ground truth helpers

    def _hand(self, idx: int) -> dict[str, int]:
        ps = self.state.player_state
        return {r: int(ps[f"P{idx}_{r}_IN_HAND"]) for r in RESOURCES}

    def _hands(self) -> dict[int, dict[str, int]]:
        return {i: self._hand(i) for i in range(len(self.idx_color))}

    def _totals(self) -> dict[str, int]:
        return {self.color_name[i]: sum(self._hand(i).values())
                for i in range(len(self.idx_color))}

    # action to public-event translation

    def _events_for(self, action, before: dict[int, dict[str, int]],
                    after: dict[int, dict[str, int]]) -> list:
        """Translate one catanatron action (with the true hand delta it
        produced) into the public colonist events the bridge would see."""
        at = action.action_type.name
        actor = self._idx_of(action.color)
        events: list = []

        if at == "ROLL":
            # Production is folded into this tick's deltas: one ProduceEvent
            # per seat that gained cards. Setup payout and robber blocking
            # are already reflected in the true delta, so this stays exact.
            for i, color in self.color_name.items():
                gained = {r: after[i][r] - before[i][r]
                          for r in RESOURCES if after[i][r] > before[i][r]}
                if gained:
                    events.append(ProduceEvent(player=color, resources=gained))
            return events

        if at in _PIECE:
            paid = self._is_paid_build(actor, before, after)
            events.append(BuildEvent(player=self.color_name[actor],
                                     piece=_PIECE[at], paid=paid))
            # A setup second settlement pays its adjacent tiles immediately,
            # on the same BUILD_SETTLEMENT tick (the actor's hand GROWS).
            # colonist narrates that initial production publicly, so emit it
            # as a ProduceEvent; otherwise the model never sees those cards.
            gained = {r: after[actor][r] - before[actor][r]
                      for r in RESOURCES if after[actor][r] > before[actor][r]}
            if gained:
                events.append(ProduceEvent(player=self.color_name[actor],
                                           resources=gained))
            return events

        if at == "BUY_DEVELOPMENT_CARD":
            events.append(DevCardBuyEvent(player=self.color_name[actor]))
            return events

        if at == "PLAY_YEAR_OF_PLENTY":
            taken = {r: after[actor][r] - before[actor][r]
                     for r in RESOURCES if after[actor][r] > before[actor][r]}
            events.append(DevCardPlayEvent(player=self.color_name[actor],
                                           card="year_of_plenty", resources=taken))
            return events

        if at == "PLAY_MONOPOLY":
            res = action.value
            gained = after[actor][res] - before[actor][res]
            events.append(MonopolyStealEvent(player=self.color_name[actor],
                                             resource=res, count=max(0, gained)))
            return events

        if at in ("PLAY_KNIGHT_CARD", "PLAY_ROAD_BUILDING"):
            # Knight's robber/steal lands on a following MOVE_ROBBER tick;
            # road-building's two free roads land on following BUILD_ROAD
            # ticks (paid=False by the delta check). Nothing to emit here.
            events.append(DevCardPlayEvent(player=self.color_name[actor],
                                           card=_DEV_PLAY_CARD[at]))
            return events

        if at == "MOVE_ROBBER":
            _coord, victim_color, resource = action.value
            if victim_color is None:
                return events  # no one to rob
            thief = self.color_name[actor]
            victim = victim_color.value
            viewer_involved = (self.viewer_color in (thief, victim))
            # Public stream: resource only revealed when the viewer is a
            # party to the steal; otherwise it is hidden (resource=None).
            events.append(StealEvent(thief=thief, victim=victim,
                                     resource=resource if viewer_involved else None))
            return events

        if at == "MARITIME_TRADE":
            give = [r for r in action.value[:4] if r is not None]
            get = action.value[4]
            gave: dict[str, int] = {}
            for r in give:
                gave[r] = gave.get(r, 0) + 1
            events.append(TradeCommitEvent(
                giver=self.color_name[actor], receiver="BANK",
                gave=gave, got={get: 1}))
            return events

        # DISCARD, END_TURN, and anything else.
        if at == "DISCARD":
            lost = {r: before[actor][r] - after[actor][r]
                    for r in RESOURCES if before[actor][r] > after[actor][r]}
            if lost:
                events.append(DiscardEvent(player=self.color_name[actor],
                                           resources=lost))
            return events
        return events

    def _is_paid_build(self, actor: int, before, after) -> bool:
        """A build is paid iff the actor's hand actually shrank. Setup
        settlements/roads and road-building's free roads cost nothing."""
        spent = sum(max(0, before[actor][r] - after[actor][r]) for r in RESOURCES)
        return spent > 0

    def _idx_of(self, color) -> int:
        for i, c in self.idx_color.items():
            if c == color:
                return i
        return -1

    # main loop

    def run(self, metrics: Metrics, seed: int) -> None:
        events_log: list = []
        # Prime the viewer's hand and the totals before any action.
        self._sync_viewer()
        self.model.reconcile(totals=self._totals())
        guard = 0
        while self.game.winning_color() is None and guard < 5000:
            guard += 1
            before = self._hands()
            action = self.game.play_tick()
            after = self._hands()
            for ev in self._events_for(action, before, after):
                events_log.append(ev)
                self.model.apply(ev, self.color_map)
            self._sync_viewer()
            self.model.reconcile(totals=self._totals())
            self._score(metrics, seed, events_log)

    def _sync_viewer(self) -> None:
        self.model.set_self_hand(self.viewer_color, self._hand(self.viewer_idx))

    def _score(self, metrics: Metrics, seed: int, events_log: list) -> None:
        metrics.steps += 1
        if self.model.is_synced():
            metrics.synced_steps += 1
        for i, color in self.color_name.items():
            if i == self.viewer_idx:
                continue
            true_hand = self._hand(i)
            bel = self.model.beliefs(color)
            for r in RESOURCES:
                truth = true_hand[r]
                b = bel[r]
                metrics.cells += 1
                metrics.true_total += truth
                metrics.abs_err_sum += abs(b.expected - truth)
                metrics.floor_sum += min(b.minimum, truth)
                if b.p_at_least_one >= 0.5:
                    metrics.claim_one += 1
                    if truth >= 1:
                        metrics.claim_one_correct += 1
                if b.minimum > truth:
                    # HARD INVARIANT BROKEN: model guarantees more of this
                    # resource than the opponent actually holds.
                    last_ev = events_log[-1] if events_log else None
                    metrics.violations.append({
                        "seed": seed,
                        "viewer": self.viewer_color,
                        "opponent": color,
                        "resource": r,
                        "minimum": b.minimum,
                        "true": truth,
                        "step": metrics.steps,
                        "last_event": repr(last_ev),
                        "events_applied": self.model.events_applied,
                    })


def run(games: int, variant: str, base_seed: int, profile: str,
        viewer: int = 0, verbose: bool = False) -> Metrics:
    import random as _random

    favor = _PROFILE_ACTIONS.get(profile, ())
    metrics = Metrics()
    cmap = None if variant == "classic" else _build_variant_map(variant)
    for g in range(games):
        seed = base_seed + g
        # catanatron does `self.seed = seed or random.randrange(...)`, so a
        # falsy seed (0) silently becomes a fresh OS-random seed and the
        # game stops being reproducible. Offset to a strictly positive
        # game seed so every run is deterministic. `Game.__init__` then
        # `random.seed`s the global module from it, which drives the seat
        # shuffle, dev-card deck, dice and bot 7-discards in lockstep.
        game_seed = abs(seed) * 2 + 1
        # Per-game RNG drives the biased players deterministically and is
        # independent of the global module, so the profile bias never
        # perturbs the engine's reproducible stream.
        rng = _random.Random(game_seed * 7919 + 17)
        players = [BiasedRandomPlayer(c, _random.Random(rng.random()), favor)
                   for c in SEAT_COLORS]
        if variant == "classic":
            game = Game(players, seed=game_seed)
        else:
            cmap = _build_variant_map(variant)
            game = Game(players, seed=game_seed, catan_map=cmap,
                        vps_to_win=14 if variant == "volcano" else 10)
        v = viewer % len(game.state.colors)
        driver = _GameDriver(game, v)
        try:
            driver.run(metrics, seed)
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] game seed={seed} raised {exc!r}; counted partial",
                  file=sys.stderr)
        metrics.games += 1
        if verbose and (g + 1) % 50 == 0:
            print(f"  ...{g + 1}/{games} games, "
                  f"{len(metrics.violations)} violations so far")
    return metrics


def _print_summary(metrics: Metrics, args) -> None:
    print("=" * 60)
    print(f"opp-inference accuracy  variant={args.variant}  "
          f"profile={args.profile}  seed={args.seed}")
    print("-" * 60)
    print(f"games                {metrics.games}")
    print(f"steps scored         {metrics.steps}")
    print(f"opp-res cells        {metrics.cells}")
    print(f"floor violations     {len(metrics.violations)}")
    print(f"expected-count MAE   {metrics.mae:.4f}")
    print(f"resolution (floor)   {metrics.resolution:.4f}  "
          f"({metrics.floor_sum}/{metrics.true_total} cards pinned)")
    print(f"calibration p>=1     {metrics.calibration:.4f}  "
          f"({metrics.claim_one_correct}/{metrics.claim_one} claims true)")
    print(f"is_synced rate       {metrics.sync_rate:.4f}")
    if metrics.violations:
        print("-" * 60)
        print(f"FLOOR-INVARIANT VIOLATIONS ({len(metrics.violations)}):")
        shown = metrics.violations[:10]
        for v in shown:
            print(f"  seed={v['seed']} step={v['step']} opp={v['opponent']} "
                  f"{v['resource']} min={v['minimum']} > true={v['true']} "
                  f"after {v['last_event']}")
        if len(metrics.violations) > len(shown):
            print(f"  ... and {len(metrics.violations) - len(shown)} more")
    print("=" * 60)


def metrics_as_dict(metrics: Metrics) -> dict:
    """Flat, JSON-serializable view of a run, for the regression test and
    any downstream tooling that wants the numbers without scraping stdout."""
    return {
        "games": metrics.games,
        "steps": metrics.steps,
        "cells": metrics.cells,
        "true_total": metrics.true_total,
        "abs_err_sum": metrics.abs_err_sum,
        "mae": metrics.mae,
        "resolution": metrics.resolution,
        "calibration": metrics.calibration,
        "sync_rate": metrics.sync_rate,
        "n_violations": len(metrics.violations),
        "violations": metrics.violations[:25],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--games", type=int, default=100)
    ap.add_argument("--variant", default="classic",
                    choices=["classic", "twirl", "volcano"])
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--profile", default="mixed",
                    choices=list(_PROFILE_ACTIONS))
    ap.add_argument("--viewer", type=int, default=0,
                    help="seat index (0-3) used as the known-hand viewer")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true",
                    help="emit metrics as one JSON line instead of a table")
    args = ap.parse_args(argv)

    metrics = run(args.games, args.variant, args.seed, args.profile,
                  viewer=args.viewer, verbose=args.verbose)
    if args.json:
        import json
        print(json.dumps(metrics_as_dict(metrics)))
    else:
        _print_summary(metrics, args)
    return 1 if metrics.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
