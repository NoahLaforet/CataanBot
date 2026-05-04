// msgpack.js — minimal MessagePack decoder for colonist's WS frames.
//
// Direct port of src/catanbot/colonist_proto.py's _decode_one. Same
// philosophy: subset that colonist actually uses (fixmap, fixarray,
// fixstr, str8/16/32, uint8-64, int8-64, float32/64, bool, nil, bin,
// positive/negative fixint, array16/32, map16/32). No third-party
// deps — tiny enough to ship inline with the extension.
//
// Throws MsgpackError on malformed input rather than corrupting the
// caller's offset; the proto.js wrapper above this catches and
// returns an error frame so a single bad capture doesn't kill the
// pipeline.

export class MsgpackError extends Error {
    constructor(message) {
        super(message);
        this.name = 'MsgpackError';
    }
}

// DataView wrapper so we can read big-endian numbers cleanly without
// keeping track of a stale view across slices.
function dv(buf) {
    if (buf instanceof Uint8Array) {
        return new DataView(buf.buffer,
                            buf.byteOffset, buf.byteLength);
    }
    if (buf instanceof ArrayBuffer) {
        return new DataView(buf);
    }
    throw new MsgpackError('expected Uint8Array or ArrayBuffer');
}

const TEXT_DECODER = new TextDecoder('utf-8', { fatal: false });

function readStr(buf, off, length) {
    const slice = buf.subarray(off, off + length);
    return [TEXT_DECODER.decode(slice), off + length];
}

function readBin(buf, off, length) {
    return [buf.slice(off, off + length), off + length];
}

function readMap(buf, view, off, count) {
    const out = {};
    for (let i = 0; i < count; i++) {
        const [k, off1] = decodeOne(buf, view, off);
        const [v, off2] = decodeOne(buf, view, off1);
        out[k] = v;
        off = off2;
    }
    return [out, off];
}

function readArray(buf, view, off, count) {
    const out = new Array(count);
    for (let i = 0; i < count; i++) {
        const [v, off1] = decodeOne(buf, view, off);
        out[i] = v;
        off = off1;
    }
    return [out, off];
}

/** Decode one msgpack value starting at offset `off`. Returns
 *  [value, newOffset]. */
export function decodeOne(buf, view, off) {
    if (off >= buf.length) {
        throw new MsgpackError(
            `unexpected end of buffer at offset ${off}`);
    }
    const b = buf[off];
    off += 1;

    // Positive fixint (0x00 - 0x7f)
    if (b <= 0x7F) return [b, off];
    // fixmap (0x80 - 0x8f)
    if (b >= 0x80 && b <= 0x8F) return readMap(buf, view, off, b & 0x0F);
    // fixarray (0x90 - 0x9f)
    if (b >= 0x90 && b <= 0x9F) return readArray(buf, view, off, b & 0x0F);
    // fixstr (0xa0 - 0xbf)
    if (b >= 0xA0 && b <= 0xBF) return readStr(buf, off, b & 0x1F);
    // Negative fixint (0xe0 - 0xff)
    if (b >= 0xE0) return [b - 0x100, off];

    switch (b) {
        case 0xC0: return [null, off];
        case 0xC2: return [false, off];
        case 0xC3: return [true, off];
        case 0xC4: {  // bin 8
            const n = buf[off]; off += 1;
            return readBin(buf, off, n);
        }
        case 0xC5: {  // bin 16
            const n = view.getUint16(off, false); off += 2;
            return readBin(buf, off, n);
        }
        case 0xC6: {  // bin 32
            const n = view.getUint32(off, false); off += 4;
            return readBin(buf, off, n);
        }
        case 0xCA: {  // float 32
            const v = view.getFloat32(off, false); return [v, off + 4];
        }
        case 0xCB: {  // float 64
            const v = view.getFloat64(off, false); return [v, off + 8];
        }
        case 0xCC: return [buf[off], off + 1];                 // uint 8
        case 0xCD: return [view.getUint16(off, false), off + 2];   // uint 16
        case 0xCE: return [view.getUint32(off, false), off + 4];   // uint 32
        case 0xCF: {  // uint 64 — JS doesn't have native u64; cap at safe int
            const hi = view.getUint32(off, false);
            const lo = view.getUint32(off + 4, false);
            // hi * 2^32 + lo, but only safe up to 2^53. colonist's
            // counters are small integers in practice so this is
            // fine; if a real overflow ever appears we'd switch
            // to BigInt.
            return [hi * 0x100000000 + lo, off + 8];
        }
        case 0xD0: return [view.getInt8(off), off + 1];        // int 8
        case 0xD1: return [view.getInt16(off, false), off + 2];    // int 16
        case 0xD2: return [view.getInt32(off, false), off + 4];    // int 32
        case 0xD3: {  // int 64 — same caveat as uint 64
            const hi = view.getInt32(off, false);
            const lo = view.getUint32(off + 4, false);
            return [hi * 0x100000000 + lo, off + 8];
        }
        case 0xD9: {  // str 8
            const n = buf[off]; off += 1;
            return readStr(buf, off, n);
        }
        case 0xDA: {  // str 16
            const n = view.getUint16(off, false); off += 2;
            return readStr(buf, off, n);
        }
        case 0xDB: {  // str 32
            const n = view.getUint32(off, false); off += 4;
            return readStr(buf, off, n);
        }
        case 0xDC: {  // array 16
            const n = view.getUint16(off, false); off += 2;
            return readArray(buf, view, off, n);
        }
        case 0xDD: {  // array 32
            const n = view.getUint32(off, false); off += 4;
            return readArray(buf, view, off, n);
        }
        case 0xDE: {  // map 16
            const n = view.getUint16(off, false); off += 2;
            return readMap(buf, view, off, n);
        }
        case 0xDF: {  // map 32
            const n = view.getUint32(off, false); off += 4;
            return readMap(buf, view, off, n);
        }
    }
    throw new MsgpackError(
        `unsupported msgpack type byte 0x${b.toString(16).padStart(2, '0')} `
        + `at offset ${off - 1}`);
}

/** Decode a single msgpack value from the head of `data`.
 *  `data` may be a Uint8Array, ArrayBuffer, or base64 string
 *  (the b64 form is what inject.js postMessages out). */
export function decodeMsgpack(data) {
    const buf = toUint8Array(data);
    const view = dv(buf);
    const [value] = decodeOne(buf, view, 0);
    return value;
}

/** Coerce common input shapes to a Uint8Array for the decoder. */
export function toUint8Array(data) {
    if (data instanceof Uint8Array) return data;
    if (data instanceof ArrayBuffer) return new Uint8Array(data);
    if (typeof data === 'string') {
        // base64 → bytes. atob returns a binary string; expand to
        // Uint8Array. inject.js produces base64 via btoa() so
        // round-tripping preserves bytes exactly.
        const bin = atob(data);
        const out = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
        return out;
    }
    if (data && data.buffer instanceof ArrayBuffer) {
        return new Uint8Array(data.buffer,
                              data.byteOffset, data.byteLength);
    }
    throw new MsgpackError(`unsupported input type: ${typeof data}`);
}

/** Decode a colonist incoming frame envelope. Server-to-client
 *  frames are pure msgpack and almost always shape:
 *    {"id": <channel>, "data": <payload>}
 *  Returns {channel, payload, error}. */
export function decodeIncomingFrame(data) {
    try {
        const msg = decodeMsgpack(data);
        if (msg && typeof msg === 'object' && 'id' in msg) {
            return { channel: String(msg.id), payload: msg.data };
        }
        return { channel: null, payload: msg };
    } catch (e) {
        return { channel: null, payload: null, error: e.message };
    }
}

/** Decode a colonist outgoing frame envelope:
 *    [type(1)][flag(1)][name_len(1)][name_bytes(name_len)][msgpack_body]
 */
export function decodeOutgoingFrame(data) {
    const buf = toUint8Array(data);
    if (buf.length < 3) {
        return { channel: null, payload: null,
                 error: 'truncated envelope header' };
    }
    const envType = buf[0];
    const envFlag = buf[1];
    const nameLen = buf[2];
    if (buf.length < 3 + nameLen) {
        return { envelopeType: envType, envelopeFlag: envFlag,
                 channel: null, payload: null,
                 error: 'truncated channel name' };
    }
    const nameSlice = buf.subarray(3, 3 + nameLen);
    const name = String.fromCharCode.apply(null, nameSlice);
    const body = buf.subarray(3 + nameLen);
    try {
        const payload = body.length ? decodeMsgpack(body) : null;
        return { envelopeType: envType, envelopeFlag: envFlag,
                 channel: name, payload };
    } catch (e) {
        return { envelopeType: envType, envelopeFlag: envFlag,
                 channel: name, payload: null, error: e.message };
    }
}
