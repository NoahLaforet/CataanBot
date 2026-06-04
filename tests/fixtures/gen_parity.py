"""Generate the bridge-vs-standalone differential parity fixtures.

Run from the repo root:  PYTHONPATH=src .venv/bin/python tests/fixtures/gen_parity.py

Reads a real ws_capture, SANITIZES usernames, and writes two committed
fixtures the node parity test consumes:

  tests/fixtures/parity_frames.json  - the shared decoded {type, payload}
      frames (usernames replaced with neutral names), fed to BOTH engines.
  tests/fixtures/parity_golden.json  - the bridge's derived public state
      after replaying those frames: per-player (vp, played_knights,
      longest_road, has_army, has_road, settlements, cities) as a sorted
      multiset, so the comparison is color-mapping independent.

Hand totals are intentionally excluded: opponent per-resource hands are
deduced and legitimately differ between the engines (the hand-tracker is
a separate, public-info-limited concern).
"""
from __future__ import annotations

import json
from pathlib import Path

from catanbot.colonist_proto import load_capture
from catanbot.live_game import LiveGame

REPO = Path(__file__).resolve().parents[2]
CAPTURE = (REPO / "ws_captures"
           / "catanbot-ws-fort4092-early-2026-04-21T23-23-22.json")
FRAMES_OUT = REPO / "tests" / "fixtures" / "parity_frames.json"
GOLDEN_OUT = REPO / "tests" / "fixtures" / "parity_golden.json"

# Stable neutral aliases; the real handles never reach the repo.
_ALIASES = ["Avery", "Blair", "Casey", "Devon", "Emery", "Finley"]


def _sanitize(obj, name_map: dict[str, str]):
    """Deep-copy with every 'username' value swapped for a neutral alias
    and 'countryCode' dropped to a placeholder."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "username" and isinstance(v, str):
                out[k] = name_map.setdefault(
                    v, _ALIASES[len(name_map) % len(_ALIASES)])
            elif k == "countryCode":
                out[k] = "XX"
            else:
                out[k] = _sanitize(v, name_map)
        return out
    if isinstance(obj, list):
        return [_sanitize(v, name_map) for v in obj]
    if isinstance(obj, bytes):
        return obj.hex()
    return obj


def _player_stats(game) -> list[list[int]]:
    """Sorted per-player public-state tuples from the bridge tracker."""
    state = game.state
    counts: dict[str, list[int]] = {}
    for color, idx in state.color_to_index.items():
        ps = state.player_state
        counts[color.name] = [
            int(ps.get(f"P{idx}_VICTORY_POINTS", 0)),
            int(ps.get(f"P{idx}_PLAYED_KNIGHT", 0)),
            int(ps.get(f"P{idx}_LONGEST_ROAD_LENGTH", 0)),
            1 if ps.get(f"P{idx}_HAS_ARMY") else 0,
            1 if ps.get(f"P{idx}_HAS_ROAD") else 0,
            0,  # settlements (filled below)
            0,  # cities
        ]
    for _node, (color, kind) in game.state.board.buildings.items():
        row = counts.get(color.name)
        if row is None:
            continue
        if kind == "CITY":
            row[6] += 1
        else:
            row[5] += 1
    return sorted(counts.values())


def main() -> None:
    if not CAPTURE.exists():
        raise SystemExit(f"capture not found: {CAPTURE}")
    name_map: dict[str, str] = {}
    frames = []
    for f in load_capture(CAPTURE):
        if f.error or not isinstance(f.payload, dict):
            continue
        frames.append(_sanitize(f.payload, name_map))

    FRAMES_OUT.write_text(json.dumps(frames, separators=(",", ":")))

    lg = LiveGame()
    for payload in frames:
        try:
            lg.feed(payload)
        except Exception:  # noqa: BLE001 - mirror the live tolerant feed
            pass
    if lg.tracker is None:
        raise SystemExit("bridge did not boot from the capture")
    golden = {
        "source": CAPTURE.name,
        "frame_count": len(frames),
        "players_sorted": _player_stats(lg.tracker.game),
    }
    GOLDEN_OUT.write_text(json.dumps(golden, indent=2))
    print(f"wrote {FRAMES_OUT.name} ({len(frames)} frames) and "
          f"{GOLDEN_OUT.name}")
    print("golden players_sorted:")
    for row in golden["players_sorted"]:
        print("  ", row)


if __name__ == "__main__":
    main()
