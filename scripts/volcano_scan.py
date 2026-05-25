"""Authoritative Volcano board scan + fog-reveal mining.

Parses the GameStart via LiveSession.from_game_start (the real mapping
path), prints the tile-type histogram and gold/fog positions, then walks
the whole capture collecting every TileRevealEvent so we can see what
resources + numbers the fog hides.
"""
from __future__ import annotations

import base64
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from catanbot.colonist_proto import decode_frame
from catanbot.live_game import LiveGame
from catanbot.colonist_map import (COLONIST_TILE_RESOURCE, FOG_TILE_TYPES,
                                    tile_resource)

RES = {0: "DESERT", 1: "WOOD", 2: "BRICK", 3: "SHEEP", 4: "WHEAT",
       5: "ORE", 6: "GOLD", 7: "FOG", 8: "FOG2"}


def frames(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
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
            fr = decode_frame(base64.b64decode(raw["b64"]), raw["dir"])
        except Exception:
            continue
        yield fr


def _report_game(idx, tt, dice, reveals):
    hist = Counter(tt.values())
    visible_res = Counter()
    for t, ty in tt.items():
        r = tile_resource(ty)
        if r:
            visible_res[r] += 1
    golds = [t for t, ty in tt.items() if ty == 6]
    fogs = [t for t, ty in tt.items() if ty in FOG_TILE_TYPES]
    print(f"\n========== GAME #{idx} ==========")
    print(f"tiles: {len(tt)}  histogram: "
          f"{ {RES.get(k,k): v for k,v in sorted(hist.items())} }")
    print(f"visible producing resources: {dict(visible_res)}")
    print(f"gold tiles: {len(golds)}  fog tiles: {len(fogs)}")
    for g in golds:
        print(f"  gold tid={g} number={dice.get(g)}")
    rdist = Counter(r for r, _ in reveals)
    ndist = Counter(n for _, n in reveals if n)
    print(f"fog reveals: {len(reveals)}  resource dist: {dict(rdist)}")
    print(f"  reveal number dist: {dict(sorted(ndist.items()))}")
    missing = [r for r in ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")
               if visible_res.get(r, 0) == 0]
    print(f"  resources ABSENT from visible board: {missing}")
    revealed_res = set(rdist)
    print(f"  resources that fog revealed:          {sorted(revealed_res)}")


def main(path_str: str) -> int:
    path = Path(path_str).expanduser()
    game = LiveGame()
    cur_tt = None
    cur_dice = None
    cur_reveals: list[tuple] = []
    game_idx = 0
    sig = None

    def flush():
        nonlocal game_idx
        if cur_tt:
            game_idx += 1
            _report_game(game_idx, cur_tt, cur_dice, cur_reveals)

    for fr in frames(path):
        if fr.error or not isinstance(fr.payload, dict):
            continue
        results = game.feed(fr.payload)
        sess = game.session
        if sess is None:
            continue
        tt = sess.mapping.tile_types
        if tt:
            new_sig = (len(tt), tuple(sorted(Counter(tt.values()).items())))
            if new_sig != sig:
                # new board detected
                flush()
                sig = new_sig
                cur_tt = dict(tt)
                cur_dice = dict(sess.mapping.tile_dice)
                cur_reveals = []
        for r in results:
            if type(r.event).__name__ == "TileRevealEvent":
                ev = r.event
                cur_reveals.append((getattr(ev, "resource", None),
                                    getattr(ev, "dice", None)
                                    or getattr(ev, "number", None)))
    flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "sessions/active.jsonl"))
