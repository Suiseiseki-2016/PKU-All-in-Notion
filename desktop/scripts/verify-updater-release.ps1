param(
    [string]$ExpectedVersion = "",
    [string]$ManifestPath = "",
    [string]$E2EReport = ""
)
$ErrorActionPreference = "Stop"
$desktop = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repo = (Resolve-Path (Join-Path $desktop "..")).Path
if (-not $ExpectedVersion) {
    $ExpectedVersion = (Get-Content -LiteralPath (Join-Path $desktop "package.json") -Raw | ConvertFrom-Json).version
}
if (-not $ManifestPath) {
    $ManifestPath = Join-Path $repo "dist\latest-$ExpectedVersion.json"
}
$signed = Join-Path $desktop "src-tauri\target\release\bundle\nsis\PKU All in One_$($ExpectedVersion)_x64-setup.exe"
$published = Join-Path $repo "dist\PKU-All-in-One-Windows-$ExpectedVersion.exe"
$sig = "$signed.sig"
foreach ($file in @($ManifestPath, $signed, $published, $sig)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing release file: $file" }
}
if (-not $E2EReport) { throw "Product E2E report is mandatory before updater publication; no E2E bypass." }
$python = Join-Path $desktop "src-tauri\target\release\resources\runtime\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Missing bundled Python for product E2E gate" }
& $python (Join-Path $repo "scripts\product_e2e_gate.py") --root $repo --report $E2EReport --artifact $published --target windows --version $ExpectedVersion
if ($LASTEXITCODE -ne 0) { throw "Product E2E gate rejected updater publication." }
$env:PATH = (Join-Path $env:USERPROFILE ".cargo\bin") + ";" + $env:PATH
$env:CARGO_NET_OFFLINE = "true"
& cargo run --release --offline --manifest-path (Join-Path $desktop "tools\updater-verify\Cargo.toml") -- $ManifestPath (Join-Path $desktop "src-tauri\tauri.conf.json") $signed $published $sig $ExpectedVersion
if ($LASTEXITCODE -ne 0) { throw "Updater release verification failed" }
