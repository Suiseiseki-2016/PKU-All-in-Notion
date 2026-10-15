[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TestRoot,
    [ValidateRange(30, 7200)][int]$LifetimeSeconds = 600,
    [ValidatePattern('^[P-Z]$')][string]$DriveLetter
)

# Actual NTFS disk-full fault on a NEW bounded VHD, never the host volume.
# An elevated helper accepts only fill/release/stop signals; never paths or
# arbitrary commands. Native E2E must separately verify product write failure.
$ErrorActionPreference = 'Stop'
$pkuDiskRoot = (Resolve-Path -LiteralPath $TestRoot).Path.TrimEnd('\')
# Use the caller's inherited TEMP, matching Python's isolated fixture. Under
# a SYSTEM-hosted shell GetTempPath may choose WINDOWS/TEMP despite that env.
if ([string]::IsNullOrWhiteSpace($env:TEMP)) { throw 'Caller TEMP is required.' }
$pkuDiskTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
if (-not $pkuDiskRoot.StartsWith($pkuDiskTemp + '\', [StringComparison]::OrdinalIgnoreCase) -or
    (Split-Path -Leaf $pkuDiskRoot) -notlike 'pku-native-disk-full-*' -or
    $pkuDiskRoot.IndexOfAny([char[]]"`r`n`"") -ge 0) {
    throw 'Disk-full root must be a newly isolated pku-native-disk-full directory in TEMP.'
}
$pkuDiskImage = Join-Path $pkuDiskRoot 'disk-full.vhd'
$pkuDiskApp = Join-Path $pkuDiskRoot 'primary'
if (Test-Path -LiteralPath $pkuDiskApp) {
    $pkuDiskAppItem = Get-Item -LiteralPath $pkuDiskApp
    if (-not $pkuDiskAppItem.PSIsContainer -or
        ($pkuDiskAppItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Native profile parent must be an ordinary directory.'
    }
} else { New-Item -ItemType Directory -Path $pkuDiskApp | Out-Null }
# The installed app's DATA_DIR is precisely primary/data. Its .env and
# panel.log stay on the host so error reporting can survive disk exhaustion.
$pkuDiskMount = Join-Path $pkuDiskApp 'data'
$pkuDiskReceipt = Join-Path $pkuDiskRoot 'disk-full-receipt.json'
$pkuDiskControl = Join-Path $pkuDiskRoot 'disk-full-control.json'
$pkuDiskFiller = Join-Path $pkuDiskMount 'owned-disk-full-filler.bin'
$pkuDiskDriveRoot = if ($DriveLetter) { $DriveLetter + ':\' } else { $null }
if ($pkuDiskDriveRoot -and [IO.Directory]::GetLogicalDrives() -contains $pkuDiskDriveRoot) {
    throw 'Requested private drive letter is already occupied.'
}
if (Test-Path -LiteralPath $pkuDiskImage) { throw 'Never reuse or format an existing disk image.' }
if (Test-Path -LiteralPath $pkuDiskMount) {
    $pkuDiskMountItem = Get-Item -LiteralPath $pkuDiskMount
    if (-not $pkuDiskMountItem.PSIsContainer -or
        ($pkuDiskMountItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        @(Get-ChildItem -LiteralPath $pkuDiskMount -Force).Count) {
        throw 'Mount directory must be empty and must not already be a mount point.'
    }
} else { New-Item -ItemType Directory -Path $pkuDiskMount | Out-Null }
$pkuDiskHost = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($pkuDiskRoot))
if ($pkuDiskHost.AvailableFreeSpace -lt 512MB) { throw 'Host needs at least 512 MiB free; no host-space exhaustion.' }
$pkuDiskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$pkuDiskPrincipal = [Security.Principal.WindowsPrincipal]::new($pkuDiskIdentity)
if (-not $pkuDiskPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Windows administrator approval is required to attach the isolated 64 MiB disk image.'
}
$pkuDiskRecord = @{
    kind = 'isolated-real-ntfs-disk-full-not-native-e2e-verdict'
    image = $pkuDiskImage; mount = $pkuDiskMount; maximum_image_mib = 64
    active = $false; attached = $false; cleanup_verified = $false
    native_product_failure_verified = $false; full_release_gate_passed = $false
    sequence = 0; started_at = [DateTimeOffset]::UtcNow.ToString('o')
}

function Save-PkuDiskReceipt {
    $pkuDiskRecord | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $pkuDiskReceipt -Encoding UTF8
}

function Invoke-PkuOwnedDiskPart([string[]]$Lines, [string]$Name) {
    # Callers only provide fixed templates with the validated NEW image path.
    $pkuDiskScript = Join-Path $pkuDiskRoot ($Name + '.diskpart.txt')
    $Lines | Set-Content -LiteralPath $pkuDiskScript -Encoding ASCII
    & "$env:SystemRoot\System32\diskpart.exe" /s $pkuDiskScript |
        Set-Content -LiteralPath (Join-Path $pkuDiskRoot ($Name + '.diskpart.log')) -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { throw 'DiskPart did not complete the isolated image operation.' }
}

function Get-PkuOwnedVolume {
    $pkuDiskActualImage = Get-DiskImage -ImagePath $pkuDiskImage
    if (-not $pkuDiskActualImage.Attached -or
        [IO.Path]::GetFullPath($pkuDiskActualImage.ImagePath) -ne $pkuDiskImage) {
        throw 'Exact isolated disk image is not attached.'
    }
    $pkuDiskPartitions = @($pkuDiskActualImage | Get-Disk | Get-Partition |
        Where-Object { @($_.AccessPaths | Where-Object { $_.TrimEnd('\') -eq $pkuDiskMount }).Count -eq 1 })
    if ($pkuDiskPartitions.Count -ne 1) { throw 'Isolated mount must belong to exactly one partition of the exact image.' }
    if ($pkuDiskDriveRoot -and
        @($pkuDiskPartitions[0].AccessPaths | Where-Object { $_ -eq $pkuDiskDriveRoot }).Count -ne 1) {
        throw 'Private drive alias must belong to the exact isolated image partition.'
    }
    $pkuDiskVolumes = @($pkuDiskPartitions[0] | Get-Volume)
    if ($pkuDiskVolumes.Count -ne 1 -or $pkuDiskVolumes[0].FileSystem -ne 'NTFS' -or
        $pkuDiskVolumes[0].Size -gt 64MB -or $pkuDiskVolumes[0].Size -le 0) {
        throw 'Fault volume must be the bounded private NTFS volume.'
    }
    return $pkuDiskVolumes[0]
}

function Release-PkuOwnedFiller {
    $null = Get-PkuOwnedVolume
    # Only this helper's exact filler file is deleted. Student/test data and
    # the disk image remain. No recursive deletion or computed volume deletion.
    if (Test-Path -LiteralPath $pkuDiskFiller) {
        $pkuDiskFillerItem = Get-Item -LiteralPath $pkuDiskFiller
        if ($pkuDiskFillerItem.PSIsContainer -or
            ($pkuDiskFillerItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Filler must be the exact regular file created by this helper.'
        }
        Remove-Item -LiteralPath $pkuDiskFiller
    }
    $pkuDiskRecord.active = $false
    $pkuDiskRecord.released_free_bytes = (Get-PkuOwnedVolume).SizeRemaining
    if ($pkuDiskRecord.released_free_bytes -le 0) { throw 'Free space restoration was not verified.' }
}

try {
    $pkuDiskSetup = @(
        "create vdisk file=`"$pkuDiskImage`" maximum=64 type=expandable",
        "select vdisk file=`"$pkuDiskImage`"", 'attach vdisk',
        'create partition primary', 'format fs=ntfs quick label=PKU_E2E_DISK',
        "assign mount=`"$pkuDiskMount`""
    )
    if ($pkuDiskDriveRoot) { $pkuDiskSetup += "assign letter=$DriveLetter" }
    $pkuDiskSetup += 'exit'
    Invoke-PkuOwnedDiskPart $pkuDiskSetup 'setup'
    $pkuDiskVolume = Get-PkuOwnedVolume
    # A freshly formatted NTFS root does not necessarily grant the caller
    # directory listing/read rights. Bind access to the owner of this exact
    # newly isolated TEMP root; change only this private volume's root ACL.
    $pkuDiskCallerSid = [IO.Directory]::GetAccessControl($pkuDiskRoot).GetOwner(
        [Security.Principal.SecurityIdentifier])
    # Apply to the verified volume GUID root itself. Setting security on a
    # mounted folder can target the host reparse-point object instead.
    $pkuDiskAclRoot = $pkuDiskVolume.UniqueId
    $pkuDiskAcl = [IO.Directory]::GetAccessControl($pkuDiskAclRoot)
    $pkuDiskRule = [Security.AccessControl.FileSystemAccessRule]::new($pkuDiskCallerSid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit),
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow)
    $pkuDiskAcl.SetAccessRule($pkuDiskRule)
    [IO.Directory]::SetAccessControl($pkuDiskAclRoot, $pkuDiskAcl)
    $pkuDiskMountAcl = [IO.Directory]::GetAccessControl($pkuDiskMount)
    $pkuDiskMountAcl.SetAccessRule($pkuDiskRule)
    [IO.Directory]::SetAccessControl($pkuDiskMount, $pkuDiskMountAcl)
    $pkuDiskRecord.attached = $true
    $pkuDiskRecord.isolated_volume_caller_sid = $pkuDiskCallerSid.Value
    $pkuDiskRecord.volume_root_dacl = [IO.Directory]::GetAccessControl($pkuDiskAclRoot).GetSecurityDescriptorSddlForm(
        [Security.AccessControl.AccessControlSections]::Access)
    $pkuDiskRecord.mount_dacl = [IO.Directory]::GetAccessControl($pkuDiskMount).GetSecurityDescriptorSddlForm(
        [Security.AccessControl.AccessControlSections]::Access)
    try {
        $pkuDiskRecord.volume_root_sacl = [IO.Directory]::GetAccessControl($pkuDiskAclRoot,
            [Security.AccessControl.AccessControlSections]::Audit).GetSecurityDescriptorSddlForm(
                [Security.AccessControl.AccessControlSections]::Audit)
    } catch { $pkuDiskRecord.sacl_observation_error = $_.Exception.GetType().FullName }
    $pkuDiskRecord.volume_unique_id = $pkuDiskVolume.UniqueId
    $pkuDiskRecord.native_data_dir = $pkuDiskDriveRoot
    $pkuDiskRecord.volume_size_bytes = $pkuDiskVolume.Size
    $pkuDiskRecord.initial_free_bytes = $pkuDiskVolume.SizeRemaining
    Save-PkuDiskReceipt
    $pkuDiskDeadline = [DateTimeOffset]::UtcNow.AddSeconds($LifetimeSeconds)
    while ([DateTimeOffset]::UtcNow -lt $pkuDiskDeadline) {
        if (Test-Path -LiteralPath $pkuDiskControl) {
            try { $pkuDiskCommand = Get-Content -LiteralPath $pkuDiskControl -Raw | ConvertFrom-Json }
            catch { Start-Sleep -Milliseconds 200; continue }
            if ($pkuDiskCommand.sequence -isnot [int] -or
                $pkuDiskCommand.sequence -le $pkuDiskRecord.sequence) {
                Start-Sleep -Milliseconds 200; continue
            }
            if ($pkuDiskCommand.operation -notin @('fill', 'release', 'stop')) {
                throw 'Unknown disk fault operation; no arbitrary elevated command is accepted.'
            }
            $pkuDiskRecord.sequence = $pkuDiskCommand.sequence
            if ($pkuDiskCommand.operation -eq 'stop') { break }
            if ($pkuDiskCommand.operation -eq 'release') {
                Release-PkuOwnedFiller
                Save-PkuDiskReceipt
                continue
            }
            $null = Get-PkuOwnedVolume
            if ($pkuDiskRecord.active -or -not (Test-Path -LiteralPath $pkuDiskFiller)) {
                throw 'Caller must exclusively create a new empty filler; never activate twice.'
            }
            $pkuDiskInitialFiller = Get-Item -LiteralPath $pkuDiskFiller
            if ($pkuDiskInitialFiller.PSIsContainer -or $pkuDiskInitialFiller.Length -ne 0 -or
                ($pkuDiskInitialFiller.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Only the new zero-byte regular filler in the isolated volume may be filled.'
            }
            $pkuDiskBuffer = [byte[]]::new(256KB)
            $pkuDiskStream = [IO.File]::Open($pkuDiskFiller, [IO.FileMode]::Open,
                [IO.FileAccess]::Write, [IO.FileShare]::Read)
            $pkuDiskError = $null
            try {
                for ($pkuDiskCount = 0; $pkuDiskCount -lt 257; $pkuDiskCount++) {
                    $pkuDiskStream.Write($pkuDiskBuffer, 0, $pkuDiskBuffer.Length)
                    $pkuDiskStream.Flush($true)
                }
            } catch {
                $pkuDiskBaseError = $_.Exception.GetBaseException()
                if ($pkuDiskBaseError -isnot [IO.IOException]) { throw }
                $pkuDiskError = $pkuDiskBaseError.HResult -band 0xFFFF
            } finally { $pkuDiskStream.Dispose() }
            if ($pkuDiskError -notin @(39, 112)) {
                throw 'Actual Windows disk-full error 39 or 112 was not observed.'
            }
            # Exhaust the remaining clusters too: a rejected 256 KiB write
            # alone does not establish that smaller product writes will fail.
            $pkuDiskStream = [IO.File]::Open($pkuDiskFiller, [IO.FileMode]::Append,
                [IO.FileAccess]::Write, [IO.FileShare]::Read)
            $pkuDiskSmallError = $null
            try {
                for ($pkuDiskCount = 0; $pkuDiskCount -lt 65; $pkuDiskCount++) {
                    $pkuDiskStream.Write($pkuDiskBuffer, 0, 4096)
                    $pkuDiskStream.Flush($true)
                }
            } catch {
                $pkuDiskBaseError = $_.Exception.GetBaseException()
                if ($pkuDiskBaseError -isnot [IO.IOException]) { throw }
                $pkuDiskSmallError = $pkuDiskBaseError.HResult -band 0xFFFF
            } finally { $pkuDiskStream.Dispose() }
            if ($pkuDiskSmallError -notin @(39, 112)) {
                throw 'Small real filesystem writes did not fail with disk-full.'
            }
            $pkuDiskRecord.active = $true
            $pkuDiskRecord.actual_fill_winerror = $pkuDiskError
            $pkuDiskRecord.actual_small_write_winerror = $pkuDiskSmallError
            $pkuDiskRecord.remaining_free_bytes = (Get-PkuOwnedVolume).SizeRemaining
            $pkuDiskRecord.filler_bytes = (Get-Item -LiteralPath $pkuDiskFiller).Length
            $pkuDiskRecord.fault_started_at = [DateTimeOffset]::UtcNow.ToString('o')
            Save-PkuDiskReceipt
        }
        Start-Sleep -Milliseconds 200
    }
} catch {
    # Save only error class; paths are already constrained, no credentials.
    $pkuDiskRecord.helper_error_type = $_.Exception.GetType().FullName
    $pkuDiskRecord.helper_base_error_type = $_.Exception.GetBaseException().GetType().FullName
    $pkuDiskRecord.helper_base_winerror = $_.Exception.GetBaseException().HResult -band 0xFFFF
    $pkuDiskRecord.helper_error_line = $_.InvocationInfo.ScriptLineNumber
    throw
} finally {
    if (Test-Path -LiteralPath $pkuDiskImage) {
        $pkuDiskActualImage = Get-DiskImage -ImagePath $pkuDiskImage
        if ($pkuDiskActualImage.Attached) {
            try { Release-PkuOwnedFiller }
            finally {
                Invoke-PkuOwnedDiskPart @("select vdisk file=`"$pkuDiskImage`"", 'detach vdisk', 'exit') 'cleanup'
            }
        }
        $pkuDiskRecord.attached = (Get-DiskImage -ImagePath $pkuDiskImage).Attached
        $pkuDiskRecord.cleanup_verified = -not $pkuDiskRecord.attached
    }
    $pkuDiskRecord.finished_at = [DateTimeOffset]::UtcNow.ToString('o')
    Save-PkuDiskReceipt
    if (-not $pkuDiskRecord.cleanup_verified) { throw 'Isolated disk detach cleanup has not been verified.' }
}
