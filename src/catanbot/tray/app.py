"""macOS menu-bar app for the CatanBot bridge (rumps).

A clickable toolbar item: start/stop the local bridge, a status dot, an
"open colonist.io and monitor" action, and a small settings menu. Run it
with `bin/catanbot-tray` (after `pip install -e '.[bridge,tray]'`).
bin/catanbot stays the canonical cross-platform launcher; this just
spawns the same bridge under the hood and offers one-click control.
"""
from __future__ import annotations

import json
import urllib.request
import webbrowser

import rumps

from catanbot.tray import process

_DOT = {"running": "\U0001F7E2", "starting": "\U0001F7E1", "stopped": "⚪"}
_LABEL = {"running": "bridge on", "starting": "starting", "stopped": "bridge off"}


class CatanBotTray(rumps.App):
    def __init__(self) -> None:
        super().__init__("CatanBot", title="⚪ CatanBot", quit_button=None)
        self.port = process.DEFAULT_PORT
        self.menu = [
            "Start bridge",
            "Stop bridge",
            None,
            "Open colonist.io",
            None,
            ["Settings", ["Toggle Friendly Robber", "Set port…"]],
            None,
            "Quit CatanBot",
        ]
        # Poll status onto the title bar every couple of seconds.
        self._timer = rumps.Timer(self._refresh, 2)
        self._timer.start()
        self._refresh(None)

    # --- status ------------------------------------------------------
    def _refresh(self, _sender) -> None:
        st = process.status(self.port)
        self.title = f"{_DOT.get(st, _DOT['stopped'])} CatanBot"

    def _notify(self, msg: str) -> None:
        try:
            rumps.notification("CatanBot", "", msg)
        except Exception:  # noqa: BLE001
            pass

    # --- bridge control ---------------------------------------------
    @rumps.clicked("Start bridge")
    def start(self, _sender) -> None:
        process.start(self.port)
        self._refresh(None)

    @rumps.clicked("Stop bridge")
    def stop(self, _sender) -> None:
        process.stop()
        self._refresh(None)

    @rumps.clicked("Open colonist.io")
    def open_and_monitor(self, _sender) -> None:
        # Make sure the bridge is up, then open the game so the panel
        # has something to talk to the moment it loads.
        if process.status(self.port) == "stopped":
            process.start(self.port)
        webbrowser.open("https://colonist.io")
        self._refresh(None)

    # --- settings ----------------------------------------------------
    @rumps.clicked("Settings", "Toggle Friendly Robber")
    def toggle_friendly_robber(self, sender) -> None:
        sender.state = 0 if sender.state else 1
        ok = self._post_config({"friendly_robber_active": bool(sender.state)})
        self._notify(
            f"Friendly Robber {'on' if sender.state else 'off'}"
            if ok else "Start the bridge first to change settings")

    @rumps.clicked("Settings", "Set port…")
    def set_port(self, _sender) -> None:
        win = rumps.Window(
            title="Bridge port",
            message="Port the bridge listens on (default 8765):",
            default_text=str(self.port), dimensions=(120, 20))
        resp = win.run()
        if resp.clicked and resp.text.strip().isdigit():
            self.port = int(resp.text.strip())
            self._refresh(None)

    @rumps.clicked("Quit CatanBot")
    def quit_app(self, _sender) -> None:
        process.stop()
        rumps.quit_application()

    # --- helpers -----------------------------------------------------
    def _post_config(self, body: dict) -> bool:
        url = f"http://{process.BRIDGE_HOST}:{self.port}/config"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=1.0):
                return True
        except Exception:  # noqa: BLE001
            return False


def main() -> None:
    CatanBotTray().run()


if __name__ == "__main__":
    main()
