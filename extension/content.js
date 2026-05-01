// Content script — runs in isolated world on colonist.io.
//
// Three jobs:
//  1. Inject inject.js as a real <script> so it lives in the page's
//     main world and can patch window.WebSocket where colonist
//     actually uses it. (Content scripts cannot patch page globals
//     from the isolated world.)
//  2. Bridge messages between the page world (postMessage from
//     inject.js) and the extension's service worker (chrome.runtime
//     .sendMessage), so WS frames + DOM-log payloads land at the
//     bridge HTTP endpoints.
//  3. Auto-remove the "Remove Ads" buttons colonist sprinkles
//     throughout its UI.
//
// The DOM-log scraper here is a faithful port of the userscript's
// scraper: same class selectors, same structured `parts` payload,
// same dedup logic. The bridge's parser relies on the `parts` array
// being present for trade offers + other multi-token events; a
// flatter payload misses those.

(function bootCataanbotContent() {
    const LOG_PREFIX = '[cataanbot]';

    // ---- Step 1: inject inject.js into the main world ----
    const url = chrome.runtime.getURL('inject.js');
    const s = document.createElement('script');
    s.src = url;
    s.onload = () => s.remove();
    (document.head || document.documentElement).appendChild(s);

    // ---- Step 2: relay page-world WS frames to background ----
    window.addEventListener('message', (ev) => {
        if (ev.source !== window) return;
        const data = ev.data;
        if (!data || data.source !== 'cataanbot-ws') return;
        chrome.runtime.sendMessage({
            type: 'ws-frame',
            frame: data.frame,
        }).catch(() => {
            // Service worker may have been suspended; drop quietly.
        });
    });

    // ---- Step 3: DOM /log scraper (full userscript port) ----
    // Selectors captured from DOM recon (docs/colonist_recon.md).
    // Class hashes are fragile across deploys — fall back defensively
    // if the primary selector misses.
    const SEL = {
        scroller: 'div.virtualScroller-lSkdkGJi',
        entry:    'div.scrollItemContainer-WXX2rkzf',
    };
    const NODE_KEY_ATTR = 'cataanbotKey';
    const RECENT_TTL_MS = 60000;
    const AT_BOTTOM_PX = 50;
    const recentSeen = new Map();

    function isAtBottom(scroller) {
        return (scroller.scrollHeight - scroller.scrollTop
                - scroller.clientHeight) < AT_BOTTOM_PX;
    }

    // Walk the whole scrollItemContainer in document order, emitting
    // ordered parts. We can't just walk messagePart because some
    // events (dev-card play "X used [Knight]") render the card icon
    // as a sibling of messagePart, not a child. Avatars have alt=""
    // and are dropped by the icon rule below.
    function serializeEntry(el) {
        const root = el;
        const parts = [];

        const walk = (node) => {
            if (node.nodeType === Node.TEXT_NODE) {
                const t = (node.textContent || '')
                    .replace(/\s+/g, ' ').trim();
                if (t) parts.push({ kind: 'text', text: t });
                return;
            }
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            const elNode = node;
            if (elNode.tagName === 'IMG') {
                const alt = elNode.alt || '';
                if (!alt) return; // drop avatar
                parts.push({
                    kind: 'icon',
                    alt,
                    src_tail: (elNode.getAttribute('src') || '')
                        .split('/').pop(),
                });
                return;
            }
            // Player name pill: colored span. Inline color: is the
            // happy path, but fall back to computed style if needed.
            const style = elNode.getAttribute
                && elNode.getAttribute('style') || '';
            const hasInlineColor = /(^|[^-])color\s*:/i.test(style);
            const hasInlineBg = /background(-color)?\s*:/i.test(style);
            if (elNode.tagName === 'SPAN'
                    && (hasInlineColor || hasInlineBg)) {
                const name = (elNode.innerText || '').trim();
                if (name) {
                    let color = elNode.style.color || '';
                    if (!color) {
                        try {
                            color = window.getComputedStyle(elNode).color || '';
                        } catch (_) {}
                    }
                    let bg = elNode.style.backgroundColor || '';
                    if (!bg && hasInlineBg) {
                        try {
                            bg = window.getComputedStyle(elNode)
                                .backgroundColor || '';
                        } catch (_) {}
                    }
                    parts.push({ kind: 'name', name, color, bg });
                }
                return;
            }
            // VP callout: <span class="vp-text">+1 VP</span>
            if (elNode.classList
                    && elNode.classList.contains('vp-text')) {
                parts.push({ kind: 'vp',
                    text: (elNode.innerText || '').trim() });
                return;
            }
            // Recurse into generic containers.
            for (const child of elNode.childNodes) walk(child);
        };

        for (const child of root.childNodes) walk(child);

        const text = parts
            .filter(p => p.kind === 'text'
                || p.kind === 'name' || p.kind === 'vp')
            .map(p => p.kind === 'name' ? p.name : p.text)
            .join(' ')
            .replace(/\s+/g, ' ')
            .trim();
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
            key: `${text}|${icons.map(i => i.alt).join(',')}`
                + `|${names.map(n => n.name).join(',')}`,
        };
    }

    // Active user from localStorage.userState — colonist stores the
    // logged-in user there. Much more reliable than DOM scraping
    // (the "(You)" marker only shows in the lobby).
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
        } catch (_) {}
        return null;
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

        chrome.runtime.sendMessage({
            type: 'log-entry',
            payload,
        }).catch(() => {});
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
                    } else if (n.querySelectorAll) {
                        n.querySelectorAll(SEL.entry).forEach(processEntry);
                    }
                });
            }
        });
        observer.observe(scroller, { childList: true, subtree: true });

        // Safety net: poll every 500ms for any entries the observer
        // missed. MutationObservers can batch rapid insertions
        // (common on colonist's virtualized list) and occasionally
        // skip nodes; the per-node dedup above means re-scanning is
        // cheap and idempotent.
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
                console.warn(LOG_PREFIX, 'gave up waiting for scroller');
            }
        }, 500);
    }
    waitForScroller();

    // ---- Step 4: nuke "Remove Ads" buttons ----
    function nukeRemoveAdsButtons() {
        const candidates = document.querySelectorAll(
            'button, a, [role="button"]');
        for (const el of candidates) {
            const text = (el.textContent || '').trim().toLowerCase();
            if (text === 'remove ads' || text === 'remove ad'
                || text === 'remove ads now'
                || text === 'remove all ads') {
                try { el.remove(); } catch (e) {}
            }
        }
    }
    nukeRemoveAdsButtons();
    let _adSweepTimer = null;
    const adObserver = new MutationObserver(() => {
        if (_adSweepTimer) return;
        _adSweepTimer = setTimeout(() => {
            _adSweepTimer = null;
            nukeRemoveAdsButtons();
        }, 150);
    });
    function attachAdObserver() {
        if (!document.body) {
            setTimeout(attachAdObserver, 100);
            return;
        }
        adObserver.observe(document.body,
            { childList: true, subtree: true });
    }
    attachAdObserver();

    console.log(LOG_PREFIX, 'content script loaded');
})();
