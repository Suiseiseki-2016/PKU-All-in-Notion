param(
    [string]$ReleaseDir = ""
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
if (Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue) {
    throw "A panel port is already in use; close the existing app before smoke testing."
}
$started = Get-Date
$process = Start-Process -FilePath $app -WorkingDirectory $ReleaseDir -PassThru
try {
    $port = $null
    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 400
        foreach ($candidate in $ports) {
            try {
                $response = Invoke-WebRequest -Uri "http://127.0.0.1:$candidate/healthz" -TimeoutSec 1 -UseBasicParsing
                if ($response.StatusCode -eq 200) { $port = $candidate; break }
            } catch {}
        }
    } until ($port -or (Get-Date) -gt $deadline)
    if (-not $port) { throw "Desktop panel did not start within 45 seconds." }
    $page = Invoke-WebRequest -Uri "http://127.0.0.1:$port/app" -TimeoutSec 5 -UseBasicParsing
    if ($page.StatusCode -ne 200 -or $page.Content -notmatch 'id="app"') { throw "Student /app did not load." }
    $appProcess = Get-Process -Id $process.Id -ErrorAction Stop
    if ($appProcess.MainWindowHandle -eq 0) { throw "Tauri desktop window did not appear." }
    $children = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ParentProcessId -eq $process.Id -and $_.Name -eq 'pku-sync.exe'
    })
    if ($children.Count -ne 1) { throw "Expected one pku-sync sidecar, found $($children.Count)." }
    $pythonChildren = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ParentProcessId -eq $children[0].ProcessId -and $_.Name -eq 'python.exe'
    })
    if ($pythonChildren.Count -ne 1) { throw "Expected one bundled Python child, found $($pythonChildren.Count)." }
    foreach ($pidToCheck in @($children[0].ProcessId, $pythonChildren[0].ProcessId)) {
        if ((Get-Process -Id $pidToCheck).MainWindowHandle -ne 0) { throw "A background helper has a visible window: $pidToCheck" }
    }
    $second = Start-Process -FilePath $app -WorkingDirectory $ReleaseDir -PassThru
    if (-not $second.WaitForExit(15000)) { throw "Second launch did not exit as a single instance." }
    if ((Get-Process -Id $process.Id -ErrorAction SilentlyContinue) -eq $null) { throw "First desktop window disappeared after second launch." }
    if (-not $appProcess.CloseMainWindow()) { throw "Could not request desktop window close." }
    if (-not $process.WaitForExit(15000)) { throw "Desktop did not exit after close." }
    Start-Sleep -Seconds 2
    if (Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue) { throw "Panel port remains in use after close." }
    if (Get-Process -Id $children[0].ProcessId,$pythonChildren[0].ProcessId -ErrorAction SilentlyContinue) { throw "Panel process tree remains after close." }
    $log = Join-Path $env:USERPROFILE "PKU-All-in-Notion\panel.log"
    if (-not (Test-Path $log)) { throw "Launcher log was not written." }
    [pscustomobject]@{
        release = $ReleaseDir
        port = $port
        desktopWindow = $true
        helperWindows = 0
        singleInstance = $true
        portReleased = $true
        panelLog = $log
        started = $started.ToString('s')
    } | ConvertTo-Json
} finally {
    if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
        $process.CloseMainWindow() | Out-Null
    }
}
