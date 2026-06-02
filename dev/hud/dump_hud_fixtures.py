"""Generate HUD render fixtures from real WS captures.

Replays colonist WebSocket captures through a LiveGame and dumps the
exact dict the /advisor route returns (_build_advisor_snapshot), so the
dev harness (dev/hud/harness.html) can render the real panel.js against
representative states with no live game or bridge.

Run from the repo root:  PYTHONPATH=src python dev/hud/dump_hud_fixtures.py
Writes dev/hud/fixtures/<name>.json. These are dev-only artifacts and are
not shipped in the extension package.
"""
from __future__ import annotations

import json
from pathlib import Path

from catanbot.bridge import _build_advisor_snapshot
from catanbot.colonist_proto import load_capture
from catanbot.live import ColorMap
from catanbot.live_game import LiveGame
from catanbot.tracker import Tracker

REPO = Path(__file__).resolve().parents[2]
WS = REPO / "ws_captures"
OUT = Path(__file__).resolve().parent / "fixtures"


def iter_payloads(path: Path):
    for frame in load_capture(path):
        if frame.error:
            continue
        p = frame.payload
        if isinstance(p, dict):
            yield p


def build_st(caps: list[Path]) -> dict:
    game = LiveGame()
    for path in caps:
        for payload in iter_payloads(path):
            game.feed(payload)
    return {
        "seq": 0, "game": game, "ws_count": 0, "log_count": 0,
        "last_roll": None, "robber_pending": False, "robber_snapshot": None,
        "display_colors": {}, "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }


# name -> ordered list of capture files to replay into one state.
SCENARIOS = {
    "midgame": [
        WS / "catanbot-ws-fort4092-early-2026-04-21T23-23-22.json",
        WS / "catanbot-ws-fort4092-midgame-2026-04-21T23-34-04.json",
    ],
    "early": [
        WS / "catanbot-ws-fort4092-early-2026-04-21T23-23-22.json",
    ],
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, caps in SCENARIOS.items():
        missing = [c for c in caps if not c.exists()]
        if missing:
            print(f"skip {name}: missing {[m.name for m in missing]}")
            continue
        snap = _build_advisor_snapshot(build_st(caps))
        text = json.dumps(snap, indent=2, default=str)
        (OUT / f"{name}.json").write_text(text)
        populated = sorted(k for k, v in snap.items() if v not in (None, [], {}, 0))
        print(f"wrote {name}.json ({len(text)} bytes)")
        print(f"  populated keys: {populated}")


if __name__ == "__main__":
    main()
