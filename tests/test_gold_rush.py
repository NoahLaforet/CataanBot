"""Gold Rush / Black Forest fog-board strategy gaps.

Four gaps the fog board exposed that classic-tuned scorers missed:

1. Fog pursuit: roads into the fog ring carry reveal EV, so they must
   surface in the opening-road and mid-game road recs.
2. Road Building timing: RB should fire to reveal fog even with no
   longest-road swing, and stay HOLD when there's neither.
3. Opening restriction: under restrictedStartingPlacement the first two
   settlements may only land on shown-tile corners (no fog-adjacent ones).
4. Gold valuation: a gold-adjacent settle/city is worth the wildcard, and
   gold_pick picks the scarcest / most-unlocking resource.

The synthetic tests build a fresh catanatron board and stamp the fog /
gold annotations directly so they run without any capture. The
capture-driven tests replay a real Gold Rush game and skip when the
gitignored captures aren't present in this checkout.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# Captures are gitignored and only live in the main checkout. Look in the
# worktree first, then the canonical repo path, and skip if neither has it.
_CAPTURE_DIRS = (
    Path(__file__).parent.parent / "ws_captures",
    Path("/Users/noah/Desktop/Github/CatanBot/ws_captures"),
)
_GOLDRUSH_LIVE = "goldrush-live-2026-06-01.jsonl"
_GOLDRUSH_SETUP = "goldrush-setup-fixture.jsonl"


def _find_capture(name: str) -> Path | None:
    for d in _CAPTURE_DIRS:
        p = d / name
        if p.exists():
            return p
    return None


@pytest.fixture(autouse=True)
def _restore_global_config():
    """Replaying a real Gold Rush GameStart auto-detects this game's VP
    target (15) and discard limit (10) into global config. Snapshot it
    before each test and restore after so the capture-driven tests can't
    leak a 15-VP target into other modules' default-10-VP assumptions."""
    from catanbot import config

    saved_vp = config.get_vp_target()
    saved_dl = config.get_discard_limit()
    try:
        yield
    finally:
        config.set_vp_target(saved_vp)
        config.set_discard_limit(saved_dl)


def _replay(path: Path, limit: int | None = None, until_started: bool = False):
    """Replay a jsonl WS capture line-by-line through the live pipeline."""
    from catanbot.bridge import _feed_ws_payload
    from catanbot.live_game import LiveGame

    g = LiveGame()
    i = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                try:
                    _feed_ws_payload(g, payload)
                except Exception:  # noqa: BLE001
                    pass
            i += 1
            if until_started and getattr(g, "started", False):
                break
            if limit is not None and i >= limit:
                break
    return g


def _fresh_game():
    from catanatron import Color, Game, RandomPlayer

    g = Game([RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                        Color.WHITE, Color.ORANGE)], seed=1)
    b = g.state.board
    b.build_settlement(Color.RED, 0, initial_build_phase=True)
    b.build_road(Color.RED, (0, 1))
    return g


def _mark_far_node_fog(g, far_int: int) -> frozenset:
    """Stamp a fog-hex signature on a tile touching ``far_int`` and return
    the fog node set. Mirrors what annotate_fog_nodes derives from a live
    board: a resource-less, number-less tile whose corners are all fog."""
    m = g.state.board.map
    fog_set: set[int] = set()
    for tile in m.adjacent_tiles.get(far_int, []):
        if hasattr(tile, "nodes"):
            try:
                tile.resource = None
                tile.number = None
            except Exception:  # noqa: BLE001
                pass
            fog_set.update(int(n) for n in tile.nodes.values())
            break
    m.fog_node_ids = frozenset(fog_set)
    return m.fog_node_ids


# --------------------------------------------------------------------------
# colonist_map annotation
# --------------------------------------------------------------------------

def test_annotate_fog_nodes_empty_on_classic_board():
    """A board with no fog hexes annotates an empty fog set (and the attr
    is always present so callers can read it unconditionally)."""
    from catanatron.models.map import BASE_MAP_TEMPLATE, CatanMap
    from catanbot.colonist_map import annotate_fog_nodes

    cat_map = CatanMap.from_template(BASE_MAP_TEMPLATE)
    annotate_fog_nodes(cat_map, {})
    assert cat_map.fog_node_ids == frozenset()


def test_refresh_fog_nodes_shrinks_as_fog_reveals():
    """refresh_fog_nodes reads the live mapping.tile_types: a fog tile
    (type 7) contributes its corners, a revealed tile (real type) does
    not. Re-deriving after a reveal drops the revealed tile's corners."""
    from catanatron.models.map import BASE_MAP_TEMPLATE, CatanMap
    from catanbot.colonist_map import refresh_fog_nodes

    cat_map = CatanMap.from_template(BASE_MAP_TEMPLATE)
    # Two real land tiles; pretend each is a fog hex via tile_types.
    coords = list(cat_map.land_tiles.keys())[:2]

    class _Mapping:
        tile_coord = {0: coords[0], 1: coords[1]}
        tile_types = {0: 7, 1: 8}  # both fog

    refresh_fog_nodes(cat_map, _Mapping())
    both = set(cat_map.fog_node_ids)
    assert both, "expected fog corners while both tiles are fog"

    # Reveal tile 1 (flip its colonist type to a real resource int).
    _Mapping.tile_types = {0: 7, 1: 1}  # tile 1 revealed to wood
    refresh_fog_nodes(cat_map, _Mapping())
    one = set(cat_map.fog_node_ids)
    assert one < both, f"fog set should shrink on reveal: {one} vs {both}"


# --------------------------------------------------------------------------
# Gap 1: fog pursuit in the opening scorer + road scorers
# --------------------------------------------------------------------------

def test_opening_scorer_credits_fog_adjacent_node():
    """A node touching the fog ring scores strictly higher than the same
    node with fog cleared, and exposes a FOG wildcard resource."""
    from catanbot.advisor import score_opening_nodes

    g = _fresh_game()
    m = g.state.board.map
    # Pick a land node and stamp the fog signature on one of its tiles.
    target = 25
    fog = _mark_far_node_fog(g, target)
    assert target in fog

    with_fog = {s.node_id: s for s in score_opening_nodes(g)}
    assert "FOG" in with_fog[target].resources
    score_with = with_fog[target].score

    m.fog_node_ids = frozenset()
    without = {s.node_id: s for s in score_opening_nodes(g)}
    assert score_with > without[target].score


def test_best_opening_road_aims_at_fog_reveal():
    """_best_opening_road values a direction whose far/landing touches fog
    above the same geometry with no fog, so fog pursuit moves the pick."""
    from catanbot.advisor import (
        _best_opening_road, _build_node_neighbors,
    )

    g = _fresh_game()
    m = g.state.board.map
    neighbors = _build_node_neighbors(m)
    land = set(m.land_nodes)
    first, second = 0, 1
    # Find a far node reachable from `second` and mark it fog.
    far = next(n for n in neighbors.get(second, ()) if n != first)
    fog = _mark_far_node_fog(g, far)

    road_fog = _best_opening_road(m, first, second, neighbors, land)
    m.fog_node_ids = frozenset()
    road_nofog = _best_opening_road(m, first, second, neighbors, land)

    assert road_fog is not None and road_nofog is not None
    # The fog reveal must raise the landing score for the same direction
    # (or flip the chosen direction toward fog), so either way it rises.
    assert road_fog.landing_score > road_nofog.landing_score
    # And at least one endpoint on the fog-aware road touches fog.
    touches = (road_fog.far_node in fog
               or (road_fog.landing_node in fog
                   if road_fog.landing_node is not None else False))
    assert touches


def test_midgame_road_rec_surfaces_road_into_fog():
    """A buildable road whose far end is a fog corner produces no
    production, so on the classic scorer it vanished from the rec list.
    With the fog bonus it surfaces as a road rec flagged 'reveals fog'."""
    from catanbot.recommender import recommend_actions

    g = _fresh_game()
    b = g.state.board
    edges = list(b.buildable_edges(__import__("catanatron").Color.RED))
    far = next(int(x) for (a, x) in edges
               for x in (int(a), int(x)) if int(x) not in (0, 1))
    fog = _mark_far_node_fog(g, far)

    out = recommend_actions(g, "RED", {"WOOD": 1, "BRICK": 1}, top=8)
    road_to_fog = [
        r for r in out
        if r.get("kind") == "road"
        and (any(int(x) in fog for x in (r.get("edge") or []))
             or r.get("landing_node") in fog)
    ]
    assert road_to_fog, "a road into the fog ring should surface"
    assert any("reveals fog" in (r.get("detail") or "") for r in road_to_fog)


# --------------------------------------------------------------------------
# Gap 2: Road Building timing on a fog board
# --------------------------------------------------------------------------

class _FakeTracker:
    def __init__(self, game):
        self.game = game


class _FakeGame:
    """Wrapper exposing .tracker.game like the live bridge passes in."""
    def __init__(self, cat_game):
        self.tracker = _FakeTracker(cat_game)


def _rb_game_with_card():
    from catanatron import Color

    g = _fresh_game()
    idx = g.state.color_to_index[Color.RED]
    g.state.player_state[f"P{idx}_ROAD_BUILDING_IN_HAND"] = 1
    return g, Color.RED


def test_rb_hint_holds_with_no_fog_and_no_lr_swing():
    """Baseline: no fog, no longest-road swing, roads plentiful -> HOLD."""
    from catanbot.bridge_hints import _compute_rb_hint

    g, _color = _rb_game_with_card()
    g.state.board.map.fog_node_ids = frozenset()
    hint = _compute_rb_hint(_FakeGame(g), "RED")
    assert hint is not None
    assert hint["should_play"] is False


def test_rb_hint_plays_to_reveal_fog():
    """Fog in reach of a free road flips the verdict to PLAY with a reason
    that names the fog reveal, even with no longest-road swing."""
    from catanbot.bridge_hints import _compute_rb_hint, _free_road_reaches_fog

    g, color = _rb_game_with_card()
    edges = list(g.state.board.buildable_edges(color))
    far = next(int(x) for (a, x) in edges
               for x in (int(a), int(x)) if int(x) not in (0, 1))
    _mark_far_node_fog(g, far)

    assert _free_road_reaches_fog(_FakeGame(g), color) > 0
    hint = _compute_rb_hint(_FakeGame(g), "RED")
    assert hint["should_play"] is True
    assert "fog" in hint["reason"].lower()


# --------------------------------------------------------------------------
# Gap 3: opening restriction excludes fog-adjacent corners
# --------------------------------------------------------------------------

def test_recommend_opening_excludes_fog_corners_when_restricted():
    """Under restrictedStartingPlacement the opening recs must never land
    on a fog-adjacent node; clearing the flag lets them back in."""
    from catanbot.recommender import recommend_opening

    g = _fresh_game()
    # Fresh empty-ish board: clear RED's seeded build so round-1 has a
    # full legal pool to rank.
    g2 = __import__("catanatron").Game(
        [__import__("catanatron").RandomPlayer(c)
         for c in __import__("catanatron").Color][:4], seed=2)
    m = g2.state.board.map
    # Mark a chunk of nodes fog (a couple of tiles' worth).
    fog: set[int] = set()
    for tile in list(m.land_tiles.values())[:3]:
        try:
            tile.resource = None
            tile.number = None
        except Exception:  # noqa: BLE001
            pass
        fog.update(int(n) for n in tile.nodes.values())
    m.fog_node_ids = frozenset(fog)

    m.restricted_starting_placement = True
    restricted = recommend_opening(g2, None, top=20)
    assert restricted, "opening recs should still exist on shown corners"
    assert not any(r.get("node_id") in fog for r in restricted), (
        "restricted opening must not recommend a fog-adjacent corner")

    m.restricted_starting_placement = False
    unrestricted = recommend_opening(g2, None, top=54)
    # With the restriction off, fog corners are eligible again, so at least
    # one should appear among the ranked picks (control assertion).
    assert any(r.get("node_id") in fog for r in unrestricted)


# --------------------------------------------------------------------------
# Gap 4: gold valuation mid-game + gold pick
# --------------------------------------------------------------------------

def test_node_production_credits_gold_wildcard():
    """A gold-adjacent node (resource-less tile with a real number) gets the
    wildcard yield folded into its production so settle/city recs value it."""
    from catanbot.recommender import _node_gold_value, _node_pip_production

    g = _fresh_game()
    m = g.state.board.map
    node = 25
    base = _node_pip_production(m, node)
    # Stamp gold on a tile touching `node`.
    for tile in m.adjacent_tiles.get(node, []):
        if hasattr(tile, "nodes"):
            tile.resource = None
            tile.number = 6  # 5 pips
            m.gold_node_ids = frozenset(int(n) for n in tile.nodes.values())
            m.gold_number = 6
            break
    assert _node_gold_value(m, node) > 0
    assert _node_pip_production(m, node) > base


def test_gold_pick_targets_bottleneck_then_thinnest():
    """gold_pick takes the bottleneck resource of the nearest build, and
    banks the thinnest-produced resource when every build is affordable."""
    from catanbot.bridge_economy import _gold_resource_pick

    # Everything affordable -> bank the thinnest-produced (ORE here).
    pick = _gold_resource_pick(
        {"WHEAT": 5, "ORE": 5, "WOOD": 3, "BRICK": 3, "SHEEP": 3},
        {"ORE": 0.02, "WHEAT": 0.4, "WOOD": 0.3, "BRICK": 0.3, "SHEEP": 0.3},
    )
    assert pick is not None
    assert pick["resource"] == "ORE"

    # Short exactly one card for a build -> pick that completing resource.
    pick2 = _gold_resource_pick({"WHEAT": 2, "ORE": 2}, {"ORE": 0.1})
    assert pick2 is not None
    assert pick2["resource"] in ("ORE", "WHEAT", "SHEEP")
    assert pick2.get("toward")


# --------------------------------------------------------------------------
# Capture-driven (skip when the gitignored Gold Rush captures are absent)
# --------------------------------------------------------------------------

def test_capture_sets_restricted_placement_and_fog():
    """The real Gold Rush GameStart stamps restricted_starting_placement
    True and seeds the fog ring on the built map."""
    cap = _find_capture(_GOLDRUSH_LIVE)
    if cap is None:
        pytest.skip("Gold Rush live capture not present")
    g = _replay(cap, until_started=True)
    cat_map = g.tracker.game.state.board.map
    assert getattr(cat_map, "restricted_starting_placement", False) is True
    from catanbot.colonist_map import refresh_fog_nodes
    refresh_fog_nodes(cat_map, g.session.mapping)
    assert len(cat_map.fog_node_ids) > 0


def test_capture_opening_recs_avoid_fog_corners():
    """Replaying the real GameStart and asking for opening recs, none may
    land on a fog-adjacent corner (restricted placement honoured)."""
    cap = _find_capture(_GOLDRUSH_LIVE)
    if cap is None:
        pytest.skip("Gold Rush live capture not present")
    from catanbot.colonist_map import refresh_fog_nodes
    from catanbot.recommender import recommend_opening

    g = _replay(cap, until_started=True)
    cat_map = g.tracker.game.state.board.map
    refresh_fog_nodes(cat_map, g.session.mapping)
    fog = set(cat_map.fog_node_ids)
    assert fog
    recs = recommend_opening(g.tracker.game, None, top=10)
    assert not any(r.get("node_id") in fog for r in recs)


def test_capture_gold_reveals_and_feeds_pick():
    """Replaying the full game, the gold hex reveals mid-game (a numbered,
    resource-less tile) and refresh_gold_nodes lights up gold_node_ids so
    the gold-pick advisor and gold valuation fire."""
    cap = _find_capture(_GOLDRUSH_LIVE)
    if cap is None:
        pytest.skip("Gold Rush live capture not present")
    from catanbot.colonist_map import refresh_gold_nodes
    from catanbot.recommender import _node_gold_value

    g = _replay(cap)
    cat_map = g.tracker.game.state.board.map
    refresh_gold_nodes(cat_map)
    assert len(cat_map.gold_node_ids) > 0
    assert cat_map.gold_number
    nid = sorted(cat_map.gold_node_ids)[0]
    assert _node_gold_value(cat_map, nid) > 0


def test_capture_snapshot_builds_with_fog_and_gold():
    """End-to-end: a full advisor snapshot off the real capture builds
    without error and exposes the fog hint while fog remains."""
    cap = _find_capture(_GOLDRUSH_LIVE)
    if cap is None:
        pytest.skip("Gold Rush live capture not present")
    from catanbot.bridge import _build_advisor_snapshot
    from catanbot.live import ColorMap
    from catanbot.tracker import Tracker

    g = _replay(cap, limit=600)  # mid-game: fog still partly unrevealed
    st = {
        "seq": 1, "game": g, "ws_count": 600, "log_count": 0,
        "last_roll": None, "robber_pending": False, "robber_snapshot": None,
        "display_colors": {}, "pm_tracker": Tracker(), "pm_color_map": ColorMap(),
    }
    # The autouse _restore_global_config fixture saves/restores the VP
    # target the snapshot auto-detects (15) so it can't leak into other
    # tests' default-10-VP assumptions.
    snap = _build_advisor_snapshot(st)
    cat_map = g.tracker.game.state.board.map
    # Fog ring still annotated on the live map mid-game.
    assert getattr(cat_map, "fog_node_ids", None) is not None
    # The fog hint surfaces while fog remains (variant gate must pass).
    if snap.get("variant") in ("black_forest", "volcano"):
        assert snap.get("fog_hint") is not None
