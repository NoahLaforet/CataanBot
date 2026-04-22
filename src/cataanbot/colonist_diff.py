"""Extract structured Events from colonist.io WebSocket diff frames.

Colonist ships game-state deltas as type=91 ``GameStateDiff`` frames that
carry only the fields that changed since the last frame. Shape::

    payload = {"type": 91, "payload": {"diff": {...}}, "sequence": ...}

The diffs we care about for board mirroring:

* ``diff.mapState.tileCornerStates.{cid} = {owner, buildingType}``
    – buildingType 1 = settlement, 2 = city. ``owner`` is a colonist
      player color id (1..6), which we resolve through
      ``playerUserStates`` into a username.
* ``diff.mapState.tileEdgeStates.{eid} = {owner, type}``
    – road placement. The pre-existing corner mapping hands us the two
      catanatron node ids that bound this edge.
* ``diff.mechanicRobberState = {locationTileIndex: tid}``
    – robber moved. ``tid`` is a colonist tile id that we resolve to a
      catanatron cube coord via ``MapMapping.tile_coord``.

Dice rolls, dev-card buys, and resource distributions also ride type=91
frames but aren't handled here — they land in the roll/produce/devbuy
paths once the DOM parser or a future WS-side parser emits them.

A ``LiveSession`` holds the ``MapMapping`` from GameStart plus the color
id → username table, so the extractor is a pure function that takes one
diff and returns a list of ``Event`` objects ready for ``apply_event``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cataanbot.colonist_map import (
    MapMapping, build_mapping, corner_tile_signature, tile_resource,
)
from cataanbot.events import (
    BuildEvent, DevCardBuyEvent, Event, HandSyncEvent, ProduceEvent,
    RobberMoveEvent, RollEvent, VPEvent,
)

# Resource type ints used inside `playerStates.{cid}.resourceCards.cards`.
# 0 is a placeholder opponents see in place of your real cards — if any
# slot is non-zero, the snapshot belongs to the self-player whose tab
# owns the WS session. Same mapping as the tile-type ints.
_CARD_RESOURCE = {
    1: "WOOD", 2: "BRICK", 3: "SHEEP", 4: "WHEAT", 5: "ORE",
}


# Weights applied to keys in colonist's victoryPointsState dicts so we
# can sum them into a display VP total. Key 0 = settlements (1 VP each),
# key 1 = cities (2 VPs each), key 2 = VP dev cards held (self only, 1
# each), key 4 = has-longest-road flag (2 VPs), key 5 = has-largest-army
# flag (2 VPs). Anything else we haven't seen defaults to 0 so an
# unexpected tracking key doesn't inflate the total.
_VP_WEIGHTS: dict[int, int] = {0: 1, 1: 2, 2: 1, 4: 2, 5: 2}


class LiveSessionError(RuntimeError):
    pass


@dataclass
class LiveSession:
    """Persistent state needed to translate colonist diffs to events."""
    mapping: MapMapping
    # colonist color id (1..6) → username as displayed in the log
    player_names: dict[int, str] = field(default_factory=dict)
    # cid → last-seen buildingType (0 = unbuilt, 1 = settlement, 2 = city).
    # Lets us distinguish a fresh settlement from a city upgrade on the
    # same corner, since the diff carries only the new state.
    known_corners: dict[int, int] = field(default_factory=dict)
    # cid → last-seen owner color id. Needed for per-player yield
    # computation on a roll, since the tracker's catanatron board has a
    # random resource layout that doesn't match colonist's.
    corner_owners: dict[int, int] = field(default_factory=dict)
    # eid → last-seen owner (0 = empty). Suppresses re-dispatch of roads
    # that haven't actually changed between snapshots.
    known_edges: dict[int, int] = field(default_factory=dict)
    # Colonist tile id of the robber's current location. None until a
    # mechanicRobberState diff lands.
    robber_tile_id: int | None = None
    # Colonist color id whose WS session we're observing. Identified the
    # first time we see a non-zero resourceCards entry: colonist ships
    # real resource type ints for the viewer and zero-fills everyone
    # else's cards. Used to gate hand-sync emission to the one player
    # whose snapshot is fully specified.
    self_color_id: int | None = None
    # cid → last-seen count of development cards held. Dev-card buys
    # (new int appended to the list) are detected by count growth; we
    # don't need to know the type, just that a purchase happened and the
    # hand should be debited 1 WHEAT + 1 SHEEP + 1 ORE.
    dev_card_counts: dict[int, int] = field(default_factory=dict)
    # cid → current resource-card count for every player. For the self-
    # player this is the authoritative total. For opponents it's ground
    # truth on hand SIZE even though the per-resource breakdown is
    # hidden (colonist zero-fills the cards array for privacy). Used by
    # the robber advisor to rank steal EV by victim hand size without
    # depending on catanatron's per-resource tracking, which drifts low
    # when unseen events (trades, steals, discards we miss) fire.
    hand_card_counts: dict[int, int] = field(default_factory=dict)
    # Last-seen value of `currentState.currentTurnPlayerColor`. Colonist
    # only ships this key in the diff that it *changes* in, so a roll
    # frame on the same player's turn arrives without it. Caching here
    # lets a roll fall back to the prior turn's color when the current
    # diff omits it.
    current_turn_color_id: int | None = None
    # Color id currently holding Longest Road / Largest Army per
    # colonist's authoritative mechanic state. ``mechanicLongestRoadState
    # .{cid}.hasLongestRoad`` flips to true when awarded and to false on
    # the previous holder when it transfers. Tracking it here lets us
    # emit a VPEvent on the transition so the tracker's HAS_ROAD /
    # HAS_ARMY flags stay synced with colonist's view — our own road /
    # knight tracking can lag when a diff is missed, and without this
    # handshake the VP bonus never lands on the HUD.
    has_longest_road_cid: int | None = None
    has_largest_army_cid: int | None = None
    # Colonist's authoritative VP breakdown per color. Keys are int
    # source-ids (0=settle count, 1=city count, 2=held VP cards for
    # self, 4=has-longest-road flag, 5=has-largest-army flag) and
    # values are counts. Summing with ``_VP_WEIGHTS`` yields the same
    # total colonist displays above each player's name — more robust
    # than recomputing from our internal building tracker, which can
    # drift across reconnects or dropped diffs.
    victory_points_state: dict[int, dict[int, int]] = field(
        default_factory=dict)

    @classmethod
    def from_game_start(cls, body: dict[str, Any]) -> "LiveSession":
        """Build a session from a GameStart (type=4) payload.

        ``body`` is the outer dict — the one with both ``gameState`` and
        ``playerUserStates``. We accept an already-unwrapped gameState
        dict too for convenience (the username table is then empty,
        and diff players resolve to ``playerN`` placeholders).
        """
        game_state = body.get("gameState") if "gameState" in body else body
        if not isinstance(game_state, dict):
            raise LiveSessionError("GameStart payload has no gameState")
        map_state = game_state.get("mapState")
        if not isinstance(map_state, dict):
            raise LiveSessionError("gameState has no mapState")
        mapping = build_mapping(map_state)

        # Self detection: GameStart ships ``playerColor`` at the top
        # level = the local seat's color — the most direct signal.
        # Fallback: only the local client's playerUserStates entry has
        # a real integer ``userId`` (bots have userId=null). Latching
        # before any resource frames land lets round-2 opening picks
        # (complement-aware ranking against my placed settlement) fire
        # as soon as the first settlement is down, instead of waiting
        # a full round for the 2nd-settle resource arrival to reveal
        # self.
        self_cid: int | None = None
        raw_self = body.get("playerColor")
        if isinstance(raw_self, int):
            self_cid = raw_self

        names: dict[int, str] = {}
        for entry in body.get("playerUserStates", []) or []:
            if not isinstance(entry, dict):
                continue
            color = entry.get("selectedColor")
            user = entry.get("username")
            if color is None or not user:
                continue
            names[int(color)] = str(user)
            # Fallback: infer self from the entry with a real userId.
            if (self_cid is None and entry.get("userId") is not None
                    and not entry.get("isBot")):
                self_cid = int(color)

        sess = cls(mapping=mapping, player_names=names)
        if self_cid is not None:
            sess.self_color_id = self_cid

        # Seed known_corners / known_edges from the starting map state so
        # our first diff after GameStart doesn't replay every existing
        # placement (the setup-phase corners and roads).
        for cid_str, c in map_state.get("tileCornerStates", {}).items():
            bt = int(c.get("buildingType") or 0)
            owner = c.get("owner")
            if bt:
                sess.known_corners[int(cid_str)] = bt
            if owner:
                sess.corner_owners[int(cid_str)] = int(owner)
        for eid_str, e in map_state.get("tileEdgeStates", {}).items():
            owner = e.get("owner")
            if owner:
                sess.known_edges[int(eid_str)] = int(owner)

        # Seed initial robber position if set (pre-game defaults to desert).
        robber = game_state.get("mechanicRobberState") or {}
        if isinstance(robber, dict) and "locationTileIndex" in robber:
            sess.robber_tile_id = int(robber["locationTileIndex"])

        # Seed per-color VP breakdown from colonist's playerStates. On
        # a mid-game reconnect this ships the full current VP state for
        # every player, so we're immediately in sync with what the UI
        # shows — no catch-up needed from rebuilding history.
        player_states = game_state.get("playerStates") or {}
        if isinstance(player_states, dict):
            for cid_str, pstate in player_states.items():
                if not isinstance(pstate, dict):
                    continue
                try:
                    cid = int(cid_str)
                except (TypeError, ValueError):
                    continue
                vps = pstate.get("victoryPointsState")
                if isinstance(vps, dict):
                    sess.victory_points_state[cid] = _parse_vp_state(vps)

        # Seed bonus-holder cids from the full game state so a mid-game
        # reconnect doesn't re-award the card on the next diff that
        # happens to re-ship `hasLongestRoad: true`.
        for attr, key in (("has_longest_road_cid", "mechanicLongestRoadState"),
                          ("has_largest_army_cid", "mechanicLargestArmyState")):
            mech = game_state.get(key) or {}
            if not isinstance(mech, dict):
                continue
            flag = ("hasLongestRoad" if attr == "has_longest_road_cid"
                    else "hasLargestArmy")
            for pid_str, pstate in mech.items():
                if isinstance(pstate, dict) and pstate.get(flag):
                    try:
                        setattr(sess, attr, int(pid_str))
                    except (TypeError, ValueError):
                        pass
                    break

        return sess

    def player_for(self, color_id: int | None) -> str:
        if color_id is None:
            return ""
        return self.player_names.get(int(color_id), f"player{int(color_id)}")

    def vp_total(self, color_id: int | None) -> int:
        """Weighted sum of colonist's victoryPointsState for a color.

        Returns 0 when we haven't seen a vp snapshot for this color yet
        (pre-first-diff, or a cid we don't recognize). Uses _VP_WEIGHTS
        to translate source-id counts into VP — so a state of
        ``{0: 2, 1: 1, 4: 1}`` means 2 settles + 1 city + longest road
        = 2*1 + 1*2 + 1*2 = 6 VPs, matching what colonist's UI shows.
        """
        if color_id is None:
            return 0
        state = self.victory_points_state.get(int(color_id))
        if not state:
            return 0
        return sum(_VP_WEIGHTS.get(k, 0) * v for k, v in state.items())


def _parse_vp_state(vps: dict[Any, Any]) -> dict[int, int]:
    """Convert colonist's string-keyed victoryPointsState to ints.

    Colonist ships keys as strings ('0', '1', '4', ...) in its msgpack
    payload — coerce them to ints so they match _VP_WEIGHTS keys.
    Silently drops entries with unparseable keys or values.
    """
    out: dict[int, int] = {}
    for k, v in vps.items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def events_from_diff(
    sess: LiveSession, diff: dict[str, Any],
) -> list[Event]:
    """Turn one type=91 diff body into structured Events.

    Returns an empty list if the diff carries nothing we translate. The
    session is mutated: known_corners / known_edges are updated so the
    next call reflects the post-diff state.
    """
    if not isinstance(diff, dict):
        return []
    out: list[Event] = []

    map_diff = diff.get("mapState") or {}
    corner_diff = map_diff.get("tileCornerStates") or {}
    edge_diff = map_diff.get("tileEdgeStates") or {}

    for cid_str, c in corner_diff.items():
        try:
            cid = int(cid_str)
        except (TypeError, ValueError):
            continue
        if not isinstance(c, dict):
            continue
        bt = c.get("buildingType")
        if bt not in (1, 2):
            continue
        # City upgrades ship as {"buildingType": 2} with no owner —
        # colonist only includes owner in the diff when it actually
        # changed. Fall back to the cached owner so the upgrade isn't
        # dropped; otherwise the tracker stays on SETTLEMENT at that
        # node and the recommender keeps suggesting "build city" on a
        # corner that's already a city.
        owner = c.get("owner")
        if owner is None:
            owner = sess.corner_owners.get(cid)
        if owner is None:
            continue
        node_id = sess.mapping.node_id.get(cid)
        if node_id is None:
            continue
        prev = sess.known_corners.get(cid, 0)
        if prev == bt:
            continue
        piece = "city" if bt == 2 else "settlement"
        out.append(BuildEvent(
            player=sess.player_for(int(owner)),
            piece=piece,
            node_id=node_id,
        ))
        sess.known_corners[cid] = int(bt)
        sess.corner_owners[cid] = int(owner)
        # A player's *2nd* settlement places and immediately yields the
        # three adjacent tile resources — colonist never ships this as a
        # dice-roll payout, and without it opponent hands stay blank
        # until their first real roll fires. Detect it by counting the
        # owner's settlements *after* this update: exactly 2 means this
        # diff is the 2nd-settlement placement. The WS playerStates diff
        # also ships a resourceCards update in the same frame for the
        # self-player, so HandSyncEvent (emitted further down) will
        # overwrite any double-count on our own hand.
        if piece == "settlement":
            owner_settlements = sum(
                1 for cid2, own in sess.corner_owners.items()
                if own == int(owner)
                and sess.known_corners.get(cid2) == 1)
            if owner_settlements == 2:
                bag = _starting_resources_for_corner(sess.mapping, cid)
                if bag:
                    out.append(ProduceEvent(
                        player=sess.player_for(int(owner)),
                        resources=bag,
                    ))

    for eid_str, e in edge_diff.items():
        try:
            eid = int(eid_str)
        except (TypeError, ValueError):
            continue
        if not isinstance(e, dict):
            continue
        owner = e.get("owner")
        if not owner:
            continue
        if sess.known_edges.get(eid) == int(owner):
            continue
        pair = sess.mapping.edge_nodes.get(eid)
        if pair is None:
            continue
        a, b = sorted(pair)
        out.append(BuildEvent(
            player=sess.player_for(int(owner)),
            piece="road",
            edge_nodes=(a, b),
        ))
        sess.known_edges[eid] = int(owner)

    robber = diff.get("mechanicRobberState")
    if isinstance(robber, dict) and "locationTileIndex" in robber:
        try:
            tid = int(robber["locationTileIndex"])
        except (TypeError, ValueError):
            tid = None
        if tid is not None:
            coord = sess.mapping.tile_coord.get(tid)
            if coord is not None:
                sess.robber_tile_id = tid
                out.append(RobberMoveEvent(
                    player="",         # diff doesn't name the mover
                    tile_label="",
                    prob=None,
                    coord=coord,
                ))

    for ev in _dev_card_buy_events(
            sess, diff.get("mechanicDevelopmentCardsState") or {}):
        out.append(ev)

    for ev in _hand_sync_events(sess, diff.get("playerStates") or {}):
        out.append(ev)

    _merge_vp_state(sess, diff.get("playerStates") or {})

    for ev in _bonus_vp_events(sess, diff):
        out.append(ev)

    # Latch currentTurnPlayerColor any time the diff ships it, so a later
    # roll frame on the same player's turn can still be attributed.
    cs = diff.get("currentState") or {}
    if isinstance(cs, dict) and cs.get("currentTurnPlayerColor") is not None:
        try:
            sess.current_turn_color_id = int(cs["currentTurnPlayerColor"])
        except (TypeError, ValueError):
            pass

    dice = diff.get("diceState") or {}
    # A fresh roll always carries both dice1 and dice2 in the diff. A
    # "diceThrown: False" frame on its own only signals the roll has
    # been consumed — no new roll, no new event.
    if isinstance(dice, dict) and "dice1" in dice and "dice2" in dice:
        # Prefer the value that just landed in this diff (most precise);
        # fall back to the session-cached turn color for roll frames
        # that don't re-ship it.
        roller_color = cs.get("currentTurnPlayerColor")
        cid = (int(roller_color) if roller_color is not None
               else sess.current_turn_color_id)
        player = sess.player_for(cid)
        out.append(RollEvent(player=player, d1=int(dice["dice1"]),
                             d2=int(dice["dice2"])))

    return out


def _merge_vp_state(
    sess: LiveSession, player_states: dict[str, Any],
) -> None:
    """Update the session's per-color victoryPointsState from a diff.

    Colonist only ships the *changed* entries in a diff — e.g. a
    settlement build sends ``{'0': 2}`` to overwrite the old settlement
    count. Merge these onto the running state per color so the full
    breakdown stays current and ``vp_total`` reflects what the UI shows.
    """
    if not isinstance(player_states, dict):
        return
    for cid_str, pstate in player_states.items():
        if not isinstance(pstate, dict):
            continue
        vps = pstate.get("victoryPointsState")
        if not isinstance(vps, dict):
            continue
        try:
            cid = int(cid_str)
        except (TypeError, ValueError):
            continue
        current = sess.victory_points_state.setdefault(cid, {})
        current.update(_parse_vp_state(vps))


def _dev_card_buy_events(
    sess: LiveSession, dev_state: dict[str, Any],
) -> list[DevCardBuyEvent]:
    """Detect dev-card purchases by watching each player's card-list length.

    Colonist ships every player's full `developmentCards.cards` list
    when any one of them changes. The list grows when a card is bought
    (real type for the self-player, placeholder int 10 for opponents)
    and shrinks when a card is played. We only care about growth here —
    plays come through `gameLogState` with a known type, which the DOM
    parser already classifies.

    Emits one DevCardBuyEvent per player whose card count increased
    compared to our tracked state. Self-player buys are suppressed: the
    resource debit is already covered by the HandSyncEvent that follows
    in the same diff.
    """
    out: list[DevCardBuyEvent] = []
    players = dev_state.get("players")
    if not isinstance(players, dict):
        return out
    for cid_str, pstate in players.items():
        if not isinstance(pstate, dict):
            continue
        dev = pstate.get("developmentCards")
        if not isinstance(dev, dict):
            continue
        cards = dev.get("cards")
        if not isinstance(cards, list):
            continue
        try:
            cid = int(cid_str)
        except (TypeError, ValueError):
            continue
        prev = sess.dev_card_counts.get(cid, 0)
        new_count = len(cards)
        sess.dev_card_counts[cid] = new_count
        if new_count > prev and cid != sess.self_color_id:
            for _ in range(new_count - prev):
                out.append(DevCardBuyEvent(player=sess.player_for(cid)))
    return out


def _hand_sync_events(
    sess: LiveSession, player_states: dict[str, Any],
) -> list[HandSyncEvent]:
    """Emit HandSyncEvents for each player whose resource cards appear
    in this diff with real resource type ints.

    Colonist ships the viewer's cards as real resource ints (1..5) and
    zero-fills everyone else's. We latch onto the first colorId that
    reveals non-zero ints and treat subsequent snapshots from that id
    as authoritative hand state. Opponent zero-fill entries are skipped
    here — those are count-only signals handled by the opponent hand
    inference pass.
    """
    out: list[HandSyncEvent] = []
    for cid_str, pstate in player_states.items():
        if not isinstance(pstate, dict):
            continue
        rc = pstate.get("resourceCards")
        if not isinstance(rc, dict):
            continue
        cards = rc.get("cards")
        if not isinstance(cards, list):
            continue
        try:
            cid = int(cid_str)
        except (TypeError, ValueError):
            continue
        # Hand size is authoritative for everyone — latch it regardless
        # of whether we can resolve the per-resource breakdown.
        sess.hand_card_counts[cid] = sum(
            1 for c in cards if isinstance(c, int))
        has_real = any(int(c) != 0 for c in cards if isinstance(c, int))
        if has_real and sess.self_color_id is None:
            sess.self_color_id = cid
        if cid != sess.self_color_id:
            continue
        bag: dict[str, int] = {}
        for c in cards:
            if not isinstance(c, int):
                continue
            resource = _CARD_RESOURCE.get(c)
            if resource is None:
                continue
            bag[resource] = bag.get(resource, 0) + 1
        out.append(HandSyncEvent(
            player=sess.player_for(cid),
            resources=bag,
        ))
    return out


def _bonus_vp_events(
    sess: LiveSession, diff: dict[str, Any],
) -> list[VPEvent]:
    """Emit VPEvents for Longest Road / Largest Army transitions.

    Colonist ships the authoritative holder on each build / knight play
    as ``mechanic{LongestRoad,LargestArmy}State.{cid}.has{LongestRoad,
    LargestArmy}: true``. Flipping this to ``false`` on the previous
    holder is what tells the client to re-paint the VP counter. Our
    local tracker otherwise has to infer the award from a road-length
    or knight-count recompute, and that inference breaks if any of
    those events go missing (road diff drops, knight play isn't seen
    on the DOM log). Emitting a VPEvent on the diff transition lets
    the tracker's existing ``_apply_vp`` path sync HAS_ROAD / HAS_ARMY
    — so the VP counter gets the bonus even when our own count lags.

    Emits zero-to-one event per mechanic per diff: only on the frame
    that actually changes the holder.
    """
    out: list[VPEvent] = []
    for mech_key, flag_key, attr, reason in (
        ("mechanicLongestRoadState", "hasLongestRoad",
         "has_longest_road_cid", "longest_road"),
        ("mechanicLargestArmyState", "hasLargestArmy",
         "has_largest_army_cid", "largest_army"),
    ):
        mech = diff.get(mech_key) or {}
        if not isinstance(mech, dict):
            continue
        new_holder_cid: int | None = None
        for pid_str, pstate in mech.items():
            if not isinstance(pstate, dict):
                continue
            if flag_key in pstate and bool(pstate[flag_key]):
                try:
                    new_holder_cid = int(pid_str)
                except (TypeError, ValueError):
                    continue
                break
        if new_holder_cid is None:
            continue
        prev_cid = getattr(sess, attr)
        if prev_cid == new_holder_cid:
            continue
        out.append(VPEvent(
            player=sess.player_for(new_holder_cid),
            reason=reason,
            vp_delta=2,
            previous_holder=(sess.player_for(prev_cid)
                             if prev_cid is not None else None),
        ))
        setattr(sess, attr, new_holder_cid)
    return out


def _starting_resources_for_corner(
    mapping: MapMapping, cid: int,
) -> dict[str, int]:
    """Return the per-resource yield a 2nd settlement gets from its
    three adjacent tiles. Skips desert (non-producing) tiles."""
    bag: dict[str, int] = {}
    for tid, corners in mapping.tile_corners.items():
        if cid not in corners:
            continue
        res = tile_resource(mapping.tile_types.get(tid, 0))
        if res is None:
            continue
        bag[res] = bag.get(res, 0) + 1
    return bag


def produce_events_for_roll(
    sess: LiveSession, dice_total: int,
) -> list[ProduceEvent]:
    """Compute per-player yields for a dice total using colonist's
    actual resource layout and the session's tracked corner ownership.

    Emits one ``ProduceEvent`` per player with a non-empty yield. The
    tile under the robber is skipped (zero yield), matching real play.
    Call separately from ``events_from_diff`` — the diff emits the
    ``RollEvent`` (informational for the tracker) and this fills in the
    distribution catanatron would otherwise compute off the wrong map.
    """
    if dice_total == 7:
        return []
    per_player: dict[str, dict[str, int]] = {}
    for tid, dice in sess.mapping.tile_dice.items():
        if dice != dice_total:
            continue
        if tid == sess.robber_tile_id:
            continue
        res = tile_resource(sess.mapping.tile_types.get(tid, 0))
        if res is None:
            continue
        for cid in sess.mapping.tile_corners.get(tid, ()):
            owner = sess.corner_owners.get(cid)
            if owner is None:
                continue
            # Skip the self-player. Their post-roll hand is covered by
            # the HandSyncEvent we emit from playerStates.resourceCards,
            # which is an ABSOLUTE snapshot of their post-roll cards.
            # Adding this delta on top would double-count the yield.
            if (sess.self_color_id is not None
                    and int(owner) == sess.self_color_id):
                continue
            bt = sess.known_corners.get(cid, 0)
            if bt not in (1, 2):
                continue
            amount = 2 if bt == 2 else 1
            name = sess.player_for(int(owner))
            bag = per_player.setdefault(name, {})
            bag[res] = bag.get(res, 0) + amount
    return [ProduceEvent(player=p, resources=bag)
            for p, bag in per_player.items() if bag]


def events_from_frame_payload(
    sess: LiveSession, payload: dict[str, Any],
) -> list[Event]:
    """Convenience wrapper: pull the diff out of a decoded type=91 frame.

    Accepts the full ``frame.payload`` dict (``{"type": 91, "payload":
    {"diff": ...}, "sequence": ...}``). Returns an empty list if the
    frame isn't a diff or the diff is empty.
    """
    if not isinstance(payload, dict):
        return []
    if payload.get("type") != 91:
        return []
    body = payload.get("payload") or {}
    diff = body.get("diff") if isinstance(body, dict) else None
    if not isinstance(diff, dict):
        return []
    events = events_from_diff(sess, diff)
    # A RollEvent emitted by events_from_diff signals we're on the roll
    # frame itself; append the derived per-player ProduceEvents so the
    # whole distribution lands in one dispatch batch.
    for ev in list(events):
        if isinstance(ev, RollEvent):
            events.extend(produce_events_for_roll(sess, ev.total))
            break
    return events
