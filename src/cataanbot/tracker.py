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
_RESOURCE_NAMES = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")

# Canonical dev card types matching catanatron's player_state keys.
_DEV_CARD_TYPES = (
    "KNIGHT",
    "MONOPOLY",
    "YEAR_OF_PLENTY",
    "ROAD_BUILDING",
    "VICTORY_POINT",
)

# Short aliases accepted in REPL commands.
_DEV_CARD_ALIASES = {
    "KNIGHT": "KNIGHT",
    "K": "KNIGHT",
    "MONO": "MONOPOLY",
    "MONOPOLY": "MONOPOLY",
    "YOP": "YEAR_OF_PLENTY",
    "YEAR_OF_PLENTY": "YEAR_OF_PLENTY",
    "ROAD": "ROAD_BUILDING",
    "ROAD_BUILDING": "ROAD_BUILDING",
    "VP": "VICTORY_POINT",
    "VICTORY_POINT": "VICTORY_POINT",
}

# Bump if the on-disk save format changes in a breaking way.
SAVE_FORMAT_VERSION = 1


def _resolve_dev_type(raw: str) -> str:
    """Accept a short alias or full name; return the canonical type."""
    key = raw.upper().replace("-", "_")
    if key not in _DEV_CARD_ALIASES:
        allowed = ", ".join(sorted(set(_DEV_CARD_ALIASES)))
        raise TrackerError(
            f"unknown dev-card type {raw!r}; try one of: {allowed}"
        )
    return _DEV_CARD_ALIASES[key]


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
        # Opponent settlements can break a longest road — recompute always.
        self._recompute_longest_road()
        self._recompute_vp()

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
        self._recompute_vp()

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
        self._recompute_longest_road()
        self._recompute_vp()

    def _recompute_longest_road(self) -> None:
        """Refresh `LONGEST_ROAD_LENGTH` per color and reassign `HAS_ROAD`.

        Uses catanatron's `continuous_roads_by_player` for the per-color
        longest path; requires length ≥ 5 to qualify. The current holder
        keeps the card unless another color strictly exceeds them —
        matches the standard Catan rule."""
        state = self.game.state
        board = state.board
        lengths: dict = {}
        for color, idx in state.color_to_index.items():
            try:
                paths = board.continuous_roads_by_player(color)
            except Exception:
                paths = []
            length = max((len(p) for p in paths), default=0)
            lengths[color] = length
            state.player_state[f"P{idx}_LONGEST_ROAD_LENGTH"] = length

        current_holder = None
        for color, idx in state.color_to_index.items():
            if state.player_state.get(f"P{idx}_HAS_ROAD"):
                current_holder = color
                break

        eligible = [(c, lengths[c]) for c, _ in lengths.items()
                    if lengths[c] >= 5]
        if not eligible:
            new_holder = None
        else:
            best_color, best_len = max(eligible, key=lambda kv: kv[1])
            if (current_holder is not None
                    and lengths.get(current_holder, 0) >= 5
                    and best_len <= lengths[current_holder]):
                new_holder = current_holder
            else:
                new_holder = best_color

        if new_holder is not current_holder:
            if current_holder is not None:
                idx = state.color_to_index[current_holder]
                state.player_state[f"P{idx}_HAS_ROAD"] = False
            if new_holder is not None:
                idx = state.color_to_index[new_holder]
                state.player_state[f"P{idx}_HAS_ROAD"] = True

    def _recompute_largest_army(self) -> None:
        """Refresh `HAS_ARMY` based on PLAYED_KNIGHT counts.

        Threshold is 3 played knights; holder keeps the card until another
        color strictly exceeds them."""
        state = self.game.state
        played: dict = {}
        for color, idx in state.color_to_index.items():
            played[color] = int(
                state.player_state.get(f"P{idx}_PLAYED_KNIGHT", 0)
            )

        current_holder = None
        for color, idx in state.color_to_index.items():
            if state.player_state.get(f"P{idx}_HAS_ARMY"):
                current_holder = color
                break

        eligible = [(c, played[c]) for c in played if played[c] >= 3]
        if not eligible:
            new_holder = None
        else:
            best_color, best_n = max(eligible, key=lambda kv: kv[1])
            if (current_holder is not None
                    and played.get(current_holder, 0) >= 3
                    and best_n <= played[current_holder]):
                new_holder = current_holder
            else:
                new_holder = best_color

        if new_holder is not current_holder:
            if current_holder is not None:
                idx = state.color_to_index[current_holder]
                state.player_state[f"P{idx}_HAS_ARMY"] = False
            if new_holder is not None:
                idx = state.color_to_index[new_holder]
                state.player_state[f"P{idx}_HAS_ARMY"] = True

    def _recompute_vp(self) -> None:
        """Recompute VICTORY_POINTS keys from current board state.

        catanatron only refreshes `P{i}_VICTORY_POINTS` inside its
        tick-based play loop, which the tracker bypasses entirely. Without
        this, VP stays at 0 after every direct `build_settlement`/`build_city`
        call, which breaks both the legend strip and the robber advisor's
        VP weighting. We recompute from visible sources only: buildings plus
        HAS_ROAD / HAS_ARMY. Hidden VP cards are intentionally excluded
        since opponents shouldn't see them."""
        state = self.game.state
        board = state.board
        s_count: dict[Any, int] = {}
        c_count: dict[Any, int] = {}
        for _nid, (color, kind) in board.buildings.items():
            if kind == "CITY":
                c_count[color] = c_count.get(color, 0) + 1
            else:
                s_count[color] = s_count.get(color, 0) + 1
        for color, idx in state.color_to_index.items():
            vp = s_count.get(color, 0) + 2 * c_count.get(color, 0)
            if bool(state.player_state.get(f"P{idx}_HAS_ROAD", False)):
                vp += 2
            if bool(state.player_state.get(f"P{idx}_HAS_ARMY", False)):
                vp += 2
            state.player_state[f"P{idx}_VICTORY_POINTS"] = vp

    def move_robber(self, coord: tuple[int, int, int]) -> None:
        self._apply_robber(coord)
        self.history.append({"op": "robber", "args": list(coord)})

    def _apply_robber(self, coord: tuple[int, int, int]) -> None:
        if coord not in self.game.state.board.map.land_tiles:
            raise TrackerError(f"no land tile at {coord}")
        self.game.state.board.robber_coordinate = coord

    # --- dice rolls ------------------------------------------------------
    def roll(self, number: int) -> dict[str, dict[str, int]]:
        """Record that `number` was rolled. Returns per-color payout dict.

        Distributes resources via catanatron's `yield_resources` — which
        already honors the robber — directly into the player_state hand
        counters. Does NOT advance turn state or set HAS_ROLLED, since the
        tracker deliberately ignores turn order."""
        payout = self._apply_roll(number)
        self.history.append({"op": "roll", "args": [number]})
        return payout

    def _apply_roll(self, number: int) -> dict[str, dict[str, int]]:
        if not 2 <= number <= 12:
            raise TrackerError(f"dice sum {number} is not in 2..12")
        state = self.game.state
        if number == 7:
            # No distribution on 7. The robber move is a separate command.
            return {}

        from catanatron.state import yield_resources, RESOURCES
        payout, _depleted = yield_resources(
            state.board, state.resource_freqdeck, number
        )

        result: dict[str, dict[str, int]] = {}
        for color, freqdeck in payout.items():
            idx = state.color_to_index[color]
            hand_delta: dict[str, int] = {}
            for res_idx, resource in enumerate(RESOURCES):
                amount = freqdeck[res_idx]
                if amount == 0:
                    continue
                key = f"P{idx}_{resource}_IN_HAND"
                state.player_state[key] = state.player_state.get(key, 0) + amount
                hand_delta[resource] = amount
                # Subtract from the bank so depletion stays accurate.
                state.resource_freqdeck[res_idx] -= amount
            if hand_delta:
                result[color.name] = hand_delta
        return result

    # --- manual resource adjustments ------------------------------------
    def give(self, color: str, amount: int, resource: str) -> None:
        """Add `amount` of `resource` to a color's hand (e.g. from a trade)."""
        self._apply_adjust(color, amount, resource, sign=+1)
        self.history.append({
            "op": "give", "args": [color.upper(), int(amount), resource.upper()]
        })

    def take(self, color: str, amount: int, resource: str) -> None:
        """Remove `amount` of `resource` from a color's hand (build cost,
        trade, knight steal, discard, etc.)."""
        self._apply_adjust(color, amount, resource, sign=-1)
        self.history.append({
            "op": "take", "args": [color.upper(), int(amount), resource.upper()]
        })

    def _apply_adjust(self, color: str, amount: int, resource: str,
                      sign: int) -> None:
        if amount < 0:
            raise TrackerError("amount must be non-negative; "
                               "use `take` to remove and `give` to add")
        resource = resource.upper()
        if resource not in _RESOURCE_NAMES:
            raise TrackerError(
                f"unknown resource {resource!r}; use one of "
                f"{', '.join(_RESOURCE_NAMES)}"
            )
        state = self.game.state
        idx = state.color_to_index[self._color(color)]
        key = f"P{idx}_{resource}_IN_HAND"
        current = int(state.player_state.get(key, 0))
        new_val = current + sign * amount
        if new_val < 0:
            raise TrackerError(
                f"{color.upper()} only has {current} {resource}; "
                f"can't take {amount}"
            )
        state.player_state[key] = new_val
        # Keep the bank's freqdeck consistent with player totals.
        res_idx = _RESOURCE_NAMES.index(resource)
        state.resource_freqdeck[res_idx] -= sign * amount

    # --- dev cards -------------------------------------------------------
    def devbuy(self, color: str, dev_type: str) -> str:
        """Give a color one dev card of `dev_type`. Returns the canonical type.

        Does NOT auto-debit the wheat/sheep/ore cost — pair with `take` if
        you want hand totals to stay honest."""
        canonical = _resolve_dev_type(dev_type)
        self._apply_devbuy(color, canonical)
        self.history.append({"op": "devbuy", "args": [color.upper(), canonical]})
        return canonical

    def _apply_devbuy(self, color: str, dev_type: str) -> None:
        state = self.game.state
        idx = state.color_to_index[self._color(color)]
        key = f"P{idx}_{dev_type}_IN_HAND"
        state.player_state[key] = int(state.player_state.get(key, 0)) + 1
        # Remove one matching card from the remaining deck if any are left,
        # so renderings / future advisors know what's been drawn.
        deck = state.development_listdeck
        for i, card in enumerate(deck):
            if card == dev_type:
                deck.pop(i)
                return
        # Deck is out of that type; not a hard error since the user might
        # be reconstructing a game or running an unusual variant.

    def devplay(self, color: str, dev_type: str) -> str:
        """Move a dev card from a color's hand to their "played" column."""
        canonical = _resolve_dev_type(dev_type)
        self._apply_devplay(color, canonical)
        self.history.append({"op": "devplay", "args": [color.upper(), canonical]})
        return canonical

    def _apply_devplay(self, color: str, dev_type: str) -> None:
        state = self.game.state
        idx = state.color_to_index[self._color(color)]
        in_hand_key = f"P{idx}_{dev_type}_IN_HAND"
        played_key = f"P{idx}_PLAYED_{dev_type}"
        current = int(state.player_state.get(in_hand_key, 0))
        if current <= 0:
            raise TrackerError(
                f"{color.upper()} has no {dev_type} card in hand to play"
            )
        state.player_state[in_hand_key] = current - 1
        state.player_state[played_key] = (
            int(state.player_state.get(played_key, 0)) + 1
        )
        if dev_type == "KNIGHT":
            self._recompute_largest_army()
            self._recompute_vp()

    def dev_counts(self, color: str) -> dict[str, dict[str, int]]:
        """Return {type: {'hand': n, 'played': n}} for one color."""
        state = self.game.state
        idx = state.color_to_index[self._color(color)]
        out: dict[str, dict[str, int]] = {}
        for t in _DEV_CARD_TYPES:
            out[t] = {
                "hand": int(state.player_state.get(f"P{idx}_{t}_IN_HAND", 0)),
                "played": int(state.player_state.get(f"P{idx}_PLAYED_{t}", 0)),
            }
        return out

    # --- trades ----------------------------------------------------------
    def trade(self, color_a: str, amt_a: int, res_a: str,
              color_b: str, amt_b: int, res_b: str) -> None:
        """Atomic player-to-player trade.

        A gives `amt_a res_a` to B; B gives `amt_b res_b` to A. Validated
        up front so a mid-trade failure can't leave hands half-moved."""
        self._apply_trade(color_a, amt_a, res_a, color_b, amt_b, res_b)
        self.history.append({
            "op": "trade",
            "args": [color_a.upper(), int(amt_a), res_a.upper(),
                     color_b.upper(), int(amt_b), res_b.upper()],
        })

    def _apply_trade(self, color_a: str, amt_a: int, res_a: str,
                     color_b: str, amt_b: int, res_b: str) -> None:
        # Pre-check both hands so nothing moves if either side is short.
        self._require_hand(color_a, amt_a, res_a)
        self._require_hand(color_b, amt_b, res_b)
        self._apply_adjust(color_a, amt_a, res_a, sign=-1)
        self._apply_adjust(color_b, amt_b, res_b, sign=-1)
        self._apply_adjust(color_a, amt_b, res_b, sign=+1)
        self._apply_adjust(color_b, amt_a, res_a, sign=+1)

    def mtrade(self, color: str, amt_out: int, res_out: str,
               res_in: str) -> None:
        """Maritime trade: spend `amt_out res_out` for 1 `res_in` from the bank.

        The caller picks the rate (4/3/2) by passing whatever `amt_out` the
        actual port/bank allows. Tracker doesn't enforce port eligibility —
        it mirrors, doesn't referee."""
        self._apply_mtrade(color, amt_out, res_out, res_in)
        self.history.append({
            "op": "mtrade",
            "args": [color.upper(), int(amt_out),
                     res_out.upper(), res_in.upper()],
        })

    def _apply_mtrade(self, color: str, amt_out: int, res_out: str,
                      res_in: str) -> None:
        self._require_hand(color, amt_out, res_out)
        self._apply_adjust(color, amt_out, res_out, sign=-1)
        self._apply_adjust(color, 1, res_in, sign=+1)

    def _require_hand(self, color: str, amount: int, resource: str) -> None:
        resource = resource.upper()
        if resource not in _RESOURCE_NAMES:
            raise TrackerError(
                f"unknown resource {resource!r}; use one of "
                f"{', '.join(_RESOURCE_NAMES)}"
            )
        if amount < 0:
            raise TrackerError("amount must be non-negative")
        have = self.hand(color).get(resource, 0)
        if have < amount:
            raise TrackerError(
                f"{color.upper()} only has {have} {resource}; "
                f"can't move {amount}"
            )

    def hand(self, color: str) -> dict[str, int]:
        """Return the given color's current resource hand."""
        from catanatron.state import RESOURCES
        state = self.game.state
        c = self._color(color)
        idx = state.color_to_index[c]
        return {
            r: int(state.player_state.get(f"P{idx}_{r}_IN_HAND", 0))
            for r in RESOURCES
        }

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
            elif name == "roll":
                self._apply_roll(args[0])
            elif name == "give":
                self._apply_adjust(args[0], args[1], args[2], sign=+1)
            elif name == "take":
                self._apply_adjust(args[0], args[1], args[2], sign=-1)
            elif name == "devbuy":
                self._apply_devbuy(args[0], args[1])
            elif name == "devplay":
                self._apply_devplay(args[0], args[1])
            elif name == "trade":
                self._apply_trade(args[0], args[1], args[2],
                                  args[3], args[4], args[5])
            elif name == "mtrade":
                self._apply_mtrade(args[0], args[1], args[2], args[3])
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

    def vp_status(self) -> dict[str, Any]:
        """Snapshot of public VP per color plus a callout for near-winners.

        `callout` is one of: "winner", "one_away", "two_away", "leader",
        or None when the race is still wide open (all < 6 VP).
        `leaders` is the list of color names tied for the top score."""
        state = self.game.state
        per_color: dict[str, int] = {}
        for color_name in DEFAULT_COLORS:
            idx = state.color_to_index.get(self._color(color_name))
            if idx is None:
                continue
            per_color[color_name] = int(
                state.player_state.get(f"P{idx}_VICTORY_POINTS", 0)
            )
        if not per_color:
            return {"per_color": {}, "leaders": [], "top": 0, "callout": None}
        top = max(per_color.values())
        leaders = [c for c, v in per_color.items() if v == top]
        if top >= 10:
            callout = "winner"
        elif top >= 9:
            callout = "one_away"
        elif top >= 8:
            callout = "two_away"
        elif top >= 6:
            callout = "leader"
        else:
            callout = None
        return {"per_color": per_color, "leaders": leaders,
                "top": top, "callout": callout}

    def vp_callout_line(self) -> str | None:
        """Single-line human-readable form of `vp_status()` for summaries.
        Returns None when no callout applies (early game)."""
        status = self.vp_status()
        callout = status["callout"]
        if callout is None:
            return None
        who = "/".join(status["leaders"])
        top = status["top"]
        if callout == "winner":
            return f"*** {who} at {top} VP — GAME OVER ***"
        if callout == "one_away":
            return f"!! {who} at {top} VP — one turn from winning !!"
        if callout == "two_away":
            return f"{who} at {top} VP — two from winning"
        return f"Leader: {who} at {top} VP"

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

        lines = []
        callout = self.vp_callout_line()
        if callout:
            lines.append(callout)
            lines.append("")
        lines.append(f"seed: {self.seed}   history ops: {len(self.history)}")
        lines.append(f"robber: {board.robber_coordinate}")
        res_cols = "".join(f"{r[:3]:>4}" for r in _RESOURCE_NAMES)
        header = (f"{'color':<7} {'settle':>6} {'city':>4} {'road':>4} "
                  f"|{res_cols}  tot")
        lines.append(header)
        lines.append("-" * len(header))
        for color_name in DEFAULT_COLORS:
            stats = by_color.get(color_name, {"SETTLEMENT": 0, "CITY": 0})
            hand = self.hand(color_name)
            hand_cols = "".join(f"{hand[r]:>4}" for r in _RESOURCE_NAMES)
            total = sum(hand.values())
            lines.append(
                f"{color_name:<7} {stats['SETTLEMENT']:>6} "
                f"{stats['CITY']:>4} {road_counts.get(color_name, 0):>4} "
                f"|{hand_cols}  {total:>3}"
            )

        # Dev-card table: only show colors that have any dev-card activity.
        dev_header_labels = {
            "KNIGHT": "knt", "MONOPOLY": "mno", "YEAR_OF_PLENTY": "yop",
            "ROAD_BUILDING": "rb", "VICTORY_POINT": "vp",
        }
        any_dev_activity = False
        dev_rows = []
        for color_name in DEFAULT_COLORS:
            counts = self.dev_counts(color_name)
            if any(v["hand"] or v["played"] for v in counts.values()):
                any_dev_activity = True
            cells = []
            for t in _DEV_CARD_TYPES:
                c = counts[t]
                cells.append(f"{c['hand']}/{c['played']}")
            dev_rows.append((color_name, cells))
        if any_dev_activity:
            lines.append("")
            dev_header = (
                f"{'color':<7} "
                + " ".join(f"{dev_header_labels[t]:>5}" for t in _DEV_CARD_TYPES)
                + "   (hand/played)"
            )
            lines.append(dev_header)
            lines.append("-" * len(dev_header))
            for name, cells in dev_rows:
                lines.append(
                    f"{name:<7} " + " ".join(f"{c:>5}" for c in cells)
                )
        return "\n".join(lines)
