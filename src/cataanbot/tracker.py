"""Manual game-state tracker.

Wraps a catanatron `Game` object with methods that mirror a real game's
board state — settlements, cities, roads, and the robber — without caring
whose turn it is or whether they "could have afforded" the action. The
scoring/advisor layer reads off the same Game, so anything the tracker
records shows up in the render and the advisor output.

Also maintains a seed + action history so we can `undo`, `save`, and
`load` by replaying the sequence of operations against a freshly seeded
Game. That gives us cheap undo for free and reproducible save files
that survive catanatron internals changing between versions.

Resource tracking, dice rolls, dev cards, and trades are intentionally
not handled here yet. Once the board mirror is rock-solid, we'll layer
those on top.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from catanatron import Game


DEFAULT_COLORS = ("RED", "BLUE", "WHITE", "ORANGE")

# Bump if the on-disk save format changes in a breaking way.
SAVE_FORMAT_VERSION = 1


class TrackerError(ValueError):
    """Raised when an input refers to an unknown color, node, or edge."""


class Tracker:
    """Thin wrapper around a `Game` focused on board-state mirroring.

    Every state-changing method (`settle`, `city`, `road`, `move_robber`)
    appends to `self.history` *only after* the underlying catanatron call
    succeeds, so failed commands don't corrupt the replay log.
    """

    def __init__(self, seed: int | None = None) -> None:
        self.seed: int = seed if seed is not None else secrets.randbits(63)
        self.history: list[dict[str, Any]] = []
        self.game = self._new_game(self.seed)

    # --- lifecycle -------------------------------------------------------
    def reset(self, seed: int | None = None) -> None:
        """Discard any tracked state and start over on a new board."""
        self.seed = seed if seed is not None else secrets.randbits(63)
        self.history = []
        self.game = self._new_game(self.seed)

    @staticmethod
    def _new_game(seed: int) -> "Game":
        from catanatron import Color, Game, RandomPlayer
        return Game(
            [RandomPlayer(c) for c in (Color.RED, Color.BLUE,
                                       Color.WHITE, Color.ORANGE)],
            seed=seed,
        )

    # --- color / node validation ----------------------------------------
    def _color(self, name: str):
        from catanatron import Color
        name = name.upper()
        try:
            return Color[name]
        except KeyError as e:
            raise TrackerError(
                f"unknown color {name!r}; use one of "
                f"{', '.join(DEFAULT_COLORS)}"
            ) from e

    def _require_node(self, node_id: int) -> None:
        if node_id not in self.game.state.board.map.land_nodes:
            raise TrackerError(f"node {node_id} is not a land node on this map")

    # --- building ops ----------------------------------------------------
    def settle(self, color: str, node_id: int) -> None:
        self._apply_settle(color, node_id)
        self.history.append({"op": "settle", "args": [color.upper(), node_id]})

    def _apply_settle(self, color: str, node_id: int) -> None:
        self._require_node(node_id)
        try:
            self.game.state.board.build_settlement(
                self._color(color), node_id, initial_build_phase=True
            )
        except ValueError as e:
            raise TrackerError(str(e)) from e

    def city(self, color: str, node_id: int) -> None:
        self._apply_city(color, node_id)
        self.history.append({"op": "city", "args": [color.upper(), node_id]})

    def _apply_city(self, color: str, node_id: int) -> None:
        self._require_node(node_id)
        c = self._color(color)
        board = self.game.state.board
        existing = board.buildings.get(node_id)
        if existing is None:
            try:
                board.build_settlement(c, node_id, initial_build_phase=True)
            except ValueError as e:
                raise TrackerError(str(e)) from e
        elif existing[0] != c:
            raise TrackerError(
                f"node {node_id} already has a {existing[0].name} "
                f"{existing[1].lower()} — can't place a {c.name} city there"
            )
        try:
            board.build_city(c, node_id)
        except ValueError as e:
            raise TrackerError(str(e)) from e

    def road(self, color: str, node_a: int, node_b: int) -> None:
        self._apply_road(color, node_a, node_b)
        self.history.append({"op": "road",
                             "args": [color.upper(), node_a, node_b]})

    def _apply_road(self, color: str, node_a: int, node_b: int) -> None:
        self._require_node(node_a)
        self._require_node(node_b)
        try:
            self.game.state.board.build_road(self._color(color),
                                             (node_a, node_b))
        except ValueError as e:
            raise TrackerError(str(e)) from e

    def move_robber(self, coord: tuple[int, int, int]) -> None:
        self._apply_robber(coord)
        self.history.append({"op": "robber", "args": list(coord)})

    def _apply_robber(self, coord: tuple[int, int, int]) -> None:
        if coord not in self.game.state.board.map.land_tiles:
            raise TrackerError(f"no land tile at {coord}")
        self.game.state.board.robber_coordinate = coord

    # --- history ops -----------------------------------------------------
    def undo(self) -> dict[str, Any] | None:
        """Drop the last successful op and replay everything before it.

        Returns the dropped op (or None if history was empty)."""
        if not self.history:
            return None
        dropped = self.history[-1]
        self._replay(self.seed, self.history[:-1])
        return dropped

    def _replay(self, seed: int, history: list[dict[str, Any]]) -> None:
        """Rebuild the game from scratch at `seed` and re-apply `history`."""
        self.seed = seed
        self.game = self._new_game(seed)
        new_history: list[dict[str, Any]] = []
        for op in history:
            name = op["op"]
            args = op["args"]
            if name == "settle":
                self._apply_settle(args[0], args[1])
            elif name == "city":
                self._apply_city(args[0], args[1])
            elif name == "road":
                self._apply_road(args[0], args[1], args[2])
            elif name == "robber":
                self._apply_robber(tuple(args))
            else:
                raise TrackerError(f"unknown op {name!r} in history")
            new_history.append(op)
        self.history = new_history

    # --- save / load -----------------------------------------------------
    def save(self, path: str | Path) -> Path:
        path = Path(path)
        payload = {
            "format": SAVE_FORMAT_VERSION,
            "seed": self.seed,
            "history": self.history,
        }
        path.write_text(json.dumps(payload, indent=2))
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Tracker":
        payload = json.loads(Path(path).read_text())
        if payload.get("format") != SAVE_FORMAT_VERSION:
            raise TrackerError(
                f"save file format {payload.get('format')!r} not supported "
                f"(expected {SAVE_FORMAT_VERSION})"
            )
        tracker = cls(seed=int(payload["seed"]))
        tracker._replay(tracker.seed, payload.get("history", []))
        return tracker

    # --- output ----------------------------------------------------------
    def render(self, path: str | Path) -> Path:
        from cataanbot.render import render_board
        return render_board(self.game, path)

    def summary(self) -> str:
        board = self.game.state.board
        by_color: dict[str, dict[str, int]] = {}
        for _nid, (color, kind) in board.buildings.items():
            entry = by_color.setdefault(color.name, {"SETTLEMENT": 0, "CITY": 0})
            entry[kind] += 1
        road_counts: dict[str, int] = {}
        for _edge, color in board.roads.items():
            road_counts[color.name] = road_counts.get(color.name, 0) + 1
        for name in road_counts:
            road_counts[name] //= 2

        lines = [f"seed: {self.seed}   history ops: {len(self.history)}",
                 f"robber: {board.robber_coordinate}"]
        header = f"{'color':<7} {'settle':>7} {'city':>5} {'road':>5}"
        lines.append(header)
        lines.append("-" * len(header))
        for color_name in DEFAULT_COLORS:
            stats = by_color.get(color_name, {"SETTLEMENT": 0, "CITY": 0})
            lines.append(
                f"{color_name:<7} {stats['SETTLEMENT']:>7} "
                f"{stats['CITY']:>5} {road_counts.get(color_name, 0):>5}"
            )
        return "\n".join(lines)
