// Service worker. Two jobs:
//
//  1. Side-panel toggle from the toolbar action. Chrome 116+ lets us
//     open the panel directly from the user-gesture click; we just
//     enable that behavior on install.
//  2. Bridge HTTP forwarder. Content scripts hand us {type: 'ws-frame'}
//     and {type: 'log-entry'} messages; we POST them to the local
//     bridge. The reason this isn't done directly from content.js is
//     that some bridge errors / network probing benefits from running
//     under the service worker's looser CORS rules + persistent host
//     permissions.

const BRIDGE_BASE = 'http://127.0.0.1:8765';

// Frame replay cache. The standalone path needs the most-recent
// GameStart frame (the one carrying mapState) to build the JS board,
// and the most-recent full-state frame (anything with playerStates
// or mechanic*State) to populate hands / buildings. When the side
// panel mounts AFTER those frames already passed (user opened the
// panel mid-game, reloaded the panel during a game, etc.) the panel
// asks us to replay them via 'request-replay'. We keep:
//
//   lastGameStartFrame — first frame after a (re)connect that ships
//     a tileHexStates payload. Replaced on each new GameStart.
//   lastStateFrame     — most recent frame carrying playerStates
//     so opening progress / hands can be reconstructed without
//     waiting for the next state delta.
let lastGameStartFrame = null;
let lastStateFrame = null;

// MV3 service workers get terminated after ~30s idle, which would
// drop the in-memory cache exactly when the side panel mounts and
// asks for a replay. Mirror to chrome.storage.session so the cache
// survives a service-worker restart within the browser session.
// chrome.storage.session is in-memory only (cleared when the
// browser closes) so it's the right tier — no on-disk write churn
// for every WS frame. Reads/writes are async; we await the read
// in the replay handler but fire-and-forget the writes.
function _persistFrame(slot, frame) {
    try {
        chrome.storage.session.set({ [slot]: frame }).catch(() => {});
    } catch (_) { /* storage API unavailable */ }
}
async function _loadCachedFrames() {
    try {
        const got = await chrome.storage.session.get([
            'lastGameStartFrame', 'lastStateFrame']);
        if (got.lastGameStartFrame && !lastGameStartFrame) {
            lastGameStartFrame = got.lastGameStartFrame;
        }
        if (got.lastStateFrame && !lastStateFrame) {
            lastStateFrame = got.lastStateFrame;
        }
    } catch (_) {}
}
// Hydrate on every service worker boot. chrome.storage.session
// loads asynchronously; the request-replay handler also calls
// this so a panel mount that hits a cold service worker still
// gets the cached frames replayed once they arrive.
_loadCachedFrames();
// Base64 boundary-aligned variants of the key strings. msgpack
// embeds string keys as raw ascii at unpredictable byte offsets, so
// we check all three b64 phasings and accept any match.
function _b64Variants(s) {
    // Wrap s with 0..2 leading bytes so the b64 alignment shifts.
    // Then take the middle slice that's guaranteed to be in the
    // encoded version of `s` regardless of the alignment.
    const out = [];
    for (let pad = 0; pad < 3; pad += 1) {
        const padded = '\x00'.repeat(pad) + s + '\x00'.repeat((3 - (pad + s.length) % 3) % 3);
        // base64 manually
        let bin = '';
        for (let i = 0; i < padded.length; i += 1) {
            bin += padded.charCodeAt(i).toString(2).padStart(8, '0');
        }
        let b64 = '';
        const CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
        for (let i = 0; i < bin.length; i += 6) {
            b64 += CHARS[parseInt(bin.slice(i, i + 6).padEnd(6, '0'), 2)];
        }
        // Skip the padded-byte chunks at start/end so we get a
        // substring that's stable across this alignment.
        const startSkip = Math.ceil((pad * 8) / 6);
        const endSkip = pad === 0 ? 0 : Math.ceil(((3 - (pad + s.length) % 3) % 3 * 8) / 6);
        const slice = b64.slice(startSkip, b64.length - endSkip);
        if (slice.length >= 8) out.push(slice);
    }
    return out;
}
const _GAMESTART_B64S = _b64Variants('tileHexStates');
const _PLAYERSTATES_B64S = _b64Variants('playerStates');
// GameStart frames carry the full mapState (hex states, corner
// states, edge states, port states) and run ~6-10kB of base64.
// Mid-game incremental deltas are typically <1kB and may include
// the literal "tileHexStates" key with an empty dict ("no tile
// changes this delta"). Match on key bytes AND minimum size so
// we only ever cache the real GameStart, not deltas that mention
// the key.
const GAMESTART_MIN_B64_BYTES = 3072;
function _frameLooksLikeGameStart(frame) {
    if (!frame || frame.kind !== 'arraybuffer' || !frame.b64) return false;
    // Hard size floor — a real GameStart with mapState + cornerStates
    // + edgeStates + portStates is always ≥ 3kB. Anything smaller
    // can't carry the full payload no matter what keys it mentions.
    if (frame.b64.length < GAMESTART_MIN_B64_BYTES) return false;
    // Either the literal key bytes match (reliable) OR the frame is
    // big enough that GameStart is the only plausible source. Some
    // captures show a single occasional mid-game resync frame ~6kB
    // that does carry mapState — we cache those too as a safety net.
    return _GAMESTART_B64S.some(v => frame.b64.includes(v))
        || frame.b64.length >= 5000;
}
function _frameLooksLikeState(frame) {
    if (!frame || frame.kind !== 'arraybuffer' || !frame.b64) return false;
    return _PLAYERSTATES_B64S.some(v => frame.b64.includes(v))
        // State frames also tend to be >500 bytes; any frame
        // mid-game large enough to carry playerStates is worth
        // caching even if the sniff misses.
        || frame.b64.length >= 768;
}

chrome.runtime.onInstalled.addListener(() => {
    // The in-page HUD is the primary surface now, so the toolbar icon no
    // longer opens the side panel — it opens the in-page settings menu
    // instead (see onClicked below). Explicitly turn the open-on-click
    // behavior OFF so the action click fires our onClicked handler.
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false })
        .catch(err => console.warn('[catanbot] sidePanel setup:', err));
});

// Toolbar icon click -> open the in-page settings menu on the colonist tab,
// rather than the old side panel. loghud.js listens for 'open-settings'. If
// the active tab isn't colonist, focus an existing colonist tab if there is
// one so the click still lands somewhere useful.
chrome.action.onClicked.addListener(async (tab) => {
    try {
        if (tab && isColonistUrl(tab.url)) {
            chrome.tabs.sendMessage(tab.id, { type: 'open-settings' })
                .catch(() => {});
            return;
        }
        const tabs = await chrome.tabs.query({ url: 'https://colonist.io/*' });
        if (tabs && tabs.length) {
            await chrome.tabs.update(tabs[0].id, { active: true });
            chrome.tabs.sendMessage(tabs[0].id, { type: 'open-settings' })
                .catch(() => {});
        }
    } catch (e) { /* no colonist tab open; nothing to do */ }
});

// Toolbar badge — shows "ON" in green when the active tab is on
// colonist.io so Noah can spot whether the extension is wired up
// before clicking Start Game. Chrome doesn't allow programmatic
// auto-opening of the side panel (user gesture required), so the
// badge is the best we can do as a visual reminder. WS frames ARE
// captured even with the panel closed (the inject.js content script
// patches WebSocket at document_start in the page world), but the
// HUD only renders once the panel is open.
function setColonistBadge(tabId, isColonist) {
    try {
        if (isColonist) {
            chrome.action.setBadgeText({ tabId, text: 'ON' });
            chrome.action.setBadgeBackgroundColor({
                tabId, color: '#16a34a',  // green
            });
            chrome.action.setTitle({
                tabId,
                title: 'CatanBot active on this tab — click to open panel',
            });
        } else {
            chrome.action.setBadgeText({ tabId, text: '' });
            chrome.action.setTitle({
                tabId,
                title: 'CatanBot — open colonist.io to use',
            });
        }
    } catch (e) {
        // Tab may have been closed mid-update; safe to ignore.
    }
}

function isColonistUrl(url) {
    return typeof url === 'string' && url.startsWith('https://colonist.io');
}

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
    try {
        const tab = await chrome.tabs.get(tabId);
        setColonistBadge(tabId, isColonistUrl(tab.url));
    } catch (_) { /* tab gone */ }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (changeInfo.url || changeInfo.status === 'complete') {
        setColonistBadge(tabId, isColonistUrl(tab.url));
    }
});

async function postJson(url, payload) {
    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        return resp.ok;
    } catch (e) {
        // Bridge isn't running. Quiet; the panel will surface the
        // not-connected state on its own poll.
        return false;
    }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || typeof msg !== 'object') return false;
    if (msg.type === 'ws-frame') {
        postJson(`${BRIDGE_BASE}/ws`, msg.frame);
        // Cache for the replay path so a panel that opens mid-game
        // can recover the GameStart + most-recent state without
        // waiting for the next colonist delta. Mirrored to
        // chrome.storage.session so the cache survives a service
        // worker restart (MV3 kills idle SWs after ~30s).
        if (_frameLooksLikeGameStart(msg.frame)) {
            lastGameStartFrame = msg.frame;
            _persistFrame('lastGameStartFrame', msg.frame);
        }
        if (_frameLooksLikeState(msg.frame)) {
            lastStateFrame = msg.frame;
            _persistFrame('lastStateFrame', msg.frame);
        }
        // Broadcast the frame to all extension contexts (the side
        // panel listens for these). The panel uses them as its data
        // source when the local bridge is unreachable — standalone
        // mode runs the JS recommender directly off the same colonist
        // WS frames the bridge would have consumed. When the bridge
        // IS up, the panel ignores the broadcast and uses /advisor.
        try {
            chrome.runtime.sendMessage({
                type: 'ws-frame-broadcast', frame: msg.frame,
            }).catch(() => {});
        } catch (_) { /* no listeners; ignore */ }
        // Don't await — service worker shouldn't hold the message
        // channel open just so a fire-and-forget POST can resolve.
        return false;
    }
    if (msg.type === 'request-replay') {
        // Side panel just mounted. Replay the cached GameStart +
        // last state frame so the standalone path can recover its
        // state without waiting for the next colonist delta. Both
        // frames go through the same broadcast channel a live
        // frame would, so the panel listener handles them
        // identically. Pulls from chrome.storage.session as a
        // fallback when the in-memory cache is empty (cold service
        // worker after MV3 idle-kill).
        (async () => {
            if (!lastGameStartFrame || !lastStateFrame) {
                await _loadCachedFrames();
            }
            try {
                if (lastGameStartFrame) {
                    chrome.runtime.sendMessage({
                        type: 'ws-frame-broadcast',
                        frame: lastGameStartFrame,
                        replay: true,
                    }).catch(() => {});
                }
                if (lastStateFrame
                        && lastStateFrame !== lastGameStartFrame) {
                    chrome.runtime.sendMessage({
                        type: 'ws-frame-broadcast',
                        frame: lastStateFrame,
                        replay: true,
                    }).catch(() => {});
                }
            } catch (_) {}
            if (sendResponse) sendResponse({
                ok: true,
                had_game_start: !!lastGameStartFrame,
                had_state: !!lastStateFrame,
            });
        })();
        return true;  // keep the message channel open for the async response
    }
    if (msg.type === 'reset-replay-cache') {
        lastGameStartFrame = null;
        lastStateFrame = null;
        try {
            chrome.storage.session.remove([
                'lastGameStartFrame', 'lastStateFrame']).catch(() => {});
        } catch (_) {}
        if (sendResponse) sendResponse({ ok: true });
        return true;
    }
    if (msg.type === 'log-entry') {
        postJson(`${BRIDGE_BASE}/log`, msg.payload);
        // Mirror to extension contexts so the side panel's
        // standalone path can extract usernames + colors from
        // chat entries (colonist's WS frames don't carry
        // usernames; they only come through the chat DOM).
        try {
            chrome.runtime.sendMessage({
                type: 'log-entry-broadcast',
                payload: msg.payload,
            }).catch(() => {});
        } catch (_) { /* no listeners */ }
        return false;
    }
    if (msg.type === 'feedback') {
        postJson(`${BRIDGE_BASE}/feedback`, msg.payload);
        return false;
    }
    if (msg.type === 'get-advisor') {
        // The in-page HUD (loghud.js) can't fetch the bridge directly: a
        // content script's http://127.0.0.1 request from the https colonist
        // page is blocked in some browsers (Comet: ERR_BLOCKED_BY_CLIENT).
        // The service worker runs in the extension context with the
        // 127.0.0.1 host permission, so it fetches and hands back the snap.
        (async () => {
            try {
                const resp = await fetch(`${BRIDGE_BASE}/advisor`,
                    { method: 'GET' });
                if (!resp.ok) {
                    sendResponse({ ok: false, status: resp.status });
                    return;
                }
                sendResponse({ ok: true, snap: await resp.json() });
            } catch (e) {
                sendResponse({ ok: false, error: String(e) });
            }
        })();
        return true;  // keep the channel open for the async response
    }
    if (msg.type === 'streamer-anon') {
        // Sync content.js's username→fantasy-name map to the bridge so
        // the side panel can read the same labels and stop diverging
        // (panel previously had its own counter — see 2026-05-04
        // Elin/Dara/Fynn vs chat's Aria/Bran/Cyrus regression).
        postJson(`${BRIDGE_BASE}/streamer-anon`, msg.payload);
        return false;
    }
    if (msg.type === 'open-postmortem') {
        // Auto-pop the postmortem when a game ends. Place the new
        // tab immediately to the right of the colonist tab (rather
        // than the very end of the strip) and don't steal focus —
        // Noah can flip to it when he's done with whatever's open.
        // Background instead of foreground because postmortems often
        // land in the middle of multi-game sessions where stealing
        // focus would interrupt a fresh GameStart.
        (async () => {
            let openerIndex = undefined;
            try {
                const tabs = await chrome.tabs.query({
                    url: 'https://colonist.io/*',
                });
                if (tabs && tabs.length) {
                    // Most recently active colonist tab wins.
                    tabs.sort((a, b) =>
                        (b.lastAccessed || 0) - (a.lastAccessed || 0));
                    openerIndex = (tabs[0].index ?? 0) + 1;
                }
            } catch (_) { /* fall through with undefined index */ }
            try {
                await chrome.tabs.create({
                    url: `${BRIDGE_BASE}/postmortem`,
                    active: false,
                    index: openerIndex,
                });
            } catch (e) {
                console.warn('[catanbot] open-postmortem failed:', e);
            }
        })();
        return false;
    }
    if (msg.type === 'reset-bridge') {
        postJson(`${BRIDGE_BASE}/reset`, {})
            .then(ok => sendResponse({ ok }));
        return true;  // keep channel open for async response
    }
    if (msg.type === 'set-config') {
        // In-page settings menu (loghud.js) writes VP target / discard limit
        // to the bridge's /config. Same worker-proxy reason as get-advisor:
        // the content script can't POST 127.0.0.1 from the https page.
        postJson(`${BRIDGE_BASE}/config`, msg.payload || {})
            .then(ok => sendResponse({ ok }));
        return true;  // keep channel open for async response
    }
    return false;
});
