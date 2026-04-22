"""Runtime configuration for game-mode-sensitive thresholds.

Catan's defaults (10 VP to win, discard at 8+ cards on a 7-roll) bake
into the heuristics all over the recommender and advisor. Extended
variants or table-rule games can shift those targets, so rather than
hardcode 10 at every comparison, callers derive thresholds from a
single `VP_TARGET` + `DISCARD_LIMIT` pair.

Values read from environment variables at module load:
    CATAANBOT_VP_TARGET    — default 10
    CATAANBOT_DISCARD_LIMIT — default 7 (discard-on-7 triggers at N+1)

The "close to winning" / "largest army threat" / "mid-late game"
thresholds scale with `VP_TARGET` via fixed ratios. For a 10 VP game:
close_to_win=8, la_threat=7, mid_late=6 — same as the old hardcoded
values. For a 12 VP game these scale to 10 / 8 / 7 automatically.
"""
from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


VP_TARGET: int = _env_int("CATAANBOT_VP_TARGET", 10)
DISCARD_LIMIT: int = _env_int("CATAANBOT_DISCARD_LIMIT", 7)


# Ratios used to derive per-game-mode thresholds. Kept near 0.8 / 0.7 /
# 0.6 of target so a 10 VP game lands on the familiar 8 / 7 / 6 values.
_CLOSE_TO_WIN_RATIO = 0.80
_LARGEST_ARMY_THREAT_RATIO = 0.70
_MID_LATE_RATIO = 0.60
_EARLY_GAME_BASELINE_RATIO = 0.30


def close_to_win_vp(target: int = VP_TARGET) -> int:
    """VP at which an opponent counts as "close to winning" — cut off
    resource feeds, hold cards, skip generous trades.

    10 VP → 8. 12 VP → 10. Floored at 2 so small target games still
    have a meaningful gap."""
    return max(2, round(target * _CLOSE_TO_WIN_RATIO))


def largest_army_threat_vp(target: int = VP_TARGET) -> int:
    """VP at which an opponent playing knights becomes a largest-army
    race threat and self should start playing knights proactively.

    10 VP → 7. Scales linearly with target."""
    return max(2, round(target * _LARGEST_ARMY_THREAT_RATIO))


def mid_late_vp(target: int = VP_TARGET) -> int:
    """VP at which the game transitions from "building up" to "closing
    out" — robber priorities shift toward VP-weighted blocking.

    10 VP → 6."""
    return max(1, round(target * _MID_LATE_RATIO))


def early_game_baseline_vp(target: int = VP_TARGET) -> int:
    """VP below which blocking is worth close to baseline (no urgency).
    Used as the anchor for _vp_weight — above this the weight ramps.

    10 VP → 3. Same as the old hardcoded baseline."""
    return max(1, round(target * _EARLY_GAME_BASELINE_RATIO))
