// Content script — runs in isolated world on colonist.io.
//
// Two jobs:
//  1. Inject inject.js as a real <script> so it lives in the page's
//     main world and can patch window.WebSocket where colonist
//     actually uses it. (Content scripts cannot patch page globals
//     from the isolated world.)
//  2. Bridge messages between the page world (postMessage from
//     inject.js) and the extension's service worker (chrome.runtime
//     .sendMessage), so WS frames + DOM-log payloads land at the
//     bridge HTTP endpoints.
//
// The DOM-log scraper lives here because the chat panel is in the
// page DOM, which content scripts can read directly — no need to
// hop through the page world.

(function bootCataanbotContent() {
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

    // ---- Step 3: DOM /log scraper ----
    // Reuses the same selectors and parsing rules the userscript used.
    // Captures new chat-log entries (rolls, builds, trades, dev-card
    // plays, robber moves) and forwards each as a /log POST. Same
    // payload shape: {ts, text, names, icons, raw_html}.
    const LOG_CONTAINER_SELECTORS = [
        '#game-log-text',                        // most stable id
        '[class*="game-log"]',                   // class-hash variants
        '[class*="chat"][class*="messages"]',    // fallback
    ];
    const SEEN_NODES = new WeakSet();

    function findLogContainer() {
        for (const sel of LOG_CONTAINER_SELECTORS) {
            const el = document.querySelector(sel);
            if (el) return el;
        }
        return null;
    }

    function parseEntry(node) {
        // Walk the entry collecting visible text + per-token names
        // (with the colonist user color) + icon alts. Same shape the
        // bridge's parser expects.
        const text = node.textContent.replace(/\s+/g, ' ').trim();
        const names = [];
        const icons = [];
        node.querySelectorAll('[style*="color:"]').forEach(span => {
            // Inline color style on a name span — colonist's pattern.
            const m = span.style && span.style.color;
            const nameText = span.textContent.trim();
            if (nameText && m) {
                names.push({ name: nameText, color: m });
            }
        });
        node.querySelectorAll('img').forEach(img => {
            if (img.alt) icons.push({ alt: img.alt });
        });
        return {
            ts: Date.now() / 1000,
            text, names, icons,
            raw_html: node.outerHTML.slice(0, 4096),
        };
    }

    function emitEntry(node) {
        if (SEEN_NODES.has(node)) return;
        SEEN_NODES.add(node);
        if (!node.textContent || !node.textContent.trim()) return;
        const payload = parseEntry(node);
        chrome.runtime.sendMessage({
            type: 'log-entry',
            payload,
        }).catch(() => {});
    }

    function attachObserver() {
        const container = findLogContainer();
        if (!container) {
            // Lobby/loading state — try again shortly.
            setTimeout(attachObserver, 800);
            return;
        }
        // Snapshot existing entries (in case we attached late).
        Array.from(container.children).forEach(emitEntry);
        const obs = new MutationObserver(records => {
            for (const r of records) {
                r.addedNodes.forEach(n => {
                    if (n.nodeType === 1) emitEntry(n);
                });
            }
        });
        obs.observe(container, { childList: true, subtree: false });
        console.log('[cataanbot] log observer attached');
    }
    attachObserver();

    // ---- Step 4: nuke "Remove Ads" buttons ----
    // Colonist sprinkles "Remove Ads" CTAs in the lobby and in-game
    // panels. Class hashes rotate between deploys, so the only stable
    // identifier is the visible text. Walk any clickable-looking
    // element whose direct text reads exactly "Remove Ads" and yank
    // it out of the DOM. A MutationObserver re-runs the sweep when
    // colonist re-renders the affected panel (turn-end, lobby joins,
    // route changes), so the user never sees one come back.
    //
    // We only consider tight scopes (button / a / role=button / a
    // small whitelist of likely classes) to avoid removing a parent
    // container that incidentally has "Remove Ads" in deep child text.
    function nukeRemoveAdsButtons() {
        const candidates = document.querySelectorAll(
            'button, a, [role="button"]');
        for (const el of candidates) {
            const text = (el.textContent || '').trim().toLowerCase();
            if (text === 'remove ads' || text === 'remove ad'
                || text === 'remove ads now' || text === 'remove all ads') {
                // Pull the button itself rather than a parent — leaves
                // any sibling content intact. If colonist's layout
                // collapses oddly without the button, we can switch to
                // visibility:hidden later, but in practice the host
                // element's flex layout reflows fine.
                try { el.remove(); } catch (e) {}
            }
        }
    }
    nukeRemoveAdsButtons();
    // Re-sweep on every body mutation. Debounced so a flurry of
    // mutations during a route change doesn't run the QSA dozens of
    // times per frame; one trailing call per ~150ms window is enough.
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
})();
