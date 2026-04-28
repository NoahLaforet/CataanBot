"""Runtime configuration for game-mode-sensitive thresholds.

Catan's defaults (10 VP to win, discard at 8+ cards on a 7-roll) bake
into the heuristics all over the recommender and advisor. Extended
variants or table-rule games can shift those targets, so rather than
hardcode 10 at every comparison, callers derive thresholds from a
single ``VP_TARGET`` + ``DISCARD_LIMIT`` pair.

Initial values read from environment variables at module load:

    CATAANBOT_VP_TARGET    — default 10
    CATAANBOT_DISCARD_LIMIT — default 7 (discard-on-7 triggers at N+1)

Values are also runtime-mutable through ``set_vp_target`` /
``set_discard_limit`` so the userscript drawer can switch the bridge
into a 14-VP game mid-session without restart. The module-level
``VP_TARGET`` / ``DISCARD_LIMIT`` attributes always reflect the live
state via ``__getattr__`` — callers reading ``config.VP_TARGET``
inside a function get a fresh value every time. Top-of-module
``from cataanbot.config import VP_TARGET`` bindings, however, freeze
the value at import time and will NOT see updates; those callers were
migrated to lazy reads (see eval._discard_limit, report._discard_threshold).

The "close to winning" / "largest army threat" / "mid-late game"
thresholds scale with the live target via fixed ratios. For a 10 VP
game: close_to_win=8, la_threat=7, mid_late=6 — same as the old
hardcoded values. For a 12 VP game these scale to 10 / 8 / 7
automatically.
"""
from __future__ import annotations

import os
from typing import Any


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Runtime-mutable state. ``__getattr__`` exposes these as
# ``config.VP_TARGET`` / ``config.DISCARD_LIMIT`` so attribute access
# tracks the live value. Don't read this dict from outside the module —
# go through the accessors / module attributes.
_state: dict[str, int] = {
    "VP_TARGET": _env_int("CATAANBOT_VP_TARGET", 10),
    "DISCARD_LIMIT": _env_int("CATAANBOT_DISCARD_LIMIT", 7),
}


def __getattr__(name: str) -> Any:
    """Module-level attribute access (PEP 562). Lets callers read
    ``config.VP_TARGET`` and get the current live value, not a frozen
    import-time snapshot. Anything not in ``_state`` falls through to
    the standard AttributeError."""
    if name in _state:
        return _state[name]
    raise AttributeError(f"module 'cataanbot.config' has no attribute {name!r}")


def get_vp_target() -> int:
    return _state["VP_TARGET"]


def get_discard_limit() -> int:
    return _state["DISCARD_LIMIT"]


def set_vp_target(value: int) -> None:
    """Set the active VP target. Validated as a positive int — falsy
    or non-int values are rejected so a bad userscript POST can't
    silently zero the target."""
    n = int(value)
    if n < 1:
        raise ValueError(f"VP target must be >= 1, got {n}")
    _state["VP_TARGET"] = n


def set_discard_limit(value: int) -> None:
    """Set the active discard limit (the hand size *before* which a
    7-roll triggers a discard). Validated positive int."""
    n = int(value)
    if n < 1:
        raise ValueError(f"discard limit must be >= 1, got {n}")
    _state["DISCARD_LIMIT"] = n


# Ratios used to derive per-game-mode thresholds. Kept near 0.8 / 0.7 /
# 0.6 of target so a 10 VP game lands on the familiar 8 / 7 / 6 values.
_CLOSE_TO_WIN_RATIO = 0.80
_LARGEST_ARMY_THREAT_RATIO = 0.70
_MID_LATE_RATIO = 0.60
_EARLY_GAME_BASELINE_RATIO = 0.30


def close_to_win_vp(target: int | None = None) -> int:
    """VP at which an opponent counts as "close to winning" — cut off
    resource feeds, hold cards, skip generous trades.

    10 VP → 8. 12 VP → 10. Floored at 2 so small target games still
    have a meaningful gap. Reads the live ``VP_TARGET`` when called
    without an argument."""
    if target is None:
        target = _state["VP_TARGET"]
    return max(2, round(target * _CLOSE_TO_WIN_RATIO))


def largest_army_threat_vp(target: int | None = None) -> int:
    """VP at which an opponent playing knights becomes a largest-army
    race threat and self should start playing knights proactively.

    10 VP → 7. Scales linearly with target."""
    if target is None:
        target = _state["VP_TARGET"]
    return max(2, round(target * _LARGEST_ARMY_THREAT_RATIO))


def mid_late_vp(target: int | None = None) -> int:
    """VP at which the game transitions from "building up" to "closing
    out" — robber priorities shift toward VP-weighted blocking.

    10 VP → 6."""
    if target is None:
        target = _state["VP_TARGET"]
    return max(1, round(target * _MID_LATE_RATIO))


def early_game_baseline_vp(target: int | None = None) -> int:
    """VP below which blocking is worth close to baseline (no urgency).
    Used as the anchor for _vp_weight — above this the weight ramps.

    10 VP → 3. Same as the old hardcoded baseline."""
    if target is None:
        target = _state["VP_TARGET"]
    return max(1, round(target * _EARLY_GAME_BASELINE_RATIO))
