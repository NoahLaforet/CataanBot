"""Empirically calibrate colonist tile.type -> resource for a capture.

Replays a WS capture through LiveGame, and on every roll compares the
*authoritative* self-hand delta (colonist's resourceCards snapshot) to
which of self's settlements/cities sit on a non-robber tile of the rolled
number. Each such tile contributes `amount` (1 settle / 2 city) units of
its (unknown) type's resource. Accumulating across rolls lets us recover
the type->resource permutation without trusting the static table.

Usage: python scripts/volcano_calibrate.py sessions/active.jsonl
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import base64
import json

from catanbot.colonist_proto import load_capture, decode_frame
from catanbot.live_game import LiveGame
from catanbot.colonist_map import COLONIST_TILE_RESOURCE


def load_frames(path: Path):
    """Yield DecodedFrame from either a {"buffer":[...]} capture or a
    line-delimited .jsonl mirror (what the bridge writes via --ws-jsonl)."""
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("{") and '"buffer"' in stripped[:200]:
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
            frame = decode_frame(base64.b64decode(raw["b64"]), raw["dir"])
        except Exception:
            continue
        frame.ts = raw.get("ts", 0.0)
        yield frame

def main(path_str: str) -> int:
    path = Path(path_str).expanduser()
    game = LiveGame()

    # type -> {resource_name: weighted_units} co-occurrence on pure rolls
    score: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    pure_hits: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    prev_hand: dict[str, int] = {}
    rolls_seen = 0

    def self_breakdown() -> dict[str, int]:
        sess = game.session
        if sess is None or sess.self_color_id is None or game.tracker is None:
            return {}
        user = sess.player_names.get(sess.self_color_id)
        if not user:
            return {}
        try:
            color = game.color_map.get(user)
        except Exception:
            return {}
        return dict(game.tracker.hand(color))

    for frame in load_frames(path):
        if frame.error or not isinstance(frame.payload, dict):
            continue
        results = game.feed(frame.payload)
        sess = game.session
        if sess is None:
            continue
        # Did a roll resolve in this batch?
        rolled = None
        for r in results:
            if type(r.event).__name__ == "RollEvent":
                rolled = getattr(r.event, "total", None)
        if rolled is None or rolled == 7:
            # still refresh prev_hand snapshot
            cur = self_breakdown()
            if cur:
                prev_hand = cur
            continue
        rolls_seen += 1
        cur = self_breakdown()
        if not cur or not prev_hand:
            prev_hand = cur or prev_hand
            continue
        delta = {r: cur.get(r, 0) - prev_hand.get(r, 0)
                 for r in set(cur) | set(prev_hand)}
        delta = {r: d for r, d in delta.items() if d > 0}
        prev_hand = cur
        if not delta:
            continue
        # which of self's tiles produced on this roll. corner_owners are
        # keyed by colonist color-id ints, so compare to self_color_id.
        contrib: dict[int, int] = defaultdict(int)  # type_int -> units
        cid = sess.self_color_id
        for tid, dice in sess.mapping.tile_dice.items():
            if dice != rolled or tid == sess.robber_tile_id:
                continue
            ty = sess.mapping.tile_types.get(tid, 0)
            for corner in sess.mapping.tile_corners.get(tid, ()):  # type: ignore
                if sess.corner_owners.get(corner) != cid:
                    continue
                bt = sess.known_corners.get(corner, 0)
                if bt in (1, 2):
                    contrib[ty] += 2 if bt == 2 else 1
        if not contrib:
            continue
        # Pure roll: exactly one type contributed -> every gained resource
        # is that type's resource.
        if len(contrib) == 1:
            (ty, units), = contrib.items()
            for r, d in delta.items():
                pure_hits[ty][r] += d
        # General co-occurrence signal
        for ty, units in contrib.items():
            for r, d in delta.items():
                score[ty][r] += min(units, d)

    print(f"rolls analyzed: {rolls_seen}")
    print(f"static table:   {COLONIST_TILE_RESOURCE}")
    print("\n--- PURE-ROLL evidence (type -> resource: hits) ---")
    for ty in sorted(pure_hits):
        ev = dict(sorted(pure_hits[ty].items(), key=lambda kv: -kv[1]))
        print(f"  type {ty}: {ev}")
    print("\n--- co-occurrence score (type -> resource) ---")
    for ty in sorted(score):
        ev = dict(sorted(score[ty].items(), key=lambda kv: -kv[1]))
        print(f"  type {ty}: {ev}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "sessions/active.jsonl"))
