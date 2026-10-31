#!/usr/bin/env bash
# Move the cloud Runtime (blue-green Runtime slots + tqkq-sidecar and its enabled overlays, nginx,
# referenced secret/config files, data volumes) from the current host to a new one.
# DRY-RUN BY DEFAULT — the plan is printed and read-only checks run; nothing changes until --apply.
# DNS is never touched: the last phase prints the record to change and the rollback.
#
#   deploy/vps/migrate-to-host.sh <new-ssh-alias> --from <old-ssh-alias> [--sha <full-sha>]
#                                 [--phase init|secrets|env|images|data|nginx|up|verify|dns|all]
#                                 [--domain <fqdn>] [--base /opt/ai-trader] [--new-ip <ipv4>]
#                                 [--force-data] [--apply]
#
# Safety contract (#769):
# - Layout preflight first, for every phase and before anything stops or is written: the source's
#   running release must be the blue-green layout (trader-runtime-blue/green + tqkq-sidecar, nginx
#   upstream include), its effective compose (base + images + the overlays .env.production enables)
#   must resolve, every file .env.production references must exist under --base, and the two hosts
#   must really be different machines. --sha must equal the source's running release (default:
#   that release) — a migration never carries new code. Anything else is refused untouched.
# - Every cross-host copy is staged on the target, compared by sha256 on both ends (secret values
#   are never printed, only names) and only then moved into place; any pipe stage failing aborts
#   (pipefail) and leaves the target's live files as they were.
# - data copies whole volumes (Runtime data volume, plus the paper ledger volume when enabled),
#   leaving out only codex-* (removed adapter homes), rebuildable caches and the SQLite main files,
#   which travel as a consistent .backup that must pass integrity_check == ok. It refuses before
#   stopping the source when the target has ai-trader containers running or holds volume contents
#   that are not exactly what the last migration installed (--force-data overrides; the replaced
#   contents are kept beside the volume). Each run writes a new, never-reused backup directory.
# - images are the source's own runtime/sidecar images: the tag's image id must be the one the
#   source's containers run, and the same id on both hosts (no build).
# - --apply holds the deploy/maintenance lock + .deploying on the target from the start, and on the
#   source from the data phase on, so no release or cleanup runs in between. Like deploy-release.sh,
#   a marker is released only when the remote outcome is known: after an ssh disconnect (255) to
#   that host or an interrupted run (signal) it stays until someone confirms nothing still runs.
# - up fails while any ai-trader container still runs on the source (one TqKq login per account), or
#   when the env, a file it references or a state volume on the target is not what was migrated;
#   it switches current only after the Runtime healthz and every started sidecar/pool/paper member
#   pass deploy-release.sh's member_gate checks. verify fails unless the new host's healthz over
#   TLS reports the release, liveTrading disabled and owner-password auth.
#
# The new host's ssh user need not be root: every command that touches $BASE, docker or the data
# volume runs under non-interactive sudo. Lessons from the 2026-09-16 zgo-la → tencent-ai-trader
# move are marked "(09-16)". The runbook is docs/RUNTIME_MIGRATION.md.
set -euo pipefail

usage() { sed -n '7,10p' "$0" >&2; exit 2; }
[ $# -ge 1 ] || usage
NEW=$1; shift
FROM=""
SHA=""
PHASE=all
DOMAIN=ai-runtime.qilinshuzhi.com
BASE=/opt/ai-trader
NEW_IP=""
APPLY=0
FORCE_DATA=0
while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM=$2; shift 2 ;;
    --sha) SHA=$2; shift 2 ;;
    --phase) PHASE=$2; shift 2 ;;
    --domain) DOMAIN=$2; shift 2 ;;
    --base) BASE=$2; shift 2 ;;
    --new-ip) NEW_IP=$2; shift 2 ;;
    --force-data) FORCE_DATA=1; shift ;;
    --apply) APPLY=1; shift ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done
case "$PHASE" in init|secrets|env|images|data|nginx|up|verify|dns|all) ;; *) echo "unknown phase: $PHASE" >&2; usage ;; esac
[ -n "$FROM" ] || { echo "--from <old-ssh-alias> is required" >&2; usage; }
[ "$FROM" != "$NEW" ] || { echo "REFUSED: --from and the new host are the same alias" >&2; exit 2; }
case "$SHA" in "") ;; *[!0-9a-f]*) echo "--sha must be the full 40-char sha" >&2; exit 2 ;; esac
[ -z "$SHA" ] || [ ${#SHA} = 40 ] || { echo "--sha must be the full 40-char sha" >&2; exit 2; }

REPO=$(cd "$(dirname "$0")/../.." && pwd)
PROJECT=ai-trader-production
VOLROOT=/var/lib/docker/volumes
CLOUD_VOL=${PROJECT}_ai-trader-cloud-data
PAPER_VOL=${PROJECT}_ai-trader-paper-data
UPSTREAM_INCLUDE=/etc/nginx/ai-trader-runtime-upstream.conf
NGINX_CONF=/etc/nginx/conf.d/ai-trader-runtime.conf
RUN=$(date -u +%Y%m%dT%H%M%SZ)-$$   # names this run's snapshot/staging dirs; never reused
LTMP=$(mktemp -d)
# Every ssh goes through here. A 255 means the connection dropped: whatever the command started on
# that host (sudo, tar, docker load, the volume install) may still run or have half-finished, so
# the host is recorded as "outcome unknown". A file, not a variable: ssh also runs in pipelines
# and $( ), which are subshells.
ssh() {
  local rc=0
  command ssh "$@" || rc=$?
  if [ "$rc" = 255 ] && [ "$1" != -G ]; then : >"$LTMP/unknown-$1"; fi
  return "$rc"
}
# An --apply run holds the target's deploy/maintenance marker from the first change on, and the data
# phase holds the source's (the marker deploy-release.sh and the maintenance timer take under the
# same lock). As in deploy-release.sh, a marker is released only when this run's remote outcome on
# that host is known: an ssh disconnect there (255) or a signal (rc >= 128) keeps it, so no deploy
# or maintenance sweep enters while a migration step may still be running.
SOURCE_GUARD=0 TARGET_GUARD=0
release_guard() {
  local name=$1 host=$2 rc=$3
  if [ "$rc" = 255 ] || [ "$rc" -ge 128 ] || [ -e "$LTMP/unknown-$host" ]; then
    echo "${name}_GUARD_RETAINED: the outcome of this migration on $host is unknown (ssh disconnect or interrupted run, rc $rc) — confirm none of its tar/sqlite3/docker commands still runs there, then remove $host:$BASE/.deploying" >&2
    return 0
  fi
  ssh "$host" "sudo -n rmdir $BASE/.deploying" || echo "${name}_GUARD_RETAINED: remove $host:$BASE/.deploying once nothing runs" >&2
}
cleanup() {
  local rc=$?
  if [ "$SOURCE_GUARD" = 1 ]; then release_guard SOURCE "$FROM" "$rc"; fi
  if [ "$TARGET_GUARD" = 1 ]; then release_guard TARGET "$NEW" "$rc"; fi
  rm -rf "$LTMP"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

say() { printf '\n== %s\n' "$*"; }
die() { printf 'REFUSED: %s\n' "$*" >&2; exit 1; }
fail() { printf 'FAILED: %s\n' "$*" >&2; exit 1; }
# A state gate stops --apply; a dry-run only reports what the gate sees now (earlier phases of the
# same plan have not really run).
gate() { if [ "$APPLY" = 1 ]; then die "$*"; else printf '   (dry-run) under --apply this stops here: %s\n' "$*"; fi; }
lsha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }
# run <host> <command>: print, and execute only with --apply.
run() { local host=$1; shift; printf '[%s] %s\n' "$host" "$*"; if [ "$APPLY" = 1 ]; then ssh "$host" "$*"; fi; }

# Remote helpers: one bash program per call, fed on stdin and run as root. The library below is
# prepended to each, so the target-state logic is the same in the gate and in the install step.
# read -d '' rather than $(cat <<…): bash 3.2 (macOS) misparses case patterns inside $( ).
IFS= read -r -d '' R_LIB <<'LIB' || true
set -euo pipefail
shopt -s nullglob dotglob
PROJECT=ai-trader-production
VOLROOT=/var/lib/docker/volumes
# sha256 of every file; a reader's -shm and empty -wal hold no data, so a read never counts as a change.
manifest() { (cd "$1" && find . -type f ! -name '*-shm' ! \( -name '*-wal' -size 0 \) -exec sha256sum {} + | LC_ALL=C sort -k2); }
running() { docker ps -q --filter "label=com.docker.compose.project=$PROJECT"; }
owner() { ls -nd "$1" | awk '{print $3":"$4}'; }
# absent | empty | installed (byte-identical to what the last migration installed) | foreign
vol_state() {
  local d=$VOLROOT/$2/_data rec=$1/backups/migrate-installed/$2.sha256 m
  if [ ! -d "$d" ]; then echo absent; return 0; fi
  m=$(manifest "$d") || return 1   # unreadable is not empty
  if [ -z "$m" ]; then echo empty; return 0; fi
  if [ -f "$rec" ] && [ "$m" = "$(cat "$rec")" ]; then echo installed; return 0; fi
  echo foreign
}
LIB
# rsh <host> <program> [args…]: run a helper program (always — callers decide about --apply).
rsh() { local host=$1 prog=$2; shift 2; ssh "$host" "sudo -n bash -s -- $*" <<<"$R_LIB
$prog"; }

# ---------------------------------------------------------------------------------------------
# Preflight: facts about both hosts, read-only. Any unreadable fact refuses (unknown ≠ absent).
IFS= read -r -d '' R_SOURCE_FACTS <<'PROG' || true
base=$1
cur=$(readlink -f "$base/current" || true)
echo "current=$cur"
echo "machine=$(cat /etc/machine-id)"
if [ -e "$base/.deploying" ]; then echo deploying=1; fi
echo "running=$(running | wc -l | tr -d ' ')"
[ -n "$cur" ] && [ -d "$cur" ] || exit 0
c=$cur/deploy/vps/docker-compose.yml
if grep -q '^  trader-runtime-blue:' "$c" && grep -q '^  trader-runtime-green:' "$c" && grep -q '^  tqkq-sidecar:' "$c"; then echo slots=blue-green; fi
if grep -q '^  trader-runtime:' "$c"; then echo slots=legacy; fi
if [ -f /etc/nginx/ai-trader-runtime-upstream.conf ]; then echo "upstream=$(grep -o -m1 '1800[01]' /etc/nginx/ai-trader-runtime-upstream.conf || true)"; fi
env=$base/.env.production
if [ ! -f "$env" ]; then echo env=missing; exit 0; fi
files="-f $c"
[ ! -f "$cur/deploy/vps/images.yml" ] || files="$files -f $cur/deploy/vps/images.yml"
for flag in ACCOUNTS_OVERLAY:accounts TQKQ_POOL:pool PAPER_ENGINE:paper; do
  if grep -q "^AI_TRADER_${flag%%:*}=1\$" "$env"; then
    echo "overlay=${flag#*:}"
    files="$files -f $cur/deploy/vps/docker-compose.${flag#*:}.yml"
  fi
done
if out=$(docker compose -p "$PROJECT" --env-file "$env" $files config --services 2>/dev/null); then
  echo "services=" $out
else
  echo services_error=1
fi
# Host files the compose secrets read: every *_FILE / *_FILE_HOST path in the env file, plus
# everything in $base/secrets (deploy-release requires secrets/voice-realtime-key by name).
lines=$(grep -E '^[A-Za-z0-9_]+_FILE(_HOST)?=' "$env" || true)
while IFS= read -r line; do
  [ -n "$line" ] || continue
  val=${line#*=}; val=${val%\"}; val=${val#\"}; val=${val%\'}; val=${val#\'}
  case "$val" in ""|/dev/null) continue ;; /*) ;; *) echo "badfile=${line%%=*}"; continue ;; esac
  if [ -f "$val" ]; then echo "file=$val"; else echo "missingfile=$val"; fi
done <<<"$lines"
for f in "$base"/secrets/*; do [ ! -f "$f" ] || echo "file=$f"; done
if [ -f "$VOLROOT/${PROJECT}_ai-trader-cloud-data/_data/ai-trader.sqlite3" ]; then echo vol=cloud; fi
if [ -f "$VOLROOT/${PROJECT}_ai-trader-paper-data/_data/paper.sqlite3" ]; then echo vol=paper; fi
PROG

say "preflight: layout and identity of $FROM → $NEW (read-only)"
FACTS=$(rsh "$FROM" "$R_SOURCE_FACTS" "$BASE") || die "cannot read the layout of $FROM (ssh/sudo failed) — nothing was changed"
NEW_FACTS=$(ssh "$NEW" "cat /etc/machine-id && if sudo -n test -e $BASE/.deploying; then echo DEPLOYING; fi") || die "cannot reach $NEW (ssh failed) — nothing was changed"
NEW_MACHINE=$(printf '%s\n' "$NEW_FACTS" | head -n1)
case "$NEW_FACTS" in *DEPLOYING*) die "$NEW has $BASE/.deploying — a deploy or maintenance run is active or was interrupted there; inspect it first" ;; esac
SRC_CURRENT="" SRC_MACHINE="" SRC_RUNNING="" SLOTS="" UPSTREAM="" ENV_STATE="" OVERLAYS="" SERVICES="" SERVICES_ERROR=0 FILES="" BAD="" VOLS_SEEN=""
while IFS= read -r line; do
  case "$line" in
    current=*) SRC_CURRENT=${line#current=} ;;
    machine=*) SRC_MACHINE=${line#machine=} ;;
    running=*) SRC_RUNNING=${line#running=} ;;
    slots=*) SLOTS="$SLOTS ${line#slots=}" ;;
    upstream=*) UPSTREAM=${line#upstream=} ;;
    env=*) ENV_STATE=${line#env=} ;;
    overlay=*) OVERLAYS="$OVERLAYS ${line#overlay=}" ;;
    services=*) SERVICES=" ${line#services=} " ;;
    services_error=*) SERVICES_ERROR=1 ;;
    file=*) FILES="$FILES
${line#file=}" ;;
    missingfile=*|badfile=*) BAD="$BAD ${line}" ;;
    vol=*) VOLS_SEEN="$VOLS_SEEN ${line#vol=}" ;;
    deploying=*) die "$FROM has $BASE/.deploying — deploy-release.sh or maintenance is active or ended without a verified outcome; inspect it first" ;;
  esac
done <<<"$FACTS"
has() { case " $1 " in *" $2 "*) return 0 ;; *) return 1 ;; esac; }

[ -n "$SRC_MACHINE" ] && [ "$SRC_MACHINE" != "$NEW_MACHINE" ] || die "$FROM and $NEW report the same machine-id — they are one host"
case "$SRC_CURRENT" in
  "$BASE"/releases/*) RUNNING_SHA=${SRC_CURRENT##*/} ;;
  *) die "$FROM has no $BASE/current release (got '${SRC_CURRENT}')" ;;
esac
case "$RUNNING_SHA" in *[!0-9a-f]*) die "$FROM current release '$RUNNING_SHA' is not a full sha" ;; esac
[ ${#RUNNING_SHA} = 40 ] || die "$FROM current release '$RUNNING_SHA' is not a full sha"
if [ -z "$SHA" ]; then SHA=$RUNNING_SHA
elif [ "$SHA" != "$RUNNING_SHA" ]; then die "--sha $SHA is not the release running on $FROM ($RUNNING_SHA); a migration moves the running release only"; fi
SHORT=$(printf '%s' "$SHA" | cut -c1-7)
has "$SLOTS" blue-green && ! has "$SLOTS" legacy || die "$FROM runs a layout this script does not support (expects trader-runtime-blue/green + tqkq-sidecar, no single trader-runtime service; found:${SLOTS:- none})"
case "$UPSTREAM" in 18000) ACTIVE=blue ;; 18001) ACTIVE=green ;; *) die "$FROM has no nginx upstream include naming a slot ($UPSTREAM_INCLUDE)" ;; esac
[ "$ENV_STATE" != missing ] || die "$FROM has no $BASE/.env.production"
[ "$SERVICES_ERROR" = 0 ] || die "the effective compose on $FROM (base + images + enabled overlays) does not resolve"
NEED_SERVICES="trader-runtime-blue trader-runtime-green tqkq-sidecar"
has "$OVERLAYS" accounts && NEED_SERVICES="$NEED_SERVICES tqkq-sidecar-2"
has "$OVERLAYS" pool && NEED_SERVICES="$NEED_SERVICES tqkq-pool"
has "$OVERLAYS" paper && NEED_SERVICES="$NEED_SERVICES paper-engine"
for s in $NEED_SERVICES; do has "$SERVICES" "$s" || die "service $s missing from the effective compose on $FROM"; done
[ -z "$BAD" ] || die ".env.production on $FROM references files that are missing or relative:$BAD"
FILES=$(printf '%s\n' "$FILES" | sed '/^$/d' | LC_ALL=C sort -u)
REL_FILES=""
for f in $FILES; do
  case "$f" in "$BASE"/*) REL_FILES="$REL_FILES ${f#"$BASE"/}" ;; *) die "referenced file $f lies outside $BASE — not supported, move it under $BASE first" ;; esac
done
has "$VOLS_SEEN" cloud || die "$FROM has no Runtime database in $VOLROOT/$CLOUD_VOL"
# Volumes that hold state: the Runtime data volume, and the paper ledger when the paper engine is
# enabled. The tqkq/pool volumes are the sidecars' HOME (TqSdk caches) and are rebuilt on start.
DATA_SPECS="$CLOUD_VOL:ai-trader.sqlite3"
if has "$OVERLAYS" paper; then
  has "$VOLS_SEEN" paper || die "the paper overlay is enabled on $FROM but $VOLROOT/$PAPER_VOL has no paper.sqlite3"
  DATA_SPECS="$DATA_SPECS $PAPER_VOL:paper.sqlite3"
fi
git -C "$REPO" cat-file -e "$SHA^{commit}" 2>/dev/null || die "release $SHORT is not in the local repository ($REPO) — fetch it first"
# Refresh the authority even for dry-run: a candidate's own HEAD may still carry the old timer.
git -C "$REPO" fetch origin refs/heads/main:refs/remotes/origin/main --no-tags || {
  echo "MAINTENANCE_TIMER_MAIN_UNREADABLE: cannot refresh main; no remote changes made" >&2; exit 1;
}
TIMER=deploy/vps/ai-trader-maintenance.timer
if ! git -C "$REPO" cat-file -e "origin/main:$TIMER" || ! git -C "$REPO" cat-file -e "$SHA:$TIMER" ||
   ! git -C "$REPO" diff --quiet origin/main "$SHA" -- "$TIMER"; then
  echo "MAINTENANCE_TIMER_MISMATCH: candidate timer is missing, unreadable, or differs from refreshed main; carry main's timer into the candidate. No remote changes made." >&2
  exit 1
fi

git -C "$REPO" show "$SHA:deploy/vps/docker-compose.yml" | grep -q '^  trader-runtime-blue:' || die "release $SHORT's compose is not blue-green"
git -C "$REPO" show "$SHA:deploy/vps/nginx-ai-trader-runtime.conf" | grep -q "include $UPSTREAM_INCLUDE;" || die "release $SHORT's nginx template has no upstream include"
COMPOSE_FILES="-f $BASE/releases/$SHA/deploy/vps/docker-compose.yml -f $BASE/releases/$SHA/deploy/vps/images.yml"
for o in $OVERLAYS; do COMPOSE_FILES="$COMPOSE_FILES -f $BASE/releases/$SHA/deploy/vps/docker-compose.$o.yml"; done
COMPOSE="docker compose -p $PROJECT --env-file $BASE/.env.production"
RUNDIR=$BASE/backups/migrate-$SHORT-$RUN
# The containers a working source runs: every effective service except the idle slot. The rollback
# list is this set plus whatever else runs, so it stays complete even if the source is already
# (partly) stopped when the data phase starts.
IDLE=green; [ "$ACTIVE" = blue ] || IDLE=blue
EXPECTED=""
for s in $SERVICES; do [ "$s" = "trader-runtime-$IDLE" ] || EXPECTED="$EXPECTED,${PROJECT}-$s-1"; done
EXPECTED=${EXPECTED#,}
echo "   release $SHORT (running on $FROM), serving slot $ACTIVE, overlays:${OVERLAYS:- none}, ${SRC_RUNNING:-0} containers running on $FROM"
echo "   referenced files: $(printf '%s\n' $REL_FILES | wc -l | tr -d ' ') under $BASE; data volumes: $(printf '%s ' $DATA_SPECS | sed 's/:[^ ]*//g')"
[ "$APPLY" = 1 ] || echo "   DRY RUN — add --apply to execute. run id $RUN"

want() { case "$PHASE" in all|"$1") return 0 ;; *) return 1 ;; esac; }

if [ "$APPLY" = 1 ]; then
  ssh "$NEW" "sudo -n mkdir -p $BASE && sudo -n flock -n /run/lock/ai-trader-update-maintenance.lock mkdir $BASE/.deploying" \
    || die "a deploy or maintenance run holds $NEW (lock or $BASE/.deploying) — nothing was changed"
  TARGET_GUARD=1
fi

# copy_base_files <label> <paths relative to $BASE…>: tar from $FROM into a staging directory on
# $NEW, compare sha256 of every file on both ends (names only are printed), then rename each into
# place. A failing pipe or a mismatch leaves $NEW's live files untouched.
copy_base_files() {
  local label=$1; shift
  local inc=$BASE/.migrate-incoming-$RUN-$label
  printf '[%s→%s] %s\n' "$FROM" "$NEW" "$*"
  [ "$APPLY" = 1 ] || return 0
  if ! ssh "$FROM" "sudo -n tar -C $BASE -c --numeric-owner -- $*" \
      | ssh "$NEW" "sudo -n sh -c 'umask 077; mkdir -p $inc && tar -x -p --same-owner --numeric-owner -C $inc'"; then
    ssh "$NEW" "sudo -n rm -rf $inc" || true
    fail "copying $label from $FROM failed; $NEW's files are unchanged"
  fi
  local src dst
  src=$(ssh "$FROM" "sudo -n sh -c 'cd $BASE && sha256sum -- $*'") || fail "hashing $label on $FROM failed"
  dst=$(ssh "$NEW" "sudo -n sh -c 'cd $inc && sha256sum -- $*'") || fail "hashing staged $label on $NEW failed"
  if [ "$src" != "$dst" ]; then
    ssh "$NEW" "sudo -n rm -rf $inc" || true
    fail "$label differs between $FROM and $NEW after the copy ($(diff <(printf '%s\n' "$src") <(printf '%s\n' "$dst") | awk '/^[<>]/{print $3}' | sort -u | tr '\n' ' ')); $NEW's files are unchanged"
  fi
  ssh "$NEW" "sudo -n bash -s -- $BASE $inc $*" <<'PROG'
set -euo pipefail
base=$1 inc=$2; shift 2
for r; do mkdir -p "$(dirname "$base/$r")"; mv -f "$inc/$r" "$base/$r"; done
[ ! -d "$base/secrets" ] || chmod 700 "$base/secrets"
rm -rf "$inc"
PROG
  dst=$(ssh "$NEW" "sudo -n sh -c 'cd $BASE && sha256sum -- $*'") || fail "re-hashing $label on $NEW failed"
  [ "$src" = "$dst" ] || fail "$label on $NEW does not match $FROM after install"
  echo "   $label: $(printf '%s\n' "$src" | wc -l | tr -d ' ') file(s) identical on both hosts (sha256)"
}

# push_file <local file> <remote path>: stage, compare sha256, rename into place.
push_file() {
  local file=$1 dest=$2 sum
  printf '[local→%s] %s\n' "$NEW" "$dest"
  [ "$APPLY" = 1 ] || return 0
  sum=$(lsha "$file" | awk '{print $1}')
  ssh "$NEW" "sudo -n sh -c 'mkdir -p \$(dirname $dest) && cat > $dest.incoming-$RUN'" <"$file" || fail "upload of $dest failed"
  [ "$(ssh "$NEW" "sudo -n sha256sum $dest.incoming-$RUN" | awk '{print $1}')" = "$sum" ] || {
    ssh "$NEW" "sudo -n rm -f $dest.incoming-$RUN" || true; fail "$dest arrived corrupted; the live file is unchanged"; }
  ssh "$NEW" "sudo -n mv -f $dest.incoming-$RUN $dest"
}

# source_quiet: proves no ai-trader container runs on $FROM (an unreachable host is not "stopped").
source_quiet() {
  local n
  n=$(ssh "$FROM" "sudo -n docker ps -q --filter label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ') \
    || { gate "cannot read the containers on $FROM — it must be proven stopped (one TqKq login per account)"; return 0; }
  [ "$n" = 0 ] || gate "$n ai-trader container(s) still run on $FROM — stop them first (one TqKq login per account, one SQLite writer)"
}

if want init; then
  say "init: packages, directories, firewall on $NEW"
  # docker.io conflicts with a preinstalled docker-ce (cloud images ship one); only install when absent. (09-16)
  run "$NEW" "sudo -n apt-get update -qq && sudo -n apt-get install -y -qq nginx certbot python3 ufw fail2ban sqlite3 && (sudo -n docker compose version >/dev/null 2>&1 || sudo -n apt-get install -y -qq docker.io docker-compose-v2)"
  run "$NEW" "sudo -n mkdir -p $BASE/secrets $BASE/releases $BASE/backups $BASE/maintenance /var/www/letsencrypt && sudo -n chmod 700 $BASE/secrets"
  # Containers (172.16.0.0/12) reach host-side relays on the docker bridge address through INPUT: the
  # NAS RSSHub relay (rsshub-tunnel, 11200) and the CFTC egress SOCKS (host-units/ai-trader-egress, 1080).
  run "$NEW" "sudo -n ufw allow 22/tcp && sudo -n ufw allow 80/tcp && sudo -n ufw allow 8443/tcp && sudo -n ufw allow from 172.16.0.0/12 to 172.17.0.1 port 11200 proto tcp comment 'ai-trader rsshub tunnel relay' && sudo -n ufw allow from 172.16.0.0/12 to 172.17.0.1 port 1080 proto tcp comment 'ai-trader cftc egress' && sudo -n ufw --force enable"
  run "$NEW" "sudo -n systemctl enable --now docker nginx fail2ban"
  run "$NEW" "sudo -n docker --version && sudo -n docker compose version && nginx -v"
fi

if want secrets; then
  say "secrets: every file .env.production references plus $BASE/secrets/* (owners and modes kept; values never printed)"
  # shellcheck disable=SC2086
  copy_base_files secrets $REL_FILES
fi

if want env; then
  say "env: $FROM:$BASE/.env.production → $NEW (byte-identical; the release's images.yml pins AI_TRADER_BUILD_SHA)"
  copy_base_files env .env.production
fi

if want images; then
  say "images: git archive $SHORT → $NEW:$BASE/releases/$SHA, the source's runtime + sidecar images, images.yml for the blue-green layout"
  printf '[local→%s] git archive %s (sha256 compared before extracting)\n' "$NEW" "$SHORT"
  if [ "$APPLY" = 1 ]; then
    git -C "$REPO" archive --format=tar "$SHA" >"$LTMP/release.tar"
    push_file "$LTMP/release.tar" "$BASE/releases/.release-$SHA.tar"
    ssh "$NEW" "sudo -n sh -c 'mkdir -p $BASE/releases/$SHA && tar -x -C $BASE/releases/$SHA -f $BASE/releases/.release-$SHA.tar && rm -f $BASE/releases/.release-$SHA.tar'"
  fi
  # The images are the ones the source runs, proven by image id (content-addressed): the source's
  # tag must be the image its containers were created from (a retag after they started — a failed or
  # manual preload — would move an unrelated image under the release's name, and healthz would still
  # report the sha images.yml injects), and the same id on both hosts. A short tag already on the
  # target is reused only when its id matches, otherwise the source's image is moved with
  # docker save | docker load. No build, so no PyPI/registry reach is needed. (09-16)
  SIDE_USERS="${PROJECT}-tqkq-sidecar-1"
  has "$OVERLAYS" accounts && SIDE_USERS="$SIDE_USERS ${PROJECT}-tqkq-sidecar-2-1"
  has "$OVERLAYS" pool && SIDE_USERS="$SIDE_USERS ${PROJECT}-tqkq-pool-1"
  has "$OVERLAYS" paper && SIDE_USERS="$SIDE_USERS ${PROJECT}-paper-engine-1"
  for img in ai-trader-runtime:$SHORT ai-trader-sidecar:$SHORT; do
    case "$img" in ai-trader-runtime:*) users="${PROJECT}-trader-runtime-$ACTIVE-1" ;; *) users=$SIDE_USERS ;; esac
    printf '[%s→%s] %s (image id = what %s run(s) on %s; compared on both hosts)\n' "$FROM" "$NEW" "$img" "$users" "$FROM"
    [ "$APPLY" = 1 ] || continue
    src_id=$(ssh "$FROM" "sudo -n docker image inspect -f '{{.Id}}' $img") || fail "$img is not on $FROM — the running release's image must come from there"
    # Stopped containers keep their image id, so this also holds once the data phase stopped them.
    used=$(ssh "$FROM" "sudo -n docker container inspect -f '{{.Image}}' $users") \
      || fail "cannot read the image of $users on $FROM — the moved image must be proven to be the one they run"
    for u in $used; do
      [ "$u" = "$src_id" ] || fail "$img on $FROM ($src_id) is not the image its containers run ($u) — the tag moved after they started; point it back at the running image before migrating"
    done
    new_id=$(ssh "$NEW" "sudo -n docker image inspect -f '{{.Id}}' $img 2>/dev/null" || true)
    if [ "$new_id" != "$src_id" ]; then
      ssh "$FROM" "sudo -n docker save $img" | ssh "$NEW" "sudo -n docker load -q >/dev/null" || fail "moving $img from $FROM failed"
      new_id=$(ssh "$NEW" "sudo -n docker image inspect -f '{{.Id}}' $img") || fail "$img missing on $NEW after load"
      [ "$new_id" = "$src_id" ] || fail "$img on $NEW ($new_id) is not the image $FROM runs ($src_id)"
    fi
    echo "   $img: same image id on both hosts"
  done
  # The maintenance timer runs scripts/maintain_update_host.py from $BASE/maintenance; both come from the release tree. (09-16)
  run "$NEW" "sudo -n install -m 644 $BASE/releases/$SHA/scripts/maintain_update_host.py $BASE/maintenance/maintain_update_host.py && sudo -n install -m 644 $BASE/releases/$SHA/deploy/vps/ai-trader-maintenance.service $BASE/releases/$SHA/deploy/vps/ai-trader-maintenance.timer /etc/systemd/system/"
  # Install the independent CFTC egress unit; its key remains host-local.
  run "$NEW" "u=ai-trader-egress; id -u \$u >/dev/null 2>&1 || sudo -n useradd --system --create-home --home-dir /var/lib/\$u --shell /usr/sbin/nologin \$u || exit 1; sudo -n install -d -m 700 -o \$u -g \$u /var/lib/\$u/.ssh && sudo -n install -m 644 $BASE/releases/$SHA/deploy/vps/host-units/\$u.service /etc/systemd/system/\$u.service && sudo -n systemctl daemon-reload && sudo -n systemctl enable --now ai-trader-egress"
  # Same images.yml as deploy-release.sh writes: both slots, the sidecar, and each enabled overlay member.
  IMAGES="services:\n  trader-runtime-blue:\n    image: ai-trader-runtime:$SHORT\n    environment:\n      AI_TRADER_BUILD_SHA: $SHA\n  trader-runtime-green:\n    image: ai-trader-runtime:$SHORT\n    environment:\n      AI_TRADER_BUILD_SHA: $SHA\n  tqkq-sidecar:\n    image: ai-trader-sidecar:$SHORT\n"
  has "$OVERLAYS" accounts && IMAGES="$IMAGES  tqkq-sidecar-2:\n    image: ai-trader-sidecar:$SHORT\n"
  has "$OVERLAYS" pool && IMAGES="$IMAGES  tqkq-pool:\n    image: ai-trader-sidecar:$SHORT\n"
  has "$OVERLAYS" paper && IMAGES="$IMAGES  paper-engine:\n    image: ai-trader-sidecar:$SHORT\n"
  run "$NEW" "sudo -n sh -c \"printf '$IMAGES' > $BASE/releases/$SHA/deploy/vps/images.yml\" && cat $BASE/releases/$SHA/deploy/vps/images.yml"
fi

IFS= read -r -d '' R_TARGET_STATE <<'PROG' || true
base=$1; shift
# Assignments, not $(…) inside echo: a failing docker ps or hash must fail this program, never read as 0.
ids=$(running)
echo "running=$(printf '%s' "$ids" | grep -c . || true)"
all=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")
echo "existing=$(printf '%s' "$all" | grep -c . || true)"
# Every project container by name, running or not: the up gate checks they are the ones this
# migration's own up creates, not whatever happens to exist.
names=$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}')
echo "names=$(printf '%s' "$names" | tr '\n' ' ')"
for spec; do
  st=$(vol_state "$base" "${spec%%:*}") || exit 1
  d=$base/backups/migrate-installed
  rec=norec; [ ! -f "$d/${spec%%:*}.sha256" ] || rec=rec
  # "started": this migration's up recorded (a copy of the install record) that it began on exactly
  # the contents the data phase installed; a later data phase rewrites the record and voids it.
  started=notstarted
  if [ "$rec" = rec ] && [ -f "$d/${spec%%:*}.up-started" ] && cmp -s "$d/${spec%%:*}.sha256" "$d/${spec%%:*}.up-started"; then started=started; fi
  echo "vol ${spec%%:*} $st $rec $started"
done
PROG
# check_target_data: before the source is stopped and again right before the install.
check_target_data() {
  local state line bad=""
  state=$(rsh "$NEW" "$R_TARGET_STATE" "$BASE" $DATA_SPECS) || { gate "cannot read the container/volume state of $NEW"; return 0; }
  while IFS= read -r line; do
    case "$line" in
      running=0) ;;
      running=*) gate "${line#running=} ai-trader container(s) run on $NEW — data never replaces a live database" ;;
      existing=*) ;;
      "vol "*" absent "*|"vol "*" empty "*|"vol "*" installed "*) echo "   $NEW ${line#vol }" ;;
      "vol "*) bad="$bad ${line#vol }" ;;
    esac
  done <<<"$state"
  if [ -n "$bad" ]; then
    if [ "$FORCE_DATA" = 1 ]; then echo "   --force-data: replacing$bad (current contents kept as <volume>/_data.before-$RUN)"
    else gate "$NEW already holds data that the last migration did not install:$bad — it may be newer than $FROM; inspect it, or re-run with --force-data to replace it (kept beside the volume)"; fi
  fi
}

IFS= read -r -d '' R_STOP_AND_SNAPSHOT <<'PROG' || true
base=$1 rundir=$2 expected=$3; shift 3
mkdir -p "$base/backups"
mkdir "$rundir"   # unique per run: an earlier snapshot is never overwritten
names=$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}')
# The rollback list: the source's full serving set plus anything else running now.
printf '%s\n' $names ${expected//,/ } | awk 'NF && !seen[$0]++' >"$rundir/stopped-containers"
cp "$rundir/stopped-containers" "$base/backups/migrate-stopped-containers"
others="" side=""
for n in $names; do case "$n" in *-tqkq-sidecar-1) side=$n ;; *) others="$others $n" ;; esac; done
# Runtime slots and netns members first, the sidecar (netns owner, TqKq login) last.
[ -z "$others" ] || docker stop $others >/dev/null
[ -z "$side" ] || docker stop $side >/dev/null
left=$(running)
[ -z "$left" ] || { echo "SOURCE_STILL_RUNNING: $(echo $left)" >&2; exit 1; }
echo "   stopped: $(echo ${names:-nothing was running}) (rollback list: $base/backups/migrate-stopped-containers)"
for spec; do
  vol=${spec%%:*} db=${spec#*:}
  src=$VOLROOT/$vol/_data st=$rundir/$vol
  mkdir "$st"; chown "$(owner "$src")" "$st"
  list=()
  for e in "$src"/*; do
    n=${e##*/}
    case "$n" in
      "$db"|"$db"-wal|"$db"-shm|"$db"-journal) ;;  # travels as the .backup below
      codex-*|exchange-fees.json|market-backup-catalog.json) echo "   $vol: left behind $n" ;;
      *) list+=("$n") ;;
    esac
  done
  if [ ${#list[@]} -gt 0 ]; then tar -C "$src" -c --numeric-owner -- "${list[@]}" | tar -C "$st" -x -p --same-owner --numeric-owner; fi
  sqlite3 "$src/$db" ".backup '$st/$db'"
  chown "$(owner "$src/$db")" "$st/$db"; chmod 600 "$st/$db"
  ic=$(sqlite3 "$st/$db" 'pragma integrity_check')
  [ "$ic" = ok ] || { echo "INTEGRITY_FAILED $vol/$db: $ic" >&2; exit 1; }
  manifest "$st" >"$rundir/$vol.sha256"
  echo "   $vol: snapshot of $(wc -l <"$rundir/$vol.sha256" | tr -d ' ') files, $db integrity ok"
done
PROG

IFS= read -r -d '' R_INSTALL <<'PROG' || true
base=$1 run=$2 force=$3 rundir=$4; shift 4
ids=$(running)
[ -z "$ids" ] || { echo "TARGET_RUNNING: ai-trader containers run on this host" >&2; exit 1; }
for spec; do
  st=$(vol_state "$base" "${spec%%:*}") || exit 1
  case "$st" in
    absent|empty|installed) ;;
    *) [ "$force" = 1 ] || { echo "TARGET_DATA_CHANGED: ${spec%%:*}" >&2; exit 1; } ;;
  esac
  [ -d "$VOLROOT/${spec%%:*}/_data.incoming-$run" ] || { echo "NO_STAGED_DATA: ${spec%%:*}" >&2; exit 1; }
done
mkdir -p "$base/backups/migrate-installed" "$rundir"
for spec; do
  vol=${spec%%:*} db=${spec#*:}
  d=$VOLROOT/$vol/_data inc=$VOLROOT/$vol/_data.incoming-$run
  st=$(vol_state "$base" "$vol") || exit 1
  # Always swap the verified snapshot in, so owners and modes come from it even when the bytes match.
  [ ! -d "$d" ] || mv "$d" "$d.before-$run"
  mv "$inc" "$d"
  # Only foreign (forced) contents are kept; installed ones equal the previous snapshot kept on the source.
  if [ -d "$d.before-$run" ] && [ "$st" != foreign ]; then rm -rf "$d.before-$run"; fi
  if [ -d "$d.before-$run" ]; then echo "   $vol: installed; previous contents kept at $d.before-$run"; else echo "   $vol: installed ($st before)"; fi
  manifest "$d" >"$base/backups/migrate-installed/$vol.sha256"
  cp -p "$d/$db" "$rundir/$vol-$db"
done
PROG

if want data; then
  say "data: target check → stop every ai-trader container on $FROM → consistent snapshot → copy → sha256 on both ends → install on $NEW"
  # 1. Before anything stops: the target must be quiet and hold nothing newer than a previous run installed.
  check_target_data
  # 2. Stop writers and TqKq logins on the source, then snapshot into a new run directory.
  printf '[%s] stop all %s containers (slots, overlays, sidecar last); snapshot %s into %s\n' "$FROM" "$PROJECT" "$DATA_SPECS" "$RUNDIR"
  if [ "$APPLY" = 1 ]; then
    # Same lock and marker as deploy-release.sh / maintenance: no deploy may switch current, env,
    # images or the schema on the source while it is stopped and copied.
    ssh "$FROM" "sudo -n flock -n /run/lock/ai-trader-update-maintenance.lock mkdir $BASE/.deploying" \
      || fail "a deploy or maintenance run holds $FROM (lock or $BASE/.deploying) — nothing was stopped"
    SOURCE_GUARD=1
  fi
  if [ "$APPLY" = 1 ]; then rsh "$FROM" "$R_STOP_AND_SNAPSHOT" "$BASE" "$RUNDIR" "$EXPECTED" $DATA_SPECS || fail "stop/snapshot on $FROM failed — see above; $NEW untouched. Restart $FROM as the dns phase's ROLLBACK step 2 shows"; fi
  # 3. Copy each volume snapshot into a staging directory beside the target volume and compare.
  for spec in $DATA_SPECS; do
    vol=${spec%%:*} db=${spec#*:}
    inc=$VOLROOT/$vol/_data.incoming-$RUN
    printf '[%s→%s] %s (whole volume snapshot) → %s\n' "$FROM" "$NEW" "$vol" "$inc"
    [ "$APPLY" = 1 ] || continue
    ssh "$FROM" "sudo -n tar -C $RUNDIR/$vol -c --numeric-owner ." \
      | ssh "$NEW" "sudo -n sh -c 'docker volume create $vol >/dev/null && mkdir $inc && tar -x -p --same-owner --numeric-owner -C $inc'" \
      || fail "copying $vol failed; $NEW's live volume is untouched ($FROM stays stopped — see the dns phase for the restart)"
    src=$(ssh "$FROM" "sudo -n cat $RUNDIR/$vol.sha256") || fail "reading the $vol manifest on $FROM failed"
    dst=$(rsh "$NEW" 'manifest "$1"' "$inc") || fail "hashing the staged $vol on $NEW failed"
    [ -n "$src" ] && [ "$src" = "$dst" ] || fail "$vol differs between $FROM's snapshot and $NEW's staged copy; $NEW's live volume is untouched"
    ic=$(ssh "$NEW" "sudo -n sqlite3 $inc/$db 'pragma integrity_check'") || fail "integrity_check on $NEW failed"
    [ "$ic" = ok ] || fail "$vol/$db integrity_check on $NEW: $ic"
    echo "   $vol: $(printf '%s\n' "$src" | wc -l | tr -d ' ') files identical on both hosts (sha256), $db integrity ok"
  done
  # 4. Re-check the target and swap the staged volumes in (atomic renames; replaced contents kept).
  printf '[%s] install staged volumes (refuses if containers run or the volume changed since the check)\n' "$NEW"
  if [ "$APPLY" = 1 ]; then
    rsh "$NEW" "$R_INSTALL" "$BASE" "$RUN" "$FORCE_DATA" "$RUNDIR" $DATA_SPECS || fail "install on $NEW refused or failed; staged copies stay at <volume>/_data.incoming-$RUN"
    COUNTS="select 'sessions',count(*) from sessions union all select 'events',count(*) from events union all select 'proposals',count(*) from proposals union all select 'tqkq_sim_order_links',count(*) from tqkq_sim_order_links union all select 'account_trades',count(*) from account_trades"
    # Read from the run's copy, not the live volume (a read would leave files there).
    ssh "$NEW" "sudo -n sqlite3 $RUNDIR/$CLOUD_VOL-ai-trader.sqlite3 \"$COUNTS\"" | tr '\n' ' ' || echo "(row counts unavailable — the sha256 comparison above is the check)"
    echo
  fi
  echo "   (every ai-trader container on $FROM stays stopped: one SQLite writer and one TqKq login per account; rollback list: $FROM:$BASE/backups/migrate-stopped-containers)"
fi

if want nginx; then
  say "nginx: render the release's nginx-ai-trader-runtime.conf for $DOMAIN, upstream include → blue (18000)"
  git -C "$REPO" show "$SHA:deploy/vps/nginx-ai-trader-runtime.conf" | sed "s/ai-runtime\.qilinshuzhi\.com/$DOMAIN/g" >"$LTMP/nginx.conf"
  printf 'upstream ai_trader_runtime { server 127.0.0.1:18000; }\n' >"$LTMP/upstream.conf"
  push_file "$LTMP/upstream.conf" "$UPSTREAM_INCLUDE"
  push_file "$LTMP/nginx.conf" "$NGINX_CONF"
  echo "   certificate: the TLS server block needs /etc/letsencrypt/live/$DOMAIN/ — until DNS points here certbot cannot validate;"
  echo "   either copy the live cert from $FROM — accounts included, or 'certbot renew' has no ACME account (09-16):"
  echo "     ssh $FROM sudo tar -C /etc/letsencrypt -c accounts live archive renewal | ssh $NEW sudo tar -x -C /etc/letsencrypt"
  echo "   or run 'sudo certbot certonly --webroot -w /var/www/letsencrypt -d $DOMAIN' on $NEW right after the DNS switch."
  run "$NEW" "sudo -n nginx -t && sudo -n systemctl reload nginx"
fi

# Each started netns member through its own authenticated /healthz, inside its container — the same
# ports, token and judgment as deploy-release.sh's member_gate (these services have no compose
# healthcheck, so up --wait only proves "running"): the pool answers {status ok, pool true};
# sidecar/paper name themselves, keep live trading disabled and report data normal or stale
# (connecting/unavailable fails). Up to 60 s each. Args: <service>:<kind>…; the caller sets $compose.
IFS= read -r -d '' R_MEMBER_GATE <<'PROG' || true
bad=0
for m; do
  svc=${m%%:*} kind=${m#*:}
  if [ "$kind" = sidecar ]; then port=19001
  elif [ "$kind" = side2 ]; then port=19002
  elif [ "$kind" = pool ]; then port=19010
  else port=19011
  fi
  if [ "$kind" = pool ]; then
    check="d['status']=='ok' and d.get('pool') is True"
  elif [ "$kind" = paper ]; then
    check="d['service']=='ai-trader-paper-engine' and d['liveTrading']=='disabled' and d['dataStatus'] in ('normal','stale')"
  else
    check="d['service']=='ai-trader-tqkq-readonly-sidecar' and d['liveTrading']=='disabled' and d['dataStatus'] in ('normal','stale')"
  fi
  ok=0
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if $compose exec -T "$svc" python -c "import json,os,urllib.request
req=urllib.request.Request('http://127.0.0.1:$port/healthz')
req.add_header('Authorization','Bearer '+open(os.environ['AI_TRADER_READONLY_SIDECAR_TOKEN_FILE']).read().strip())
d=json.load(urllib.request.urlopen(req,timeout=5))
assert $check" >/dev/null 2>&1; then ok=1; break; fi
    sleep 5
  done
  if [ "$ok" = 1 ]; then echo "   $svc: healthy"; else echo "MEMBER_UNHEALTHY: $svc" >&2; bad=1; fi
done
exit "$bad"
PROG

if want up; then
  UP_SERVICES="tqkq-sidecar"
  has "$OVERLAYS" accounts && UP_SERVICES="$UP_SERVICES tqkq-sidecar-2"
  has "$OVERLAYS" pool && UP_SERVICES="$UP_SERVICES tqkq-pool"
  UP_SERVICES="$UP_SERVICES trader-runtime-blue"
  has "$OVERLAYS" paper && UP_SERVICES="$UP_SERVICES paper-engine"
  say "up: $UP_SERVICES on $NEW; healthz must report $SHORT, liveTrading disabled, owner-password"
  # Hard preconditions: nothing of ai-trader runs on the source, the env is the source's, data was installed.
  source_quiet
  # The env and every file it references (secrets, credential key, accounts file) byte-identical.
  src_files=$(ssh "$FROM" "sudo -n sh -c 'cd $BASE && sha256sum -- .env.production $REL_FILES'") || src_files=""
  new_files=$(ssh "$NEW" "sudo -n sh -c 'cd $BASE && sha256sum -- .env.production $REL_FILES' 2>/dev/null") || new_files=""
  [ -n "$src_files" ] && [ "$src_files" = "$new_files" ] || gate "$NEW's .env.production or a file it references is missing or differs from $FROM's — run the secrets and env phases"
  # Every state volume holds what the data phase installed — compose would otherwise create it empty.
  # A volume that has changed since (the Runtime wrote to it) passes only when this migration's own
  # earlier up started on exactly that install (the .up-started record below) and the only
  # containers on the host are the ones that up creates; any other container, or a change nobody
  # here started, is data the migration cannot vouch for.
  UP_NAMES=""
  for s in $UP_SERVICES rsshub-tunnel rsshub; do UP_NAMES="$UP_NAMES ${PROJECT}-$s-1"; done
  if ustate=$(rsh "$NEW" "$R_TARGET_STATE" "$BASE" $DATA_SPECS); then
    names=$(printf '%s\n' "$ustate" | sed -n 's/^names=//p')
    while IFS= read -r line; do
      v=${line#vol }; v=${v%% *}   # "<volume> <state> <rec|norec> <started|notstarted>"
      case "$line" in
        "vol "*" installed rec "*) ;;
        "vol "*" foreign rec started")
          stray=""
          for n in $names; do case " $UP_NAMES " in *" $n "*) ;; *) stray="$stray $n" ;; esac; done
          [ -z "$stray" ] || gate "$v on $NEW: changed since the data phase installed it, and containers this migration's up does not create exist on $NEW:$stray — inspect them" ;;
        "vol "*" foreign rec "*) gate "$v on $NEW: changed since the data phase installed it, and no up of this migration started on it — re-run the data phase" ;;
        "vol "*) gate "$v on $NEW: the data phase has not installed this volume (compose would create it empty)" ;;
      esac
    done <<<"$ustate"
  else
    gate "cannot read the container/volume state of $NEW"
  fi
  # Record, before the first container starts, that this up begins on exactly the installed contents.
  if [ "$APPLY" = 1 ]; then
    rsh "$NEW" 'base=$1; shift
for spec; do v=${spec%%:*}; cp -p "$base/backups/migrate-installed/$v.sha256" "$base/backups/migrate-installed/$v.up-started"; done' "$BASE" $DATA_SPECS \
      || fail "could not record the up start on $NEW — nothing was started"
  else
    printf '[%s] record %s/backups/migrate-installed/<volume>.up-started (copy of the install record)\n' "$NEW" "$BASE"
  fi
  run "$NEW" "cd $BASE && sudo -n $COMPOSE $COMPOSE_FILES up -d --no-build --wait $UP_SERVICES"
  MEMBERS="tqkq-sidecar:sidecar"
  has "$OVERLAYS" accounts && MEMBERS="$MEMBERS tqkq-sidecar-2:side2"
  has "$OVERLAYS" pool && MEMBERS="$MEMBERS tqkq-pool:pool"
  has "$OVERLAYS" paper && MEMBERS="$MEMBERS paper-engine:paper"
  printf '[%s] Runtime healthz, then member health as deploy-release.sh member_gate: %s\n' "$NEW" "$MEMBERS"
  if [ "$APPLY" = 1 ]; then
    STOP_NEW="The new containers are still up and current is NOT switched; stop them on $NEW (sudo docker stop \$(sudo docker ps -q --filter label=com.docker.compose.project=$PROJECT)) before restarting anything on $FROM"
    ssh "$NEW" "curl -fsS http://127.0.0.1:18000/healthz | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['status']=='ok' and d['commit']=='$SHA' and d['liveTrading']=='disabled' and d['authMode']=='owner-password', d; print(json.dumps(d))\"" \
      || fail "healthz on $NEW did not pass. $STOP_NEW"
    # shellcheck disable=SC2086
    rsh "$NEW" "compose='$COMPOSE $COMPOSE_FILES'
$R_MEMBER_GATE" $MEMBERS || fail "a sidecar/pool/paper member on $NEW is not healthy (MEMBER_UNHEALTHY above). $STOP_NEW"
  fi
  # Same as deploy-release.sh: the NAS RSSHub relay is optional for serving.
  run "$NEW" "cd $BASE && sudo -n $COMPOSE $COMPOSE_FILES up -d --no-build --no-deps --wait rsshub-tunnel || echo 'RSSHUB_TUNNEL_START_FAILED: Runtime serves without the NAS RSSHub sources'"
  run "$NEW" "cd $BASE && if sudo -n grep -q '^AI_TRADER_LOCAL_RSSHUB=1\$' $BASE/.env.production; then sudo -n $COMPOSE $COMPOSE_FILES --profile local-rsshub up -d --no-build --no-deps --wait rsshub || echo 'RSSHUB_START_FAILED: Runtime serves without the RSSHub news sources'; fi"
  run "$NEW" "sudo -n ln -sfn $BASE/releases/$SHA $BASE/current && sudo -n systemctl daemon-reload && sudo -n systemctl enable --now ai-trader-maintenance.timer"
fi

if want verify; then
  say "verify (before DNS): TLS healthz of the new host through its IP with the production Host name"
  # api.ipify.org is unreachable from CN hosts; pass --new-ip, or the ssh alias's HostName is used. (09-16)
  [ -n "$NEW_IP" ] || NEW_IP=$(ssh -G "$NEW" 2>/dev/null | awk '/^hostname /{print $2}')
  echo "   curl -fsS --noproxy '*' --resolve $DOMAIN:8443:$NEW_IP https://$DOMAIN:8443/healthz   (--noproxy: a system proxy ignores --resolve)"
  if [ "$APPLY" = 1 ]; then
    [ -n "$NEW_IP" ] || fail "no address for $NEW — pass --new-ip"
    source_quiet
    body=$(curl -fsS --max-time 20 --noproxy '*' --resolve "$DOMAIN:8443:$NEW_IP" "https://$DOMAIN:8443/healthz") || fail "TLS healthz of $NEW via $DOMAIN failed (certificate, nginx or Runtime)"
    printf '%s' "$body" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['status']=='ok' and d['commit']=='$SHA' and d['liveTrading']=='disabled' and d['authMode']=='owner-password', d" \
      || fail "TLS healthz of $NEW does not report status ok / commit $SHORT / liveTrading disabled / owner-password: $body"
    echo "   VERIFIED (automated): TLS healthz on $NEW = $SHORT, liveTrading disabled, owner-password; nothing of ai-trader runs on $FROM"
  fi
  # Host-side units (#793): the installed file must be the release's; active/inactive is reported, not
  # failed — a unit stays dormant until its key is placed by hand (see the unit header), and the
  # Runtime serves without either (CFTC unavailable until then).
  printf '[%s] read back host-units: installed file = release file, enabled, active, listener\n' "$NEW"
  if [ "$APPLY" = 1 ]; then
    for u in ai-trader-egress:172.17.0.1:1080; do
      unit=${u%%:*} addr=${u#*:}
      want=$(git -C "$REPO" show "$SHA:deploy/vps/host-units/$unit.service" | lsha | awk '{print $1}')
      have=$(ssh "$NEW" "sudo -n sha256sum /etc/systemd/system/$unit.service" | awk '{print $1}') || have=""
      [ -n "$want" ] && [ "$want" = "$have" ] || fail "/etc/systemd/system/$unit.service on $NEW is missing or differs from release $SHORT — run the images phase"
      state=$(ssh "$NEW" "systemctl is-enabled $unit 2>&1; systemctl is-active $unit 2>&1; if sudo -n ss -ltn \"( sport = :${addr##*:} )\" | grep -q ${addr%:*}:; then echo listening; else echo not-listening; fi" | tr '\n' ' ') || state="unreadable"
      case "$state" in
        *"enabled active listening"*) echo "   $unit: enabled, active, listening on $addr" ;;
        *) echo "   WARNING $unit on $NEW: $state— place its key as described in deploy/vps/host-units/$unit.service, then systemctl restart $unit" ;;
      esac
    done
  fi
  echo "   NOT automated — check by hand and record the result: sign in against it (Authorization bearer from the desktop"
  echo "   connection file, or the web client with an /etc/hosts override) and read /api/runtime/status (trading·ready),"
  echo "   /api/positions (matches $FROM), /api/voice/realtime/status (keyConfigured)."
fi

if want dns; then
  say "dns: manual step — change the A record of $DOMAIN to the new host; TTL first lowered to the plan minimum (alidns free tier: 600 s) a day ahead"
  echo "   after the switch: healthz via the domain shows the new commit; desktop clients reconnect on their own (same domain, same cookie secret)."
  echo "   ROLLBACK (within 7 days, old host kept intact): point the A record back, then"
  echo "     1. on $NEW stop every ai-trader container and confirm none runs (one TqKq login per account):"
  echo "        sudo docker stop \$(sudo docker ps -q --filter label=com.docker.compose.project=$PROJECT)"
  echo "     2. on $FROM start what the data phase stopped, the sidecar first:"
  echo "        sudo docker start ${PROJECT}-tqkq-sidecar-1 && sudo docker start \$(grep -v tqkq-sidecar-1 $BASE/backups/migrate-stopped-containers)"
  echo "   Rows written on the new host in between are lost on rollback unless saved first — keep the window short and note the time."
fi
