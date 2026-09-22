# PKU All in Notion - Windows uv bootstrap (wave 1)
#
# Runs inside the Inno Setup installer (pku-all-in-notion.iss, [Code]
# CurStepChanged ssPostInstall) and can also be run standalone from the
# installer bundle directory.
#
# What it does (per library/packaging-design.md, user-approved 2026-09-18):
#   1. installs the OFFICIAL standalone uv when missing
#      (https://astral.sh/uv/install.ps1);
#   2. provisions uv-managed Python 3.11 (python-build-standalone - a fresh
#      machine needs NO system Python);
#   3. installs the app as a uv tool BY PACKAGE NAME from the public PEP 503
#      package index (independent static path on aeoluswu.info, never the
#      relay), so the uv receipt records the name requirement plus that
#      index and a literal `uv tool upgrade pku-course-sync` (the in-app
#      autoupdate apply path) can resolve a newer published wheel. The
#      bundled release wheel is kept ONLY as an explicit --find-links
#      fallback when the public index is unreachable - the fallback still
#      installs by name, never as a wheel-path receipt (which would pin
#      upgrades to one file);
#   4. never writes secrets or data into the install dir: credentials live in
#      the per-user app dir (%USERPROFILE%\PKU-All-in-Notion, created here,
#      pinned by launch-panel.ps1).
#
# A PyPI mirror (e.g. TUNA) is honored through uv's own env vars
# (UV_DEFAULT_INDEX), set for this process by the installer's mirror task.
# The public package index is passed as an ADDITIONAL index (--index), so
# dependencies keep resolving from PyPI or the mirror while pku-course-sync
# itself comes from the public index (it is not published on PyPI). The
# installer (Inno [Registry]) additionally persists UV_DEFAULT_INDEX at
# the user level when the mirror task is selected; the receipt-retained
# public index keeps literal `uv tool upgrade` working alongside it.
#
# Exit codes: 0 ok; 2 fallback wheel missing when needed; 3 uv install
# failed; 4 Python provisioning failed; 5 tool install failed (bad index/
# mirror URL, or both the public index and the offline fallback failed);
# 6 pku-sync shim missing.
#
# NOTE: this file is UTF-8 with a leading BOM on purpose - Windows
# PowerShell 5.1 decodes BOM-less .ps1 files as ANSI and would corrupt the
# Chinese copy below.

param(
    # Public PEP 503 package index the app is installed from BY NAME. The
    # uv receipt retains this index so a literal `uv tool upgrade
    # pku-course-sync` resolves newer published wheels.
    [string]$Index = 'https://aeoluswu.info/packages/simple/',
    # Path to the bundled release wheel (offline fallback ONLY), or a
    # directory containing exactly one pku_course_sync-*-py3-none-any.whl
    # (default: the script's own bundle directory, which is where the Inno
    # installer unpacks it).
    [string]$Wheel = $PSScriptRoot,
    # Optional PyPI mirror URL for DEPENDENCIES, e.g. https://pypi.tuna.tsinghua.edu.cn/simple
    [string]$Mirror = "",
    [string]$PythonVersion = "3.11",
    # Per-user app dir (holds .env + data; never inside the install dir).
    [string]$AppDir = ""
)

$ErrorActionPreference = 'Continue'

if (-not $AppDir) {
    $AppDir = Join-Path $env:USERPROFILE 'PKU-All-in-Notion'
}
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$LogFile = Join-Path $AppDir 'install.log'

function Log([string]$Line) {
    Write-Host $Line
    Add-Content -Path $LogFile -Value $Line -Encoding UTF8
}

# Run one native command, streaming its output to the console and to
# install.log. ErrorActionPreference stays 'Continue' so redirected stderr
# from native tools (uv writes progress to stderr) cannot abort the script;
# failures are detected from $LASTEXITCODE instead.
function Invoke-Logged {
    param([string]$Exe, [string[]]$ArgList, [string]$What)
    Log ("==> " + $What)
    & $Exe @ArgList 2>&1 | ForEach-Object {
        $line = "$_"
        Write-Host $line
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
    }
    return $LASTEXITCODE
}

Log ("=== PKU All in Notion bootstrap " + (Get-Date -Format s) + " ===")
Log ("app dir: " + $AppDir)

# --- 1. locate the bundled wheel (offline fallback source) ------------------
# The wheel is NOT the normal install source anymore; a missing or ambiguous
# wheel only disables the offline fallback and is logged, never fatal for the
# index-backed normal path below.
$WheelPath = $null
if (Test-Path $Wheel -PathType Leaf) {
    $WheelPath = (Resolve-Path $Wheel).Path
} elseif (Test-Path $Wheel -PathType Container) {
    $candidates = @(Get-ChildItem -Path $Wheel -Filter 'pku_course_sync-*-py3-none-any.whl' |
        Sort-Object Name)
    if ($candidates.Count -eq 1) {
        $WheelPath = $candidates[0].FullName
    } elseif ($candidates.Count -gt 1) {
        Log "bootstrap: 安装目录里找到多个 wheel，离线回退不可用（不影响公共软件源安装）："
        foreach ($c in $candidates) { Log ("  " + $c.FullName) }
    }
}
if (-not $WheelPath) {
    Log "bootstrap: 没有找到内置离线安装包（pku_course_sync-*-py3-none-any.whl）；公共软件源不可用时将无法回退。"
} else {
    Log ("offline fallback wheel: " + $WheelPath)
}

# --- 1b. validate the public package index ---------------------------------
if ($Index -notmatch '^https?://') {
    Log ("bootstrap: 公共软件源地址必须是 http(s):// 开头：" + $Index)
    exit 5
}
if (-not $Index.EndsWith('/')) { $Index = $Index + '/' }
Log ("package index: " + $Index)

# --- 2. locate or install the official standalone uv ----------------------
$uvBinDir = Join-Path $env:USERPROFILE '.local\bin'
$uvExe = $null
$cmd = Get-Command uv -ErrorAction SilentlyContinue
if ($cmd) { $uvExe = $cmd.Source }
if (-not $uvExe) {
    $candidate = Join-Path $uvBinDir 'uv.exe'
    if (Test-Path $candidate) { $uvExe = $candidate }
}
if (-not $uvExe) {
    Log "bootstrap: 正在安装官方 uv（https://docs.astral.sh/uv/）…"
    # Official standalone installer. uv itself is downloaded from GitHub
    # releases, so the PyPI mirror does not apply to this step.
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" 2>&1 |
        ForEach-Object {
            $line = "$_"
            Write-Host $line
            Add-Content -Path $LogFile -Value $line -Encoding UTF8
        }
    if ($LASTEXITCODE -ne 0) {
        Log ("bootstrap: uv 安装失败（退出码 " + $LASTEXITCODE + "）。")
        exit 3
    }
    $candidate = Join-Path $uvBinDir 'uv.exe'
    if (Test-Path $candidate) { $uvExe = $candidate }
}
if (-not $uvExe) {
    Log "bootstrap: uv 安装后仍未找到 uv.exe。"
    exit 3
}
Log ("uv: " + $uvExe)
# The installer's PATH update only reaches NEW processes; this session needs
# it explicitly.
if ((-not ($env:Path -split ';') -contains $uvBinDir) -and (Test-Path $uvBinDir)) {
    $env:Path = $uvBinDir + ';' + $env:Path
}
$null = Invoke-Logged -Exe $uvExe -ArgList @('--version') -What 'uv --version'

# --- 3. optional PyPI mirror (uv env vars, this process) -------------------
if ($Mirror) {
    if ($Mirror -notmatch '^https?://') {
        Log ("bootstrap: 镜像地址必须是 http(s):// 开头：" + $Mirror)
        exit 5
    }
    $env:UV_DEFAULT_INDEX = $Mirror
    Log ("mirror (UV_DEFAULT_INDEX): " + $Mirror)
}

# --- 4. provision uv-managed Python (python-build-standalone) -------------
$rc = Invoke-Logged -Exe $uvExe -ArgList @('python', 'install', $PythonVersion) -What ("provisioning Python " + $PythonVersion + "（uv 管理的解释器，无需系统 Python）")
if ($rc -ne 0) {
    Log ("bootstrap: Python " + $PythonVersion + " 安装失败（退出码 " + $rc + "）。")
    exit 4
}

# --- 5. install / update the app as a uv tool (index-backed, by name) -------
# `--index` adds the public package index on top of the default index (PyPI,
# or the mirror set above), so dependencies resolve from there while
# pku-course-sync itself comes from the public index (it is not on PyPI).
# The receipt records the name requirement plus this index; a literal
# `uv tool upgrade pku-course-sync` therefore resolves newer published
# wheels - including for mirror users (a user-level UV_DEFAULT_INDEX only
# replaces the default slot, and pku-course-sync is not on PyPI/mirrors).
# --force: re-running the installer (the wave-1 update path) replaces the
# tool env in place, moving to the newest published version. The per-user
# app dir (.env + data) is never touched.
$rc = Invoke-Logged -Exe $uvExe -ArgList @(
        'tool', 'install', '--force', '--managed-python',
        '--python', $PythonVersion, '--index', $Index, 'pku-course-sync'
    ) -What ('uv tool install pku-course-sync（公共软件源 ' + $Index + '；依赖下载可能需要几分钟）')
if ($rc -ne 0) {
    # --- 5b. offline fallback: bundled release wheel via --find-links --------
    # The bundled wheel is ONLY an explicit fallback for an unreachable or
    # not-yet-published public index. The requirement stays BY NAME (never a
    # wheel-path receipt, which pins upgrades to one file): uv's --find-links
    # takes a directory, so a wheel file resolves to its parent directory
    # (the Inno bundle dir holds exactly one wheel).
    if (-not $WheelPath) {
        Log ("bootstrap: 公共软件源安装失败（退出码 " + $rc + "），且没有内置离线安装包可回退。")
        exit 2
    }
    Log ("bootstrap: 公共软件源安装失败（退出码 " + $rc + "），改用内置离线安装包（--find-links）…")
    $fallbackLinks = if (Test-Path $WheelPath -PathType Leaf) {
        Split-Path -Parent $WheelPath
    } else {
        $WheelPath
    }
    $rc = Invoke-Logged -Exe $uvExe -ArgList @(
            'tool', 'install', '--force', '--managed-python',
            '--python', $PythonVersion, '--find-links', $fallbackLinks, 'pku-course-sync'
        ) -What 'uv tool install pku-course-sync（离线回退；依赖仍需从默认源下载）'
    if ($rc -ne 0) {
        Log ("bootstrap: 应用安装失败（公共软件源与离线回退均未成功；退出码 " + $rc + "）。")
        exit 5
    }
    Log "bootstrap: 已用内置离线安装包完成安装（公共软件源当前不可用；部署后可用 uv tool upgrade pku-course-sync --index 切换到公共软件源）。"
}

# --- 6. verify the entry-point shim ----------------------------------------
$toolBinDir = if ($env:UV_TOOL_BIN_DIR) { $env:UV_TOOL_BIN_DIR } else { $uvBinDir }
$shim = Join-Path $toolBinDir 'pku-sync.exe'
if (-not (Test-Path $shim)) {
    Log ("bootstrap: 安装完成但没有找到 pku-sync.exe（期望位置 " + $shim + "）。")
    exit 6
}
$null = Invoke-Logged -Exe $uvExe -ArgList @('tool', 'list') -What 'uv tool list'
Log ("bootstrap 完成。pku-sync: " + $shim)
Log "bootstrap: 凭据与数据保存在用户目录（PKU-All-in-Notion），安装目录不保存任何个人数据。"
exit 0
