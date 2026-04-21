// ==UserScript==
// @name         cataanbot — colonist.io log bridge
// @namespace    https://github.com/NoahLaforet/CataanBot
// @version      0.1.0
// @description  Streams colonist.io game-log events to the cataanbot FastAPI bridge on localhost:8765.
// @author       Noah Laforet
// @match        https://colonist.io/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

/* eslint-disable no-console */
(() => {
    'use strict';

    const BRIDGE_URL = 'http://127.0.0.1:8765/log';
    const LOG_PREFIX = '[cataanbot]';

    // Selectors captured from DOM recon (COLONIST_RECON.md). Class
    // hashes are fragile across deploys — fall back defensively.
    const SEL = {
        scroller: 'div.virtualScroller-lSkdkGJi',
        entry:    'div.scrollItemContainer-WXX2rkzf',
        text:     'span.messagePart-XeUsOgLX',
        icon:     'img.lobby-chat-text-icon',
    };

    const seenKeys = new Set();

    function serializeEntry(el) {
        const textSpan = el.querySelector(SEL.text);
        const text = (textSpan?.innerText || el.innerText || '').trim();

        // Player name pills: inline colored spans nested inside messagePart.
        const names = Array.from(
            (textSpan || el).querySelectorAll('span[style*="color:"]')
        ).map(s => ({
            name: (s.innerText || '').trim(),
            color: (s.style.color || '').trim(),
        })).filter(n => n.name);

        // Icons (resources, dice, pieces, tiles). Alt text is the key.
        const icons = Array.from(el.querySelectorAll('img')).map(img => ({
            alt: img.alt || '',
            src_tail: (img.getAttribute('src') || '').split('/').pop(),
        })).filter(i => i.alt || i.src_tail);

        return {
            ts: Date.now() / 1000,
            text,
            names,
            icons,
            // A compact key for dedup — virtualized list can re-render.
            key: `${text}|${icons.map(i => i.alt).join(',')}|${names.map(n => n.name).join(',')}`,
        };
    }

    function post(payload) {
        // GM_xmlhttpRequest dodges CORS; fetch would also work since the
        // bridge enables CORS, but this path is more robust across hosts.
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
        if (seenKeys.has(payload.key)) return;
        seenKeys.add(payload.key);
        post(payload);
        console.log(LOG_PREFIX, '->', payload.text || '(icons)',
                    payload.icons.map(i => i.alt).filter(Boolean).join(','));
    }

    function attach(scroller) {
        console.log(LOG_PREFIX, 'attached to log scroller');
        // Seed with whatever's already in view.
        scroller.querySelectorAll(SEL.entry).forEach(processEntry);

        const observer = new MutationObserver((mutations) => {
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
    }

    function waitForScroller() {
        let tries = 0;
        const maxTries = 600;           // ≈5 minutes at 500ms
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
