#!/usr/bin/env bash
# Wrap dist/Hondana.app in a drag-to-Applications disk image.
#   packaging/macos/build_dmg.sh <version> <arch>      -> dist/Hondana-<version>-macos-<arch>.dmg
set -euo pipefail
version="$1"
arch="$2"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
ditto dist/Hondana.app "$stage/Hondana.app"   # ditto keeps the bundle's symlinks and signature
ln -s /Applications "$stage/Applications"
hdiutil create -volname "Hondana $version" -srcfolder "$stage" -ov -format UDZO \
  "dist/Hondana-$version-macos-$arch.dmg"
