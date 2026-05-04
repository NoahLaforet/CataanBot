// bridge_probe.js — detect whether Noah's local Python bridge is
// reachable on 127.0.0.1:8765. Exports a single async function that
// caches the result for ~30 seconds so the panel doesn't probe on
// every poll.
//
// When the bridge is reachable, the extension uses bridge-served
// snapshots (full strategy archetype tracker, postmortem auto-open,
// 1-ply search rerank). When it isn't, the extension falls back to
// the in-extension JS recommender (lib/advisor, lib/recommender,
// etc.) and hides the bridge-only panels.
//
// This file is the only place that hard-codes the bridge URL — the
// rest of the extension reads `bridgeReachable()` and branches.

const BRIDGE_URL_BASE = 'http://127.0.0.1:8765';
const PROBE_PATH = '/';            // health endpoint, always cheap
const CACHE_MS = 30_000;
const PROBE_TIMEOUT_MS = 1500;

let _cached = { reachable: null, at: 0 };
let _inflight = null;

async function _doProbe() {
    // AbortController so the probe doesn't hang the panel when the
    // bridge is dead and the OS takes its sweet time refusing the
    // connection.
    const controller = new AbortController();
    const timer = setTimeout(
        () => controller.abort(), PROBE_TIMEOUT_MS);
    try {
        const resp = await fetch(BRIDGE_URL_BASE + PROBE_PATH, {
            method: 'GET',
            signal: controller.signal,
            // Bypass the SW fetch cache — we want a real connect.
            cache: 'no-store',
        });
        return resp.ok;
    } catch (_) {
        // Aborted, refused, DNS fail — all read as "not reachable."
        return false;
    } finally {
        clearTimeout(timer);
    }
}

/** Return true when the local Python bridge is reachable.
 *
 * Caches for CACHE_MS; concurrent calls share a single inflight
 * probe. Default opts trust the cache; pass `{ fresh: true }` to
 * force a re-probe (e.g. after the user clicks "retry connection"
 * in the extension popup).
 */
export async function bridgeReachable(opts = {}) {
    const now = Date.now();
    if (!opts.fresh
            && _cached.reachable !== null
            && (now - _cached.at) < CACHE_MS) {
        return _cached.reachable;
    }
    if (_inflight) return _inflight;
    _inflight = (async () => {
        const ok = await _doProbe();
        _cached = { reachable: ok, at: Date.now() };
        _inflight = null;
        return ok;
    })();
    return _inflight;
}

/** Force-clear the cache. Used by tests and by the popup's
 *  "retry connection" button. */
export function _resetBridgeCache() {
    _cached = { reachable: null, at: 0 };
    _inflight = null;
}

export const BRIDGE_URL = BRIDGE_URL_BASE;
