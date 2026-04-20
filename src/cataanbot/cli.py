"""CataanBot CLI — entry point."""
from __future__ import annotations

import argparse
import sys


def _new_game():
    from catanatron import Color, Game, RandomPlayer
    return Game([RandomPlayer(c) for c in (Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE)])


def cmd_doctor() -> int:
    """Verify catanatron imports and a fresh Game can be constructed."""
    try:
        game = _new_game()
    except ImportError as e:
        print(f"catanatron not installed or import failed: {e}", file=sys.stderr)
        print("run: pip install -e .", file=sys.stderr)
        return 1

    state = game.state
    print(f"catanatron OK — built 4-player Game with {len(state.players)} seats")
    print(f"board: {len(state.board.map.land_tiles)} land tiles, "
          f"{len(state.board.map.port_nodes)} port nodes")
    return 0


def cmd_render(output: str, hex_size: int, ticks: int) -> int:
    """Render a fresh random board to a PNG, optionally after N simulated ticks
    so settlements/roads/cities show up on the output."""
    try:
        from cataanbot.render import render_board
    except ImportError as e:
        print(f"render deps missing: {e}", file=sys.stderr)
        print("run: pip install -e .", file=sys.stderr)
        return 1
    game = _new_game()
    for _ in range(ticks):
        if not game.state.current_prompt:
            break
        try:
            game.play_tick()
        except Exception as e:
            print(f"sim stopped early at tick: {e}", file=sys.stderr)
            break
    path = render_board(game, output, hex_size=hex_size)
    print(f"wrote {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cataanbot",
        description="Settlers of Catan advisor.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="Verify catanatron integration works.")

    p_render = sub.add_parser("render", help="Render a fresh random board to PNG.")
    p_render.add_argument("-o", "--output", default="board.png",
                          help="Output PNG path (default: board.png)")
    p_render.add_argument("--hex-size", type=int, default=60,
                          help="Hex radius in pixels (default: 60)")
    p_render.add_argument("--ticks", type=int, default=0,
                          help="Simulate this many game ticks before rendering "
                               "so settlements/roads show up (default: 0).")

    args = parser.parse_args(argv)
    if args.cmd == "doctor":
        return cmd_doctor()
    if args.cmd == "render":
        return cmd_render(args.output, args.hex_size, args.ticks)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
