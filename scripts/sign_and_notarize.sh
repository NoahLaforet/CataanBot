#!/usr/bin/env bash
# Developer ID sign + notarize + staple the self-contained CatanBot.app so a
# DOWNLOADED copy opens by double-click on macOS 14/15 with no Gatekeeper
# wall. Run after bin/build-app-bundle.sh.
#
# Why this is needed: macOS Sequoia removed the right-click > Open bypass, and
# an ad-hoc-signed download usually throws "is damaged and can't be opened"
# with no Open button. Notarizing is the only path that just opens by
# double-click for a non-technical user.
#
# ONE-TIME SETUP
#   1. Create a "Developer ID Application" certificate:
#        Xcode > Settings > Accounts > (your team) > Manage Certificates > +
#      Confirm it is installed:
#        security find-identity -v -p codesigning   # lists Developer ID Application: ... (TEAMID)
#   2. App-specific password at appleid.apple.com (Sign-In & Security).
#   3. cp .env.example .env  and fill it in (.env is gitignored).
#   4. Store notary credentials once (reads your .env):
#        set -a; . ./.env; set +a
#        xcrun notarytool store-credentials "$NOTARY_PROFILE" \
#          --apple-id "$APPLE_ID" --team-id "$TEAM_ID" --password "$APP_SPECIFIC_PASSWORD"
#
# THEN, each release:
#   ./bin/build-app-bundle.sh
#   ./scripts/sign_and_notarize.sh
#   gh release upload <tag> dist/CatanBot-macos.zip --clobber -R NoahLaforet/CatanBot
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

[ -f .env ] || { echo "missing .env (cp .env.example .env and fill it in)" >&2; exit 1; }
set -a; . ./.env; set +a
: "${SIGN_IDENTITY:?set SIGN_IDENTITY in .env}"
: "${NOTARY_PROFILE:?set NOTARY_PROFILE in .env}"

APP="dist/CatanBot.app"
ENT="entitlements.plist"
ZIP="dist/CatanBot-macos.zip"
[ -d "$APP" ] || { echo "no $APP — run ./bin/build-app-bundle.sh first" >&2; exit 1; }

echo "==> strip xattrs (iCloud/FinderInfo metadata breaks codesign)"
xattr -cr "$APP"

# Sign INSIDE-OUT (never `codesign --deep` for signing a PyInstaller bundle):
# every nested dylib/so first, then any other nested Mach-O, then the .app.
echo "==> sign nested dylibs/.so"
find "$APP" -type f \( -name "*.so" -o -name "*.dylib" \) -print0 \
  | while IFS= read -r -d '' f; do
      codesign --force --timestamp --options runtime \
        --entitlements "$ENT" --sign "$SIGN_IDENTITY" "$f"
    done
echo "==> sign nested Mach-O executables"
find "$APP/Contents" -type f -perm +111 ! -name "*.dylib" ! -name "*.so" -print0 \
  | while IFS= read -r -d '' f; do
      if file "$f" | grep -q "Mach-O"; then
        codesign --force --timestamp --options runtime \
          --entitlements "$ENT" --sign "$SIGN_IDENTITY" "$f"
      fi
    done
echo "==> sign the app bundle"
codesign --force --timestamp --options runtime \
  --entitlements "$ENT" --sign "$SIGN_IDENTITY" "$APP"
codesign --verify --strict --deep --verbose=2 "$APP"

echo "==> notarize (ditto zip, submit, wait)"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY_PROFILE" --wait

echo "==> staple + verify"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
spctl -a -vvv -t exec "$APP" || true   # expect: accepted, source=Notarized Developer ID

echo "==> re-zip the stapled app for upload"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

echo
echo "notarized + stapled: $APP"
echo "ship  gh release upload <tag> $ZIP --clobber -R NoahLaforet/CatanBot"
