# Windows desktop regression acceptance (2026-09-24)

Build host: Windows 11 Pro x64, build 26200. Inno Setup 6.7.3 at `D:\Inno Setup 6\ISCC.exe`.

## Commands

- `uv build --wheel`
- `cd desktop; npm run build`
- `powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\stage-inno-payload.ps1`
- `& 'D:\Inno Setup 6\ISCC.exe' installer\windows\pku-all-in-notion.iss`
- `.venv-windows-test\Scripts\python.exe -m pytest -q --basetemp .pytest-basetemp\windows-full-20260924`
- `powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\smoke-release.ps1`
- NSIS `/S` install, followed by the same smoke test with `-ReleaseDir "$env:LOCALAPPDATA\Programs\PKU All in Notion"`.

## Results

- Full test suite: 927 passed. Edge browser regression covers password confirmation, verification refresh, and Notion connection error/retry.
- NSIS: `desktop\src-tauri\target\release\bundle\nsis\PKU All in Notion_0.1.1_x64-setup.exe` (114.0 MiB).
- Inno: `dist\PKU-All-in-Notion-Setup-0.1.1.exe` (112.5 MiB).
- NSIS installed to `%LOCALAPPDATA%\Programs\PKU All in Notion`. Start Menu and Desktop shortcuts target `pku-desktop.exe`. Bundled `pku-sync.exe` and `resources\runtime\python.exe` are present.
- From the release tree and the installed directory, the desktop window appeared, `/app` returned 200, the sidecar and Python had no separate window, second launch reused the first instance, and close released port 8791 and ended the child process tree. `panel.log` exists under `%USERPROFILE%\PKU-All-in-Notion`.
- No `.env` file was found under the installation directory; the existing user data directory remains under `%USERPROFILE%\PKU-All-in-Notion`.

SmartScreen was not assessed because NSIS was installed silently. Inno was compiled but not installed. Live email delivery and an attended Notion OAuth approval require real external accounts and were not exercised; their local API and browser flows were covered with fakes.

## Notion preview follow-up

- Rebuilt the wheel offline with `uv build --wheel --offline`; set `UV_OFFLINE=1`
  and used the installed Rust toolchain at `C:\Program Files\Rust stable MSVC 1.98\bin`
  for `npm run build`.
- Rebuilt both installers: the NSIS setup under
  `desktop/src-tauri/target/release/bundle/nsis/` and the Inno setup at
  `dist/PKU-All-in-Notion-Setup-0.1.1.exe` using
  `D:\Inno Setup 6\ISCC.exe`.
- The Edge browser test opens a seeded Notion page through the injected system
  browser opener and confirms that the desktop page remains at `/app` with
  the dashboard selected. The bundled Python runtime imports the new endpoint.
- Regression: 925 passed with `tests/test_panel_ports.py` excluded because the
  user's already running desktop app occupies a panel port. Targeted Edge and
  external-navigation tests: 5 passed. Real Google account sign-in was not
  exercised.
- The currently running installed copy was not replaced during this build.

## Sidebar scroll follow-up

- Desktop shell now has a viewport-height grid with independent main-content
  and sidebar scroll areas. At widths below 900px, the existing single-column
  document scroll is preserved.
- Real Edge regression checks that scrolling long right-hand content leaves
  the sidebar at the same viewport position and does not scroll the document.
  At 800px it checks that the page itself scrolls. Student UI suite: 15 passed.
- Rebuilt the wheel, NSIS setup, and Inno setup on Windows. Verified the staged
  release runtime contains the new CSS rules.
