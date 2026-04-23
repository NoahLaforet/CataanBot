"""FastAPI bridge for the colonist.io userscript.

Two ingestion paths, both POST from the userscript running in-page:

* ``POST /log`` — DOM game-log events. Each new line from colonist's
  chat panel arrives here as a parsed ``{text, names, icons, ...}``
  payload. This is the parser-driven path: `parse_event` classifies
  it and `_print_event` echoes it to stdout.

* ``POST /ws``  — raw WebSocket frame dumps. The userscript patches
  the page's WebSocket constructor and forwards every inbound frame
  here (base64 of the msgpack body). The bridge decodes it and feeds
  a singleton ``LiveGame`` — the same pipeline the ``ws-replay`` CLI
  drives against capture files, but live. When the singleton has
  booted off a GameStart frame, the dispatcher's results stream to
  stdout in the same format as ws-replay's ``--verbose``.

Run:
    ./bin/cataanbot bridge                 # default :8765
    ./bin/cataanbot bridge --port 9000
    ./bin/cataanbot bridge --jsonl path    # mirror /log events to disk
    ./bin/cataanbot bridge --ws-jsonl path # mirror /ws frames to disk
    ./bin/cataanbot live                   # bridge + advisor output on

DOM log payload shape (see userscript/colonist_cataanbot.user.js):

    {
      "ts": 1713640000.123,
      "text": "Gratia stole  from Nona",
      "names":  [{"name": "Gratia", "color": "#E27174"},
                 {"name": "Nona",   "color": "#E09742"}],
      "icons":  [{"alt": "Resource Card"}],
      "raw_html": "<div>...</div>"          // optional, best-effort
    }

WS frame payload shape (mirrors the capture-dump buffer entries):

    {
      "dir":  "in" | "out",                // direction
      "ts":   1713640000.123,
      "wsId": 1,
      "kind": "text" | "arraybuffer",
      "byteLength": 48,
      "b64":  "gqJpZKMx..." | null,        // base64 for binary frames
      "data": "{\"type\":\"Connected\",...}" | null  // text frames
    }
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _build_app(jsonl_path: Path | None = None,
               ws_jsonl_path: Path | None = None,
               advisor: bool = False,
               postmortem_dir: Path | None = None):
    """Construct the FastAPI app. Imports kept lazy so the rest of the
    package doesn't require fastapi just to import cli.py."""
    from fastapi import Body, FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from cataanbot.live import ColorMap
    from cataanbot.live_game import LiveGame
    from cataanbot.tracker import Tracker

    app = FastAPI(title="cataanbot bridge", version="0.2")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mutable state kept in a dict so closures can rebind the LiveGame
    # when a fresh game starts (a new type=4 frame after a match ends).
    # `seq` bumps on every ingested WS frame/log event so the overlay
    # knows whether a fresh poll would return new data.
    st = {
        "log_count": 0,
        "ws_count": 0,
        "ws_errors": 0,
        "game": LiveGame(),
        "seq": 0,
        "last_roll": None,        # {"player","color","total","is_you"}
        # Ring-buffer of the last ~10 rolls, most-recent last.
        # Each entry: {"total", "is_you", "color", "hit_you", "blocked_you"}.
        # Populated in _track_overlay_state on every RollEvent; used by
        # the overlay's "recent rolls" strip to spot droughts and streaks.
        "roll_history": [],
        # Monotonic game-roll counter. Separate from roll_history (which
        # caps at 10) so the overlay can show "turn ~N" regardless of
        # buffer size. Not decremented; resets only on /reset.
        "total_rolls": 0,
        # total_rolls value at the moment the robber last moved onto a
        # self tile. None until the first robber-on-me move. Used to
        # enrich the robber_on_me banner with "placed N rolls ago" —
        # a direct persistence signal. blocks_recent alone can read
        # "0" even when the robber has sat there forever (if the number
        # hasn't come up), so persistence and cost are complementary.
        "robber_moved_at_rolls": None,
        "robber_pending": False,  # self rolled 7, hasn't placed robber yet
        "robber_snapshot": None,  # cached score_robber_targets payload
        # Auto-postmortem buffers. Fed from the /log path so the output
        # shape matches `cataanbot replay --postmortem`. Independent from
        # LiveGame's WS tracker — the two pipelines never cross.
        "pm_tracker": Tracker(),
        "pm_color_map": ColorMap(),
        "pm_events": [],
        "pm_results": [],
        "pm_timestamps": [],
        "pm_written": False,
        "pm_dir": postmortem_dir,
        # username → CSS color harvested from DOM-log name pills. The
        # WS gameState only ships an opaque integer color id (and the
        # colonist palette includes premium unlocks like BLACK that
        # don't map onto catanatron's 4-color enum), so the chat log is
        # our source of truth for what color the user actually sees.
        "display_colors": {},
        # Latest pending player-to-player trade offer from the DOM log.
        # {"player", "give", "want", "ts"} when live; cleared on commit,
        # on any subsequent offer, or on the next dice roll. Evaluated
        # lazily in the snapshot builder so the verdict always reflects
        # the freshest tracker state.
        "pending_trade_offer": None,
    }

    @app.get("/")
    def root() -> dict[str, Any]:
        g = st["game"]
        return {
            "service": "cataanbot bridge",
            "version": "0.2",
            "log_events": st["log_count"],
            "ws_frames": st["ws_count"],
            "ws_errors": st["ws_errors"],
            "game_started": g.started,
            "players": g.color_map.as_dict() if g.started else {},
        }

    @app.post("/log")
    def log(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        st["log_count"] += 1
        st["seq"] += 1
        _harvest_display_colors(st, payload)
        _print_event(payload, st["log_count"])
        if jsonl_path is not None:
            with jsonl_path.open("a") as f:
                f.write(json.dumps(payload) + "\n")
        _feed_postmortem(st, payload)
        return {"ok": True, "received": st["log_count"]}

    @app.get("/advisor")
    def advisor_snapshot() -> dict[str, Any]:
        return _build_advisor_snapshot(st)

    @app.post("/ws")
    def ws_frame(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        st["ws_count"] += 1
        st["seq"] += 1
        if ws_jsonl_path is not None:
            with ws_jsonl_path.open("a") as f:
                f.write(json.dumps(payload) + "\n")
        try:
            results = _feed_ws_payload(st["game"], payload)
        except Exception as e:  # noqa: BLE001 — bridge must not crash
            st["ws_errors"] += 1
            print(f"[ws #{st['ws_count']:05d}] decode error: {e}",
                  flush=True)
            return {"ok": False, "error": str(e)}

        game = st["game"]
        # First frame that boots the game — emit a header.
        if results is None and game.started and st.get("_booted") is None:
            st["_booted"] = True
            _print_game_start(game)
            return {"ok": True, "booted": True,
                    "players": game.color_map.as_dict()}

        if results:
            _track_overlay_state(st, results)
            _print_dispatch_results(
                game, results, st["ws_count"], advisor=advisor)
        return {"ok": True, "results": len(results or [])}

    @app.post("/reset")
    def reset() -> dict[str, Any]:
        st["log_count"] = 0
        st["ws_count"] = 0
        st["ws_errors"] = 0
        st["game"] = LiveGame()
        st["seq"] = 0
        st["last_roll"] = None
        st["roll_history"] = []
        st["total_rolls"] = 0
        st["robber_moved_at_rolls"] = None
        st["robber_pending"] = False
        st["robber_snapshot"] = None
        st["pm_tracker"] = Tracker()
        st["pm_color_map"] = ColorMap()
        st["pm_events"] = []
        st["pm_results"] = []
        st["pm_timestamps"] = []
        st["pm_written"] = False
        st["display_colors"] = {}
        st["pending_trade_offer"] = None
        st.pop("_booted", None)
        print("[bridge] game state reset", flush=True)
        return {"ok": True}

    return app


def _feed_ws_payload(game, payload: dict[str, Any]):
    """Decode one userscript WS-frame entry and push it through LiveGame.

    Returns None for frames we don't care about (opens, closes, decode
    errors, non-type=4/91 payloads) and a list of DispatchResults
    otherwise. A GameStart boot also returns an empty list — callers
    can distinguish by checking ``game.started`` pre/post."""
    import base64
    import json as _json

    from cataanbot.colonist_proto import decode_frame

    direction = payload.get("dir")
    if direction not in ("in", "out"):
        return None

    kind = payload.get("kind")
    if kind == "text":
        # Text frames from colonist are JSON (e.g. "Connected"). They
        # aren't part of the game-state pipe.
        text = payload.get("data")
        if not isinstance(text, str):
            return None
        try:
            body = _json.loads(text)
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        return game.feed(body)

    b64 = payload.get("b64")
    if not b64:
        return None
    data = base64.b64decode(b64)
    frame = decode_frame(data, direction)
    if frame.error or not isinstance(frame.payload, dict):
        return None
    return game.feed(frame.payload)


def _print_game_start(game) -> None:
    print("\n=== game booted via /ws ===", flush=True)
    print(f"    players: {game.color_map.as_dict()}", flush=True)
    if game.session and game.session.self_color_id is not None:
        self_user = game.session.player_names.get(
            game.session.self_color_id, "?")
        print(f"    self: {self_user} "
              f"(color id {game.session.self_color_id})",
              flush=True)
    board = game.tracker.game.state.board
    print(f"    map: {len(board.map.land_tiles)} land tiles, "
          f"robber at {board.robber_coordinate}", flush=True)
    print()


def _harvest_display_colors(st, payload: dict[str, Any]) -> None:
    """Pull {name, color} pairs out of a /log payload and latch them.

    Colonist's chat pills carry the player's true UI color in a CSS
    ``style="color: rgb(...)"`` attribute. The userscript captures this
    as ``names: [{name, color}]`` on each payload. Cache the first
    non-empty color per username — once someone shows up in the log
    we know what color they are for the rest of the game."""
    names = payload.get("names")
    if not isinstance(names, list):
        return
    for entry in names:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        color = entry.get("color")
        # Fall back to the chat pill's background color when text color
        # is missing — colonist ships WHITE-player names without inline
        # color styles (would be invisible on white chat bg) and instead
        # uses a colored background.
        bg = entry.get("bg")
        picked = None
        if isinstance(color, str) and color.strip():
            picked = color.strip()
        elif isinstance(bg, str) and bg.strip():
            picked = bg.strip()
        if (isinstance(name, str) and name and picked
                and name not in st["display_colors"]):
            st["display_colors"][name] = picked


def _feed_postmortem(st, payload: dict[str, Any]) -> None:
    """Mirror the /log payload into the postmortem-collector pipeline.

    Parses the DOM-log payload, dispatches through a dedicated Tracker +
    ColorMap, and appends the (event, result, timestamp) triple. When a
    GameOverEvent lands we render a self-contained HTML postmortem once
    and flip ``pm_written`` so reruns (log virtualization echoes) don't
    stomp the file.
    """
    from cataanbot.events import (
        DevCardPlayEvent, GameOverEvent, RobberMoveEvent, RollEvent,
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
    if (isinstance(event, DevCardPlayEvent) and event.card == "knight"
            and game is not None
            and _is_self_player(game, event.player)):
        st["robber_pending"] = True
        st["robber_snapshot"] = _compute_robber_snapshot(
            game, display_colors=st.get("display_colors") or {})
    elif isinstance(event, RobberMoveEvent):
        st["robber_pending"] = False
        st["robber_snapshot"] = None

    if isinstance(event, GameOverEvent) and not st["pm_written"]:
        _write_postmortem(st, event)


def _write_postmortem(st, game_over) -> None:
    """Render the HTML postmortem to ``st['pm_dir']`` (or the default)."""
    import time as _time
    from pathlib import Path as _Path

    from cataanbot.postmortem import render_postmortem_html

    out_dir = st.get("pm_dir")
    if out_dir is None:
        out_dir = _Path.home() / "Desktop" / "CataanBot" / "postmortems"
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

    try:
        final_vp = st["pm_tracker"].vp_status()["per_color"]
    except Exception:  # noqa: BLE001
        final_vp = {}

    try:
        path = render_postmortem_html(
            events=st["pm_events"],
            dispatch_results=st["pm_results"],
            timestamps=st["pm_timestamps"],
            color_map=st["pm_color_map"],
            final_vp=final_vp,
            out_path=out_path,
            jsonl_path=None,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[pm] render failed: {e}", flush=True)
        return

    st["pm_written"] = True
    print(f"\n=== postmortem written → {path} ===\n", flush=True)


def _track_overlay_state(st, results) -> None:
    """Maintain the overlay's tiny FSM alongside dispatch.

    Two bits of state that the overlay wants to show but the tracker
    doesn't expose on its own:

    * last_roll — the most recent RollEvent. The overlay highlights it
      so you can see at a glance whether your pips just fired.
    * robber_pending — True between the moment *you* roll a 7 and the
      moment a RobberMoveEvent lands. While pending, /advisor ships the
      top-N robber target ranking so the overlay can surface it inline.
    """
    from cataanbot.events import RobberMoveEvent, RollEvent

    game = st["game"]
    for r in results:
        if r.status not in ("applied", "skipped"):
            continue
        if isinstance(r.event, RollEvent):
            is_you = _is_self_player(game, r.event.player)
            color = None
            if r.event.player:
                try:
                    color = game.color_map.get(r.event.player)
                except Exception:  # noqa: BLE001
                    color = None
            st["last_roll"] = {
                "player": r.event.player,
                "color": color,
                "total": r.event.total,
                "is_you": bool(is_you),
            }
            # Ring-buffer entry. hit_you/blocked_you are computed NOW
            # against current board state — i.e., the buildings that
            # were on the board when this roll happened (robber
            # placement might update immediately after via a separate
            # event, but the yield math for this roll fired first).
            # gained_total / blocked_total are the raw card counts so
            # the snap can aggregate "actual vs expected" over the
            # window without having to re-run _compute_roll_yield per
            # entry.
            entry: dict[str, Any] = {
                "total": r.event.total,
                "is_you": bool(is_you),
                "color": color,
                "hit_you": False,
                "blocked_you": False,
                "gained_total": 0,
                "blocked_total": 0,
            }
            if r.event.total and r.event.total != 7:
                try:
                    sess = game.session
                    if sess and sess.self_color_id is not None:
                        uname = sess.player_names.get(sess.self_color_id)
                        if uname:
                            sc = game.color_map.get(uname)
                            y = _compute_roll_yield(game, sc, r.event.total)
                            if y:
                                g = int(y.get("total", 0))
                                b = int(y.get("blocked_total", 0))
                                entry["gained_total"] = g
                                entry["blocked_total"] = b
                                entry["hit_you"] = g > 0
                                entry["blocked_you"] = b > 0
                except Exception:  # noqa: BLE001
                    pass
            hist = list(st.get("roll_history") or [])
            hist.append(entry)
            st["roll_history"] = hist[-10:]
            st["total_rolls"] = int(st.get("total_rolls") or 0) + 1
            if r.event.total == 7 and is_you:
                st["robber_pending"] = True
                st["robber_snapshot"] = _compute_robber_snapshot(
                    game, display_colors=st["display_colors"])
            elif r.event.total == 7:
                # Opponent rolled 7 — you don't pick, clear any stale
                # overlay ranking from a prior self-roll if somehow still set.
                st["robber_pending"] = False
                st["robber_snapshot"] = None
        elif isinstance(r.event, RobberMoveEvent):
            # Any robber move clears pending — once the robber lands the
            # overlay's ranking is stale.
            st["robber_pending"] = False
            st["robber_snapshot"] = None
            # Anchor the persist counter at the current roll count.
            # _compute_robber_on_me only runs when the robber is on a
            # self tile, so the snap builder can safely treat the
            # counter as "when did this sit-on-me start" without having
            # to check here whether the destination is a self tile.
            st["robber_moved_at_rolls"] = int(st.get("total_rolls") or 0)


def _compute_robber_snapshot(
    game, display_colors: dict[str, str] | None = None, top: int = 5,
) -> list[dict[str, Any]] | None:
    """Snapshot the top-N robber rankings for the overlay.

    Each target gets a ``suggested_victim`` color — the best single person
    to steal from when the tile has more than one adjacent opposing
    settlement/city. Scoring: card count dominates (biggest EV per steal,
    more cards = more likely to hold a needed resource), but a near-win
    opponent (VP ≥ ``mid_late_vp()``) gets boosted priority to deny them
    resources.
    """
    from cataanbot.advisor import score_robber_targets

    sess = game.session
    if sess is None or sess.self_color_id is None:
        return None
    username = sess.player_names.get(sess.self_color_id)
    if not username:
        return None
    try:
        color = game.color_map.get(username)
    except Exception:  # noqa: BLE001
        return None
    # catanatron-color → username, so the victim pills can surface the
    # real colonist UI color for the robber ranking.
    reverse = {}
    for cid, user in sess.player_names.items():
        try:
            reverse[game.color_map.get(user)] = user
        except Exception:  # noqa: BLE001
            continue
    hand_size_override: dict[str, int] = {}
    for cid, count in sess.hand_card_counts.items():
        user = sess.player_names.get(cid)
        if not user:
            continue
        try:
            c = game.color_map.get(user)
        except Exception:  # noqa: BLE001
            continue
        hand_size_override[c] = int(count)
    try:
        scores = score_robber_targets(
            game.tracker.game, color,
            hand_size_override=hand_size_override or None,
        )
    except Exception:  # noqa: BLE001
        return None
    display = display_colors or {}
    out = []
    for s in scores[:top]:
        # Pick the best victim: card count dominates (best steal EV), VP
        # pressure boosts near-winners, pip contribution is a small nudge.
        from cataanbot.config import close_to_win_vp, mid_late_vp
        close_vp = close_to_win_vp()
        mid_vp = mid_late_vp()
        def _victim_priority(vcolor: str) -> float:
            cards = s.opponent_hand_size.get(vcolor, 0)
            vp = s.victim_vp.get(vcolor, 0)
            pips = s.victims.get(vcolor, 0)
            vp_weight = 3.0 if vp >= close_vp else (
                1.8 if vp >= mid_vp else 1.0)
            return cards * vp_weight + pips * 0.3
        suggested_color: str | None = None
        if s.victims:
            # Prefer a victim with >=1 card; all-empty-hands falls back to
            # the highest priority anyway, which is fine.
            with_cards = [
                c for c in s.victims
                if s.opponent_hand_size.get(c, 0) > 0
            ]
            pool = with_cards or list(s.victims.keys())
            suggested_color = max(pool, key=_victim_priority)
        out.append({
            "coord": list(s.coord),
            "resource": s.resource,
            "number": s.number,
            "score": round(s.score, 2),
            "suggested_victim": suggested_color,
            "victims": [
                {
                    "color": c,
                    "color_css": display.get(reverse.get(c, "")),
                    "username": reverse.get(c),
                    "pips": pips,
                    "vp": s.victim_vp.get(c, 0),
                    "cards": s.opponent_hand_size.get(c, 0),
                    "suggested": (c == suggested_color),
                }
                for c, pips in sorted(
                    s.victims.items(), key=lambda kv: -kv[1])
            ],
        })
    return out


# Cost tables mirror the build_costs used elsewhere in the bot. Keys
# are ordered by "preserve this build over the others" priority — a
# city beats a settlement beats a dev card beats a road when we can
# only keep one of them post-discard.
_DISCARD_PRESERVE_PLANS: tuple[tuple[str, dict[str, int]], ...] = (
    ("city", {"WHEAT": 2, "ORE": 3}),
    ("settlement", {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1}),
    ("dev card", {"WHEAT": 1, "SHEEP": 1, "ORE": 1}),
    ("road", {"WOOD": 1, "BRICK": 1}),
)
# Drop priority: resources on the left go first. SHEEP's the cheapest
# to re-acquire (3:1 bank trade still leaves an edge) and isn't the
# bottleneck for cities the way WHEAT/ORE are. WOOD/BRICK slot in
# between — roads are cheaper than cities. WHEAT/ORE last: losing an
# ore delays city tempo more than losing any other card.
_DISCARD_PRIORITY: tuple[str, ...] = (
    "SHEEP", "WOOD", "BRICK", "WHEAT", "ORE")


def _compute_discard_plan(
    hand: dict[str, int], need: int,
) -> tuple[dict[str, int], str | None]:
    """Return {resource: count} to discard plus a rationale string.

    Strategy: try to preserve the most valuable build we can afford
    *after* the discard. Drop from resources not needed for that build,
    lowest-priority first; only break into the preserved build if the
    remaining non-reserved cards aren't enough.
    """
    if need <= 0:
        return {}, None
    total = sum(hand.values())
    after = total - need
    preserve_name: str | None = None
    reserved: dict[str, int] = {}
    for name, cost in _DISCARD_PRESERVE_PLANS:
        cost_total = sum(cost.values())
        if cost_total > after:
            continue
        if all(hand.get(r, 0) >= n for r, n in cost.items()):
            preserve_name = name
            reserved = dict(cost)
            break
    drops: dict[str, int] = {}
    remaining = need
    for r in _DISCARD_PRIORITY:
        if remaining == 0:
            break
        droppable = hand.get(r, 0) - reserved.get(r, 0)
        if droppable <= 0:
            continue
        take = min(droppable, remaining)
        drops[r] = drops.get(r, 0) + take
        remaining -= take
    if remaining > 0:
        # Reserved cards weren't enough slack — we have to dip into the
        # preserved build. Drop from its cheapest resource first.
        preserve_name = None
        for r in _DISCARD_PRIORITY:
            if remaining == 0:
                break
            available = hand.get(r, 0) - drops.get(r, 0)
            if available <= 0:
                continue
            take = min(available, remaining)
            drops[r] = drops.get(r, 0) + take
            remaining -= take
    return drops, preserve_name


def _compute_discard_hint(
    hand: dict[str, int], cards: int,
) -> dict[str, Any] | None:
    """Recommend which cards to discard when self must discard on a 7.

    Fires only when the authoritative card count exceeds the discard
    limit (default 7). Returns None otherwise so the overlay can hide
    the banner. Uses the authoritative ``cards`` total — the tracker's
    per-resource breakdown is trusted for the *shape* but the total is
    ``cards``; a drift between the two surfaces elsewhere already.
    """
    from cataanbot.config import DISCARD_LIMIT
    if cards <= DISCARD_LIMIT:
        return None
    need = cards // 2
    if need <= 0:
        return None
    drops, preserve = _compute_discard_plan(hand, need)
    if not drops:
        return None
    if preserve:
        rationale = f"keep enough to {preserve}"
    else:
        rationale = "trim least-scarce cards"
    return {
        "need": need,
        "drop": drops,
        "rationale": rationale,
    }


# Resource tie-breaker weights when a Monopoly would net equal totals
# across resources — prefer stealing the strategically scarcer cards.
_MONOPOLY_RES_WEIGHT = {
    "ORE": 5, "WHEAT": 5, "BRICK": 3, "WOOD": 3, "SHEEP": 2,
}
_BUILD_COSTS_MONOPOLY = {
    "city": {"WHEAT": 2, "ORE": 3},
    "settlement": {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1},
    "dev card": {"WHEAT": 1, "SHEEP": 1, "ORE": 1},
    "road": {"WOOD": 1, "BRICK": 1},
}


def _compute_monopoly_hint(
    game, self_color: str, self_hand: dict[str, int],
) -> dict[str, Any] | None:
    """Pick the best resource to steal when self plays Monopoly.

    Fires only when self holds at least one MONOPOLY card. Ranks each
    resource by the inferred total held across opps; ties break toward
    resources that would unlock an immediate build for self.
    """
    from catanatron import Color
    try:
        my_enum = Color[self_color.upper()]
    except Exception:  # noqa: BLE001
        return None
    state = game.tracker.game.state
    idx = state.color_to_index.get(my_enum)
    if idx is None:
        return None
    held = int(state.player_state.get(f"P{idx}_MONOPOLY_IN_HAND", 0))
    if held <= 0:
        return None
    # Aggregate inferred counts across opps via the tracker.
    totals: dict[str, int] = {
        "WOOD": 0, "BRICK": 0, "SHEEP": 0, "WHEAT": 0, "ORE": 0,
    }
    for opp_color in state.color_to_index:
        if opp_color == my_enum:
            continue
        try:
            opp_hand = game.tracker.hand(opp_color.value)
        except Exception:  # noqa: BLE001
            continue
        for r, n in opp_hand.items():
            if r in totals:
                totals[r] += int(n)
    if not any(totals.values()):
        return None
    # Rank: (count, unlock-bonus, resource-weight).
    def _unlock(res: str) -> int:
        # 1 if grabbing `res` unlocks any build we couldn't afford.
        gained = dict(self_hand)
        gained[res] = gained.get(res, 0) + totals[res]
        for name, cost in _BUILD_COSTS_MONOPOLY.items():
            if all(gained.get(r, 0) >= n for r, n in cost.items()):
                if not all(self_hand.get(r, 0) >= n for r, n in cost.items()):
                    return 1
        return 0
    ranked = sorted(
        totals.items(),
        key=lambda kv: (kv[1], _unlock(kv[0]),
                        _MONOPOLY_RES_WEIGHT.get(kv[0], 0)),
        reverse=True,
    )
    best_res, best_count = ranked[0]
    if best_count <= 0:
        return None
    # Unlock reason: which build does this unlock (if any)?
    unlock_reason: str | None = None
    gained = dict(self_hand)
    gained[best_res] = gained.get(best_res, 0) + best_count
    for name, cost in _BUILD_COSTS_MONOPOLY.items():
        if (all(gained.get(r, 0) >= n for r, n in cost.items())
                and not all(self_hand.get(r, 0) >= n
                            for r, n in cost.items())):
            unlock_reason = f"unlocks {name}"
            break
    return {
        "have": held,
        "resource": best_res,
        "est_steal": best_count,
        "totals": totals,
        "unlock": unlock_reason,
    }


def _compute_yop_hint(
    game, self_color: str, self_hand: dict[str, int],
) -> dict[str, Any] | None:
    """Suggest which pair to pick with Year-of-Plenty.

    Fires only when self holds at least one YEAR_OF_PLENTY card. Picks
    the pair that unlocks the most valuable buildable; falls back to
    the pair that aligns with the costliest build closest to complete.
    """
    from catanatron import Color
    try:
        my_enum = Color[self_color.upper()]
    except Exception:  # noqa: BLE001
        return None
    state = game.tracker.game.state
    idx = state.color_to_index.get(my_enum)
    if idx is None:
        return None
    held = int(state.player_state.get(f"P{idx}_YEAR_OF_PLENTY_IN_HAND", 0))
    if held <= 0:
        return None
    # For each target build, compute deficit in self_hand. A pick is
    # "unlocking" iff total_deficit <= 2 (YoP grants exactly 2 cards).
    best: tuple[int, str, list[str]] | None = None  # (priority, build, [r1, r2])
    priority = {"city": 4, "settlement": 3, "dev card": 2, "road": 1}
    for name, cost in _BUILD_COSTS_MONOPOLY.items():
        deficit: dict[str, int] = {}
        for r, n in cost.items():
            d = n - self_hand.get(r, 0)
            if d > 0:
                deficit[r] = d
        total = sum(deficit.values())
        if total == 0:
            # Already affordable; YoP would be wasted on this target.
            continue
        if total > 2:
            continue
        pick: list[str] = []
        for r, d in deficit.items():
            pick.extend([r] * d)
        if len(pick) < 2:
            # Fill the second slot with a resource toward the next-
            # best build (city takes priority if YoP is generous).
            needs_next = None
            for n2, cost2 in _BUILD_COSTS_MONOPOLY.items():
                if n2 == name:
                    continue
                for r2, need in cost2.items():
                    have = self_hand.get(r2, 0)
                    if name != n2 and have + pick.count(r2) < need:
                        needs_next = r2
                        break
                if needs_next:
                    break
            pick.append(needs_next or "ORE")  # ORE as safe default
        pick = pick[:2]
        p = priority.get(name, 0)
        if best is None or p > best[0]:
            best = (p, name, pick)
    if best is None:
        return None
    _, build_name, pair = best
    return {
        "have": held,
        "pair": pair,
        "unlock": build_name,
    }


def _compute_rb_hint(game, self_color: str) -> dict[str, Any] | None:
    """Recommend whether to play Road Building this turn.

    Fires only when self holds a ROAD_BUILDING card AND has at least
    one road piece left to place. Two free roads is worth the most
    when it swings longest road — either qualifying self or catching
    an opp who's about to. Secondary case: road supply is almost
    exhausted, so play while the cards are still useful.

    Returns ``{have, should_play, reason, self_len, opp_len}`` or None
    when we shouldn't surface a hint. The projected length is a naive
    +2 to self's current chain — catanatron recomputes topology-aware
    length after play, so this is a hint upper bound, not a promise.
    """
    from catanatron import Color
    try:
        my_enum = Color[self_color.upper()]
    except Exception:  # noqa: BLE001
        return None
    state = game.tracker.game.state
    idx = state.color_to_index.get(my_enum)
    if idx is None:
        return None
    held = int(state.player_state.get(
        f"P{idx}_ROAD_BUILDING_IN_HAND", 0))
    if held <= 0:
        return None
    # Need at least 1 road piece left to get any value. (The card
    # still plays with 0 roads available but grants nothing — treat
    # as a non-hint in that case to avoid nudging a wasted play.)
    pieces = _pieces_for_color(game, self_color)
    roads_left = int(pieces.get("road_left", 0))
    if roads_left <= 0:
        return None

    self_len = int(state.player_state.get(
        f"P{idx}_LONGEST_ROAD_LENGTH", 0))
    self_has = bool(state.player_state.get(
        f"P{idx}_HAS_ROAD", False))
    opp_max = 0
    opp_has = False
    for c, oidx in state.color_to_index.items():
        if c == my_enum:
            continue
        ln = int(state.player_state.get(
            f"P{oidx}_LONGEST_ROAD_LENGTH", 0))
        opp_max = max(opp_max, ln)
        if state.player_state.get(f"P{oidx}_HAS_ROAD", False):
            opp_has = True
    projected = self_len + min(2, roads_left)
    qualify = 5  # base-game longest-road threshold

    should = False
    reason = "hold — no clear swing yet"
    if not self_has and projected >= max(qualify, opp_max + 1):
        should = True
        reason = (f"secures longest road "
                  f"({self_len}→{projected} vs opp {opp_max})")
    elif opp_has and opp_max >= qualify and projected >= opp_max:
        should = True
        reason = (f"catches opp longest road "
                  f"(proj {projected} ≥ opp {opp_max})")
    elif roads_left <= 2:
        # Almost out of roads — card loses value the longer you hold it.
        should = True
        reason = f"road pieces running low ({roads_left} left)"

    return {
        "have": held,
        "should_play": should,
        "reason": reason,
        "self_len": self_len,
        "opp_len": opp_max,
    }


def _compute_knight_hint(
    game, display_colors: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Recommend whether to play a Knight dev card this turn.

    Fires only when self has at least one KNIGHT in hand. The "should
    play" logic weighs:
        * Robber currently on one of self's tiles → urgent remove
        * Top robber target score >= 4 → meaningful block
        * An opp at 7+ VP with 2+ played knights → deny largest-army

    Returns {have, should_play, reason, best_target} or None if self has
    no Knight or we can't determine self color.
    """
    from catanatron import Color

    sess = game.session
    if sess is None or sess.self_color_id is None:
        return None
    username = sess.player_names.get(sess.self_color_id)
    if not username:
        return None
    try:
        color = game.color_map.get(username)
    except Exception:  # noqa: BLE001
        return None
    try:
        my_enum = Color[color.upper()]
    except Exception:  # noqa: BLE001
        return None

    state = game.tracker.game.state
    idx = state.color_to_index.get(my_enum)
    if idx is None:
        return None
    knight_in_hand = int(
        state.player_state.get(f"P{idx}_KNIGHT_IN_HAND", 0))
    if knight_in_hand <= 0:
        return None

    board = game.tracker.game.state.board
    robber = board.robber_coordinate
    # Robber currently blocking me? Find self buildings on the robber tile.
    self_blocked_pips = 0
    m = board.map
    robber_tile = m.land_tiles.get(robber) if robber else None
    if robber_tile is not None and robber_tile.number:
        from cataanbot.advisor import PIP_DOTS_BY_NUMBER
        robber_node_ids = set(robber_tile.nodes.values())
        for nid, (bcol, _bt) in board.buildings.items():
            if bcol != my_enum or int(nid) not in robber_node_ids:
                continue
            self_blocked_pips += PIP_DOTS_BY_NUMBER.get(robber_tile.number, 0)

    # Opp closing in on largest army? (>= 2 played knights, at/above
    # largest-army-threat VP threshold)
    from cataanbot.config import largest_army_threat_vp
    la_threat_vp = largest_army_threat_vp()
    largest_army_threat = False
    for opp_color, opp_idx in state.color_to_index.items():
        if opp_color == my_enum:
            continue
        played = int(state.player_state.get(
            f"P{opp_idx}_PLAYED_KNIGHT", 0))
        vp = int(state.player_state.get(
            f"P{opp_idx}_VICTORY_POINTS", 0))
        if played >= 2 and vp >= la_threat_vp:
            largest_army_threat = True
            break

    # Best robber target score (reuses the existing ranker).
    top_targets = _compute_robber_snapshot(
        game, display_colors=display_colors, top=1) or []
    top_target = top_targets[0] if top_targets else None
    top_score = float(top_target["score"]) if top_target else 0.0

    should = False
    reason = "hold — no urgent block"
    if self_blocked_pips > 0:
        should = True
        reason = f"robber on your tile ({self_blocked_pips} pips blocked)"
    elif largest_army_threat:
        should = True
        reason = "opp closing on largest army — break their tempo"
    elif top_score >= 4.0:
        should = True
        reason = f"strong block available (score {top_score:+.1f})"

    return {
        "have": knight_in_hand,
        "should_play": should,
        "reason": reason,
        "best_target": top_target,
    }


def _compute_longest_road_race(
    game, self_color: str | None,
) -> dict[str, Any] | None:
    """Flag a longest-road race when either side is 1 segment away.

    Returns a banner dict (level + message) or None. Levels:
        * "self_push" — self is 1 road away from qualifying (5 segs)
        * "opp_threat" — an opp is 1 road away from qualifying
        * "contested" — both sides are within 1 of current holder
    The banner is noise-free once the race is settled (holder is 2+
    ahead of everyone). We deliberately don't alert on "self just
    won longest road" because the VP banner already handles that.
    """
    from catanatron import Color

    state = game.tracker.game.state
    if self_color is None:
        return None
    try:
        my_enum = Color[self_color.upper()]
    except Exception:  # noqa: BLE001
        return None

    # Build (color, length, has_road) per seated player.
    lengths: list[tuple[object, int, bool]] = []
    for col, idx in state.color_to_index.items():
        length = int(state.player_state.get(
            f"P{idx}_LONGEST_ROAD_LENGTH", 0))
        has_road = bool(state.player_state.get(
            f"P{idx}_HAS_ROAD", False))
        lengths.append((col, length, has_road))
    if not lengths:
        return None

    self_entry = next((e for e in lengths if e[0] == my_enum), None)
    opps = [e for e in lengths if e[0] != my_enum]
    if self_entry is None:
        return None
    self_len = self_entry[1]
    self_has = self_entry[2]
    opp_max = max((e[1] for e in opps), default=0)
    opp_holder = any(e[2] for e in opps)

    # Nobody's close yet — don't spam early game.
    if self_len < 4 and opp_max < 4:
        return None

    # Already held + lead by 2+: race is over, no alert.
    if self_has and self_len >= opp_max + 2:
        return None
    if opp_holder and opp_max >= self_len + 2:
        return None

    # Contested first (most specific): both sides ≥4 and within 1.
    # Keeps the contested banner from being drowned out by the plain
    # opp_threat path when we're neck-and-neck at 4.
    if self_len >= 4 and opp_max >= 4 and abs(self_len - opp_max) <= 1:
        holder = "you" if self_has else ("opp" if opp_holder else "—")
        return {
            "level": "contested",
            "self_len": self_len,
            "opp_len": opp_max,
            "message": (
                f"longest-road race: you {self_len} vs opp {opp_max}"
                f" (holder: {holder})"),
        }
    # Self pushing: we're on 4+, nobody else is close.
    if self_len >= 4 and not self_has and opp_max < self_len:
        return {
            "level": "self_push",
            "self_len": self_len,
            "opp_len": opp_max,
            "message": f"1 road → longest road (you have {self_len})",
        }
    # Opp threat: someone else is on 4+ and ahead of us.
    if opp_max >= 4 and opp_max >= self_len and not self_has:
        gap = opp_max - self_len
        if opp_holder:
            msg = f"opp holds longest road ({opp_max}) — {gap} ahead"
        else:
            msg = f"opp 1 road from longest road ({opp_max})"
        return {
            "level": "opp_threat",
            "self_len": self_len,
            "opp_len": opp_max,
            "message": msg,
        }
    return None


def _compute_production(
    game, color: str,
) -> dict[str, Any] | None:
    """Expected resource yield per roll given current builds.

    Sums ``map.node_production[node_id]`` across every settlement (×1)
    and city (×2) this color owns. ``per_roll`` is the total expected
    cards per dice roll — a rough pace indicator (1.0 = one card per
    roll, 2.5 = well-established). ``top_resource`` names the most-
    produced resource so Noah can tell "ore-heavy" from "sheep-heavy"
    at a glance.

    Color-generic: used for self (pace check) and each opp (threat
    ranking — informs robber target and trade-block priority).
    """
    try:
        from catanatron import Color
        my_enum = Color[color.upper()]
        board = game.tracker.game.state.board
        m = board.map
    except Exception:  # noqa: BLE001
        return None
    totals: dict[str, float] = {
        "WOOD": 0.0, "BRICK": 0.0, "SHEEP": 0.0, "WHEAT": 0.0, "ORE": 0.0,
    }
    for nid, (col, btype) in board.buildings.items():
        if col != my_enum:
            continue
        mult = 2.0 if str(btype).upper() == "CITY" else 1.0
        for res, pips in m.node_production.get(int(nid), {}).items():
            if res in totals:
                totals[res] += mult * float(pips)
    per_roll = sum(totals.values())
    top_res = max(totals, key=lambda r: totals[r]) if per_roll > 0 else None
    return {
        "per_roll": per_roll,
        "by_resource": totals,
        "top_resource": top_res if (top_res and totals[top_res] > 0) else None,
    }


def _owned_ports(game, color: str) -> list[str] | None:
    """Return a sorted list of ports this color has a coastal building
    on. Each entry is the port label as shown in ``advisor.player_ports``:
    a resource name (``"WHEAT"``, ``"SHEEP"``, etc.) for a 2:1, or
    ``"GENERIC"`` for the 3:1 port. Returns None on failure so the
    overlay can skip the render instead of guessing.
    """
    try:
        from cataanbot.advisor import player_ports
        ports = player_ports(game.tracker.game, color)
    except Exception:  # noqa: BLE001
        return None
    # Stable order so the overlay doesn't flicker between refreshes.
    # GENERIC last so the specific 2:1s read first.
    specific = sorted(p for p in ports if p != "GENERIC")
    if "GENERIC" in ports:
        specific.append("GENERIC")
    return specific


def _knights_played(game, color: str) -> int:
    """Knights already played by `color` (for largest-army tracking).

    At 3+ this qualifies the player for largest army; holders at 2 are
    one knight away. Per-player visibility complements the single
    largest_army_race banner — shows *which* opp is actually the
    threat when multiple have dev cards in hand.
    """
    try:
        from catanatron import Color
        my_enum = Color[color.upper()]
        idx = game.tracker.game.state.color_to_index.get(my_enum)
        if idx is None:
            return 0
        return int(game.tracker.game.state.player_state.get(
            f"P{idx}_PLAYED_KNIGHT", 0))
    except Exception:  # noqa: BLE001
        return 0


# Build cost table used by _affordable_builds. Kept local to avoid
# coupling the opp-afford snapshot to discard-plan ordering, which is
# priority-sorted rather than impact-sorted. Order here is VP-impact
# descending so the overlay shows the most worrying afford tag first.
_AFFORD_COSTS: tuple[tuple[str, dict[str, int]], ...] = (
    ("city", {"WHEAT": 2, "ORE": 3}),
    ("settlement", {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1}),
    ("dev", {"WHEAT": 1, "SHEEP": 1, "ORE": 1}),
    ("road", {"WOOD": 1, "BRICK": 1}),
)


def _affordable_builds(
    inferred: dict[str, int], unknown: int = 0,
) -> list[str] | None:
    """Return the builds an opp's *definitely-known* hand can cover.

    Conservative by design: only flags builds whose every cost slot is
    fully covered by the inferred bucket. Unknowns don't count — they
    might be anything, so claiming affordability would over-alert Noah
    on buys that rely on hidden cards. Returns [] when the hand covers
    nothing, None on bad input (so overlay can silent-skip).

    When hand_tracked is false (unknown > 0) the answer is still useful:
    inferred is a *lower bound*, so "can: city" under unknowns still
    means they can city now, even if their hidden cards add more.
    """
    if not isinstance(inferred, dict):
        return None
    out: list[str] = []
    for name, cost in _AFFORD_COSTS:
        if all(inferred.get(r, 0) >= n for r, n in cost.items()):
            out.append(name)
    return out


def _pieces_for_color(game, color: str) -> dict[str, int]:
    """Settlement / city / road counts placed and remaining per color.

    Counts directly off the board (buildings dict + roads dict) since
    our tracker keeps those authoritative but doesn't decrement the
    catanatron ``Px_*_AVAILABLE`` pool keys. Base-game caps are 5/4/15
    for settlements/cities/roads. Roads in catanatron are stored with
    both edge directions, so we count unique frozenset edges.
    """
    try:
        from catanatron import Color
        my_enum = Color[color.upper()]
        board = game.tracker.game.state.board
    except Exception:  # noqa: BLE001
        return {"settle": 0, "settle_left": 5, "city": 0, "city_left": 4,
                "road": 0, "road_left": 15}
    settle = 0
    city = 0
    for nid, (col, btype) in board.buildings.items():
        if col != my_enum:
            continue
        if str(btype).upper() == "CITY":
            city += 1
        else:
            settle += 1
    seen_edges: set[frozenset] = set()
    for edge, col in board.roads.items():
        if col != my_enum:
            continue
        key = frozenset(edge) if not isinstance(edge, frozenset) else edge
        seen_edges.add(key)
    road = len(seen_edges)
    return {
        "settle": settle, "settle_left": max(0, 5 - settle),
        "city": city, "city_left": max(0, 4 - city),
        "road": road, "road_left": max(0, 15 - road),
    }


def _compute_bank_supply(game) -> dict[str, Any] | None:
    """Estimate how many of each resource remain in the bank.

    Uses the 19-per-resource Catan rule and the tracker's authoritative
    player hands to compute the difference. Returns None if we can't
    trust the math (e.g. a player has a totally inferred hand with
    unknowns, which would double-count against the bank). The `low`
    list calls out resources with ≤2 in the bank so the overlay can
    flash a warning — you can't port/4:1-trade into an empty resource
    and no one can receive it on a dice roll until someone pays back.
    """
    sess = game.session
    if sess is None:
        return None
    totals = {r: 0 for r in ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")}
    for user in sess.player_names.values():
        try:
            c = game.color_map.get(user)
        except Exception:  # noqa: BLE001
            continue
        h = game.tracker.hand(c)
        for r in totals:
            totals[r] += int(h.get(r, 0))
    remaining = {r: max(0, 19 - totals[r]) for r in totals}
    low = sorted(
        [(r, n) for r, n in remaining.items() if n <= 2],
        key=lambda kv: kv[1])
    return {
        "remaining": remaining,
        "low": [{"resource": r, "count": n} for r, n in low],
    }


def _compute_dev_deck_remaining(game) -> dict[str, Any] | None:
    """Estimate how many dev cards are left in the deck.

    Base game starts with 25: 14 knights, 5 VP, 2 monopoly, 2 YoP,
    2 road building. A dev card bought stays out of the deck forever
    (played or not), so ``remaining = 25 - total_ever_bought``. Total
    ever bought = sum across all players of (unplayed dev cards in
    hand) + (played knights + played specials). VP cards sit silently
    in hand so they're already covered by the unplayed count.

    Returns ``{remaining, drawn, low}`` where `low` is a bool flagged
    when ≤2 cards remain — buying a dev card becomes a gamble that
    can't happen at all once the deck is empty.
    """
    sess = game.session
    if sess is None:
        return None
    try:
        state = game.tracker.game.state
    except Exception:  # noqa: BLE001
        return None
    total_unplayed = 0
    total_played_actions = 0
    for cid in sess.player_names:
        total_unplayed += int(sess.dev_card_counts.get(cid, 0))
    action_keys = ("PLAYED_KNIGHT", "PLAYED_MONOPOLY",
                   "PLAYED_YEAR_OF_PLENTY", "PLAYED_ROAD_BUILDING")
    for _c, idx in state.color_to_index.items():
        for k in action_keys:
            total_played_actions += int(state.player_state.get(
                f"P{idx}_{k}", 0))
    drawn = total_unplayed + total_played_actions
    # Clamp — if tracking drift somehow outputs drawn > 25 we don't
    # want to surface a negative number.
    remaining = max(0, 25 - drawn)
    return {
        "remaining": remaining,
        "drawn": drawn,
        "low": remaining <= 2,
    }


def _compute_largest_army_race(
    game, self_color: str | None,
) -> dict[str, Any] | None:
    """Flag a largest-army race once any player has ≥2 played knights.

    Largest-army qualifies at 3 played knights, so 2 = "one knight
    away." Same level structure as the longest-road race helper:
    self_push / opp_threat / contested / settled (silent).

    We look at PLAYED_KNIGHT (actual knights played) because that's
    the only authoritative count — knights in hand don't yet count
    toward the title.
    """
    from catanatron import Color

    state = game.tracker.game.state
    if self_color is None:
        return None
    try:
        my_enum = Color[self_color.upper()]
    except Exception:  # noqa: BLE001
        return None

    played: list[tuple[object, int, bool]] = []
    for col, idx in state.color_to_index.items():
        n = int(state.player_state.get(f"P{idx}_PLAYED_KNIGHT", 0))
        has_army = bool(state.player_state.get(f"P{idx}_HAS_ARMY", False))
        played.append((col, n, has_army))
    if not played:
        return None

    self_entry = next((e for e in played if e[0] == my_enum), None)
    opps = [e for e in played if e[0] != my_enum]
    if self_entry is None:
        return None
    self_n = self_entry[1]
    self_has = self_entry[2]
    opp_max = max((e[1] for e in opps), default=0)
    opp_holder = any(e[2] for e in opps)

    # Silent pre-race: need at least one side on 2 to matter.
    if self_n < 2 and opp_max < 2:
        return None
    # Settled: holder is 2+ ahead.
    if self_has and self_n >= opp_max + 2:
        return None
    if opp_holder and opp_max >= self_n + 2:
        return None

    # Contested (most specific): both sides ≥2 and within 1.
    if self_n >= 2 and opp_max >= 2 and abs(self_n - opp_max) <= 1:
        holder = "you" if self_has else ("opp" if opp_holder else "—")
        return {
            "level": "contested",
            "self_n": self_n,
            "opp_n": opp_max,
            "message": (
                f"largest-army race: you {self_n} vs opp {opp_max}"
                f" (holder: {holder})"),
        }
    if self_n >= 2 and not self_has and opp_max < self_n:
        return {
            "level": "self_push",
            "self_n": self_n,
            "opp_n": opp_max,
            "message": f"1 knight → largest army (you have {self_n})",
        }
    if opp_max >= 2 and opp_max >= self_n and not self_has:
        gap = opp_max - self_n
        if opp_holder:
            msg = f"opp holds largest army ({opp_max}) — {gap} ahead"
        else:
            msg = f"opp 1 knight from largest army ({opp_max})"
        return {
            "level": "opp_threat",
            "self_n": self_n,
            "opp_n": opp_max,
            "message": msg,
        }
    return None


def _compute_roll_yield(
    game, color: str, number: int,
) -> dict[str, Any] | None:
    """Break down what self would produce from a specific roll.

    Iterates every tile with ``tile.number == number``. For each, if
    the robber is parked there the buildings on that tile are blocked
    (tallied under ``blocked``); otherwise they contribute their
    resource to ``gained`` (×1 per settlement, ×2 per city). Returns
    None on bad input so the overlay can silent-skip.

    Used by the last-roll banner to surface what the dice actually
    delivered — and, more importantly, what the robber cost. A line
    like "+1 ore (3 ore blocked on the 8)" is worth more than just
    knowing a 7 didn't hit.
    """
    if number == 7 or not number:
        return None
    try:
        from catanatron import Color
        my_enum = Color[color.upper()]
        board = game.tracker.game.state.board
        m = board.map
    except Exception:  # noqa: BLE001
        return None
    robber_coord = board.robber_coordinate
    gained: dict[str, int] = {}
    blocked: dict[str, int] = {}
    tiles_touched = 0
    for coord, tile in m.land_tiles.items():
        if tile.number != number or not tile.resource:
            continue
        node_ids = set(tile.nodes.values())
        # Count self buildings on this tile. Settlement = ×1, city = ×2.
        for nid, (bcol, btype) in board.buildings.items():
            if bcol != my_enum or int(nid) not in node_ids:
                continue
            mult = 2 if str(btype).upper() == "CITY" else 1
            bucket = blocked if coord == robber_coord else gained
            bucket[tile.resource] = bucket.get(tile.resource, 0) + mult
            tiles_touched += 1
    if tiles_touched == 0:
        # Self's board has no exposure to this number — still useful to
        # report ("rolled 4 · nothing for you") so the banner is
        # informative rather than silent. Caller decides rendering.
        return {"gained": {}, "blocked": {}, "total": 0, "blocked_total": 0}
    return {
        "gained": gained,
        "blocked": blocked,
        "total": sum(gained.values()),
        "blocked_total": sum(blocked.values()),
    }


def _compute_robber_on_me(game) -> dict[str, Any] | None:
    """Persistent "robber is blocking you" banner.

    Different from knight_hint: fires whenever the robber is parked on
    a self tile, regardless of whether a knight is in hand. Reports
    which tile and how many pips are being suppressed so the overlay
    can show the ongoing cost — a reminder to trade into dev cards or
    push for a knight.
    """
    from catanatron import Color

    sess = game.session
    if sess is None or sess.self_color_id is None:
        return None
    username = sess.player_names.get(sess.self_color_id)
    if not username:
        return None
    try:
        color = game.color_map.get(username)
    except Exception:  # noqa: BLE001
        return None
    try:
        my_enum = Color[color.upper()]
    except Exception:  # noqa: BLE001
        return None

    board = game.tracker.game.state.board
    robber = board.robber_coordinate
    if not robber:
        return None
    m = board.map
    robber_tile = m.land_tiles.get(robber)
    if robber_tile is None or not robber_tile.number:
        # Desert or uninit — robber parked here costs nothing.
        return None

    from cataanbot.advisor import PIP_DOTS_BY_NUMBER
    robber_node_ids = set(robber_tile.nodes.values())
    pips = 0
    building_count = 0
    has_city = False
    for nid, (bcol, btype) in board.buildings.items():
        if bcol != my_enum or int(nid) not in robber_node_ids:
            continue
        per_building = PIP_DOTS_BY_NUMBER.get(robber_tile.number, 0)
        if str(btype).upper() == "CITY":
            per_building *= 2
            has_city = True
        pips += per_building
        building_count += 1
    if building_count == 0:
        return None
    return {
        "resource": robber_tile.resource,
        "number": robber_tile.number,
        "buildings": building_count,
        "has_city": has_city,
        "pips_blocked": pips,
    }


def _build_advisor_snapshot(st) -> dict[str, Any]:
    """JSON payload for the userscript overlay.

    Poll-friendly: callers diff on `seq` to detect change, but can
    unconditionally re-render if they prefer. All fields are safe to
    render even before a game has booted — `self` is None until then."""
    game = st["game"]
    snap: dict[str, Any] = {
        "seq": st["seq"],
        "game_started": game.started,
        "ws_frames": st["ws_count"],
        "log_events": st["log_count"],
        "self": None,
        "opps": [],
        "last_roll": st.get("last_roll"),
        "roll_history": list(st.get("roll_history") or []),
        "total_rolls": int(st.get("total_rolls") or 0),
        "robber_pending": bool(st.get("robber_pending")),
        "robber_targets": st.get("robber_snapshot") or [],
        # "forced" = self rolled a 7 and must place the robber now;
        # "knight" = self holds a KNIGHT, targets shown as play-timing aid.
        # None when targets are empty or from an older setup flow.
        "robber_reason": (
            "forced" if st.get("robber_pending") else None),
        "my_turn": False,
        "recommendations": [],
        "incoming_trade": None,
        "knight_hint": None,
        "monopoly_hint": None,
        "yop_hint": None,
        "rb_hint": None,
        "discard_hint": None,
        "threat": None,
        "robber_on_me": None,
        "longest_road_race": None,
        "largest_army_race": None,
        "bank_supply": None,
        "dev_deck": None,
        "yield_summary": None,
    }
    if not game.started:
        return snap
    sess = game.session
    if sess is None:
        return snap
    # Opening picks don't need a latched self-color — they're a
    # board-level ranking of the top remaining spots. self_color_id
    # stays None until colonist ships a resourceCards frame with real
    # (non-zero) values, which only happens once resources land. So we
    # evaluate setup_phase here, before the self-color gate, so Noah
    # sees opening picks from the first frame rather than after his
    # 2nd settlement drops resources.
    #
    # We count settlements per seat directly rather than trusting
    # catanatron's ``is_initial_build_phase`` flag — that only flips
    # when catanatron's own turn machinery transitions into mid-game,
    # and our event-driven dispatch doesn't always trigger that.
    cat_game = game.tracker.game
    # Setup-phase detection: count settlements+cities per seat directly.
    # Must include cities — when a settlement upgrades, catanatron
    # rewrites the building's type, so a seat with 2 openings that
    # later upgrades one drops to ``settlements == 1`` even though the
    # opening phase is long over.
    num_players = len(sess.player_names) if sess.player_names else 0
    buildings_per_color: dict[Any, int] = {}
    for _nid, (col, btype) in cat_game.state.board.buildings.items():
        if btype in ("SETTLEMENT", "CITY"):
            buildings_per_color[col] = (
                buildings_per_color.get(col, 0) + 1)
    # Roads: count unique edges per color. catanatron stores each road
    # under both (a,b) and (b,a) orderings, so de-dup with a frozenset.
    roads_per_color: dict[Any, int] = {}
    seen_edges: set[frozenset[int]] = set()
    for edge, col in cat_game.state.board.roads.items():
        key = frozenset((int(edge[0]), int(edge[1])))
        if key in seen_edges:
            continue
        seen_edges.add(key)
        roads_per_color[col] = roads_per_color.get(col, 0) + 1
    # Opening is complete only when every color has 2 settlements AND
    # 2 roads. The 2nd settlement alone isn't enough — if we flipped
    # ``is_setup`` False as soon as the last 2nd settlement landed, the
    # opening picks (with their road-direction hint) would vanish
    # before the placing player had a chance to lay the matching road.
    opening_complete = (
        num_players > 0
        and len(buildings_per_color) >= num_players
        and min(buildings_per_color.values()) >= 2
        and len(roads_per_color) >= num_players
        and min(roads_per_color.values()) >= 2
    )
    is_setup = not opening_complete
    snap["setup_phase"] = is_setup
    # Game-progress header: rough round count + phase label so
    # tactical banners (stall, hot numbers, bank supply) have an
    # anchor. Round approximates as total_rolls / num_players; each
    # round every player rolls once, so this is tight in practice.
    # Phases are chosen against typical 10-VP game duration (~15-25
    # rounds): early focuses on expansion, mid on cities/dev cards,
    # late on the VP race. Silent during setup — no rolls yet means
    # the round math is undefined and the phase is obvious anyway.
    if not is_setup and num_players > 0:
        total_rolls = int(st.get("total_rolls") or 0)
        round_approx = (total_rolls // num_players) + 1
        if round_approx <= 5:
            phase = "early"
        elif round_approx <= 12:
            phase = "mid"
        else:
            phase = "late"
        snap["game_progress"] = {
            "round": round_approx,
            "phase": phase,
            "num_players": num_players,
            "total_rolls": total_rolls,
        }
    else:
        snap["game_progress"] = None
    if is_setup:
        from cataanbot.recommender import recommend_opening
        # self_color_id latches after self's 2nd settlement ships its
        # first resourceCards frame. Pass it in when we have it so the
        # "finish your opening road" followup can fire — without a
        # color, recommend_opening can't tell whose road is missing.
        rec_color: str | None = None
        if sess.self_color_id is not None:
            user = sess.player_names.get(sess.self_color_id)
            if user:
                try:
                    rec_color = game.color_map.get(user)
                except Exception:  # noqa: BLE001
                    rec_color = None
        try:
            snap["recommendations"] = recommend_opening(
                cat_game, rec_color, top=5)
        except Exception as e:  # noqa: BLE001
            print(f"[advisor] recommend_opening failed: {e!r}",
                  flush=True)
            snap["recommendations"] = []
    if sess.self_color_id is None:
        return snap
    username = sess.player_names.get(sess.self_color_id)
    if not username:
        return snap
    try:
        self_color = game.color_map.get(username)
    except Exception:  # noqa: BLE001
        return snap
    hand = dict(game.tracker.hand(self_color))
    # Authoritative total comes from colonist's raw resourceCards.cards
    # length (what we track in hand_card_counts). tracker.hand() is the
    # event-reconstructed breakdown and can drift when we miss frames
    # (disconnects, dead ws sessions) — a drift indicator we surface.
    tracker_total = sum(hand.values())
    cards = int(sess.hand_card_counts.get(sess.self_color_id, tracker_total))
    hand_drift = (tracker_total != cards)
    afford = []
    if all(hand.get(r, 0) >= n for r, n in
           (("WOOD", 1), ("BRICK", 1), ("SHEEP", 1), ("WHEAT", 1))):
        afford.append("settlement")
    if hand.get("WHEAT", 0) >= 2 and hand.get("ORE", 0) >= 3:
        afford.append("city")
    if hand.get("WOOD", 0) >= 1 and hand.get("BRICK", 0) >= 1:
        afford.append("road")
    if (hand.get("WHEAT", 0) >= 1 and hand.get("SHEEP", 0) >= 1
            and hand.get("ORE", 0) >= 1):
        afford.append("dev card")
    vp = _get_vp(game, self_color)
    # Monopoly vulnerability: a monopoly play takes EVERY card of one
    # resource from all opponents, so a 5+ stack in a single bucket is
    # real exposure. Only flag when an opp could actually play one —
    # if nobody holds an unplayed dev card, monopoly isn't on the
    # menu this turn cycle. Conservative on type: dev_card_counts
    # lumps VPs in with playables (we can't see types), so this
    # sometimes fires on "impossible" VP-only hands. Better a false
    # positive than missing a real hit that costs 5+ cards.
    mono_risk = None
    MONO_STACK_THRESHOLD = 5
    opps_with_devs = any(
        int(sess.dev_card_counts.get(cid, 0)) > 0
        for cid in sess.player_names
        if cid != sess.self_color_id
    )
    if opps_with_devs:
        big_stacks = [(r, n) for r, n in hand.items()
                      if n >= MONO_STACK_THRESHOLD]
        if big_stacks:
            # Pick the tallest stack — that's the biggest single-play
            # loss if it gets monopolied.
            big_stacks.sort(key=lambda rn: -rn[1])
            r, n = big_stacks[0]
            mono_risk = {"resource": r, "count": n}
    snap["self"] = {
        "username": username,
        "color": self_color,
        "color_css": st["display_colors"].get(username),
        "hand": hand,
        "cards": cards,
        "afford": afford,
        "vp": vp,
        # True when per-resource breakdown disagrees with the raw-total.
        # Overlay surfaces this so Noah knows the hand detail is unreliable
        # until the next HandSync frame corrects us.
        "hand_drift": hand_drift,
        "pieces": _pieces_for_color(game, self_color),
        "vp_breakdown": _vp_breakdown(game, self_color),
        "knights_played": _knights_played(game, self_color),
        "ports": _owned_ports(game, self_color),
        "production": _compute_production(game, self_color),
        # Monopoly exposure. None when no big stack or no opp could
        # play monopoly; {"resource", "count"} otherwise.
        "monopoly_risk": mono_risk,
    }
    # Enrich the last-roll with self's yield breakdown: what the dice
    # actually delivered from self's buildings, plus what was blocked
    # by the robber. Only when the last roll is a non-7 (7s don't
    # produce). Silent skip on computation failure.
    lr = snap.get("last_roll")
    if lr and lr.get("total") and lr["total"] != 7:
        try:
            lr["yield"] = _compute_roll_yield(
                game, self_color, int(lr["total"]))
        except Exception as e:  # noqa: BLE001
            print(f"[advisor] roll_yield failed: {e!r}", flush=True)
        # Opponent-yields on the same roll. Answers "did that roll
        # just feed the leader while I got nothing?" — a key piece of
        # context that self-only yield hides. Silent on zero-gain opps
        # to keep the banner tight; only opps who actually got or were
        # blocked from cards show up. Iterate directly off catanatron's
        # color_to_index so we don't depend on snap["opps"] being
        # populated yet (it's built later in this function).
        try:
            opp_yields = []
            for opp_color_enum in cat_game.state.color_to_index:
                if opp_color_enum.value == self_color:
                    continue
                oc = opp_color_enum.value
                oy = _compute_roll_yield(game, oc, int(lr["total"]))
                if not oy:
                    continue
                g = int(oy.get("total", 0))
                b = int(oy.get("blocked_total", 0))
                if g == 0 and b == 0:
                    continue
                opp_yields.append({
                    "color": oc,
                    "gained_total": g,
                    "blocked_total": b,
                })
            lr["opponent_yields"] = opp_yields
        except Exception as e:  # noqa: BLE001
            print(f"[advisor] opponent_yields failed: {e!r}", flush=True)
    # Aggregate self yield vs expected across the roll_history window.
    # Sums the per-entry gained/blocked totals (populated at roll time)
    # and compares actual gained against production.per_roll × non-7
    # rolls. Skipped when the window is empty — a single "0 vs 0" line
    # is just noise on turn 1. Overlay renders this as a small dim
    # trailer under the recent-rolls strip so Noah can answer "am I
    # being starved?" without counting manually.
    hist = st.get("roll_history") or []
    non_seven = [e for e in hist if e.get("total") != 7]
    per_roll = float((snap["self"].get("production") or {})
                     .get("per_roll", 0.0))
    # Gate on production: before self has a settlement down, per_roll=0
    # and "got 0/0 (N rolls)" is just visual noise. Also skip when the
    # window is empty — no rolls yet means nothing meaningful to say.
    if non_seven and per_roll > 0:
        got = sum(int(e.get("gained_total", 0)) for e in non_seven)
        blocked = sum(int(e.get("blocked_total", 0)) for e in non_seven)
        expected = per_roll * len(non_seven)
        snap["yield_summary"] = {
            "window": len(non_seven),
            "got": got,
            "blocked": blocked,
            "expected": round(expected, 1),
        }
    else:
        snap["yield_summary"] = None
    # Sevens density: how many 7s in the recent window vs expected.
    # Baseline is 6/36 ≈ 16.7% — so 3+ sevens in a 10-roll window is
    # ~2× expected. Use the whole history (not non_seven) because the
    # window sizing matters too — a 3-of-4 burst is a bigger signal
    # than 3-of-10. Silent when < 3 sevens; the noise floor for
    # "random clustering" is around 2 in 10 per binomial math.
    sevens_count = sum(1 for e in hist if e.get("total") == 7)
    window_len = len(hist)
    sevens_hot = None
    if sevens_count >= 3 and window_len >= 4:
        sevens_hot = {
            "sevens": sevens_count,
            "window": window_len,
        }
    snap["sevens_hot"] = sevens_hot
    # Hot numbers: productive dice that have over-rolled in the window.
    # Sibling to sevens_hot but for the resource-producing dice. For
    # each non-7 number, compare actual count to its 36-roll baseline
    # (6/8=5/36, 5/9=4/36, 4/10=3/36, 3/11=2/36, 2/12=1/36). Flag when
    # count≥3 AND actual≥2× expected. Useful because a hot 8 snowballs
    # whoever's on it, so Noah can brace (or stay aggressive). Sort by
    # ratio and take top 2 so the HUD shows the most-anomalous first
    # without clutter.
    NUM_WEIGHTS = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5,
                   8: 5, 9: 4, 10: 3, 11: 2, 12: 1}
    hot_numbers: list[dict] = []
    if window_len >= 4:
        counts: dict[int, int] = {}
        for e in hist:
            n = int(e.get("total", 0))
            if n in NUM_WEIGHTS:
                counts[n] = counts.get(n, 0) + 1
        for n, c in counts.items():
            expected = window_len * NUM_WEIGHTS[n] / 36.0
            if c >= 3 and c >= 2.0 * expected:
                hot_numbers.append({
                    "number": n,
                    "count": c,
                    "expected": round(expected, 1),
                })
        hot_numbers.sort(
            key=lambda x: -(x["count"] / max(x["expected"], 0.01))
        )
    snap["hot_numbers"] = hot_numbers[:2] if hot_numbers else None
    # Production stall: count non-7 rolls since the most recent gain.
    # Useful because a "3 rolls dry" drought on a 2-pip/turn engine is
    # expected variance, while the same drought on a 5-pip engine is a
    # real signal (probably a robber or bad-number cluster). Only fires
    # when per_roll > 0 — otherwise there's nothing to be behind on.
    stall = None
    if non_seven and per_roll > 0:
        count_since_gain = 0
        for e in reversed(non_seven):
            if int(e.get("gained_total", 0)) > 0:
                break
            count_since_gain += 1
        # Only surface if the whole window was dry AND it's meaningful.
        # 3+ non-7 rolls with nothing is the threshold — below that,
        # a single miss in 2 rolls is perfectly normal on even big
        # engines and would just clutter the HUD.
        if count_since_gain >= 3:
            stall = {
                "rolls_dry": count_since_gain,
                "window": len(non_seven),
                "per_roll": round(per_roll, 2),
            }
    snap["production_stall"] = stall
    # "My turn" is derived from colonist's currentTurnPlayerColor cache.
    # Recommendations only fire when it's actually my turn — off-turn
    # suggestions would just be noise.
    my_cid = sess.self_color_id
    snap["my_turn"] = (sess.current_turn_color_id is not None
                       and sess.current_turn_color_id == my_cid)
    # Mid-game recs: only when it's actually my turn. During setup the
    # opening picks were already populated above, so skip here.
    if not is_setup and snap["my_turn"]:
        try:
            from cataanbot.recommender import recommend_actions
            # Feed the bank_supply we already computed into the rec
            # planner so port/4:1 trades get skipped when the bank is
            # dry on the needed resource.
            bank_for_recs = (
                snap.get("bank_supply") or {}).get("remaining")
            snap["recommendations"] = recommend_actions(
                cat_game, self_color, hand, top=4,
                bank_supply=bank_for_recs)
        except Exception as e:  # noqa: BLE001
            print(f"[advisor] recommend_actions failed: {e!r}",
                  flush=True)
            snap["recommendations"] = []
    # Physical-supply cap: base Catan has 19 of each resource, so by
    # conservation `bank[r] + sum(all hands)[r] == 19`. The tracker's
    # internal freqdeck stays consistent, but its "max-resource" guess
    # on unknown steals can still attribute a resource to one opp even
    # when that many aren't physically unclaimed. Cap each opp's
    # inferred bucket to `19 - bank[r] - self[r]` so we never display
    # "4 ore" when only 2 are actually left in play.
    _CAP_RESOURCES = ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")
    opp_res_cap: dict[str, int] = {}
    try:
        freqdeck = cat_game.state.resource_freqdeck
        for idx, r in enumerate(_CAP_RESOURCES):
            opp_res_cap[r] = max(
                0, 19 - int(freqdeck[idx]) - int(hand.get(r, 0)))
    except Exception:  # noqa: BLE001
        opp_res_cap = {}
    for cid, count in sorted(sess.hand_card_counts.items()):
        if cid == sess.self_color_id:
            continue
        user = sess.player_names.get(cid)
        if not user:
            continue
        try:
            c = game.color_map.get(user)
        except Exception:  # noqa: BLE001
            continue
        # Inferred per-resource breakdown. The tracker applies every
        # observable delta (produce, known trades, builds, dev-card buys)
        # to opponent hands as they happen — so ``tracker.hand(color)``
        # is a lower-bound estimate. It can *diverge* from the authoritative
        # ``hand_card_counts`` total when a 3rd-party steal or a
        # closed-type discard happens; we surface that gap as ``unknown``.
        # Steals/discards between opponents make the breakdown noisy, so
        # clients should treat high unknown% as low-confidence.
        try:
            inferred = dict(game.tracker.hand(c))
        except Exception:  # noqa: BLE001
            inferred = {}
        # Clip to physical supply first. Any excess gets absorbed into
        # ``unknown`` below, which is more honest than displaying a
        # count that couldn't exist on the board.
        if opp_res_cap:
            for r, n in list(inferred.items()):
                cap = opp_res_cap.get(r, n)
                if n > cap:
                    inferred[r] = cap
        # Reconcile inference against the authoritative card count.
        # Over-attribution (inferred > real) happens when the tracker's
        # "max-resource" guess for unknown steals commits to the wrong
        # resource and we haven't caught up yet. Trim only the excess
        # from the largest bucket(s) instead of zeroing the whole hand —
        # the partial knowledge we still have is more useful than a
        # blanket "?". Remaining gap after trimming is ``unknown``.
        inferred_total = sum(inferred.values())
        real_total = int(count)
        if inferred_total > real_total:
            trimmed = dict(inferred)
            excess = inferred_total - real_total
            while excess > 0 and any(v > 0 for v in trimmed.values()):
                best = max(trimmed, key=lambda r: trimmed.get(r, 0))
                n = min(excess, trimmed[best])
                trimmed[best] -= n
                excess -= n
            inferred = trimmed
            inferred_total = sum(inferred.values())
        unknown = max(0, real_total - inferred_total)
        snap["opps"].append({
            "username": user,
            "color": c,
            "color_css": st["display_colors"].get(user),
            "cards": real_total,
            "hand": inferred,
            "unknown": unknown,
            # True when we know every card: breakdown sums to the total.
            "hand_tracked": (unknown == 0 and real_total > 0),
            "vp": _get_vp(game, c),
            # Unplayed dev cards in hand. Includes hidden VPs, so a
            # spike here is a real "they might be close to 10" signal.
            # Counting comes from colonist's authoritative card-list
            # length; we can't see the types, only the size.
            "dev_cards": int(sess.dev_card_counts.get(cid, 0)),
            "pieces": _pieces_for_color(game, c),
            "knights_played": _knights_played(game, c),
            # Builds the inferred hand definitely covers. Conservative:
            # unknowns don't count, so this underestimates. Useful to
            # pre-warn about an opp's likely next-turn VP jump.
            "can_afford": _affordable_builds(inferred, unknown),
            # Per-opp per-roll production. Drives robber-target choice
            # (shut down the biggest engine) and trade-block priority.
            "production": _compute_production(game, c),
            # Ports this opp can access. Trade-partner signal: an opp
            # with a 2:1 on a resource is a worse counterparty for that
            # resource (they'd rather bank-trade than meet you halfway).
            "ports": _owned_ports(game, c),
        })

    pending = st.get("pending_trade_offer")
    if pending:
        snap["incoming_trade"] = _evaluate_pending_trade(
            st, game, self_color, hand, pending)
    # Knight-card play-timing advice: only fires when self has >=1 KNIGHT
    # in hand. Harmless to compute every snapshot; _compute_knight_hint
    # bails out cheaply when nothing to say.
    try:
        snap["knight_hint"] = _compute_knight_hint(
            game, display_colors=st.get("display_colors") or {})
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] knight_hint failed: {e!r}", flush=True)
    # When self holds a KNIGHT and isn't already facing a forced robber
    # placement, surface the full target ranking so Noah can eyeball the
    # block before committing. Don't clobber "forced" state — the 7-roll
    # path owns robber_targets in that case.
    kh = snap.get("knight_hint") or {}
    if kh.get("have", 0) > 0 and not snap["robber_pending"]:
        try:
            full = _compute_robber_snapshot(
                game, display_colors=st.get("display_colors") or {})
            if full:
                snap["robber_targets"] = full
                snap["robber_reason"] = "knight"
        except Exception as e:  # noqa: BLE001
            print(f"[advisor] knight robber targets failed: {e!r}",
                  flush=True)
    try:
        snap["monopoly_hint"] = _compute_monopoly_hint(
            game, self_color, hand)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] monopoly_hint failed: {e!r}", flush=True)
    try:
        snap["yop_hint"] = _compute_yop_hint(game, self_color, hand)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] yop_hint failed: {e!r}", flush=True)
    try:
        snap["rb_hint"] = _compute_rb_hint(game, self_color)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] rb_hint failed: {e!r}", flush=True)
    # Discard-on-7 advice: fires whenever self's hand exceeds the discard
    # limit. The overlay should render it prominently on a 7-roll, but
    # we compute unconditionally — it's cheap and "you're over the limit"
    # is useful context even before a roll lands.
    try:
        snap["discard_hint"] = _compute_discard_hint(hand, cards)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] discard_hint failed: {e!r}", flush=True)
    # Leader-threat banner: flag when any opp is at/near the win
    # threshold so the overlay can shift tone toward defense. Uses the
    # same close_to_win_vp() knob the rest of the bot respects.
    snap["threat"] = _compute_leader_threat(snap)
    # Persistent robber-on-me warning — visible every snapshot while
    # the robber sits on a self tile, not just during a 7-roll or when
    # a knight is in hand.
    try:
        snap["robber_on_me"] = _compute_robber_on_me(game)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] robber_on_me failed: {e!r}", flush=True)
    # Enrich the banner with a recent-cost tally from roll_history. The
    # helper is game-state-only; the history lives in `st`. Blocked-
    # count across the window quantifies what the robber has actually
    # been costing, not just current-turn pips. Useful because pips
    # alone don't tell you whether the robber has been grinding you for
    # 3 straight rolls or was placed this turn.
    if snap.get("robber_on_me"):
        hist = st.get("roll_history") or []
        non_seven = [e for e in hist if e.get("total") != 7]
        snap["robber_on_me"]["rolls_recent"] = len(non_seven)
        snap["robber_on_me"]["blocks_recent"] = sum(
            1 for e in non_seven if e.get("blocked_you"))
        # Persistence: how many rolls since the robber last moved.
        # rolls_since_placed answers "how long has this been sitting
        # on me" — blocks_recent is the cost so far, this is the
        # duration so far. Together they let the banner distinguish
        # "just placed, may move soon" from "grinding me for 4 rolls".
        placed_at = st.get("robber_moved_at_rolls")
        if placed_at is not None:
            total = int(st.get("total_rolls") or 0)
            snap["robber_on_me"]["rolls_since_placed"] = max(
                0, total - int(placed_at))
    # Longest-road race tracker: only alerts once someone hits 4 segs.
    # Silent early game, settles down once a clear winner is ≥2 ahead.
    try:
        snap["longest_road_race"] = _compute_longest_road_race(
            game, self_color)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] longest_road_race failed: {e!r}", flush=True)
    # Largest-army race tracker: parallel to longest-road but on played
    # knights. Visible even when self has no knight in hand (knight_hint
    # only fires with self-knight, so largest-army threats slipped by).
    try:
        snap["largest_army_race"] = _compute_largest_army_race(
            game, self_color)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] largest_army_race failed: {e!r}", flush=True)
    # Bank-supply warning: if any resource is ≤2 left in the bank, Noah
    # needs to know — can't 4:1 trade into an empty pool and a 7-steal
    # may be the only way to get more.
    try:
        snap["bank_supply"] = _compute_bank_supply(game)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] bank_supply failed: {e!r}", flush=True)
    try:
        snap["dev_deck"] = _compute_dev_deck_remaining(game)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] dev_deck failed: {e!r}", flush=True)
    return snap


def _compute_leader_threat(snap: dict[str, Any]) -> dict[str, Any] | None:
    """Flag the highest-VP opp and label the urgency.

    Returns a dict or None when nobody's ahead enough to warrant a
    banner. Close-to-win and mid-late thresholds track config so the
    warning scales with the game's VP_TARGET (default 10 → 8 = close).

    Enrichment — ``threat_vector`` lists *how* the leader could close
    the gap right now: an affordable VP-granting build ("city"/
    "settlement") bumps urgency because VP can be claimed this turn,
    and unplayed dev cards flag hidden-VP risk (hidden VP cards count
    toward the win total the moment their total hits target). These
    convert the banner from "watch the leader" to "the leader can
    actually end it now" — which is a different decision for Noah.
    """
    from cataanbot.config import close_to_win_vp, mid_late_vp, VP_TARGET
    opps = snap.get("opps") or []
    if not opps:
        return None
    leader = max(opps, key=lambda o: o.get("vp", 0))
    leader_vp = int(leader.get("vp", 0))
    if leader_vp < mid_late_vp():
        return None
    self_snap = snap.get("self") or {}
    my_vp = int(self_snap.get("vp", 0))
    close_vp = close_to_win_vp()
    gap_to_win = max(0, VP_TARGET - leader_vp)

    # Threat vector: what tools does the leader have right now?
    # 'vp_build' = can afford city or settlement (+1 VP next turn).
    # 'dev_vp' = holds dev cards, any of which could be a hidden VP.
    vector: list[str] = []
    can_afford = leader.get("can_afford") or []
    vp_builds = [b for b in can_afford if b in ("city", "settlement")]
    if vp_builds:
        vector.append("vp_build")
    # Dev cards are only urgent when leader is genuinely close — at
    # mid_late VP they might be knights, and a knight is less scary
    # than a hidden VP at 9 VP.
    dev_cards = int(leader.get("dev_cards", 0) or 0)
    if dev_cards > 0 and leader_vp >= close_vp:
        vector.append("dev_vp")

    # Level maps to overlay styling: "win" is effectively over, "close"
    # = one build from winning, "mid" = worth noticing but not yet
    # urgent. A leader at "mid" with a VP-build in hand gets bumped to
    # "close" — they can actually close faster than their VP suggests.
    if leader_vp >= VP_TARGET:
        level = "win"
    elif leader_vp >= close_vp:
        level = "close"
    elif vp_builds and leader_vp >= close_vp - 1:
        level = "close"
    else:
        level = "mid"
    gap = leader_vp - my_vp

    # Build a means-tag for the message. Order: vp_build first (most
    # concrete), dev_vp second. Empty string when no vector present.
    means_parts = []
    if "vp_build" in vector:
        means_parts.append(f"can {'/'.join(vp_builds)}")
    if "dev_vp" in vector:
        means_parts.append(f"{dev_cards} dev")
    means = f" ({', '.join(means_parts)})" if means_parts else ""

    if level == "close":
        msg = (f"{leader.get('username')} at {leader_vp} VP — "
               f"one build from winning{means}")
    elif level == "win":
        msg = f"{leader.get('username')} at {leader_vp} VP — game over"
    else:
        msg = f"{leader.get('username')} leads at {leader_vp} VP{means}"
    return {
        "leader_username": leader.get("username"),
        "leader_color": leader.get("color"),
        "leader_color_css": leader.get("color_css"),
        "leader_vp": leader_vp,
        "my_vp": my_vp,
        "gap": gap,
        "gap_to_win": gap_to_win,
        "threat_vector": vector,
        "level": level,
        "message": msg,
    }


def _evaluate_pending_trade(st, game, self_color, self_hand,
                            pending: dict[str, Any]) -> dict[str, Any] | None:
    """Build the ``incoming_trade`` snapshot field — offer metadata plus
    a verdict from ``recommender.evaluate_incoming_trade``.

    Skips self-originated offers: those are our outbound proposals and
    don't need an accept/decline recommendation. Returns None in that
    case so the overlay hides the panel.
    """
    from cataanbot.recommender import evaluate_incoming_trade

    offerer = pending.get("player") or ""
    sess = game.session
    if sess is not None and sess.self_color_id is not None:
        self_user = sess.player_names.get(sess.self_color_id)
        if self_user and offerer == self_user:
            return None

    give = pending.get("give") or {}
    want = pending.get("want") or {}
    opp_vp = 0
    try:
        opp_color = game.color_map.get(offerer)
        opp_vp = _get_vp(game, opp_color)
    except Exception:  # noqa: BLE001
        opp_color = None

    try:
        verdict = evaluate_incoming_trade(
            game.tracker.game, self_color, self_hand,
            give, want, opp_vp=opp_vp,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] evaluate_incoming_trade failed: {e!r}",
              flush=True)
        return None

    return {
        "offerer": offerer,
        "offerer_color": opp_color,
        "offerer_color_css": st["display_colors"].get(offerer),
        "offerer_vp": opp_vp,
        "give": give,
        "want": want,
        **verdict,
    }


def _vp_breakdown(game, color: str) -> dict[str, int] | None:
    """Per-category VP breakdown from colonist's victoryPointsState.

    Returns ``{settle, city, vp_cards, longest_road, largest_army,
    total}`` or None when we don't have a live colonist session. Only
    works for self in the general case — VP cards are hidden for opps
    (colonist never ships key 2 for another player), so for opps the
    vp_cards slot is always 0 and total understates by their hidden
    VPs.
    """
    try:
        sess = getattr(game, "session", None)
        color_map = getattr(game, "color_map", None)
        if sess is None or color_map is None:
            return None
        username = color_map.reverse(color)
        if username is None:
            return None
        cid = None
        for c, name in sess.player_names.items():
            if name == username:
                cid = c
                break
        if cid is None:
            return None
        state = sess.victory_points_state.get(cid)
        if not state:
            return None
        # Keys: 0=settle count, 1=city count, 2=held VP cards,
        # 4=longest-road flag, 5=largest-army flag.
        settle = int(state.get(0, 0))
        city = int(state.get(1, 0))
        vp_cards = int(state.get(2, 0))
        lr = int(state.get(4, 0)) * 2
        la = int(state.get(5, 0)) * 2
        total = settle + city * 2 + vp_cards + lr + la
        return {
            "settle": settle, "city": city * 2, "vp_cards": vp_cards,
            "longest_road": lr, "largest_army": la, "total": total,
        }
    except Exception:  # noqa: BLE001
        return None


def _get_vp(game, color: str) -> int:
    """VP for `color` — prefer colonist's authoritative state.

    Colonist's victoryPointsState per color is what its UI displays
    (settles + cities + held VP cards + longest-road/largest-army
    flags). Using it directly avoids drift that would otherwise creep
    in when BuildEvents are missed on reconnect or when a knight-play
    doesn't reach our tracker. Falls back to catanatron's internal
    VICTORY_POINTS when we can't resolve the color to a colonist cid
    (e.g. ws-replay fixtures without a LiveSession).
    """
    try:
        color_map = getattr(game, "color_map", None)
        sess = getattr(game, "session", None)
        if sess is not None and color_map is not None:
            username = color_map.reverse(color)
            if username is not None:
                for cid, name in sess.player_names.items():
                    if name == username:
                        if sess.victory_points_state.get(cid):
                            return sess.vp_total(cid)
                        break
    except Exception:  # noqa: BLE001
        pass
    try:
        from catanatron import Color
        c = Color[color.upper()]
        idx = game.tracker.game.state.color_to_index.get(c)
        if idx is None:
            return 0
        return int(game.tracker.game.state.player_state.get(
            f"P{idx}_VICTORY_POINTS", 0))
    except Exception:  # noqa: BLE001
        return 0


def _print_dispatch_results(game, results, seq: int,
                            advisor: bool = False) -> None:
    from cataanbot.events import (
        BuildEvent, DevCardBuyEvent, HandSyncEvent, ProduceEvent,
        RobberMoveEvent, RollEvent,
    )

    for r in results:
        cls = type(r.event).__name__
        if r.status == "applied":
            print(f"[ws #{seq:05d}] {cls}: {r.message}", flush=True)
        elif r.status == "error":
            print(f"[ws #{seq:05d}] ERROR {cls}: {r.message}", flush=True)

    if not advisor:
        return

    # When the self-player rolls a 7, the next thing they have to do is
    # pick a robber tile — surface the ranking right away so they don't
    # have to alt-tab. Opponent 7-rolls are handled by the RobberMoveEvent
    # path elsewhere (nothing to suggest — they pick).
    for r in results:
        if (isinstance(r.event, RollEvent) and r.event.total == 7
                and r.status in ("applied", "skipped")
                and _is_self_player(game, r.event.player)):
            _print_robber_targets(game)
            break

    # Minimal advisor output: whenever the self-player's hand is
    # updated, or after a roll, print what they can afford to build.
    triggered = any(isinstance(r.event, (HandSyncEvent, RollEvent))
                    and r.status in ("applied", "skipped")
                    for r in results)
    if not triggered:
        return
    _print_self_advisor(game)


def _is_self_player(game, username: str | None) -> bool:
    if not username:
        return False
    sess = game.session
    if sess is None or sess.self_color_id is None:
        return False
    return sess.player_names.get(sess.self_color_id) == username


def _print_robber_targets(game, top: int = 5) -> None:
    """Compact top-N robber ranking for when the self-player rolls a 7."""
    from cataanbot.advisor import score_robber_targets

    sess = game.session
    if sess is None or sess.self_color_id is None:
        return
    username = sess.player_names.get(sess.self_color_id)
    if not username:
        return
    color = game.color_map.get(username)
    # Ground-truth opponent hand sizes from the WS snapshot. Falls back
    # to catanatron's per-resource tracking for any seat we haven't seen
    # a resourceCards entry for yet.
    hand_size_override: dict[str, int] = {}
    for cid, count in sess.hand_card_counts.items():
        user = sess.player_names.get(cid)
        if not user:
            continue
        try:
            c = game.color_map.get(user)
        except Exception:  # noqa: BLE001
            continue
        hand_size_override[c] = int(count)
    try:
        scores = score_robber_targets(
            game.tracker.game, color,
            hand_size_override=hand_size_override or None,
        )
    except Exception as e:  # noqa: BLE001
        print(f"    [robber] ranking failed: {e}", flush=True)
        return
    if not scores:
        print("    [robber] no legal targets", flush=True)
        return
    print(f"    [robber] you rolled 7 — top {top} targets for {color}:",
          flush=True)
    for i, s in enumerate(scores[:top], start=1):
        coord_str = f"({s.coord[0]},{s.coord[1]},{s.coord[2]})"
        tile_str = ("DESERT" if s.resource is None
                    else f"{s.resource[:3]}{s.number or ''}")
        if s.victims:
            victim_str = ", ".join(
                f"{c}({p}p/{s.victim_vp.get(c,0)}VP/"
                f"{s.opponent_hand_size.get(c,0)}c)"
                for c, p in s.victims.items()
            )
        else:
            victim_str = "—"
        print(f"        {i}. {coord_str:<12} {tile_str:<8} "
              f"score={s.score:+5.1f}  {victim_str}", flush=True)


def _print_self_advisor(game) -> None:
    """Print a compact what-can-I-build line for the self-player."""
    if not game.started:
        return
    sess = game.session
    if sess is None or sess.self_color_id is None:
        return
    username = sess.player_names.get(sess.self_color_id)
    if not username:
        return
    color = game.color_map.get(username)
    hand = game.tracker.hand(color)
    cards = sum(hand.values())
    afford = []
    if all(hand.get(r, 0) >= n for r, n in
           (("WOOD", 1), ("BRICK", 1), ("SHEEP", 1), ("WHEAT", 1))):
        afford.append("settlement")
    if hand.get("WHEAT", 0) >= 2 and hand.get("ORE", 0) >= 3:
        afford.append("city")
    if hand.get("WOOD", 0) >= 1 and hand.get("BRICK", 0) >= 1:
        afford.append("road")
    if (hand.get("WHEAT", 0) >= 1 and hand.get("SHEEP", 0) >= 1
            and hand.get("ORE", 0) >= 1):
        afford.append("dev card")
    # Two-letter abbreviations so Wood and Wheat don't collide.
    abbrev = {"WOOD": "Wd", "BRICK": "Br", "SHEEP": "Sh",
              "WHEAT": "Wh", "ORE": "Or"}
    hand_str = " ".join(
        f"{n}{abbrev.get(r, r[:2])}"
        for r, n in hand.items() if n
    ) or "∅"
    buildable = ", ".join(afford) if afford else "nothing"
    print(f"    [you] {color} {cards}c ({hand_str}) → can build: "
          f"{buildable}", flush=True)

    # Opponent hand sizes in a second line — just counts, since per-
    # resource breakdowns are hidden. Helpful context for trade and
    # robber decisions even when no 7 has rolled yet.
    opp_parts = []
    for cid, count in sorted(sess.hand_card_counts.items()):
        if cid == sess.self_color_id:
            continue
        user = sess.player_names.get(cid)
        if not user:
            continue
        try:
            c = game.color_map.get(user)
        except Exception:  # noqa: BLE001
            continue
        opp_parts.append(f"{c} {count}c")
    if opp_parts:
        print(f"    [opp] {' · '.join(opp_parts)}", flush=True)


def _print_event(payload: dict[str, Any], n: int) -> None:
    """Human-readable stdout echo so you can tail the bridge live.

    Shows the structured parse on the first line and (for anything
    we can't classify yet) the raw payload on a second line so we can
    add rules for the misses.
    """
    from cataanbot.events import UnknownEvent
    from cataanbot.parser import parse_event

    ts = payload.get("ts")
    if ts is None:
        ts = time.time()
    ts_str = time.strftime("%H:%M:%S", time.localtime(ts))

    event = parse_event(payload)
    cls = type(event).__name__
    print(f"[{ts_str} #{n:04d}] {cls}: {_event_oneliner(event)}", flush=True)
    if isinstance(event, UnknownEvent):
        text = (payload.get("text") or "").strip()
        icons = [i.get("alt", "") for i in payload.get("icons") or []]
        print(f"           raw: {text}  icons={icons}", flush=True)


def _event_oneliner(event: Any) -> str:
    """Compact human-readable summary of a parsed Event."""
    from cataanbot.events import (
        BuildEvent, DevCardBuyEvent, DevCardPlayEvent, DiscardEvent,
        DisconnectEvent, GameOverEvent, InfoEvent, MonopolyStealEvent,
        NoStealEvent, ProduceEvent, RobberMoveEvent, RollBlockedEvent,
        RollEvent, StealEvent, TradeCommitEvent, TradeOfferEvent,
        UnknownEvent, VPEvent,
    )

    if isinstance(event, RollEvent):
        return f"{event.player} rolled {event.total} ({event.d1}+{event.d2})"
    if isinstance(event, ProduceEvent):
        return f"{event.player} got {_fmt_res(event.resources)}"
    if isinstance(event, BuildEvent):
        vp = f" +{event.vp_delta} VP" if event.vp_delta else ""
        return f"{event.player} built {event.piece}{vp}"
    if isinstance(event, DiscardEvent):
        return f"{event.player} discarded {_fmt_res(event.resources)}"
    if isinstance(event, RobberMoveEvent):
        prob = f" (prob {event.prob})" if event.prob is not None else ""
        return f"{event.player} moved robber → {event.tile_label}{prob}"
    if isinstance(event, StealEvent):
        res = f" [{event.resource}]" if event.resource else ""
        return f"{event.thief} stole from {event.victim}{res}"
    if isinstance(event, NoStealEvent):
        return "no one to steal from"
    if isinstance(event, TradeOfferEvent):
        return (f"{event.player} offers {_fmt_res(event.give)} for "
                f"{_fmt_res(event.want) or '?'}")
    if isinstance(event, TradeCommitEvent):
        return (f"{event.giver} gave {_fmt_res(event.gave)} and got "
                f"{_fmt_res(event.got)} from {event.receiver}")
    if isinstance(event, DevCardBuyEvent):
        return f"{event.player} bought dev card"
    if isinstance(event, DevCardPlayEvent):
        extra = ""
        if event.resources:
            extra = f" → {_fmt_res(event.resources)}"
        elif event.resource:
            extra = f" → {event.resource}"
        return f"{event.player} played {event.card}{extra}"
    if isinstance(event, MonopolyStealEvent):
        return (f"{event.player} monopolied {event.count}x{event.resource} "
                f"from opponents")
    if isinstance(event, VPEvent):
        frm = f" (from {event.previous_holder})" if event.previous_holder else ""
        return f"{event.player} +{event.vp_delta} VP ({event.reason}){frm}"
    if isinstance(event, RollBlockedEvent):
        prob = f" (prob {event.prob})" if event.prob is not None else ""
        return f"robber blocks {event.tile_label}{prob} — no production"
    if isinstance(event, GameOverEvent):
        return f"GAME OVER — {event.winner} won"
    if isinstance(event, InfoEvent):
        return f"info: {event.text}"
    if isinstance(event, DisconnectEvent):
        return f"{event.player} {'reconnected' if event.reconnected else 'disconnected'}"
    if isinstance(event, UnknownEvent):
        return "?"
    return str(event)


def _fmt_res(resources: dict[str, int]) -> str:
    if not resources:
        return ""
    return " ".join(f"{count}x{name}" for name, count in resources.items())


def serve(host: str = "127.0.0.1", port: int = 8765,
          jsonl: str | None = None,
          ws_jsonl: str | None = None,
          advisor: bool = False,
          postmortem_dir: str | None = None) -> int:
    """Run the bridge with uvicorn. Blocks until Ctrl-C."""
    try:
        import uvicorn
    except ImportError:
        print("bridge deps missing — install with: "
              "pip install -e '.[bridge]'")
        return 1

    jsonl_path = Path(jsonl).expanduser() if jsonl else None
    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"mirroring log events to {jsonl_path}")

    ws_jsonl_path = Path(ws_jsonl).expanduser() if ws_jsonl else None
    if ws_jsonl_path is not None:
        ws_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"mirroring WS frames to {ws_jsonl_path}")

    pm_dir: Path | None
    if postmortem_dir is None:
        pm_dir = Path.home() / "Desktop" / "CataanBot" / "postmortems"
    elif postmortem_dir == "":
        pm_dir = None  # explicit opt-out
    else:
        pm_dir = Path(postmortem_dir).expanduser()
    if pm_dir is not None:
        print(f"auto-postmortem will write to {pm_dir}/")

    app = _build_app(jsonl_path=jsonl_path, ws_jsonl_path=ws_jsonl_path,
                     advisor=advisor, postmortem_dir=pm_dir)
    print(f"cataanbot bridge listening on http://{host}:{port}")
    print("POST  /log      — userscript DOM log events")
    print("POST  /ws       — userscript WebSocket frames")
    print("GET   /         — health + counters + game state")
    print("GET   /advisor  — compact advisor snapshot (for the overlay)")
    print("POST  /reset    — clear game state and counters")
    if advisor:
        print("advisor output: ON")
    print("Ctrl-C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
