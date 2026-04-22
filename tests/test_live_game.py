"""End-to-end: feed a real WS capture through LiveGame.feed and verify
the tracker ends up with a sensible board + hand state.

This is the regression we lean on for live-session polish — if any event
extraction or dispatch regresses, the counts stop matching the capture.
"""
from __future__ import annotations

from pathlib import Path

import pytest

CAPTURE_EARLY = (Path(__file__).parent.parent
                 / "ws_captures"
                 / "cataanbot-ws-fort4092-early-2026-04-21T23-23-22.json")
CAPTURE_MIDGAME = (Path(__file__).parent.parent
                   / "ws_captures"
                   / "cataanbot-ws-fort4092-midgame-2026-04-21T23-34-04.json")


def _iter_payloads(path: Path):
    from cataanbot.colonist_proto import load_capture
    for frame in load_capture(path):
        if frame.error:
            continue
        p = frame.payload
        if isinstance(p, dict):
            yield p


def test_feed_game_start_boots_everything():
    if not CAPTURE_EARLY.exists():
        pytest.skip("live capture not present")
    from cataanbot.live_game import LiveGame
    game = LiveGame()
    assert not game.started

    # The first type=4 we see should fully boot the game.
    for payload in _iter_payloads(CAPTURE_EARLY):
        game.feed(payload)
        if game.started:
            break
    assert game.started
    assert game.tracker is not None
    assert game.session is not None
    assert game.color_map is not None
    # Map should be the colonist-derived one — 19 land tiles, 9 ports.
    cat_map = game.tracker.game.state.board.map
    assert len(cat_map.land_tiles) == 19
    # Color map seeded with all known players.
    assert len(game.color_map.as_dict()) == len(game.session.player_names)


def test_feed_midgame_capture_builds_and_rolls_apply():
    """Replay the midgame capture after seeding from the GameStart of
    the early capture (they're the same game). Every Build/Roll/Produce
    that lands should either apply cleanly or be a known skip — if the
    pipeline regresses, we start seeing ``error`` status."""
    if not CAPTURE_EARLY.exists() or not CAPTURE_MIDGAME.exists():
        pytest.skip("live captures not present")
    from cataanbot.live_game import LiveGame

    game = LiveGame()

    # Seed from the early capture's GameStart.
    for payload in _iter_payloads(CAPTURE_EARLY):
        game.feed(payload)
        if game.started:
            break
    assert game.started

    # Drive every diff from both captures through the pipeline.
    status_counts: dict[str, int] = {}
    errors: list[str] = []
    for path in (CAPTURE_EARLY, CAPTURE_MIDGAME):
        for payload in _iter_payloads(path):
            for result in game.feed(payload):
                status_counts[result.status] = (
                    status_counts.get(result.status, 0) + 1)
                if result.status == "error":
                    errors.append(
                        f"{type(result.event).__name__}: {result.message}")

    # Something must have actually applied — if we regress to zero
    # applies, the whole WS pipeline is broken.
    assert status_counts.get("applied", 0) > 0, (
        f"nothing applied; status breakdown was {status_counts}")
    # Errors are unexpected given the event sources are WS-built with
    # real topology. If any show up, surface them explicitly.
    assert not errors, f"dispatch errors: {errors[:5]}"

    # Board state should be non-empty: some settlements/cities placed.
    buildings = game.tracker.game.state.board.buildings
    assert len(buildings) >= 4, (
        f"expected at least the 4 initial settlements, got {len(buildings)}")


def test_paid_builds_debit_costs_and_setup_is_free():
    """First 2 settlements + 2 roads per color are free; subsequent
    settlements/cities/roads should debit the standard build cost."""
    if not CAPTURE_EARLY.exists():
        pytest.skip("live capture not present")
    from cataanbot.events import BuildEvent
    from cataanbot.live_game import LiveGame

    game = LiveGame()
    for payload in _iter_payloads(CAPTURE_EARLY):
        game.feed(payload)
        if game.started:
            break
    assert game.started

    red = game.color_map.get("Alice" if game.color_map.has("Alice") else
                             next(iter(game.color_map.as_dict())))
    before = dict(game.tracker.hand(red))
    # Pre-load enough cards to cover a settlement + a city + a road.
    for res, n in (("WOOD", 2), ("BRICK", 2), ("SHEEP", 1),
                   ("WHEAT", 3), ("ORE", 3)):
        game.tracker.give(red, n, res)

    # Simulate four settlement + two road BuildEvents: the first two
    # settlements and two roads are the setup-phase placements (free).
    username = game.color_map.reverse(red)
    free_events = [
        BuildEvent(player=username, piece="settlement", node_id=0),
        BuildEvent(player=username, piece="road", edge_nodes=(0, 1)),
        BuildEvent(player=username, piece="settlement", node_id=3),
        BuildEvent(player=username, piece="road", edge_nodes=(3, 4)),
    ]
    paid_events = [
        BuildEvent(player=username, piece="settlement", node_id=6),
        BuildEvent(player=username, piece="road", edge_nodes=(6, 7)),
        BuildEvent(player=username, piece="city", node_id=6),
    ]
    for ev in free_events + paid_events:
        game._debit_build(ev)

    after = game.tracker.hand(red)
    # Paid settlement = W+B+Sh+Wh, paid road = W+B, city = 2Wh+3Ore.
    # Net debit from paid builds only (setup was free).
    expected_debit = {"WOOD": 2, "BRICK": 2, "SHEEP": 1,
                      "WHEAT": 3, "ORE": 3}
    for res, n in expected_debit.items():
        got = before.get(res, 0) + n - after.get(res, 0)
        assert got == n, (
            f"{res}: expected debit of {n}, got {got} "
            f"(before={before.get(res, 0)}, after={after.get(res, 0)})")
