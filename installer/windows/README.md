# Windows installer (wave 1)

Inno Setup package for the PKU All in Notion student client, per the
user-approved packaging design (`library/packaging-design.md`, mission M5):

- the `.exe` **wraps the uv bootstrap** (`bootstrap.ps1`): installs the
  official standalone uv when missing, provisions uv-managed Python 3.11
  (python-build-standalone — a fresh machine needs **no system Python**),
  and installs the app as a **uv tool** from the bundled release wheel;
- per-user install (`PrivilegesRequired=lowest`, `{localappdata}\Programs`) —
  no admin prompt, nothing outside the user profile;
- Start Menu / Desktop shortcuts start the panel via `launch-panel.ps1`,
  which pins CWD to the per-user app dir (`%USERPROFILE%\PKU-All-in-Notion`),
  lets the panel auto-open the browser at the selected port
  (8791/8792/8793, loopback only), and appends every startup line —
  including the selected port — to `panel.log` in the app dir;
- a PyPI mirror (TUNA) is supported through uv's own env vars: the
  `tunamirror` task passes `-Mirror` to `bootstrap.ps1` for the install and
  persists `UV_DEFAULT_INDEX` at the user level (HKCU, removed at uninstall)
  so the in-app autoupdate's `uv tool upgrade` also resolves through it.

## Layout

| File | Role |
|---|---|
| `pku-all-in-notion.iss` | Inno Setup 6 script (compile with `ISCC.exe`) |
| `bootstrap.ps1` | uv bootstrap — runs as a **required** install step; nonzero exit aborts the install |
| `launch-panel.ps1` | panel launcher — shortcut target; CWD pinned to the app dir |

`bootstrap.ps1` also runs standalone:
`powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap.ps1 [-Wheel <wheel-or-dir>] [-Mirror <url>] [-PythonVersion 3.11] [-AppDir <dir>]`.
Exit codes: 0 ok; 2 wheel not found; 3 uv install failed; 4 Python
provisioning failed; 5 tool install failed; 6 shim missing. It writes a
transcript to `<AppDir>\install.log` (default app dir:
`%USERPROFILE%\PKU-All-in-Notion`).

## Build (release engineer)

```powershell
# 1. wheel into dist\ (version from pyproject.toml)
uv build

# 2. compile the installer
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\windows\pku-all-in-notion.iss
# or:  ISCC.exe /DMyAppVersion=x.y.z installer\windows\pku-all-in-notion.iss
```

Output: `dist\PKU-All-in-Notion-Setup-<version>.exe` — **unsigned**; the
SmartScreen prompt it triggers is captured honestly in the pilot evidence
and documented for students (design decision, wave 1; signing is post-pilot).

**Known build-tool gap (recorded 2026-09-22):** `ISCC.exe` is not installed
on the development host. Every non-compile leg is verified locally (wheel
build, bootstrap, launcher, mirror); compiling the `.exe` requires Inno
Setup 6 on the build machine.

## China-network notes (measured on the mission host)

- The PyPI mirror (`UV_DEFAULT_INDEX`) accelerates the dependency download
  (the heaviest part of the install: faster-whisper/opencv/PyAV/onnxruntime
  stack ≈ 0.5–1 GB). Expected duration is recorded in the feature evidence
  pack under the mission's `validation\m5-packaging-pilot\` directory.
- `uv` itself and the Python 3.11 interpreter are downloaded from GitHub
  releases (astral-sh/uv, python-build-standalone), **not** PyPI, so the
  mirror does not apply to those two steps. `UV_PYTHON_INSTALL_MIRROR` can
  redirect interpreter downloads if ever needed (not set by this installer,
  out of the approved wave-1 scope).

## Update and uninstall semantics

- Running a newer version's installer over an existing install is the
  wave-1 update path: `uv tool install --force` replaces the tool env; the
  per-user app dir (`.env`, data, logs) is never touched.
- Uninstall removes the uv tool (best effort) and the install dir, never
  the per-user app dir; the uninstaller says so explicitly. The persisted
  `UV_DEFAULT_INDEX` value is removed with it.

## Deliberately out of scope (wave 1)

- Branded shortcut icon (shortcuts currently show the PowerShell icon).
- Signed/notarized installers, frozen binaries, native launcher binaries.
- A launcher-level single-instance guard (single-instance usage is
  documented in the pilot guide instead).
