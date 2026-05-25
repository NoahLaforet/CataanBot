"""Aggregate fog-reveal patterns across every capture we have.

For each capture: parse the starting board's visible resources, then walk
all TileRevealEvents and tally what resource + number each fog hex hid.
The question: is fog placement random, or does it systematically hide the
resources missing from the visible board?
"""
from __future__ import annotations

import base64
import json
import sys
from collections import Counter
from pathlib import Path

from catanbot.colonist_proto import decode_frame, load_capture
from catanbot.live_game import LiveGame
from catanbot.colonist_map import tile_resource, FOG_TILE_TYPES

ALL_RES = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")


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


def analyze(path: Path):
    game = LiveGame()
    visible0 = None
    reveals = []
    for fr in frames(path):
        if fr.error or not isinstance(fr.payload, dict):
            continue
        results = game.feed(fr.payload)
        sess = game.session
        if sess and visible0 is None and sess.mapping.tile_types:
            vis = Counter()
            for ty in sess.mapping.tile_types.values():
                r = tile_resource(ty)
                if r:
                    vis[r] += 1
            visible0 = vis
        for r in results:
            if type(r.event).__name__ == "TileRevealEvent":
                ev = r.event
                reveals.append((getattr(ev, "resource", None),
                                getattr(ev, "dice", None)
                                or getattr(ev, "number", None)))
    return visible0 or Counter(), reveals


def main(paths):
    grand_res = Counter()
    grand_num = Counter()
    grand_revealed_missing = 0
    grand_revealed_total = 0
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        vis, reveals = analyze(p)
        if not reveals:
            continue
        absent = {r for r in ALL_RES if vis.get(r, 0) == 0}
        rdist = Counter(r for r, _ in reveals)
        ndist = Counter(n for _, n in reveals if n)
        revealed_in_absent = sum(c for r, c in rdist.items() if r in absent)
        grand_revealed_missing += revealed_in_absent
        grand_revealed_total += sum(rdist.values())
        grand_res.update(rdist)
        grand_num.update(ndist)
        print(f"\n{p.name}")
        print(f"  visible resources:  {dict(vis)}")
        print(f"  absent from visible: {sorted(absent)}")
        print(f"  fog revealed:        {dict(rdist)}  (n={sum(rdist.values())})")
        print(f"  reveals hitting an absent resource: "
              f"{revealed_in_absent}/{sum(rdist.values())}")

    print("\n" + "=" * 50)
    print("AGGREGATE across all captures")
    print(f"  total fog reveals: {grand_revealed_total}")
    print(f"  resource dist: {dict(grand_res)}")
    print(f"  number dist:   {dict(sorted(grand_num.items()))}")
    if grand_revealed_total:
        pct = 100 * grand_revealed_missing / grand_revealed_total
        print(f"  reveals that landed on a resource ABSENT from the "
              f"visible board: {grand_revealed_missing}/{grand_revealed_total} "
              f"({pct:.0f}%)")
        # number quality: share of reveals on strong numbers (5,6,8,9)
        strong = sum(c for n, c in grand_num.items() if n in (5, 6, 8, 9))
        tot = sum(grand_num.values())
        if tot:
            print(f"  reveals on strong numbers (5/6/8/9): "
                  f"{strong}/{tot} ({100*strong/tot:.0f}%)")


if __name__ == "__main__":
    args = sys.argv[1:] or [
        "ws_captures/volcano-2026-05-23.jsonl",
        "ws_captures/blackforest-2026-05-18.jsonl",
        "ws_captures/blackforest-game2-2026-05-18.jsonl",
        "sessions/active.jsonl",
    ]
    main(args)
