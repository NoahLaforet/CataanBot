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
               advisor: bool = False):
    """Construct the FastAPI app. Imports kept lazy so the rest of the
    package doesn't require fastapi just to import cli.py."""
    from fastapi import Body, FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from cataanbot.live_game import LiveGame

    app = FastAPI(title="cataanbot bridge", version="0.2")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mutable state kept in a dict so closures can rebind the LiveGame
    # when a fresh game starts (a new type=4 frame after a match ends).
    st = {
        "log_count": 0,
        "ws_count": 0,
        "ws_errors": 0,
        "game": LiveGame(),
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
        _print_event(payload, st["log_count"])
        if jsonl_path is not None:
            with jsonl_path.open("a") as f:
                f.write(json.dumps(payload) + "\n")
        return {"ok": True, "received": st["log_count"]}

    @app.post("/ws")
    def ws_frame(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        st["ws_count"] += 1
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
            _print_dispatch_results(
                game, results, st["ws_count"], advisor=advisor)
        return {"ok": True, "results": len(results or [])}

    @app.post("/reset")
    def reset() -> dict[str, Any]:
        st["log_count"] = 0
        st["ws_count"] = 0
        st["ws_errors"] = 0
        st["game"] = LiveGame()
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

    # Minimal advisor output: whenever the self-player's hand is
    # updated, or after a roll, print what they can afford to build.
    triggered = any(isinstance(r.event, (HandSyncEvent, RollEvent))
                    and r.status in ("applied", "skipped")
                    for r in results)
    if not triggered:
        return
    _print_self_advisor(game)


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
    hand_str = " ".join(f"{n}{r[0]}" for r, n in hand.items() if n) or "∅"
    buildable = ", ".join(afford) if afford else "nothing"
    print(f"    [you] {color} {cards}c ({hand_str}) → can build: "
          f"{buildable}", flush=True)


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
          advisor: bool = False) -> int:
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

    app = _build_app(jsonl_path=jsonl_path, ws_jsonl_path=ws_jsonl_path,
                     advisor=advisor)
    print(f"cataanbot bridge listening on http://{host}:{port}")
    print("POST  /log    — userscript DOM log events")
    print("POST  /ws     — userscript WebSocket frames")
    print("GET   /       — health + counters + game state")
    print("POST  /reset  — clear game state and counters")
    if advisor:
        print("advisor output: ON")
    print("Ctrl-C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
