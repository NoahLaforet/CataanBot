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
        const tiles = tilesText(rec.tiles);
        const arrow = (rec.kind === 'road' && tiles) ? '→ ' : '';
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
}
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
    width: 100%; padding: 1px 6px 2px 6px; box-sizing: border-box;
    font-size: 11px;
    font-family: -apple-system, system-ui, sans-serif;
}
.cbo-row-read .cbo-chip {
    background: rgba(0, 0, 0, 0.10); border-radius: 4px; padding: 0 4px;
    white-space: nowrap; color: #222;
}
.cbo-row-read .cbo-chip.cbo-maybe { opacity: 0.5; }
.cbo-row-read .cbo-sep { color: #999; margin: 0 2px; font-weight: 700; }`;
        (document.head || document.documentElement).appendChild(style);
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

        // HUD body (placeholder until P2 wires the /advisor render).
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
            child.style.display = hideNative ? 'none' : '';
        }
        tabs.querySelectorAll('.cbo-tab').forEach((b) => {
            b.classList.toggle('active', b.dataset.tab === tab);
        });
    }

    function teardown() {
        const cont = (root && root.parentElement)
            || (tabs && tabs.parentElement);
        for (const child of nativeChildren(cont)) child.style.display = '';
        if (root && root.parentElement) root.remove();
        if (tabs && tabs.parentElement) tabs.remove();
        document.querySelectorAll('.cbo-row-read').forEach((e) => e.remove());
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
        const showRecs = snap.my_turn || snap.setup_phase;
        if (snap.variant_recs_disabled) {
            out.push('<div class="cbo-h">next move</div>'
                + '<div class="cbo-placeholder">recs off (variant board)</div>');
        } else if (showRecs && recs.length) {
            out.push('<div class="cbo-h">next move</div>'
                + `<div class="cbo-rec">${topRecHtml(recs[0])}</div>`);
        } else if (showRecs) {
            out.push('<div class="cbo-h">next move</div>'
                + '<div class="cbo-placeholder">no recommendation</div>');
        }
        const robberBlock = robberHtml(snap);
        if (robberBlock) out.push(robberBlock);
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
            // In-row player reads are DISABLED: colonist wraps its player
            // rows in a simplebar custom-scrollbar container that clips
            // injected siblings and misaligns appended children (verified
            // live in a bot game). The opponent reads live cleanly in the
            // OPPONENTS section of the HUD instead. Clear any stragglers.
            document.querySelectorAll('.cbo-row-read').forEach((e) => e.remove());
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
