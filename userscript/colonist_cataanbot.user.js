// ==UserScript==
// @name         cataanbot — colonist.io log bridge
// @namespace    https://github.com/NoahLaforet/CataanBot
// @version      0.15.0
// @description  Streams colonist.io game-log events + WebSocket frames to the cataanbot FastAPI bridge on localhost:8765. v0.10.1 bumps HUD font 12→14px and width 280→340px for readability; v0.10.0 added the incoming-trade accept/decline panel.
// @author       Noah Laforet
// @match        https://colonist.io/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

/* eslint-disable no-console */
(() => {
    'use strict';

    const BRIDGE_URL = 'http://127.0.0.1:8765/log';
    const BRIDGE_WS_URL = 'http://127.0.0.1:8765/ws';
    const BRIDGE_ADVISOR_URL = 'http://127.0.0.1:8765/advisor';
    const ADVISOR_POLL_MS = 1000;
    const LOG_PREFIX = '[cataanbot]';

    // Fire-and-forget POST. Used by both the DOM log forwarder (/log)
    // and the WS frame forwarder (/ws). Keeps the userscript quiet even
    // if the bridge is down so a game session isn't noisy.
    function postTo(url, payload, { quiet } = {}) {
        if (typeof GM_xmlhttpRequest === 'function') {
            GM_xmlhttpRequest({
                method: 'POST',
                url,
                headers: { 'Content-Type': 'application/json' },
                data: JSON.stringify(payload),
                onerror: (e) => { if (!quiet)
                    console.warn(LOG_PREFIX, 'POST failed', e); },
            });
        } else {
            fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                mode: 'cors',
            }).catch(e => { if (!quiet)
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
            if (typeof GM_xmlhttpRequest === 'function') {
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
    // Emoji icons render at a glance vs. letter abbrevs — on a dense
    // HUD, 🌲 is faster to parse than "Wd". Kept the abbrev map as a
    // fallback for callers that want text, but the main renderers use
    // icons.
    const RES_ICON = {
        WOOD: '🌲', BRICK: '🧱', SHEEP: '🐑',
        WHEAT: '🌾', ORE: '⛰️',
    };
    const iconFor = (res) => RES_ICON[res]
        || RES_ABBREV[res] || (res || '?').slice(0, 2);

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

    function mountOverlay() {
        if (!document.body) return null;
        let host = document.getElementById('cataanbot-overlay-host');
        if (host && host.shadowRoot) {
            // Already mounted (e.g. via a re-entry path). Rewire the
            // ui handle so callers can still drive renders.
            const root = host.shadowRoot;
            return {
                host,
                panel: root.getElementById('panel'),
                body: root.getElementById('body'),
                content: root.getElementById('content'),
                dot: root.getElementById('dot'),
            };
        }
        host = document.createElement('div');
        host.id = 'cataanbot-overlay-host';
        host.style.cssText = 'position:fixed;top:12px;right:12px;'
            + 'z-index:2147483647;pointer-events:auto;';
        const root = host.attachShadow({ mode: 'open' });
        root.innerHTML = `
<style>
  :host, * { box-sizing: border-box; }
  /* --panel-w / --panel-h / --font-scale are mutated by the resize
     handle. Font size scales with width so "bigger HUD" means bigger
     text, not just spread-out text. Every font declaration below uses
     calc(base * var(--font-scale)) so one knob controls everything. */
  .panel {
    --panel-w: 340px;
    --panel-h: auto;
    --font-scale: 1;
    font: calc(14px * var(--font-scale))/1.4 ui-monospace, Menlo, Consolas, monospace;
    color: #e8e8e8;
    background: rgba(18, 18, 22, 0.94);
    border: 1px solid #2a2a32;
    border-radius: 6px;
    width: var(--panel-w);
    height: var(--panel-h);
    box-shadow: 0 6px 24px rgba(0,0,0,0.45);
    user-select: none;
    position: relative;
  }
  /* Resize handle in the bottom-right corner. Diagonal ticks hint at
     drag-to-resize. Pointer-events are only enabled on the handle so
     text selection inside the panel still works. */
  .resize-handle {
    position: absolute; right: 0; bottom: 0;
    width: 14px; height: 14px;
    cursor: nwse-resize;
    background:
      linear-gradient(135deg,
        transparent 0%, transparent 45%,
        #4a4a55 45%, #4a4a55 50%,
        transparent 50%, transparent 65%,
        #4a4a55 65%, #4a4a55 70%,
        transparent 70%);
    border-bottom-right-radius: 6px;
    opacity: 0.6;
  }
  .resize-handle:hover { opacity: 1; }
  /* If the body is scrollable (user made the panel short), let it
     scroll rather than overflow the panel container. */
  .body.scrollable { overflow-y: auto; }
  .header {
    display: flex; align-items: center; gap: 6px;
    padding: 6px 8px;
    cursor: move;
    border-bottom: 1px solid #2a2a32;
    background: linear-gradient(180deg, #23232a, #1a1a20);
    border-radius: 6px 6px 0 0;
  }
  .title { font-weight: 600; flex: 1; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #555; }
  .dot.live { background: #7ac74f; }
  .btn {
    cursor: pointer; padding: 0 6px; color: #aaa;
    border: 1px solid #2a2a32; border-radius: 3px;
    background: transparent;
  }
  .btn:hover { color: #fff; border-color: #444; }
  .body { padding: 8px; }
  .body.collapsed { display: none; }
  .you {
    display: flex; align-items: baseline; gap: 6px;
    margin-bottom: 2px;
  }
  .color-pill {
    display: inline-block;
    padding: 1px 6px; border-radius: 3px;
    color: #111; font-weight: 700;
  }
  .hand { color: #d0d0d0; margin: 2px 0; }
  .afford { color: #b8e8b8; margin: 0 0 6px; }
  .afford.none { color: #888; }
  .vpb { color: #888; font-size: calc(11px * var(--font-scale)); }
  .ports { color: #78b4d8; font-size: calc(11px * var(--font-scale));
           margin: 0 0 4px; }
  .prod { color: #9ecfaa; font-size: calc(11px * var(--font-scale));
          margin: 0 0 4px; }
  .hr { height: 1px; background: #2a2a32; margin: 6px 0; }
  .opps { display: grid; grid-template-columns: 1fr; gap: 2px; }
  .opp { color: #ccc; font-size: calc(13px * var(--font-scale)); }
  .opp-hand { color: #b5c4d0; font-variant-numeric: tabular-nums; }
  .opp.tracked .opp-hand { color: #a4ef9c; }
  .opp.hot-knight { color: #ffaa55; }
  /* Tag that shows builds an opp can definitely pay for right now.
     Amber because it's a near-term threat (next-turn +VP in the
     worst case) without being a red-alert on its own. */
  .opp .can-afford { color: #ffb347; font-weight: 600; }
  .roll { margin: 4px 0 2px; color: #d8d8d8; }
  .roll.you-rolled { color: #ffde7a; }
  .robber-h { color: #ff9066; font-weight: 600; margin-top: 4px; }
  table.robber { width: 100%; border-collapse: collapse; margin-top: 2px; }
  table.robber td { padding: 1px 4px 1px 0; vertical-align: top; }
  .recs-h { color: #7ac74f; font-weight: 600; margin: 6px 0 2px; }
  .rec { color: #d8d8d8; margin: 1px 0; }
  .rec .kind {
    display: inline-block; min-width: 56px; color: #ffde7a; font-weight: 600;
  }
  .rec.top { color: #fff; font-weight: 600; }
  .rec .detail { color: #aaa; font-weight: 400; }
  .rec .score {
    display: inline-block; min-width: 52px;
    padding: 0 5px; border-radius: 3px;
    font-weight: 700; text-align: center;
    font-variant-numeric: tabular-nums;
  }
  .rec .score.strong { background: #1e4d2b; color: #a4ef9c; }
  .rec .score.decent { background: #504620; color: #ffe07a; }
  .rec .score.weak   { background: #2a2a32; color: #999; }
  .rec .tiles { color: #b8c6d6; font-weight: 500; }
  /* Tile chips: number-first so 6/8 "red pip" rolls pop visually.
     Resource abbrev follows in a softer color; chips space out for
     easier scan than dot-separated tokens. */
  .tile-chip { display: inline-block; margin-right: 6px;
               font-variant-numeric: tabular-nums; }
  .tile-num { color: #ffd36e; font-weight: 700; }
  .tile-num.hot { color: #ff7b7b; }
  .tile-res { color: #b8c6d6; font-weight: 500; margin-left: 2px; }
  .recs-h.plan-h { color: #7aa7d6; margin-top: 4px; }
  .rec.plan { opacity: 0.85; font-style: italic; }
  .rec.plan .kind { color: #a0c8f0; }
  /* Road-direction hint under an opening-settlement pick. */
  .rec-sub { color: #9ad0b5; font-size: calc(13px * var(--font-scale));
             padding: 0 8px 3px 62px; opacity: 0.95; }
  .rec-sub .warn { color: #f0a57a; font-weight: 500; }
  .rec-sub .arrow { color: #7a9aa8; margin-right: 4px; }
  /* Paired-2nd-settlement hint — blue accent to distinguish from road. */
  .rec-sub.plan-second { color: #a0c0e8; }
  .rec-sub.plan-second .arrow { color: #7a9ab8; }
  .rec-sub.plan-second .cov { color: #8fb0d8; margin-left: 6px;
                              font-variant: tabular-nums; }
  /* Strategy-archetype tag (balanced / wood-first / ore-city / port /
     dev-card). */
  .rec-sub.plan-second .arch { background: rgba(160, 192, 232, 0.18);
                               color: #d0e0f5; border-radius: 6px;
                               padding: 1px 6px; margin-left: 6px;
                               font-size: calc(11px * var(--font-scale)); font-weight: 500;
                               text-transform: lowercase;
                               letter-spacing: 0.02em; }
  /* Option letter (A/B/C/D) shown before round-1 picks so Noah can
     reference them by letter — "I'm taking Option B". */
  .rec .opt { display: inline-block; min-width: 22px; margin-right: 6px;
              padding: 1px 6px; border-radius: 4px;
              background: rgba(255, 222, 122, 0.16); color: #ffde7a;
              font-weight: 600; font-size: calc(12px * var(--font-scale));
              font-variant: tabular-nums; text-align: center; }
  /* Trade recs wear a distinct color so "spend 4 for 1" reads as
     something other than a straight build action. */
  .rec.trade .kind { color: #f0a57a; }
  .turn-hint { color: #888; margin-top: 4px; font-style: italic; }
  .drift { color: #ff9999; margin: 2px 0; font-size: calc(13px * var(--font-scale)); }
  /* Incoming trade panel — pops up when an opponent makes an offer and
     vanishes on the next roll/commit. Verdict pill color signals the
     advice at a glance (green=accept, red=decline, muted=consider). */
  .trade-offer {
    border: 1px solid #3a3a4a; border-radius: 4px;
    padding: 4px 6px; margin: 6px 0 4px;
    background: #1a1d24;
  }
  .trade-offer .trade-h { color: #f0a57a; font-weight: 600;
                          margin-bottom: 2px; }
  .trade-offer .trade-body { color: #d8d8d8; font-size: calc(13px * var(--font-scale));
                             margin: 1px 0; }
  .trade-offer .trade-reason { color: #aaa; font-size: calc(13px * var(--font-scale));
                               font-style: italic; margin-top: 2px; }
  .trade-offer .verdict {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-weight: 700; letter-spacing: 0.5px; margin-right: 4px;
  }
  .trade-offer .verdict.accept  { background: #1e4d2b; color: #a4ef9c; }
  .trade-offer .verdict.decline { background: #4d1e1e; color: #ef9c9c; }
  .trade-offer .verdict.consider{ background: #404040; color: #ddd; }
  .trade-offer .swap-side { color: #b8c6d6; }
  .trade-offer .swap-arrow { color: #7a9aa8; margin: 0 4px; }
  .trade-offer .counter {
    margin-top: 4px; padding: 3px 6px; border-radius: 3px;
    background: #1c2b34; border-left: 2px solid #7ac7e8;
    font-size: calc(13px * var(--font-scale)); color: #c8dde8;
  }
  .trade-offer .counter .counter-h {
    color: #7ac7e8; font-weight: 600; margin-right: 4px;
  }
  .trade-offer .counter .counter-reason {
    color: #8aa0ab; font-style: italic; margin-left: 4px;
  }
  .victim-top { color: #ffd36e; font-weight: 700; }
  .knight-hint {
    border: 1px solid #3a3a4a; border-radius: 4px;
    padding: 4px 6px; margin: 6px 0 4px;
    background: #1a1d24;
  }
  .knight-hint .kh-h {
    color: #ffd36e; font-weight: 600; margin-bottom: 2px;
  }
  .knight-hint .kh-reason {
    color: #d8d8d8; font-size: calc(13px * var(--font-scale)); margin: 1px 0;
  }
  .knight-hint .kh-verdict {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-weight: 700; letter-spacing: 0.5px; margin-right: 4px;
  }
  .knight-hint .kh-verdict.play { background: #1e4d2b; color: #a4ef9c; }
  .knight-hint .kh-verdict.hold { background: #404040; color: #ddd; }
  .dev-hint {
    border: 1px solid #3a3a4a; border-radius: 4px;
    padding: 4px 6px; margin: 4px 0;
    background: #1a1d24;
  }
  .dev-hint .dv-h { color: #9ec7ff; font-weight: 600; margin-bottom: 2px; }
  .dev-hint .dv-body { color: #d8d8d8; font-size: calc(13px * var(--font-scale)); }
  .dev-hint .dv-unlock { color: #a4ef9c; font-style: italic; margin-left: 4px; }
  .threat {
    border-radius: 4px; padding: 4px 6px; margin: 6px 0 4px;
    font-weight: 600;
  }
  .threat.mid { background: #2a2a18; color: #e0d480; border: 1px solid #555538; }
  .threat.close { background: #3a1f14; color: #ff9e5e; border: 1px solid #6b3828; }
  .threat.win { background: #4a1414; color: #ff5e5e; border: 1px solid #8b3333; }
  .robber-on-me {
    border-radius: 4px; padding: 4px 6px; margin: 6px 0 4px;
    background: #2a1a28; color: #d8a0d8; border: 1px solid #553355;
    font-weight: 600;
  }
  .robber-on-me .rom-sub {
    display: block; font-weight: 400; font-size: calc(12px * var(--font-scale));
    color: #a88cb0; margin-top: 2px;
  }
  .lr-race {
    border-radius: 4px; padding: 4px 6px; margin: 6px 0 4px;
    font-weight: 600;
  }
  .lr-race.self_push { background: #1a2a1f; color: #9be89e; border: 1px solid #2a5538; }
  .lr-race.opp_threat { background: #2a1f14; color: #ff9e5e; border: 1px solid #6b3828; }
  .lr-race.contested { background: #1a1f2a; color: #9ecfff; border: 1px solid #2e3b55; }
  .la-race {
    border-radius: 4px; padding: 4px 6px; margin: 6px 0 4px;
    font-weight: 600;
  }
  .la-race.self_push { background: #1a2a1f; color: #9be89e; border: 1px solid #2a5538; }
  .la-race.opp_threat { background: #2a1f14; color: #ff9e5e; border: 1px solid #6b3828; }
  .la-race.contested { background: #1a1f2a; color: #9ecfff; border: 1px solid #2e3b55; }
  .bank-low {
    border-radius: 4px; padding: 4px 6px; margin: 6px 0 4px;
    background: #1f1f14; color: #e8d878; border: 1px solid #4a4028;
    font-weight: 600;
  }
  .bank-low .bl-sub {
    display: block; font-weight: 400; font-size: calc(12px * var(--font-scale));
    color: #a89c68; margin-top: 2px;
  }
  .dev-deck {
    color: #888; font-size: calc(11px * var(--font-scale));
    margin: 2px 0;
  }
  .dev-deck.low { color: #e8d878; font-weight: 600; }
  .discard-hint {
    border: 1px solid #5a2a2a; border-radius: 4px;
    padding: 4px 6px; margin: 6px 0 4px;
    background: #2a1a1a;
  }
  .discard-hint .dh-h {
    color: #ff9e5e; font-weight: 700; margin-bottom: 2px;
  }
  .discard-hint .dh-drops {
    color: #f0d8c0; font-size: calc(13px * var(--font-scale));
  }
  .discard-hint .dh-reason {
    color: #bfa68a; font-style: italic;
    font-size: calc(12px * var(--font-scale));
  }
  .muted { color: #888; }
  .err { color: #ff9999; }
</style>
<div class="panel" id="panel">
  <div class="header" id="header">
    <span class="dot" id="dot"></span>
    <span class="title">CataanBot</span>
    <button class="btn" id="toggle" title="collapse/expand">_</button>
  </div>
  <div class="body" id="body">
    <div id="content"><span class="muted">waiting for bridge…</span></div>
  </div>
  <div class="resize-handle" id="resize-handle" title="drag to resize"></div>
</div>`;

        document.body.appendChild(host);

        const panel = root.getElementById('panel');
        const body = root.getElementById('body');
        const content = root.getElementById('content');
        const header = root.getElementById('header');
        const dot = root.getElementById('dot');
        root.getElementById('toggle').addEventListener('click', (e) => {
            e.stopPropagation();
            body.classList.toggle('collapsed');
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
        const PANEL_W_MIN = 260, PANEL_W_MAX = 720;
        const BASE_W = 340;
        function scaleForWidth(w) {
            // 340→1.0, 640→1.5 — linear, clamped to [0.85, 1.6].
            const s = 1.0 + (w - BASE_W) * 0.5 / 300;
            return Math.max(0.85, Math.min(1.6, s));
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

        const handle = root.getElementById('resize-handle');
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

        return { host, panel, body, content, dot };
    }

    function renderOverlay(ui, snap, live) {
        ui.dot.classList.toggle('live', !!live);
        if (!snap) {
            ui.content.innerHTML =
                '<span class="err">bridge unreachable</span>';
            return;
        }
        if (!snap.game_started) {
            ui.content.innerHTML =
                '<span class="muted">waiting for game start…</span>';
            return;
        }
        const parts = [];
        const me = snap.self;
        if (me) {
            const bg = pillColor(me);
            const fg = contrastText(bg);
            const pill = `<span class="color-pill" style="background:${bg};`
                + `color:${fg};">${escapeHtml(me.username)}</span>`;
            let piecesTag = '';
            if (me.pieces) {
                const p = me.pieces;
                // Compact s/c/r format: "3s/1c/6r". A zero count is still
                // worth showing — running out of a piece type locks off
                // that build permanently.
                piecesTag = ` · ${p.settle}s/${p.city}c/${p.road}r`;
            }
            // Knights played — only surface once any have been played,
            // since 0 is the boring default. At 3+ this earns largest
            // army; rendering it here lets Noah eyeball progress.
            const kpTag = (me.knights_played || 0) > 0
                ? ` · ${me.knights_played}k` : '';
            parts.push(`<div class="you">${pill}`
                + ` <span class="muted">${me.cards}c · ${me.vp} VP${piecesTag}${kpTag}</span></div>`);
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
            const hand = Object.entries(me.hand || {})
                .filter(([, n]) => n > 0)
                .map(([r, n]) => `${n} ${iconFor(r)}`)
                .join('  ') || '<span class="muted">∅</span>';
            parts.push(`<div class="hand">${hand}</div>`);
            // Hand-drift warning. Tracker's event-reconstructed breakdown
            // disagreed with colonist's authoritative card count — the
            // per-resource detail is unreliable until the next HandSync
            // frame corrects us. Typically caused by a ws disconnect.
            if (me.hand_drift) {
                parts.push('<div class="drift">⚠ hand detail stale '
                    + '(waiting for resync)</div>');
            }
            const afford = (me.afford || []).join(' · ');
            parts.push(afford
                ? `<div class="afford">→ ${afford}</div>`
                : `<div class="afford none">→ nothing buildable</div>`);
            // Owned ports: "2:1 whe · 2:1 shp · 3:1". Reminds Noah to
            // over-produce toward his cheap-trade resources. Skipped
            // silently when no ports are claimed yet.
            if ((me.ports || []).length) {
                const portSegs = me.ports.map(p => p === 'GENERIC'
                    ? '3:1'
                    : `2:1 ${p.slice(0, 3).toLowerCase()}`);
                parts.push(`<div class="ports">ports: `
                    + escapeHtml(portSegs.join(' · ')) + '</div>');
            }
            // Production rate — expected cards per dice roll given
            // current builds. Skip at 0 (setup phase) to avoid a
            // meaningless "0.00/roll" line.
            const prod = me.production;
            if (prod && prod.per_roll > 0) {
                const top = prod.top_resource
                    ? ` · strongest ${prod.top_resource.slice(0, 3).toLowerCase()}`
                    : '';
                parts.push(`<div class="prod">prod: `
                    + `${prod.per_roll.toFixed(2)}/roll${top}</div>`);
            }
        }
        // Setup-phase opening picks render unconditionally — it's
        // useful to plan around them even off-turn so you know what to
        // grab when your slot comes up.
        const isSetup = !!snap.setup_phase;
        // Recommendations — only shown when it's my turn (mid-game) or
        // during setup (always useful). Split into:
        //   "best moves"      — things affordable right now
        //   "planning ahead"  — 1-2 cards from a better move; "save for X"
        // Both groups sorted by score desc within the list the backend sent.
        if ((snap.my_turn || isSetup)
                && (snap.recommendations || []).length) {
            const nowRecs = [];
            const soonRecs = [];
            for (const r of snap.recommendations) {
                (r.when === 'soon' ? soonRecs : nowRecs).push(r);
            }
            // Tile chips: one span per producing tile, number-first so
            // 6/8 (red-pip rolls) jump out. Returns a string of HTML
            // fragments joined without separators — spacing is CSS.
            const tilesToHtml = (arr) => (arr || [])
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
            const renderRec = (r, isTop, optLetter) => {
                const topCls = isTop ? ' top' : '';
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
                }[r.kind] || r.kind.replace(/_/g, ' ');
                const tilesHtml = tilesToHtml(r.tiles);
                // Roads lead to a landing spot — arrow makes it read as
                // "this road → these tiles" rather than "on these tiles".
                const arrowHtml = r.kind === 'road'
                    ? '<span class="arrow">→</span> '
                    : '';
                const loc = tilesHtml
                    ? ` ${arrowHtml}${tilesHtml}`
                    : '';
                const s = Number(r.score || 0);
                const scoreCls = s >= 8 ? 'strong'
                    : (s >= 5 ? 'decent' : 'weak');
                const planCls = r.when === 'soon' ? ' plan' : '';
                const tradeCls = (r.kind === 'trade'
                    || r.kind === 'propose_trade') ? ' trade' : '';
                // Option A/B/C/D label — only during opening picks so
                // Noah can say "I'm taking Option B" out loud with a
                // friend across the table.
                const optHtml = optLetter
                    ? `<span class="opt">${optLetter}</span>`
                    : '';
                parts.push(`<div class="rec${topCls}${planCls}${tradeCls}">`
                    + optHtml
                    + `<span class="score ${scoreCls}">${s.toFixed(1)}/10</span>`
                    + ` <span class="kind">${kindLabel}</span>`
                    + `<span class="tiles">${loc}</span> `
                    + `<span class="detail">${escapeHtml(r.detail || '')}`
                    + `</span></div>`);
                // Opening-settlement picks include a nested road hint:
                // "which direction to lay your road so it extends toward
                // the best 2-hop expansion spot." Render as a sub-line.
                if (r.kind === 'opening_settlement' && r.road
                        && r.road.toward_tiles) {
                    const towardHtml = tilesToHtml(r.road.toward_tiles);
                    if (towardHtml) {
                        const warn = r.road.contested
                            ? ' <span class="warn">⚠ contested</span>'
                            : '';
                        parts.push('<div class="rec-sub">'
                            + '<span class="arrow">↳ road →</span> '
                            + towardHtml
                            + warn
                            + '</div>');
                    }
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
            if (nowRecs.length) {
                const header = isSetup
                    ? '→ opening picks'
                    : '→ best moves';
                parts.push(`<div class="recs-h">${header}</div>`);
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
                parts.push('<div class="turn-hint">your turn — '
                    + 'nothing affordable</div>');
            }
            if (soonRecs.length) {
                parts.push('<div class="recs-h plan-h">'
                    + '→ planning ahead</div>');
                soonRecs.forEach(r => renderRec(r, false));
            }
        } else if (snap.my_turn) {
            parts.push('<div class="turn-hint">your turn — '
                + 'nothing affordable</div>');
        }
        if ((snap.opps || []).length) {
            parts.push('<div class="hr"></div>');
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
                        handParts.push(`${n} ${iconFor(res)}`);
                    }
                }
                if ((o.unknown || 0) > 0) {
                    handParts.push(`${o.unknown} ?`);
                }
                const breakdown = handParts.length
                    ? `<span class="opp-hand">${handParts.join('  ')}</span>`
                    : '';
                const trackCls = o.hand_tracked ? ' tracked' : '';
                const devTag = (o.dev_cards || 0) > 0
                    ? ` · ${o.dev_cards}dev`
                    : '';
                let piecesTag = '';
                if (o.pieces) {
                    const p = o.pieces;
                    piecesTag = ` · ${p.settle}s/${p.city}c/${p.road}r`;
                }
                // Played-knights counter — silent at 0, flags at 2+
                // (one away from largest army) so the overlay colors
                // pick that opp out of the list.
                const kpTag = (o.knights_played || 0) > 0
                    ? ` · ${o.knights_played}k` : '';
                const hotKnight = (o.knights_played || 0) >= 2;
                const rowCls = hotKnight ? ' hot-knight' : '';
                // Per-opp expected cards per roll. Drives robber and
                // trade-block priority — compare across rows to pick
                // the strongest engine. Silent at 0 (setup / robbed
                // out). 'p' is short for per-roll production.
                let prodTag = '';
                if (o.production && o.production.per_roll > 0) {
                    prodTag = ` · ${o.production.per_roll.toFixed(2)}p`;
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
                parts.push(`<div class="opp${trackCls}${rowCls}">${pill}`
                    + ` <span class="muted">${o.cards}c · ${o.vp}VP${devTag}${piecesTag}${kpTag}${prodTag}</span>${affordTag}`
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
            // Pack -> "1 🧱 2 🐑" for both sides of the swap.
            const fmtSide = (pack) => {
                const keys = Object.keys(pack || {});
                if (!keys.length) return '∅';
                return keys
                    .filter(r => pack[r] > 0)
                    .map(r => `${pack[r]} ${iconFor(r)}`)
                    .join(' ');
            };
            const verdictCls = ['accept', 'decline', 'consider']
                .includes(t.verdict) ? t.verdict : 'consider';
            const verdictLabel = verdictCls.toUpperCase();
            parts.push('<div class="trade-offer">');
            parts.push(`<div class="trade-h">incoming trade ${offererPill}`
                + `<span class="muted">${t.offerer_vp ?? 0} VP</span></div>`);
            parts.push('<div class="trade-body">'
                + '<span class="swap-side">gives ' + escapeHtml(fmtSide(t.give))
                + '</span><span class="swap-arrow">↔</span>'
                + '<span class="swap-side">wants ' + escapeHtml(fmtSide(t.want))
                + '</span></div>');
            parts.push('<div class="trade-reason">'
                + `<span class="verdict ${verdictCls}">${verdictLabel}</span>`
                + escapeHtml(t.reason || '') + '</div>');
            if (t.counter) {
                // Counter-offer is a fairer version we'd actually accept.
                // Show give→want like the main offer so Noah can type it in.
                parts.push('<div class="counter">'
                    + '<span class="counter-h">counter:</span>'
                    + '<span class="swap-side">ask '
                    + escapeHtml(fmtSide(t.counter.give))
                    + '</span><span class="swap-arrow">↔</span>'
                    + '<span class="swap-side">for '
                    + escapeHtml(fmtSide(t.counter.want))
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
            parts.push(`<div class="roll ${lr.is_you ? 'you-rolled' : ''}">`
                + `${who}</div>`);
        }
        if (snap.knight_hint && snap.knight_hint.have > 0) {
            // Standalone knight-play panel (separate from the active-robber
            // ranking): fires whenever self holds a Knight so Noah knows
            // whether to play it this turn.
            const kh = snap.knight_hint;
            const verdictCls = kh.should_play ? 'play' : 'hold';
            const verdictLbl = kh.should_play ? 'PLAY' : 'HOLD';
            let tail = '';
            if (kh.best_target) {
                const t = kh.best_target;
                const tile = t.resource
                    ? `${t.resource.slice(0, 3)}${t.number ?? ''}`
                    : 'DES';
                const scoreTxt = (t.score > 0 ? '+' : '') + t.score;
                tail = ` · top ${tile} (${scoreTxt})`;
            }
            parts.push('<div class="knight-hint">');
            parts.push(`<div class="kh-h">knight card (×${kh.have})</div>`);
            parts.push('<div class="kh-reason">'
                + `<span class="kh-verdict ${verdictCls}">${verdictLbl}</span>`
                + escapeHtml(kh.reason || '')
                + escapeHtml(tail) + '</div>');
            parts.push('</div>');
        }
        if (snap.monopoly_hint && snap.monopoly_hint.have > 0) {
            const mh = snap.monopoly_hint;
            const resLbl = mh.resource.slice(0, 3).toLowerCase();
            let body = `target <b>${escapeHtml(resLbl)}</b> · ~${mh.est_steal} cards`;
            if (mh.unlock) {
                body += `<span class="dv-unlock">${escapeHtml(mh.unlock)}</span>`;
            }
            parts.push('<div class="dev-hint">');
            parts.push(`<div class="dv-h">monopoly (×${mh.have})</div>`);
            parts.push(`<div class="dv-body">${body}</div>`);
            parts.push('</div>');
        }
        if (snap.yop_hint && snap.yop_hint.have > 0) {
            const yh = snap.yop_hint;
            const pair = (yh.pair || []).map(r => r.slice(0, 3).toLowerCase()).join(' + ');
            let body = `pick <b>${escapeHtml(pair)}</b>`;
            if (yh.unlock) {
                body += `<span class="dv-unlock">unlocks ${escapeHtml(yh.unlock)}</span>`;
            }
            parts.push('<div class="dev-hint">');
            parts.push(`<div class="dv-h">year of plenty (×${yh.have})</div>`);
            parts.push(`<div class="dv-body">${body}</div>`);
            parts.push('</div>');
        }
        if (snap.rb_hint && snap.rb_hint.have > 0) {
            const rh = snap.rb_hint;
            const verdictLbl = rh.should_play ? 'PLAY' : 'HOLD';
            const verdictCls = rh.should_play ? 'play' : 'hold';
            parts.push('<div class="dev-hint">');
            parts.push(`<div class="dv-h">road building (×${rh.have})</div>`);
            parts.push(`<div class="dv-body">`
                + `<span class="kh-verdict ${verdictCls}">${verdictLbl}</span>`
                + escapeHtml(rh.reason || '') + '</div>');
            parts.push('</div>');
        }
        if (snap.threat && snap.threat.message) {
            const lvl = snap.threat.level || 'mid';
            parts.push(`<div class="threat ${lvl}">`
                + escapeHtml(snap.threat.message)
                + '</div>');
        }
        if (snap.robber_on_me) {
            const rom = snap.robber_on_me;
            const res = (rom.resource || '').slice(0, 3).toLowerCase();
            const tileLbl = `${res}${rom.number || ''}`;
            const nBuilds = rom.buildings;
            const sub = nBuilds > 1
                ? `${nBuilds} buildings blocked`
                : (rom.has_city ? 'city blocked' : 'settlement blocked');
            parts.push('<div class="robber-on-me">');
            parts.push(`robber on your ${escapeHtml(tileLbl)} ·`
                + ` ${rom.pips_blocked} pips suppressed`);
            parts.push(`<span class="rom-sub">${escapeHtml(sub)}</span>`);
            parts.push('</div>');
        }
        if (snap.longest_road_race) {
            const lr = snap.longest_road_race;
            const lvl = lr.level || 'contested';
            parts.push(`<div class="lr-race ${lvl}">`
                + escapeHtml(lr.message || '')
                + '</div>');
        }
        if (snap.largest_army_race) {
            const la = snap.largest_army_race;
            const lvl = la.level || 'contested';
            parts.push(`<div class="la-race ${lvl}">`
                + escapeHtml(la.message || '')
                + '</div>');
        }
        if (snap.bank_supply && (snap.bank_supply.low || []).length) {
            const low = snap.bank_supply.low;
            const lbl = low.map(e =>
                `${e.resource.slice(0, 3).toLowerCase()} ${e.count}`
            ).join(' · ');
            parts.push('<div class="bank-low">');
            parts.push(`bank low: ${escapeHtml(lbl)}`);
            parts.push('<span class="bl-sub">4:1 trades blocked at 0</span>');
            parts.push('</div>');
        }
        if (snap.dev_deck) {
            // Only surface when the deck is getting thin — at full
            // stock it's just noise. Flashes amber at <=2 (same
            // threshold the backend flags as `low`).
            const dd = snap.dev_deck;
            if (dd.remaining <= 10) {
                const cls = dd.low ? 'dev-deck low' : 'dev-deck';
                parts.push(`<div class="${cls}">`
                    + `dev deck: ${dd.remaining} left`
                    + (dd.low ? ' — last chance to buy' : '')
                    + '</div>');
            }
        }
        if (snap.discard_hint && snap.discard_hint.need > 0) {
            const dh = snap.discard_hint;
            const dropText = Object.entries(dh.drop)
                .map(([res, n]) => `${n} ${res.slice(0, 2).toLowerCase()}`)
                .join(' · ');
            parts.push('<div class="discard-hint">');
            parts.push(`<div class="dh-h">discard ${dh.need} (over 7)</div>`);
            parts.push(`<div class="dh-drops">${escapeHtml(dropText)}</div>`);
            if (dh.rationale) {
                parts.push(`<div class="dh-reason">${escapeHtml(dh.rationale)}</div>`);
            }
            parts.push('</div>');
        }
        if ((snap.robber_targets || []).length
            && (snap.robber_pending || snap.robber_reason === 'knight')) {
            // Header depends on why targets are showing: a forced 7-roll
            // placement is urgent ("robber targets"); a knight-held hint
            // is advisory ("knight → robber targets").
            const rhTxt = snap.robber_reason === 'knight'
                ? 'knight → robber targets'
                : 'robber targets';
            parts.push(`<div class="robber-h">${rhTxt}</div>`);
            parts.push('<table class="robber">');
            for (let i = 0; i < snap.robber_targets.length; i++) {
                const t = snap.robber_targets[i];
                const tile = t.resource
                    ? `${t.resource.slice(0, 3)}${t.number ?? ''}`
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
        ui.content.innerHTML = parts.join('');
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
                    renderOverlay(ui, snap, true);
                } else if (!lastSnap) {
                    renderOverlay(ui, snap, true);
                }
            }).catch(() => {
                renderOverlay(ui, lastSnap, false);
            });
        };
        tick();
        setInterval(tick, ADVISOR_POLL_MS);
    }

    // WebSocket frame capture. Colonist renders the board on a single
    // <canvas id="game-canvas"> — no per-tile DOM — so the only way to
    // map board state to catanatron coordinates is to read the game
    // protocol directly. We patch the WebSocket constructor before any
    // colonist code runs (hence @run-at document-start), wrap send and
    // message events on every instance, and stash frames to a rolling
    // buffer on unsafeWindow.__cataanbotWS for offline inspection.
    //
    // unsafeWindow is required because Tampermonkey runs userscripts in
    // Chrome's isolated content-script world. Patching `window.WebSocket`
    // from the isolated world changes the isolated window, not the main
    // world colonist actually uses — so colonist's `new WebSocket()` call
    // hits the untouched native constructor. `unsafeWindow` is the main-
    // world window; patches there propagate to the real runtime.
    //
    // Every frame is captured in full as base64 so the protocol can be
    // decoded offline. v0.5.1 truncated large frames to 64 bytes which
    // hid GameStart + tileCornerStates diffs — exactly the topology
    // frames we need. v0.5.2 also skips the ~1Hz ping/pong envelope
    // (channel id "136", ~33 bytes) so long capture sessions don't
    // evict the important frames at the head of the buffer.
    //
    // Expose __cataanbotWSDump() as a one-shot "save capture to disk"
    // helper so we can grab the buffer from the DevTools console without
    // pasting a scrape every time.
    const WS_BUFFER_MAX = 2000;
    (function installWSInterceptor() {
        const tgt = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
        if (tgt.__cataanbotWS) return;
        const buffer = [];
        const summary = { opened: 0, sent: 0, recv: 0,
            pings: 0, errors: 0 };
        tgt.__cataanbotWS = { buffer, summary };

        const NativeWebSocket = tgt.WebSocket;
        if (!NativeWebSocket) return;

        function bytesToBase64(bytes) {
            let bin = '';
            const chunk = 0x8000;
            for (let i = 0; i < bytes.length; i += chunk) {
                bin += String.fromCharCode.apply(
                    null, bytes.subarray(i, i + chunk));
            }
            return btoa(bin);
        }

        function describeData(data) {
            if (typeof data === 'string') {
                return { kind: 'text', length: data.length, data };
            }
            let bytes = null;
            let kind = null;
            if (data instanceof ArrayBuffer) {
                bytes = new Uint8Array(data);
                kind = 'arraybuffer';
            } else if (ArrayBuffer.isView(data)) {
                bytes = new Uint8Array(
                    data.buffer, data.byteOffset, data.byteLength);
                kind = data.constructor?.name || 'typedarray';
            }
            if (bytes) {
                return {
                    kind, byteLength: bytes.length,
                    b64: bytesToBase64(bytes),
                };
            }
            if (typeof Blob !== 'undefined' && data instanceof Blob) {
                return { kind: 'blob', byteLength: data.size, pending: true };
            }
            return { kind: typeof data, preview: String(data).slice(0, 120) };
        }

        // colonist's keepalive envelope is channel id "136" with only a
        // {timestamp: uint64} body. Always ~33 bytes and drowns out the
        // ~5KB GameStart frame if we buffer them. Detect by byte pattern
        // on the raw head: msgpack fixmap-2 (0x82) "id"(0xA2 i d) "136"
        // (0xA3 '1' '3' '6').
        const PING_PATTERN = [0x82, 0xa2, 0x69, 0x64,
            0xa3, 0x31, 0x33, 0x36];
        function isPingBytes(bytes) {
            if (!bytes || bytes.length > 40) return false;
            for (let i = 0; i < PING_PATTERN.length; i++) {
                if (bytes[i] !== PING_PATTERN[i]) return false;
            }
            return true;
        }

        function pushFrame(frame) {
            buffer.push(frame);
            if (buffer.length > WS_BUFFER_MAX) {
                buffer.splice(0, buffer.length - WS_BUFFER_MAX);
            }
        }

        function recordFrame(dir, data, wsId) {
            let bytes = null;
            if (data instanceof ArrayBuffer) {
                bytes = new Uint8Array(data);
            } else if (ArrayBuffer.isView(data)) {
                bytes = new Uint8Array(
                    data.buffer, data.byteOffset, data.byteLength);
            }
            if (bytes && isPingBytes(bytes)) {
                summary.pings += 1;
                return;
            }
            const frame = { dir, ts: Date.now() / 1000,
                wsId, ...describeData(data) };
            pushFrame(frame);
            // Forward inbound-direction frames to the bridge. Outbound
            // (user actions) aren't needed for the game-state pipe and
            // would just double traffic. Bridge is local so this is
            // cheap; keep it quiet if the bridge is down so nobody sees
            // failure spam mid-game.
            if (dir === 'in' && (frame.b64 || frame.data)) {
                postTo(BRIDGE_WS_URL, frame, { quiet: true });
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
                    recordFrame('out', data, wsId);
                } catch (e) { summary.errors += 1; }
                return origSend(data);
            };

            ws.addEventListener('message', (ev) => {
                try {
                    summary.recv += 1;
                    recordFrame('in', ev.data, wsId);
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
        tgt.WebSocket = PatchedWebSocket;

        tgt.__cataanbotWSDump = function dumpWS(label) {
            const payload = {
                schema: 1, capturedAt: Date.now() / 1000,
                url: location.href, summary,
                buffer: buffer.slice(),
            };
            const blob = new Blob(
                [JSON.stringify(payload, null, 2)],
                { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            const ts = new Date().toISOString()
                .replace(/[:.]/g, '-').slice(0, 19);
            a.download = `cataanbot-ws-${label ? label + '-' : ''}${ts}.json`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
            return a.download;
        };

        console.log(LOG_PREFIX, 'WS interceptor v0.6.0 installed on',
            tgt === window ? 'window' : 'unsafeWindow',
            '(forwarding to', BRIDGE_WS_URL + ')');
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
            // Player name pill: colored span. Inline `color:` is the
            // happy path, but some colonist builds use CSS classes or
            // background pills (e.g. WHITE players whose text color
            // isn't readable on a white chat bg). Fall back to computed
            // style so we don't drop the name entirely when the color
            // isn't inline.
            const style = el.getAttribute?.('style') || '';
            const hasInlineColor = /(^|[^-])color\s*:/i.test(style);
            const hasInlineBg = /background(-color)?\s*:/i.test(style);
            if (el.tagName === 'SPAN' && (hasInlineColor || hasInlineBg)) {
                const name = (el.innerText || '').trim();
                if (name) {
                    let color = el.style.color || '';
                    if (!color) {
                        try {
                            const cs = window.getComputedStyle(el);
                            color = cs.color || '';
                        } catch (_) { /* ignore */ }
                    }
                    // Some colonist variants pill names with a tinted
                    // background; expose that too so the bridge can
                    // fall back to it when text color is unusable.
                    let bg = el.style.backgroundColor || '';
                    if (!bg && hasInlineBg) {
                        try {
                            bg = window.getComputedStyle(el)
                                .backgroundColor || '';
                        } catch (_) { /* ignore */ }
                    }
                    parts.push({ kind: 'name', name, color, bg });
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
        postTo(BRIDGE_URL, payload);
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
    startAdvisorPoll();
})();
