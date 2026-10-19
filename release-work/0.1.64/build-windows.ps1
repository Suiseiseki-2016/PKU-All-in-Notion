# Run on the existing Windows build host after uploading windows-source.tar.gz.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = 'E:\remote_project\pku-release-0.1.64'
$previous = 'E:\remote_project\pku-course-sync'
New-Item -ItemType Directory -Force $root | Out-Null
tar -xzf E:\remote_project\pku-0164-source.tar.gz -C $root
if ($LASTEXITCODE -ne 0) { throw 'Source extraction failed' }
# Use regular directories. User-profile Rust shims and junctions may fail
# Windows trust checks; inherit the host's working system toolchain PATH.
robocopy "$previous\desktop\node_modules" "$root\desktop\node_modules" /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -gt 7) { throw 'Node tool copy failed' }
robocopy "$previous\desktop\src-tauri\target" "$root\desktop\src-tauri\target" /E /XD resources bundle incremental /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -gt 7) { throw 'Cargo cache copy failed' }
Copy-Item "$previous\desktop\.env" "$root\desktop\.env"
Start-Transcript -Path "$root\build-windows-final.log" -Force
try {
    & "$root\desktop\scripts\build-signed-windows.ps1" -CheckOnly
    & "$root\desktop\scripts\build-signed-windows.ps1"
} finally {
    Stop-Transcript
}
