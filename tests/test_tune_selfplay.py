"""Per-call eval weights override + the head-to-head tuning harness.

evaluate_state grew a ``weights`` parameter so a candidate weight-set and
the champion can be scored inside the SAME game (what fixed-seed
head-to-head tuning needs). These tests pin the two things that must hold:
the override is behaviour-preserving when omitted (the shipped HUD path is
untouched), and it genuinely re-weights when supplied. A tiny mirror run
also smoke-tests scripts/tune_selfplay.py so the harness can't silently rot.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # make the `scripts` namespace importable


def _fresh_game():
    from catanatron import Color, Game, RandomPlayer

    return Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=3,
    )


def test_weights_none_matches_global_default():
    """evaluate_state(..., weights=None) must equal scoring with the
    module-global EVAL_WEIGHTS — the live HUD calls it with no weights and
    its behaviour must be byte-for-byte unchanged by the refactor."""
    from catanbot.eval import EVAL_WEIGHTS, evaluate_state

    g = _fresh_game()
    g.state.board.build_settlement(__import__("catanatron").Color.RED, 0,
                                   initial_build_phase=True)
    assert evaluate_state(g, "RED") == evaluate_state(g, "RED",
                                                      weights=EVAL_WEIGHTS)


def test_weights_override_reweights_score():
    """Doubling vp_linear must raise the eval for a VP-leading player —
    proof the override actually flows into _player_score and isn't ignored."""
    from catanatron import Color
    from catanbot.eval import EVAL_WEIGHTS, evaluate_state

    g = _fresh_game()
    idx = g.state.color_to_index[Color.RED]
    g.state.player_state[f"P{idx}_VICTORY_POINTS"] = 3  # RED clearly ahead

    base = evaluate_state(g, "RED")
    hot = dict(EVAL_WEIGHTS)
    hot["vp_linear"] = EVAL_WEIGHTS["vp_linear"] * 2
    assert evaluate_state(g, "RED", weights=hot) > base


def test_override_isolated_from_global():
    """Passing an override must not mutate the module global — otherwise
    one candidate's weights would leak into the next game."""
    from catanbot.eval import EVAL_WEIGHTS, evaluate_state

    before = dict(EVAL_WEIGHTS)
    g = _fresh_game()
    evaluate_state(g, "RED", weights={**EVAL_WEIGHTS, "prod": 999.0})
    assert EVAL_WEIGHTS == before


def test_robber_aware_discounts_blocked_tile():
    """With robber_aware on, a state where the robber sits on the player's
    own tile must score LOWER than the blind eval (the blocked tile no longer
    counts), and must be IDENTICAL to blind when the robber is on a tile the
    player doesn't touch (e.g. the desert). This is the correctness contract
    independent of any win-rate result."""
    from catanatron import Color
    from catanbot.eval import EVAL_WEIGHTS, evaluate_state

    g = _fresh_game()
    board = g.state.board
    board.build_settlement(Color.RED, 0, initial_build_phase=True)
    # Reference the term explicitly, not via the default (which now ships at
    # 2.0): blind = robber_aware 0, aware = robber_aware 1.
    blind_w = {**EVAL_WEIGHTS, "robber_aware": 0.0}
    aware_w = {**EVAL_WEIGHTS, "robber_aware": 1.0}

    blind = evaluate_state(g, "RED", weights=blind_w)
    # Default robber sits on the desert (number=None) -> nothing to discount.
    assert evaluate_state(g, "RED", weights=aware_w) == blind

    # Find a numbered tile RED's node 0 sits on and park the robber there.
    m = board.map
    red_tile = next((coord for coord, t in m.land_tiles.items()
                     if t.number is not None and 0 in t.nodes.values()), None)
    assert red_tile is not None
    board.robber_coordinate = red_tile
    # Blind still ignores it; robber-aware must drop (production blocked).
    assert evaluate_state(g, "RED", weights=blind_w) == blind
    assert evaluate_state(g, "RED", weights=aware_w) < blind


def test_harness_mirror_runs_and_is_fair():
    """A tiny mirror match (candidate == champion) must complete games and
    return a sane win rate. Not asserting ~25% here (too few games for a
    tight CI); just that the harness runs end-to-end and reports structure."""
    from scripts.tune_selfplay import head_to_head, _seeds

    seeds = _seeds(8, master=20260607)
    out = head_to_head({}, seeds)
    assert out["games"] > 0
    assert 0.0 <= out["winrate"] <= 1.0
    assert out["ci_low"] <= out["winrate"] <= out["ci_high"]
