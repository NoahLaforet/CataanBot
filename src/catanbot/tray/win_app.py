"""Windows system-tray app for the CatanBot bridge (pystray).

The Windows counterpart to the macOS rumps menu-bar app: a notification-area
icon (the brand C) that runs the local bridge with no console window, plus a
right-click menu to open colonist.io and quit. The bridge runs as a child
process (this same frozen exe re-run with --run-bridge), so Quit can
terminate it cleanly and closing the tray takes the bridge down with it.

pystray and its win32 backend are Windows-only, so the import is guarded; if
it is missing the caller falls back to running the bridge directly so the
download is never a no-op.
"""
from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

try:
    import pystray
    from PIL import Image
except ImportError:  # pragma: no cover - pystray is Windows-only here
    pystray = None
    Image = None


def _icon_path() -> Path:
    """The brand art, bundled next to the frozen exe or in the repo."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")) / "catanbot_assets" \
            / "icon-128.png"
    return Path(__file__).resolve().parents[3] / "extension" / "icons" \
        / "icon-128.png"


def _spawn_bridge() -> subprocess.Popen:
    """Re-run this same exe in bridge mode, with no console window."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.Popen([sys.executable, "--run-bridge"],
                            creationflags=flags)


def run_tray() -> None:
    if pystray is None or Image is None:
        # No GUI lib available: just run the bridge so the user still gets a
        # working bridge (no tray, but the panel connects).
        _spawn_bridge().wait()
        return

    proc = _spawn_bridge()
    img = Image.open(_icon_path())

    def _open(icon, item):  # noqa: ARG001
        webbrowser.open("https://colonist.io")

    def _quit(icon, item):  # noqa: ARG001
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("CatanBot bridge is running", None, enabled=False),
        pystray.MenuItem("Open colonist.io", _open, default=True),
        pystray.MenuItem("Quit", _quit),
    )
    icon = pystray.Icon("CatanBot", img, "CatanBot bridge", menu)
    try:
        icon.run()
    finally:
        # Tray closed: make sure the bridge child goes with it.
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
