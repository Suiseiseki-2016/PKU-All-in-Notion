param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core') {
    throw 'This script must run on the Windows release build host.'
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$desktopDir = Split-Path -Parent $PSScriptRoot
$dotenvPath = Join-Path $desktopDir '.env'
$dotenv = @{}
if (Test-Path -LiteralPath $dotenvPath -PathType Leaf) {
    foreach ($line in (Get-Content -LiteralPath $dotenvPath)) {
        if ($line -match '^\s*(TAURI_SIGNING_PRIVATE_KEY|TAURI_SIGNING_PRIVATE_KEY_PASSWORD|TAURI_SIGNING_PRIVATE_KEY_PASSWORD_DPAPI)\s*=\s*(.*?)\s*$') {
            $dotenv[$Matches[1]] = $Matches[2].Trim('"', "'")
        }
    }
}
$candidateLocalAppData = [System.Collections.Generic.List[string]]::new()
try {
    $profileRegistryPath = "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$($identity.User.Value)"
    $registeredProfile = (Get-ItemProperty -LiteralPath $profileRegistryPath -Name ProfileImagePath -ErrorAction Stop).ProfileImagePath
    $candidateLocalAppData.Add((Join-Path ([Environment]::ExpandEnvironmentVariables($registeredProfile)) 'AppData\Local'))
} catch {
    # Some locked-down accounts do not expose the profile registry entry.
}
if ($env:LOCALAPPDATA) { $candidateLocalAppData.Add($env:LOCALAPPDATA) }

$adminDir = $null
$keyPath = $null
$passwordPath = $null
if ($dotenv.ContainsKey('TAURI_SIGNING_PRIVATE_KEY')) {
    $configuredKey = $dotenv['TAURI_SIGNING_PRIVATE_KEY']
    if (-not [IO.Path]::IsPathRooted($configuredKey)) {
        $configuredKey = Join-Path $desktopDir $configuredKey
    }
    if (-not (Test-Path -LiteralPath $configuredKey -PathType Leaf)) {
        throw "Updater key configured in desktop/.env does not exist: $configuredKey"
    }
    $keyPath = (Resolve-Path -LiteralPath $configuredKey).Path
    $adminDir = Split-Path -Parent $keyPath
    if ($dotenv.ContainsKey('TAURI_SIGNING_PRIVATE_KEY_PASSWORD_DPAPI')) {
        $passwordPath = $dotenv['TAURI_SIGNING_PRIVATE_KEY_PASSWORD_DPAPI']
        if (-not [IO.Path]::IsPathRooted($passwordPath)) {
            $passwordPath = Join-Path $desktopDir $passwordPath
        }
    } else {
        $passwordPath = Join-Path $adminDir 'tauri-updater-password.dpapi'
    }
}
foreach ($localAppData in ($candidateLocalAppData | Select-Object -Unique)) {
    if ($keyPath) { break }
    $candidateAdminDir = Join-Path $localAppData 'PKU-Admin'
    $candidateKey = Join-Path $candidateAdminDir 'tauri-updater.key'
    $candidatePassword = Join-Path $candidateAdminDir 'tauri-updater-password.dpapi'
    if ((Test-Path -LiteralPath $candidateKey -PathType Leaf) -and
        (Test-Path -LiteralPath $candidatePassword -PathType Leaf)) {
        $adminDir = $candidateAdminDir
        $keyPath = $candidateKey
        $passwordPath = $candidatePassword
        break
    }
}
if (-not $keyPath) {
    $searched = ($candidateLocalAppData | Select-Object -Unique | ForEach-Object { Join-Path $_ 'PKU-Admin' }) -join '; '
    throw "Updater key/password files are missing for $($identity.Name). Searched: $searched. Use the original signing account; do not generate a new key."
}

if ($dotenv.ContainsKey('TAURI_SIGNING_PRIVATE_KEY_PASSWORD')) {
    $securePassword = ConvertTo-SecureString -String $dotenv['TAURI_SIGNING_PRIVATE_KEY_PASSWORD'] -AsPlainText -Force
} else {
    if (-not (Test-Path -LiteralPath $passwordPath -PathType Leaf)) {
        throw "Updater DPAPI password file is missing: $passwordPath"
    }
    try {
        $protectedPassword = (Get-Content -LiteralPath $passwordPath -Raw).Trim()
        $securePassword = ConvertTo-SecureString -String $protectedPassword -ErrorAction Stop
    } catch {
        throw 'The updater password cannot be decrypted by this Windows user/session. Sign in as the original build account with its user profile loaded.'
    }
}
if ($securePassword.Length -eq 0) {
    throw 'The updater password store decrypted to an empty value.'
}

if ($CheckOnly) {
    Write-Host "Updater signing key and password are available to $($identity.Name) in $adminDir."
    $securePassword.Dispose()
    exit 0
}

$passwordPtr = [IntPtr]::Zero
try {
    $passwordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    $env:TAURI_SIGNING_PRIVATE_KEY = $keyPath
    $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPtr)
    Push-Location $desktopDir
    try {
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "Windows release build failed (exit code $LASTEXITCODE)." }
    } finally {
        Pop-Location
    }
} finally {
    Remove-Item Env:TAURI_SIGNING_PRIVATE_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD -ErrorAction SilentlyContinue
    if ($passwordPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPtr)
    }
    $securePassword.Dispose()
}
