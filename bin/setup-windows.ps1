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

# --- Make sure git + python are in the distro ----------------------
Write-Step "Installing git + python inside Ubuntu (sudo prompt may appear)"
$prereqs = "sudo -n apt-get update -qq 2>/dev/null && sudo -n apt-get install -y -qq git python3 python3-venv 2>/dev/null"
$installAttempt = wsl --exec bash -c "$prereqs && echo PREREQS_OK || echo PREREQS_NEEDS_PASSWORD"
if ($installAttempt -match "PREREQS_OK") {
    Write-Ok "git + python3 + python3-venv present"
} else {
    Write-Warn "passwordless sudo isn't enabled — running interactive install."
    Write-Note "Ubuntu will prompt for your WSL password once."
    wsl --exec bash -c "sudo apt-get update && sudo apt-get install -y git python3 python3-venv"
    if ($LASTEXITCODE -ne 0) {
        Write-Err "apt-get install failed. Open an Ubuntu terminal and run:"
        Write-Note "    sudo apt-get update && sudo apt-get install -y git python3 python3-venv"
        Write-Note "Then re-run this script."
        exit 1
    }
    Write-Ok "git + python3 + python3-venv installed"
}

# --- Clone the repo inside the distro at ~/CatanBot ----------------
Write-Step "Cloning CatanBot inside Ubuntu (~/CatanBot)"
$cloneCheck = wsl --exec bash -lc "if [ -d ~/CatanBot/.git ]; then echo PRESENT; else echo MISSING; fi"
if ($cloneCheck -match "PRESENT") {
    Write-Ok "~/CatanBot already cloned — pulling latest"
    wsl --exec bash -lc "cd ~/CatanBot && git pull --ff-only 2>&1 | tail -3"
} else {
    wsl --exec bash -lc "git clone https://github.com/NoahLaforet/CatanBot.git ~/CatanBot"
    if ($LASTEXITCODE -ne 0) {
        Write-Err "git clone failed inside Ubuntu."
        exit 1
    }
    Write-Ok "cloned to ~/CatanBot"
}

# --- Bootstrap the launcher (creates .venv + installs deps) --------
Write-Step "Bootstrapping the bridge venv inside Ubuntu (~30s on first run)"
wsl --exec bash -lc "cd ~/CatanBot && ./bin/catanbot --help >/dev/null"
if ($LASTEXITCODE -eq 0) {
    Write-Ok "bridge ready — venv created + dependencies installed"
} else {
    Write-Err "bridge bootstrap failed. Open Ubuntu and run:"
    Write-Note "    cd ~/CatanBot && ./bin/catanbot --help"
    Write-Note "to see the full error."
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
$wslUser = (wsl --exec bash -c 'echo $USER').Trim()
$extPath = "\\wsl.localhost\Ubuntu\home\$wslUser\CatanBot\extension"

Write-Host ""
Write-Host "  Setup done." -ForegroundColor Green
Write-Host ""
Write-Host "  ── Start the bridge ──" -ForegroundColor Cyan
Write-Host "  Open Ubuntu (Start menu → 'Ubuntu') and run:" -ForegroundColor White
Write-Host ""
Write-Host "    cd ~/CatanBot && ./bin/catanbot live" -ForegroundColor White
Write-Host ""
Write-Host "  Leave that terminal open — it logs every event." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  ── Load the Chrome extension ──" -ForegroundColor Cyan
Write-Host "  In Chrome:" -ForegroundColor White
Write-Host "    1. Open  chrome://extensions" -ForegroundColor White
Write-Host "    2. Toggle  Developer mode (top right)" -ForegroundColor White
Write-Host "    3. Click  Load unpacked  and pick:" -ForegroundColor White
Write-Host "         $extPath" -ForegroundColor Yellow
Write-Host "    4. Pin the CatanBot icon (puzzle piece menu → pin)" -ForegroundColor White
Write-Host "    5. Click the icon, open colonist.io, start a game." -ForegroundColor White
Write-Host ""
