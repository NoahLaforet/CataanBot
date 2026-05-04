"""Advisor scoring: opening, second-settle, robber, trade."""
from __future__ import annotations

import pytest

from cataanbot.advisor import (
    _vp_weight,
    evaluate_trade,
    legal_nodes_after_picks,
    score_opening_nodes,
    score_robber_targets,
    score_second_settlements,
)
from cataanbot.tracker import Tracker


@pytest.fixture
def tracker():
    return Tracker(seed=4242)


def test_score_opening_nodes_returns_ranked_results(tracker):
    scores = score_opening_nodes(tracker.game)
    assert len(scores) > 0
    # Sorted descending.
    for a, b in zip(scores, scores[1:]):
        assert a.score >= b.score


def test_score_opening_nodes_respects_legal_pool(tracker):
    all_scores = score_opening_nodes(tracker.game)
    picked = all_scores[0].node_id
    legal = legal_nodes_after_picks(tracker.game, [picked])
    filtered = score_opening_nodes(tracker.game, legal_nodes=legal)
    filtered_ids = {s.node_id for s in filtered}
    assert picked not in filtered_ids
    # No neighbor of the picked node is eligible either.
    assert len(filtered_ids) < len(all_scores)


def test_score_opening_top_node_is_on_a_good_number(tracker):
    """Top opening pick should touch at least one high-pip (6/8) or
    multi-resource tile — guards against a regression that would rank
    desert-adjacent corner nodes highly."""
    top = score_opening_nodes(tracker.game)[0]
    numbers = [n for _res, n in top.tiles if n is not None]
    # raw_production is the per-roll expected-yield sum (each tile's
    # probability ×1), so a 3-tile inland node caps around ~0.4. Sanity:
    # the top node should touch at least 2 numbered tiles with nonzero
    # expected yield, not a corner desert-adjacent spot.
    assert top.raw_production > 0.2
    assert len(numbers) >= 2


def test_port_bonus_scales_with_produced_resource():
    """A 2:1 port on a produced resource should outweigh 3:1 generic,
    a 2:1 on an unproduced resource should still outrank 3:1 generic
    (resource-specific future option value > pure flexibility), and a
    richer-production corner on the same port should be valued more
    than a leaner one."""
    from cataanbot.advisor import _port_bonus
    # 3:1 generic: tiebreaker only.
    generic = _port_bonus("3:1", {"WHEAT": 0.3, "ORE": 0.3})
    # 2:1 on unproduced: small but strictly above 3:1.
    unprod = _port_bonus("SHEEP 2:1", {"WHEAT": 0.3, "ORE": 0.3})
    # 2:1 on a lightly-produced resource: scales with production.
    prod_light = _port_bonus("WHEAT 2:1", {"WHEAT": 1.0})
    # 2:1 on a strongly-produced resource: base + prod scaling.
    prod_heavy = _port_bonus("WHEAT 2:1", {"WHEAT": 5.0})
    assert generic > 0 and unprod > 0
    assert unprod > generic, (
        "2:1 on a future-expansion resource has more option value than "
        "a generic 3:1 — the previous calibration had this backwards.")
    assert prod_light > generic
    assert prod_light > unprod
    assert prod_heavy > prod_light
    # No port → zero bonus.
    assert _port_bonus(None, {"WHEAT": 3.0}) == 0.0


def test_port_bonus_does_not_tier_flip_against_three_tile_corner():
    """A 2-tile coastal corner with a 3:1 generic port should NOT
    outrank a 3-tile interior corner with comparable raw production.

    Regression test for the screenshot Noah hit on a real opening
    pick: a 2-tile coastal `8 wheat / 10 wood + 3:1` showed score 7.3,
    edging out a 3-tile interior `9 wood / 2 brick / 6 sheep` at 7.2.
    Root cause was port_bonus(3:1) returning 0.10 — the same
    magnitude as a whole tile of raw production (cards-per-roll), so
    the bonus closed the gap that diversity should have kept open.

    The fix is calibration: 3:1 ports return ~0.005, well under the
    ~0.06 raw-production gap between a 2-tile and 3-tile corner with
    similar pip totals.
    """
    from cataanbot.advisor import _DIVERSITY_BY_COUNT, _port_bonus
    # Approximate cards-per-roll for the screenshot's two-tile pick.
    # 8 (5 pips) ≈ 0.139, 10 (3 pips) ≈ 0.083 → ~0.222 raw across two
    # distinct resources. Plus a 3:1 port:
    coastal_two_tile_raw = 0.222
    coastal_two_tile_port = _port_bonus("3:1", {"WHEAT": 0.139,
                                                "WOOD": 0.083})
    coastal_score = (coastal_two_tile_raw * _DIVERSITY_BY_COUNT[2]
                     + coastal_two_tile_port)

    # Three-tile interior: 9 (4 pips) ≈ 0.111, 2 (1 pip) ≈ 0.028,
    # 6 (5 pips) ≈ 0.139 → ~0.278 raw across 3 resources.
    interior_three_tile_raw = 0.278
    interior_score = (interior_three_tile_raw * _DIVERSITY_BY_COUNT[3]
                      + 0.0)

    assert interior_score > coastal_score, (
        f"3-tile interior (score={interior_score:.3f}) should outrank "
        f"2-tile coastal+3:1 (score={coastal_score:.3f}) — port bonus "
        "is over-weighted")


def test_score_second_settlements_excludes_first_node(tracker):
    top = score_opening_nodes(tracker.game)[0]
    seconds = score_second_settlements(tracker.game, top.node_id, color="RED")
    ids = {s.node_id for s in seconds}
    assert top.node_id not in ids


def test_second_settle_port_bonus_shares_first_settle_formula(tracker):
    """Second-settle port scoring used to run its own ad-hoc formula
    (0.03 base + 0.3 * combined) which was 3× hotter than the first-
    settle curve and could hit 0.9+ on a port-matching pair. Unified
    onto _port_bonus — the same curve that governs first-settle. The
    guard: no port bonus may exceed the per-resource cap (0.15 + 0.05
    * combined-pips-on-that-resource), which is what the shared helper
    would return."""
    from cataanbot.advisor import (
        _port_bonus, score_second_settlements)
    top = score_opening_nodes(tracker.game)[0]
    seconds = score_second_settlements(tracker.game, top.node_id, color="RED")
    # Recompute what the unified helper would return given the same
    # combined dict that score_second_settlements uses internally.
    # combined = first-node production + this-node production, per
    # resource. We only have this node's production on the result, so
    # rebuild combined by adding the first-node side.
    m = tracker.game.state.board.map
    first_prod = m.node_production.get(top.node_id, {})
    had_port_nodes = False
    for s in seconds:
        if not s.port:
            continue
        had_port_nodes = True
        combined = {r: first_prod.get(r, 0.0) + s.resources.get(r, 0.0)
                    for r in s.resources}
        expected = _port_bonus(s.port, combined)
        assert abs(s.port_bonus - expected) < 1e-9, (
            f"node {s.node_id} port {s.port}: got {s.port_bonus}, "
            f"expected {expected} from shared helper")
    assert had_port_nodes, "fixture should include at least one port node"


def test_score_robber_targets_skips_current_robber(tracker):
    from catanatron import Color
    # Move robber onto a specific tile we can verify is skipped.
    robber = tracker.game.state.board.robber_coordinate
    results = score_robber_targets(tracker.game, "RED")
    assert all(r.coord != robber for r in results)
    assert len(results) > 0


def test_robber_targets_favor_opponent_builds(tracker):
    """A tile with an opponent settlement should outscore one with nobody."""
    from catanatron import Color
    # Place BLUE on a high-pip spot.
    top = score_opening_nodes(tracker.game)[0]
    tracker.settle("BLUE", top.node_id)
    scores = score_robber_targets(tracker.game, "RED")
    with_victim = [s for s in scores if s.opponent_blocked > 0]
    assert with_victim, "placing BLUE should create at least one robber target"
    # The highest overall score should hit an opponent.
    assert scores[0].opponent_blocked > 0


def test_friendly_robber_threshold_configurable():
    """The Friendly Robber threshold is configurable via
    cataanbot.config — house rules can use values other than 2.
    Setting it to 0 effectively disables the rule even when the
    flag is on."""
    from cataanbot import config
    # Restore default at end so other tests don't see drift.
    original = config.get_friendly_robber_protected_vp()
    try:
        config.set_friendly_robber_protected_vp(3)
        assert config.get_friendly_robber_protected_vp() == 3
        config.set_friendly_robber_protected_vp(0)
        assert config.get_friendly_robber_protected_vp() == 0
        # Negative values rejected.
        import pytest
        with pytest.raises(ValueError):
            config.set_friendly_robber_protected_vp(-1)
    finally:
        config.set_friendly_robber_protected_vp(original)


def test_friendly_robber_filters_low_vp_victims(tracker):
    """Colonist's optional Friendly Robber rule protects players at or
    below a VP threshold. score_robber_targets should drop those
    victims from each tile's victim list when the threshold is set, so
    the ranking matches what colonist's UI will actually allow Noah to
    pick."""
    # Place BLUE on a high-pip spot (BLUE's VP = 1 from one settlement).
    top = score_opening_nodes(tracker.game)[0]
    tracker.settle("BLUE", top.node_id)
    # Without the rule: BLUE shows up as a victim.
    free_play = score_robber_targets(tracker.game, "RED")
    has_blue_free = any("BLUE" in s.victims for s in free_play)
    assert has_blue_free, "BLUE should be a victim when rule is off"
    # With friendly_robber_min_vp=2, BLUE (1 VP) is protected.
    rule_on = score_robber_targets(
        tracker.game, "RED", friendly_robber_min_vp=2)
    has_blue_rule = any("BLUE" in s.victims for s in rule_on)
    assert not has_blue_rule, (
        "BLUE shouldn't appear as a victim with friendly robber on")


def test_evaluate_trade_delta_sign(tracker):
    # With no buildings, every resource has equal marginal value, so
    # giving N for N of a different resource is a wash.
    e = evaluate_trade(tracker.game, "RED", 1, "WOOD", 1, "WHEAT")
    assert abs(e.delta) < 1e-6
    # Giving 2 wood for 1 wheat at equal marginal value is unfavorable.
    e = evaluate_trade(tracker.game, "RED", 2, "WOOD", 1, "WHEAT")
    assert e.delta < 0


def test_evaluate_trade_favors_scarce_resource(tracker):
    """If RED produces lots of WOOD and no WHEAT, getting WHEAT in
    return for WOOD at 1:1 should be favorable."""
    from catanatron import Color
    # Pick a node that produces WOOD but no WHEAT so the asymmetry is
    # unambiguous in marginal-value terms.
    m = tracker.game.state.board.map
    pick = None
    for nid in m.land_nodes:
        prod = m.node_production.get(nid, {})
        if prod.get("WOOD", 0) > 0.1 and prod.get("WHEAT", 0) == 0:
            pick = nid
            break
    assert pick is not None
    tracker.game.state.board.build_settlement(
        Color.RED, pick, initial_build_phase=True
    )
    e = evaluate_trade(tracker.game, "RED", 1, "WOOD", 1, "WHEAT")
    assert e.delta > 0


def test_vp_weight_preserves_legacy_10vp_calibration():
    """The robber VP-weight ramp used to be anchored at the hardcoded
    baseline of 3 VP. After the config refactor the baseline is derived
    (early_game_baseline_vp = round(0.3 * target)), but for the default
    10-VP game the output must match the old calibration exactly —
    otherwise every robber score calibrated against the old scale
    drifts silently."""
    # Legacy: below baseline (3 VP) all clamp to 1.0.
    assert _vp_weight(0) == 1.0
    assert _vp_weight(3) == 1.0
    # Legacy: 0.4 per VP above baseline.
    assert _vp_weight(6) == pytest.approx(2.2)
    assert _vp_weight(9) == pytest.approx(3.4)


def test_vp_weight_scales_with_custom_target():
    """For a 12-VP game the baseline lifts to 4, so vp=3 is now
    sub-baseline (weight 1.0) and the ramp above 4 matches the linear
    slope. This is the whole point of making target configurable."""
    # 12 * 0.3 = 3.6 → baseline=4
    assert _vp_weight(3, vp_target=12) == 1.0
    assert _vp_weight(4, vp_target=12) == 1.0
    assert _vp_weight(7, vp_target=12) == pytest.approx(2.2)


def test_weighted_raw_production_boosts_wheat():
    """Reddit 36k-game finding #2: wheat is the #1 winning resource
    (used in every major build). _weighted_raw_production should
    return a higher number for an equal-pip wheat node vs an equal-
    pip non-wheat node so the opening eval reads wheat as more
    valuable per card.
    """
    from cataanbot.advisor import _weighted_raw_production

    # Same total cards/roll, different resource composition.
    wheat_node = {"WHEAT": 0.139, "BRICK": 0.083}
    ore_node   = {"ORE":   0.139, "BRICK": 0.083}
    assert (_weighted_raw_production(wheat_node)
            > _weighted_raw_production(ore_node)), (
        "wheat-bearing corner should score above an ore-bearing "
        "corner of equal total pip production")
    # Bias is small enough not to swamp diversity differences.
    diff = (_weighted_raw_production(wheat_node)
            - _weighted_raw_production(ore_node))
    assert diff < 0.05, (
        f"wheat bias is too aggressive: {diff:.4f} cards/roll over "
        f"the equal-pip baseline")


def test_diversity_multiplier_reflects_composition_over_pips():
    """Reddit 36k-game finding #3: composition beats raw pips. The
    3-distinct boost should clear a roughly equal-pip 1-distinct
    stack, since the data shows 3-resource corners dominating even
    at lower pip counts (highest-win-rate placement was 16 pips).
    """
    from cataanbot.advisor import _DIVERSITY_BY_COUNT
    # 3-distinct boost > 2-distinct boost > flat.
    assert _DIVERSITY_BY_COUNT[3] > _DIVERSITY_BY_COUNT[2] > _DIVERSITY_BY_COUNT[1]
    # Spread between 1-distinct and 3-distinct must clear ~20% so a
    # 0.20-raw 3-distinct corner reads above an equal-raw single-
    # resource stack. Below ~1.18 the boost stops clearing the gap
    # against ports + denial in real games.
    assert _DIVERSITY_BY_COUNT[3] >= 1.18
