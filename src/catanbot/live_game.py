"""A live colonist.io game wired through the full CatanBot pipeline.

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

from dataclasses import dataclass, field
from typing import Any

from catanbot.colonist_diff import (
    _DEV_CARD_TYPE, LiveSession, LiveSessionError, events_from_frame_payload,
)
from catanbot.colonist_map import build_catanatron_map_from_colonist
from catanbot.events import (
    BankSyncEvent, BuildEvent, HandSyncEvent, ProduceEvent,
)
from catanbot.live import ColorMap, DispatchResult, apply_event
from catanbot.opp_inference import OppHandModel
from catanbot.tracker import Tracker, TrackerError

# Standard Catan build costs. WS diffs don't carry the resource deltas
# that accompany a build (only the board state changed), so LiveGame
# debits the cost itself when a placement succeeds.
_SETTLEMENT_COST = {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1}
_CITY_COST = {"WHEAT": 2, "ORE": 3}
_ROAD_COST = {"WOOD": 1, "BRICK": 1}


def _apply_game_settings(body: dict[str, Any]) -> None:
    """Push the game's variant settings (VP target, discard limit) into
    the live config so heuristics scale to non-standard games.

    Colonist's GameStart frame ships these in ``gameSettings``:

        gameSettings.victoryPointsToWin   — int (default 10)
        gameSettings.cardDiscardLimit     — int (default 7)

    Old captures from before the auto-detect work may be missing
    ``gameSettings`` entirely or carry only a partial dict — silently
    skip whatever is absent so ws-replay still chews through old logs.
    Any value already set via the userscript drawer gets overwritten,
    which is the right semantic: the colonist game is the authority.
    """
    gs = body.get("gameSettings")
    if not isinstance(gs, dict):
        return
    from catanbot import config
    vp = gs.get("victoryPointsToWin")
    if isinstance(vp, int) and vp >= 1:
        try:
            config.set_vp_target(vp)
        except (TypeError, ValueError):
            pass
    discard = gs.get("cardDiscardLimit")
    if isinstance(discard, int) and discard >= 1:
        try:
            config.set_discard_limit(discard)
        except (TypeError, ValueError):
            pass


def _gamestart_player_set_changed(session, body: dict[str, Any]) -> bool:
    """True if the new GameStart body has a different player roster
    than the current session.

    Colonist's "quit and start a new match" path doesn't always emit
    a GameOver frame before the next GameStart. Without that, the
    session's `game_over_emitted` flag stays False and the GameStart
    falls into the resync-into-existing-tracker path, which keeps
    the OLD game's player_names + color_map alive. Downstream the
    chat-side log scraper sends fresh usernames (Calan/Hamlet/Kara)
    while catanatron RollEvents still emit stale ones (Vlad/Budd/
    Wiburg) — display_colors lookup keys never match opps[i].username
    and pills render with catanatron-enum fallback hex (Wiburg→WHITE
    → off-white pill for what should be colonist orange).

    Compare the player_username sets — any difference means a new
    match and a full reboot is the right call.
    """
    game_state = body.get("gameState") if "gameState" in body else body
    if not isinstance(game_state, dict):
        return False
    player_states = game_state.get("playerStates")
    if not isinstance(player_states, dict):
        return False
    new_names = set()
    for entry in player_states.values():
        if not isinstance(entry, dict):
            continue
        username = entry.get("username")
        if isinstance(username, str) and username:
            new_names.add(username)
    if not new_names:
        return False
    cur_names = set((getattr(session, "player_names", None) or {}).values())
    return bool(cur_names) and new_names != cur_names


def _gamestart_shape_changed(session, body: dict[str, Any]) -> bool:
    """True if the new GameStart body's mapState shape differs from the
    one the session was booted with.

    Colonist sends two GameStart frames in a row on weekly maps like
    Twirl: a 19/54/72/9 placeholder followed by the real variant shape
    (Twirl: 42/126/168/12). Treating the second frame as a reconnect
    leaves the placeholder mapping in place and every diff with a
    corner/edge id past the placeholder range vanishes silently.
    Compare the four counts; if any moved, the bridge needs to rebuild
    from scratch.
    """
    game_state = body.get("gameState") if "gameState" in body else body
    if not isinstance(game_state, dict):
        return False
    new_ms = game_state.get("mapState")
    if not isinstance(new_ms, dict):
        return False
    new_counts = (
        len(new_ms.get("tileHexStates") or {}),
        len(new_ms.get("tileCornerStates") or {}),
        len(new_ms.get("tileEdgeStates") or {}),
        len(new_ms.get("portEdgeStates") or {}),
    )
    mapping = getattr(session, "mapping", None)
    if mapping is None:
        return False
    cur_counts = (
        len(mapping.tile_coord),
        len(mapping.node_id),
        len(mapping.edge_nodes),
        len(mapping.port_edges),
    )
    return new_counts != cur_counts


def _gamestart_board_regressed(tracker, body: dict[str, Any]) -> bool:
    """True if the incoming GameStart shows FEWER placed buildings than we
    currently track — a fresh game (or quit-and-restart) rather than a
    forward reconnect.

    A genuine reconnect replays the *current* authoritative gameState, so
    its placed-building count is >= what we already have. A brand-new game
    on the same board shape + same players (no GameOver seen, e.g. a quick
    rematch) ships an emptier board. Without rebooting, the prior game's
    settlements linger in catanatron's board and trip the distance rule
    when the new game places a settlement on a now-"adjacent-to-occupied"
    node — the stacked-GameStart drift that dropped builds on variant
    maps (Volcano capture 2026-05-23: 7 settlements / 14 roads lost).
    """
    if tracker is None:
        return False
    game_state = body.get("gameState") if "gameState" in body else body
    if not isinstance(game_state, dict):
        return False
    ms = game_state.get("mapState")
    if not isinstance(ms, dict):
        return False
    corners = ms.get("tileCornerStates") or {}
    incoming = sum(
        1 for c in corners.values()
        if isinstance(c, dict) and int(c.get("buildingType") or 0) > 0)
    try:
        current = len(tracker.game.state.board.buildings)
    except Exception:  # noqa: BLE001
        return False
    return incoming < current


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
    # Per-color tally of applied {settlement,city,road} placements. First
    # 2 settlements and 2 roads each are free (setup phase); everything
    # else is a paid build and gets cost-debited in ``_debit_build``.
    build_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    # Probabilistic opponent-hand model. Booted alongside the tracker on
    # GameStart; fed the DOM game-log narrative from the /log handler and
    # anchored to the WS self-hand + bank + authoritative card counts.
    opp_model: OppHandModel | None = None

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
        # catanatron only has 4 player colors. A 5-6 player colonist
        # lobby (big Twirl / Volcano lobbies seat up to 6) has no color
        # for the 5th/6th seat, so seeding the color map below would
        # raise ColorMapError partway through and leave a half-booted,
        # corrupt game: started would read True (session + tracker both
        # set) over a color_map missing seats, and every later frame
        # naming an un-seated player would re-raise. Stop before seating
        # catanatron. Keep the session so the snapshot can surface
        # "limited tracking" (players_unsupported), but leave
        # tracker/color_map unset so started stays False and the feed
        # loop skips diffs cleanly instead of crashing on every frame.
        if self.session.too_many_players():
            self.tracker = None
            self.color_map = None
            return
        _apply_game_settings(body)
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
        # Boot the probabilistic opponent-hand model with the seated
        # colors and which one is us, so it knows whose hand is exact.
        self_color = None
        self_cid = self.session.self_color_id
        if self_cid is not None:
            self_name = self.session.player_names.get(self_cid)
            if self_name and self.color_map.has(self_name):
                self_color = self.color_map.get(self_name)
        self.opp_model = OppHandModel(
            list(self.color_map.as_dict().values()), self_color=self_color)
        self._replay_pre_existing_buildings()

    def _replay_pre_existing_buildings(self) -> None:
        """Apply any buildings/roads carried in the GameStart mapState.

        A reconnect mid-game ships the full current mapState — every
        settlement, city, and road that's already on the board. Without
        replaying them the tracker starts empty, catanatron's building
        table stays empty, and downstream consumers (setup-phase gate,
        longest-road/largest-army recompute, distance-2 checks in the
        recommender) all see a false "nothing has been built" world.
        This resyncs the tracker so a mid-game reconnect picks up where
        the live session left off.

        Fresh games emit the setup-phase builds as real BuildEvents via
        diffs, so in a non-reconnect start this is a no-op —
        known_corners / known_edges start empty on a real GameStart.
        """
        sess = self.session
        for cid, bt in list(sess.known_corners.items()):
            if bt not in (1, 2):
                continue
            owner_cid = sess.corner_owners.get(cid)
            if owner_cid is None:
                continue
            node_id = sess.mapping.node_id.get(cid)
            if node_id is None:
                continue
            piece = "city" if bt == 2 else "settlement"
            ev = BuildEvent(
                player=sess.player_for(owner_cid),
                piece=piece,
                node_id=node_id,
            )
            try:
                result = apply_event(self.tracker, self.color_map, ev)
            except Exception:  # noqa: BLE001
                # Bad seed (corner already taken, off-board, etc.) —
                # skip rather than crash the entire boot.
                continue
            if result.status == "applied":
                color = self.color_map.get(ev.player)
                tally = self.build_counts.setdefault(
                    color, {"settlement": 0, "city": 0, "road": 0})
                tally[piece] += 1
        # Roads have to connect to an existing settlement or another road
        # of the same color — catanatron rejects "floating" placements
        # with ``Invalid Road Placement``. When we replay the full
        # snapshot in one pass, a road whose only connector is another
        # replayed road can fail if that connector hasn't been placed
        # yet. Retry until we stop making progress; any still-failing
        # roads are genuinely disconnected (which shouldn't happen on a
        # well-formed colonist snapshot, but we swallow rather than
        # crash the feed).
        pending: list[tuple[int, int, BuildEvent]] = []
        for eid, owner_cid in sess.known_edges.items():
            if not owner_cid:
                continue
            pair = sess.mapping.edge_nodes.get(eid)
            if pair is None:
                continue
            a, b = sorted(pair)
            ev = BuildEvent(
                player=sess.player_for(int(owner_cid)),
                piece="road",
                edge_nodes=(a, b),
            )
            pending.append((a, b, ev))
        while pending:
            next_pending: list[tuple[int, int, BuildEvent]] = []
            applied_any = False
            for a, b, ev in pending:
                try:
                    result = apply_event(self.tracker, self.color_map, ev)
                except Exception:  # noqa: BLE001
                    # Defer same as a non-applied result; if every
                    # remaining road raises we'll exit via the no-
                    # progress branch below rather than crashing.
                    next_pending.append((a, b, ev))
                    continue
                if result.status == "applied":
                    applied_any = True
                    color = self.color_map.get(ev.player)
                    tally = self.build_counts.setdefault(
                        color, {"settlement": 0, "city": 0, "road": 0})
                    tally["road"] += 1
                else:
                    next_pending.append((a, b, ev))
            if not applied_any:
                break
            pending = next_pending

    def feed(self, payload: dict[str, Any]) -> list[DispatchResult]:
        """Push one WS frame payload into the game. Returns dispatch results.

        * type=4 (GameStart): boots the session if we hadn't yet; if the
          session is already booted, this is a reconnect — colonist ships
          the full gameState again to bring the new WS subscriber up to
          speed. We re-sync the self-hand from the replay's playerStates
          (board state is preserved) so the tracker recovers from drift
          accumulated during the dead connection.
        * type=91 (GameStateDiff): extracts Events and dispatches each to
          the Tracker, returning a ``DispatchResult`` per event.
        * Anything else: returns an empty list.
        """
        if not isinstance(payload, dict):
            return []
        ptype = payload.get("type")
        body = payload.get("payload") or {}
        if ptype == 4:
            # Type=4 frames sometimes ship without a usable gameState
            # (auth handshakes, reconnect acks, partial server frames).
            # Treat those as no-ops instead of letting LiveSessionError
            # bubble out of feed() — a thrown exception here gets caught
            # at the bridge's /ws handler and printed as
            # "[ws #N] decode error: GameStart payload has no gameState",
            # which leaves the bot in a half-booted state and blocks the
            # real GameStart that follows. Silent skip on malformed
            # type=4 frames is the right move; the live game keeps
            # waiting for a proper boot frame.
            usable = (isinstance(body, dict)
                      and isinstance(body.get("gameState") or body,
                                     dict)
                      and isinstance(
                          (body.get("gameState") or body).get("mapState"),
                          dict))
            if not usable:
                return []
            try:
                # New-game detection: if we already saw end-of-game in
                # this session (game_over_emitted toggled by the WS-side
                # GameOver detector) and a GameStart frame arrives, this
                # is a fresh match — boot from scratch instead of
                # resyncing into the prior game's tracker. Without this,
                # rolls/VPs/buildings accumulated across games (Noah's
                # 2026-05-02 case where the new game showed VP=13 from
                # the prior win).
                rebooted = False
                if (self.started and self.session is not None
                        and getattr(self.session,
                                    "game_over_emitted", False)):
                    # Force a fresh boot — clear session + tracker so
                    # `started` flips False, then start_from_game_state
                    # rebuilds everything for the new match.
                    self.session = None
                    self.tracker = None
                    rebooted = True
                # Shape-mismatch reboot: weekly maps like Twirl ship two
                # GameStart frames in sequence — first a 19/54/72/9
                # placeholder, then the real variant shape (Twirl is
                # 42/126/168/12). Without rebuilding, the placeholder
                # mapping stays and every diff with a corner/edge id
                # past the placeholder range silently drops, so
                # settlements/roads vanish and the next valid build
                # fails with "Invalid Road Placement".
                if (self.started and self.session is not None
                        and _gamestart_shape_changed(self.session, body)):
                    self.session = None
                    self.tracker = None
                    rebooted = True
                # Player-set reboot: colonist's quit-and-start-new path
                # doesn't emit GameOver, so we never see
                # game_over_emitted flip. Without it, a fresh GameStart
                # with different players falls into _resync_from_replay
                # and keeps the old color_map alive. That mismatch is
                # what makes display_colors lookup miss in streamer
                # mode (chat names new, opps[i].username old).
                if (self.started and self.session is not None
                        and _gamestart_player_set_changed(
                            self.session, body)):
                    self.session = None
                    self.tracker = None
                    rebooted = True
                # Board-regression reboot: a same-shape, same-players
                # rematch (no GameOver seen) ships an emptier board than
                # we track. Resyncing only hands would leave the prior
                # game's settlements on the board and trip the distance
                # rule on the new game's nearby placements. Reboot when
                # the incoming GameStart has fewer buildings than we hold.
                if (self.started and self.session is not None
                        and _gamestart_board_regressed(self.tracker, body)):
                    self.session = None
                    self.tracker = None
                    rebooted = True
                if not self.started:
                    self.start_from_game_state(body)
                    if rebooted:
                        # Surface to callers (bridge) so they can clear
                        # their own overlay state (rolls, histogram,
                        # robber targets, etc.).
                        self._just_rebooted = True
                else:
                    self._resync_from_replay(body)
            except LiveSessionError:
                # The detailed pre-check above should have caught this,
                # but belt-and-suspenders: never let LiveSessionError
                # bubble out of feed().
                return []
            return []
        if ptype != 91 or not self.started:
            return []

        events = events_from_frame_payload(self.session, payload)
        # Apply each event INDEPENDENTLY. A single failing event (e.g.
        # tracker.road raising TrackerError on an invalid placement)
        # used to nuke the whole frame via the list-comp short-circuit,
        # silently dropping any later events in the same diff
        # (settlement + robber + hand sync could all vanish behind one
        # bad road). Catching per-event keeps the rest of the frame
        # alive and surfaces the failure as an "error" DispatchResult.
        results: list[DispatchResult] = []
        for ev in events:
            try:
                results.append(
                    apply_event(self.tracker, self.color_map, ev))
            except Exception as e:  # noqa: BLE001
                results.append(DispatchResult(
                    event=ev, status="error",
                    message=f"apply_event raised: {e!r}"))
        for result in results:
            if (result.status == "applied"
                    and isinstance(result.event, BuildEvent)):
                self._debit_build(result.event)
        # Anchor the opponent-hand model with the authoritative WS data:
        # our own exact hand, the resource bank, and roll production.
        # Production is taken from the WS stream (not the DOM log) so it
        # lands in the same frame as the per-player card-count bump it
        # causes - the DOM "got" line can lag or drop, which would leave
        # the inferred floor short and force a reseed. The DOM /log handler
        # feeds the rest of the public narrative (build/steal/trade/...).
        if self.opp_model is not None:
            for ev in events:
                if isinstance(ev, HandSyncEvent):
                    color = self._color_for_username(ev.player)
                    if color is not None:
                        self.opp_model.set_self_hand(color, ev.resources)
                elif isinstance(ev, BankSyncEvent):
                    self.opp_model.set_bank(ev.resources)
                elif isinstance(ev, ProduceEvent):
                    self.opp_model.apply(ev, self.color_map)
        # Snap played-knight counts to colonist's authoritative
        # mechanicKnightState.knightsPlayed. A self knight play fires a
        # DevCardPlayEvent from BOTH the DOM-log parser and the WS
        # developmentCardsUsed path, so catanatron's PLAYED_KNIGHT
        # double-counts; overwrite it after the whole frame is applied.
        # HAS_ARMY is synced authoritatively elsewhere, so this only
        # corrects the count (which drives the display + recommender sim).
        try:
            self._sync_knight_counts(
                (payload.get("payload") or {}).get("diff") or {})
        except Exception:  # noqa: BLE001
            pass
        return results

    def _sync_knight_counts(self, diff: dict[str, Any]) -> None:
        """Snap SELF's PLAYED_KNIGHT to colonist's authoritative play
        history so a single self knight play does not double-count.

        A self knight play fires a DevCardPlayEvent from BOTH the DOM-log
        parser and the WS ``developmentCardsUsed`` diff, so catanatron's
        ``P{idx}_PLAYED_KNIGHT`` is bumped twice per real play and the
        count runs away (12, 14, 16...). Colonist ships the viewer's full
        typed play history in ``developmentCardsUsed``, which the diff
        decoder latches onto ``session.self_dev_used``; the number of
        KNIGHT entries there is the true count. Overwriting with it every
        frame neutralizes the double-count regardless of which event
        sources fired. Opponents are single-sourced (DOM-log only, no
        ``developmentCardsUsed`` shipped for them) so they are untouched.
        """
        if self.tracker is None or self.session is None:
            return
        sess = self.session
        if sess.self_color_id is None or self.color_map is None:
            return
        self_user = sess.player_names.get(sess.self_color_id)
        if not self_user or not self.color_map.has(self_user):
            return
        used = getattr(sess, "self_dev_used", None)
        if not isinstance(used, list):
            return
        knight_int = next(
            (i for i, name in _DEV_CARD_TYPE.items() if name == "KNIGHT"), 11)
        count = sum(1 for x in used if x == knight_int)
        try:
            self.tracker.set_played_knights(
                self.color_map.get(self_user), count)
        except Exception:  # noqa: BLE001
            pass

    def _resync_from_replay(self, body: dict[str, Any]) -> None:
        """Reapply just the hand state from a reconnect's full gameState.

        Colonist replays the *current* gameState on a new WS session —
        including every player's resourceCards. If we dropped frames
        during a disconnect, the tracker's self-hand will be stale. We
        re-run the HandSync emitter against the replay and push a
        corrective HandSyncEvent through the normal dispatcher, which
        overwrites the tracker's hand via ``tracker.set_hand``.

        Everything else (board, roads, buildings) stays as-is — the
        mapState snapshot in a reconnect frame matches what we already
        have, so there's nothing to replay there.
        """
        from catanbot.colonist_diff import _hand_sync_events
        game_state = body.get("gameState") if "gameState" in body else body
        if not isinstance(game_state, dict):
            return
        player_states = game_state.get("playerStates") or {}
        if not isinstance(player_states, dict):
            return
        events = _hand_sync_events(self.session, player_states)
        for ev in events:
            apply_event(self.tracker, self.color_map, ev)

    def _debit_build(self, event: BuildEvent) -> None:
        """Charge the standard cost for a placement, if it wasn't free.

        Setup-phase builds (each color's first 2 settlements and first
        2 roads) are free, as are road-building dev-card roads. We can't
        see that distinction from the WS diff alone, so we infer it from
        the running per-color count of applied placements.

        Self-color builds are skipped: the playerStates.resourceCards
        snapshot that rides alongside the build in the same diff is an
        absolute post-build hand, and HandSyncEvent already applied it
        authoritatively. Debiting again would over-deduct and leave the
        tracker 3 ORE / 2 WHEAT short of ground truth on every city.

        Cost debits are best-effort: if a color's inferred hand lacks
        the resource, we swallow the error rather than crashing the
        feed. Missing card context is expected in beta — trades with
        hidden resources and third-party steals will leave gaps.
        """
        color = self.color_map.get(event.player)
        tally = self.build_counts.setdefault(
            color, {"settlement": 0, "city": 0, "road": 0})
        tally[event.piece] += 1
        if self._is_self_color(color):
            return
        if event.piece == "settlement" and tally["settlement"] > 2:
            cost = _SETTLEMENT_COST
        elif event.piece == "city":
            cost = _CITY_COST
        elif event.piece == "road" and tally["road"] > 2:
            cost = _ROAD_COST
        else:
            return
        for resource, amount in cost.items():
            try:
                self.tracker.take(color, amount, resource)
            except TrackerError:
                pass

    def _color_for_username(self, username: str | None) -> str | None:
        if not username or self.color_map is None:
            return None
        try:
            return self.color_map.get(username)
        except Exception:  # noqa: BLE001 — unseated / over-full table
            return None

    def _is_self_color(self, color: str) -> bool:
        if self.session is None or self.session.self_color_id is None:
            return False
        self_name = self.session.player_names.get(self.session.self_color_id)
        if not self_name or not self.color_map.has(self_name):
            return False
        return self.color_map.get(self_name) == color
