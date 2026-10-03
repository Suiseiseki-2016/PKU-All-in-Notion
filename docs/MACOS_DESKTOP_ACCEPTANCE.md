# macOS desktop acceptance

This checklist validates the product window, bundled local service and DMG;
it is separate from the older terminal-based macOS/Linux pilot procedure.

## Build

```bash
uv build --wheel
cd desktop
npm ci
npm run build:macos
```

Expected target on Apple Silicon: `aarch64-apple-darwin`. To keep File
Provider from reattaching disallowed Finder metadata, the DMG is written below
`/private/tmp/pku-all-in-notion-target/release/bundle/dmg/` by default.

## Local acceptance

1. Mount the DMG and drag **PKU All in Notion** to Applications.
2. Launch it from Applications; it must open one native window, not Terminal
   and not a standalone browser tab.
3. Wait for the student UI. Confirm `~/PKU-All-in-Notion/panel.log` exists.
4. Launch the app a second time. It must focus the first window rather than
   start a second service.
5. Exercise product login and the Notion external-browser flow. OAuth must
   return to the local callback and the app window must remain on `/app`.
6. Quit the app and verify ports 8791–8793 are free and neither the sidecar nor
   bundled Python remains running.
7. Relaunch and confirm local session/data survive a normal quit and upgrade.

## Distribution gate

Local builds are ad-hoc signed. Public download requires a Developer ID
Application identity plus Apple notarization and stapling. Verify the final
artifact with:

```bash
codesign --verify --deep --strict --verbose=2 "/Applications/PKU All in Notion.app"
spctl --assess --type execute --verbose=2 "/Applications/PKU All in Notion.app"
xcrun stapler validate "/Applications/PKU All in Notion.app"
```
