"""Tests for the colonist WebSocket protocol decoder."""

from __future__ import annotations

import base64
import json
import struct

import pytest

from cataanbot.colonist_proto import (
    DecodedFrame, MsgpackError, decode_frame, decode_msgpack, load_capture,
)


# ---- MessagePack primitives ------------------------------------------------

def test_positive_fixint():
    assert decode_msgpack(bytes([0x00])) == 0
    assert decode_msgpack(bytes([0x7F])) == 127


def test_negative_fixint():
    assert decode_msgpack(bytes([0xE0])) == -32
    assert decode_msgpack(bytes([0xFF])) == -1


def test_bool_and_nil():
    assert decode_msgpack(bytes([0xC0])) is None
    assert decode_msgpack(bytes([0xC2])) is False
    assert decode_msgpack(bytes([0xC3])) is True


def test_fixstr():
    buf = bytes([0xA3]) + b"hey"
    assert decode_msgpack(buf) == "hey"


def test_str8_str16():
    payload = b"x" * 200
    buf = bytes([0xD9, len(payload)]) + payload
    assert decode_msgpack(buf) == "x" * 200

    payload = b"y" * 300
    buf = bytes([0xDA]) + struct.pack(">H", len(payload)) + payload
    assert decode_msgpack(buf) == "y" * 300


def test_uint_variants():
    assert decode_msgpack(bytes([0xCC, 0xFE])) == 0xFE
    assert decode_msgpack(bytes([0xCD, 0x01, 0x02])) == 0x0102
    assert decode_msgpack(bytes([0xCE, 0x00, 0x00, 0x01, 0x00])) == 0x100
    assert (
        decode_msgpack(bytes([0xCF]) + struct.pack(">Q", 0x123456789ABCDEF0))
        == 0x123456789ABCDEF0
    )


def test_int_variants():
    assert decode_msgpack(bytes([0xD0, 0xFE])) == -2
    assert decode_msgpack(bytes([0xD1]) + struct.pack(">h", -1234)) == -1234
    assert decode_msgpack(bytes([0xD2]) + struct.pack(">i", -100000)) == -100000
    assert decode_msgpack(bytes([0xD3]) + struct.pack(">q", -(2 ** 40))) == -(2 ** 40)


def test_float_variants():
    v = decode_msgpack(bytes([0xCB]) + struct.pack(">d", 1.25))
    assert v == 1.25


def test_fixmap_and_fixarray():
    # {id: "130", data: [1, 2, 3]}
    buf = (
        bytes([0x82])              # fixmap-2
        + bytes([0xA2]) + b"id"    # key
        + bytes([0xA3]) + b"130"   # value
        + bytes([0xA4]) + b"data"  # key
        + bytes([0x93, 0x01, 0x02, 0x03])  # fixarray-3 of fixints
    )
    assert decode_msgpack(buf) == {"id": "130", "data": [1, 2, 3]}


def test_bin8():
    buf = bytes([0xC4, 0x03, 0xDE, 0xAD, 0xBE])
    assert decode_msgpack(buf) == b"\xde\xad\xbe"


def test_unknown_type_raises():
    with pytest.raises(MsgpackError):
        # 0xC1 is "reserved / never used" in msgpack.
        decode_msgpack(bytes([0xC1]))


# ---- Frame envelope --------------------------------------------------------

def _msgpack_pong(timestamp: int = 0x0000019D_B24E9AA0) -> bytes:
    """Build the same envelope colonist sends on channel 136."""
    return (
        bytes([0x82])              # fixmap-2
        + bytes([0xA2]) + b"id"
        + bytes([0xA3]) + b"136"
        + bytes([0xA4]) + b"data"
        + bytes([0x81])            # fixmap-1
        + bytes([0xA9]) + b"timestamp"
        + bytes([0xCF]) + struct.pack(">Q", timestamp)
    )


def test_incoming_ping_frame_decodes():
    data = _msgpack_pong()
    frame = decode_frame(data, "in")
    assert frame.error is None
    assert frame.channel == "136"
    assert frame.payload == {"timestamp": 0x0000019D_B24E9AA0}


def test_incoming_type130_diff_frame_decodes():
    body = (
        bytes([0x82])
        + bytes([0xA2]) + b"id"
        + bytes([0xA3]) + b"130"
        + bytes([0xA4]) + b"data"
        + bytes([0x83])  # fixmap-3
        + bytes([0xA4]) + b"type" + bytes([0x5B])
        + bytes([0xA7]) + b"payload" + bytes([0x80])
        + bytes([0xA8]) + b"sequence" + bytes([0x29])
    )
    frame = decode_frame(body, "in")
    assert frame.channel == "130"
    assert frame.payload == {"type": 0x5B, "payload": {}, "sequence": 0x29}


def test_outgoing_envelope_with_channel_name():
    msgpack_body = (
        bytes([0x83])              # fixmap-3
        + bytes([0xA6]) + b"action" + bytes([0x42])
        + bytes([0xA7]) + b"payload" + bytes([0xC0])  # nil
        + bytes([0xA8]) + b"sequence" + bytes([0x00])
    )
    envelope = bytes([0x03, 0x01, 0x06]) + b"021617" + msgpack_body
    frame = decode_frame(envelope, "out")
    assert frame.error is None
    assert frame.envelope_type == 0x03
    assert frame.envelope_flag == 0x01
    assert frame.channel == "021617"
    assert frame.payload == {"action": 66, "payload": None, "sequence": 0}


def test_outgoing_truncated_header_is_soft_error():
    frame = decode_frame(b"\x04", "out")
    assert frame.error is not None


def test_incoming_truncated_buffer_is_soft_error():
    # First 10 bytes of a 33-byte ping: the uint64 timestamp can't fit.
    data = _msgpack_pong()[:10]
    frame = decode_frame(data, "in")
    assert frame.error is not None


# ---- Capture loading -------------------------------------------------------

def test_load_capture_handles_b64_preview_and_head(tmp_path):
    ping_bytes = _msgpack_pong()
    outgoing_msgpack = bytes([0x81]) + bytes([0xA4]) + b"ping" + bytes([0x01])
    outgoing_envelope = bytes([0x03, 0x01, 0x00]) + outgoing_msgpack

    capture = {
        "schema": 1,
        "buffer": [
            # v0.5.2: full base64
            {
                "dir": "in", "ts": 1.0, "wsId": 1,
                "kind": "arraybuffer",
                "byteLength": len(ping_bytes),
                "b64": base64.b64encode(ping_bytes).decode(),
            },
            # v0.5.1 outgoing: comma-separated stringified Uint8Array
            {
                "dir": "out", "ts": 2.0, "wsId": 1,
                "kind": "object",
                "preview": ",".join(str(b) for b in outgoing_envelope),
            },
            # v0.5.1 incoming: truncated latin-1 head (full here because small)
            {
                "dir": "in", "ts": 3.0, "wsId": 1,
                "kind": "arraybuffer",
                "byteLength": len(ping_bytes),
                "head": ping_bytes.decode("latin-1"),
            },
        ],
    }
    p = tmp_path / "cap.json"
    p.write_text(json.dumps(capture))

    frames = list(load_capture(p))
    assert len(frames) == 3
    assert frames[0].error is None and frames[0].channel == "136"
    assert frames[1].error is None and frames[1].envelope_type == 0x03
    assert frames[2].error is None and frames[2].channel == "136"


def test_decoded_frame_dataclass_defaults():
    f = DecodedFrame("in", 0.0, None)
    assert f.payload is None
    assert f.error is None
    assert f.extra == {}
