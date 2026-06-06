# CatanBot on Windows

This is the native-Windows download path: grab a prebuilt bundle, double-click
one `.exe`, and the bridge runs locally so the Chrome panel connects. No WSL,
no Python, no terminal.

> Prefer running from source on Windows? The `git clone` + WSL2 route is in the
> main [README](../README.md#-windows-users--read-this-first). This doc covers
> the one-download bundle instead.

---

## For end users (download and run)

1. Open the latest release:
   [github.com/NoahLaforet/CatanBot/releases/latest](https://github.com/NoahLaforet/CatanBot/releases/latest).
2. Under **Assets**, download **`CatanBot-windows.zip`**.
3. Right-click the downloaded zip, choose **Extract All**, and pick a folder
   (Downloads is fine).
4. Open the extracted folder and double-click **`CatanBot.exe`**.

The bridge starts on `127.0.0.1:8765`. Leave its console window open while you
play; closing it stops the bridge. The CatanBot Chrome side panel detects the
running bridge and connects on its own.

### "Windows protected your PC" (SmartScreen)

The first time you run `CatanBot.exe`, Windows almost certainly shows a blue
**Microsoft Defender SmartScreen** dialog titled **"Windows protected your
PC"** that says it "prevented an unrecognized app from starting." This is
expected. CatanBot is an unsigned hobby build, so it has no code-signing
certificate and no download reputation yet, and SmartScreen flags any
downloaded `.exe` that it does not already recognize. It is a reputation
warning, not a virus detection.

To run it anyway:

1. Click **More info** (small link under the message text).
2. Click the **Run anyway** button that appears.

You only have to do this once per download; Windows remembers your choice for
that copy of the file.

If you do not see a **Run anyway** button, the file may still be tagged as
blocked from the internet. Close the dialog, then right-click `CatanBot.exe`,
choose **Properties**, and on the **General** tab tick **Unblock** at the
bottom, then **OK**. Double-click the exe again.

> Only do this for the `CatanBot.exe` you downloaded from the official release
> linked above. SmartScreen exists to stop you running unknown software, so the
> "Run anyway" step should be a deliberate choice, not a reflex.

### After it is running

- The bridge console prints each parsed game event as you play.
- In Chrome, open the CatanBot side panel (pin the green CatanBot icon, then
  click it). It connects to `127.0.0.1:8765` automatically and renders the HUD.
- To stop CatanBot, close the bridge console window.
- To update later, download the newer `CatanBot-windows.zip` from the latest
  release and repeat the unzip + run steps.

Your game state never leaves your machine. The bridge is local and the
extension's only network destination is `127.0.0.1:8765`.

---

## For the developer (build and publish)

PyInstaller does not cross-compile, so the Windows bundle has to be built on
real Windows (a Windows VM is fine). This produces the same `catanbot-bridge`
one-file binary as the macOS path, renamed to `CatanBot.exe` and zipped as the
download asset.

### Prerequisites (native Windows)

- **Python 3.12** (3.11+ works; catanatron needs 3.11 or newer). Install from
  python.org and tick "Add python.exe to PATH".
- **PowerShell 7** (`pwsh`). Windows PowerShell 5.1 also works, but the build
  script targets `pwsh`.
- The bridge extras plus PyInstaller, into a clean venv:

```powershell
git clone https://github.com/NoahLaforet/CatanBot.git
cd CatanBot
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[bridge]" pyinstaller
```

### Build

```powershell
pwsh bin/build-bridge-windows.ps1
```

The script checks for Python 3.11+ and PyInstaller, builds from the shared
`catanbot-bridge.spec` (the same onefile/console build with the uvicorn hidden
imports and `catanbot` + `catanatron` collected whole that the macOS path
uses), copies the result to the friendly name `dist\CatanBot.exe`, and packs
that into `dist\CatanBot-windows.zip`. It finishes by printing the exact
`gh release upload` line to run.

Smoke-test before shipping:

```powershell
.\dist\CatanBot.exe
# in another terminal:
curl http://127.0.0.1:8765/advisor
# expect: {"seq":0,"game_started":false,...
```

### Publish to the GitHub release

Attach the zip to the release with the stable, OS-predictable name the
extension's download card expects:

```powershell
gh release upload <tag> dist/CatanBot-windows.zip --clobber -R NoahLaforet/CatanBot
```

It then resolves at
`https://github.com/NoahLaforet/CatanBot/releases/latest/download/CatanBot-windows.zip`,
the mirror of the macOS `CatanBot-macos.zip` asset.

---

## Code signing (future option, not required now)

The download ships unsigned today. That is why end users see the SmartScreen
"Windows protected your PC" prompt and have to click through "More info" then
"Run anyway." This is the Windows analog of the macOS Gatekeeper block that
notarization clears.

To remove the warning, the `.exe` needs an **Authenticode** code-signing
certificate from a trusted CA, applied with `signtool`:

```bat
signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 ^
    /a dist\CatanBot.exe
```

Two certificate tiers exist:

- **Standard (OV) certificate** (roughly $100-200/yr). Signs the binary and
  builds SmartScreen reputation gradually as more people download and run it.
  Early downloaders may still see the prompt until reputation accrues.
- **EV (Extended Validation) certificate** (roughly $250-400/yr). Higher
  identity validation and faster reputation. Note that since March 2024,
  Microsoft no longer treats an EV signature as an instant SmartScreen bypass,
  so even EV builds reputation rather than clearing the prompt on day one.

Either tier is a recurring cost for a free hobby project, so signing is
deferred. The current "More info -> Run anyway" path is acceptable for an
unsigned friends-and-power-users build, and this section is the recipe to
follow if CatanBot ever warrants a paid certificate. See also the
cross-platform `SIGNING.md` and `docs/BRIDGE_INSTALLER.md`.

---

## Sources

SmartScreen behavior and the "More info -> Run anyway" / Unblock steps:

- [Microsoft Defender SmartScreen prevented an unrecognized app from starting (The Windows Club)](https://www.thewindowsclub.com/microsoft-defender-smartscreen-prevented-an-unrecognized-app-from-starting)
- ["Windows protected your PC" message during install (nTop Support)](https://support.ntop.com/hc/en-us/articles/360061374554-A-Windows-protected-your-PC-message-displays-during-install)
- [How to handle "Windows Defender SmartScreen prevented an unrecognized app from running" (Laerdal Help Center)](https://laerdal.my.site.com/HelpCenter/s/article/How-to-handle-error-message-Windows-Defender-SmartScreen-prevented-an-unrecognized-app-from-running)

Code-signing certificate tiers, cost, and the March 2024 EV/SmartScreen change:

- [Which Code Signing Certificate do I Need? EV or OV? (SSL.com)](https://www.ssl.com/faqs/which-code-signing-certificate-do-i-need-ev-ov/)
- [Reputation with OV certificates and EV certificates (Microsoft Q&A)](https://learn.microsoft.com/en-us/answers/questions/417016/reputation-with-ov-certificates-and-are-ev-certifi)
- [MS SmartScreen and Application Reputation (Sectigo Knowledge Base)](https://support.sectigo.com/PS_KnowledgeDetailPageFaq?Id=kA01N000000zFJx)
