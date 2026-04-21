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

from cataanbot.colonist_map import MapMapping, build_mapping
from cataanbot.events import BuildEvent, Event, RobberMoveEvent


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
    # eid → last-seen owner (0 = empty). Suppresses re-dispatch of roads
    # that haven't actually changed between snapshots.
    known_edges: dict[int, int] = field(default_factory=dict)

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

        names: dict[int, str] = {}
        for entry in body.get("playerUserStates", []) or []:
            if not isinstance(entry, dict):
                continue
            color = entry.get("selectedColor")
            user = entry.get("username")
            if color is None or not user:
                continue
            names[int(color)] = str(user)

        sess = cls(mapping=mapping, player_names=names)

        # Seed known_corners / known_edges from the starting map state so
        # our first diff after GameStart doesn't replay every existing
        # placement (the setup-phase corners and roads).
        for cid_str, c in map_state.get("tileCornerStates", {}).items():
            bt = int(c.get("buildingType") or 0)
            if bt:
                sess.known_corners[int(cid_str)] = bt
        for eid_str, e in map_state.get("tileEdgeStates", {}).items():
            owner = e.get("owner")
            if owner:
                sess.known_edges[int(eid_str)] = int(owner)

        return sess

    def player_for(self, color_id: int | None) -> str:
        if color_id is None:
            return ""
        return self.player_names.get(int(color_id), f"player{int(color_id)}")


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
        owner = c.get("owner")
        bt = c.get("buildingType")
        if owner is None or bt not in (1, 2):
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
                out.append(RobberMoveEvent(
                    player="",         # diff doesn't name the mover
                    tile_label="",
                    prob=None,
                    coord=coord,
                ))

    return out


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
    return events_from_diff(sess, diff)
