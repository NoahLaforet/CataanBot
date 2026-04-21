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
    """Human-readable stdout echo so you can tail the bridge live."""
    text = (payload.get("text") or "").strip()
    icons = payload.get("icons") or []
    names = payload.get("names") or []
    icon_str = " ".join(f"[{i.get('alt','?')}]" for i in icons)
    name_str = " ".join(
        f"{n.get('name','?')}({n.get('color','')})" for n in names
    )
    ts = payload.get("ts")
    if ts is None:
        ts = time.time()
    ts_str = time.strftime("%H:%M:%S", time.localtime(ts))
    line = f"[{ts_str} #{n:04d}] {text}"
    if icon_str:
        line += f"  {icon_str}"
    if name_str:
        line += f"   ({name_str})"
    print(line, flush=True)


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
