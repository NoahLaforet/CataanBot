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

    def do_roll(self, arg: str) -> None:
        """roll <n> — record a dice roll; distributes resources via catanatron."""
        parts = shlex.split(arg)
        if len(parts) != 1:
            print("usage: roll <n>  (n is 2..12, the dice SUM)")
            return
        try:
            number = int(parts[0])
        except ValueError as e:
            print(f"error: {e}")
            return
        try:
            payout = self.tracker.roll(number)
        except TrackerError as e:
            print(f"error: {e}")
            return
        if not payout:
            print(f"rolled {number}: no resources produced "
                  f"(robber blocking, or nobody adjacent).")
        else:
            lines = [f"rolled {number}:"]
            for color, delta in payout.items():
                delta_str = ", ".join(f"+{v} {k}" for k, v in delta.items())
                lines.append(f"  {color}: {delta_str}")
            print("\n".join(lines))

    def do_give(self, arg: str) -> None:
        """give <COLOR> <N> <RESOURCE> — add resources to a color's hand.

        Use for trade gains, monopoly targets, year-of-plenty, or anything
        else that increases a player's hand outside of a dice roll."""
        parts = shlex.split(arg)
        if len(parts) != 3:
            print("usage: give <COLOR> <N> <RESOURCE>")
            return
        color, n, resource = parts
        try:
            self.tracker.give(color, int(n), resource)
        except (TrackerError, ValueError) as e:
            print(f"error: {e}")
            return
        self._maybe_render()

    def do_take(self, arg: str) -> None:
        """take <COLOR> <N> <RESOURCE> — remove resources from a color's hand.

        Use for build costs, trade losses, knight steals, monopoly victims,
        or discards on a 7."""
        parts = shlex.split(arg)
        if len(parts) != 3:
            print("usage: take <COLOR> <N> <RESOURCE>")
            return
        color, n, resource = parts
        try:
            self.tracker.take(color, int(n), resource)
        except (TrackerError, ValueError) as e:
            print(f"error: {e}")
            return
        self._maybe_render()

    def do_hand(self, arg: str) -> None:
        """hand <COLOR> — print the resource hand for one color."""
        parts = shlex.split(arg)
        if len(parts) != 1:
            print("usage: hand <COLOR>")
            return
        try:
            hand = self.tracker.hand(parts[0])
        except TrackerError as e:
            print(f"error: {e}")
            return
        total = sum(hand.values())
        summary = ", ".join(f"{v} {k}" for k, v in hand.items() if v > 0) \
                  or "(empty)"
        print(f"{parts[0].upper()} hand ({total} cards): {summary}")

    def do_trade(self, arg: str) -> None:
        """trade <A> <N> <RES_A> <B> <M> <RES_B> — atomic player-to-player trade.

        A gives N of RES_A to B; B gives M of RES_B to A. Validates both
        hands first, so nothing moves if either side is short.
        Example: `trade RED 2 wheat BLUE 1 ore`."""
        parts = shlex.split(arg)
        if len(parts) != 6:
            print("usage: trade <A> <N> <RES_A> <B> <M> <RES_B>")
            return
        a, n, ra, b, m, rb = parts
        try:
            self.tracker.trade(a, int(n), ra, b, int(m), rb)
        except (TrackerError, ValueError) as e:
            print(f"error: {e}")
            return
        print(f"{a.upper()} {n} {ra.lower()} <-> {b.upper()} {m} {rb.lower()}")
        self._maybe_render()

    def do_mtrade(self, arg: str) -> None:
        """mtrade <COLOR> <N> <RES_OUT> <RES_IN> — maritime trade with the bank.

        Spends N of RES_OUT for 1 RES_IN. Pass whatever rate (4/3/2) the
        actual port/bank access allows.
        Example: `mtrade RED 3 wheat ore` (3:1 wheat port)."""
        parts = shlex.split(arg)
        if len(parts) != 4:
            print("usage: mtrade <COLOR> <N> <RES_OUT> <RES_IN>")
            return
        color, n, res_out, res_in = parts
        try:
            self.tracker.mtrade(color, int(n), res_out, res_in)
        except (TrackerError, ValueError) as e:
            print(f"error: {e}")
            return
        print(f"{color.upper()} -{n} {res_out.lower()}  +1 {res_in.lower()} (bank)")
        self._maybe_render()

    def do_devbuy(self, arg: str) -> None:
        """devbuy <COLOR> <TYPE> — give a color a dev card.

        TYPE accepts short aliases: k/knight, mono/monopoly, yop/year_of_plenty,
        road/road_building, vp/victory_point. Does NOT auto-debit the wheat/
        sheep/ore cost — pair with `take` commands if you want the hand to
        stay honest."""
        parts = shlex.split(arg)
        if len(parts) != 2:
            print("usage: devbuy <COLOR> <TYPE>  "
                  "(k | mono | yop | road | vp — or the full names)")
            return
        color, dev_type = parts
        try:
            canonical = self.tracker.devbuy(color, dev_type)
        except TrackerError as e:
            print(f"error: {e}")
            return
        print(f"{color.upper()} bought a {canonical}.")
        self._maybe_render()

    def do_devplay(self, arg: str) -> None:
        """devplay <COLOR> <TYPE> — play a dev card from a color's hand.

        Knight steals / monopoly pulls / year-of-plenty picks / road-building
        placements are manual: use `give`/`take`/`road`/`robber` commands to
        reflect the actual effects on the board."""
        parts = shlex.split(arg)
        if len(parts) != 2:
            print("usage: devplay <COLOR> <TYPE>  "
                  "(k | mono | yop | road | vp — or the full names)")
            return
        color, dev_type = parts
        try:
            canonical = self.tracker.devplay(color, dev_type)
        except TrackerError as e:
            print(f"error: {e}")
            return
        print(f"{color.upper()} played {canonical}.")
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

    def do_secondadvice(self, arg: str) -> None:
        """secondadvice <COLOR> [first_node] — rank legal second-settlement picks.

        If first_node is omitted, uses COLOR's single existing settlement
        (errors if they have 0 or 2+). Scoring favors nodes that fill in
        resources the first settlement lacks — complement value dominates
        raw pip count, which is usually the right instinct in the opening."""
        parts = shlex.split(arg)
        if not parts or len(parts) > 2:
            print("usage: secondadvice <COLOR> [first_node]")
            return
        color = parts[0]
        try:
            c = self.tracker._color(color)
        except TrackerError as e:
            print(f"error: {e}")
            return

        first_node: int | None = None
        if len(parts) == 2:
            try:
                first_node = int(parts[1])
            except ValueError:
                print(f"error: first_node must be an integer, got {parts[1]!r}")
                return
        else:
            # Find the color's single settlement.
            own = [nid for nid, (bc, kind) in
                   self.tracker.game.state.board.buildings.items()
                   if bc == c and kind == "SETTLEMENT"]
            if len(own) == 0:
                print(f"error: {color.upper()} has no settlement yet — "
                      f"place one first or pass the node id explicitly")
                return
            if len(own) > 1:
                print(f"error: {color.upper()} has {len(own)} settlements; "
                      f"pass the intended first node explicitly "
                      f"(candidates: {sorted(own)})")
                return
            first_node = own[0]

        from cataanbot.advisor import (
            score_second_settlements, format_second_settlement_ranking,
        )
        try:
            scores = score_second_settlements(self.tracker.game,
                                              first_node, color)
        except ValueError as e:
            print(f"error: {e}")
            return
        print(format_second_settlement_ranking(scores, first_node, top=10))

    def do_tradeeval(self, arg: str) -> None:
        """tradeeval <COLOR> <N> <RES_OUT> <M> <RES_IN> — is this trade good for COLOR?

        Values each resource inversely to the color's production rate
        (rarer = worth more), with a port-ownership bonus since ports
        let you offload excess. Delta > 0 means the trade helps COLOR.
        Example: `tradeeval RED 2 wheat 1 ore`."""
        parts = shlex.split(arg)
        if len(parts) != 5:
            print("usage: tradeeval <COLOR> <N> <RES_OUT> <M> <RES_IN>")
            return
        color, n_out, res_out, n_in, res_in = parts
        try:
            self.tracker._color(color)
        except TrackerError as e:
            print(f"error: {e}")
            return
        try:
            give_n = int(n_out)
            get_n = int(n_in)
        except ValueError as e:
            print(f"error: amounts must be integers ({e})")
            return
        from cataanbot.advisor import evaluate_trade, format_trade_eval
        try:
            ev = evaluate_trade(self.tracker.game, color,
                                give_n, res_out, get_n, res_in)
        except ValueError as e:
            print(f"error: {e}")
            return
        print(format_trade_eval(ev))

    def do_robberadvice(self, arg: str) -> None:
        """robberadvice <COLOR> [top] — best tiles to park the robber on.

        Scores every land tile by (opponent pips blocked − your pips
        blocked), with tiebreakers on victim hand size. Reads live
        tracker state — so settle/city/roll/give/take first, then ask."""
        parts = shlex.split(arg)
        if not parts or len(parts) > 2:
            print("usage: robberadvice <COLOR> [top]")
            return
        color = parts[0]
        try:
            top = int(parts[1]) if len(parts) == 2 else 8
        except ValueError:
            print(f"error: top must be an integer, got {parts[1]!r}")
            return
        try:
            # Validate color early for a nice error message.
            self.tracker._color(color)
        except TrackerError as e:
            print(f"error: {e}")
            return
        from cataanbot.advisor import score_robber_targets, format_robber_ranking
        scores = score_robber_targets(self.tracker.game, color)
        print(format_robber_ranking(scores, color, top=top))

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
