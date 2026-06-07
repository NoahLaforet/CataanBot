// Optional in-page overlay - runs in the isolated world on colonist.io
// alongside content.js. Draws a small dark draggable panel directly over
// the board with the per-turn essentials: each opponent's per-resource
// hand read ("wood 2 +67%" style, same shape the side panel renders from
// snap.opps[].hand_probs) and the single top live recommendation.
//
// Default OFF. The side-panel settings drawer writes a localStorage key
// (catanbot.overlay) AND mirrors it to chrome.storage.local under
// `overlay`; this script reads chrome.storage.local on boot + listens
// for changes, so existing users never see the overlay until they opt
// in.
//
// Data source: the local bridge's GET http://127.0.0.1:8765/advisor
// snapshot, polled every ~500ms while the overlay is enabled. The bridge
// already CORS-allows https://colonist.io, and the extension carries the
// 127.0.0.1:8765 host permission, so the isolated-world fetch here works
// without going through background.js. We reuse the exact snapshot field
// names the side panel uses (snap.opps[].username, .hand_probs,
// .color_css; snap.recommendations[0]) so the read matches the HUD.
//
// Everything here guards its DOM ops and never throws on the page: a
// broken render must not interfere with colonist. pointer-events are
// scoped to the overlay box only, so clicks outside its bounds always
// reach the board.

(function bootCatanbotOverlay() {
    const LOG_PREFIX = '[catanbot-overlay]';
    const BRIDGE_BASE = 'http://127.0.0.1:8765';
    const POLL_MS = 500;

    // localStorage keys (colonist.io origin). The toggle is mirrored here
    // from chrome.storage.local for a synchronous read at boot; geometry
    // / minimized state live here too so they survive reloads.
    const LS_ON = 'catanbot.overlay';
    const LS_GEOM = 'catanbot.overlay.geom';
    const LS_MIN = 'catanbot.overlay.min';

    // ---- Resource glyphs + colors (mirrors panel.js RES_EMOJI / COLOR_HEX
    // so the read looks the same as the side panel without depending on
    // panel.css, which lives in the side-panel iframe, not the page). ----
    const RES_EMOJI = {
        WOOD: '🌲', BRICK: '🧱', SHEEP: '🐑',
        WHEAT: '🌾', ORE: '⛰️',
    };
    const RES_ABBREV = {
        WOOD: 'Wd', BRICK: 'Br', SHEEP: 'Sh', WHEAT: 'Wh', ORE: 'Or',
    };
    const COLOR_HEX = {
        RED: '#e8715f', BLUE: '#4aa7d4', ORANGE: '#e29a4a',
        WHITE: '#f0f0f0', GREEN: '#7ac74f', BROWN: '#a07045',
    };
    const KIND_LABEL = {
        settlement: 'settle',
        city: 'city',
        road: 'road',
        dev_card: 'dev card',
        trade: 'trade',
        propose_trade: 'propose',
        bank_trade: 'port/bank',
        discard: 'discard',
        opening_settlement: 'settle',
    };

    function iconFor(res) {
        return RES_EMOJI[res]
            || RES_ABBREV[res]
            || String(res || '?').slice(0, 2);
    }
    function escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    // Pick the best available pill color - same precedence as panel.js
    // pillColor(): the colonist CSS color first, then the catanatron enum.
    function pillColor(player) {
        if (player && player.color_css) return player.color_css;
        if (player && player.color) return COLOR_HEX[player.color] || '#888';
        return '#888';
    }
    // Readable text color (black/white) for a given bg - trimmed copy of
    // panel.js contrastText(). Best-effort parse of hex / rgb().
    function contrastText(css) {
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
                    r = parseInt(m[1], 10);
                    g = parseInt(m[2], 10);
                    b = parseInt(m[3], 10);
                }
            }
        }
        if (r == null) return '#fff';
        // Relative luminance - pick black text on light backgrounds.
        const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
        return lum > 0.6 ? '#111' : '#fff';
    }

    // ---- Hand read: render one opp's hand_probs the same way panel.js
    // does ("wood 2 +67%" - guaranteed minimum plus a faded chance of
    // more, or a dim "ore 33%" when nothing is certain). Falls back to the
    // plain inferred hand + unknown count when hand_probs is absent. ----
    function handReadHtml(o) {
        const parts = [];
        const hp = o && o.hand_probs;
        if (hp) {
            for (const res of Object.keys(hp)) {
                const b = hp[res] || {};
                const mn = b.min || 0;
                const more = b.more || 0;   // P(more than the floor)
                const p1 = b.p1 || 0;       // P(at least one)
                if (mn > 0) {
                    const tail = more > 0.04
                        ? ` <span class="cbo-prob">+${Math.round(more * 100)}%</span>`
                        : '';
                    parts.push(
                        `<span class="cbo-chip">${iconFor(res)} ${mn}${tail}</span>`);
                } else if (p1 > 0.04) {
                    parts.push(
                        `<span class="cbo-chip cbo-maybe">${iconFor(res)} `
                        + `<span class="cbo-prob">${Math.round(p1 * 100)}%</span></span>`);
                }
            }
        } else {
            const hand = (o && o.hand) || {};
            for (const res of Object.keys(hand)) {
                const n = hand[res];
                if (n > 0) {
                    parts.push(`<span class="cbo-chip">${iconFor(res)} ${n}</span>`);
                }
            }
            if (o && (o.unknown || 0) > 0) {
                parts.push(`<span class="cbo-chip">? ${o.unknown}</span>`);
            }
        }
        return parts.join('');
    }

    // ---- Top recommendation: one compact line. We don't have the side
    // panel's full tile-chip renderer here (it depends on panel.css), so
    // compose a plain-text read from the same rec fields: a kind label,
    // the tile resource/number chips, and the detail prose. ----
    function tilesText(arr) {
        return (arr || [])
            .filter(t => t && t[0] !== 'DESERT')
            .map(t => {
                const icon = iconFor(t[0]);
                const num = t[1];
                return num == null ? icon : `${num}${icon}`;
            })
            .join(' ');
    }
    function topRecHtml(rec) {
        if (!rec) return '';
        const effectiveKind = (rec.action === 'road') ? 'road' : rec.kind;
        const kindLabel = KIND_LABEL[effectiveKind]
            || String(effectiveKind || '').replace(/_/g, ' ');
        const tiles = tilesText(rec.tiles);
        const arrow = (rec.kind === 'road' && tiles) ? '→ ' : '';
        const detail = rec.detail ? ` ${rec.detail}` : '';
        const score = Number(rec.score || 0);
        const scoreStr = Number.isFinite(score) ? score.toFixed(1) : '';
        const loc = tiles ? ` <span class="cbo-rec-tiles">${arrow}${escapeHtml(tiles)}</span>` : '';
        return `<span class="cbo-rec-score">${escapeHtml(scoreStr)}</span>`
            + `<span class="cbo-rec-kind">${escapeHtml(kindLabel)}</span>`
            + loc
            + `<span class="cbo-rec-detail">${escapeHtml(detail)}</span>`;
    }

    // ---- Self-contained styling. Injected once, scoped under #cbo-root so
    // it can't leak into colonist's DOM. pointer-events are ON only for
    // the overlay box; the wrapper is display:contents and the box is the
    // single interactive surface, so clicks anywhere else fall through. ----
    const STYLE_ID = 'cbo-style';
    function ensureStyle() {
        try {
            if (document.getElementById(STYLE_ID)) return;
            const style = document.createElement('style');
            style.id = STYLE_ID;
            style.textContent = `
#cbo-root {
    position: fixed;
    z-index: 2147483600;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 12px;
    line-height: 1.35;
    color: #e8e8ea;
    background: rgba(20, 22, 28, 0.94);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 8px;
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.45);
    width: 320px;
    min-width: 200px;
    min-height: 40px;
    max-height: 80vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    /* pointer-events live on the box itself; nothing outside it blocks
       colonist clicks because the box is the only element on the page. */
    pointer-events: auto;
    user-select: none;
    resize: both;
}
#cbo-root.cbo-min {
    height: auto !important;
    min-height: 0;
    resize: none;
}
#cbo-root.cbo-min .cbo-body { display: none; }
#cbo-head {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 8px;
    background: rgba(255, 255, 255, 0.06);
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    cursor: move;
    flex: 0 0 auto;
}
#cbo-root.cbo-min #cbo-head { border-bottom: none; }
.cbo-title {
    font-weight: 600;
    letter-spacing: 0.03em;
    color: #cfd2d8;
    flex: 1 1 auto;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.cbo-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: #777;
    flex: 0 0 auto;
}
.cbo-dot.cbo-live { background: #16a34a; }
.cbo-btn {
    cursor: pointer;
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: #d8dade;
    border-radius: 4px;
    width: 18px; height: 18px;
    line-height: 14px;
    text-align: center;
    font-size: 12px;
    padding: 0;
    flex: 0 0 auto;
}
.cbo-btn:hover { background: rgba(255, 255, 255, 0.16); }
.cbo-body {
    padding: 6px 8px 8px;
    overflow-y: auto;
    flex: 1 1 auto;
}
.cbo-sec {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #8b8f99;
    margin: 4px 0 3px;
}
.cbo-rec {
    background: rgba(122, 199, 79, 0.10);
    border: 1px solid rgba(122, 199, 79, 0.30);
    border-radius: 6px;
    padding: 5px 7px;
    margin-bottom: 6px;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 5px;
}
.cbo-rec-score {
    font-weight: 700;
    color: #7ac74f;
}
.cbo-rec-kind {
    font-weight: 600;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.04em;
}
.cbo-rec-tiles { color: #e8e8ea; }
.cbo-rec-detail { color: #b6b9c0; flex: 1 1 100%; font-size: 11px; }
.cbo-opp {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 5px;
    padding: 3px 0;
    border-top: 1px solid rgba(255, 255, 255, 0.05);
}
.cbo-opp:first-of-type { border-top: none; }
.cbo-pill {
    display: inline-block;
    padding: 1px 7px;
    border-radius: 9px;
    font-weight: 600;
    font-size: 11px;
    max-width: 110px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.cbo-hand {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 6px;
    flex: 1 1 auto;
}
.cbo-chip { white-space: nowrap; }
.cbo-maybe { opacity: 0.6; }
.cbo-prob { color: #9aa0aa; font-size: 10px; }
.cbo-empty { color: #8b8f99; font-style: italic; padding: 4px 0; }
`;
            (document.head || document.documentElement).appendChild(style);
        } catch (_) { /* never throw on the page */ }
    }

    // ---- Geometry persistence ----
    function loadGeom() {
        try {
            const raw = localStorage.getItem(LS_GEOM);
            if (!raw) return null;
            const g = JSON.parse(raw);
            if (g && typeof g === 'object') return g;
        } catch (_) {}
        return null;
    }
    function saveGeom(g) {
        try { localStorage.setItem(LS_GEOM, JSON.stringify(g)); }
        catch (_) {}
    }
    function loadMin() {
        try { return localStorage.getItem(LS_MIN) === '1'; }
        catch (_) { return false; }
    }
    function saveMin(on) {
        try { localStorage.setItem(LS_MIN, on ? '1' : '0'); }
        catch (_) {}
    }

    // ---- Overlay element lifecycle ----
    let root = null;          // #cbo-root box
    let bodyEl = null;        // .cbo-body
    let dotEl = null;         // live dot
    let pollTimer = null;
    let overlayOn = false;
    let minimized = loadMin();

    function clampToViewport(left, top, width) {
        const vw = window.innerWidth || 1280;
        const vh = window.innerHeight || 800;
        const w = width || 320;
        const maxLeft = Math.max(0, vw - Math.min(w, vw) * 0.4);
        const maxTop = Math.max(0, vh - 40);
        return {
            left: Math.min(Math.max(0, left), maxLeft),
            top: Math.min(Math.max(0, top), maxTop),
        };
    }

    function buildOverlay() {
        if (root) return;
        ensureStyle();
        try {
            root = document.createElement('div');
            root.id = 'cbo-root';

            const head = document.createElement('div');
            head.id = 'cbo-head';

            dotEl = document.createElement('span');
            dotEl.className = 'cbo-dot';

            const title = document.createElement('span');
            title.className = 'cbo-title';
            title.textContent = 'CatanBot';

            const minBtn = document.createElement('button');
            minBtn.className = 'cbo-btn';
            minBtn.type = 'button';
            minBtn.title = 'minimize / expand';
            minBtn.textContent = minimized ? '+' : '_';

            head.appendChild(dotEl);
            head.appendChild(title);
            head.appendChild(minBtn);

            bodyEl = document.createElement('div');
            bodyEl.className = 'cbo-body';
            bodyEl.innerHTML = '<div class="cbo-empty">waiting for bridge…</div>';

            root.appendChild(head);
            root.appendChild(bodyEl);

            // Apply saved geometry, else default to top-right of the board.
            const g = loadGeom();
            const width = (g && Number.isFinite(g.width)) ? g.width : 320;
            const height = (g && Number.isFinite(g.height)) ? g.height : null;
            let left = (g && Number.isFinite(g.left)) ? g.left
                : Math.max(0, (window.innerWidth || 1280) - width - 24);
            let top = (g && Number.isFinite(g.top)) ? g.top : 96;
            const cl = clampToViewport(left, top, width);
            root.style.left = cl.left + 'px';
            root.style.top = cl.top + 'px';
            root.style.width = width + 'px';
            if (height) root.style.height = height + 'px';

            if (minimized) root.classList.add('cbo-min');

            (document.body || document.documentElement).appendChild(root);

            // Minimize toggle.
            minBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                minimized = !minimized;
                root.classList.toggle('cbo-min', minimized);
                minBtn.textContent = minimized ? '+' : '_';
                saveMin(minimized);
            });

            // Drag by the header bar.
            wireDrag(head);
            // Persist size after a resize gesture settles.
            wireResizePersist();
        } catch (err) {
            // Tear down a half-built overlay so we never leave a broken
            // node on the page.
            try { if (root && root.parentNode) root.parentNode.removeChild(root); }
            catch (_) {}
            root = null; bodyEl = null; dotEl = null;
            console.warn(LOG_PREFIX, 'build failed', err);
        }
    }

    function destroyOverlay() {
        try {
            if (root && root.parentNode) root.parentNode.removeChild(root);
        } catch (_) {}
        root = null; bodyEl = null; dotEl = null;
    }

    function wireDrag(handle) {
        let dragging = false;
        let startX = 0, startY = 0, baseLeft = 0, baseTop = 0;
        handle.addEventListener('mousedown', (e) => {
            // Ignore clicks on the minimize button (handled separately).
            if (e.target && e.target.classList
                    && e.target.classList.contains('cbo-btn')) return;
            dragging = true;
            startX = e.clientX;
            startY = e.clientY;
            const r = root.getBoundingClientRect();
            baseLeft = r.left;
            baseTop = r.top;
            e.preventDefault();
        });
        // Listen on window so a fast drag that outruns the cursor over the
        // box still tracks. Guard every op.
        window.addEventListener('mousemove', (e) => {
            if (!dragging || !root) return;
            const nx = baseLeft + (e.clientX - startX);
            const ny = baseTop + (e.clientY - startY);
            const cl = clampToViewport(nx, ny, root.offsetWidth);
            root.style.left = cl.left + 'px';
            root.style.top = cl.top + 'px';
        });
        window.addEventListener('mouseup', () => {
            if (!dragging || !root) { dragging = false; return; }
            dragging = false;
            persistGeom();
        });
    }

    // The box uses CSS `resize: both`; there's no resize event on plain
    // elements, so observe size changes and debounce a persist.
    function wireResizePersist() {
        try {
            if (typeof ResizeObserver === 'undefined') return;
            let t = null;
            const ro = new ResizeObserver(() => {
                if (t) return;
                t = setTimeout(() => { t = null; persistGeom(); }, 250);
            });
            ro.observe(root);
        } catch (_) {}
    }

    function persistGeom() {
        if (!root) return;
        try {
            const r = root.getBoundingClientRect();
            saveGeom({
                left: Math.round(r.left),
                top: Math.round(r.top),
                width: Math.round(root.offsetWidth),
                height: minimized ? null : Math.round(root.offsetHeight),
            });
        } catch (_) {}
    }

    // ---- Render from a snapshot ----
    function setLive(live) {
        try {
            if (dotEl) dotEl.classList.toggle('cbo-live', !!live);
        } catch (_) {}
    }

    function render(snap) {
        if (!bodyEl) return;
        try {
            const out = [];

            // Top recommendation - only on our turn (or setup), matching the
            // side panel's gate. recommendations[0] is the hero rec.
            const recs = (snap && snap.recommendations) || [];
            const showRecs = (snap && (snap.my_turn || snap.setup_phase));
            if (showRecs && recs.length) {
                out.push('<div class="cbo-sec">top move</div>');
                out.push(`<div class="cbo-rec">${topRecHtml(recs[0])}</div>`);
            }

            // Opponent hand reads. Drop placeholder seats (bots /
            // disconnected players whose real names never reached us).
            const opps = ((snap && snap.opps) || []).filter(
                o => o && !o.is_placeholder);
            out.push('<div class="cbo-sec">opponents</div>');
            if (opps.length) {
                for (const o of opps) {
                    const bg = pillColor(o);
                    const fg = contrastText(bg);
                    const name = escapeHtml(o.username || '?');
                    const hand = handReadHtml(o);
                    out.push('<div class="cbo-opp">'
                        + `<span class="cbo-pill" style="background:${bg};color:${fg};">${name}</span>`
                        + `<span class="cbo-hand">${hand}</span>`
                        + '</div>');
                }
            } else {
                out.push('<div class="cbo-empty">no opponents yet</div>');
            }

            bodyEl.innerHTML = out.join('');
        } catch (err) {
            // Never let a render throw bubble onto the page. Leave the last
            // good content in place.
            console.warn(LOG_PREFIX, 'render failed', err);
        }
    }

    // ---- Bridge polling ----
    async function pollOnce() {
        if (!overlayOn || !root) return;
        try {
            const resp = await fetch(`${BRIDGE_BASE}/advisor`, {
                method: 'GET',
                cache: 'no-store',
            });
            if (!resp.ok) { setLive(false); return; }
            const snap = await resp.json();
            setLive(true);
            render(snap);
        } catch (_) {
            // Bridge down / unreachable. Show a quiet placeholder once,
            // keep the dot grey, and keep trying.
            setLive(false);
            if (bodyEl && !bodyEl.dataset.cboDown) {
                try {
                    bodyEl.innerHTML =
                        '<div class="cbo-empty">bridge not reachable</div>';
                    bodyEl.dataset.cboDown = '1';
                } catch (_) {}
            }
        }
        if (bodyEl && bodyEl.dataset.cboDown) {
            // Clear the down-flag once we recover so render() repaints.
            // (render already overwrote innerHTML on a good poll.)
            delete bodyEl.dataset.cboDown;
        }
    }

    function startPolling() {
        if (pollTimer) return;
        pollOnce();
        pollTimer = setInterval(pollOnce, POLL_MS);
    }
    function stopPolling() {
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    // ---- Enable / disable ----
    function applyOverlay(on) {
        overlayOn = !!on;
        if (overlayOn) {
            buildOverlay();
            if (root) startPolling();
        } else {
            stopPolling();
            destroyOverlay();
        }
    }

    // ---- Toggle wiring. Mirror of content.js's streamer-mode pattern:
    // synchronous read of the colonist-origin localStorage mirror for an
    // instant first paint, then the authoritative chrome.storage.local
    // read + change listener. ----
    try {
        if (localStorage.getItem(LS_ON) === '1') {
            // Defer the actual build until the DOM is ready enough to append.
            overlayOn = true;
        }
    } catch (_) {}

    function boot() {
        try {
            chrome.storage.local.get(['overlay'], (res) => {
                const on = !!(res && res.overlay);
                try {
                    localStorage.setItem(LS_ON, on ? '1' : '0');
                } catch (_) {}
                applyOverlay(on);
            });
            chrome.storage.onChanged.addListener((changes, area) => {
                if (area !== 'local' || !changes.overlay) return;
                const on = !!changes.overlay.newValue;
                try {
                    localStorage.setItem(LS_ON, on ? '1' : '0');
                } catch (_) {}
                applyOverlay(on);
            });
        } catch (_) {
            // Extension context invalidated (e.g. after a reload while the
            // tab stays open). Fall back to the localStorage mirror so a
            // previously-enabled overlay still appears.
            if (overlayOn) applyOverlay(true);
        }
    }

    // document_start can run before <body> exists; wait for it so the
    // append target is present.
    if (document.body) {
        boot();
    } else {
        document.addEventListener('DOMContentLoaded', boot, { once: true });
        // Belt-and-suspenders: if DOMContentLoaded already fired in a race,
        // a short poll catches the body.
        const bw = setInterval(() => {
            if (document.body) {
                clearInterval(bw);
                if (!root && !pollTimer) boot();
            }
        }, 50);
        setTimeout(() => clearInterval(bw), 10000);
    }
})();
