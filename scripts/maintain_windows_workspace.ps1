# Installed outside the checkout; GitHub's native before/after-job hooks invoke this.
param(
    [string]$WorkRoot = "D:\ai-trader-ci\_work",
    [long]$LimitBytes = 10GB,
    [switch]$Apply = ($env:AI_TRADER_WINDOWS_CLEAN_APPLY -eq "1")
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
try {
$timer = [Diagnostics.Stopwatch]::StartNew()
$WorkRoot = [IO.Path]::GetFullPath($WorkRoot).TrimEnd('\', '/')
if ((Split-Path -Leaf $WorkRoot) -ne '_work' -or $LimitBytes -le 0) {
    throw 'windows_workspace_boundary_invalid'
}
$repository = Join-Path $WorkRoot 'ai-trader\ai-trader'
$pytestRoot = Join-Path (Split-Path -Parent $WorkRoot) 'pytest'
function Test-PlainAncestors([string]$Path) {
    for ($item = [IO.DirectoryInfo]::new($Path); $null -ne $item; $item = $item.Parent) {
        if ($item.Exists -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $false }
    }
    return $true
}
if (-not (Test-PlainAncestors $WorkRoot)) { throw 'windows_workspace_linked_root' }

# Separate Cargo profiles let an old debug tree go before a recent release tree.
# Sources/Git, exact-lock dependencies, tools/actions, unknown files and installer
# candidates outside this root are protected, even if they alone exceed the budget.
$outputs = @(
    (Join-Path $repository 'apps\desktop\src-tauri\target\debug'),
    (Join-Path $repository 'apps\desktop\src-tauri\target\release'),
    (Join-Path $repository 'apps\client\dist'),
    (Join-Path $repository 'apps\client\test-results'),
    (Join-Path $repository 'apps\client\playwright-report'),
    (Join-Path $repository 'test-results')
)
if ((Test-PlainAncestors $pytestRoot) -and [IO.Directory]::Exists($pytestRoot)) {
    $outputs += @([IO.DirectoryInfo]::new($pytestRoot).GetDirectories() | Where-Object Name -Match '^\d+-\d+$' | ForEach-Object FullName)
}
$byPath = @{}
foreach ($path in $outputs) {
    if (Test-PlainAncestors $path) {
        $byPath[$path] = [PSCustomObject]@{ Path = $path; Bytes = 0L; Found = $false; Linked = $false; Modified = [DateTime]::MinValue }
    }
}
# One .NET walk counts all normal files and classifies known outputs at the same
# time. It never follows reparse points; any output containing one is preserved.
function Measure-Tree([string]$Path, [bool]$Record = $true) {
    if (-not [IO.Directory]::Exists($Path) -or -not (Test-PlainAncestors $Path)) { return 0L }
    $pending = [Collections.Generic.Stack[object]]::new()
    $pending.Push(@{ Directory = [IO.DirectoryInfo]::new($Path); Owner = $null })
    $bytes = 0L
    while ($pending.Count) {
        $current = $pending.Pop()
        foreach ($entry in $current.Directory.EnumerateFileSystemInfos()) {
            $owner = $current.Owner
            if ($Record -and $byPath.ContainsKey($entry.FullName)) {
                $owner = $byPath[$entry.FullName]
                $owner.Found = $true
            }
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                if ($null -ne $owner) { $owner.Linked = $true }
                continue
            }
            if ($null -ne $owner -and $entry.LastWriteTimeUtc -gt $owner.Modified) { $owner.Modified = $entry.LastWriteTimeUtc }
            if ($entry.Attributes -band [IO.FileAttributes]::Directory) {
                $pending.Push(@{ Directory = $entry; Owner = $owner })
            } else {
                $bytes += $entry.Length
                if ($null -ne $owner) { $owner.Bytes += $entry.Length }
            }
        }
    }
    return $bytes
}
$total = (Measure-Tree $WorkRoot) + (Measure-Tree $pytestRoot)
$candidates = @($byPath.Values | Where-Object { $_.Found -and -not $_.Linked -and $_.Bytes -gt 0 } | Sort-Object Modified)
$reclaimable = 0L
foreach ($candidate in $candidates) { $reclaimable += $candidate.Bytes }
$report = [ordered]@{
    workRoot = $WorkRoot; limitBytes = $LimitBytes; totalBytes = $total
    reclaimableBytes = $reclaimable; protectedBytes = $total - $reclaimable
    apply = [bool]$Apply; removed = @(); status = 'below-limit'; afterBytes = $total
}
if ($total -gt $LimitBytes -and $reclaimable -gt 0) {
    $normalizedRoot = $WorkRoot.Replace('\', '/') + '/'
    $busy = @(Get-CimInstance Win32_Process | Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath.Replace('\', '/').StartsWith($normalizedRoot, [StringComparison]::OrdinalIgnoreCase)) -or
        ($_.Name -match '^(cargo|rustc|rustdoc|makensis|MSBuild|cl|link|node)\.exe$' -and
            $_.CommandLine -and $_.CommandLine.Replace('\', '/').IndexOf($normalizedRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    })
    if ($busy.Count) {
        $report.status = 'active-build-or-runtime'
    } else {
        $remaining = $total
        $selected = @()
        foreach ($candidate in $candidates) {
            if ($remaining -le $LimitBytes) { break }
            $selected += $candidate.Path
            $remaining -= $candidate.Bytes
            if (-not $Apply) { continue }
            if (-not (Test-PlainAncestors $candidate.Path)) { throw 'windows_workspace_output_became_linked' }
            if ($candidate.Path.StartsWith("$repository\", [StringComparison]::OrdinalIgnoreCase)) {
                $relative = $candidate.Path.Substring($repository.Length + 1).Replace('\', '/')
                $tracked = @(& git -c "safe.directory=$repository" -C $repository ls-files -- $relative)
                if ($LASTEXITCODE -ne 0 -or $tracked.Count) { throw "windows_workspace_output_not_disposable:$($candidate.Path)" }
            }
            Remove-Item -LiteralPath $candidate.Path -Recurse -Force
            $report.removed += $candidate.Path
        }
        if ($Apply) {
            $report.status = 'reclaimed'
            $report.afterBytes = (Measure-Tree $WorkRoot $false) + (Measure-Tree $pytestRoot $false)
        } else {
            $report.status = 'dry-run'
            $report['candidates'] = $selected
        }
    }
} elseif ($total -gt $LimitBytes) {
    $report.status = 'protected-content-exceeds-limit'
}
$report['elapsedMs'] = $timer.ElapsedMilliseconds
$report | ConvertTo-Json -Depth 4 -Compress
} catch {
    # Maintenance is not another CI gate. Refuse uncertain deletion and report it.
    Write-Warning "Windows workspace retention skipped: $($_.Exception.Message)"
}
