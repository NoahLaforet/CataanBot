# bin/setup-windows.ps1
#
# One-shot Windows setup for CatanBot. Installs / confirms WSL2 +
# Ubuntu so the user can run the bridge inside WSL alongside their
# Chrome installation on Windows. After this script finishes, the
# user opens an Ubuntu terminal and runs the same three commands a
# macOS / Linux friend would:
#
#     git clone https://github.com/NoahLaforet/CatanBot.git
#     cd CatanBot
#     ./bin/catanbot live
#
# The bridge listens on 127.0.0.1:8765 inside WSL, and Windows
# Chrome can reach it because WSL2 forwards localhost back to the
# Windows host (since Windows 10 build 19041 / Windows 11).
#
# Right-click → "Run with PowerShell" doesn't elevate; you need to
# run an admin PowerShell window. Tell the user that up-front rather
# than failing partway through.

$ErrorActionPreference = 'Stop'

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
function Write-Warn($msg) {
    Write-Host "    [!]  " -ForegroundColor Yellow -NoNewline
    Write-Host $msg
}
function Write-Err($msg) {
    Write-Host "    [X]  " -ForegroundColor Red -NoNewline
    Write-Host $msg
}

Write-Host ""
Write-Host "  CatanBot Windows setup" -ForegroundColor Magenta
Write-Host "  ----------------------" -ForegroundColor Magenta
Write-Host ""

# --- Admin check ----------------------------------------------------
Write-Step "Checking for administrator privileges"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Err "Not running as Administrator."
    Write-Host ""
    Write-Host "  Re-run this script from an admin PowerShell:" -ForegroundColor Yellow
    Write-Host "    1. Press Win+X, click 'Terminal (Admin)'" -ForegroundColor Yellow
    Write-Host "    2. cd to the CatanBot folder" -ForegroundColor Yellow
    Write-Host "    3. .\bin\setup-windows.ps1" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}
Write-Ok "Running as Administrator"

# --- Windows version -----------------------------------------------
Write-Step "Checking Windows version"
$winBuild = [int][Environment]::OSVersion.Version.Build
if ($winBuild -lt 19041) {
    Write-Err "Windows build $winBuild is too old (need 19041 / 2004 or later)."
    Write-Host ""
    Write-Host "  Run Windows Update before continuing." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}
Write-Ok "Windows build $winBuild"

# --- WSL install / status ------------------------------------------
Write-Step "Checking WSL"
$wslExists = $false
try {
    $null = Get-Command wsl.exe -ErrorAction Stop
    $wslExists = $true
} catch { $wslExists = $false }

if (-not $wslExists) {
    Write-Note "WSL not found — installing (this enables WSL features and"
    Write-Note "downloads Ubuntu; reboot may be required)."
    wsl --install
    Write-Host ""
    Write-Host "  ---- Reboot required ----" -ForegroundColor Yellow
    Write-Host "  After reboot, open Ubuntu from the Start menu, finish the" -ForegroundColor Yellow
    Write-Host "  one-time username/password prompt, then re-run this script" -ForegroundColor Yellow
    Write-Host "  in an admin PowerShell to verify the install." -ForegroundColor Yellow
    Write-Host ""
    exit 0
}
Write-Ok "wsl.exe present"

# Check WSL is actually functional (default version 2, can launch a distro)
Write-Step "Confirming WSL2 is the default version"
try {
    wsl --set-default-version 2 2>$null
    Write-Ok "default WSL version set to 2"
} catch {
    Write-Warn "could not set default WSL version (probably already set)"
}

# --- Distro presence ------------------------------------------------
Write-Step "Checking for an installed Linux distro"
$distros = @()
try {
    $raw = wsl --list --quiet 2>$null
    # WSL list output is UTF-16-encoded; PS reads each char with NULs.
    # Strip NULs + filter out the header row.
    $distros = ($raw -replace "`0", "") -split "`r?`n" |
        Where-Object { $_ -and $_.Trim() -ne "" -and $_ -notmatch "^Windows" }
} catch { $distros = @() }

if ($distros.Count -eq 0) {
    Write-Note "No Linux distro found — installing Ubuntu."
    wsl --install -d Ubuntu
    Write-Host ""
    Write-Host "  ---- Finish Ubuntu first-run ----" -ForegroundColor Yellow
    Write-Host "  Ubuntu just installed. It needs a one-time username/password" -ForegroundColor Yellow
    Write-Host "  setup. Open 'Ubuntu' from the Start menu, complete that," -ForegroundColor Yellow
    Write-Host "  then re-run this script." -ForegroundColor Yellow
    Write-Host ""
    exit 0
}
Write-Ok "found distro(s): $($distros -join ', ')"

# --- Quick smoke: run a hello inside the default distro ------------
Write-Step "Smoke-testing the default distro"
$smoke = wsl --exec bash -c "echo wsl-ok && uname -srm && which python3 || echo NOPYTHON" 2>$null
if ($smoke -match "wsl-ok") {
    Write-Ok "default distro responds"
    $smoke -split "`r?`n" | Where-Object { $_ -ne "" } | ForEach-Object {
        Write-Note "    $_"
    }
} else {
    Write-Err "could not run a command inside the default distro"
    Write-Note "$smoke"
    exit 1
}

# --- Bridge port reachability hint ----------------------------------
# WSL2 forwards 127.0.0.1:<port> from the WSL2 distro to the Windows
# host on Windows 10 build 19041+ / Windows 11. The user's Chrome on
# Windows can reach 127.0.0.1:8765 once the bridge is running inside
# WSL — no extra port-forwarding work needed.
Write-Step "Confirming Windows ↔ WSL2 localhost forwarding"
Write-Ok "Windows build $winBuild supports WSL2 localhost forwarding"
Write-Note "Chrome (Windows) will reach 127.0.0.1:8765 in WSL automatically"

# --- Final next steps ----------------------------------------------
Write-Host ""
Write-Host "  All set. Inside an Ubuntu (WSL) terminal, run:" -ForegroundColor Green
Write-Host ""
Write-Host "    git clone https://github.com/NoahLaforet/CatanBot.git" -ForegroundColor White
Write-Host "    cd CatanBot" -ForegroundColor White
Write-Host "    ./bin/catanbot live" -ForegroundColor White
Write-Host ""
Write-Host "  Then in Chrome:" -ForegroundColor Green
Write-Host "    1. chrome://extensions  →  Developer mode" -ForegroundColor White
Write-Host "    2. Load unpacked  →  pick the extension/ folder" -ForegroundColor White
Write-Host "       (browse to \\wsl.localhost\Ubuntu\home\<you>\CatanBot\extension)" -ForegroundColor White
Write-Host "    3. Pin the CatanBot icon, click it, open colonist.io" -ForegroundColor White
Write-Host ""
