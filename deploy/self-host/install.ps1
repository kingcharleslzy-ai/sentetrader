# Sente 自建版安装（Windows）。在仓库根目录运行：powershell -ExecutionPolicy Bypass -File deploy\self-host\install.ps1
# 更新：git pull 后再运行一次。国内网络可先设 UV_DEFAULT_INDEX（PyPI 镜像）与 npm 镜像。
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\..")

function Need($command, $hint) {
  if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "缺少 $command，请先安装：$hint" }
}
function Run { & $args[0] $args[1..($args.Count - 1)]; if ($LASTEXITCODE -ne 0) { throw "失败：$args" } }
Need uv "https://docs.astral.sh/uv/getting-started/installation/"
Need npm "Node 22，https://nodejs.org/"

# Same locked set as the cloud images (Dockerfile.cloud), exported so a mirror index can serve it.
function Install-Project($name, $extra) {
  $dir = "services\$name"
  $requirements = New-TemporaryFile
  Run uv venv --python 3.12 --allow-existing "$dir\.venv"
  Run uv export --project $dir --frozen --no-dev --extra $extra --no-emit-project -o $requirements
  Run uv pip install --python "$dir\.venv" -r $requirements
  Run uv pip install --python "$dir\.venv" --no-deps -e $dir
  Remove-Item $requirements
}
Install-Project trader-runtime research
Install-Project tqkq-sidecar tqkq

Run npm ci
$env:VITE_CLOUD_RUNTIME_URL = "same-origin"; $env:VITE_DEFAULT_RUNTIME_MODE = "cloud"
$env:VITE_CLOUD_AUTH_MODE = "owner-password"; $env:VITE_APP_BASE_PATH = "/"
Run npm --workspace @ai-trader/client run build

$python = "services\trader-runtime\.venv\Scripts\python.exe"
if (-not (Test-Path (Join-Path $env:USERPROFILE ".sente\config.json"))) { Run $python deploy\self-host\sente.py setup }
Run $python deploy\self-host\sente.py service install
