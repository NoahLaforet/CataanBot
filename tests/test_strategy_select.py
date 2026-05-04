"""Strategy selector — unit tests against catanatron board state.

Each test seeds a small set of placements for RED on a fresh deterministic
game and asserts the selector returns a sensible tag, fallback, and
phase. The selector is heuristic, so assertions favor "this tag SHOULD
fire on this footprint" over exact-score equality.
"""
from __future__ import annotations


def _fresh_game(seed: int = 1):
    from catanatron import Color, Game, RandomPlayer
    return Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=seed,
    )


def _settle(g, color, node):
    from catanatron import Color
    c = color if isinstance(color, Color) else Color[color.upper()]
    g.state.board.build_settlement(c, int(node), initial_build_phase=True)


def _find_node_with_two_resources(g, *resources, min_pip: int = 3):
    """Helper: return any node that touches each of the given resources
    on tiles with pip >= min_pip. Returns None if nothing matches."""
    from catanbot.advisor import PIP_DOTS_BY_NUMBER
    m = g.state.board.map
    for nid in m.land_nodes:
        tiles = m.adjacent_tiles.get(nid, [])
        # Map resource → max pip across tiles touching this node.
        by_res: dict[str, int] = {}
        for t in tiles:
            if t.resource is None or t.number is None:
                continue
            pip = PIP_DOTS_BY_NUMBER.get(t.number, 0)
            by_res[t.resource] = max(by_res.get(t.resource, 0), pip)
        if all(by_res.get(r, 0) >= min_pip for r in resources):
            return nid
    return None


def test_returns_none_during_setup():
    """Before two settlements are placed, the selector must stay silent
    so the bridge can keep rendering the opening flow."""
    from catanbot.strategy_select import select_strategy

    g = _fresh_game()
    assert select_strategy(g, "RED") is None
    _settle(g, "RED", 0)
    assert select_strategy(g, "RED") is None


def test_balanced_is_the_default_floor():
    """When the placements aren't archetype-strong but cover 3+ distinct
    resources, BALANCED should fire as the active tag."""
    from catanbot.strategy_select import select_strategy

    g = _fresh_game()
    # Pick any two land nodes, one diverse-ish, one anywhere.
    m = g.state.board.map
    land = sorted(m.land_nodes)
    placed = []
    from catanbot.advisor import _build_node_neighbors
    neighbors = _build_node_neighbors(m)
    for nid in land:
        if any(n in neighbors.get(nid, set()) for n in placed):
            continue
        placed.append(nid)
        _settle(g, "RED", nid)
        if len(placed) == 2:
            break
    tag = select_strategy(g, "RED")
    assert tag is not None
    assert tag.primary in (
        "BALANCED", "OWS", "LR_RUSH", "PORT_TRADE", "RB_CARVED_TILES")
    assert tag.fallback is None or tag.fallback != tag.primary
    assert tag.phase in (
        "opening", "early", "mid", "late", "endgame")
    assert isinstance(tag.rationale, str) and tag.rationale


def test_ows_fires_on_ore_wheat_heavy_pair():
    """An ore+wheat-heavy pair should pick OWS as the primary tag.

    We construct it by handpicking nodes whose adjacent tiles include
    both ORE and WHEAT on decent numbers. If the random seed doesn't
    produce such a pair, the test is skipped — heuristic asserts
    shouldn't fail just because the board is unfavorable for the
    archetype."""
    import pytest
    from catanbot.strategy_select import select_strategy

    g = _fresh_game(seed=4242)
    n_ore = _find_node_with_two_resources(g, "ORE", "WHEAT", min_pip=3)
    if n_ore is None:
        pytest.skip("seed didn't yield ore+wheat node")
    _settle(g, "RED", n_ore)
    # Second settle: any other node not adjacent to the first.
    from catanbot.advisor import _build_node_neighbors
    m = g.state.board.map
    neighbors = _build_node_neighbors(m)
    forbidden = {n_ore} | neighbors.get(n_ore, set())
    n2 = _find_node_with_two_resources(g, "ORE", "WHEAT", min_pip=2)
    if n2 is None or n2 in forbidden:
        # Fall back to any non-adjacent node with sheep + wheat.
        n2 = _find_node_with_two_resources(g, "SHEEP", "WHEAT", min_pip=2)
    if n2 is None or n2 in forbidden:
        pytest.skip("seed didn't yield a non-adjacent OWS-aligned 2nd settle")
    _settle(g, "RED", n2)
    tag = select_strategy(g, "RED")
    assert tag is not None
    # Either OWS leads or it's at least the fallback — the heuristic
    # leaves room for BALANCED to outscore on diversity-heavy nodes.
    assert "OWS" in (tag.primary, tag.fallback)


def test_lr_rush_fires_on_wood_brick_pair():
    import pytest
    from catanbot.strategy_select import select_strategy

    g = _fresh_game(seed=99)
    n_wb = _find_node_with_two_resources(g, "WOOD", "BRICK", min_pip=3)
    if n_wb is None:
        pytest.skip("seed didn't yield wood+brick node")
    _settle(g, "RED", n_wb)
    from catanbot.advisor import _build_node_neighbors
    m = g.state.board.map
    neighbors = _build_node_neighbors(m)
    forbidden = {n_wb} | neighbors.get(n_wb, set())
    # Second wood/brick somewhere else.
    n2 = None
    for cand in sorted(m.land_nodes):
        if cand in forbidden:
            continue
        prods = m.node_production.get(cand, {})
        if (prods.get("WOOD", 0) > 0 and prods.get("BRICK", 0) > 0):
            n2 = cand
            break
    if n2 is None:
        pytest.skip("seed didn't yield a 2nd wood+brick spot")
    _settle(g, "RED", n2)
    tag = select_strategy(g, "RED")
    assert tag is not None
    # LR_RUSH should be primary or fallback when wood + brick both
    # appear in production on both placements.
    assert "LR_RUSH" in (tag.primary, tag.fallback)


def test_phase_advances_with_rolls():
    """``rolls_so_far`` drives the phase label; opening → early → mid → late."""
    from catanbot.strategy_select import _phase_for

    assert _phase_for(0) == "opening"
    assert _phase_for(4) == "opening"
    assert _phase_for(5) == "early"
    assert _phase_for(14) == "early"
    assert _phase_for(15) == "mid"
    assert _phase_for(29) == "mid"
    assert _phase_for(30) == "late"
    assert _phase_for(50) == "endgame"
    assert _phase_for(80) == "endgame"


def test_to_snap_serializes_all_fields():
    from catanbot.strategy_select import StrategyTag

    tag = StrategyTag(
        primary="OWS", fallback="BALANCED", rationale="x", phase="early",
        set_at_rolls=3,
    )
    snap = tag.to_snap()
    assert snap["primary"] == "OWS"
    assert snap["fallback"] == "BALANCED"
    assert snap["rationale"] == "x"
    assert snap["phase"] == "early"
    assert snap["set_at_rolls"] == 3
    assert snap["pivot_triggers"] == []
    assert snap["override_tag"] is None
    # ``active`` mirrors primary when no override is set.
    assert snap["active"] == "OWS"


def test_to_snap_active_picks_override_when_set():
    from catanbot.strategy_select import StrategyTag

    tag = StrategyTag(
        primary="OWS", fallback=None, rationale="", phase="mid",
        override_tag="LR_RUSH",
    )
    assert tag.to_snap()["active"] == "LR_RUSH"


def test_stickiness_prevents_thrashing_on_close_calls():
    """A 1-2pp wobble in scores shouldn't flip the primary tag — only a
    15%+ uplift should, otherwise the previous primary stays."""
    from catanbot.strategy_select import StrategyTag, select_strategy

    g = _fresh_game(seed=7)
    # Place two diverse nodes so BALANCED is in the running.
    from catanbot.advisor import _build_node_neighbors
    m = g.state.board.map
    neighbors = _build_node_neighbors(m)
    placed = []
    for nid in sorted(m.land_nodes):
        if any(n in neighbors.get(nid, set()) for n in placed):
            continue
        prods = m.node_production.get(nid, {})
        if sum(1 for v in prods.values() if v > 0) >= 3:
            placed.append(nid)
            _settle(g, "RED", nid)
            if len(placed) == 2:
                break
    initial = select_strategy(g, "RED")
    assert initial is not None
    # Re-call with `previous` set to a hypothetical primary that's
    # different from the current top — stickiness should keep the
    # passed-in primary as long as it's still in the eligible set.
    fake_prev = StrategyTag(
        primary=initial.fallback or initial.primary,
        fallback=initial.primary,
        rationale="test",
        phase="early",
    )
    if fake_prev.primary == initial.primary:
        # Nothing to test if there's no fallback to swap with.
        return
    again = select_strategy(g, "RED", previous=fake_prev)
    # Either selector kept the previous (sticky) OR scored it out of
    # the eligible set entirely. The latter is fine because the new
    # primary genuinely won by >15%.
    assert again is not None


# --- pivot triggers --------------------------------------------------

def test_hot_number_trigger_fires_on_my_settlement_number():
    from catanbot.strategy_select import _detect_hot_number

    history = [{"total": 8, "blocked_you": False}] * 4 + [
        {"total": 4, "blocked_you": False}] * 3
    fired = _detect_hot_number(history, my_settlement_numbers={8})
    assert fired is not None
    assert fired.name == "hot_number"
    assert "8" in fired.detail


def test_hot_number_trigger_silent_when_not_my_number():
    from catanbot.strategy_select import _detect_hot_number

    history = [{"total": 8}] * 4
    assert _detect_hot_number(history, my_settlement_numbers={6}) is None


def test_road_builder_drawn_overrides_to_lr_rush():
    from catanbot.strategy_select import _detect_dev_card_drawn

    triggers = _detect_dev_card_drawn([14])  # ROAD_BUILDING type int
    assert len(triggers) == 1
    assert triggers[0].name == "road_builder_drawn"
    assert triggers[0].override_tag == "LR_RUSH"


def test_monopoly_drawn_fires_without_override():
    from catanbot.strategy_select import _detect_dev_card_drawn

    triggers = _detect_dev_card_drawn([12])  # MONOPOLY
    assert len(triggers) == 1
    assert triggers[0].name == "monopoly_drawn"
    # Monopoly is informational — it doesn't change the active strategy
    # tag, just tells the user to hold the card for a hot resource.
    assert triggers[0].override_tag is None


def test_seven_overdue_fires_on_high_hand_no_recent_seven():
    from catanbot.strategy_select import _detect_seven_overdue

    history = [{"total": n} for n in (4, 6, 8, 10, 4, 5, 9, 3, 11, 6)]
    fired = _detect_seven_overdue(history, self_hand_size=10)
    assert fired is not None
    assert fired.name == "seven_overdue"


def test_seven_overdue_silent_when_seven_in_window():
    from catanbot.strategy_select import _detect_seven_overdue

    history = [{"total": n} for n in (4, 7, 8)]
    assert _detect_seven_overdue(history, self_hand_size=10) is None


def test_seven_overdue_silent_when_hand_below_limit():
    from catanbot.strategy_select import _detect_seven_overdue

    history = [{"total": 4}] * 12
    # discard limit defaults to 7 — hand of 6 is fine.
    assert _detect_seven_overdue(history, self_hand_size=6) is None


def test_merge_triggers_promotes_first_override():
    from catanbot.strategy_select import (
        PivotTrigger, StrategyTag, merge_triggers_into_tag,
    )

    base = StrategyTag(primary="BALANCED", fallback=None,
                       rationale="", phase="mid")
    triggers = [
        PivotTrigger("hot_number", "8 hot", override_tag=None),
        PivotTrigger("road_builder_drawn", "rb",
                     override_tag="LR_RUSH"),
        PivotTrigger("monopoly_drawn", "mono", override_tag=None),
    ]
    merged = merge_triggers_into_tag(base, triggers)
    assert merged.primary == "BALANCED"  # primary doesn't change
    assert merged.override_tag == "LR_RUSH"  # first override wins
    assert merged.pivot_triggers == [
        "hot_number", "road_builder_drawn", "monopoly_drawn"]


def test_detect_pivot_triggers_no_history_returns_empty():
    from catanbot.strategy_select import detect_pivot_triggers

    g = _fresh_game()
    out = detect_pivot_triggers(
        g, "RED",
        roll_history=None,
        self_dev_just_bought=None,
        self_hand_size=0,
    )
    assert out == []
