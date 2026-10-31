#!/bin/sh
# Sente 自建版安装（macOS / Linux）。在仓库根目录运行：sh deploy/self-host/install.sh
# 更新：git pull 后再运行一次。国内网络可先设 UV_DEFAULT_INDEX（PyPI 镜像）与 npm 镜像。
set -eu
cd "$(dirname "$0")/../.."

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少 $1，请先安装：$2" >&2; exit 1; }; }
need uv "https://docs.astral.sh/uv/getting-started/installation/"
need npm "Node 22，https://nodejs.org/"

# Same locked set as the cloud images (Dockerfile.cloud), exported so a mirror index can serve it.
install_project() {
  dir="services/$1"
  requirements="$(mktemp)"
  uv venv --python 3.12 --allow-existing "$dir/.venv"
  uv export --project "$dir" --frozen --no-dev --extra "$2" --no-emit-project -o "$requirements"
  uv pip install --python "$dir/.venv" -r "$requirements"
  uv pip install --python "$dir/.venv" --no-deps -e "$dir"
  rm -f "$requirements"
}
install_project trader-runtime research
install_project tqkq-sidecar tqkq

npm ci
VITE_CLOUD_RUNTIME_URL=same-origin VITE_DEFAULT_RUNTIME_MODE=cloud VITE_CLOUD_AUTH_MODE=owner-password \
  VITE_APP_BASE_PATH=/ npm --workspace @ai-trader/client run build

python=services/trader-runtime/.venv/bin/python
[ -f "${SENTE_HOME:-$HOME/.sente}/config.json" ] || "$python" deploy/self-host/sente.py setup
"$python" deploy/self-host/sente.py service install
