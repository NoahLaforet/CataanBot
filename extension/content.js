// Content script — runs in isolated world on colonist.io.
//
// Two jobs:
//  1. Bridge messages between the page world (postMessage from
//     inject.js, which is loaded via a separate `world: MAIN`
//     content script entry in manifest.json) and the extension's
//     service worker, so WS frames + DOM-log payloads land at the
//     bridge HTTP endpoints.
//  2. Auto-remove the "Remove Ads" buttons colonist sprinkles
//     throughout its UI.
//
// (inject.js used to be appended dynamically as a <script> tag here,
// but that was async — colonist's bundle could create its WebSocket
// before our patch installed, missing the GameStart frame. Loading
// it via manifest content_scripts with world:"MAIN" runs it
// synchronously at document_start before colonist's app bundle.)
//
// The DOM-log scraper here is a faithful port of the userscript's
// scraper: same class selectors, same structured `parts` payload,
// same dedup logic. The bridge's parser relies on the `parts` array
// being present for trade offers + other multi-token events; a
// flatter payload misses those.

(function bootCataanbotContent() {
    const LOG_PREFIX = '[catanbot]';

    // ---- Streamer mode: anonymize colonist's own DOM -------------
    // Panel.js writes the toggle state to chrome.storage.local;
    // content.js reads it on init + listens for changes. When on,
    // we walk colonist's chat log + player banners and rewrite
    // every player-name text node to a stable generic label
    // ("You" / "Opp 1" / "Opp 2" / ...). React re-renders may
    // overwrite us; the MutationObserver below re-applies on every
    // DOM change so the rewrite usually sticks within a frame.
    let streamerOn = false;
    const STREAMER_DATA_FLAG = 'cataanonymized';
    const _anonMap = new Map();        // real-name → "Opp N"
    let _anonSelfName = null;          // detected from localStorage userState
    let _anonSeq = 0;
    function anonLabelFor(name) {
        if (!name) return name;
        if (_anonSelfName && name === _anonSelfName) return 'You';
        if (!_anonMap.has(name)) {
            _anonSeq += 1;
            _anonMap.set(name, `Opp ${_anonSeq}`);
        }
        return _anonMap.get(name);
    }
    function anonymizeColonistDOM() {
        if (!streamerOn) return;
        // Player-name spans always use inline color: or background-
        // color: — same signature serializeEntry already detects.
        // querySelectorAll for both forms; we'll filter in the loop.
        const candidates = document.querySelectorAll(
            'span[style*="color"], span[style*="background"]');
        for (const el of candidates) {
            // Skip elements that don't directly contain text
            // (player-name spans hold a single text child).
            const txt = (el.innerText || '').trim();
            if (!txt) continue;
            // Skip non-name spans — vp-text, system messages, etc.
            // The dedup attribute also short-circuits already-anon'd
            // elements unless the text changed under us.
            if (el.dataset[STREAMER_DATA_FLAG] === txt) continue;
            // Heuristic: real names are short (< 30 chars), no spaces
            // at the start, look like usernames. Skip multi-word
            // sentences (system messages with inline-styled text).
            if (txt.length > 30 || txt.includes(' ') && txt.length > 16) {
                continue;
            }
            const anon = anonLabelFor(txt);
            if (anon !== txt) {
                el.textContent = anon;
                el.dataset[STREAMER_DATA_FLAG] = anon;
            }
        }
    }
    // Detect self username from colonist's localStorage userState —
    // same source the existing detectSelf() uses below.
    function _refreshSelfName() {
        try {
            const raw = localStorage.getItem('userState');
            if (!raw) return;
            const obj = JSON.parse(raw);
            _anonSelfName = obj && obj.username ? obj.username : null;
        } catch (_) { /* ignore */ }
    }
    // Pull the toggle state from chrome.storage.local + listen for
    // changes. localStorage on the colonist page is a different
    // origin than the panel's; chrome.storage.local is shared
    // across extension contexts.
    try {
        chrome.storage.local.get(['streamer'], (res) => {
            streamerOn = !!(res && res.streamer);
            if (streamerOn) {
                _refreshSelfName();
                anonymizeColonistDOM();
            }
        });
        chrome.storage.onChanged.addListener((changes, area) => {
            if (area !== 'local' || !changes.streamer) return;
            streamerOn = !!changes.streamer.newValue;
            if (streamerOn) {
                _refreshSelfName();
                anonymizeColonistDOM();
            } else {
                // Toggle off: clear the dedup flag so a future
                // toggle-on re-applies. Real names stay where
                // React last wrote them.
                document.querySelectorAll(
                    `[data-${STREAMER_DATA_FLAG.replace(/([A-Z])/g, '-$1').toLowerCase()}]`
                ).forEach(el => delete el.dataset[STREAMER_DATA_FLAG]);
                _anonMap.clear();
                _anonSeq = 0;
            }
        });
    } catch (_) { /* extension context may be invalidated */ }
    // Re-apply on every DOM mutation so React re-renders don't win.
    // Cheap because anonymizeColonistDOM is idempotent on dedup.
    new MutationObserver(() => {
        if (streamerOn) anonymizeColonistDOM();
    }).observe(document.documentElement,
        { childList: true, subtree: true, characterData: true });

    // ---- Relay page-world WS frames to background ----
    // chrome.runtime.sendMessage throws SYNCHRONOUSLY when the
    // extension's context has been invalidated (e.g. after Noah hits
    // "reload" on chrome://extensions while the colonist tab stays
    // open). The promise .catch() doesn't help with a sync throw, so
    // wrap the whole call. Once invalidated, the listener can't
    // recover anyway — set a flag to short-circuit subsequent
    // messages instead of spamming the console.
    let extensionDead = false;
    window.addEventListener('message', (ev) => {
        if (extensionDead) return;
        if (ev.source !== window) return;
        const data = ev.data;
        if (!data || data.source !== 'catanbot-ws') return;
        try {
            chrome.runtime.sendMessage({
                type: 'ws-frame',
                frame: data.frame,
            }).catch(() => {
                // Service worker may have been suspended; drop quietly.
            });
        } catch (err) {
            if (String(err).includes('Extension context invalidated')) {
                extensionDead = true;
                console.warn(LOG_PREFIX,
                    'extension context invalidated — '
                    + 'reload the colonist tab to reconnect');
            }
        }
    });

    // ---- Step 3: DOM /log scraper (full userscript port) ----
    // Selectors captured from DOM recon (docs/colonist_recon.md).
    // Class hashes are fragile across deploys — fall back defensively
    // if the primary selector misses.
    // Class hashes shift on every colonist deploy. The exact
    // selector here is the one that worked at last DOM-recon time;
    // ``ENTRY_SELECTOR_FALLBACK`` is a unioned matcher that tries
    // the prefix form too. Keeping the exact one first helps us
    // notice when colonist redeploys (the prefix path will hit a
    // different element first the next time).
    const SEL = {
        scroller: 'div.virtualScroller-lSkdkGJi',
        entry:    'div.scrollItemContainer-WXX2rkzf',
    };
    const ENTRY_SELECTOR_FALLBACK = (
        SEL.entry
        + ', [class^="scrollItemContainer-"]'
        + ', [class*=" scrollItemContainer-"]');
    const NODE_KEY_ATTR = 'catanbotKey';
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
        if (!el.matches(ENTRY_SELECTOR_FALLBACK)) return;
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

        if (extensionDead) return;
        try {
            chrome.runtime.sendMessage({
                type: 'log-entry',
                payload,
            }).catch(() => {});
        } catch (err) {
            if (String(err).includes('Extension context invalidated')) {
                extensionDead = true;
            }
        }
    }

    function attach(scroller) {
        console.log(LOG_PREFIX, 'attached to log scroller');
        scroller.querySelectorAll(ENTRY_SELECTOR_FALLBACK).forEach(processEntry);

        const observer = new MutationObserver((mutations) => {
            // No isAtBottom gate — when Noah scrolled up to read chat
            // history mid-game, log_events would silently fall to 0
            // because every new entry was filtered out. Per-node
            // dedup via dataset[NODE_KEY_ATTR] + recentSeen handles
            // any double-processing risk; we don't need the scroll
            // guard.
            for (const m of mutations) {
                m.addedNodes.forEach((n) => {
                    if (!(n instanceof Element)) return;
                    if (n.matches(ENTRY_SELECTOR_FALLBACK)) {
                        processEntry(n);
                    } else if (n.querySelectorAll) {
                        n.querySelectorAll(ENTRY_SELECTOR_FALLBACK)
                            .forEach(processEntry);
                    }
                });
            }
        });
        observer.observe(scroller, { childList: true, subtree: true });

        // Safety net: poll every 500ms for any entries the observer
        // missed. MutationObservers can batch rapid insertions
        // (common on colonist's virtualized list) and occasionally
        // skip nodes; the per-node dedup above means re-scanning is
        // cheap and idempotent. Same no-isAtBottom-gate reasoning as
        // the observer above.
        setInterval(() => {
            scroller.querySelectorAll(ENTRY_SELECTOR_FALLBACK).forEach(processEntry);
        }, 500);
    }

    // Find the chat-log scroller. CSS-module class hashes
    // (virtualScroller-lSkdkGJi etc.) change on every colonist
    // deploy — so falling back to a class-prefix match keeps the
    // scraper alive across redeploys. Final fallback: any element
    // that contains an entry that looks like a colonist chat row
    // (an element matching SEL.entry or its prefix variant).
    function findScroller() {
        const exact = document.querySelector(SEL.scroller);
        if (exact) return exact;
        // Prefix match — class hashes shift but the human-readable
        // prefix usually doesn't.
        const prefix = document.querySelector(
            '[class^="virtualScroller-"], [class*=" virtualScroller-"]');
        if (prefix) return prefix;
        // Last resort: walk up from any entry-like row to find the
        // closest scrollable ancestor.
        const entry = document.querySelector(SEL.entry)
            || document.querySelector(
                '[class^="scrollItemContainer-"], '
                + '[class*=" scrollItemContainer-"]');
        if (entry) {
            let cur = entry.parentElement;
            while (cur) {
                const ov = getComputedStyle(cur).overflowY;
                if (ov === 'auto' || ov === 'scroll') return cur;
                cur = cur.parentElement;
            }
        }
        return null;
    }

    function waitForScroller() {
        let tries = 0;
        const maxTries = 600;
        const iv = setInterval(() => {
            tries += 1;
            const scroller = findScroller();
            if (scroller) {
                clearInterval(iv);
                console.info(
                    LOG_PREFIX, 'scroller found',
                    scroller.className.slice(0, 80));
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
