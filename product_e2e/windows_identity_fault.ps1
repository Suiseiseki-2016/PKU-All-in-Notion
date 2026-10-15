[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TestRoot,
    [Parameter(Mandatory = $true)][string]$Program,
    [Parameter(Mandatory = $true)][string]$Receipt,
    [Parameter(Mandatory = $true)][string]$StopSignal,
    [ValidateRange(10, 600)][int]$LifetimeSeconds = 180
)

# Run only after Windows UAC approval. This helper has no credentials and
# changes no proxy settings. The block matches one isolated interpreter.
$ErrorActionPreference = 'Stop'
$pkuFaultRoot = (Resolve-Path -LiteralPath $TestRoot).Path.TrimEnd('\')
$pkuFaultProgram = (Resolve-Path -LiteralPath $Program).Path
$pkuFaultExpectedProgram = Join-Path $pkuFaultRoot 'installed\resources\runtime\python.exe'
if ($pkuFaultProgram -ne $pkuFaultExpectedProgram -or
    (Split-Path -Leaf $pkuFaultRoot) -notlike 'pku-native-revision-*') {
    throw 'Firewall target must be the exact isolated installed candidate interpreter.'
}
$pkuFaultReceipt = [IO.Path]::GetFullPath($Receipt)
$pkuFaultStop = [IO.Path]::GetFullPath($StopSignal)
if (-not $pkuFaultReceipt.StartsWith($pkuFaultRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
    -not $pkuFaultStop.StartsWith($pkuFaultRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Fault receipt and stop signal must stay inside the isolated test directory.'
}
$pkuFaultIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$pkuFaultPrincipal = [Security.Principal.WindowsPrincipal]::new($pkuFaultIdentity)
if (-not $pkuFaultPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Windows administrator approval is required for the temporary firewall fault.'
}
$pkuFaultAddresses = @([Net.Dns]::GetHostAddresses('iaaa.pku.edu.cn') | ForEach-Object IPAddressToString)
$pkuFaultCourseAddresses = @([Net.Dns]::GetHostAddresses('course.pku.edu.cn') | ForEach-Object IPAddressToString)
if (-not $pkuFaultAddresses.Count -or @($pkuFaultAddresses | Where-Object { $_ -in $pkuFaultCourseAddresses }).Count) {
    throw 'IAAA must have distinct addresses so the course callback remains reachable.'
}
$pkuFaultRule = 'PKU-E2E-Identity-' + [Guid]::NewGuid().ToString('N')
$pkuFaultRecord = @{
    kind = 'temporary-owned-interpreter-identity-network-fault'
    rule = $pkuFaultRule
    program = $pkuFaultProgram
    program_sha256 = (Get-FileHash -LiteralPath $pkuFaultProgram -Algorithm SHA256).Hash.ToLowerInvariant()
    addresses = $pkuFaultAddresses
    port = 443
    lifetime_seconds = $LifetimeSeconds
    active = $false
    removed = $false
}
try {
    New-NetFirewallRule -Name $pkuFaultRule -DisplayName $pkuFaultRule -Direction Outbound `
        -Action Block -Enabled True -Profile Any -Protocol TCP -RemotePort 443 `
        -RemoteAddress $pkuFaultAddresses -Program $pkuFaultProgram | Out-Null
    $pkuFaultRecord.active = $true
    $pkuFaultRecord.started_at = [DateTimeOffset]::UtcNow.ToString('o')
    $pkuFaultRecord | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $pkuFaultReceipt -Encoding UTF8
    $pkuFaultDeadline = [DateTimeOffset]::UtcNow.AddSeconds($LifetimeSeconds)
    while ([DateTimeOffset]::UtcNow -lt $pkuFaultDeadline -and -not (Test-Path -LiteralPath $pkuFaultStop)) {
        Start-Sleep -Milliseconds 250
    }
} finally {
    Remove-NetFirewallRule -Name $pkuFaultRule -ErrorAction SilentlyContinue
    $pkuFaultRecord.active = $false
    $pkuFaultRecord.removed = $null -eq (Get-NetFirewallRule -Name $pkuFaultRule -ErrorAction SilentlyContinue)
    $pkuFaultRecord.other_test_rules_remaining = @(Get-NetFirewallRule -Name 'PKU-E2E-Identity-*' `
        -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
    $pkuFaultRecord.finished_at = [DateTimeOffset]::UtcNow.ToString('o')
    $pkuFaultRecord | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $pkuFaultReceipt -Encoding UTF8
    if (-not $pkuFaultRecord.removed) { throw 'Temporary firewall fault cleanup was not verified.' }
}
