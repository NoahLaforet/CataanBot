# bin/build-bridge-windows.ps1
#
# Build the single self-contained CatanBot bridge .exe with PyInstaller
# and package it as the Windows release asset. This is the Windows
# equivalent of bin/build-bridge-bundle.sh (macOS / Linux): a double-click
# console .exe that runs the bridge on 127.0.0.1:8765 with the advisor on,
# no Python install required on the player's machine.
#
# Run this ON A WINDOWS PC. PyInstaller does NOT cross-compile, so the
# Mac build can't produce the .exe; it has to be built here. Requires the
# bridge extras plus pyinstaller in the active environment:
#
#     pip install -e ".[bridge]" pyinstaller
#
# Outputs:
#     dist/catanbot-bridge.exe      raw PyInstaller onefile output
#     dist/CatanBot.exe             friendly copy the player double-clicks
#     dist/CatanBot-windows.zip     the GitHub release asset
#
# The zip name (CatanBot-windows.zip) and the release tag (v0.45.1) MUST
# match what the extension panel's Windows button downloads from:
#     https://github.com/NoahLaforet/CatanBot/releases/latest/download/CatanBot-windows.zip
# If you bump the tag, update RELEASE_TAG below and re-cut the release;
# the "latest/download" URL itself never changes per release.

$ErrorActionPreference = 'Stop'

# Release tag the CatanBot-windows.zip asset gets uploaded to. Keep this
# in lockstep with the macOS release so a single "latest" carries both.
$RELEASE_TAG = 'v0.45.1'

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==>" -ForegroundColor Cyan -NoNewline
    Write-Host " $msg"
}
function Write-Note($msg) {
    Write-Host "    $msg" -ForegroundColor DarkGray
}
function Write-Ok($msg) {
    Write-Host "    [OK] " -ForegroundColor Green -NoNewline
    Write-Host $msg
}
function Write-Err($msg) {
    Write-Host "    [X]  " -ForegroundColor Red -NoNewline
    Write-Host $msg
}

# Run from the repo root regardless of where this script is invoked from,
# so the relative paths in the spec (bin/bridge_entry.py, pathex 'src')
# resolve the same way the macOS shell builder expects.
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $RepoRoot

Write-Host ""
Write-Host "  CatanBot Windows bridge build" -ForegroundColor Magenta
Write-Host "  -----------------------------" -ForegroundColor Magenta

# --- Python 3.11+ check --------------------------------------------
# The bridge bundle pins to a modern interpreter (matches the macOS
# build). Prefer the 'py' launcher with an explicit 3.11+ request, fall
# back to whatever 'python' is on PATH and verify its version.
Write-Step "Checking for Python 3.11 or newer"
$Py = $null
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    & py -3.11 -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) { $Py = @('py', '-3.11') }
}
if (-not $Py) {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) { $Py = @('python') }
}
if (-not $Py) {
    Write-Err "No Python found on PATH (tried the 'py' launcher and 'python')."
    Write-Note "Install Python 3.11+ from https://www.python.org/downloads/"
    Write-Note "and make sure 'Add python.exe to PATH' is checked."
    exit 1
}

# Confirm the chosen interpreter is actually 3.11 or newer.
$verOk = & @Py -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
    $ver = & @Py -c "import platform; print(platform.python_version())" 2>$null
    Write-Err "Python $ver is too old (need 3.11 or newer)."
    Write-Note "Install Python 3.11+ from https://www.python.org/downloads/"
    exit 1
}
$ver = & @Py -c "import platform; print(platform.python_version())" 2>$null
Write-Ok "Python $ver"

# --- PyInstaller check ---------------------------------------------
Write-Step "Checking for PyInstaller"
& @Py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Err "PyInstaller is not installed in this Python."
    Write-Note 'pip install -e ".[bridge]" pyinstaller'
    exit 1
}
Write-Ok "PyInstaller present"

# --- Build ----------------------------------------------------------
# Build straight from the CLI flags (the same set bin/build-bridge-bundle.sh
# uses), NOT a .spec file: catanbot-bridge.spec is gitignored, so a fresh
# clone does not have it. uvicorn resolves its loop / http / websocket impls
# at runtime via conditional imports PyInstaller's static analysis misses, so
# they are declared as hidden imports; --collect-all uvicorn grabs the rest,
# and catanbot + catanatron are pulled in whole. --console onefile yields one
# double-click .exe. --clean drops stale work; --noconfirm stays non-interactive.
$PyiArgs = @(
    '--noconfirm', '--clean',
    '--name', 'catanbot-bridge',
    '--onefile', '--windowed',
    '--icon', 'bin/catanbot.ico',
    '--paths', 'src',
    '--collect-submodules', 'catanbot',
    '--collect-submodules', 'catanatron',
    '--collect-all', 'uvicorn',
    '--collect-all', 'pystray',
    '--add-data', 'extension/icons/icon-128.png;catanbot_assets',
    '--hidden-import', 'uvicorn.logging',
    '--hidden-import', 'uvicorn.loops.auto',
    '--hidden-import', 'uvicorn.loops.asyncio',
    '--hidden-import', 'uvicorn.protocols.http.auto',
    '--hidden-import', 'uvicorn.protocols.http.h11_impl',
    '--hidden-import', 'uvicorn.protocols.websockets.auto',
    '--hidden-import', 'uvicorn.protocols.websockets.websockets_impl',
    '--hidden-import', 'uvicorn.protocols.websockets.wsproto_impl',
    '--hidden-import', 'uvicorn.lifespan.on',
    'bin/win_tray_entry.py'
)
Write-Step "Building dist/catanbot-bridge.exe (PyInstaller, this takes a minute)"
# PyInstaller logs progress to stderr; under Windows PowerShell 5.1 with
# $ErrorActionPreference = 'Stop' that can surface as a terminating error
# before the exit-code check runs. Relax error handling just for the build
# call and trust $LASTEXITCODE to report success or failure.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& @Py -m PyInstaller @PyiArgs
$buildExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP
if ($buildExit -ne 0) {
    Write-Err "PyInstaller build failed (see output above)."
    exit 1
}

$ExePath = Join-Path 'dist' 'catanbot-bridge.exe'
if (-not (Test-Path $ExePath)) {
    Write-Err "Build finished but $ExePath is missing."
    Write-Note "Check the PyInstaller output above for the real failure."
    exit 1
}
Write-Ok "built $ExePath"

# --- Package --------------------------------------------------------
# Copy to the friendly name the player double-clicks, then zip just that
# one .exe (Compress-Archive, -Force overwrites a prior zip). The release
# asset is the zip, not the bare .exe, to match the macOS .zip flow and
# to keep the GitHub "latest/download" URL pointing at a stable name.
Write-Step "Packaging CatanBot.exe into CatanBot-windows.zip"
$FriendlyExe = Join-Path 'dist' 'CatanBot.exe'
Copy-Item -Path $ExePath -Destination $FriendlyExe -Force
Write-Ok "copied to $FriendlyExe"

$ZipPath = Join-Path 'dist' 'CatanBot-windows.zip'
Compress-Archive -Path $FriendlyExe -DestinationPath $ZipPath -Force
if (-not (Test-Path $ZipPath)) {
    Write-Err "Compress-Archive ran but $ZipPath is missing."
    exit 1
}
$ZipSizeMB = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
Write-Ok "wrote $ZipPath ($ZipSizeMB MB)"

# --- Release upload hint -------------------------------------------
# This script builds + packages only; cutting the release is a manual
# step so you can eyeball the asset first. --clobber replaces an asset of
# the same name on an existing tag, so re-running is safe. The asset name
# (CatanBot-windows.zip) is what the panel's "latest/download" URL fetches.
Write-Step "Next step: attach the zip to the GitHub release"
Write-Host ""
Write-Host "    gh release upload $RELEASE_TAG dist/CatanBot-windows.zip --clobber -R NoahLaforet/CatanBot" -ForegroundColor White
Write-Host ""
Write-Note "If the tag doesn't exist yet, create it first, e.g.:"
Write-Note "    gh release create $RELEASE_TAG -R NoahLaforet/CatanBot -t $RELEASE_TAG -n `"CatanBot $RELEASE_TAG`""
Write-Host ""

# Optional polish (not required to ship): give the exe a real icon instead
# of the default PyInstaller one. Generate a .ico from the extension icon:
#     python -c "from PIL import Image; Image.open('extension/icons/icon-128.png').save('bin/catanbot.ico', sizes=[(16,16),(32,32),(48,48),(128,128)])"
# Then add  '--icon', 'bin/catanbot.ico'  to $PyiArgs above. Skipped here on
# purpose: the build ships fine
# without it, and a missing .ico would otherwise fail the build.
