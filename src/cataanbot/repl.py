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

_TURN_ORDER = ("RED", "BLUE", "WHITE", "ORANGE")

# Standard Catan build costs. Tracker stays mirror-not-referee, so these
# only fire via the convenience `build` / `devbuy!` commands — direct
# `settle` / `city` / `road` / `devbuy` remain free, matching the rest of
# the commands' "if the user says it, we record it" stance.
_BUILD_COSTS = {
    "settle":   {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1},
    "city":     {"WHEAT": 2, "ORE": 3},
    "road":     {"WOOD": 1, "BRICK": 1},
    "dev":      {"SHEEP": 1, "WHEAT": 1, "ORE": 1},
}


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
        self.turn_idx: int = 0  # index into _TURN_ORDER; shown in the prompt
        self._refresh_prompt()

    # --- command handlers -------------------------------------------------
    def do_new(self, _arg: str) -> None:
        """new — reset to a fresh random board."""
        self.tracker.reset()
        print("fresh board.")
        self._maybe_render()

    def do_settle(self, arg: str) -> None:
        """settle <COLOR> <node> — place a settlement at node id.

        If this is the color's FIRST settlement on the board, automatically
        offers a short second-settlement recommendation right after so the
        user doesn't have to re-invoke the advisor manually during draft."""
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
        self._maybe_offer_secondadvice(color.upper(), int(node))

    def _maybe_offer_secondadvice(self, color_name: str, first_node: int) -> None:
        """When `color_name` has placed exactly one settlement, surface a
        short top-5 second-settlement ranking. Silent otherwise and
        silently swallows any advisor failure — this is a convenience
        layer, not a checkpoint."""
        try:
            c = self.tracker._color(color_name)
        except TrackerError:
            return
        own_settlements = [
            nid for nid, (bc, kind)
            in self.tracker.game.state.board.buildings.items()
            if bc == c and kind == "SETTLEMENT"
        ]
        if len(own_settlements) != 1:
            return
        try:
            from cataanbot.advisor import (
                score_second_settlements, format_second_settlement_ranking,
            )
            scores = score_second_settlements(
                self.tracker.game, first_node, color_name
            )
        except Exception:
            return
        print()
        print(f"-- auto: top 5 second-settlement picks for {color_name} --")
        print(format_second_settlement_ranking(scores, first_node, top=5))

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

    def do_discard(self, arg: str) -> None:
        """discard <COLOR> <N1> <RES1> [<N2> <RES2> ...] — 7-roll discard wrapper.

        Verifies the whole stack up front, then debits atomically. Under
        the hood this is just sequenced `take` ops so undo/save/replay
        handle it naturally.
        Example: `discard RED 2 wheat 1 ore` discards 2 wheat + 1 ore."""
        parts = shlex.split(arg)
        if len(parts) < 3 or (len(parts) - 1) % 2 != 0:
            print("usage: discard <COLOR> <N1> <RES1> [<N2> <RES2> ...]")
            return
        color = parts[0]
        pairs: list[tuple[int, str]] = []
        try:
            for i in range(1, len(parts), 2):
                pairs.append((int(parts[i]), parts[i + 1]))
        except ValueError as e:
            print(f"error: {e}")
            return
        try:
            self.tracker._color(color)
        except TrackerError as e:
            print(f"error: {e}")
            return
        # Atomicity: pre-validate every resource before touching the hand.
        # Handles duplicates too (discarding 2 wheat + 1 wheat must check 3).
        need: dict[str, int] = {}
        for n, r in pairs:
            if n < 0:
                print("error: amounts must be non-negative")
                return
            need[r.upper()] = need.get(r.upper(), 0) + n
        try:
            for r, total in need.items():
                self.tracker._require_hand(color, total, r)
        except TrackerError as e:
            print(f"error: {e}")
            return
        for n, r in pairs:
            if n == 0:
                continue
            self.tracker.take(color, n, r)
        summary = ", ".join(f"-{n} {r.lower()}" for n, r in pairs if n)
        print(f"{color.upper()} discarded {summary}.")
        self._maybe_render()

    def do_build(self, arg: str) -> None:
        """build <COLOR> settle|city|road|dev <ARGS> — place and auto-debit cost.

        Mirror-not-referee still applies: costs come out of the color's
        hand, but no turn check and no bank-depletion rules. Shortcuts
        for when you don't want to `take` the cost manually.
          build RED settle 17
          build RED city   17
          build RED road   16 17
          build RED dev             (draws a random dev-deck card)
        For ambiguous cases (e.g. you want to buy a specific dev type
        without the cost), keep using the primitive `settle/city/road/
        devbuy` commands."""
        parts = shlex.split(arg)
        if len(parts) < 2:
            print("usage: build <COLOR> settle|city|road|dev <ARGS>")
            return
        color, kind, *rest = parts
        kind = kind.lower()
        if kind not in _BUILD_COSTS:
            print(f"error: unknown build kind {kind!r}; "
                  f"use one of {', '.join(_BUILD_COSTS)}")
            return
        try:
            self.tracker._color(color)
        except TrackerError as e:
            print(f"error: {e}")
            return
        cost = _BUILD_COSTS[kind]
        try:
            for r, n in cost.items():
                self.tracker._require_hand(color, n, r)
        except TrackerError as e:
            print(f"error: {e}")
            return

        try:
            if kind == "settle":
                if len(rest) != 1:
                    print("usage: build <COLOR> settle <node>")
                    return
                self.tracker.settle(color, int(rest[0]))
            elif kind == "city":
                if len(rest) != 1:
                    print("usage: build <COLOR> city <node>")
                    return
                self.tracker.city(color, int(rest[0]))
            elif kind == "road":
                if len(rest) != 2:
                    print("usage: build <COLOR> road <node_a> <node_b>")
                    return
                self.tracker.road(color, int(rest[0]), int(rest[1]))
            elif kind == "dev":
                if rest:
                    # User can still name the specific type if known.
                    self.tracker.devbuy(color, rest[0])
                else:
                    deck = self.tracker.game.state.development_listdeck
                    if not deck:
                        print("error: dev-card deck is empty")
                        return
                    self.tracker.devbuy(color, deck[0])
        except (TrackerError, ValueError) as e:
            print(f"error: {e}  (nothing debited)")
            return

        # Build succeeded; now debit the cost.
        for r, n in cost.items():
            self.tracker.take(color, n, r)
        cost_str = " + ".join(f"{n} {r.lower()}" for r, n in cost.items())
        print(f"{color.upper()} built {kind} (-{cost_str}).")
        self._maybe_render()

    def do_turn(self, arg: str) -> None:
        """turn [next|COLOR] — show or advance the turn pointer.

        This is purely informational — nothing in the tracker enforces
        turns. The pointer just appears in the prompt so you can glance
        down and know whose action you're about to record.
          turn            — print current turn
          turn next       — advance to the next color in RED→BLUE→WHITE→ORANGE
          turn RED        — jump to a specific color"""
        arg = arg.strip()
        if not arg:
            print(f"turn: {_TURN_ORDER[self.turn_idx]}")
            return
        if arg.lower() == "next":
            self.turn_idx = (self.turn_idx + 1) % len(_TURN_ORDER)
        else:
            try:
                self.turn_idx = _TURN_ORDER.index(arg.upper())
            except ValueError:
                print(f"error: unknown color {arg!r}; use next or "
                      f"{', '.join(_TURN_ORDER)}")
                return
        self._refresh_prompt()
        print(f"turn: {_TURN_ORDER[self.turn_idx]}")

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

    def do_stats(self, arg: str) -> None:
        """stats [path.png] — dice-roll histogram + delivered-resources summary.

        Replays history to tally roll frequencies, per-color resources
        actually delivered by dice, and per-tile production counts. If
        an argument is given, also writes a PNG histogram to that path."""
        path = arg.strip()
        from cataanbot.stats import compute_stats, format_stats, render_histogram
        stats = compute_stats(self.tracker)
        print(format_stats(stats))
        if path:
            try:
                out = render_histogram(stats, path)
            except Exception as e:
                print(f"(histogram write failed: {e})")
                return
            print(f"\nwrote {out}")

    def do_hands(self, _arg: str) -> None:
        """hands — per-color resource accounting.

        Shows current hand counts (authoritative for the tracker), plus
        totals for dice-produced, observed spends, and observed receives.
        Useful for quickly eyeballing who's sitting on a big hand vs who
        just spent everything."""
        from cataanbot.hands import estimate_hands, format_hands
        print(format_hands(estimate_hands(self.tracker)))

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
        """show — print a text summary of tracked state.

        Appends a compact two-line dice-roll histogram when rolls have
        been recorded, so you can eyeball luck without running `stats`."""
        print(self.tracker.summary())
        if any(op["op"] == "roll" for op in self.tracker.history):
            from cataanbot.stats import compute_stats, format_mini_histogram
            print()
            print(format_mini_histogram(compute_stats(self.tracker)))

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

    def _refresh_prompt(self) -> None:
        """Embed the current turn color in the prompt for at-a-glance state."""
        self.prompt = f"catan [{_TURN_ORDER[self.turn_idx]}]> "

    def emptyline(self) -> None:
        # don't repeat the last command on empty input.
        pass


def run() -> int:
    TrackerRepl().cmdloop()
    return 0
