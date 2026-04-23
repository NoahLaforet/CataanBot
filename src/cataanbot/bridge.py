"""FastAPI bridge for the colonist.io userscript.

Two ingestion paths, both POST from the userscript running in-page:

* ``POST /log`` — DOM game-log events. Each new line from colonist's
  chat panel arrives here as a parsed ``{text, names, icons, ...}``
  payload. This is the parser-driven path: `parse_event` classifies
  it and `_print_event` echoes it to stdout.

* ``POST /ws``  — raw WebSocket frame dumps. The userscript patches
  the page's WebSocket constructor and forwards every inbound frame
  here (base64 of the msgpack body). The bridge decodes it and feeds
  a singleton ``LiveGame`` — the same pipeline the ``ws-replay`` CLI
  drives against capture files, but live. When the singleton has
  booted off a GameStart frame, the dispatcher's results stream to
  stdout in the same format as ws-replay's ``--verbose``.

Run:
    ./bin/cataanbot bridge                 # default :8765
    ./bin/cataanbot bridge --port 9000
    ./bin/cataanbot bridge --jsonl path    # mirror /log events to disk
    ./bin/cataanbot bridge --ws-jsonl path # mirror /ws frames to disk
    ./bin/cataanbot live                   # bridge + advisor output on

DOM log payload shape (see userscript/colonist_cataanbot.user.js):

    {
      "ts": 1713640000.123,
      "text": "Gratia stole  from Nona",
      "names":  [{"name": "Gratia", "color": "#E27174"},
                 {"name": "Nona",   "color": "#E09742"}],
      "icons":  [{"alt": "Resource Card"}],
      "raw_html": "<div>...</div>"          // optional, best-effort
    }

WS frame payload shape (mirrors the capture-dump buffer entries):

    {
      "dir":  "in" | "out",                // direction
      "ts":   1713640000.123,
      "wsId": 1,
      "kind": "text" | "arraybuffer",
      "byteLength": 48,
      "b64":  "gqJpZKMx..." | null,        // base64 for binary frames
      "data": "{\"type\":\"Connected\",...}" | null  // text frames
    }
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _build_app(jsonl_path: Path | None = None,
               ws_jsonl_path: Path | None = None,
               advisor: bool = False,
               postmortem_dir: Path | None = None):
    """Construct the FastAPI app. Imports kept lazy so the rest of the
    package doesn't require fastapi just to import cli.py."""
    from fastapi import Body, FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker

    app = FastAPI(title="cataanbot bridge", version="0.2")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mutable state kept in a dict so closures can rebind the LiveGame
    # when a fresh game starts (a new type=4 frame after a match ends).
    # `seq` bumps on every ingested WS frame/log event so the overlay
    # knows whether a fresh poll would return new data.
    st = {
        "log_count": 0,
        "ws_count": 0,
        "ws_errors": 0,
        "game": LiveGame(),
        "seq": 0,
        "last_roll": None,        # {"player","color","total","is_you"}
        "robber_pending": False,  # self rolled 7, hasn't placed robber yet
        "robber_snapshot": None,  # cached score_robber_targets payload
        # Auto-postmortem buffers. Fed from the /log path so the output
        # shape matches `cataanbot replay --postmortem`. Independent from
        # LiveGame's WS tracker — the two pipelines never cross.
        "pm_tracker": Tracker(),
        "pm_color_map": ColorMap(),
        "pm_events": [],
        "pm_results": [],
        "pm_timestamps": [],
        "pm_written": False,
        "pm_dir": postmortem_dir,
        # username → CSS color harvested from DOM-log name pills. The
        # WS gameState only ships an opaque integer color id (and the
        # colonist palette includes premium unlocks like BLACK that
        # don't map onto catanatron's 4-color enum), so the chat log is
        # our source of truth for what color the user actually sees.
        "display_colors": {},
        # Latest pending player-to-player trade offer from the DOM log.
        # {"player", "give", "want", "ts"} when live; cleared on commit,
        # on any subsequent offer, or on the next dice roll. Evaluated
        # lazily in the snapshot builder so the verdict always reflects
        # the freshest tracker state.
        "pending_trade_offer": None,
    }

    @app.get("/")
    def root() -> dict[str, Any]:
        g = st["game"]
        return {
            "service": "cataanbot bridge",
            "version": "0.2",
            "log_events": st["log_count"],
            "ws_frames": st["ws_count"],
            "ws_errors": st["ws_errors"],
            "game_started": g.started,
            "players": g.color_map.as_dict() if g.started else {},
        }

    @app.post("/log")
    def log(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        st["log_count"] += 1
        st["seq"] += 1
        _harvest_display_colors(st, payload)
        _print_event(payload, st["log_count"])
        if jsonl_path is not None:
            with jsonl_path.open("a") as f:
                f.write(json.dumps(payload) + "\n")
        _feed_postmortem(st, payload)
        return {"ok": True, "received": st["log_count"]}

    @app.get("/advisor")
    def advisor_snapshot() -> dict[str, Any]:
        return _build_advisor_snapshot(st)

    @app.post("/ws")
    def ws_frame(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        st["ws_count"] += 1
        st["seq"] += 1
        if ws_jsonl_path is not None:
            with ws_jsonl_path.open("a") as f:
                f.write(json.dumps(payload) + "\n")
        try:
            results = _feed_ws_payload(st["game"], payload)
        except Exception as e:  # noqa: BLE001 — bridge must not crash
            st["ws_errors"] += 1
            print(f"[ws #{st['ws_count']:05d}] decode error: {e}",
                  flush=True)
            return {"ok": False, "error": str(e)}

        game = st["game"]
        # First frame that boots the game — emit a header.
        if results is None and game.started and st.get("_booted") is None:
            st["_booted"] = True
            _print_game_start(game)
            return {"ok": True, "booted": True,
                    "players": game.color_map.as_dict()}

        if results:
            _track_overlay_state(st, results)
            _print_dispatch_results(
                game, results, st["ws_count"], advisor=advisor)
        return {"ok": True, "results": len(results or [])}

    @app.post("/reset")
    def reset() -> dict[str, Any]:
        st["log_count"] = 0
        st["ws_count"] = 0
        st["ws_errors"] = 0
        st["game"] = LiveGame()
        st["seq"] = 0
        st["last_roll"] = None
        st["robber_pending"] = False
        st["robber_snapshot"] = None
        st["pm_tracker"] = Tracker()
        st["pm_color_map"] = ColorMap()
        st["pm_events"] = []
        st["pm_results"] = []
        st["pm_timestamps"] = []
        st["pm_written"] = False
        st["display_colors"] = {}
        st["pending_trade_offer"] = None
        st.pop("_booted", None)
        print("[bridge] game state reset", flush=True)
        return {"ok": True}

    return app


def _feed_ws_payload(game, payload: dict[str, Any]):
    """Decode one userscript WS-frame entry and push it through LiveGame.

    Returns None for frames we don't care about (opens, closes, decode
    errors, non-type=4/91 payloads) and a list of DispatchResults
    otherwise. A GameStart boot also returns an empty list — callers
    can distinguish by checking ``game.started`` pre/post."""
    import base64
    import json as _json

    from cataanbot.colonist_proto import decode_frame

    direction = payload.get("dir")
    if direction not in ("in", "out"):
        return None

    kind = payload.get("kind")
    if kind == "text":
        # Text frames from colonist are JSON (e.g. "Connected"). They
        # aren't part of the game-state pipe.
        text = payload.get("data")
        if not isinstance(text, str):
            return None
        try:
            body = _json.loads(text)
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        return game.feed(body)

    b64 = payload.get("b64")
    if not b64:
        return None
    data = base64.b64decode(b64)
    frame = decode_frame(data, direction)
    if frame.error or not isinstance(frame.payload, dict):
        return None
    return game.feed(frame.payload)


def _print_game_start(game) -> None:
    print("\n=== game booted via /ws ===", flush=True)
    print(f"    players: {game.color_map.as_dict()}", flush=True)
    if game.session and game.session.self_color_id is not None:
        self_user = game.session.player_names.get(
            game.session.self_color_id, "?")
        print(f"    self: {self_user} "
              f"(color id {game.session.self_color_id})",
              flush=True)
    board = game.tracker.game.state.board
    print(f"    map: {len(board.map.land_tiles)} land tiles, "
          f"robber at {board.robber_coordinate}", flush=True)
    print()


def _harvest_display_colors(st, payload: dict[str, Any]) -> None:
    """Pull {name, color} pairs out of a /log payload and latch them.

    Colonist's chat pills carry the player's true UI color in a CSS
    ``style="color: rgb(...)"`` attribute. The userscript captures this
    as ``names: [{name, color}]`` on each payload. Cache the first
    non-empty color per username — once someone shows up in the log
    we know what color they are for the rest of the game."""
    names = payload.get("names")
    if not isinstance(names, list):
        return
    for entry in names:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        color = entry.get("color")
        # Fall back to the chat pill's background color when text color
        # is missing — colonist ships WHITE-player names without inline
        # color styles (would be invisible on white chat bg) and instead
        # uses a colored background.
        bg = entry.get("bg")
        picked = None
        if isinstance(color, str) and color.strip():
            picked = color.strip()
        elif isinstance(bg, str) and bg.strip():
            picked = bg.strip()
        if (isinstance(name, str) and name and picked
                and name not in st["display_colors"]):
            st["display_colors"][name] = picked


def _feed_postmortem(st, payload: dict[str, Any]) -> None:
    """Mirror the /log payload into the postmortem-collector pipeline.

    Parses the DOM-log payload, dispatches through a dedicated Tracker +
    ColorMap, and appends the (event, result, timestamp) triple. When a
    GameOverEvent lands we render a self-contained HTML postmortem once
    and flip ``pm_written`` so reruns (log virtualization echoes) don't
    stomp the file.
    """
    from cataanbot.events import (
        DevCardPlayEvent, GameOverEvent, RobberMoveEvent, RollEvent,
        TradeCommitEvent, TradeOfferEvent,
    )
    from cataanbot.live import apply_event
    from cataanbot.parser import parse_event

    try:
        event = parse_event(payload)
    except Exception as e:  # noqa: BLE001
        print(f"[pm] parse error: {e}", flush=True)
        return
    try:
        result = apply_event(st["pm_tracker"], st["pm_color_map"], event)
    except Exception as e:  # noqa: BLE001
        print(f"[pm] dispatch error: {e}", flush=True)
        return

    ts = payload.get("ts")
    ts_f = float(ts) if isinstance(ts, (int, float)) else None

    st["pm_events"].append(event)
    st["pm_results"].append(result)
    st["pm_timestamps"].append(ts_f)

    # Trade-offer lifecycle. Offers are informational to the tracker but
    # the advisor surfaces them as accept/decline recommendations, so we
    # cache the latest one here and let the snapshot builder evaluate it
    # against the live hand. Any commit/roll invalidates the cached offer.
    if isinstance(event, TradeOfferEvent):
        st["pending_trade_offer"] = {
            "player": event.player,
            "give": dict(event.give),
            "want": dict(event.want),
            "ts": ts_f,
        }
    elif isinstance(event, (TradeCommitEvent, RollEvent)):
        st["pending_trade_offer"] = None

    # Robber ranking on Knight play. WS pipeline's _track_overlay_state
    # already covers the 7-roll case, but DevCardPlayEvents only come
    # through the DOM log, so we hook here. Clearing on RobberMoveEvent
    # is redundant with the WS path but costs nothing and keeps us safe
    # if colonist stops shipping the robber-move diff.
    game = st.get("game")
    if (isinstance(event, DevCardPlayEvent) and event.card == "knight"
            and game is not None
            and _is_self_player(game, event.player)):
        st["robber_pending"] = True
        st["robber_snapshot"] = _compute_robber_snapshot(
            game, display_colors=st.get("display_colors") or {})
    elif isinstance(event, RobberMoveEvent):
        st["robber_pending"] = False
        st["robber_snapshot"] = None

    if isinstance(event, GameOverEvent) and not st["pm_written"]:
        _write_postmortem(st, event)


def _write_postmortem(st, game_over) -> None:
    """Render the HTML postmortem to ``st['pm_dir']`` (or the default)."""
    import time as _time
    from pathlib import Path as _Path

    from cataanbot.postmortem import render_postmortem_html

    out_dir = st.get("pm_dir")
    if out_dir is None:
        out_dir = _Path.home() / "Desktop" / "CataanBot" / "postmortems"
    out_dir = _Path(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[pm] could not create {out_dir}: {e}", flush=True)
        return

    stamp = _time.strftime("%Y-%m-%d_%H%M%S")
    winner = (getattr(game_over, "winner", "") or "game").strip() or "game"
    safe_winner = "".join(
        c if c.isalnum() or c in ("-", "_") else "_" for c in winner)
    out_path = out_dir / f"{stamp}_{safe_winner}.html"

    try:
        final_vp = st["pm_tracker"].vp_status()["per_color"]
    except Exception:  # noqa: BLE001
        final_vp = {}

    try:
        path = render_postmortem_html(
            events=st["pm_events"],
            dispatch_results=st["pm_results"],
            timestamps=st["pm_timestamps"],
            color_map=st["pm_color_map"],
            final_vp=final_vp,
            out_path=out_path,
            jsonl_path=None,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[pm] render failed: {e}", flush=True)
        return

    st["pm_written"] = True
    print(f"\n=== postmortem written → {path} ===\n", flush=True)


def _track_overlay_state(st, results) -> None:
    """Maintain the overlay's tiny FSM alongside dispatch.

    Two bits of state that the overlay wants to show but the tracker
    doesn't expose on its own:

    * last_roll — the most recent RollEvent. The overlay highlights it
      so you can see at a glance whether your pips just fired.
    * robber_pending — True between the moment *you* roll a 7 and the
      moment a RobberMoveEvent lands. While pending, /advisor ships the
      top-N robber target ranking so the overlay can surface it inline.
    """
    from cataanbot.events import RobberMoveEvent, RollEvent

    game = st["game"]
    for r in results:
        if r.status not in ("applied", "skipped"):
            continue
        if isinstance(r.event, RollEvent):
            is_you = _is_self_player(game, r.event.player)
            color = None
            if r.event.player:
                try:
                    color = game.color_map.get(r.event.player)
                except Exception:  # noqa: BLE001
                    color = None
            st["last_roll"] = {
                "player": r.event.player,
                "color": color,
                "total": r.event.total,
                "is_you": bool(is_you),
            }
            if r.event.total == 7 and is_you:
                st["robber_pending"] = True
                st["robber_snapshot"] = _compute_robber_snapshot(
                    game, display_colors=st["display_colors"])
            elif r.event.total == 7:
                # Opponent rolled 7 — you don't pick, clear any stale
                # overlay ranking from a prior self-roll if somehow still set.
                st["robber_pending"] = False
                st["robber_snapshot"] = None
        elif isinstance(r.event, RobberMoveEvent):
            # Any robber move clears pending — once the robber lands the
            # overlay's ranking is stale.
            st["robber_pending"] = False
            st["robber_snapshot"] = None


def _compute_robber_snapshot(
    game, display_colors: dict[str, str] | None = None, top: int = 5,
) -> list[dict[str, Any]] | None:
    """Snapshot the top-N robber rankings for the overlay.

    Each target gets a ``suggested_victim`` color — the best single person
    to steal from when the tile has more than one adjacent opposing
    settlement/city. Scoring: card count dominates (biggest EV per steal,
    more cards = more likely to hold a needed resource), but a near-win
    opponent (VP ≥ ``mid_late_vp()``) gets boosted priority to deny them
    resources.
    """
    from cataanbot.advisor import score_robber_targets

    sess = game.session
    if sess is None or sess.self_color_id is None:
        return None
    username = sess.player_names.get(sess.self_color_id)
    if not username:
        return None
    try:
        color = game.color_map.get(username)
    except Exception:  # noqa: BLE001
        return None
    # catanatron-color → username, so the victim pills can surface the
    # real colonist UI color for the robber ranking.
    reverse = {}
    for cid, user in sess.player_names.items():
        try:
            reverse[game.color_map.get(user)] = user
        except Exception:  # noqa: BLE001
            continue
    hand_size_override: dict[str, int] = {}
    for cid, count in sess.hand_card_counts.items():
        user = sess.player_names.get(cid)
        if not user:
            continue
        try:
            c = game.color_map.get(user)
        except Exception:  # noqa: BLE001
            continue
        hand_size_override[c] = int(count)
    try:
        scores = score_robber_targets(
            game.tracker.game, color,
            hand_size_override=hand_size_override or None,
        )
    except Exception:  # noqa: BLE001
        return None
    display = display_colors or {}
    out = []
    for s in scores[:top]:
        # Pick the best victim: card count dominates (best steal EV), VP
        # pressure boosts near-winners, pip contribution is a small nudge.
        from cataanbot.config import close_to_win_vp, mid_late_vp
        close_vp = close_to_win_vp()
        mid_vp = mid_late_vp()
        def _victim_priority(vcolor: str) -> float:
            cards = s.opponent_hand_size.get(vcolor, 0)
            vp = s.victim_vp.get(vcolor, 0)
            pips = s.victims.get(vcolor, 0)
            vp_weight = 3.0 if vp >= close_vp else (
                1.8 if vp >= mid_vp else 1.0)
            return cards * vp_weight + pips * 0.3
        suggested_color: str | None = None
        if s.victims:
            # Prefer a victim with >=1 card; all-empty-hands falls back to
            # the highest priority anyway, which is fine.
            with_cards = [
                c for c in s.victims
                if s.opponent_hand_size.get(c, 0) > 0
            ]
            pool = with_cards or list(s.victims.keys())
            suggested_color = max(pool, key=_victim_priority)
        out.append({
            "coord": list(s.coord),
            "resource": s.resource,
            "number": s.number,
            "score": round(s.score, 2),
            "suggested_victim": suggested_color,
            "victims": [
                {
                    "color": c,
                    "color_css": display.get(reverse.get(c, "")),
                    "username": reverse.get(c),
                    "pips": pips,
                    "vp": s.victim_vp.get(c, 0),
                    "cards": s.opponent_hand_size.get(c, 0),
                    "suggested": (c == suggested_color),
                }
                for c, pips in sorted(
                    s.victims.items(), key=lambda kv: -kv[1])
            ],
        })
    return out


# Cost tables mirror the build_costs used elsewhere in the bot. Keys
# are ordered by "preserve this build over the others" priority — a
# city beats a settlement beats a dev card beats a road when we can
# only keep one of them post-discard.
_DISCARD_PRESERVE_PLANS: tuple[tuple[str, dict[str, int]], ...] = (
    ("city", {"WHEAT": 2, "ORE": 3}),
    ("settlement", {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1}),
    ("dev card", {"WHEAT": 1, "SHEEP": 1, "ORE": 1}),
    ("road", {"WOOD": 1, "BRICK": 1}),
)
# Drop priority: resources on the left go first. SHEEP's the cheapest
# to re-acquire (3:1 bank trade still leaves an edge) and isn't the
# bottleneck for cities the way WHEAT/ORE are. WOOD/BRICK slot in
# between — roads are cheaper than cities. WHEAT/ORE last: losing an
# ore delays city tempo more than losing any other card.
_DISCARD_PRIORITY: tuple[str, ...] = (
    "SHEEP", "WOOD", "BRICK", "WHEAT", "ORE")


def _compute_discard_plan(
    hand: dict[str, int], need: int,
) -> tuple[dict[str, int], str | None]:
    """Return {resource: count} to discard plus a rationale string.

    Strategy: try to preserve the most valuable build we can afford
    *after* the discard. Drop from resources not needed for that build,
    lowest-priority first; only break into the preserved build if the
    remaining non-reserved cards aren't enough.
    """
    if need <= 0:
        return {}, None
    total = sum(hand.values())
    after = total - need
    preserve_name: str | None = None
    reserved: dict[str, int] = {}
    for name, cost in _DISCARD_PRESERVE_PLANS:
        cost_total = sum(cost.values())
        if cost_total > after:
            continue
        if all(hand.get(r, 0) >= n for r, n in cost.items()):
            preserve_name = name
            reserved = dict(cost)
            break
    drops: dict[str, int] = {}
    remaining = need
    for r in _DISCARD_PRIORITY:
        if remaining == 0:
            break
        droppable = hand.get(r, 0) - reserved.get(r, 0)
        if droppable <= 0:
            continue
        take = min(droppable, remaining)
        drops[r] = drops.get(r, 0) + take
        remaining -= take
    if remaining > 0:
        # Reserved cards weren't enough slack — we have to dip into the
        # preserved build. Drop from its cheapest resource first.
        preserve_name = None
        for r in _DISCARD_PRIORITY:
            if remaining == 0:
                break
            available = hand.get(r, 0) - drops.get(r, 0)
            if available <= 0:
                continue
            take = min(available, remaining)
            drops[r] = drops.get(r, 0) + take
            remaining -= take
    return drops, preserve_name


def _compute_discard_hint(
    hand: dict[str, int], cards: int,
) -> dict[str, Any] | None:
    """Recommend which cards to discard when self must discard on a 7.

    Fires only when the authoritative card count exceeds the discard
    limit (default 7). Returns None otherwise so the overlay can hide
    the banner. Uses the authoritative ``cards`` total — the tracker's
    per-resource breakdown is trusted for the *shape* but the total is
    ``cards``; a drift between the two surfaces elsewhere already.
    """
    from cataanbot.config import DISCARD_LIMIT
    if cards <= DISCARD_LIMIT:
        return None
    need = cards // 2
    if need <= 0:
        return None
    drops, preserve = _compute_discard_plan(hand, need)
    if not drops:
        return None
    if preserve:
        rationale = f"keep enough to {preserve}"
    else:
        rationale = "trim least-scarce cards"
    return {
        "need": need,
        "drop": drops,
        "rationale": rationale,
    }


# Resource tie-breaker weights when a Monopoly would net equal totals
# across resources — prefer stealing the strategically scarcer cards.
_MONOPOLY_RES_WEIGHT = {
    "ORE": 5, "WHEAT": 5, "BRICK": 3, "WOOD": 3, "SHEEP": 2,
}
_BUILD_COSTS_MONOPOLY = {
    "city": {"WHEAT": 2, "ORE": 3},
    "settlement": {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1},
    "dev card": {"WHEAT": 1, "SHEEP": 1, "ORE": 1},
    "road": {"WOOD": 1, "BRICK": 1},
}


def _compute_monopoly_hint(
    game, self_color: str, self_hand: dict[str, int],
) -> dict[str, Any] | None:
    """Pick the best resource to steal when self plays Monopoly.

    Fires only when self holds at least one MONOPOLY card. Ranks each
    resource by the inferred total held across opps; ties break toward
    resources that would unlock an immediate build for self.
    """
    from catanatron import Color
    try:
        my_enum = Color[self_color.upper()]
    except Exception:  # noqa: BLE001
        return None
    state = game.tracker.game.state
    idx = state.color_to_index.get(my_enum)
    if idx is None:
        return None
    held = int(state.player_state.get(f"P{idx}_MONOPOLY_IN_HAND", 0))
    if held <= 0:
        return None
    # Aggregate inferred counts across opps via the tracker.
    totals: dict[str, int] = {
        "WOOD": 0, "BRICK": 0, "SHEEP": 0, "WHEAT": 0, "ORE": 0,
    }
    for opp_color in state.color_to_index:
        if opp_color == my_enum:
            continue
        try:
            opp_hand = game.tracker.hand(opp_color.value)
        except Exception:  # noqa: BLE001
            continue
        for r, n in opp_hand.items():
            if r in totals:
                totals[r] += int(n)
    if not any(totals.values()):
        return None
    # Rank: (count, unlock-bonus, resource-weight).
    def _unlock(res: str) -> int:
        # 1 if grabbing `res` unlocks any build we couldn't afford.
        gained = dict(self_hand)
        gained[res] = gained.get(res, 0) + totals[res]
        for name, cost in _BUILD_COSTS_MONOPOLY.items():
            if all(gained.get(r, 0) >= n for r, n in cost.items()):
                if not all(self_hand.get(r, 0) >= n for r, n in cost.items()):
                    return 1
        return 0
    ranked = sorted(
        totals.items(),
        key=lambda kv: (kv[1], _unlock(kv[0]),
                        _MONOPOLY_RES_WEIGHT.get(kv[0], 0)),
        reverse=True,
    )
    best_res, best_count = ranked[0]
    if best_count <= 0:
        return None
    # Unlock reason: which build does this unlock (if any)?
    unlock_reason: str | None = None
    gained = dict(self_hand)
    gained[best_res] = gained.get(best_res, 0) + best_count
    for name, cost in _BUILD_COSTS_MONOPOLY.items():
        if (all(gained.get(r, 0) >= n for r, n in cost.items())
                and not all(self_hand.get(r, 0) >= n
                            for r, n in cost.items())):
            unlock_reason = f"unlocks {name}"
            break
    return {
        "have": held,
        "resource": best_res,
        "est_steal": best_count,
        "totals": totals,
        "unlock": unlock_reason,
    }


def _compute_yop_hint(
    game, self_color: str, self_hand: dict[str, int],
) -> dict[str, Any] | None:
    """Suggest which pair to pick with Year-of-Plenty.

    Fires only when self holds at least one YEAR_OF_PLENTY card. Picks
    the pair that unlocks the most valuable buildable; falls back to
    the pair that aligns with the costliest build closest to complete.
    """
    from catanatron import Color
    try:
        my_enum = Color[self_color.upper()]
    except Exception:  # noqa: BLE001
        return None
    state = game.tracker.game.state
    idx = state.color_to_index.get(my_enum)
    if idx is None:
        return None
    held = int(state.player_state.get(f"P{idx}_YEAR_OF_PLENTY_IN_HAND", 0))
    if held <= 0:
        return None
    # For each target build, compute deficit in self_hand. A pick is
    # "unlocking" iff total_deficit <= 2 (YoP grants exactly 2 cards).
    best: tuple[int, str, list[str]] | None = None  # (priority, build, [r1, r2])
    priority = {"city": 4, "settlement": 3, "dev card": 2, "road": 1}
    for name, cost in _BUILD_COSTS_MONOPOLY.items():
        deficit: dict[str, int] = {}
        for r, n in cost.items():
            d = n - self_hand.get(r, 0)
            if d > 0:
                deficit[r] = d
        total = sum(deficit.values())
        if total == 0:
            # Already affordable; YoP would be wasted on this target.
            continue
        if total > 2:
            continue
        pick: list[str] = []
        for r, d in deficit.items():
            pick.extend([r] * d)
        if len(pick) < 2:
            # Fill the second slot with a resource toward the next-
            # best build (city takes priority if YoP is generous).
            needs_next = None
            for n2, cost2 in _BUILD_COSTS_MONOPOLY.items():
                if n2 == name:
                    continue
                for r2, need in cost2.items():
                    have = self_hand.get(r2, 0)
                    if name != n2 and have + pick.count(r2) < need:
                        needs_next = r2
                        break
                if needs_next:
                    break
            pick.append(needs_next or "ORE")  # ORE as safe default
        pick = pick[:2]
        p = priority.get(name, 0)
        if best is None or p > best[0]:
            best = (p, name, pick)
    if best is None:
        return None
    _, build_name, pair = best
    return {
        "have": held,
        "pair": pair,
        "unlock": build_name,
    }


def _compute_knight_hint(
    game, display_colors: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Recommend whether to play a Knight dev card this turn.

    Fires only when self has at least one KNIGHT in hand. The "should
    play" logic weighs:
        * Robber currently on one of self's tiles → urgent remove
        * Top robber target score >= 4 → meaningful block
        * An opp at 7+ VP with 2+ played knights → deny largest-army

    Returns {have, should_play, reason, best_target} or None if self has
    no Knight or we can't determine self color.
    """
    from catanatron import Color

    sess = game.session
    if sess is None or sess.self_color_id is None:
        return None
    username = sess.player_names.get(sess.self_color_id)
    if not username:
        return None
    try:
        color = game.color_map.get(username)
    except Exception:  # noqa: BLE001
        return None
    try:
        my_enum = Color[color.upper()]
    except Exception:  # noqa: BLE001
        return None

    state = game.tracker.game.state
    idx = state.color_to_index.get(my_enum)
    if idx is None:
        return None
    knight_in_hand = int(
        state.player_state.get(f"P{idx}_KNIGHT_IN_HAND", 0))
    if knight_in_hand <= 0:
        return None

    board = game.tracker.game.state.board
    robber = board.robber_coordinate
    # Robber currently blocking me? Find self buildings on the robber tile.
    self_blocked_pips = 0
    m = board.map
    robber_tile = m.land_tiles.get(robber) if robber else None
    if robber_tile is not None and robber_tile.number:
        from cataanbot.advisor import PIP_DOTS_BY_NUMBER
        robber_node_ids = set(robber_tile.nodes.values())
        for nid, (bcol, _bt) in board.buildings.items():
            if bcol != my_enum or int(nid) not in robber_node_ids:
                continue
            self_blocked_pips += PIP_DOTS_BY_NUMBER.get(robber_tile.number, 0)

    # Opp closing in on largest army? (>= 2 played knights, at/above
    # largest-army-threat VP threshold)
    from cataanbot.config import largest_army_threat_vp
    la_threat_vp = largest_army_threat_vp()
    largest_army_threat = False
    for opp_color, opp_idx in state.color_to_index.items():
        if opp_color == my_enum:
            continue
        played = int(state.player_state.get(
            f"P{opp_idx}_PLAYED_KNIGHT", 0))
        vp = int(state.player_state.get(
            f"P{opp_idx}_VICTORY_POINTS", 0))
        if played >= 2 and vp >= la_threat_vp:
            largest_army_threat = True
            break

    # Best robber target score (reuses the existing ranker).
    top_targets = _compute_robber_snapshot(
        game, display_colors=display_colors, top=1) or []
    top_target = top_targets[0] if top_targets else None
    top_score = float(top_target["score"]) if top_target else 0.0

    should = False
    reason = "hold — no urgent block"
    if self_blocked_pips > 0:
        should = True
        reason = f"robber on your tile ({self_blocked_pips} pips blocked)"
    elif largest_army_threat:
        should = True
        reason = "opp closing on largest army — break their tempo"
    elif top_score >= 4.0:
        should = True
        reason = f"strong block available (score {top_score:+.1f})"

    return {
        "have": knight_in_hand,
        "should_play": should,
        "reason": reason,
        "best_target": top_target,
    }


def _build_advisor_snapshot(st) -> dict[str, Any]:
    """JSON payload for the userscript overlay.

    Poll-friendly: callers diff on `seq` to detect change, but can
    unconditionally re-render if they prefer. All fields are safe to
    render even before a game has booted — `self` is None until then."""
    game = st["game"]
    snap: dict[str, Any] = {
        "seq": st["seq"],
        "game_started": game.started,
        "ws_frames": st["ws_count"],
        "log_events": st["log_count"],
        "self": None,
        "opps": [],
        "last_roll": st.get("last_roll"),
        "robber_pending": bool(st.get("robber_pending")),
        "robber_targets": st.get("robber_snapshot") or [],
        # "forced" = self rolled a 7 and must place the robber now;
        # "knight" = self holds a KNIGHT, targets shown as play-timing aid.
        # None when targets are empty or from an older setup flow.
        "robber_reason": (
            "forced" if st.get("robber_pending") else None),
        "my_turn": False,
        "recommendations": [],
        "incoming_trade": None,
        "knight_hint": None,
        "monopoly_hint": None,
        "yop_hint": None,
        "discard_hint": None,
        "threat": None,
    }
    if not game.started:
        return snap
    sess = game.session
    if sess is None:
        return snap
    # Opening picks don't need a latched self-color — they're a
    # board-level ranking of the top remaining spots. self_color_id
    # stays None until colonist ships a resourceCards frame with real
    # (non-zero) values, which only happens once resources land. So we
    # evaluate setup_phase here, before the self-color gate, so Noah
    # sees opening picks from the first frame rather than after his
    # 2nd settlement drops resources.
    #
    # We count settlements per seat directly rather than trusting
    # catanatron's ``is_initial_build_phase`` flag — that only flips
    # when catanatron's own turn machinery transitions into mid-game,
    # and our event-driven dispatch doesn't always trigger that.
    cat_game = game.tracker.game
    # Setup-phase detection: count settlements+cities per seat directly.
    # Must include cities — when a settlement upgrades, catanatron
    # rewrites the building's type, so a seat with 2 openings that
    # later upgrades one drops to ``settlements == 1`` even though the
    # opening phase is long over.
    num_players = len(sess.player_names) if sess.player_names else 0
    buildings_per_color: dict[Any, int] = {}
    for _nid, (col, btype) in cat_game.state.board.buildings.items():
        if btype in ("SETTLEMENT", "CITY"):
            buildings_per_color[col] = (
                buildings_per_color.get(col, 0) + 1)
    # Roads: count unique edges per color. catanatron stores each road
    # under both (a,b) and (b,a) orderings, so de-dup with a frozenset.
    roads_per_color: dict[Any, int] = {}
    seen_edges: set[frozenset[int]] = set()
    for edge, col in cat_game.state.board.roads.items():
        key = frozenset((int(edge[0]), int(edge[1])))
        if key in seen_edges:
            continue
        seen_edges.add(key)
        roads_per_color[col] = roads_per_color.get(col, 0) + 1
    # Opening is complete only when every color has 2 settlements AND
    # 2 roads. The 2nd settlement alone isn't enough — if we flipped
    # ``is_setup`` False as soon as the last 2nd settlement landed, the
    # opening picks (with their road-direction hint) would vanish
    # before the placing player had a chance to lay the matching road.
    opening_complete = (
        num_players > 0
        and len(buildings_per_color) >= num_players
        and min(buildings_per_color.values()) >= 2
        and len(roads_per_color) >= num_players
        and min(roads_per_color.values()) >= 2
    )
    is_setup = not opening_complete
    snap["setup_phase"] = is_setup
    if is_setup:
        from cataanbot.recommender import recommend_opening
        # self_color_id latches after self's 2nd settlement ships its
        # first resourceCards frame. Pass it in when we have it so the
        # "finish your opening road" followup can fire — without a
        # color, recommend_opening can't tell whose road is missing.
        rec_color: str | None = None
        if sess.self_color_id is not None:
            user = sess.player_names.get(sess.self_color_id)
            if user:
                try:
                    rec_color = game.color_map.get(user)
                except Exception:  # noqa: BLE001
                    rec_color = None
        try:
            snap["recommendations"] = recommend_opening(
                cat_game, rec_color, top=5)
        except Exception as e:  # noqa: BLE001
            print(f"[advisor] recommend_opening failed: {e!r}",
                  flush=True)
            snap["recommendations"] = []
    if sess.self_color_id is None:
        return snap
    username = sess.player_names.get(sess.self_color_id)
    if not username:
        return snap
    try:
        self_color = game.color_map.get(username)
    except Exception:  # noqa: BLE001
        return snap
    hand = dict(game.tracker.hand(self_color))
    # Authoritative total comes from colonist's raw resourceCards.cards
    # length (what we track in hand_card_counts). tracker.hand() is the
    # event-reconstructed breakdown and can drift when we miss frames
    # (disconnects, dead ws sessions) — a drift indicator we surface.
    tracker_total = sum(hand.values())
    cards = int(sess.hand_card_counts.get(sess.self_color_id, tracker_total))
    hand_drift = (tracker_total != cards)
    afford = []
    if all(hand.get(r, 0) >= n for r, n in
           (("WOOD", 1), ("BRICK", 1), ("SHEEP", 1), ("WHEAT", 1))):
        afford.append("settlement")
    if hand.get("WHEAT", 0) >= 2 and hand.get("ORE", 0) >= 3:
        afford.append("city")
    if hand.get("WOOD", 0) >= 1 and hand.get("BRICK", 0) >= 1:
        afford.append("road")
    if (hand.get("WHEAT", 0) >= 1 and hand.get("SHEEP", 0) >= 1
            and hand.get("ORE", 0) >= 1):
        afford.append("dev card")
    vp = _get_vp(game, self_color)
    snap["self"] = {
        "username": username,
        "color": self_color,
        "color_css": st["display_colors"].get(username),
        "hand": hand,
        "cards": cards,
        "afford": afford,
        "vp": vp,
        # True when per-resource breakdown disagrees with the raw-total.
        # Overlay surfaces this so Noah knows the hand detail is unreliable
        # until the next HandSync frame corrects us.
        "hand_drift": hand_drift,
    }
    # "My turn" is derived from colonist's currentTurnPlayerColor cache.
    # Recommendations only fire when it's actually my turn — off-turn
    # suggestions would just be noise.
    my_cid = sess.self_color_id
    snap["my_turn"] = (sess.current_turn_color_id is not None
                       and sess.current_turn_color_id == my_cid)
    # Mid-game recs: only when it's actually my turn. During setup the
    # opening picks were already populated above, so skip here.
    if not is_setup and snap["my_turn"]:
        try:
            from cataanbot.recommender import recommend_actions
            snap["recommendations"] = recommend_actions(
                cat_game, self_color, hand, top=4)
        except Exception as e:  # noqa: BLE001
            print(f"[advisor] recommend_actions failed: {e!r}",
                  flush=True)
            snap["recommendations"] = []
    # Physical-supply cap: base Catan has 19 of each resource, so by
    # conservation `bank[r] + sum(all hands)[r] == 19`. The tracker's
    # internal freqdeck stays consistent, but its "max-resource" guess
    # on unknown steals can still attribute a resource to one opp even
    # when that many aren't physically unclaimed. Cap each opp's
    # inferred bucket to `19 - bank[r] - self[r]` so we never display
    # "4 ore" when only 2 are actually left in play.
    _CAP_RESOURCES = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")
    opp_res_cap: dict[str, int] = {}
    try:
        freqdeck = cat_game.state.resource_freqdeck
        for idx, r in enumerate(_CAP_RESOURCES):
            opp_res_cap[r] = max(
                0, 19 - int(freqdeck[idx]) - int(hand.get(r, 0)))
    except Exception:  # noqa: BLE001
        opp_res_cap = {}
    for cid, count in sorted(sess.hand_card_counts.items()):
        if cid == sess.self_color_id:
            continue
        user = sess.player_names.get(cid)
        if not user:
            continue
        try:
            c = game.color_map.get(user)
        except Exception:  # noqa: BLE001
            continue
        # Inferred per-resource breakdown. The tracker applies every
        # observable delta (produce, known trades, builds, dev-card buys)
        # to opponent hands as they happen — so ``tracker.hand(color)``
        # is a lower-bound estimate. It can *diverge* from the authoritative
        # ``hand_card_counts`` total when a 3rd-party steal or a
        # closed-type discard happens; we surface that gap as ``unknown``.
        # Steals/discards between opponents make the breakdown noisy, so
        # clients should treat high unknown% as low-confidence.
        try:
            inferred = dict(game.tracker.hand(c))
        except Exception:  # noqa: BLE001
            inferred = {}
        # Clip to physical supply first. Any excess gets absorbed into
        # ``unknown`` below, which is more honest than displaying a
        # count that couldn't exist on the board.
        if opp_res_cap:
            for r, n in list(inferred.items()):
                cap = opp_res_cap.get(r, n)
                if n > cap:
                    inferred[r] = cap
        # Reconcile inference against the authoritative card count.
        # Over-attribution (inferred > real) happens when the tracker's
        # "max-resource" guess for unknown steals commits to the wrong
        # resource and we haven't caught up yet. Trim only the excess
        # from the largest bucket(s) instead of zeroing the whole hand —
        # the partial knowledge we still have is more useful than a
        # blanket "?". Remaining gap after trimming is ``unknown``.
        inferred_total = sum(inferred.values())
        real_total = int(count)
        if inferred_total > real_total:
            trimmed = dict(inferred)
            excess = inferred_total - real_total
            while excess > 0 and any(v > 0 for v in trimmed.values()):
                best = max(trimmed, key=lambda r: trimmed.get(r, 0))
                n = min(excess, trimmed[best])
                trimmed[best] -= n
                excess -= n
            inferred = trimmed
            inferred_total = sum(inferred.values())
        unknown = max(0, real_total - inferred_total)
        snap["opps"].append({
            "username": user,
            "color": c,
            "color_css": st["display_colors"].get(user),
            "cards": real_total,
            "hand": inferred,
            "unknown": unknown,
            # True when we know every card: breakdown sums to the total.
            "hand_tracked": (unknown == 0 and real_total > 0),
            "vp": _get_vp(game, c),
            # Unplayed dev cards in hand. Includes hidden VPs, so a
            # spike here is a real "they might be close to 10" signal.
            # Counting comes from colonist's authoritative card-list
            # length; we can't see the types, only the size.
            "dev_cards": int(sess.dev_card_counts.get(cid, 0)),
        })

    pending = st.get("pending_trade_offer")
    if pending:
        snap["incoming_trade"] = _evaluate_pending_trade(
            st, game, self_color, hand, pending)
    # Knight-card play-timing advice: only fires when self has >=1 KNIGHT
    # in hand. Harmless to compute every snapshot; _compute_knight_hint
    # bails out cheaply when nothing to say.
    try:
        snap["knight_hint"] = _compute_knight_hint(
            game, display_colors=st.get("display_colors") or {})
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] knight_hint failed: {e!r}", flush=True)
    # When self holds a KNIGHT and isn't already facing a forced robber
    # placement, surface the full target ranking so Noah can eyeball the
    # block before committing. Don't clobber "forced" state — the 7-roll
    # path owns robber_targets in that case.
    kh = snap.get("knight_hint") or {}
    if kh.get("have", 0) > 0 and not snap["robber_pending"]:
        try:
            full = _compute_robber_snapshot(
                game, display_colors=st.get("display_colors") or {})
            if full:
                snap["robber_targets"] = full
                snap["robber_reason"] = "knight"
        except Exception as e:  # noqa: BLE001
            print(f"[advisor] knight robber targets failed: {e!r}",
                  flush=True)
    try:
        snap["monopoly_hint"] = _compute_monopoly_hint(
            game, self_color, hand)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] monopoly_hint failed: {e!r}", flush=True)
    try:
        snap["yop_hint"] = _compute_yop_hint(game, self_color, hand)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] yop_hint failed: {e!r}", flush=True)
    # Discard-on-7 advice: fires whenever self's hand exceeds the discard
    # limit. The overlay should render it prominently on a 7-roll, but
    # we compute unconditionally — it's cheap and "you're over the limit"
    # is useful context even before a roll lands.
    try:
        snap["discard_hint"] = _compute_discard_hint(hand, cards)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] discard_hint failed: {e!r}", flush=True)
    # Leader-threat banner: flag when any opp is at/near the win
    # threshold so the overlay can shift tone toward defense. Uses the
    # same close_to_win_vp() knob the rest of the bot respects.
    snap["threat"] = _compute_leader_threat(snap)
    return snap


def _compute_leader_threat(snap: dict[str, Any]) -> dict[str, Any] | None:
    """Flag the highest-VP opp and label the urgency.

    Returns a dict or None when nobody's ahead enough to warrant a
    banner. Close-to-win and mid-late thresholds track config so the
    warning scales with the game's VP_TARGET (default 10 → 8 = close).
    """
    from cataanbot.config import close_to_win_vp, mid_late_vp, VP_TARGET
    opps = snap.get("opps") or []
    if not opps:
        return None
    leader = max(opps, key=lambda o: o.get("vp", 0))
    leader_vp = int(leader.get("vp", 0))
    if leader_vp < mid_late_vp():
        return None
    self_snap = snap.get("self") or {}
    my_vp = int(self_snap.get("vp", 0))
    close_vp = close_to_win_vp()
    # Level maps to overlay styling: "win" is effectively over, "close"
    # = one build from winning, "mid" = worth noticing but not yet urgent.
    if leader_vp >= VP_TARGET:
        level = "win"
    elif leader_vp >= close_vp:
        level = "close"
    else:
        level = "mid"
    gap = leader_vp - my_vp
    if level == "close":
        msg = (f"{leader.get('username')} at {leader_vp} VP — "
               f"one build from winning")
    elif level == "win":
        msg = f"{leader.get('username')} at {leader_vp} VP — game over"
    else:
        msg = f"{leader.get('username')} leads at {leader_vp} VP"
    return {
        "leader_username": leader.get("username"),
        "leader_color": leader.get("color"),
        "leader_color_css": leader.get("color_css"),
        "leader_vp": leader_vp,
        "my_vp": my_vp,
        "gap": gap,
        "level": level,
        "message": msg,
    }


def _evaluate_pending_trade(st, game, self_color, self_hand,
                            pending: dict[str, Any]) -> dict[str, Any] | None:
    """Build the ``incoming_trade`` snapshot field — offer metadata plus
    a verdict from ``recommender.evaluate_incoming_trade``.

    Skips self-originated offers: those are our outbound proposals and
    don't need an accept/decline recommendation. Returns None in that
    case so the overlay hides the panel.
    """
    from cataanbot.recommender import evaluate_incoming_trade

    offerer = pending.get("player") or ""
    sess = game.session
    if sess is not None and sess.self_color_id is not None:
        self_user = sess.player_names.get(sess.self_color_id)
        if self_user and offerer == self_user:
            return None

    give = pending.get("give") or {}
    want = pending.get("want") or {}
    opp_vp = 0
    try:
        opp_color = game.color_map.get(offerer)
        opp_vp = _get_vp(game, opp_color)
    except Exception:  # noqa: BLE001
        opp_color = None

    try:
        verdict = evaluate_incoming_trade(
            game.tracker.game, self_color, self_hand,
            give, want, opp_vp=opp_vp,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] evaluate_incoming_trade failed: {e!r}",
              flush=True)
        return None

    return {
        "offerer": offerer,
        "offerer_color": opp_color,
        "offerer_color_css": st["display_colors"].get(offerer),
        "offerer_vp": opp_vp,
        "give": give,
        "want": want,
        **verdict,
    }


def _get_vp(game, color: str) -> int:
    """VP for `color` — prefer colonist's authoritative state.

    Colonist's victoryPointsState per color is what its UI displays
    (settles + cities + held VP cards + longest-road/largest-army
    flags). Using it directly avoids drift that would otherwise creep
    in when BuildEvents are missed on reconnect or when a knight-play
    doesn't reach our tracker. Falls back to catanatron's internal
    VICTORY_POINTS when we can't resolve the color to a colonist cid
    (e.g. ws-replay fixtures without a LiveSession).
    """
    try:
        color_map = getattr(game, "color_map", None)
        sess = getattr(game, "session", None)
        if sess is not None and color_map is not None:
            username = color_map.reverse(color)
            if username is not None:
                for cid, name in sess.player_names.items():
                    if name == username:
                        if sess.victory_points_state.get(cid):
                            return sess.vp_total(cid)
                        break
    except Exception:  # noqa: BLE001
        pass
    try:
        from catanatron import Color
        c = Color[color.upper()]
        idx = game.tracker.game.state.color_to_index.get(c)
        if idx is None:
            return 0
        return int(game.tracker.game.state.player_state.get(
            f"P{idx}_VICTORY_POINTS", 0))
    except Exception:  # noqa: BLE001
        return 0


def _print_dispatch_results(game, results, seq: int,
                            advisor: bool = False) -> None:
    from cataanbot.events import (
        BuildEvent, DevCardBuyEvent, HandSyncEvent, ProduceEvent,
        RobberMoveEvent, RollEvent,
    )

    for r in results:
        cls = type(r.event).__name__
        if r.status == "applied":
            print(f"[ws #{seq:05d}] {cls}: {r.message}", flush=True)
        elif r.status == "error":
            print(f"[ws #{seq:05d}] ERROR {cls}: {r.message}", flush=True)

    if not advisor:
        return

    # When the self-player rolls a 7, the next thing they have to do is
    # pick a robber tile — surface the ranking right away so they don't
    # have to alt-tab. Opponent 7-rolls are handled by the RobberMoveEvent
    # path elsewhere (nothing to suggest — they pick).
    for r in results:
        if (isinstance(r.event, RollEvent) and r.event.total == 7
                and r.status in ("applied", "skipped")
                and _is_self_player(game, r.event.player)):
            _print_robber_targets(game)
            break

    # Minimal advisor output: whenever the self-player's hand is
    # updated, or after a roll, print what they can afford to build.
    triggered = any(isinstance(r.event, (HandSyncEvent, RollEvent))
                    and r.status in ("applied", "skipped")
                    for r in results)
    if not triggered:
        return
    _print_self_advisor(game)


def _is_self_player(game, username: str | None) -> bool:
    if not username:
        return False
    sess = game.session
    if sess is None or sess.self_color_id is None:
        return False
    return sess.player_names.get(sess.self_color_id) == username


def _print_robber_targets(game, top: int = 5) -> None:
    """Compact top-N robber ranking for when the self-player rolls a 7."""
    from cataanbot.advisor import score_robber_targets

    sess = game.session
    if sess is None or sess.self_color_id is None:
        return
    username = sess.player_names.get(sess.self_color_id)
    if not username:
        return
    color = game.color_map.get(username)
    # Ground-truth opponent hand sizes from the WS snapshot. Falls back
    # to catanatron's per-resource tracking for any seat we haven't seen
    # a resourceCards entry for yet.
    hand_size_override: dict[str, int] = {}
    for cid, count in sess.hand_card_counts.items():
        user = sess.player_names.get(cid)
        if not user:
            continue
        try:
            c = game.color_map.get(user)
        except Exception:  # noqa: BLE001
            continue
        hand_size_override[c] = int(count)
    try:
        scores = score_robber_targets(
            game.tracker.game, color,
            hand_size_override=hand_size_override or None,
        )
    except Exception as e:  # noqa: BLE001
        print(f"    [robber] ranking failed: {e}", flush=True)
        return
    if not scores:
        print("    [robber] no legal targets", flush=True)
        return
    print(f"    [robber] you rolled 7 — top {top} targets for {color}:",
          flush=True)
    for i, s in enumerate(scores[:top], start=1):
        coord_str = f"({s.coord[0]},{s.coord[1]},{s.coord[2]})"
        tile_str = ("DESERT" if s.resource is None
                    else f"{s.resource[:3]}{s.number or ''}")
        if s.victims:
            victim_str = ", ".join(
                f"{c}({p}p/{s.victim_vp.get(c,0)}VP/"
                f"{s.opponent_hand_size.get(c,0)}c)"
                for c, p in s.victims.items()
            )
        else:
            victim_str = "—"
        print(f"        {i}. {coord_str:<12} {tile_str:<8} "
              f"score={s.score:+5.1f}  {victim_str}", flush=True)


def _print_self_advisor(game) -> None:
    """Print a compact what-can-I-build line for the self-player."""
    if not game.started:
        return
    sess = game.session
    if sess is None or sess.self_color_id is None:
        return
    username = sess.player_names.get(sess.self_color_id)
    if not username:
        return
    color = game.color_map.get(username)
    hand = game.tracker.hand(color)
    cards = sum(hand.values())
    afford = []
    if all(hand.get(r, 0) >= n for r, n in
           (("WOOD", 1), ("BRICK", 1), ("SHEEP", 1), ("WHEAT", 1))):
        afford.append("settlement")
    if hand.get("WHEAT", 0) >= 2 and hand.get("ORE", 0) >= 3:
        afford.append("city")
    if hand.get("WOOD", 0) >= 1 and hand.get("BRICK", 0) >= 1:
        afford.append("road")
    if (hand.get("WHEAT", 0) >= 1 and hand.get("SHEEP", 0) >= 1
            and hand.get("ORE", 0) >= 1):
        afford.append("dev card")
    # Two-letter abbreviations so Wood and Wheat don't collide.
    abbrev = {"WOOD": "Wd", "BRICK": "Br", "SHEEP": "Sh",
              "WHEAT": "Wh", "ORE": "Or"}
    hand_str = " ".join(
        f"{n}{abbrev.get(r, r[:2])}"
        for r, n in hand.items() if n
    ) or "∅"
    buildable = ", ".join(afford) if afford else "nothing"
    print(f"    [you] {color} {cards}c ({hand_str}) → can build: "
          f"{buildable}", flush=True)

    # Opponent hand sizes in a second line — just counts, since per-
    # resource breakdowns are hidden. Helpful context for trade and
    # robber decisions even when no 7 has rolled yet.
    opp_parts = []
    for cid, count in sorted(sess.hand_card_counts.items()):
        if cid == sess.self_color_id:
            continue
        user = sess.player_names.get(cid)
        if not user:
            continue
        try:
            c = game.color_map.get(user)
        except Exception:  # noqa: BLE001
            continue
        opp_parts.append(f"{c} {count}c")
    if opp_parts:
        print(f"    [opp] {' · '.join(opp_parts)}", flush=True)


def _print_event(payload: dict[str, Any], n: int) -> None:
    """Human-readable stdout echo so you can tail the bridge live.

    Shows the structured parse on the first line and (for anything
    we can't classify yet) the raw payload on a second line so we can
    add rules for the misses.
    """
    from cataanbot.events import UnknownEvent
    from cataanbot.parser import parse_event

    ts = payload.get("ts")
    if ts is None:
        ts = time.time()
    ts_str = time.strftime("%H:%M:%S", time.localtime(ts))

    event = parse_event(payload)
    cls = type(event).__name__
    print(f"[{ts_str} #{n:04d}] {cls}: {_event_oneliner(event)}", flush=True)
    if isinstance(event, UnknownEvent):
        text = (payload.get("text") or "").strip()
        icons = [i.get("alt", "") for i in payload.get("icons") or []]
        print(f"           raw: {text}  icons={icons}", flush=True)


def _event_oneliner(event: Any) -> str:
    """Compact human-readable summary of a parsed Event."""
    from cataanbot.events import (
        BuildEvent, DevCardBuyEvent, DevCardPlayEvent, DiscardEvent,
        DisconnectEvent, GameOverEvent, InfoEvent, MonopolyStealEvent,
        NoStealEvent, ProduceEvent, RobberMoveEvent, RollBlockedEvent,
        RollEvent, StealEvent, TradeCommitEvent, TradeOfferEvent,
        UnknownEvent, VPEvent,
    )

    if isinstance(event, RollEvent):
        return f"{event.player} rolled {event.total} ({event.d1}+{event.d2})"
    if isinstance(event, ProduceEvent):
        return f"{event.player} got {_fmt_res(event.resources)}"
    if isinstance(event, BuildEvent):
        vp = f" +{event.vp_delta} VP" if event.vp_delta else ""
        return f"{event.player} built {event.piece}{vp}"
    if isinstance(event, DiscardEvent):
        return f"{event.player} discarded {_fmt_res(event.resources)}"
    if isinstance(event, RobberMoveEvent):
        prob = f" (prob {event.prob})" if event.prob is not None else ""
        return f"{event.player} moved robber → {event.tile_label}{prob}"
    if isinstance(event, StealEvent):
        res = f" [{event.resource}]" if event.resource else ""
        return f"{event.thief} stole from {event.victim}{res}"
    if isinstance(event, NoStealEvent):
        return "no one to steal from"
    if isinstance(event, TradeOfferEvent):
        return (f"{event.player} offers {_fmt_res(event.give)} for "
                f"{_fmt_res(event.want) or '?'}")
    if isinstance(event, TradeCommitEvent):
        return (f"{event.giver} gave {_fmt_res(event.gave)} and got "
                f"{_fmt_res(event.got)} from {event.receiver}")
    if isinstance(event, DevCardBuyEvent):
        return f"{event.player} bought dev card"
    if isinstance(event, DevCardPlayEvent):
        extra = ""
        if event.resources:
            extra = f" → {_fmt_res(event.resources)}"
        elif event.resource:
            extra = f" → {event.resource}"
        return f"{event.player} played {event.card}{extra}"
    if isinstance(event, MonopolyStealEvent):
        return (f"{event.player} monopolied {event.count}x{event.resource} "
                f"from opponents")
    if isinstance(event, VPEvent):
        frm = f" (from {event.previous_holder})" if event.previous_holder else ""
        return f"{event.player} +{event.vp_delta} VP ({event.reason}){frm}"
    if isinstance(event, RollBlockedEvent):
        prob = f" (prob {event.prob})" if event.prob is not None else ""
        return f"robber blocks {event.tile_label}{prob} — no production"
    if isinstance(event, GameOverEvent):
        return f"GAME OVER — {event.winner} won"
    if isinstance(event, InfoEvent):
        return f"info: {event.text}"
    if isinstance(event, DisconnectEvent):
        return f"{event.player} {'reconnected' if event.reconnected else 'disconnected'}"
    if isinstance(event, UnknownEvent):
        return "?"
    return str(event)


def _fmt_res(resources: dict[str, int]) -> str:
    if not resources:
        return ""
    return " ".join(f"{count}x{name}" for name, count in resources.items())


def serve(host: str = "127.0.0.1", port: int = 8765,
          jsonl: str | None = None,
          ws_jsonl: str | None = None,
          advisor: bool = False,
          postmortem_dir: str | None = None) -> int:
    """Run the bridge with uvicorn. Blocks until Ctrl-C."""
    try:
        import uvicorn
    except ImportError:
        print("bridge deps missing — install with: "
              "pip install -e '.[bridge]'")
        return 1

    jsonl_path = Path(jsonl).expanduser() if jsonl else None
    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"mirroring log events to {jsonl_path}")

    ws_jsonl_path = Path(ws_jsonl).expanduser() if ws_jsonl else None
    if ws_jsonl_path is not None:
        ws_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"mirroring WS frames to {ws_jsonl_path}")

    pm_dir: Path | None
    if postmortem_dir is None:
        pm_dir = Path.home() / "Desktop" / "CataanBot" / "postmortems"
    elif postmortem_dir == "":
        pm_dir = None  # explicit opt-out
    else:
        pm_dir = Path(postmortem_dir).expanduser()
    if pm_dir is not None:
        print(f"auto-postmortem will write to {pm_dir}/")

    app = _build_app(jsonl_path=jsonl_path, ws_jsonl_path=ws_jsonl_path,
                     advisor=advisor, postmortem_dir=pm_dir)
    print(f"cataanbot bridge listening on http://{host}:{port}")
    print("POST  /log      — userscript DOM log events")
    print("POST  /ws       — userscript WebSocket frames")
    print("GET   /         — health + counters + game state")
    print("GET   /advisor  — compact advisor snapshot (for the overlay)")
    print("POST  /reset    — clear game state and counters")
    if advisor:
        print("advisor output: ON")
    print("Ctrl-C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
