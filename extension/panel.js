// CataanBot side-panel render. Ported from userscript v0.25.4 with
// the WebSocket hook and DOM-log scraper removed (those live in
// inject.js / content.js inside the extension). Adapted to:
//
//  - Use document directly instead of a shadow root (we own the
//    side-panel page).
//  - Use plain fetch() instead of GM_xmlhttpRequest (extension has
//    host_permissions for localhost).
//  - Treat 'host' as document.body and 'root' as document, so all
//    the userscript's mountOverlay machinery still finds its nodes.
//  - Skip the WS interceptor + DOM-log scraper sections (lines
//    4272+ of the userscript) — those have moved to inject.js +
//    content.js so the page can hook colonist's WebSocket from the
//    main world.
//
// Everything else — banners, recommendations, robber targets, knight
// hint, monopoly hint, YoP / RB hints, strategic options, win-this-
// turn, plan banner, dev-deck, bank supply, eval sparkline, roll
// histogram, move-quality strip — is the same code that ships in the
// userscript. The point: feature parity in the side panel.

(() => {
    'use strict';


    const BRIDGE_URL = 'http://127.0.0.1:8765/log';
    const BRIDGE_WS_URL = 'http://127.0.0.1:8765/ws';
    const BRIDGE_ADVISOR_URL = 'http://127.0.0.1:8765/advisor';
    const BRIDGE_FEEDBACK_URL = 'http://127.0.0.1:8765/feedback';
    // 500ms poll: bridge bumps `seq` on every WS frame from colonist, so a
    // shorter interval directly halves the worst-case lag between a roll
    // landing in the game and the HUD reflecting it. The advisor endpoint
    // is a cheap dict serialization; doubling the rate is a non-issue.
    const ADVISOR_POLL_MS = 500;
    const LOG_PREFIX = '[cataanbot]';

    // Push-style refresh hook. Set by ``startAdvisorPoll`` to a function
    // that schedules a near-immediate /advisor poll. The /ws forwarder
    // calls this after each POST so the HUD updates within ~30ms of a
    // state change (roll, build, trade) instead of waiting up to
    // ADVISOR_POLL_MS for the next periodic tick. Without this, Noah
    // sees the colonist UI react before the HUD does — feels laggy
    // mid-turn ("HUD says I have 1 brick but I just got another").
    let triggerAdvisorRefresh = () => {};

    // Latest /advisor snapshot — updated by the polling loop, read by
    // event handlers that need state context (e.g. feedback chips
    // attaching a "what was happening when I clicked this" hint).
    // Module-scope so closures across mountOverlay + startAdvisorPoll
    // see the same value.
    let latestAdvisorSnap = null;

    // Fire-and-forget POST. Used by both the DOM log forwarder (/log)
    // and the WS frame forwarder (/ws). Keeps the userscript quiet even
    // if the bridge is down so a game session isn't noisy. Optionally
    // chains a callback after the POST completes — used by the /ws
    // pipe to trigger an /advisor refresh once the bridge has actually
    // ingested the frame.
    function postTo(url, payload, { quiet, after } = {}) {
        if (false /* extension uses fetch directly */) {
            GM_xmlhttpRequest({
                method: 'POST',
                url,
                headers: { 'Content-Type': 'application/json' },
                data: JSON.stringify(payload),
                onload: () => { if (after) try { after(); } catch (e) {} },
                onerror: (e) => { if (!quiet)
                    console.warn(LOG_PREFIX, 'POST failed', e); },
            });
        } else {
            fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                mode: 'cors',
            }).then(() => { if (after) try { after(); } catch (e) {} })
              .catch(e => { if (!quiet)
                  console.warn(LOG_PREFIX, 'fetch failed', e); });
        }
    }

    // Advisor overlay — small draggable panel in the top-right showing
    // the self hand, what's affordable, opponent card counts, and (while
    // you owe a robber move after rolling a 7) the top-N target ranking.
    // Polls GET /advisor once a second; diffs on seq so re-renders are
    // cheap. The panel survives a page nav because document-start mounts
    // it as soon as <body> exists.
    //
    // Intentionally framework-free DOM: no React/Vue, just <div> + inline
    // styles in a shadow root. The shadow isolates us from colonist's
    // stylesheets (which aggressively style class-less descendants) while
    // letting us keep the mount point inside <body> so the whole page
    // isn't frozen under a fixed-overlay host.
    function getJson(url) {
        return new Promise((resolve, reject) => {
            if (false /* extension uses fetch directly */) {
                GM_xmlhttpRequest({
                    method: 'GET', url,
                    onload: (r) => {
                        try { resolve(JSON.parse(r.responseText)); }
                        catch (e) { reject(e); }
                    },
                    onerror: (e) => reject(e),
                });
            } else {
                fetch(url, { mode: 'cors' })
                    .then(r => r.json()).then(resolve, reject);
            }
        });
    }

    // POST that returns a Promise resolving to the parsed JSON
    // response — the synchronous postTo() above is fire-and-forget.
    // Used by handlers that need to round-trip a confirmation
    // (e.g., /config write-back).
    function postJson(url, payload) {
        return new Promise((resolve, reject) => {
            if (false /* extension uses fetch directly */) {
                GM_xmlhttpRequest({
                    method: 'POST', url,
                    headers: { 'Content-Type': 'application/json' },
                    data: JSON.stringify(payload),
                    onload: (r) => {
                        try { resolve(JSON.parse(r.responseText)); }
                        catch (e) { reject(e); }
                    },
                    onerror: (e) => reject(e),
                });
            } else {
                fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                    mode: 'cors',
                }).then(r => r.json()).then(resolve, reject);
            }
        });
    }

    // Fallback pill colors for the catanatron 4-enum. Used only when
    // the bridge hasn't yet harvested a CSS color for the player from
    // the chat log — which happens for the first few WS frames of a
    // game before the first log line shows up.
    const COLOR_HEX = {
        RED: '#e8715f', BLUE: '#4aa7d4', ORANGE: '#e29a4a',
        WHITE: '#f0f0f0', GREEN: '#7ac74f', BROWN: '#a07045',
    };
    const RES_ABBREV = {
        WOOD: 'Wd', BRICK: 'Br', SHEEP: 'Sh', WHEAT: 'Wh', ORE: 'Or',
    };
    // Inline SVG resource icons — single-stroke line art so they
    // inherit currentColor + sit cleanly inline with text. Authored
    // by hand at 14×14 viewBox so each glyph reads at HUD body size
    // without rendering noise. Replaces the 🌲🧱🐑🌾⛰️ emoji set
    // (which clashed with the cleaner panel aesthetic and rendered
    // inconsistently across OS font stacks).
    //
    // The matching CSS in panel.css gives each .res-glyph a 1em
    // inline size + colors per resource (wood=brown, brick=red,
    // sheep=light, wheat=amber, ore=slate).
    const RES_SVG = {
        WOOD: '<svg class="res-glyph res-wood" viewBox="0 0 24 24" '
            + 'fill="none" stroke="currentColor" stroke-width="1.6" '
            + 'stroke-linejoin="round" aria-hidden="true">'
            + '<path d="M12 3 L7 11 L9.5 11 L5 18 L9 18 L11 22 '
            + 'L13 22 L15 18 L19 18 L14.5 11 L17 11 Z"/></svg>',
        BRICK: '<svg class="res-glyph res-brick" viewBox="0 0 24 24" '
            + 'fill="none" stroke="currentColor" stroke-width="1.6" '
            + 'stroke-linejoin="round" aria-hidden="true">'
            + '<rect x="3" y="6" width="8" height="5"/>'
            + '<rect x="13" y="6" width="8" height="5"/>'
            + '<rect x="3" y="12" width="18" height="5"/>'
            + '</svg>',
        SHEEP: '<svg class="res-glyph res-sheep" viewBox="0 0 24 24" '
            + 'fill="none" stroke="currentColor" stroke-width="1.6" '
            + 'stroke-linejoin="round" aria-hidden="true">'
            + '<circle cx="6" cy="13" r="3"/>'
            + '<circle cx="11" cy="11" r="3.5"/>'
            + '<circle cx="15" cy="11" r="3.5"/>'
            + '<circle cx="19" cy="13" r="3"/>'
            + '<line x1="9" y1="18" x2="9" y2="20"/>'
            + '<line x1="14" y1="18" x2="14" y2="20"/>'
            + '</svg>',
        WHEAT: '<svg class="res-glyph res-wheat" viewBox="0 0 24 24" '
            + 'fill="none" stroke="currentColor" stroke-width="1.6" '
            + 'stroke-linecap="round" aria-hidden="true">'
            + '<line x1="12" y1="4" x2="12" y2="22"/>'
            + '<path d="M12 8 L8 10 L12 12"/>'
            + '<path d="M12 8 L16 10 L12 12"/>'
            + '<path d="M12 13 L8 15 L12 17"/>'
            + '<path d="M12 13 L16 15 L12 17"/>'
            + '</svg>',
        ORE: '<svg class="res-glyph res-ore" viewBox="0 0 24 24" '
            + 'fill="none" stroke="currentColor" stroke-width="1.6" '
            + 'stroke-linejoin="round" aria-hidden="true">'
            + '<path d="M6 9 L12 3 L18 9 L15 21 L9 21 Z"/>'
            + '<line x1="6" y1="9" x2="18" y2="9"/>'
            + '<line x1="12" y1="3" x2="9" y2="21"/>'
            + '<line x1="12" y1="3" x2="15" y2="21"/>'
            + '</svg>',
    };
    // Per-resource emoji map. v0.27.0 swapped these for inline SVG
    // glyphs but Noah preferred emojis on the default HUD — they
    // read at a glance and don't have the line-art "diagram" feel.
    // The non-default styles (terminal/newspaper/HUD/minimal) use
    // their own renderers and pick the right glyph for their look.
    const RES_EMOJI = {
        WOOD: '🌲', BRICK: '🧱', SHEEP: '🐑',
        WHEAT: '🌾', ORE: '⛰️',
    };
    const iconFor = (res) => RES_EMOJI[res]
        || RES_SVG[res]
        || `<span class="res-glyph res-fallback">`
            + (RES_ABBREV[res] || (res || '?').slice(0, 2)) + '</span>';

    // Pick the best available pill color. Prefer the CSS color the
    // chat-pill shipped (true colonist UI color, including premium
    // unlocks like black), fall back to the catanatron enum mapping.
    function pillColor(player) {
        if (player && player.color_css) return player.color_css;
        if (player && player.color) return COLOR_HEX[player.color] || '#888';
        return '#888';
    }

    // Return readable text color (black or white) for a given bg.
    function contrastText(css) {
        // Best-effort parse. RGB/RGBA or hex.
        const c = String(css || '').trim();
        let r, g, b;
        let m = c.match(/^#([0-9a-f]{3})$/i);
        if (m) {
            r = parseInt(m[1][0] + m[1][0], 16);
            g = parseInt(m[1][1] + m[1][1], 16);
            b = parseInt(m[1][2] + m[1][2], 16);
        } else {
            m = c.match(/^#([0-9a-f]{6})$/i);
            if (m) {
                r = parseInt(m[1].slice(0, 2), 16);
                g = parseInt(m[1].slice(2, 4), 16);
                b = parseInt(m[1].slice(4, 6), 16);
            } else {
                m = c.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
                if (m) {
                    r = +m[1]; g = +m[2]; b = +m[3];
                } else {
                    return '#111';
                }
            }
        }
        // Perceived brightness; threshold picked so mid-blue → white text.
        const luma = 0.299 * r + 0.587 * g + 0.114 * b;
        return luma > 140 ? '#111' : '#fff';
    }

    let _mountedOnce = false;
    function mountOverlay() {
        if (!document.body) return null;
        let host = document.body;
        if (_mountedOnce) {
            // Subsequent calls: just return the cached ref bag. All
            // listeners + first-time setup ran on the first call.
            return {
                host,
                panel: document.getElementById('panel'),
                body: document.getElementById('body'),
                content: document.getElementById('content'),
                dot: document.getElementById('dot'),
                histHost: document.getElementById('hist-host'),
                hist: document.getElementById('hist'),
                histTotal: document.getElementById('hist-total'),
            };
        }
        _mountedOnce = true;
        // Extension panel: DOM is already in panel.html.
        // Skip the userscript-style shadow-root build —
        // just expose the existing nodes via the same handle.
        host = document.body;
        const root = document;

        const panel = document.getElementById('panel');
        const body = document.getElementById('body');
        const content = document.getElementById('content');
        const header = document.getElementById('header');
        const dot = document.getElementById('dot');
        document.getElementById('toggle').addEventListener('click', (e) => {
            e.stopPropagation();
            body.classList.toggle('collapsed');
        });

        // ---------- Pop-out into a separate browser window ----------
        // Uses the Document Picture-in-Picture API (Chrome 116+). The
        // HUD covers a real chunk of the colonist board when docked,
        // and a PiP window lets the user move it onto a second display
        // or off to the side. We move the host element (and its shadow
        // root) wholesale into the PiP document — no re-render, no
        // duplicate state — so all the existing JS that drives the
        // panel keeps working without modification.
        //
        // When the PiP window closes (user X's it, or browser tab
        // navigates away), we move the host back into the colonist
        // page so the HUD survives. If the API isn't available
        // (Firefox, Safari, older Chrome) we tell the user instead of
        // failing silently.
        const popoutBtn = document.getElementById('popout');
        let pipWindowRef = null;
        async function popOut() {
            if (pipWindowRef && !pipWindowRef.closed) {
                pipWindowRef.focus();
                return;
            }
            const w = window || window;
            if (!w.documentPictureInPicture
                || !w.documentPictureInPicture.requestWindow) {
                console.warn('[cataanbot] documentPictureInPicture not '
                    + 'available — pop-out needs Chrome 116+ or Edge.');
                popoutBtn.title = 'Pop-out unavailable in this browser';
                popoutBtn.disabled = true;
                return;
            }
            try {
                const pip = await w.documentPictureInPicture.requestWindow({
                    width: 660,
                    height: 920,
                });
                pipWindowRef = pip;
                // Mirror the font preconnect into the PiP document so
                // JetBrains Mono / Inter render the same way they do
                // in the original tab.
                const fontLink = pip.document.createElement('link');
                fontLink.rel = 'stylesheet';
                fontLink.href = 'https://fonts.googleapis.com/css2'
                    + '?family=Inter:wght@400;500;600;700;800;900'
                    + '&family=JetBrains+Mono:wght@400;500;600;700'
                    + '&display=swap';
                pip.document.head.appendChild(fontLink);
                // Reset the PiP body — Chrome ships with default
                // margin/padding that cuts our 660px width otherwise.
                pip.document.body.style.cssText = 'margin:0;padding:0;'
                    + 'background:#0a0d14;height:100vh;overflow:auto;';
                // Move the host (which carries the shadow root + all
                // panel state) into the PiP document. We hold a sentinel
                // span so we can put it back when the PiP closes.
                const placeholder = document.createElement('span');
                placeholder.id = 'cataanbot-popout-placeholder';
                host.parentNode.insertBefore(placeholder, host);
                // Strip the fixed-positioning so the panel fills the
                // PiP window naturally; cache the original style to
                // restore on close.
                const originalCss = host.style.cssText;
                host.style.cssText = 'position:static;'
                    + 'width:100%;display:block;';
                pip.document.body.appendChild(host);
                popoutBtn.textContent = '⇲';
                popoutBtn.title = 'pop back in';

                pip.addEventListener('pagehide', () => {
                    // PiP closed — reattach to colonist page.
                    host.style.cssText = originalCss;
                    if (placeholder.parentNode) {
                        placeholder.parentNode.replaceChild(host, placeholder);
                    } else {
                        document.body.appendChild(host);
                    }
                    popoutBtn.textContent = '⇱';
                    popoutBtn.title = 'pop out into a separate window (alt+o)';
                    pipWindowRef = null;
                }, { once: true });
            } catch (err) {
                console.warn('[cataanbot] pop-out failed:', err);
            }
        }
        popoutBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            popOut();
        });

        // ---------- Feedback chips on rec cards ----------
        // Delegated click handler on the panel — survives the
        // innerHTML rewrites that #content does on every poll, so we
        // don't have to re-attach listeners per rec card. Reads the
        // serialized rec dict off the data-rec attribute and posts to
        // /feedback. The visual state (.fb-marked) is set on the
        // clicked button so the choice is sticky for the rest of this
        // rec render — though next /advisor poll will rebuild the
        // rec list and the marker resets, which matches "you marked
        // this rec, not the rec slot."
        panel.addEventListener('click', (e) => {
            const target = e.target;
            if (!(target instanceof Element)) return;
            if (!target.classList.contains('fb')) return;
            e.stopPropagation();
            const dataAttr = target.getAttribute('data-rec');
            if (!dataAttr) return;
            let recDict;
            try {
                recDict = JSON.parse(dataAttr);
            } catch (err) {
                console.warn('[cataanbot] bad fb data:', err);
                return;
            }
            // Only thumbs-down exists — playing the rec is the implicit
            // good signal (auto-logged by the bridge); silence is neutral.
            // Tiny sliver of game state so the labeled rec is interpretable
            // later without rebuilding the snap.
            const snap = latestAdvisorSnap;
            const hint = (snap && {
                seq: snap.seq,
                self_vp: (snap.self || {}).vp,
                self_cards: (snap.self || {}).cards,
                round: snap.round,
                phase: snap.phase,
            }) || {};
            postTo(BRIDGE_FEEDBACK_URL, {
                label: 'bad', rec: recDict, snapshot_hint: hint,
            }, { quiet: false });
            target.classList.add('fb-marked');
        });

        // --------------------------------------------------------------
        // Settings drawer + actions. Holds the New Game reset, pause
        // toggle, opacity slider, and snapshot export. Everything here
        // is optional — the overlay works without any of it — but these
        // are the knobs Noah needs during a session without hunting for
        // a terminal (reset the bridge, mute banners mid-chat, make the
        // HUD translucent, grab a snapshot for bug reports).
        // --------------------------------------------------------------
        const settingsBtn = document.getElementById('settings');
        const drawer = document.getElementById('drawer');
        function openDrawer() {
            drawer.classList.add('open');
            // Re-fetch /config so the VP / discard inputs reflect the
            // bridge's current state — auto-detect from GameStart may
            // have changed it since the userscript first booted.
            if (typeof refreshCfgFromBridge === 'function') {
                refreshCfgFromBridge();
            }
        }
        function toggleDrawer() {
            if (drawer.classList.contains('open')) {
                drawer.classList.remove('open');
            } else {
                openDrawer();
            }
        }
        settingsBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleDrawer();
        });

        // New Game = two-click confirm. First click arms the button for
        // 3s (red flash + "click again to confirm"); second click posts
        // /reset. Auto-disarms so an accidental press doesn't linger.
        const newGameBtn = document.getElementById('new-game');
        const NEW_GAME_LABEL = 'new game';
        let armTimer = null;
        function disarmNewGame() {
            newGameBtn.classList.remove('armed');
            newGameBtn.textContent = NEW_GAME_LABEL;
            if (armTimer) { clearTimeout(armTimer); armTimer = null; }
        }
        function armNewGame() {
            newGameBtn.classList.add('armed');
            newGameBtn.textContent = 'click again to confirm';
            if (armTimer) clearTimeout(armTimer);
            armTimer = setTimeout(disarmNewGame, 3000);
        }
        function fireNewGame() {
            disarmNewGame();
            postTo('http://127.0.0.1:8765/reset', {}, { quiet: true });
            newGameBtn.classList.add('flash-ok');
            newGameBtn.textContent = 'reset ✓';
            setTimeout(() => {
                newGameBtn.classList.remove('flash-ok');
                newGameBtn.textContent = NEW_GAME_LABEL;
            }, 1200);
        }
        newGameBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            if (newGameBtn.classList.contains('armed')) fireNewGame();
            else armNewGame();
        });

        // Pause toggle — sets data-paused on the panel so the CSS filter
        // suppresses tactical sections. The advisor keeps polling so
        // unpause is instant and state is still current.
        const pauseInput = document.getElementById('pause');
        function applyPaused(paused) {
            panel.dataset.paused = paused ? '1' : '0';
            try {
                localStorage.setItem(
                    'cataanbot.paused', paused ? '1' : '0');
            } catch (_) { /* storage blocked — fine */ }
        }
        try {
            const savedPause =
                localStorage.getItem('cataanbot.paused') === '1';
            pauseInput.checked = savedPause;
            applyPaused(savedPause);
        } catch (_) { applyPaused(false); }
        pauseInput.addEventListener('change', () => {
            applyPaused(pauseInput.checked);
        });

        // Opacity slider — applies to the host element (outside shadow)
        // so the whole overlay goes translucent. 100% = default. Useful
        // for placing the HUD over the board without blocking reads.
        const opacityInput = document.getElementById('opacity');
        const opacityVal = document.getElementById('opacity-val');
        function applyOpacity(pct) {
            const clamped = Math.max(40, Math.min(100, pct));
            host.style.opacity = (clamped / 100).toFixed(2);
            opacityVal.textContent = clamped + '%';
            try {
                localStorage.setItem('cataanbot.opacity', String(clamped));
            } catch (_) { /* storage blocked */ }
        }
        try {
            const savedOp = parseInt(
                localStorage.getItem('cataanbot.opacity') || '', 10);
            if (Number.isFinite(savedOp)) {
                opacityInput.value = String(savedOp);
                applyOpacity(savedOp);
            }
        } catch (_) { /* storage blocked */ }
        opacityInput.addEventListener('input', () => {
            applyOpacity(parseInt(opacityInput.value, 10));
        });

        // Game mode config — VP target + discard limit. POSTs to the
        // bridge's /config so a 14-VP / 10-discard variant can be
        // played without restarting. Persisted to localStorage so the
        // inputs reflect the last-used values immediately on reload;
        // refreshed from the bridge every time the drawer opens, so
        // values stay current with the bridge's auto-detect (a fresh
        // GameStart frame stamps in colonist's gameSettings). Debounced
        // POST on change so spam-clicking the spinner arrows doesn't
        // fire a POST per keystroke.
        const vpInput = document.getElementById('vp-target');
        const discardInput = document.getElementById('discard-limit');
        const cfgStatus = document.getElementById('game-cfg-status');
        const CFG_DEBOUNCE_MS = 350;
        let cfgPostTimer = null;
        let cfgStatusTimer = null;
        function setCfgStatus(text, kind) {
            cfgStatus.textContent = text || '';
            cfgStatus.classList.remove('ok', 'err');
            if (kind) cfgStatus.classList.add(kind);
            if (cfgStatusTimer) clearTimeout(cfgStatusTimer);
            if (text && kind === 'ok') {
                cfgStatusTimer = setTimeout(() => {
                    cfgStatus.textContent = '';
                    cfgStatus.classList.remove('ok');
                }, 1500);
            }
        }
        function readCfgInputs() {
            const vp = parseInt(vpInput.value, 10);
            const dl = parseInt(discardInput.value, 10);
            return {
                vp_target: Number.isFinite(vp) ? vp : null,
                discard_limit: Number.isFinite(dl) ? dl : null,
            };
        }
        function persistCfgLocal(vp, dl) {
            try {
                if (Number.isFinite(vp)) {
                    localStorage.setItem('cataanbot.vp_target', String(vp));
                }
                if (Number.isFinite(dl)) {
                    localStorage.setItem(
                        'cataanbot.discard_limit', String(dl));
                }
            } catch (_) { /* storage blocked */ }
        }
        function applyCfgToInputs(cfg) {
            if (!cfg) return;
            if (Number.isFinite(cfg.vp_target)) {
                vpInput.value = String(cfg.vp_target);
            }
            if (Number.isFinite(cfg.discard_limit)) {
                discardInput.value = String(cfg.discard_limit);
            }
            persistCfgLocal(cfg.vp_target, cfg.discard_limit);
        }
        function refreshCfgFromBridge() {
            getJson('http://127.0.0.1:8765/config')
                .then(applyCfgToInputs)
                .catch(() => { /* offline — keep current */ });
        }
        function postCfg() {
            const body = readCfgInputs();
            if (body.vp_target === null && body.discard_limit === null) {
                setCfgStatus('invalid', 'err');
                return;
            }
            postJson('http://127.0.0.1:8765/config', body)
                .then((res) => {
                    if (!res || res.ok === false) {
                        setCfgStatus('error', 'err');
                        return;
                    }
                    applyCfgToInputs(res);
                    setCfgStatus('saved', 'ok');
                })
                .catch(() => setCfgStatus('offline', 'err'));
        }
        function scheduleCfgPost() {
            if (cfgPostTimer) clearTimeout(cfgPostTimer);
            cfgPostTimer = setTimeout(postCfg, CFG_DEBOUNCE_MS);
            setCfgStatus('…', null);
        }
        // Seed from localStorage immediately so the inputs aren't
        // empty if the bridge hasn't responded yet.
        try {
            const lvp = parseInt(
                localStorage.getItem('cataanbot.vp_target') || '', 10);
            const ldl = parseInt(
                localStorage.getItem('cataanbot.discard_limit') || '', 10);
            if (Number.isFinite(lvp)) vpInput.value = String(lvp);
            if (Number.isFinite(ldl)) discardInput.value = String(ldl);
        } catch (_) { /* storage blocked */ }
        // Initial fetch on userscript boot — covers the case where
        // the user never opens the drawer but the values still need
        // to stay synced (e.g., for localStorage persistence). After
        // this, every drawer open re-fetches via the click handler so
        // auto-detected values from a fresh GameStart show up live.
        refreshCfgFromBridge();
        vpInput.addEventListener('change', scheduleCfgPost);
        discardInput.addEventListener('change', scheduleCfgPost);

        // Copy Snapshot — fetches the current /advisor JSON and writes
        // it to the clipboard. Exists so Noah can paste exact tracker
        // state into a bug report without screenshotting.
        const copySnapBtn = document.getElementById('copy-snap');
        const COPY_SNAP_LABEL = 'copy snapshot';
        async function copySnapshot() {
            try {
                const snap = await getJson(
                    'http://127.0.0.1:8765/advisor');
                const text = JSON.stringify(snap, null, 2);
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(text);
                } else {
                    // Fallback: dump into a hidden textarea and copy.
                    const ta = document.createElement('textarea');
                    ta.value = text; ta.style.position = 'fixed';
                    ta.style.opacity = '0';
                    document.body.appendChild(ta);
                    ta.select(); document.execCommand('copy');
                    document.body.removeChild(ta);
                }
                copySnapBtn.classList.add('flash-ok');
                copySnapBtn.textContent = 'copied ✓';
            } catch (_) {
                copySnapBtn.textContent = 'copy failed';
            }
            setTimeout(() => {
                copySnapBtn.classList.remove('flash-ok');
                copySnapBtn.textContent = COPY_SNAP_LABEL;
            }, 1200);
        }
        copySnapBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            copySnapshot();
        });

        // Keyboard shortcuts. Alt+<letter> avoids typing collisions with
        // colonist's chat and the board. Uses e.code (KeyP/KeyC/etc)
        // because on macOS Alt produces special chars for e.key.
        window.addEventListener('keydown', (e) => {
            if (!e.altKey || e.metaKey || e.ctrlKey || e.shiftKey) return;
            if (e.code === 'KeyP') {
                e.preventDefault();
                pauseInput.checked = !pauseInput.checked;
                applyPaused(pauseInput.checked);
            } else if (e.code === 'KeyC') {
                e.preventDefault();
                body.classList.toggle('collapsed');
            } else if (e.code === 'KeyS') {
                e.preventDefault();
                toggleDrawer();
            } else if (e.code === 'KeyN') {
                e.preventDefault();
                if (!drawer.classList.contains('open')) {
                    openDrawer();
                }
                if (newGameBtn.classList.contains('armed')) fireNewGame();
                else armNewGame();
            } else if (e.code === 'KeyO') {
                e.preventDefault();
                popOut();
            }
        });

        // Simple drag: on mousedown in the header, track pointer and move
        // the host element. Panel uses top/right by default; once the user
        // drags we switch to top/left for positional stability.
        let dragging = null;
        header.addEventListener('mousedown', (e) => {
            dragging = {
                startX: e.clientX, startY: e.clientY,
                hostLeft: host.getBoundingClientRect().left,
                hostTop: host.getBoundingClientRect().top,
            };
            host.style.right = 'auto';
            host.style.left = dragging.hostLeft + 'px';
            host.style.top = dragging.hostTop + 'px';
            e.preventDefault();
        });
        window.addEventListener('mousemove', (e) => {
            if (!dragging) return;
            host.style.left =
                (dragging.hostLeft + e.clientX - dragging.startX) + 'px';
            host.style.top =
                (dragging.hostTop + e.clientY - dragging.startY) + 'px';
        });
        window.addEventListener('mouseup', () => { dragging = null; });

        // Resize handle: drag mutates --panel-w and --font-scale so the
        // HUD grows proportionally (text and spacing scale together).
        // Width is the primary knob; font-scale follows a linear fit
        // from base 340px → 1.0 up to 640px → 1.5. Persisted to
        // localStorage so the size survives reloads.
        const PANEL_W_MIN = 320, PANEL_W_MAX = 820;
        const BASE_W = 480;
        function scaleForWidth(w) {
            // 480→1.0, 800→1.6 — linear, clamped to [0.9, 1.7].
            const s = 1.0 + (w - BASE_W) * 0.6 / 320;
            return Math.max(0.9, Math.min(1.7, s));
        }
        function applySize(w) {
            const clamped = Math.max(PANEL_W_MIN, Math.min(PANEL_W_MAX, w));
            panel.style.setProperty('--panel-w', clamped + 'px');
            panel.style.setProperty('--font-scale',
                                    scaleForWidth(clamped).toFixed(3));
            try { localStorage.setItem('cataanbot.hudWidth', String(clamped)); }
            catch (_) { /* private mode, storage blocked — fine */ }
        }
        // Restore saved width on boot.
        try {
            const saved = parseInt(
                localStorage.getItem('cataanbot.hudWidth') || '', 10);
            if (Number.isFinite(saved)) applySize(saved);
        } catch (_) { /* storage unavailable */ }

        const handle = document.getElementById('resize-handle');
        let resizing = null;
        handle.addEventListener('mousedown', (e) => {
            e.stopPropagation();
            e.preventDefault();
            resizing = {
                startX: e.clientX,
                startW: panel.getBoundingClientRect().width,
            };
        });
        window.addEventListener('mousemove', (e) => {
            if (!resizing) return;
            applySize(resizing.startW + (e.clientX - resizing.startX));
        });
        window.addEventListener('mouseup', () => { resizing = null; });

        const histHost = document.getElementById('hist-host');
        const hist = document.getElementById('hist');
        const histTotal = document.getElementById('hist-total');
        const evalHost = document.getElementById('eval-host');
        const evalGraph = document.getElementById('eval-graph');
        const evalLine = document.getElementById('eval-line');
        const evalFill = document.getElementById('eval-fill');
        const evalDot = document.getElementById('eval-dot');
        const evalCur = document.getElementById('eval-cur');
        const mqHost = document.getElementById('mq-host');
        const mqTally = document.getElementById('mq-tally');
        const mqLast = document.getElementById('mq-last');
        const devDeckHost = document.getElementById('dev-deck-host');
        const devDeck = document.getElementById('dev-deck');
        // Pre-populate the 11 columns once. renderOverlay only mutates
        // bar heights + class flags from here on — the column DOM never
        // gets rebuilt, which is what lets CSS height transitions fire
        // on actual roll deltas instead of replaying from 0 each tick.
        const HIST_NUMS = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
        const HIST_HOT = new Set([6, 8]);
        hist.innerHTML = HIST_NUMS.map((n) => {
            const cls = ['hist-col'];
            if (n === 7) cls.push('seven');
            else if (HIST_HOT.has(n)) cls.push('hot');
            return `<div class="${cls.join(' ')}" data-n="${n}">`
                + `<div class="hist-count" data-count></div>`
                + `<div class="hist-bar-wrap">`
                + `<div class="hist-exp" data-exp></div>`
                + `<div class="hist-bar" data-bar style="height:0%"></div>`
                + `</div>`
                + `<div class="hist-num">${n}</div>`
                + `</div>`;
        }).join('');
        const variantBadge = document.getElementById('variant-badge');
        return {
            host, panel, body, content, dot,
            histHost, hist, histTotal,
            evalHost, evalGraph, evalLine, evalFill, evalDot, evalCur,
            mqHost, mqTally, mqLast,
            devDeckHost, devDeck,
            variantBadge,
        };
    }

    // Tile chips: one span per producing tile, number-first so 6/8
    // (red-pip rolls) jump out. Hoisted out of the renderOverlay
    // recs-flow block so the dev-card-hint placement section (which
    // also calls tilesToHtml) doesn't blow up with "tilesToHtml is
    // not defined" — the call site at the rec-hint placement is
    // outside the if(my_turn || isSetup) block where the local
    // tilesToHtml used to live. (Bug Noah saw on 2026-05-01.)
    function tilesToHtml(arr) {
        return (arr || [])
            .filter(t => t && t[0] !== 'DESERT')
            .map(t => {
                const icon = iconFor(t[0]);
                const num = t[1];
                if (num == null) {
                    return `<span class="tile-chip">`
                        + `<span class="tile-res">${icon}`
                        + `</span></span>`;
                }
                const hot = (num === 6 || num === 8);
                const cls = hot ? 'tile-num hot' : 'tile-num';
                return `<span class="tile-chip">`
                    + `<span class="${cls}">${num}</span>`
                    + `<span class="tile-res">${icon}`
                    + `</span></span>`;
            })
            .join('');
    }

    function renderOverlay(ui, snap, live) {
        ui.dot.classList.toggle('live', !!live);
        if (!snap) {
            ui.content.innerHTML =
                '<span class="err">bridge unreachable</span>';
            if (ui.histHost) ui.histHost.classList.add('hidden');
            if (ui.evalHost) ui.evalHost.classList.add('hidden');
            if (ui.mqHost) ui.mqHost.classList.add('hidden');
            if (ui.devDeckHost) ui.devDeckHost.classList.add('hidden');
            return;
        }
        if (!snap.game_started) {
            ui.content.innerHTML =
                '<span class="muted">waiting for game start…</span>';
            if (ui.histHost) ui.histHost.classList.add('hidden');
            if (ui.evalHost) ui.evalHost.classList.add('hidden');
            if (ui.mqHost) ui.mqHost.classList.add('hidden');
            if (ui.devDeckHost) ui.devDeckHost.classList.add('hidden');
            // Clear header pills too — they shouldn't persist across
            // a reset/new-game while the next game's settings load.
            ui.panel.dataset.variant = 'classic';
            ui.panel.dataset.friendlyRobber = '0';
            if (ui.variantBadge) {
                ui.variantBadge.textContent = '';
                ui.variantBadge.title = '';
            }
            ui.panel.dataset.phase = 'pre';
            return;
        }
        // Phase tag drives [data-phase=...] CSS hooks for visual demotion
        // of phase-irrelevant content. setup→opening picks dominate; late
        // → production/yield breakdown dimmed since the game is decided
        // by VP threats, not long-horizon planning. The bridge already
        // computes phase from rounds (≤5 early, ≤12 mid, >12 late) and
        // skips the field during setup, so we use snap.setup_phase as
        // the explicit setup signal and fall back to "early" until the
        // first roll lands.
        ui.panel.dataset.phase = snap.setup_phase
            ? 'setup'
            : (snap.game_progress && snap.game_progress.phase) || 'early';
        // Variant-board badge in the header. CSS shows it only when
        // data-variant="non-classic". Tooltip carries the raw label
        // (e.g. "variant: extension=2, map=1") so the user can see
        // which colonist setting flagged.
        if (ui.variantBadge) {
            const variant = snap.variant || 'classic';
            if (variant === 'classic') {
                ui.panel.dataset.variant = 'classic';
                ui.variantBadge.textContent = '';
                ui.variantBadge.title = '';
            } else if (snap.variant_recs_disabled) {
                ui.panel.dataset.variant = 'non-classic';
                ui.variantBadge.textContent = 'variant — recs OFF';
                ui.variantBadge.title = `${variant} — bot can't track this map's node IDs reliably (catanatron underneath only models classic). Recommendations suppressed to avoid suggesting occupied corners. Histogram + opp tracking still work.`;
            } else {
                ui.panel.dataset.variant = 'non-classic';
                ui.variantBadge.textContent = 'variant map';
                ui.variantBadge.title = `${variant} — same Catan rules, different board shape. Opening picks, recommender, and port 2:1 trade rates all work on the actual geometry.`;
            }
        }
        // Friendly Robber pill — colonist's optional rule that
        // protects players at or below 2 VP from being robbed. When
        // active, the bot's robber-target ranking has already
        // filtered protected victims so suggestions match what
        // colonist will actually let you pick.
        ui.panel.dataset.friendlyRobber = snap.friendly_robber_active
            ? '1' : '0';
        const parts = [];
        // WIN THIS TURN banner — highest-priority signal. Renders above
        // every other HUD element so Noah never misses a single-move
        // win. Covers: settle/city (+1 VP), road→LR (+2 VP), knight→LA
        // (+2 VP). Confidence "medium" (road→LR) gets a hedge prefix
        // since placement matters; "high" paths are unambiguous.
        if (snap.winning_move && snap.winning_move.message) {
            const wm = snap.winning_move;
            const conf = wm.confidence === 'high' ? 'high' : 'hedge';
            const headline = wm.confidence === 'high'
                ? 'WIN THIS TURN'
                : 'WIN THIS TURN (if placement works)';
            const altFrag = (wm.alternatives || []).length > 0
                ? '<div class="wm-alts">also: '
                    + wm.alternatives.map(a =>
                        `<span>${escapeHtml(a.detail)}</span>`
                      ).join('; ')
                    + '</div>'
                : '';
            parts.push(`<div class="winning-move ${conf}">`
                + `<span class="wm-head">${escapeHtml(headline)}</span>`
                + `<div class="wm-detail">${escapeHtml(wm.detail || '')}`
                + ` · ${wm.vp}→${wm.vp_after} VP</div>`
                + altFrag
                + '</div>');
        }
        // Game-progress header: anchors the tactical signals below.
        // Silent in setup — phase is self-evident then.
        const gp = snap.game_progress;
        if (gp) {
            // Standings trailer: "BLUE leading at 7 · you at 4" (or
            // "you leading at 6 · BLUE at 4"). Only surface when both
            // leader and self are set and VPs are beyond the trivial
            // opening (>=3) — before that everyone is tied and the
            // leader label is noise.
            let standingsTag = '';
            const st = snap.standings;
            if (st && st.leader && (st.self_vp >= 3 || st.leader.vp >= 3)) {
                if (st.self_is_leader) {
                    standingsTag = ` · <span class="stand-self">you `
                        + `${st.self_vp} (lead)</span>`;
                } else {
                    const leadName = escapeHtml(st.leader.username || '?');
                    standingsTag = ` · ${leadName} ${st.leader.vp}`
                        + ` <span class="stand-gap">· you ${st.self_vp}`
                        + ` (-${st.gap_to_leader})</span>`;
                }
            }
            parts.push('<div class="gprog">'
                + `round <span class="gp-round">${gp.round}</span> · `
                + `<span class="ph-${gp.phase}">${gp.phase}</span>`
                + standingsTag
                + '</div>');
        }
        const me = snap.self;
        if (me) {
            parts.push('<div class="card self">');
            const bg = pillColor(me);
            const fg = contrastText(bg);
            const pill = `<span class="color-pill" style="background:${bg};`
                + `color:${fg};">${escapeHtml(me.username)}</span>`;
            // Meta trailer: cards · knights. Piece counts (Xs/Yc/Zr)
            // were dropped — they read as cryptic shorthand and Catan's
            // own UI already shows them. Knights-played stays since it's
            // a hidden-VP signal we surface elsewhere.
            const metaSegs = [];
            // discard_limit follows the catanatron/colonist convention:
            // first hand size that triggers discard on a 7 is limit+1.
            // So at default 7, cards > 7 (i.e. 8+) goes red. Two values
            // below the cliff (6,7) goes amber — one bad roll from
            // halving. Default 7 if the snap hasn't surfaced a value
            // yet, which preserves prior >=8 behavior.
            const discardLimit = snap.discard_limit || 7;
            const cardsCount = me.cards || 0;
            const cardsTier = cardsCount > discardLimit ? 'fat-hand'
                : (cardsCount > discardLimit - 2 ? 'watch-hand' : '');
            metaSegs.push(cardsTier
                ? `<span class="${cardsTier}">${me.cards} cards</span>`
                : `${me.cards} cards`);
            if ((me.knights_played || 0) > 0) {
                metaSegs.push(`${me.knights_played} knights`);
            }
            const metaHtml = `<span class="self-meta">`
                + metaSegs.join(' · ') + `</span>`;
            // VP number as the visual anchor of the self card — sized up,
            // right-aligned via margin-left:auto in CSS so the pill stays
            // flush-left and the eye snaps between them.
            const vpBig = `<span class="vp-big">${me.vp}`
                + `<span class="lbl">VP</span></span>`;
            parts.push(`<div class="you">${pill}${vpBig}${metaHtml}</div>`);
            // VP breakdown — only worth surfacing once VP > 2 (past the
            // trivial 2-settle opening). Shows how VP composes so Noah can
            // tell a 6-VP-via-cities lead apart from a 6-VP-via-longest-road
            // that flips back the moment somebody outbuilds his road.
            if (me.vp_breakdown && me.vp > 2) {
                const b = me.vp_breakdown;
                const segs = [];
                if (b.settle) segs.push(`${b.settle}s`);
                // city slot is already doubled (cities × 2).
                if (b.city) segs.push(`${b.city}c`);
                if (b.vp_cards) segs.push(`${b.vp_cards}vc`);
                if (b.longest_road) segs.push(`${b.longest_road}LR`);
                if (b.largest_army) segs.push(`${b.largest_army}LA`);
                if (segs.length >= 2) {
                    parts.push(`<div class="vpb">${segs.join(' + ')}`
                        + ` = ${b.total} VP</div>`);
                }
            }
            // Icons scan faster than letter abbrevs on a dense HUD.
            // Wrap a vulnerable stack in .mono-risk so it pops amber
            // — matches the monopoly_risk field on the snap.
            const monoRes = me.monopoly_risk ? me.monopoly_risk.resource : null;
            const hand = Object.entries(me.hand || {})
                .filter(([, n]) => n > 0)
                .map(([r, n]) => {
                    const cls = (r === monoRes) ? ' class="mono-risk"' : '';
                    return `<span${cls}>${iconFor(r)} ${n}</span>`;
                })
                .join('') || '<span class="muted">∅</span>';
            parts.push(`<div class="hand">${hand}</div>`);
            if (me.monopoly_risk) {
                const mr = me.monopoly_risk;
                parts.push('<div class="mono-warn">'
                    + `<span class="b-ico">🚨</span> ${mr.count} `
                    + `${iconFor(mr.resource)} at monopoly risk`
                    + '</div>');
            }
            // Hand-drift warning. Tracker's event-reconstructed breakdown
            // disagreed with colonist's authoritative card count — the
            // per-resource detail is unreliable until the next HandSync
            // frame corrects us. Typically caused by a ws disconnect.
            if (me.hand_drift) {
                parts.push('<div class="drift">⚠ stale hand · '
                    + 'resyncing</div>');
            }
            const afford = (me.afford || []).join(' · ');
            if (afford) {
                parts.push(`<div class="afford">`
                    + `<span class="b-ico">✅</span> ${afford}</div>`);
            } else if (me.next_build) {
                // Nearest-miss gap as a direction-of-travel hint:
                // "1 brick from settlement" is more useful than
                // "nothing buildable" because it says what to aim for.
                const nb = me.next_build;
                const missingStr = Object.entries(nb.missing || {})
                    .map(([r, n]) => `${iconFor(r)} ${n}`)
                    .join(' + ');
                parts.push(`<div class="afford near">`
                    + `<span class="b-ico">⏳</span> ${missingStr}`
                    + ` from ${escapeHtml(nb.build)}</div>`);
            } else {
                parts.push('<div class="afford none">– nothing buildable</div>');
            }
            // Owned ports — chip group, ⚓ as label glyph. Matches the
            // format used on opp rows so the eye doesn't have to learn
            // two layouts. Silent until self owns at least one port.
            if ((me.ports || []).length) {
                const chips = me.ports.map(p => p === 'GENERIC'
                    ? '<span class="port-chip">3:1</span>'
                    : `<span class="port-chip">${iconFor(p)} 2:1</span>`
                ).join('');
                parts.push(`<div class="ports">⚓ ${chips}</div>`);
            }
            // Production rate — expected cards per dice roll given
            // current builds. Terse format: "1.50/roll · 🌾" — the
            // "prod:" label and "strongest" word are dropped since the
            // /roll suffix is self-evident and the icon stands alone.
            // Skipped at 0 (setup phase) to avoid a meaningless line.
            const prod = me.production;
            if (prod && prod.per_roll > 0) {
                const top = prod.top_resource
                    ? ` · ${iconFor(prod.top_resource)}`
                    : '';
                parts.push(`<div class="prod">`
                    + `${prod.per_roll.toFixed(2)}/roll${top}</div>`);
            }
            parts.push('</div>');  // .card.self
        }
        // Setup-phase opening picks render unconditionally — it's
        // useful to plan around them even off-turn so you know what to
        // grab when your slot comes up.
        const isSetup = !!snap.setup_phase;
        // Off-turn ribbon. When it's not my turn (and we're past
        // setup) the bridge intentionally suppresses rec computation,
        // but the user benefits from a clear visual cue that the
        // panel is in "watch" mode — otherwise an empty rec block
        // looks like a render bug. The ribbon also tells you whose
        // turn it is when colonist's WS metadata has the username.
        if (!snap.my_turn && !isSetup) {
            const turnUser = snap.current_turn_username
                || snap.current_turn_color || 'an opponent';
            parts.push(
                '<div class="off-turn-ribbon">'
                + '<span class="b-ico">⏳</span> '
                + `${escapeHtml(String(turnUser))}'s turn — watching</div>`);
        }
        // Recommendations — only shown when it's my turn (mid-game) or
        // during setup (always useful). Split into:
        //   "best moves"      — things affordable right now
        //   "planning ahead"  — 1-2 cards from a better move; "save for X"
        // Both groups sorted by score desc within the list the backend sent.
        // Wrap the whole rec block in .recs-flow — CSS gives it order:-1
        // inside the flex body so the hero rec sits at the very top,
        // above the self card and everything else. Banners (winning_move,
        // gprog) keep order:-2 so they outrank even the recs.
        parts.push('<div class="recs-flow">');
        if ((snap.my_turn || isSetup)
                && (snap.recommendations || []).length) {
            const nowRecs = [];
            const soonRecs = [];
            for (const r of snap.recommendations) {
                (r.when === 'soon' ? soonRecs : nowRecs).push(r);
            }
            // tilesToHtml is hoisted to module scope above renderOverlay
            // so the dev-card-hint placement section can use it too.
            const renderRec = (r, isTop, optLetter) => {
                const topCls = isTop ? ' top' : '';
                // Setup-phase followup recs are kind='opening_settlement'
                // but their primary action is laying a road (the settle
                // is already down). Backend flags that with action:'road'
                // so we show "ROAD" as the label — matches what Noah's
                // about to actually do.
                const effectiveKind = (r.action === 'road')
                    ? 'road' : r.kind;
                const kindLabel = {
                    settlement: 'settle',
                    city: 'city',
                    road: 'road',
                    dev_card: 'dev card',
                    trade: 'trade',
                    propose_trade: 'propose',
                    bank_trade: 'port/bank',
                    discard: 'discard',
                    opening_settlement: 'settle',
                }[effectiveKind] || effectiveKind.replace(/_/g, ' ');
                const tilesHtml = tilesToHtml(r.tiles);
                // Roads lead to a landing spot — render as
                // "→ between [tile a] [tile b]" so the player reads
                // the road as connecting two tiles. The compass arrow
                // (N/NE/etc) was dropped after repeated catanatron→
                // colonist orientation regressions; the tile labels
                // alone disambiguate which edge unambiguously.
                let arrowHtml = '';
                if (r.kind === 'road') {
                    arrowHtml = '<span class="arrow">→</span>';
                    if (tilesHtml) {
                        arrowHtml += ' <span class="muted">between</span> ';
                    }
                }
                // Plain right-arrow for roads even when tiles are missing —
                // signals "this is a placement rec" without committing to
                // a compass direction we can't reliably render.
                const loc = (r.kind === 'road' && arrowHtml)
                    ? ` ${arrowHtml}${tilesHtml}`
                    : (tilesHtml ? ` ${arrowHtml}${tilesHtml}` : '');
                const s = Number(r.score || 0);
                const scoreCls = s >= 8 ? 'strong'
                    : (s >= 5 ? 'decent' : 'weak');
                // Search delta from 1-ply rerank: how much the bot's
                // state-eval moves if you play this rec. Chess-eval-bar
                // analogue, scaled to mid-game range (~0-150). Hidden
                // when null (unsimulatable kinds: propose_trade, etc.)
                // or 0 (no information). Sign + magnitude both inform
                // colour; +20 reads as "real upside", -10 as "active
                // negative."
                let evHtml = '';
                if (r.search_delta != null
                        && Number.isFinite(r.search_delta)
                        && Math.abs(r.search_delta) >= 0.5) {
                    const ev = Number(r.search_delta);
                    const evCls = ev >= 20 ? 'pos-strong'
                        : (ev >= 5 ? 'pos'
                            : (ev > -5 ? 'neutral'
                                : (ev > -20 ? 'neg' : 'neg-strong')));
                    const sign = ev > 0 ? '+' : '';
                    evHtml = `<span class="ev ${evCls}" title="how much better this move scores than doing nothing">`
                        + `${sign}${ev.toFixed(0)}</span>`;
                }
                const planCls = r.when === 'soon' ? ' plan' : '';
                const tradeCls = (r.kind === 'trade'
                    || r.kind === 'propose_trade') ? ' trade' : '';
                // Recommender flags road alternates with `alt: true` —
                // they're "if the hero road is blocked, here are the
                // next-best edges" entries. Dim them so they don't
                // compete visually with the hero or the rest of the
                // ranked rec list. Never apply to the hero itself.
                const altCls = (r.alt && !isTop) ? ' alt' : '';
                // kind-build hides the detail prose on the hero — the
                // tile chips already say what's being built. Trades /
                // discards / dev cards keep detail visible because the
                // verb alone ("PROPOSE", "PORT/BANK") is meaningless.
                const buildKinds = new Set([
                    'settlement', 'city', 'road',
                    'opening_settlement']);
                const buildCls = buildKinds.has(effectiveKind)
                    ? ' kind-build' : '';
                // Option A/B/C/D label — only during opening picks so
                // Noah can say "I'm taking Option B" out loud with a
                // friend across the table.
                const optHtml = optLetter
                    ? `<span class="opt">${optLetter}</span>`
                    : '';
                // Thumbs-down only. Per Noah's feedback model:
                // playing the rec is the implicit "good" signal
                // (auto-logged as label="auto_good" by the bridge
                // when self builds match the top rec); ignoring is
                // implicit "neutral"; the explicit chip is reserved
                // for "this rec was genuinely bad — don't suggest
                // again". One button = no flip-flop, no accidental
                // up-clicks.
                const fbPayload = JSON.stringify({
                    kind: r.kind, when: r.when, score: r.score,
                    detail: r.detail, give: r.give, get: r.get,
                    unlocks: r.unlocks, node_id: r.node_id,
                    edge: r.edge, alt: r.alt,
                });
                const fbHtml = `<span class="fb-row">`
                    + `<button class="fb fb-down" `
                    + `data-rec='${escapeHtml(fbPayload)}' `
                    + `title="this rec was bad — log it">👎</button>`
                    + `</span>`;
                parts.push(`<div class="rec${topCls}${planCls}${tradeCls}${buildCls}${altCls}">`
                    + optHtml
                    + `<span class="score ${scoreCls}">${s.toFixed(1)}</span>`
                    + evHtml
                    + ` <span class="kind">${kindLabel}</span>`
                    + `<span class="tiles">${loc}</span> `
                    + `<span class="detail">${escapeHtml(r.detail || '')}`
                    + `</span>${fbHtml}</div>`);
                // Opening-settlement picks include a nested road hint:
                // "your follow-up road sits between these tiles." The
                // tile chips are the disambiguator — compass direction
                // was dropped after repeated orientation regressions.
                if (r.kind === 'opening_settlement' && r.road
                        && r.road.edge_tiles) {
                    const towardHtml = tilesToHtml(
                        r.road.edge_tiles || []);
                    const dirHtml = '<span class="arrow">↳ road</span> ';
                    let warn = r.road.contested
                        ? ' <span class="warn">⚠ contested</span>'
                        : '';
                    if (r.road.sealed) {
                        warn += ' <span class="warn">⚠ corridor sealed'
                            + '</span>';
                    }
                    const tail = towardHtml
                        ? '<span class="muted">between</span> ' + towardHtml
                        : '';
                    parts.push('<div class="rec-sub">'
                        + dirHtml
                        + tail
                        + warn
                        + '</div>');
                }
                // In-game road sealed-corridor warning. The direction
                // arrow itself is already on the main rec line above
                // (via arrowHtml); rendering it again here was a
                // duplicate Noah flagged. Keep just the warn line when
                // it applies.
                if (r.kind === 'road' && r.sealed) {
                    parts.push('<div class="rec-sub">'
                        + '<span class="warn">⚠ corridor sealed</span>'
                        + '</div>');
                }
                // Round-1 picks also carry plan.second — the best paired
                // 2nd-settlement for this F. Render it as its own sub-line
                // so Noah reads each F pick as a coordinated 2-settle plan.
                const planSecond = r.plan && r.plan.second;
                if (planSecond && planSecond.tiles
                        && planSecond.tiles.length) {
                    const planHtml = tilesToHtml(planSecond.tiles);
                    const cov = planSecond.covers
                        ? `<span class="cov">cov ${planSecond.covers}/5</span>`
                        : '';
                    const arch = r.plan.archetype
                        ? ` <span class="arch">${escapeHtml(
                            r.plan.archetype)}</span>`
                        : '';
                    parts.push('<div class="rec-sub plan-second">'
                        + '<span class="arrow">↳ 2nd:</span> '
                        + planHtml + cov + arch
                        + '</div>');
                }
            };
            // Game plan banner — frames the rec list with a short
            // principal variation: "2 roads · need 1🧱 1🐑 · 4:1 🌾→⛰️
            // if stuck". Only renders mid-game (setup owns opening
            // picks separately).
            if (!isSetup && snap.game_plan && snap.game_plan.summary) {
                const gp = snap.game_plan;
                const kindCls = gp.goal_kind === 'city'
                    ? 'plan-city' : 'plan-settle';
                const goalTiles = (gp.goal_tiles && gp.goal_tiles.length)
                    ? ` ${tilesToHtml(gp.goal_tiles)}` : '';
                const missingCount = gp.missing
                    ? Object.keys(gp.missing).length : 0;
                const readyCls = missingCount === 0 ? ' ready' : '';
                let body = '<span class="gp-kind">'
                    + escapeHtml(gp.goal_kind || '') + '</span>'
                    + '<span class="gp-summary">'
                    + escapeHtml(gp.summary) + '</span>'
                    + goalTiles;
                parts.push(`<div class="game-plan ${kindCls}${readyCls}">`
                    + '<div class="gp-h">plan</div>'
                    + `<div class="gp-body">${body}</div>`
                    + '</div>');
            }
            if (nowRecs.length) {
                // Mid-game: skip the "best moves" header — the hero rec
                // is huge already and the label adds chrome. Setup phase
                // still gets the "opening picks" header so the A/B/C
                // letters have a visual anchor.
                if (isSetup) {
                    parts.push('<div class="recs-h">→ opening picks</div>');
                }
                nowRecs.forEach((r, i) => {
                    // Only stamp A/B/C/... on opening picks — mid-game
                    // recs already read as a ranked action list.
                    const optLetter = (isSetup
                        && r.kind === 'opening_settlement')
                        ? String.fromCharCode(65 + i)
                        : null;
                    renderRec(r, i === 0, optLetter);
                });
            } else if (!isSetup) {
                parts.push('<div class="turn-hint">nothing affordable '
                    + '· save up</div>');
            }
            if (soonRecs.length) {
                parts.push('<div class="recs-h plan-h">'
                    + '→ planning ahead</div>');
                soonRecs.forEach(r => renderRec(r, false));
            }
            // Longer-horizon / riskier plays — LR push, LA push, dev-
            // card dive. VP swing is the headline so these read as
            // "what's the most I can gain by committing pieces?"
            // rather than disappearing into the affordable-now list.
            const strat = snap.strategic_options;
            if (!isSetup && strat && strat.length) {
                parts.push('<div class="recs-h plan-h">'
                    + '→ long game</div>');
                for (const s of strat) {
                    parts.push('<div class="strat-opt">'
                        + `<span class="strat-vp">+${s.vp_swing}VP</span>`
                        + `<span class="strat-label">`
                        + escapeHtml(s.label) + '</span>'
                        + `<span class="strat-detail">`
                        + escapeHtml(s.detail) + '</span>'
                        + '</div>');
                }
            }
        } else if (snap.my_turn) {
            parts.push('<div class="turn-hint">nothing affordable '
                + '· save up</div>');
        }
        // --- Dev-card play-timing cluster ---
        // Knight / Monopoly / YoP / Road-Building hints are all "should I
        // play this dev card right now" decisions. Group them into one
        // cluster right under the rec list so the PLAY/HOLD verdict sits
        // in Noah's first scan, not buried below opponents + trade +
        // roll history. Section header only when at least one fires.
        // Header pill: total dev cards self holds + how many are
        // "just bought this turn" (Catan's no-play-on-buy delay). Since
        // colonist hides the card type from the DOM log, we show all
        // four hint blocks side-by-side and let the user pick the one
        // that matches what's actually in their dev card panel.
        const devBlocks = [];
        const devHeld = Number(snap.dev_cards_held || 0);
        const devVpHeld = Number(snap.dev_cards_vp_held || 0);
        const devNonVp = Number(snap.dev_cards_non_vp_held || 0);
        const devJust = Number(snap.dev_cards_just_bought || 0);
        const devPlayable = Number(snap.dev_cards_playable || 0);
        // Type-known: bridge decoded the type from colonist's WS frame
        // (knight/monopoly/yop/rb), so only the matching hint block
        // renders. Affects copy in the summary pill — "pick the
        // matching block below" makes no sense when there's only one.
        const devTypeKnown = !!snap.dev_cards_type_known;
        if (devHeld > 0) {
            // Header pill: total cards, broken down VP / non-VP when
            // we have any VPs (colonist's victoryPointsState reports
            // self's VP-dev count separately so we can subtract).
            // Non-VP cards are the only ones that can be PLAYED —
            // VP cards just sit in your hand contributing to victory.
            let pill = `<div class="dev-summary">`
                + `<span class="dev-summary-h">dev cards</span> `
                + `<b>${devHeld}</b>`;
            if (devVpHeld > 0 && devNonVp > 0) {
                pill += ` <span class="dev-summary-pl">`
                    + `(${devVpHeld} VP, ${devNonVp} playable)</span>`;
            } else if (devVpHeld > 0 && devNonVp === 0) {
                pill += ` <span class="dev-summary-pl">`
                    + `(all VP — nothing to play)</span>`;
            }
            if (devJust > 0) {
                pill += ` <span class="dev-summary-j">`
                    + `· ${devJust} just bought, play next turn`
                    + `</span>`;
            } else if (devPlayable > 0 && devVpHeld === 0
                       && !devTypeKnown) {
                // Only show "pick the matching block" when type
                // isn't decoded — when we know the type, only the
                // one matching block renders so there's nothing to
                // pick between.
                pill += ` <span class="dev-summary-pl">`
                    + `pick the matching block below</span>`;
            }
            pill += '</div>';
            devBlocks.push(pill);
        }
        // Per-hint headers drop the "×N" count when we're inferring
        // from playable_count (the dev_cards_held aggregate). Showing
        // "knight ×1, monopoly ×1, yop ×1, rb ×1" for a single card
        // of unknown type would be more confusing than helpful — the
        // count is on the dev-summary pill above. Type-specific
        // counters (catanatron's *_IN_HAND) only land in tests that
        // poke them directly, so the multiplier is mostly cosmetic.
        const showCount = devHeld === 0;
        if (snap.knight_hint && snap.knight_hint.have > 0) {
            const kh = snap.knight_hint;
            const verdictCls = kh.should_play ? 'play' : 'hold';
            const verdictLbl = kh.should_play ? 'PLAY' : 'HOLD';
            // Reason is now self-contained natural-language ("an opp
            // is close to LA — play to deny", "robber's on you — play
            // to clear it"), so no need for a stat tail. The bot's
            // top robber target is still in kh.best_target for the
            // post-play targets ranking; we don't surface it pre-play
            // any more (that gives away the plan to opps watching
            // a stream).
            const hintCls = kh.should_play
                ? 'knight-hint should-play' : 'knight-hint';
            const hdr = showCount
                ? `knight ×${kh.have}` : 'knight';
            devBlocks.push('<div class="' + hintCls + '">'
                + `<div class="kh-h">${hdr}</div>`
                + '<div class="kh-reason">'
                + `<span class="kh-verdict ${verdictCls}">${verdictLbl}</span>`
                + escapeHtml(kh.reason || '') + '</div>'
                + '</div>');
        }
        if (snap.monopoly_hint && snap.monopoly_hint.have > 0) {
            const mh = snap.monopoly_hint;
            const resLbl = iconFor(mh.resource);
            const verdictCls = mh.should_play ? 'play' : 'hold';
            const verdictLbl = mh.should_play ? 'PLAY' : 'HOLD';
            let body = `<span class="kh-verdict ${verdictCls}">${verdictLbl}</span>`
                + `target <b>${resLbl}</b> · ~${mh.est_steal} cards`;
            if (mh.unlock) {
                body += `<span class="dv-unlock">${escapeHtml(mh.unlock)}</span>`;
            }
            let sub = '';
            if (mh.top_holder && mh.top_holder.count > 0) {
                const th = mh.top_holder;
                // Always render the swatch — fall back to the
                // catanatron color hex when colonist hasn't surfaced
                // the seat's CSS color yet (early in the game). Pre-fix
                // this rendered as empty when display was missing,
                // making the "drains 4 from white" line read as
                // disembodied text with no player marker.
                const bg = th.display || COLOR_HEX[th.color] || '#888';
                const swatch = `<span class="mh-swatch" `
                    + `style="background:${escapeHtml(bg)}"></span>`;
                sub = `<div class="dv-sub">`
                    + swatch
                    + escapeHtml(`drains ${th.count} from ${th.color.toLowerCase()}`)
                    + '</div>';
            }
            const hintCls = mh.should_play
                ? 'dev-hint should-play' : 'dev-hint';
            const hdr = showCount
                ? `monopoly ×${mh.have}` : 'monopoly';
            devBlocks.push('<div class="' + hintCls + '">'
                + `<div class="dv-h">${hdr}</div>`
                + `<div class="dv-body">${body}${sub}</div>`
                + '</div>');
        }
        if (snap.yop_hint && snap.yop_hint.have > 0) {
            const yh = snap.yop_hint;
            const pair = (yh.pair || []).map(r => iconFor(r)).join(' + ');
            const verdictCls = yh.should_play ? 'play' : 'hold';
            const verdictLbl = yh.should_play ? 'PLAY' : 'HOLD';
            let body = `<span class="kh-verdict ${verdictCls}">${verdictLbl}</span>`
                + `pick <b>${pair}</b>`;
            if (yh.unlock) {
                body += `<span class="dv-unlock">unlocks ${escapeHtml(yh.unlock)}</span>`;
            }
            if (yh.bank_ok === false) {
                body += `<div class="dv-sub">`
                    + escapeHtml(yh.reason || 'bank short on pair') + '</div>';
            }
            const hintCls = yh.should_play
                ? 'dev-hint should-play' : 'dev-hint';
            const hdr = showCount
                ? `year of plenty ×${yh.have}` : 'year of plenty';
            devBlocks.push('<div class="' + hintCls + '">'
                + `<div class="dv-h">${hdr}</div>`
                + `<div class="dv-body">${body}</div>`
                + '</div>');
        }
        if (snap.rb_hint && (snap.rb_hint.have > 0
                || (snap.rb_hint.free_roads_pending || 0) > 0)) {
            const rh = snap.rb_hint;
            // Mid-RB (card played, free roads pending) gets a "PLACE"
            // verdict so the banner reads as a follow-through cue, not
            // a pre-play decision. PLAY/HOLD is the pre-play binary.
            const midRb = (rh.have <= 0
                && (rh.free_roads_pending || 0) > 0);
            const verdictLbl = midRb
                ? 'PLACE'
                : (rh.should_play ? 'PLAY' : 'HOLD');
            const verdictCls = midRb
                ? 'play'
                : (rh.should_play ? 'play' : 'hold');
            let body = `<div class="dv-body">`
                + `<span class="kh-verdict ${verdictCls}">${verdictLbl}</span>`
                + escapeHtml(rh.reason || '');
            if (rh.placement) {
                const pl = rh.placement;
                const towardHtml = (pl.toward_tiles
                        && pl.toward_tiles.length)
                    ? ` toward ${tilesToHtml(pl.toward_tiles)}`
                    : '';
                const labelPrefix = (pl.edges && pl.edges.length > 1)
                    ? 'road #1' : 'lay road';
                let sub = `<div class="dv-sub">`
                    + `<span class="dv-arrow">→</span>`
                    + labelPrefix + towardHtml;
                if (pl.placement_reason) {
                    sub += `<span class="dv-unlock">`
                        + escapeHtml(pl.placement_reason) + '</span>';
                }
                sub += '</div>';
                body += sub;
                // Road Building lays TWO free roads. Show the second
                // placement when the suggester picked one — without
                // this, the player sees "lay road toward 8 wheat" and
                // has to figure out the second placement themselves
                // even though the bot already evaluated it.
                if (pl.edges && pl.edges.length > 1) {
                    const secondToward = (pl.second_toward_tiles
                            && pl.second_toward_tiles.length)
                        ? ` toward ${tilesToHtml(pl.second_toward_tiles)}`
                        : '';
                    let sub2 = `<div class="dv-sub">`
                        + `<span class="dv-arrow">→</span>`
                        + 'road #2' + secondToward;
                    if (pl.second_placement_reason) {
                        sub2 += `<span class="dv-unlock">`
                            + escapeHtml(pl.second_placement_reason)
                            + '</span>';
                    }
                    sub2 += '</div>';
                    body += sub2;
                }
            }
            body += '</div>';
            const hintCls = rh.should_play
                ? 'dev-hint should-play' : 'dev-hint';
            // Header reads "road building (1 left)" mid-RB so it's
            // obvious the placement guidance is for the remaining
            // free road, not a pre-play hint.
            let hdr;
            if (midRb) {
                hdr = (rh.free_roads_pending > 1)
                    ? `road building (${rh.free_roads_pending} left)`
                    : 'road building (1 left)';
            } else {
                hdr = showCount
                    ? `road building ×${rh.have}` : 'road building';
            }
            devBlocks.push('<div class="' + hintCls + '">'
                + `<div class="dv-h">${hdr}</div>`
                + body
                + '</div>');
        }
        if (devBlocks.length) {
            // No section label — the dev-card hint cards carry their own
            // header (knight ×N, monopoly ×N) and stand out visually.
            parts.push(devBlocks.join(''));
        }
        // Robber targets — render INSIDE recs-flow so the ranking sits
        // right under the recommendations instead of below all the cards
        // (which forced Noah to scroll every time he played a knight or
        // rolled a 7). Only rendered on self's turn — the 'placed'
        // window from a previous self-turn shouldn't carry into the
        // next opp's turn (stale ranking). The bridge clears
        // robber_snapshot on the next non-7 roll, but turn transitions
        // can race past that, so double-gate here.
        if (snap.my_turn
            && (snap.robber_targets || []).length
            && (snap.robber_pending
                || snap.robber_reason === 'knight'
                || snap.robber_reason === 'placed')) {
            const rhTxt = snap.robber_reason === 'knight'
                ? 'knight → robber targets'
                : snap.robber_reason === 'placed'
                    ? 'robber placed · ranking'
                    : 'robber targets';
            parts.push(`<div class="robber-h">${rhTxt}</div>`);
            parts.push('<table class="robber">');
            for (let i = 0; i < snap.robber_targets.length; i++) {
                const t = snap.robber_targets[i];
                const tile = t.resource
                    ? `${iconFor(t.resource)}${t.number ?? ''}`
                    : 'DES';
                const victims = (t.victims || []).map(v => {
                    const bg = v.color_css || COLOR_HEX[v.color] || '#888';
                    const fg = contrastText(bg);
                    const star = v.suggested ? '★' : '';
                    const pill = `<span class="color-pill" style="background:${bg};`
                        + `color:${fg};font-size:calc(10px * var(--font-scale));${
                            v.suggested ? 'outline:2px solid #ffd36e;' : ''
                        }">${escapeHtml((v.color || '?').slice(0, 1))}</span>`;
                    const label = `${pill}${v.pips}p/${v.vp}vp/${v.cards}c`;
                    return v.suggested
                        ? `<span class="victim-top">${star}${label}</span>`
                        : label;
                }).join(' ') || '<span class="muted">—</span>';
                parts.push(`<tr>`
                    + `<td>${i + 1}.</td>`
                    + `<td>${tile}</td>`
                    + `<td>${t.score > 0 ? '+' : ''}${t.score}</td>`
                    + `<td>${victims}</td></tr>`);
            }
            parts.push('</table>');
        }
        // Close .recs-flow — everything above (recs, plan-ahead, long-game,
        // dev-card hints, robber-targets) is "what to do this turn" and
        // floats above the self/opps panels via CSS flex order.
        parts.push('</div>');
        if ((snap.opps || []).length) {
            parts.push('<div class="sec-h sec-opps">opponents</div>');
            parts.push('<div class="opps">');
            for (const o of snap.opps) {
                const bg = pillColor(o);
                const fg = contrastText(bg);
                const pill = `<span class="color-pill" style="background:${bg};`
                    + `color:${fg};">${escapeHtml(o.username)}</span>`;
                // Inferred hand breakdown + unknown remainder. The hand
                // comes from the tracker (produce + known trades + builds
                // etc). Unknown counts reflect 3rd-party steals and
                // closed-type discards where we know the count moved but
                // not the type. "?" is shown for unknown cards when there
                // are any, alongside the known resources.
                const handParts = [];
                const hand = o.hand || {};
                for (const [res, n] of Object.entries(hand)) {
                    if (n > 0) {
                        handParts.push(`<span>${iconFor(res)} ${n}</span>`);
                    }
                }
                if ((o.unknown || 0) > 0) {
                    handParts.push(`<span>? ${o.unknown}</span>`);
                }
                const breakdown = handParts.length
                    ? `<span class="opp-hand">${handParts.join('')}</span>`
                    : '';
                const trackCls = o.hand_tracked ? ' tracked' : '';
                // Hand-drift indicator. When an opp has 2+ unknown
                // cards (hidden steals we couldn't attribute) the
                // inferred breakdown is significantly wrong — every
                // 'they hold X' rec gating on it is shakier. Add a
                // small chip so Noah can mentally discount the read.
                // 1 unknown is normal noise; 2+ is the threshold where
                // the postmortem reconstruction also flags drift.
                const uncertainTag = (o.unknown || 0) >= 2
                    ? ` <span class="hand-uncertain" `
                    + `title="${o.unknown} unknown cards — `
                    + `inferred hand may be wrong">~${o.unknown}?</span>`
                    : '';
                // Dev-card tag: uniform grey at low VP, amber/bold
                // when the dev-stash could plausibly be hiding VPs
                // that push them to the win threshold.
                const devTag = (o.dev_cards || 0) > 0
                    ? (o.dev_stash_risk
                        ? ` · <span class="dev-stash">${o.dev_cards}dev🔒</span>`
                        : ` · ${o.dev_cards}dev`)
                    : '';
                // Played-knights counter — silent at 0, flags at 2+
                // (one away from largest army) so the overlay colors
                // pick that opp out of the list. Piece counts
                // ("Xs/Yc/Zr") removed — Catan's own UI shows them and
                // they read as clutter on the HUD.
                const kpTag = (o.knights_played || 0) > 0
                    ? ` · ${o.knights_played}k` : '';
                const hotKnight = (o.knights_played || 0) >= 2;
                const rowCls = hotKnight ? ' hot-knight' : '';
                // Per-opp expected cards per roll. Drives robber and
                // trade-block priority — compare across rows to pick
                // the strongest engine. Silent at 0 (setup / robbed
                // 'p' was short for per-roll production but read as
                // jargon. Inline as "0.42/roll" so it reads as plain
                // English without needing a glossary lookup.
                let prodTag = '';
                if (o.production && o.production.per_roll > 0) {
                    prodTag = ` · <span class="opp-prod" title="expected resources gained per dice roll">`
                        + `${o.production.per_roll.toFixed(2)}/roll</span>`;
                }
                // Opp ports — trade-partner signal. Drop the inline
                // "port:" prefix and the comma joins; that format read
                // as "port mountain, 3" when the ⛰️ ore icon failed to
                // render (or got read aloud). Use ⚓ as a stable label
                // glyph, then 2:1 chips per specific resource and 3:1
                // for generic. Silent when no ports.
                let opPortTag = '';
                if (Array.isArray(o.ports) && o.ports.length) {
                    const chips = o.ports.map(p => p === 'GENERIC'
                        ? '<span class="op-port">3:1</span>'
                        : `<span class="op-port">${iconFor(p)} 2:1</span>`
                    ).join('');
                    opPortTag = ` · <span class="op-ports">⚓ ${chips}`
                        + '</span>';
                }
                // Builds the inferred hand can already pay for. Skip
                // 'road' alone — too noisy, doesn't move VP on its own.
                // 'city' and 'settlement' are the real warning signs.
                let affordTag = '';
                if (Array.isArray(o.can_afford) && o.can_afford.length) {
                    const meaningful = o.can_afford.filter(
                        b => b !== 'road');
                    if (meaningful.length) {
                        affordTag = ` · <span class="can-afford">can: `
                            + `${meaningful.join(', ')}</span>`;
                    }
                }
                // 1-short: opp is one resource from city/settlement.
                // Dim-amber so it doesn't compete with can_afford
                // (which is already-flipped and more urgent). Only
                // surface when can_afford for this opp didn't claim
                // the same build. "?" suffix marks uncertain (unknown
                // cards could already cover it).
                let oneShortTag = '';
                if (o.one_short) {
                    const os = o.one_short;
                    const tail = os.uncertain ? '?' : '';
                    oneShortTag = ` · <span class="one-short">1 `
                        + `${iconFor(os.need)} → `
                        + `${os.build}${tail}</span>`;
                }
                // Fat-hand marker: opps over the discard cliff are primed
                // for a 7-roll — they lose half AND are likely steal
                // targets. Two below the cliff is amber "watch" — same
                // logic as the self row, kept symmetric so the eye reads
                // both sides with one rule. (See self-row note for why
                // it's > limit, not >= limit.)
                const oppDiscardLimit = snap.discard_limit || 7;
                const oppCardCount = o.cards || 0;
                const oppCardTier = oppCardCount > oppDiscardLimit ? 'fat-hand'
                    : (oppCardCount > oppDiscardLimit - 2 ? 'watch-hand' : '');
                let cardsSpan = oppCardTier
                    ? `<span class="${oppCardTier}">${o.cards}c</span>`
                    : `${o.cards}c`;
                // Hand-growth trailer: +3 means accumulating, -2 means
                // just spent/got-stolen-from. Only surface when abs>=2
                // because +1 is ambient and would noise the row out.
                if (typeof o.card_delta === 'number'
                        && Math.abs(o.card_delta) >= 2) {
                    const sign = o.card_delta > 0 ? '+' : '';
                    const cls = o.card_delta > 0 ? 'card-up' : 'card-dn';
                    cardsSpan += ` <span class="${cls}">(${sign}${o.card_delta})</span>`;
                }
                // VP and card count own their own visual weight — those
                // are the two highest-priority signals per opp (close to
                // winning + discard/steal target). Tier off the live
                // VP target so a VP-12 mode pushes the danger line out
                // to 10 instead of red-flagging 8 (which is mid-game in
                // longer modes). Defaults to the standard 10-VP target.
                const vpTarget = snap.vp_target || 10;
                const vpCls = o.vp >= vpTarget - 2 ? 'opp-vp danger'
                    : (o.vp >= vpTarget - 4 ? 'opp-vp watch' : 'opp-vp');
                const vpHtml = `<span class="${vpCls}">${o.vp}`
                    + '<span class="lbl">VP</span></span>';
                // Strip the leading " · " each conditional tag carries
                // and rejoin with a single separator. Prevents a stray
                // leading dot when the first applicable tag is missing.
                const mutedTags = [devTag, kpTag, prodTag, opPortTag]
                    .map(t => t.replace(/^ · /, ''))
                    .filter(Boolean)
                    .join(' · ');
                const mutedHtml = mutedTags
                    ? ` <span class="muted">· ${mutedTags}</span>` : '';
                parts.push(`<div class="opp${trackCls}${rowCls}">${pill}`
                    + ` ${vpHtml} <span class="opp-cards">${cardsSpan}</span>`
                    + uncertainTag
                    + mutedHtml
                    + `${affordTag}${oneShortTag}`
                    + (breakdown ? ` ${breakdown}` : '')
                    + `</div>`);
            }
            parts.push('</div>');
        }
        if (snap.incoming_trade) {
            const t = snap.incoming_trade;
            const bg = t.offerer_color_css
                || COLOR_HEX[t.offerer_color] || '#888';
            const fg = contrastText(bg);
            const offererPill = t.offerer
                ? `<span class="color-pill" style="background:${bg};`
                    + `color:${fg};">${escapeHtml(t.offerer)}</span> `
                : '';
            // Pack -> "🧱 1 🐑 2" for both sides of the swap.
            const fmtSide = (pack) => {
                const keys = Object.keys(pack || {});
                if (!keys.length) return '∅';
                return keys
                    .filter(r => pack[r] > 0)
                    .map(r => `${iconFor(r)} ${pack[r]}`)
                    .join(' ');
            };
            const verdictCls = ['accept', 'decline', 'consider']
                .includes(t.verdict) ? t.verdict : 'consider';
            const verdictLabel = verdictCls.toUpperCase();
            parts.push(`<div class="trade-offer verdict-${verdictCls}">`);
            // Verdict is the headline — what's the bot saying to do.
            // Promote it to the top of the banner so the eye lands on
            // ACCEPT/DECLINE/CONSIDER first, then reads the deal terms.
            parts.push('<div class="trade-h">'
                + `<span class="verdict ${verdictCls}">${verdictLabel}</span>`
                + `<span class="trade-meta">from ${offererPill}`
                + `<span class="muted">${t.offerer_vp ?? 0} VP</span></span>`
                + '</div>');
            parts.push('<div class="trade-body">'
                + '<span class="swap-side">gives ' + fmtSide(t.give)
                + '</span><span class="swap-arrow">↔</span>'
                + '<span class="swap-side">wants ' + fmtSide(t.want)
                + '</span></div>');
            if (t.reason) {
                parts.push('<div class="trade-reason">'
                    + escapeHtml(t.reason) + '</div>');
            }
            if (t.counter) {
                // Counter-offer is a fairer version we'd actually accept.
                // Show give→want like the main offer so Noah can type it in.
                parts.push('<div class="counter">'
                    + '<span class="counter-h">counter:</span>'
                    + '<span class="swap-side">ask '
                    + fmtSide(t.counter.give)
                    + '</span><span class="swap-arrow">↔</span>'
                    + '<span class="swap-side">for '
                    + fmtSide(t.counter.want)
                    + '</span>'
                    + (t.counter.reason
                        ? `<span class="counter-reason">`
                            + escapeHtml(t.counter.reason) + `</span>`
                        : '')
                    + '</div>');
            }
            parts.push('</div>');
        }
        if (snap.last_roll) {
            // No section header — the last-roll banner is a chunky color
            // block already; a label on top of it just adds chrome.
            const lr = snap.last_roll;
            let who;
            if (lr.is_you) {
                who = `you rolled <b>${lr.total}</b>`;
            } else if (lr.player) {
                who = `${escapeHtml(lr.player)} rolled ${lr.total}`;
            } else if (lr.color) {
                who = `${escapeHtml(lr.color.toLowerCase())} rolled ${lr.total}`;
            } else {
                who = `rolled <b>${lr.total}</b>`;
            }
            // Yield breakdown: what self actually received, and what
            // the robber blocked. Skips silently when yield is missing
            // (7-roll or compute failure) or wholly empty (no exposure).
            let yieldLine = '';
            const y = lr.yield;
            if (y) {
                const gPairs = Object.entries(y.gained || {})
                    .filter(([_, n]) => n > 0);
                const bPairs = Object.entries(y.blocked || {})
                    .filter(([_, n]) => n > 0);
                const gained = gPairs.length
                    ? gPairs.map(([r, n]) => `${iconFor(r)} +${n}`).join(' ')
                    : '';
                const blocked = bPairs.length
                    ? ' <span class="roll-blocked">blocked: '
                        + bPairs.map(([r, n]) => `${iconFor(r)} ${n}`).join(' ')
                        + '</span>'
                    : '';
                if (gained || blocked) {
                    yieldLine = ' <span class="roll-yield">'
                        + gained + blocked + '</span>';
                } else if (lr.total !== 7) {
                    // Explicit "nothing" so Noah isn't wondering whether
                    // the pipeline broke or the roll just missed him.
                    yieldLine = ' <span class="roll-yield muted">—</span>';
                }
            }
            parts.push(`<div class="roll ${lr.is_you ? 'you-rolled' : ''}">`
                + `${who}${yieldLine}</div>`);
            // Opponent-yields on the same roll. Compact dim sub-line
            // answering "did that feed somebody else?" Important on
            // rolls where self got nothing — otherwise the banner
            // reads "rolled 8, —" and hides the fact that an opp just
            // scooped 4 cards. Blocked counts are surfaced parenthetic.
            const oys = lr.opponent_yields;
            if (Array.isArray(oys) && oys.length) {
                const parts2 = oys.map((o) => {
                    const g = o.gained_total > 0
                        ? `${escapeHtml((o.color || '').toLowerCase())} +${o.gained_total}`
                        : escapeHtml((o.color || '').toLowerCase());
                    const b = o.blocked_total > 0
                        ? ` <span class="oy-blk">(${o.blocked_total} blk)</span>`
                        : '';
                    return g + b;
                }).join(' · ');
                parts.push(`<div class="opp-yields">they: ${parts2}</div>`);
            }
        }
        // Roll distribution removed entirely. Last-roll info is on
        // the banner already; the chart will return when there's a
        // design that actually works (animated + readable).
        // Yield summary: actual vs expected cards across the roll
        // window. Flags "behind" when expected is clearly above actual,
        // i.e. dice droughts or the robber have cost us.
        const ys = snap.yield_summary;
        if (ys && ys.window > 0) {
            const behind = (ys.expected - ys.got) > 0.3 * ys.expected
                && ys.expected > 1.0;
            const blockedFrag = ys.blocked > 0
                ? `<span class="ys-sep">·</span>blocked ${ys.blocked}`
                : '';
            parts.push(`<div class="yield-sum ${behind ? 'behind' : ''}">`
                + `got ${ys.got}/${ys.expected} (${ys.window} rolls)`
                + blockedFrag
                + '</div>');
        }
        // Removed: prod-stall, sevens-hot, hot-numbers banners.
        // All three were info-only — they told Noah something was
        // happening but didn't change his next move. With 5+ banners
        // stacking the bottom of the HUD became unreadable noise.
        // Bridge still computes the data (snap.production_stall,
        // snap.sevens_hot, snap.hot_numbers); postmortems can surface
        // them. The live HUD just doesn't.
        if (snap.threat && snap.threat.message) {
            const lvl = snap.threat.level || 'mid';
            parts.push(`<div class="threat ${lvl}">`
                + `<span class="b-ico">⚠️</span> `
                + escapeHtml(snap.threat.message)
                + '</div>');
        }
        // 3rd-settle milestone — biggest pre-mid-game predictor of
        // winning per the Reddit 36k-game data (winners build #3
        // ~7 turns earlier than losers; 10.9% of losers never build
        // it). Backend fires snap.milestone whenever footprint == 2
        // past turn 5; here we render it with the resource deficit
        // so Noah sees what to aim for.
        if (snap.milestone && snap.milestone.kind === 'third_settle') {
            const ms = snap.milestone;
            const missingFrag = Object.entries(ms.missing || {})
                .filter(([, n]) => n > 0)
                .map(([r, n]) => `${iconFor(r)} ${n}`)
                .join(' + ');
            const missingTail = missingFrag
                ? ` · need ${missingFrag}`
                : ' · ready to build';
            parts.push(`<div class="milestone third-settle">`
                + `<span class="b-ico">🏗</span> `
                + `<span class="ms-head">${escapeHtml(ms.headline)}</span>`
                + `<span class="ms-tail">${missingTail}</span>`
                + '</div>');
        }
        // Self close-to-win banner — symmetric with snap.threat but
        // fires on self VP hitting the close threshold.
        if (snap.win_proximity && snap.win_proximity.message) {
            const wlvl = snap.win_proximity.level || 'close';
            parts.push(`<div class="win-prox ${wlvl}">`
                + `<span class="b-ico">👑</span> `
                + escapeHtml(snap.win_proximity.message)
                + '</div>');
        }
        if (snap.robber_on_me) {
            const rom = snap.robber_on_me;
            const tileLbl = `${iconFor(rom.resource)}${rom.number || ''}`;
            const nBuilds = rom.buildings;
            const subParts = [];
            subParts.push(nBuilds > 1
                ? `${nBuilds} buildings blocked`
                : (rom.has_city ? 'city blocked' : 'settlement blocked'));
            // Recent cost tally: how many of the last N non-7 rolls
            // actually hit this blocked tile. Zero is meaningful too
            // — it means the robber's there but hasn't bitten yet.
            if (rom.rolls_recent != null && rom.blocks_recent != null
                && rom.rolls_recent > 0) {
                subParts.push(
                    `lost ${rom.blocks_recent}/${rom.rolls_recent} recent`);
            }
            // Persistence: how many rolls ago the robber landed here.
            // Complements the cost tally — 0 blocks over 5 rolls means
            // "stuck here but lucky so far", while a fresh placement
            // with 0 blocks just means "nobody's rolled the number yet".
            if (rom.rolls_since_placed != null) {
                const n = rom.rolls_since_placed;
                subParts.push(n === 0
                    ? 'just placed'
                    : `placed ${n} ${n === 1 ? 'roll' : 'rolls'} ago`);
            }
            parts.push('<div class="robber-on-me">');
            // Head line: tile + expected card loss per roll (probability-
            // weighted). Raw pip count is kept as a parenthetical for
            // players who still want the pip read, but the headline
            // number is in cards so it translates to impact intuitively.
            let headExtra = '';
            if (typeof rom.expected_per_roll === 'number'
                && rom.expected_per_roll > 0) {
                headExtra = ` · ${rom.expected_per_roll.toFixed(2)}/roll lost`;
                if (typeof rom.expected_lost_total === 'number'
                        && rom.expected_lost_total > 0.05) {
                    headExtra += ` (~${rom.expected_lost_total.toFixed(1)}`
                        + ' cards bled)';
                }
            } else {
                // Translate raw pip count to cards-per-roll so it
                // reads as "the robber blocks ~0.42 cards each roll"
                // instead of jargon nobody outside Catan boards
                // understands. /36 because a pip is 1/36 odds.
                const cpr = (rom.pips_blocked || 0) / 36;
                headExtra = ` · blocking ~${cpr.toFixed(2)} cards/roll`;
            }
            parts.push(`<span class="b-ico">🚫</span> `
                + `robber on your ${tileLbl}${headExtra}`);
            parts.push(`<span class="rom-sub">${escapeHtml(subParts.join(' · '))}</span>`);
            parts.push('</div>');
        }
        if (snap.longest_road_race) {
            const lr = snap.longest_road_race;
            const lvl = lr.level || 'contested';
            parts.push(`<div class="lr-race ${lvl}">`
                + `<span class="b-ico">🛣️</span> `
                + escapeHtml(lr.message || '')
                + '</div>');
        }
        if (snap.largest_army_race) {
            const la = snap.largest_army_race;
            const lvl = la.level || 'contested';
            parts.push(`<div class="la-race ${lvl}">`
                + `<span class="b-ico">⚔️</span> `
                + escapeHtml(la.message || '')
                + '</div>');
        }
        // Removed: bank-low and dev-deck banners. Bank running low on
        // a resource almost never changes Noah's call (he just trades
        // 3:1 instead of 4:1) and the dev-deck count belongs in the
        // postmortem rather than competing for HUD real estate.
        if (snap.discard_hint && snap.discard_hint.need > 0) {
            const dh = snap.discard_hint;
            const dropText = Object.entries(dh.drop)
                .map(([res, n]) => `${iconFor(res)} ${n}`)
                .join(' · ');
            parts.push('<div class="discard-hint">');
            parts.push(`<div class="dh-h">`
                + `<span class="b-ico">🎲</span> discard ${dh.need}</div>`);
            parts.push(`<div class="dh-drops">${dropText}</div>`);
            if (dh.rationale) {
                parts.push(`<div class="dh-reason">${escapeHtml(dh.rationale)}</div>`);
            }
            parts.push('</div>');
        }
        // Pre-roll spend-down warning. Distinct from the reactive
        // discard_hint above (which fires AFTER a 7). This one fires
        // PROACTIVELY at 9+ cards so Noah can spend down before a 7
        // costs him 4-5 cards. Suppressed during/after a real discard
        // (when discard_hint is active) so the two banners don't
        // double up.
        if (snap.seven_prep && !snap.discard_hint) {
            const sp = snap.seven_prep;
            const dropText = Object.entries(sp.would_drop || {})
                .map(([res, n]) => `${iconFor(res)} ${n}`)
                .join(' · ');
            const cls = sp.level === 'danger'
                ? 'seven-prep danger'
                : 'seven-prep';
            parts.push(`<div class="${cls}">`);
            parts.push(`<div class="sp-h">`
                + `<span class="b-ico">⚠</span> ${escapeHtml(sp.message)}`
                + `</div>`);
            parts.push(`<div class="sp-drops">would lose: ${dropText}</div>`);
            parts.push('</div>');
        }
        // Persistent multi-turn plan banner. Sticks across polls so
        // Noah sees a north star even as individual recs reshuffle.
        // Hidden when no plan is locked (plan is None server-side) —
        // the recommender already surfaces affordable builds, no need
        // to compete with them.
        if (snap.plan && snap.plan.summary) {
            const plan = snap.plan;
            const ready = plan.ready ? ' ready' : '';
            const turnsHeld = plan.turns_held > 1
                ? ` <span class="muted">held ${plan.turns_held}</span>`
                : '';
            const progressParts = [];
            for (const [res, p] of Object.entries(plan.progress || {})) {
                const have = p.have || 0;
                const need = p.need || 0;
                const cls = have >= need ? 'pos' : 'fg-mute';
                progressParts.push(
                    `<span class="${cls}">${have}/${have + need} `
                    + `${iconFor(res)}</span>`);
            }
            parts.push(`<div class="plan${ready}">`);
            parts.push(`<div class="plan-h">`
                + `<span class="b-ico">🎯</span> PLAN`
                + turnsHeld
                + `</div>`);
            parts.push(`<div class="plan-summary">${escapeHtml(plan.summary)}</div>`);
            if (progressParts.length) {
                parts.push(`<div class="plan-progress">`
                    + progressParts.join(' · ') + `</div>`);
            }
            parts.push('</div>');
        }
        ui.content.innerHTML = parts.join('');
        renderHistogram(ui, snap);
        renderEvalGraph(ui, snap);
        renderMoveQuality(ui, snap);
        renderDevDeckByType(ui, snap);
    }

    // Dev-deck remaining-by-type strip — base deck count minus
    // PLAYED_{type} summed across all seats. Tells Noah at a glance
    // whether knights are scarce (LA contestability), monopolies are
    // depleted (no more big-pot risk), etc. Lives at the very bottom
    // of the HUD per Noah's 2026-05-02 ask. VP cards intentionally
    // dropped — they sit hidden in hands and never log a "played"
    // action so we can't infer remaining from plays.
    function renderDevDeckByType(ui, snap) {
        if (!ui || !ui.devDeckHost) return;
        const dd = (snap && snap.dev_deck) || null;
        const byType = (dd && dd.by_type) || null;
        if (!byType) {
            ui.devDeckHost.classList.add('hidden');
            return;
        }
        ui.devDeckHost.classList.remove('hidden');
        const ORDER = ['KNIGHT', 'MONOPOLY', 'ROAD_BUILDING',
                       'YEAR_OF_PLENTY'];
        const LABEL = {
            KNIGHT: 'kn', MONOPOLY: 'mn',
            ROAD_BUILDING: 'rb', YEAR_OF_PLENTY: 'yop',
        };
        const cells = [];
        for (const type of ORDER) {
            const t = byType[type];
            if (!t) continue;
            const remaining = Number(t.remaining || 0);
            const base = Number(t.base || 0);
            const cls = remaining === 0 ? 'dev-cell out'
                : remaining <= 1 ? 'dev-cell low' : 'dev-cell';
            cells.push(
                `<span class="${cls}" title="${type.toLowerCase()} `
                + `· ${remaining} of ${base} remaining (in deck or held)">`
                + `<span class="dev-lbl">${LABEL[type]}</span>`
                + `<span class="dev-num">${remaining}</span>`
                + '</span>');
        }
        ui.devDeck.innerHTML = cells.join('');
    }

    // 36-roll baseline weights — number of dice combos that produce
    // each total. 7 is excluded because the bar wraps that case in CSS;
    // the column's still rendered for hot-7 alarming, but the expected
    // tick would just say "yes, 7s happen" which isn't actionable.
    const HIST_WEIGHTS = {
        2: 1, 3: 2, 4: 3, 5: 4, 6: 5,
        8: 5, 9: 4, 10: 3, 11: 2, 12: 1,
    };

    function renderHistogram(ui, snap) {
        if (!ui || !ui.hist || !ui.histHost) return;
        const hg = (snap && snap.roll_histogram) || null;
        const total = (snap && snap.total_rolls) || 0;
        if (!hg || total <= 0) {
            ui.histHost.classList.add('hidden');
            return;
        }
        ui.histHost.classList.remove('hidden');
        if (ui.histTotal) ui.histTotal.textContent = String(total);
        let max = 1;
        for (let n = 2; n <= 12; n++) {
            const c = Number(hg[n] || 0);
            if (c > max) max = c;
        }
        const lastTotal = (snap.last_roll && snap.last_roll.total) || null;
        const cols = ui.hist.children;
        for (let i = 0; i < cols.length; i++) {
            const col = cols[i];
            const n = Number(col.dataset.n);
            const c = Number(hg[n] || 0);
            const pct = (c / max) * 100;
            const bar = col.querySelector('[data-bar]');
            const cnt = col.querySelector('[data-count]');
            const exp = col.querySelector('[data-exp]');
            if (bar) bar.style.height = pct + '%';
            if (cnt) cnt.textContent = c > 0 ? String(c) : '';
            // Expected tick: % of bar height where this column would sit
            // if dice obeyed the 36-roll baseline. Same denominator (max)
            // as the actual bar so the two are directly comparable.
            if (exp) {
                const w = HIST_WEIGHTS[n];
                if (w) {
                    const expectedCount = total * w / 36;
                    const expPct = Math.min(
                        100, (expectedCount / max) * 100);
                    exp.style.bottom = expPct + '%';
                    exp.style.display = '';
                } else {
                    exp.style.display = 'none';
                }
            }
            col.classList.toggle('last', n === lastTotal);
        }
    }

    // Eval-graph rendering (HUD principle #6). Walks `eval_history`
    // (per-roll {roll, eval} samples) and updates the persistent SVG
    // path. Y-axis is signed: positive eval (above zero line) = self
    // ahead, negative = behind. Domain auto-fits [min, max] with a
    // ±10 floor so a flat opening doesn't squish into a single line.
    // Hidden until 2+ samples land — one point isn't a sparkline.
    function renderEvalGraph(ui, snap) {
        if (!ui || !ui.evalHost || !ui.evalLine) return;
        const eh = (snap && snap.eval_history) || [];
        if (!Array.isArray(eh) || eh.length < 2) {
            ui.evalHost.classList.add('hidden');
            return;
        }
        ui.evalHost.classList.remove('hidden');
        const W = 200, H = 56, MID = H / 2;
        // Symmetric domain so the zero line stays at MID. Floor at ±10
        // so a near-flat opening still draws something visible.
        let absMax = 10;
        for (const e of eh) {
            const v = Number(e.eval);
            if (Number.isFinite(v) && Math.abs(v) > absMax) {
                absMax = Math.abs(v);
            }
        }
        // Expand a tiny margin so the line doesn't kiss the top/bottom.
        const dom = absMax * 1.08;
        const n = eh.length;
        const xStep = n > 1 ? W / (n - 1) : 0;
        const pts = [];
        for (let i = 0; i < n; i++) {
            const v = Number(eh[i].eval) || 0;
            const x = i * xStep;
            const y = MID - (v / dom) * MID;
            pts.push([x, Math.max(0, Math.min(H, y))]);
        }
        // Line path.
        const linePath = pts.map(
            (p, i) => (i === 0 ? `M${p[0].toFixed(1)},${p[1].toFixed(1)}`
                                : `L${p[0].toFixed(1)},${p[1].toFixed(1)}`)
        ).join(' ');
        ui.evalLine.setAttribute('d', linePath);
        // Fill region between line and the zero baseline. Drawn in
        // a single path that closes back to MID — coloured by the
        // sign of the most-recent sample so the strongest visual
        // cue (current state) drives the fill colour.
        const last = pts[pts.length - 1];
        const fillPath = linePath
            + ` L${last[0].toFixed(1)},${MID.toFixed(1)}`
            + ` L${pts[0][0].toFixed(1)},${MID.toFixed(1)} Z`;
        if (ui.evalFill) {
            ui.evalFill.setAttribute('d', fillPath);
            const lastV = Number(eh[eh.length - 1].eval) || 0;
            ui.evalFill.classList.toggle('eval-fill-pos', lastV >= 0);
            ui.evalFill.classList.toggle('eval-fill-neg', lastV < 0);
        }
        // Last-sample dot — the chess "current eval" anchor.
        if (ui.evalDot) {
            ui.evalDot.setAttribute('cx', last[0].toFixed(1));
            ui.evalDot.setAttribute('cy', last[1].toFixed(1));
            const lastV = Number(eh[eh.length - 1].eval) || 0;
            ui.evalDot.classList.toggle('pos', lastV >= 5);
            ui.evalDot.classList.toggle('neg', lastV <= -5);
        }
        if (ui.evalCur) {
            const v = Number(eh[eh.length - 1].eval) || 0;
            const sign = v > 0 ? '+' : '';
            ui.evalCur.textContent = `${sign}${v.toFixed(0)}`;
            ui.evalCur.classList.toggle('pos', v >= 5);
            ui.evalCur.classList.toggle('neg', v <= -5);
            ui.evalCur.classList.toggle(
                'neutral', v > -5 && v < 5);
        }
    }

    // Move-quality strip (HUD principle #7). Reads `move_history`
    // from the snapshot — one entry per post-setup self build. Each
    // entry carries a chess-style classification (!!, !, ?!, ?, ??)
    // computed by the bridge against its top-10 recs at decision
    // time, plus the bot's top rec for an optional "you played X,
    // top was Y" diff and a search_delta_gap pill (EV left on the
    // table when sim eval is available). Hidden until at least one
    // graded build lands.
    const MQ_BAND = {
        '!!': { cls: 'top',  label: '!!' },
        '!':  { cls: 'good', label: '!'  },
        '?!': { cls: 'ok',   label: '?!' },
        '?':  { cls: 'weak', label: '?'  },
        '??': { cls: 'bad',  label: '??' },
    };

    function _mqLocLabel(piece, loc) {
        // Settlements/cities use the node id; roads use the unordered
        // edge endpoints. Match how the rec list reads tiles so the
        // diff line looks consistent with what the player just saw.
        if (piece === 'road') {
            if (Array.isArray(loc) && loc.length >= 2) {
                const a = Math.min(loc[0], loc[1]);
                const b = Math.max(loc[0], loc[1]);
                return `${a}-${b}`;
            }
            return '?';
        }
        return loc == null ? '?' : String(loc);
    }

    function renderMoveQuality(ui, snap) {
        if (!ui || !ui.mqHost || !ui.mqTally || !ui.mqLast) return;
        const mh = (snap && snap.move_history) || [];
        if (!Array.isArray(mh) || mh.length === 0) {
            ui.mqHost.classList.add('hidden');
            return;
        }
        ui.mqHost.classList.remove('hidden');

        // Tally: count by classification across the (up to last 30)
        // graded builds. Show every band that's >0 so a 0-blunder
        // game doesn't carry a misleading "??: 0" pill.
        const counts = { '!!': 0, '!': 0, '?!': 0, '?': 0, '??': 0 };
        let nograde = 0;
        for (const e of mh) {
            const c = e && e.classification;
            if (c && counts.hasOwnProperty(c)) counts[c] += 1;
            else nograde += 1;
        }
        const tallyParts = [];
        for (const k of ['!!', '!', '?!', '?', '??']) {
            if (counts[k] === 0) continue;
            const band = MQ_BAND[k];
            tallyParts.push(
                `<span class="mq-t">`
                + `<span class="mq-tag ${band.cls}">${band.label}</span>`
                + `<span class="mq-c">${counts[k]}</span>`
                + `</span>`);
        }
        if (nograde > 0) {
            tallyParts.push(
                `<span class="mq-t">`
                + `<span class="mq-tag none">·</span>`
                + `<span class="mq-c">${nograde}</span>`
                + `</span>`);
        }
        ui.mqTally.innerHTML = tallyParts.join('');

        // Last-build line: badge + "settle 9" + optional "vs top
        // road 1-2" diff when the bot's top pick was something
        // different + optional EV gap pill.
        const last = mh[mh.length - 1];
        const piece = escapeHtml(last.piece || '?');
        const loc = _mqLocLabel(last.piece, last.loc);
        const lastBits = [];
        const cls = last.classification;
        if (cls && MQ_BAND[cls]) {
            const band = MQ_BAND[cls];
            lastBits.push(
                `<span class="mq-badge ${band.cls}">${band.label}</span>`);
        } else {
            lastBits.push(
                `<span class="mq-badge none" title="no recs cached at`
                + ` decision time">·</span>`);
        }
        lastBits.push(
            `<span class="mq-piece">${piece} ${escapeHtml(loc)}</span>`);

        // "you played X, top was Y" diff. Only show when the actual
        // wasn't the top pick — same-rec-top is the "!!" case and
        // doesn't need a diff.
        if (last.rank !== 1 && last.top_kind) {
            const topLoc = _mqLocLabel(last.top_kind, last.top_loc);
            lastBits.push(`<span class="mq-vs">vs</span>`);
            lastBits.push(
                `<span class="mq-top">${escapeHtml(last.top_kind)}`
                + ` ${escapeHtml(topLoc)}</span>`);
        }

        // EV gap pill: how much eval the player left on the table by
        // skipping the top rec. Only meaningful when the bridge had
        // search_delta on both recs; bridge sets sd_gap=null otherwise.
        const gap = last.search_delta_gap;
        if (typeof gap === 'number' && gap > 0.05) {
            lastBits.push(
                `<span class="mq-gap" title="how much better the top-ranked move would have scored vs. what you played">`
                + `−${gap.toFixed(1)} pts</span>`);
        }
        ui.mqLast.innerHTML = lastBits.join(' ');
    }

    function escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function startAdvisorPoll() {
        let ui = mountOverlay();
        if (!ui) {
            // document.body not there yet — @run-at document-start fires
            // before the DOM is built on colonist. Keep retrying; every
            // tick is cheap and mountOverlay is idempotent once the host
            // exists.
            setTimeout(startAdvisorPoll, 200);
            return;
        }
        let lastSeq = -1;
        let lastSnap = null;
        const tick = () => {
            // Re-grab the ui handle every tick in case the host element
            // got nuked (colonist occasionally wipes the DOM between
            // lobby and game views). mountOverlay is a no-op if already
            // present, a full rebuild if not.
            ui = mountOverlay() || ui;
            getJson(BRIDGE_ADVISOR_URL).then((snap) => {
                if (snap && snap.seq !== lastSeq) {
                    lastSeq = snap.seq;
                    lastSnap = snap;
                    latestAdvisorSnap = snap;
                    renderOverlay(ui, snap, true);
                } else if (!lastSnap) {
                    renderOverlay(ui, snap, true);
                    if (snap) latestAdvisorSnap = snap;
                }
            }).catch(() => {
                renderOverlay(ui, lastSnap, false);
            });
        };
        tick();
        setInterval(tick, ADVISOR_POLL_MS);
        // Push-style refresh hook for the /ws forwarder. Debounced
        // ~30ms so a burst of WS frames (one diff per game-state
        // delta — colonist clusters them) coalesces into a single
        // /advisor fetch instead of N back-to-back hits. Periodic
        // tick stays as a safety net.
        let _refreshTimer = null;
        triggerAdvisorRefresh = () => {
            if (_refreshTimer) return;
            _refreshTimer = setTimeout(() => {
                _refreshTimer = null;
                tick();
            }, 30);
        };
    }


    // Boot the advisor polling loop. The userscript called this from
    // an init block tied to document-start; in the extension panel
    // the document is already ready when this script runs, so just
    // invoke directly.
    startAdvisorPoll();
})();
