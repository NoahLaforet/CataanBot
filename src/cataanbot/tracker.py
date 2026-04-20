"""Manual game-state tracker.

Wraps a catanatron `Game` object with methods that mirror a real game's
board state — settlements, cities, roads, and the robber — without caring
whose turn it is or whether they "could have afforded" the action. The
scoring/advisor layer reads off the same Game, so anything the tracker
records shows up in the render and the advisor output.

Resource tracking, dice rolls, dev cards, and trades are intentionally
not handled here yet. Once the board mirror is rock-solid, we'll layer
those on top.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from catanatron import Game
    from catanatron.models.enums import Color as ColorEnum  # noqa: F401


DEFAULT_COLORS = ("RED", "BLUE", "WHITE", "ORANGE")


class TrackerError(ValueError):
    """Raised when an input refers to an unknown color, node, or edge."""


class Tracker:
    """Thin wrapper around a `Game` focused on board-state mirroring."""

    def __init__(self) -> None:
        self.game = self._fresh_game()

    # --- lifecycle -------------------------------------------------------
    def reset(self) -> None:
        """Discard any tracked state and start over on a new random board."""
        self.game = self._fresh_game()

    @staticmethod
    def _fresh_game() -> "Game":
        from catanatron import Color, Game, RandomPlayer
        return Game([RandomPlayer(c) for c in
                     (Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE)])

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
        self._require_node(node_id)
        board = self.game.state.board
        try:
            board.build_settlement(self._color(color), node_id,
                                   initial_build_phase=True)
        except ValueError as e:
            raise TrackerError(str(e)) from e

    def city(self, color: str, node_id: int) -> None:
        self._require_node(node_id)
        c = self._color(color)
        board = self.game.state.board
        existing = board.buildings.get(node_id)
        if existing is None:
            # catanatron's build_city requires an existing settlement. Place
            # one first so the upgrade is valid.
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
        self._require_node(node_a)
        self._require_node(node_b)
        board = self.game.state.board
        try:
            board.build_road(self._color(color), (node_a, node_b))
        except ValueError as e:
            raise TrackerError(str(e)) from e

    def move_robber(self, coord: tuple[int, int, int]) -> None:
        if coord not in self.game.state.board.map.land_tiles:
            raise TrackerError(f"no land tile at {coord}")
        self.game.state.board.robber_coordinate = coord

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
        # roads are stored both directions in catanatron; divide by 2.
        road_counts: dict[str, int] = {}
        for _edge, color in board.roads.items():
            road_counts[color.name] = road_counts.get(color.name, 0) + 1
        for name in road_counts:
            road_counts[name] //= 2

        lines = [f"robber: {board.robber_coordinate}"]
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
