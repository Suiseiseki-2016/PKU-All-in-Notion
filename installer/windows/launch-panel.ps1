# PKU All in Notion - panel launcher (Start Menu / Desktop shortcut target).
#
# Per library/packaging-design.md: the launcher pins the working directory to
# the per-user app dir (%USERPROFILE%\PKU-All-in-Notion) so the CWD-first
# .env lookup (pku_sync.config) and the relative DATA_DIR land THERE - the
# install dir never holds secrets or data. The panel itself auto-opens the
# browser at the selected port (8791/8792/8793, loopback only), and every
# startup line (including the selected port) is appended to panel.log in the
# app dir so support has evidence without a terminal.
#
# The console window stays open while the panel runs; closing the window
# stops the panel (single-instance usage is documented in the pilot guide).
#
# NOTE: this file is UTF-8 with a leading BOM on purpose - Windows
# PowerShell 5.1 decodes BOM-less .ps1 files as ANSI and would corrupt the
# Chinese copy below.

param(
    # Overridable for local verification; defaults to the per-user app dir.
    [string]$AppDir = ""
)

$ErrorActionPreference = 'Continue'

if (-not $AppDir) {
    $AppDir = Join-Path $env:USERPROFILE 'PKU-All-in-Notion'
}
try {
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
} catch {
    Write-Host ("无法创建应用目录 " + $AppDir + "：" + $_)
    exit 1
}
$LogFile = Join-Path $AppDir 'panel.log'
$Host.UI.RawUI.WindowTitle = 'PKU All in Notion 面板'

# UTF-8 end to end: the child prints Chinese; panel.log must stay UTF-8 so
# support tooling and secret scans can read it (never cp936, never UTF-16).
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONUTF8 = '1'

# Locate the uv tool shim: uv's default executable dir (where both the uv
# installer and `uv tool install` put exes) first, then PATH.
$binDir = if ($env:UV_TOOL_BIN_DIR) { $env:UV_TOOL_BIN_DIR } else { Join-Path $env:USERPROFILE '.local\bin' }
$pkuSync = Join-Path $binDir 'pku-sync.exe'
if (-not (Test-Path $pkuSync)) {
    $cmd = Get-Command pku-sync -ErrorAction SilentlyContinue
    if ($cmd) { $pkuSync = $cmd.Source }
}
if (-not (Test-Path $pkuSync)) {
    $msg = '没有找到 pku-sync.exe：应用尚未安装完成，请重新运行安装程序。'
    Write-Host $msg
    Add-Content -Path $LogFile -Value ("[launcher] " + (Get-Date -Format s) + " " + $msg) -Encoding UTF8
    exit 1
}
# Keep uv resolvable for the in-app update path (`uv tool upgrade`) even when
# the session that started the shortcut predates the PATH update.
if ((Test-Path (Join-Path $binDir 'uv.exe')) -and ((($env:Path -split ';') -notcontains $binDir))) {
    $env:Path = $binDir + ';' + $env:Path
}

# Pin CWD to the per-user app dir BEFORE starting the panel: this is what
# keeps .env, DATA_DIR and update-pending.json out of the install dir.
Set-Location $AppDir
Add-Content -Path $LogFile -Value ("[launcher] " + (Get-Date -Format s) + " 启动面板，工作目录 " + $AppDir) -Encoding UTF8

# Stream panel output to the console AND panel.log. ErrorActionPreference
# stays 'Continue' so redirected stderr cannot abort the pipeline.
& $pkuSync panel 2>&1 | ForEach-Object {
    $line = "$_"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}
$exitCode = $LASTEXITCODE
Add-Content -Path $LogFile -Value ("[launcher] " + (Get-Date -Format s) + " 面板退出，退出码 " + $exitCode) -Encoding UTF8
exit $exitCode
