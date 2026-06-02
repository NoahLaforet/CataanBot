#!/usr/bin/env bash
# Build a single self-contained CatanBot bridge binary with PyInstaller.
#
# Run this ON EACH TARGET OS (macOS / Windows / Linux) you want to ship
# for: PyInstaller does NOT cross-compile. Requires the bridge extras
# plus pyinstaller in the active environment:
#
#   pip install -e '.[bridge]' pyinstaller
#
# Output: dist/catanbot-bridge        (macOS / Linux)
#         dist/catanbot-bridge.exe    (Windows)
#
# The binary starts the bridge on 127.0.0.1:8765 with the advisor on
# (see bin/bridge_entry.py). After building, smoke-test it:
#
#   ./dist/catanbot-bridge &
#   curl -s http://127.0.0.1:8765/advisor | head -c 200
#
# Then wrap the binary in a platform installer (see
# docs/BRIDGE_INSTALLER.md) and sign it (see SIGNING.md).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PY="${PYTHON:-python3}"
if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
    echo "build-bridge-bundle: PyInstaller is not installed in $PY." >&2
    echo "  pip install -e '.[bridge]' pyinstaller" >&2
    exit 1
fi

# uvicorn picks its event loop / HTTP / WebSocket implementations at
# runtime via conditional imports that PyInstaller's static analysis
# misses, so they are declared as hidden imports below. --collect-all
# uvicorn grabs the rest of its tree; catanatron + catanbot are pulled
# in whole. PyInstaller's bundled matplotlib hook handles the postmortem
# renderer's data files automatically.
"$PY" -m PyInstaller \
    --noconfirm --clean \
    --name catanbot-bridge \
    --onefile \
    --console \
    --paths src \
    --collect-submodules catanbot \
    --collect-submodules catanatron \
    --collect-all uvicorn \
    --hidden-import uvicorn.logging \
    --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.loops.asyncio \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.http.h11_impl \
    --hidden-import uvicorn.protocols.websockets.auto \
    --hidden-import uvicorn.protocols.websockets.websockets_impl \
    --hidden-import uvicorn.protocols.websockets.wsproto_impl \
    --hidden-import uvicorn.lifespan.on \
    bin/bridge_entry.py

echo
echo "built  dist/catanbot-bridge*"
echo "smoke  ./dist/catanbot-bridge &  then  curl http://127.0.0.1:8765/advisor"
