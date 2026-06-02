# Bridge installer (one-click, no Python)

The build recipe for Option E in `BRIDGE_DISTRIBUTION.md`: bundle the
Python bridge into a single self-contained binary per OS, wrap it in a
platform installer, publish the installers as GitHub release assets, and
point the extension's "bridge not detected" card at them. The extension
already works standalone (experimental); this makes the optional bridge
a one-download install instead of `git clone` plus a venv.

Steps 1 and 2 are scripted and step 1 is verified on macOS. Steps 3 to 5
need your machines, certificates, and a GitHub release, so they are the
batched-for-the-end work.

## 1. Build the bundle (per OS, scripted)

PyInstaller does not cross-compile, so run this on each OS you ship for:

```bash
pip install -e '.[bridge]' pyinstaller
bash bin/build-bridge-bundle.sh
# -> dist/catanbot-bridge      (macOS / Linux)
# -> dist/catanbot-bridge.exe  (Windows)
```

Smoke test:

```bash
./dist/catanbot-bridge &
curl -s http://127.0.0.1:8765/advisor | head -c 80   # {"seq":0,"game_started":false,...
```

Status: verified on macOS (arm64), a 23 MB one-file binary that serves
`/advisor` with HTTP 200. catanatron is pure Python, so there are no C
extensions to fight; uvicorn's runtime-selected loop/HTTP/WebSocket
modules are declared as hidden imports in the build script, and
PyInstaller's bundled matplotlib hook covers the postmortem renderer.
The binary writes sessions and postmortems under a per-user data dir
(see `bin/bridge_entry.py`), so it never needs write access to its own
location.

## 2. Wrap in a platform installer

### macOS
Sign and notarize the binary (see `SIGNING.md`), then either:
- Drop it at `/usr/local/bin/catanbot-bridge` and install the
  `bin/com.catanbot.bridge.plist` LaunchAgent so it starts on login, or
- Build a `.pkg` with `pkgbuild` that installs the binary plus the
  LaunchAgent and runs `launchctl load` in a postinstall script.

### Windows
Wrap `catanbot-bridge.exe` with Inno Setup or NSIS: install to
`%LOCALAPPDATA%\CatanBot\`, add a Startup-folder shortcut so it launches
on login, and sign the installer (see `SIGNING.md`).

### Linux
Package as an `.AppImage` (or `.deb`). Optionally ship a `systemd --user`
unit for auto-start. No signing required.

## 3. Publish as release assets

Attach the installers to a GitHub release (the `vX.Y.Z` release is fine)
with stable, OS-predictable names so the extension can deep-link to the
latest:

- `CatanBot-bridge-macos.pkg`
- `CatanBot-bridge-windows.exe`
- `CatanBot-bridge-linux.AppImage`

They resolve at
`https://github.com/NoahLaforet/CatanBot/releases/latest/download/<asset>`.

## 4. Wire the extension download buttons

Once the assets are published, extend the `_bridge_unreachable` card in
`extension/panel.js` (the "bridge unreachable" block) with per-OS
download buttons and a connection re-test. Sketch:

```js
const REL = 'https://github.com/NoahLaforet/CatanBot/releases/latest/download';
// inside the bd-actions div:
+ `<a href="${REL}/CatanBot-bridge-macos.pkg">macOS</a>`
+ `<a href="${REL}/CatanBot-bridge-windows.exe">Windows</a>`
+ `<a href="${REL}/CatanBot-bridge-linux.AppImage">Linux</a>`
+ `<button id="bd-retest">test connection</button>`
```

Wire `#bd-retest` to re-probe `127.0.0.1:8765/advisor` and re-render.
Keep the existing install-docs link as the manual fallback. (Held until
the assets exist so the buttons never 404.)

## 5. Flip the listing public

With a frictionless install live, switch the Chrome Web Store listing
from unlisted to public (see the visibility step in
`STORE_LISTING.md`).
