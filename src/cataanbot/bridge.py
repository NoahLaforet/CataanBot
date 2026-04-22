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
        if (isinstance(name, str) and name
                and isinstance(color, str) and color.strip()
                and name not in st["display_colors"]):
            st["display_colors"][name] = color.strip()


def _feed_postmortem(st, payload: dict[str, Any]) -> None:
    """Mirror the /log payload into the postmortem-collector pipeline.

    Parses the DOM-log payload, dispatches through a dedicated Tracker +
    ColorMap, and appends the (event, result, timestamp) triple. When a
    GameOverEvent lands we render a self-contained HTML postmortem once
    and flip ``pm_written`` so reruns (log virtualization echoes) don't
    stomp the file.
    """
    from cataanbot.events import GameOverEvent
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
    """Snapshot the top-N robber rankings for the overlay."""
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
    return [
        {
            "coord": list(s.coord),
            "resource": s.resource,
            "number": s.number,
            "score": round(s.score, 2),
            "victims": [
                {
                    "color": c,
                    "color_css": display.get(reverse.get(c, "")),
                    "username": reverse.get(c),
                    "pips": pips,
                    "vp": s.victim_vp.get(c, 0),
                    "cards": s.opponent_hand_size.get(c, 0),
                }
                for c, pips in sorted(
                    s.victims.items(), key=lambda kv: -kv[1])
            ],
        }
        for s in scores[:top]
    ]


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
        "my_turn": False,
        "recommendations": [],
    }
    if not game.started:
        return snap
    sess = game.session
    if sess is None or sess.self_color_id is None:
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
    if snap["my_turn"]:
        try:
            from cataanbot.recommender import recommend_actions
            snap["recommendations"] = recommend_actions(
                game.tracker.game, self_color, hand, top=4)
        except Exception:  # noqa: BLE001
            snap["recommendations"] = []
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
        snap["opps"].append({
            "username": user,
            "color": c,
            "color_css": st["display_colors"].get(user),
            "cards": int(count),
            "vp": _get_vp(game, c),
        })
    return snap


def _get_vp(game, color: str) -> int:
    """Public VP for `color` from catanatron's state."""
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
