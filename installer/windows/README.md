# Windows installer (desktop product path)

One **setup.exe** installs the Tauri desktop shell plus a bundled relocatable
Python runtime (`pku-sync` sidecar). Start Menu / Desktop shortcuts launch the
**Tauri exe** (system WebView window → student `/app`). Users do not install
Python/uv and do not open a browser to use the panel.

Per-user data stays in `%USERPROFILE%\PKU-All-in-Notion` (`.env`, data, logs).
The install directory under `%LOCALAPPDATA%\Programs\PKU All in Notion` holds
only the app + runtime.

## Artifacts

| Artifact | How |
| --- | --- |
| Tauri NSIS (preferred fast path) | `cd desktop && npm run build` → `src-tauri\target\release\bundle\nsis\*-setup.exe` |
| Inno Setup (Chinese wizard + explicit data-dir uninstall note) | After Tauri build: `stage-inno-payload.ps1` then `ISCC.exe pku-all-in-notion.iss` → `dist\PKU-All-in-Notion-Setup-<ver>.exe` |

Both ship the same binaries: app exe, `pku-sync.exe` sidecar, `resources\runtime\`.

## Layout

| File | Role |
| --- | --- |
| `pku-all-in-notion.iss` | Inno Setup 6 script (compile with `ISCC.exe`) |
| `payload\` | Staged Tauri release tree (gitignored; created by stage script) |
| `bootstrap.ps1` / `launch-panel.ps1` | **Legacy pilot** (uv tool + browser). Not used by the current `.iss`. |

## Build (release engineer, Windows)

```powershell
# 0. Optional: wheel for a pinned install into the sidecar runtime
uv build

# 1. Stage sidecar + runtime, build Tauri (NSIS)
cd desktop
npm install
npm run build

# 2a. Ship Tauri NSIS directly, or…
# 2b. Wrap with Inno:
powershell -NoProfile -ExecutionPolicy Bypass -File ..\desktop\scripts\stage-inno-payload.ps1
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" ..\installer\windows\pku-all-in-notion.iss
```

Unsigned builds will hit SmartScreen until code signing is wired (post-pilot).

## WebView2

Tauri NSIS can bootstrap Evergreen WebView2. The Inno-wrapped payload assumes
WebView2 is available (most Windows 10/11 machines) or that the user installs
it when prompted.

## Update / uninstall

- Re-run a newer setup.exe to replace the install dir; the per-user app dir is
  never touched.
- Uninstall removes the program files only; the uninstaller states that
  `%USERPROFILE%\PKU-All-in-Notion` is retained.
- In-app `uv tool upgrade` autoupdate from the pilot path does **not** apply to
  the sidecar bundle; product auto-update is a follow-up (Tauri updater /
  reinstall).

## macOS

DMG / notarization is documented as follow-up in `desktop/README.md`. Same
Tauri shell; different bundle + signing.
