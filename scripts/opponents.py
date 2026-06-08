"""Adversarial sparring opponents for self-play, beyond catanatron's bots.

The 2026-06-07 weight search saturated because every opponent available
(random / weighted / vp / the champion mirror) plays "by the book": each
maximizes its OWN position. Real Colonist games are not like that. Humans
go off-book to disrupt the leader, and they gang up on whoever is ahead
rather than quietly racing. The champion eval scores a state as
``own - 0.8*max_opp``, which silently assumes opponents optimize for
themselves; these players break that assumption on purpose so the eval can
be measured (and hardened) against it.

Three archetypes, all usable on any map and at any player count:

- ChampionPlayer  — greedy 1-ply on the shipped EVAL_WEIGHTS. The strong,
  by-the-book baseline (centralizes what eval_player / tune_selfplay had).
- AntiBookPlayer  — mostly the champion, but with probability ``epsilon``
  it makes the single legal move that most HURTS the current leader (which
  is our bot when our bot is winning), instead of its own best move. This
  is the "unpredictability factor": disruptive, leader-seeking noise the
  bot must react to.
- HunterPlayer    — points every robber, knight, and monopoly at a target
  (default: the current leader) to deny and steal, and otherwise develops
  normally. This is "hunting the opponent on purpose rather than going for
  the win." It is the direct stress test for the ``- 0.8*max_opp`` term.

These read catanatron's action shapes directly:
  MOVE_ROBBER  value = (tile_cube_coord, victim_color_or_None, None)
  PLAY_MONOPOLY value = resource_str
  BUILD_*      value = node_id / edge / etc.
"""
from __future__ import annotations

import random

from catanatron import Color, Player
from catanatron.models.enums import ActionType
from catanbot import eval as ce

_RESOURCES = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")
_PIP_BY_NUMBER = {2: 1, 12: 1, 3: 2, 11: 2, 4: 3, 10: 3,
                  5: 4, 9: 4, 6: 5, 8: 5}


def greedy_decide(game, color, playable, weights):
    """Greedy 1-ply: copy + execute each legal action, keep the one whose
    resulting state scores best for ``color``. The shared brain for the
    champion baseline and the fallback behaviour of the adversaries."""
    if not playable:
        return None
    if len(playable) == 1:
        return playable[0]
    best, best_v = None, float("-inf")
    for a in playable:
        g2 = game.copy()
        try:
            g2.execute(a)
        except Exception:  # noqa: BLE001
            continue
        v = ce.evaluate_state(g2, color, weights)
        if v > best_v:
            best_v, best = v, a
    return best if best is not None else playable[0]


def _public_vp(state, color) -> int:
    idx = state.color_to_index[color]
    return int(state.player_state.get(f"P{idx}_VICTORY_POINTS", 0))


def _leader_color(game, exclude=None):
    """The color with the most PUBLIC victory points (what a human reads as
    "the leader"), excluding ``exclude``. Ties break by colour order so the
    choice is deterministic for fixed-seed reproducibility."""
    st = game.state
    best, best_vp = None, -1
    for c in st.colors:
        if c == exclude:
            continue
        vp = _public_vp(st, c)
        if vp > best_vp:
            best_vp, best = vp, c
    return best


def _tile_block_value(game, coord, target) -> float:
    """How much of ``target``'s production a robber on ``coord`` would deny:
    tile pips x (2 for a city) summed over the target's buildings touching
    that tile. Higher = a more painful block for the target."""
    m = game.state.board.map
    tile = m.land_tiles.get(coord)
    if tile is None or tile.number is None:
        return 0.0
    pips = _PIP_BY_NUMBER.get(int(tile.number), 0)
    if not pips:
        return 0.0
    buildings = game.state.board.buildings
    blocked = 0.0
    for node_id in tile.nodes.values():
        owner = buildings.get(node_id)
        if owner and owner[0] == target:
            blocked += 2.0 if owner[1] == "CITY" else 1.0
    return pips * blocked


class ChampionPlayer(Player):
    """Greedy 1-ply on the shipped (or supplied) EVAL_WEIGHTS."""

    def __init__(self, color, weights=None):
        super().__init__(color)
        self.weights = dict(ce.EVAL_WEIGHTS) if weights is None else weights

    def decide(self, game, playable_actions):
        return greedy_decide(game, self.color, playable_actions, self.weights)


class AntiBookPlayer(Player):
    """Champion most of the time; with prob ``epsilon`` plays the single
    legal move that most lowers the current leader's evaluation. Disruptive,
    leader-seeking, and (because the leader is often our bot) something the
    bot must actively react to."""

    # Action types that are real strategic levers (worth weaponizing). ROLL
    # / END_TURN / DISCARD aren't disruptions, so off-book turns defer to the
    # champion there.
    _LEVERS = frozenset({
        ActionType.MOVE_ROBBER, ActionType.PLAY_MONOPOLY,
        ActionType.PLAY_KNIGHT_CARD, ActionType.PLAY_YEAR_OF_PLENTY,
        ActionType.PLAY_ROAD_BUILDING, ActionType.BUILD_ROAD,
        ActionType.BUILD_SETTLEMENT, ActionType.BUILD_CITY,
        ActionType.MARITIME_TRADE,
    })

    def __init__(self, color, epsilon=0.25, seed=0, weights=None):
        super().__init__(color)
        self.epsilon = epsilon
        self.weights = dict(ce.EVAL_WEIGHTS) if weights is None else weights
        self._rng = random.Random(seed ^ (hash(color) & 0xFFFF))

    def decide(self, game, playable_actions):
        pa = playable_actions
        if not pa:
            return None
        if len(pa) > 1 and self._rng.random() < self.epsilon:
            disruptive = self._most_disruptive(game, pa)
            if disruptive is not None:
                return disruptive
        return greedy_decide(game, self.color, pa, self.weights)

    def _most_disruptive(self, game, playable):
        """The lever move that minimizes the leader's evaluation. Targets the
        leader; if we ARE the leader, targets the next strongest player (so
        the disruption never just helps us)."""
        leader = _leader_color(game)
        victim = leader
        if victim == self.color:
            victim = _leader_color(game, exclude=self.color)
        if victim is None:
            return None
        levers = [a for a in playable if a.action_type in self._LEVERS]
        if not levers:
            return None
        worst, worst_v = None, float("inf")
        for a in levers:
            g2 = game.copy()
            try:
                g2.execute(a)
            except Exception:  # noqa: BLE001
                continue
            v = ce.evaluate_state(g2, victim, self.weights)
            if v < worst_v:
                worst_v, worst = v, a
        return worst


class HunterPlayer(Player):
    """Funnels every robber / knight / monopoly at a target (default: the
    current leader) and otherwise develops on its own eval. Models a human
    who decides to take you down rather than race the board."""

    def __init__(self, color, target=None, weights=None):
        super().__init__(color)
        # target=None -> hunt the live leader each turn; a Color -> hunt that
        # fixed player (used to make "everyone hunts our bot" stress tests).
        self.target = target
        self.weights = dict(ce.EVAL_WEIGHTS) if weights is None else weights

    def _target_color(self, game):
        if self.target is not None and self.target != self.color:
            return self.target
        return _leader_color(game, exclude=self.color)

    def decide(self, game, playable_actions):
        pa = playable_actions
        if not pa:
            return None
        target = self._target_color(game)
        if target is None:
            return greedy_decide(game, self.color, pa, self.weights)

        robber = [a for a in pa if a.action_type == ActionType.MOVE_ROBBER]
        if robber:
            return self._aim_robber(game, robber, target)

        mono = [a for a in pa if a.action_type == ActionType.PLAY_MONOPOLY]
        if mono:
            return self._aim_monopoly(game, mono, target)

        # Eagerly play a knight to unlock a robber aimed at a leading target.
        knight = [a for a in pa if a.action_type == ActionType.PLAY_KNIGHT_CARD]
        if knight and _public_vp(game.state, target) >= _public_vp(
                game.state, self.color):
            return knight[0]

        return greedy_decide(game, self.color, pa, self.weights)

    def _aim_robber(self, game, robber, target):
        """Prefer stealing from the target; among those, block the target's
        highest-yield tile. If no robber action steals the target (they have
        nothing to take), still block their best tile."""
        steal_target = [a for a in robber if a.value[1] == target]
        pool = steal_target if steal_target else robber
        return max(pool, key=lambda a: _tile_block_value(
            game, a.value[0], target))

    def _aim_monopoly(self, game, mono, target):
        """Monopoly the resource the target holds the most of."""
        st = game.state
        tidx = st.color_to_index[target]
        held = {r: int(st.player_state.get(f"P{tidx}_{r}_IN_HAND", 0))
                for r in _RESOURCES}
        best_res = max(held, key=held.get)
        for a in mono:
            if a.value == best_res:
                return a
        return mono[0]
