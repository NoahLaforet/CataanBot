"""Interactive REPL for mirroring a real Catan game into a Tracker.

Each command takes whatever state change happened on the physical board and
applies it to the wrapped catanatron Game, then optionally re-renders. The
prompt is intentionally terse — during an actual game, Noah is watching the
physical board, not reading CLI verbosity.
"""
from __future__ import annotations

import cmd
import shlex
from pathlib import Path

from cataanbot.tracker import DEFAULT_COLORS, Tracker, TrackerError


AUTO_RENDER_PATH = "tracked_board.png"


class TrackerRepl(cmd.Cmd):
    intro = (
        "cataanbot tracker — mirror a live Catan game.\n"
        "type `help` for commands, `quit` to exit.\n"
    )
    prompt = "catan> "

    def __init__(self) -> None:
        super().__init__()
        self.tracker = Tracker()
        self.auto_render = True
        self.render_path = AUTO_RENDER_PATH

    # --- command handlers -------------------------------------------------
    def do_new(self, _arg: str) -> None:
        """new — reset to a fresh random board."""
        self.tracker.reset()
        print("fresh board.")
        self._maybe_render()

    def do_settle(self, arg: str) -> None:
        """settle <COLOR> <node> — place a settlement at node id."""
        parts = shlex.split(arg)
        if len(parts) != 2:
            print("usage: settle <COLOR> <node>")
            return
        color, node = parts
        try:
            self.tracker.settle(color, int(node))
        except (TrackerError, ValueError) as e:
            print(f"error: {e}")
            return
        self._maybe_render()

    def do_city(self, arg: str) -> None:
        """city <COLOR> <node> — upgrade (or place) to a city at node id."""
        parts = shlex.split(arg)
        if len(parts) != 2:
            print("usage: city <COLOR> <node>")
            return
        color, node = parts
        try:
            self.tracker.city(color, int(node))
        except (TrackerError, ValueError) as e:
            print(f"error: {e}")
            return
        self._maybe_render()

    def do_road(self, arg: str) -> None:
        """road <COLOR> <node_a> <node_b> — build a road between two nodes."""
        parts = shlex.split(arg)
        if len(parts) != 3:
            print("usage: road <COLOR> <node_a> <node_b>")
            return
        color, a, b = parts
        try:
            self.tracker.road(color, int(a), int(b))
        except (TrackerError, ValueError) as e:
            print(f"error: {e}")
            return
        self._maybe_render()

    def do_robber(self, arg: str) -> None:
        """robber <x> <y> <z> — move the robber to a tile coordinate."""
        parts = shlex.split(arg)
        if len(parts) != 3:
            print("usage: robber <x> <y> <z>  (cube coords, e.g. `robber 1 -1 0`)")
            return
        try:
            coord = tuple(int(p) for p in parts)
        except ValueError as e:
            print(f"error: {e}")
            return
        try:
            self.tracker.move_robber(coord)
        except TrackerError as e:
            print(f"error: {e}")
            return
        self._maybe_render()

    def do_undo(self, _arg: str) -> None:
        """undo — drop the most recent op and replay everything before it."""
        try:
            dropped = self.tracker.undo()
        except TrackerError as e:
            print(f"error: {e}")
            return
        if dropped is None:
            print("nothing to undo.")
            return
        args = " ".join(str(x) for x in dropped["args"])
        print(f"undid: {dropped['op']} {args}")
        self._maybe_render()

    def do_save(self, arg: str) -> None:
        """save <path> — write tracked state to a JSON file."""
        path = arg.strip()
        if not path:
            print("usage: save <path>")
            return
        try:
            out = self.tracker.save(path)
        except Exception as e:
            print(f"error: {e}")
            return
        print(f"wrote {out}")

    def do_load(self, arg: str) -> None:
        """load <path> — replace current state with a JSON save file."""
        path = arg.strip()
        if not path:
            print("usage: load <path>")
            return
        try:
            self.tracker = Tracker.load(path)
        except (TrackerError, FileNotFoundError, ValueError) as e:
            print(f"error: {e}")
            return
        print(f"loaded {path} ({len(self.tracker.history)} ops, "
              f"seed={self.tracker.seed})")
        self._maybe_render()

    def do_show(self, _arg: str) -> None:
        """show — print a text summary of tracked state."""
        print(self.tracker.summary())

    def do_render(self, arg: str) -> None:
        """render [path] — write the current board to a PNG (default: tracked_board.png)."""
        path = arg.strip() or self.render_path
        try:
            out = self.tracker.render(path)
        except Exception as e:
            print(f"error: {e}")
            return
        print(f"wrote {out}")

    def do_autorender(self, arg: str) -> None:
        """autorender on|off — toggle whether commands re-render after every change."""
        arg = arg.strip().lower()
        if arg in ("on", "true", "1", "yes"):
            self.auto_render = True
        elif arg in ("off", "false", "0", "no"):
            self.auto_render = False
        elif arg == "":
            print(f"auto-render is {'on' if self.auto_render else 'off'} "
                  f"(file: {self.render_path})")
            return
        else:
            print("usage: autorender on | off")
            return
        print(f"auto-render: {'on' if self.auto_render else 'off'}")

    def do_colors(self, _arg: str) -> None:
        """colors — list recognized player colors."""
        print(", ".join(DEFAULT_COLORS))

    def do_quit(self, _arg: str) -> bool:
        """quit — exit the REPL."""
        return True

    do_exit = do_quit
    do_EOF = do_quit

    # --- helpers ---------------------------------------------------------
    def _maybe_render(self) -> None:
        if not self.auto_render:
            return
        try:
            self.tracker.render(self.render_path)
        except Exception as e:
            print(f"(auto-render failed: {e})")

    def emptyline(self) -> None:
        # don't repeat the last command on empty input.
        pass


def run() -> int:
    TrackerRepl().cmdloop()
    return 0
