# Launch readiness (v0.42.0)

Last audit: 2026-06-02, by a 4-dimension adversarial pass (privacy and
security, no-cheat invariant, correctness and parity, release hygiene).

## Go / no-go: GO for an unlisted launch

The two invariants that matter most are clean, and every launch-blocking
finding has been fixed. Remaining work is asset creation and the
per-OS installer build, all of which need your machines and are listed
at the bottom.

## Audit results

| Dimension | Verdict | Notes |
|---|---|---|
| No-cheat / public-information-only | PASS | Both engines use only public info: opponent hands are inferred from the visible log plus WS totals, opponent dev-card types stay masked until played, the live recommender never peeks the deck. The one deck-order read is in the offline `repl.py`, never wired to the advisor. |
| Correctness / parity | PASS | 741 pytest pass (2 skip), 27 JS tests pass, `node --check` clean. Standalone matches the bridge on the Phase 1 surfaces with no merge regressions. |
| Privacy / security | FIXED | Two majors fixed (below). |
| Release hygiene | FIXED | Em-dash sweep completed on the JS lib and listing copy; version sync, CHANGELOG, and no-co-author-trailer all clean. |

## Fixed in this pass (launch-hardening)

- **Removed the Google Fonts fetch.** `panel.css` `@import` and the
  pop-out `panel.js` font link loaded webfonts from
  `fonts.googleapis.com`, a third network destination that broke the
  "only colonist.io and 127.0.0.1" privacy claim. The panel now uses the
  system font stack already declared in `--font` / `--font-mono` (local
  Inter / JetBrains Mono still resolve if installed). The claim is now
  literally true.
- **Restricted the bridge CORS.** `allow_origins=["*"]` let any site the
  user visits read `/advisor` cross-origin while the bridge ran. Now
  scoped to `https://colonist.io` plus `chrome-extension://.*` (the only
  legit callers).
- **Em-dash sweep finished.** The dot-separator pass had missed
  `extension/lib/strategy.js` (9 strings) and `extension/lib/hints.js`
  (4 strings), which render on the no-bridge HUD, plus the panel.html
  friendly-robber tooltip and the store-listing copy. All now use the
  dot separator.
- **Tightened a few surfaces** the audit flagged as looser than needed:
  `web_accessible_resources` scoped from `<all_urls>` to colonist.io,
  the page-world `postMessage` target origin scoped from `*` to the page
  origin, and the panel startup diagnostic downgraded from `console.log`
  to `console.debug`.

## Open, non-blocking notes

- The dead `GM_xmlhttpRequest` branches (gated behind `if (false)`) could
  be deleted to shrink the review surface. Cosmetic.
- `repl.py`'s `deck[0]` fallback is offline-only and not an invariant
  violation; could be made order-independent for clarity.
- The restricted CORS should be smoke-tested in a real browser (the
  extension talks to the bridge from a `chrome-extension://` origin,
  which the new policy allows): see the no-bridge / bridge checks in
  `SMOKE_TEST.md`.

## Batched human steps (your machines + accounts)

1. Walk `docs/SMOKE_TEST.md` against a live game (bridge on and off),
   including the variant and trade checks.
2. Capture the 5 Chrome Web Store screenshots (opening picks, mid-game
   strategy banner, robber + dev hints, trade accept, auto-postmortem).
3. Build the Windows `.exe` and Linux `.AppImage` bridge bundles
   (`bin/build-bridge-bundle.sh` on each OS; macOS is verified), wrap and
   sign them (`SIGNING.md`), and publish as release assets, then flip on
   the extension download buttons (`docs/BRIDGE_INSTALLER.md`).
4. Upload `dist/catanbot-extension-v0.42.0.zip` plus the promo tile and
   screenshots in the CWS dev console; submit unlisted.
5. Flip the listing public once the installer assets are live, then post
   the announcement (`docs/LAUNCH_POST.md`).
