$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$bundleRoot = Join-Path $repositoryRoot "apps/desktop/src-tauri/target/release/bundle/nsis"
$runIdentity = if ($env:GITHUB_RUN_ID) {
    "$($env:GITHUB_RUN_ID)-$($env:GITHUB_RUN_ATTEMPT)"
} else {
    [Guid]::NewGuid().ToString("N")
}
$temporaryRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$installRoot = Join-Path $temporaryRoot "ai-trader-nsis-smoke-$runIdentity"
$uninstallRegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AI Trader"
$defaultUserInstallRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "AI Trader"
$installer = $null
$application = $null
$descendantProcessIds = @()

function Wait-ForPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$TimeoutSeconds = 60
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $Path) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "windows_installer_path_timeout:$Path"
}

function Get-DescendantProcess {
    param([Parameter(Mandatory = $true)][int]$RootProcessId)
    $processes = @(Get-CimInstance Win32_Process)
    $pending = [System.Collections.Generic.Queue[int]]::new()
    $pending.Enqueue($RootProcessId)
    $found = [System.Collections.Generic.List[object]]::new()
    while ($pending.Count -gt 0) {
        $parentId = $pending.Dequeue()
        foreach ($process in $processes) {
            if ([int]$process.ParentProcessId -eq $parentId) {
                $found.Add($process)
                $pending.Enqueue([int]$process.ProcessId)
            }
        }
    }
    return @($found)
}

function Get-PeSubsystem {
    param([Parameter(Mandatory = $true)][string]$Path)
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -lt 64) {
        throw "windows_pe_header_too_short:$Path"
    }
    $peOffset = [System.BitConverter]::ToInt32($bytes, 60)
    if ($peOffset -lt 0 -or $peOffset + 94 -gt $bytes.Length) {
        throw "windows_pe_header_offset_invalid:$Path"
    }
    if ([System.BitConverter]::ToUInt32($bytes, $peOffset) -ne 0x00004550) {
        throw "windows_pe_signature_invalid:$Path"
    }
    $optionalHeaderOffset = $peOffset + 24
    $magic = [System.BitConverter]::ToUInt16($bytes, $optionalHeaderOffset)
    if ($magic -notin @(0x010B, 0x020B)) {
        throw "windows_pe_optional_header_invalid:$Path"
    }
    return [System.BitConverter]::ToUInt16($bytes, $optionalHeaderOffset + 68)
}

function Assert-NoLocalServices {
    # The cloud-only desktop must come up without spawning a local Runtime or TqKq sidecar.
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$RootProcess,
        [int]$SettleSeconds = 8
    )
    $forbidden = @("ai-trader-runtime.exe", "ai-trader-tqkq-sidecar.exe")
    $deadline = (Get-Date).AddSeconds($SettleSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($RootProcess.HasExited) {
            throw "windows_installed_app_exited_early"
        }
        $names = @(Get-DescendantProcess -RootProcessId $RootProcess.Id | ForEach-Object Name)
        $spawned = @($forbidden | Where-Object { $_ -in $names })
        if ($spawned.Count -gt 0) {
            throw "windows_installed_app_spawned_local_services:$($spawned -join ',')"
        }
        Start-Sleep -Milliseconds 500
        $RootProcess.Refresh()
    }
}

if (Test-Path -LiteralPath $installRoot) {
    throw "windows_install_root_preexisting:$installRoot"
}
if ((Test-Path -LiteralPath $uninstallRegistryPath) -or (Test-Path -LiteralPath $defaultUserInstallRoot)) {
    throw "windows_installer_smoke_user_profile_not_clean(registry=$(Test-Path -LiteralPath $uninstallRegistryPath),installRoot=$defaultUserInstallRoot exists=$(Test-Path -LiteralPath $defaultUserInstallRoot),localAppData=$([Environment]::GetFolderPath('LocalApplicationData')),user=$(whoami))"
}

try {
    $installers = @(Get-ChildItem -LiteralPath $bundleRoot -File -Filter "*-setup.exe")
    if ($installers.Count -ne 1) {
        throw "windows_nsis_installer_count_invalid:$($installers.Count)"
    }
    $installer = $installers[0]
    # NSIS accepts /D=<path> only as its final argument. Use an isolated target
    # so native CI never overwrites or uninstalls a user's existing application.
    $install = Start-Process -FilePath $installer.FullName -ArgumentList @(
        "/S",
        "/D=$installRoot"
    ) -Wait -PassThru
    if ($install.ExitCode -ne 0) {
        throw "windows_nsis_install_failed:$($install.ExitCode)"
    }
    Wait-ForPath -Path $installRoot
    Wait-ForPath -Path $uninstallRegistryPath

    $registeredInstallRoot = (Get-ItemProperty -LiteralPath $uninstallRegistryPath -Name InstallLocation).InstallLocation.Trim('"')
    if ([IO.Path]::GetFullPath($registeredInstallRoot) -ne [IO.Path]::GetFullPath($installRoot)) {
        throw "windows_nsis_install_registry_mismatch:$registeredInstallRoot"
    }

    $appCandidates = @(
        Get-ChildItem -LiteralPath $installRoot -File -Filter "*.exe" |
            Where-Object { $_.Name -in @("ai-trader-desktop.exe", "AI Trader.exe") }
    )
    if ($appCandidates.Count -ne 1) {
        throw "windows_installed_app_count_invalid:$($appCandidates.Count)"
    }
    if ((Get-PeSubsystem -Path $appCandidates[0].FullName) -ne 2) {
        throw "windows_installed_app_not_gui_subsystem:$($appCandidates[0].FullName)"
    }

    if (Test-Path -LiteralPath (Join-Path $installRoot "ai-trader-tqkq-sidecar.exe")) {
        throw "windows_installed_app_ships_tqkq_sidecar"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $installRoot "ai-trader-runtime.exe"))) {
        throw "windows_installed_cli_entry_missing"
    }
    $application = Start-Process -FilePath $appCandidates[0].FullName -PassThru
    Assert-NoLocalServices -RootProcess $application
    $descendantProcessIds = @(
        Get-DescendantProcess -RootProcessId $application.Id |
            ForEach-Object { [int]$_.ProcessId }
    )
    if ($application.HasExited) {
        throw "windows_installed_app_not_running"
    }
}
finally {
    if ($null -ne $application -and -not $application.HasExited) {
        Stop-Process -Id $application.Id -Force
        $application.WaitForExit(10000) | Out-Null
    }
    $deadline = (Get-Date).AddSeconds(10)
    do {
        $remaining = @(
            $descendantProcessIds |
                Where-Object { $null -ne (Get-Process -Id $_ -ErrorAction SilentlyContinue) }
        )
        if ($remaining.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    foreach ($processId in $remaining) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    $uninstaller = Join-Path $installRoot "uninstall.exe"
    if (Test-Path -LiteralPath $uninstaller) {
        $uninstall = Start-Process -FilePath $uninstaller -ArgumentList "/S" -Wait -PassThru
        if ($uninstall.ExitCode -ne 0) {
            throw "windows_nsis_uninstall_failed:$($uninstall.ExitCode)"
        }
    }
}

if (Test-Path -LiteralPath $installRoot) {
    throw "windows_nsis_uninstall_incomplete:$installRoot"
}
if (Test-Path -LiteralPath $uninstallRegistryPath) {
    throw "windows_nsis_uninstall_registry_incomplete:$uninstallRegistryPath"
}

@{
    status = "ok"
    installer = $installer.Name
    installMode = "currentUser"
    applicationSubsystem = "windows-gui"
    bundledBinaries = @("ai-trader-runtime.exe")
    localServicesSpawned = $false
    liveTrading = "disabled"
} | ConvertTo-Json -Compress
