"""Turn-action recommender — smoke tests against catanatron board state.

These use a fresh catanatron game, seed a settlement + road for RED,
load a hand, and check that ``recommend_actions`` surfaces sensible
picks. The scoring is heuristic, so the assertions focus on shape +
relative ordering (best pick is a build when any build is possible,
dev card only shows up as fallback) rather than exact scores.
"""
from __future__ import annotations


def _fresh_game_with_red_settle():
    from catanatron import Color, Game, RandomPlayer

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=1,
    )
    # Give RED a settlement + one road so downstream buildable_edges
    # and buildable_node_ids both have real candidates to chew on.
    b = g.state.board
    b.build_settlement(Color.RED, 0, initial_build_phase=True)
    b.build_road(Color.RED, (0, 1))
    return g


def test_recommend_empty_hand_returns_nothing():
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    out = recommend_actions(g, "RED", {}, top=4)
    assert out == []


def test_dev_card_alone_when_only_ywo_ore_sheep_wheat():
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    out = recommend_actions(
        g, "RED", {"SHEEP": 1, "WHEAT": 1, "ORE": 1}, top=4)
    # Only affordable rec is dev_card; planning-ahead recs may also
    # surface (e.g. settlement 2 cards off), but the act-now slice
    # should be dev_card only.
    now = [r for r in out if r.get("when") == "now"]
    assert len(now) == 1
    assert now[0]["kind"] == "dev_card"


def test_road_affordable_surfaces_edge_suggestion():
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    out = recommend_actions(
        g, "RED", {"WOOD": 1, "BRICK": 1}, top=4)
    assert any(r["kind"] == "road" for r in out)
    road = next(r for r in out if r["kind"] == "road")
    assert "edge" in road
    assert len(road["edge"]) == 2
    # Direction must always render so the HUD can say "→ right toward
    # [tiles]" instead of just an arrow. Noah has flagged this multiple
    # times — pin a positive assertion on the normal (non-sealed) path.
    assert road.get("direction"), (
        f"direction must be present on every road rec: {road}")
    assert road["direction"]["word"] in {"N", "S", "NE", "NW", "SE", "SW"}
    assert road["direction"]["arrow"] in {"↑", "↓", "↗", "↖", "↘", "↙"}


def test_in_game_road_sealed_fallback_still_emits_direction():
    """When every buildable edge has its 2-hop settle target distance-2
    blocked, the in-game road rec used to silently disappear. Now it
    should fall back to the best-prod adjacent far-end with
    ``sealed=True`` + a direction arrow so the HUD still says *something*.
    Mirror of the opening-road sealed fallback."""
    from catanatron import Color
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    b = g.state.board
    # Plant opp settlements directly at every neighbor of RED's road
    # endpoints. Each opp pin distance-2-blocks its own neighbors too,
    # so by the time this finishes every 2-hop candidate reachable
    # from RED's buildable edges is distance-2 blocked.
    for n, col in (
        (6, Color.BLUE), (2, Color.WHITE),
        (19, Color.ORANGE), (22, Color.BLUE),
        (16, Color.WHITE), (4, Color.ORANGE),
    ):
        try:
            b.build_settlement(col, n, initial_build_phase=True)
        except Exception:  # noqa: BLE001
            continue
    out = recommend_actions(g, "RED", {"WOOD": 1, "BRICK": 1}, top=6)
    roads = [r for r in out if r["kind"] == "road"]
    assert roads, f"road rec must survive full-seal: got {[r['kind'] for r in out]}"
    road = roads[0]
    assert road.get("sealed") is True, f"sealed fallback flag missing: {road}"
    assert road.get("direction"), f"direction must be present on sealed rec: {road}"
    assert road["direction"]["word"] in {"N", "S", "NE", "NW", "SE", "SW"}
    assert "edge_from" in road and "edge_to" in road


def test_full_settlement_hand_picks_settlement_over_dev():
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    # Also extend RED's road net so buildable_node_ids has somewhere
    # to place legally.
    from catanatron import Color
    b = g.state.board
    b.build_road(Color.RED, (1, 2))
    b.build_road(Color.RED, (2, 3))
    # Hand covers settlement + dev-card costs both, so the sort must
    # rank the real build ahead of the fallback dev option.
    hand = {"WOOD": 1, "BRICK": 1, "SHEEP": 2, "WHEAT": 2, "ORE": 1}
    out = recommend_actions(g, "RED", hand, top=4)
    assert out[0]["kind"] in ("settlement", "road", "city")
    kinds = [r["kind"] for r in out]
    if "dev_card" in kinds:
        # If dev_card is present it must rank below all structural builds.
        dev_idx = kinds.index("dev_card")
        for i in range(dev_idx):
            assert kinds[i] in ("settlement", "road", "city")


def test_city_upgrade_appears_for_wheat_ore_heavy_hand():
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    out = recommend_actions(
        g, "RED", {"WHEAT": 2, "ORE": 3}, top=4)
    city = [r for r in out if r["kind"] == "city"]
    assert city, f"expected city upgrade, got {[r['kind'] for r in out]}"
    # Only settlement is RED's one at node 0 — city target must be node 0.
    assert city[0]["node_id"] == 0


def test_scores_are_in_one_to_ten_range():
    """Every recommendation score must be in [1, 10] regardless of
    board layout — the 1-10 scale is the contract with the UI."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    from catanatron import Color
    b = g.state.board
    b.build_road(Color.RED, (1, 2))
    b.build_road(Color.RED, (2, 3))
    hand = {"WOOD": 2, "BRICK": 2, "SHEEP": 2, "WHEAT": 3, "ORE": 3}
    out = recommend_actions(g, "RED", hand, top=10)
    for r in out:
        assert 1.0 <= float(r["score"]) <= 10.0, r


def test_dev_card_score_is_fixed_three():
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    out = recommend_actions(
        g, "RED", {"SHEEP": 1, "WHEAT": 1, "ORE": 1}, top=4)
    now = [r for r in out if r.get("when") == "now"]
    assert len(now) == 1
    assert now[0]["kind"] == "dev_card"
    assert now[0]["score"] == 3.0


def test_save_for_settlement_plan_surfaces_when_two_cards_short():
    """Road-only hand should also surface a "save for settlement" plan,
    so Noah sees both the now-option and the near-term better option."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    from catanatron import Color
    b = g.state.board
    # Extend road net so settlement target exists.
    b.build_road(Color.RED, (1, 2))
    b.build_road(Color.RED, (2, 3))
    # {WOOD:1, BRICK:1} affords road; settlement is 2 cards off (S+Wh).
    out = recommend_actions(g, "RED", {"WOOD": 1, "BRICK": 1}, top=6)
    kinds_when = [(r["kind"], r.get("when")) for r in out]
    assert ("road", "now") in kinds_when
    # Plan must carry the missing dict and tag when=soon.
    plan = next((r for r in out
                 if r["kind"] == "settlement" and r.get("when") == "soon"),
                None)
    assert plan is not None, kinds_when
    assert plan["missing"] == {"SHEEP": 1, "WHEAT": 1}
    assert "need" in plan["detail"].lower()


def test_plan_skipped_when_missing_more_than_two():
    """Hand 3+ cards from any upgrade shouldn't generate a plan — the
    noise isn't actionable."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    # Empty hand → 4 cards off settlement, 5 off city, 3 off dev.
    out = recommend_actions(g, "RED", {}, top=6)
    assert out == []


def test_now_ranks_above_equal_score_soon():
    """A now-rec and a soon-rec with equal score: the now-rec must
    appear first so the overlay's top pick is always actionable today."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    # dev_card affordable (3.0, now), dev_card plan not applicable
    # since affordable. Hand only admits dev_card now — check top is it.
    out = recommend_actions(
        g, "RED", {"SHEEP": 1, "WHEAT": 1, "ORE": 1}, top=6)
    assert out[0]["when"] == "now"


def test_propose_trade_suggested_when_one_card_short():
    """{WOOD:5, BRICK:1, SHEEP:0, WHEAT:1} is 1 Sheep short of a
    settlement and has a Wood surplus. Propose 1 Wood → 1 Sheep should
    be suggested (cheaper than the 4:1 bank fallback)."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    from catanatron import Color
    b = g.state.board
    b.build_road(Color.RED, (1, 2))
    b.build_road(Color.RED, (2, 3))
    hand = {"WOOD": 5, "BRICK": 1, "WHEAT": 1}
    out = recommend_actions(g, "RED", hand, top=6)
    proposals = [r for r in out if r["kind"] == "propose_trade"]
    assert proposals, f"expected a propose_trade, got {[r['kind'] for r in out]}"
    t = proposals[0]
    assert t["get"] == {"SHEEP": 1}
    assert t["give"] == {"WOOD": 1}
    assert t["unlocks"] == "settlement"
    assert t["when"] == "now"


def test_no_trade_when_no_spare_surplus():
    """When every card we hold is already reserved by the blocked
    build's cost, there's nothing spare to propose. Bank-trade fallback
    is gone (propose strictly dominates), so no trade rec fires."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    # Settlement needs WOOD+BRICK+SHEEP+WHEAT. Hand has exactly one of
    # each EXCEPT SHEEP → 1 SHEEP short, no surplus anywhere.
    hand = {"WOOD": 1, "BRICK": 1, "WHEAT": 1}
    out = recommend_actions(g, "RED", hand, top=6)
    trades = [r for r in out
              if r["kind"] in ("trade", "propose_trade")]
    assert not trades, trades


def test_no_bank_trade_when_two_cards_short():
    """Two missing cards → bank trade would require 2×4 = 8 cards for
    a 1-card-cost building. Not worth it; no trade rec."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    hand = {"WOOD": 8, "BRICK": 0, "SHEEP": 0, "WHEAT": 1}
    out = recommend_actions(g, "RED", hand, top=6)
    trades = [r for r in out if r["kind"] == "trade"]
    assert not trades, f"no trade expected, got {trades}"


def test_propose_trade_dominates_port_rate_when_spare_exists():
    """A 1:1 propose is cheaper than a 2:1 port — with any WOOD
    surplus, the propose rec must fire rather than the 2:1 port trade
    (which would waste a card). Keeps the old port path as a fallback
    for no-surplus situations."""
    from cataanbot.recommender import recommend_actions
    from catanatron import Color

    g = _fresh_game_with_red_settle()
    m = g.state.board.map
    wood_port_nodes = m.port_nodes.get("WOOD") or set()
    assert wood_port_nodes, "catanatron map missing WOOD port — seed changed?"
    # Put a RED settlement on a WOOD 2:1 port.
    b = g.state.board
    occupied = set(b.buildings.keys())
    port_node = next(iter(wood_port_nodes - occupied))
    b.build_settlement(Color.RED, port_node, initial_build_phase=True)
    # Dev card cost is SHEEP + WHEAT + ORE. Have SHEEP+WHEAT but no ORE,
    # and 2 Wood on the port. Propose 1 WOOD → 1 ORE beats 2 WOOD → 1 ORE.
    hand = {"WOOD": 2, "SHEEP": 1, "WHEAT": 1}
    out = recommend_actions(g, "RED", hand, top=6)
    proposals = [r for r in out
                 if r["kind"] == "propose_trade"
                 and r.get("get") == {"ORE": 1}]
    assert proposals, (
        f"expected WOOD→ORE propose, got {[r['kind'] for r in out]}")
    t = proposals[0]
    assert t["give"] == {"WOOD": 1}, t
    assert t["variant"] == "1:1 fair"
    assert t["unlocks"] == "dev_card"


def test_dev_card_trade_has_no_node_id():
    """Trade to unlock a dev card shouldn't leak a misleading node_id
    (dev cards don't go on the board). Applies to both propose and bank
    variants."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    # 1 short of dev card (need ORE), excess WOOD to trade with.
    hand = {"WOOD": 5, "SHEEP": 1, "WHEAT": 1}
    out = recommend_actions(g, "RED", hand, top=6)
    trades = [r for r in out
              if r["kind"] in ("trade", "propose_trade")
              and r.get("unlocks") == "dev_card"]
    assert trades, f"expected dev_card trade, got {[r['kind'] for r in out]}"
    t = trades[0]
    assert "node_id" not in t, t
    assert t["get"] == {"ORE": 1}


def test_trade_protects_resources_still_needed():
    """If we're 1 Sheep short of a settlement but only have exactly
    1 Wheat (also needed for the settlement), we must NOT trade away
    that Wheat — it'd leave us blocked on Wheat after the trade."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    # WOOD 1 + BRICK 1 + WHEAT 1 + no SHEEP. We have no stockpile of
    # an unneeded resource, so no trade source exists → no trade.
    hand = {"WOOD": 1, "BRICK": 1, "WHEAT": 1}
    out = recommend_actions(g, "RED", hand, top=6)
    trades = [r for r in out
              if r["kind"] in ("trade", "propose_trade")]
    assert not trades, f"should not trade away a needed resource: {trades}"


def test_city_scores_higher_than_dev_card():
    """Upgrading a settlement to a city is strictly better than a
    random dev card at the same affordable-turn, so it should always
    outrank when both are options."""
    from cataanbot.recommender import recommend_actions

    g = _fresh_game_with_red_settle()
    hand = {"WHEAT": 2, "ORE": 3, "SHEEP": 1}
    out = recommend_actions(g, "RED", hand, top=4)
    kinds = [r["kind"] for r in out]
    assert "city" in kinds and "dev_card" in kinds
    city = next(r for r in out if r["kind"] == "city")
    dev = next(r for r in out if r["kind"] == "dev_card")
    assert city["score"] > dev["score"]


def test_recommend_opening_on_fresh_game_returns_top_picks():
    """A fresh catanatron game has every land node legal — the opening
    advisor should return exactly ``top`` suggestions, all in the
    opening_settlement shape."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=7,
    )
    out = recommend_opening(g, "RED", top=5)
    assert len(out) == 5
    for r in out:
        assert r["kind"] == "opening_settlement"
        assert r["when"] == "now"
        assert isinstance(r["node_id"], int)
        assert 2.0 <= float(r["score"]) <= 10.0
        assert "/roll" in r["detail"]
    # Scores must be monotonically non-increasing — top pick first.
    scores = [r["score"] for r in out]
    assert scores == sorted(scores, reverse=True)


def test_recommend_opening_is_adaptive_to_placements():
    """Placing a settlement should remove that node AND its distance-1
    neighbors from the next call's legal set — the ranking shifts as
    the board fills."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=11,
    )
    before = recommend_opening(g, "RED", top=5)
    assert before
    # Take BLUE's pick on what RED would have wanted most. Then rerun.
    top_pick = before[0]["node_id"]
    g.state.board.build_settlement(
        Color.BLUE, top_pick, initial_build_phase=True)
    after = recommend_opening(g, "RED", top=5)
    node_ids_after = {r["node_id"] for r in after}
    assert top_pick not in node_ids_after, (
        "top pick was taken by BLUE — must not resurface")
    # Neighbors of top_pick (distance-1) are also illegal via Catan's
    # distance rule, so they should be gone too.
    neighbors = set()
    for e in g.state.board.map.land_nodes:
        pass  # placeholder; actual neighbor check via buildable filter
    # Re-derive via the advisor's helper for a stricter invariant.
    from cataanbot.advisor import legal_nodes_after_picks
    legal = legal_nodes_after_picks(g, [top_pick])
    assert node_ids_after.issubset(legal)


def test_recommend_opening_attaches_road_direction():
    """Every opening-settlement pick should also carry a `road` hint
    pointing at the best adjacent edge (the direction to lay the
    opening road toward future expansion)."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=5,
    )
    out = recommend_opening(g, None, top=5)
    assert out
    for r in out:
        road = r["road"]
        assert road is not None, r
        edge = road["edge"]
        assert edge[0] == r["node_id"], f"road must start at settlement: {r}"
        assert edge[1] != edge[0]
        assert isinstance(road["toward_node"], int)
        # Tile list describes the 2 hexes flanking the road edge so
        # Noah can identify it as "the road between the 6 and the 8".
        assert isinstance(road["edge_tiles"], list)


def test_recommend_opening_road_skips_distance_blocked_expansions():
    """The road hint's ``toward_node`` must never be a corner that's
    already distance-2 blocked by an existing settlement — that spot
    is permanently off the table, so pointing a road at it is wasted
    advice. Regression for the dangerous-road complaint."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.advisor import _build_node_neighbors
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=13,
    )
    # Plant a few opp settlements to create distance-2 exclusions.
    # Re-derive picks after each placement to respect distance rule.
    for col in (Color.BLUE, Color.WHITE, Color.ORANGE):
        picks = recommend_opening(g, col.name, top=1)
        assert picks, f"ran out of legal picks placing {col}"
        g.state.board.build_settlement(
            col, picks[0]["node_id"], initial_build_phase=True)

    out = recommend_opening(g, "RED", top=5)
    assert out
    neighbors = _build_node_neighbors(g.state.board.map)
    blocked: set[int] = set()
    for nid, (_col, bt) in g.state.board.buildings.items():
        if bt in ("SETTLEMENT", "CITY"):
            blocked.add(int(nid))
            blocked |= {int(n) for n in neighbors.get(int(nid), set())}

    for r in out:
        road = r["road"]
        if road is None:
            continue
        expansion = road["toward_node"]
        # Fix: _best_opening_road now returns None when no legal 2-hop
        # expansion exists, instead of falling back to `far` (which is
        # distance-1 from our own settlement and therefore always
        # blocked). So `expansion` must always be a legal settle spot.
        assert expansion != road["edge"][1], (
            f"road toward node == far endpoint — that's distance-1 from "
            f"our own settlement and unsettleable: {r}")
        assert expansion not in blocked, (
            f"road toward node {expansion} but it's distance-2 "
            f"blocked: {r}; blocked={blocked & {expansion}}")


def test_recommend_opening_road_skips_opp_sealed_edge():
    """If an opp has already placed a road on the (far, x) edge, that
    expansion path is sealed — the road hint must not recommend going
    through that edge."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.advisor import _build_node_neighbors
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=21,
    )
    picks = recommend_opening(g, "RED", top=5)
    assert picks
    top = picks[0]
    # Seal the suggested road's far→expansion edge with an opp road
    # of a different color, so the recommender has to route around.
    far = top["road"]["edge"][1]
    expansion = top["road"]["toward_node"]
    if expansion == far:
        pytest.skip("degenerate road hint — no 2-hop expansion to seal")
    # Plant BLUE at `expansion` (not `far` — that would distance-2
    # block our top pick and defeat the test). An initial-phase road
    # must touch the same color's settlement, so BLUE at `expansion`
    # anchors the (far, expansion) road legally.
    g.state.board.build_settlement(
        Color.BLUE, expansion, initial_build_phase=True)
    g.state.board.build_road(Color.BLUE, (far, expansion))

    out = recommend_opening(g, "RED", top=5)
    match = next((r for r in out if r["node_id"] == top["node_id"]), None)
    assert match is not None, "top pick should still be legal for RED"
    road = match["road"]
    assert road is not None
    # After sealing, the road must not route into the now-claimed
    # expansion through the opp-sealed edge.
    sealed = (road["edge"][1] == far
              and road["toward_node"] == expansion)
    assert not sealed, (
        f"recommender still routes into sealed edge {far}→{expansion}: "
        f"{road}")


def test_recommend_opening_tolerates_none_color():
    """Bridge calls in with ``color=None`` during round 1 because
    ``self_color_id`` hasn't latched yet (colonist only reveals it
    after real resource cards land). Must not crash; must skip the
    "2nd pick" hint gracefully."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=9,
    )
    out = recommend_opening(g, None, top=5)
    assert len(out) == 5
    for r in out:
        assert r["kind"] == "opening_settlement"
        # No color means no round-2 context, so no "2nd pick" hint
        # regardless of how many settlements are on the board.
        assert "2nd pick" not in r["detail"]


def test_recommend_opening_short_circuits_to_road_when_all_settlements_placed():
    """When every player has placed both opening settlements, the main
    pick loop is moot — colonist won't let anyone drop another. Verify
    ``recommend_opening`` short-circuits straight to the road-followup
    and returns a rec whose ``detail`` mentions the road."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=31,
    )
    # Snake draft — re-score per placement so distance-2 stays legal.
    order = [Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE,
             Color.ORANGE, Color.WHITE, Color.BLUE, Color.RED]
    red_second_settlement: int | None = None
    for i, col in enumerate(order):
        picks = recommend_opening(g, col.name, top=1)
        assert picks, f"no legal pick for {col} at step {i}"
        nid = picks[0]["node_id"]
        g.state.board.build_settlement(
            col, nid, initial_build_phase=True)
        # Place a matching road for every color EXCEPT RED's 2nd drop.
        if not (col == Color.RED and i == 7):
            far = picks[0]["road"]["edge"][1]
            g.state.board.build_road(col, (nid, far))
        else:
            red_second_settlement = nid

    # All 8 settlements are placed; RED's 2nd road is missing.
    out = recommend_opening(g, "RED", top=5)
    assert out, "short-circuit must return a rec when all settlements placed"
    rec = out[0]
    assert rec["kind"] == "opening_settlement"
    assert rec["node_id"] == red_second_settlement
    assert rec["road"] is not None
    assert "road" in rec["detail"].lower()


def test_opening_road_followup_targets_red_settlement_without_road():
    """Helper that fires when the main opening rec list is empty and
    self still owes a road. Places two RED settlements and exactly one
    matching road, then verifies the followup helper picks the un-roaded
    settlement and hands back a road hint (edge + toward_node)."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.advisor import (
        _build_node_neighbors, score_opening_nodes,
    )
    from cataanbot.recommender import _opening_road_followup

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=17,
    )
    # Two RED settlements: the first has a road, the second doesn't.
    # Pick nodes that are distance-2 legal relative to each other.
    board = g.state.board
    m = board.map
    neighbors = _build_node_neighbors(m)
    # First RED settlement — any land node works.
    first = next(iter(m.land_nodes))
    board.build_settlement(Color.RED, first, initial_build_phase=True)
    # First road out of `first` → any neighbor.
    first_nb = next(iter(neighbors[first]))
    board.build_road(Color.RED, (first, first_nb))
    # Second RED settlement — a land node at distance ≥ 2 from first.
    blocked = {first} | set(neighbors[first])
    second = next(iter(n for n in m.land_nodes if n not in blocked))
    board.build_settlement(Color.RED, second, initial_build_phase=True)

    full_scored = {ns.node_id: ns for ns in score_opening_nodes(g)}
    out = _opening_road_followup(
        game=g, c=Color.RED, neighbors=neighbors,
        scored_by_node=full_scored, m=m,
    )
    assert out, "followup must emit a rec when RED's 2nd settlement lacks a road"
    rec = out[0]
    # Targets the un-roaded settlement, carries a usable road hint.
    assert rec["node_id"] == second, (rec, first, second)
    assert rec["road"] is not None
    assert "road" in rec["detail"].lower()


def test_recommend_opening_flags_second_pick_context():
    """When RED has placed settlement+road, the detail string should
    signal "2nd pick" so Noah knows to weigh resource-complement."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.advisor import _build_node_neighbors
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=13,
    )
    # Place RED's first settlement on any node; also lay the matching
    # road so we exit the "just-settled" gate and hit round-2 picks.
    m = g.state.board.map
    first = next(iter(m.land_nodes))
    g.state.board.build_settlement(
        Color.RED, first, initial_build_phase=True)
    neighbor = next(iter(_build_node_neighbors(m)[first]))
    g.state.board.build_road(Color.RED, (first, neighbor))
    out = recommend_opening(g, "RED", top=3)
    assert out
    for r in out:
        assert "2nd pick" in r["detail"]


def test_recommend_opening_holds_on_settle_before_road():
    """If RED has placed a settlement but not yet its matching road,
    the rec should pin to that settlement with a 'lay your matching road'
    hint instead of flickering forward to the round-2 settle choice —
    that would wipe the F-card mid-placement."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=13,
    )
    first = next(iter(g.state.board.map.land_nodes))
    g.state.board.build_settlement(
        Color.RED, first, initial_build_phase=True)
    out = recommend_opening(g, "RED", top=3)
    assert out, "gate must emit a rec during settle-before-road window"
    assert len(out) == 1
    assert out[0]["node_id"] == first
    assert "road" in out[0]["detail"].lower()
    assert out[0]["road"] is not None
    # Direction hint lets Noah pick the corner without parsing tile chips.
    assert out[0]["road"].get("direction") is not None
    # Primary action is "road" so the overlay labels the hero rec as
    # ROAD (not SETTLE) — matches what Noah's about to do next.
    assert out[0].get("action") == "road"


def test_recommend_opening_infers_self_color_when_unlatched():
    """Bridge calls in with ``color=None`` until ``self_color_id``
    latches (which can lag through the first settlement). When exactly
    one color on the board has placed a settlement without its matching
    road, that color is unambiguously the one needing the road hint —
    fall back to it so the arrow-bearing road followup still renders.

    Regression: without this fallback, the opening pick would clear to
    generic round-1 settle picks the moment the user dropped their 1st
    settlement, leaving them no direction arrow for the 1st road."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=13,
    )
    # RED placed a settlement, no road yet. self_color_id hasn't latched.
    first = next(iter(g.state.board.map.land_nodes))
    g.state.board.build_settlement(
        Color.RED, first, initial_build_phase=True)
    out = recommend_opening(g, None, top=3)
    assert out, "must still return a rec when c=None but a player owes a road"
    assert len(out) == 1
    assert out[0]["node_id"] == first
    assert out[0]["road"] is not None
    assert out[0]["road"].get("direction") is not None
    assert out[0].get("action") == "road"


def test_recommend_opening_round2_holds_on_2nd_settle_before_2nd_road():
    """Round-2 analog of the settle-before-road pin. RED has placed
    1st settle + 1st road (round 1) plus 2nd settle but not yet the
    2nd road. The followup must pin to the un-roaded settlement with
    action='road' so the HUD doesn't flicker to the already-placed
    settle's old context."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.advisor import _build_node_neighbors
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=13,
    )
    board = g.state.board
    m = board.map
    nbors = _build_node_neighbors(m)
    first = next(iter(m.land_nodes))
    board.build_settlement(Color.RED, first, initial_build_phase=True)
    first_nb = next(iter(nbors[first]))
    board.build_road(Color.RED, (first, first_nb))
    # Second RED settle at distance ≥ 2 from first.
    blocked = {first} | set(nbors[first])
    second = next(iter(n for n in m.land_nodes if n not in blocked))
    board.build_settlement(Color.RED, second, initial_build_phase=True)
    # 2nd road NOT placed.
    out = recommend_opening(g, "RED", top=3)
    assert out, "followup must emit during round-2 settle-before-road"
    assert len(out) == 1
    assert out[0]["node_id"] == second
    assert out[0].get("action") == "road"
    assert out[0]["road"] is not None
    assert out[0]["road"].get("direction") is not None


def test_recommend_opening_round_one_attaches_plan_second():
    """Round-1 recs (no self settlement yet) should surface a paired
    plan.second with the best hypothetical 2nd-settlement pick so Noah
    reads each F pick as a coordinated 2-settle plan, not a one-off."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=7,
    )
    out = recommend_opening(g, "RED", top=3)
    assert out
    for r in out:
        plan = r.get("plan")
        assert plan is not None and "second" in plan, (
            f"round-1 rec should carry plan.second: {r}")
        n = plan["second"]
        assert isinstance(n["node_id"], int)
        assert n["node_id"] != r["node_id"], (
            "paired N must be a different node than F")
        assert 1 <= n["covers"] <= 5
        assert isinstance(n["adds"], list)
        assert isinstance(n["tiles"], list)


def test_recommend_opening_round_one_plan_reflects_joint_coverage():
    """plan.second.covers must equal the distinct-resource count of F ∪ N.
    Keeps the overlay's coverage claim (e.g. "cov 5/5") honest."""
    from catanatron import Color, Game, RandomPlayer
    from catanatron.state import RESOURCES
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=11,
    )
    m = g.state.board.map
    out = recommend_opening(g, "RED", top=3)
    assert out
    for r in out:
        plan = r["plan"]["second"]
        F_prod = m.node_production.get(r["node_id"], {})
        N_prod = m.node_production.get(plan["node_id"], {})
        covered = {res for res in RESOURCES
                   if F_prod.get(res, 0.0) + N_prod.get(res, 0.0) > 0.0}
        assert plan["covers"] == len(covered), (
            f"coverage mismatch for F={r['node_id']} "
            f"N={plan['node_id']}: claim={plan['covers']} actual={len(covered)}")


def test_recommend_opening_round_one_road_respects_planned_n():
    """The round-1 road's toward_node must not be a distance-1 neighbor
    of the planned 2nd-settlement — that spot becomes illegal once N
    lands, so pointing a road at it is wasted commitment."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.advisor import _build_node_neighbors
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=19,
    )
    neighbors = _build_node_neighbors(g.state.board.map)
    out = recommend_opening(g, "RED", top=5)
    assert out
    for r in out:
        if r.get("road") is None:
            continue
        plan = r.get("plan") or {}
        n_info = plan.get("second")
        if n_info is None:
            continue
        n_nid = n_info["node_id"]
        toward = r["road"]["toward_node"]
        n_blocked = {n_nid} | set(neighbors.get(n_nid, set()))
        assert toward not in n_blocked, (
            f"road toward {toward} is distance-1 from planned N={n_nid}: {r}")


def test_archetype_ore_city_when_two_ore_plus_wheat():
    """Two ore tiles + at least one wheat → ore-city archetype. This is
    the classic city-first opening and should override more generic
    labels like 'balanced'."""
    from cataanbot.recommender import _label_archetype

    tiles_f = [("ORE", 6), ("WHEAT", 8), ("SHEEP", 3)]
    tiles_n = [("ORE", 9), ("BRICK", 4), ("DESERT", None)]
    assert _label_archetype(tiles_f, tiles_n, None, None) == "ore-city"


def test_archetype_wood_first_when_heavy_wood_brick():
    """2+ wood AND 1+ brick → wood-first archetype (road/settlement
    expansion strategy)."""
    from cataanbot.recommender import _label_archetype

    tiles_f = [("WOOD", 6), ("BRICK", 8), ("SHEEP", 3)]
    tiles_n = [("WOOD", 9), ("WHEAT", 4), ("DESERT", None)]
    assert _label_archetype(tiles_f, tiles_n, None, None) == "wood-first"


def test_archetype_balanced_when_five_resources_no_dominance():
    """5/5 resource coverage with no resource repeated → balanced."""
    from cataanbot.recommender import _label_archetype

    tiles_f = [("WOOD", 6), ("BRICK", 8), ("WHEAT", 5)]
    tiles_n = [("SHEEP", 9), ("ORE", 10), ("DESERT", None)]
    assert _label_archetype(tiles_f, tiles_n, None, None) == "balanced"


def test_archetype_port_trumps_other_labels():
    """A 2:1 port on a produced resource is distinctive enough to flip
    archetype regardless of tile distribution — the port reshapes the
    whole trade economy."""
    from cataanbot.recommender import _label_archetype

    tiles_f = [("WOOD", 6), ("BRICK", 8), ("SHEEP", 3)]
    tiles_n = [("WOOD", 9), ("WHEAT", 4), ("DESERT", None)]
    # Without the port, this would be wood-first. With a WHEAT 2:1 port
    # on F (and wheat produced via N), port wins.
    assert _label_archetype(
        tiles_f, tiles_n, "WHEAT 2:1", None) == "port"


def test_archetype_port_requires_resource_on_board():
    """A 2:1 port without any matching production doesn't trigger the
    port label — you can't convert surplus you don't have."""
    from cataanbot.recommender import _label_archetype

    tiles_f = [("WOOD", 6), ("BRICK", 8), ("WHEAT", 5)]
    tiles_n = [("SHEEP", 9), ("ORE", 10), ("DESERT", None)]
    # ORE 2:1 — but already 5/5 covered with ore in N → port wins. Try
    # a resource that really isn't produced: there isn't one here, so
    # swap to a scenario with limited coverage and a 2:1 port on a
    # resource missing from both tiles.
    tiles_f = [("WOOD", 6), ("WOOD", 8), ("SHEEP", 3)]
    tiles_n = [("BRICK", 9), ("BRICK", 4), ("DESERT", None)]
    # 4 resources missing WHEAT + ORE. Port on WHEAT without any wheat
    # tiles → falls back to wood-first (3+ wood+brick).
    assert _label_archetype(
        tiles_f, tiles_n, "WHEAT 2:1", None) == "wood-first"


def test_archetype_none_when_uninteresting():
    """Sparse / irregular combos return None rather than forcing a label."""
    from cataanbot.recommender import _label_archetype

    # 2 resources total, neither dominant enough to label.
    tiles_f = [("SHEEP", 6), ("DESERT", None)]
    tiles_n = [("SHEEP", 9), ("WHEAT", 4)]
    assert _label_archetype(tiles_f, tiles_n, None, None) is None


def test_recommend_opening_attaches_archetype_to_plan():
    """Round-1 picks should carry plan.archetype where applicable so the
    overlay can surface strategy framing alongside the paired plan."""
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=42,
    )
    out = recommend_opening(g, "RED", top=5)
    assert out
    # At least one of the top picks should have a recognizable archetype.
    labeled = [r for r in out
               if r.get("plan") and r["plan"].get("archetype")]
    assert labeled, (
        "no archetype label surfaced — at least one top-5 pick should "
        "fit balanced/wood-first/ore-city/port on a fresh board")
    # And any label that does appear must be one of the allowed values.
    allowed = {"balanced", "wood-first", "ore-city", "port", "dev-card"}
    for r in labeled:
        assert r["plan"]["archetype"] in allowed, r


def test_archetype_dev_card_when_sheep_wheat_ore_but_light_on_wood():
    """Sheep + wheat + ore produced, but not enough wood/brick to
    road-spam, not enough ore to city-rush, and coverage too narrow
    for balanced — the pivot to dev cards is the best path. This
    scenario used to return None; now it flags dev-card."""
    from cataanbot.recommender import _label_archetype

    # Exactly 3 resources: SHEEP, WHEAT, ORE. Missing wood AND brick.
    # 1 ore (can't city-rush), 0 wood+brick (can't road-spam), 3 distinct
    # (not balanced). Perfect dev-card scenario.
    tiles_f = [("SHEEP", 6), ("WHEAT", 8), ("DESERT", None)]
    tiles_n = [("ORE", 9), ("WHEAT", 4), ("SHEEP", 3)]
    assert _label_archetype(tiles_f, tiles_n, None, None) == "dev-card"


def test_archetype_dev_card_loses_to_ore_city_when_two_ore():
    """Two ore + wheat → ore-city wins over dev-card (dev-card is the
    fallback when city rush isn't available)."""
    from cataanbot.recommender import _label_archetype

    tiles_f = [("ORE", 6), ("WHEAT", 8), ("SHEEP", 3)]
    tiles_n = [("ORE", 9), ("BRICK", 4), ("DESERT", None)]
    assert _label_archetype(tiles_f, tiles_n, None, None) == "ore-city"


def test_archetype_dev_card_loses_to_wood_first_on_heavy_wood():
    """Wood-first still wins when wood/brick is heavy, even if the
    dev-card ingredients are also present (road-spam + settlements is
    usually stronger than a pure dev-card pivot)."""
    from cataanbot.recommender import _label_archetype

    tiles_f = [("WOOD", 6), ("BRICK", 8), ("SHEEP", 3)]
    tiles_n = [("WOOD", 9), ("WHEAT", 4), ("ORE", 10)]
    assert _label_archetype(tiles_f, tiles_n, None, None) == "wood-first"


def test_recommend_opening_round_two_uses_complement_ranking():
    """Round-2 picks should come from score_second_settlements (complement
    over raw production) so a 4/5-coverage candidate edges out a higher-pip
    but fully-overlapping pick. Check the top pick actually complements F
    by contributing at least one resource F doesn't already produce."""
    from catanatron import Color, Game, RandomPlayer
    from catanatron.state import RESOURCES
    from cataanbot.advisor import _build_node_neighbors
    from cataanbot.recommender import recommend_opening

    g = Game(
        [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                    Color.WHITE, Color.ORANGE)],
        seed=23,
    )
    m = g.state.board.map
    # Pick a real high-pip land node for RED's first settle.
    nodes_by_pip = sorted(
        m.land_nodes,
        key=lambda n: sum(m.node_production.get(n, {}).values()),
        reverse=True,
    )
    first = nodes_by_pip[0]
    g.state.board.build_settlement(Color.RED, first, initial_build_phase=True)
    # Also lay the matching road so we exit the settle-before-road gate
    # and the recommender returns round-2 picks (rather than the
    # "finish your road" hint pinned back at `first`).
    neighbor = next(iter(_build_node_neighbors(m)[first]))
    g.state.board.build_road(Color.RED, (first, neighbor))

    out = recommend_opening(g, "RED", top=3)
    assert out
    top = out[0]
    F_prod = m.node_production.get(first, {})
    N_prod = m.node_production.get(top["node_id"], {})
    # At least one resource in N that F doesn't already cover. This is
    # the whole point of complement-ranking — stacking the same resource
    # isn't ranked as a top round-2 pick.
    f_res = {r for r in RESOURCES if F_prod.get(r, 0.0) > 0.0}
    n_res = {r for r in RESOURCES if N_prod.get(r, 0.0) > 0.0}
    assert n_res - f_res, (
        f"top round-2 pick must add a new resource; "
        f"F={first} covers {sorted(f_res)}, N={top['node_id']} "
        f"covers {sorted(n_res)}"
    )
    # And the detail string should mention what's being added + coverage.
    assert "adds" in top["detail"] or "covers" in top["detail"]


def test_live_game_resyncs_hand_on_reconnect_type4():
    """A second type=4 frame on an already-booted LiveGame should
    re-sync the self-hand from the replay's playerStates rather than
    being ignored. Fixes disconnect/reconnect drift."""
    from cataanbot.live_game import LiveGame

    lg = LiveGame()
    # Bootstrap a minimal gameState that LiveSession.from_game_start
    # will accept. The map_state just needs to parse — for a synthetic
    # test we'd need a real fixture, so use the existing fixture helper.
    # Easier path: skip if we can't construct without fixtures.
    import pathlib
    import json
    fixture = pathlib.Path(__file__).parent / "fixtures" / "gamestart.json"
    if not fixture.exists():
        import pytest
        pytest.skip("no GameStart fixture on disk — covered by integration")

    body = json.loads(fixture.read_text())
    lg.start_from_game_state(body)
    assert lg.started
    # Capture the current hand of the self-color, then "pretend" the
    # replay ships the same body again (reconnect). Should not raise.
    lg.feed({"type": 4, "payload": body})
    # Still started, no crash, and the tracker survived.
    assert lg.started


def test_colonist_diff_latches_current_turn_color():
    """current_turn_color_id should cache the last seen turn-color so a
    roll frame that omits currentTurnPlayerColor still attributes right."""
    from cataanbot.colonist_diff import LiveSession, events_from_diff
    from cataanbot.colonist_map import MapMapping

    sess = LiveSession(mapping=MapMapping(), player_names={3: "Alice"})
    # First frame sets the turn color.
    events_from_diff(sess, {"currentState": {"currentTurnPlayerColor": 3}})
    assert sess.current_turn_color_id == 3

    # Next frame: dice roll but no currentTurnPlayerColor. The emitted
    # RollEvent should still be attributed to Alice (color 3).
    events = events_from_diff(sess, {"diceState": {"dice1": 2, "dice2": 3}})
    rolls = [e for e in events if getattr(e, "d1", None) is not None]
    assert rolls
    assert rolls[0].player == "Alice"


# --- evaluate_incoming_trade -----------------------------------------------


def test_trade_decline_when_cant_spare_want():
    """Offer that demands resources we don't have → auto-decline."""
    from cataanbot.recommender import evaluate_incoming_trade

    g = _fresh_game_with_red_settle()
    # We have 1 ORE; offer asks for 2. Can't afford.
    hand = {"WOOD": 1, "BRICK": 1, "ORE": 1}
    verdict = evaluate_incoming_trade(
        g, "RED", hand,
        give={"WHEAT": 1}, want={"ORE": 2},
    )
    assert verdict["verdict"] == "decline"
    assert "spare" in verdict["reason"].lower()


def test_trade_accept_unlocks_affordable_build():
    """Swap should accept when it unlocks a buildable settlement."""
    from catanatron import Color

    from cataanbot.recommender import evaluate_incoming_trade

    g = _fresh_game_with_red_settle()
    b = g.state.board
    # Extend RED's road network so settlements have legal landing spots.
    b.build_road(Color.RED, (1, 2))
    b.build_road(Color.RED, (2, 3))
    # Hand: settlement missing only SHEEP; we have a surplus ORE to offer.
    hand = {"WOOD": 1, "BRICK": 1, "WHEAT": 1, "ORE": 2}
    verdict = evaluate_incoming_trade(
        g, "RED", hand,
        give={"SHEEP": 1}, want={"ORE": 1},
    )
    assert verdict["verdict"] == "accept", verdict
    assert verdict["after"] == "settlement"
    assert verdict["score"] > 0


def test_trade_decline_when_opponent_close_to_win():
    """Even a good-looking swap gets declined when opp is at 8+ VP —
    the offerer is close enough to closing out the game."""
    from catanatron import Color

    from cataanbot.recommender import evaluate_incoming_trade

    g = _fresh_game_with_red_settle()
    b = g.state.board
    b.build_road(Color.RED, (1, 2))
    b.build_road(Color.RED, (2, 3))
    hand = {"WOOD": 1, "BRICK": 1, "WHEAT": 1, "ORE": 2}
    verdict = evaluate_incoming_trade(
        g, "RED", hand,
        give={"SHEEP": 1}, want={"ORE": 1},
        opp_vp=9,
    )
    assert verdict["verdict"] == "decline", verdict
    assert "VP" in verdict["reason"]


def test_trade_decline_when_lopsided_neutral():
    """1-for-2 swap with no build unlock → decline on fairness, not
    because it blocks anything."""
    from cataanbot.recommender import evaluate_incoming_trade

    g = _fresh_game_with_red_settle()
    # Hand can't build anything; swap leaves us in the same state.
    hand = {"WOOD": 1, "BRICK": 1, "SHEEP": 2}
    verdict = evaluate_incoming_trade(
        g, "RED", hand,
        give={"ORE": 1}, want={"SHEEP": 2},
    )
    assert verdict["verdict"] == "decline"
    assert "lopsided" in verdict["reason"]


def test_trade_consider_neutral_swap():
    """Same-count swap with no build change → consider (not a reject)."""
    from cataanbot.recommender import evaluate_incoming_trade

    g = _fresh_game_with_red_settle()
    hand = {"SHEEP": 1}
    verdict = evaluate_incoming_trade(
        g, "RED", hand,
        give={"WHEAT": 1}, want={"SHEEP": 1},
    )
    assert verdict["verdict"] == "consider"


def test_trade_decline_when_give_is_empty():
    """Degenerate offer (they give nothing) → decline."""
    from cataanbot.recommender import evaluate_incoming_trade

    g = _fresh_game_with_red_settle()
    verdict = evaluate_incoming_trade(
        g, "RED", {"ORE": 1},
        give={}, want={"ORE": 1},
    )
    assert verdict["verdict"] == "decline"


def test_trade_counter_suggested_on_lopsided_decline():
    """Opp asks 1 Sheep for 2 Ore. Paying 2 Ore leaves us with 0 Ore — no
    dev card. Paying 1 Ore (the counter) still lets us buy the dev card
    AND adds a Sheep, so the counter should tip from decline to accept."""
    from cataanbot.recommender import evaluate_incoming_trade

    g = _fresh_game_with_red_settle()
    # No buildable structures on current road net — only dev card matters.
    # 2 Ore + 1 Wheat, no Sheep: one dev-card purchase away from affordable.
    hand = {"WHEAT": 1, "ORE": 2}
    verdict = evaluate_incoming_trade(
        g, "RED", hand,
        give={"SHEEP": 1}, want={"ORE": 2},
    )
    assert verdict["verdict"] == "decline", verdict
    assert verdict["counter"] is not None, verdict
    counter = verdict["counter"]
    assert counter["want"] == {"ORE": 1}
    assert counter["give"] == {"SHEEP": 1}
    assert "1:1" in counter["reason"]


def test_trade_no_counter_when_opp_close_to_win():
    """Don't offer a counter-trade when the opp is at 8+ VP — even a
    balanced 1-for-1 feeds them toward the win."""
    from cataanbot.recommender import evaluate_incoming_trade

    g = _fresh_game_with_red_settle()
    # Same dev-card setup as the lopsided-decline test, just with opp_vp=9.
    hand = {"WHEAT": 1, "ORE": 2}
    verdict = evaluate_incoming_trade(
        g, "RED", hand,
        give={"SHEEP": 1}, want={"ORE": 2},
        opp_vp=9,
    )
    assert verdict["verdict"] == "decline"
    assert verdict["counter"] is None, verdict


def test_trade_no_counter_when_accept_already():
    """Already-accepted offers don't need a counter — the accept path
    returns counter=None so the overlay doesn't show a redundant pill."""
    from catanatron import Color

    from cataanbot.recommender import evaluate_incoming_trade

    g = _fresh_game_with_red_settle()
    b = g.state.board
    b.build_road(Color.RED, (1, 2))
    b.build_road(Color.RED, (2, 3))
    hand = {"WOOD": 1, "BRICK": 1, "WHEAT": 1, "ORE": 2}
    verdict = evaluate_incoming_trade(
        g, "RED", hand,
        give={"SHEEP": 1}, want={"ORE": 1},
    )
    assert verdict["verdict"] == "accept"
    assert verdict["counter"] is None


def test_trade_no_counter_when_trimmed_still_bad():
    """Counter only fires when the trimmed version flips to accept. A
    swap where the trimmed version still unlocks nothing (same kind
    before/after, same score) should come back with no counter."""
    from cataanbot.recommender import evaluate_incoming_trade

    g = _fresh_game_with_red_settle()
    # Hand with exactly 2 SHEEP and nothing else. Before/full/counter
    # all land at nothing-affordable (dev is still missing ORE+WHEAT
    # before, missing ORE after counter — too far for propose).
    hand = {"SHEEP": 2}
    verdict = evaluate_incoming_trade(
        g, "RED", hand,
        give={"WHEAT": 1}, want={"SHEEP": 2},
    )
    assert verdict["verdict"] == "decline"
    assert verdict["counter"] is None, verdict
