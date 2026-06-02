#!/usr/bin/env python3
"""PyInstaller entry point for the bundled CatanBot bridge.

Starts the FastAPI bridge on 127.0.0.1:8765 with the advisor on, the
same as `catanbot bridge --advisor`, but as a single self-contained
binary with no Python install required. Session autosave and
postmortems are written under a per-user data dir so the binary never
needs write access to its own (possibly read-only) location.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _data_dir() -> Path:
    """Per-user, writable data dir for sessions/ and postmortems/."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "CatanBot"
    elif os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        base = (Path(root) if root else Path.home() / "AppData" / "Local") \
            / "CatanBot"
    else:
        root = os.environ.get("XDG_DATA_HOME")
        base = (Path(root) if root else Path.home() / ".local" / "share") \
            / "catanbot"
    base.mkdir(parents=True, exist_ok=True)
    return base


def main() -> int:
    os.chdir(_data_dir())
    from catanbot.bridge import serve
    return serve(host="127.0.0.1", port=8765, advisor=True)


if __name__ == "__main__":
    raise SystemExit(main())
