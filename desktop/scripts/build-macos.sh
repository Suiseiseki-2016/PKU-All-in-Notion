#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_TAURI="$DESKTOP_DIR/src-tauri"
TARGET_ROOT="${PKU_MACOS_TARGET_DIR:-/private/tmp/pku-all-in-notion-target}"
export CARGO_TARGET_DIR="$TARGET_ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "build-macos.sh must run on macOS" >&2
  exit 1
fi

cd "$DESKTOP_DIR"

# Build first without bundling. Repositories under Desktop/iCloud can attach
# com.apple.provenance/Finder metadata to generated binaries and copied icons;
# codesign rejects those attributes. Strip them after compilation and before
# Tauri assembles/signs the app bundle.
npx tauri build --no-bundle
xattr -cr \
  "$SRC_TAURI/icons" \
  "$SRC_TAURI/binaries" \
  "$SRC_TAURI/resources/runtime" \
  "$TARGET_ROOT/release"
APP="$TARGET_ROOT/release/bundle/macos/PKU All in Notion.app"
DMG_DIR="$TARGET_ROOT/release/bundle/dmg"
DMG_STAGE="$TARGET_ROOT/release/bundle/dmg-stage"
VERSION="$(node -p "require('./package.json').version")"
DMG="$DMG_DIR/PKU All in Notion_${VERSION}_aarch64.dmg"

# Tauri's Finder-based DMG decorator writes com.apple.FinderInfo back onto
# the already signed app when the repository lives under Desktop/iCloud. A
# plain hdiutil image is deterministic and still provides the conventional
# app + Applications drag target.
# The Windows release enables signed updater artifacts globally. A manually
# installed local DMG does not publish a Mac updater archive, so avoid asking
# for the Windows updater private key while bundling this app.
PATH="$SCRIPT_DIR/macos-tools:$PATH" npx tauri bundle --bundles app \
  --config '{"bundle":{"createUpdaterArtifacts":false}}'
/usr/bin/xattr -cr "$APP"
/usr/bin/codesign --verify --deep --strict "$APP"
rm -rf "$DMG_STAGE"
mkdir -p "$DMG_STAGE" "$DMG_DIR"
/usr/bin/ditto "$APP" "$DMG_STAGE/PKU All in Notion.app"
ln -s /Applications "$DMG_STAGE/Applications"
/usr/bin/hdiutil create \
  -volname "PKU All in Notion" \
  -srcfolder "$DMG_STAGE" \
  -ov \
  -format UDZO \
  "$DMG"
echo "DMG ready: $DMG"
