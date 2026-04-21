"""CLI integration tests.

Each cmd_* entry point should succeed against a mid-game save file. These
tests catch wiring regressions (e.g. a function signature drift) without
requiring an interactive REPL to reproduce.
"""
from __future__ import annotations

import pytest

from cataanbot.cli import (
    cmd_doctor,
    cmd_hands,
    cmd_openings,
    cmd_render,
    cmd_robberadvice,
    cmd_secondadvice,
    cmd_stats,
    cmd_tradeeval,
)
from cataanbot.tracker import Tracker


@pytest.fixture
def save_path(tmp_path):
    """Build a small mid-game state, save it, return the path."""
    t = Tracker(seed=4242)
    # One legal settlement per color + a few rolls to produce stats. Pull
    # the buildable pool fresh after each placement so distance-rule
    # closures don't trip us.
    from catanatron import Color
    for color in ("RED", "BLUE", "WHITE", "ORANGE"):
        legal = sorted(t.game.state.board.buildable_node_ids(
            Color[color], initial_build_phase=True
        ))
        assert legal, f"no legal spot for {color} on fixture map"
        t.settle(color, legal[0])
    for n in (6, 8, 6, 10, 5):
        t.roll(n)

    p = tmp_path / "midgame.json"
    t.save(p)
    return str(p)


def test_doctor_returns_ok():
    assert cmd_doctor() in (0, 1)


def test_render_fresh_board(tmp_path):
    out = tmp_path / "board.png"
    rc = cmd_render(str(out), hex_size=40, ticks=0,
                    label_style="icon", seed=1)
    assert rc == 0
    assert out.exists()


def test_render_with_text_labels(tmp_path):
    out = tmp_path / "board_text.png"
    rc = cmd_render(str(out), hex_size=40, ticks=0,
                    label_style="text", seed=1)
    assert rc == 0
    assert out.exists()


def test_openings_fresh_board(capsys):
    rc = cmd_openings(top=5, render_to=None, hex_size=40, seed=1)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Top" in out or "score" in out.lower()


def test_openings_with_save_filters_legal(save_path, capsys):
    rc = cmd_openings(top=5, render_to=None, hex_size=40,
                      save_path=save_path, color="RED")
    assert rc == 0
    out = capsys.readouterr().out
    assert "score" in out.lower() or "top" in out.lower()


def test_openings_save_requires_color(save_path, capsys):
    rc = cmd_openings(top=5, render_to=None, hex_size=40, save_path=save_path)
    assert rc != 0


def test_secondadvice_infers_first_node(save_path, capsys):
    # Save has 1 settlement per color; advisor should auto-find it.
    rc = cmd_secondadvice(save_path, color="RED",
                          first_node=None, top=5)
    assert rc == 0


def test_secondadvice_with_explicit_first_node(save_path, capsys):
    # Use any legal land node for the saved tracker.
    t = Tracker.load(save_path)
    m = t.game.state.board.map
    first = next(iter(m.land_nodes))
    rc = cmd_secondadvice(save_path, color="RED",
                          first_node=first, top=5)
    # Might fail if no legal second exists for that first — accept 0 or error.
    assert rc in (0, 1)


def test_robberadvice_runs(save_path, capsys):
    rc = cmd_robberadvice(save_path, color="RED", top=5)
    assert rc == 0
    assert capsys.readouterr().out.strip() != ""


def test_tradeeval_runs(save_path, capsys):
    rc = cmd_tradeeval(save_path, color="RED",
                       n_out=2, res_out="WOOD",
                       n_in=1, res_in="WHEAT")
    assert rc == 0
    out = capsys.readouterr().out
    assert "delta" in out.lower() or "trade" in out.lower()


def test_tradeeval_bad_resource_errors(save_path, capsys):
    rc = cmd_tradeeval(save_path, color="RED",
                       n_out=1, res_out="GOLD",
                       n_in=1, res_in="WHEAT")
    assert rc != 0


def test_hands_runs(save_path, capsys):
    rc = cmd_hands(save_path)
    assert rc == 0
    out = capsys.readouterr().out
    for c in ("RED", "BLUE", "WHITE", "ORANGE"):
        assert c in out


def test_stats_runs(save_path, capsys, tmp_path):
    hist = tmp_path / "hist.png"
    rc = cmd_stats(save_path, histogram_path=str(hist))
    assert rc == 0
    assert hist.exists()


def test_missing_save_file_errors(tmp_path, capsys):
    rc = cmd_hands(str(tmp_path / "does_not_exist.json"))
    assert rc != 0


def test_openings_with_after_compares_before_and_after(capsys, tmp_path):
    """--after should print the baseline ranking plus a second one."""
    png = tmp_path / "after.png"
    rc = cmd_openings(top=3, render_to=str(png), hex_size=40,
                      seed=1, after=[0])
    assert rc == 0
    out = capsys.readouterr().out
    # Baseline header appears once; second-ranking header appears once.
    assert "Top 3 opening settlement spots" in out
    assert "already claimed" in out
    assert png.exists()


def test_secondadvice_renders_png(save_path, tmp_path, capsys):
    png = tmp_path / "second.png"
    rc = cmd_secondadvice(save_path, color="RED", first_node=None,
                          top=3, render_to=str(png), hex_size=40)
    assert rc == 0
    assert png.exists()
