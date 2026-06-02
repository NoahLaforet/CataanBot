"""Per-opponent played-dev-card breakdown (advanced advisor, public info).

A dev card's type is revealed the moment it is played, so catanatron
tracks PLAYED_{type} per color and surfacing it is fair (it shows what an
opponent has burned and, by elimination, what may still be in hand). VP
cards are excluded since they are never played.
"""
from __future__ import annotations

from types import SimpleNamespace


def _game_with_played(played: dict[str, int]):
    from catanatron import Color, Game, RandomPlayer
    cg = Game([RandomPlayer(c) for c in (Color.RED, Color.BLUE)], seed=1)
    idx = cg.state.color_to_index[Color.RED]
    for key, n in played.items():
        cg.state.player_state[f"P{idx}_{key}"] = n
    # _played_dev_by_type expects a LiveGame-like with .tracker.game.
    return SimpleNamespace(tracker=SimpleNamespace(game=cg))


def test_played_dev_by_type_reads_per_type_counts():
    from catanatron import Color
    from catanbot.bridge_economy import _played_dev_by_type
    g = _game_with_played({"PLAYED_KNIGHT": 2, "PLAYED_MONOPOLY": 1})
    out = _played_dev_by_type(g, Color.RED)
    assert out == {"KNIGHT": 2, "MONOPOLY": 1,
                   "YEAR_OF_PLENTY": 0, "ROAD_BUILDING": 0}
    # Accepts a color-name string too (matches _knights_played).
    assert _played_dev_by_type(g, "RED")["KNIGHT"] == 2


def test_played_dev_by_type_excludes_vp_and_defaults_zero():
    from catanatron import Color
    from catanbot.bridge_economy import _played_dev_by_type
    g = _game_with_played({"PLAYED_VICTORY_POINT": 3})
    out = _played_dev_by_type(g, Color.BLUE)
    # No plays for BLUE; VP cards are never surfaced.
    assert out == {"KNIGHT": 0, "MONOPOLY": 0,
                   "YEAR_OF_PLENTY": 0, "ROAD_BUILDING": 0}
    assert "VICTORY_POINT" not in out
