"""Parse a raw userscript payload into a structured Event.

The userscript v0.2+ sends payloads shaped:

    {
      "ts": 1713640000.123,
      "text": "Hans wants to give Lumber Lumber Ore for Grain",
      "parts": [
        {"kind": "name", "name": "Hans", "color": "rgb(224, 151, 66)"},
        {"kind": "text", "text": "wants to give"},
        {"kind": "icon", "alt": "Lumber"},
        {"kind": "icon", "alt": "Lumber"},
        {"kind": "icon", "alt": "Ore"},
        {"kind": "text", "text": "for"},
        {"kind": "icon", "alt": "Grain"},
      ],
      "names": [...],
      "icons": [...],
    }

`parse_event` dispatches on the text patterns observed in live games.
Anything we can't classify becomes `UnknownEvent` so the bridge keeps
flowing while we add rules for it.
"""
from __future__ import annotations

import re
from typing import Any

from cataanbot.events import (
    COLONIST_TO_CATAN_RESOURCE,
    BuildEvent,
    DevCardBuyEvent,
    DevCardPlayEvent,
    DiscardEvent,
    DisconnectEvent,
    Event,
    GameOverEvent,
    InfoEvent,
    MonopolyStealEvent,
    NoStealEvent,
    ProduceEvent,
    RobberMoveEvent,
    RollBlockedEvent,
    RollEvent,
    StealEvent,
    TradeCommitEvent,
    TradeOfferEvent,
    UnknownEvent,
    VPEvent,
)


def _icons(parts: list[dict], start: int = 0, end: int | None = None) -> list[str]:
    """Return the `alt` of every icon part in [start, end)."""
    if end is None:
        end = len(parts)
    return [p["alt"] for p in parts[start:end] if p.get("kind") == "icon"]


def _count_resources(alts: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for a in alts:
        canon = COLONIST_TO_CATAN_RESOURCE.get(a)
        if canon is None:
            continue
        out[canon] = out.get(canon, 0) + 1
    return out


def _find_text(parts: list[dict], needle: str, start: int = 0) -> int:
    """Return the part index whose text contains `needle`, or -1."""
    needle_lc = needle.lower()
    for i in range(start, len(parts)):
        p = parts[i]
        if p.get("kind") == "text" and needle_lc in (p.get("text") or "").lower():
            return i
    return -1


def _first_name(parts: list[dict]) -> str | None:
    for p in parts:
        if p.get("kind") == "name":
            return p.get("name")
    return None


def _names(parts: list[dict]) -> list[str]:
    return [p["name"] for p in parts if p.get("kind") == "name"]


def _text_join(parts: list[dict]) -> str:
    out = []
    for p in parts:
        k = p.get("kind")
        if k == "text":
            out.append(p.get("text", ""))
        elif k == "name":
            out.append(p.get("name", ""))
    return " ".join(out).strip()


def parse_event(payload: dict[str, Any]) -> Event:
    parts = payload.get("parts") or []
    if not parts:
        # Back-compat for v0.1 userscript payloads — best-effort only.
        return UnknownEvent(
            text=payload.get("text", ""),
            icons=[i.get("alt", "") for i in payload.get("icons", [])],
            names=[n.get("name", "") for n in payload.get("names", [])],
        )

    text = _text_join(parts).lower()
    player = _first_name(parts)
    self_name = payload.get("self")

    # --- Info / skip lines ---------------------------------------------------
    if text.startswith("friendly robber"):
        return InfoEvent(text=_text_join(parts))
    if text.startswith("bot is selecting"):
        return InfoEvent(text=_text_join(parts))
    if text.startswith("happy settling") or "list of commands" in text:
        return InfoEvent(text=_text_join(parts))
    if text.startswith("no player to steal from"):
        return NoStealEvent()

    # --- Disconnect / reconnect (may render as plain text without a name span)
    disc = _DISCONNECT_RE.search(_text_join(parts))
    if disc:
        return DisconnectEvent(
            player=disc.group(1),
            reconnected=disc.group(2).lower() == "re",
        )

    # --- Game over ----------------------------------------------------------
    # "Hans won the game!" + trophy icons
    if "won the game" in text and player is not None:
        return GameOverEvent(winner=player)

    # --- Roll blocked (tile carries the subject, no player name) --------------
    if "blocked by the robber" in text:
        tile, prob = _robber_target(parts)
        return RollBlockedEvent(tile_label=tile, prob=prob)

    # --- Self-perspective steals (log reveals the resource) ------------------
    # "You stole from X [Brick]"
    if text.startswith("you stole from") and player is not None:
        res_alt = _first_resource_alt(parts)
        return StealEvent(
            thief=self_name or "YOU",
            victim=player,
            resource=COLONIST_TO_CATAN_RESOURCE.get(res_alt) if res_alt else None,
        )
    # "X stole from you [Wool]"
    if player is not None and "stole from you" in text:
        res_alt = _first_resource_alt(parts)
        return StealEvent(
            thief=player,
            victim=self_name or "YOU",
            resource=COLONIST_TO_CATAN_RESOURCE.get(res_alt) if res_alt else None,
        )

    if player is None:
        return UnknownEvent(
            text=_text_join(parts),
            icons=_icons(parts),
            names=_names(parts),
        )

    # --- Rolls ---------------------------------------------------------------
    if "rolled" in text:
        dice = [a for a in _icons(parts) if a.startswith("dice_")]
        if len(dice) >= 2:
            try:
                d1 = int(dice[0].split("_")[1])
                d2 = int(dice[1].split("_")[1])
                return RollEvent(player=player, d1=d1, d2=d2)
            except (ValueError, IndexError):
                pass

    # --- Production ----------------------------------------------------------
    # "Hans got [Lumber] [Wool]"
    if _starts_with_name_then(parts, "got"):
        res = _count_resources(_icons(parts))
        if res:
            return ProduceEvent(player=player, resources=res)
    # Setup-phase starter resources: "Hans received starting resources [..]"
    if "received starting resources" in text:
        res = _count_resources(_icons(parts))
        if res:
            return ProduceEvent(player=player, resources=res)

    # --- Discard -------------------------------------------------------------
    if "discarded" in text:
        res = _count_resources(_icons(parts))
        if res:
            return DiscardEvent(player=player, resources=res)

    # --- Build ---------------------------------------------------------------
    # "Hans built a Settlement  (+1 VP)" / "BrickdDaddy built a Road"
    # Setup phase and dev-card placements use "placed a" instead of "built a".
    if "built a" in text or "placed a" in text:
        piece = _build_piece(parts)
        if piece is not None:
            vp = 1 if piece in ("settlement", "city") else 0
            return BuildEvent(player=player, piece=piece, vp_delta=vp)

    # --- Robber move ---------------------------------------------------------
    # "Hans moved Robber  to Desert" or "...to [robber] [prob_9] [ore tile]"
    if "moved robber" in text:
        tile, prob = _robber_target(parts)
        return RobberMoveEvent(player=player, tile_label=tile, prob=prob)

    # --- Steal ---------------------------------------------------------------
    # "Grega stole  from Hans"
    if "stole" in text and "from" in text:
        others = [n for n in _names(parts) if n != player]
        if others:
            return StealEvent(thief=player, victim=others[0])

    # --- Monopoly claim ------------------------------------------------------
    # Follow-up to "X used Monopoly": "X stole N [resource]". No 'from'
    # clause, no victim name — the count is pooled across all opponents.
    if "stole" in text and "from" not in text:
        m = _MONOPOLY_COUNT_RE.search(_text_join(parts))
        res_alt = _first_resource_alt_ci(parts)
        canon = COLONIST_TO_CATAN_RESOURCE.get(res_alt) if res_alt else None
        if m and canon:
            return MonopolyStealEvent(
                player=player, resource=canon, count=int(m.group(1)),
            )

    # --- Bank trade ----------------------------------------------------------
    # "Hans gave bank ... and took ..."
    if "gave bank" in text:
        gave, got = _split_trade_icons(parts, "and took")
        return TradeCommitEvent(
            giver=player, receiver="BANK",
            gave=_count_resources(gave), got=_count_resources(got),
        )

    # --- Player-to-player trade (committed) ----------------------------------
    # "Hans gave ... and got ... from Grega"
    if "gave" in text and "and got" in text and "from" in text:
        others = [n for n in _names(parts) if n != player]
        gave, got = _split_trade_icons(parts, "and got")
        if others:
            return TradeCommitEvent(
                giver=player, receiver=others[0],
                gave=_count_resources(gave), got=_count_resources(got),
            )

    # --- Trade offer (pre-commit) --------------------------------------------
    # "Hans wants to give ... for ..."
    if "wants to give" in text:
        give, want = _split_trade_icons(parts, "for")
        return TradeOfferEvent(
            player=player,
            give=_count_resources(give),
            want=_count_resources(want),
        )

    # --- Dev card buy / play -------------------------------------------------
    # "X bought [Development Card]"
    if "bought" in text and any(
            a.lower() == "development card" for a in _icons(parts)
    ):
        return DevCardBuyEvent(player=player)
    # Year of Plenty: "X took from bank [Grain] [Ore]" — 2 resource icons.
    if "took from bank" in text:
        resources = _count_resources(_icons(parts))
        return DevCardPlayEvent(
            player=player, card="year_of_plenty", resources=resources,
        )
    # Knight / Road Building / Monopoly / generic: "X used [icon?]"
    if "used" in text:
        alts = [a.lower() for a in _icons(parts)]
        text_lc = text
        if "knight" in text_lc or any("knight" in a for a in alts):
            return DevCardPlayEvent(player=player, card="knight")
        if "road building" in text_lc or any("road_building" in a for a in alts):
            return DevCardPlayEvent(player=player, card="road_building")
        if "monopoly" in text_lc or any("monopoly" in a for a in alts):
            return DevCardPlayEvent(player=player, card="monopoly")
        if "year of plenty" in text_lc:
            return DevCardPlayEvent(player=player, card="year_of_plenty")
        # Card type not recoverable from the log line alone; downstream
        # can resolve from the next event (e.g. a robber move => knight).
        return DevCardPlayEvent(player=player, card="unknown")

    # --- VP standalone callouts ----------------------------------------------
    if "longest road" in text or "largest army" in text:
        reason = "longest_road" if "longest road" in text else "largest_army"
        others = [n for n in _names(parts) if n != player]
        previous = others[0] if others else None
        return VPEvent(
            player=player, reason=reason, vp_delta=2,
            previous_holder=previous,
        )

    return UnknownEvent(
        text=_text_join(parts),
        icons=_icons(parts),
        names=_names(parts),
    )


# ---------------------------------------------------------------------------
# Helpers below
# ---------------------------------------------------------------------------

def _starts_with_name_then(parts: list[dict], keyword: str) -> bool:
    """True if the first name is followed by text beginning with `keyword`."""
    saw_name = False
    kw = keyword.lower()
    for p in parts:
        k = p.get("kind")
        if k == "name" and not saw_name:
            saw_name = True
            continue
        if saw_name and k == "text":
            return (p.get("text") or "").strip().lower().startswith(kw)
    return False


def _build_piece(parts: list[dict]) -> str | None:
    """Identify what was built from the icon alt ('settlement'/'road'/'city')."""
    alts = [a.lower() for a in _icons(parts)]
    for piece in ("settlement", "city", "road"):
        if piece in alts:
            return piece
    # Fallback: parse from text in case the icon failed.
    text = _text_join(parts).lower()
    for piece in ("settlement", "city", "road"):
        if f"built a {piece}" in text:
            return piece
    return None


def _robber_target(parts: list[dict]) -> tuple[str, int | None]:
    """Extract tile label + probability from a robber-move payload."""
    # Desert is labeled via text ("to Desert"); all other tiles show
    # [robber] [prob_N] [<resource> tile] icons.
    text = _text_join(parts).lower()
    if "to desert" in text:
        return "Desert", None
    icons = _icons(parts)
    prob = None
    tile = "Unknown"
    for a in icons:
        if a.startswith("prob_"):
            try:
                prob = int(a.split("_")[1])
            except (ValueError, IndexError):
                prob = None
        elif a.endswith(" tile"):
            tile = a
    return tile, prob


def _split_trade_icons(
    parts: list[dict],
    boundary: str,
) -> tuple[list[str], list[str]]:
    """Split icon alts into (before_boundary, after_boundary).

    Used for trade messages that place the two resource lists around a
    separator word like 'for', 'and got', or 'and took'. Scans text parts
    for the first containing `boundary` (case-insensitive), then returns
    icons seen before that index and after it.
    """
    boundary_idx = _find_text(parts, boundary)
    if boundary_idx < 0:
        # Boundary text not found — return everything as 'before'.
        return _icons(parts), []
    return _icons(parts, 0, boundary_idx), _icons(parts, boundary_idx + 1)


def _first_resource_alt(parts: list[dict]) -> str | None:
    """Return the alt of the first icon whose alt names a known resource."""
    for a in _icons(parts):
        if a in COLONIST_TO_CATAN_RESOURCE:
            return a
    return None


def _first_resource_alt_ci(parts: list[dict]) -> str | None:
    """Case-insensitive variant: colonist sometimes lowercases icon alts
    (seen in Monopoly follow-up rows)."""
    lookup = {k.lower(): k for k in COLONIST_TO_CATAN_RESOURCE}
    for a in _icons(parts):
        if a.lower() in lookup:
            return lookup[a.lower()]
    return None


# "Afrika stole 10" — one-or-two-digit count after 'stole'.
_MONOPOLY_COUNT_RE = re.compile(r"stole\s+(\d+)", re.IGNORECASE)


# Disconnect/reconnect lines often render without a colored name span
# — colonist wraps the name in plain text inside a different DOM class.
# Match the raw text so we don't depend on `_first_name`.
_DISCONNECT_RE = re.compile(
    r"(\S+)\s+has\s+(dis|re)connected", re.IGNORECASE,
)


def _dev_card_kind(text: str) -> str:
    t = text.lower()
    if "knight" in t:
        return "knight"
    if "road building" in t:
        return "road_building"
    if "year of plenty" in t:
        return "year_of_plenty"
    if "monopoly" in t:
        return "monopoly"
    return "unknown"
