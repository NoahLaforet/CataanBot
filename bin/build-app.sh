#!/usr/bin/env bash
# Build a double-clickable macOS .app that launches the CatanBot menu-bar
# tray. Generates a .icns from the brand art (extension/icons/icon-128.png)
# and a thin .app wrapper whose executable execs bin/catanbot-tray, so it
# reuses the repo venv + bridge (no frozen Python). Installs to
# ~/Applications/CatanBot.app by default; pass a dir to override.
#
#   ./bin/build-app.sh            # -> ~/Applications/CatanBot.app
#   open ~/Applications/CatanBot.app   # or double-click in Finder
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${1:-$HOME/Applications}"
APP="$APP_DIR/CatanBot.app"
C="$APP/Contents"
SRC_ICON="$REPO_ROOT/extension/icons/icon-128.png"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "build-app: the .app is macOS only." >&2
    exit 1
fi
command -v sips >/dev/null && command -v iconutil >/dev/null || {
    echo "build-app: needs sips + iconutil (macOS built-ins)." >&2
    exit 1
}
[ -f "$SRC_ICON" ] || { echo "build-app: missing $SRC_ICON" >&2; exit 1; }

# --- .icns from the brand art: upscale once to 1024, derive every slot.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ICONSET="$WORK/CatanBot.iconset"
BIG="$WORK/icon-1024.png"
mkdir -p "$ICONSET"
sips -z 1024 1024 "$SRC_ICON" --out "$BIG" >/dev/null
for s in 16 32 64 128 256 512; do
    sips -z "$s" "$s" "$BIG" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    d=$((s * 2))
    sips -z "$d" "$d" "$BIG" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done

rm -rf "$APP"
mkdir -p "$C/MacOS" "$C/Resources"
iconutil -c icns "$ICONSET" -o "$C/Resources/CatanBot.icns"

# --- thin launcher: exec the repo-local tray launcher (absolute path so a
#     Finder double-click resolves the repo regardless of working dir).
cat > "$C/MacOS/CatanBot" <<EOF
#!/usr/bin/env bash
# Finder launches with a minimal PATH (no Homebrew, no shell profile).
# Add the common interpreter locations so bin/catanbot-tray can find
# python3.11+ if it needs to bootstrap the venv on first run.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:\$PATH"
exec "$REPO_ROOT/bin/catanbot-tray"
EOF
chmod +x "$C/MacOS/CatanBot"

# --- Info.plist. LSUIElement=1 keeps it a menu-bar agent (no Dock icon
#     while running), but the .app still shows + double-clicks in Finder.
cat > "$C/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>CatanBot</string>
  <key>CFBundleDisplayName</key><string>CatanBot</string>
  <key>CFBundleIdentifier</key><string>io.colonist.catanbot.tray</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>CatanBot</string>
  <key>CFBundleIconFile</key><string>CatanBot</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
EOF

# Ad-hoc codesign so Gatekeeper on modern macOS will launch a locally
# built app. Strip xattrs first: iCloud / FinderInfo metadata makes
# codesign fail. An unsigned bundle is rejected outright ("no usable
# signature"); ad-hoc signing makes it launchable (you may still need to
# approve it once in System Settings > Privacy & Security on first run).
xattr -cr "$APP" 2>/dev/null || true
if codesign --force --deep --sign - "$APP" >/dev/null 2>&1; then
    echo "ad-hoc signed"
else
    echo "warning: codesign failed; approve it in System Settings > Privacy & Security" >&2
fi

touch "$APP"   # nudge Finder/Dock to pick up the icon
echo "built  $APP"
echo "launch by double-clicking it in Finder, or:  open \"$APP\""
