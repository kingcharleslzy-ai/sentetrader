<#
Keeps the read-only MT5 bridge (127.0.0.1:18930) and its reverse tunnel to the Tencent production
host (its 127.0.0.1:11210, user ai-trader-tunnel) running in user 1's desktop session, next to the
MT5 terminal the owner keeps logged in. Started by the scheduled task "AI Trader MT5 Bridge" at
user 1 logon; restarts a child that exits (10 s, doubling to 5 min while it keeps failing).
-Stop ends both children and exits. Layout: D:\mt5-bridge\{current.txt, releases\<sha>, secrets, logs}.
#>
param([switch]$Stop)
$ErrorActionPreference = "Stop"
$Root = "D:\mt5-bridge"
$Logs = Join-Path $Root "logs"
$Secrets = Join-Path $Root "secrets"
$Ssh = "C:\Program Files\Git\usr\bin\ssh.exe"
$BridgePort = 18930
$Forward = "127.0.0.1:11210:127.0.0.1:$BridgePort"
$TunnelTarget = "ai-trader-tunnel@123.206.121.38"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

function Write-Log([string]$Message) {
  "$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss.fffK') $Message" |
    Add-Content -Path (Join-Path $Logs "supervise-$(Get-Date -Format yyyyMMdd).log") -Encoding utf8
}

function Get-Children {
  # The uv venv python.exe is a launcher with a child interpreter; both carry the same command line.
  Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -eq "python.exe" -and $_.CommandLine -like "*-m mt5_bridge*") -or
    ($_.Name -eq "ssh.exe" -and $_.CommandLine -like "*$Forward*")
  }
}

function Stop-Children {
  foreach ($process in Get-Children) {
    Write-Log "stop pid=$($process.ProcessId) $($process.Name)"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
  }
}

function Start-Bridge {
  $sha = (Get-Content (Join-Path $Root "current.txt") -Raw).Trim()
  $app = Join-Path $Root "releases\$sha"
  $env:MT5_BRIDGE_TOKEN_FILE = Join-Path $Secrets "token"
  $env:MT5_BRIDGE_PORT = "$BridgePort"
  $env:MT5_BRIDGE_LOG_DIR = $Logs
  $stamp = Get-Date -Format yyyyMMdd-HHmmss
  Write-Log "start bridge release=$sha"
  Start-Process -FilePath (Join-Path $app ".venv\Scripts\python.exe") -ArgumentList "-m", "mt5_bridge" `
    -WorkingDirectory $app -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $Logs "bridge-console-$stamp.out.log") `
    -RedirectStandardError (Join-Path $Logs "bridge-console-$stamp.log")
}

function Start-Tunnel {
  $key = (Join-Path $Secrets "tunnel_ed25519") -replace '\\', '/'
  $known = (Join-Path $Secrets "known_hosts") -replace '\\', '/'
  $log = (Join-Path $Logs "tunnel-$(Get-Date -Format yyyyMMdd).log") -replace '\\', '/'
  Write-Log "start tunnel $Forward"
  Start-Process -FilePath $Ssh -WindowStyle Hidden -PassThru -ArgumentList @(
    "-F", "none", "-N", "-T", "-E", $log, "-i", $key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=$known", "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=20",
    "-R", $Forward, $TunnelTarget)
}

if ($Stop) { Stop-Children; Write-Log "stopped by request"; exit 0 }
Stop-Children  # leftovers of an earlier supervisor: ending the task ends only this script
$children = @{
  bridge = @{ start = ${function:Start-Bridge}; process = $null; delay = 5; next = [datetime]::MinValue; started = $null }
  tunnel = @{ start = ${function:Start-Tunnel}; process = $null; delay = 5; next = [datetime]::MinValue; started = $null }
}
$nextCleanup = [datetime]::MinValue
while ($true) {
  foreach ($name in @($children.Keys)) {
    $child = $children[$name]
    if ($child.process -and -not $child.process.HasExited) { continue }
    if ($child.process) {
      $lived = ((Get-Date) - $child.started).TotalSeconds
      $child.delay = if ($lived -gt 120) { 10 } else { [Math]::Min([Math]::Max($child.delay * 2, 10), 300) }
      Write-Log "$name exited code=$($child.process.ExitCode) after $([int]$lived)s; retry in $($child.delay)s"
      $child.next = (Get-Date).AddSeconds($child.delay)
      $child.process = $null
    }
    if ((Get-Date) -lt $child.next) { continue }
    try {
      $child.process = & $child.start
      $null = $child.process.Handle  # keeps ExitCode readable after exit
      $child.started = Get-Date
    } catch {
      Write-Log "$name start failed: $_"
      $child.next = (Get-Date).AddSeconds(60)
    }
  }
  if ((Get-Date) -gt $nextCleanup) {
    Get-ChildItem $Logs -File | Where-Object LastWriteTime -lt (Get-Date).AddDays(-14) |
      Remove-Item -Force -ErrorAction SilentlyContinue
    $nextCleanup = (Get-Date).AddHours(6)
  }
  Start-Sleep -Seconds 5
}
