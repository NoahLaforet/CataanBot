"""Headless self-play smoke test across map geometries.

Runs N catanatron games with random players on the classic board and on
each variant board we can build from a capture (Twirl, Volcano, ...),
asserting full games complete without the engine crashing. This is the
foundation for any future recommender training: it proves the variant
geometry (nodes/edges/ports/distance rule) is sound for complete games,
not just the opening.

It does NOT tune anything — it's a crash/geometry gate. Wiring CatanBot's
recommender in as a catanatron Player for win-rate eval is the next step.

Usage: python scripts/selfplay_smoke.py [games_per_map]
"""
from __future__ import annotations

import sys
from pathlib import Path

from catanatron import Color, Game, RandomPlayer
from catanbot.colonist_proto import load_capture
from catanbot.live_game import LiveGame

ROOT = Path(__file__).resolve().parent.parent
COLORS = [Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE]

# (label, capture filename or None for classic, vps_to_win)
MAPS = [
    ("classic", None, 10),
    ("twirl", "twirl-win-2026-05-03.json", 10),
    ("volcano", "volcano-2026-05-23.jsonl", 14),
]


def _build_map_from_capture(name: str):
    path = ROOT / "ws_captures" / name
    if not path.exists():
        return None
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
        return None
    lg.feed(last)
    return lg.tracker.game.state.board.map if lg.started else None


def run(games: int) -> int:
    failures = 0
    for label, cap, vps in MAPS:
        cmap = None
        if cap is not None:
            cmap = _build_map_from_capture(cap)
            if cmap is None:
                print(f"[SKIP] {label}: no capture")
                continue
        crashes = 0
        done = 0
        for _ in range(games):
            players = [RandomPlayer(c) for c in COLORS]
            try:
                # Rebuild the variant map per game: catanatron mutates the
                # shared STATIC_GRAPH, and a fresh build re-augments it.
                if cap is not None:
                    cmap = _build_map_from_capture(cap)
                g = Game(players, catan_map=cmap, vps_to_win=vps)
                g.play()
                done += 1
            except Exception as e:  # noqa: BLE001
                crashes += 1
                if crashes <= 1:
                    print(f"    {label} crash: {e!r}")
        tiles = len(cmap.land_tiles) if cmap else 19
        tag = "OK" if crashes == 0 else "CRASH"
        print(f"[{tag:5s}] {label:8s} ({tiles} tiles): "
              f"{done}/{games} completed, {crashes} crashes")
        if crashes:
            failures += 1
    print(f"\n{failures} map(s) with crashes")
    return 1 if failures else 0


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    sys.exit(run(n))
