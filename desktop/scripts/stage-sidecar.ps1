# Stage Windows sidecar for Tauri bundle:
#   1) standalone uv-managed Python + pku-course-sync under src-tauri/resources/runtime
#   2) thin Rust launcher -> src-tauri/binaries/pku-sync-<triple>.exe
#
# Run from repo (or via npm run stage-sidecar in desktop/).
# Requires: uv, rustc/cargo, network (first Python + deps download is large).

param(
    [string]$PythonVersion = "3.11",
    [string]$RepoRoot = "",
    [switch]$SkipRuntime,
    [switch]$DevStubOnly
)

$ErrorActionPreference = "Stop"

function Resolve-RepoRoot {
    param([string]$Hint)
    if ($Hint) { return (Resolve-Path $Hint).Path }
    if ($env:PKU_REPO_ROOT) { return (Resolve-Path $env:PKU_REPO_ROOT).Path }
    $here = $PSScriptRoot
    # desktop/scripts -> repo root
    return (Resolve-Path (Join-Path $here "..\..")).Path
}

$RepoRoot = Resolve-RepoRoot -Hint $RepoRoot
$Desktop = Join-Path $RepoRoot "desktop"
$SrcTauri = Join-Path $Desktop "src-tauri"
$RuntimeDir = Join-Path $SrcTauri "resources\runtime"
$BinariesDir = Join-Path $SrcTauri "binaries"

Write-Host "==> Repo: $RepoRoot"
New-Item -ItemType Directory -Force -Path $BinariesDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $SrcTauri "resources") | Out-Null

$triple = $env:TAURI_ENV_TARGET_TRIPLE
if (-not $triple) {
    # Default Windows desktop triple for this product path.
    $triple = "x86_64-pc-windows-msvc"
}
$sidecarName = "pku-sync-$triple.exe"
$sidecarDest = Join-Path $BinariesDir $sidecarName

function Build-Sidecar {
    param([switch]$Release)
    $profile = if ($Release) { "release" } else { "debug" }
    $launcher = Join-Path $Desktop "sidecar-launcher"
    Write-Host "==> Building pku-sync-sidecar ($triple, $profile) from $launcher"
    Push-Location $launcher
    try {
        if ($Release) {
            cargo build --release
        }
        else {
            cargo build
        }
        if ($LASTEXITCODE -ne 0) { throw "cargo build pku-sync-sidecar failed ($LASTEXITCODE)" }
        $built = Join-Path $launcher "target\$profile\pku-sync-sidecar.exe"
        if (-not (Test-Path $built)) {
            throw "missing $built"
        }
        Copy-Item -Force $built $sidecarDest
        Write-Host "    staged $sidecarDest"
    }
    finally {
        Pop-Location
    }
}

function Stage-DevStub {
    # Sidecar binary alone is enough for tauri conf; without resources/runtime
    # the shell falls back to PATH pku-sync (see lib.rs / sidecar main).
    Build-Sidecar
    if (Test-Path $RuntimeDir) {
        Write-Host "==> Dev stub: leaving existing runtime at $RuntimeDir"
    }
    else {
        New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
        Set-Content -Path (Join-Path $RuntimeDir "README.txt") -Value @(
            "Dev stub: no portable Python here."
            "tauri dev uses PATH pku-sync; production builds run stage-sidecar.ps1 without -DevStubOnly."
        ) -Encoding UTF8
    }
}

function Stage-Runtime {
    Write-Host "==> Staging standalone Python runtime ($PythonVersion)"
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        throw "uv not found on PATH. Install https://docs.astral.sh/uv/ and retry."
    }

    # Copy the complete managed interpreter, not a venv whose pyvenv.cfg
    # points back to the build machine.
    $managedPython = & uv python find $PythonVersion --managed-python 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $managedPython) {
        & uv python install $PythonVersion
        if ($LASTEXITCODE -ne 0) { throw "uv python install failed ($LASTEXITCODE)" }
        $managedPython = & uv python find $PythonVersion --managed-python
        if ($LASTEXITCODE -ne 0 -or -not $managedPython) { throw "uv python find failed ($LASTEXITCODE)" }
    }
    $pythonHome = Split-Path -Parent ($managedPython | Select-Object -Last 1)

    $resourceRoot = [System.IO.Path]::GetFullPath((Join-Path $SrcTauri 'resources'))
    $runtimeTarget = [System.IO.Path]::GetFullPath($RuntimeDir)
    if (-not $runtimeTarget.StartsWith($resourceRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace runtime outside $resourceRoot"
    }
    for ($attempt = 1; $attempt -le 6 -and (Test-Path -LiteralPath $RuntimeDir); $attempt++) {
        try {
            Remove-Item -LiteralPath $RuntimeDir -Recurse -Force -ErrorAction Stop
        } catch [System.IO.IOException] {
            if ($attempt -eq 6) { throw }
            Start-Sleep -Seconds 2
        }
    }
    if (Test-Path -LiteralPath $RuntimeDir) { throw "Runtime directory is still in use: $RuntimeDir" }
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    Copy-Item -Path (Join-Path $pythonHome '*') -Destination $RuntimeDir -Recurse -Force
    # This private copy is the application's own install target.
    Remove-Item -Force (Join-Path $RuntimeDir 'Lib\EXTERNALLY-MANAGED') -ErrorAction SilentlyContinue

    $py = Join-Path $RuntimeDir "python.exe"
    if (-not (Test-Path $py)) {
        throw "runtime python missing: $py"
    }

    # Editable installs retain a source checkout path and are not distributable.
    # Always package the current checkout.  Reusing a same-version wheel can
    # silently ship older Python code after a desktop-only rebuild.
    & uv build --wheel $RepoRoot
    if ($LASTEXITCODE -ne 0) { throw "uv build --wheel failed ($LASTEXITCODE)" }
    $wheel = Get-ChildItem -Path (Join-Path $RepoRoot "dist") -Filter "pku_course_sync-*-py3-none-any.whl" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $wheel) { throw 'uv build did not produce a pku-course-sync wheel.' }

    Write-Host "==> Installing wheel $($wheel.Name)"
    & uv pip install --python $py --system --link-mode copy $wheel.FullName
    if ($LASTEXITCODE -ne 0) { throw "uv pip install failed ($LASTEXITCODE)" }

    Write-Host "==> Smoke: python -m pku_sync --help"
    $helpText = & $py -m pku_sync --help
    $smokeExit = $LASTEXITCODE
    $helpText | Select-Object -First 5
    if ($smokeExit -ne 0) { throw "python -m pku_sync smoke failed ($smokeExit)" }

    Set-Content -Path (Join-Path $RuntimeDir ".bundle-ok") -Value "ok" -Encoding ASCII
    Write-Host "==> Runtime ready: $RuntimeDir"
}

if ($DevStubOnly) {
    Stage-DevStub
    Write-Host "==> Done (dev stub)."
    exit 0
}

Build-Sidecar -Release
if (-not $SkipRuntime) {
    Stage-Runtime
}
elseif (-not (Test-Path (Join-Path $RuntimeDir ".bundle-ok"))) {
    Write-Host "WARNING: -SkipRuntime set and no .bundle-ok marker; production bundle will lack panel runtime."
}

Write-Host "==> Sidecar stage complete."
Write-Host "    binary:  $sidecarDest"
Write-Host "    runtime: $RuntimeDir"
