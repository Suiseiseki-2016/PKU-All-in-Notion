# PKU All in Notion — desktop shell

Tauri 2 thin shell: **download one installer → install → double-click → desktop window**.

Production ships a **bundled relocatable Python runtime** + thin `pku-sync` sidecar
(`externalBin`). Dev mode (`tauri dev`) stages a stub sidecar and uses PATH
`pku-sync`.

## Runtime flow

1. Resolve panel binary (bundled sidecar / bundled `python -m pku_sync` / PATH)
2. CWD = `%USERPROFILE%\PKU-All-in-Notion` (or `~/PKU-All-in-Notion`)
3. Spawn `panel --no-browser`
4. Poll `GET http://127.0.0.1:8791|8792|8793/healthz` (~30s)
5. Navigate the system WebView to `/app`
6. Single-instance lock; kill the panel process tree on window close
7. Append launcher lines to `panel.log` in the app dir

## Prerequisites

| Tool | Notes |
| --- | --- |
| Rust (stable) | `rustup` / `cargo` |
| Node.js + npm | For `@tauri-apps/cli` |
| uv | Stages the private Python runtime on Windows and macOS release builds |
| WebView2 | **Windows** — Evergreen Runtime (NSIS can bootstrap) |
| `pku-sync` on PATH | **Dev only** (`tauri dev` fallback) |

**First-party acceptance is Windows.** WSL/Linux is useful for editing and for
`cargo build --bin pku-sync-sidecar`, but often lacks WebView2 / GTK–WebKit
`-dev` packages; do not treat WSL `tauri build` failure as a product blocker.

macOS release builds run on an Apple Silicon Mac and produce a native DMG.

## Develop (PATH `pku-sync`)

```bash
cd desktop
npm install
# optional explicit stub: npm run stage-sidecar:dev
npm run tauri dev
```

`beforeDevCommand` runs `stage-sidecar:dev` (builds the thin sidecar; no Python
tree). The shell then finds `pku-sync` via `UV_TOOL_BIN_DIR` → `~/.local/bin` →
`PATH`.

### Windows WebView check

```bat
cd desktop
npm install
npm run tauri dev
```

Expect loading copy 「正在启动本机服务…」, then `/app` inside the window; second
launch focuses the existing window; close frees ports `8791–8793`.

## Build on Windows (release)

```powershell
# Optional: produce dist\pku_course_sync-*-py3-none-any.whl for a pinned runtime install
uv build

cd desktop
npm install
npm run build
```

`beforeBuildCommand` runs `stage-sidecar`, which:

1. `cargo build --release --bin pku-sync-sidecar` → `src-tauri/binaries/pku-sync-x86_64-pc-windows-msvc.exe`
2. Reuse or install a uv-managed Python 3.11, then copy the complete interpreter into `src-tauri/resources/runtime`
3. Build a wheel if needed and install it with its dependencies into that private runtime

Tauri then embeds `externalBin` + `resources/runtime` and produces:

- **NSIS setup (primary):** `src-tauri\target\release\bundle\nsis\*-setup.exe`
- **Updater signature:** matching `*.exe.sig` in the same directory
- Unpacked exe + sidecar + resources under `src-tauri\target\release\`

### On-demand recording notes

From **总览**, open a course and select **课堂录像**. Save teaching-network credentials locally, sync the recording index, choose the matching teaching-network course once, then select one recording and a ready lecture. Browsing courses and recording metadata does not download media. Download, transcription, notes, and Notion publishing start only after the student confirms the selected recording. Only that recording is downloaded, transcribed, summarized and published. Daily/automate actions sync metadata but skip media by default; CLI operators must pass `--with-recordings` for batch media work.

### Campus-first learning home

The student dashboard uses Teaching Network course, recording, and attachment-name metadata as its starting point. Syncing the catalog does not download videos or attachments. Recordings define the lesson sequence; slides are linked only when their lecture number matches exactly one recording. Uncertain slides stay at course level for review. A recording is downloaded, transcribed, and published only after its own confirmation.

For the shortest first-run Notion flow, configure the public Notion connection with an optional **blank learning-home template** in the Notion Developer portal. In the OAuth prompt the student can duplicate that template; Notion shares the new page with the connection and returns `duplicated_template_id`, which the desktop app adopts as the learning home. Standard OAuth instead requires the student to share a page; the desktop app then lets them select that authorized page and creates a learning home beneath it. Notion's standard public OAuth does not grant general workspace-wide page access.
### Automatic updates

For SSH development, missing updater signing keys during release builds, and installed-client troubleshooting, see [Windows remote build and updater guide](../docs/WINDOWS_REMOTE_BUILD_AND_UPDATER.md).

Release builds check `https://pku.aeoluswu.info/updates/{{target}}/{{arch}}/{{current_version}}` after the desktop window starts. If a newer signed release exists, the app asks whether to download and install it. The server responds with HTTP 204 when no newer release is available. Windows installs through the signed NSIS updater; user data under `%USERPROFILE%\PKU-All-in-Notion` is separate from the installation directory.

The Tauri updater signing private key is stored only on the Windows build host at `%LOCALAPPDATA%\PKU-Admin\tauri-updater.key`; its password is stored with Windows DPAPI at `%LOCALAPPDATA%\PKU-Admin\tauri-updater-password.dpapi`. Back up the key and its password securely before relying on future releases. The public key is in `src-tauri/tauri.conf.json`. Every future Windows build must use the same private key, set `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, and publish the NSIS `.exe` with its matching `.sig` through the release manifest on the server. A lost key means installed clients cannot verify future automatic updates.

On the original Windows signing host, run `powershell -NoProfile -ExecutionPolicy Bypass -File desktop/scripts/build-signed-windows.ps1` from the repository root. The script reads gitignored `desktop/.env` when present; on this host, it contains only paths to the existing key and DPAPI-protected password. Without that file, the script locates the current Windows user's registered profile even when an SSH session has a different `%LOCALAPPDATA%`. Tauri does not load signing variables from `.env` automatically; this wrapper passes them only to the build process.

The release manifest's `signature` must be the **exact text in the generated `.sig` file, with only its trailing newline removed**. Do not Base64-encode that text again. Before publishing `dist/latest-<version>.json`, run `powershell -NoProfile -ExecutionPolicy Bypass -File desktop/scripts/verify-updater-release.ps1 -ExpectedVersion <version>` from the repository root. This checks the manifest, published installer bytes, embedded public key, cryptographic signature, and signed version; publication must stop if it fails.

The first updater-enabled Windows version is **0.1.2**. Earlier installations need one manual install of 0.1.2 from the download page. The Mac DMG can be offered for manual download; Mac automatic updates additionally require the matching signed `.app.tar.gz` and `.app.tar.gz.sig`, built with this same key. Do not publish a Mac update entry based on a DMG alone.

### Optional Inno wrapper (Chinese wizard)

```powershell
powershell -File desktop\scripts\stage-inno-payload.ps1
& "D:\Inno Setup 6\ISCC.exe" installer\windows\pku-all-in-notion.iss  # path on this build machine
```

Output: `dist\PKU-All-in-Notion-Setup-<version>.exe`

See `installer/windows/README.md`.

## Build on macOS (Apple Silicon)

Prerequisites: Xcode Command Line Tools, Rust, Node.js/npm and uv.

```bash
uv build --wheel
cd desktop
npm ci
npm run build:macos
```

The build stages a complete private Python 3.11 runtime, then bundles it with
the Tauri window and sidecar. The build target defaults to
`/private/tmp/pku-all-in-notion-target` to avoid iCloud/Finder metadata
breaking code signing. Set `PKU_MACOS_TARGET_DIR` for another local volume.

Local DMGs use ad-hoc signing. Public distribution requires Developer ID
Application signing and Apple notarization. See
`docs/MACOS_DESKTOP_ACCEPTANCE.md` for the manual acceptance checklist.
The DMG is for manual installation; the updater also needs a signed
`.app.tar.gz` and `.app.tar.gz.sig` before advertising Mac updates.

## What the user double-clicks

1. **Install:** `PKU-All-in-Notion-Setup-*.exe` (NSIS or Inno)
2. **Daily use:** Start Menu / Desktop **「PKU All in Notion」** → Tauri exe
   (not PowerShell, not a browser window)

Data/credentials remain under `%USERPROFILE%\PKU-All-in-Notion`.

## Layout

| Path | Role |
| --- | --- |
| `src/` | Loading / error UI before `/app` |
| `src-tauri/` | Spawn, healthz, navigate, single-instance, cleanup |
| `sidecar-launcher/` | Standalone Rust crate → `externalBin` `pku-sync` (no Tauri/GTK deps) |
| `src-tauri/binaries/` | Staged `pku-sync-<triple>.exe` (`externalBin`) |
| `src-tauri/resources/runtime/` | Staged relocatable Python + `pku-sync` package |
| `scripts/stage-sidecar.*` | Build-host staging for sidecar + runtime |
| `scripts/stage-inno-payload.ps1` | Copy release tree into `installer/windows/payload` |

## Remaining gaps (follow-ups)

| Gap | Notes |
| --- | --- |
| Code signing | Unsigned setup → SmartScreen; Authenticode not wired |
| macOS DMG | Apple Silicon build and DMG work; Developer ID signing and notarization remain |
| Auto-update | Signed Windows updater is enabled in 0.1.2; Mac updater artifacts pending |
| WSL GUI build | No WebView2 / often missing GTK WebKit deps |

## Out of scope (other milestones)

- Admin subdomain / Cloudflare Access
- Server / Resend changes
- Changing OS-agnostic rules inside `pku_sync/` beyond `python -m pku_sync` (`__main__.py`)


## Windows 发布验收

每次准备新安装包时，在 Windows x64 构建机按顺序运行：

```powershell
uv sync --all-extras
uv run python -m compileall -q pku_sync
uv run pytest -q
uv build --wheel
cd desktop
npm ci
npm run build
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-windows-release.ps1
```

全量测试包括账号与邮箱验证、Notion 授权与外部打开、教学网课程与课件索引、按需下载转写和写入 Notion、练习与扣点、兑换码、侧栏滚动和页面交互。Windows 构建验证会检查 NSIS 安装包、sidecar、内嵌 Python、包内实际导入位置以及版本一致性。运行端口测试前应关闭已安装的桌面应用，以便测试独占 8791–8793。

安装包构建后仍需在真实 Windows 桌面做一次安装验收：启动窗口进入 `/app`、第二次启动聚焦已有窗口、退出后端口释放、升级保留 `%USERPROFILE%\PKU-All-in-Notion`。外部教学网、Notion 和支付/兑换码服务的真实账号检查应使用专门测试账号，不能由无凭据的 CI 假数据替代。
