// CatanBot in-page HUD: takes over colonist.io's right-hand log column.
//
// Goal: stop needing the Chrome side panel. A native-beige HUD card + a
// [Log | CatanBot] tab bar are injected into colonist's beige log container
// (found by content.js's findLogContainer). The CatanBot tab shows the HUD
// and hides the native log; the Log tab flips back. A "replace" setting hides
// the tab bar and shows the HUD only.
//
// Runs in the SAME isolated world as content.js (one content_scripts entry),
// so it shares window.__catanbot: content.js owns the selectors + drives
// re-anchoring; this file owns the node + render. P1 here is structure only
// (tab bar + placeholder body); P2 wires the /advisor render.
//
// Gated behind localStorage 'catanbot.log_hud' (default OFF) so it never
// disrupts until Noah flips it on. Streamer-mode safe: every HUD node is
// stamped with content.js's skip flag so the username sweep leaves it alone.
(function bootCatanbotLogHud() {
    'use strict';
    const LOG_PREFIX = '[catanbot-loghud]';

    // localStorage keys (colonist.io origin).
    const LS_ON = 'catanbot.log_hud';       // '1' = HUD on
    const LS_REPLACE = 'catanbot.loghud.replace'; // '1' = no tab bar, HUD only
    const LS_TAB = 'catanbot.loghud.tab';    // 'catanbot' | 'log'

    // content.js's streamer skip flag (element.dataset.cataanonymized). Any
    // truthy value makes content.js's username sweep skip the node, so the
    // HUD's own colored name pills are never rewritten.
    const STREAMER_FLAG = 'cataanonymized';

    const ROOT_ID = 'cbo-loghud';
    const TABS_ID = 'cbo-loghud-tabs';
    const STYLE_ID = 'cbo-loghud-style';
    const POLL_MS = 1000;   // half the side panel cadence; HUD shows less

    let root = null;   // HUD body element
    let tabs = null;   // tab-bar element
    let _noContainer = 0;   // consecutive failed anchor finds
    let _failsafe = false;  // floating-overlay fallback already tripped
    let _everConnected = false;  // a successful /advisor fetch has happened
    let _lastSnap = null;   // most recent /advisor snapshot (for hover reads)
    let _tip = null;        // hover tooltip for per-player reads
    let _tradeAnchored = false;  // trade badge is pinned to colonist's panel

    // ---- Slim render helpers. Ported from overlay.js so the in-page read
    // matches the side panel exactly (same snapshot fields, same "wood 2
    // +67%" hand format). TODO: extract these + overlay.js's copies into a
    // shared utility.js to kill the duplication once the HUD stabilizes. ----
    const RES_EMOJI = {
        WOOD: '\u{1F332}', BRICK: '\u{1F9F1}', SHEEP: '\u{1F411}',
        WHEAT: '\u{1F33E}', ORE: '\u{1FAA8}',
    };
    const RES_ABBREV = {
        WOOD: 'wd', BRICK: 'br', SHEEP: 'sh', WHEAT: 'wh', ORE: 'or',
    };
    const COLOR_HEX = {
        RED: '#d24a43', BLUE: '#3b7dd8', WHITE: '#d8d8d8', ORANGE: '#e08a30',
        GREEN: '#46a45a', BROWN: '#8a6240',
    };
    const KIND_LABEL = {
        city: 'CITY', settlement: 'SETTLE', road: 'ROAD', dev_card: 'DEV',
        buy_dev: 'DEV', knight: 'KNIGHT', trade: 'TRADE',
        propose_trade: 'TRADE', discard: 'DISCARD',
        opening_settlement: 'SETTLE',
    };
    function iconFor(res) {
        return RES_EMOJI[res] || RES_ABBREV[res] || String(res || '?').slice(0, 2);
    }
    function escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    function pillColor(p) {
        if (p && p.color_css) return p.color_css;
        if (p && p.color) return COLOR_HEX[p.color] || '#888';
        return '#888';
    }
    function tilesText(arr) {
        return (arr || [])
            .filter((t) => t && t[0] !== 'DESERT')
            .map((t) => (t[1] == null ? iconFor(t[0]) : `${t[1]}${iconFor(t[0])}`))
            .join(' ');
    }
    function topRecHtml(rec) {
        if (!rec) return '';
        const kind = (rec.action === 'road') ? 'road' : rec.kind;
        const kindLabel = KIND_LABEL[kind] || String(kind || '').replace(/_/g, ' ');
        // For an opening-road follow-up the rec is kind=opening_settlement,
        // action=road: the SETTLEMENT is already down, so show the ROAD's own
        // tiles (rec.road.edge_tiles) and a direction arrow, not rec.tiles
        // (which is the settlement spot). Without this the in-page render
        // dropped the opening road entirely.
        const tiles = (rec.action === 'road' && rec.road && rec.road.edge_tiles)
            ? tilesText(rec.road.edge_tiles)
            : tilesText(rec.tiles);
        const arrow = (kind === 'road' && tiles) ? '→ ' : '';
        const detail = rec.detail ? ` ${rec.detail}` : '';
        const loc = tiles
            ? ` <span class="cbo-rec-tiles">${arrow}${escapeHtml(tiles)}</span>` : '';
        return `<span class="cbo-rec-kind">${escapeHtml(kindLabel)}</span>${loc}`
            + `<span class="cbo-rec-detail">${escapeHtml(detail)}</span>`;
    }
    // Hand read, clean version (per Noah): CONFIRMED cards first (icon +
    // count, no percent), then a separator, then only the LIKELY-but-unsure
    // resources as dim icons (no count, no percent). The raw "+49%" noise is
    // gone; the unsure group is just "probably also holds these".
    const LIKELY_CUTOFF = 0.45;   // P(at least one) above this = "likely"
    function handReadHtml(o) {
        const confirmed = [];
        const likely = [];
        const hp = o && o.hand_probs;
        if (hp) {
            const entries = Object.keys(hp).map((res) => ({ res, ...hp[res] }));
            for (const e of entries) {
                if ((e.min || 0) > 0) {
                    confirmed.push(
                        `<span class="cbo-chip">${iconFor(e.res)} ${e.min}</span>`);
                }
            }
            entries
                .filter((e) => (e.min || 0) === 0 && (e.p1 || 0) > LIKELY_CUTOFF)
                .sort((a, b) => (b.p1 || 0) - (a.p1 || 0))
                .forEach((e) => {
                    likely.push(
                        `<span class="cbo-chip cbo-maybe">${iconFor(e.res)}</span>`);
                });
        } else {
            const hand = (o && o.hand) || {};
            for (const res of Object.keys(hand)) {
                if (hand[res] > 0) {
                    confirmed.push(
                        `<span class="cbo-chip">${iconFor(res)} ${hand[res]}</span>`);
                }
            }
            if (o && (o.unknown || 0) > 0) {
                likely.push(`<span class="cbo-chip cbo-maybe">? ${o.unknown}</span>`);
            }
        }
        let html = confirmed.join('');
        if (likely.length) {
            html += '<span class="cbo-sep">|</span>' + likely.join('');
        }
        return html || '<span class="cbo-maybe">no read</span>';
    }

    // Default ON: the in-page HUD is now the primary surface (the side panel
    // is demoted to an icon-reachable fallback). Set 'catanbot.log_hud'='0'
    // to turn the in-page HUD off and go back to the side panel only.
    function enabled() {
        try { return localStorage.getItem(LS_ON) !== '0'; }
        catch (e) { return true; }
    }
    function replaceMode() {
        try { return localStorage.getItem(LS_REPLACE) === '1'; }
        catch (e) { return false; }
    }
    function currentTab() {
        try { return localStorage.getItem(LS_TAB) || 'catanbot'; }
        catch (e) { return 'catanbot'; }
    }
    function setTab(t) {
        try { localStorage.setItem(LS_TAB, t); } catch (e) { /* private mode */ }
        applyTab();
    }

    // Stamp the HUD subtree so content.js's streamer username-sweep skips it.
    function stampStreamer(node) {
        if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
        node.dataset[STREAMER_FLAG] = 'hud';
        const kids = node.querySelectorAll('*');
        for (let i = 0; i < kids.length; i += 1) {
            kids[i].dataset[STREAMER_FLAG] = 'hud';
        }
    }

    function ensureStyle() {
        if (document.getElementById(STYLE_ID)) return;
        const style = document.createElement('style');
        style.id = STYLE_ID;
        // Native-beige: inherit colonist's parchment background + font from
        // the container, lay our content in warm browns with hairline rules,
        // so it reads as part of the site rather than a foreign dark box.
        style.textContent = `
#${TABS_ID} {
    display: flex;
    gap: 2px;
    padding: 4px 6px 0 6px;
    font-family: inherit;
    box-sizing: border-box;
    position: relative;
}
#${TABS_ID} .cbo-gear { margin-left: auto; padding: 5px 9px; }
#${TABS_ID} .cbo-settings {
    position: absolute;
    top: 100%;
    right: 6px;
    z-index: 10;
    background: #f3ead7;
    border: 1px solid rgba(90, 62, 28, 0.30);
    border-radius: 6px;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.22);
    padding: 6px 8px;
    min-width: 150px;
}
#${TABS_ID} .cbo-set-row {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 3px 2px;
    font-size: 12px;
    color: #2f2415;
    cursor: pointer;
    white-space: nowrap;
}
#${TABS_ID} .cbo-set-row input { margin: 0; cursor: pointer; }
#${TABS_ID} .cbo-tab {
    appearance: none;
    border: none;
    background: rgba(90, 62, 28, 0.10);
    color: #5a4a32;
    font: inherit;
    font-weight: 600;
    font-size: 13px;
    letter-spacing: 0.02em;
    padding: 5px 12px;
    border-radius: 7px 7px 0 0;
    cursor: pointer;
    line-height: 1.2;
}
#${TABS_ID} .cbo-tab.active {
    background: rgba(90, 62, 28, 0.20);
    color: #2f2415;
    box-shadow: inset 0 -2px 0 0 #b8862f;
}
#${ROOT_ID} {
    font-family: inherit;
    color: #2f2415;
    padding: 8px 10px 10px 10px;
    box-sizing: border-box;
    font-size: 13px;
    line-height: 1.35;
    overflow-y: auto;
}
#${ROOT_ID} .cbo-h {
    font-weight: 700;
    font-size: 12px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #6b5836;
    margin: 8px 0 3px 0;
    border-bottom: 1px solid rgba(90, 62, 28, 0.18);
    padding-bottom: 2px;
}
#${ROOT_ID} .cbo-h:first-child { margin-top: 0; }
#${ROOT_ID} .cbo-placeholder {
    color: #8a7656;
    font-style: italic;
    padding: 6px 0;
}
#${ROOT_ID} .cbo-rec {
    font-size: 14px;
    font-weight: 600;
    color: #2f2415;
    padding: 2px 0 4px 0;
}
#${ROOT_ID} .cbo-rec-kind {
    display: inline-block;
    background: #b8862f;
    color: #fff;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.04em;
    padding: 1px 6px;
    border-radius: 4px;
    margin-right: 5px;
    vertical-align: middle;
}
#${ROOT_ID} .cbo-rec-tiles { font-weight: 700; }
#${ROOT_ID} .cbo-rec-detail { color: #6b5836; font-weight: 500; }
#${ROOT_ID} .cbo-fallback {
    color: #6b5836; font-size: 12px; padding: 1px 0 2px 8px;
    border-left: 2px solid rgba(90, 62, 28, 0.25); margin: 2px 0 0 2px;
}
#${ROOT_ID} .cbo-opp {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 2px 0;
    flex-wrap: wrap;
}
#${ROOT_ID} .cbo-pill {
    font-weight: 700;
    font-size: 12px;
    padding: 1px 7px;
    border-radius: 9px;
    white-space: nowrap;
    flex: 0 0 auto;
}
#${ROOT_ID} .cbo-reads { display: inline-flex; gap: 4px; flex-wrap: wrap; }
#${ROOT_ID} .cbo-chip {
    background: rgba(90, 62, 28, 0.10);
    border-radius: 5px;
    padding: 0 5px;
    font-size: 12px;
    white-space: nowrap;
}
#${ROOT_ID} .cbo-chip.cbo-maybe { opacity: 0.55; }
#${ROOT_ID} .cbo-chip.cbo-mono { box-shadow: inset 0 0 0 1.5px #c0392b; }
#${ROOT_ID} .cbo-sep { color: #b3a07d; margin: 0 3px; font-weight: 700; }
#${ROOT_ID} .cbo-prob { color: #9c7b3a; font-size: 11px; }
#${ROOT_ID} .cbo-self { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
#${ROOT_ID} .cbo-vp { font-weight: 800; font-size: 14px; color: #2f2415; }
#${ROOT_ID} .cbo-self-meta { color: #6b5836; font-size: 11px; }
#${ROOT_ID} .cbo-nextbuild { color: #6b5836; font-size: 12px; margin-top: 2px; }
#${ROOT_ID} .cbo-h.cbo-urgent { color: #9c2d22; }
#${ROOT_ID} .cbo-robber-row {
    display: flex; align-items: center; gap: 5px;
    font-size: 13px; padding: 1px 0;
}
#${ROOT_ID} .cbo-foot {
    margin-top: 8px;
    padding: 5px 8px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
}
#${ROOT_ID} .cbo-foot.cbo-red { background: rgba(192, 57, 43, 0.14); color: #9c2d22; }
#${ROOT_ID} .cbo-foot.cbo-amber { background: rgba(216, 134, 47, 0.16); color: #8a5618; }
#${ROOT_ID} .cbo-foot.cbo-green { background: rgba(70, 164, 90, 0.16); color: #2f6b3f; }
#${ROOT_ID}.cbo-u-red { box-shadow: inset 3px 0 0 0 #c0392b; }
#${ROOT_ID}.cbo-u-amber { box-shadow: inset 3px 0 0 0 #d8862f; }
#${ROOT_ID}.cbo-u-green { box-shadow: inset 3px 0 0 0 #46a45a; }
#${ROOT_ID}.cbo-u-turn { box-shadow: inset 3px 0 0 0 #b8862f; }
/* Per-player read injected into colonist's OWN player rows: unscoped (these
   live in colonist's DOM, not under #${ROOT_ID}). One thin line per player. */
.cbo-row-read {
    display: flex; flex-wrap: wrap; gap: 3px; align-items: center;
    width: 100%; padding: 2px 8px 3px 8px; box-sizing: border-box;
    margin: 0; box-shadow: inset 2px 0 0 0 rgba(70, 196, 99, 0.7);
    background: rgba(18, 20, 26, 0.55);
    color: #fff;
    font: 600 11px/1.3 -apple-system, system-ui, sans-serif;
}
.cbo-row-read .cbo-chip {
    background: rgba(255, 255, 255, 0.14); border-radius: 4px; padding: 0 4px;
    white-space: nowrap; color: #fff;
}
.cbo-row-read .cbo-chip.cbo-maybe { opacity: 0.5; }
.cbo-row-read .cbo-sep { color: rgba(255, 255, 255, 0.5); margin: 0 2px; font-weight: 700; }
/* Trade verdict badge pinned to colonist's own trade panel. */
#cbo-trade-badge {
    position: fixed;
    z-index: 2147483600;
    padding: 4px 10px;
    border-radius: 7px;
    color: #fff;
    font: 700 13px/1.3 -apple-system, system-ui, sans-serif;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35);
    pointer-events: none;
    max-width: 280px;
}
#cbo-trade-badge .cbo-tb-tag {
    font-size: 9px; letter-spacing: 0.06em; text-transform: uppercase;
    opacity: 0.7; margin-right: 5px; font-weight: 600;
}
#cbo-trade-badge.cbo-tb-accept { background: #2e8b57; }
#cbo-trade-badge.cbo-tb-decline { background: #c0392b; }
#cbo-trade-badge.cbo-tb-consider { background: #3b7dd8; }
/* Glow the recommended bottom build button (road/settle/city/dev). */
.cbo-action-hl {
    outline: 3px solid #46c463 !important;
    outline-offset: 2px !important;
    border-radius: 10px !important;
    animation: cbo-actpulse 1.1s ease-in-out infinite alternate !important;
}
@keyframes cbo-actpulse {
    from { box-shadow: 0 0 6px 2px rgba(70, 196, 99, 0.5); }
    to { box-shadow: 0 0 16px 5px rgba(70, 196, 99, 0.95); }
}
/* Knight (or dev) to play -> glow that card in the hand, red + a "!" badge. */
.cbo-cue-knight {
    position: relative !important;
    outline: 3px solid #e0483e !important;
    outline-offset: 2px !important;
    border-radius: 8px !important;
    animation: cbo-knightpulse 1s ease-in-out infinite alternate !important;
}
.cbo-cue-knight::after {
    content: '!';
    position: absolute; top: -8px; right: -8px;
    width: 16px; height: 16px; line-height: 16px; text-align: center;
    background: #e0483e; color: #fff; border-radius: 50%;
    font: 700 12px/16px -apple-system, system-ui, sans-serif;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.4); z-index: 2147483600;
}
@keyframes cbo-knightpulse {
    from { box-shadow: 0 0 5px 2px rgba(224, 72, 62, 0.5); }
    to { box-shadow: 0 0 15px 5px rgba(224, 72, 62, 0.95); }
}
/* Cards to discard on a 7 -> amber glow on each card to drop. */
.cbo-cue-dev {
    outline: 3px solid #d8862f !important;
    outline-offset: 2px !important;
    border-radius: 8px !important;
    animation: cbo-droppulse 1s ease-in-out infinite alternate !important;
}
@keyframes cbo-droppulse {
    from { box-shadow: 0 0 5px 2px rgba(216, 134, 47, 0.5); }
    to { box-shadow: 0 0 15px 5px rgba(216, 134, 47, 0.95); }
}
/* Hover tooltip: a player's read beside their row. */
#cbo-tip {
    position: fixed;
    z-index: 2147483600;
    background: rgba(20, 22, 28, 0.96);
    color: #fff;
    padding: 5px 9px;
    border-radius: 7px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
    font: 600 12px/1.3 -apple-system, system-ui, sans-serif;
    pointer-events: none;
    white-space: nowrap;
    display: flex; gap: 4px; align-items: center;
}
#cbo-tip .cbo-tip-tag {
    font-size: 9px; letter-spacing: 0.06em; text-transform: uppercase;
    opacity: 0.6; margin-right: 2px;
}
#cbo-tip .cbo-chip { background: rgba(255,255,255,0.12); border-radius: 4px; padding: 0 4px; }
#cbo-tip .cbo-chip.cbo-maybe { opacity: 0.55; }
#cbo-tip .cbo-sep { opacity: 0.5; margin: 0 2px; }
/* Hide colonist's native log off-screen (still dimensioned) when the CatanBot
   tab is up, so its virtual scroller doesn't error on a display:none list. */
.cbo-hidden-native {
    position: absolute !important;
    left: -99999px !important;
    top: 0 !important;
    visibility: hidden;
}
`;
        (document.head || document.documentElement).appendChild(style);
    }

    // --- In-page settings (gear in the tab bar) -----------------------------
    // Streamer mode is read from localStorage (content.js keeps it synced) but
    // WRITTEN via chrome.storage.local 'streamer' — content.js listens for that
    // change and applies the DOM anonymization. Pause + replace are plain flags.
    function _streamerOn() {
        try { return localStorage.getItem('cataan.streamer') === '1'; }
        catch (e) { return false; }
    }
    function _setStreamer(v) {
        try { chrome.storage.local.set({ streamer: !!v }); } catch (e) { /* ctx */ }
    }
    function _paused() {
        try { return localStorage.getItem('catanbot.paused') === '1'; }
        catch (e) { return false; }
    }
    function _toggleRow(label, get, set) {
        const row = document.createElement('label');
        row.className = 'cbo-set-row';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = !!get();
        cb.addEventListener('change', () => set(cb.checked));
        const span = document.createElement('span');
        span.textContent = label;
        row.appendChild(cb);
        row.appendChild(span);
        return row;
    }
    function buildSettingsPanel() {
        const p = document.createElement('div');
        p.className = 'cbo-settings';
        p.style.display = 'none';
        p.appendChild(_toggleRow('Streamer mode', _streamerOn, _setStreamer));
        p.appendChild(_toggleRow('Pause recs', _paused, (v) => {
            try { localStorage.setItem('catanbot.paused', v ? '1' : '0'); }
            catch (e) { /* private mode */ }
        }));
        p.appendChild(_toggleRow('Replace log', replaceMode, (v) => {
            try { localStorage.setItem(LS_REPLACE, v ? '1' : '0'); }
            catch (e) { /* private mode */ }
            applyTab();
        }));
        stampStreamer(p);
        return p;
    }

    function buildNodes() {
        // Tab bar.
        tabs = document.createElement('div');
        tabs.id = TABS_ID;
        const tLog = document.createElement('button');
        tLog.className = 'cbo-tab';
        tLog.dataset.tab = 'log';
        tLog.textContent = 'Log';
        tLog.addEventListener('click', () => setTab('log'));
        const tHud = document.createElement('button');
        tHud.className = 'cbo-tab';
        tHud.dataset.tab = 'catanbot';
        tHud.textContent = 'CatanBot';
        tHud.addEventListener('click', () => setTab('catanbot'));
        tabs.appendChild(tLog);
        tabs.appendChild(tHud);

        // Gear -> settings dropdown, pushed to the right.
        const gear = document.createElement('button');
        gear.className = 'cbo-tab cbo-gear';
        gear.textContent = '⚙';
        gear.title = 'CatanBot settings';
        const panel = buildSettingsPanel();
        gear.addEventListener('click', (e) => {
            e.stopPropagation();
            panel.style.display = panel.style.display === 'block'
                ? 'none' : 'block';
        });
        document.addEventListener('click', () => { panel.style.display = 'none'; });
        tabs.appendChild(gear);
        tabs.appendChild(panel);

        // HUD body (the /advisor render writes here each poll).
        root = document.createElement('div');
        root.id = ROOT_ID;
        root.dataset.cboLoghud = '1';
        root.innerHTML = '<div class="cbo-placeholder">CatanBot HUD'
            + ' — start a game to see recommendations.</div>';

        stampStreamer(tabs);
        stampStreamer(root);
    }

    // colonist's native log content = every direct child of the container
    // that isn't one of our injected nodes. CLASS-AGNOSTIC on purpose: the
    // old virtualScroller-/virtualContainer- class hashes already rotted
    // since the April recon, so we hide "everything that isn't ours" instead
    // of matching a class that moves on every colonist deploy.
    function nativeChildren(container) {
        if (!container) return [];
        return Array.from(container.children).filter(
            (c) => c !== tabs && c !== root);
    }

    // Show/hide HUD body vs native log per the current tab + replace mode,
    // and reflect the active tab styling.
    function applyTab(container) {
        if (!root || !tabs) return;
        const cont = container || root.parentElement || tabs.parentElement;
        const replace = replaceMode();
        const tab = replace ? 'catanbot' : currentTab();
        tabs.style.display = replace ? 'none' : 'flex';
        root.style.display = (tab === 'catanbot') ? 'block' : 'none';
        // Hide the native log only when the CatanBot tab is up AND we've
        // connected — a user without the bridge keeps their log (the HUD
        // shows additively until it has something real to show).
        const hideNative = (tab === 'catanbot' && _everConnected);
        for (const child of nativeChildren(cont)) {
            // Move the native log OFF-SCREEN (still dimensioned) rather than
            // display:none — colonist's virtual scroller calls scrollToIndex
            // on the log, and a display:none list has no dimensions, so it
            // retries forever and floods the console with "Failed to scroll".
            child.classList.toggle('cbo-hidden-native', hideNative);
        }
        tabs.querySelectorAll('.cbo-tab').forEach((b) => {
            b.classList.toggle('active', b.dataset.tab === tab);
        });
    }

    function teardown() {
        const cont = (root && root.parentElement)
            || (tabs && tabs.parentElement);
        for (const child of nativeChildren(cont)) {
            child.classList.remove('cbo-hidden-native');
        }
        if (root && root.parentElement) root.remove();
        if (tabs && tabs.parentElement) tabs.remove();
        document.querySelectorAll('.cbo-row-read').forEach((e) => e.remove());
        if (_tip && _tip.parentElement) { _tip.remove(); _tip = null; }
        const tb = document.getElementById('cbo-trade-badge');
        if (tb) tb.remove();
        document.querySelectorAll(
            '.cbo-action-hl, .cbo-cue-dev, .cbo-cue-knight').forEach(
            (el) => el.classList.remove(
                'cbo-action-hl', 'cbo-cue-dev', 'cbo-cue-knight'));
    }

    // Readable text color (black/white) for a pill background.
    function contrastText(css) {
        const c = String(css || '').trim();
        let r;
        let g;
        let b;
        let m = c.match(/^#([0-9a-f]{6})$/i);
        if (m) {
            r = parseInt(m[1].slice(0, 2), 16);
            g = parseInt(m[1].slice(2, 4), 16);
            b = parseInt(m[1].slice(4, 6), 16);
        } else {
            m = c.match(/^#([0-9a-f]{3})$/i);
            if (m) {
                r = parseInt(m[1][0] + m[1][0], 16);
                g = parseInt(m[1][1] + m[1][1], 16);
                b = parseInt(m[1][2] + m[1][2], 16);
            } else {
                m = c.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
                if (m) { r = +m[1]; g = +m[2]; b = +m[3]; }
            }
        }
        if (r == null) return '#fff';
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.6 ? '#111' : '#fff';
    }

    function streamerOn() {
        try { return localStorage.getItem('cataan.streamer') === '1'; }
        catch (e) { return false; }
    }
    // One-line urgency footer: the single most pressing signal. Robber-on-you
    // first (it's costing you now), then leader threat, then your own
    // win-proximity. Threat names the leader, so it's suppressed in streamer
    // mode to avoid leaking an opponent's username on stream.
    function footerHtml(snap) {
        const rb = snap.robber_block_hint;
        if (rb && rb.reason) {
            return `<div class="cbo-foot cbo-red">${escapeHtml(rb.reason)}</div>`;
        }
        if (snap.robber_on_me) {
            return '<div class="cbo-foot cbo-red">Robber is on you.</div>';
        }
        if (!streamerOn() && snap.threat && snap.threat.message) {
            return `<div class="cbo-foot cbo-amber">${escapeHtml(snap.threat.message)}</div>`;
        }
        if (snap.win_proximity && snap.win_proximity.message) {
            return `<div class="cbo-foot cbo-green">${escapeHtml(snap.win_proximity.message)}</div>`;
        }
        return '';
    }
    // Urgency class for the card's left border accent.
    function urgencyOf(snap) {
        if (!snap) return '';
        if ((snap.robber_block_hint && snap.robber_block_hint.reason)
                || snap.robber_on_me) return 'cbo-u-red';
        if (snap.threat && snap.threat.message) return 'cbo-u-amber';
        if (snap.win_proximity && snap.win_proximity.message) return 'cbo-u-green';
        if (snap.my_turn) return 'cbo-u-turn';
        return '';
    }

    // "You" card: VP, hand, cards/knights, nearest-build gap, monopoly risk.
    // Compact mirror of panel.js's self card, sized to fit the column.
    function selfHtml(snap) {
        const me = snap.self;
        if (!me) return '';
        const bg = pillColor(me);
        const meta = [`${me.cards || 0} cards`];
        if ((me.knights_played || 0) > 0) meta.push(`${me.knights_played} kn`);
        const monoRes = me.monopoly_risk ? me.monopoly_risk.resource : null;
        const hand = Object.entries(me.hand || {})
            .filter(([, n]) => n > 0)
            .map(([r, n]) => `<span class="cbo-chip${r === monoRes
                ? ' cbo-mono' : ''}">${iconFor(r)} ${n}</span>`)
            .join('') || '<span class="cbo-maybe">empty</span>';
        let out = '<div class="cbo-h">you</div>'
            + '<div class="cbo-self">'
            + `<span class="cbo-pill" style="background:${escapeHtml(bg)};`
            + `color:${contrastText(bg)}">${escapeHtml(me.username || 'you')}</span>`
            + `<span class="cbo-vp">${me.vp || 0} VP</span>`
            + `<span class="cbo-self-meta">${meta.join(' · ')}</span></div>`
            + `<div class="cbo-reads">${hand}</div>`;
        const nb = me.next_build;
        if (nb && nb.missing) {
            const miss = Object.entries(nb.missing)
                .filter(([, n]) => n > 0)
                .map(([r, n]) => `${iconFor(r)}${n > 1 ? ` ${n}` : ''}`)
                .join(' ');
            if (miss) {
                out += `<div class="cbo-nextbuild">${miss} from `
                    + `${escapeHtml(nb.build || 'next build')}</div>`;
            }
        }
        if (me.monopoly_risk) {
            out += '<div class="cbo-foot cbo-red">monopoly risk: '
                + `${me.monopoly_risk.count} ${iconFor(me.monopoly_risk.resource)}`
                + ' exposed</div>';
        }
        return out;
    }

    // Robber targets: the ranked tiles to rob, shown only when the decision
    // is live (a 7 is pending or a knight is up). Top 3, each as tile +
    // suggested-victim pills, compact.
    function robberHtml(snap) {
        const ts = snap.robber_targets || [];
        const show = snap.robber_pending || snap.robber_reason === 'knight';
        if (!show || !ts.length) return '';
        const rows = [];
        for (let i = 0; i < Math.min(3, ts.length); i += 1) {
            const t = ts[i];
            const tile = t.resource
                ? `${iconFor(t.resource)}${t.number == null ? '' : t.number}`
                : 'desert';
            const victims = (t.victims || []).map((v) => {
                const bg = v.color_css || COLOR_HEX[v.color] || '#888';
                const star = v.suggested ? '★' : '';
                const who = escapeHtml(String(v.username || v.color || '?')
                    .slice(0, 1));
                return `<span class="cbo-pill" style="background:${escapeHtml(bg)};`
                    + `color:${contrastText(bg)}">${star}${who}</span>`;
            }).join(' ');
            rows.push(`<div class="cbo-robber-row"><b>${i + 1}.</b> ${tile} `
                + `${victims}</div>`);
        }
        return '<div class="cbo-h cbo-urgent">robber targets</div>'
            + rows.join('');
    }

    // Build the HUD body HTML from an /advisor snapshot: top rec (gated like
    // the side panel) + robber targets + your card + opponent reads + footer.
    function renderBody(snap) {
        if (!snap) return '<div class="cbo-placeholder">connecting…</div>';
        const out = [];
        const recs = snap.recommendations || [];
        const showRecs = (snap.my_turn || snap.setup_phase) && !_paused();
        if (_paused()) {
            out.push('<div class="cbo-h">next move</div>'
                + '<div class="cbo-placeholder">recs paused</div>');
        } else if (snap.variant_recs_disabled) {
            out.push('<div class="cbo-h">next move</div>'
                + '<div class="cbo-placeholder">recs off (variant board)</div>');
        } else if (showRecs && recs.length) {
            let block = '<div class="cbo-h">next move</div>'
                + `<div class="cbo-rec">${topRecHtml(recs[0])}</div>`;
            // If the top pick is a trade, show the next-best as the
            // "if they deny, do this instead" fallback (Noah's ask).
            const k0 = recs[0].action === 'road' ? 'road' : recs[0].kind;
            if ((k0 === 'trade' || k0 === 'propose_trade') && recs[1]) {
                block += `<div class="cbo-fallback">if denied &rarr; `
                    + `${topRecHtml(recs[1])}</div>`;
            }
            out.push(block);
        } else if (showRecs) {
            out.push('<div class="cbo-h">next move</div>'
                + '<div class="cbo-placeholder">no recommendation</div>');
        }
        const robberBlock = robberHtml(snap);
        if (robberBlock) out.push(robberBlock);
        // Discard guidance, re-homed into the HUD (was a floating panel). On a
        // 7 the matching cards in your hand also glow (applyActionCues).
        const dh = snap.discard_hint;
        if (dh) {
            out.push('<div class="cbo-foot cbo-amber">DISCARD &middot; '
                + escapeHtml(dh.reason || dh.message
                    || 'drop your lowest-value cards') + '</div>');
        }
        const selfBlock = selfHtml(snap);
        if (selfBlock) out.push(selfBlock);
        const opps = (snap.opps || []).filter((o) => o && !o.is_placeholder);
        if (opps.length) {
            out.push('<div class="cbo-h">opponents</div>');
            for (const o of opps) {
                const bg = pillColor(o);
                const name = escapeHtml(o.username || o.color || '?');
                out.push('<div class="cbo-opp">'
                    + `<span class="cbo-pill" style="background:${escapeHtml(bg)};`
                    + `color:${contrastText(bg)}">${name}</span>`
                    + `<span class="cbo-reads">${handReadHtml(o)}</span></div>`);
            }
        }
        const foot = footerHtml(snap);
        if (foot) out.push(foot);
        if (!out.length) {
            return '<div class="cbo-placeholder">CatanBot HUD'
                + ' — start a game to see recommendations.</div>';
        }
        return out.join('');
    }

    // Self hand as known chips (we see our own cards).
    function selfReadChips(me) {
        const h = Object.entries((me && me.hand) || {})
            .filter(([, n]) => n > 0)
            .map(([r, n]) => `<span class="cbo-chip">${iconFor(r)} ${n}</span>`)
            .join('');
        return h || '<span class="cbo-chip cbo-maybe">empty</span>';
    }

    // Inject CatanBot's read next to each player in colonist's OWN player
    // panel (recon: gamePlayerInformationContainer > opponentsScrollContainer
    // > playerRow-*, plus a sibling self playerRow-*). One compact read line
    // per row, matched to a player by the name the row starts with. Re-runs
    // each poll so it survives React wiping the rows; skips silently if the
    // panel isn't present.
    function injectPlayerReads(snap) {
        const existing = document.querySelectorAll('.cbo-row-read');
        if (!enabled() || !snap) { existing.forEach((e) => e.remove()); return; }
        const cont = document.querySelector(
            '[class*="gamePlayerInformationContainer"]');
        if (!cont) { existing.forEach((e) => e.remove()); return; }
        const rows = cont.querySelectorAll('[class*="playerRow-"]');
        if (!rows.length) return;

        const players = [];
        if (snap.self && snap.self.username) {
            players.push(Object.assign({ _self: true }, snap.self));
        }
        for (const o of (snap.opps || [])) {
            if (o && !o.is_placeholder && o.username) players.push(o);
        }

        for (const row of rows) {
            const txt = (row.textContent || '').trim();
            let match = null;
            let best = 0;
            for (const p of players) {
                if (txt.startsWith(p.username) && p.username.length > best) {
                    match = p; best = p.username.length;
                }
            }
            // Insert the read as a SIBLING line right after the row (in the
            // vertical stack), NOT as a child of the row — appending inside
            // the row fought colonist's flex layout and misaligned it.
            const next = row.nextElementSibling;
            let read = (next && next.classList
                && next.classList.contains('cbo-row-read')) ? next : null;
            if (!match) { if (read) read.remove(); continue; }
            if (!read) {
                read = document.createElement('div');
                read.className = 'cbo-row-read';
                row.parentNode.insertBefore(read, row.nextSibling);
            }
            read.innerHTML = match._self
                ? selfReadChips(match) : handReadHtml(match);
            stampStreamer(read);
        }
    }

    // Per-player reads "next to each player" as a HOVER tooltip: colonist's
    // player rows are too tight + horizontally clipped for a clean
    // always-visible line (verified live), so instead, hovering a row pops
    // that player's CatanBot read beside it. Non-destructive, fits, and works
    // for opponents (inferred) and self (known). Reads from the cached snap.
    function ensureTip() {
        if (_tip && _tip.isConnected) return _tip;
        _tip = document.createElement('div');
        _tip.id = 'cbo-tip';
        _tip.style.display = 'none';
        stampStreamer(_tip);
        (document.body || document.documentElement).appendChild(_tip);
        return _tip;
    }
    function _playerForRow(row) {
        if (!_lastSnap) return null;
        const txt = (row.textContent || '').trim();
        const players = [];
        if (_lastSnap.self && _lastSnap.self.username) {
            players.push(Object.assign({ _self: true }, _lastSnap.self));
        }
        for (const o of (_lastSnap.opps || [])) {
            if (o && !o.is_placeholder && o.username) players.push(o);
        }
        let match = null;
        let best = 0;
        for (const p of players) {
            if (txt.startsWith(p.username) && p.username.length > best) {
                match = p; best = p.username.length;
            }
        }
        return match;
    }
    function attachRowHovers() {
        if (!enabled()) return;
        const cont = document.querySelector(
            '[class*="gamePlayerInformationContainer"]');
        if (!cont) return;
        cont.querySelectorAll('[class*="playerRow"]').forEach((row) => {
            if (row.dataset.cboHover) return;
            row.dataset.cboHover = '1';
            row.addEventListener('mouseenter', () => {
                const p = _playerForRow(row);
                if (!p) return;
                const tip = ensureTip();
                tip.innerHTML = '<span class="cbo-tip-tag">read</span>'
                    + (p._self ? selfReadChips(p) : handReadHtml(p));
                stampStreamer(tip);
                const r = row.getBoundingClientRect();
                tip.style.display = 'block';
                const tw = tip.getBoundingClientRect().width;
                // prefer left of the row (over the blank gap); fall back below.
                let left = r.left - tw - 8;
                if (left < 4) left = r.left;
                tip.style.left = `${Math.round(left)}px`;
                tip.style.top = `${Math.round(r.top + 6)}px`;
            });
            row.addEventListener('mouseleave', () => {
                if (_tip) _tip.style.display = 'none';
            });
        });
    }

    // Literal trade injection: pin a CatanBot verdict badge to colonist's own
    // trade panel. Recon: the accept/decline icons are IMG .tradeResponseStatus-*
    // and the collapse is IMG .showHideTradeIcon-*; walk up to the panel. Sets
    // _tradeAnchored so the notification doesn't double up.
    function injectTradeBadge(snap) {
        _tradeAnchored = false;
        const old = document.getElementById('cbo-trade-badge');
        if (!enabled() || !snap || !snap.incoming_trade) {
            if (old) old.remove(); return;
        }
        const icon = document.querySelector(
            '[class*="showHideTradeIcon"], [class*="tradeResponseStatus"]');
        if (!icon) { if (old) old.remove(); return; }
        let panel = icon;
        for (let i = 0; i < 6; i += 1) {
            if (!panel.parentElement) break;
            panel = panel.parentElement;
            if (panel.getBoundingClientRect().width > 220) break;
        }
        let badge = old;
        if (!badge) {
            badge = document.createElement('div');
            badge.id = 'cbo-trade-badge';
            (document.body || document.documentElement).appendChild(badge);
        }
        const t = snap.incoming_trade;
        const v = ['accept', 'decline', 'consider'].includes(t.verdict)
            ? t.verdict : 'consider';
        badge.className = `cbo-tb-${v}`;
        badge.innerHTML = '<span class="cbo-tb-tag">CatanBot</span>'
            + `<b>${v.toUpperCase()}</b>`
            + (t.reason ? ` ${escapeHtml(t.reason)}` : '');
        stampStreamer(badge);
        const r = panel.getBoundingClientRect();
        // Sit flush under the panel, left-aligned, so it reads as attached
        // rather than floating loose over the board.
        badge.style.left = `${Math.round(r.left)}px`;
        badge.style.top = `${Math.round(r.bottom + 1)}px`;
        badge.style.display = 'block';
        _tradeAnchored = true;
    }

    // Unified action cues: light up the REAL colonist element you act on.
    // One entry point, called each poll. Clears every prior cue first, then
    // each sub-cue applies under its own guard (build bar on your turn, the
    // central place button in setup, the knight card / discard cards when
    // those moments fire). Cues anchor to colonist's own DOM, never a floater.
    function applyActionCues(snap) {
        document.querySelectorAll(
            '.cbo-action-hl, .cbo-cue-dev, .cbo-cue-knight').forEach(
            (el) => el.classList.remove(
                'cbo-action-hl', 'cbo-cue-dev', 'cbo-cue-knight'));
        if (!enabled() || !snap || _paused()) return;
        highlightBuildButton(snap);
        highlightCentralAction(snap);
        highlightKnightCard(snap);
        highlightDiscardCards(snap);
    }

    // Bottom build bar. Recon: colonist's action bar is a row of
    // .actionButton-* cards; road/settle/city carry a numeric piece count,
    // dev sits just left of road. Glows the matching one on your turn (not in
    // setup: there the central "Place" button is the control, see below).
    function highlightBuildButton(snap) {
        if (!snap.my_turn || snap.setup_phase) return;
        const recs = snap.recommendations || [];
        if (!recs.length) return;
        const kind = recs[0].action === 'road' ? 'road' : recs[0].kind;
        const btns = Array.from(
            document.querySelectorAll('[class*="actionButton-"]'))
            .filter((b) => !/Container/.test(b.className.toString()))
            .sort((a, b) => a.getBoundingClientRect().x
                - b.getBoundingClientRect().x);
        if (!btns.length) return;
        // road/settle/city are the buttons carrying a piece-count badge.
        const counted = btns.filter((b) => /\d/.test((b.textContent || '').trim()));
        let target = null;
        if (kind === 'road') target = counted[0];
        else if (kind === 'settlement' || kind === 'opening_settlement') {
            target = counted[1];
        } else if (kind === 'city') target = counted[2];
        else if (kind === 'dev_card' || kind === 'buy_dev') {
            const i = btns.indexOf(counted[0]);
            target = i > 0 ? btns[i - 1] : null;
        }
        if (target) target.classList.add('cbo-action-hl');
    }

    // Setup-phase placement: colonist's central status button reads "Place
    // Settlement" / "Place Road" during the opening. Glow it so the cue lands
    // on the actual control (the board itself is a WebGL canvas with no DOM
    // node to glow). Only in setup + my turn, so it never grabs End Turn/Roll.
    function highlightCentralAction(snap) {
        if (!snap.my_turn || !snap.setup_phase) return;
        const central = document.querySelector(
            '[class*="actionButtonContainer"]');
        if (central) central.classList.add('cbo-action-hl');
    }

    // Knight (or any dev) to play -> glow THAT card in the dev-card hand.
    // Gated on a live recon of the dev-card hand element: findDevCard returns
    // null until that selector is confirmed, so this is a safe no-op rather
    // than glowing the wrong thing. CSS (.cbo-cue-knight) is ready.
    function highlightKnightCard(snap) {
        const kh = snap.knight_hint;
        const knightRec = (snap.recommendations || []).some(
            (r) => r && (r.kind === 'knight' || r.action === 'knight'));
        if (!knightRec && !(kh && kh.should_play)) return;
        const card = findDevCard('knight');
        if (card) card.classList.add('cbo-cue-knight');
    }

    // Discard on a 7 -> glow the specific cards to drop. Gated on a live recon
    // of the hand resource-card elements (findHandResourceCard); no-op until
    // confirmed. The discard guidance text already shows in the HUD footer.
    function highlightDiscardCards(snap) {
        const d = snap.discard_hint;
        if (!d) return;
        const drop = d.cards || d.drop || d.resources || null;
        if (!drop) return;
        for (const res of Object.keys(drop)) {
            if (!(drop[res] > 0)) continue;
            const els = findHandResourceCards(res, drop[res]);
            for (const el of els) el.classList.add('cbo-cue-dev');
        }
    }

    // ---- Hand-element resolvers. Return null/[] until the colonist hand DOM
    // is reconned live (the dev-card hand and the resource-card hand are not
    // present during setup, so they need a mid-game capture). Wiring the cues
    // through these stubs means the moment the selector is confirmed, the
    // glow lights up with no other change. ----
    function findDevCard(/* type */) {
        return null;
    }
    function findHandResourceCards(/* res, count */) {
        return [];
    }

    let _bridgeDown = false;
    async function fetchAndRender() {
        if (!enabled() || !root) return;
        // Fetch via the background service worker, NOT a direct in-page fetch:
        // a content script's http://127.0.0.1 request from the https colonist
        // page is blocked in some browsers (Comet: ERR_BLOCKED_BY_CLIENT). The
        // worker has the host permission and isn't page-blocked.
        let snap = null;
        try {
            const res = await chrome.runtime.sendMessage({ type: 'get-advisor' });
            if (res && res.ok) snap = res.snap;
        } catch (e) { /* worker asleep / extension reloading */ }

        if (snap) {
            root.innerHTML = renderBody(snap);
            root.className = urgencyOf(snap);   // left-border urgency accent
            stampStreamer(root);   // re-stamp the freshly rendered nodes
            injectTradeBadge(snap);   // verdict pinned to colonist's trade panel
            applyActionCues(snap);    // glow the real element to act on
            _lastSnap = snap;
            // Native opponent reads: one compact line injected under each
            // colonist player row (sibling, in the scroll content). Confirmed
            // visible live; runs AFTER _lastSnap so the hover fallback agrees.
            injectPlayerReads(snap);
            attachRowHovers();    // hover a player row -> fuller read
            _bridgeDown = false;
            if (!_everConnected) { _everConnected = true; applyTab(); }
        } else if (!_bridgeDown) {
            _bridgeDown = true;
            root.innerHTML = '<div class="cbo-placeholder">bridge offline'
                + ' — start the CatanBot app.</div>';
            stampStreamer(root);
        }
    }

    // One-time toast (used by the fail-safe). Stamped so the streamer sweep
    // ignores it; auto-removes.
    function toast(msg) {
        try {
            const t = document.createElement('div');
            t.textContent = msg;
            t.style.cssText = 'position:fixed;bottom:16px;left:50%;'
                + 'transform:translateX(-50%);z-index:2147483600;'
                + 'background:rgba(20,22,28,0.95);color:#fff;'
                + 'font:13px -apple-system,system-ui,sans-serif;padding:8px 14px;'
                + 'border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,0.4);'
                + 'pointer-events:none;max-width:80vw;text-align:center;';
            t.dataset[STREAMER_FLAG] = 'hud';
            (document.body || document.documentElement).appendChild(t);
            setTimeout(() => { try { t.remove(); } catch (e) { /* gone */ } }, 6000);
        } catch (e) { /* no body yet */ }
    }

    // Called by content.js's observer + 500ms interval (via
    // window.__catanbot.ensureHudAttached) and by our own driver below.
    function ensureHudAttached() {
        if (!enabled()) { teardown(); return; }
        const finder = window.__catanbot && window.__catanbot.findLogContainer;
        if (typeof finder !== 'function') return;
        const found = finder();
        if (!found || !found.el) {
            // Fail-safe: if the log container can't be located after ~10s
            // (a colonist deploy rotted every selector tier), fall back to
            // the floating overlay so advice never silently disappears.
            _noContainer += 1;
            if (_noContainer >= 15 && !_failsafe) {
                _failsafe = true;
                try { localStorage.setItem('catanbot.overlay', '1'); }
                catch (e) { /* private mode */ }
                console.warn(LOG_PREFIX, 'log container not found after retries;'
                    + ' falling back to the floating overlay');
                toast('CatanBot could not attach to the log; using the'
                    + ' floating overlay instead.');
            }
            return;
        }
        _noContainer = 0;
        _failsafe = false;
        const container = found.el;

        ensureStyle();
        if (!root || !tabs) buildNodes();

        // (Re)attach as the first children of the container. insertBefore
        // MOVES existing nodes, so this never duplicates after a re-render.
        if (tabs.parentElement !== container) {
            container.insertBefore(tabs, container.firstChild);
        }
        if (root.parentElement !== container) {
            container.insertBefore(root, tabs.nextSibling);
        }
        applyTab(container);
    }

    // Expose for content.js's re-anchor hook, and run our own lightweight
    // driver (content.js only drives after it finds the scroller; the
    // container can exist independently, so we self-drive too).
    window.__catanbot = window.__catanbot || {};
    window.__catanbot.ensureHudAttached = ensureHudAttached;

    try { ensureHudAttached(); } catch (e) { /* container not ready yet */ }
    setInterval(() => {
        try { ensureHudAttached(); } catch (e) { /* keep trying */ }
    }, 700);
    // Data poll: refresh the HUD body from /advisor (only when enabled).
    setInterval(() => {
        try { fetchAndRender(); } catch (e) { /* bridge hiccup */ }
    }, POLL_MS);

    console.info(LOG_PREFIX, 'ready (on by default; disable with'
        + " localStorage 'catanbot.log_hud'='0')");
})();
