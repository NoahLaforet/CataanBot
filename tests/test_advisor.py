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
    and a richer-production corner on the same port should be valued
    more than a leaner one."""
    from cataanbot.advisor import _port_bonus
    # 3:1 generic port: small fixed bonus.
    generic = _port_bonus("3:1", {"WHEAT": 0.3, "ORE": 0.3})
    # 2:1 on unproduced: still small (can't offload until expansion).
    unprod = _port_bonus("SHEEP 2:1", {"WHEAT": 0.3, "ORE": 0.3})
    # 2:1 on a lightly-produced resource: base bonus.
    prod_light = _port_bonus("WHEAT 2:1", {"WHEAT": 1.0})
    # 2:1 on a strongly-produced resource: base + prod scaling.
    prod_heavy = _port_bonus("WHEAT 2:1", {"WHEAT": 5.0})
    assert generic > 0 and unprod > 0
    assert prod_light > generic
    assert prod_light > unprod
    assert prod_heavy > prod_light
    # No port → zero bonus.
    assert _port_bonus(None, {"WHEAT": 3.0}) == 0.0


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
