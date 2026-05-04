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

chrome.runtime.onInstalled.addListener(() => {
    // Open the side panel on action-icon click. Chrome's side-panel
    // API requires this opt-in; without it the icon does nothing.
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true })
        .catch(err => console.warn('[catanbot] sidePanel setup:', err));
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
        // Also broadcast the frame to all extension contexts (the
        // side panel listens for these). The panel uses them as
        // its data source when the local bridge is unreachable —
        // standalone mode runs the JS recommender directly off
        // the same colonist WS frames the bridge would have
        // consumed. When the bridge IS up, the panel ignores
        // the broadcast and uses /advisor as before.
        try {
            chrome.runtime.sendMessage({
                type: 'ws-frame-broadcast', frame: msg.frame,
            }).catch(() => {});
        } catch (_) { /* no listeners; ignore */ }
        // Don't await — service worker shouldn't hold the message
        // channel open just so a fire-and-forget POST can resolve.
        return false;
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
    return false;
});
