// ==UserScript==
// @name         cataanbot — colonist.io log bridge
// @namespace    https://github.com/NoahLaforet/CataanBot
// @version      0.18.1
// @description  Streams colonist.io game-log events + WebSocket frames to the cataanbot FastAPI bridge on localhost:8765. v0.16.0 rebuilds the HUD on a token-based design system: Archivo display font + JetBrains Mono, consistent spacing scale, role-based color palette, banner family with left-edge accent bars. v0.10.1 bumped HUD font 12→14px and width 280→340px; v0.10.0 added the incoming-trade panel.
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
  /* --------------------------------------------------------------
     CataanBot HUD — v0.17 redesign.

     Principles:
     - ONE font family (JetBrains Mono) used across the HUD. Hierarchy
       comes from size/weight/casing, not a second typeface. Fewer
       decisions, more consistency.
     - Five signal colors total (pos / warn / alert / info / accent).
       Every semantic state maps to one of them — no bespoke hues.
     - Section dividers are real: a horizontal rule plus an inline
       uppercase label. The eye lands on section boundaries before
       it reads data.
     - Self block lives in an explicit card with a colored left
       border. Opps live in their own sibling cards. Banners share a
       single archetype (3px left-edge bar + uniform dark bg).
     - 14px body base with a 4px spacing grid. Generous line-height.
     -------------------------------------------------------------- */
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700;800&display=swap');

  :host, * { box-sizing: border-box; }

  .panel {
    /* layout */
    --panel-w: 380px;
    --panel-h: auto;
    --font-scale: 1;

    /* spacing — 4px rhythm */
    --s-1: 2px;
    --s-2: 4px;
    --s-3: 8px;
    --s-4: 12px;
    --s-5: 16px;
    --s-6: 20px;
    --s-7: 24px;

    /* surfaces */
    --bg-0: #0c0d11;
    --bg-1: #14161d;
    --bg-2: #1c1e27;
    --bg-3: #252834;
    --line:        rgba(255, 255, 255, 0.06);
    --line-strong: rgba(255, 255, 255, 0.12);

    /* text ladder */
    --fg:       #eef0f5;
    --fg-mute:  #a9aeba;
    --fg-dim:   #70768a;
    --fg-label: #7a7fa0;

    /* signal palette — only 5 */
    --pos:    #7ed99f;
    --warn:   #ecb06a;
    --alert:  #ff6d61;
    --info:   #7dc0f2;
    --accent: #ffcf5e;

    --radius-sm: 3px;
    --radius:    6px;
    --radius-lg: 10px;

    --font: 'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace;

    font-family: var(--font);
    font-size: calc(14px * var(--font-scale));
    line-height: 1.55;
    color: var(--fg);
    background: var(--bg-0);
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-lg);
    width: var(--panel-w);
    /* Keep the whole HUD clamped to the viewport — header + drawer stay
       pinned at the top, body scrolls when tactical content overflows. */
    max-height: calc(100vh - 24px);
    display: flex;
    flex-direction: column;
    box-shadow:
      0 0 0 1px rgba(255, 255, 255, 0.03) inset,
      0 20px 50px rgba(0, 0, 0, 0.6),
      0 6px 16px rgba(0, 0, 0, 0.35);
    user-select: none;
    position: relative;
  }

  /* Header strip — live-dot + title + collapse button */
  .header {
    display: flex; align-items: center; gap: var(--s-3);
    padding: var(--s-3) var(--s-4);
    cursor: move;
    border-bottom: 1px solid var(--line);
    background: var(--bg-2);
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    flex: 0 0 auto;
  }
  .title {
    flex: 1;
    font-weight: 800;
    font-size: calc(12px * var(--font-scale));
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--fg);
  }
  .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--fg-dim);
    transition: background 0.2s, box-shadow 0.2s;
  }
  .dot.live {
    background: var(--pos);
    box-shadow: 0 0 0 3px rgba(126, 217, 159, 0.18);
  }
  .btn {
    cursor: pointer;
    padding: 0 var(--s-3);
    color: var(--fg-dim);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    background: transparent;
    font-family: var(--font);
    font-size: calc(12px * var(--font-scale));
  }
  .btn:hover { color: var(--fg); border-color: var(--line-strong); }

  /* Body container. Flex child so the panel's max-height clamps it
     and tactical content scrolls rather than bleeding off the viewport
     when the drawer is open. min-height:0 is the magic that lets a
     flex child shrink below its natural size so overflow kicks in. */
  .body {
    padding: var(--s-4) var(--s-4) var(--s-6);
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    /* Thin, restrained scrollbar so the HUD doesn't look like a textarea. */
    scrollbar-width: thin;
    scrollbar-color: var(--bg-3) transparent;
  }
  .body::-webkit-scrollbar { width: 6px; }
  .body::-webkit-scrollbar-track { background: transparent; }
  .body::-webkit-scrollbar-thumb {
    background: var(--bg-3);
    border-radius: 3px;
  }
  .body::-webkit-scrollbar-thumb:hover { background: var(--line-strong); }
  .body.collapsed { display: none; }

  /* --------------------------------------------------------------
     Resize grip — visible, grabbable. Three diagonal ticks in the
     bottom-right corner. Rounded to match panel's corner radius so
     the grip visually lives at the panel edge.
     -------------------------------------------------------------- */
  .resize-handle {
    position: absolute; right: 0; bottom: 0;
    width: 18px; height: 18px;
    cursor: nwse-resize;
    border-bottom-right-radius: var(--radius-lg);
    background:
      linear-gradient(135deg,
        transparent 0%, transparent 30%,
        var(--fg-mute) 30%, var(--fg-mute) 38%,
        transparent 38%, transparent 52%,
        var(--fg-mute) 52%, var(--fg-mute) 60%,
        transparent 60%, transparent 74%,
        var(--fg-mute) 74%, var(--fg-mute) 82%,
        transparent 82%);
    opacity: 0.55;
    transition: opacity 0.15s ease;
  }
  .resize-handle:hover { opacity: 1; }

  /* --------------------------------------------------------------
     Game-progress strip — "ROUND 7 · MID · YOU +1". Sits at the top
     under the header, separated from content by a hairline.
     -------------------------------------------------------------- */
  .gprog {
    font-size: calc(10px * var(--font-scale));
    font-weight: 700;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--fg-label);
    font-variant-numeric: tabular-nums;
    margin: 0 0 var(--s-4);
    padding-bottom: var(--s-3);
    border-bottom: 1px solid var(--line);
  }
  .gprog .ph-early { color: var(--pos); }
  .gprog .ph-mid   { color: var(--accent); }
  .gprog .ph-late  { color: var(--alert); }
  .gprog .stand-self { color: var(--accent); }
  .gprog .stand-gap  { color: var(--fg-dim); }

  /* --------------------------------------------------------------
     Section headings — horizontal hairline + inline uppercase label.
     Primary mechanism for grouping content. Two shapes:
       .sec-h   — generic section (OPPS, ROLL, etc.)
       .recs-h  — recommendations (green accent)
     Both share the same structure so they feel visually consistent.
     -------------------------------------------------------------- */
  .sec-h, .recs-h, .robber-h {
    display: flex; align-items: center;
    gap: var(--s-3);
    margin: var(--s-5) 0 var(--s-3);
    font-size: calc(10px * var(--font-scale));
    font-weight: 700;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--fg-label);
  }
  .sec-h::after, .recs-h::after, .robber-h::after {
    content: "";
    flex: 1;
    height: 1px;
    background: var(--line);
  }
  .recs-h          { color: var(--pos); }
  .recs-h.plan-h   { color: var(--info); }
  .robber-h        { color: var(--alert); }
  .sec-h.sec-opps  { color: var(--fg-label); }
  .sec-h.sec-roll  { color: var(--info); }
  .sec-h.sec-signals { color: var(--warn); }

  /* Deprecated in v0.17 — section headers replace the raw hr */
  .hr { display: none; }

  /* --------------------------------------------------------------
     SELF CARD — player identity + hand + primary action.
     Distinct visual container. The top row is the identity bar
     (pill name + big VP number + terse stats). Below that, the
     hand row gets generous horizontal spacing. Afford line is the
     call-to-action — green when something's buildable.
     -------------------------------------------------------------- */
  .card.self {
    padding: var(--s-4);
    background: var(--bg-1);
    border: 1px solid var(--line);
    border-left: 3px solid var(--fg-dim);
    border-radius: var(--radius);
  }
  .you {
    display: flex; align-items: center;
    gap: var(--s-3);
    flex-wrap: wrap;
    margin-bottom: var(--s-2);
  }
  .color-pill {
    display: inline-block;
    padding: 2px var(--s-3);
    border-radius: var(--radius);
    color: #111;
    font-weight: 800;
    font-size: calc(12px * var(--font-scale));
    letter-spacing: 0.04em;
    vertical-align: middle;
  }
  .you .color-pill {
    font-size: calc(13px * var(--font-scale));
    padding: 2px var(--s-3);
  }
  .you .vp-big {
    font-size: calc(18px * var(--font-scale));
    font-weight: 800;
    color: var(--accent);
    font-variant-numeric: tabular-nums;
    line-height: 1;
    margin-left: auto;
    letter-spacing: 0.02em;
  }
  .you .vp-big .lbl {
    font-size: calc(9px * var(--font-scale));
    color: var(--fg-dim);
    letter-spacing: 0.18em;
    margin-left: var(--s-1);
    font-weight: 700;
    vertical-align: 2px;
    text-transform: uppercase;
  }
  .you .self-meta {
    color: var(--fg-mute);
    font-size: calc(10px * var(--font-scale));
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    opacity: 0.8;
    width: 100%;
    margin-top: var(--s-1);
  }
  .you .self-meta .fat-hand {
    color: var(--alert);
    font-weight: 700;
  }

  /* Hand row — resource counts, bigger and airier */
  .hand {
    display: flex; flex-wrap: wrap;
    gap: var(--s-2) var(--s-4);
    padding: var(--s-2) 0;
    font-size: calc(15px * var(--font-scale));
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    color: var(--fg);
  }
  .hand span {
    display: inline-flex; align-items: center;
    gap: 3px;
  }
  .hand .mono-risk {
    color: var(--warn);
    font-weight: 700;
    text-shadow: 0 0 6px rgba(236, 176, 106, 0.3);
  }
  .mono-warn {
    color: var(--warn);
    font-size: calc(11px * var(--font-scale));
    font-weight: 700;
    margin: var(--s-1) 0;
    letter-spacing: 0.04em;
  }

  .afford {
    color: var(--pos);
    font-weight: 700;
    margin: var(--s-2) 0 var(--s-1);
    font-size: calc(13px * var(--font-scale));
    letter-spacing: 0.02em;
  }
  .afford.none { color: var(--fg-dim); font-weight: 400; }
  .afford.near { color: var(--warn); font-weight: 600; }

  /* Self sub-info row — VP breakdown, ports, production rate. All
     dim metadata that sits below the main self card. */
  .vpb {
    color: var(--fg-dim);
    font-size: calc(11px * var(--font-scale));
    margin-top: var(--s-1);
    letter-spacing: 0.02em;
  }
  .ports {
    color: var(--info);
    font-size: calc(11px * var(--font-scale));
    margin-top: var(--s-1);
    letter-spacing: 0.04em;
  }
  .prod {
    color: var(--pos);
    opacity: 0.9;
    font-size: calc(11px * var(--font-scale));
    margin-top: var(--s-1);
    letter-spacing: 0.04em;
  }

  /* Hand-drift warning — appears when tracker state disagrees */
  .drift {
    color: var(--alert);
    font-size: calc(11px * var(--font-scale));
    margin: var(--s-1) 0;
    font-weight: 600;
  }

  /* --------------------------------------------------------------
     OPP CARDS — list of opponent rows, each in its own mini-card.
     Left border highlights tracked (green) or hot-knight (amber).
     Hand breakdown sits inline on the same row; tags trail.
     -------------------------------------------------------------- */
  .opps {
    display: flex; flex-direction: column;
    gap: var(--s-2);
  }
  .opp {
    padding: var(--s-3);
    background: var(--bg-1);
    border: 1px solid var(--line);
    border-left: 2px solid var(--fg-dim);
    border-radius: var(--radius);
    color: var(--fg-mute);
    font-size: calc(12px * var(--font-scale));
    transition: border-left-color 0.2s ease;
    line-height: 1.45;
  }
  .opp.tracked    { border-left-color: var(--pos); }
  .opp.hot-knight { border-left-color: var(--warn); }
  .opp .color-pill {
    font-size: calc(11px * var(--font-scale));
    padding: 1px var(--s-2);
  }
  .opp .opp-hand {
    color: var(--fg);
    font-variant-numeric: tabular-nums;
    font-size: calc(13px * var(--font-scale));
    display: inline-flex; flex-wrap: wrap;
    gap: var(--s-1) var(--s-3);
    margin-left: var(--s-2);
  }
  .opp.tracked .opp-hand { color: var(--pos); }
  .opp .can-afford {
    color: var(--warn);
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    font-size: calc(10px * var(--font-scale));
  }
  .opp .one-short {
    color: var(--warn);
    opacity: 0.7;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    font-size: calc(11px * var(--font-scale));
  }
  .opp .fat-hand, .you .fat-hand {
    color: var(--alert);
    font-weight: 700;
  }
  .opp .card-up {
    color: var(--alert);
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .opp .card-dn {
    color: var(--fg-dim);
    font-variant-numeric: tabular-nums;
  }
  .opp .dev-stash {
    color: var(--warn);
    font-weight: 700;
  }

  /* --------------------------------------------------------------
     ROLL BANNER — last dice outcome. Prominent when self rolled
     (accent gold); dimmer for opp rolls. Yield trailer in green.
     -------------------------------------------------------------- */
  .roll {
    color: var(--fg);
    font-weight: 500;
    font-size: calc(14px * var(--font-scale));
    margin-bottom: var(--s-2);
    letter-spacing: 0.01em;
  }
  .roll.you-rolled {
    color: var(--accent);
    font-weight: 700;
    font-size: calc(15px * var(--font-scale));
  }
  .roll b { font-weight: 800; }
  .roll .roll-yield {
    color: var(--pos);
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    margin-left: var(--s-2);
  }
  .roll .roll-blocked {
    color: var(--alert);
    opacity: 0.75;
    font-weight: 600;
    margin-left: var(--s-2);
  }
  .opp-yields {
    color: var(--fg-dim);
    font-size: calc(11px * var(--font-scale));
    margin: 0 0 var(--s-2);
    font-variant-numeric: tabular-nums;
  }
  .opp-yields .oy-blk {
    color: var(--alert);
    opacity: 0.7;
  }

  /* Recent rolls strip */
  .roll-history {
    display: flex; flex-wrap: wrap; align-items: baseline;
    gap: var(--s-1);
    font-variant-numeric: tabular-nums;
    font-size: calc(11px * var(--font-scale));
    color: var(--fg-dim);
    margin: var(--s-2) 0;
    letter-spacing: 0.06em;
  }
  .roll-history .rh-label {
    color: var(--fg-label);
    font-size: calc(9px * var(--font-scale));
    font-weight: 700;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    margin-right: var(--s-2);
  }
  .roll-history .rh {
    display: inline-block;
    min-width: 16px;
    padding: 1px var(--s-1);
    border-radius: 2px;
    text-align: center;
    font-weight: 500;
  }
  .roll-history .rh.hit {
    color: var(--pos);
    font-weight: 700;
    background: rgba(126, 217, 159, 0.1);
  }
  .roll-history .rh.blocked {
    color: var(--alert);
    opacity: 0.8;
    text-decoration: underline;
    text-decoration-color: var(--alert);
  }
  .roll-history .rh.seven {
    color: var(--alert);
    font-weight: 800;
  }
  .roll-history .rh-count {
    color: var(--fg-dim);
    opacity: 0.55;
    margin-left: var(--s-2);
  }
  .yield-sum {
    color: var(--fg-mute);
    font-size: calc(11px * var(--font-scale));
    margin: 0 0 var(--s-2);
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.02em;
  }
  .yield-sum.behind { color: var(--warn); }
  .yield-sum .ys-sep { color: var(--fg-dim); opacity: 0.4; margin: 0 var(--s-2); }

  .prod-stall {
    color: var(--warn);
    font-size: calc(11px * var(--font-scale));
    font-weight: 700;
    margin: var(--s-1) 0;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .sevens-hot {
    color: var(--alert);
    font-size: calc(11px * var(--font-scale));
    font-weight: 700;
    margin: var(--s-1) 0;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .hot-numbers {
    color: var(--accent);
    font-size: calc(11px * var(--font-scale));
    font-weight: 700;
    margin: var(--s-1) 0;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  /* --------------------------------------------------------------
     RECOMMENDATIONS — ranked action list. Top rec gets highlighted
     card treatment (green left border + card bg) so it reads as the
     primary CTA. Planning entries dim + italic.
     -------------------------------------------------------------- */
  .rec {
    color: var(--fg);
    padding: var(--s-2) 0;
    line-height: 1.5;
    display: flex; flex-wrap: wrap;
    align-items: baseline;
    gap: var(--s-2);
    font-size: calc(13px * var(--font-scale));
  }
  .rec.top {
    padding: var(--s-3);
    margin: var(--s-1) 0;
    background: var(--bg-1);
    border-radius: var(--radius);
    border-left: 3px solid var(--pos);
  }
  .rec .kind {
    min-width: 60px;
    color: var(--accent);
    font-weight: 700;
    letter-spacing: 0.04em;
    font-size: calc(12px * var(--font-scale));
    text-transform: uppercase;
  }
  .rec .detail {
    color: var(--fg-mute);
    font-size: calc(11px * var(--font-scale));
    font-weight: 400;
    flex: 1 1 100%;
  }
  .rec .score {
    min-width: 50px;
    padding: 1px var(--s-2);
    border-radius: var(--radius-sm);
    font-weight: 800;
    text-align: center;
    font-variant-numeric: tabular-nums;
    font-size: calc(10px * var(--font-scale));
    letter-spacing: 0.06em;
  }
  .rec .score.strong { background: rgba(126, 217, 159, 0.16); color: var(--pos); }
  .rec .score.decent { background: rgba(236, 176, 106, 0.16); color: var(--warn); }
  .rec .score.weak   { background: var(--bg-3); color: var(--fg-dim); }
  .rec .tiles { color: var(--fg-mute); font-size: calc(12px * var(--font-scale)); }
  .tile-chip {
    display: inline-block;
    margin-right: var(--s-2);
    font-variant-numeric: tabular-nums;
  }
  .tile-num { color: var(--accent); font-weight: 700; }
  .tile-num.hot { color: var(--alert); }
  .tile-res { color: var(--fg-mute); font-weight: 500; margin-left: 2px; }

  .rec.plan {
    opacity: 0.78;
    padding: var(--s-1) 0;
  }
  .rec.plan .kind { color: var(--info); }

  .rec-sub {
    color: var(--pos);
    font-size: calc(11px * var(--font-scale));
    padding: 0 0 var(--s-1) 62px;
    opacity: 0.92;
  }
  .rec-sub .warn  { color: var(--warn); font-weight: 600; }
  .rec-sub .arrow { color: var(--fg-dim); margin-right: var(--s-1); }
  .rec-sub.plan-second { color: var(--info); }
  .rec-sub.plan-second .arrow { color: var(--fg-dim); }
  .rec-sub.plan-second .cov {
    color: var(--info);
    opacity: 0.75;
    margin-left: var(--s-2);
    font-variant-numeric: tabular-nums;
  }
  .rec-sub.plan-second .arch {
    background: rgba(125, 192, 242, 0.16);
    color: var(--info);
    border-radius: var(--radius-lg);
    padding: 1px var(--s-2);
    margin-left: var(--s-2);
    font-size: calc(10px * var(--font-scale));
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
  .rec .opt {
    min-width: 22px;
    padding: 1px var(--s-2);
    border-radius: var(--radius-sm);
    background: rgba(255, 207, 94, 0.18);
    color: var(--accent);
    font-weight: 800;
    font-size: calc(11px * var(--font-scale));
    text-align: center;
  }
  .rec.trade .kind { color: var(--warn); }
  .turn-hint {
    color: var(--fg-dim);
    font-style: italic;
    margin: var(--s-2) 0;
    font-size: calc(12px * var(--font-scale));
  }

  /* --------------------------------------------------------------
     BANNER FAMILY — every tactical alert uses the same archetype:
     a card with a 3px left-edge accent bar in its level color. The
     card background is near-uniform (bg-1); the color of the bar +
     the text is what communicates urgency. Headers inside banners
     are uppercase tracked labels.
     -------------------------------------------------------------- */
  .threat, .win-prox, .robber-on-me,
  .lr-race, .la-race, .bank-low,
  .trade-offer, .knight-hint, .dev-hint, .discard-hint {
    position: relative;
    padding: var(--s-3) var(--s-3) var(--s-3) var(--s-4);
    margin: var(--s-2) 0;
    border-radius: var(--radius);
    background: var(--bg-1);
    border: 1px solid var(--line);
    border-left: 3px solid var(--fg-dim);
    font-size: calc(13px * var(--font-scale));
    font-weight: 500;
    letter-spacing: 0.01em;
  }

  .threat.mid   { border-left-color: var(--accent); color: var(--accent); }
  .threat.close { border-left-color: var(--alert); color: var(--alert); }
  .threat.win   {
    border-left-color: var(--alert);
    color: var(--alert);
    font-weight: 700;
    background: linear-gradient(90deg,
      rgba(255, 109, 97, 0.08), var(--bg-1) 50%);
  }

  .win-prox.close   { border-left-color: var(--pos); color: var(--pos); }
  .win-prox.close-1 {
    border-left-color: var(--pos);
    color: var(--pos);
    font-weight: 600;
    background: linear-gradient(90deg,
      rgba(126, 217, 159, 0.08), var(--bg-1) 50%);
  }
  .win-prox.win {
    border-left-color: var(--pos);
    color: var(--pos);
    font-weight: 700;
    background: linear-gradient(90deg,
      rgba(126, 217, 159, 0.16), var(--bg-1) 50%);
  }

  .robber-on-me {
    border-left-color: var(--alert);
    color: var(--alert);
    font-weight: 600;
  }
  .robber-on-me .rom-sub {
    display: block;
    font-weight: 400;
    font-size: calc(11px * var(--font-scale));
    color: var(--fg-mute);
    opacity: 0.85;
    margin-top: var(--s-1);
    letter-spacing: 0;
  }

  .lr-race.self_push, .la-race.self_push {
    border-left-color: var(--pos); color: var(--pos);
  }
  .lr-race.opp_threat, .la-race.opp_threat {
    border-left-color: var(--alert); color: var(--alert);
  }
  .lr-race.contested, .la-race.contested {
    border-left-color: var(--info); color: var(--info);
  }

  .bank-low {
    border-left-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
  }
  .bank-low .bl-sub {
    display: block;
    font-weight: 400;
    font-size: calc(11px * var(--font-scale));
    color: var(--fg-mute);
    opacity: 0.8;
    margin-top: var(--s-1);
    letter-spacing: 0;
  }

  .dev-deck {
    color: var(--fg-dim);
    font-size: calc(10px * var(--font-scale));
    margin: var(--s-2) 0;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    font-weight: 600;
  }
  .dev-deck.low {
    color: var(--accent);
    font-weight: 800;
  }

  .discard-hint { border-left-color: var(--alert); }
  .discard-hint .dh-h {
    color: var(--alert);
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    font-size: calc(10px * var(--font-scale));
    margin-bottom: var(--s-2);
  }
  .discard-hint .dh-drops {
    color: var(--fg);
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }
  .discard-hint .dh-reason {
    color: var(--fg-dim);
    font-size: calc(11px * var(--font-scale));
    font-style: italic;
    margin-top: var(--s-1);
  }

  .trade-offer { border-left-color: var(--warn); }
  .trade-offer .trade-h {
    color: var(--warn);
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    font-size: calc(10px * var(--font-scale));
    margin-bottom: var(--s-2);
    display: flex; align-items: center; gap: var(--s-2);
  }
  .trade-offer .trade-h .muted {
    text-transform: none;
    letter-spacing: 0;
    color: var(--fg-mute);
    font-weight: 500;
  }
  .trade-offer .trade-body {
    color: var(--fg);
    font-variant-numeric: tabular-nums;
    margin: var(--s-1) 0;
  }
  .trade-offer .trade-reason {
    color: var(--fg-mute);
    font-size: calc(11px * var(--font-scale));
    font-style: italic;
    margin-top: var(--s-2);
    display: flex; align-items: center; gap: var(--s-2); flex-wrap: wrap;
  }
  .trade-offer .verdict {
    padding: 1px var(--s-2);
    border-radius: var(--radius-sm);
    font-weight: 800;
    letter-spacing: 0.12em;
    font-size: calc(9px * var(--font-scale));
  }
  .trade-offer .verdict.accept   { background: rgba(126, 217, 159, 0.18); color: var(--pos); }
  .trade-offer .verdict.decline  { background: rgba(255, 109, 97, 0.18);  color: var(--alert); }
  .trade-offer .verdict.consider { background: rgba(255, 255, 255, 0.08); color: var(--fg-mute); }
  .trade-offer .swap-side { color: var(--fg); }
  .trade-offer .swap-arrow { color: var(--fg-dim); margin: 0 var(--s-2); }
  .trade-offer .counter {
    margin-top: var(--s-2);
    padding: var(--s-2) var(--s-3);
    border-radius: var(--radius-sm);
    background: rgba(125, 192, 242, 0.08);
    border-left: 2px solid var(--info);
    color: var(--fg);
    font-size: calc(12px * var(--font-scale));
  }
  .trade-offer .counter .counter-h {
    color: var(--info);
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    font-size: calc(10px * var(--font-scale));
    margin-right: var(--s-2);
  }
  .trade-offer .counter .counter-reason {
    color: var(--fg-dim);
    font-style: italic;
    font-size: calc(11px * var(--font-scale));
    margin-left: var(--s-2);
  }

  .knight-hint { border-left-color: var(--accent); }
  .knight-hint .kh-h {
    color: var(--accent);
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    font-size: calc(10px * var(--font-scale));
    margin-bottom: var(--s-2);
  }
  .knight-hint .kh-reason {
    color: var(--fg);
    font-size: calc(12px * var(--font-scale));
    margin: var(--s-1) 0;
    display: flex; align-items: center; gap: var(--s-2); flex-wrap: wrap;
  }
  .knight-hint .kh-verdict {
    padding: 1px var(--s-2);
    border-radius: var(--radius-sm);
    font-weight: 800;
    letter-spacing: 0.12em;
    font-size: calc(9px * var(--font-scale));
  }
  .knight-hint .kh-verdict.play { background: rgba(126, 217, 159, 0.18); color: var(--pos); }
  .knight-hint .kh-verdict.hold { background: rgba(255, 255, 255, 0.08); color: var(--fg-mute); }

  .dev-hint { border-left-color: var(--info); }
  .dev-hint .dv-h {
    color: var(--info);
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    font-size: calc(10px * var(--font-scale));
    margin-bottom: var(--s-2);
  }
  .dev-hint .dv-body {
    color: var(--fg);
    font-variant-numeric: tabular-nums;
    font-size: calc(12px * var(--font-scale));
  }
  .dev-hint .dv-unlock {
    color: var(--pos);
    font-style: italic;
    margin-left: var(--s-2);
  }

  /* Robber targets table */
  table.robber {
    width: 100%;
    border-collapse: collapse;
    margin-top: var(--s-2);
    font-variant-numeric: tabular-nums;
  }
  table.robber td {
    padding: var(--s-1) var(--s-2) var(--s-1) 0;
    vertical-align: top;
    font-size: calc(11px * var(--font-scale));
  }
  .victim-top { color: var(--accent); font-weight: 800; }

  .muted { color: var(--fg-dim); }
  .err { color: var(--alert); }

  /* --------------------------------------------------------------
     SETTINGS DRAWER — reveals below the header. Holds the New Game
     action, pause toggle, opacity slider, and copy-snapshot. Shows
     keyboard hints at the bottom so shortcuts are discoverable
     without a separate docs page.
     -------------------------------------------------------------- */
  .settings-btn { font-size: calc(13px * var(--font-scale)); padding: 0 var(--s-2); }

  .paused-badge {
    display: none;
    padding: 1px var(--s-2);
    border-radius: var(--radius-sm);
    background: rgba(236, 176, 106, 0.18);
    color: var(--warn);
    font-size: calc(9px * var(--font-scale));
    font-weight: 800;
    letter-spacing: 0.18em;
    text-transform: uppercase;
  }
  .panel[data-paused="1"] .paused-badge { display: inline-block; }

  .drawer {
    display: none;
    flex-direction: column;
    gap: var(--s-2);
    padding: var(--s-2) var(--s-3) var(--s-3);
    border-bottom: 1px solid var(--line);
    background: var(--bg-1);
    flex: 0 0 auto;
  }
  .drawer.open { display: flex; }
  .drawer-row {
    display: flex; align-items: center;
    gap: var(--s-2);
    flex-wrap: wrap;
  }
  .drawer-label {
    color: var(--fg-label);
    font-size: calc(9px * var(--font-scale));
    font-weight: 700;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    min-width: 64px;
  }
  .drawer-btn {
    padding: var(--s-1) var(--s-3);
    border-radius: var(--radius-sm);
    background: var(--bg-2);
    border: 1px solid var(--line-strong);
    color: var(--fg);
    font-family: var(--font);
    font-size: calc(11px * var(--font-scale));
    font-weight: 600;
    cursor: pointer;
    letter-spacing: 0.04em;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
  }
  .drawer-btn:hover {
    background: var(--bg-3);
    border-color: var(--fg-mute);
  }
  .drawer-btn.danger {
    color: var(--alert);
    border-color: rgba(255, 109, 97, 0.35);
  }
  .drawer-btn.danger:hover {
    background: rgba(255, 109, 97, 0.12);
    border-color: var(--alert);
  }
  .drawer-btn.armed {
    background: var(--alert);
    color: #111;
    border-color: var(--alert);
    font-weight: 800;
    animation: cataanbot-armed-pulse 0.7s ease-in-out infinite alternate;
  }
  @keyframes cataanbot-armed-pulse {
    from { box-shadow: 0 0 0 0 rgba(255, 109, 97, 0.55); }
    to   { box-shadow: 0 0 0 5px rgba(255, 109, 97, 0); }
  }
  .drawer-btn.flash-ok {
    background: rgba(126, 217, 159, 0.18);
    color: var(--pos);
    border-color: var(--pos);
  }

  .toggle {
    display: inline-flex; align-items: center;
    gap: var(--s-2);
    cursor: pointer;
    color: var(--fg-mute);
    font-size: calc(11px * var(--font-scale));
    letter-spacing: 0.02em;
  }
  .toggle input[type="checkbox"] {
    appearance: none;
    -webkit-appearance: none;
    width: 28px; height: 16px;
    border-radius: 10px;
    background: var(--bg-3);
    border: 1px solid var(--line-strong);
    position: relative;
    cursor: pointer;
    margin: 0;
    transition: background 0.15s, border-color 0.15s;
  }
  .toggle input[type="checkbox"]::after {
    content: "";
    position: absolute;
    top: 1px; left: 1px;
    width: 12px; height: 12px;
    border-radius: 50%;
    background: var(--fg-mute);
    transition: left 0.15s, background 0.15s;
  }
  .toggle input[type="checkbox"]:checked {
    background: rgba(236, 176, 106, 0.3);
    border-color: var(--warn);
  }
  .toggle input[type="checkbox"]:checked::after {
    left: 13px;
    background: var(--warn);
  }

  .drawer input[type="range"] {
    flex: 1;
    -webkit-appearance: none;
    appearance: none;
    height: 4px;
    background: var(--bg-3);
    border-radius: 2px;
    outline: none;
    cursor: pointer;
  }
  .drawer input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 14px; height: 14px;
    border-radius: 50%;
    background: var(--accent);
    cursor: pointer;
    border: 2px solid var(--bg-0);
  }
  .drawer input[type="range"]::-moz-range-thumb {
    width: 14px; height: 14px;
    border-radius: 50%;
    background: var(--accent);
    cursor: pointer;
    border: 2px solid var(--bg-0);
  }
  .opacity-val {
    color: var(--fg-mute);
    font-size: calc(11px * var(--font-scale));
    font-variant-numeric: tabular-nums;
    min-width: 38px;
    text-align: right;
  }

  .drawer-help { margin-top: var(--s-1); }
  .drawer-hint {
    color: var(--fg-dim);
    font-size: calc(10px * var(--font-scale));
    letter-spacing: 0.02em;
    font-style: italic;
  }

  /* Paused render filter: keep the game-progress strip and the self
     card, hide every tactical section (opps, recs, banners, rolls). */
  .panel[data-paused="1"] #content > div:not(.gprog):not(.card) { display: none; }
</style>
<div class="panel" id="panel">
  <div class="header" id="header">
    <span class="dot" id="dot"></span>
    <span class="title">CataanBot</span>
    <span class="paused-badge" id="paused-badge">paused</span>
    <button class="btn settings-btn" id="settings" title="settings (alt+s)">⚙</button>
    <button class="btn" id="toggle" title="collapse/expand (alt+c)">_</button>
  </div>
  <div class="drawer" id="drawer">
    <div class="drawer-row">
      <span class="drawer-label">actions</span>
      <button class="drawer-btn danger" id="new-game">new game</button>
      <button class="drawer-btn" id="copy-snap">copy snapshot</button>
    </div>
    <div class="drawer-row">
      <span class="drawer-label">advisor</span>
      <label class="toggle"><input type="checkbox" id="pause"/><span>pause banners &amp; recs</span></label>
    </div>
    <div class="drawer-row">
      <span class="drawer-label">opacity</span>
      <input type="range" id="opacity" min="40" max="100" step="5" value="100"/>
      <span class="opacity-val" id="opacity-val">100%</span>
    </div>
    <div class="drawer-row drawer-help">
      <span class="drawer-label">keys</span>
      <span class="drawer-hint">alt+p pause &middot; alt+c collapse &middot; alt+n new game &middot; alt+s settings</span>
    </div>
  </div>
  <div class="body" id="body">
    <div id="content"><span class="muted">waiting for bridge&hellip;</span></div>
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

        // --------------------------------------------------------------
        // Settings drawer + actions. Holds the New Game reset, pause
        // toggle, opacity slider, and snapshot export. Everything here
        // is optional — the overlay works without any of it — but these
        // are the knobs Noah needs during a session without hunting for
        // a terminal (reset the bridge, mute banners mid-chat, make the
        // HUD translucent, grab a snapshot for bug reports).
        // --------------------------------------------------------------
        const settingsBtn = root.getElementById('settings');
        const drawer = root.getElementById('drawer');
        settingsBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            drawer.classList.toggle('open');
        });

        // New Game = two-click confirm. First click arms the button for
        // 3s (red flash + "click again to confirm"); second click posts
        // /reset. Auto-disarms so an accidental press doesn't linger.
        const newGameBtn = root.getElementById('new-game');
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
        const pauseInput = root.getElementById('pause');
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
        const opacityInput = root.getElementById('opacity');
        const opacityVal = root.getElementById('opacity-val');
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

        // Copy Snapshot — fetches the current /advisor JSON and writes
        // it to the clipboard. Exists so Noah can paste exact tracker
        // state into a bug report without screenshotting.
        const copySnapBtn = root.getElementById('copy-snap');
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
                drawer.classList.toggle('open');
            } else if (e.code === 'KeyN') {
                e.preventDefault();
                if (!drawer.classList.contains('open')) {
                    drawer.classList.add('open');
                }
                if (newGameBtn.classList.contains('armed')) fireNewGame();
                else armNewGame();
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
        const PANEL_W_MIN = 260, PANEL_W_MAX = 720;
        const BASE_W = 380;
        function scaleForWidth(w) {
            // 380→1.0, 680→1.5 — linear, clamped to [0.85, 1.6].
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
                    standingsTag = ` · <span class="stand-self">you leading `
                        + `at ${st.self_vp}</span>`;
                } else {
                    const leadName = escapeHtml(st.leader.username || '?');
                    standingsTag = ` · ${leadName} leading at ${st.leader.vp}`
                        + ` <span class="stand-gap">(you ${st.self_vp}, -${st.gap_to_leader})</span>`;
                }
            }
            parts.push('<div class="gprog">'
                + `round ${gp.round} · `
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
            // Meta trailer: cards · pieces · knights. Uppercase tracked
            // label pinned below the identity row so the main row stays
            // clean — pill on the left, big VP on the right.
            const metaSegs = [];
            const meFatHand = (me.cards || 0) >= 8;
            metaSegs.push(meFatHand
                ? `<span class="fat-hand">${me.cards}c</span>`
                : `${me.cards}c`);
            if (me.pieces) {
                const p = me.pieces;
                metaSegs.push(`${p.settle}s/${p.city}c/${p.road}r`);
            }
            if ((me.knights_played || 0) > 0) {
                metaSegs.push(`${me.knights_played}k`);
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
                    return `<span${cls}>${n} ${iconFor(r)}</span>`;
                })
                .join('  ') || '<span class="muted">∅</span>';
            parts.push(`<div class="hand">${hand}</div>`);
            if (me.monopoly_risk) {
                const mr = me.monopoly_risk;
                parts.push('<div class="mono-warn">'
                    + `⚠ ${mr.count} ${iconFor(mr.resource)} at monopoly risk`
                    + '</div>');
            }
            // Hand-drift warning. Tracker's event-reconstructed breakdown
            // disagreed with colonist's authoritative card count — the
            // per-resource detail is unreliable until the next HandSync
            // frame corrects us. Typically caused by a ws disconnect.
            if (me.hand_drift) {
                parts.push('<div class="drift">⚠ hand detail stale '
                    + '(waiting for resync)</div>');
            }
            const afford = (me.afford || []).join(' · ');
            if (afford) {
                parts.push(`<div class="afford">→ ${afford}</div>`);
            } else if (me.next_build) {
                // Nearest-miss gap as a direction-of-travel hint:
                // "1 brick from settlement" is more useful than
                // "nothing buildable" because it says what to aim for.
                const nb = me.next_build;
                const missingStr = Object.entries(nb.missing || {})
                    .map(([r, n]) => `${n} ${r.toLowerCase().slice(0,3)}`)
                    .join(' + ');
                parts.push(`<div class="afford near">→ ${escapeHtml(missingStr)}`
                    + ` from ${escapeHtml(nb.build)}</div>`);
            } else {
                parts.push('<div class="afford none">→ nothing buildable</div>');
            }
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
            parts.push('</div>');  // .card.self
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
                // Leads with a compass arrow + direction word
                // ("↗ upper-right") so Noah can read placement at a
                // glance instead of parsing tile chips every time.
                if (r.kind === 'opening_settlement' && r.road
                        && r.road.toward_tiles) {
                    const towardHtml = tilesToHtml(r.road.toward_tiles);
                    if (towardHtml) {
                        const warn = r.road.contested
                            ? ' <span class="warn">⚠ contested</span>'
                            : '';
                        const dir = r.road.direction;
                        const dirHtml = dir
                            ? `<span class="arrow">↳ ${escapeHtml(
                                dir.arrow)} ${escapeHtml(
                                dir.word)}</span> `
                              + '<span class="muted">toward</span> '
                            : '<span class="arrow">↳ road →</span> ';
                        parts.push('<div class="rec-sub">'
                            + dirHtml
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
                // Dev-card tag: uniform grey at low VP, amber/bold
                // when the dev-stash could plausibly be hiding VPs
                // that push them to the win threshold.
                const devTag = (o.dev_cards || 0) > 0
                    ? (o.dev_stash_risk
                        ? ` · <span class="dev-stash">${o.dev_cards}dev🔒</span>`
                        : ` · ${o.dev_cards}dev`)
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
                // Opp ports. Trade-partner signal: 2:1 on a resource
                // means they'd rather bank-trade than swap with you.
                // Silent when no ports. Format: "port:whe,shp,3" where
                // 3 stands in for the generic 3:1.
                let opPortTag = '';
                if (Array.isArray(o.ports) && o.ports.length) {
                    const segs = o.ports.map(p => p === 'GENERIC'
                        ? '3'
                        : p.slice(0, 3).toLowerCase());
                    opPortTag = ` · port:${segs.join(',')}`;
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
                        + `${os.need.toLowerCase().slice(0,3)} → `
                        + `${os.build}${tail}</span>`;
                }
                // Fat-hand marker: opps carrying 8+ cards are primed
                // for a 7-roll — they discard half AND are likely steal
                // targets. Color the cards count so Noah eyeballs it
                // without doing the addition.
                const fatHand = (o.cards || 0) >= 8;
                let cardsSpan = fatHand
                    ? `<span class="fat-hand">${o.cards}c</span>`
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
                parts.push(`<div class="opp${trackCls}${rowCls}">${pill}`
                    + ` <span class="muted">${cardsSpan} · ${o.vp}VP${devTag}${piecesTag}${kpTag}${prodTag}${opPortTag}</span>${affordTag}${oneShortTag}`
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
            parts.push('<div class="sec-h sec-roll">roll</div>');
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
                    ? gPairs.map(([r, n]) => `+${n} ${iconFor(r)}`).join(' ')
                    : '';
                const blocked = bPairs.length
                    ? ' <span class="roll-blocked">blocked: '
                        + bPairs.map(([r, n]) => `${n} ${iconFor(r)}`).join(' ')
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
        // Recent rolls strip: shows the last ~10 dice totals so Noah
        // can see at a glance which numbers have been dry (pip drought)
        // vs streaky. Most recent on the right; self-hits in green,
        // robber-blocked in amber-underline, 7s in red.
        const hist = snap.roll_history;
        if (hist && hist.length > 0) {
            const cells = hist.map((e) => {
                const t = e.total;
                let cls = 'rh';
                if (t === 7) cls += ' seven';
                else if (e.blocked_you) cls += ' blocked';
                else if (e.hit_you) cls += ' hit';
                return `<span class="${cls}">${t}</span>`;
            }).join('');
            // Absolute game-roll counter so Noah can tell "turn ~5" from
            // "turn ~20" — roll_history only keeps the last 10, but
            // total_rolls is monotonic. Shown in parens after the label.
            const totalRolls = snap.total_rolls || 0;
            const tail = totalRolls > 0 ? ` <span class="rh-count">(${totalRolls})</span>` : '';
            parts.push('<div class="roll-history">'
                + '<span class="rh-label">recent:</span>'
                + cells + tail + '</div>');
        }
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
        // Production stall: surface a discrete banner when self has
        // gone 3+ non-7 rolls without a gain AND has a real engine
        // (per_roll > 0). yield_summary already shows the aggregate;
        // this is the "right now" cue that pushes toward a trade or
        // dev-card buy instead of just waiting for the next roll.
        const ps = snap.production_stall;
        if (ps && ps.rolls_dry >= 3) {
            parts.push('<div class="prod-stall">'
                + `dry ${ps.rolls_dry} rolls · ${ps.per_roll}/roll expected`
                + '</div>');
        }
        // Sevens-hot: 7s are coming up at 2×+ expected rate in the
        // recent window. Practical effect: lean toward holding fewer
        // cards / spending before crossing the discard threshold.
        const sh = snap.sevens_hot;
        if (sh) {
            parts.push('<div class="sevens-hot">'
                + `7s hot: ${sh.sevens}/${sh.window} recent — consider discarding`
                + '</div>');
        }
        // Hot numbers: productive dice (non-7) over-rolling at 2×+
        // expected. Brief line showing top-2 most-anomalous so Noah
        // can map them to his tiles. Stays active as long as the ratio
        // holds — cools naturally as the window fills with other rolls.
        const hn = snap.hot_numbers;
        if (hn && hn.length > 0) {
            const pieces = hn.map(h =>
                `${h.number} (${h.count}× vs ${h.expected}×)`).join(', ');
            parts.push('<div class="hot-numbers">hot: ' + pieces + '</div>');
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
        // Self close-to-win banner — symmetric with snap.threat but
        // fires on self VP hitting the close threshold.
        if (snap.win_proximity && snap.win_proximity.message) {
            const wlvl = snap.win_proximity.level || 'close';
            parts.push(`<div class="win-prox ${wlvl}">`
                + escapeHtml(snap.win_proximity.message)
                + '</div>');
        }
        if (snap.robber_on_me) {
            const rom = snap.robber_on_me;
            const res = (rom.resource || '').slice(0, 3).toLowerCase();
            const tileLbl = `${res}${rom.number || ''}`;
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
                    `${rom.blocks_recent}/${rom.rolls_recent} recent rolls lost`);
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
                headExtra = ` · ${rom.pips_blocked} pips suppressed`;
            }
            parts.push(`robber on your ${escapeHtml(tileLbl)}${headExtra}`);
            parts.push(`<span class="rom-sub">${escapeHtml(subParts.join(' · '))}</span>`);
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
