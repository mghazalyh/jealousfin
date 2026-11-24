#!/usr/bin/env bash
set -euo pipefail
# QuickJS-NG v0.10.1; immutable source revision, not a floating dependency.
revision=3c9afc9943323ee9c7dbd123c0cd991448f4b6c2
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git -C "$tmp" init -q
git -C "$tmp" remote add origin https://github.com/quickjs-ng/quickjs.git
git -C "$tmp" fetch --depth 1 origin "$revision"
git -C "$tmp" checkout -q --detach FETCH_HEAD
test "$(git -C "$tmp" rev-parse HEAD)" = "$revision"
mkdir -p Vendor/QuickJS
cp "$tmp"/*.h Vendor/QuickJS/
for f in cutils.c libregexp.c libunicode.c quickjs.c xsum.c; do
  cp "$tmp/$f" Vendor/QuickJS/
done
cp "$tmp/LICENSE" Vendor/QuickJS/LICENSE
printf '%s\n' "$revision" > Vendor/QuickJS/REVISION
