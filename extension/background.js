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
        .catch(err => console.warn('[cataanbot] sidePanel setup:', err));
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
        // Don't await — service worker shouldn't hold the message
        // channel open just so a fire-and-forget POST can resolve.
        return false;
    }
    if (msg.type === 'log-entry') {
        postJson(`${BRIDGE_BASE}/log`, msg.payload);
        return false;
    }
    if (msg.type === 'feedback') {
        postJson(`${BRIDGE_BASE}/feedback`, msg.payload);
        return false;
    }
    if (msg.type === 'reset-bridge') {
        postJson(`${BRIDGE_BASE}/reset`, {})
            .then(ok => sendResponse({ ok }));
        return true;  // keep channel open for async response
    }
    return false;
});
