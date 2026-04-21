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


def cmd_openings(top: int, render_to: str | None, hex_size: int,
                 save_path: str | None = None,
                 color: str | None = None) -> int:
    """Rank opening settlement spots.

    Default: fresh random board, scores every land node.

    With `--save PATH --color C`: load a live tracker state and filter the
    candidate pool to nodes C can legally place on right now (distance
    rule honored, taken spots excluded). Useful in the middle of the
    opening draft when turns have already been played."""
    try:
        from cataanbot.advisor import score_opening_nodes, format_opening_ranking
    except ImportError as e:
        print(f"advisor deps missing: {e}", file=sys.stderr)
        return 1

    legal_nodes = None
    if save_path:
        if not color:
            print("--save requires --color to know whose legal spots to use",
                  file=sys.stderr)
            return 1
        tracker = _load_tracker(save_path)
        if tracker is None:
            return 1
        from cataanbot.tracker import TrackerError
        try:
            c = tracker._color(color)
        except TrackerError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        game = tracker.game
        legal_nodes = set(
            game.state.board.buildable_node_ids(c, initial_build_phase=True)
        )
        if not legal_nodes:
            print(f"no legal opening spots left for {color}", file=sys.stderr)
            return 1
    else:
        game = _new_game()

    scores = score_opening_nodes(game, legal_nodes=legal_nodes)
    print(format_opening_ranking(scores, top=top))
    if render_to:
        from cataanbot.render import render_board
        top_nodes = [s.node_id for s in scores[:top]]
        path = render_board(game, render_to, hex_size=hex_size,
                            highlight_nodes=top_nodes)
        print(f"\nboard rendered to {path} (top {top} marked with gold dots)")
    return 0


def cmd_play() -> int:
    """Launch the manual-tracker REPL."""
    try:
        from cataanbot.repl import run
    except ImportError as e:
        print(f"tracker deps missing: {e}", file=sys.stderr)
        return 1
    return run()


def _load_tracker(save_path: str):
    """Load a tracker save file, print errors to stderr, return None on failure."""
    from cataanbot.tracker import Tracker, TrackerError
    try:
        return Tracker.load(save_path)
    except FileNotFoundError:
        print(f"no save file at {save_path}", file=sys.stderr)
        return None
    except (TrackerError, ValueError) as e:
        print(f"could not load {save_path}: {e}", file=sys.stderr)
        return None


def cmd_robberadvice(save_path: str, color: str, top: int) -> int:
    """Run robber advisor against a saved tracker state."""
    tracker = _load_tracker(save_path)
    if tracker is None:
        return 1
    from cataanbot.advisor import score_robber_targets, format_robber_ranking
    from cataanbot.tracker import TrackerError
    try:
        tracker._color(color)
    except TrackerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    scores = score_robber_targets(tracker.game, color)
    print(format_robber_ranking(scores, color, top=top))
    return 0


def cmd_tradeeval(save_path: str, color: str, n_out: int, res_out: str,
                  n_in: int, res_in: str) -> int:
    """Run trade evaluator against a saved tracker state."""
    tracker = _load_tracker(save_path)
    if tracker is None:
        return 1
    from cataanbot.advisor import evaluate_trade, format_trade_eval
    from cataanbot.tracker import TrackerError
    try:
        tracker._color(color)
    except TrackerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        ev = evaluate_trade(tracker.game, color, n_out, res_out, n_in, res_in)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(format_trade_eval(ev))
    return 0


def cmd_stats(save_path: str, histogram_path: str | None) -> int:
    """Dice-roll stats against a saved tracker state."""
    tracker = _load_tracker(save_path)
    if tracker is None:
        return 1
    from cataanbot.stats import compute_stats, format_stats, render_histogram
    stats = compute_stats(tracker)
    print(format_stats(stats))
    if histogram_path:
        out = render_histogram(stats, histogram_path)
        print(f"\nwrote {out}")
    return 0


def cmd_secondadvice(save_path: str, color: str, first_node: int | None,
                     top: int) -> int:
    """Run second-settlement advisor against a saved tracker state."""
    tracker = _load_tracker(save_path)
    if tracker is None:
        return 1
    from cataanbot.advisor import (
        score_second_settlements, format_second_settlement_ranking,
    )
    from cataanbot.tracker import TrackerError
    try:
        c = tracker._color(color)
    except TrackerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if first_node is None:
        own = [nid for nid, (bc, kind) in
               tracker.game.state.board.buildings.items()
               if bc == c and kind == "SETTLEMENT"]
        if len(own) == 0:
            print(f"{color.upper()} has no settlement in the save — "
                  f"pass --first-node explicitly", file=sys.stderr)
            return 1
        if len(own) > 1:
            print(f"{color.upper()} has {len(own)} settlements in the save; "
                  f"pick one via --first-node (candidates: {sorted(own)})",
                  file=sys.stderr)
            return 1
        first_node = own[0]

    try:
        scores = score_second_settlements(tracker.game, first_node, color)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(format_second_settlement_ranking(scores, first_node, top=top))
    return 0


def cmd_render(output: str, hex_size: int, ticks: int,
               label_style: str = "icon") -> int:
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
    path = render_board(game, output, hex_size=hex_size,
                        label_style=label_style)
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
    p_render.add_argument("--labels", choices=("icon", "text"), default="icon",
                          help="Tile labels: geometric 'icon' (default) or "
                               "the older 'text' (WHEAT/WOOD/...) for debugging.")

    p_openings = sub.add_parser(
        "openings",
        help="Rank opening settlement spots on a fresh random board.",
    )
    p_openings.add_argument("--top", type=int, default=10,
                            help="How many spots to show (default: 10).")
    p_openings.add_argument("--render", dest="render_to", default=None,
                            help="Also render the generated board to this PNG.")
    p_openings.add_argument("--hex-size", type=int, default=60,
                            help="Hex radius in pixels when --render is used.")
    p_openings.add_argument("--save", dest="save_path", default=None,
                            help="Score a live tracker state instead of a fresh "
                                 "random board. Requires --color.")
    p_openings.add_argument("--color", default=None,
                            help="Whose legal placements to filter to when "
                                 "--save is given.")

    sub.add_parser("play", help="Launch the manual-tracker REPL.")

    p_robber = sub.add_parser(
        "robberadvice",
        help="Best robber tiles against a saved tracker state.",
    )
    p_robber.add_argument("save", help="Path to a tracker JSON save file.")
    p_robber.add_argument("color", help="Color to advise (RED/BLUE/WHITE/ORANGE).")
    p_robber.add_argument("--top", type=int, default=8,
                          help="How many tiles to show (default: 8).")

    p_trade = sub.add_parser(
        "tradeeval",
        help="Evaluate a proposed trade against a saved tracker state.",
    )
    p_trade.add_argument("save", help="Path to a tracker JSON save file.")
    p_trade.add_argument("color", help="Color whose perspective to evaluate.")
    p_trade.add_argument("n_out", type=int, help="Count of resource given.")
    p_trade.add_argument("res_out", help="Resource given (WOOD/BRICK/...).")
    p_trade.add_argument("n_in", type=int, help="Count of resource received.")
    p_trade.add_argument("res_in", help="Resource received.")

    p_stats = sub.add_parser(
        "stats",
        help="Dice-roll stats from a saved tracker state.",
    )
    p_stats.add_argument("save", help="Path to a tracker JSON save file.")
    p_stats.add_argument("--histogram", dest="histogram_path", default=None,
                         help="Also write a PNG roll histogram to this path.")

    p_second = sub.add_parser(
        "secondadvice",
        help="Rank second-settlement spots against a saved tracker state.",
    )
    p_second.add_argument("save", help="Path to a tracker JSON save file.")
    p_second.add_argument("color", help="Color to advise.")
    p_second.add_argument("--first-node", type=int, default=None,
                          help="Node id of the already-placed first settlement. "
                               "If omitted, uses COLOR's single settlement from "
                               "the save.")
    p_second.add_argument("--top", type=int, default=10,
                          help="How many spots to show (default: 10).")

    args = parser.parse_args(argv)
    if args.cmd == "doctor":
        return cmd_doctor()
    if args.cmd == "render":
        return cmd_render(args.output, args.hex_size, args.ticks, args.labels)
    if args.cmd == "openings":
        return cmd_openings(args.top, args.render_to, args.hex_size,
                            args.save_path, args.color)
    if args.cmd == "play":
        return cmd_play()
    if args.cmd == "robberadvice":
        return cmd_robberadvice(args.save, args.color, args.top)
    if args.cmd == "tradeeval":
        return cmd_tradeeval(args.save, args.color, args.n_out, args.res_out,
                             args.n_in, args.res_in)
    if args.cmd == "secondadvice":
        return cmd_secondadvice(args.save, args.color, args.first_node,
                                args.top)
    if args.cmd == "stats":
        return cmd_stats(args.save, args.histogram_path)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
