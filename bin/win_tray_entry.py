#!/usr/bin/env python3
"""PyInstaller entry point for the Windows CatanBot tray app.

One frozen exe, two roles by argv (mirrors the macOS bin/tray_entry.py):

    (no args)      run the system-tray app (catanbot.tray.win_app.run_tray)
    --run-bridge   run the FastAPI bridge with no console, the child the
                   tray spawns

If the tray fails for any reason the entry falls back to running the bridge
directly, so a downloaded exe is never a no-op: the worst case is a working
bridge with no tray icon.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _run_bridge() -> int:
    # Windowed builds have no console, so sys.stdout/stderr can be None;
    # uvicorn's logging would crash. Point them at devnull first.
    devnull = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = devnull
    if sys.stderr is None:
        sys.stderr = devnull
    root = os.environ.get("LOCALAPPDATA") \
        or str(Path.home() / "AppData" / "Local")
    base = Path(root) / "CatanBot"
    base.mkdir(parents=True, exist_ok=True)
    os.chdir(base)
    from catanbot.bridge import serve
    return serve(host="127.0.0.1", port=8765, advisor=True)


def main() -> int:
    if "--run-bridge" in sys.argv:
        return _run_bridge()
    try:
        from catanbot.tray.win_app import run_tray
        run_tray()
    except Exception:  # noqa: BLE001
        # Never leave the user with a dead download: if the tray cannot
        # start, run the bridge directly so the panel still connects.
        return _run_bridge()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
