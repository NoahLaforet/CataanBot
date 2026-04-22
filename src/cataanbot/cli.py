"""CataanBot CLI — entry point."""
from __future__ import annotations

import argparse
import sys


def _new_game(seed: int | None = None):
    """Fresh 4-player Game. Passing a seed makes the map reproducible
    across CLI invocations — useful for `openings --after` comparisons."""
    from catanatron import Color, Game, RandomPlayer
    players = [RandomPlayer(c) for c in
               (Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE)]
    if seed is not None:
        return Game(players, seed=seed)
    return Game(players)


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
                 color: str | None = None,
                 after: list[int] | None = None,
                 seed: int | None = None) -> int:
    """Rank opening settlement spots.

    Default: fresh random board, scores every land node.

    With `--save PATH --color C`: load a live tracker state and filter the
    candidate pool to nodes C can legally place on right now (distance
    rule honored, taken spots excluded). Useful in the middle of the
    opening draft when turns have already been played.

    With `--after N [N ...]`: show a baseline ranking first, then a
    second ranking assuming each given node is already claimed (both
    the node itself and its distance-rule neighbors get removed). Makes
    the denial/blocking math visible as a side-by-side."""
    try:
        from cataanbot.advisor import (
            score_opening_nodes, format_opening_ranking,
            legal_nodes_after_picks,
        )
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
        game = _new_game(seed=seed)

    scores = score_opening_nodes(game, legal_nodes=legal_nodes)
    print(format_opening_ranking(scores, top=top))

    # When --after is given, we render the *alternatives* top-N alongside
    # the claimed picks (as gray X markers) — that matches what the terminal
    # output emphasizes, and lets the image stand alone.
    render_scores = scores
    after_scores = None
    if after:
        after_legal = legal_nodes_after_picks(game, after)
        if legal_nodes is not None:
            after_legal &= legal_nodes
        if not after_legal:
            print(f"\n(no legal spots remain after picks {after})")
        else:
            picks_str = ", ".join(str(n) for n in after)
            print(f"\nAssuming {picks_str} already claimed:")
            after_scores = score_opening_nodes(game, legal_nodes=after_legal)
            print(format_opening_ranking(after_scores, top=top))
            render_scores = after_scores

    if render_to:
        from cataanbot.render import render_board
        top_nodes = [s.node_id for s in render_scores[:top]]
        path = render_board(game, render_to, hex_size=hex_size,
                            highlight_nodes=top_nodes,
                            claimed_nodes=after)
        suffix = " (gray X = claimed via --after)" if after else ""
        print(f"\nboard rendered to {path} "
              f"(top {top} marked with gold dots{suffix})")
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
                     top: int, render_to: str | None = None,
                     hex_size: int = 60) -> int:
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
    if render_to:
        from cataanbot.render import render_board
        top_nodes = [s.node_id for s in scores[:top]]
        path = render_board(tracker.game, render_to, hex_size=hex_size,
                            highlight_nodes=top_nodes)
        print(f"\nboard rendered to {path} (top {top} marked with gold dots)")
    return 0


def cmd_bridge(host: str, port: int, jsonl: str | None,
               ws_jsonl: str | None = None, advisor: bool = False) -> int:
    """Run the FastAPI bridge that receives colonist.io log events and
    WebSocket frames from the Tampermonkey userscript."""
    from cataanbot.bridge import serve
    return serve(host=host, port=port, jsonl=jsonl,
                 ws_jsonl=ws_jsonl, advisor=advisor)


def cmd_replay(jsonl_path: str, player_args: list[str] | None,
               verbose: bool, save_to: str | None,
               render_to: str | None, hex_size: int,
               report: bool = False,
               report_out: str | None = None,
               vp_chart: str | None = None,
               production_chart: str | None = None,
               dice_chart: str | None = None,
               hand_chart: str | None = None,
               postmortem: str | None = None) -> int:
    """Replay a bridge JSONL file through the Event→Tracker dispatcher.

    Each line is parsed with `parse_event` and applied to a fresh
    Tracker via `apply_event`. Lets us audit past games offline — no
    live colonist session needed."""
    import json

    from cataanbot.live import ColorMap, ColorMapError, apply_event
    from cataanbot.parser import parse_event
    from cataanbot.tracker import Tracker

    color_map = ColorMap()
    if player_args:
        for arg in player_args:
            if "=" not in arg:
                print(
                    f"bad --player arg {arg!r}; use USERNAME=COLOR",
                    file=sys.stderr,
                )
                return 1
            user, color = arg.split("=", 1)
            try:
                color_map.add(user.strip(), color.strip())
            except ColorMapError as e:
                print(f"--player: {e}", file=sys.stderr)
                return 1

    try:
        fh = open(jsonl_path)
    except FileNotFoundError:
        print(f"no such file: {jsonl_path}", file=sys.stderr)
        return 1

    tracker = Tracker()
    counts = {"applied": 0, "skipped": 0, "unhandled": 0, "error": 0}
    events_for_report = []
    results_for_report = []
    timestamps_for_report = []
    # Dedup window for "placed a ..." / "built a ..." echoes. Colonist's
    # virtualized scroller destroys + re-creates log nodes as they
    # scroll out, and the 0.4.2 userscript's 5s content cache was
    # shorter than the ~45s setup-phase burst — so setup placements
    # re-fired 2-4 times per player. Scoped to placement/build lines
    # only because those events are always resource-gated: a real
    # same-piece build by the same player within 60s would require
    # fresh production between them, which the game's turn cadence
    # rarely allows. Other events (rolls, produces, trades) can
    # legitimately repeat within 60s and get left alone.
    DEDUP_WINDOW_S = 60.0
    seen_build_ts: dict[str, float] = {}
    dedup_dropped = 0
    with fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"{jsonl_path}:{lineno}: bad JSON — {e}",
                      file=sys.stderr)
                continue
            key = payload.get("key") if isinstance(payload, dict) else None
            raw_ts = payload.get("ts") if isinstance(payload, dict) else None
            ts_f = float(raw_ts) if isinstance(raw_ts, (int, float)) else None
            text_lc = (payload.get("text") or "").lower() if isinstance(
                payload, dict) else ""
            is_build_line = (
                "placed a" in text_lc or "built a" in text_lc
            )
            if key and ts_f is not None and is_build_line:
                last = seen_build_ts.get(key)
                if last is not None and (ts_f - last) < DEDUP_WINDOW_S:
                    dedup_dropped += 1
                    seen_build_ts[key] = ts_f
                    continue
                seen_build_ts[key] = ts_f
            event = parse_event(payload)
            result = apply_event(tracker, color_map, event)
            counts[result.status] = counts.get(result.status, 0) + 1
            if (report or report_out or vp_chart or production_chart
                    or dice_chart or hand_chart or postmortem):
                events_for_report.append(event)
                results_for_report.append(result)
                ts = payload.get("ts") if isinstance(payload, dict) else None
                timestamps_for_report.append(
                    float(ts) if isinstance(ts, (int, float)) else None,
                )
            if verbose or result.status == "error":
                stream = sys.stderr if result.status == "error" else sys.stdout
                print(
                    f"[{result.status:>9}] {type(event).__name__}: "
                    f"{result.message}",
                    file=stream,
                )

    print()
    print(
        f"replay complete: {counts['applied']} applied, "
        f"{counts['skipped']} skipped, "
        f"{counts['unhandled']} unhandled, "
        f"{counts['error']} errors",
    )
    if dedup_dropped:
        print(
            f"  (deduped {dedup_dropped} echoed event(s) "
            f"from colonist's virtualized log)"
        )
    mapping_str = ", ".join(
        f"{u}={c}" for u, c in color_map.as_dict().items()
    ) or "(empty)"
    print(f"color map: {mapping_str}")
    print()
    print(tracker.summary())

    if report or report_out:
        from cataanbot.report import build_report, format_report
        final_vp = tracker.vp_status()["per_color"]
        rep = build_report(
            events=events_for_report,
            dispatch_results=results_for_report,
            color_map=color_map,
            final_vp=final_vp,
            timestamps=timestamps_for_report,
            jsonl_path=jsonl_path,
        )
        rendered = format_report(rep)
        if report:
            print()
            print(rendered)
        if report_out:
            from pathlib import Path
            out_path = Path(report_out)
            out_path.write_text(rendered + "\n")
            print(f"\nwrote report to {out_path}")

    if save_to:
        path = tracker.save(save_to)
        print(f"\nsaved tracker state to {path}")
    if render_to:
        path = tracker.render(render_to)
        print(f"board rendered to {path}")
    if vp_chart:
        from cataanbot.timeline import build_vp_timeline, render_vp_chart
        samples = build_vp_timeline(
            events_for_report, timestamps_for_report, color_map,
        )
        path = render_vp_chart(
            samples, color_map, vp_chart,
            title=f"VP over time — {jsonl_path}",
        )
        print(f"wrote VP timeline to {path}")
    if production_chart:
        from cataanbot.timeline import (
            build_production_timeline, render_production_chart,
        )
        prod_samples = build_production_timeline(
            events_for_report, timestamps_for_report, color_map,
        )
        path = render_production_chart(
            prod_samples, color_map, production_chart,
            title=f"Cards received from rolls — {jsonl_path}",
        )
        print(f"wrote production timeline to {path}")
    if dice_chart:
        from collections import Counter
        from cataanbot.dice_chart import render_dice_histogram
        from cataanbot.events import RollEvent
        hist: Counter = Counter()
        for e in events_for_report:
            if isinstance(e, RollEvent):
                hist[e.d1 + e.d2] += 1
        path = render_dice_histogram(
            hist, dice_chart,
            title=f"Dice rolls — actual vs. expected — {jsonl_path}",
        )
        print(f"wrote dice histogram to {path}")
    if hand_chart:
        from cataanbot.timeline import (
            build_hand_timeline, render_hand_chart,
        )
        hand_samples = build_hand_timeline(
            events_for_report, timestamps_for_report, color_map,
        )
        path = render_hand_chart(
            hand_samples, color_map, hand_chart,
            title=f"Hand size over time — {jsonl_path}",
        )
        print(f"wrote hand timeline to {path}")
    if postmortem:
        from cataanbot.postmortem import render_postmortem_html
        final_vp = tracker.vp_status()["per_color"]
        path = render_postmortem_html(
            events=events_for_report,
            dispatch_results=results_for_report,
            timestamps=timestamps_for_report,
            color_map=color_map,
            final_vp=final_vp,
            out_path=postmortem,
            jsonl_path=jsonl_path,
        )
        print(f"wrote postmortem HTML to {path}")
    return 0


def cmd_ws_replay(capture_path: str, verbose: bool,
                  save_to: str | None, render_to: str | None,
                  hex_size: int) -> int:
    """Drive a colonist WS capture file through LiveGame.feed().

    Unlike ``replay`` (which consumes bridge JSONL from the DOM log
    scraper), this path feeds decoded WebSocket frames — so builds,
    roads, and robber moves resolve to real catanatron node/coord ids
    and actually apply, not just get echoed as 'unhandled'."""
    from pathlib import Path

    from cataanbot.colonist_proto import load_capture
    from cataanbot.live_game import LiveGame

    path = Path(capture_path).expanduser()
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1

    game = LiveGame()
    counts: dict[str, int] = {}
    event_counts: dict[str, int] = {}
    for frame in load_capture(path):
        if frame.error:
            continue
        payload = frame.payload
        if not isinstance(payload, dict):
            continue
        for result in game.feed(payload):
            counts[result.status] = counts.get(result.status, 0) + 1
            ev_name = type(result.event).__name__
            event_counts[ev_name] = event_counts.get(ev_name, 0) + 1
            if verbose:
                print(f"{result.status:10s} {ev_name:20s} {result.message}")

    if not game.started:
        print("capture has no GameStart (type=4) frame — nothing to replay",
              file=sys.stderr)
        return 1

    print(f"\nfed {sum(counts.values())} events "
          f"({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))})")
    print(f"players: {game.color_map.as_dict()}")
    print()
    print(game.tracker.summary())

    if save_to:
        out = game.tracker.save(Path(save_to).expanduser())
        print(f"\nwrote tracker save to {out}")
    if render_to:
        try:
            from cataanbot.render import render_board
        except ImportError as e:
            print(f"render deps missing: {e}", file=sys.stderr)
            return 1
        rendered = render_board(game.tracker.game,
                                Path(render_to).expanduser(),
                                hex_size=hex_size)
        print(f"wrote {rendered}")
    return 0


def cmd_hands(save_path: str) -> int:
    """Per-color hand accounting against a saved tracker state."""
    tracker = _load_tracker(save_path)
    if tracker is None:
        return 1
    from cataanbot.hands import estimate_hands, format_hands
    print(format_hands(estimate_hands(tracker)))
    return 0


def cmd_render(output: str, hex_size: int, ticks: int,
               label_style: str = "icon",
               seed: int | None = None) -> int:
    """Render a fresh random board to a PNG, optionally after N simulated ticks
    so settlements/roads/cities show up on the output."""
    try:
        from cataanbot.render import render_board
    except ImportError as e:
        print(f"render deps missing: {e}", file=sys.stderr)
        print("run: pip install -e .", file=sys.stderr)
        return 1
    game = _new_game(seed=seed)
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


def _package_version() -> str:
    """Look up the installed package version; fall back to 'unknown' if
    we're running from a source tree without setuptools metadata."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("cataanbot")
    except PackageNotFoundError:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cataanbot",
        description="Settlers of Catan advisor.",
    )
    parser.add_argument("--version", action="version",
                        version=f"cataanbot {_package_version()}")
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
    p_render.add_argument("--seed", type=int, default=None,
                          help="Seed the fresh game so the map is reproducible "
                               "across runs.")

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
    p_openings.add_argument("--after", type=int, nargs="+", default=None,
                            metavar="NODE",
                            help="Show a follow-up ranking assuming these "
                                 "node IDs are already claimed. Each pick "
                                 "removes itself + its neighbors from the "
                                 "candidate pool.")
    p_openings.add_argument("--seed", type=int, default=None,
                            help="Seed the fresh game so the map is "
                                 "reproducible — lets --after comparisons "
                                 "work across runs. Ignored with --save.")

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

    p_hands = sub.add_parser(
        "hands",
        help="Per-color hand accounting against a saved tracker state.",
    )
    p_hands.add_argument("save", help="Path to a tracker JSON save file.")

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
    p_second.add_argument("--render", dest="render_to", default=None,
                          help="Also render the tracker's board to this PNG "
                               "with the top-N spots marked.")
    p_second.add_argument("--hex-size", type=int, default=60,
                          help="Hex radius in pixels when --render is used.")

    p_bridge = sub.add_parser(
        "bridge",
        help="Run the FastAPI bridge that ingests colonist.io log events "
             "and WebSocket frames from the Tampermonkey userscript.",
    )
    p_bridge.add_argument("--host", default="127.0.0.1",
                          help="Bind host (default: 127.0.0.1).")
    p_bridge.add_argument("--port", type=int, default=8765,
                          help="Bind port (default: 8765).")
    p_bridge.add_argument("--jsonl", default=None,
                          help="Mirror every /log event to this .jsonl file "
                               "(one JSON object per line).")
    p_bridge.add_argument("--ws-jsonl", dest="ws_jsonl", default=None,
                          help="Mirror every /ws frame to this .jsonl file "
                               "so you can re-run offline via ws-replay.")
    p_bridge.add_argument("--advisor", action="store_true",
                          help="Print an advisor line after each hand/roll "
                               "update (what you can afford to build).")

    p_live = sub.add_parser(
        "live",
        help="Run the bridge with the live advisor on — the watch-a-game "
             "mode. Same as `bridge --advisor`.",
    )
    p_live.add_argument("--host", default="127.0.0.1",
                        help="Bind host (default: 127.0.0.1).")
    p_live.add_argument("--port", type=int, default=8765,
                        help="Bind port (default: 8765).")
    p_live.add_argument("--jsonl", default=None,
                        help="Mirror /log events to this .jsonl file.")
    p_live.add_argument("--ws-jsonl", dest="ws_jsonl", default=None,
                        help="Mirror /ws frames to this .jsonl file.")

    p_ws_replay = sub.add_parser(
        "ws-replay",
        help="Drive a colonist WebSocket capture file through the full "
             "LiveGame pipeline (maps builds/roads/robber to real board "
             "positions, unlike the JSONL replay path).",
    )
    p_ws_replay.add_argument("capture", help="Path to a WS capture .json file.")
    p_ws_replay.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print each event's dispatch status as it's applied.",
    )
    p_ws_replay.add_argument(
        "--save", dest="save_to", default=None,
        help="Write the final tracker state to a JSON save file.",
    )
    p_ws_replay.add_argument(
        "--render", dest="render_to", default=None,
        help="Render the final tracker state to this PNG.",
    )
    p_ws_replay.add_argument(
        "--hex-size", type=int, default=60,
        help="Hex radius in pixels when --render is used.",
    )

    p_replay = sub.add_parser(
        "replay",
        help="Replay a bridge .jsonl file through the Event→Tracker "
             "dispatcher so we can audit past games offline.",
    )
    p_replay.add_argument("jsonl", help="Path to the .jsonl file.")
    p_replay.add_argument(
        "--player", dest="player", action="append", default=None,
        metavar="USERNAME=COLOR",
        help="Pin a colonist username to a catanatron color "
             "(RED/BLUE/WHITE/ORANGE). Repeatable. Unset names auto-assign "
             "in first-appearance order.",
    )
    p_replay.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print each event's dispatch status as it's applied.",
    )
    p_replay.add_argument(
        "--save", dest="save_to", default=None,
        help="Write the final tracker state to a JSON save file you can "
             "feed to the advisor subcommands.",
    )
    p_replay.add_argument(
        "--render", dest="render_to", default=None,
        help="Render the final tracker state to this PNG.",
    )
    p_replay.add_argument(
        "--hex-size", type=int, default=60,
        help="Hex radius in pixels when --render is used.",
    )
    p_replay.add_argument(
        "--report", action="store_true",
        help="After replay, print a postmortem report: winner, final VP, "
             "per-player aggregates, dice histogram, and parser quality.",
    )
    p_replay.add_argument(
        "--report-out", dest="report_out", default=None,
        help="Write the postmortem report to this text file "
             "(implies --report data collection; doesn't force stdout).",
    )
    p_replay.add_argument(
        "--vp-chart", dest="vp_chart", default=None,
        metavar="PATH",
        help="Render a PNG line chart of public VP over time to PATH. "
             "Uses per-event timestamps from the JSONL when present.",
    )
    p_replay.add_argument(
        "--production-chart", dest="production_chart", default=None,
        metavar="PATH",
        help="Render a PNG line chart of cumulative resource cards "
             "received from rolls (dice luck + placement quality).",
    )
    p_replay.add_argument(
        "--dice-chart", dest="dice_chart", default=None,
        metavar="PATH",
        help="Render a PNG bar chart of actual vs. expected roll "
             "counts per value (the 2d6 fairness graphic).",
    )
    p_replay.add_argument(
        "--hand-chart", dest="hand_chart", default=None,
        metavar="PATH",
        help="Render a PNG line chart of reconstructed hand size over "
             "time per player. Based on event-stream replay.",
    )
    p_replay.add_argument(
        "--postmortem", dest="postmortem", default=None,
        metavar="PATH",
        help="Write a single self-contained HTML postmortem to PATH, "
             "with the full text report and all four charts inline.",
    )

    args = parser.parse_args(argv)
    if args.cmd == "doctor":
        return cmd_doctor()
    if args.cmd == "render":
        return cmd_render(args.output, args.hex_size, args.ticks, args.labels,
                          args.seed)
    if args.cmd == "openings":
        return cmd_openings(args.top, args.render_to, args.hex_size,
                            args.save_path, args.color, args.after, args.seed)
    if args.cmd == "play":
        return cmd_play()
    if args.cmd == "robberadvice":
        return cmd_robberadvice(args.save, args.color, args.top)
    if args.cmd == "tradeeval":
        return cmd_tradeeval(args.save, args.color, args.n_out, args.res_out,
                             args.n_in, args.res_in)
    if args.cmd == "secondadvice":
        return cmd_secondadvice(args.save, args.color, args.first_node,
                                args.top, args.render_to, args.hex_size)
    if args.cmd == "stats":
        return cmd_stats(args.save, args.histogram_path)
    if args.cmd == "hands":
        return cmd_hands(args.save)
    if args.cmd == "bridge":
        return cmd_bridge(args.host, args.port, args.jsonl,
                          ws_jsonl=args.ws_jsonl, advisor=args.advisor)
    if args.cmd == "live":
        return cmd_bridge(args.host, args.port, args.jsonl,
                          ws_jsonl=args.ws_jsonl, advisor=True)
    if args.cmd == "replay":
        return cmd_replay(args.jsonl, args.player, args.verbose,
                          args.save_to, args.render_to, args.hex_size,
                          args.report, args.report_out, args.vp_chart,
                          args.production_chart, args.dice_chart,
                          args.hand_chart, args.postmortem)
    if args.cmd == "ws-replay":
        return cmd_ws_replay(args.capture, args.verbose, args.save_to,
                             args.render_to, args.hex_size)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
