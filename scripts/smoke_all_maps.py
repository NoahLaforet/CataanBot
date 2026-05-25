"""Smoke-test every WS capture through the engine, across all map types.

Loads each capture (buffer-format .json or line-delimited .jsonl), replays
it through LiveGame, and reports per-capture: map size, variant label,
dispatch status counts, any exceptions, and a board-drift check
(catanatron buildings vs colonist's authoritative corner ownership).

Catches crashes, parse errors, and build drift on classic + every variant
(Twirl, Pond, Black Forest, Volcano, ...). Exit code is non-zero if any
capture raised or drifted, so it doubles as a CI-style gate.
"""
from __future__ import annotations

import base64
import json
import sys
import traceback
from collections import Counter
from pathlib import Path

from catanbot.colonist_proto import decode_frame, load_capture
from catanbot.live_game import LiveGame

ROOT = Path(__file__).resolve().parent.parent


def frames(path: Path):
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("{") and '"buffer"' in text[:200]:
        yield from load_capture(path)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if raw.get("dir") not in ("in", "out") or "b64" not in raw:
            continue
        try:
            yield decode_frame(base64.b64decode(raw["b64"]), raw["dir"])
        except Exception:
            continue


def smoke(path: Path) -> dict:
    game = LiveGame()
    status = Counter()
    exc = None
    try:
        for fr in frames(path):
            if fr.error or not isinstance(fr.payload, dict):
                continue
            for res in game.feed(fr.payload):
                status[res.status] += 1
    except Exception:
        exc = traceback.format_exc()
    out = {"status": dict(status), "exc": exc, "started": game.started}
    if game.started and game.session is not None:
        s = game.session
        out["tiles"] = len(s.mapping.tile_types)
        try:
            out["variant"] = s.variant_label()
        except Exception:
            out["variant"] = "?"
        try:
            b = game.tracker.game.state.board.buildings
            out["cat_buildings"] = len(b)
            out["known_corners"] = len(s.known_corners)
            out["drift"] = out["cat_buildings"] != out["known_corners"]
        except Exception:
            out["drift"] = None
    return out


def main(argv):
    cap_dir = ROOT / "ws_captures"
    paths = sorted(p for p in cap_dir.glob("*")
                   if p.suffix in (".json", ".jsonl")
                   and "replayed" not in p.name and "audit" not in p.name)
    if argv:
        paths = [Path(a) for a in argv]
    failures = 0
    for p in paths:
        r = smoke(p)
        tag = "OK"
        if r["exc"]:
            tag = "CRASH"
            failures += 1
        elif r.get("drift"):
            tag = "DRIFT"
            failures += 1
        elif r["status"].get("error"):
            tag = "BUILD-ERR"
            failures += 1
        line = (f"[{tag:9s}] {p.name:42s} "
                f"tiles={r.get('tiles','-')} variant={r.get('variant','-')} "
                f"status={r['status']}")
        if r.get("cat_buildings") is not None:
            line += (f" buildings={r.get('cat_buildings')}/"
                     f"{r.get('known_corners')}")
        print(line)
        if r["exc"]:
            print("    " + r["exc"].strip().splitlines()[-1])
    print(f"\n{len(paths)} captures, {failures} with issues")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
