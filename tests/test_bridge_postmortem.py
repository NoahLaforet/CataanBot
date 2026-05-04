"""Auto-postmortem path on the /log pipeline.

The bridge is built so that when a DOM-log GameOverEvent lands, an HTML
postmortem is rendered to disk without any user action. Verifies that:

    * the collector actually parses + dispatches /log payloads,
    * GameOverEvent triggers exactly one file write (re-firing the same
      event — which happens when colonist's virtualized scroller
      re-renders the line — does not stomp the first write).
"""
from __future__ import annotations

from pathlib import Path

from catanbot.bridge import _feed_postmortem
from catanbot.live import ColorMap
from catanbot.tracker import Tracker


def _payload(parts, ts=0.0):
    return {"ts": ts, "text": "", "parts": parts, "names": [], "icons": [],
            "self": None}


def _name(n):
    return {"kind": "name", "name": n, "color": ""}


def _text(t):
    return {"kind": "text", "text": t}


def _icon(alt):
    return {"kind": "icon", "alt": alt, "src_tail": ""}


def _fresh_state(pm_dir: Path) -> dict:
    return {
        "pm_tracker": Tracker(),
        "pm_color_map": ColorMap(),
        "pm_events": [],
        "pm_results": [],
        "pm_timestamps": [],
        "pm_written": False,
        "pm_dir": pm_dir,
    }


def test_game_over_writes_postmortem_once(tmp_path: Path):
    st = _fresh_state(tmp_path)

    # A minimal "game" — one roll, one produce, then the game-over line.
    _feed_postmortem(st, _payload(
        [_name("Alice"), _text("rolled"), _icon("dice_3"), _icon("dice_4")],
        ts=1.0,
    ))
    _feed_postmortem(st, _payload(
        [_name("Alice"), _text("got"), _icon("wood"), _icon("wood")],
        ts=2.0,
    ))
    _feed_postmortem(st, _payload(
        [_name("Alice"), _text("won the game!"), _icon("trophy")],
        ts=3.0,
    ))

    written = list(tmp_path.glob("*.html"))
    assert len(written) == 1
    assert written[0].read_text().startswith("<!doctype html>")
    assert st["pm_written"] is True
    assert "Alice" in written[0].name  # winner in the filename

    # Re-firing the same game-over line (colonist log virtualization)
    # must not overwrite or double-emit.
    _feed_postmortem(st, _payload(
        [_name("Alice"), _text("won the game!"), _icon("trophy")],
        ts=3.0,
    ))
    assert len(list(tmp_path.glob("*.html"))) == 1


def test_feed_postmortem_without_game_over_writes_nothing(tmp_path: Path):
    st = _fresh_state(tmp_path)
    _feed_postmortem(st, _payload(
        [_name("Alice"), _text("rolled"), _icon("dice_3"), _icon("dice_4")],
        ts=1.0,
    ))
    assert list(tmp_path.glob("*.html")) == []
    assert st["pm_written"] is False


def test_resolve_final_vp_falls_back_to_build_events():
    """Regression for the 2026-04-30 opp postmortem where the
    colonist + tracker paths both returned 2/2 (impossible — winner had
    to have 10+ VP). Build-derived fallback uses pm_events to compute
    settles + 2*cities + LR/LA flags, which works whenever the DOM log
    captured the BuildEvents (always true for a completed game)."""
    from catanbot.bridge_postmortem import _resolve_final_vp
    from catanbot.events import BuildEvent, VPEvent
    cm = ColorMap({"Alice": "RED", "Bob": "BLUE"})
    pm_events = [
        BuildEvent(player="Alice", piece="settlement"),
        BuildEvent(player="Alice", piece="settlement"),
        BuildEvent(player="Alice", piece="settlement"),
        BuildEvent(player="Alice", piece="city"),
        BuildEvent(player="Bob", piece="settlement"),
        BuildEvent(player="Bob", piece="settlement"),
        BuildEvent(player="Bob", piece="city"),
        BuildEvent(player="Bob", piece="city"),
        VPEvent(player="Alice", reason="longest_road", vp_delta=2),
        VPEvent(player="Bob", reason="longest_road", vp_delta=2),
        VPEvent(player="Alice", reason="largest_army", vp_delta=2),
    ]
    st = {
        "game": None,  # forces colonist + live-tracker paths to skip
        "pm_events": pm_events,
        "pm_color_map": cm,
        "pm_tracker": Tracker(),
    }
    vps = _resolve_final_vp(st)
    # Alice: 3 settles + 1 city = +1 (city overwrites a settle, net +1)
    # = 3 + 1 = 4 building VP, + 2 (LA, since Bob took LR) = 6 VP total.
    # Bob: 2 settles + 2 cities = +2 = 2 + 2 = 4 building VP, + 2 (LR) = 6 VP.
    assert vps == {"RED": 6, "BLUE": 6}


def test_harvest_display_colors_latches_first_css_color():
    from catanbot.bridge import _harvest_display_colors

    st = {"display_colors": {}}
    _harvest_display_colors(st, {
        "names": [
            {"name": "Alice", "color": "rgb(232, 113, 95)"},
            {"name": "Bob",   "color": "#121214"},
        ],
    })
    assert st["display_colors"] == {
        "Alice": "rgb(232, 113, 95)",
        "Bob": "#121214",
    }

    # Later appearances don't overwrite — first color wins.
    _harvest_display_colors(st, {
        "names": [{"name": "Alice", "color": "rgb(1,2,3)"}],
    })
    assert st["display_colors"]["Alice"] == "rgb(232, 113, 95)"

    # Empty / missing colors are skipped.
    _harvest_display_colors(st, {
        "names": [{"name": "Cara", "color": ""},
                  {"name": "Dana"}],
    })
    assert "Cara" not in st["display_colors"]
    assert "Dana" not in st["display_colors"]


def test_apply_colonist_game_settings_syncs_vp_target():
    """Regression for Noah's 2026-04-30 game where colonist's
    gameSettings shipped victoryPointsToWin=15 but the bot kept
    VP_TARGET=10 internally — every endgame heuristic fired at the
    wrong threshold. Auto-detect on game boot now syncs from
    game.session.game_settings."""
    from types import SimpleNamespace
    from catanbot.bridge import _apply_colonist_game_settings
    from catanbot import config
    original_vp = config.get_vp_target()
    original_dl = config.get_discard_limit()
    try:
        sess = SimpleNamespace(
            game_settings={
                "victoryPointsToWin": 15,
                "cardDiscardLimit": 9,
            },
        )
        game = SimpleNamespace(session=sess)
        _apply_colonist_game_settings(game)
        assert config.get_vp_target() == 15
        assert config.get_discard_limit() == 9
    finally:
        config.set_vp_target(original_vp)
        config.set_discard_limit(original_dl)


def test_apply_colonist_game_settings_silent_on_missing_keys():
    """Older colonist versions (or non-classic gameTypes) may omit
    victoryPointsToWin / cardDiscardLimit. Don't crash; don't change
    config; just leave the defaults alone."""
    from types import SimpleNamespace
    from catanbot.bridge import _apply_colonist_game_settings
    from catanbot import config
    original_vp = config.get_vp_target()
    original_dl = config.get_discard_limit()
    try:
        sess = SimpleNamespace(game_settings={})
        game = SimpleNamespace(session=sess)
        _apply_colonist_game_settings(game)
        assert config.get_vp_target() == original_vp
        assert config.get_discard_limit() == original_dl
    finally:
        config.set_vp_target(original_vp)
        config.set_discard_limit(original_dl)
