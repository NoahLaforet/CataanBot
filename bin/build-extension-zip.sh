#!/usr/bin/env bash
# Build a clean upload zip of the extension/ directory for the
# Chrome Web Store dev console.
#
# Strips junk that shouldn't ship (.DS_Store, *.swp, stale logs)
# and writes to dist/catanbot-extension-v{VERSION}.zip where
# VERSION comes from extension/manifest.json. CWS rejects zips
# containing files starting with `_` other than _locales/, so
# this script also fails loud if any are detected.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXT_DIR="$REPO_ROOT/extension"
DIST_DIR="$REPO_ROOT/dist"

if [ ! -f "$EXT_DIR/manifest.json" ]; then
    echo "build-extension-zip: no manifest at $EXT_DIR/manifest.json" >&2
    exit 1
fi

VERSION="$(python3 -c "
import json
with open('$EXT_DIR/manifest.json') as f:
    print(json.load(f).get('version', '0.0.0'))
")"

mkdir -p "$DIST_DIR"
ZIP_PATH="$DIST_DIR/catanbot-extension-v${VERSION}.zip"
rm -f "$ZIP_PATH"

# Reject reserved-name files in the bundle root. CWS bans these.
BAD="$(find "$EXT_DIR" -maxdepth 1 -name '_*' -not -name '_locales' \
    -print 2>/dev/null || true)"
if [ -n "$BAD" ]; then
    echo "build-extension-zip: refusing to package files starting with _" >&2
    echo "$BAD" >&2
    exit 1
fi

# Build the zip from inside extension/ so paths are relative to
# the manifest (CWS expects manifest.json at the zip root).
(
    cd "$EXT_DIR"
    zip -r -X "$ZIP_PATH" . \
        -x '.DS_Store' '*/.DS_Store' \
        -x '*.swp' '*/*.swp' \
        -x '*~' '*/*~' \
        -x 'pulse-test.html' \
        -x '*.log' '*/*.log'
) >/dev/null

echo "built  $ZIP_PATH"
echo "size   $(du -h "$ZIP_PATH" | cut -f1)"
echo
echo "next  open https://chrome.google.com/webstore/devconsole"
echo "      → upload this zip → fill in fields per docs/STORE_LISTING.md"
