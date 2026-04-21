"""FastAPI bridge for the colonist.io userscript.

Day 1 goal: prove the pipe. The userscript running inside colonist.io
watches the game log via a MutationObserver and POSTs each new entry
here. This module does *not* parse colonist events yet — it stores the
raw serialized DOM payload and echoes it to stdout so you can watch
events flow live.

Run:
    ./bin/cataanbot bridge                 # default :8765
    ./bin/cataanbot bridge --port 9000
    ./bin/cataanbot bridge --jsonl path    # also mirror to a .jsonl file

Payload shape the userscript sends (see userscript/colonist_cataanbot.user.js):

    {
      "ts": 1713640000.123,
      "text": "Gratia stole  from Nona",
      "names":  [{"name": "Gratia", "color": "#E27174"},
                 {"name": "Nona",   "color": "#E09742"}],
      "icons":  [{"alt": "Resource Card"}],
      "raw_html": "<div>...</div>"          // optional, best-effort
    }
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _build_app(jsonl_path: Path | None = None):
    """Construct the FastAPI app. Imports kept lazy so the rest of the
    package doesn't require fastapi just to import cli.py."""
    from fastapi import Body, FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="cataanbot bridge", version="0.1")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    seen_count = {"n": 0}

    @app.get("/")
    def root() -> dict[str, Any]:
        return {
            "service": "cataanbot bridge",
            "version": "0.1",
            "events_received": seen_count["n"],
        }

    @app.post("/log")
    def log(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        seen_count["n"] += 1
        _print_event(payload, seen_count["n"])
        if jsonl_path is not None:
            with jsonl_path.open("a") as f:
                f.write(json.dumps(payload) + "\n")
        return {"ok": True, "received": seen_count["n"]}

    @app.post("/reset")
    def reset() -> dict[str, Any]:
        seen_count["n"] = 0
        return {"ok": True}

    return app


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
        DisconnectEvent, InfoEvent, NoStealEvent, ProduceEvent,
        RobberMoveEvent, RollBlockedEvent, RollEvent, StealEvent,
        TradeCommitEvent, TradeOfferEvent, UnknownEvent, VPEvent,
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
    if isinstance(event, VPEvent):
        frm = f" (from {event.previous_holder})" if event.previous_holder else ""
        return f"{event.player} +{event.vp_delta} VP ({event.reason}){frm}"
    if isinstance(event, RollBlockedEvent):
        prob = f" (prob {event.prob})" if event.prob is not None else ""
        return f"robber blocks {event.tile_label}{prob} — no production"
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
          jsonl: str | None = None) -> int:
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
        print(f"mirroring events to {jsonl_path}")

    app = _build_app(jsonl_path=jsonl_path)
    print(f"cataanbot bridge listening on http://{host}:{port}")
    print("POST  /log    — userscript drops events here")
    print("GET   /       — health + counter")
    print("POST  /reset  — zero the counter")
    print("Ctrl-C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
