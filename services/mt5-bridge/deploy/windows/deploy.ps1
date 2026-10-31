<#
Activates an unpacked bridge release on the 4090 (run as user 1 over SSH):
  powershell -NoProfile -ExecutionPolicy Bypass -File D:\mt5-bridge\releases\<sha>\deploy\windows\deploy.ps1 -Sha <sha>
Builds the release's own .venv from uv.lock (through Clash 127.0.0.1:7897 when it is up), installs
supervise.ps1, records the previous release in previous.txt, registers the scheduled task
"AI Trader MT5 Bridge" (user 1 logon, interactive, limited rights) and restarts it.
Rollback: run the previous release's deploy.ps1 with -Sha (Get-Content D:\mt5-bridge\previous.txt).
#>
param([Parameter(Mandatory = $true)][string]$Sha)
$ErrorActionPreference = "Stop"
$Root = "D:\mt5-bridge"
$TaskName = "AI Trader MT5 Bridge"
$app = Join-Path $Root "releases\$Sha"
if (-not (Test-Path (Join-Path $app "pyproject.toml"))) { throw "release_missing:$app" }
foreach ($secret in "token", "tunnel_ed25519", "known_hosts") {
  if (-not (Test-Path (Join-Path $Root "secrets\$secret"))) { throw "secret_missing:$secret" }
}
if ((Test-NetConnection 127.0.0.1 -Port 7897 -WarningAction SilentlyContinue).TcpTestSucceeded) {
  $env:HTTPS_PROXY = "http://127.0.0.1:7897"; $env:HTTP_PROXY = $env:HTTPS_PROXY
}
Push-Location $app
try { uv sync --frozen --no-dev --python 3.12; if ($LASTEXITCODE) { throw "uv_sync_failed:$LASTEXITCODE" } }
finally { Pop-Location }

$current = Join-Path $Root "current.txt"
if ((Test-Path $current) -and (Get-Content $current -Raw).Trim() -ne $Sha) {
  Copy-Item $current (Join-Path $Root "previous.txt") -Force
}
Copy-Item (Join-Path $app "deploy\windows\supervise.ps1") (Join-Path $Root "supervise.ps1") -Force
Set-Content -Path $current -Value $Sha -Encoding ascii -NoNewline

$user = "$env:COMPUTERNAME\1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Root\supervise.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
  -Description "AI Trader read-only MT5 market-data bridge and its reverse tunnel (issue #976)" -Force | Out-Null

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "supervise.ps1") -Stop
Start-ScheduledTask -TaskName $TaskName
"deployed $Sha; task $((Get-ScheduledTask -TaskName $TaskName).State)"
