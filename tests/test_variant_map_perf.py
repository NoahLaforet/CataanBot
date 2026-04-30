"""Performance check on CatanMap construction.

The bridge calls ``build_mapping`` once at GameStart, so a multi-second
build would visibly delay the HUD ("Connecting…" hangs until the first
advisor snapshot returns). These tests pin the construction cost as a
performance regression guard:

* Classic 19/54/72/9 from a real capture should stay under 100ms median.
* Variant shapes (synthetic 7-tile flower as proxy) should also stay
  well under that — the variant builder synthesizes a water ring, which
  is the biggest cost we'd expect to grow.

The numbers are conservative: classic medians are ~0.4ms on Noah's M1,
so 100ms gives 200x headroom for slower CI / debug builds.
"""
from __future__ import annotations

import time
from pathlib import Path
from statistics import median

import pytest

CAPTURE_MIDGAME = (Path(__file__).parent.parent
                   / "ws_captures"
                   / "cataanbot-ws-fort4092-midgame-2026-04-21T23-34-04.json")


def _bench(fn, runs: int = 50) -> tuple[float, float]:
    """Return (best_ms, median_ms) over ``runs`` calls."""
    times: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000.0)
    return min(times), median(times)


def _find_map_state(payload):
    """Walk a capture payload to find a tileHexStates-bearing dict."""
    if isinstance(payload, dict):
        if payload.get("tileHexStates"):
            return payload
        for v in payload.values():
            r = _find_map_state(v)
            if r is not None:
                return r
    elif isinstance(payload, list):
        for v in payload:
            r = _find_map_state(v)
            if r is not None:
                return r
    return None


def test_classic_build_mapping_under_100ms_median():
    """Classic build_mapping should finish in well under 100ms median.

    This is a backstop against accidental quadratic behavior — the
    function has historically been ~0.4ms on a real capture, so 100ms
    is a 200x cliff that only fires on a real regression."""
    if not CAPTURE_MIDGAME.exists():
        pytest.skip("midgame capture not present")
    from cataanbot.colonist_map import build_mapping
    from cataanbot.colonist_proto import load_capture

    map_state = None
    for frame in load_capture(CAPTURE_MIDGAME):
        if frame.error:
            continue
        ms = _find_map_state(frame.payload)
        if ms is not None:
            map_state = ms
            break
    assert map_state is not None, "no mapState found in capture"

    # Sanity-check it's a classic shape before benchmarking.
    assert len(map_state["tileHexStates"]) == 19

    best, med = _bench(lambda: build_mapping(map_state), runs=100)
    # Generous bound — local M1 sees ~0.4ms median; CI could be 10x
    # slower under coverage / debug. 100ms still represents a real
    # regression.
    assert med < 100.0, f"classic build_mapping median {med:.2f}ms > 100ms"


def test_variant_build_mapping_under_100ms_median():
    """Variant CatanMap construction (water-ring synthesis + token
    placement) on a 7-tile flower stays under 100ms median.

    Synthetic shape because we don't have a checked-in variant capture
    yet — the flower exercises the variant code path (non-classic shape
    bypasses the BASE_MAP_TEMPLATE branch). Benchmarks the heavier
    custom CatanMap builder, not the cheaper template path."""
    from cataanbot.colonist_map import (
        build_mapping, corner_tile_signature, edge_endpoint_signatures,
    )

    positions = [
        (0, 0), (1, -1), (-1, 1),
        (1, 0), (-1, 0), (0, 1), (0, -1),
    ]
    pos_set = set(positions)
    types = [0, 1, 2, 3, 4, 5, 1]
    dice = [0, 4, 5, 6, 8, 9, 10]

    hex_states = {}
    for tid, (x, y) in enumerate(positions, start=1):
        hex_states[str(tid)] = {
            "x": x, "y": y, "type": types[tid - 1],
            "diceNumber": dice[tid - 1],
        }

    corner_states = {}
    seen_c = set()
    cid = 0
    for x, y in positions:
        for cx in range(x - 1, x + 2):
            for cy in range(y - 1, y + 2):
                for cz in (0, 1):
                    sig = corner_tile_signature(cx, cy, cz)
                    if sig in seen_c or not any(t in pos_set for t in sig):
                        continue
                    seen_c.add(sig)
                    cid += 1
                    corner_states[str(cid)] = {"x": cx, "y": cy, "z": cz}

    edge_states = {}
    seen_e = set()
    eid = 0
    for x, y in positions:
        for ex in range(x - 1, x + 2):
            for ey in range(y - 1, y + 2):
                for ez in (0, 1, 2):
                    try:
                        a, b = edge_endpoint_signatures(ex, ey, ez)
                    except Exception:  # noqa: BLE001
                        continue
                    key = frozenset((a, b))
                    if (key in seen_e
                            or not any(t in pos_set for t in a)
                            or not any(t in pos_set for t in b)):
                        continue
                    seen_e.add(key)
                    eid += 1
                    edge_states[str(eid)] = {"x": ex, "y": ey, "z": ez}

    map_state = {
        "tileHexStates": hex_states,
        "tileCornerStates": corner_states,
        "tileEdgeStates": edge_states,
        "portEdgeStates": {},
    }

    best, med = _bench(lambda: build_mapping(map_state), runs=50)
    assert med < 100.0, f"variant build_mapping median {med:.2f}ms > 100ms"


def test_variant_build_no_quadratic_blowup():
    """Variant build cost should not be wildly worse than classic.

    Hard to assert tightly across machines, so we use a soft 30x ceiling:
    if variant takes more than 30x what classic takes, something's
    quadratic in tile count and the bridge will lag noticeably on bigger
    boards (Pond=24, Black-Forest=larger)."""
    if not CAPTURE_MIDGAME.exists():
        pytest.skip("midgame capture not present")
    from cataanbot.colonist_map import (
        build_mapping, corner_tile_signature, edge_endpoint_signatures,
    )
    from cataanbot.colonist_proto import load_capture

    classic_ms = None
    for frame in load_capture(CAPTURE_MIDGAME):
        if frame.error:
            continue
        ms = _find_map_state(frame.payload)
        if ms is not None:
            classic_ms = ms
            break
    assert classic_ms is not None

    # Build the same flower variant fixture as the previous test.
    positions = [
        (0, 0), (1, -1), (-1, 1),
        (1, 0), (-1, 0), (0, 1), (0, -1),
    ]
    pos_set = set(positions)
    hex_states = {
        str(tid): {"x": x, "y": y, "type": tid % 6,
                   "diceNumber": (tid % 11) + 2}
        for tid, (x, y) in enumerate(positions, start=1)
    }
    corner_states = {}
    cid = 0
    seen_c = set()
    for x, y in positions:
        for cx in range(x - 1, x + 2):
            for cy in range(y - 1, y + 2):
                for cz in (0, 1):
                    sig = corner_tile_signature(cx, cy, cz)
                    if sig in seen_c or not any(t in pos_set for t in sig):
                        continue
                    seen_c.add(sig)
                    cid += 1
                    corner_states[str(cid)] = {"x": cx, "y": cy, "z": cz}
    edge_states = {}
    eid = 0
    seen_e = set()
    for x, y in positions:
        for ex in range(x - 1, x + 2):
            for ey in range(y - 1, y + 2):
                for ez in (0, 1, 2):
                    try:
                        a, b = edge_endpoint_signatures(ex, ey, ez)
                    except Exception:  # noqa: BLE001
                        continue
                    key = frozenset((a, b))
                    if (key in seen_e
                            or not any(t in pos_set for t in a)
                            or not any(t in pos_set for t in b)):
                        continue
                    seen_e.add(key)
                    eid += 1
                    edge_states[str(eid)] = {"x": ex, "y": ey, "z": ez}
    variant_ms = {
        "tileHexStates": hex_states,
        "tileCornerStates": corner_states,
        "tileEdgeStates": edge_states, "portEdgeStates": {},
    }

    _, classic_med = _bench(lambda: build_mapping(classic_ms), runs=50)
    _, variant_med = _bench(lambda: build_mapping(variant_ms), runs=50)
    # Classic median is so small (sub-ms) that division noise can
    # make the ratio explode. Floor the denominator so we don't fail
    # spuriously on a fast classic run.
    classic_floor = max(classic_med, 0.5)
    ratio = variant_med / classic_floor
    assert ratio < 30.0, (
        f"variant build_mapping {variant_med:.2f}ms is "
        f"{ratio:.1f}x classic ({classic_med:.2f}ms) — possible "
        "quadratic regression in variant code path"
    )
