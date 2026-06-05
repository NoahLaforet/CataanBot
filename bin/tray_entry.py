#!/usr/bin/env python3
"""PyInstaller entry point for the self-contained CatanBot menu-bar app.

One frozen binary plays two roles depending on argv:

    (no args)      run the rumps menu-bar tray  (catanbot.tray.app:main)
    --run-bridge   run the bundled FastAPI bridge (cf. bin/bridge_entry.py)

The tray spawns this same binary with ``--run-bridge`` to start the bridge
(see catanbot.tray.process.bridge_command), so the whole thing ships as a
single self-contained .app: no repo, no venv, no Python install, and
nothing read or written outside ~/Library/Application Support/CatanBot.
That last part matters on modern macOS: a Finder-launched app gets no TCC
access to ~/Desktop or ~/Documents, so a launcher that reached into the
repo there would be killed with "Operation not permitted". Staying inside
Application Support sidesteps TCC entirely.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _data_dir() -> Path:
    """Per-user, writable data dir for sessions/ + postmortems/ (mirrors
    bin/bridge_entry.py). Always TCC-accessible, unlike ~/Desktop."""
    base = Path.home() / "Library" / "Application Support" / "CatanBot"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _run_bridge() -> int:
    # Windowed (.app) builds have no console, so sys.stdout/sys.stderr can
    # be None; uvicorn's logging writes to them and would crash. Point them
    # at devnull before anything in the bridge logs.
    devnull = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = devnull
    if sys.stderr is None:
        sys.stderr = devnull

    port = 8765
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass

    os.chdir(_data_dir())
    from catanbot.bridge import serve
    return serve(host="127.0.0.1", port=port, advisor=True)


def main() -> int:
    if "--run-bridge" in sys.argv:
        return _run_bridge()
    from catanbot.tray.app import main as tray_main
    tray_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
