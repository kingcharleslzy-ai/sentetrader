#!/usr/bin/env bash
# AI Trader 浸泡测试控制脚本（不进 CI）。在浸泡宿主（默认 mac-mini-cc）上运行，stage 除外。
#
#   soak.sh stage <git-ref> [ssh-host]   在开发机上跑：git archive 指定提交到宿主 $SOAK_ROOT/src/<sha12>/
#   soak.sh up                           启动浸泡 VM（若停着），构建并启动独立 compose project，核对 healthz
#   soak.sh run [--users N] [--duration 24h] [--spawn-rate 1] [--rum|--no-rum] [--label txt]
#                                        后台启动 Locust + 资源采样器（nohup，PID 写进结果目录）
#   soak.sh status                       进程、最近日志、最近一次采样、VM 与宿主内存
#   soak.sh collect [结果目录] [--warmup-minutes 30]  生成 report.md / report.json（默认 $SOAK_ROOT/current）
#   soak.sh down [--stop-vm]             停 Locust/采样器，compose down --volumes --remove-orphans，保留结果
#
# 参数（环境变量）：SOAK_ROOT（默认 /Volumes/BACKUPSSD/mac-mini-cc/ai-trader-soak）、SOAK_SRC（被测源码，
# 默认本脚本所在仓库根）、SOAK_PROJECT（ai-trader-soak）、SOAK_PORT（18180，避开 CI 的 18080）、
# SOAK_PROFILE（Colima profile，默认 sente-linux-trial；拒绝 sente-ci-check）、SOAK_USERS（25）、
# SOAK_DURATION（24h）、SOAK_SPAWN_RATE（1）、SOAK_RUM（on/off）、SOAK_JWT_TTL（3600）、
# SOAK_BUILD_PROXY（构建时 docker CLI 取 registry token 用的代理；默认读宿主系统 HTTPS 代理）。
#
# 边界：所有 docker 命令都用 DOCKER_HOST 指向浸泡 VM 的 socket，从不 `docker context use`、不碰
# sente-ci-check、不 prune；被测栈只能是 fake 数据/假 agent，liveTrading 必须 disabled。
set -euo pipefail

export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOAK_ROOT="${SOAK_ROOT:-/Volumes/BACKUPSSD/mac-mini-cc/ai-trader-soak}"
SOAK_SRC="${SOAK_SRC:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PROJECT="${SOAK_PROJECT:-ai-trader-soak}"
PORT="${SOAK_PORT:-18180}"
PROFILE="${SOAK_PROFILE:-sente-linux-trial}"
LOCUST_VERSION="2.46.7"
VENV="$SOAK_ROOT/venv"
SECRET_FILE="$SOAK_ROOT/jwt-secret"
TENANT="soak-tenant"
BASE_URL="http://127.0.0.1:$PORT"

die() { echo "soak: $*" >&2; exit 1; }
log() { echo "soak: $*" >&2; }

guard() {
  [ "$PROFILE" != "sente-ci-check" ] || die "refusing to use the CI profile sente-ci-check"
  export DOCKER_HOST="unix://$HOME/.colima/$PROFILE/docker.sock"
  export DOCKER_CONFIG="$SOAK_ROOT/docker-config"
  mkdir -p "$DOCKER_CONFIG"
  [ -f "$DOCKER_CONFIG/config.json" ] || \
    printf '{"cliPluginsExtraDirs":["/opt/homebrew/lib/docker/cli-plugins"]}\n' > "$DOCKER_CONFIG/config.json"
}

stack_env() {
  [ -f "$SECRET_FILE" ] || (umask 077 && openssl rand -hex 32 > "$SECRET_FILE")
  AI_TRADER_CLOUD_JWT_SECRET="$(cat "$SECRET_FILE")"
  export AI_TRADER_CLOUD_JWT_SECRET
  export AI_TRADER_CLOUD_AUTH_PROVIDER=development-hs256
  export AI_TRADER_CLOUD_TENANT_ID="$TENANT"
  export AI_TRADER_CLOUD_PORT="$PORT"
  export AI_TRADER_PUBLIC_RUNTIME_URL="$BASE_URL"
  export AI_TRADER_ALLOWED_ORIGINS="$BASE_URL"
  export AI_TRADER_ALLOW_INSECURE_LOCAL_CLOUD=1
  # The staged copy carries .soak-sha; a git checkout answers with its HEAD. A directory name is never an identity.
  AI_TRADER_BUILD_SHA="$(cat "$SOAK_SRC/.soak-sha" 2>/dev/null || git -C "$SOAK_SRC" rev-parse --verify HEAD 2>/dev/null || true)"
  [ -n "$AI_TRADER_BUILD_SHA" ] || die "cannot tell which commit SOAK_SRC=$SOAK_SRC is (no .soak-sha, not a git checkout): use soak.sh stage"
  export AI_TRADER_BUILD_SHA
}

compose() {
  docker compose -p "$PROJECT" -f "$SOAK_SRC/docker-compose.cloud.yml" \
    -f "$SCRIPT_DIR/docker-compose.soak.yml" "$@"
}

build_proxy() {
  if [ -n "${SOAK_BUILD_PROXY+x}" ]; then echo "$SOAK_BUILD_PROXY"; return; fi
  scutil --proxy 2>/dev/null | awk '/HTTPSEnable : 1/{on=1} /HTTPSProxy :/{h=$3} /HTTPSPort :/{p=$3} END{if(on&&h&&p) print "http://" h ":" p}'
}

current_dir() { readlink "$SOAK_ROOT/current" 2>/dev/null || true; }
# A pid file only counts while that pid is still our process: after a run ends the number can be
# reused by an unrelated process, and `down` must never signal that one. $2 is a word of our command line.
pid_alive() {
  local pid
  [ -f "$1" ] || return 1
  pid="$(cat "$1")"
  kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o command= 2>/dev/null | grep -q -- "$2"
}

cmd_stage() {
  local ref="${1:?usage: soak.sh stage <git-ref> [ssh-host]}" host="${2:-mac-mini-cc}" sha short top
  # git archive run inside a subdirectory archives only that subtree: always run it at the top level.
  top="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
  sha="$(git -C "$top" rev-parse --verify "$ref^{commit}")"
  short="${sha:0:12}"
  git -C "$top" archive --format=tar "$sha" | ssh "$host" \
    "set -e; d='$SOAK_ROOT/src/$short'; rm -rf \"\$d.tmp\"; mkdir -p \"\$d.tmp\"; tar -x -C \"\$d.tmp\"; \
     echo '$sha' > \"\$d.tmp/.soak-sha\"; rm -rf \"\$d\"; mv \"\$d.tmp\" \"\$d\"; echo \"\$d\""
}

cmd_up() {
  guard
  [ -f "$SOAK_SRC/docker-compose.cloud.yml" ] || die "no docker-compose.cloud.yml under SOAK_SRC=$SOAK_SRC"
  local state
  state="$(colima list 2>/dev/null | awk -v p="$PROFILE" '$1==p{print $2}')"
  [ -n "$state" ] || die "Colima profile $PROFILE does not exist"
  if [ "$state" != "Running" ]; then
    log "starting Colima profile $PROFILE (its own CPU/memory settings)"
    colima start "$PROFILE"
  fi
  docker info --format '{{.Architecture}} {{.NCPU}}cpu {{.MemTotal}}B' >/dev/null || die "docker in $PROFILE unreachable"
  stack_env
  local proxy
  proxy="$(build_proxy)"
  log "building $PROJECT from $SOAK_SRC (commit $AI_TRADER_BUILD_SHA, native platform${proxy:+, CLI proxy $proxy})"
  if [ -n "$proxy" ]; then
    HTTPS_PROXY="$proxy" HTTP_PROXY="$proxy" NO_PROXY="localhost,127.0.0.1" compose build
  else
    compose build
  fi
  compose up -d --wait
  curl -fsS "$BASE_URL/healthz" | python3 -I -c '
import json, sys
d = json.load(sys.stdin)
assert d["liveTrading"] == "disabled" and d["mode"] == "fake" and d["authMode"] == "cloud-jwt", d
print("soak: healthz ok version=%s commit=%s simulationTrading=%s" % (d.get("version"), d.get("commit"), d.get("simulationTrading")))'
  printf '{"project":"%s","port":%s,"src":"%s","commit":"%s","profile":"%s"}\n' \
    "$PROJECT" "$PORT" "$SOAK_SRC" "$AI_TRADER_BUILD_SHA" "$PROFILE" > "$SOAK_ROOT/stack.json"
}

ensure_locust() {
  if [ ! -x "$VENV/bin/locust" ] || ! "$VENV/bin/locust" --version 2>/dev/null | grep -q "locust $LOCUST_VERSION "; then
    local proxy
    proxy="$(build_proxy)"
    log "installing locust==$LOCUST_VERSION into $VENV"
    HTTPS_PROXY="$proxy" HTTP_PROXY="$proxy" uv venv -q --allow-existing --python 3.12 "$VENV"
    HTTPS_PROXY="$proxy" HTTP_PROXY="$proxy" uv pip install -q --python "$VENV/bin/python" "locust==$LOCUST_VERSION"
  fi
}

cmd_run() {
  guard
  local users="${SOAK_USERS:-25}" duration="${SOAK_DURATION:-24h}" spawn="${SOAK_SPAWN_RATE:-1}"
  local rum="${SOAK_RUM:-on}" label="" run previous
  while [ $# -gt 0 ]; do
    case "$1" in
      --users) users="$2"; shift 2 ;;
      --duration) duration="$2"; shift 2 ;;
      --spawn-rate) spawn="$2"; shift 2 ;;
      --rum) rum=on; shift ;;
      --no-rum) rum=off; shift ;;
      --label) label="-$2"; shift 2 ;;
      *) die "unknown run option $1" ;;
    esac
  done
  previous="$(current_dir)"
  if [ -n "$previous" ] && pid_alive "$previous/locust.pid" locust; then die "a run is still active: $previous"; fi
  curl -fsS "$BASE_URL/healthz" >/dev/null || die "stack not answering on $BASE_URL (soak.sh up first)"
  ensure_locust
  stack_env
  # The result is labelled with SOAK_SRC's commit, so the running stack must be built from that commit.
  curl -fsS "$BASE_URL/healthz" | python3 -I -c '
import json, sys
running, wanted = json.load(sys.stdin).get("commit") or "", sys.argv[1]
n = min(len(running), len(wanted))
if n < 7 or running[:n] != wanted[:n]:
    sys.exit("soak: the stack runs commit %r but SOAK_SRC is %r: run `soak.sh up` first" % (running, wanted))' \
    "$AI_TRADER_BUILD_SHA" || exit 1
  run="$SOAK_ROOT/results/$(date +%Y%m%d-%H%M%S)$label"
  mkdir -p "$run"
  cp "$SCRIPT_DIR/locustfile.py" "$run/locustfile.py"
  local rum_flag="--rum"; [ "$rum" = on ] || rum_flag="--no-rum"
  python3 -I - "$run/meta.json" "$users" "$duration" "$spawn" "$rum" "$PROJECT" "$PORT" "$PROFILE" "$SOAK_SRC" \
    "$AI_TRADER_BUILD_SHA" "$LOCUST_VERSION" "${SOAK_JWT_TTL:-3600}" <<'PY'
import json, sys, time
keys = ["users", "duration", "spawnRate", "rum", "project", "port", "profile", "src", "commit", "locust", "jwtTtl"]
meta = dict(zip(keys, sys.argv[2:]))
meta["startedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
open(sys.argv[1], "w").write(json.dumps(meta, ensure_ascii=False, indent=1) + "\n")
PY
  # The secret reaches Locust through its environment only; nothing is written next to the results.
  nohup env AI_TRADER_CLOUD_TENANT_ID="$TENANT" SOAK_JWT_TTL="${SOAK_JWT_TTL:-3600}" \
    "$VENV/bin/locust" -f "$run/locustfile.py" --headless --host "$BASE_URL" \
    --users "$users" --spawn-rate "$spawn" --run-time "$duration" --stop-timeout 30 \
    --csv "$run/locust" --csv-full-history --only-summary --html "$run/locust.html" \
    --logfile "$run/locust.log" --soak-out "$run" "$rum_flag" > "$run/locust.out" 2>&1 &
  echo $! > "$run/locust.pid"
  nohup python3 -I "$SCRIPT_DIR/sample.py" --project "$PROJECT" --profile "$PROFILE" \
    --out "$run/samples.jsonl" --interval 60 --watch-pid "$(cat "$run/locust.pid")" > "$run/sampler.log" 2>&1 &
  echo $! > "$run/sampler.pid"
  ln -sfn "$run" "$SOAK_ROOT/current"
  log "started: $run (users=$users duration=$duration rum=$rum locust pid $(cat "$run/locust.pid"))"
}

cmd_status() {
  guard
  local run
  run="$(current_dir)"
  echo "== run: ${run:-none}"
  if [ -n "$run" ]; then
    for name in locust sampler; do
      if pid_alive "$run/$name.pid" "$([ "$name" = sampler ] && echo sample.py || echo locust)"; then echo "$name: running (pid $(cat "$run/$name.pid"))"; else echo "$name: stopped"; fi
    done
    [ -f "$run/meta.json" ] && grep -E '"(startedAt|users|duration|rum|commit)"' "$run/meta.json" | tr -d ' ' | paste -sd ' ' -
    echo "-- locust.log (tail)"; tail -n 5 "$run/locust.log" 2>/dev/null || true
    [ -f "$run/samples.jsonl" ] && tail -n 1 "$run/samples.jsonl" | python3 -I -c '
import json, sys, time
d = json.loads(sys.stdin.read())
c = d.get("containers", {}); p = d.get("proc", {}).get("trader-runtime", {})
print("-- last sample %ds ago: %s; runtime fd=%s tcp=%s data=%sKiB; vm avail=%sMiB; host free=%s%%" % (
    time.time() - d["ts"], ", ".join("%s %sMiB %s%%" % (k, v.get("memMiB"), v.get("cpu")) for k, v in c.items()),
    p.get("fd"), p.get("tcp"), p.get("dataKiB"), d.get("vm", {}).get("availableMiB"), d.get("host", {}).get("memFreePercent")))'
  fi
  echo "== colima"; colima list
  echo "== host"; memory_pressure -Q 2>/dev/null || memory_pressure | tail -1
  echo "== stack"; compose ps --format '{{.Service}} {{.State}} {{.Status}}' 2>/dev/null || true
}

cmd_collect() {
  local run
  run="$(current_dir)"
  if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then run="$1"; shift; fi
  [ -n "$run" ] && [ -d "$run" ] || die "no results directory"
  python3 -I "$SCRIPT_DIR/report.py" "$run" "$@"
}

cmd_down() {
  guard
  local run stop_vm=0
  [ "${1:-}" = "--stop-vm" ] && stop_vm=1
  run="$(current_dir)"
  if [ -n "$run" ]; then
    if pid_alive "$run/locust.pid" locust; then
      kill -TERM "$(cat "$run/locust.pid")"
      for _ in $(seq 1 60); do pid_alive "$run/locust.pid" locust || break; sleep 1; done
    fi
    if pid_alive "$run/sampler.pid" sample.py; then
      sleep 2
      kill -TERM "$(cat "$run/sampler.pid")" 2>/dev/null || true
    fi
  fi
  stack_env
  compose down --volumes --remove-orphans
  if [ "$stop_vm" = 1 ]; then colima stop "$PROFILE"; fi
  log "stack down; results kept in ${run:-$SOAK_ROOT/results}"
}

command="${1:-}"
[ $# -gt 0 ] && shift
case "$command" in
  stage) cmd_stage "$@" ;;
  up) cmd_up ;;
  run) cmd_run "$@" ;;
  status) cmd_status ;;
  collect) cmd_collect "$@" ;;
  down) cmd_down "$@" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
