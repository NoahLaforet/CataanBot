# Code signing the bridge bundle

The bundled bridge binary (`bin/build-bridge-bundle.sh`) runs unsigned
for local testing, but a distributable installer must be signed or users
hit Gatekeeper (macOS) and SmartScreen (Windows) warnings. This is the
one part of the installer story that needs a paid certificate, so it is
deferred until you are ready to publish.

## macOS

Needs an Apple Developer membership ($99/yr) and a "Developer ID
Application" certificate in your login keychain.

```bash
# 1. Sign the binary with a hardened runtime.
codesign --force --options runtime --timestamp \
    --sign "Developer ID Application: Your Name (TEAMID)" \
    dist/catanbot-bridge

# 2. Notarize (Apple scans it; required for Gatekeeper to pass).
ditto -c -k --keepParent dist/catanbot-bridge dist/catanbot-bridge.zip
xcrun notarytool submit dist/catanbot-bridge.zip \
    --apple-id you@example.com --team-id TEAMID \
    --password "app-specific-password" --wait

# 3. Staple the ticket onto the binary (or the .pkg, see below).
xcrun stapler staple dist/catanbot-bridge
```

A `.pkg` installer should be signed with a "Developer ID Installer"
certificate (`productsign --sign "Developer ID Installer: ..."`) and
notarized the same way. The existing `bin/build-app.sh` ad-hoc signs the
menu-bar `.app`; that is fine for a self-built local app but not for
distribution.

## Windows

SmartScreen flags unsigned executables until they build reputation. An
EV (Extended Validation) code-signing certificate ($300+/yr) clears it
immediately; a standard OV cert works but earns reputation slowly.

```bat
signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 ^
    /a dist\catanbot-bridge.exe
:: Sign the Inno Setup installer .exe the same way after building it.
```

## Linux

No signing required. AppImages can be GPG-signed optionally
(`appimagetool --sign`); most users will not check the signature.

## Until you have certificates

Ship the installers unsigned with a short "first launch" note: on macOS,
right-click the app and choose Open to bypass Gatekeeper once; on
Windows, click "More info" then "Run anyway" on the SmartScreen prompt.
This is acceptable for an unlisted / friends-and-power-users launch and
removes the cost barrier until the listing goes fully public.
