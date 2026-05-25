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

from catanbot.colonist_map import (
    FOG_TILE_TYPES, GOLD_TILE_TYPES, KNOWN_CLASSIC_TILE_TYPES, MapMapping,
    build_mapping, corner_tile_signature, is_fog_tile, tile_resource,
)
from catanbot.events import (
    BankSyncEvent, BuildEvent, DevCardBuyEvent, DevCardSelfBuyTypedEvent,
    Event, HandSyncEvent, ProduceEvent,
    DevCardPlayEvent, RobberMoveEvent, RollEvent, TileRevealEvent,
    TradeCloseEvent, TradeOfferEvent, VPEvent,
)

# Resource type ints used inside `playerStates.{cid}.resourceCards.cards`.
# 0 is a placeholder opponents see in place of your real cards — if any
# slot is non-zero, the snapshot belongs to the self-player whose tab
# owns the WS session. Same mapping as the tile-type ints.
_CARD_RESOURCE = {
    1: "WOOD", 2: "BRICK", 3: "SHEEP", 4: "WHEAT", 5: "ORE",
}

# Dev-card type ints used inside
# ``mechanicDevelopmentCardsState.players.{cid}.developmentCards.cards``.
# 10 is the opponent placeholder (the type is hidden until played); for
# the self player, real ints land. Confirmed against real games:
#   * type 11 → KNIGHT (5-buy capture 2026-04-29: 3 buys, 3 plays)
#   * type 12 → VICTORY_POINT (capture 2026-04-29: bought, held to end)
#   * type 13 → MONOPOLY (Pond game 2026-04-29: hint matched the buy)
#   * type 14 → ROAD_BUILDING (capture 2026-04-29: matched user buy)
#   * type 15 → YEAR_OF_PLENTY (by elimination — only type left, not
#               yet directly observed but highly likely correct)
_DEV_CARD_TYPE = {
    10: None,                # opp placeholder — type hidden
    11: "KNIGHT",
    12: "VICTORY_POINT",
    13: "MONOPOLY",
    14: "ROAD_BUILDING",
    15: "YEAR_OF_PLENTY",
}


# Weights applied to keys in colonist's victoryPointsState dicts so we
# can sum them into a display VP total. Key 0 = settlements (1 VP each),
# key 1 = cities (2 VPs each), key 2 = VP dev cards held (self only, 1
# each), key 4 = has-longest-road flag (2 VPs), key 5 = has-largest-army
# flag (2 VPs). Anything else we haven't seen defaults to 0 so an
# unexpected tracking key doesn't inflate the total.
_VP_WEIGHTS: dict[int, int] = {0: 1, 1: 2, 2: 1, 4: 2, 5: 2}

# Colonist mapSetting ids for layout-only variants where the rules are
# classic Catan but the board is a custom shape. Recs are safe on these
# because the recommender scores geometry from the parsed CatanMap,
# not the canonical 19-tile template. Promotion path: capture a game
# on a new weekly map, confirm tiles are types 0..5 only, add the id.
_KNOWN_LAYOUT_VARIANTS: dict[int, str] = {
    31: "twirl",
}

# Runtime allow-list of mapSetting ids the user has clicked "Scan map"
# on. Mirrors _KNOWN_LAYOUT_VARIANTS but lives only for the lifetime of
# the bridge process — weekly maps like Scramble change every week, so
# we don't want to bake them into source. variant_label() promotes a
# scanned mapSetting to label "scanned" (which the recs gate accepts).
_SCANNED_MAP_SETTINGS: set[int] = set()


def mark_map_setting_scanned(map_setting: int) -> None:
    """Add ``map_setting`` to the runtime scanned allow-list."""
    _SCANNED_MAP_SETTINGS.add(int(map_setting))


def is_map_setting_scanned(map_setting: int) -> bool:
    return int(map_setting) in _SCANNED_MAP_SETTINGS


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
    # Last RollEvent emitted, as ``(roller_cid, d1, d2)``. Used to
    # suppress duplicate RollEvent emissions when colonist rebroadcasts
    # a session state frame mid-game (reconnect / late-join / occasional
    # resync) — the same diceState dict re-fires events_from_diff
    # without dedup. Two genuinely back-to-back rolls with identical
    # (player, d1, d2) is mathematically possible but vanishingly rare:
    # base Catan rotates turns between rolls, so the same player can't
    # roll twice in a row, and even with knight plays, a second roll
    # after a knight is impossible (knights are pre-roll only). Two
    # rolls in a row by the same player with identical dice would have
    # to be from a bug in colonist itself, not normal play.
    last_roll_emitted: tuple | None = None
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
    # Colonist's gameSettings dict from GameStart — captures the
    # variant-board flags (gameType, modeSetting, extensionSetting,
    # scenarioSetting, mapSetting) plus VP target / discard limit.
    # All-zeros = base classic Catan. Non-zero values flag a variant
    # (Seafarers, Cities & Knights, custom map, etc.). Stored here so
    # the advisor snapshot can surface "playing on: classic" or warn
    # "this looks like a variant — recs may not be tuned for it" until
    # variant-specific strategy lands.
    game_settings: dict[str, Any] = field(default_factory=dict)
    # Self-only "developmentCardsBoughtThisTurn" cache. Colonist
    # reports authoritatively which card(s) were bought during the
    # current turn (typed for self) — clears to null on turn flip.
    # We mirror that here so the advisor snapshot can compute a
    # just-bought carve-out without homemade tracking. List of type
    # ints; empty when nothing was bought this turn or we haven't
    # latched yet.
    self_dev_bought_this_turn: list[int] = field(default_factory=list)
    # Self-only "developmentCardsUsed" cache. Colonist ships the
    # full play history with types each game (e.g. [11, 14, 11]
    # after self played two knights and a road-building). Used to
    # cross-check catanatron's PLAYED_{type} counters and as the
    # authoritative source if the DOM-log play handler missed an
    # event. Empty until self plays their first card.
    self_dev_used: list[int] = field(default_factory=list)
    # Per-cid snapshot of the most-recent ``developmentCards.cards``
    # list (typed for self, placeholder ints for opps). Used to
    # multiset-diff against the new list when self's count grows so
    # we can identify which type int was added and emit a typed
    # DevCardSelfBuyTypedEvent for catanatron's tracker.
    dev_card_lists: dict[int, list[int]] = field(default_factory=dict)
    # Tile type ints colonist sent that we don't have a name for —
    # variant-board indicator. Populated from mapState.tileHexStates
    # at GameStart. Empty on classic; non-empty signals Seafarers /
    # gold-tile / Black Forest / etc. Surfaced in /advisor as a
    # warning to the user that strategy isn't tuned for this map.
    non_classic_tiles: set[int] = field(default_factory=set)
    # Colonist's optional "Friendly Robber" rule — protects players
    # at or below a VP threshold (typically 2) from being robbed.
    # Detected by an InfoEvent text starting with "friendly robber"
    # (colonist announces it once at game start). The bot's robber-
    # target ranking filters protected victims out so the suggestions
    # match what colonist's UI will actually allow.
    friendly_robber_active: bool = False
    # Active trade-offer ids we've already emitted a TradeOfferEvent
    # for. Colonist sends partial updates (e.g. just playerResponses
    # changes) as repeated entries in tradeState.activeOffers; we
    # need to dedup so the HUD doesn't re-fire the offer banner on
    # every keystroke from the offerer. Cleared when the offer key
    # appears in closedOffers or with a null value.
    active_offer_ids: set[str] = field(default_factory=set)
    # One-shot gate so the WS-side GameOverEvent emission fires exactly
    # once. Without this, every subsequent diff after the winning
    # transition would re-emit GameOverEvent and flood the postmortem.
    game_over_emitted: bool = False
    # Authoritative resource-bank counts, keyed by colonist resource
    # type int (1=WOOD..5=ORE). Seeded from GameStart's bankState and
    # patched by every diff that carries one — colonist ships partial
    # bankState deltas (just the changed resource). Mirrored into the
    # tracker via BankSyncEvent so the freqdeck never drifts negative.
    bank_resources: dict[int, int] = field(default_factory=dict)

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

        # Capture the variant-board flags so downstream callers can
        # detect non-classic boards. The "all flags == 0" pattern is
        # plain classic Catan; non-zero values flag Seafarers, Cities
        # & Knights, custom maps, etc. Until variant-specific strategy
        # lands, this is purely informational.
        gs = body.get("gameSettings")
        if isinstance(gs, dict):
            sess.game_settings = {
                k: gs.get(k) for k in (
                    "gameType", "modeSetting", "extensionSetting",
                    "scenarioSetting", "mapSetting", "diceSetting",
                    "victoryPointsToWin", "cardDiscardLimit",
                ) if k in gs
            }

        # Tile-type sweep: any int outside KNOWN_CLASSIC_TILE_TYPES
        # is a variant tile (gold hex, ocean for seafarers, fog for
        # cities & knights, etc.). Record the unknown ints so the
        # advisor can warn the user even when gameSettings flags don't
        # fire (e.g. a custom map distributed via gameType but no
        # explicit extension flag).
        for type_int in mapping.tile_types.values():
            if int(type_int) not in KNOWN_CLASSIC_TILE_TYPES:
                sess.non_classic_tiles.add(int(type_int))

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

        # Seed the resource bank from GameStart's full bankState so the
        # tracker's freqdeck starts on ground truth (matters for boards
        # whose bank isn't the classic 19-per-resource, and so diff
        # deltas patch a complete picture rather than an empty one).
        bank = game_state.get("bankState") or {}
        if isinstance(bank, dict):
            cards = bank.get("resourceCards")
            if isinstance(cards, dict):
                for k, v in cards.items():
                    try:
                        sess.bank_resources[int(k)] = int(v)
                    except (TypeError, ValueError):
                        continue

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

    def variant_label(self) -> str:
        """Short human-readable variant name from game_settings + map.

        Returns ``"classic"`` when all four variant flags
        (modeSetting / extensionSetting / scenarioSetting / mapSetting)
        are 0 AND no unknown tile types are on the board. Layout-only
        variants where colonist plays standard Catan rules on a custom
        board (Twirl mapSetting=31, etc.) get a known short name so
        the recs gate can let them through — the recommender's
        geometry-blind scoring is safe on any classic-rule layout.
        Anything else returns ``"variant"`` with whatever signal fired
        ("variant: ext=2, tiles={6,7}") so the HUD can warn the user
        that strategy may not be tuned for this map.
        """
        gs = self.game_settings or {}
        flag_keys = (
            "modeSetting", "extensionSetting",
            "scenarioSetting", "mapSetting",
        )
        nonzero = {k: gs[k] for k in flag_keys
                   if isinstance(gs.get(k), int) and gs[k] != 0}
        if not nonzero and not self.non_classic_tiles:
            return "classic"
        # Known layout-only variants — classic rules, custom board.
        # Promotion safe: variant tile types must still be empty, and
        # the only nonzero flag must be the map id we recognize.
        if (not self.non_classic_tiles
                and set(nonzero) == {"mapSetting"}
                and nonzero["mapSetting"] in _KNOWN_LAYOUT_VARIANTS):
            return _KNOWN_LAYOUT_VARIANTS[nonzero["mapSetting"]]
        # Same shape, runtime allow-list: user clicked "Scan map" on
        # an unknown weekly map (e.g. Scramble). Confirmed classic-tile
        # only and mapSetting-only at scan time, so geometry-scored
        # recs are safe just like Twirl above.
        if (not self.non_classic_tiles
                and set(nonzero) == {"mapSetting"}
                and is_map_setting_scanned(nonzero["mapSetting"])):
            return "scanned"
        # Black Forest — the board's only non-classic tiles are fog
        # hexes (types 7/8) and the sole variant flag is the map id.
        # Fog reveal is fully modelled by the reveal-event path, so
        # recs are safe here; the gate whitelists "black_forest".
        if (self.non_classic_tiles
                and self.non_classic_tiles <= FOG_TILE_TYPES
                and set(nonzero) <= {"mapSetting"}):
            return "black_forest"
        # Volcano (mapSetting 34) — gold/volcano hex (type 6) plus the
        # Black Forest fog hexes (7/8), nothing else exotic. The gold hex
        # builds as a non-producing tile but its nodes are valued as a
        # wildcard by the opening scorer (annotate_gold_nodes), and fog
        # reveal is handled like Black Forest, so geometry-scored recs are
        # safe. The gate whitelists "volcano".
        if (self.non_classic_tiles
                and self.non_classic_tiles <= (GOLD_TILE_TYPES | FOG_TILE_TYPES)
                and self.non_classic_tiles & GOLD_TILE_TYPES
                and set(nonzero) == {"mapSetting"}):
            return "volcano"
        parts = []
        for k, v in nonzero.items():
            parts.append(f"{k.replace('Setting','')}={v}")
        if self.non_classic_tiles:
            tiles = ",".join(str(t) for t in sorted(self.non_classic_tiles))
            parts.append(f"tiles={{{tiles}}}")
        return "variant: " + ", ".join(parts)

    def player_for(self, color_id: int | None) -> str:
        if color_id is None:
            return ""
        return self.player_names.get(int(color_id), f"player{int(color_id)}")

    def is_placeholder_username(self, username: str | None) -> bool:
        """True when ``username`` is a synthetic ``playerN`` slot label
        rather than a real colonist username. Happens when a seat
        joined without sending a playerUserStates entry (kicked /
        disconnected mid-game / replay-from-stale-autosave). Consumers
        use this to suppress phantom-opp rows in the HUD or to render
        them with a distinct label (no color leak in streamer mode).
        """
        if not username:
            return True
        import re
        return bool(re.match(r"^player\d+$", str(username)))

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
        # diff is the 2nd-settlement placement.
        #
        # Skip the self-player: self's hand is authoritative via
        # HandSyncEvent (from playerStates.resourceCards). Emitting this
        # ProduceEvent for self assumed the resourceCards diff always
        # rides in the SAME frame and overwrites it — but colonist ships
        # partial deltas, and a split frame leaves self's hand inflated by
        # the 3 starting cards for the rest of the game. produce_events_
        # for_roll skips self for the same reason. (audit 2026-05-24)
        if (piece == "settlement"
                and (sess.self_color_id is None
                     or int(owner) != sess.self_color_id)):
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

    # Black Forest fog reveal — a road pointed at a fog hex flips it to
    # a real tile, shipped as a tileHexStates diff carrying the new
    # `type` (resource ints 0..5) and a real `diceNumber`. Only a
    # fog -> non-fog transition counts; colonist occasionally re-ships
    # an unchanged hex state, and a non-fog hex never re-rolls.
    hex_diff = map_diff.get("tileHexStates") or {}
    for tid_str, t in hex_diff.items():
        try:
            tid = int(tid_str)
        except (TypeError, ValueError):
            continue
        if not isinstance(t, dict) or t.get("type") is None:
            continue
        new_type = int(t["type"])
        prev_type = sess.mapping.tile_types.get(tid)
        if prev_type is None or not is_fog_tile(prev_type):
            continue
        if is_fog_tile(new_type):
            continue
        coord = sess.mapping.tile_coord.get(tid)
        if coord is None:
            continue
        raw_dice = t.get("diceNumber")
        number = int(raw_dice) if raw_dice else None  # 0 = desert
        resource = tile_resource(new_type)             # None = desert
        sess.mapping.tile_types[tid] = new_type
        if number is not None:
            sess.mapping.tile_dice[tid] = number
        out.append(TileRevealEvent(
            coord=coord, resource=resource, number=number))

    # Resource bank — colonist ships partial bankState deltas (just the
    # resources whose count changed). Merge into the session's running
    # bank and emit the merged whole so the tracker resyncs to ground
    # truth instead of drifting on give/take accounting.
    bank_state = diff.get("bankState")
    if isinstance(bank_state, dict):
        cards = bank_state.get("resourceCards")
        if isinstance(cards, dict):
            changed = False
            for k, v in cards.items():
                try:
                    type_int, count = int(k), int(v)
                except (TypeError, ValueError):
                    continue
                if sess.bank_resources.get(type_int) != count:
                    sess.bank_resources[type_int] = count
                    changed = True
            if changed:
                merged = {
                    _CARD_RESOURCE[t]: c
                    for t, c in sess.bank_resources.items()
                    if t in _CARD_RESOURCE
                }
                if merged:
                    out.append(BankSyncEvent(resources=merged))

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

    for ev in _trade_offer_events(sess, diff.get("tradeState") or {}):
        out.append(ev)

    _merge_vp_state(sess, diff.get("playerStates") or {})

    # WS-side game-over detection. The DOM-log "X won the game" parser
    # is intermittent (extension's chat-log scraper goes dark sometimes),
    # so missing GameOverEvent means no postmortem gets written. Walk
    # the merged victoryPointsState — any color whose weighted total
    # hits the configured VP target wins. Emit ONCE; the per-session
    # gate prevents duplicate emissions on later diffs.
    if not sess.game_over_emitted:
        from catanbot.config import get_vp_target
        from catanbot.events import GameOverEvent as _GameOverEvent
        try:
            vp_target = int(get_vp_target())
        except Exception:  # noqa: BLE001
            vp_target = 10
        for cid in sess.victory_points_state.keys():
            if sess.vp_total(cid) >= vp_target:
                sess.game_over_emitted = True
                out.append(_GameOverEvent(winner=sess.player_for(cid)))
                break

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
        d1 = int(dice["dice1"])
        d2 = int(dice["dice2"])
        # Dedup: skip when this exact roll was just emitted. Catches
        # state-resync rebroadcasts that duplicate a recent roll diff,
        # which would otherwise inflate roll_histogram + total_rolls.
        sig = (cid, d1, d2)
        if sess.last_roll_emitted != sig:
            sess.last_roll_emitted = sig
            player = sess.player_for(cid)
            out.append(RollEvent(player=player, d1=d1, d2=d2))

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
) -> list[Event]:
    """Detect dev-card purchases by watching each player's card-list length.

    Colonist ships every player's full `developmentCards.cards` list
    when any one of them changes. The list grows when a card is bought
    (real type for the self-player, placeholder int 10 for opponents)
    and shrinks when a card is played. We only care about growth here —
    plays come through `gameLogState` with a known type, which the DOM
    parser already classifies.

    Emits one DevCardBuyEvent per opponent whose card count increased
    (the resource debit is what catanatron's tracker needs). For the
    self-player, emits a typed ``DevCardSelfBuyTypedEvent`` per new
    card so catanatron's ``{TYPE}_IN_HAND`` counter for self stays in
    sync with what colonist actually dealt out — without this, every
    play-timing hint has to fall back to an aggregate "playable"
    count and can't tell knight from monopoly. Untyped DOM-log
    DevCardBuyEvent for self still handles the resource debit; the
    typed event is purely for catanatron-state.

    Type mapping comes from ``_DEV_CARD_TYPE`` (decoded from a real
    capture). Card type ints colonist sent that we don't have a name
    for produce no event — better silent than wrong.
    """
    out: list[Event] = []
    players = dev_state.get("players")
    if not isinstance(players, dict):
        return out
    # Per-cid snapshot of the prior cards list — used to multiset-diff
    # against the new list when count grows so we can name the new
    # type int(s). dev_card_counts tracks size; dev_card_lists tracks
    # the multiset.
    prior_lists = sess.dev_card_lists
    for cid_str, pstate in players.items():
        if not isinstance(pstate, dict):
            continue
        try:
            cid = int(cid_str)
        except (TypeError, ValueError):
            continue
        # Sync self's bought-this-turn carve-out from colonist's
        # authoritative ``developmentCardsBoughtThisTurn`` — list of
        # type ints when set, null when cleared (turn flip). Replaces
        # the homemade DOM-log buy-counter + turn-flip-reset
        # bookkeeping so the just-bought carve-out doesn't drift if
        # any DOM-log buy line gets dropped.
        if cid == sess.self_color_id and "developmentCardsBoughtThisTurn" in pstate:
            bought = pstate.get("developmentCardsBoughtThisTurn")
            if isinstance(bought, list):
                sess.self_dev_bought_this_turn = [
                    int(x) for x in bought if isinstance(x, int)]
            else:
                sess.self_dev_bought_this_turn = []
        # Same for ``developmentCardsUsed`` — colonist ships self's
        # full play history with types each game, so we can mirror
        # it as authoritative per-type played counts. Useful for
        # the LA / VP / advisor heuristics that read PLAYED_KNIGHT
        # etc. (the DOM-log path also populates these, but it can
        # lag or drop a line; this is the source-of-truth.)
        if cid == sess.self_color_id and "developmentCardsUsed" in pstate:
            used = pstate.get("developmentCardsUsed")
            if isinstance(used, list):
                new_used = [
                    int(x) for x in used if isinstance(x, int)]
                # Multiset diff vs. the previous list — every newly
                # appearing type-int is a self play we haven't emitted
                # yet. Without this the WS path was silent on plays;
                # the DOM-log "X used a Knight" parse was the only
                # source of DevCardPlayEvent. When the DOM-log path
                # is dark (no /log POSTs from the extension) the
                # robber-rec on self knight play never fires.
                from collections import Counter
                added = Counter(new_used) - Counter(sess.self_dev_used)
                sess.self_dev_used = new_used
                self_user = sess.player_names.get(cid)
                for type_int, n in added.items():
                    name = _DEV_CARD_TYPE.get(int(type_int))
                    if not name or name == "VICTORY_POINT":
                        # VP cards aren't actively "played" — they just
                        # reveal. Skip so we don't spurious-fire a play
                        # event for VP unveiling.
                        continue
                    card_kind = name.lower()
                    for _ in range(n):
                        out.append(DevCardPlayEvent(
                            player=self_user or sess.player_for(cid),
                            card=card_kind,
                        ))
            elif used is None:
                # Diff that doesn't ship the field — leave cached
                # value alone. Colonist clears bought_this_turn to
                # null on turn flip but never used (it's permanent
                # game history).
                pass
        dev = pstate.get("developmentCards")
        if not isinstance(dev, dict):
            continue
        cards = dev.get("cards")
        if not isinstance(cards, list):
            continue
        prev_count = sess.dev_card_counts.get(cid, 0)
        new_count = len(cards)
        sess.dev_card_counts[cid] = new_count
        if new_count > prev_count:
            if cid != sess.self_color_id:
                # Opp buy: untyped event. The resource debit is
                # what the tracker needs — we don't know the type
                # because colonist sends a placeholder.
                for _ in range(new_count - prev_count):
                    out.append(DevCardBuyEvent(player=sess.player_for(cid)))
            else:
                # Self buy: typed event(s). Compare prior list to
                # current to find which int(s) appeared. Note: this
                # assumes the prior list is a prefix or close to it
                # (colonist appends new cards) but we use multiset
                # diff to be robust against any reordering.
                prev_list = prior_lists.get(cid, [])
                from collections import Counter
                added = Counter(cards) - Counter(prev_list)
                for type_int, n in added.items():
                    name = _DEV_CARD_TYPE.get(int(type_int))
                    if not name:
                        continue
                    for _ in range(n):
                        out.append(DevCardSelfBuyTypedEvent(
                            player=sess.player_for(cid),
                            card_type=name,
                        ))
        prior_lists[cid] = list(cards)
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


def _trade_offer_events(
    sess: LiveSession, trade_state: dict[str, Any],
) -> list[Event]:
    """Emit TradeOfferEvent for each new incoming offer in the diff.

    Colonist ships ``tradeState.activeOffers`` as a partial dict — a
    full payload (with id/creator/offered/wanted) on creation, partial
    updates (just playerResponses) as opps respond, and the key with
    a null value on close. We only emit on the FIRST sighting of an id
    that's a real offer payload, deduping via ``sess.active_offer_ids``.

    Self-creator offers are skipped — the HUD's incoming-trade banner
    is for offers Noah needs to react to, not ones he just sent. Same
    for offers in ``closedOffers`` (already past the decision window).

    Until 2026-04-30 the only offer-detection path was the DOM-log
    "X wants to give ... for ..." text. That worked when colonist
    surfaced the offer in chat but missed every offer sent through the
    UI button alone. The WS path catches both.
    """
    out: list[Event] = []
    if not isinstance(trade_state, dict):
        return out
    active = trade_state.get("activeOffers")
    if isinstance(active, dict):
        for offer_id, payload in active.items():
            if payload is None:
                # Offer was withdrawn / expired / declined with no commit.
                # Emit a close event so the HUD's incoming-trade banner
                # can clear the moment the decision window closes —
                # without this, the banner sticks around with the last
                # offer's verdict until the next offer (or game over).
                if offer_id in sess.active_offer_ids:
                    out.append(TradeCloseEvent(offer_id=str(offer_id)))
                sess.active_offer_ids.discard(offer_id)
                continue
            if offer_id in sess.active_offer_ids:
                continue
            if not isinstance(payload, dict):
                continue
            offered = payload.get("offeredResources")
            wanted = payload.get("wantedResources")
            creator = payload.get("creator")
            if (offered is None or wanted is None
                    or creator is None):
                # Partial-update frame (e.g. playerResponses change) —
                # we already have it cached or it's a status update on
                # one we've seen. Skip silently.
                continue
            if (sess.self_color_id is not None
                    and int(creator) == int(sess.self_color_id)):
                # Self-created offer — Noah doesn't need a banner for
                # his own send.
                sess.active_offer_ids.add(offer_id)
                continue
            give = _ints_to_resource_counter(offered)
            want = _ints_to_resource_counter(wanted)
            if not give and not want:
                continue
            sess.active_offer_ids.add(offer_id)
            out.append(TradeOfferEvent(
                player=sess.player_for(int(creator)),
                give=give,
                want=want,
                offer_id=str(offer_id),
            ))
    closed = trade_state.get("closedOffers")
    if isinstance(closed, dict):
        for offer_id in closed:
            if offer_id in sess.active_offer_ids:
                out.append(TradeCloseEvent(offer_id=str(offer_id)))
            sess.active_offer_ids.discard(offer_id)
    return out


def _ints_to_resource_counter(ints: Any) -> dict[str, int]:
    """Map a list of colonist resource ints to a {resource: count} dict.

    Resource int 0 is the opponent placeholder ("hidden card"); we drop
    it so a counter for an opp's offer doesn't carry meaningless zero
    keys. Unknown ints (anything outside _CARD_RESOURCE) are silently
    skipped — better than poisoning the counter with bogus types.
    """
    out: dict[str, int] = {}
    if not isinstance(ints, list):
        return out
    for n in ints:
        try:
            res = _CARD_RESOURCE.get(int(n))
        except (TypeError, ValueError):
            continue
        if res is None:
            continue
        out[res] = out.get(res, 0) + 1
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
