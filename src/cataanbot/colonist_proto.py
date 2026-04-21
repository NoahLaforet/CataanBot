"""Decode colonist.io WebSocket frames captured by the userscript.

Colonist uses a MessagePack-encoded wire protocol. Frames come in two
envelope shapes:

Incoming (server → client)
    Pure MessagePack. Empirically always a 2-entry map
    ``{"id": "<channel>", "data": <payload>}``. Channels observed so far:
        "lobby"  — lobby state
        "130"    — game-state diffs / actions (the interesting channel)
        "136"    — keepalive pings with a single "timestamp" field
        "137"    — chat

Outgoing (client → server)
    ``[type(1)][flag(1)][name_len(1)][name_bytes(name_len)][msgpack_body]``
    with ``name_bytes`` being the ASCII channel name (e.g. ``"021617"``
    for the room id, or empty for keepalive pongs).

This module exposes:

    decode_msgpack(b: bytes) -> object
    decode_frame(b: bytes, direction: str) -> DecodedFrame
    load_capture(path) -> iter[DecodedFrame]

The MessagePack decoder is a minimal hand-rolled implementation — we
want zero third-party deps in the cataanbot package, and the subset
colonist uses is small (fixmap/fixarray/fixstr, str8/16, uint8-64,
int8-64, float32/64, bool, nil, bin, positive/negative fixint).

The decoder is tolerant: if it hits an unknown type byte it records a
``_decode_error`` at that offset rather than raising, so a single bad
frame never kills a batch analysis.
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


class MsgpackError(ValueError):
    pass


def _read_map(buf: bytes, off: int, count: int) -> tuple[dict, int]:
    out: dict = {}
    for _ in range(count):
        key, off = _decode_one(buf, off)
        value, off = _decode_one(buf, off)
        out[key] = value
    return out, off


def _read_array(buf: bytes, off: int, count: int) -> tuple[list, int]:
    out: list = []
    for _ in range(count):
        value, off = _decode_one(buf, off)
        out.append(value)
    return out, off


def _read_str(buf: bytes, off: int, length: int) -> tuple[str, int]:
    end = off + length
    # colonist occasionally puts latin-1 bytes in user display names;
    # utf-8 with 'replace' keeps the decoder robust.
    return buf[off:end].decode("utf-8", errors="replace"), end


def _read_bin(buf: bytes, off: int, length: int) -> tuple[bytes, int]:
    end = off + length
    return bytes(buf[off:end]), end


def _decode_one(buf: bytes, off: int) -> tuple[Any, int]:
    if off >= len(buf):
        raise MsgpackError(f"unexpected end of buffer at offset {off}")
    b = buf[off]
    off += 1

    # Positive fixint (0x00 - 0x7f)
    if b <= 0x7F:
        return b, off
    # fixmap (0x80 - 0x8f)
    if 0x80 <= b <= 0x8F:
        return _read_map(buf, off, b & 0x0F)
    # fixarray (0x90 - 0x9f)
    if 0x90 <= b <= 0x9F:
        return _read_array(buf, off, b & 0x0F)
    # fixstr (0xa0 - 0xbf)
    if 0xA0 <= b <= 0xBF:
        return _read_str(buf, off, b & 0x1F)
    # Negative fixint (0xe0 - 0xff)
    if b >= 0xE0:
        return b - 0x100, off

    # Named type bytes
    if b == 0xC0:
        return None, off
    if b == 0xC2:
        return False, off
    if b == 0xC3:
        return True, off
    if b == 0xC4:  # bin 8
        length = buf[off]; off += 1
        return _read_bin(buf, off, length)
    if b == 0xC5:  # bin 16
        length = struct.unpack_from(">H", buf, off)[0]; off += 2
        return _read_bin(buf, off, length)
    if b == 0xC6:  # bin 32
        length = struct.unpack_from(">I", buf, off)[0]; off += 4
        return _read_bin(buf, off, length)
    if b == 0xCA:  # float 32
        v = struct.unpack_from(">f", buf, off)[0]; off += 4
        return v, off
    if b == 0xCB:  # float 64
        v = struct.unpack_from(">d", buf, off)[0]; off += 8
        return v, off
    if b == 0xCC:  # uint 8
        return buf[off], off + 1
    if b == 0xCD:  # uint 16
        v = struct.unpack_from(">H", buf, off)[0]; return v, off + 2
    if b == 0xCE:  # uint 32
        v = struct.unpack_from(">I", buf, off)[0]; return v, off + 4
    if b == 0xCF:  # uint 64
        v = struct.unpack_from(">Q", buf, off)[0]; return v, off + 8
    if b == 0xD0:  # int 8
        v = struct.unpack_from(">b", buf, off)[0]; return v, off + 1
    if b == 0xD1:  # int 16
        v = struct.unpack_from(">h", buf, off)[0]; return v, off + 2
    if b == 0xD2:  # int 32
        v = struct.unpack_from(">i", buf, off)[0]; return v, off + 4
    if b == 0xD3:  # int 64
        v = struct.unpack_from(">q", buf, off)[0]; return v, off + 8
    if b == 0xD9:  # str 8
        length = buf[off]; off += 1
        return _read_str(buf, off, length)
    if b == 0xDA:  # str 16
        length = struct.unpack_from(">H", buf, off)[0]; off += 2
        return _read_str(buf, off, length)
    if b == 0xDB:  # str 32
        length = struct.unpack_from(">I", buf, off)[0]; off += 4
        return _read_str(buf, off, length)
    if b == 0xDC:  # array 16
        count = struct.unpack_from(">H", buf, off)[0]; off += 2
        return _read_array(buf, off, count)
    if b == 0xDD:  # array 32
        count = struct.unpack_from(">I", buf, off)[0]; off += 4
        return _read_array(buf, off, count)
    if b == 0xDE:  # map 16
        count = struct.unpack_from(">H", buf, off)[0]; off += 2
        return _read_map(buf, off, count)
    if b == 0xDF:  # map 32
        count = struct.unpack_from(">I", buf, off)[0]; off += 4
        return _read_map(buf, off, count)

    raise MsgpackError(
        f"unsupported msgpack type byte 0x{b:02x} at offset {off - 1}")


def decode_msgpack(data: bytes) -> Any:
    """Decode a single MessagePack value from the head of ``data``."""
    value, _ = _decode_one(data, 0)
    return value


@dataclass
class DecodedFrame:
    direction: str            # 'in' | 'out' | 'open' | 'close'
    ts: float
    channel: str | None       # server channel id for inbound, room id for outbound
    envelope_type: int | None = None  # outbound only
    envelope_flag: int | None = None  # outbound only
    payload: Any = None
    raw_length: int = 0
    error: str | None = None
    extra: dict = field(default_factory=dict)


def decode_frame(data: bytes, direction: str) -> DecodedFrame:
    """Decode a single captured frame.

    ``direction`` is ``'in'`` (server → client, pure msgpack) or ``'out'``
    (client → server, has the 3-byte + name envelope).

    Capture dumps from userscript v0.5.1 and earlier truncate large
    incoming frames to 64 bytes; those raise mid-parse with
    ``struct.error``. We catch both ``struct.error`` and
    ``MsgpackError`` and return the frame with an error message rather
    than raising.
    """
    if direction == "in":
        try:
            msg = decode_msgpack(data)
        except (MsgpackError, struct.error) as e:
            return DecodedFrame(direction, 0.0, None,
                                raw_length=len(data), error=str(e))
        channel = msg.get("id") if isinstance(msg, dict) else None
        payload = msg.get("data") if isinstance(msg, dict) else msg
        return DecodedFrame(direction, 0.0, channel,
                            payload=payload, raw_length=len(data))

    if direction == "out":
        if len(data) < 3:
            return DecodedFrame(direction, 0.0, None,
                                raw_length=len(data),
                                error="truncated envelope header")
        env_type = data[0]
        env_flag = data[1]
        name_len = data[2]
        if len(data) < 3 + name_len:
            return DecodedFrame(direction, 0.0, None,
                                envelope_type=env_type,
                                envelope_flag=env_flag,
                                raw_length=len(data),
                                error="truncated channel name")
        name = data[3:3 + name_len].decode("ascii", errors="replace")
        body = data[3 + name_len:]
        try:
            payload = decode_msgpack(body) if body else None
        except (MsgpackError, struct.error) as e:
            return DecodedFrame(direction, 0.0, name,
                                envelope_type=env_type,
                                envelope_flag=env_flag,
                                raw_length=len(data),
                                error=str(e))
        return DecodedFrame(direction, 0.0, name,
                            envelope_type=env_type,
                            envelope_flag=env_flag,
                            payload=payload,
                            raw_length=len(data))

    # open / close / other meta frames have no payload to decode
    return DecodedFrame(direction, 0.0, None, raw_length=0)


def _frame_bytes(frame: dict) -> bytes | None:
    """Extract raw bytes from one of the userscript's frame records.

    v0.5.2 captures as ``b64``. v0.5.1 and earlier captured:
      - incoming as ``head`` (truncated latin-1 string, first 64 bytes)
      - outgoing as a stringified Uint8Array in ``preview`` (complete
        but as a comma-separated decimal list)
    We accept all three so old dumps are still analyzable.
    """
    if "b64" in frame:
        return base64.b64decode(frame["b64"])
    if frame.get("kind") == "object" and frame.get("preview"):
        try:
            return bytes(int(x) for x in frame["preview"].split(","))
        except ValueError:
            return None
    if frame.get("kind") == "arraybuffer" and frame.get("head"):
        # latin-1 round-trip preserves the bytes we captured; will be
        # truncated to 64 bytes for pre-v0.5.2 captures.
        return frame["head"].encode("latin-1", errors="replace")
    return None


def load_capture(path: str | Path) -> Iterator[DecodedFrame]:
    """Yield ``DecodedFrame`` objects from a userscript capture dump."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for raw in data.get("buffer", []):
        direction = raw.get("dir")
        if direction in ("open", "close"):
            yield DecodedFrame(direction, raw.get("ts", 0.0),
                               None, raw_length=0,
                               extra={"url": raw.get("url")})
            continue
        if direction not in ("in", "out"):
            continue
        body = _frame_bytes(raw)
        if body is None:
            yield DecodedFrame(direction, raw.get("ts", 0.0), None,
                               raw_length=raw.get("byteLength", 0),
                               error="no bytes in frame")
            continue
        frame = decode_frame(body, direction)
        frame.ts = raw.get("ts", 0.0)
        yield frame


def summarize(frames: Iterator[DecodedFrame]) -> dict:
    """Cheap histogram over an iterator of decoded frames."""
    counts: dict[str, int] = {}
    errors = 0
    for f in frames:
        if f.error:
            errors += 1
            continue
        key = f.direction
        if f.channel is not None:
            key = f"{f.direction}:{f.channel}"
            if f.direction == "in" and isinstance(f.payload, dict):
                t = f.payload.get("type")
                if t is not None:
                    key = f"{key}/type={t}"
        counts[key] = counts.get(key, 0) + 1
    return {"counts": counts, "errors": errors}
