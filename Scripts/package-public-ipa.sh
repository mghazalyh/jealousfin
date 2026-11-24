#!/usr/bin/env bash
set -euo pipefail
: "${JEALOUSFIN_BUILD_NUMBER:?Expected numeric build number}"
[[ "$JEALOUSFIN_BUILD_NUMBER" =~ ^[0-9]+$ ]]
APP="DerivedData/Build/Products/Release-iphoneos/Jealousfin.app"
STAGED="build/release-app/Jealousfin.app"
BUNDLE="com.example.jealousfin"
test -f "$APP/Jealousfin"
test -f "$APP/Info.plist"
check_metadata() {
  local app="$1"
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Info.plist")" == "$BUNDLE" ]]
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app/Info.plist")" == "0.1.13" ]]
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$app/Info.plist")" == "$JEALOUSFIN_BUILD_NUMBER" ]]
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :MinimumOSVersion' "$app/Info.plist")" == "15.0" ]]
  [[ "$(lipo -archs "$app/Jealousfin")" == arm64 ]]
  [[ -z "$(find "$app" -type f \( -iname 'JealousfinDefaults.plist' -o -iname '*.mobileprovision' -o -iname '*.p12' -o -iname '*.pfx' -o -iname '*.key' \) -print -quit)" ]]
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :LSRequiresIPhoneOS' "$app/Info.plist")" == true ]]
  test -x "$app/Jealousfin"
  [[ "$(stat -f %Lp "$app/Jealousfin")" == 755 ]]
}
chmod 755 "$APP/Jealousfin"
check_metadata "$APP"
mkdir -p build
rm -rf build/Payload build/release-app
mkdir -p build/Payload build/release-app
cp -R "$APP" "$STAGED"
cp Vendor/QuickJS/LICENSE "$STAGED/QuickJS-LICENSE.txt"
python3 -c 'import json; m=json.load(open("Promoted/baseline.json")); print(json.dumps({k:m[k] for k in ("schemaVersion","revision","sources")},sort_keys=True))' > "$STAGED/PromotedBaseline.json"
# Preserve the unsigned packaging format used by the previously installed RC1.
# This is input for a device-side installer, not a native OTA package.
if codesign -dv "$STAGED/Jealousfin" >/dev/null 2>&1; then
  codesign --remove-signature "$STAGED/Jealousfin"
fi
rm -rf "$STAGED/_CodeSignature"
chmod 755 "$STAGED/Jealousfin"
xattr -cr "$STAGED"
verify_unsigned() {
  local app="$1"
  check_metadata "$app"
  test ! -e "$app/_CodeSignature"
  if codesign -dv "$app/Jealousfin" >/dev/null 2>&1; then
    echo "Unsigned package unexpectedly has a signature" >&2
    return 1
  fi
}
verify_unsigned "$STAGED"
rm -f build/Jealousfin-unsigned.ipa build/Jealousfin-unsigned.ipa.sha256
cp -R "$STAGED" build/Payload/
(cd build && /usr/bin/zip -qry Jealousfin-unsigned.ipa Payload)
rm -rf build/Payload/Jealousfin.app
unzip -tq build/Jealousfin-unsigned.ipa
unsigned_check="$(mktemp -d)"
trap 'rm -rf "$unsigned_check"' EXIT
ditto -x -k build/Jealousfin-unsigned.ipa "$unsigned_check"
verify_unsigned "$unsigned_check/Payload/Jealousfin.app"
(cd build && shasum -a 256 Jealousfin-unsigned.ipa > Jealousfin-unsigned.ipa.sha256 && shasum -a 256 -c Jealousfin-unsigned.ipa.sha256)
rm -rf "$unsigned_check"
trap - EXIT
# Sign only after every resource is staged. Do not copy shared profile entitlements.
# This is an ad hoc signature for TrollStore, not an Apple distribution signature.
xattr -cr "$STAGED"
codesign --force --deep --sign - --timestamp=none "$STAGED"
verify_signature() {
  local app="$1"
  local details
  check_metadata "$app"
  codesign --verify --deep --strict --verbose=2 "$app"
  details="$(codesign -dv --verbose=4 "$app" 2>&1)"
  grep -Fx "Identifier=$BUNDLE" <<< "$details"
  grep -Fx 'Signature=adhoc' <<< "$details"
  local entitlements
  entitlements="$(codesign -d --entitlements :- "$app" 2>/dev/null)"
  if [[ -n "$entitlements" ]]; then
    printf '%s' "$entitlements" | python3 -c 'import plistlib,sys; e=plistlib.loads(sys.stdin.buffer.read()); assert not e, "Unexpected public IPA entitlements"'
  fi
}
verify_signature "$STAGED"
rm -f build/Jealousfin.ipa build/Jealousfin.ipa.sha256
cp -R "$STAGED" build/Payload/
verify_signature build/Payload/Jealousfin.app
(cd build && /usr/bin/zip -qry Jealousfin.ipa Payload)
unzip -tq build/Jealousfin.ipa
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
ditto -x -k build/Jealousfin.ipa "$scratch"
verify_signature "$scratch/Payload/Jealousfin.app"
(cd build && shasum -a 256 Jealousfin.ipa > Jealousfin.ipa.sha256 && shasum -a 256 -c Jealousfin.ipa.sha256)
echo "Public package verified: $BUNDLE, build $JEALOUSFIN_BUILD_NUMBER, ARM64, iOS 15.0."
