#!/usr/bin/env bash
# Build the self-contained CatanBot menu-bar .app (PyInstaller).
#
# Unlike bin/build-app.sh (a thin wrapper that execs the repo's
# bin/catanbot-tray and therefore needs the repo + venv on disk), this
# produces a fully self-contained .app that bundles Python + the bridge,
# so it can be dragged to /Applications on any Mac and launched with no
# setup. It is also what the Chrome extension hands out for download.
#
#   PYTHON=.venv/bin/python ./bin/build-app-bundle.sh
#
# Output: dist/CatanBot.app
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "build-app-bundle: the .app is macOS only." >&2
    exit 1
fi

PY="${PYTHON:-.venv/bin/python}"
if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
    echo "build-app-bundle: PyInstaller not installed in $PY." >&2
    echo "  $PY -m pip install -e '.[bridge,tray]' pyinstaller" >&2
    exit 1
fi

# --- .icns from the brand art (same recipe as bin/build-app.sh).
SRC_ICON="$REPO_ROOT/extension/icons/icon-128.png"
[ -f "$SRC_ICON" ] || { echo "build-app-bundle: missing $SRC_ICON" >&2; exit 1; }
mkdir -p build
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
ICONSET="$WORK/CatanBot.iconset"; BIG="$WORK/icon-1024.png"
mkdir -p "$ICONSET"
sips -z 1024 1024 "$SRC_ICON" --out "$BIG" >/dev/null
for s in 16 32 64 128 256 512; do
    sips -z "$s" "$s" "$BIG" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    d=$((s * 2))
    sips -z "$d" "$d" "$BIG" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o build/CatanBot.icns

# --- freeze.
"$PY" -m PyInstaller --noconfirm --clean catanbot-tray.spec

APP="dist/CatanBot.app"
[ -d "$APP" ] || { echo "build-app-bundle: PyInstaller did not produce $APP" >&2; exit 1; }

# --- ad-hoc codesign. Strip xattrs first (iCloud / FinderInfo metadata
#     makes codesign fail), then deep-sign the whole bundle so the
#     embedded Mach-O binaries validate when Finder launches it.
xattr -cr "$APP" 2>/dev/null || true
if codesign --force --deep --sign - "$APP" >/dev/null 2>&1; then
    echo "ad-hoc signed"
else
    echo "warning: codesign failed; approve in System Settings > Privacy & Security" >&2
fi

# --- package the download artifact. ditto (not zip) preserves the bundle
#     layout + code signature; this CatanBot-macos.zip is exactly what the
#     Chrome extension's Download button points at on the GitHub release.
#     For a PUBLIC release, notarize the .app first so it opens by
#     double-click without a Gatekeeper block: scripts/sign_and_notarize.sh
#     (which re-runs this ditto step after stapling).
ZIP="dist/CatanBot-macos.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

echo
echo "built  $APP"
echo "zipped $ZIP ($(du -h "$ZIP" | cut -f1))"
echo "test   open \"$REPO_ROOT/$APP\"   # menu-bar item + bridge auto-starts on :8765"
echo "ship   scripts/sign_and_notarize.sh   # Developer ID sign + notarize, then:"
echo "       gh release upload <tag> $ZIP --clobber -R NoahLaforet/CatanBot"
