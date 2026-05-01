// Side panel render. Polls /advisor on the local bridge and rebuilds
// the panel content on each new snapshot. Same UX pattern as the
// userscript, just simpler — no shadow DOM (we own the document), no
// pop-out (this IS the pop-out), no GM_xmlhttpRequest (extension has
// host_permissions for localhost).

const BRIDGE_BASE = 'http://127.0.0.1:8765';
const BRIDGE_ADVISOR_URL = `${BRIDGE_BASE}/advisor`;
const BRIDGE_FEEDBACK_URL = `${BRIDGE_BASE}/feedback`;
const POLL_MS = 500;

const RES_GLYPH = {
    WOOD: '🌲', BRICK: '🧱', SHEEP: '🐑',
    WHEAT: '🌾', ORE: '⛰️',
};
function glyphFor(res) {
    return RES_GLYPH[(res || '').toUpperCase()] || res || '';
}
function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

const panel = document.getElementById('panel');
const statusEl = document.getElementById('status');
const contentEl = document.getElementById('content');
const variantBadge = document.getElementById('variant-badge');
const friendlyBadge = document.getElementById('friendly-robber-badge');

// Latest snapshot — read by the feedback click handler so each
// labeled rec has game-state context.
let latestSnap = null;
let lastSeq = -1;

document.getElementById('reset').addEventListener('click', async () => {
    try {
        await fetch(`${BRIDGE_BASE}/reset`,
            { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({}) });
    } catch (e) {
        console.warn('[cataanbot] reset failed:', e);
    }
});

contentEl.addEventListener('click', (ev) => {
    const t = ev.target;
    if (!(t instanceof Element)) return;
    if (!t.classList.contains('fb')) return;
    const recAttr = t.getAttribute('data-rec');
    if (!recAttr) return;
    let recDict;
    try {
        recDict = JSON.parse(recAttr);
    } catch (err) {
        return;
    }
    const label = t.classList.contains('fb-up') ? 'good' : 'bad';
    const hint = (latestSnap && {
        seq: latestSnap.seq,
        self_vp: (latestSnap.self || {}).vp,
        self_cards: (latestSnap.self || {}).cards,
        round: latestSnap.round,
        phase: latestSnap.phase,
    }) || {};
    fetch(BRIDGE_FEEDBACK_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label, rec: recDict, snapshot_hint: hint }),
    }).catch(() => {});
    t.classList.add('fb-marked');
    const sib = t.classList.contains('fb-up')
        ? t.parentElement.querySelector('.fb-down')
        : t.parentElement.querySelector('.fb-up');
    if (sib) sib.classList.remove('fb-marked');
});

function renderBanner(parts, kind, header, message, opts = {}) {
    if (!message) return;
    const cls = ['banner', kind];
    if (opts.level) cls.push(opts.level);
    if (opts.ready) cls.push('ready');
    parts.push(`<div class="${cls.join(' ')}">`);
    parts.push(`<div class="banner-h">${escapeHtml(header)}</div>`);
    parts.push(`<div class="banner-msg">${escapeHtml(message)}</div>`);
    if (opts.subline) {
        parts.push(`<div class="plan-progress">${opts.subline}</div>`);
    }
    parts.push(`</div>`);
}

function renderRecCard(parts, r, isTop) {
    const kindLabels = {
        settlement: 'settle', city: 'city', road: 'road',
        dev_card: 'dev card', bank_trade: 'port/bank',
        propose_trade: 'propose', opening_settlement: 'settle',
    };
    const label = kindLabels[r.kind] || r.kind;
    const score = Number(r.score || 0);
    const scoreCls = score >= 8 ? 'strong'
        : (score >= 5 ? 'decent' : 'weak');
    const fbPayload = JSON.stringify({
        kind: r.kind, when: r.when, score: r.score,
        detail: r.detail, give: r.give, get: r.get,
        unlocks: r.unlocks, node_id: r.node_id, edge: r.edge,
    });
    const fbHtml =
        `<span class="fb-row">`
        + `<button class="fb fb-up" data-rec='${escapeHtml(fbPayload)}' `
        + `title="rec was helpful">👍</button>`
        + `<button class="fb fb-down" data-rec='${escapeHtml(fbPayload)}' `
        + `title="rec was bad">👎</button>`
        + `</span>`;
    parts.push(
        `<div class="rec${isTop ? ' top' : ''}">`
        + `<span class="score ${scoreCls}">${score.toFixed(1)}</span>`
        + ` <span class="kind">${escapeHtml(label)}</span>`
        + ` <span class="detail">${escapeHtml(r.detail || '')}</span>`
        + fbHtml
        + `</div>`
    );
}

function renderSelf(parts, snap) {
    const self = snap.self || {};
    if (!self.username && !self.color) return;
    const handBits = [];
    for (const r of ['WOOD', 'BRICK', 'SHEEP', 'WHEAT', 'ORE']) {
        const n = (self.hand || {})[r] || 0;
        if (n > 0) handBits.push(`${n}${glyphFor(r)}`);
    }
    parts.push('<div class="self">');
    parts.push('<div class="self-h">');
    parts.push(`<span class="self-name">${escapeHtml(self.username || self.color || 'self')}</span>`);
    if (self.vp != null) {
        parts.push(`<span class="vp">${self.vp} VP</span>`);
    }
    if (self.cards != null) {
        parts.push(`<span class="cards">${self.cards} cards</span>`);
    }
    parts.push('</div>');
    if (handBits.length) {
        parts.push(`<div class="hand">${handBits.join('  ')}</div>`);
    }
    if (self.production && self.production.per_roll > 0) {
        parts.push(
            `<div class="prod">${self.production.per_roll.toFixed(2)} cards/roll`
            + (self.production.top_resource
                ? ` · top ${glyphFor(self.production.top_resource)}` : '')
            + `</div>`);
    }
    parts.push('</div>');
}

function renderOpps(parts, snap) {
    const opps = snap.opps || [];
    if (!opps.length) return;
    parts.push('<div class="opps">');
    for (const o of opps) {
        const bg = o.color_css || '#888';
        const initial = (o.color || '?').slice(0, 1);
        const nameSpan = `<span class="opp-name">`
            + `${escapeHtml(o.username || o.color || 'opp')}</span>`;
        const vpSpan = o.vp != null
            ? `<span class="opp-vp">${o.vp}VP</span>` : '';
        const cardsSpan = o.cards != null
            ? `<span class="opp-cards">${o.cards} cards</span>` : '';
        const uncertain = (o.unknown || 0) >= 2
            ? `<span class="hand-uncertain" `
              + `title="${o.unknown} unknown cards — `
              + `inferred hand may be wrong">~${o.unknown}?</span>`
            : '';
        parts.push(
            `<div class="opp">`
            + `<span class="color-pill" style="background:${bg};color:#000">`
            + `${initial}</span>`
            + ` ${nameSpan} ${vpSpan} ${uncertain} ${cardsSpan}`
            + `</div>`
        );
    }
    parts.push('</div>');
}

function renderRecs(parts, snap) {
    const recs = snap.recommendations || [];
    if (!recs.length) return;
    parts.push('<div class="recs">');
    parts.push(`<div class="recs-h">recommendations</div>`);
    for (let i = 0; i < recs.length; i++) {
        renderRecCard(parts, recs[i], i === 0);
    }
    parts.push('</div>');
}

function renderBanners(parts, snap) {
    // Plan first — it's the through-line.
    if (snap.plan && snap.plan.summary) {
        const p = snap.plan;
        const progressBits = [];
        for (const [res, prog] of Object.entries(p.progress || {})) {
            const have = prog.have || 0;
            const need = prog.need || 0;
            const cls = have >= need ? 'pos' : 'fg-mute';
            progressBits.push(
                `<span class="${cls}">${have}/${have + need} `
                + `${glyphFor(res)}</span>`);
        }
        renderBanner(parts, 'plan', 'PLAN', p.summary, {
            ready: p.ready,
            subline: progressBits.join(' · '),
        });
    }
    // Win proximity — self is closing.
    if (snap.win_proximity && snap.win_proximity.message) {
        renderBanner(parts, 'win-prox', 'CLOSING', snap.win_proximity.message);
    }
    // Threat — opp is closing.
    if (snap.threat && snap.threat.message) {
        renderBanner(parts, 'threat', 'THREAT', snap.threat.message);
    }
    // Pre-roll spend-down warning.
    if (snap.seven_prep && !snap.discard_hint) {
        const sp = snap.seven_prep;
        const dropText = Object.entries(sp.would_drop || {})
            .map(([r, n]) => `${n}${glyphFor(r)}`).join(' · ');
        renderBanner(parts, 'seven-prep', '⚠ SPEND DOWN',
            sp.message, {
                level: sp.level,
                subline: `would lose: ${escapeHtml(dropText)}`,
            });
    }
    // Reactive discard hint (after a 7).
    if (snap.discard_hint && snap.discard_hint.need > 0) {
        const dh = snap.discard_hint;
        const dropText = Object.entries(dh.drop || {})
            .map(([r, n]) => `${n}${glyphFor(r)}`).join(' · ');
        renderBanner(parts, 'discard', '🎲 DISCARD',
            `discard ${dh.need} cards · ${dropText}`,
            { subline: dh.rationale ? `reason: ${escapeHtml(dh.rationale)}` : '' });
    }
}

function renderSnap(snap) {
    if (!snap) return;
    if (snap.variant_label) {
        variantBadge.textContent = snap.variant_label;
        variantBadge.classList.add('show');
    } else {
        variantBadge.classList.remove('show');
    }
    if (snap.friendly_robber_active) {
        friendlyBadge.classList.add('show');
    } else {
        friendlyBadge.classList.remove('show');
    }
    if (!snap.game_started) {
        contentEl.innerHTML =
            '<div class="muted" style="text-align:center;'
            + 'padding:32px 12px;font-size:13px">'
            + 'waiting for game start…<br>'
            + '<span style="color:var(--fg-dim)">'
            + 'open a colonist.io game in another tab</span></div>';
        return;
    }
    const parts = [];
    renderBanners(parts, snap);
    renderSelf(parts, snap);
    renderRecs(parts, snap);
    renderOpps(parts, snap);
    contentEl.innerHTML = parts.join('');
}

async function tick() {
    try {
        const resp = await fetch(BRIDGE_ADVISOR_URL, { cache: 'no-store' });
        if (!resp.ok) {
            panel.dataset.phase = 'connecting';
            statusEl.textContent =
                `bridge responded with ${resp.status} — start the bridge `
                + `(./bin/cataanbot bridge)`;
            return;
        }
        const snap = await resp.json();
        if (!snap) return;
        if (snap.seq !== lastSeq) {
            lastSeq = snap.seq;
            latestSnap = snap;
            renderSnap(snap);
        }
        panel.dataset.phase = 'connected';
    } catch (err) {
        panel.dataset.phase = 'connecting';
        statusEl.innerHTML =
            'no connection to bridge.<br>'
            + '<span style="color:var(--fg-dim);font-size:11px">'
            + 'start it with <span class="mono">./bin/cataanbot bridge</span>'
            + ' from the project directory</span>';
    }
}

tick();
setInterval(tick, POLL_MS);
