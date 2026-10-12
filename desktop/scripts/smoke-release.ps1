param(
    [string]$ReleaseDir = "",
    [string]$AppDataDir = "",
    [string]$ResultPath = ""
)
$ErrorActionPreference = "Stop"
if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") { throw "Windows only" }
if (-not $ReleaseDir) {
    $ReleaseDir = Join-Path $PSScriptRoot "..\src-tauri\target\release"
}
$ReleaseDir = (Resolve-Path $ReleaseDir).Path
$app = Join-Path $ReleaseDir "pku-desktop.exe"
$sidecar = Join-Path $ReleaseDir "pku-sync.exe"
$python = Join-Path $ReleaseDir "resources\runtime\python.exe"
foreach ($file in @($app, $sidecar, $python)) {
    if (-not (Test-Path $file)) { throw "Missing release file: $file" }
}
$ports = @(8791, 8792, 8793)
if (@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'pku-desktop.exe' }).Count) {
    throw "A desktop app is already running; leave it untouched and retry after closing it."
}
if (Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue) {
    throw "A panel port is already in use; close the existing app before smoke testing."
}
if (-not $AppDataDir) { $AppDataDir = Join-Path ([System.IO.Path]::GetTempPath()) ("pku-release-smoke-" + [guid]::NewGuid().ToString('N')) }
if (-not [System.IO.Path]::IsPathRooted($AppDataDir)) { throw "AppDataDir must be an absolute fresh directory." }
$AppDataDir = [System.IO.Path]::GetFullPath($AppDataDir)
$studentDir = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE 'PKU-All-in-Notion'))
if ((Test-Path -LiteralPath $AppDataDir) -or
    $AppDataDir.TrimEnd('\') -eq [System.IO.Path]::GetPathRoot($AppDataDir).TrimEnd('\') -or
    $AppDataDir.Equals($studentDir, [System.StringComparison]::OrdinalIgnoreCase) -or
    $AppDataDir.StartsWith($studentDir + '\', [System.StringComparison]::OrdinalIgnoreCase)) { throw "AppDataDir must be new and outside the student profile." }
if ($ResultPath) {
    if (-not [System.IO.Path]::IsPathRooted($ResultPath) -or (Test-Path -LiteralPath $ResultPath)) { throw "ResultPath must be absolute and new." }
    if (-not (Test-Path -LiteralPath (Split-Path -Parent $ResultPath) -PathType Container)) { throw "ResultPath parent is missing." }
}
New-Item -ItemType Directory -Path $AppDataDir | Out-Null
$overrides = @{
    PKU_DESKTOP_DATA_DIR = $AppDataDir; DATA_DIR = (Join-Path $AppDataDir 'data')
    WEBVIEW2_USER_DATA_FOLDER = (Join-Path $AppDataDir 'webview-profile'); PYTHONUTF8 = '1'
    NOTION_TOKEN = ''; PLATFORM_TOKEN = ''; PKU_USERNAME = ''; PKU_PASSWORD = ''; OPENAI_API_KEY = ''
    PYTHONPATH = ''; PKU_NOTION_API_BASE = ''; PKU_CAMPUS_MEDIA_PROXY = ''
}
$savedEnvironment = @{}
$started = [DateTime]::UtcNow
$process = $null
$second = $null
$cycles = @()
$failure = $null
try {
    foreach ($key in $overrides.Keys) {
        $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $overrides[$key], 'Process')
    }
    # Repeat with the same isolated profile to exercise restart and port reuse.
    foreach ($cycle in 1..2) {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $process = Start-Process -FilePath $app -WorkingDirectory $AppDataDir -WindowStyle Hidden -PassThru
    $port = $null
    $identity = $null
    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 400
        if ($process.HasExited) { throw "Desktop exited before its backend became ready." }
        foreach ($candidate in $ports) {
            try {
                $response = Invoke-WebRequest -Uri "http://127.0.0.1:$candidate/healthz" -TimeoutSec 1 -UseBasicParsing
                $health = $response.Content | ConvertFrom-Json
                if ($response.StatusCode -eq 200 -and $health.ok -eq $true -and
                    $health.instance_id -match ('^' + $process.Id + '-[1-9][0-9]*$')) {
                    $port = $candidate; $identity = $health.instance_id; break
                }
            } catch {}
        }
    } until ($port -or (Get-Date) -gt $deadline)
    if (-not $port) { throw "Desktop panel did not start within 45 seconds." }
    $startupSeconds = $timer.Elapsed.TotalSeconds
    $page = Invoke-WebRequest -Uri "http://127.0.0.1:$port/app" -TimeoutSec 5 -UseBasicParsing
    if ($page.StatusCode -ne 200 -or $page.Content -notmatch 'id="app"') { throw "Student /app did not load." }
    $appProcess = Get-Process -Id $process.Id -ErrorAction Stop
    if ($appProcess.MainWindowHandle -eq 0) { throw "Tauri desktop window did not appear." }
    $children = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ParentProcessId -eq $process.Id -and $_.Name -eq 'pku-sync.exe'
    })
    if ($children.Count -ne 1 -or $children[0].ExecutablePath -ne $sidecar) { throw "Expected this release's single sidecar." }
    $pythonChildren = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ParentProcessId -eq $children[0].ProcessId -and $_.Name -eq 'python.exe'
    })
    if ($pythonChildren.Count -ne 1 -or $pythonChildren[0].ExecutablePath -ne $python) { throw "Expected this release's bundled Python child." }
    $listener = @(Get-NetTCPConnection -LocalPort $port -State Listen)
    if ($listener.Count -ne 1 -or $listener[0].OwningProcess -ne $pythonChildren[0].ProcessId) { throw "Panel listener belongs to another process." }
    foreach ($pidToCheck in @($children[0].ProcessId, $pythonChildren[0].ProcessId)) {
        if ((Get-Process -Id $pidToCheck).MainWindowHandle -ne 0) { throw "A background helper has a visible window: $pidToCheck" }
    }
    $second = Start-Process -FilePath $app -WorkingDirectory $AppDataDir -WindowStyle Hidden -PassThru
    if (-not $second.WaitForExit(15000)) { throw "Second launch did not exit as a single instance." }
    $second = $null
    if ((Get-Process -Id $process.Id -ErrorAction SilentlyContinue) -eq $null) { throw "First desktop window disappeared after second launch." }
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 3
    if ($health.instance_id -ne $identity) { throw "Second launch replaced the original backend." }
    if (-not $appProcess.CloseMainWindow()) { throw "Could not request desktop window close." }
    if (-not $process.WaitForExit(15000)) { throw "Desktop did not exit after close." }
    Start-Sleep -Seconds 2
    if (Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue) { throw "Panel port remains in use after close." }
    if (Get-Process -Id $children[0].ProcessId,$pythonChildren[0].ProcessId -ErrorAction SilentlyContinue) { throw "Panel process tree remains after close." }
    $log = Join-Path $AppDataDir 'panel.log'
    if (-not (Test-Path $log)) { throw "Launcher log was not written." }
    $cycles += [pscustomobject]@{
        cycle = $cycle
        startupSeconds = [math]::Round($startupSeconds, 3)
        instanceId = $identity
        release = $ReleaseDir
        port = $port
        desktopWindow = $true
        helperWindows = 0
        singleInstance = $true
        portReleased = $true
        panelLog = $log
        started = $started.ToString('s')
    }
    $process = $null
    }
} catch {
    $failure = $_
} finally {
    # Forced cleanup retains failure; only our original live handles are used.
    foreach ($owned in @($second, $process)) {
        if ($owned -and -not $owned.HasExited) {
            $owned.CloseMainWindow() | Out-Null
            if (-not $owned.WaitForExit(10000)) {
                & taskkill.exe /PID $owned.Id /T /F | Out-Null
                $owned.WaitForExit(10000) | Out-Null
            }
        }
    }
    foreach ($key in $savedEnvironment.Keys) { [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], 'Process') }
    $proof = [ordered]@{ kind = 'isolated-installed-desktop-lifecycle-smoke'; passed = ($null -eq $failure)
        release = $ReleaseDir; appDataDir = $AppDataDir; cycles = $cycles
        version = (Get-Item -LiteralPath $app).VersionInfo.ProductVersion
        appSha256 = (Get-FileHash -LiteralPath $app -Algorithm SHA256).Hash.ToLower()
        startedAt = $started.ToString('o'); finishedAt = [DateTime]::UtcNow.ToString('o'); fullReleaseGatePassed = $false }
    if ($failure) { $proof['failureType'] = $failure.Exception.GetType().Name }
    $json = $proof | ConvertTo-Json -Depth 5
    if ($ResultPath) { [System.IO.File]::WriteAllText($ResultPath, $json, [System.Text.UTF8Encoding]::new($false)) }
    $json
}
if ($failure) { throw $failure }
