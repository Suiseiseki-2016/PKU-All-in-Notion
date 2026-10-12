param([string]$ExpectedVersion = "", [string]$E2EReport = "")
$ErrorActionPreference = "Stop"
if (-not [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([System.Runtime.InteropServices.OSPlatform]::Windows)) { throw "Windows release verification must run on Windows." }
$desktop = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $ExpectedVersion) { $ExpectedVersion = (Get-Content (Join-Path $desktop "package.json") -Raw | ConvertFrom-Json).version }
$release = Join-Path $desktop "src-tauri\target\release"
$app = Join-Path $release "pku-desktop.exe"
$sidecar = Join-Path $release "pku-sync.exe"
$runtime = Join-Path $release "resources\runtime"
$python = Join-Path $runtime "python.exe"
foreach ($path in @($app, $sidecar, $python, (Join-Path $runtime ".bundle-ok"))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing release file: $path" }
}
$repo = (Resolve-Path (Join-Path $desktop "..")).Path
$bundleLock = Join-Path $runtime '.bundle-lock.sha256'
$bundleRequirements = Join-Path $runtime '.bundle-requirements.txt'
foreach ($path in @($bundleLock, $bundleRequirements)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing locked runtime dependency evidence: $path" }
}
$lockAlgorithm = [System.Security.Cryptography.SHA256]::Create()
try { $expectedLock = -join ($lockAlgorithm.ComputeHash([System.IO.File]::ReadAllBytes((Join-Path $repo 'uv.lock'))) | ForEach-Object { $_.ToString('x2') }) }
finally { $lockAlgorithm.Dispose() }
if ((Get-Content -LiteralPath $bundleLock -Raw).Trim() -ne $expectedLock) { throw "Bundled dependencies were staged from a different lockfile." }
$installer = Get-ChildItem -LiteralPath (Join-Path $release "bundle\nsis") -Filter "*_$($ExpectedVersion)_x64-setup.exe" -File | Select-Object -First 1
if (-not $installer -or $installer.Length -lt 1000000) { throw "Missing or incomplete NSIS installer for $ExpectedVersion" }
$installedMetadata = Get-ChildItem -LiteralPath (Join-Path $runtime "Lib\site-packages") -Filter "pku_course_sync-*.dist-info" -Directory
if ($installedMetadata.Count -ne 1 -or $installedMetadata[0].Name -ne "pku_course_sync-$ExpectedVersion.dist-info") { throw "Bundled Python package version does not match $ExpectedVersion" }
Push-Location $release
try { $module = (& $python -c 'import pku_sync; print(pku_sync.__file__)' | Select-Object -Last 1) }
finally { Pop-Location }
if ($LASTEXITCODE -ne 0 -or -not $module) { throw "Bundled Python cannot import pku_sync" }
$module = [System.IO.Path]::GetFullPath($module)
$root = [System.IO.Path]::GetFullPath($runtime) + [System.IO.Path]::DirectorySeparatorChar
if (-not $module.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Bundled package points outside the runtime: $module" }
if (-not $E2EReport) { throw "Product E2E report is mandatory. Build a candidate, run the installed-native suite, then verify release. No E2E bypass." }
& $python (Join-Path $repo "scripts\product_e2e_gate.py") --root $repo --report $E2EReport --artifact $installer.FullName --target windows --version $ExpectedVersion
if ($LASTEXITCODE -ne 0) { throw "Product E2E release gate failed; this installer remains an unaccepted candidate." }
[pscustomobject]@{ Version = $ExpectedVersion; Installer = $installer.FullName; InstallerBytes = $installer.Length; App = $app; Sidecar = $sidecar; Runtime = $python; PackageModule = $module } | Format-List
