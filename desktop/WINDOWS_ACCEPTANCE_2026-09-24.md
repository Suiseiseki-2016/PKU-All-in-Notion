# Windows desktop acceptance — 2026-09-24

- Build machine: Windows 11 Pro x64, version 10.0.26200; Rust MSVC 1.98.1, Node/npm, uv, WebView2, Inno Setup 6.7.3.
- Build: `uv build --wheel --offline`; `cd desktop && npm install && npm run build` (with `UV_OFFLINE=1`, `CARGO_NET_OFFLINE=true` and the local Rust binary directory on `PATH`). The NSIS tool cache was primed from the installed NSIS and Tauri's SHA1-verified plugin because the CLI download timed out.
- NSIS: `src-tauri/target/release/bundle/nsis/PKU All in Notion_0.1.1_x64-setup.exe` (118,553,098 bytes).
- Release tree: `src-tauri/target/release/pku-desktop.exe`, `pku-sync.exe`, and `resources/runtime/python.exe` with package dependencies.
- Inno: `powershell -File desktop/scripts/stage-inno-payload.ps1`, then `D:\Inno Setup 6\ISCC.exe /Q installer\windows\pku-all-in-notion.iss`; output `dist/PKU-All-in-Notion-Setup-0.1.1.exe` (116,536,199 bytes). Inno installation itself was not exercised.
- NSIS install, reinstall, and uninstall were exercised with silent `/S` on the Windows host. Default clean install path: `%LOCALAPPDATA%\Programs\PKU All in Notion`. Start Menu and Desktop shortcuts target its `pku-desktop.exe`.
- Launched from the Start Menu shortcut: a Tauri window titled `PKU All in Notion` displayed the student login page. Launcher log recorded navigation to `http://127.0.0.1:8791/app`; `/app` and `/healthz` returned HTTP 200. A window screenshot is `acceptance-window.png`.
- Second launch left one Tauri process and one sidecar. Closing the window ended the sidecar and bundled Python processes; ports 8791–8793 were free.
- `%USERPROFILE%\PKU-All-in-Notion\panel.log` contains launcher entries. No `.env`, `.db`, or `.sqlite` files were found in the install tree. Reinstall kept the user directory and its file count. Final NSIS uninstall removed the install directory and kept the user directory; the package was then reinstalled.
- SmartScreen was not observed because installation was silent. The transient startup message was not captured visually; the launcher log and final window state were verified.
