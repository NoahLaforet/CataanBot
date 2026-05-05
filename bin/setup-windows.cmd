@echo off
REM bin/setup-windows.cmd
REM
REM Double-clickable Windows setup launcher. Re-runs itself
REM elevated if not already admin, then invokes the PowerShell
REM setup script with -ExecutionPolicy Bypass so users don't have
REM to fight Windows' default "no unsigned scripts" rule.
REM
REM Usage from File Explorer: double-click bin\setup-windows.cmd.
REM Usage from a terminal:    bin\setup-windows.cmd

setlocal
cd /d "%~dp0\.."

REM --- Admin check ----------------------------------------------------
net session >nul 2>&1
if %errorLevel% NEQ 0 (
    echo [setup] not admin yet — relaunching elevated...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)

REM --- Run the PowerShell script with execution policy bypass --------
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-windows.ps1"
set EXITCODE=%errorlevel%

echo.
echo Press any key to close this window...
pause >nul
exit /b %EXITCODE%
