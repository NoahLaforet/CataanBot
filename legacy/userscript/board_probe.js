// cataanbot — one-shot colonist.io board DOM probe (v2).
//
// Run this from the devtools Console while you're in an active game.
// It dumps a wide capture of the page DOM to a downloadable JSON file
// so we can build the DOM-coord → catanatron-node mapping offline.
//
// v2 changes over v1: walks the whole <body>, not just #ui-game. v1
// captured 7 elements because colonist renders the hex board in a
// sibling container outside #ui-game and our selector list was too
// narrow. v2 is broad — anything visible with a className or a
// <canvas>/<svg>/<img>/<polygon>/<circle> tag gets included, and we
// also print a class-prefix histogram to the console so you can eyeball
// whether we actually caught the board before uploading.
//
// Safe to re-run mid-game. Does NOT modify the DOM. Does NOT send
// anything over the network (all offline, user-initiated download).

(() => {
    'use strict';

    const MAX_ELEMENTS = 4000;   // guardrail — base board needs ~50-200
    const MIN_AREA_PX = 16;      // skip 1x1 markers, keep everything else

    // Strip colonist's rotating hash suffix (-aBcD1eF2) from a class
    // name so the summary groups variants that are the same semantic
    // component across deploys.
    function stripHash(cls) {
        return (cls || '')
            .split(/\s+/)
            .map(tok => tok.replace(/-[A-Za-z0-9_]{6,}$/, ''))
            .filter(Boolean)
            .join(' ');
    }

    function visibleBBox(el) {
        const r = el.getBoundingClientRect();
        if (r.width * r.height < MIN_AREA_PX) return null;
        if (r.bottom < 0 || r.top > window.innerHeight) return null;
        if (r.right < 0 || r.left > window.innerWidth) return null;
        return { x: r.x, y: r.y, w: r.width, h: r.height };
    }

    function serializeElement(el) {
        const bbox = visibleBBox(el);
        if (!bbox) return null;
        const attrs = {};
        for (const a of el.attributes) {
            // keep attrs short — skip inline style's long declarations
            // unless it's a transform (positions live there).
            if (a.name === 'style') {
                const m = a.value.match(/transform\s*:\s*[^;]+/i);
                if (m) attrs.style_transform = m[0];
                continue;
            }
            attrs[a.name] = a.value.length > 200
                ? a.value.slice(0, 200) + '…' : a.value;
        }
        return {
            tag: el.tagName,
            className: stripHash(el.className?.baseVal ?? el.className ?? ''),
            rawClassName: (el.className?.baseVal ?? el.className ?? '')
                .toString().slice(0, 200),
            id: el.id || '',
            attrs,
            bbox,
            text: (el.textContent || '').trim().slice(0, 80),
        };
    }

    // Everything with a className OR a structural tag we care about.
    function isCandidate(el) {
        if (!(el instanceof Element)) return false;
        const t = el.tagName;
        if (t === 'CANVAS' || t === 'SVG' || t === 'IMG'
            || t === 'POLYGON' || t === 'CIRCLE' || t === 'IMAGE'
            || t === 'PATH' || t === 'G') return true;
        const cls = (el.className?.baseVal ?? el.className ?? '').toString();
        return cls.length > 0;
    }

    function walkAll(root) {
        const out = [];
        const walker = document.createTreeWalker(
            root, NodeFilter.SHOW_ELEMENT, null);
        let node = walker.currentNode;
        while (node) {
            if (isCandidate(node)) {
                const ser = serializeElement(node);
                if (ser) {
                    out.push(ser);
                    if (out.length >= MAX_ELEMENTS) break;
                }
            }
            node = walker.nextNode();
        }
        return out;
    }

    function summarizeByPrefix(elements) {
        const byPrefix = new Map();
        for (const e of elements) {
            const key = e.tag + ' ' + (e.className.split(' ')[0] || '(none)');
            if (!byPrefix.has(key)) byPrefix.set(key, 0);
            byPrefix.set(key, byPrefix.get(key) + 1);
        }
        return Array.from(byPrefix.entries())
            .sort((a, b) => b[1] - a[1]);
    }

    function probe() {
        const elements = walkAll(document.body);
        const canvases = elements.filter(e => e.tag === 'CANVAS');
        const svgs = elements.filter(e => e.tag === 'SVG');
        const imgs = elements.filter(e => e.tag === 'IMG');
        const payload = {
            schema_version: 2,
            ts: Date.now() / 1000,
            url: window.location.href,
            viewport: {
                w: window.innerWidth, h: window.innerHeight,
                dpr: window.devicePixelRatio,
            },
            document_title: document.title,
            body_bbox: (() => {
                const r = document.body.getBoundingClientRect();
                return { x: r.x, y: r.y, w: r.width, h: r.height };
            })(),
            counts: {
                total: elements.length,
                canvas: canvases.length,
                svg: svgs.length,
                img: imgs.length,
            },
            elements,
        };
        console.log(`[cataanbot-probe v2] captured ${elements.length} elements `
            + `(${canvases.length} canvas, ${svgs.length} svg, `
            + `${imgs.length} img)`);
        console.log('[cataanbot-probe v2] top class prefixes:');
        const summary = summarizeByPrefix(elements).slice(0, 30);
        console.table(summary.map(([k, n]) => ({ prefix: k, count: n })));
        if (canvases.length > 0) {
            console.log('[cataanbot-probe v2] canvas bboxes:');
            console.table(canvases.map(c => ({
                id: c.id, class: c.className,
                x: Math.round(c.bbox.x), y: Math.round(c.bbox.y),
                w: Math.round(c.bbox.w), h: Math.round(c.bbox.h),
            })));
        }
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
        console.log('[cataanbot-probe v2] saved cataanbot-board-probe.json');
    }

    const payload = probe();
    download(payload);
})();
