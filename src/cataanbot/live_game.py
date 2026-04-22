"""A live colonist.io game wired through the full CataanBot pipeline.

Ties together the three moving parts we've built piecemeal:

* ``LiveSession`` — colonist map topology + player-name table +
  per-corner/edge/robber state from the WS diff stream.
* ``Tracker`` — catanatron-backed board mirror that advisors read from.
* ``ColorMap`` — colonist-username ↔ catanatron-color bridge.

Driven by one method: ``feed(payload)``. Given a raw type=4 GameStart
payload, we initialize session + map + tracker + color map. On every
subsequent type=91 diff we pull Events via
``events_from_frame_payload`` and dispatch them through ``apply_event``.

This is the surface both the live WS bridge and the ws-replay CLI hook
into, so the in-process behavior of "watch a real game" and "audit a
capture file" stay byte-for-byte identical.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cataanbot.colonist_diff import (
    LiveSession, LiveSessionError, events_from_frame_payload,
)
from cataanbot.colonist_map import build_catanatron_map_from_colonist
from cataanbot.live import ColorMap, DispatchResult, apply_event
from cataanbot.tracker import Tracker


@dataclass
class LiveGame:
    """Container for one in-progress colonist game.

    Construction is deferred: ``LiveGame()`` yields an un-started game
    until ``feed`` sees a GameStart frame. Until then, feeding diffs is a
    no-op so replay scripts can push the whole capture through without
    having to seek to GameStart manually.
    """
    session: LiveSession | None = None
    tracker: Tracker | None = None
    color_map: ColorMap | None = None

    @property
    def started(self) -> bool:
        return self.session is not None and self.tracker is not None

    def start_from_game_state(self, body: dict[str, Any]) -> None:
        """Boot session, CatanMap, Tracker, and ColorMap from a GameStart body.

        ``body`` is the outer dict (what lives at ``frame.payload["payload"]``
        for a type=4 frame) — same shape ``LiveSession.from_game_start``
        expects. Colors are auto-assigned from the colonist color-id
        order so catanatron seats match what the live game shows.
        """
        self.session = LiveSession.from_game_start(body)
        game_state = body.get("gameState") if "gameState" in body else body
        map_state = game_state.get("mapState")
        if not isinstance(map_state, dict):
            raise LiveSessionError(
                "GameStart payload has no mapState for CatanMap")
        catan_map = build_catanatron_map_from_colonist(
            map_state, mapping=self.session.mapping)
        self.tracker = Tracker(catan_map=catan_map)
        # Seed the color map in the colonist color-id order (1..6) so
        # catanatron's seat order matches what colonist shows.
        self.color_map = ColorMap()
        for color_id in sorted(self.session.player_names):
            username = self.session.player_names[color_id]
            self.color_map.get(username)

    def feed(self, payload: dict[str, Any]) -> list[DispatchResult]:
        """Push one WS frame payload into the game. Returns dispatch results.

        * type=4 (GameStart): boots the session if we hadn't yet; replays
          after the first GameStart are ignored (a single capture file
          only contains one game).
        * type=91 (GameStateDiff): extracts Events and dispatches each to
          the Tracker, returning a ``DispatchResult`` per event.
        * Anything else: returns an empty list.
        """
        if not isinstance(payload, dict):
            return []
        ptype = payload.get("type")
        body = payload.get("payload") or {}
        if ptype == 4 and not self.started:
            self.start_from_game_state(body)
            return []
        if ptype != 91 or not self.started:
            return []

        events = events_from_frame_payload(self.session, payload)
        return [apply_event(self.tracker, self.color_map, ev) for ev in events]
