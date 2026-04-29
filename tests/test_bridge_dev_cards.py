"""Self dev-card holdings tracking + just-bought delay (HUD principle
#7 follow-up).

Catanatron's *_IN_HAND counters never increment for self because the
DOM-log dev-card-buy line hides the card type. The bridge tracks
holdings as an aggregate count so the play-timing hints can fire even
when the type-specific counter is zero. Plus the "just bought this
turn can't play" rule that catanatron doesn't model — Catan's actual
no-play-on-buy-turn restriction.
"""
from __future__ import annotations

from types import SimpleNamespace

from cataanbot.bridge import (
    _maybe_clear_dev_just_bought,
    _track_overlay_state,
)
from cataanbot.events import DevCardBuyEvent, DevCardPlayEvent
from cataanbot.live import DispatchResult


def _make_state(*, self_name="Noah", opp_name="Bob",
                cur_cid=1, last_cid=None):
    """Smallest st dict the dev-card overlay hooks read.

    session.self_color_id + .player_names is what _is_self_player checks;
    current_turn_color_id drives the just-bought-this-turn carve-out
    via _maybe_clear_dev_just_bought.
    """
    sess = SimpleNamespace(
        self_color_id=1,
        player_names={1: self_name, 2: opp_name},
        current_turn_color_id=cur_cid,
    )
    game = SimpleNamespace(session=sess)
    return {
        "game": game,
        "dev_cards_held": 0,
        "dev_cards_bought_this_turn": 0,
        "_last_turn_cid": last_cid,
    }


def _dispatch(ev, status="applied"):
    return DispatchResult(event=ev, status=status, message="test")


def test_self_buy_increments_held_and_bought_this_turn():
    st = _make_state()
    ev = DevCardBuyEvent(player="Noah")
    _track_overlay_state(st, [_dispatch(ev)])
    assert st["dev_cards_held"] == 1
    assert st["dev_cards_bought_this_turn"] == 1


def test_opponent_buy_does_not_count():
    # Self-only tracking — opp buys must not pollute self's count.
    st = _make_state()
    ev = DevCardBuyEvent(player="Bob")
    _track_overlay_state(st, [_dispatch(ev)])
    assert st["dev_cards_held"] == 0
    assert st["dev_cards_bought_this_turn"] == 0


def test_self_play_decrements_held_only():
    # bought_this_turn doesn't decrement on play — it only resets on
    # turn flip. Otherwise a buy + play in the same turn would zero the
    # carve-out and re-enable the next play (which Catan forbids).
    st = _make_state()
    st["dev_cards_held"] = 2
    st["dev_cards_bought_this_turn"] = 1
    ev = DevCardPlayEvent(player="Noah", card="knight")
    _track_overlay_state(st, [_dispatch(ev)])
    assert st["dev_cards_held"] == 1
    assert st["dev_cards_bought_this_turn"] == 1


def test_opponent_play_does_not_decrement_self():
    st = _make_state()
    st["dev_cards_held"] = 2
    ev = DevCardPlayEvent(player="Bob", card="knight")
    _track_overlay_state(st, [_dispatch(ev)])
    assert st["dev_cards_held"] == 2


def test_play_floors_held_at_zero():
    # Defensive: if the bridge missed a buy event but saw a play
    # (rare, but DOM-log virtualization can drop lines), we must not
    # let held go negative.
    st = _make_state()
    st["dev_cards_held"] = 0
    ev = DevCardPlayEvent(player="Noah", card="knight")
    _track_overlay_state(st, [_dispatch(ev)])
    assert st["dev_cards_held"] == 0


def test_just_bought_clears_on_self_to_opp_turn_flip():
    # Self bought a card on their turn (cid=1, self). When colonist's
    # current_turn_color_id flips to opp (cid=2), the carve-out clears
    # and the card becomes playable. _last_turn_cid must update so we
    # don't re-clear on subsequent polls within the same turn.
    st = _make_state(cur_cid=2, last_cid=1)
    st["dev_cards_held"] = 1
    st["dev_cards_bought_this_turn"] = 1
    _maybe_clear_dev_just_bought(st)
    assert st["dev_cards_bought_this_turn"] == 0
    assert st["_last_turn_cid"] == 2


def test_just_bought_does_not_clear_within_self_turn():
    # Still self's turn — the carve-out should not clear yet.
    st = _make_state(cur_cid=1, last_cid=1)
    st["dev_cards_bought_this_turn"] = 1
    _maybe_clear_dev_just_bought(st)
    assert st["dev_cards_bought_this_turn"] == 1


def test_just_bought_does_not_clear_on_opp_to_opp_flip():
    # Opp1 → Opp2. Self never had the turn, nothing to clear, but
    # _last_turn_cid still updates so the next opp→self transition
    # latches correctly.
    st = _make_state(cur_cid=3, last_cid=2)
    sess = st["game"].session
    sess.player_names[3] = "Charlie"
    st["dev_cards_bought_this_turn"] = 0
    _maybe_clear_dev_just_bought(st)
    assert st["dev_cards_bought_this_turn"] == 0
    assert st["_last_turn_cid"] == 3


def test_rb_hint_fires_on_playable_count_when_in_hand_zero():
    """The four dev-card hints all need to surface even when
    catanatron's *_IN_HAND counters stay at 0 for self (which they
    always do — the buy handler can't see card type from colonist's
    DOM log). playable_count is the overlay-tracked aggregate that
    gates them instead.

    rb_hint is the lightest of the four to test in isolation —
    knight/monopoly hints pull through robber-snapshot and
    opp-hand-tracker code paths that need a fully-booted LiveGame
    fixture. Their gate logic is identical so this single rb test
    covers the contract.
    """
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.bridge import _compute_rb_hint
    from cataanbot.tracker import Tracker

    g = Game([RandomPlayer(c) for c in (
        Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE)], seed=1)
    # Plant one RED settlement so the rb-hint placement search has a
    # network to extend from.
    g.state.board.build_settlement(
        Color.RED, 0, initial_build_phase=True)
    g.state.board.build_road(Color.RED, (0, 1))
    tr = Tracker()
    tr.game = g
    game_wrapper = SimpleNamespace(tracker=tr)
    out = _compute_rb_hint(game_wrapper, "RED", playable_count=1)
    assert out is not None
    assert out["have"] == 1


def test_rb_hint_returns_none_when_neither_signal_says_held():
    # Both type-specific counter (catanatron) and aggregate
    # (overlay) say 0 → hint silent. The previous-behaviour
    # contract: a player who doesn't hold the card sees nothing.
    from catanatron import Color, Game, RandomPlayer
    from cataanbot.bridge import _compute_rb_hint
    from cataanbot.tracker import Tracker

    g = Game([RandomPlayer(c) for c in (
        Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE)], seed=1)
    g.state.board.build_settlement(
        Color.RED, 0, initial_build_phase=True)
    g.state.board.build_road(Color.RED, (0, 1))
    tr = Tracker()
    tr.game = g
    out = _compute_rb_hint(
        SimpleNamespace(tracker=tr), "RED", playable_count=0)
    assert out is None


def test_full_buy_play_cycle_across_turns():
    # End-to-end: buy on self's turn → can't play yet → turn flips →
    # carve-out clears → card becomes playable.
    st = _make_state(cur_cid=1, last_cid=1)
    # Buy on self's turn
    _track_overlay_state(st, [_dispatch(DevCardBuyEvent(player="Noah"))])
    assert st["dev_cards_held"] == 1
    assert st["dev_cards_bought_this_turn"] == 1
    playable = st["dev_cards_held"] - st["dev_cards_bought_this_turn"]
    assert playable == 0

    # Turn flips to opp
    sess = st["game"].session
    sess.current_turn_color_id = 2
    _maybe_clear_dev_just_bought(st)
    playable = st["dev_cards_held"] - st["dev_cards_bought_this_turn"]
    assert playable == 1

    # Self's turn comes back around — still playable, plays it
    sess.current_turn_color_id = 1
    _maybe_clear_dev_just_bought(st)
    _track_overlay_state(st, [_dispatch(
        DevCardPlayEvent(player="Noah", card="knight"))])
    assert st["dev_cards_held"] == 0
