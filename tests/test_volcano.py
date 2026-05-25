"""Volcano weekly map (mapSetting 34) support.

The Volcano board is a 71-tile variant: an ore/desert-heavy visible board
with a single gold/volcano hex in the centre and ~18 Black Forest fog
hexes. Gold (tile type 6) pays a resource of your choice on its number;
fog (type 7) reveals into a real tile when a road points at it. These
tests pin the variant detection, the gold-node annotation, and that the
opening scorer treats the gold hex as a wildcard rather than dead desert.

Driven by a real capture taken 2026-05-23 (BrickdDaddy/Conah/Hubert).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from catanbot.advisor import score_opening_nodes
from catanbot.colonist_proto import load_capture
from catanbot.live_game import LiveGame
from catanbot.recommender import recommend_opening

VOLCANO_CAPTURE = (Path(__file__).parent.parent
                   / "ws_captures" / "volcano-2026-05-23.jsonl")


def _volcano_game() -> LiveGame:
    """Full replay of the capture (multiple stacked games — exercises the
    reboot path; ends on the last game's mid/late-game state)."""
    if not VOLCANO_CAPTURE.exists():
        pytest.skip(f"volcano capture not present at {VOLCANO_CAPTURE}")
    game = LiveGame()
    for fr in load_capture(VOLCANO_CAPTURE):
        if fr.error or not isinstance(fr.payload, dict):
            continue
        game.feed(fr.payload)
    if not game.started:
        pytest.skip("volcano capture did not boot a game")
    return game


def _volcano_fresh_game() -> LiveGame:
    """A pre-placement board built from the capture's last GameStart only
    — deterministic empty board for opening-pick / gold-annotation tests."""
    if not VOLCANO_CAPTURE.exists():
        pytest.skip(f"volcano capture not present at {VOLCANO_CAPTURE}")
    last_gs = None
    for fr in load_capture(VOLCANO_CAPTURE):
        if fr.error or not isinstance(fr.payload, dict):
            continue
        p = fr.payload
        if p.get("type") == 4:
            body = p.get("payload")
            if not isinstance(body, dict):
                continue
            gstate = body.get("gameState") if "gameState" in body else body
            if isinstance(gstate, dict) and isinstance(
                    gstate.get("mapState"), dict):
                last_gs = p
    if last_gs is None:
        pytest.skip("no usable GameStart in volcano capture")
    game = LiveGame()
    game.feed(last_gs)
    if not game.started:
        pytest.skip("volcano GameStart did not boot a game")
    return game


def test_volcano_builds_without_crash():
    """The 71-tile board parses into a catanatron map cleanly."""
    game = _volcano_game()
    m = game.tracker.game.state.board.map
    assert len(m.land_tiles) == 71


def test_volcano_no_build_drift_across_stacked_games():
    """Replaying the full capture (which stacks several games' GameStarts
    via the autosave) must not drop builds: catanatron's board ends in
    sync with colonist's authoritative corner ownership. Regression for
    the STATIC_GRAPH stacked-rebuild drift (board-regression reboot)."""
    game = _volcano_game()
    board = game.tracker.game.state.board
    cat_buildings = len(board.buildings)
    known = len(game.session.known_corners)
    assert cat_buildings == known, (
        f"board drift: catanatron has {cat_buildings} buildings, "
        f"colonist tracked {known}")


def test_volcano_variant_label():
    """gold(6)+fog(7) on a mapSetting board reads as 'volcano', not the
    generic 'variant:' fallback that suppresses recs."""
    game = _volcano_game()
    assert game.session.variant_label() == "volcano"


def test_volcano_recs_enabled_in_gate():
    """The recs gate must let 'volcano' through."""
    from catanbot.bridge import _RECS_SAFE_VARIANTS
    assert "volcano" in _RECS_SAFE_VARIANTS


def test_volcano_gold_nodes_annotated():
    """annotate_gold_nodes records the gold hex's 6 nodes + a real number."""
    game = _volcano_game()
    m = game.tracker.game.state.board.map
    assert len(m.gold_node_ids) == 6        # one hex == six corners
    assert m.gold_number in range(2, 13) and m.gold_number != 7


def test_volcano_gold_scored_as_wildcard():
    """A gold-adjacent node carries a GOLD resource + GOLD tile label."""
    game = _volcano_fresh_game()
    m = game.tracker.game.state.board.map
    gold_number = m.gold_number
    scores = {ns.node_id: ns for ns in score_opening_nodes(game.tracker.game)}
    gold_nodes = [scores[n] for n in m.gold_node_ids if n in scores]
    assert gold_nodes, "no gold nodes scored"
    assert any("GOLD" in ns.resources for ns in gold_nodes)
    assert any(("GOLD", gold_number) in ns.tiles for ns in gold_nodes)


def test_volcano_opening_recs_nonempty():
    """recommend_opening produces ranked picks on a fresh volcano board."""
    game = _volcano_fresh_game()
    recs = recommend_opening(game.tracker.game, None, top=5)
    assert recs, "expected opening recommendations on volcano board"


# --- gold-pick advisor: which resource to take when gold rolls -------------

def test_gold_pick_completes_a_build():
    """One resource short of a build → take that resource to finish it."""
    from catanbot.bridge_economy import _gold_resource_pick
    pick = _gold_resource_pick({"WHEAT": 2, "ORE": 2})  # 1 ore from a city
    assert pick["resource"] == "ORE"
    assert pick["toward"] == "city"
    assert "completes" in pick["reason"]


def test_gold_pick_breaks_ties_by_utility():
    """Short both sheep and wheat for a settlement → wheat wins the tie
    (more downstream builds)."""
    from catanbot.bridge_economy import _gold_resource_pick
    pick = _gold_resource_pick({"WOOD": 1, "BRICK": 1})
    assert pick["resource"] == "WHEAT"


def test_gold_pick_prefers_thin_production_when_flush():
    """When every build is affordable, bank the resource you produce
    least."""
    from catanbot.bridge_economy import _gold_resource_pick
    pick = _gold_resource_pick(
        {"WHEAT": 3, "ORE": 3, "WOOD": 2, "BRICK": 2, "SHEEP": 2},
        {"ORE": 0.0, "WHEAT": 0.3, "WOOD": 0.4, "BRICK": 0.2, "SHEEP": 0.3},
    )
    assert pick["resource"] == "ORE"
    assert pick["toward"] is None
