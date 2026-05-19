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

(function bootCatanbotContent() {
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

    // Inject a hard-blank CSS rule so player-colored spans render
    // transparent UNTIL our anonymizer marks them with the dedup
    // attribute. Kills the first-frame flash where colonist's
    // React paints "Morgan" before our MutationObserver fires.
    // Scoped to html[data-cataan-streamer="1"] so it's a no-op
    // when streamer mode is off.
    function _ensureStreamerCss() {
        const id = 'cataan-streamer-css';
        if (document.getElementById(id)) return;
        const style = document.createElement('style');
        style.id = id;
        style.textContent = `
            html[data-cataan-streamer="1"]
                span[style*="color"]:not([data-${STREAMER_DATA_FLAG}]),
            html[data-cataan-streamer="1"]
                div[style*="color"]:not([data-${STREAMER_DATA_FLAG}]) {
                color: transparent !important;
                text-shadow: none !important;
            }
        `;
        (document.head || document.documentElement)
            .appendChild(style);
    }
    _ensureStreamerCss();
    let _anonSelfName = null;
    // Stable seat-order labels. First opp we see gets the first
    // fantasy name, second gets the second, etc. Self is always
    // "You" regardless of seat. Colors were too fragile across
    // colonist's color-shuffling so we dropped that approach;
    // these names are obviously fake to a viewer but readable.
    const _FANTASY_NAMES = [
        'Aria', 'Bran', 'Cyrus', 'Dara',
        'Elin', 'Fynn', 'Gaia', 'Hugo',
    ];
    const _name_to_anon = new Map();   // real → assigned label
    let _anonSeq = 0;
    // Debounced sync of _name_to_anon to the bridge so the side panel
    // pills agree with what colonist's chat / banners show. Without
    // this the panel maintains its own counter and diverges (panel
    // showed Elin/Dara/Fynn while chat showed Aria/Bran/Cyrus on
    // 2026-05-04 — same set of usernames, different counter state).
    let _anonSyncPending = false;
    function _scheduleAnonSync() {
        if (_anonSyncPending) return;
        _anonSyncPending = true;
        setTimeout(() => {
            _anonSyncPending = false;
            const names = {};
            for (const [k, v] of _name_to_anon.entries()) {
                names[k] = v;
            }
            try {
                chrome.runtime.sendMessage({
                    type: 'streamer-anon',
                    payload: { self: _anonSelfName, names },
                }).catch(() => {});
            } catch (_) {
                // Extension context may be invalidated (post-reload);
                // already handled by the ws-frame relay's flag.
            }
        }, 50);
    }
    function anonLabelFor(name) {
        if (!name) return name;
        if (_anonSelfName && name === _anonSelfName) return 'You';
        if (!_name_to_anon.has(name)) {
            const slot = _FANTASY_NAMES[_anonSeq % _FANTASY_NAMES.length];
            const ordinal = Math.floor(_anonSeq / _FANTASY_NAMES.length);
            _name_to_anon.set(name,
                ordinal === 0 ? slot : `${slot} ${ordinal + 1}`);
            _anonSeq += 1;
            _scheduleAnonSync();
        }
        return _name_to_anon.get(name);
    }
    function _looksLikeUsername(txt) {
        if (!txt) return false;
        if (txt.length > 30) return false;
        if (txt.length < 2) return false;
        // Multi-word sentences are rarely usernames.
        if (txt.includes(' ') && txt.length > 16) return false;
        return true;
    }
    function _isInputLike(el) {
        // Don't rewrite the user's compose box. Tighter check than
        // before — only flag the actual input/textarea and its
        // immediate wrapper, not anything 4 levels up. The wider
        // walk was over-skipping chat-message usernames whose
        // ancestor tree happened to include a sibling input.
        if (!el) return false;
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
            return true;
        }
        if (el.getAttribute
                && el.getAttribute('contenteditable') === 'true') {
            return true;
        }
        const p = el.parentElement;
        if (!p) return false;
        if (p.tagName === 'INPUT' || p.tagName === 'TEXTAREA') {
            return true;
        }
        if (p.getAttribute
                && p.getAttribute('contenteditable') === 'true') {
            return true;
        }
        return false;
    }
    function _rewriteUsername(el, txt) {
        if (_isInputLike(el)) return;
        if (el.dataset[STREAMER_DATA_FLAG] === txt) return;
        const anon = anonLabelFor(txt);
        // Even when no rewrite is needed (anon === txt), mark the
        // span as anonymized so the streamer-mode CSS lets it
        // render. Without this the CSS keeps inline-colored spans
        // transparent forever — Noah hit this on self chat rows
        // where colonist already renders "You" with inline color
        // and our anonymizer would short-circuit, leaving the
        // span permanently blanked. (Bug fix 2026-05-04 after
        // capturing the literal HTML: colonist's own "You" text
        // was getting hidden by our universal blank rule.)
        if (anon === txt) {
            el.dataset[STREAMER_DATA_FLAG] = txt;
            return;
        }
        // Trailing space prevents "Aria placed" from collapsing into
        // "Ariaplaced" when the next sibling text node starts
        // directly. Cheap and harmless when there's already a
        // separator.
        el.textContent = anon + ' ';
        el.dataset[STREAMER_DATA_FLAG] = anon;
        // Preserve the real username so the chat serializer can ship
        // it to the bridge under the same key the panel uses for
        // display_colors lookup. Without this, serializeEntry reads
        // the post-rewrite "Aria" / "Bran" / ... and the bridge
        // stores colors under the anon name while the panel queries
        // by the real catanatron-side username — perpetual miss.
        el.dataset.cataanonReal = txt;
    }
    function anonymizeColonistDOM() {
        if (!streamerOn) return;
        // Pass 0 — self chat rows. Colonist renders self's name
        // as a literal "You" inside a multi-child span (text
        // "You" + nested ":" sub-span):
        //   <span class="username-..." style="color:#3d3d3d">
        //     You<span class="colon-...">: </span>
        //   </span>
        // Pass 1 below skips multi-child spans, so these never
        // get marked anonymized and the streamer blank CSS leaves
        // them permanently transparent. Walk username-classed
        // spans whose FIRST text node is "You" and mark them
        // dedup'd. Restricted to "You" so opponent username spans
        // (which need real rewriting) still flow through pass 1
        // / pass 3 unchanged.
        // Bug fix 2026-05-04 — captured the literal HTML when
        // self-chat rendered as ": hi" with no name.
        const usernameSpans = document.querySelectorAll(
            '[class*="username-"]');
        for (const el of usernameSpans) {
            if (el.dataset[STREAMER_DATA_FLAG]) continue;
            // First text node, trimmed.
            let firstText = '';
            for (const ch of el.childNodes) {
                if (ch.nodeType === Node.TEXT_NODE) {
                    firstText = (ch.textContent || '').trim();
                    if (firstText) break;
                }
            }
            if (firstText === 'You') {
                el.dataset[STREAMER_DATA_FLAG] = 'You';
            }
        }
        // Pass 1 — chat / pill spans with their own inline color
        // signature. Discovers usernames as we go, so subsequent
        // banner sweeps can match them.
        const colored = document.querySelectorAll(
            'span[style*="color"], span[style*="background"], '
            + 'div[style*="color"], div[style*="background"]');
        for (const el of colored) {
            if (el.children.length > 0) continue;
            const txt = (el.innerText || '').trim();
            if (!_looksLikeUsername(txt)) continue;
            _rewriteUsername(el, txt);
        }
        // Pass 2 — banner / aside / any plain element whose text is
        // a username we've already remembered. Banner rows often
        // hold the colored backdrop on a parent and the username
        // text on a plain inner div, so pass 1 misses them.
        if (_name_to_anon.size === 0 && !_anonSelfName) return;
        const all = document.querySelectorAll(
            'div, span, p, a, button, label, h1, h2, h3, h4');
        for (const el of all) {
            if (el.children.length > 0) continue;
            const txt = (el.innerText || '').trim();
            if (!_looksLikeUsername(txt)) continue;
            if (!_name_to_anon.has(txt) && _anonSelfName !== txt) {
                continue;
            }
            _rewriteUsername(el, txt);
        }
        // Pass 3 — text-node walk. Catches usernames embedded in
        // arbitrary text content like "Roehm's Turn", "BrickdDaddy:
        // hi", or any other live string colonist composes from
        // template + username. Pass 2 only matches elements whose
        // ENTIRE text is a username; pass 3 matches anywhere a
        // known name appears with word boundaries.
        _rewriteTextNodes();
    }
    function _rewriteTextNodes() {
        const knownNames = [..._name_to_anon.keys()];
        if (_anonSelfName && !knownNames.includes(_anonSelfName)) {
            knownNames.push(_anonSelfName);
        }
        if (knownNames.length === 0) return;
        knownNames.sort((a, b) => b.length - a.length);
        const escaped = knownNames.map(n =>
            n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
        const re = new RegExp('\\b(' + escaped.join('|') + ')\\b', 'g');
        const filter = {
            acceptNode: (node) => {
                if (!node.textContent
                        || node.textContent.length < 2) {
                    return NodeFilter.FILTER_REJECT;
                }
                const parent = node.parentElement;
                if (!parent) return NodeFilter.FILTER_REJECT;
                if (_isInputLike(parent)) {
                    return NodeFilter.FILTER_REJECT;
                }
                const tag = parent.tagName;
                if (tag === 'SCRIPT' || tag === 'STYLE'
                        || tag === 'NOSCRIPT') {
                    return NodeFilter.FILTER_REJECT;
                }
                return NodeFilter.FILTER_ACCEPT;
            },
        };
        const updates = [];
        function walkRoot(root) {
            // Walk text nodes in this root.
            const walker = document.createTreeWalker(
                root, NodeFilter.SHOW_TEXT, filter);
            let node;
            while ((node = walker.nextNode())) {
                if (!re.test(node.textContent)) continue;
                re.lastIndex = 0;
                updates.push(node);
            }
            // Recurse into open shadow roots — colonist's modals
            // (robber-victim picker, dev-card popups, trade
            // dialogs) sometimes mount via React portal into a
            // Shadow DOM where the main-document walker is blind.
            // Closed shadow roots stay opaque, but those are rare
            // in colonist's bundle.
            const elementWalker = document.createTreeWalker(
                root, NodeFilter.SHOW_ELEMENT, null);
            let el;
            while ((el = elementWalker.nextNode())) {
                if (el.shadowRoot) {
                    try { walkRoot(el.shadowRoot); }
                    catch (_) { /* closed root or perm denied */ }
                }
            }
        }
        walkRoot(document.body);
        for (const node of updates) {
            re.lastIndex = 0;
            const newText = node.textContent.replace(re,
                (_, name) => anonLabelFor(name));
            if (newText !== node.textContent) {
                node.textContent = newText;
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
            const next = obj && obj.username ? obj.username : null;
            if (next !== _anonSelfName) {
                _anonSelfName = next;
                _scheduleAnonSync();
            }
        } catch (_) { /* ignore */ }
    }
    // Pull the toggle state from chrome.storage.local + listen for
    // changes. localStorage on the colonist page is a different
    // origin than the panel's; chrome.storage.local is shared
    // across extension contexts.
    function _burstAnon() {
        // First pass right away, then a few delayed re-passes to
        // catch elements that hadn't rendered yet (banners
        // sometimes lazy-mount, chat history sometimes paginates).
        // The 0/100/300/700ms cadence covers ~95% of late-render
        // cases without flooding.
        if (!streamerOn) return;
        anonymizeColonistDOM();
        for (const ms of [100, 300, 700, 1500]) {
            setTimeout(() => {
                if (streamerOn) anonymizeColonistDOM();
            }, ms);
        }
    }
    function _setStreamerHtmlFlag(on) {
        document.documentElement.dataset.cataanStreamer = on ? '1' : '0';
    }
    try {
        chrome.storage.local.get(['streamer'], (res) => {
            streamerOn = !!(res && res.streamer);
            _setStreamerHtmlFlag(streamerOn);
            if (streamerOn) {
                _refreshSelfName();
                _burstAnon();
            }
        });
        chrome.storage.onChanged.addListener((changes, area) => {
            if (area !== 'local' || !changes.streamer) return;
            streamerOn = !!changes.streamer.newValue;
            _setStreamerHtmlFlag(streamerOn);
            if (streamerOn) {
                _refreshSelfName();
                _burstAnon();
            } else {
                // Toggle off: clear the dedup flag so a future
                // toggle-on re-applies. Real names stay where
                // React last wrote them.
                document.querySelectorAll(
                    `[data-${STREAMER_DATA_FLAG.replace(/([A-Z])/g, '-$1').toLowerCase()}]`
                ).forEach(el => delete el.dataset[STREAMER_DATA_FLAG]);
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
                // Prefer the pre-anon real username when the
                // anonymizer rewrote this element. Bridge stores
                // colors keyed by real username (matches what panel
                // queries); shipping the anon would silently break
                // the lookup in streamer mode.
                const name = (
                    elNode.dataset && elNode.dataset.cataanonReal)
                    || (elNode.innerText || '').trim();
                if (name) {
                    // Streamer-mode hard-blank rule overrides colonist's
                    // inline color with `color: transparent !important`,
                    // so getComputedStyle returns rgba(0,0,0,0) for any
                    // un-anonymized span. Filter that out — otherwise
                    // we latch "transparent" as the player's color and
                    // the panel pill renders white. Inline style.color
                    // still reads the original author value, so it's
                    // safe; only the computed-style fallback is poison.
                    const isTransparent = (s) => {
                        if (!s) return true;
                        const t = String(s).trim().toLowerCase();
                        if (!t) return true;
                        if (t === 'transparent') return true;
                        const m = t.match(
                            /rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*(?:,\s*([\d.]+)\s*)?\)/);
                        if (m && m[1] !== undefined
                                && parseFloat(m[1]) === 0) return true;
                        return false;
                    };
                    let color = elNode.style.color || '';
                    if (!color) {
                        try {
                            color = window.getComputedStyle(elNode).color || '';
                        } catch (_) {}
                    }
                    if (isTransparent(color)) color = '';
                    let bg = elNode.style.backgroundColor || '';
                    if (!bg && hasInlineBg) {
                        try {
                            bg = window.getComputedStyle(elNode)
                                .backgroundColor || '';
                        } catch (_) {}
                    }
                    if (isTransparent(bg)) bg = '';
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
            .map(p => ({ name: p.name, color: p.color, bg: p.bg }));
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
