"""CataanBot CLI — entry point.

For now this just verifies catanatron is installed and can build a 4-player
game state. Real commands (suggest, render, ingest) come in later phases.
"""
from __future__ import annotations

import argparse
import sys


def cmd_doctor() -> int:
    """Verify catanatron imports and a fresh Game can be constructed."""
    try:
        from catanatron import Color, Game, RandomPlayer
    except ImportError as e:
        print(f"catanatron not installed or import failed: {e}", file=sys.stderr)
        print("run: pip install -e .", file=sys.stderr)
        return 1

    players = [RandomPlayer(c) for c in (Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE)]
    game = Game(players)
    state = game.state
    print(f"catanatron OK — built 4-player Game with {len(state.players)} seats")
    print(f"board: {len(state.board.map.land_tiles)} land tiles, "
          f"{len(state.board.map.port_nodes)} port nodes")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cataanbot",
        description="Settlers of Catan advisor.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="Verify catanatron integration works.")

    args = parser.parse_args(argv)
    if args.cmd == "doctor":
        return cmd_doctor()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
