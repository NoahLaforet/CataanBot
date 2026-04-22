// ==UserScript==
// @name         cataanbot — colonist.io log bridge
// @namespace    https://github.com/NoahLaforet/CataanBot
// @version      0.6.0
// @description  Streams colonist.io game-log events + WebSocket frames to the cataanbot FastAPI bridge on localhost:8765. v0.6 forwards every captured WS frame to /ws so the live advisor can run.
// @author       Noah Laforet
// @match        https://colonist.io/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

/* eslint-disable no-console */
(() => {
    'use strict';

    const BRIDGE_URL = 'http://127.0.0.1:8765/log';
    const BRIDGE_WS_URL = 'http://127.0.0.1:8765/ws';
    const LOG_PREFIX = '[cataanbot]';

    // Fire-and-forget POST. Used by both the DOM log forwarder (/log)
    // and the WS frame forwarder (/ws). Keeps the userscript quiet even
    // if the bridge is down so a game session isn't noisy.
    function postTo(url, payload, { quiet } = {}) {
        if (typeof GM_xmlhttpRequest === 'function') {
            GM_xmlhttpRequest({
                method: 'POST',
                url,
                headers: { 'Content-Type': 'application/json' },
                data: JSON.stringify(payload),
                onerror: (e) => { if (!quiet)
                    console.warn(LOG_PREFIX, 'POST failed', e); },
            });
        } else {
            fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                mode: 'cors',
            }).catch(e => { if (!quiet)
                console.warn(LOG_PREFIX, 'fetch failed', e); });
        }
    }

    // WebSocket frame capture. Colonist renders the board on a single
    // <canvas id="game-canvas"> — no per-tile DOM — so the only way to
    // map board state to catanatron coordinates is to read the game
    // protocol directly. We patch the WebSocket constructor before any
    // colonist code runs (hence @run-at document-start), wrap send and
    // message events on every instance, and stash frames to a rolling
    // buffer on unsafeWindow.__cataanbotWS for offline inspection.
    //
    // unsafeWindow is required because Tampermonkey runs userscripts in
    // Chrome's isolated content-script world. Patching `window.WebSocket`
    // from the isolated world changes the isolated window, not the main
    // world colonist actually uses — so colonist's `new WebSocket()` call
    // hits the untouched native constructor. `unsafeWindow` is the main-
    // world window; patches there propagate to the real runtime.
    //
    // Every frame is captured in full as base64 so the protocol can be
    // decoded offline. v0.5.1 truncated large frames to 64 bytes which
    // hid GameStart + tileCornerStates diffs — exactly the topology
    // frames we need. v0.5.2 also skips the ~1Hz ping/pong envelope
    // (channel id "136", ~33 bytes) so long capture sessions don't
    // evict the important frames at the head of the buffer.
    //
    // Expose __cataanbotWSDump() as a one-shot "save capture to disk"
    // helper so we can grab the buffer from the DevTools console without
    // pasting a scrape every time.
    const WS_BUFFER_MAX = 2000;
    (function installWSInterceptor() {
        const tgt = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
        if (tgt.__cataanbotWS) return;
        const buffer = [];
        const summary = { opened: 0, sent: 0, recv: 0,
            pings: 0, errors: 0 };
        tgt.__cataanbotWS = { buffer, summary };

        const NativeWebSocket = tgt.WebSocket;
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
                kind = data.constructor?.name || 'typedarray';
            }
            if (bytes) {
                return {
                    kind, byteLength: bytes.length,
                    b64: bytesToBase64(bytes),
                };
            }
            if (typeof Blob !== 'undefined' && data instanceof Blob) {
                return { kind: 'blob', byteLength: data.size, pending: true };
            }
            return { kind: typeof data, preview: String(data).slice(0, 120) };
        }

        // colonist's keepalive envelope is channel id "136" with only a
        // {timestamp: uint64} body. Always ~33 bytes and drowns out the
        // ~5KB GameStart frame if we buffer them. Detect by byte pattern
        // on the raw head: msgpack fixmap-2 (0x82) "id"(0xA2 i d) "136"
        // (0xA3 '1' '3' '6').
        const PING_PATTERN = [0x82, 0xa2, 0x69, 0x64,
            0xa3, 0x31, 0x33, 0x36];
        function isPingBytes(bytes) {
            if (!bytes || bytes.length > 40) return false;
            for (let i = 0; i < PING_PATTERN.length; i++) {
                if (bytes[i] !== PING_PATTERN[i]) return false;
            }
            return true;
        }

        function pushFrame(frame) {
            buffer.push(frame);
            if (buffer.length > WS_BUFFER_MAX) {
                buffer.splice(0, buffer.length - WS_BUFFER_MAX);
            }
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
            pushFrame(frame);
            // Forward inbound-direction frames to the bridge. Outbound
            // (user actions) aren't needed for the game-state pipe and
            // would just double traffic. Bridge is local so this is
            // cheap; keep it quiet if the bridge is down so nobody sees
            // failure spam mid-game.
            if (dir === 'in' && (frame.b64 || frame.data)) {
                postTo(BRIDGE_WS_URL, frame, { quiet: true });
            }
        }

        function PatchedWebSocket(url, protocols) {
            const ws = protocols === undefined
                ? new NativeWebSocket(url)
                : new NativeWebSocket(url, protocols);
            summary.opened += 1;
            const wsId = summary.opened;
            pushFrame({ dir: 'open', ts: Date.now() / 1000, wsId, url });

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
            ws.addEventListener('close', () => {
                pushFrame({ dir: 'close', ts: Date.now() / 1000, wsId });
            });
            return ws;
        }
        PatchedWebSocket.prototype = NativeWebSocket.prototype;
        PatchedWebSocket.CONNECTING = NativeWebSocket.CONNECTING;
        PatchedWebSocket.OPEN = NativeWebSocket.OPEN;
        PatchedWebSocket.CLOSING = NativeWebSocket.CLOSING;
        PatchedWebSocket.CLOSED = NativeWebSocket.CLOSED;
        tgt.WebSocket = PatchedWebSocket;

        tgt.__cataanbotWSDump = function dumpWS(label) {
            const payload = {
                schema: 1, capturedAt: Date.now() / 1000,
                url: location.href, summary,
                buffer: buffer.slice(),
            };
            const blob = new Blob(
                [JSON.stringify(payload, null, 2)],
                { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            const ts = new Date().toISOString()
                .replace(/[:.]/g, '-').slice(0, 19);
            a.download = `cataanbot-ws-${label ? label + '-' : ''}${ts}.json`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
            return a.download;
        };

        console.log(LOG_PREFIX, 'WS interceptor v0.6.0 installed on',
            tgt === window ? 'window' : 'unsafeWindow',
            '(forwarding to', BRIDGE_WS_URL + ')');
    })();

    // Selectors captured from DOM recon (COLONIST_RECON.md). Class
    // hashes are fragile across deploys — fall back defensively.
    const SEL = {
        scroller: 'div.virtualScroller-lSkdkGJi',
        entry:    'div.scrollItemContainer-WXX2rkzf',
        text:     'span.messagePart-XeUsOgLX',
    };

    // Dedup is three-layered:
    //  1. At-bottom gate — we only process entries when the scroller is
    //     at the bottom (user is following live). Scrolling up to review
    //     causes colonist to destroy the bottom nodes and re-render older
    //     ones in-place; treating those as new floods the bridge with
    //     minutes-old events stamped with the current time. Pausing while
    //     scrolled up sidesteps the whole mess. New events that arrive
    //     while scrolled up are captured when the user scrolls back down.
    //  2. Per-DOM-node dataset marker — skips a node we already processed
    //     this run. Handles MutationObserver + polling racing on the same
    //     element.
    //  3. Content cache — skips the same payload if we posted it within
    //     RECENT_TTL_MS. Backstop for recycled nodes that slip past the
    //     at-bottom check and lose their per-node dataset marker when
    //     colonist's virtualizer destroys + re-creates them on scroll.
    // TTL needs to be long enough to cover a full setup phase (~45s of
    // back-to-back "placed a Settlement / Road" events that share the
    // same content key per player). Legitimate same-content repeats at
    // this distance are rare: a whole turn cycle takes ~20-30s in a
    // 4-player game, so 60s is comfortably below the "two of the same
    // roll outcome actually repeat" floor. game5 had ~90 duplicated
    // keys at the 5s setting because setup spans longer than 5s.
    const NODE_KEY_ATTR = 'cataanbotKey';
    const RECENT_TTL_MS = 60000;
    const AT_BOTTOM_PX = 50;
    const recentSeen = new Map();

    function isAtBottom(scroller) {
        return (scroller.scrollHeight - scroller.scrollTop
                - scroller.clientHeight) < AT_BOTTOM_PX;
    }

    // Walk the whole scrollItemContainer in document order, emitting
    // ordered parts. We can't just walk messagePart because some events
    // (dev-card play "X used [Knight]") render the card icon as a
    // sibling of messagePart, not a child. Avatars have alt="" and are
    // dropped by the icon rule below.
    function serializeEntry(el) {
        const root = el;

        const parts = [];

        const walk = (node) => {
            if (node.nodeType === Node.TEXT_NODE) {
                const t = (node.textContent || '').replace(/\s+/g, ' ').trim();
                if (t) parts.push({ kind: 'text', text: t });
                return;
            }
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            const el = node;
            if (el.tagName === 'IMG') {
                const alt = el.alt || '';
                if (!alt) return; // drop avatar
                parts.push({
                    kind: 'icon',
                    alt,
                    src_tail: (el.getAttribute('src') || '').split('/').pop(),
                });
                return;
            }
            // Player name pill: inline colored span. If we match one,
            // don't recurse — emit as a single token.
            const style = el.getAttribute?.('style') || '';
            if (el.tagName === 'SPAN' && /color\s*:/i.test(style)) {
                const name = (el.innerText || '').trim();
                if (name) {
                    parts.push({
                        kind: 'name',
                        name,
                        color: el.style.color || '',
                    });
                }
                return;
            }
            // VP callout: <span class="vp-text">+1 VP</span>
            if (el.classList?.contains('vp-text')) {
                parts.push({ kind: 'vp', text: (el.innerText || '').trim() });
                return;
            }
            // Recurse into generic containers.
            for (const child of el.childNodes) walk(child);
        };

        for (const child of root.childNodes) walk(child);

        const text = parts
            .filter(p => p.kind === 'text' || p.kind === 'name' || p.kind === 'vp')
            .map(p => p.kind === 'name' ? p.name : p.text)
            .join(' ')
            .replace(/\s+/g, ' ')
            .trim();

        // Flat views kept for back-compat + easy debugging.
        const names = parts.filter(p => p.kind === 'name')
            .map(p => ({ name: p.name, color: p.color }));
        const icons = parts.filter(p => p.kind === 'icon')
            .map(p => ({ alt: p.alt, src_tail: p.src_tail }));

        return {
            ts: Date.now() / 1000,
            self: detectSelf(),
            text,
            parts,
            names,
            icons,
            key: `${text}|${icons.map(i => i.alt).join(',')}|${names.map(n => n.name).join(',')}`,
        };
    }

    // Detect the active user's username from localStorage.userState —
    // colonist.io stores the logged-in user there as `username`. Much
    // more reliable than DOM scraping (the "(You)" marker only shows
    // up in the lobby, not during gameplay). Cached after first read.
    let cachedSelf = null;
    function detectSelf() {
        if (cachedSelf) return cachedSelf;
        try {
            const raw = localStorage.getItem('userState');
            if (!raw) return null;
            const us = JSON.parse(raw);
            if (us && typeof us.username === 'string' && us.username) {
                cachedSelf = us.username;
                return cachedSelf;
            }
        } catch (_) {
            /* ignore parse errors */
        }
        return null;
    }

    function post(payload) {
        postTo(BRIDGE_URL, payload);
    }

    function processEntry(el) {
        if (!el || !(el instanceof Element)) return;
        if (!el.matches(SEL.entry)) return;
        const payload = serializeEntry(el);
        if (!payload.text && payload.icons.length === 0) return;
        if (el.dataset[NODE_KEY_ATTR] === payload.key) return;
        el.dataset[NODE_KEY_ATTR] = payload.key;

        const now = Date.now();
        const expiresAt = recentSeen.get(payload.key);
        if (expiresAt && expiresAt > now) return;
        recentSeen.set(payload.key, now + RECENT_TTL_MS);
        if (recentSeen.size > 400) {
            for (const [k, t] of recentSeen) {
                if (t <= now) recentSeen.delete(k);
            }
        }

        post(payload);
        console.log(LOG_PREFIX, '->', payload.text || '(icons)',
                    payload.icons.map(i => i.alt).filter(Boolean).join(','));
    }

    function attach(scroller) {
        console.log(LOG_PREFIX, 'attached to log scroller');
        scroller.querySelectorAll(SEL.entry).forEach(processEntry);

        const observer = new MutationObserver((mutations) => {
            if (!isAtBottom(scroller)) return;
            for (const m of mutations) {
                m.addedNodes.forEach((n) => {
                    if (!(n instanceof Element)) return;
                    if (n.matches(SEL.entry)) {
                        processEntry(n);
                    } else {
                        n.querySelectorAll?.(SEL.entry).forEach(processEntry);
                    }
                });
            }
        });
        observer.observe(scroller, { childList: true, subtree: true });

        // Safety net: poll every 500ms for any entries the observer missed.
        // MutationObservers can batch rapid insertions (common on colonist's
        // virtualized list) and occasionally skip nodes; the per-node dedup
        // above means re-scanning is cheap and idempotent.
        setInterval(() => {
            if (!isAtBottom(scroller)) return;
            scroller.querySelectorAll(SEL.entry).forEach(processEntry);
        }, 500);
    }

    function waitForScroller() {
        let tries = 0;
        const maxTries = 600;
        const iv = setInterval(() => {
            tries += 1;
            const scroller = document.querySelector(SEL.scroller);
            if (scroller) {
                clearInterval(iv);
                attach(scroller);
                return;
            }
            if (tries >= maxTries) {
                clearInterval(iv);
                console.warn(LOG_PREFIX, 'gave up waiting for log scroller');
            }
        }, 500);
    }

    console.log(LOG_PREFIX, 'loaded — waiting for a game to open');
    waitForScroller();
})();
