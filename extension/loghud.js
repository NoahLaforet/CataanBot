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

    let root = null;   // HUD body element
    let tabs = null;   // tab-bar element
    let _noContainer = 0;   // consecutive failed anchor finds
    let _failsafe = false;  // floating-overlay fallback already tripped
    let _everConnected = false;  // a successful /advisor fetch has happened
    let _lastSnap = null;   // most recent /advisor snapshot (drives adaptive poll)
    let _lastSig = null;    // signature of the last rendered snapshot (skip re-render)
    let _cuedEls = [];      // colonist elements we've glowed (clear without a sweep)
    let _container = null;  // cached log container handle (fast re-anchor path)
    let _tabState = '';     // last applyTab state string (skip redundant DOM work)
    const _cfgInputs = {};  // gear-menu number inputs, keyed by /config field
    let _settingsPanel = null;  // the gear dropdown (opened by the toolbar icon)
    let _recIdx = 0;        // which recommendation is shown (click-to-cycle)
    let _recSig = '';       // rec-set signature; resets _recIdx on a new turn
    let _tradeAnchored = false;  // trade badge is pinned to colonist's panel
    let _currentTradeKey = null;   // identity of the live incoming trade
    let _tradeDismissedKey = null;  // trade the user already acted on (suppress)
    let _tradeDismissWired = false;  // the dismiss click listener is installed

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
    // Opponent reads use CONFIRMED cards first (icon + count), then a thin
    // separator and the LIKELY-but-unsure resources as dim icons. Rendered onto
    // colonist's own player rows by rowReadHtml; this cutoff is shared.
    const LIKELY_CUTOFF = 0.45;   // P(at least one) above this = "likely"

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
#${TABS_ID} .cbo-set-h {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em;
    color: #8a7656; font-weight: 700; margin: 6px 2px 2px;
    border-bottom: 1px solid rgba(90, 62, 28, 0.15); padding-bottom: 2px;
}
#${TABS_ID} .cbo-set-h:first-child { margin-top: 0; }
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
#${TABS_ID} .cbo-set-row.cbo-num { justify-content: space-between; cursor: default; }
#${TABS_ID} .cbo-set-row.cbo-num input {
    width: 46px; text-align: center; font: inherit; font-size: 12px; cursor: text;
    border: 1px solid rgba(90, 62, 28, 0.30); border-radius: 4px; padding: 1px 3px;
    background: #fff; color: #2f2415;
}
#${TABS_ID} .cbo-tab {
    appearance: none;
    border: 1px solid rgba(90, 62, 28, 0.18);
    border-bottom: none;
    background: rgba(90, 62, 28, 0.08);
    color: #6b5836;
    font: inherit;
    font-weight: 700;
    font-size: 13px;
    letter-spacing: 0.02em;
    padding: 5px 14px;
    border-radius: 7px 7px 0 0;
    cursor: pointer;
    line-height: 1.2;
}
#${TABS_ID} .cbo-tab:hover { background: rgba(90, 62, 28, 0.15); }
#${TABS_ID} .cbo-tab.active {
    background: rgba(184, 134, 47, 0.22);
    color: #2f2415;
    box-shadow: inset 0 -3px 0 0 #b8862f;
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
#${ROOT_ID} .cbo-rec-cyc {
    cursor: pointer; border-radius: 6px; margin: 0 -4px; padding: 2px 4px 4px;
    transition: background 0.12s;
}
#${ROOT_ID} .cbo-rec-cyc:hover { background: rgba(184, 134, 47, 0.14); }
#${ROOT_ID} .cbo-rec-cyc-hint {
    display: inline-block; margin-left: 7px; font-size: 10px; font-weight: 600;
    color: #9c7b3a; vertical-align: middle; opacity: 0.85;
}
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
#${ROOT_ID} .cbo-foot.cbo-strong { font-weight: 800; letter-spacing: 0.01em; }
/* Strategy banner: archetype headline + a dim rationale line. */
#${ROOT_ID} .cbo-strat {
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
    font-size: 13px; font-weight: 700; margin-top: 2px;
}
#${ROOT_ID} .cbo-strat-phase {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em;
    opacity: 0.6; font-weight: 600;
}
#${ROOT_ID} .cbo-strat-why {
    flex-basis: 100%; font-size: 11px; font-weight: 500;
    opacity: 0.75; margin-top: 1px;
}
/* Economy line: bank-low / dev-deck, a label with a value pill. */
#${ROOT_ID} .cbo-eco {
    display: flex; justify-content: space-between; align-items: center;
    font-size: 12px; padding: 2px 0; gap: 8px;
}
#${ROOT_ID} .cbo-eco-v { font-weight: 700; }
#${ROOT_ID} .cbo-eco.cbo-eco-low .cbo-eco-v { color: #9c2d22; }
/* Dice tempo: one quiet line of anomalies. */
#${ROOT_ID} .cbo-dice {
    font-size: 12px; opacity: 0.85; padding: 2px 0;
}
/* Game plan: near-term VP goal. Kind pill + tiles + a dim summary line. */
#${ROOT_ID} .cbo-plan {
    font-size: 13px; font-weight: 600; color: #2f2415; padding: 2px 0 1px 0;
}
#${ROOT_ID} .cbo-plan-sum { color: #6b5836; font-weight: 500; font-size: 12px; }
#${ROOT_ID} .cbo-plan.cbo-ready .cbo-rec-kind { background: #46a45a; }
/* Strategic options: long-game VP-swing plays, one row each. */
#${ROOT_ID} .cbo-opt {
    display: flex; align-items: baseline; gap: 6px; padding: 1px 0; font-size: 12px;
}
#${ROOT_ID} .cbo-opt-vp {
    flex: 0 0 auto; font-weight: 800; font-size: 11px; color: #2f6b3f;
    background: rgba(70, 164, 90, 0.15); border-radius: 4px; padding: 0 5px;
}
#${ROOT_ID} .cbo-opt-label { font-weight: 700; color: #2f2415; }
#${ROOT_ID} .cbo-opt-detail { color: #6b5836; }
/* Dev-card cluster: a verdict pill + the play, one row per held dev card. */
#${ROOT_ID} .cbo-dev {
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
    font-size: 12px; padding: 2px 0;
}
#${ROOT_ID} .cbo-verdict {
    flex: 0 0 auto; font-size: 10px; font-weight: 800; letter-spacing: 0.04em;
    padding: 1px 6px; border-radius: 4px; color: #fff;
}
#${ROOT_ID} .cbo-verdict.cbo-v-play { background: #46a45a; }
#${ROOT_ID} .cbo-verdict.cbo-v-hold { background: #9a8a6a; }
#${ROOT_ID} .cbo-verdict.cbo-v-place { background: #3b7dd8; }
#${ROOT_ID} .cbo-dev-what { color: #2f2415; font-weight: 600; }
#${ROOT_ID} .cbo-dev-why { color: #6b5836; font-weight: 500; flex-basis: 100%; }
#${ROOT_ID} .cbo-dev-name {
    color: #9c7b3a; font-weight: 700; text-transform: lowercase; font-size: 11px;
}
/* Round / phase + standings status strip (quiet, sits at the foot). */
#${ROOT_ID} .cbo-prog {
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
    font-size: 11px; color: #8a7656; margin-top: 6px;
    padding-top: 4px; border-top: 1px solid rgba(90, 62, 28, 0.15);
}
#${ROOT_ID} .cbo-prog-phase { text-transform: uppercase; letter-spacing: 0.04em; }
#${ROOT_ID} .cbo-prog-lead { color: #6b5836; font-weight: 600; }
#${ROOT_ID} .cbo-prog-lead.cbo-self-lead { color: #2f6b3f; }
#${ROOT_ID}.cbo-u-red { box-shadow: inset 3px 0 0 0 #c0392b; }
#${ROOT_ID}.cbo-u-amber { box-shadow: inset 3px 0 0 0 #d8862f; }
#${ROOT_ID}.cbo-u-green { box-shadow: inset 3px 0 0 0 #46a45a; }
#${ROOT_ID}.cbo-u-turn { box-shadow: inset 3px 0 0 0 #b8862f; }
/* Opponent resource read injected onto colonist's OWN player rows (unscoped:
   lives in colonist's DOM, not under #${ROOT_ID}). Matches colonist's white
   Open Sans so it reads as part of the tracker row, pinned along the bottom. */
.cbo-prow {
    position: absolute; left: 84px; bottom: 3px;
    display: inline-flex; gap: 5px; align-items: center;
    font: 800 13px/1.1 "Open Sans", -apple-system, system-ui, sans-serif;
    color: #2b2620; letter-spacing: 0.01em;
    background: rgba(245, 240, 228, 0.88);
    border: 1px solid rgba(120, 95, 55, 0.28);
    border-radius: 7px; padding: 2px 7px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.18);
    pointer-events: none; white-space: nowrap; z-index: 6; max-width: 210px;
}
.cbo-prow .cbo-pr-chip { display: inline-flex; align-items: center; gap: 1px; }
.cbo-prow .cbo-pr-maybe { opacity: 0.5; }
.cbo-prow .cbo-pr-sep { opacity: 0.35; margin: 0 1px; font-weight: 400; }
.cbo-prow .cbo-pr-none { opacity: 0.55; font-weight: 600; font-style: italic; }
/* Trade verdict badge pinned to colonist's own trade panel. Colonist-native:
   tan/cream like their panels, dark text, game font, with a thin colored
   left-edge as the only verdict accent (no loud red/green box). */
#cbo-trade-badge {
    position: fixed;
    z-index: 2147483600;
    padding: 5px 11px 5px 9px;
    border-radius: 8px;
    color: #2b2620;
    background: #ece2cc;
    border: 1px solid rgba(120, 95, 55, 0.30);
    border-left-width: 4px;
    font: 700 13px/1.3 "Open Sans", -apple-system, system-ui, sans-serif;
    box-shadow: 0 3px 12px rgba(0, 0, 0, 0.22);
    pointer-events: none;
    max-width: 280px;
}
#cbo-trade-badge .cbo-tb-tag {
    font-size: 9px; letter-spacing: 0.06em; text-transform: uppercase;
    color: #8a7350; margin-right: 5px; font-weight: 700;
}
#cbo-trade-badge .cbo-tb-verdict { font-weight: 800; }
#cbo-trade-badge.cbo-tb-accept { border-left-color: #2e8b57; }
#cbo-trade-badge.cbo-tb-accept .cbo-tb-verdict { color: #1f6b40; }
#cbo-trade-badge.cbo-tb-decline { border-left-color: #c0392b; }
#cbo-trade-badge.cbo-tb-decline .cbo-tb-verdict { color: #a32b1e; }
#cbo-trade-badge.cbo-tb-consider { border-left-color: #3b7dd8; }
#cbo-trade-badge.cbo-tb-consider .cbo-tb-verdict { color: #2a5fa8; }
/* Board overlay: a green ring drawn over the recommended robber tile (the
   board is a WebGL canvas, so this is a positioned layer, not a DOM glow).
   Tile pixel positions are computed geometrically from the canvas rect. */
#cbo-board-overlay {
    position: fixed; inset: 0; pointer-events: none; z-index: 2147483500;
}
/* A pointy-top hexagon traced around the recommended robber tile, glowing
   green like the in-hand card cue (Noah's ask). The top pick pulses bright;
   the 2nd/3rd picks are static + dim. */
#cbo-board-overlay .cbo-bo-mark { position: absolute; overflow: visible; }
#cbo-board-overlay .cbo-bo-hex {
    fill: rgba(70, 196, 99, 0.12);
    stroke: #46c463; stroke-width: 5; stroke-linejoin: round;
    filter: drop-shadow(0 0 5px #46c463) drop-shadow(0 0 10px rgba(70, 196, 99, 0.7));
    animation: cbo-bopulse 1.05s ease-in-out infinite alternate;
}
@keyframes cbo-bopulse {
    from { opacity: 0.6; stroke-width: 4; }
    to { opacity: 1; stroke-width: 6; }
}
#cbo-board-overlay .cbo-bo-rank2 .cbo-bo-hex {
    stroke: #e0a93f; stroke-width: 3; opacity: 0.7; animation: none;
    fill: rgba(224, 169, 63, 0.08);
    filter: drop-shadow(0 0 4px rgba(224, 169, 63, 0.6));
}
#cbo-board-overlay .cbo-bo-rank3 .cbo-bo-hex {
    stroke: #c79a5c; stroke-width: 2; opacity: 0.5; animation: none;
    fill: none; filter: none;
}
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
        return { row, input: cb };
    }
    function _sectionHeader(label) {
        const h = document.createElement('div');
        h.className = 'cbo-set-h';
        h.textContent = label;
        return h;
    }
    // A numeric setting written to the bridge's /config via the worker. Bounds
    // are clamped client-side; the bridge re-validates. The input is registered
    // so it can be refreshed from the live snapshot when the menu opens.
    function _numberRow(label, key, min, max) {
        const row = document.createElement('div');
        row.className = 'cbo-set-row cbo-num';
        const span = document.createElement('span');
        span.textContent = label;
        const inp = document.createElement('input');
        inp.type = 'number';
        inp.min = String(min);
        inp.max = String(max);
        const commit = () => {
            let v = parseInt(inp.value, 10);
            if (!Number.isFinite(v)) return;
            v = Math.max(min, Math.min(max, v));
            inp.value = String(v);
            try {
                chrome.runtime.sendMessage(
                    { type: 'set-config', payload: { [key]: v } });
            } catch (e) { /* worker asleep / extension reloading */ }
        };
        inp.addEventListener('change', commit);
        inp.addEventListener('keydown', (e) => {
            e.stopPropagation();   // don't let colonist hotkeys eat the digits
            if (e.key === 'Enter') { inp.blur(); }
        });
        row.appendChild(span);
        row.appendChild(inp);
        _cfgInputs[key] = inp;
        return row;
    }
    // Pull the current VP target / discard limit out of the live snapshot so
    // the inputs show the truth (not a stale default) when the menu opens.
    function refreshSettingsInputs() {
        const s = _lastSnap;
        if (!s) return;
        if (_cfgInputs.vp_target && document.activeElement !== _cfgInputs.vp_target
                && s.vp_target != null) {
            _cfgInputs.vp_target.value = String(s.vp_target);
        }
        if (_cfgInputs.discard_limit
                && document.activeElement !== _cfgInputs.discard_limit
                && s.discard_limit != null) {
            _cfgInputs.discard_limit.value = String(s.discard_limit);
        }
    }
    function buildSettingsPanel() {
        const p = document.createElement('div');
        p.className = 'cbo-settings';
        p.style.display = 'none';
        p.appendChild(_sectionHeader('Display'));
        p.appendChild(_toggleRow('Streamer mode', _streamerOn, _setStreamer).row);
        p.appendChild(_toggleRow('Pause recs', _paused, (v) => {
            try { localStorage.setItem('catanbot.paused', v ? '1' : '0'); }
            catch (e) { /* private mode */ }
        }).row);
        p.appendChild(_sectionHeader('Advisor'));
        p.appendChild(_numberRow('VP target', 'vp_target', 3, 30));
        p.appendChild(_numberRow('Discard at', 'discard_limit', 5, 20));
        stampStreamer(p);
        return p;
    }
    // Open the gear menu from the toolbar-icon click (background.js sends
    // 'open-settings'). Make sure the HUD is on the CatanBot tab so the menu is
    // actually visible, then drop the panel with live values filled in.
    function openSettings() {
        if (!_settingsPanel) return;
        if (currentTab() !== 'catanbot' && !replaceMode()) setTab('catanbot');
        refreshSettingsInputs();
        _settingsPanel.style.display = 'block';
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
        _settingsPanel = panel;
        gear.addEventListener('click', (e) => {
            e.stopPropagation();
            const opening = panel.style.display !== 'block';
            if (opening) refreshSettingsInputs();   // show live VP/discard values
            panel.style.display = opening ? 'block' : 'none';
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
        // Click-to-cycle the recommendation. Delegated on root (which persists
        // across innerHTML rebuilds), so clicking the rec advances to the
        // next-best and re-renders immediately.
        root.addEventListener('click', (e) => {
            if (!e.target.closest || !e.target.closest('.cbo-rec-cyc')) return;
            const recs = (_lastSnap && _lastSnap.recommendations) || [];
            if (recs.length <= 1) return;
            _recIdx = (_recIdx + 1) % recs.length;
            try {
                root.innerHTML = renderBody(_lastSnap);
                root.className = urgencyOf(_lastSnap);
                stampStreamer(root);
            } catch (err) { /* re-render hiccup */ }
        });

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
        const tab = currentTab();
        // Hide the native log only when the CatanBot tab is up AND we've
        // connected — a user without the bridge keeps their log (the HUD
        // shows additively until it has something real to show).
        const hideNative = (tab === 'catanbot' && _everConnected);
        const kids = nativeChildren(cont);
        // Skip all DOM writes when nothing that affects them changed. kids.length
        // is in the key so a freshly added native child still gets hidden. This
        // is what makes the hot re-anchor path nearly free.
        const state = `${tab}|${hideNative}|${kids.length}`;
        if (cont === _container && state === _tabState) return;
        _tabState = state;
        // The tab bar (Log | CatanBot | gear) is ALWAYS shown — hiding it was a
        // footgun that stranded the user with no way back to the log or
        // settings. The old "HUD only" replace mode is retired.
        tabs.style.display = 'flex';
        root.style.display = (tab === 'catanbot') ? 'block' : 'none';
        for (const child of kids) {
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
        const tb = document.getElementById('cbo-trade-badge');
        if (tb) tb.remove();
        const bo = document.getElementById('cbo-board-overlay');
        if (bo) bo.remove();
        document.querySelectorAll('.cbo-prow').forEach((e) => e.remove());
        clearCues();
        _container = null;
        _tabState = '';
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

    // ---- Parity sections. Each mirrors a side-panel block, compact for the
    // log column, and returns '' when its snapshot field is absent so the
    // caller can push unconditionally. Field shapes match the bridge's
    // _build_advisor_snapshot (strategy/race/bank/dev/dice/winning_move). ----
    const STRAT_LABELS = {
        OWS: 'Ore-Wheat-Sheep', LR_RUSH: 'Longest Road rush',
        PORT_TRADE: 'Port trader', RB_CARVED_TILES: 'Road Builder',
        BALANCED: 'Balanced',
    };
    const STRAT_ICONS = {
        OWS: '🏛', LR_RUSH: '🛣', PORT_TRADE: '⛵',
        RB_CARVED_TILES: '🛤', BALANCED: '⚖️',
    };
    // "You can win THIS turn" - highest-priority banner, press-the-button now.
    function winningMoveHtml(snap) {
        const w = snap.winning_move;
        if (!w || !w.message) return '';
        return `<div class="cbo-foot cbo-green cbo-strong">${escapeHtml(w.message)}</div>`;
    }
    // Strategy / board-affinity banner (snap.strategy from the selector).
    function strategyHtml(snap) {
        const st = snap.strategy;
        if (!st) return '';
        const ranking = Array.isArray(st.ranking) ? st.ranking : [];
        const preview = !st.active && ranking.length;
        if (!st.active && !preview) return '';
        if (preview) {
            const tease = ranking.slice(0, 3)
                .map((r) => STRAT_LABELS[r.tag] || r.tag).join(' · ');
            return '<div class="cbo-h">strategy</div>'
                + '<div class="cbo-strat">🧭 board affinity'
                + `<span class="cbo-strat-why">${escapeHtml(tease)}</span></div>`;
        }
        const tag = String(st.active);
        const label = STRAT_LABELS[tag] || tag;
        const icon = STRAT_ICONS[tag] || '🎯';
        const phase = st.phase
            ? ` <span class="cbo-strat-phase">${escapeHtml(st.phase)}</span>` : '';
        const why = st.rationale
            ? `<div class="cbo-strat-why">${escapeHtml(st.rationale)}</div>` : '';
        return '<div class="cbo-h">strategy</div>'
            + `<div class="cbo-strat">${icon} ${escapeHtml(label)}${phase}</div>${why}`;
    }
    // Longest-road + largest-army race banners (each ships a ready message).
    function raceHtml(snap) {
        const out = [];
        const cls = (lvl) => (lvl === 'opp_threat' ? 'cbo-red'
            : (lvl === 'self_push' ? 'cbo-green' : 'cbo-amber'));
        for (const race of [snap.longest_road_race, snap.largest_army_race]) {
            if (race && race.message) {
                out.push(`<div class="cbo-foot ${cls(race.level)}">`
                    + `${escapeHtml(race.message)}</div>`);
            }
        }
        return out.join('');
    }
    // Bank supply (low resources) + dev-deck remaining (with knight scarcity).
    function bankDevHtml(snap) {
        const out = [];
        const bank = snap.bank_supply;
        if (bank && Array.isArray(bank.low) && bank.low.length) {
            const chips = bank.low
                .map((x) => `${iconFor(x.resource)} ${x.count}`).join('  ');
            out.push('<div class="cbo-eco">bank low'
                + `<span class="cbo-eco-v">${chips}</span></div>`);
        }
        const dd = snap.dev_deck;
        if (dd && typeof dd.remaining === 'number') {
            const kn = (dd.by_type && dd.by_type.KNIGHT)
                ? dd.by_type.KNIGHT.remaining : null;
            const knTxt = (kn != null) ? ` · ${kn} kn` : '';
            const low = dd.low ? ' cbo-eco-low' : '';
            out.push(`<div class="cbo-eco${low}">dev deck`
                + `<span class="cbo-eco-v">${dd.remaining} left${knTxt}</span></div>`);
        }
        return out.join('');
    }
    // Dice tempo: hot numbers, a 7s cluster, a production drought, an engine
    // gap. One compact line; silent when nothing is anomalous.
    function tempoHtml(snap) {
        const bits = [];
        const hot = snap.hot_numbers;
        if (Array.isArray(hot) && hot.length) {
            bits.push('hot ' + hot.map((h) => h.number).join('/'));
        }
        if (snap.sevens_hot) bits.push(`${snap.sevens_hot.sevens}×7`);
        if (snap.production_stall) {
            bits.push(`${snap.production_stall.rolls_dry} dry`);
        }
        // engine_deficit gets its own full per-roll line (engineDeficitHtml),
        // so it's intentionally not duplicated as a bare bit here.
        if (!bits.length) return '';
        return `<div class="cbo-dice">🎲 ${escapeHtml(bits.join(' · '))}</div>`;
    }
    // Knight-in-hand nudge (snap.knight_hint: {have, should_play, reason}).
    function knightHintHtml(snap) {
        const kh = snap.knight_hint;
        if (!kh || !kh.should_play) return '';
        const why = kh.reason ? ` · ${escapeHtml(kh.reason)}` : '';
        return `<div class="cbo-foot cbo-amber">play a knight${why}</div>`;
    }
    // Post-game banner (snap.game_over: {winner, is_self, message}).
    function gameOverHtml(snap) {
        const g = snap.game_over;
        if (!g || !g.message) return '';
        const cls = g.is_self ? 'cbo-green cbo-strong' : 'cbo-amber';
        return `<div class="cbo-foot ${cls}">${escapeHtml(g.message)}</div>`;
    }
    // Missing chips from a {res: count} dict (shared by plan / milestone).
    function missingChips(missing) {
        return Object.entries(missing || {})
            .filter(([, n]) => n > 0)
            .map(([r, n]) => `${iconFor(r)}${n > 1 ? ` ${n}` : ''}`)
            .join(' ');
    }
    // 3rd-settlement milestone (snap.milestone): the single biggest early-game
    // predictor, so it gets a prominent banner. Compact: headline + what's left.
    function milestoneHtml(snap) {
        const m = snap.milestone;
        if (!m || !m.headline) return '';
        const miss = missingChips(m.missing);
        const tail = miss ? ` &middot; need ${miss}` : ' &middot; ready';
        const cls = miss ? 'cbo-amber' : 'cbo-green cbo-strong';
        return `<div class="cbo-foot ${cls}">\u{1F3E0} ${escapeHtml(m.headline)}`
            + `${tail}</div>`;
    }
    // Game plan (snap.game_plan): the near-term VP goal. Kind pill + goal tiles
    // + the bridge's own summary line ('ready to city' / '2 short, need ...').
    function gamePlanHtml(snap) {
        const gp = snap.game_plan;
        if (!gp || !gp.summary) return '';
        const ready = !gp.missing || Object.keys(gp.missing).length === 0;
        const label = gp.goal_kind === 'city' ? 'CITY' : 'SETTLE';
        const tiles = tilesText(gp.goal_tiles);
        const loc = tiles ? ` <span class="cbo-rec-tiles">${escapeHtml(tiles)}</span>` : '';
        const hops = (gp.roads_needed > 0) ? ` <span class="cbo-plan-sum">`
            + `${gp.roads_needed} road${gp.roads_needed > 1 ? 's' : ''}</span>` : '';
        return '<div class="cbo-h">plan</div>'
            + `<div class="cbo-plan${ready ? ' cbo-ready' : ''}">`
            + `<span class="cbo-rec-kind">${label}</span>${loc}${hops}</div>`
            + `<div class="cbo-plan-sum">${escapeHtml(gp.summary)}</div>`;
    }
    // Strategic options (snap.strategic_options): long-game VP-swing plays the
    // flat affordable-now rec list misses (LR push, LA defend/snipe, dev dive).
    function strategicOptionsHtml(snap) {
        const opts = snap.strategic_options;
        if (!Array.isArray(opts) || !opts.length) return '';
        const rows = opts.slice(0, 3).map((o) => {
            const vp = (o.vp_swing != null) ? `+${o.vp_swing}VP` : '';
            return '<div class="cbo-opt">'
                + (vp ? `<span class="cbo-opt-vp">${escapeHtml(vp)}</span>` : '')
                + `<span class="cbo-opt-label">${escapeHtml(o.label || o.kind || '')}</span>`
                + (o.detail ? `<span class="cbo-opt-detail">${escapeHtml(o.detail)}</span>` : '')
                + '</div>';
        }).join('');
        return '<div class="cbo-h">long game</div>' + rows;
    }
    // Dev-card cluster (monopoly_hint / yop_hint / rb_hint): a PLAY/HOLD/PLACE
    // verdict + the concrete play for each dev card you actually hold. Silent
    // when you hold none. Mirrors the side panel's dev block, compacted.
    function devClusterHtml(snap) {
        const rows = [];
        const verdict = (play, place) => {
            if (place) return '<span class="cbo-verdict cbo-v-place">PLACE</span>';
            return play
                ? '<span class="cbo-verdict cbo-v-play">PLAY</span>'
                : '<span class="cbo-verdict cbo-v-hold">HOLD</span>';
        };
        const mh = snap.monopoly_hint;
        if (mh && mh.have > 0) {
            const tgt = mh.resource ? `${iconFor(mh.resource)}` : '';
            const unlock = mh.unlock ? ` &middot; ${escapeHtml(mh.unlock)}` : '';
            rows.push('<div class="cbo-dev">' + verdict(mh.should_play)
                + '<span class="cbo-dev-name">mono</span>'
                + `<span class="cbo-dev-what">grab ${tgt} ~${mh.est_steal || 0}`
                + `${unlock}</span></div>`);
        }
        const yh = snap.yop_hint;
        if (yh && yh.have > 0) {
            const pair = (yh.pair || []).map(iconFor).join(' + ');
            const unlock = yh.unlock ? ` &middot; ${escapeHtml(yh.unlock)}` : '';
            rows.push('<div class="cbo-dev">' + verdict(yh.should_play)
                + '<span class="cbo-dev-name">plenty</span>'
                + `<span class="cbo-dev-what">take ${pair}${unlock}</span></div>`);
        }
        const rh = snap.rb_hint;
        if (rh && (rh.have > 0 || rh.free_roads_pending > 0)) {
            const placing = (rh.have <= 0 && rh.free_roads_pending > 0);
            const pl = rh.placement;
            const toward = pl && pl.toward_tiles ? tilesText(pl.toward_tiles) : '';
            const what = placing
                ? `lay road${toward ? ` &rarr; ${escapeHtml(toward)}` : ''}`
                : `2 free roads${toward ? ` &rarr; ${escapeHtml(toward)}` : ''}`;
            rows.push('<div class="cbo-dev">' + verdict(rh.should_play, placing)
                + '<span class="cbo-dev-name">road bld</span>'
                + `<span class="cbo-dev-what">${what}</span></div>`);
        }
        if (!rows.length) return '';
        return '<div class="cbo-h">dev cards</div>' + rows.join('');
    }
    // Proactive 7-prep warning (snap.seven_prep): spend down BEFORE the 7 lands.
    // Suppressed once a real discard is live so it doesn't double with discard.
    function sevenPrepHtml(snap) {
        const sp = snap.seven_prep;
        if (!sp || !sp.message || snap.discard_hint) return '';
        const drop = missingChips(sp.would_drop);
        const lose = drop ? ` &middot; lose ${drop}` : '';
        const cls = sp.level === 'danger' ? 'cbo-red' : 'cbo-amber';
        return `<div class="cbo-foot ${cls}">⚠ ${escapeHtml(sp.message)}`
            + `${lose}</div>`;
    }
    // Yield summary (snap.yield_summary): actual vs expected cards over a roll
    // window. Flags 'behind' when you're well under expectation (drought/robber).
    function yieldSummaryHtml(snap) {
        const y = snap.yield_summary;
        if (!y || !(y.window > 0)) return '';
        const exp = (typeof y.expected === 'number') ? y.expected : 0;
        const behind = (exp - y.got) > 0.3 * exp && exp > 1.0;
        const blk = (y.blocked > 0) ? ` &middot; ${y.blocked} blk` : '';
        return `<div class="cbo-eco${behind ? ' cbo-eco-low' : ''}">yield`
            + `<span class="cbo-eco-v">${y.got}/${exp.toFixed(1)} `
            + `(${y.window}r)${blk}</span></div>`;
    }
    // Engine deficit (snap.engine_deficit): the full per-roll comparison the
    // tempo line only hinted at. Leader name omitted -> streamer-safe.
    function engineDeficitHtml(snap) {
        const e = snap.engine_deficit;
        if (!e) return '';
        const ratio = (typeof e.ratio === 'number') ? `${e.ratio.toFixed(1)}x` : '';
        return '<div class="cbo-eco cbo-eco-low">engine gap'
            + `<span class="cbo-eco-v">${e.self_per_roll}/roll vs `
            + `${e.leader_per_roll}${ratio ? ` (${ratio})` : ''}</span></div>`;
    }
    // Round / phase + standings status strip. Quiet, sits at the foot. Leader
    // name suppressed in streamer mode (gap still shown).
    function progressHtml(snap) {
        const gp = snap.game_progress;
        if (!gp) return '';
        const phase = gp.phase
            ? `<span class="cbo-prog-phase">${escapeHtml(gp.phase)}</span>` : '';
        const round = (gp.round != null) ? `round ${gp.round}` : '';
        let lead = '';
        const st = snap.standings;
        if (st && st.leader && (st.self_vp >= 3 || st.leader.vp >= 3)) {
            if (st.self_is_leader) {
                lead = `<span class="cbo-prog-lead cbo-self-lead">you `
                    + `${st.self_vp} (lead)</span>`;
            } else {
                const who = streamerOn()
                    ? 'leader' : escapeHtml(st.leader.username || 'leader');
                lead = `<span class="cbo-prog-lead">${who} ${st.leader.vp}`
                    + ` &middot; you ${st.self_vp} (-${st.gap_to_leader})</span>`;
            }
        }
        if (!round && !phase && !lead) return '';
        return `<div class="cbo-prog">${round} ${phase}${lead}</div>`;
    }
    // Variant-board advisor lines (gold pick / fog) + a variant tag. These keys
    // come straight from the bridge; the side panel only shows a variant badge,
    // so this is in-page-only value for volcano / black-forest / scanned maps.
    function variantHtml(snap) {
        const out = [];
        const g = snap.gold_pick;
        if (g && g.resource) {
            const tw = g.toward ? ` &middot; ${escapeHtml(g.toward)}` : '';
            out.push('<div class="cbo-eco">gold pick'
                + `<span class="cbo-eco-v">${iconFor(g.resource)}${tw}</span></div>`);
        }
        const f = snap.fog_hint;
        if (f && f.message) {
            out.push(`<div class="cbo-dice">\u{1F32B} ${escapeHtml(f.message)}</div>`);
        }
        return out.join('');
    }

    // Build the HUD body HTML from an /advisor snapshot: top rec (gated like
    // the side panel) + robber targets + your card + opponent reads + footer.
    function renderBody(snap) {
        if (!snap) return '<div class="cbo-placeholder">connecting…</div>';
        const out = [];
        const push = (html) => { if (html) out.push(html); };
        // Post-game first (it overrides everything), then "you can win now",
        // then the single biggest early signal (the 3rd-settlement milestone).
        push(gameOverHtml(snap));
        push(winningMoveHtml(snap));
        push(milestoneHtml(snap));
        const recs = snap.recommendations || [];
        const showRecs = (snap.my_turn || snap.setup_phase) && !_paused();
        if (_paused()) {
            out.push('<div class="cbo-h">next move</div>'
                + '<div class="cbo-placeholder">recs paused</div>');
        } else if (snap.variant_recs_disabled) {
            out.push('<div class="cbo-h">next move</div>'
                + '<div class="cbo-placeholder">recs off (variant board)</div>');
        } else if (showRecs && recs.length) {
            // Click-to-cycle: clicking the rec advances to the next-best one.
            // _recIdx persists across re-renders but resets when the rec set
            // itself changes (a new turn / decision), tracked via _recSig.
            const recSig = recs.map((r) => `${r.kind || ''}.${r.action || ''}`
                + `.${JSON.stringify(r.tiles || (r.road && r.road.edge_tiles) || '')}`)
                .join('|');
            if (recSig !== _recSig) { _recSig = recSig; _recIdx = 0; }
            const idx = Math.min(_recIdx, recs.length - 1);
            const cyc = recs.length > 1
                ? `<span class="cbo-rec-cyc-hint">${idx + 1}/${recs.length}`
                    + ' &middot; click for next</span>' : '';
            let block = '<div class="cbo-h">next move</div>'
                + `<div class="cbo-rec${recs.length > 1 ? ' cbo-rec-cyc' : ''}"`
                + ` title="${recs.length > 1 ? 'click for the next-best option' : ''}">`
                + `${topRecHtml(recs[idx])}${cyc}</div>`;
            // If the shown pick is a trade, surface the next one as the
            // "if they deny, do this instead" fallback (Noah's ask).
            const kc = recs[idx].action === 'road' ? 'road' : recs[idx].kind;
            const nxt = recs[idx + 1] || recs[0];
            if ((kc === 'trade' || kc === 'propose_trade') && nxt && nxt !== recs[idx]) {
                block += `<div class="cbo-fallback">if denied &rarr; `
                    + `${topRecHtml(nxt)}</div>`;
            }
            out.push(block);
        } else if (showRecs) {
            out.push('<div class="cbo-h">next move</div>'
                + '<div class="cbo-placeholder">no recommendation</div>');
        }
        // Near-term goal frame + strategy archetype + long-game swing plays.
        push(gamePlanHtml(snap));
        push(strategyHtml(snap));
        push(strategicOptionsHtml(snap));
        push(robberHtml(snap));
        // Discard guidance, re-homed into the HUD (was a floating panel). On a
        // 7 the matching cards in your hand also glow (applyActionCues). The
        // proactive seven_prep warning self-suppresses once a discard is live.
        const dh = snap.discard_hint;
        if (dh) {
            out.push('<div class="cbo-foot cbo-amber">DISCARD &middot; '
                + escapeHtml(dh.reason || dh.message
                    || 'drop your lowest-value cards') + '</div>');
        }
        push(sevenPrepHtml(snap));
        // Dev-card plays (verdict per held card), then your card, knight nudge.
        push(devClusterHtml(snap));
        push(selfHtml(snap));
        push(knightHintHtml(snap));
        // Opponent reads are injected straight onto colonist's own player rows
        // (injectPlayerReads), not listed here — that frees a big chunk of the
        // log column and keeps the read where you're already looking.
        // Board-state cluster: LR/LA race, bank + dev deck, yield vs expected,
        // engine gap, dice tempo, variant (gold/fog). Each is silent unless its
        // read is live, so the column stays quiet in a calm position.
        const board = raceHtml(snap) + bankDevHtml(snap) + yieldSummaryHtml(snap)
            + engineDeficitHtml(snap) + tempoHtml(snap) + variantHtml(snap);
        if (board) {
            out.push('<div class="cbo-h">board</div>' + board);
        }
        push(footerHtml(snap));
        // Quiet round / phase / standings status strip at the very foot.
        push(progressHtml(snap));
        if (!out.length) {
            return '<div class="cbo-placeholder">CatanBot HUD'
                + ' — start a game to see recommendations.</div>';
        }
        return out.join('');
    }

    // Compact resource read for a colonist player row: confirmed cards as
    // icon+count, then a thin separator and the likely-but-unsure resources as
    // dim icons. Same model as the HUD's hand read, sized for the tracker row.
    function rowReadHtml(o) {
        const conf = [];
        const likely = [];
        const hp = o && o.hand_probs;
        if (hp) {
            for (const res of Object.keys(hp)) {
                if ((hp[res].min || 0) > 0) {
                    conf.push(`<span class="cbo-pr-chip">${iconFor(res)}${hp[res].min}</span>`);
                }
            }
            Object.keys(hp)
                .filter((r) => (hp[r].min || 0) === 0 && (hp[r].p1 || 0) > LIKELY_CUTOFF)
                .sort((a, b) => (hp[b].p1 || 0) - (hp[a].p1 || 0))
                .forEach((r) => likely.push(
                    `<span class="cbo-pr-chip cbo-pr-maybe">${iconFor(r)}</span>`));
        } else {
            const hand = (o && o.hand) || {};
            for (const res of Object.keys(hand)) {
                if (hand[res] > 0) {
                    conf.push(`<span class="cbo-pr-chip">${iconFor(res)}${hand[res]}</span>`);
                }
            }
            if (o && (o.unknown || 0) > 0) {
                likely.push(`<span class="cbo-pr-chip cbo-pr-maybe">?${o.unknown}</span>`);
            }
        }
        let html = conf.join(' ');
        if (likely.length) {
            html += (conf.length ? '<span class="cbo-pr-sep">|</span>' : '')
                + likely.join(' ');
        }
        return html || '<span class="cbo-pr-none">no cards</span>';
    }

    // Inject the inferred resource breakdown straight onto colonist's OWN
    // opponent player rows (recon: gamePlayerInformationContainer >
    // opponentPlayerRow > playerInformation), pinned along the bottom of each
    // tracker panel so the read lives where Noah is already looking. Matched to
    // a player by the name the row starts with, re-run each poll so it survives
    // React wiping the row, silent when the panel isn't present.
    function injectPlayerReads(snap) {
        const wipe = () => document.querySelectorAll('.cbo-prow')
            .forEach((e) => e.remove());
        if (!enabled() || !snap) { wipe(); return; }
        const cont = document.querySelector(
            '[class*="gamePlayerInformationContainer"]');
        if (!cont) { wipe(); return; }
        const rows = cont.querySelectorAll('[class*="opponentPlayerRow"]');
        if (!rows.length) { wipe(); return; }
        const opps = (snap.opps || [])
            .filter((o) => o && !o.is_placeholder && o.username);
        for (const row of rows) {
            const info = row.querySelector('[class*="playerInformation"]') || row;
            const txt = (row.textContent || '').trim();
            let match = null;
            let best = 0;
            for (const o of opps) {
                if (txt.startsWith(o.username) && o.username.length > best) {
                    match = o; best = o.username.length;
                }
            }
            let read = info.querySelector(':scope > .cbo-prow');
            if (!match) { if (read) read.remove(); continue; }
            if (getComputedStyle(info).position === 'static') {
                info.style.position = 'relative';
            }
            if (!read) {
                read = document.createElement('div');
                read.className = 'cbo-prow';
                info.appendChild(read);
            }
            read.innerHTML = rowReadHtml(match);
            stampStreamer(read);
        }
    }

    // Literal trade injection: pin a CatanBot verdict badge to colonist's own
    // trade panel. Recon: the accept/decline icons are IMG .tradeResponseStatus-*
    // and the collapse is IMG .showHideTradeIcon-*; walk up to the panel. Sets
    // _tradeAnchored so the notification doesn't double up.
    function injectTradeBadge(snap) {
        _tradeAnchored = false;
        const old = document.getElementById('cbo-trade-badge');
        if (!enabled() || !snap || !snap.incoming_trade) {
            if (old) old.remove();
            _currentTradeKey = null;
            return;
        }
        const t = snap.incoming_trade;
        // Stable key for this offer so a dismiss (the user clicked
        // accept/decline) suppresses the badge instantly without waiting for
        // the bridge to clear incoming_trade a poll or two later.
        let key;
        try { key = JSON.stringify(t.give || t.offer || t.want || t); }
        catch (e) { key = String(t.reason || ''); }
        _currentTradeKey = key;
        if (key === _tradeDismissedKey) { if (old) old.remove(); return; }
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
        const v = ['accept', 'decline', 'consider'].includes(t.verdict)
            ? t.verdict : 'consider';
        badge.className = `cbo-tb-${v}`;
        badge.innerHTML = '<span class="cbo-tb-tag">CatanBot</span>'
            + `<span class="cbo-tb-verdict">${v.toUpperCase()}</span>`
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

    // Instant dismiss: the moment the user acts on colonist's trade panel
    // (accept / decline / counter / collapse), hide our verdict badge and
    // suppress it for this offer, so it never lingers a beat after the click.
    function wireTradeDismiss() {
        if (_tradeDismissWired) return;
        _tradeDismissWired = true;
        document.addEventListener('click', (e) => {
            const tb = document.getElementById('cbo-trade-badge');
            if (!tb || tb.style.display === 'none') return;
            if (e.target && e.target.closest && e.target.closest(
                '[class*="tradeResponseStatus"], [class*="showHideTradeIcon"], '
                + '[class*="tradeOffer"], [class*="tradeMovement"], '
                + '[class*="tradePanel"], [class*="acceptButton"], '
                + '[class*="declineButton"]')) {
                _tradeDismissedKey = _currentTradeKey;
                tb.style.display = 'none';
            }
        }, true);
    }

    // ---- Board overlay. The board is a WebGL canvas (#game-canvas) with no
    // DOM per tile, and colonist exposes no coordinates, so tile screen
    // positions are computed geometrically from catanatron's cube coords
    // (q = coord[0], r = coord[2]; EAST = +screen-x, pointy-top). The board
    // center + hex size are stored as fractions of the canvas rect so the map
    // scales with the window. Calibrated live against a real game; it lands the
    // ring on the correct tile (slight off-center from colonist's 3D tilt is
    // fine — the HUD's ranked text read is the exact backup). ----
    const BOARD_CALIB = { fx: 0.339, fy: 0.474, fdE: 0.0684, fdV: 0.0739 };
    function boardCoordToPixel(coord) {
        const canvas = document.getElementById('game-canvas');
        if (!canvas || !Array.isArray(coord) || coord.length < 3) return null;
        const r = canvas.getBoundingClientRect();
        if (!r.width || !r.height) return null;
        const cx = r.left + BOARD_CALIB.fx * r.width;
        const cy = r.top + BOARD_CALIB.fy * r.height;
        const dE = BOARD_CALIB.fdE * r.width;
        const dV = BOARD_CALIB.fdV * r.width;
        const q = coord[0];
        const rr = coord[2];
        // py uses -rr: catanatron r grows toward colonist's NORTH (screen-up),
        // per the bridge's coordinate notes (colonist +ay = SOUTH = down maps to
        // r = -1). The horizontal shear keeps each row centered, so it isn't
        // flipped. Pending a final confirm on a live robber (trivial to revert).
        return { x: cx + q * dE + rr * (dE / 2), y: cy - rr * dV, size: dE };
    }
    // Draw a green ring over the recommended robber tile (and dimmer rings on
    // the 2nd/3rd choices) whenever the robber decision is live. Mirrors
    // robberHtml's gating; silent otherwise. Re-runs each poll so the rings
    // track a window resize and clear the instant the decision ends.
    function updateBoardOverlay(snap) {
        let layer = document.getElementById('cbo-board-overlay');
        const show = enabled() && snap && !_paused()
            && (snap.robber_pending || snap.robber_reason === 'knight');
        const targets = (snap && snap.robber_targets) || [];
        if (!show || !targets.length) {
            if (layer) layer.innerHTML = '';
            return;
        }
        if (!layer) {
            layer = document.createElement('div');
            layer.id = 'cbo-board-overlay';
            (document.body || document.documentElement).appendChild(layer);
        }
        const marks = [];
        for (let i = 0; i < Math.min(3, targets.length); i += 1) {
            const p = boardCoordToPixel(targets[i].coord);
            if (!p) continue;
            // Pointy-top hexagon traced around the tile: width = hex flat-to-
            // flat (~size), height taller by 2/sqrt(3). viewBox is a unit hex.
            const w = Math.round(p.size * 1.02);
            const h = Math.round(p.size * 1.18);
            const rankCls = i === 0 ? '' : (i === 1 ? ' cbo-bo-rank2' : ' cbo-bo-rank3');
            marks.push(`<svg class="cbo-bo-mark${rankCls}" viewBox="0 0 100 116"`
                + ` preserveAspectRatio="none" style="left:${Math.round(p.x - w / 2)}px;`
                + `top:${Math.round(p.y - h / 2)}px;width:${w}px;height:${h}px">`
                + '<polygon class="cbo-bo-hex" points="50,2 97,30 97,86 50,114 3,86 3,30"/>'
                + '</svg>');
        }
        layer.innerHTML = marks.join('');
        stampStreamer(layer);
    }

    // Clear every cue by walking the handles we recorded (NO full-document
    // querySelectorAll) so an idle tick costs nothing. A glowed element that
    // React already removed from the DOM just no-ops on classList.remove.
    function clearCues() {
        for (const el of _cuedEls) {
            try {
                el.classList.remove(
                    'cbo-action-hl', 'cbo-cue-dev', 'cbo-cue-knight');
            } catch (e) { /* element gone */ }
        }
        _cuedEls = [];
    }
    function addCue(el, cls) {
        if (!el) return;
        el.classList.add(cls);
        _cuedEls.push(el);
    }
    // Would any cue fire for this snapshot? Cheap, field-only check so we can
    // skip the DOM sweeps entirely on the many ticks with nothing to cue
    // (opponent turns, pre-roll, etc.).
    function cuesWanted(snap) {
        if (snap.my_turn) return true;        // build bar / setup place button
        if (snap.discard_hint) return true;   // cards to drop on a 7
        const kh = snap.knight_hint;
        if (kh && kh.should_play) return true;
        return (snap.recommendations || []).some(
            (r) => r && (r.kind === 'knight' || r.action === 'knight'));
    }

    // Unified action cues: light up the REAL colonist element you act on.
    // One entry point, called each poll. Clears prior cues via the tracked
    // handles, bails fast when nothing needs a cue, then each sub-cue applies
    // under its own guard. Cues anchor to colonist's own DOM, never a floater.
    function applyActionCues(snap) {
        clearCues();
        if (!enabled() || !snap || _paused() || !cuesWanted(snap)) return;
        highlightBuildButton(snap);
        // No setup-phase bottom-bar glow: it lit the whole action container,
        // which read as "highlight everything". Opening placements are cued on
        // the board instead (see the board overlay), not on the bottom bar.
        highlightKnightCard(snap);
        highlightDiscardCards(snap);
    }

    // Build costs, used to gate the button glow on what you can actually
    // afford RIGHT NOW (the rec can be a multi-turn goal like "city, need 2
    // wheat" — we shouldn't glow a button you can't click).
    const BUILD_COST = {
        road: { WOOD: 1, BRICK: 1 },
        settlement: { WOOD: 1, BRICK: 1, SHEEP: 1, WHEAT: 1 },
        city: { WHEAT: 2, ORE: 3 },
        dev_card: { SHEEP: 1, WHEAT: 1, ORE: 1 },
        buy_dev: { SHEEP: 1, WHEAT: 1, ORE: 1 },
    };
    function canAffordBuild(me, kind) {
        const cost = BUILD_COST[kind];
        if (!cost) return true;   // unknown kind -> don't suppress
        const hand = (me && me.hand) || {};
        for (const res of Object.keys(cost)) {
            if ((hand[res] || 0) < cost[res]) return false;
        }
        return true;
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
        // Only glow a build you can pay for this turn — never light up a "city"
        // button when the city is just the plan and you're short the resources.
        if (!canAffordBuild(snap.self, kind)) return;
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
        addCue(target, 'cbo-action-hl');
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
        addCue(card, 'cbo-cue-knight');
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
            for (const el of els) addCue(el, 'cbo-cue-dev');
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

    // Signature of a snapshot, used to skip the expensive innerHTML rebuild on
    // ticks where nothing the HUD shows actually changed (the common case on an
    // opponent's turn). The bridge stamps every snapshot with a monotonic `seq`
    // that ticks on EVERY ingest frame (so it changes when nothing rendered
    // changed) — plus `ws_frames`/`log_events` counters and big history/stats
    // blobs the HUD never renders. All of those are stripped here so the
    // compare reflects only what the HUD actually shows. This is strictly more
    // correct than diffing on `seq`: it also catches time-based eviction
    // (robber-snapshot staleness, trade expiry) that leaves `seq` unchanged.
    // Any failure returns a unique value so we always re-render rather than
    // risk a stale UI.
    const SIG_SKIP = new Set([
        'seq', 'ws_frames', 'log_events', 'total_rolls', 'bridge_version',
        'roll_history', 'move_history', 'eval_history', 'steal_matrix',
        'dice_expected', 'roll_histogram', 'latest_postmortem',
    ]);
    let _sigErr = 0;
    function snapSignature(snap) {
        try {
            return JSON.stringify(
                snap, (k, v) => (SIG_SKIP.has(k) ? undefined : v));
        } catch (e) {
            _sigErr += 1;
            return `__err${_sigErr}`;
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
            // Only rebuild the HUD body when the snapshot the HUD renders
            // actually changed. The body is our own DOM (React never touches
            // it), so a skipped tick keeps the exact same nodes — no churn.
            const sig = snapSignature(snap);
            if (sig !== _lastSig) {
                root.innerHTML = renderBody(snap);
                root.className = urgencyOf(snap);   // left-border urgency accent
                stampStreamer(root);   // re-stamp the freshly rendered nodes
                _lastSig = sig;
            }
            // These anchor to colonist's volatile DOM and self-gate cheaply, so
            // they run every tick (re-asserting the glow if React wiped it, and
            // repositioning the trade badge) even when the body render is
            // skipped. Both no-op fast when their trigger isn't live.
            injectTradeBadge(snap);   // verdict pinned to colonist's trade panel
            injectPlayerReads(snap);  // resource read onto colonist's player rows
            updateBoardOverlay(snap); // green ring on the recommended robber tile
            applyActionCues(snap);    // glow the real element to act on
            _lastSnap = snap;
            _bridgeDown = false;
            if (!_everConnected) { _everConnected = true; applyTab(); sizeHudBody(); }
        } else if (!_bridgeDown) {
            _bridgeDown = true;
            _lastSig = null;
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

    // Cap the HUD body to the log box and let it scroll inside, so a tall
    // snapshot never spills past colonist's beige panel. Bounds to the lesser
    // of the container's own height and the space down to the viewport bottom
    // (the latter catches a container that auto-grows with our content). Only
    // called on attach / connect / resize — never the per-tick hot path — since
    // it reads layout; max-height is a cap so it needs no per-content recompute.
    function sizeHudBody() {
        if (!root || !_container || !_container.isConnected) return;
        try {
            const tabsH = (tabs && tabs.offsetHeight) || 28;
            const contH = _container.clientHeight;
            const rootTop = root.getBoundingClientRect().top;
            let avail = window.innerHeight - rootTop - 8;
            if (contH > tabsH + 60) avail = Math.min(avail, contH - tabsH - 2);
            root.style.maxHeight = `${Math.max(140, Math.round(avail))}px`;
        } catch (e) { /* detached mid-measure */ }
    }

    // Called by content.js's observer + 500ms interval (via
    // window.__catanbot.ensureHudAttached) and by our own driver below.
    function ensureHudAttached() {
        if (!enabled()) { teardown(); return; }
        // Fast path: we're still anchored where we put it. Skip the container
        // search (a querySelector over class-prefix selectors) and the
        // re-insert entirely; just keep the tab/native-hide state fresh, which
        // is itself a no-op when nothing changed. This is the hot path: it runs
        // on every colonist log mutation plus two safety-net intervals.
        if (root && tabs && _container && _container.isConnected
                && root.parentElement === _container
                && tabs.parentElement === _container) {
            applyTab(_container);
            return;
        }
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
        _container = container;
        applyTab(container);
        sizeHudBody();
    }

    // Expose for content.js's re-anchor hook, and run our own lightweight
    // driver (content.js only drives after it finds the scroller; the
    // container can exist independently, so we self-drive too).
    window.__catanbot = window.__catanbot || {};
    window.__catanbot.ensureHudAttached = ensureHudAttached;

    // Toolbar-icon click (background.js) -> open the in-page settings menu.
    try {
        chrome.runtime.onMessage.addListener((msg) => {
            if (msg && msg.type === 'open-settings') {
                try { openSettings(); } catch (e) { /* not attached yet */ }
            }
            return false;
        });
    } catch (e) { /* no chrome.runtime in this context */ }

    try { ensureHudAttached(); } catch (e) { /* container not ready yet */ }
    try { wireTradeDismiss(); } catch (e) { /* body not ready */ }
    // Re-cap the body height when the window resizes (the log box height tracks
    // the viewport). Debounced so a drag-resize doesn't thrash layout.
    let _resizeTimer = null;
    window.addEventListener('resize', () => {
        if (_resizeTimer) return;
        _resizeTimer = setTimeout(() => { _resizeTimer = null; sizeHudBody(); }, 150);
    });
    // Re-anchor safety net. content.js already re-anchors on every log mutation
    // and on its own 500ms interval; with the fast path this is a cheap
    // isConnected check, so 1000ms here is plenty.
    setInterval(() => {
        try { ensureHudAttached(); } catch (e) { /* keep trying */ }
    }, 1000);

    // Adaptive data poll: snappy when it's your move or a decision is live,
    // relaxed on opponents' turns. Self-scheduling so the cadence tracks the
    // last snapshot. Idle ticks are cheap now (the body render is skipped when
    // the snapshot is unchanged), so the slower idle rate only trims the
    // worker round-trips, never responsiveness when it matters.
    const POLL_ACTIVE = 450;
    const POLL_IDLE = 1100;
    function pollDelay() {
        const s = _lastSnap;
        if (!s) return POLL_ACTIVE;   // stay responsive until first connect
        if (s.my_turn || s.setup_phase || s.robber_pending
                || s.incoming_trade || s.discard_hint) return POLL_ACTIVE;
        return POLL_IDLE;
    }
    function pollLoop() {
        Promise.resolve()
            .then(fetchAndRender)
            .catch(() => { /* bridge hiccup */ })
            .then(() => { setTimeout(pollLoop, pollDelay()); });
    }
    pollLoop();

    console.info(LOG_PREFIX, 'ready (on by default; disable with'
        + " localStorage 'catanbot.log_hud'='0')");
})();
