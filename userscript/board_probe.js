// cataanbot — one-shot colonist.io board DOM probe.
//
// Run this from the devtools Console while you're in an active game.
// It dumps the board's SVG/DOM structure to a downloadable JSON file
// so we can build the DOM-coord → catanatron-node mapping offline.
//
// What it captures:
//   - The top-level game-board container (tries several selectors).
//   - Every child SVG element with a position + class + data-* attrs.
//   - Any <image>, <polygon>, <circle>, <g> under the board (tiles,
//     number pips, settlements, roads, robber, ports).
//   - A screenshot-friendly bounding box so we can line up hex coords.
//
// Output: prompts a file download named cataanbot-board-probe.json.
// Email/AirDrop it back to the repo checkout, we'll parse it.
//
// Safe to re-run mid-game. Does NOT modify the DOM. Does NOT send
// anything over the network (all offline, user-initiated download).

(() => {
    'use strict';

    // Candidate selectors for the board container. Colonist's class
    // hashes rotate across deploys, so we cast a wide net: first try
    // the known ID, then fall back to "any SVG with 19+ polygon tiles"
    // which is a structural match for the Catan hex grid.
    const CONTAINER_CANDIDATES = [
        '#ui-game',
        'div.containerLandscape-TGQeBgol',
        'div[class*="gameBoard"]',
        'div[class*="board"]',
    ];

    function findBoardRoot() {
        for (const sel of CONTAINER_CANDIDATES) {
            const el = document.querySelector(sel);
            if (el) return el;
        }
        // Structural fallback: find any SVG whose polygon count looks
        // like a Catan board (19 land hexes ± frame/water).
        const svgs = document.querySelectorAll('svg');
        for (const svg of svgs) {
            const polys = svg.querySelectorAll('polygon');
            if (polys.length >= 19 && polys.length < 80) return svg;
        }
        return null;
    }

    function serializeElement(el) {
        const rect = el.getBoundingClientRect();
        const attrs = {};
        for (const a of el.attributes) attrs[a.name] = a.value;
        return {
            tag: el.tagName,
            className: el.className?.baseVal ?? el.className ?? '',
            id: el.id || '',
            attrs,
            bbox: {
                x: rect.x, y: rect.y, w: rect.width, h: rect.height,
            },
            text: (el.textContent || '').trim().slice(0, 60),
        };
    }

    function walkInteresting(root) {
        // Grab everything that looks structural: SVG shapes, images,
        // <g> groups, and positioned HTML divs (tile labels, number
        // pip overlays). Keep it flat — we'll cross-reference offline.
        const selectors = [
            'svg', 'polygon', 'circle', 'image', 'g',
            'div[style*="transform"]', 'div[class*="tile"]',
            'div[class*="hex"]', 'div[class*="port"]',
            'div[class*="settlement"]', 'div[class*="road"]',
            'div[class*="robber"]',
        ];
        const seen = new Set();
        const out = [];
        for (const sel of selectors) {
            root.querySelectorAll(sel).forEach((el) => {
                if (seen.has(el)) return;
                seen.add(el);
                out.push(serializeElement(el));
            });
        }
        return out;
    }

    function probe() {
        const root = findBoardRoot();
        if (!root) {
            console.error('[cataanbot-probe] no board container found — '
                + 'are you in a game?');
            return null;
        }
        const rootRect = root.getBoundingClientRect();
        const payload = {
            ts: Date.now() / 1000,
            url: window.location.href,
            viewport: {
                w: window.innerWidth, h: window.innerHeight,
                dpr: window.devicePixelRatio,
            },
            board_root: {
                tag: root.tagName,
                className: root.className?.baseVal ?? root.className ?? '',
                id: root.id || '',
                bbox: {
                    x: rootRect.x, y: rootRect.y,
                    w: rootRect.width, h: rootRect.height,
                },
            },
            elements: walkInteresting(root),
        };
        console.log(`[cataanbot-probe] captured ${payload.elements.length} `
            + 'board elements');
        return payload;
    }

    function download(payload) {
        const blob = new Blob([JSON.stringify(payload, null, 2)],
            { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'cataanbot-board-probe.json';
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        console.log('[cataanbot-probe] saved cataanbot-board-probe.json');
    }

    const payload = probe();
    if (payload) download(payload);
})();
