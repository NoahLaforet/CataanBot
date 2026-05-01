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

from cataanbot.bridge_economy import (
    _compute_production,
    _owned_ports,
    _knights_played,
    _affordable_builds,
    _closest_missing_build,
    _is_dev_stash_risk,
    _one_short_vp_build,
    _pieces_for_color,
    _compute_bank_supply,
    _compute_dev_deck_remaining,
    _compute_largest_army_race,
    _compute_roll_yield,
    _vp_breakdown,
    _get_vp,
)
from cataanbot.bridge_robber import (
    _compute_robber_snapshot,
    _compute_robber_on_me,
)
from cataanbot.bridge_hints import (
    _compute_discard_plan,
    _compute_discard_hint,
    _compute_seven_prep_hint,
    _compute_monopoly_hint,
    _compute_yop_hint,
    _suggest_rb_placement,
    _compute_rb_hint,
    _compute_game_plan,
    _compute_strategic_options,
    _compute_knight_hint,
)
from cataanbot.bridge_strategy import (
    _compute_longest_road_race,
    _compute_leader_threat,
    _compute_win_proximity,
    _compute_winning_move,
)
from cataanbot.bridge_postmortem import (
    _feed_postmortem,
    _write_postmortem,
)


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
        # Self-relative eval (`evaluate_state(game, self_color)`) sampled
        # once per roll — gives the userscript a chess-style sparkline of
        # how the position has swung over time. List of
        # {"roll": total_rolls_after, "eval": float}; capped at 40 so a
        # full ~25-round game fits without ballooning the snapshot. Only
        # populated after self_color_id latches.
        "eval_history": [],
        # Most-recent recommendations served to the userscript when
        # ``my_turn`` was True. Used to classify self-builds chess-style
        # (!! / ! / ?! / ? / ??) at the moment the build event lands —
        # the recs reflect what the bot was suggesting AT THE TIME OF
        # THE DECISION, even if the per-snapshot recs change after.
        "last_recs_for_self": [],
        # Persistent multi-turn build plan. Updated by
        # _track_active_plan on each advisor poll; surfaces on the
        # snapshot as snap["plan"]. None when self has no soon-recs
        # worth tracking or when an affordable build supersedes it.
        "active_plan": None,
        # Running history of self-build classifications (post-setup
        # only). Each entry: {ts, piece, node/edge, rank, classification,
        # top_kind, top_loc, search_delta_gap}. Capped at 30 so a full
        # game fits without unbounded growth.
        "move_history": [],
        # Self's dev-card holdings tracked by COUNT (colonist hides the
        # type from logs and we don't decode the WS dev_card type ints).
        # ``dev_cards_held`` is total bought minus total played; the
        # ``_bought_this_turn`` carve-out enforces Catan's just-bought
        # delay (a card bought this turn can't be played until next).
        # Both reset on game-reset; per-turn count clears on turn-flip
        # away from self.
        "dev_cards_held": 0,
        "dev_cards_bought_this_turn": 0,
        # Last seen current_turn_color_id — used to detect self→opp
        # transitions so we can clear ``dev_cards_bought_this_turn``
        # exactly once per turn flip rather than every poll.
        "_last_turn_cid": None,
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
            _apply_colonist_game_settings(game)
            _print_game_start(game)
            # Re-seed pm_tracker with the live game's catanatron map.
            # By default pm_tracker uses BASE_MAP_TEMPLATE (classic 19
            # tiles) but variant maps have different node IDs, so the
            # postmortem path would reject every BuildEvent on Pond/etc.
            # Sharing the catan_map ensures the postmortem report can
            # actually apply build events for variant games too.
            try:
                st["pm_tracker"] = Tracker(
                    catan_map=game.tracker.game.state.board.map)
            except Exception as e:  # noqa: BLE001
                print(f"[pm] failed to reseed tracker: {e!r}", flush=True)
            return {"ok": True, "booted": True,
                    "players": game.color_map.as_dict()}

        if results:
            _track_overlay_state(st, results)
            _print_dispatch_results(
                game, results, st["ws_count"], advisor=advisor)
        return {"ok": True, "results": len(results or [])}

    @app.post("/feedback")
    def feedback(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Log a thumbs-up / thumbs-down on a recommendation.

        Userscript posts ``{label, rec, snapshot_hint}`` per click:
        ``label`` is "good" or "bad", ``rec`` is the rec dict that
        was rendered, ``snapshot_hint`` is a small subset of the
        advisor snapshot (turn/round, self VP, hand size) so the
        labeled rec has enough context to be reasoned about later
        without rebuilding the full game state.

        Appends one JSONL line per click to
        ``feedback/recs.jsonl`` next to the postmortem dir. We don't
        do any ML here yet; this is the data-collection layer for
        when sample size is large enough to train against.
        """
        label = str(payload.get("label", "")).strip().lower()
        if label not in ("good", "bad"):
            return {"ok": False, "error": "label must be 'good' or 'bad'"}
        rec = payload.get("rec") or {}
        snapshot_hint = payload.get("snapshot_hint") or {}
        out_dir = st.get("pm_dir") or (Path.cwd() / "postmortems")
        out_dir = Path(out_dir).parent / "feedback"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"could not create dir: {e}"}
        line = {
            "ts": time.time(),
            "label": label,
            "rec": rec,
            "snapshot_hint": snapshot_hint,
        }
        # Append; one click = one line. Easy to grep and stream-parse.
        out_path = out_dir / "recs.jsonl"
        try:
            with out_path.open("a") as f:
                f.write(json.dumps(line) + "\n")
        except OSError as e:
            return {"ok": False, "error": f"write failed: {e}"}
        return {"ok": True, "path": str(out_path)}

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        """Current VP target + discard limit. Userscript drawer reads
        this on init to reflect server-side state in its inputs."""
        from cataanbot import config
        return {
            "vp_target": config.get_vp_target(),
            "discard_limit": config.get_discard_limit(),
        }

    @app.post("/config")
    def post_config(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Update VP target and/or discard limit at runtime. Either
        field is optional; missing fields are left untouched. Returns
        the new full state so the userscript can confirm what stuck."""
        from cataanbot import config
        errors: list[str] = []
        if "vp_target" in payload and payload["vp_target"] is not None:
            try:
                config.set_vp_target(int(payload["vp_target"]))
            except (TypeError, ValueError) as e:
                errors.append(f"vp_target: {e}")
        if "discard_limit" in payload and payload["discard_limit"] is not None:
            try:
                config.set_discard_limit(int(payload["discard_limit"]))
            except (TypeError, ValueError) as e:
                errors.append(f"discard_limit: {e}")
        result = {
            "ok": not errors,
            "vp_target": config.get_vp_target(),
            "discard_limit": config.get_discard_limit(),
        }
        if errors:
            result["errors"] = errors
        else:
            print(
                f"[bridge] config: vp_target={result['vp_target']} "
                f"discard_limit={result['discard_limit']}",
                flush=True,
            )
        return result

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
        st["eval_history"] = []
        st["last_recs_for_self"] = []
        st["move_history"] = []
        st["dev_cards_held"] = 0
        st["dev_cards_bought_this_turn"] = 0
        st["_last_turn_cid"] = None
        st.pop("_booted", None)
        # Truncate the autosave file too — fresh game means fresh
        # state on next bridge restart.
        if ws_jsonl_path is not None:
            try:
                ws_jsonl_path.write_text("")
            except OSError:
                pass
        print("[bridge] game state reset", flush=True)
        return {"ok": True}

    # Auto-resume: replay frames written by a previous bridge session
    # so a mid-game restart or page refresh doesn't lose game state.
    # Frames apply through the same code path as a live POST /ws
    # (they were recorded from one), so the LiveGame ends up in the
    # same state it had at the moment the bridge was killed. Errors
    # are tolerated — a malformed line just stops replay; whatever
    # state was rebuilt up to that point stays.
    if ws_jsonl_path is not None and ws_jsonl_path.exists():
        replayed = 0
        try:
            with ws_jsonl_path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        break
                    try:
                        results = _feed_ws_payload(st["game"], frame)
                    except Exception:  # noqa: BLE001
                        continue
                    st["ws_count"] += 1
                    st["seq"] += 1
                    if results:
                        try:
                            _track_overlay_state(st, results)
                        except Exception:  # noqa: BLE001
                            pass
                    if (st["game"].started
                            and st.get("_booted") is None):
                        st["_booted"] = True
                        try:
                            st["pm_tracker"] = Tracker(
                                catan_map=st["game"].tracker
                                .game.state.board.map)
                        except Exception:  # noqa: BLE001
                            pass
                    replayed += 1
        except OSError:
            pass
        if replayed > 0:
            print(f"[bridge] replayed {replayed} frames from autosave",
                  flush=True)
            # Rotate the file so future restarts don't include both
            # the pre-restart history (already replayed into state)
            # and post-restart frames. Without rotation, the bridge
            # would replay the full file again on the next start —
            # state would still rebuild correctly because replay is
            # idempotent on the LiveGame (each frame computes the same
            # diff), but the dedup-guarded counters would still be
            # safe. Rotation is mostly disk-space hygiene.
            try:
                archive = ws_jsonl_path.with_suffix(".replayed.jsonl")
                ws_jsonl_path.replace(archive)
            except OSError:
                pass

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


def _apply_colonist_game_settings(game) -> None:
    """Sync VP target + discard limit from colonist's GameStart payload.

    Colonist's gameSettings carries ``victoryPointsToWin`` (10 default,
    but Noah played a 15 VP game on 2026-04-30 and every endgame
    heuristic — close_to_win, leader_threat, win_proximity, recommender
    endgame bias — was tuned for a 10-VP game while colonist enforced
    15. Same for ``cardDiscardLimit`` (7 default, but Seafarers and some
    custom games change it). Auto-detect on game boot so the bot's
    config tracks the actual rules of THIS game without Noah needing
    to hit /config manually.

    Silent no-op when the payload is missing the keys (older colonist
    versions) or when our session.game_settings hasn't been populated
    yet (frame ordering edge case)."""
    sess = getattr(game, "session", None)
    if sess is None:
        return
    gs = getattr(sess, "game_settings", None) or {}
    from cataanbot import config
    vp = gs.get("victoryPointsToWin")
    if vp is not None:
        try:
            new_vp = int(vp)
            if new_vp != config.get_vp_target():
                config.set_vp_target(new_vp)
                print(f"[bridge] auto-detected VP target: {new_vp} "
                      f"(from colonist gameSettings)", flush=True)
        except (TypeError, ValueError):
            pass
    dl = gs.get("cardDiscardLimit")
    if dl is not None:
        try:
            new_dl = int(dl)
            if new_dl != config.get_discard_limit():
                config.set_discard_limit(new_dl)
                print(f"[bridge] auto-detected discard limit: "
                      f"{new_dl} (from colonist gameSettings)",
                      flush=True)
        except (TypeError, ValueError):
            pass


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
    from cataanbot.events import (
        BuildEvent, RobberMoveEvent, RollEvent,
        TradeCommitEvent, TradeOfferEvent,
    )

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
            # Eval sparkline sample. Once per roll keeps the series tight
            # to discrete game ticks (one entry per dice roll = one entry
            # per "turn" in chess-eval-bar terms). evaluate_state is
            # ``own − 0.8 * max_opp`` so the value is signed: positive
            # means we're ahead. Skipped silently when self_color_id
            # hasn't latched yet — pre-resource frames have nothing
            # meaningful to evaluate against.
            try:
                sess2 = game.session
                if sess2 and sess2.self_color_id is not None:
                    uname2 = sess2.player_names.get(sess2.self_color_id)
                    if uname2 and game.color_map.has(uname2):
                        sc2 = game.color_map.get(uname2)
                        from cataanbot.eval import evaluate_state
                        ev_val = float(evaluate_state(
                            game.tracker.game, sc2))
                        eh = list(st.get("eval_history") or [])
                        eh.append({
                            "roll": int(st.get("total_rolls") or 0),
                            "eval": round(ev_val, 2),
                        })
                        st["eval_history"] = eh[-40:]
            except Exception as e:  # noqa: BLE001
                print(f"[overlay] eval sample failed: {e!r}",
                      flush=True)
            if r.event.total == 7 and is_you:
                st["robber_pending"] = True
                # Fresh attempt budget for this placement window.
                st["robber_snapshot_retry_n"] = 0
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
        elif isinstance(r.event, BuildEvent):
            # Move-quality classification (HUD principle #7). Only
            # self-builds; only post-setup; only when we have a cached
            # rec list to grade against. Setup detection: catanatron's
            # ``build_counts`` is incremented BEFORE this hook runs (in
            # game.feed → _debit_build), so post-state count<=2 means
            # the build that just happened was a setup placement.
            try:
                _record_self_build_quality(st, r.event)
            except Exception as e:  # noqa: BLE001
                print(f"[overlay] move-quality classify failed: {e!r}",
                      flush=True)
        elif isinstance(r.event, TradeOfferEvent):
            # Mirror of the DOM-log handler in _feed_postmortem. WS-side
            # trade offers (tradeState.activeOffers) come through here
            # since colonist's UI button doesn't always emit a chat-log
            # line. Without this hook the new WS parser would emit the
            # event but the HUD's incoming-trade banner would never
            # populate. Same payload shape as the DOM-log path so the
            # snap builder + frontend renderer don't need to know which
            # pipeline fired the offer.
            st["pending_trade_offer"] = {
                "player": r.event.player,
                "give": dict(r.event.give),
                "want": dict(r.event.want),
                "ts": None,
            }
        elif isinstance(r.event, TradeCommitEvent):
            # Same clear-on-commit rule the DOM-log handler uses: the
            # offer's decision window closes the moment a trade is
            # actually executed (or rolled past).
            st["pending_trade_offer"] = None



def _track_active_plan(
    st, full_recs: list, hand: dict[str, int],
) -> dict[str, Any] | None:
    """Maintain a persistent multi-turn plan across advisor polls.

    The recommender is stateless — every poll rebuilds recs from
    scratch. That makes the HUD feel reactive ("here's what to do
    now") instead of strategic ("here's what we're working toward").
    This tracker watches the top "soon" rec and surfaces it as a
    sticky banner. The plan only changes when:

    * The current target becomes affordable → emit a one-tick
      "READY!" pulse, then unlock so the next plan can lock in.
    * A new "soon" rec scores materially better than the active
      plan (delta >= 1.0). Avoids flip-flopping when scores wobble
      a tenth between polls.
    * The active target is no longer in the rec list at all (e.g.
      the build became impossible — settlement spot got taken,
      dev deck emptied).

    Returns a small dict for the snapshot::

        {
          "kind": "settlement" | "city" | "dev_card",
          "target_node_id": int | None,
          "missing": {res: count},
          "progress": {res: have / need},
          "score": float,
          "turns_held": int,
          "ready": bool,
          "summary": "save for city — need 1 ore"
        }

    Returns None when there's nothing to plan (no soon recs OR self
    is already affordable on something better).
    """
    if not full_recs:
        st["active_plan"] = None
        return None

    # Pull "soon" plans (1-2 cards from a build). They're the natural
    # multi-turn targets. Filter out road plans — those are
    # tactical, not strategic.
    soon_plans = [r for r in full_recs
                  if r.get("when") == "soon"
                  and r.get("kind") in ("settlement", "city", "dev_card")]

    # If self has an affordable build that's strictly better than any
    # plan target, no plan needed — they should just build it.
    affordable_now = [r for r in full_recs
                      if r.get("when") == "now"
                      and r.get("kind") in ("settlement", "city")]
    top_now_score = (max((float(r.get("score", 0.0))
                          for r in affordable_now), default=0.0))
    top_soon_score = (max((float(r.get("score", 0.0))
                           for r in soon_plans), default=0.0))

    active = st.get("active_plan")

    # Affordable-now path: if there's a same-VP-tier build affordable
    # right now AND scoring as well as the current plan, drop the plan
    # so the user just builds.
    if affordable_now and top_now_score >= top_soon_score:
        if active is not None:
            st["active_plan"] = None
        return None

    if not soon_plans:
        st["active_plan"] = None
        return None

    # Sort soon plans by score descending. Top is the candidate.
    soon_plans.sort(key=lambda r: -float(r.get("score", 0.0)))
    candidate = soon_plans[0]

    # Decide whether to keep the active plan or swap to the candidate.
    keep = False
    if active is not None:
        same_kind = active.get("kind") == candidate.get("kind")
        same_node = active.get("target_node_id") == candidate.get("node_id")
        if same_kind and (same_node or candidate.get("kind") == "dev_card"):
            keep = True
        else:
            # Swap only if the candidate is meaningfully better. Stops
            # the plan from flipping every time a coin-flip score
            # tweak shifts the order.
            cand_score = float(candidate.get("score", 0.0))
            active_score = float(active.get("score", 0.0))
            if cand_score - active_score < 1.0:
                # Re-find the active plan in the current rec list
                # (its score may have shifted slightly). If it's
                # gone, swap. If it's still there, keep.
                still_alive = next(
                    (r for r in full_recs
                     if r.get("kind") == active.get("kind")
                     and r.get("node_id") == active.get(
                         "target_node_id")),
                    None)
                if still_alive is not None:
                    keep = True

    if not keep:
        # Lock in the candidate as the new plan.
        active = {
            "kind": candidate.get("kind"),
            "target_node_id": candidate.get("node_id"),
            "missing": dict(candidate.get("missing") or {}),
            "score": float(candidate.get("score", 0.0)),
            "turns_held": 0,
            "locked_seq": st.get("seq", 0),
        }

    # Update progress tracking — how many cards we've gathered
    # toward the missing slots since the plan locked.
    missing = candidate.get("missing") or {}
    progress = {}
    for res, need in missing.items():
        # We need `need` of res; if we have more than 0, that's progress.
        progress[res] = {
            "have": int(hand.get(res, 0) or 0),
            "need": int(need),
        }
    active["missing"] = dict(missing)
    active["progress"] = progress
    active["turns_held"] = int(active.get("turns_held", 0)) + 1
    active["ready"] = sum(missing.values()) == 0

    # Summary line for the HUD. Verb-first English so it reads as
    # instruction, not data.
    label = active["kind"] if active["kind"] != "dev_card" else "dev card"
    if active["ready"]:
        active["summary"] = f"PLAN READY — build {label} now"
    else:
        miss_str = " + ".join(
            f"{n} {r.lower()}" for r, n in missing.items() if n > 0)
        active["summary"] = f"saving for {label} — need {miss_str}"

    st["active_plan"] = active
    # Return a slim copy for the snapshot (avoid round-tripping mutable
    # state into JSON output).
    return dict(active)


def _maybe_clear_dev_just_bought(st) -> None:
    """Clear ``dev_cards_bought_this_turn`` when self's turn ends.

    Called on every advisor-snap rebuild — cheap (one int compare) and
    runs in the snapshot hot path so the bought_this_turn carve-out
    naturally clears the moment colonist's diff says it's no longer
    self's turn. Tracking the cid this way avoids needing a synthetic
    EndTurnEvent that the parser would have to fabricate.
    """
    game = st.get("game")
    if not (game and getattr(game, "session", None)):
        return
    sess = game.session
    cur_cid = sess.current_turn_color_id
    last_cid = st.get("_last_turn_cid")
    self_cid = sess.self_color_id
    # Self's turn → next-player's turn: the just-bought delay is over.
    if (last_cid is not None and last_cid == self_cid
            and cur_cid is not None and cur_cid != self_cid):
        st["dev_cards_bought_this_turn"] = 0
    st["_last_turn_cid"] = cur_cid


def _record_self_build_quality(st, ev) -> None:
    """Classify a self BuildEvent against the cached recommendations
    and append a chess-style entry to ``st["move_history"]``.

    Skipped silently when the build is part of the opening (settle 1-2
    or road 1-2), when the build isn't self's, or when no recs were
    cached yet. The cached recs come from the last ``my_turn`` snapshot
    served to the userscript; they reflect what the bot was suggesting
    at decision time even if the per-snap recs change after the build.
    """
    from cataanbot.move_quality import (
        classify_build_against_recs, find_rank,
    )

    game = st["game"]
    sess = game.session
    if sess is None or sess.self_color_id is None:
        return
    self_name = sess.player_names.get(sess.self_color_id)
    if not self_name or ev.player != self_name:
        return
    try:
        color = game.color_map.get(ev.player)
    except Exception:  # noqa: BLE001
        return

    # Setup-build detection. ``_debit_build`` already incremented
    # build_counts for this event in game.feed, so the count we see is
    # the POST-state. Settlement count == 1 or 2 → was opening; road
    # count == 1 or 2 → was opening road. Skip those.
    tally = game.build_counts.get(
        color, {"settlement": 0, "city": 0, "road": 0})
    if ev.piece == "settlement" and tally.get("settlement", 0) <= 2:
        return
    if ev.piece == "road" and tally.get("road", 0) <= 2:
        return
    if ev.piece not in ("settlement", "city", "road"):
        return

    recs = list(st.get("last_recs_for_self") or [])
    if not recs:
        # No cached recs → can't classify. Surface as a "no_recs"
        # entry so the HUD can show the build happened but flag the
        # missing comparison rather than silently dropping it.
        rank = None
        classification = None
    else:
        classification, rank = classify_build_against_recs(ev, recs)

    # Search-delta gap: how much eval Noah left on the table by picking
    # his move over the bot's top. Only meaningful when both recs are
    # simulatable (search_delta is a float, not None).
    sd_gap = None
    top_rec = recs[0] if recs else None
    actual_rec = recs[rank - 1] if (rank and rank <= len(recs)) else None
    if (top_rec and actual_rec
            and isinstance(top_rec.get("search_delta"), (int, float))
            and isinstance(actual_rec.get("search_delta"), (int, float))):
        sd_gap = round(
            float(top_rec["search_delta"])
            - float(actual_rec["search_delta"]), 1)

    # Build a compact location string for the badge.
    if ev.piece == "road":
        loc = list(ev.edge_nodes) if ev.edge_nodes else None
    else:
        loc = ev.node_id

    # Top rec's location for "you played X, top was Y" diff display.
    top_loc = None
    top_kind = None
    if top_rec:
        top_kind = top_rec.get("kind")
        if top_kind == "road":
            top_loc = (list(top_rec["edge"])
                       if top_rec.get("edge") else None)
        else:
            top_loc = top_rec.get("node_id")

    entry = {
        "ts": int(st.get("total_rolls") or 0),
        "piece": ev.piece,
        "loc": loc,
        "rank": rank,
        "rec_count": len(recs),
        "classification": classification,
        "top_kind": top_kind,
        "top_loc": top_loc,
        "search_delta_gap": sd_gap,
    }
    mh = list(st.get("move_history") or [])
    mh.append(entry)
    st["move_history"] = mh[-30:]

    # Auto-feedback: if self played the top rec (rank 0 → "!!"), log
    # an auto thumbs-up so Noah doesn't have to click 👍 on every
    # rec he agrees with. He only needs to manually 👎 the bad ones.
    # Distinct label "auto_good" so analysis can separate user-
    # confirmed marks from inferred ones.
    if rank == 0 and recs:
        try:
            import json as _json
            import time as _time
            from pathlib import Path as _Path
            out_dir = st.get("pm_dir") or (_Path.cwd() / "postmortems")
            out_dir = _Path(out_dir).parent / "feedback"
            out_dir.mkdir(parents=True, exist_ok=True)
            line = {
                "ts": _time.time(),
                "label": "auto_good",
                "rec": dict(recs[0]),
                "snapshot_hint": {
                    "piece": ev.piece, "loc": loc,
                    "search_delta_gap": sd_gap,
                },
            }
            with (out_dir / "recs.jsonl").open("a") as f:
                f.write(_json.dumps(line, default=str) + "\n")
        except Exception as e:  # noqa: BLE001
            print(f"[advisor] auto-feedback failed: {e!r}", flush=True)












def _build_advisor_snapshot(st) -> dict[str, Any]:
    """JSON payload for the userscript overlay.

    Poll-friendly: callers diff on `seq` to detect change, but can
    unconditionally re-render if they prefer. All fields are safe to
    render even before a game has booted — `self` is None until then."""
    game = st["game"]
    from cataanbot import config

    # Late-retry the robber snapshot any time self owes a placement
    # but the snapshot is empty. Catches both the knight-play case
    # (DOM-log fires before session is ready) AND the 7-roll case
    # (Noah's 2026-05-01 game showed the rec missing on a self
    # 7-roll when the initial compute returned empty for a similar
    # timing reason). Bounded at ~30 attempts (≈4s at default
    # poll cadence) so a stuck retry doesn't burn CPU forever.
    needs_retry = (
        st.get("robber_snapshot_retry")
        or (st.get("robber_pending") and not st.get("robber_snapshot"))
    )
    if needs_retry and game is not None:
        attempts = int(st.get("robber_snapshot_retry_n") or 0)
        if attempts < 30:
            try:
                new_snap = _compute_robber_snapshot(
                    game, display_colors=st.get("display_colors") or {})
            except Exception:  # noqa: BLE001
                new_snap = None
            if new_snap:
                st["robber_snapshot"] = new_snap
                st["robber_snapshot_retry"] = False
                st["robber_snapshot_retry_n"] = 0
            else:
                st["robber_snapshot_retry_n"] = attempts + 1
        else:
            # Give up; clear the retry flag so we don't keep paying
            # the per-poll _compute_robber_snapshot cost.
            st["robber_snapshot_retry"] = False
            st["robber_snapshot_retry_n"] = 0
    snap: dict[str, Any] = {
        "seq": st["seq"],
        "game_started": game.started,
        "ws_frames": st["ws_count"],
        "log_events": st["log_count"],
        # Surface the runtime VP target + discard limit on every snap
        # so the userscript can scale danger/watch thresholds without
        # a separate /config round-trip and without going stale when
        # the values shift mid-game (auto-detect from GameStart).
        "vp_target": config.get_vp_target(),
        "discard_limit": config.get_discard_limit(),
        "self": None,
        "opps": [],
        "last_roll": st.get("last_roll"),
        "roll_history": list(st.get("roll_history") or []),
        "total_rolls": int(st.get("total_rolls") or 0),
        "roll_histogram": dict(st.get("roll_histogram")
                               or {i: 0 for i in range(2, 13)}),
        # Per-roll eval samples. Userscript renders as a sparkline; last
        # entry is "current eval." Empty list before self latches.
        "eval_history": list(st.get("eval_history") or []),
        # Per-self-build chess-style classifications. Userscript renders
        # as a running tally + last-move badge. Empty until first
        # post-setup self build lands; capped at 30 in `_track_overlay_state`.
        "move_history": list(st.get("move_history") or []),
        "robber_pending": bool(st.get("robber_pending")),
        "robber_targets": st.get("robber_snapshot") or [],
        # "forced" = self rolled a 7 and must place the robber now;
        # "placed" = the robber just got placed (from a 7-roll or a
        #     knight play); snapshot lingers through the turn so Noah
        #     can reflect.
        # The earlier "knight" pre-play preview was dropped — opponents
        # watching a stream shouldn't see which tile we'd target before
        # we commit to playing the card. Targets only render after
        # play (via the DOM-log knight handler in _feed_postmortem).
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
    # Surface whose turn it is when not self's, so the panel can label
    # the off-turn ribbon. Falls back to a color string if the cid
    # isn't in player_names yet (very early-game race).
    cur_cid_active = sess.current_turn_color_id
    if cur_cid_active is not None and cur_cid_active != my_cid:
        snap["current_turn_username"] = sess.player_names.get(
            cur_cid_active) or f"player {cur_cid_active}"
    else:
        snap["current_turn_username"] = None
    # Variant-board flag from colonist's gameSettings. "classic" for
    # base Catan, "variant: ..." with the non-zero flags otherwise.
    # Surfaced so the HUD can warn that strategy isn't yet tuned for
    # non-classic boards (Seafarers, Cities & Knights, custom maps).
    try:
        snap["variant"] = sess.variant_label()
        snap["game_settings"] = dict(sess.game_settings or {})
    except Exception:  # noqa: BLE001
        snap["variant"] = "classic"
        snap["game_settings"] = {}
    # Friendly Robber rule (colonist optional). When active, the
    # robber-target ranker has already filtered protected victims.
    # HUD shows a small pill so the user knows the rule is on AND
    # which suggestions reflect it.
    snap["friendly_robber_active"] = bool(
        sess.friendly_robber_active)
    # bank_supply has to be computed BEFORE recs so the propose-trade
    # bank-19 guard (line 1489 in recommender.py) actually fires —
    # without this, the bot suggests "offer brick for wood" even when
    # all 19 wood cards are sitting in the bank (Noah's 2026-05-01
    # game). Computed once here and reused by recs + the dev-card
    # hints below; if it fails the recs path falls back to no-guard
    # which is the prior behavior.
    try:
        snap["bank_supply"] = _compute_bank_supply(game)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] bank_supply failed: {e!r}", flush=True)
    # Mid-game recs: only when it's actually my turn. During setup the
    # opening picks were already populated above, so skip here.
    if not is_setup and snap["my_turn"]:
        try:
            from cataanbot.recommender import recommend_actions
            # Feed the bank_supply we already computed into the rec
            # planner so port/4:1 trades + propose-trade get skipped
            # when the bank is dry / full on the needed resource.
            bank_for_recs = (
                snap.get("bank_supply") or {}).get("remaining")
            # Same idea for dev cards: skip dev-card recs when colonist
            # has confirmed the deck is empty. Without this, the bot
            # keeps suggesting "buy a dev card" all the way to game-end
            # on a depleted deck — the user clicks and nothing happens.
            # snap["dev_deck"] is populated later in the snapshot
            # builder, so we compute it directly here.
            dev_deck_for_recs = None
            dev_deck_now = _compute_dev_deck_remaining(game)
            if dev_deck_now:
                dev_deck_for_recs = dev_deck_now.get("remaining")
            # Pull top-10 once; the visible HUD list is the first 4 of
            # that, and the move-quality classifier (HUD principle #7)
            # uses the whole list so a !/!?/?/?? rank can land outside
            # the visible top-4. Doing this in two recommend_actions
            # calls would double the search-rerank cost on every poll,
            # which is hot path.
            full_recs = recommend_actions(
                cat_game, self_color, hand, top=10,
                bank_supply=bank_for_recs,
                dev_deck_remaining=dev_deck_for_recs)
            snap["recommendations"] = full_recs[:4]
            if full_recs:
                st["last_recs_for_self"] = full_recs
            # Persistent multi-turn plan. The recommender is stateless
            # (every poll regenerates recs from scratch), which makes
            # the HUD feel reactive instead of strategic. The plan
            # tracker watches the top "soon" rec and surfaces it as a
            # sticky banner that only swaps when something materially
            # better appears — gives Noah a north star across rolls
            # instead of a fresh suggestion every time he reads the HUD.
            try:
                snap["plan"] = _track_active_plan(st, full_recs, hand)
            except Exception as e:  # noqa: BLE001
                print(f"[advisor] plan tracking failed: {e!r}",
                      flush=True)
                snap["plan"] = None
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
    # Clear the just-bought-this-turn carve-out when self's turn
    # ends — _maybe_clear_dev_just_bought is a one-int-compare guard
    # that runs every snap so we don't need a separate EndTurnEvent.
    _maybe_clear_dev_just_bought(st)
    # Self's playable NON-VP dev-card count. Catanatron's *_IN_HAND
    # counters stay at 0 for self because the buy handler can't see
    # the card type; we track total holdings as an aggregate count.
    # Colonist DOES report self's VP-dev-card count separately
    # (victory_points_state[self][source=2]) — VP cards count toward
    # the displayed VP total so they have to be visible. We subtract
    # those out so the play-timing hints only fire when self holds
    # at least one playable card type (knight / monopoly / YoP / RB).
    # Equals (total - vp_held) minus the cards bought this turn
    # (Catan's no-play-on-buy rule). When the just-bought was actually
    # a VP card, this under-counts non-VP playable for one turn —
    # acceptable false-negative since hints reappear on the next flip.
    sess = game.session
    # Prefer colonist's authoritative count of self's
    # developmentCards.cards — it's the source-of-truth for
    # holdings, and falls back to the DOM-log-driven counter when
    # we haven't seen a WS frame yet. Same pattern for the
    # just-bought-this-turn carve-out.
    have_ws_state = (
        sess is not None and sess.self_color_id is not None
        and sess.dev_card_counts.get(sess.self_color_id) is not None)
    if have_ws_state:
        dev_held = int(sess.dev_card_counts.get(sess.self_color_id) or 0)
        # Use colonist's bought-this-turn list directly — empty-list
        # is a real value (no buy this turn), not "fall back to
        # homemade." Only fall back when we don't have any WS state
        # for self yet.
        dev_just = len(sess.self_dev_bought_this_turn)
    else:
        dev_held = int(st.get("dev_cards_held") or 0)
        dev_just = int(st.get("dev_cards_bought_this_turn") or 0)
    vp_held = 0
    if sess is not None and sess.self_color_id is not None:
        vp_held = int((sess.victory_points_state
                       .get(sess.self_color_id, {})
                       .get(2, 0)) or 0)
    # Whether we trusted colonist's authoritative carve-out vs the
    # homemade fallback. Userscript can show "(authoritative)" when
    # sess-driven so the user knows the just-bought signal is solid.
    snap["dev_cards_just_bought_authoritative"] = have_ws_state
    non_vp_held = max(0, dev_held - vp_held)
    dev_playable = max(0, non_vp_held - dev_just)
    # Type-known check: when the WS diff parser successfully decoded
    # the card int(s) self bought, catanatron's per-type IN_HAND
    # counters are non-zero. The hints then gate strictly on their
    # own type counter (only matching hint fires); when type isn't
    # known, the hints fall back to playable_count (all four fire,
    # user picks the matching one). Sum across the four playable
    # types so a single decoded buy switches the mode.
    typed_held = 0
    try:
        from catanatron import Color as _Color
        _state = game.tracker.game.state
        _idx = _state.color_to_index.get(_Color[self_color.upper()])
        if _idx is not None:
            for _t in ("KNIGHT", "MONOPOLY",
                       "YEAR_OF_PLENTY", "ROAD_BUILDING"):
                typed_held += int(_state.player_state.get(
                    f"P{_idx}_{_t}_IN_HAND", 0))
    except Exception:  # noqa: BLE001
        typed_held = 0
    snap["dev_cards_held"] = dev_held
    snap["dev_cards_vp_held"] = vp_held
    snap["dev_cards_non_vp_held"] = non_vp_held
    snap["dev_cards_just_bought"] = dev_just
    snap["dev_cards_playable"] = dev_playable
    # Authoritative played-history counts per type, derived from
    # colonist's developmentCardsUsed list. Empty dict until self
    # plays their first card. Useful for downstream consumers to
    # cross-check catanatron's PLAYED_{type} state without trusting
    # the DOM-log lag.
    if sess is not None and sess.self_dev_used:
        from collections import Counter
        c = Counter(sess.self_dev_used)
        from cataanbot.colonist_diff import _DEV_CARD_TYPE
        played_by_type: dict[str, int] = {}
        for type_int, n in c.items():
            name = _DEV_CARD_TYPE.get(int(type_int))
            if name:
                played_by_type[name] = played_by_type.get(name, 0) + n
        snap["dev_cards_played_by_type"] = played_by_type
    else:
        snap["dev_cards_played_by_type"] = {}
    snap["dev_cards_type_known"] = typed_held > 0
    # When type is known, the playable_count fallback in each hint
    # would falsely fire for the non-matching types — pass 0 so the
    # hint must rely on its own IN_HAND counter.
    hint_fallback = 0 if typed_held > 0 else dev_playable
    # Knight / monopoly / YoP / RB hints: surface play-timing advice
    # for each card type whenever self has at least one playable dev
    # card, since we can't tell which type self holds from the log.
    # The hints carry their own context (robber rank, opp resource
    # tally, road-supply check) and the user picks whichever matches
    # what's actually in their dev card panel. bank_supply was
    # already computed above (before recs) so the dev-card hints
    # just read the existing snap entry.
    try:
        snap["knight_hint"] = _compute_knight_hint(
            game, display_colors=st.get("display_colors") or {},
            playable_count=hint_fallback)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] knight_hint failed: {e!r}", flush=True)
    # Robber-targets ranking is only surfaced when the player ACTUALLY
    # owes a placement (forced 7-roll, or just played a knight) — not
    # while merely holding the knight. Showing the ranking pre-play
    # was confusing UX (Noah saw "robber targets · top brick 6" when
    # he hadn't yet decided to play the knight) and gave away which
    # tile he'd target if he did play. The knight_hint verdict
    # ("PLAY/HOLD") and reason are enough for the hold-decision; the
    # tile ranking comes after he commits to play. Forced 7-roll path
    # populates robber_snapshot via _track_overlay_state, and the
    # knight-play path does it via _feed_postmortem.
    try:
        # Pass the authoritative opp card totals + bank supply so the
        # monopoly hint can clamp inferred per-resource counts against
        # physical reality. Without this, drift in tracker.hand() (which
        # accumulates phantom cards from missed steals) would inflate
        # the hint to "drains 19 from blue" even when blue holds 0
        # cards — Noah saw this on 2026-05-01.
        opp_cards = {}
        if game.session is not None:
            for cid, count in game.session.hand_card_counts.items():
                if cid == game.session.self_color_id:
                    continue
                user = game.session.player_names.get(cid)
                if user:
                    try:
                        c = game.color_map.get(user)
                        opp_cards[c] = int(count)
                    except Exception:  # noqa: BLE001
                        pass
        snap["monopoly_hint"] = _compute_monopoly_hint(
            game, self_color, hand,
            display_colors=st.get("display_colors") or {},
            playable_count=hint_fallback,
            opp_card_totals=opp_cards,
            bank_supply=snap.get("bank_supply"))
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] monopoly_hint failed: {e!r}", flush=True)
    try:
        snap["yop_hint"] = _compute_yop_hint(
            game, self_color, hand,
            bank_supply=snap.get("bank_supply"),
            playable_count=hint_fallback)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] yop_hint failed: {e!r}", flush=True)
    try:
        snap["rb_hint"] = _compute_rb_hint(
            game, self_color, playable_count=hint_fallback)
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
    # Discard-on-7 advice. Only fires when self ACTUALLY owes a
    # discard right now — i.e. self just rolled a 7 (robber_pending
    # is set and the hand still exceeds the limit). The pre-roll
    # spend-down warning below (seven_prep) covers the "watch out,
    # next 7 will hurt" case so the discard banner doesn't pop in
    # the alarming "DISCARD 4 cards" red-tile state when no 7 has
    # been rolled. (Noah saw the false positive on 2026-05-01: the
    # minimal-style HUD lit up DISCARD just because his hand was at
    # 9, with no 7 rolled.)
    try:
        if st.get("robber_pending"):
            snap["discard_hint"] = _compute_discard_hint(hand, cards)
        else:
            snap["discard_hint"] = None
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] discard_hint failed: {e!r}", flush=True)
    # Pre-roll spend-down warning. Fires when self is over the safe
    # threshold (DISCARD_LIMIT + 2) but BEFORE a 7 has been rolled.
    # The reactive discard_hint above only fires after the 7 lands;
    # this is the prevention.
    try:
        snap["seven_prep"] = _compute_seven_prep_hint(hand, cards)
    except Exception as e:  # noqa: BLE001
        print(f"[advisor] seven_prep failed: {e!r}", flush=True)
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
        elif r.status == "unhandled" and isinstance(r.event, BuildEvent):
            # BuildEvents without coords (DOM-log "X built a road"
            # without WS-side edge data) silently fall through. The
            # build doesn't apply to catanatron's tracker, which means
            # the recommender keeps suggesting the same road forever.
            # Log so Noah can see when a build was missed instead of
            # being puzzled by stale recs. Other unhandled events
            # (informational text, etc.) stay quiet.
            print(f"[ws #{seq:05d}] UNHANDLED {cls}: {r.message}",
                  flush=True)

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
          postmortem_dir: str | None = None,
          autosave: bool = True) -> int:
    """Run the bridge with uvicorn. Blocks until Ctrl-C.

    ``autosave`` (default True): mirror every inbound /ws frame to
    ``./sessions/active.jsonl`` so a mid-game bridge restart or page
    refresh can resume by replaying the file. Override ``--ws-jsonl``
    takes precedence; if neither is set, autosave kicks in.
    """
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
    if ws_jsonl_path is None and autosave:
        # Auto-resume default: mirror to a stable path so the bridge
        # can rebuild game state on restart by replaying frames. Path
        # lives next to postmortems/ for visibility. Truncated when
        # the user explicitly resets via /reset.
        ws_jsonl_path = (Path.cwd() / "sessions" / "active.jsonl"
                         ).resolve()
    if ws_jsonl_path is not None:
        ws_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"mirroring WS frames to {ws_jsonl_path}")
        # Auto-resume: if the file already has frames from a recent
        # session, replay them through the LiveGame so the bridge
        # boots with rebuilt state instead of empty.
        try:
            import time as _time
            mtime = ws_jsonl_path.stat().st_mtime if ws_jsonl_path.exists() else 0
            age_sec = _time.time() - mtime
            if 0 < age_sec < 6 * 3600:
                print(f"replaying {ws_jsonl_path} (last modified "
                      f"{int(age_sec)}s ago) to restore game state…")
            else:
                # Truncate stale autosave on cold start (older than 6h
                # is presumed to be a different game).
                if ws_jsonl_path.exists() and age_sec >= 6 * 3600:
                    ws_jsonl_path.write_text("")
                    print("autosave was stale (>6h); cleared")
        except OSError:
            pass

    pm_dir: Path | None
    if postmortem_dir is None:
        # Default to ./postmortems relative to wherever the bridge is
        # launched from. Most users run from the repo root, so this
        # lands the postmortem HTMLs in the gitignored ./postmortems/
        # directory next to ws_captures/. Override with --postmortem-dir
        # for a different location, or empty string to disable.
        pm_dir = Path.cwd() / "postmortems"
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
