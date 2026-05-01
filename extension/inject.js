// Page-context script. Injected by content.js as a <script> tag so it
// runs in colonist's main world, not the extension's isolated world.
//
// Why this gymnastics: Tampermonkey's @grant unsafeWindow let the
// userscript reach into the page's main world and patch
// window.WebSocket directly. Chrome extensions don't get that —
// content scripts always run in an isolated world, and patching
// `window.WebSocket` from there only mutates the isolated copy. So
// we inject this file as a real <script>, which inherits the page's
// global scope, and post intercepted frames out via window.postMessage
// where content.js can pick them up.
//
// Lifecycle:
//   inject.js (page world) — patches WebSocket, posts {type: 'cataanbot-ws', frame}
//   content.js (isolated)  — listens to window.message, forwards to background.js
//   background.js (sw)     — POSTs to http://127.0.0.1:8765/ws

(function installCataanbotWSHook() {
    if (window.__cataanbotWS) return;
    const buffer = [];
    const summary = { opened: 0, sent: 0, recv: 0,
        pings: 0, errors: 0 };
    window.__cataanbotWS = { buffer, summary };

    const NativeWebSocket = window.WebSocket;
    if (!NativeWebSocket) return;

    function bytesToBase64(bytes) {
        let bin = '';
        const chunk = 0x8000;
        for (let i = 0; i < bytes.length; i += chunk) {
            bin += String.fromCharCode.apply(
                null, bytes.subarray(i, i + chunk));
        }
        return btoa(bin);
    }

    function describeData(data) {
        if (typeof data === 'string') {
            return { kind: 'text', length: data.length, data };
        }
        let bytes = null;
        let kind = null;
        if (data instanceof ArrayBuffer) {
            bytes = new Uint8Array(data);
            kind = 'arraybuffer';
        } else if (ArrayBuffer.isView(data)) {
            bytes = new Uint8Array(
                data.buffer, data.byteOffset, data.byteLength);
            kind = data.constructor && data.constructor.name || 'typedarray';
        }
        if (bytes) {
            return {
                kind, byteLength: bytes.length,
                b64: bytesToBase64(bytes),
            };
        }
        return { kind: typeof data, preview: String(data).slice(0, 120) };
    }

    // colonist's keepalive envelope is channel id "136" with only a
    // {timestamp: uint64} body. Always ~33 bytes — drop them so the
    // bridge isn't drowned in pings.
    const PING_PATTERN = [0x82, 0xa2, 0x69, 0x64,
        0xa3, 0x31, 0x33, 0x36];
    function isPingBytes(bytes) {
        if (!bytes || bytes.length > 40) return false;
        for (let i = 0; i < PING_PATTERN.length; i++) {
            if (bytes[i] !== PING_PATTERN[i]) return false;
        }
        return true;
    }

    function postFrame(frame) {
        // Page-world cannot talk to chrome.* APIs. Hand off via
        // postMessage; content.js listens for the same source-origin
        // message and forwards to the extension's service worker.
        window.postMessage({ source: 'cataanbot-ws', frame }, '*');
    }

    function recordFrame(dir, data, wsId) {
        let bytes = null;
        if (data instanceof ArrayBuffer) {
            bytes = new Uint8Array(data);
        } else if (ArrayBuffer.isView(data)) {
            bytes = new Uint8Array(
                data.buffer, data.byteOffset, data.byteLength);
        }
        if (bytes && isPingBytes(bytes)) {
            summary.pings += 1;
            return;
        }
        const frame = { dir, ts: Date.now() / 1000,
            wsId, ...describeData(data) };
        buffer.push(frame);
        if (buffer.length > 2000) {
            buffer.splice(0, buffer.length - 2000);
        }
        if (dir === 'in' && (frame.b64 || frame.data)) {
            postFrame(frame);
        }
    }

    function PatchedWebSocket(url, protocols) {
        const ws = protocols === undefined
            ? new NativeWebSocket(url)
            : new NativeWebSocket(url, protocols);
        summary.opened += 1;
        const wsId = summary.opened;

        const origSend = ws.send.bind(ws);
        ws.send = function patchedSend(data) {
            try {
                summary.sent += 1;
                recordFrame('out', data, wsId);
            } catch (e) { summary.errors += 1; }
            return origSend(data);
        };

        ws.addEventListener('message', (ev) => {
            try {
                summary.recv += 1;
                recordFrame('in', ev.data, wsId);
            } catch (e) { summary.errors += 1; }
        });
        return ws;
    }
    PatchedWebSocket.prototype = NativeWebSocket.prototype;
    PatchedWebSocket.CONNECTING = NativeWebSocket.CONNECTING;
    PatchedWebSocket.OPEN = NativeWebSocket.OPEN;
    PatchedWebSocket.CLOSING = NativeWebSocket.CLOSING;
    PatchedWebSocket.CLOSED = NativeWebSocket.CLOSED;
    window.WebSocket = PatchedWebSocket;

    console.log('[cataanbot] WS interceptor (extension) installed');
})();
