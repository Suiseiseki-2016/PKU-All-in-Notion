# Manually trigger ONE lecture-notes run (scripts\lecture_notes_writer.md)
# on Windows.
#
# Why this exists: piping the key=value payload over ssh/stdin mangles the
# CJK values before they reach droid. Instead this runner reads a UTF-8
# payload file, embeds it into a temporary prompt file, and passes that file
# via -f, so no CJK value ever crosses an ssh/stdin pipe.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_lecture_notes.ps1 `
#       -PayloadFile C:\Windows\TEMP\lec_args.txt `
#       [-LogFile C:\Windows\TEMP\lec_run.out]
#
# PayloadFile (UTF-8) holds the manual's section 1.1 key=value block:
#   mode=create|overwrite
#   course_folder=...
#   course_page_id=...
#   slug=...
#   data_root=E:\pku-course-data\<course_folder>
param(
    [Parameter(Mandatory = $true)][string]$PayloadFile,
    [string]$LogFile
)
$ErrorActionPreference = 'Stop'

# PS 5.1 decodes native tool output with the console codepage (GBK on a
# Chinese system default), which would garble droid's UTF-8 stdout in the
# captured log. Pin both directions to UTF-8.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$proj = 'E:\remote_project\pku-course-sync'
$utf8 = New-Object System.Text.UTF8Encoding($false)

if (-not (Test-Path $PayloadFile)) { throw "PayloadFile not found: $PayloadFile" }
$payload = [System.IO.File]::ReadAllText($PayloadFile, $utf8)

# ASCII-only header: the executing droid reads the manual from the repo
# itself, so this prompt only needs to point at it and carry the parameters.
$header = @(
    'Execute ONE lecture-notes run following the repo manual',
    'scripts\lecture_notes_writer.md - read that file first and follow every',
    'section and red line in it. The trigger parameters for this run are the',
    'key=value lines below (they take the place of the stdin block in the',
    'manual section 1.1). Deliver the standard four-part Chinese reply.'
) -join "`r`n"

$promptFile = Join-Path $env:TEMP ('lecture_prompt_' + [guid]::NewGuid().ToString('N') + '.md')
[System.IO.File]::WriteAllText($promptFile, $header + "`r`n`r`n" + $payload, $utf8)

# droid resolves through the npm shim (droid.ps1 / droid.cmd).
$droidCmd = Get-Command droid -ErrorAction SilentlyContinue
if (-not $droidCmd) {
    $npmDroid = Join-Path $env:APPDATA 'npm\droid.cmd'
    if (Test-Path $npmDroid) { $droidCmd = Get-Item $npmDroid }
}
if (-not $droidCmd) { throw 'droid not found on PATH or in %APPDATA%\npm' }
$droidExe = if ($droidCmd.PSObject.Properties['Path']) { $droidCmd.Path } else { $droidCmd.FullName }

try {
    if ($LogFile) {
        "=== START $((Get-Date).ToString('s')) payload=$PayloadFile ===" | Out-File -FilePath $LogFile -Encoding utf8
        & $droidExe exec --cwd $proj --auto high --add-tools MCP:notion -f $promptFile -o text 2>&1 |
            Out-File -FilePath $LogFile -Append -Encoding utf8
        "=== END $((Get-Date).ToString('s')) ===" | Out-File -FilePath $LogFile -Append -Encoding utf8
    } else {
        & $droidExe exec --cwd $proj --auto high --add-tools MCP:notion -f $promptFile -o text
    }
} finally {
    Remove-Item $promptFile -Force -ErrorAction SilentlyContinue
}
