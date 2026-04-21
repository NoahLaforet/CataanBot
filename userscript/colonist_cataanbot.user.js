// ==UserScript==
// @name         cataanbot — colonist.io log bridge
// @namespace    https://github.com/NoahLaforet/CataanBot
// @version      0.5.0
// @description  Streams colonist.io game-log events to the cataanbot FastAPI bridge on localhost:8765. v0.5 adds WebSocket frame capture for topology mapping.
// @author       Noah Laforet
// @match        https://colonist.io/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

/* eslint-disable no-console */
(() => {
    'use strict';

    const BRIDGE_URL = 'http://127.0.0.1:8765/log';
    const LOG_PREFIX = '[cataanbot]';

    // WebSocket frame capture. Colonist renders the board on a single
    // <canvas id="game-canvas"> — no per-tile DOM — so the only way to
    // map board state to catanatron coordinates is to read the game
    // protocol directly. We patch the WebSocket constructor before any
    // colonist code runs (hence @run-at document-start), wrap send and
    // message events on every instance, and stash frames to a rolling
    // buffer on window.__cataanbotWS for offline inspection.
    //
    // Buffer is capped so long sessions don't balloon memory. Binary
    // frames (Blob / ArrayBuffer) are captured as a typed placeholder
    // with byteLength so we still see they happened without paying for
    // the bytes — colonist's traffic is almost certainly JSON text.
    const WS_BUFFER_MAX = 500;
    (function installWSInterceptor() {
        if (window.__cataanbotWS) return;
        const buffer = [];
        const summary = { opened: 0, sent: 0, recv: 0, errors: 0 };
        window.__cataanbotWS = { buffer, summary };

        const NativeWebSocket = window.WebSocket;
        if (!NativeWebSocket) return;

        function describeData(data) {
            if (typeof data === 'string') {
                return { kind: 'text', length: data.length, data };
            }
            if (data instanceof ArrayBuffer) {
                return { kind: 'arraybuffer', byteLength: data.byteLength };
            }
            if (typeof Blob !== 'undefined' && data instanceof Blob) {
                return { kind: 'blob', byteLength: data.size };
            }
            return { kind: typeof data, preview: String(data).slice(0, 120) };
        }

        function pushFrame(frame) {
            buffer.push(frame);
            if (buffer.length > WS_BUFFER_MAX) {
                buffer.splice(0, buffer.length - WS_BUFFER_MAX);
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
                    pushFrame({ dir: 'out', ts: Date.now() / 1000,
                        wsId, ...describeData(data) });
                } catch (e) { summary.errors += 1; }
                return origSend(data);
            };

            ws.addEventListener('message', (ev) => {
                try {
                    summary.recv += 1;
                    pushFrame({ dir: 'in', ts: Date.now() / 1000,
                        wsId, ...describeData(ev.data) });
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
        window.WebSocket = PatchedWebSocket;
        console.log(LOG_PREFIX, 'WS interceptor installed');
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
        if (typeof GM_xmlhttpRequest === 'function') {
            GM_xmlhttpRequest({
                method: 'POST',
                url: BRIDGE_URL,
                headers: { 'Content-Type': 'application/json' },
                data: JSON.stringify(payload),
                onerror: (e) => console.warn(LOG_PREFIX, 'POST failed', e),
            });
        } else {
            fetch(BRIDGE_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                mode: 'cors',
            }).catch(e => console.warn(LOG_PREFIX, 'fetch failed', e));
        }
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
