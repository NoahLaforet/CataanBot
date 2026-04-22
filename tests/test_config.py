"""Tests for game-mode threshold derivation in cataanbot.config.

The heuristics used to hardcode 8 / 7 / 6 / 3 VP thresholds for a 10-VP
game. We refactored those into ratio-based helpers so non-standard
targets (12 VP, 13 VP, etc.) scale cleanly. These tests pin the
default-10 behavior to the old hardcoded values and spot-check larger
targets.
"""
from cataanbot.config import (
    close_to_win_vp,
    early_game_baseline_vp,
    largest_army_threat_vp,
    mid_late_vp,
)


def test_default_10_vp_matches_legacy_thresholds():
    """For the standard 10-VP game, the helpers must reproduce the
    previously-hardcoded thresholds (8 / 7 / 6 / 3) — otherwise the
    refactor has silently changed existing behavior."""
    assert close_to_win_vp(10) == 8
    assert largest_army_threat_vp(10) == 7
    assert mid_late_vp(10) == 6
    assert early_game_baseline_vp(10) == 3


def test_12_vp_scales_proportionally():
    # 12 * 0.80 = 9.6 → 10 (round-half-to-even)
    assert close_to_win_vp(12) == 10
    # 12 * 0.70 = 8.4 → 8
    assert largest_army_threat_vp(12) == 8
    # 12 * 0.60 = 7.2 → 7
    assert mid_late_vp(12) == 7
    # 12 * 0.30 = 3.6 → 4
    assert early_game_baseline_vp(12) == 4


def test_13_vp_scales_proportionally():
    # 13 * 0.80 = 10.4 → 10
    assert close_to_win_vp(13) == 10
    # 13 * 0.70 = 9.1 → 9
    assert largest_army_threat_vp(13) == 9
    # 13 * 0.60 = 7.8 → 8
    assert mid_late_vp(13) == 8
    # 13 * 0.30 = 3.9 → 4
    assert early_game_baseline_vp(13) == 4


def test_small_target_respects_floors():
    """A pathologically small target (3 VP) still yields usable
    thresholds — close-to-win floors at 2 so there's always a gap
    above "just started"."""
    assert close_to_win_vp(3) >= 2
    assert largest_army_threat_vp(3) >= 2
    assert mid_late_vp(3) >= 1
    assert early_game_baseline_vp(3) >= 1


def test_monotonic_ordering_preserved():
    """For any reasonable target, the ordering of thresholds must hold:
    close_to_win > largest_army_threat > mid_late > early_game_baseline.
    If this breaks, downstream heuristics get inverted logic."""
    for target in (8, 10, 12, 13, 15, 20):
        assert close_to_win_vp(target) >= largest_army_threat_vp(target)
        assert largest_army_threat_vp(target) >= mid_late_vp(target)
        assert mid_late_vp(target) >= early_game_baseline_vp(target)


def test_module_defaults_present():
    """The module-level VP_TARGET / DISCARD_LIMIT constants must exist
    and be positive ints — they're imported by callers that don't want
    to pass a target each call."""
    from cataanbot import config

    assert isinstance(config.VP_TARGET, int)
    assert config.VP_TARGET > 0
    assert isinstance(config.DISCARD_LIMIT, int)
    assert config.DISCARD_LIMIT > 0


def test_helpers_default_to_module_target():
    """Calling the helpers without an argument must use the module
    VP_TARGET default."""
    from cataanbot import config

    assert close_to_win_vp() == close_to_win_vp(config.VP_TARGET)
    assert largest_army_threat_vp() == largest_army_threat_vp(config.VP_TARGET)
    assert mid_late_vp() == mid_late_vp(config.VP_TARGET)
    assert early_game_baseline_vp() == early_game_baseline_vp(config.VP_TARGET)
