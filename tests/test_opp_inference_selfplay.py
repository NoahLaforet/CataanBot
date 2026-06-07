"""CI-fast ground-truth regression for the opponent-hand particle filter.

``scripts/infer_accuracy.py`` drives fully observable catanatron self-play
and scores ``OppHandModel`` against the real per-player hands. This test
runs a small, fixed-seed batch of it and asserts the two things that must
never regress:

* the HARD floor invariant: the model's guaranteed per-resource minimum
  for an opponent never exceeds that opponent's true count. A single
  violation is a correctness bug (the HUD would tell a player to rob a
  card the opponent does not hold).
* a sane accuracy floor: most cards are pinned to their exact type and
  the expected-count error stays small, so a silent weakening of the
  filter (a broken event handler, a bad reconcile) fails here instead of
  shipping.

The harness is deterministic given a seed only when Python's hash seed is
pinned (catanatron's ``playable_actions`` ordering is hash-dependent), so
each batch runs the harness in a subprocess with ``PYTHONHASHSEED=0`` and
reads back its ``--json`` metrics line. Runs in ~3-5s total.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "scripts" / "infer_accuracy.py"


def _run(games: int, seed: int, variant: str = "classic",
         profile: str = "mixed") -> dict:
    """Run one harness batch in a hash-seed-pinned subprocess and return
    its metrics dict. Determinism depends on the pinned hash seed, so this
    never relies on the parent pytest process's environment."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    proc = subprocess.run(
        [sys.executable, str(HARNESS), "--json",
         "--games", str(games), "--seed", str(seed),
         "--variant", variant, "--profile", profile],
        capture_output=True, text=True, env=env, timeout=120,
    )
    # Exit code is non-zero exactly when a floor violation occurred; the
    # JSON line is always the last stdout line either way.
    assert proc.stdout.strip(), (
        f"harness produced no output\nstderr:\n{proc.stderr}")
    last = proc.stdout.strip().splitlines()[-1]
    data = json.loads(last)
    data["_returncode"] = proc.returncode
    return data


@pytest.fixture(scope="module")
def classic_batch() -> dict:
    return _run(games=24, seed=4242)


def test_zero_floor_invariant_violations(classic_batch):
    """Across a few dozen classic games, the model must never claim a
    guaranteed minimum above an opponent's true count."""
    m = classic_batch
    assert m["games"] == 24
    assert m["cells"] > 50_000  # the batch actually scored real games
    if m["n_violations"]:
        v = m["violations"][0]
        raise AssertionError(
            f"{m['n_violations']} floor-invariant violation(s); first: "
            f"seed={v['seed']} opp={v['opponent']} {v['resource']} "
            f"min={v['minimum']} > true={v['true']} at step {v['step']} "
            f"after {v['last_event']}"
        )
    # The CLI also signals violations through its exit code.
    assert m["_returncode"] == 0


def test_accuracy_thresholds(classic_batch):
    """The filter should pin most cards to the floor and keep a small
    expected-count error. Thresholds are loose enough not to flake on dice
    variance, tight enough to catch a real regression."""
    m = classic_batch
    assert m["resolution"] >= 0.80, f"resolution {m['resolution']:.3f} < 0.80"
    assert m["mae"] <= 0.20, f"expected-count MAE {m['mae']:.3f} > 0.20"
    assert m["calibration"] >= 0.90, f"calibration {m['calibration']:.3f} < 0.90"
    assert m["sync_rate"] >= 0.99, f"is_synced rate {m['sync_rate']:.3f} < 0.99"


def test_steal_heavy_profile_holds_invariant():
    """Hidden third-party steals are the hardest case for the floor; a
    steal-biased batch must still never violate it."""
    m = _run(games=16, seed=909, profile="steal-heavy")
    assert m["n_violations"] == 0, (
        f"{m['n_violations']} violation(s) under steal-heavy play; "
        f"first {m['violations'][0] if m['violations'] else None}")


def test_determinism():
    """Same seed, same metrics, run to run (hash seed pinned)."""
    a = _run(games=8, seed=55)
    b = _run(games=8, seed=55)
    assert (a["cells"], a["true_total"], a["n_violations"],
            round(a["abs_err_sum"], 6)) == (
        b["cells"], b["true_total"], b["n_violations"],
        round(b["abs_err_sum"], 6))
