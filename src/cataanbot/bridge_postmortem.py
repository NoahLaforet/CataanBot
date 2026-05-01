"""Postmortem feed + render extracted from bridge.py.

`_feed_postmortem` mirrors each /log payload into the postmortem
collector pipeline; `_write_postmortem` renders the HTML at game end.

Lazy imports of `cataanbot.bridge._is_self_player` and
`cataanbot.bridge_robber._compute_robber_snapshot` to avoid a circular
import (bridge.py imports from this module at module load time).

Re-exported by bridge.py for backwards compat.
"""
from __future__ import annotations

from typing import Any


def _feed_postmortem(st, payload: dict[str, Any]) -> None:
    """Mirror the /log payload into the postmortem-collector pipeline.

    Parses the DOM-log payload, dispatches through a dedicated Tracker +
    ColorMap, and appends the (event, result, timestamp) triple. When a
    GameOverEvent lands we render a self-contained HTML postmortem once
    and flip ``pm_written`` so reruns (log virtualization echoes) don't
    stomp the file.
    """
    from cataanbot.bridge import _is_self_player
    from cataanbot.bridge_robber import _compute_robber_snapshot
    from cataanbot.events import (
        DevCardBuyEvent, DevCardPlayEvent, GameOverEvent, InfoEvent,
        RobberMoveEvent, RollEvent,
        TradeCommitEvent, TradeOfferEvent,
    )
    from cataanbot.live import apply_event
    from cataanbot.parser import parse_event

    try:
        event = parse_event(payload)
    except Exception as e:  # noqa: BLE001
        print(f"[pm] parse error: {e}", flush=True)
        return
    try:
        result = apply_event(st["pm_tracker"], st["pm_color_map"], event)
    except Exception as e:  # noqa: BLE001
        print(f"[pm] dispatch error: {e}", flush=True)
        return

    ts = payload.get("ts")
    ts_f = float(ts) if isinstance(ts, (int, float)) else None

    st["pm_events"].append(event)
    st["pm_results"].append(result)
    st["pm_timestamps"].append(ts_f)

    # Trade-offer lifecycle. Offers are informational to the tracker but
    # the advisor surfaces them as accept/decline recommendations, so we
    # cache the latest one here and let the snapshot builder evaluate it
    # against the live hand. Any commit/roll invalidates the cached offer.
    if isinstance(event, TradeOfferEvent):
        st["pending_trade_offer"] = {
            "player": event.player,
            "give": dict(event.give),
            "want": dict(event.want),
            "ts": ts_f,
        }
    elif isinstance(event, (TradeCommitEvent, RollEvent)):
        st["pending_trade_offer"] = None

    # Robber ranking on Knight play. WS pipeline's _track_overlay_state
    # already covers the 7-roll case, but DevCardPlayEvents only come
    # through the DOM log, so we hook here. Clearing on RobberMoveEvent
    # is redundant with the WS path but costs nothing and keeps us safe
    # if colonist stops shipping the robber-move diff.
    game = st.get("game")
    # Self dev-card holdings tracking. DevCardBuyEvent for self comes
    # ONLY through the DOM log (the WS diff parser suppresses self's
    # buys because they don't reveal the card type). DevCardPlayEvent
    # for self also rides the DOM log. Hook here so the held count
    # stays in sync regardless of which pipeline fires the event.
    if (isinstance(event, DevCardBuyEvent)
            and game is not None
            and _is_self_player(game, event.player)):
        st["dev_cards_held"] = (
            int(st.get("dev_cards_held") or 0) + 1)
        st["dev_cards_bought_this_turn"] = (
            int(st.get("dev_cards_bought_this_turn") or 0) + 1)
    elif (isinstance(event, DevCardPlayEvent)
          and game is not None
          and _is_self_player(game, event.player)):
        st["dev_cards_held"] = max(
            0, int(st.get("dev_cards_held") or 0) - 1)
        # ALSO apply to the LIVE tracker. DOM-log DevCardPlayEvents
        # are normally only routed to the postmortem tracker, but the
        # live tracker's catanatron state needs to decrement
        # {type}_IN_HAND so the play-timing hints (knight_hint,
        # monopoly_hint, etc.) stop firing once the card is played.
        # Without this, the hint sticks in the HUD after play because
        # MONOPOLY_IN_HAND stays at 1 forever.
        try:
            from cataanbot.live import apply_event as _apply
            _apply(game.tracker, game.color_map, event)
        except Exception as e:  # noqa: BLE001
            print(f"[overlay] live devplay apply failed: {e!r}",
                  flush=True)

    # Friendly-robber detection: colonist announces the rule via an
    # InfoEvent at game start ("Friendly Robber is active, ..."). One-
    # shot toggle on the session — once seen, the robber-target ranker
    # filters protected victims out for the rest of the game.
    if (isinstance(event, InfoEvent)
            and game is not None and game.session is not None
            and event.text.lower().startswith("friendly robber")):
        game.session.friendly_robber_active = True

    if (isinstance(event, DevCardPlayEvent) and event.card == "knight"
            and game is not None
            and _is_self_player(game, event.player)):
        st["robber_pending"] = True
        st["robber_snapshot"] = _compute_robber_snapshot(
            game, display_colors=st.get("display_colors") or {})
    elif isinstance(event, RobberMoveEvent):
        # Drop the urgency — self no longer needs to *pick* — but
        # keep the snapshot around so the overlay's robber panel
        # stays visible through the steal + rest of the turn. Cleared
        # on the next RollEvent (or instantly if an opponent rolls a
        # new 7) in _track_overlay_state.
        st["robber_pending"] = False

    if isinstance(event, GameOverEvent) and not st["pm_written"]:
        _write_postmortem(st, event)


def _resolve_final_vp(st) -> dict[str, int]:
    """Pick the most authoritative final VP source available.

    The pm_tracker is only fed through the DOM log, where BuildEvents
    arrive without node/edge coordinates and dispatch as ``unhandled``.
    That leaves pm_tracker frozen at the opening (2 VP each) regardless
    of how the real game played out. So:

    1. **Colonist's authoritative state first.** ``game.session``'s
       ``victoryPointsState`` carries per-color totals exactly as
       colonist computes them — settles, cities, held VP cards, LR/LA
       flags. This is what colonist's UI shows. We only see opp VP
       *cards* as zero (colonist hides those), so opp totals understate
       by their hidden VPs, but settle/city/LR/LA are full-fidelity.
    2. **Live tracker fallback.** ``game.tracker.vp_status()`` reads
       the WS-driven live tracker, which gets real BuildEvent coords.
       Slightly less authoritative than colonist (no VP cards at all)
       but reliable.
    3. **Empty dict last resort.** If both sources are unavailable
       (e.g. the bridge crashed mid-game) we hand back ``{}`` so the
       postmortem still renders, just without final scores.

    The classic pm_tracker.vp_status() path is intentionally NOT used —
    it was the source of the 2/2 final-score bug Noah saw on his
    BrickdDaddy game.
    """
    game = st.get("game")
    if game is not None:
        try:
            sess = getattr(game, "session", None)
            color_map = getattr(game, "color_map", None)
            if sess is not None and color_map is not None:
                vps: dict[str, int] = {}
                for cid, username in sess.player_names.items():
                    if not sess.victory_points_state.get(cid):
                        continue
                    try:
                        color = color_map.get(username)
                    except Exception:  # noqa: BLE001
                        continue
                    vps[color] = sess.vp_total(cid)
                if vps:
                    return vps
        except Exception:  # noqa: BLE001
            pass
        try:
            return dict(game.tracker.vp_status()["per_color"])
        except Exception:  # noqa: BLE001
            pass
    try:
        return dict(st["pm_tracker"].vp_status()["per_color"])
    except Exception:  # noqa: BLE001
        return {}


def _compute_board_fingerprint(game) -> dict[str, object] | None:
    """Snapshot the board's shape so the postmortem can identify a variant.

    Reads tile/corner/edge/port counts off the live CatanMap. Classic
    Catan is 19/54/72/9; Pond is 24/76/100/8; other Seafarers / Through-
    the-Desert / Black-Forest variants each have their own shape, so the
    quad of counts is a reliable fingerprint without us having to enum
    every variant.
    """
    if game is None:
        return None
    try:
        m = game.tracker.game.state.board.map
    except Exception:  # noqa: BLE001
        return None
    fp: dict[str, object] = {}
    try:
        fp["tile_count"] = len(getattr(m, "land_tiles", {}) or {})
    except Exception:  # noqa: BLE001
        pass
    try:
        fp["corner_count"] = len(getattr(m, "land_nodes", set()) or set())
    except Exception:  # noqa: BLE001
        pass
    try:
        fp["edge_count"] = len(getattr(m, "land_edges", set()) or set())
    except Exception:  # noqa: BLE001
        pass
    try:
        fp["port_count"] = len(getattr(m, "port_nodes", set()) or set())
    except Exception:  # noqa: BLE001
        pass
    label = (fp.get("tile_count") == 19
             and fp.get("corner_count") == 54
             and "classic")
    if label:
        fp["label"] = "classic"
    else:
        fp["label"] = "variant"
    return fp or None


def _write_postmortem(st, game_over) -> None:
    """Render the HTML postmortem to ``st['pm_dir']`` (or the default)."""
    import time as _time
    from pathlib import Path as _Path

    from cataanbot.postmortem import render_postmortem_html

    out_dir = st.get("pm_dir")
    if out_dir is None:
        # Same fallback as the bridge launcher — cwd/postmortems is
        # portable for anyone who clones the repo.
        out_dir = _Path.cwd() / "postmortems"
    out_dir = _Path(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[pm] could not create {out_dir}: {e}", flush=True)
        return

    stamp = _time.strftime("%Y-%m-%d_%H%M%S")
    winner = (getattr(game_over, "winner", "") or "game").strip() or "game"
    safe_winner = "".join(
        c if c.isalnum() or c in ("-", "_") else "_" for c in winner)
    out_path = out_dir / f"{stamp}_{safe_winner}.html"

    final_vp = _resolve_final_vp(st)

    try:
        path = render_postmortem_html(
            events=st["pm_events"],
            dispatch_results=st["pm_results"],
            timestamps=st["pm_timestamps"],
            color_map=st["pm_color_map"],
            final_vp=final_vp,
            out_path=out_path,
            jsonl_path=None,
            board_fingerprint=_compute_board_fingerprint(st.get("game")),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[pm] render failed: {e}", flush=True)
        return

    st["pm_written"] = True
    print(f"\n=== postmortem written → {path} ===\n", flush=True)
