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
        # Full-game tally of each 2..12 dice total so the HUD can plot
        # a distribution chart — complementary to the last-10 window.
        # Resets only on /reset.
        "roll_histogram": {i: 0 for i in range(2, 13)},
        # total_rolls value at the moment the robber last moved onto a
        # self tile. None until the first robber-on-me move. Used to
        # enrich the robber_on_me banner with "placed N rolls ago" —
        # a direct persistence signal. blocks_recent alone can read
        # "0" even when the robber has sat there forever (if the number
        # hasn't come up), so persistence and cost are complementary.
        "robber_moved_at_rolls": None,
        "robber_pending": False,  # self rolled 7, hasn't placed robber yet
        "robber_snapshot": None,  # cached score_robber_targets payload
        # Ring-buffer of card counts per color, one sample per roll.
        # Capped at 5 samples so the delta window stays meaningful —
        # long history would blur "just snowballed" into "always big".
        # Keyed by int cid so the same shape survives color-swap resets.
        "opp_card_hist": {},
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
        st["roll_histogram"] = {i: 0 for i in range(2, 13)}
        st["robber_moved_at_rolls"] = None
        st["robber_pending"] = False
        st["robber_snapshot"] = None
        st["opp_card_hist"] = {}
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
        # Drop the urgency — self no longer needs to *pick* — but
        # keep the snapshot around so the overlay's robber panel
        # stays visible through the steal + rest of the turn. Cleared
        # on the next RollEvent (or instantly if an opponent rolls a
        # new 7) in _track_overlay_state.
        st["robber_pending"] = False

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
            # Full-game distribution tally — complements roll_history
            # (last 10 only) so the overlay can render a chart across
            # all rolls.
            rh = st.setdefault(
                "roll_histogram", {i: 0 for i in range(2, 13)})
            if isinstance(r.event.total, int) and 2 <= r.event.total <= 12:
                rh[r.event.total] = int(rh.get(r.event.total, 0)) + 1
            # Snapshot each player's card count per-roll so the snap
            # builder can compute a hand-growth delta. Ring buffer of 5
            # samples means we can answer "+3 cards in the last 3 rolls"
            # even after a couple of rolls of churn. Done on every roll
            # including 7s — a robber steal actually drops the victim's
            # count, which is itself a signal worth keeping.
            try:
                card_hist = st.setdefault("opp_card_hist", {})
                for cid, count in game.session.hand_card_counts.items():
                    series = card_hist.setdefault(int(cid), [])
                    series.append(int(count))
                    if len(series) > 5:
                        del series[0]
            except Exception as e:  # noqa: BLE001
                print(f"[overlay] card hist snapshot failed: {e!r}",
                      flush=True)
            if r.event.total == 7 and is_you:
                st["robber_pending"] = True
                st["robber_snapshot"] = _compute_robber_snapshot(
                    game, display_colors=st["display_colors"])
            elif r.event.total == 7:
                # Opponent rolled 7 — you don't pick, clear any stale
                # overlay ranking from a prior self-roll if somehow still set.
                st["robber_pending"] = False
                st["robber_snapshot"] = None
            else:
                # Fresh non-7 roll — if we were holding a post-placement
                # snapshot from an earlier 7-roll or played knight, the
                # review window for that placement is over. Clear it so
                # the robber panel doesn't cling to stale data. The
                # knight-held path in the snap builder will refill
                # targets on the next poll if self still has a knight.
                if not st.get("robber_pending"):
                    st["robber_snapshot"] = None
        elif isinstance(r.event, RobberMoveEvent):
            # Urgency ends the moment the robber lands, but keep the
            # snapshot visible so Noah can reflect on the placement
            # (and steal outcome) through the rest of the turn. Cleared
            # on the next non-7 RollEvent above.
            st["robber_pending"] = False
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
    display_colors: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Pick the best resource to steal when self plays Monopoly.

    Fires only when self holds at least one MONOPOLY card. Ranks each
    resource by the inferred total held across opps; ties break toward
    resources that would unlock an immediate build for self. Carries a
    PLAY/HOLD verdict (unlock or big-pot → PLAY; small pot w/ no unlock
    → HOLD) and the top opp holder so Noah can see where the cards are
    coming from.
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
    # Aggregate inferred counts across opps via the tracker. We also
    # remember the per-opp split so we can spotlight the top holder —
    # monopoly steals from everyone, but Noah wants to know whose stack
    # he's draining the most (it informs follow-up trade/robber calls).
    totals: dict[str, int] = {
        "WOOD": 0, "BRICK": 0, "SHEEP": 0, "WHEAT": 0, "ORE": 0,
    }
    per_opp: dict[str, dict[str, int]] = {}
    for opp_color in state.color_to_index:
        if opp_color == my_enum:
            continue
        try:
            opp_hand = game.tracker.hand(opp_color.value)
        except Exception:  # noqa: BLE001
            continue
        counts: dict[str, int] = {}
        for r, n in opp_hand.items():
            if r in totals:
                totals[r] += int(n)
                counts[r] = int(n)
        per_opp[opp_color.value] = counts
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

    # Verdict: PLAY when it unlocks or when the pot is large enough to
    # swing tempo (4+ cards is a full settlement's worth of resources).
    # HOLD when the pot is small AND no unlock — you'll get more value
    # letting opps accumulate. The 4-card threshold is intentionally
    # slightly above a single-opp production spike so we don't fire
    # PLAY on a one-roll lucky stack.
    should_play = False
    if unlock_reason:
        should_play = True
        reason = unlock_reason
    elif best_count >= 4:
        should_play = True
        reason = f"large pot ({best_count} cards)"
    else:
        reason = f"small pot ({best_count}) — wait for more"

    # Top holder: the single opp contributing the most to best_count.
    # Used by the overlay to render "drains 4 from noah" as a sub-line.
    top_holder_color: str | None = None
    top_holder_count = 0
    for color_val, counts in per_opp.items():
        n = counts.get(best_res, 0)
        if n > top_holder_count:
            top_holder_count = n
            top_holder_color = color_val
    top_holder: dict[str, Any] | None = None
    if top_holder_color is not None and top_holder_count > 0:
        dc = (display_colors or {}).get(top_holder_color, top_holder_color)
        top_holder = {
            "color": top_holder_color,
            "display": dc,
            "count": top_holder_count,
        }

    return {
        "have": held,
        "should_play": should_play,
        "reason": reason,
        "resource": best_res,
        "est_steal": best_count,
        "totals": totals,
        "unlock": unlock_reason,
        "top_holder": top_holder,
    }


def _compute_yop_hint(
    game, self_color: str, self_hand: dict[str, int],
    bank_supply: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Suggest which pair to pick with Year-of-Plenty.

    Fires only when self holds at least one YEAR_OF_PLENTY card. Picks
    the pair that unlocks the most valuable buildable; falls back to
    the pair that aligns with the costliest build closest to complete.

    When no pair would unlock anything this turn, still surface the
    hint with should_play=False so the overlay can render a HOLD
    verdict rather than silently hiding the card. If the bank is
    completely out of a resource in the chosen pair, the YoP play
    can't actually grant that card — flag bank_ok=False so Noah knows
    before spending the card.
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

    # No unlock within reach: surface a HOLD verdict pointed at the
    # cheapest build's deficit resource so Noah still sees the card.
    # Pair: two of the single resource most in demand across all builds
    # (weighted by priority). Default to ORE+WHEAT (city pair) as a
    # safe-ish hoard pick when we can't infer anything.
    if best is None:
        demand: dict[str, float] = {r: 0.0 for r in (
            "WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")}
        for name, cost in _BUILD_COSTS_MONOPOLY.items():
            w = priority.get(name, 1)
            for r, n in cost.items():
                d = n - self_hand.get(r, 0)
                if d > 0:
                    demand[r] += float(w * d)
        ranked = sorted(demand.items(),
                        key=lambda kv: kv[1], reverse=True)
        top_r = ranked[0][0] if ranked and ranked[0][1] > 0 else "ORE"
        second_r = (ranked[1][0] if len(ranked) > 1 and ranked[1][1] > 0
                    else "WHEAT")
        pair = [top_r, second_r] if top_r != second_r else [top_r, top_r]
        return {
            "have": held,
            "should_play": False,
            "reason": "no build within reach — hold",
            "pair": pair,
            "unlock": None,
            "bank_ok": True,
        }

    _, build_name, pair = best

    # Bank-supply guard: YoP can't grant a resource the bank is out of.
    # If either pick is unavailable, flag it — Noah should trade/port
    # or pick a different pair.
    bank_ok = True
    if bank_supply and isinstance(bank_supply.get("remaining"), dict):
        remaining = bank_supply["remaining"]
        needed: dict[str, int] = {}
        for r in pair:
            needed[r] = needed.get(r, 0) + 1
        for r, n in needed.items():
            if int(remaining.get(r, 0)) < n:
                bank_ok = False
                break

    reason = f"unlocks {build_name}"
    if not bank_ok:
        reason = f"bank short on {' or '.join(sorted(set(pair)))} — verify"

    return {
        "have": held,
        "should_play": bank_ok,  # If bank can't grant the pair, don't PLAY yet
        "reason": reason,
        "pair": pair,
        "unlock": build_name,
        "bank_ok": bank_ok,
    }


def _suggest_rb_placement(
    game, self_color_enum,
) -> dict[str, Any] | None:
    """Pick the best pair of free roads to lay when Road Building plays.

    Search strategy is intentionally local (no full minimax): walk
    out-edges from self's road network, then the out-edges after
    hypothetically laying each first pick. Pairs that land on a
    settlement-buildable node get ranked by that node's opening-score;
    a single-edge unlock is preferred over a 2-edge reach (less
    commitment, same reward). If no unlock is available, fall back to
    the pair that extends the longest continuous chain the most.

    Returns ``{edges, toward_node, toward_tiles, direction,
    placement_reason}`` or None when self has no legal road build.
    """
    from catanatron import Color  # noqa: F401 — only for typing clarity
    from cataanbot.advisor import (
        _build_node_neighbors, score_opening_nodes,
    )
    from cataanbot.recommender import (
        _direction_label, _node_positions, _tile_label,
    )

    board = game.state.board
    m = board.map
    neighbors = _build_node_neighbors(m)
    try:
        first_edges = list(board.buildable_edges(self_color_enum))
    except Exception:  # noqa: BLE001
        return None
    if not first_edges:
        return None

    # Distance-2 legal-settlement filter mirrors the opening scorer.
    blocked: set[int] = set()
    for nid, (col, bt) in board.buildings.items():
        if bt in ("SETTLEMENT", "CITY"):
            blocked.add(int(nid))
            blocked |= {int(x) for x in neighbors.get(int(nid), set())}
    scored = {ns.node_id: ns for ns in score_opening_nodes(game)}

    def node_is_buildable(nid: int) -> bool:
        return nid not in blocked and nid in scored

    # My network endpoints. We need this to score "longest-path gain" as
    # a fallback when no unlock is available. Chain length is just the
    # count of consecutive edges reachable from any of my network nodes
    # including the two new ones.
    my_edges: set[frozenset[int]] = set()
    my_nodes: set[int] = set()
    for (a, b), col in board.roads.items():
        if col == self_color_enum:
            my_edges.add(frozenset((int(a), int(b))))
            my_nodes.add(int(a))
            my_nodes.add(int(b))
    for nid, (col, bt) in board.buildings.items():
        if col == self_color_enum:
            my_nodes.add(int(nid))

    enemy_bld_nodes: set[int] = {
        int(nid) for nid, (col, bt) in board.buildings.items()
        if col != self_color_enum
    }

    def step2_edges_from(far_node: int, first_edge: tuple[int, int]):
        """Legal edges to build on a board where ``first_edge`` has been
        laid. Rules: can't step through an enemy settle/city; can't reuse
        an existing road."""
        out: list[tuple[int, int]] = []
        if far_node in enemy_bld_nodes:
            return out
        for nb in neighbors.get(int(far_node), ()):
            e2 = (int(far_node), int(nb))
            if nb == first_edge[0]:
                continue
            existing = (board.roads.get(e2)
                        or board.roads.get((e2[1], e2[0])))
            if existing is not None:
                continue
            out.append(e2)
        return out

    def longest_path_from(new_edges: set[frozenset[int]]) -> int:
        """Rough longest continuous chain on (my_edges ∪ new_edges).
        Not topology-perfect — we just DFS from each endpoint of a new
        edge and count the longest simple path. Good enough to rank
        extension candidates relative to each other."""
        g = my_edges | new_edges
        if not g:
            return 0
        adj: dict[int, set[int]] = {}
        for e in g:
            a, b = tuple(e)
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        # Enemy nodes break chains the same way they break road-legality.
        best_len = 0
        seeds = set()
        for e in new_edges:
            seeds |= set(e)
        if not seeds:
            seeds = set(adj)
        for start in seeds:
            stack = [(start, frozenset(), 0)]
            while stack:
                node, used, length = stack.pop()
                if length > best_len:
                    best_len = length
                for nb in adj.get(node, ()):
                    if nb in enemy_bld_nodes and nb != start:
                        continue
                    edge = frozenset((node, nb))
                    if edge in used:
                        continue
                    stack.append((nb, used | {edge}, length + 1))
        return best_len

    positions = _node_positions(m)

    # (score, tag, edges, toward_node). Higher score wins.
    candidates: list[tuple[float, str, list[tuple[int, int]], int]] = []
    for (a1, b1) in first_edges:
        new1 = {frozenset((int(a1), int(b1)))}
        # Case A: single-edge unlock at b1.
        if node_is_buildable(int(b1)):
            sc = float(scored[int(b1)].score)
            candidates.append((
                sc * 10.0 + 5.0,  # +5 bonus: 1-edge cost beats 2-edge
                "unlocks settlement",
                [(int(a1), int(b1))],
                int(b1),
            ))
        # Case B: 2-edge unlock at b2.
        for (a2, b2) in step2_edges_from(int(b1), (int(a1), int(b1))):
            if node_is_buildable(int(b2)):
                sc = float(scored[int(b2)].score)
                candidates.append((
                    sc * 10.0,
                    "unlocks 2-hop settle",
                    [(int(a1), int(b1)), (int(a2), int(b2))],
                    int(b2),
                ))
        # Case C (fallback): pure longest-road extension. Gets ranked
        # below any unlock — unlock score starts at >= _score_opening(0)
        # ≈ 2, so chain-only scores cap below 2.
        for (a2, b2) in step2_edges_from(int(b1), (int(a1), int(b1))):
            new2 = new1 | {frozenset((int(a2), int(b2)))}
            chain = longest_path_from(new2)
            candidates.append((
                0.05 * float(chain),
                f"extends chain to {chain}",
                [(int(a1), int(b1)), (int(a2), int(b2))],
                int(b2),
            ))
        # Single-edge chain extension (when no second edge is legal).
        chain1 = longest_path_from(new1)
        candidates.append((
            0.04 * float(chain1),
            f"extends chain to {chain1}",
            [(int(a1), int(b1))],
            int(b1),
        ))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    _, tag, edges, toward = candidates[0]
    out: dict[str, Any] = {
        "edges": [list(e) for e in edges],
        "toward_node": int(toward),
        "toward_tiles": _tile_label(m, int(toward)),
        "placement_reason": tag,
    }
    # Direction of the FIRST edge — read "lay a road right toward
    # [wheat 6]" as the primary action. Second edge direction is implied
    # by the chain and would be noise in the overlay.
    dir_lbl = _direction_label(positions, edges[0][0], edges[0][1])
    if dir_lbl is not None:
        out["direction"] = {"word": dir_lbl[0], "arrow": dir_lbl[1]}
    return out


def _compute_rb_hint(game, self_color: str) -> dict[str, Any] | None:
    """Recommend whether to play Road Building this turn.

    Fires only when self holds a ROAD_BUILDING card AND has at least
    one road piece left to place. Two free roads is worth the most
    when it swings longest road — either qualifying self or catching
    an opp who's about to. Secondary case: road supply is almost
    exhausted, so play while the cards are still useful.

    Returns ``{have, should_play, reason, self_len, opp_len, placement?}``
    or None when we shouldn't surface a hint. The projected length is a
    naive +2 to self's current chain — catanatron recomputes
    topology-aware length after play, so this is a hint upper bound,
    not a promise. ``placement`` carries the concrete pair of edges to
    lay when we can compute one.
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

    out: dict[str, Any] = {
        "have": held,
        "should_play": should,
        "reason": reason,
        "self_len": self_len,
        "opp_len": opp_max,
    }
    try:
        placement = _suggest_rb_placement(game.tracker.game, my_enum)
        if placement is not None:
            out["placement"] = placement
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] rb placement failed: {e!r}", flush=True)
    return out


def _compute_game_plan(
    game, self_color: str, hand: dict[str, int],
) -> dict[str, Any] | None:
    """Compose a multi-step plan toward the next meaningful goal.

    Reads like a chess principal variation — "2 roads then settle · 4
    wood→brick if stuck" — so Noah can mid-turn stay on a plan instead
    of picking from a flat list each time.

    Search finds the highest pip-prod settlement spot within 2 road
    hops of self's network (0-hop = already connected, 1-hop = one
    road away, 2-hop = two roads away). Costs out the full plan
    (roads + settlement), diffs against self's hand, and if short
    picks a trade-fallback the user could lean on: prefers a port 2:1
    or 3:1 when self owns one, falling back to 4:1 bank. Falls back
    to a city plan when no settle is reachable.

    Returns ``None`` during setup or when we can't compute anything
    meaningful. Otherwise ``{goal_kind, goal_label, goal_node?,
    goal_tiles, roads_needed, missing, trade_plan?, summary}``.
    """
    from catanatron import Color
    try:
        my_enum = (self_color if isinstance(self_color, Color)
                   else Color[str(self_color).upper()])
    except Exception:  # noqa: BLE001
        return None

    # Setup phase plans live in the opening recs, not here.
    try:
        my_idx = game.tracker.game.state.color_to_index.get(my_enum)
        if my_idx is None:
            return None
        placed = int(game.tracker.game.state.player_state.get(
            f"P{my_idx}_SETTLEMENTS_AVAILABLE", 5))
        # Fewer than 3 means we've played at least 2 settles (opening
        # done). If we still have 4+ available, we're mid-setup.
        if placed >= 4:
            return None
    except Exception:  # noqa: BLE001
        pass

    from cataanbot.advisor import _build_node_neighbors, player_ports
    from cataanbot.recommender import (
        _SETTLEMENT_COST, _CITY_COST, _ROAD_COST,
        _node_pip_production, _tile_label,
    )

    cat = game.tracker.game
    board = cat.state.board
    m = board.map
    neighbors = _build_node_neighbors(m)
    land = set(m.land_nodes)

    # Distance-2 blocked nodes — can't settle adjacent to any building.
    buildings = board.buildings
    blocked: set[int] = {int(x) for x in buildings.keys()}
    for nid in list(buildings.keys()):
        blocked |= {int(x) for x in neighbors.get(int(nid), set())}

    # My road-network nodes + my building nodes; enemy settles/cities
    # break road-legality the same way they block adjacent settlement
    # placement.
    my_nodes: set[int] = set()
    for (a, b), rc in board.roads.items():
        if rc == my_enum:
            my_nodes.add(int(a)); my_nodes.add(int(b))
    for nid, (col, _bt) in buildings.items():
        if col == my_enum:
            my_nodes.add(int(nid))
    enemy_bld_nodes: set[int] = {
        int(nid) for nid, (col, _bt) in buildings.items()
        if col != my_enum
    }
    my_edges: set[frozenset[int]] = {
        frozenset((int(a), int(b))) for (a, b), rc in board.roads.items()
        if rc == my_enum
    }

    def reach_hops(target: int) -> int | None:
        """BFS from my network to target — minimum roads needed to
        reach it. Returns 0 if already connected, 1 or 2 for roads
        needed, None when further than 2 hops. Stops at enemy buildings
        (they block road-legality)."""
        if target in my_nodes:
            return 0
        frontier: list[tuple[int, int]] = [(n, 0) for n in my_nodes]
        visited = set(my_nodes)
        while frontier:
            node, hops = frontier.pop(0)
            if hops >= 2:
                continue
            for nb in neighbors.get(node, ()):
                if nb in visited:
                    continue
                visited.add(nb)
                if nb == target:
                    return hops + 1
                if nb in enemy_bld_nodes:
                    continue
                frontier.append((nb, hops + 1))
        return None

    # Rank candidate settlement targets: prefer fewer hops, then higher
    # pip production. Filter to reachable-in-2 land nodes that aren't
    # distance-2 blocked and aren't already my own building.
    best: tuple[int, int, float] | None = None  # (hops, node, prod)
    for nid in land:
        if nid in blocked:
            continue
        hops = reach_hops(int(nid))
        if hops is None:
            continue
        prod = _node_pip_production(m, int(nid))
        if prod <= 0:
            continue
        # Sort key: (hops, -prod). Lower hops win ties go to higher prod.
        if best is None:
            best = (hops, int(nid), prod)
        else:
            if (hops, -prod) < (best[0], -best[2]):
                best = (hops, int(nid), prod)

    # No reachable settle within 2 hops → fall back to city goal.
    if best is None:
        # Pick my highest-prod settlement as the city target.
        city_best: tuple[int, float] | None = None
        for nid, (col, bt) in buildings.items():
            if col != my_enum or bt != "SETTLEMENT":
                continue
            prod = _node_pip_production(m, int(nid))
            if city_best is None or prod > city_best[1]:
                city_best = (int(nid), prod)
        if city_best is None:
            return None
        node, prod = city_best
        cost = _CITY_COST
        missing = {r: max(0, cost.get(r, 0) - hand.get(r, 0))
                   for r in ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")}
        missing = {r: n for r, n in missing.items() if n > 0}
        trade_plan = _plan_trade_fallback(cat, my_enum, hand, cost, missing)
        tiles = _tile_label(m, node)
        if missing:
            summary = (f"city at {_short_tile_label(tiles)} · "
                       f"{_format_missing_short(missing)}")
        else:
            summary = f"city at {_short_tile_label(tiles)} now"
        if trade_plan:
            summary += (f" · {trade_plan['ratio']}:1 "
                        f"{_emoji_for(trade_plan['from_res'])}"
                        f"→{_emoji_for(trade_plan['to_res'])} if stuck")
        return {
            "goal_kind": "city",
            "goal_label": f"city at {_short_tile_label(tiles)}",
            "goal_node": node,
            "goal_tiles": tiles,
            "roads_needed": 0,
            "missing": missing,
            "trade_plan": trade_plan,
            "summary": summary,
        }

    hops, node, prod = best
    # Total plan cost = hops × road + 1 settlement.
    cost: dict[str, int] = {
        k: 0 for k in ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE")}
    for r, n in _ROAD_COST.items():
        cost[r] += n * hops
    for r, n in _SETTLEMENT_COST.items():
        cost[r] += n
    missing = {r: max(0, cost[r] - hand.get(r, 0)) for r in cost}
    missing = {r: n for r, n in missing.items() if n > 0}
    trade_plan = _plan_trade_fallback(cat, my_enum, hand, cost, missing)

    tiles = _tile_label(m, node)
    # Compose a short plan string. Reads like Noah's example:
    # "2 roads then settle · 4 wood→brick if stuck".
    parts: list[str] = []
    if hops > 0:
        parts.append(f"{hops} road{'s' if hops > 1 else ''}")
    parts.append(f"settle at {_short_tile_label(tiles)}")
    summary = " → ".join(parts)
    if missing:
        summary += " · " + _format_missing_short(missing)
    if trade_plan:
        summary += (f" · {trade_plan['ratio']}:1 "
                    f"{_emoji_for(trade_plan['from_res'])}"
                    f"→{_emoji_for(trade_plan['to_res'])} if stuck")

    return {
        "goal_kind": "settlement",
        "goal_label": f"settle at {_short_tile_label(tiles)}",
        "goal_node": node,
        "goal_tiles": tiles,
        "roads_needed": hops,
        "missing": missing,
        "trade_plan": trade_plan,
        "summary": summary,
    }


def _short_tile_label(tiles: list[tuple[str, int]] | None) -> str:
    """One-line tile label: "wheat 6 + ore 11". Skip desert (no num)."""
    if not tiles:
        return "?"
    parts = []
    for t in tiles:
        if not t or t[0] == "DESERT":
            continue
        res, num = t[0], t[1]
        parts.append(f"{res.lower()[:3]}{num}" if num else res.lower()[:3])
    return "+".join(parts) if parts else "?"


def _format_missing_short(missing: dict[str, int]) -> str:
    """Compact missing-cards string: "need 1b 1s"."""
    if not missing:
        return ""
    parts = [f"{n}{r[0].lower()}" for r, n in missing.items()]
    return "need " + " ".join(parts)


_RES_EMOJI = {
    "WOOD": "🌲", "BRICK": "🧱", "SHEEP": "🐑",
    "WHEAT": "🌾", "ORE": "⛰️",
}


def _emoji_for(res: str | None) -> str:
    """Resource → emoji used across game-plan + banner trade strings."""
    if not res:
        return "?"
    return _RES_EMOJI.get(res.upper(), res[:3].lower())


def _plan_trade_fallback(
    cat_game, my_enum, hand: dict[str, int], cost: dict[str, int],
    missing: dict[str, int],
) -> dict[str, Any] | None:
    """Pick a single best trade plan to cover the first missing resource.

    Chooses the cheapest ratio available given self's port ownership —
    2:1 specific port, 3:1 generic port, otherwise 4:1 bank. The trade
    source must be a resource we hold in excess (not needed for the
    current plan). Returns None when no legal trade can bridge the gap.
    """
    if not missing:
        return None
    try:
        from cataanbot.advisor import player_ports
        ports = set(player_ports(cat_game, my_enum))
    except Exception:  # noqa: BLE001
        ports = set()
    # "Excess" = hand minus what this plan needs.
    surplus: dict[str, int] = {}
    for r in ("WOOD", "BRICK", "SHEEP", "WHEAT", "ORE"):
        excess = hand.get(r, 0) - cost.get(r, 0)
        if excess > 0:
            surplus[r] = excess
    missing_r = next(iter(missing.keys()))
    best_from: str | None = None
    best_ratio = 99
    for from_r, excess in surplus.items():
        if from_r in ports:
            ratio = 2
        elif "GENERIC" in ports:
            ratio = 3
        else:
            ratio = 4
        if excess >= ratio and ratio < best_ratio:
            best_ratio = ratio
            best_from = from_r
    if best_from is None:
        return None
    return {
        "from_res": best_from,
        "from_count": best_ratio,
        "to_res": missing_r,
        "ratio": best_ratio,
    }


def _compute_strategic_options(
    game, self_color: str, hand: dict[str, int],
) -> list[dict[str, Any]] | None:
    """Surface riskier / longer-horizon plays that the flat rec list
    doesn't cover.

    The default recommender ranks what's affordable **right now** and
    fans out "save for X" plans for 1-2 cards away. That's tight but
    conservative — it misses VP-swing plays that take pieces and turns
    but materially change the endgame:

        * **Longest road push** — when self is at 4 roads (1 away from
          qualifying) and the race is open.
        * **Largest army push** — when self has knights played + held
          ≥ 3 and the LA holder is within 1.
        * **Dev-card dive** — when self is flush on ore+wheat+sheep and
          no higher-value build fits, surface a multi-card buy toward
          hidden VP + the dev-card engine.

    Returns a list of ``{kind, label, detail, vp_swing, pieces}``
    options ordered by expected VP impact. ``None`` when nothing is
    actionable so the overlay can hide the section silently.
    """
    from catanatron import Color
    try:
        my_enum = (self_color if isinstance(self_color, Color)
                   else Color[str(self_color).upper()])
    except Exception:  # noqa: BLE001
        return None

    state = game.tracker.game.state
    my_idx = state.color_to_index.get(my_enum)
    if my_idx is None:
        return None

    # Stay quiet during setup.
    try:
        placed = int(state.player_state.get(
            f"P{my_idx}_SETTLEMENTS_AVAILABLE", 5))
        if placed >= 4:
            return None
    except Exception:  # noqa: BLE001
        pass

    ps = state.player_state
    options: list[dict[str, Any]] = []

    # ---- Longest road push -------------------------------------------
    self_len = int(ps.get(f"P{my_idx}_LONGEST_ROAD_LENGTH", 0))
    self_has_lr = bool(ps.get(f"P{my_idx}_HAS_ROAD", False))
    opp_lr_max = 0
    opp_lr_holder = False
    for col, idx in state.color_to_index.items():
        if col == my_enum:
            continue
        ol = int(ps.get(f"P{idx}_LONGEST_ROAD_LENGTH", 0))
        oh = bool(ps.get(f"P{idx}_HAS_ROAD", False))
        if ol > opp_lr_max:
            opp_lr_max = ol
        if oh:
            opp_lr_holder = True
    if (self_len >= 3 and not self_has_lr
            and self_len + 1 >= max(5, opp_lr_max + 1)):
        # 1 more segment qualifies us (5+) and beats the current opp.
        roads_needed = max(1, max(5, opp_lr_max + 1) - self_len)
        vp_swing = 2 if not opp_lr_holder else 4  # take + denial
        options.append({
            "kind": "longest_road_push",
            "label": "push longest road",
            "detail": (f"+{roads_needed} road"
                       f"{'s' if roads_needed > 1 else ''}"
                       f" to take LR"
                       + (" (denies opp)" if opp_lr_holder else "")),
            "vp_swing": vp_swing,
            "pieces": roads_needed,
        })

    # ---- Largest army push -------------------------------------------
    knights_played = int(ps.get(f"P{my_idx}_PLAYED_KNIGHT", 0))
    knights_held = int(ps.get(f"P{my_idx}_KNIGHT_IN_HAND", 0))
    self_has_la = bool(ps.get(f"P{my_idx}_HAS_ARMY", False))
    opp_knights_max = 0
    opp_la_holder = False
    for col, idx in state.color_to_index.items():
        if col == my_enum:
            continue
        ok = int(ps.get(f"P{idx}_PLAYED_KNIGHT", 0))
        oh = bool(ps.get(f"P{idx}_HAS_ARMY", False))
        if ok > opp_knights_max:
            opp_knights_max = ok
        if oh:
            opp_la_holder = True
    la_threshold = max(3, opp_knights_max + 1)
    needed_plays = max(0, la_threshold - knights_played)
    if (not self_has_la and knights_held >= 1
            and knights_played + knights_held >= la_threshold
            and needed_plays > 0):
        vp_swing = 2 if not opp_la_holder else 4
        options.append({
            "kind": "largest_army_push",
            "label": "push largest army",
            "detail": (f"play {needed_plays} knight"
                       f"{'s' if needed_plays > 1 else ''} to take LA"
                       + (" (denies opp)" if opp_la_holder else "")),
            "vp_swing": vp_swing,
            "pieces": needed_plays,
        })

    # ---- Dev-card dive ------------------------------------------------
    # When self has multiple dev-card buys stacked (3+ full bundles of
    # ore+wheat+sheep) and the board has nothing better to spend them
    # on — worth surfacing as a hidden-VP play.
    bundles = min(hand.get("ORE", 0),
                  hand.get("WHEAT", 0),
                  hand.get("SHEEP", 0))
    if bundles >= 3:
        options.append({
            "kind": "dev_card_dive",
            "label": "dev-card dive",
            "detail": (f"buy {min(bundles, 4)} dev cards — hidden VP"
                       " + knight/RB/YoP engine"),
            "vp_swing": 1,
            "pieces": 0,
        })

    if not options:
        return None
    # Higher VP swing first, then by fewer pieces needed (cheaper path).
    options.sort(key=lambda o: (-o["vp_swing"], o["pieces"]))
    return options


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
    # Name the leading opp (by length) so messages say "alice" not "opp".
    # Ties broken by whoever currently holds the title, then by iteration
    # order — same across calls so the banner doesn't flip-flop.
    top_opp = max(
        opps,
        key=lambda e: (e[1], 1 if e[2] else 0),
        default=None,
    )
    opp_max = top_opp[1] if top_opp else 0
    opp_holder_entry = next((e for e in opps if e[2]), None)
    opp_holder = opp_holder_entry is not None
    color_map = getattr(game, "color_map", None)

    def _name_for(entry) -> str:
        if entry is None or color_map is None:
            return "opp"
        col = entry[0]
        col_str = col.value if hasattr(col, "value") else str(col)
        uname = color_map.reverse(col_str)
        return uname or "opp"

    top_opp_name = _name_for(top_opp)
    holder_name = _name_for(opp_holder_entry) if opp_holder else None

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
        if self_has:
            holder = "you"
        elif holder_name:
            holder = holder_name
        else:
            holder = "nobody"
        return {
            "level": "contested",
            "self_len": self_len,
            "opp_len": opp_max,
            "opp_username": top_opp_name,
            "holder_username": holder_name,
            "message": (
                f"longest road · you {self_len} / "
                f"{top_opp_name} {opp_max} · {holder} holds"),
        }
    # Self pushing: we're on 4+, nobody else is close.
    if self_len >= 4 and not self_has and opp_max < self_len:
        return {
            "level": "self_push",
            "self_len": self_len,
            "opp_len": opp_max,
            "opp_username": top_opp_name,
            "message": f"1 road → longest road ({self_len})",
        }
    # Opp threat: someone else is on 4+ and ahead of us.
    if opp_max >= 4 and opp_max >= self_len and not self_has:
        gap = opp_max - self_len
        if opp_holder:
            msg = (
                f"{holder_name or top_opp_name} has longest road"
                f" · {opp_max} (+{gap})"
            )
        else:
            msg = f"{top_opp_name} 1 → longest road ({opp_max})"
        return {
            "level": "opp_threat",
            "self_len": self_len,
            "opp_len": opp_max,
            "opp_username": top_opp_name,
            "holder_username": holder_name,
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


def _closest_missing_build(
    hand: dict[str, int],
) -> dict[str, Any] | None:
    """The build with the smallest resource gap given ``hand``.

    When ``hand`` already covers every build, returns None — there's
    nothing to point at. Otherwise returns the nearest-miss: the build
    with the smallest sum of missing cards, with ties broken by VP
    impact (city > settlement > dev > road).

    The HUD uses this to turn "nothing buildable" into "1 brick from
    settle" — a direction of travel rather than a dead-end read.
    """
    if not isinstance(hand, dict):
        return None
    candidates: list[dict[str, Any]] = []
    # Preserves the VP-impact tie-break order — first-in-ties wins.
    BUILDS: tuple[tuple[str, dict[str, int]], ...] = (
        ("city", {"WHEAT": 2, "ORE": 3}),
        ("settlement", {"WOOD": 1, "BRICK": 1, "SHEEP": 1, "WHEAT": 1}),
        ("dev card", {"WHEAT": 1, "SHEEP": 1, "ORE": 1}),
        ("road", {"WOOD": 1, "BRICK": 1}),
    )
    for name, cost in BUILDS:
        missing: dict[str, int] = {}
        for r, n in cost.items():
            have = int(hand.get(r, 0) or 0)
            if have < n:
                missing[r] = n - have
        if not missing:
            continue  # fully affordable — not the "next" build
        gap = sum(missing.values())
        candidates.append({
            "build": name, "missing": missing, "gap": gap,
        })
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["gap"])
    return candidates[0]


def _is_dev_stash_risk(
    vp: int, dev_cards: int, vp_target: int | None = None,
) -> bool:
    """Whether an opp's dev-card stash is a hidden-VP risk.

    True when dev_cards >= 2 AND (vp + dev_cards) >= (VP_TARGET - 1).
    The ``>= 2`` floor avoids false-positiving on every late-game opp
    holding a single knight. The sum threshold models "if they flipped
    every dev as a VP, they'd be within 1 of winning" — which is when
    holding onto them stops looking like a knight race and starts
    looking like a hidden-VP play.
    """
    from cataanbot.config import VP_TARGET
    target = vp_target if vp_target is not None else VP_TARGET
    return dev_cards >= 2 and (vp + dev_cards) >= (target - 1)


def _one_short_vp_build(
    inferred: dict[str, int], unknown: int = 0,
    already_affordable: list[str] | None = None,
) -> dict | None:
    """The highest-VP build this opp is exactly 1 card short of.

    Scoped to city + settlement — the only builds worth tracking as
    threats, since road and dev rarely matter for a same-turn flip.
    When an opp is 1 ORE from a city, Noah can (a) withhold ORE in
    trades, (b) consider moving the robber onto an ORE tile, or (c)
    plan for an opp VP jump next turn. Actionable in a way that
    can_afford (already-flipped) is not.

    Skipped when the opp already has the build affordable — that's
    already surfaced by ``_affordable_builds``, showing "1 short"
    for the same opp would just be double-counting. Also skipped
    when ``unknown`` is high enough that the opp could already have
    the missing card (>=1 unknown): reporting "1 short" then would
    under-call the real risk.
    """
    if not isinstance(inferred, dict):
        return None
    already = set(already_affordable or [])
    best: dict | None = None
    # City outranks settlement for VP impact, so prefer it on ties.
    for name, cost in (("city", {"WHEAT": 2, "ORE": 3}),
                       ("settlement", {"WOOD": 1, "BRICK": 1,
                                       "SHEEP": 1, "WHEAT": 1})):
        if name in already:
            continue
        deficit = 0
        missing: str | None = None
        for r, n in cost.items():
            have = inferred.get(r, 0)
            if have < n:
                deficit += n - have
                missing = r
        if deficit == 1 and missing is not None:
            best = {"build": name, "need": missing,
                    "uncertain": unknown >= 1}
            break
    return best


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
    top_opp = max(
        opps,
        key=lambda e: (e[1], 1 if e[2] else 0),
        default=None,
    )
    opp_max = top_opp[1] if top_opp else 0
    opp_holder_entry = next((e for e in opps if e[2]), None)
    opp_holder = opp_holder_entry is not None
    color_map = getattr(game, "color_map", None)

    def _name_for(entry) -> str:
        if entry is None or color_map is None:
            return "opp"
        col = entry[0]
        col_str = col.value if hasattr(col, "value") else str(col)
        uname = color_map.reverse(col_str)
        return uname or "opp"

    top_opp_name = _name_for(top_opp)
    holder_name = _name_for(opp_holder_entry) if opp_holder else None

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
        if self_has:
            holder = "you"
        elif holder_name:
            holder = holder_name
        else:
            holder = "nobody"
        return {
            "level": "contested",
            "self_n": self_n,
            "opp_n": opp_max,
            "opp_username": top_opp_name,
            "holder_username": holder_name,
            "message": (
                f"largest army · you {self_n} / "
                f"{top_opp_name} {opp_max} · {holder} holds"),
        }
    if self_n >= 2 and not self_has and opp_max < self_n:
        return {
            "level": "self_push",
            "self_n": self_n,
            "opp_n": opp_max,
            "opp_username": top_opp_name,
            "message": f"1 knight → largest army ({self_n})",
        }
    if opp_max >= 2 and opp_max >= self_n and not self_has:
        gap = opp_max - self_n
        if opp_holder:
            msg = (
                f"{holder_name or top_opp_name} has largest army"
                f" · {opp_max} (+{gap})"
            )
        else:
            msg = f"{top_opp_name} 1 → largest army ({opp_max})"
        return {
            "level": "opp_threat",
            "self_n": self_n,
            "opp_n": opp_max,
            "opp_username": top_opp_name,
            "holder_username": holder_name,
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
    # Probability-weighted card loss per dice roll. pips_blocked already
    # doubled cities, so dividing by 36 gives the expected cards denied
    # per roll — a figure Noah can reason about in "cards" rather than
    # translating dot-counts in his head.
    expected_per_roll = pips / 36.0
    return {
        "resource": robber_tile.resource,
        "number": robber_tile.number,
        "buildings": building_count,
        "has_city": has_city,
        "pips_blocked": pips,
        "expected_per_roll": round(expected_per_roll, 3),
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
        "roll_histogram": dict(st.get("roll_histogram")
                               or {i: 0 for i in range(2, 13)}),
        "robber_pending": bool(st.get("robber_pending")),
        "robber_targets": st.get("robber_snapshot") or [],
        # "forced" = self rolled a 7 and must place the robber now;
        # "placed" = the robber just got placed (from a 7-roll or a
        #     knight play); snapshot lingers through the turn so Noah
        #     can reflect;
        # "knight" = self holds a KNIGHT, targets shown as play-timing
        #     aid — takes precedence over "placed" in the snap builder.
        # None when targets are empty.
        "robber_reason": (
            "forced" if st.get("robber_pending")
            else ("placed" if st.get("robber_snapshot") else None)),
        "my_turn": False,
        "recommendations": [],
        "incoming_trade": None,
        "knight_hint": None,
        "monopoly_hint": None,
        "yop_hint": None,
        "rb_hint": None,
        "discard_hint": None,
        "threat": None,
        "win_proximity": None,
        "robber_on_me": None,
        "longest_road_race": None,
        "largest_army_race": None,
        "bank_supply": None,
        "dev_deck": None,
        "yield_summary": None,
        "game_plan": None,
        "strategic_options": None,
        "winning_move": None,
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
        # Closest-build gap. None when every build is affordable or
        # the hand is empty; otherwise a {build, missing, gap} dict
        # pointing at the nearest-miss build so the HUD can render
        # "1 brick from settle" instead of "nothing buildable".
        "next_build": _closest_missing_build(hand),
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
        # Affordable builds computed once and reused — _one_short_vp_build
        # needs the same list to avoid double-surfacing (don't flag "1
        # short of city" when the opp can already city).
        can_afford = _affordable_builds(inferred, unknown)
        # Hand-growth signal: compare current card count against the
        # oldest sample in the ring buffer. A +3 swing over 3-4 rolls
        # means this opp is snowballing — even if not *currently*
        # affordable, the next production will probably flip a build.
        # Delta is None when we don't have history (pre-roll or
        # brand-new session); the HUD suppresses the tag in that case.
        card_delta: int | None = None
        card_hist_len: int | None = None
        try:
            card_hist = st.get("opp_card_hist") or {}
            series = card_hist.get(int(cid)) or []
            if len(series) >= 2:
                card_delta = int(count) - int(series[0])
                card_hist_len = len(series)
        except Exception:  # noqa: BLE001
            card_delta = None
            card_hist_len = None
        opp_vp = _get_vp(game, c)
        opp_dev_cards = int(sess.dev_card_counts.get(cid, 0))
        # Hidden-VP risk: see _is_dev_stash_risk docstring. Leader_threat
        # only picks the top-VP opp, so a secondary opp with a dev
        # stash would otherwise be invisible on the HUD.
        dev_stash_risk = _is_dev_stash_risk(opp_vp, opp_dev_cards)
        snap["opps"].append({
            "username": user,
            "color": c,
            "color_css": st["display_colors"].get(user),
            "cards": real_total,
            "hand": inferred,
            "unknown": unknown,
            # True when we know every card: breakdown sums to the total.
            "hand_tracked": (unknown == 0 and real_total > 0),
            # Card delta vs oldest sample in a 5-roll window. Positive
            # means accumulating; negative means spent/stolen/discarded.
            "card_delta": card_delta,
            "card_delta_window": card_hist_len,
            "vp": opp_vp,
            # Unplayed dev cards in hand. Includes hidden VPs, so a
            # spike here is a real "they might be close to 10" signal.
            # Counting comes from colonist's authoritative card-list
            # length; we can't see the types, only the size.
            "dev_cards": opp_dev_cards,
            # Hidden-VP risk flag. See comment above — True when this
            # opp's dev stash could realistically be hiding VPs that
            # put them within 1 of the game-ending VP total.
            "dev_stash_risk": dev_stash_risk,
            "pieces": _pieces_for_color(game, c),
            "knights_played": _knights_played(game, c),
            # Builds the inferred hand definitely covers. Conservative:
            # unknowns don't count, so this underestimates. Useful to
            # pre-warn about an opp's likely next-turn VP jump.
            "can_afford": can_afford,
            # The single highest-VP build this opp is exactly 1 card
            # short of. Complements can_afford (which shows what's
            # already flipped) by showing what's next in the pipeline.
            "one_short": _one_short_vp_build(
                inferred, unknown, already_affordable=can_afford),
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
    # Compute bank_supply early so the YoP hint can check it — YoP can't
    # grant a resource the bank doesn't have.
    try:
        snap["bank_supply"] = _compute_bank_supply(game)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] bank_supply failed: {e!r}", flush=True)
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
            game, self_color, hand,
            display_colors=st.get("display_colors") or {})
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] monopoly_hint failed: {e!r}", flush=True)
    try:
        snap["yop_hint"] = _compute_yop_hint(
            game, self_color, hand,
            bank_supply=snap.get("bank_supply"))
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] yop_hint failed: {e!r}", flush=True)
    try:
        snap["rb_hint"] = _compute_rb_hint(game, self_color)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] rb_hint failed: {e!r}", flush=True)
    # Multi-step plan banner — "2 roads → settle at whe6+ore11 · need
    # 1b 1s · 4:1 wood→brick if stuck". Frames the rec list with a
    # clear goal instead of just a flat ranking.
    try:
        snap["game_plan"] = _compute_game_plan(game, self_color, hand)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] game_plan failed: {e!r}", flush=True)
    # Long-horizon / riskier plays the flat rec list doesn't surface:
    # longest-road push, largest-army push, dev-card dive. VP-swing
    # driven so Noah can weigh piece commitment against potential gain.
    try:
        snap["strategic_options"] = _compute_strategic_options(
            game, self_color, hand)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] strategic_options failed: {e!r}", flush=True)
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
    # Self close-to-win banner — symmetric with leader_threat but for
    # self. Dev-card count comes from the session directly (snap doesn't
    # carry the unplayed tally — ``vp_breakdown.vp_cards`` is the played
    # slice only). Silent when self isn't close enough to matter.
    try:
        self_dev_held = 0
        if sess.self_color_id is not None:
            self_dev_held = int(
                sess.dev_card_counts.get(sess.self_color_id, 0) or 0)
        snap["win_proximity"] = _compute_win_proximity(
            snap, dev_cards_held=self_dev_held)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] win_proximity failed: {e!r}", flush=True)
        snap["win_proximity"] = None
    # Winning-move banner — fires when a single action (settle / city /
    # road→LR / knight→LA) closes the game THIS turn. Deliberately above
    # the rec list in the HUD so Noah never misses "press the button"
    # moments. Silent most turns.
    try:
        if self_color is not None:
            snap["winning_move"] = _compute_winning_move(
                game, self_color, hand, snap)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] winning_move failed: {e!r}", flush=True)
        snap["winning_move"] = None
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
            since = max(0, total - int(placed_at))
            snap["robber_on_me"]["rolls_since_placed"] = since
            # Cumulative expected loss since placement. Uses the
            # probability-weighted per-roll rate × rolls elapsed — an
            # estimate, since actual rolls may have missed the number,
            # but it's the "what did this cost me" headline number.
            # blocks_recent is the observed count over a ~10-roll
            # window; this is the lifetime since-placed estimate.
            per_roll = float(
                snap["robber_on_me"].get("expected_per_roll") or 0.0)
            snap["robber_on_me"]["expected_lost_total"] = round(
                per_roll * since, 2)
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
    # Bank-supply warning already computed above (YoP needs it). Just
    # left as a no-op marker here for clarity.
    try:
        snap["dev_deck"] = _compute_dev_deck_remaining(game)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] dev_deck failed: {e!r}", flush=True)
    # VP standings: who's leading and Noah's gap. Anchors the game-
    # progress header with an explicit leader read so Noah doesn't
    # have to eyeball each row to answer "am I ahead?". Computed
    # last so snap["self"] and snap["opps"] VP fields are populated.
    try:
        entries: list[dict[str, Any]] = []
        if snap.get("self"):
            entries.append({
                "username": snap["self"].get("username", "you"),
                "vp": int(snap["self"].get("vp", 0) or 0),
                "is_self": True,
            })
        for opp in (snap.get("opps") or []):
            entries.append({
                "username": opp.get("username"),
                "color": opp.get("color"),
                "color_css": opp.get("color_css"),
                "vp": int(opp.get("vp", 0) or 0),
                "is_self": False,
            })
        entries.sort(key=lambda e: -e["vp"])
        if entries:
            leader = entries[0]
            self_entry = next(
                (e for e in entries if e["is_self"]), None)
            self_vp = self_entry["vp"] if self_entry else 0
            snap["standings"] = {
                "leader": leader,
                "self_vp": self_vp,
                "gap_to_leader": (
                    leader["vp"] - self_vp
                    if self_entry and not leader["is_self"] else 0
                ),
                "self_is_leader": bool(
                    self_entry and leader["is_self"]),
            }
        else:
            snap["standings"] = None
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] standings failed: {e!r}", flush=True)
        snap["standings"] = None
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


def _compute_win_proximity(
    snap: dict[str, Any], dev_cards_held: int = 0,
) -> dict[str, Any] | None:
    """Self-side mirror of ``_compute_leader_threat``.

    Fires when self hits ``close_to_win_vp()`` so Noah snaps into close-
    out mode: the marginal value of a VP build leaps, bank/port trades
    that unlock one become worth lopsided ratios, and any unplayed dev
    card might already be a hidden VP that closes the game the instant
    total hits target. Returns None when self is still building up —
    banner stays out of the way until it's decision-shifting.

    Levels:
      * ``win``    — self VP >= target (game effectively over).
      * ``close-1`` — 1 VP from winning. Every decision should close.
      * ``close``  — 2 VP from winning. Start pruning non-VP spending.

    ``dev_cards_held`` is accepted as a parameter instead of fished out
    of snap because the dev count lives on the session, not the snap
    payload — callers pass it through from the session.
    """
    from cataanbot.config import close_to_win_vp, VP_TARGET
    self_snap = snap.get("self") or {}
    vp = int(self_snap.get("vp", 0) or 0)
    close_vp = close_to_win_vp()
    if vp < close_vp:
        return None
    gap_to_win = max(0, VP_TARGET - vp)
    afford = self_snap.get("afford") or []
    # Only city + settlement flip VP same-turn. Road/dev-card don't.
    vp_builds = [b for b in afford if b in ("city", "settlement")]
    if vp >= VP_TARGET:
        level = "win"
    elif gap_to_win == 1:
        level = "close-1"
    else:
        level = "close"
    if level == "win":
        msg = f"you reached {vp} VP — game over"
    elif level == "close-1":
        if vp_builds:
            msg = f"1 VP to win — {'/'.join(vp_builds)} ready"
        elif dev_cards_held > 0:
            msg = f"1 VP to win — {dev_cards_held} dev in hand"
        else:
            msg = "1 VP to win"
    else:
        if vp_builds:
            msg = (f"{gap_to_win} VP to win — "
                   f"{'/'.join(vp_builds)} ready")
        else:
            msg = f"{gap_to_win} VP to win"
    return {
        "vp": vp,
        "gap_to_win": gap_to_win,
        "vp_builds_affordable": vp_builds,
        "dev_cards_held": int(dev_cards_held),
        "level": level,
        "message": msg,
    }


def _compute_winning_move(
    game, self_color, hand: dict[str, int], snap: dict[str, Any],
) -> dict[str, Any] | None:
    """Detect when a single immediate action reaches VP_TARGET.

    Fires only when self is exactly 1 or 2 VP short and a concrete
    same-turn action closes the gap:

    * **+1 VP (settle / city)** — affordable and a legal spot exists.
    * **+2 VP (road → LR)** — self is 1 segment shy of qualifying (5
      segs minimum, strictly more than any opp), holds road cost, and
      has at least one buildable edge.
    * **+2 VP (knight → LA)** — self is 1 played knight shy of
      qualifying (3 min, strictly more than any opp), holds a KNIGHT
      in hand, not yet played this turn.

    Returns the highest-confidence option (single-build wins preferred
    over conditional LR/LA flips) or ``None`` when no winning move is
    reachable. Game_plan/win_proximity stay responsible for multi-step
    narrative; this is the **"press the button now"** banner.
    """
    from cataanbot.config import VP_TARGET
    from cataanbot.recommender import (
        _SETTLEMENT_COST, _CITY_COST, _ROAD_COST,
        _hand_can_afford,
    )
    from catanatron import Color

    self_snap = snap.get("self") or {}
    vp = int(self_snap.get("vp", 0) or 0)
    gap = VP_TARGET - vp
    if gap <= 0 or gap > 2:
        return None

    try:
        my_enum = (self_color if isinstance(self_color, Color)
                   else Color[str(self_color).upper()])
    except Exception:  # noqa: BLE001
        return None
    try:
        state = game.tracker.game.state
        board = state.board
    except Exception:  # noqa: BLE001
        return None
    my_idx = state.color_to_index.get(my_enum)
    if my_idx is None:
        return None
    ps = state.player_state
    # Setup phase is self-filtering via the gap check above: VP=0-2 in
    # setup means gap=8-10, always >2 and already rejected. No extra
    # SETTLEMENTS_AVAILABLE guard — LiveGame doesn't track that key.

    candidates: list[dict[str, Any]] = []

    # +1 VP path: affordable settlement on a legal spot.
    if gap == 1 and _hand_can_afford(hand, _SETTLEMENT_COST):
        try:
            spots = list(board.buildable_node_ids(my_enum))
        except Exception:  # noqa: BLE001
            spots = []
        if spots:
            candidates.append({
                "kind": "settle",
                "confidence": "high",
                "vp_after": vp + 1,
                "detail": "settle now — +1 VP wins the game",
            })

    # +1 VP path: city upgrade on an existing self settlement.
    if gap == 1 and _hand_can_afford(hand, _CITY_COST):
        own_settles = [
            int(nid) for nid, (col, bt) in board.buildings.items()
            if col == my_enum and str(bt).upper() == "SETTLEMENT"
        ]
        if own_settles:
            candidates.append({
                "kind": "city",
                "confidence": "high",
                "vp_after": vp + 1,
                "detail": "upgrade to city — +1 VP wins the game",
            })

    # +2 VP path: road that flips longest road.
    if gap == 2 and _hand_can_afford(hand, _ROAD_COST):
        self_len = int(ps.get(f"P{my_idx}_LONGEST_ROAD_LENGTH", 0))
        self_has_lr = bool(ps.get(f"P{my_idx}_HAS_ROAD", False))
        opp_max = 0
        for col, idx in state.color_to_index.items():
            if col == my_enum:
                continue
            ol = int(ps.get(f"P{idx}_LONGEST_ROAD_LENGTH", 0))
            if ol > opp_max:
                opp_max = ol
        # +1 road must qualify us (>= 5) and strictly beat opp max.
        qualifies = self_len + 1 >= max(5, opp_max + 1)
        if qualifies and not self_has_lr:
            try:
                edges = list(board.buildable_edges(my_enum))
            except Exception:  # noqa: BLE001
                edges = []
            if edges:
                # Confidence "medium": +1 road usually extends the LR
                # chain when we're already leading, but a branch off the
                # tail won't grow it. Noah can eyeball placement — we'd
                # need a full LR recompute to be certain.
                candidates.append({
                    "kind": "road_to_lr",
                    "confidence": "medium",
                    "vp_after": vp + 2,
                    "detail": (f"road extending your {self_len}-chain → "
                               "LR (+2 VP) wins the game"),
                })

    # +2 VP path: knight play that flips largest army.
    if gap == 2:
        knights_played = int(ps.get(f"P{my_idx}_PLAYED_KNIGHT", 0))
        knights_in_hand = int(ps.get(f"P{my_idx}_KNIGHT_IN_HAND", 0))
        played_this_turn = bool(ps.get(
            f"P{my_idx}_HAS_PLAYED_DEVELOPMENT_CARD_IN_TURN", False))
        self_has_la = bool(ps.get(f"P{my_idx}_HAS_ARMY", False))
        opp_knights_max = 0
        for col, idx in state.color_to_index.items():
            if col == my_enum:
                continue
            ok = int(ps.get(f"P{idx}_PLAYED_KNIGHT", 0))
            if ok > opp_knights_max:
                opp_knights_max = ok
        la_threshold = max(3, opp_knights_max + 1)
        qualifies = knights_played + 1 >= la_threshold
        if (qualifies and not self_has_la
                and knights_in_hand >= 1 and not played_this_turn):
            candidates.append({
                "kind": "knight_to_la",
                "confidence": "high",
                "vp_after": vp + 2,
                "detail": (f"play a Knight ({knights_played+1}/"
                           f"{la_threshold}) → LA (+2 VP) wins the game"),
            })

    if not candidates:
        return None

    # High confidence first (direct +1 builds, knight play), then road.
    conf_rank = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: conf_rank.get(c["confidence"], 9))
    top = candidates[0]
    return {
        "kind": top["kind"],
        "vp": vp,
        "vp_after": top["vp_after"],
        "confidence": top["confidence"],
        "detail": top["detail"],
        "alternatives": [
            {"kind": c["kind"], "detail": c["detail"]}
            for c in candidates[1:]
        ],
        "message": "WIN THIS TURN — " + top["detail"],
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
