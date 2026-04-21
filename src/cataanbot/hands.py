"""Per-color hand estimation by replaying tracker history.

The tracker already knows each color's hand exactly — up to the limits of
what you've told it. Our own hand is always accurate because we enter
everything. Opponent hands drift from truth when we don't manually record
their 7-roll discards, robber steals, or opponent-opponent trades.

This module replays the history into accounting buckets (produced by dice,
spent on visible builds/dev buys, received by trades we saw) so you can
reason about *where an opponent's resources came from* rather than just
reading a single hand number. It surfaces the same kind of situational
awareness a real player tracks intuitively: "red's been rolling 6s all
game and hasn't built anything, they must be sitting on a full hand."

For dice-produced counts we reuse the same `yield_resources` call the
tracker itself uses when a roll happens, so the numbers match exactly —
no divergence between what we think was delivered and what actually hit
the player_state.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cataanbot.tracker import Tracker


_RESOURCES_ORDER = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")

# Build costs for auto-accounting of `build` ops (the REPL's auto-debit
# convenience uses these too; duplicated here so hands.py is self-contained).
_BUILD_COSTS = {
    "settle":  {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1},
    "city":    {"WHEAT": 2, "ORE": 3},
    "road":    {"WOOD": 1, "BRICK": 1},
    "dev":     {"SHEEP": 1, "WHEAT": 1, "ORE": 1},
}


def _zero_bucket() -> dict[str, int]:
    return {r: 0 for r in _RESOURCES_ORDER}


def estimate_hands(tracker: "Tracker") -> dict[str, Any]:
    """Replay history and bucket resource flow per color.

    Returns per-color dicts with:
      - `produced`: sum of dice-roll payouts (bank-cap honored via the
        same `yield_resources` the tracker uses)
      - `spent`:    resources observed leaving the hand (take ops,
        build costs from explicit `build` ops, dev-card buys)
      - `received`: resources observed arriving (give ops, trade
        in-direction, bank-trade received)
      - `current`:  what the tracker's game state currently says the
        hand is — authoritative for *your* color, best-effort for others
      - `total_current`: sum of the current bucket
    """
    from catanatron.state import yield_resources, RESOURCES
    from cataanbot.tracker import Tracker, DEFAULT_COLORS

    replay = Tracker(seed=tracker.seed)
    state = replay.game.state
    board = state.board
    m = board.map

    produced: dict[str, dict[str, int]] = {c: _zero_bucket()
                                           for c in DEFAULT_COLORS}
    spent: dict[str, dict[str, int]] = {c: _zero_bucket()
                                        for c in DEFAULT_COLORS}
    received: dict[str, dict[str, int]] = {c: _zero_bucket()
                                           for c in DEFAULT_COLORS}

    def _bump(bucket, color, resource, amount):
        if color in bucket and resource in bucket[color]:
            bucket[color][resource] += amount

    for op in tracker.history:
        name = op["op"]
        args = op["args"]

        # Produced: sample the pre-roll state so bank depletion matches the
        # actual dice distribution the live game saw.
        if name == "roll":
            n = args[0]
            if n != 7:
                payout, _ = yield_resources(
                    board, state.resource_freqdeck, n
                )
                for color, freqdeck in payout.items():
                    for i, r in enumerate(RESOURCES):
                        _bump(produced, color.name, r, freqdeck[i])

        # Manual adjustments.
        if name == "give":
            _bump(received, args[0].upper(), args[2].upper(), int(args[1]))
        elif name == "take":
            _bump(spent, args[0].upper(), args[2].upper(), int(args[1]))

        # Trades: args layout matches Tracker.trade and Tracker.mtrade.
        # Confirmed by reading the trade/mtrade apply methods: (a, n_out,
        # res_out, b, n_in, res_in) for trade and (color, n_out, res_out,
        # res_in) for mtrade with implicit 1:in returned.
        elif name == "trade":
            a, n_out, r_out, b, n_in, r_in = args
            _bump(spent, a.upper(), r_out.upper(), int(n_out))
            _bump(received, a.upper(), r_in.upper(), int(n_in))
            _bump(received, b.upper(), r_out.upper(), int(n_out))
            _bump(spent, b.upper(), r_in.upper(), int(n_in))
        elif name == "mtrade":
            color, n_out, r_out, r_in = args
            _bump(spent, color.upper(), r_out.upper(), int(n_out))
            _bump(received, color.upper(), r_in.upper(), 1)

        # Dev buys cost 1 sheep + 1 wheat + 1 ore.
        elif name == "devbuy":
            color = args[0].upper()
            for r, cost in _BUILD_COSTS["dev"].items():
                _bump(spent, color, r, cost)

        # Now progress the replay so subsequent rolls sample fresh state.
        if name == "settle":
            replay._apply_settle(args[0], args[1])
        elif name == "city":
            replay._apply_city(args[0], args[1])
        elif name == "road":
            replay._apply_road(args[0], args[1], args[2])
        elif name == "robber":
            replay._apply_robber(tuple(args))
        elif name == "roll":
            replay._apply_roll(args[0])
        elif name == "give":
            replay._apply_adjust(args[0], args[1], args[2], sign=+1)
        elif name == "take":
            replay._apply_adjust(args[0], args[1], args[2], sign=-1)
        elif name == "devbuy":
            replay._apply_devbuy(args[0], args[1])
        elif name == "devplay":
            replay._apply_devplay(args[0], args[1])
        elif name == "trade":
            replay._apply_trade(args[0], args[1], args[2],
                                args[3], args[4], args[5])
        elif name == "mtrade":
            replay._apply_mtrade(args[0], args[1], args[2], args[3])

    # Pull the authoritative current hand straight off the tracker state
    # (not the replay) so any values edited via REPL reflect here.
    current: dict[str, dict[str, int]] = {}
    live_state = tracker.game.state
    for color_name in DEFAULT_COLORS:
        try:
            live_color = tracker._color(color_name)
        except Exception:
            continue
        idx = live_state.color_to_index.get(live_color)
        if idx is None:
            continue
        bucket = _zero_bucket()
        for r in _RESOURCES_ORDER:
            bucket[r] = int(live_state.player_state.get(
                f"P{idx}_{r}_IN_HAND", 0
            ))
        current[color_name] = bucket

    per_color: dict[str, dict[str, Any]] = {}
    for color_name in DEFAULT_COLORS:
        cur = current.get(color_name, _zero_bucket())
        per_color[color_name] = {
            "produced": produced[color_name],
            "spent":    spent[color_name],
            "received": received[color_name],
            "current":  cur,
            "total_current": sum(cur.values()),
            "total_produced": sum(produced[color_name].values()),
            "total_spent":    sum(spent[color_name].values()),
            "total_received": sum(received[color_name].values()),
        }
    return per_color


def format_hands(per_color: dict[str, dict[str, Any]]) -> str:
    """Table view: current hand + produced/spent/received summary."""
    lines = [
        "Hand estimates per color "
        "(current = authoritative tracker state):",
        "",
    ]
    res_cols = "".join(f"{r[:3]:>5}" for r in _RESOURCES_ORDER)
    header = (f"  {'color':<7}  {'curr':>4}  {'prod':>4}  {'spent':>5}  "
              f"{'rcvd':>4}  |{res_cols}")
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for color_name, info in per_color.items():
        cur_cells = "".join(f"{info['current'][r]:>5}"
                            for r in _RESOURCES_ORDER)
        lines.append(
            f"  {color_name:<7}  {info['total_current']:>4}  "
            f"{info['total_produced']:>4}  {info['total_spent']:>5}  "
            f"{info['total_received']:>4}  |{cur_cells}"
        )
    lines.append("")
    lines.append("  curr = current hand from tracker state")
    lines.append("  prod = resources dice-delivered this game")
    lines.append("  spent/rcvd = take/give + trades + observed build costs")
    return "\n".join(lines)
