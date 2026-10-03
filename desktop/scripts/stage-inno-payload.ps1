# Stage Inno payload from a Tauri Windows release build.
# Prerequisites (on Windows):
#   cd desktop && npm run build
#   # produces target/release/*.exe + resources + NSIS under bundle/
#
# Then:
#   powershell -File desktop/scripts/stage-inno-payload.ps1
#   ISCC.exe installer\windows\pku-all-in-notion.iss

param(
    [string]$RepoRoot = "",
    [string]$ReleaseDir = ""
)

$ErrorActionPreference = "Stop"

function Resolve-RepoRoot {
    param([string]$Hint)
    if ($Hint) { return (Resolve-Path $Hint).Path }
    $here = $PSScriptRoot
    return (Resolve-Path (Join-Path $here "..\..")).Path
}

$RepoRoot = Resolve-RepoRoot -Hint $RepoRoot
$SrcTauri = Join-Path $RepoRoot "desktop\src-tauri"
if (-not $ReleaseDir) {
    $ReleaseDir = Join-Path $SrcTauri "target\release"
}
$Payload = Join-Path $RepoRoot "installer\windows\payload"

if (-not (Test-Path $ReleaseDir)) {
    throw "Release dir not found: $ReleaseDir — run npm run build in desktop/ first."
}

$preferred = @(
    (Join-Path $ReleaseDir "PKU All in Notion.exe"),
    (Join-Path $ReleaseDir "pku-desktop.exe")
)
$appExe = $null
foreach ($c in $preferred) {
    if (Test-Path $c) {
        $appExe = Get-Item $c
        break
    }
}
if (-not $appExe) {
    $appExe = Get-ChildItem -Path $ReleaseDir -Filter "*.exe" -File |
        Where-Object {
            $_.Name -ne "pku-sync.exe" -and
            $_.Name -ne "pku-sync-sidecar.exe"
        } |
        Select-Object -First 1
}
if (-not $appExe) {
    throw "Could not find Tauri app exe under $ReleaseDir"
}

$sidecar = Join-Path $ReleaseDir "pku-sync.exe"
if (-not (Test-Path $sidecar)) {
    throw "Missing bundled sidecar $sidecar — stage-sidecar must run during tauri build."
}

$resources = Join-Path $ReleaseDir "resources"
if (-not (Test-Path (Join-Path $resources "runtime\python.exe"))) {
    $resources = Join-Path $SrcTauri "resources"
}
if (-not (Test-Path (Join-Path $resources "runtime\python.exe"))) {
    throw "Missing bundled resources/runtime/python.exe — run npm run build first."
}

Write-Host "==> Staging Inno payload from $($appExe.FullName)"
if (Test-Path $Payload) {
    Remove-Item -Recurse -Force $Payload
}
New-Item -ItemType Directory -Force -Path $Payload | Out-Null

Copy-Item -Force $appExe.FullName (Join-Path $Payload "PKU All in Notion.exe")
Copy-Item -Force $sidecar (Join-Path $Payload "pku-sync.exe")
Copy-Item -Recurse -Force $resources (Join-Path $Payload "resources")

Set-Content -Path (Join-Path $Payload "PAYLOAD.txt") -Value @(
    "Staged from: $ReleaseDir"
    "App: $($appExe.Name)"
    "Time: $(Get-Date -Format o)"
) -Encoding UTF8

Write-Host "==> Payload ready: $Payload"
Write-Host "    Next: ISCC.exe installer\windows\pku-all-in-notion.iss"
