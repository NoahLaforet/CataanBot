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

from cataanbot.bridge import _feed_postmortem
from cataanbot.live import ColorMap
from cataanbot.tracker import Tracker


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
