"""Tests for the colonist → Event extractor and the WS dispatcher path."""

from __future__ import annotations

from pathlib import Path

import pytest

from catanbot.colonist_diff import (
    LiveSession, LiveSessionError,
    events_from_diff, events_from_frame_payload, produce_events_for_roll,
)
from catanbot.colonist_proto import load_capture
from catanbot.events import (
    BankSyncEvent, BuildEvent, DevCardBuyEvent, DevCardSelfBuyTypedEvent,
    ProduceEvent, RobberMoveEvent, RollEvent, TileRevealEvent, VPEvent,
)
from catanbot.live import ColorMap, apply_event
from catanbot.tracker import Tracker


CAPTURE_EARLY = (Path(__file__).parent.parent
                 / "ws_captures"
                 / "catanbot-ws-fort4092-early-2026-04-21T23-23-22.json")
CAPTURE_MID = (Path(__file__).parent.parent
               / "ws_captures"
               / "catanbot-ws-fort4092-midgame-2026-04-21T23-34-04.json")


def _game_start_body(path: Path) -> dict:
    if not path.exists():
        pytest.skip(f"live capture not present at {path}")
    frames = list(load_capture(path))
    gs = next(f for f in frames if f.raw_length == 5156)
    return gs.payload["payload"]


# ---------------------------------------------------------------------------
# LiveSession construction
# ---------------------------------------------------------------------------

def test_from_game_start_resolves_usernames():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    # fort4092 saw BrickdDaddy (color 5), Elissa (1), Vtarj (2)
    assert sess.player_for(5) == "BrickdDaddy"
    assert sess.player_for(1) == "Elissa"
    assert sess.player_for(2) == "Vtarj"
    # Unknown color id fallbacks to a stable placeholder.
    assert sess.player_for(99) == "player99"


def test_from_game_start_requires_map_state():
    with pytest.raises(LiveSessionError):
        LiveSession.from_game_start({"playerUserStates": []})


def test_variant_label_classic_when_all_settings_zero():
    """All four variant flags == 0 in gameSettings → 'classic' board.
    First step toward variant-aware strategy: detect non-classic so
    we can warn the user our heuristics aren't tuned for it yet."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    assert sess.variant_label() == "classic"
    assert isinstance(sess.game_settings, dict)
    # The classic-board flags should all be 0 in this capture.
    for k in ("modeSetting", "extensionSetting",
              "scenarioSetting", "mapSetting"):
        assert sess.game_settings.get(k, 0) == 0, (
            f"capture wasn't classic — {k}={sess.game_settings.get(k)}")


def test_non_classic_tiles_detected_from_map():
    """Variant tile-type ints (anything outside 0-5) trigger
    non_classic_tiles and flip the variant_label even when the
    gameSettings flags are all 0 — catches custom maps that don't
    set an explicit extension flag but ship gold/ocean hexes."""
    body = _game_start_body(CAPTURE_EARLY)
    # Mutate one tile to type 6 (synthetic gold) so the sweep has
    # something to find. Fresh game_state copy so we don't pollute
    # the cached body for other tests.
    game_state = {**body["gameState"]}
    map_state = {**game_state["mapState"]}
    hex_states = dict(map_state["tileHexStates"])
    a_tid = next(iter(hex_states))
    hex_states[a_tid] = {**hex_states[a_tid], "type": 6}
    map_state["tileHexStates"] = hex_states
    game_state["mapState"] = map_state
    body = {**body, "gameState": game_state}

    sess = LiveSession.from_game_start(body)
    assert 6 in sess.non_classic_tiles
    label = sess.variant_label()
    assert label.startswith("variant:")
    assert "tiles={6}" in label


def test_variant_label_flags_non_classic():
    """Synthetic non-zero flags should produce a 'variant: ...' label
    listing the non-zero settings — the HUD's warning hook."""
    body = _game_start_body(CAPTURE_EARLY)
    body = {**body, "gameSettings": {**body.get("gameSettings", {}),
                                      "extensionSetting": 2,
                                      "mapSetting": 1}}
    sess = LiveSession.from_game_start(body)
    label = sess.variant_label()
    assert label.startswith("variant:")
    assert "extension=2" in label
    assert "map=1" in label


def test_from_game_start_seeds_existing_placements():
    body = _game_start_body(CAPTURE_EARLY)
    # Mutate a corner as if a settlement already sits there.
    game_state = {**body["gameState"], "mapState": {**body["gameState"]["mapState"]}}
    corners = dict(game_state["mapState"]["tileCornerStates"])
    some_cid = next(iter(corners))
    corners[some_cid] = {**corners[some_cid], "owner": 5, "buildingType": 1}
    game_state["mapState"]["tileCornerStates"] = corners
    body = {**body, "gameState": game_state}

    sess = LiveSession.from_game_start(body)
    # Replaying the same state as a diff shouldn't emit a build event —
    # we already knew about it.
    events = events_from_diff(sess, {"mapState": {
        "tileCornerStates": {some_cid: {"owner": 5, "buildingType": 1}}}})
    assert events == []


def test_from_game_start_latches_self_color_from_playerColor():
    """fort4092 capture was from BrickdDaddy (color 5) — ``playerColor``
    at the GameStart root directly names self's color, so
    ``self_color_id`` must latch to 5 immediately. This is the signal
    the round-2 opening picks depend on — without it, recommend_opening
    can't do complement-aware ranking against my placed settlement."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    assert sess.self_color_id == 5


def test_from_game_start_fallback_to_userId_when_playerColor_missing():
    """Belt-and-suspenders: if a future colonist variant drops
    ``playerColor``, the non-bot entry's ``userId`` is the fallback.
    Only bots have userId=null — the real client is always someone."""
    body = _game_start_body(CAPTURE_EARLY)
    body = {**body}
    body.pop("playerColor", None)
    sess = LiveSession.from_game_start(body)
    # The capture's non-bot entry (BrickdDaddy, userId=100859728) is at
    # selectedColor=5 — fallback must still land on 5.
    assert sess.self_color_id == 5


# ---------------------------------------------------------------------------
# Diff → Event translation
# ---------------------------------------------------------------------------

def test_self_dev_card_buy_emits_typed_event():
    """When self's developmentCards.cards list grows, the diff parser
    decodes the new int(s) via _DEV_CARD_TYPE and emits a
    DevCardSelfBuyTypedEvent. This is what populates catanatron's
    {type}_IN_HAND for self so the play-timing hints fire on the
    matching type instead of all four. Pinning the contract here
    so a refactor of the cards-list comparison can't silently
    revert to all-four-fire mode.
    """
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    self_cid = sess.self_color_id  # 5 in this capture
    # int 11 = KNIGHT per the decoded mapping
    diff = {"mechanicDevelopmentCardsState": {"players": {
        str(self_cid): {"developmentCards": {"cards": [11]}}}}}
    events = events_from_diff(sess, diff)
    typed = [e for e in events if isinstance(
        e, DevCardSelfBuyTypedEvent)]
    assert len(typed) == 1
    assert typed[0].card_type == "KNIGHT"
    # Untyped DevCardBuyEvent should NOT fire for self — that
    # untyped event handles opp resource debits, which don't apply
    # to self (HandSyncEvent does it).
    untyped = [e for e in events if isinstance(e, DevCardBuyEvent)]
    assert untyped == []


def test_opp_dev_card_buy_emits_untyped_event():
    """Opps' card type is hidden (placeholder int 10), so the diff
    parser keeps emitting untyped DevCardBuyEvent for them — the
    handler in live.py debits the WHEAT/SHEEP/ORE cost without
    knowing which type was bought. Test pins this for an opp cid."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    self_cid = sess.self_color_id
    opp_cid = next(c for c in sess.player_names if c != self_cid)
    # Opps see a placeholder (typically int 10)
    diff = {"mechanicDevelopmentCardsState": {"players": {
        str(opp_cid): {"developmentCards": {"cards": [10]}}}}}
    events = events_from_diff(sess, diff)
    untyped = [e for e in events if isinstance(e, DevCardBuyEvent)
               and not isinstance(e, DevCardSelfBuyTypedEvent)]
    assert len(untyped) == 1
    typed = [e for e in events if isinstance(
        e, DevCardSelfBuyTypedEvent)]
    assert typed == []


def test_self_dev_used_syncs_from_colonist():
    """Colonist ships ``developmentCardsUsed`` as the full play
    history for self (typed list, append-only). Mirror it on the
    session so downstream code has authoritative per-type played
    counts without trusting DOM-log play events."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    self_cid = sess.self_color_id
    assert sess.self_dev_used == []

    # Self played a knight (int 11), then a road building (int 14)
    diff = {"mechanicDevelopmentCardsState": {"players": {
        str(self_cid): {
            "developmentCards": {"cards": []},
            "developmentCardsUsed": [11, 14],
        }}}}
    events_from_diff(sess, diff)
    assert sess.self_dev_used == [11, 14]

    # Subsequent diff with one more played
    diff2 = {"mechanicDevelopmentCardsState": {"players": {
        str(self_cid): {
            "developmentCards": {"cards": []},
            "developmentCardsUsed": [11, 14, 11],
        }}}}
    events_from_diff(sess, diff2)
    assert sess.self_dev_used == [11, 14, 11]


def test_self_dev_bought_this_turn_syncs_from_colonist():
    """Colonist authoritatively reports self's
    ``developmentCardsBoughtThisTurn`` per turn — list of typed ints
    when bought, null when cleared on turn flip. The diff parser
    mirrors that to ``sess.self_dev_bought_this_turn`` so the
    advisor's just-bought carve-out tracks colonist's view exactly,
    no homemade end-turn detection required."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    self_cid = sess.self_color_id
    # Self bought a knight (int 11) this turn
    diff_buy = {"mechanicDevelopmentCardsState": {"players": {
        str(self_cid): {
            "developmentCards": {"cards": [11]},
            "developmentCardsBoughtThisTurn": [11],
        }}}}
    events_from_diff(sess, diff_buy)
    assert sess.self_dev_bought_this_turn == [11]

    # Turn flips — colonist clears the list to null
    diff_clear = {"mechanicDevelopmentCardsState": {"players": {
        str(self_cid): {
            "developmentCardsBoughtThisTurn": None,
        }}}}
    events_from_diff(sess, diff_clear)
    assert sess.self_dev_bought_this_turn == []


def test_self_unknown_dev_card_int_emits_no_event():
    """If colonist sends an int we haven't decoded (e.g. a future
    expansion adds a new type), the parser silently skips it
    rather than guessing. Better to miss a hint than tell the user
    they hold the wrong card."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    self_cid = sess.self_color_id
    diff = {"mechanicDevelopmentCardsState": {"players": {
        str(self_cid): {"developmentCards": {"cards": [99]}}}}}
    events = events_from_diff(sess, diff)
    typed = [e for e in events if isinstance(
        e, DevCardSelfBuyTypedEvent)]
    assert typed == []


def test_diff_settlement_becomes_build_event_with_node_id():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    pick_cid = next(cid for cid in sess.mapping.node_id
                    if cid not in sess.known_corners)
    events = events_from_diff(sess, {"mapState": {
        "tileCornerStates": {
            str(pick_cid): {"owner": 5, "buildingType": 1}}}})
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, BuildEvent)
    assert ev.piece == "settlement"
    assert ev.player == "BrickdDaddy"
    assert ev.node_id == sess.mapping.node_id[pick_cid]


def test_diff_city_upgrade_emits_city_event():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    pick_cid = next(iter(sess.mapping.node_id))
    # First a settlement, then the upgrade.
    events_from_diff(sess, {"mapState": {
        "tileCornerStates": {
            str(pick_cid): {"owner": 1, "buildingType": 1}}}})
    events = events_from_diff(sess, {"mapState": {
        "tileCornerStates": {
            str(pick_cid): {"owner": 1, "buildingType": 2}}}})
    assert [e.piece for e in events] == ["city"]
    assert events[0].node_id == sess.mapping.node_id[pick_cid]


def test_diff_city_upgrade_without_owner_falls_back_to_cached_owner():
    """Colonist only ships the owner field when it actually changes. A
    settlement→city upgrade keeps the same owner, so the diff arrives
    as ``{"buildingType": 2}`` alone. Without a fallback to the cached
    owner, the upgrade gets dropped and the tracker keeps showing
    SETTLEMENT at that corner — which is how the recommender ends up
    suggesting "build a city" on a node that's already a city."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    pick_cid = next(iter(sess.mapping.node_id))
    events_from_diff(sess, {"mapState": {
        "tileCornerStates": {
            str(pick_cid): {"owner": 1, "buildingType": 1}}}})
    # Upgrade ships without owner — the real colonist shape.
    events = events_from_diff(sess, {"mapState": {
        "tileCornerStates": {
            str(pick_cid): {"buildingType": 2}}}})
    assert [e.piece for e in events] == ["city"]
    assert events[0].player == sess.player_for(1)
    assert events[0].node_id == sess.mapping.node_id[pick_cid]


def test_diff_road_becomes_build_event_with_edge_nodes():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    pick_eid = next(eid for eid in sess.mapping.edge_nodes
                    if eid not in sess.known_edges)
    events = events_from_diff(sess, {"mapState": {
        "tileEdgeStates": {
            str(pick_eid): {"owner": 2, "type": 1}}}})
    assert len(events) == 1
    ev = events[0]
    assert ev.piece == "road"
    assert ev.player == "Vtarj"
    pair = sess.mapping.edge_nodes[pick_eid]
    assert set(ev.edge_nodes) == set(pair)


def test_diff_robber_becomes_move_event_with_coord():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    any_tid = next(iter(sess.mapping.tile_coord))
    events = events_from_diff(sess, {
        "mechanicRobberState": {"locationTileIndex": any_tid}})
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, RobberMoveEvent)
    assert ev.coord == sess.mapping.tile_coord[any_tid]


def test_diff_fog_reveal_emits_tile_reveal_event():
    """Black Forest: a tileHexStates diff flipping a fog tile (type 7/8)
    to a real resource emits one TileRevealEvent and updates the
    mapping so a re-broadcast of the same hex doesn't double-fire."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    tid = next(iter(sess.mapping.tile_coord))
    sess.mapping.tile_types[tid] = 7  # mark fog
    events = events_from_diff(sess, {"mapState": {
        "tileHexStates": {str(tid): {"type": 3, "diceNumber": 8}}}})
    reveals = [e for e in events if isinstance(e, TileRevealEvent)]
    assert len(reveals) == 1
    ev = reveals[0]
    assert ev.coord == sess.mapping.tile_coord[tid]
    assert ev.resource == "SHEEP"
    assert ev.number == 8
    assert sess.mapping.tile_types[tid] == 3
    again = events_from_diff(sess, {"mapState": {
        "tileHexStates": {str(tid): {"type": 3, "diceNumber": 8}}}})
    assert not [e for e in again if isinstance(e, TileRevealEvent)]


def test_diff_fog_reveal_applies_resource_on_tracker():
    """A TileRevealEvent dispatched to the tracker mutates the live
    CatanMap so the freshly revealed hex carries its resource/number."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    body = _game_start_body(CAPTURE_EARLY)
    from catanbot.colonist_map import build_catanatron_map_from_colonist
    cat_map = build_catanatron_map_from_colonist(
        body["gameState"]["mapState"], sess.mapping)
    tracker = Tracker(catan_map=cat_map)
    coord = next(iter(tracker.game.state.board.map.land_tiles))
    apply_event(tracker, ColorMap(),
                TileRevealEvent(coord=coord, resource="ORE", number=11))
    tile = tracker.game.state.board.map.land_tiles[coord]
    assert tile.resource == "ORE"
    assert tile.number == 11


def test_diff_bank_state_emits_bank_sync_event():
    """A bankState diff merges into the running bank and emits one
    BankSyncEvent; an unchanged re-broadcast emits nothing."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    events = events_from_diff(sess, {
        "bankState": {"resourceCards": {"1": 12, "5": 3}}})
    syncs = [e for e in events if isinstance(e, BankSyncEvent)]
    assert len(syncs) == 1
    assert syncs[0].resources["WOOD"] == 12
    assert syncs[0].resources["ORE"] == 3
    unchanged = events_from_diff(sess, {
        "bankState": {"resourceCards": {"1": 12}}})
    assert not [e for e in unchanged if isinstance(e, BankSyncEvent)]


def test_variant_label_black_forest_for_fog_board():
    """A board whose only non-classic tiles are fog hexes (7/8) and
    whose only variant flag is the map id labels as black_forest, so
    the recs gate keeps recommendations on."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    sess.non_classic_tiles = {7, 8}
    sess.game_settings = {"mapSetting": 33}
    assert sess.variant_label() == "black_forest"


def test_diff_dice_roll_emits_roll_event():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    events = events_from_diff(sess, {
        "diceState": {"dice1": 3, "dice2": 4, "diceThrown": True},
        "currentState": {"currentTurnPlayerColor": 5},
    })
    assert any(isinstance(e, RollEvent) for e in events)
    roll = next(e for e in events if isinstance(e, RollEvent))
    assert (roll.d1, roll.d2) == (3, 4)
    assert roll.player == "BrickdDaddy"


def test_diff_without_fresh_dice_emits_no_roll():
    """diceThrown: False alone (roll-consumed frame) isn't a new roll."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    events = events_from_diff(sess, {
        "diceState": {"diceThrown": False}})
    assert not any(isinstance(e, RollEvent) for e in events)


def test_produce_events_for_roll_skips_robber_tile():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    # Put a settlement on the first corner of some tile that rolls on 6.
    # Owner = Elissa (color 1) — produce_events_for_roll skips the self
    # player (BrickdDaddy / color 5) since their hand is synced via
    # resourceCards instead.
    six_tid = next(tid for tid, d in sess.mapping.tile_dice.items()
                   if d == 6)
    any_cid = next(iter(sess.mapping.tile_corners[six_tid]))
    sess.known_corners[any_cid] = 1
    sess.corner_owners[any_cid] = 1

    got = produce_events_for_roll(sess, 6)
    assert got and got[0].player == "Elissa"

    sess.robber_tile_id = six_tid
    # With the robber on the only 6-tile that corner touches, yields may
    # still appear from *other* 6-tiles if the corner is at a junction,
    # but the robbed tile itself never contributes — so the count drops.
    without_robber = sum(v for ev in produce_events_for_roll(sess, 6)
                         for v in ev.resources.values())
    assert without_robber < sum(v for ev in got for v in ev.resources.values())


def test_produce_events_for_roll_handles_seven():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    assert produce_events_for_roll(sess, 7) == []


def test_produce_events_for_roll_skips_self_player():
    """Once we've latched onto the self-player's color id, their yield
    must NOT be emitted as a ProduceEvent — HandSync from the resource-
    cards snapshot already captures it absolutely, so a delta on top
    would double-count. Opponents still get their deltas."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    six_tid = next(tid for tid, d in sess.mapping.tile_dice.items()
                   if d == 6)
    corners = list(sess.mapping.tile_corners[six_tid])
    # Self player's corner (latched below).
    sess.known_corners[corners[0]] = 1
    sess.corner_owners[corners[0]] = 5  # BrickdDaddy
    # Opponent's corner on the same tile.
    sess.known_corners[corners[1]] = 1
    sess.corner_owners[corners[1]] = 1  # Elissa

    sess.self_color_id = 5
    events = produce_events_for_roll(sess, 6)
    players = {ev.player for ev in events}
    assert "BrickdDaddy" not in players, (
        f"self-player leaked into produce events: {players}")
    assert "Elissa" in players, (
        "opponent yield should still be emitted")


def test_events_from_frame_payload_emits_roll_plus_produce():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    six_tid = next(tid for tid, d in sess.mapping.tile_dice.items() if d == 6)
    any_cid = next(iter(sess.mapping.tile_corners[six_tid]))
    sess.known_corners[any_cid] = 1
    sess.corner_owners[any_cid] = 1  # Elissa

    events = events_from_frame_payload(sess, {
        "type": 91,
        "payload": {"diff": {
            "diceState": {"dice1": 3, "dice2": 3, "diceThrown": True},
            "currentState": {"currentTurnPlayerColor": 1},
        }},
        "sequence": 1,
    })
    assert any(isinstance(e, RollEvent) for e in events)
    assert any(isinstance(e, ProduceEvent) and e.player == "Elissa"
               for e in events)


def test_empty_or_unrelated_diff_emits_nothing():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    assert events_from_diff(sess, {}) == []
    # A roll-consumed frame carries only diceThrown=False, not a fresh
    # pair of dice values — no new RollEvent.
    assert events_from_diff(
        sess, {"diceState": {"diceThrown": False}}) == []


def test_events_from_frame_payload_filters_non_diff_frames():
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    # Not a type=91 frame.
    assert events_from_frame_payload(sess, {"type": 4, "payload": {}}) == []
    # Type=91 but empty diff.
    assert events_from_frame_payload(
        sess, {"type": 91, "payload": {"diff": {}}, "sequence": 1}) == []


# ---------------------------------------------------------------------------
# Dispatcher: BuildEvent / RobberMoveEvent with topology fields → tracker
# ---------------------------------------------------------------------------

def test_build_event_with_node_id_settles_on_tracker():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    node_id = next(iter(t.game.state.board.map.land_nodes))
    result = apply_event(t, cm, BuildEvent(
        player="Alice", piece="settlement", node_id=node_id))
    assert result.status == "applied"
    assert t.game.state.board.buildings[node_id][1] == "SETTLEMENT"


def test_build_event_city_upgrades_on_tracker():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    node_id = next(iter(t.game.state.board.map.land_nodes))
    apply_event(t, cm, BuildEvent(
        player="Alice", piece="settlement", node_id=node_id))
    result = apply_event(t, cm, BuildEvent(
        player="Alice", piece="city", node_id=node_id))
    assert result.status == "applied"
    assert t.game.state.board.buildings[node_id][1] == "CITY"


def test_build_event_road_places_road_on_tracker():
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    board_map = t.game.state.board.map
    # Pick any land tile and grab an incident edge as (a, b) node ids.
    land_coord = next(iter(board_map.land_tiles))
    tile = board_map.land_tiles[land_coord]
    _, (a, b) = next(iter(tile.edges.items()))
    apply_event(t, cm, BuildEvent(
        player="Alice", piece="settlement", node_id=a))
    result = apply_event(t, cm, BuildEvent(
        player="Alice", piece="road", edge_nodes=(a, b)))
    assert result.status == "applied"
    assert (a, b) in t.game.state.board.roads \
        or (b, a) in t.game.state.board.roads


def test_robber_move_event_with_coord_updates_tracker():
    t = Tracker()
    cm = ColorMap()
    land_coord = next(iter(t.game.state.board.map.land_tiles))
    result = apply_event(t, cm, RobberMoveEvent(
        player="", tile_label="", prob=None, coord=land_coord))
    assert result.status == "applied"
    assert t.game.state.board.robber_coordinate == land_coord


def test_build_event_without_topology_stays_unhandled():
    """Regression guard for the DOM-parse code path."""
    t = Tracker()
    cm = ColorMap({"Alice": "RED"})
    result = apply_event(t, cm, BuildEvent(
        player="Alice", piece="settlement"))
    assert result.status == "unhandled"


# ---------------------------------------------------------------------------
# End-to-end: stream every type=91 frame from the midgame capture and
# make sure every produced event applies cleanly to the tracker.
# ---------------------------------------------------------------------------

def test_midgame_capture_streams_into_tracker_without_errors():
    if not CAPTURE_MID.exists():
        pytest.skip(f"midgame capture not present at {CAPTURE_MID}")

    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    tracker = Tracker()
    cm = ColorMap()

    # Seed tracker with the initial placements we snapshotted in
    # known_corners/known_edges so the midgame diffs play out against a
    # realistic board.
    map_state = _game_start_body(CAPTURE_EARLY)["gameState"]["mapState"]
    for cid_str, c in map_state["tileCornerStates"].items():
        bt = int(c.get("buildingType") or 0)
        owner = c.get("owner")
        if not bt or owner is None:
            continue
        node_id = sess.mapping.node_id[int(cid_str)]
        player = sess.player_for(int(owner))
        color = cm.get(player)
        tracker.settle(color, node_id)
        if bt == 2:
            tracker.city(color, node_id)
    for eid_str, e in map_state["tileEdgeStates"].items():
        owner = e.get("owner")
        if not owner:
            continue
        pair = sess.mapping.edge_nodes[int(eid_str)]
        a, b = tuple(pair)
        player = sess.player_for(int(owner))
        color = cm.get(player)
        try:
            tracker.road(color, a, b)
        except Exception:
            # Setup roads sometimes fail catanatron's connectivity check
            # when replayed out of order; skip silently for this smoke
            # test — the diff stream is what we're actually validating.
            pass

    applied = unhandled = errored = 0
    frames = list(load_capture(CAPTURE_MID))
    for frame in frames:
        if frame.error:
            continue
        for event in events_from_frame_payload(sess, frame.payload or {}):
            result = apply_event(tracker, cm, event)
            if result.status == "applied":
                applied += 1
            elif result.status == "error":
                errored += 1
            else:
                unhandled += 1

    assert applied > 0, "expected at least one applied build/robber event"
    assert errored == 0, (
        f"{errored} events raised tracker errors in midgame replay")


def _minimal_session() -> LiveSession:
    """LiveSession wired with stable per-color usernames — enough for the
    diff extractor to attribute VPEvents without needing a full capture."""
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    return sess


def test_diff_emits_longest_road_vpevent_on_has_flag_true():
    """``hasLongestRoad: true`` appearing on a color for the first time
    must produce a VPEvent(reason='longest_road') with that color as
    the new player. Without this, the tracker never flips HAS_ROAD
    true for the authoritative award and the +2 VP is invisible on
    the HUD even though colonist itself painted the bonus."""
    sess = _minimal_session()
    diff = {
        "mechanicLongestRoadState": {
            "1": {"longestRoad": 5, "hasLongestRoad": True},
        },
    }
    events = events_from_diff(sess, diff)
    vp_events = [e for e in events if isinstance(e, VPEvent)]
    assert len(vp_events) == 1, f"expected 1 VPEvent, got {events}"
    ev = vp_events[0]
    assert ev.reason == "longest_road"
    assert ev.player == sess.player_for(1)
    assert ev.previous_holder is None
    assert sess.has_longest_road_cid == 1


def test_diff_emits_largest_army_vpevent_with_previous_holder_on_transfer():
    """When LA transfers, colonist sets ``hasLargestArmy: true`` on the
    new holder; we must emit a VPEvent that names the *old* holder as
    previous_holder so ``_apply_vp`` strips HAS_ARMY from them before
    granting it — otherwise both flags end up true simultaneously and
    the VP counter double-counts."""
    sess = _minimal_session()
    sess.has_largest_army_cid = 2  # pretend Vtarj already held it
    diff = {
        "mechanicLargestArmyState": {
            "5": {"hasLargestArmy": True},
        },
    }
    events = events_from_diff(sess, diff)
    vp_events = [e for e in events if isinstance(e, VPEvent)]
    assert len(vp_events) == 1
    ev = vp_events[0]
    assert ev.reason == "largest_army"
    assert ev.player == sess.player_for(5)
    assert ev.previous_holder == sess.player_for(2)
    assert sess.has_largest_army_cid == 5


def test_diff_skips_vpevent_when_already_current_holder():
    """A later diff that re-ships ``hasLongestRoad: true`` on the same
    cid (e.g. the player keeps extending their road) must NOT emit a
    duplicate VPEvent — the bonus was already applied on the first
    transition, and double-firing would add another +2 VP each road."""
    sess = _minimal_session()
    sess.has_longest_road_cid = 1
    diff = {
        "mechanicLongestRoadState": {
            "1": {"longestRoad": 6, "hasLongestRoad": True},
        },
    }
    events = events_from_diff(sess, diff)
    assert not [e for e in events if isinstance(e, VPEvent)]


def test_diff_vpevent_wires_into_tracker_vp_with_bonus():
    """End-to-end: a diff awarding longest_road to a color whose
    internal road count is *below* 5 must still leave that color with
    HAS_ROAD=True and VP credited +2 after ``apply_event`` runs. This
    is the exact live bug — our tracker's own road count can lag
    colonist, and the HUD was losing the +2 because _recompute_longest
    _road was stripping the flag on the next build."""
    sess = _minimal_session()
    tracker = Tracker()
    # Start fresh — no roads on the board, nobody holds longest road.
    cm = ColorMap()
    # Pre-register so apply_event doesn't tack on a new color.
    cm.get(sess.player_for(1))

    diff = {
        "mechanicLongestRoadState": {
            "1": {"longestRoad": 5, "hasLongestRoad": True},
        },
    }
    for ev in events_from_diff(sess, diff):
        apply_event(tracker, cm, ev)

    color = cm.get(sess.player_for(1))
    state = tracker.game.state
    idx = state.color_to_index[tracker._color(color)]
    assert state.player_state[f"P{idx}_HAS_ROAD"] is True
    assert state.player_state[f"P{idx}_VICTORY_POINTS"] == 2, (
        f"VP should include +2 for longest road, got "
        f"{state.player_state[f'P{idx}_VICTORY_POINTS']}")


def test_vp_total_sums_colonist_victory_points_state():
    """``sess.vp_total`` must match the display VP colonist shows above
    a player's name: settlements*1 + cities*2 + VP-cards-held*1 +
    longest-road-flag*2 + largest-army-flag*2. Regression for the HUD
    showing VPs off-by-one against colonist's counter."""
    sess = _minimal_session()
    # 2 settles + 1 city + holds longest road = 2 + 2 + 2 = 6 VP.
    sess.victory_points_state[1] = {0: 2, 1: 1, 4: 1}
    assert sess.vp_total(1) == 6
    # Self-player adds a hidden VP card on top.
    sess.victory_points_state[5] = {0: 3, 1: 1, 2: 1}
    assert sess.vp_total(5) == 3 + 2 + 1  # 3 settles, 1 city, 1 VP card
    # Unknown color → 0.
    assert sess.vp_total(99) == 0
    assert sess.vp_total(None) == 0


def test_vp_total_updates_from_diff_playerstates():
    """A diff that reships ``victoryPointsState`` for a color must get
    merged onto the running state (not replace it wholesale — colonist
    only sends changed keys), so ``vp_total`` reflects the new award."""
    sess = _minimal_session()
    sess.victory_points_state[1] = {0: 3, 1: 1}  # 3 settles, 1 city = 5
    diff = {
        "playerStates": {
            "1": {"victoryPointsState": {"4": 1}},  # now also has longest road
        },
    }
    events_from_diff(sess, diff)
    assert sess.victory_points_state[1] == {0: 3, 1: 1, 4: 1}
    assert sess.vp_total(1) == 3 + 2 + 2  # 3 settles + 1 city + longest road


def test_vp_total_seeds_from_game_start_playerstates():
    """On a mid-game reconnect, GameStart ships each player's current
    victoryPointsState. from_game_start must seed LiveSession with
    these so the first advisor snapshot already shows authoritative VPs
    — no waiting for further diffs to catch up."""
    body = {
        "gameState": {
            "mapState": _minimal_session().mapping.map_state_raw
            if hasattr(_minimal_session().mapping, "map_state_raw")
            else _game_start_body(CAPTURE_EARLY)["gameState"]["mapState"],
            "playerStates": {
                "1": {
                    "color": 1,
                    "victoryPointsState": {"0": 2, "1": 1, "4": 1},
                },
                "5": {
                    "color": 5,
                    "victoryPointsState": {"0": 1, "1": 2, "2": 1},
                },
            },
        },
        "playerColor": 5,
        "playerUserStates": [
            {"selectedColor": 1, "username": "Elissa", "isBot": False},
            {"selectedColor": 5, "username": "Me", "userId": 42,
             "isBot": False},
        ],
    }
    sess = LiveSession.from_game_start(body)
    # Elissa: 2 settles + 1 city + longest road = 2 + 2 + 2 = 6
    assert sess.vp_total(1) == 6
    # Me: 1 settle + 2 cities + 1 VP card = 1 + 4 + 1 = 6
    assert sess.vp_total(5) == 6


def test_tracker_recompute_longest_road_does_not_strip_without_displacer():
    """Regression for the stripping bug: if a color holds HAS_ROAD (set
    via VPEvent) and our internal road count is 0 (missed road diffs),
    a subsequent _recompute_longest_road must leave HAS_ROAD alone. In
    the old logic, the absence of any qualifier set new_holder=None and
    the code stripped the existing flag — silently wiping the +2 VP
    every time any build happened."""
    from catanatron import Color

    tracker = Tracker()
    state = tracker.game.state
    idx = state.color_to_index[Color.RED]
    state.player_state[f"P{idx}_HAS_ROAD"] = True

    tracker._recompute_longest_road()
    assert state.player_state[f"P{idx}_HAS_ROAD"] is True, (
        "HAS_ROAD was stripped despite no displacer — colonist's award "
        "would be silently erased")


def test_tracker_recompute_largest_army_preserves_flag_under_undercount():
    """Same guarantee for largest army: if HAS_ARMY is true from a
    colonist VPEvent but our PLAYED_KNIGHT counter is below the
    threshold (missed ``used [knight]`` DOM log lines), recompute must
    not strip the flag."""
    from catanatron import Color

    tracker = Tracker()
    state = tracker.game.state
    idx = state.color_to_index[Color.RED]
    state.player_state[f"P{idx}_HAS_ARMY"] = True

    tracker._recompute_largest_army()
    assert state.player_state[f"P{idx}_HAS_ARMY"] is True


def test_tracker_recompute_longest_road_still_transfers_on_real_displacer():
    """The conservative recompute must not become a black hole — when
    another color's actual internal road count strictly exceeds the
    current holder's, HAS_ROAD must move. This protects the replay /
    offline analysis path (no VPEvent stream) from freezing on the
    first holder forever."""
    from catanatron import Color

    tracker = Tracker()
    state = tracker.game.state
    # Current holder is RED per prior VPEvent but their internal count
    # will show 0 — BLUE builds a continuous 6-road and should displace.
    red_idx = state.color_to_index[Color.RED]
    blue_idx = state.color_to_index[Color.BLUE]
    state.player_state[f"P{red_idx}_HAS_ROAD"] = True

    # Stub in per-color lengths without actually building — patch the
    # board method so we can drive the recompute deterministically.
    tracker.game.state.board.continuous_roads_by_player = lambda c: (
        [[1, 2, 3, 4, 5, 6]] if c == Color.BLUE else [])
    tracker._recompute_longest_road()
    assert state.player_state[f"P{red_idx}_HAS_ROAD"] is False
    assert state.player_state[f"P{blue_idx}_HAS_ROAD"] is True


def test_duplicate_roll_diff_dedupped():
    """colonist's WS occasionally rebroadcasts a session state frame
    with the same diceState dict. Without dedup, events_from_diff
    would emit two RollEvents for the same physical roll, inflating
    the bridge's total_rolls + roll_histogram.

    Sessions track last_roll_emitted = (cid, d1, d2); a repeat is
    suppressed. New rolls (different dice or different player) flow
    through normally."""
    if not CAPTURE_EARLY.exists():
        pytest.skip("capture not present")
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    sess.current_turn_color_id = 1

    # First roll fires.
    diff1 = {"diceState": {"dice1": 4, "dice2": 3},
             "currentState": {"currentTurnPlayerColor": 1}}
    events = events_from_diff(sess, diff1)
    rolls = [e for e in events if isinstance(e, RollEvent)]
    assert len(rolls) == 1
    assert rolls[0].total == 7

    # Same diff arrives again — colonist resync. Should NOT re-emit.
    events_dup = events_from_diff(sess, diff1)
    rolls_dup = [e for e in events_dup if isinstance(e, RollEvent)]
    assert rolls_dup == [], (
        "duplicate roll diff was emitted again; dedup didn't fire")

    # Different dice — emits.
    diff2 = {"diceState": {"dice1": 5, "dice2": 6},
             "currentState": {"currentTurnPlayerColor": 1}}
    events2 = events_from_diff(sess, diff2)
    rolls2 = [e for e in events2 if isinstance(e, RollEvent)]
    assert len(rolls2) == 1
    assert rolls2[0].total == 11

    # New player rolling the same combo as the original — also emits.
    diff3 = {"diceState": {"dice1": 4, "dice2": 3},
             "currentState": {"currentTurnPlayerColor": 2}}
    events3 = events_from_diff(sess, diff3)
    rolls3 = [e for e in events3 if isinstance(e, RollEvent)]
    assert len(rolls3) == 1


def test_trade_offer_event_from_active_offers():
    """Colonist's WS ships incoming trade offers via
    ``tradeState.activeOffers``. Each new id is a TradeOfferEvent for
    the HUD's incoming-trade banner; partial updates (just
    playerResponses change) and self-created offers are filtered out."""
    from catanbot.events import TradeOfferEvent
    if not CAPTURE_EARLY.exists():
        pytest.skip("capture not present")
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    sess.self_color_id = 1
    diff = {"tradeState": {"activeOffers": {
        "abc123": {
            "id": "abc123",
            "creator": 5,
            "offeredResources": [2, 2],  # 2 brick
            "wantedResources": [3],      # 1 sheep
            "playerResponses": {"1": 0},
        },
    }}}
    events = events_from_diff(sess, diff)
    offers = [e for e in events if isinstance(e, TradeOfferEvent)]
    assert len(offers) == 1
    o = offers[0]
    assert o.player == sess.player_for(5)
    assert o.give == {"BRICK": 2}
    assert o.want == {"SHEEP": 1}


def test_trade_offer_event_dedupped_on_partial_update():
    """A second diff for the same offer id (e.g. partial response
    update) must not re-emit a TradeOfferEvent."""
    from catanbot.events import TradeOfferEvent
    if not CAPTURE_EARLY.exists():
        pytest.skip("capture not present")
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    sess.self_color_id = 1
    full = {"tradeState": {"activeOffers": {
        "abc123": {
            "id": "abc123",
            "creator": 5,
            "offeredResources": [4],
            "wantedResources": [5],
            "playerResponses": {"1": 0},
        },
    }}}
    events_from_diff(sess, full)
    # Partial update — just playerResponses, no creator/offered/wanted.
    partial = {"tradeState": {"activeOffers": {
        "abc123": {"playerResponses": {"1": 1}},
    }}}
    events = events_from_diff(sess, partial)
    assert [e for e in events if isinstance(e, TradeOfferEvent)] == []


def test_trade_offer_event_skips_self_created_offers():
    """When self created the offer (creator == self_color_id), no
    incoming-trade banner — it's an outgoing send, not a decision."""
    from catanbot.events import TradeOfferEvent
    if not CAPTURE_EARLY.exists():
        pytest.skip("capture not present")
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    sess.self_color_id = 1
    diff = {"tradeState": {"activeOffers": {
        "self01": {
            "id": "self01",
            "creator": 1,  # self
            "offeredResources": [4],
            "wantedResources": [5],
            "playerResponses": {"5": 0},
        },
    }}}
    events = events_from_diff(sess, diff)
    assert [e for e in events if isinstance(e, TradeOfferEvent)] == []
    # But the id should still be tracked so it doesn't re-fire on a
    # later partial update.
    assert "self01" in sess.active_offer_ids


def test_trade_offer_id_evicts_on_close():
    """A closed offer's id must drop from active_offer_ids so the next
    incoming offer with a fresh id can fire its event."""
    from catanbot.events import TradeOfferEvent
    if not CAPTURE_EARLY.exists():
        pytest.skip("capture not present")
    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    sess.self_color_id = 1
    sess.active_offer_ids.add("abc123")
    diff = {"tradeState": {
        "activeOffers": {"abc123": None},
        "closedOffers": {"abc123": {"offeredResources": [2]}},
    }}
    events_from_diff(sess, diff)
    assert "abc123" not in sess.active_offer_ids


def test_ws_game_over_emitted_when_player_hits_vp_target():
    """When a diff merges in victoryPointsState that pushes a player
    to >= the VP target, colonist_diff should emit GameOverEvent so
    the postmortem path fires even when the DOM-log "X won the game"
    line is missing (e.g. when the chat scraper is dark).
    """
    from catanbot.colonist_diff import LiveSession, events_from_frame_payload
    from catanbot.events import GameOverEvent
    from catanbot.config import set_vp_target

    set_vp_target(10)

    sess = LiveSession.from_game_start(_game_start_body(CAPTURE_EARLY))
    # Pick any opp seat already on the board and set under-target VP,
    # then bump them past target via the diff. Source-id keys: 0=settle,
    # 1=city, 4=LR-flag (2 VP).
    target_cid = next(cid for cid in sess.player_names
                      if cid != sess.self_color_id)
    target_name = sess.player_names[target_cid]
    sess.victory_points_state[target_cid] = {0: 4, 1: 1, 4: 0, 5: 0}

    frame = {
        "type": 91,
        "payload": {
            "diff": {
                "playerStates": {
                    str(target_cid): {
                        "victoryPointsState": {"0": 5, "1": 2, "4": 1},
                    },
                },
            },
        },
    }
    events = events_from_frame_payload(sess, frame)
    over_events = [e for e in events if isinstance(e, GameOverEvent)]
    assert len(over_events) == 1, f"expected 1 GameOverEvent, got {events}"
    assert over_events[0].winner == target_name
    # Re-firing on a later diff must not duplicate the event.
    events2 = events_from_frame_payload(sess, frame)
    over_events2 = [e for e in events2 if isinstance(e, GameOverEvent)]
    assert over_events2 == [], (
        f"GameOverEvent should fire only once per session: {events2}")
