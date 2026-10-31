#!/bin/sh
# Deploy one main commit to the cloud Runtime host: archive the source, build the two images, write
# images.yml and run the per-release deploy script on the host. The deploy script keeps the
# invariants of every earlier deploy-<sha>.sh: backup, env swap, healthz asserting version+commit
# +liveTrading disabled, automatic rollback to the previous release, `current` switch.
#
# Blue-green Runtime slots (trader-runtime-blue 127.0.0.1:18000 / trader-runtime-green
# 127.0.0.1:18001, one shared sidecar netns, one data volume): the host script starts the idle
# slot on the new image, gates its /healthz on the exact commit, flips the nginx upstream include
# (nginx -t then reload), waits ten seconds for in-flight requests, and stops the old slot — the
# graceful shutdown releases the instance lease so the new slot takes over within one heartbeat.
# The sidecar (and tqkq-sidecar-2 / tqkq-pool in its netns) keeps running, so subscriptions and
# sessions survive a deploy, unless deploy/vps/service_plan.py finds that its image source or its
# container definition (compose files, overlays, .env.production values) changed — then exactly
# those containers are recreated, and the namespace members with the sidecar. --dry-run prints that
# plan per service with its reason.
#
# Interruption contract (#715): the host script runs on the host under a transient systemd unit
# (systemd-run --wait) with its stdout/stderr appended to a per-invocation host log, so a dropped
# ssh session or a closed local pipe cannot stop it mid-switch. A HUP/INT/TERM reaching it between
# the env swap and the cutover runs one rollback (blocking further signals) and exits non-zero;
# after the cutover commits, a signal keeps the finished switch — still exiting non-zero, never
# claiming a rollback. The outer script classifies only from markers read back out of that log
# (DEPLOY_OK / DEPLOY_PARTIAL / ROLLBACK_OK / ROLLBACK_FAILED); ssh 255 or a local signal without
# a completion marker is DEPLOY_UNKNOWN, and any outcome without a trusted terminal marker keeps
# the .deploying marker until the unit and log are inspected — never assumed rolled back.
#
# First migration (idempotent, runs inside the deploy when it detects the single-slot layout):
# the legacy trader-runtime container is removed, the whole project is force-recreated so the
# sidecar publishes both slot ports, the nginx template starts including the upstream file, blue
# serves immediately and green is removed until the next deploy needs it. It is a full-stack
# restart; every later deploy is seamless.
#
# Images are built on the home NAS since 2026-10-09 (offloading the 4 vCPU / 3.7 GiB production
# host): docker buildx there builds linux/amd64 and pushes by full-sha tag through the NAS tunnel's
# local forward (NAS 127.0.0.1:15000) into the production host's loopback registry
# (deploy/vps/docker-compose.registry.yml); this script then pulls both images on the host, verifies
# the digest matches what the NAS pushed and only then retags them to the short names the compose
# files use (#667: a run whose verification failed never publishes new short tags, and an existing
# short tag is reused only after the same digest verification passes and the tag provably names the
# same image as its full-sha ref — leftover short tags from an older failed flow are never consumed
# unverified; a retry that cannot prove the cache rebuilds on the NAS and re-verifies).
# A NAS or push failure STOPS the release — the fallbacks are explicit, never silent:
#   --build-on-host     build on the production host itself (the 2026-09-20..2026-10-08 default);
#                       base images via the host's registry mirrors, PyPI via the index below
#   --build-host <alias>  the old two-phase flow on another builder: build there, then
#                       docker save | gzip | ssh | docker load onto the host
#
#   deploy/vps/deploy-release.sh <full-sha> [--host tencent-ai-trader] [--build-on-host]
#                                [--build-host <alias>] [--nas-host <alias>] [--nas-jump <alias|none>] [--nas-build-root <dir>]
#                                [--nas-pypi-index <url>] [--pypi-index <url>] [--dry-run]
#
# It is generated here from variables instead of sed-editing the previous script, which mangled
# the full SHA on three of four releases.
set -eu

SHA=${1:?full git sha}; shift
HOST=tencent-ai-trader
BUILD_HOST=
# Default build path since 2026-10-09: the home NAS builds and pushes into the production host's
# loopback registry over the tunnel. nas-registry | host | remote-save.
BUILD_MODE=nas-registry
NAS_HOST=${NAS_HOST:-nas-ci}
# The workstation reaches the home NAS through the CC Mac on its LAN.
# On the CC Mac itself, use --nas-jump none (or NAS_JUMP="").
NAS_JUMP=${NAS_JUMP-mac-mini-cc}
NAS_BUILD_ROOT=${NAS_BUILD_ROOT:-/home/lizhengyi/ai-trader-builds}
# The NAS has no tencentyun-internal mirror; aliyun is the public CN fallback it can reach.
NAS_PYPI_INDEX=${NAS_PYPI_INDEX:-https://mirrors.aliyun.com/pypi/simple}
# The loopback registry on the production host and the NAS-side end of its push forward.
REGISTRY=127.0.0.1:5000
NAS_PUSH_PREFIX=127.0.0.1:15000
# PyPI index injected into the image builds as PIP_INDEX_URL/UV_DEFAULT_INDEX build args; the
# tencent mirror is reachable from inside the CN cloud at ~50 ms (pypi.org rounds to ~0.9 s).
PYPI_INDEX=${PYPI_INDEX:-https://mirrors.tencentyun.com/pypi/simple}
# Debian mirror for the sidecar image's apt step (libjemalloc2, 2026-10-13) — the APT_MIRROR build
# arg; deb.debian.org is slow from both CN builders. Same split as the PyPI indexes above.
NAS_APT_MIRROR=${NAS_APT_MIRROR:-http://mirrors.aliyun.com}
APT_MIRROR=${APT_MIRROR:-http://mirrors.tencentyun.com}
BASE=/opt/ai-trader
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST=$2; shift 2 ;;
    --build-on-host) BUILD_MODE=host; shift ;;
    --build-host) BUILD_MODE=remote-save; BUILD_HOST=$2; shift 2 ;;
    --nas-host) NAS_HOST=$2; shift 2 ;;
    --nas-jump) NAS_JUMP=$2; [ "$NAS_JUMP" != none ] || NAS_JUMP=; shift 2 ;;
    --nas-build-root) NAS_BUILD_ROOT=$2; shift 2 ;;
    --nas-pypi-index) NAS_PYPI_INDEX=$2; shift 2 ;;
    --pypi-index) PYPI_INDEX=$2; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
# The host-local and save/load paths build on BUILD_HOST (the host itself by default).
BUILD_HOST=${BUILD_HOST:-$HOST}
case "$SHA" in ????????????????????????????????????????) ;; *) echo "need the full 40-char sha" >&2; exit 2 ;; esac
SHORT=$(printf '%s' "$SHA" | cut -c1-7)
REPO=$(cd "$(dirname "$0")/../.." && pwd)
VERSION=$(node -p "require('$REPO/package.json').version")
git -C "$REPO" cat-file -e "$SHA^{commit}" || { echo "unknown commit $SHA" >&2; exit 2; }
if [ "$(git -C "$REPO" show "$SHA:package.json" | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).version")" != "$VERSION" ]; then
  echo "package.json version at $SHORT differs from the checked-out tree ($VERSION)" >&2; exit 2
fi

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

nas_ssh() {
  if [ -n "$NAS_JUMP" ]; then ssh -J "$NAS_JUMP" "$NAS_HOST" "$@"
  else ssh "$NAS_HOST" "$@"; fi
}

# Check the NAS route before acquiring any production maintenance/deploy marker.
if [ "$BUILD_MODE" = nas-registry ] && [ "$DRY" = 0 ]; then
  nas_ssh true || { echo "NAS_CONNECTION_FAILED: production untouched" >&2; exit 1; }
fi

# Maintenance and deploys share one boundary from upload through cutover. Acquire the
# marker while holding maintenance's lock, so a sweep cannot race a new candidate.
# An interrupted/disconnected deploy may leave the marker: inspect it before removing;
# age alone never authorizes maintenance to delete a possibly active release.
DEPLOY_GUARD=0
release_guard() {
  if [ "$DEPLOY_GUARD" = 1 ]; then
    if ssh "$HOST" "sudo rmdir $BASE/.deploying"; then DEPLOY_GUARD=0
    else echo "DEPLOY_GUARD_RETAINED: inspect $HOST:$BASE/.deploying before the next deploy" >&2; fi
  fi
}
if [ "$DRY" = 0 ]; then
  ssh "$HOST" "sudo flock -n /run/lock/ai-trader-update-maintenance.lock mkdir $BASE/.deploying" || {
    echo "DEPLOY_OR_MAINTENANCE_BUSY: no release files changed" >&2; exit 1;
  }
  DEPLOY_GUARD=1
  RETAIN_GUARD=0
  REMOTE_RESULT_KNOWN=0
  # A remote command that returned its own failure has finished (a failed build, or the deploy
  # unit after its rollback): release the guard. ssh's 255 (disconnect) or a signal may leave
  # remote work running — including a signal-exit (rc ≥ 128), where the local exit alone cannot
  # prove the remote script reached its own trap — so the guard stays unless the deploy phase
  # proved a definite remote outcome (REMOTE_RESULT_KNOWN, e.g. an rc ≥ 128 whose read-back log
  # still shows the host script completed its own rollback), or it explicitly asked to retain.
  trap 'rc=$?; if [ "$DEPLOY_GUARD" = 1 ]; then if [ "$RETAIN_GUARD" != 1 ] && { { [ "$rc" -ne 255 ] && [ "$rc" -lt 128 ]; } || [ "$REMOTE_RESULT_KNOWN" = 1 ]; }; then release_guard; fi
    if [ "$DEPLOY_GUARD" = 1 ]; then echo "DEPLOY_GUARD_RETAINED: verify remote work has ended before removing $BASE/.deploying" >&2; fi; fi' EXIT
  trap 'exit 130' INT
  trap 'exit 143' HUP TERM
fi

say() { printf '\n== %s\n' "$*"; }
run() {
  host=$1; shift; printf '[%s] %s\n' "$host" "$*"
  [ "$DRY" = 1 ] && return 0
  ssh "$host" "$@"
}

run_nas() {
  printf '[%s] %s\n' "$NAS_HOST" "$*"
  [ "$DRY" = 1 ] || nas_ssh "$@"
}

case "$BUILD_MODE" in
  nas-registry) say "release $VERSION @ $SHORT → $HOST (images built on $NAS_HOST via ${NAS_JUMP:-direct} and pushed to $HOST's $REGISTRY loopback registry; PyPI via $NAS_PYPI_INDEX)" ;;
  host) say "release $VERSION @ $SHORT → $HOST (images built on $HOST itself; PyPI via $PYPI_INDEX)" ;;
  remote-save) say "release $VERSION @ $SHORT → $HOST (images built on $BUILD_HOST)" ;;
esac
# The deploy scripts and release tree always live on the host; the builder needs the archive too.
for h in "$HOST"; do
  run "$h" "sudo mkdir -p $BASE/releases/$SHA && sudo chown \$(id -un) $BASE/releases/$SHA"
  printf '[local→%s] git archive %s\n' "$h" "$SHORT"
  [ "$DRY" = 1 ] || git -C "$REPO" archive "$SHA" | ssh "$h" "tar -x -C $BASE/releases/$SHA"
done
case "$BUILD_MODE" in
  nas-registry)
    run_nas "mkdir -p $NAS_BUILD_ROOT/$SHA"
    printf '[local→%s] git archive %s\n' "$NAS_HOST" "$SHORT"
    [ "$DRY" = 1 ] || git -C "$REPO" archive "$SHA" | nas_ssh "tar -x -C $NAS_BUILD_ROOT/$SHA"
    ;;
  remote-save)
    run "$BUILD_HOST" "sudo mkdir -p $BASE/releases/$SHA && sudo chown \$(id -un) $BASE/releases/$SHA"
    printf '[local→%s] git archive %s\n' "$BUILD_HOST" "$SHORT"
    [ "$DRY" = 1 ] || git -C "$REPO" archive "$SHA" | ssh "$BUILD_HOST" "tar -x -C $BASE/releases/$SHA"
    ;;
esac

registry_digest() {
  # $1 = nas|host, $2 = repo → the registry digest of the full-sha ref on that side. The ref's
  # existence is confirmed first with a read-only reference-filtered listing (#667): `docker image
  # inspect` of a missing ref also exits 1, which must never read as an infra hard stop. A listing
  # that succeeds but is empty → rc 1, not verifiable (the caller's existing rebuild path covers a
  # missing ref); any failing read — ssh, daemon, the listing itself, or the digest inspect of a
  # ref that provably exists → rc 2, explicit hard stop. Inside the old || list an ssh 255 here
  # was swallowed by set -e suppression and read as an empty digest.
  case "$1" in
    nas) listed=$(nas_ssh "docker image ls --quiet --no-trunc --filter reference=$NAS_PUSH_PREFIX/$2:$SHA" 2>/dev/null) || return 2 ;;
    host) listed=$(ssh "$HOST" "sudo docker image ls --quiet --no-trunc --filter reference=$REGISTRY/$2:$SHA" 2>/dev/null) || return 2 ;;
  esac
  [ -n "$listed" ] || return 1
  case "$1" in
    nas) raw=$(nas_ssh "docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' $NAS_PUSH_PREFIX/$2:$SHA" 2>/dev/null) || return 2 ;;
    host) raw=$(ssh "$HOST" "sudo docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' $REGISTRY/$2:$SHA" 2>/dev/null) || return 2 ;;
  esac
  digest=$(printf '%s\n' "$raw" | sed -n 's/.*@//p' | head -1)
  [ -n "$digest" ] || return 1
  printf '%s\n' "$digest"
}

verify_registry_images() {
  # What the host holds must be byte-identical to what the NAS pushed: the digests of both
  # full-sha refs have to agree on both ends of the registry, and the registry tag already pins
  # the full source sha. rc 2 = a digest read itself failed (explicit, never an empty string);
  # rc 1 = not verifiable (missing ref/digest, or a mismatch); 0 = verified with evidence printed.
  for repo in ai-trader-runtime ai-trader-sidecar; do
    nas_digest=$(registry_digest nas "$repo") || { rc=$?
      if [ "$rc" = 2 ]; then echo "IMAGE_DIGEST_READ_FAILED: $repo nas digest read failed (is the NAS reachable and does it still hold $NAS_PUSH_PREFIX/$repo:$SHA?)" >&2; return 2
      else echo "IMAGE_DIGEST_NOT_VERIFIABLE: no registry digest for $NAS_PUSH_PREFIX/$repo:$SHA on the NAS" >&2; return 1; fi; }
    host_digest=$(registry_digest host "$repo") || { rc=$?
      if [ "$rc" = 2 ]; then echo "IMAGE_DIGEST_READ_FAILED: $repo host digest read failed on $HOST" >&2; return 2
      else echo "IMAGE_DIGEST_NOT_VERIFIABLE: no registry digest for $REGISTRY/$repo:$SHA on $HOST" >&2; return 1; fi; }
    if [ "$nas_digest" != "$host_digest" ]; then
      echo "IMAGE_DIGEST_MISMATCH: $repo nas=$nas_digest host=$host_digest" >&2
      return 1
    fi
    echo "   $repo registry digest $nas_digest verified"
  done
}

say "images"
# Reuse images that already exist on the host (a rerun after a failed deploy); build output must
# never share the pipe with the image stream (it corrupted a transfer on 09-16). A reusable cache
# is proven, never assumed (#667): the nas-registry mode runs the SAME digest validator as a fresh
# release (verify_registry_images) and additionally proves each short tag still names the same
# image as its full-sha ref — a failed release once left short tags that a same-SHA rerun then
# consumed unverified. Anything unproven takes the normal NAS build path below; a digest read that
# itself failed stops the release explicitly.
REUSE=0
if [ "$DRY" = 0 ] && ssh "$HOST" "sudo docker image inspect ai-trader-runtime:$SHORT ai-trader-sidecar:$SHORT >/dev/null 2>&1"; then
  if [ "$BUILD_MODE" = nas-registry ]; then
    verify_rc=0
    verify_registry_images || verify_rc=$?
    if [ "$verify_rc" = 2 ]; then exit 1; fi
    cache_ok=0
    if [ "$verify_rc" = 0 ]; then
      cache_ok=1
      for repo in ai-trader-runtime ai-trader-sidecar; do
        short_id=$(ssh "$HOST" "sudo docker image inspect --format '{{.Id}}' $repo:$SHORT" 2>/dev/null) || { cache_ok=0; break; }
        full_id=$(ssh "$HOST" "sudo docker image inspect --format '{{.Id}}' $REGISTRY/$repo:$SHA" 2>/dev/null) || { cache_ok=0; break; }
        { [ -n "$short_id" ] && [ "$short_id" = "$full_id" ]; } || { cache_ok=0; break; }
      done
    fi
    if [ "$cache_ok" = 1 ]; then REUSE=1; else echo "   cached $SHORT images did not verify; rebuilding and re-verifying" >&2; fi
  else
    # host/remote-save build paths never transit the registry: their short tags are the built
    # bytes themselves, so reuse keeps its old tag-existence meaning.
    REUSE=1
  fi
fi
if [ "$DRY" = 1 ] || [ "$REUSE" = 0 ]; then
  if [ "$BUILD_MODE" = host ]; then
    # Local build on the production host: docker needs sudo there, and no image transfer happens.
    run "$HOST" "cd $BASE/releases/$SHA && sudo docker build -q -f services/trader-runtime/Dockerfile.cloud --build-arg PIP_INDEX_URL='$PYPI_INDEX' --build-arg UV_DEFAULT_INDEX='$PYPI_INDEX' -t ai-trader-runtime:$SHORT . >/dev/null && sudo docker build -q -f services/tqkq-sidecar/Dockerfile.cloud --build-arg PIP_INDEX_URL='$PYPI_INDEX' --build-arg UV_DEFAULT_INDEX='$PYPI_INDEX' --build-arg APT_MIRROR='$APT_MIRROR' -t ai-trader-sidecar:$SHORT . >/dev/null"
  elif [ "$BUILD_MODE" = nas-registry ]; then
    # NAS build → registry push → host pull, unchanged from the formal flow (a rerun rebuilds and
    # re-pushes from the NAS source of truth; no registry-cleanup prerequisite). The push forward
    # (NAS loopback 15000 → host loopback 5000) is held by the NAS's ai-trader-tunnel service; if
    # the NAS or tunnel is down this fails and the release stops — rerun with --build-on-host for
    # the explicit local fallback. Only the ORDER below is #667: both images verify BEFORE any new
    # short tag is written — a run whose verification failed publishes no short tags (older residue
    # may still exist, but the cache entry above never reuses it unproven), and a failure below the
    # tag step leaves the verified images under the full-sha refs and a rerun re-tags them.
    printf '[%s] build → push → %s\n' "$NAS_HOST" "$REGISTRY"
    # Pre-pull the base images as an early-fail check: the NAS daemon resolves images through its
    # registry mirrors (/etc/docker/daemon.json, installed 2026-10-09) — without them BuildKit's
    # resolver dials auth.docker.io directly, bypassing the SOCKS proxy `docker pull` uses, and
    # hangs into a token-fetch timeout from the CN home network.
    if ! run_nas "cd $NAS_BUILD_ROOT/$SHA && for base in \$(awk 'toupper(\$1)==\"FROM\" {print \$2}' services/trader-runtime/Dockerfile.cloud services/tqkq-sidecar/Dockerfile.cloud | sort -u); do docker pull \"\$base\" >/dev/null || exit 1; done && docker build -q -f services/trader-runtime/Dockerfile.cloud --build-arg PIP_INDEX_URL='$NAS_PYPI_INDEX' --build-arg UV_DEFAULT_INDEX='$NAS_PYPI_INDEX' -t $NAS_PUSH_PREFIX/ai-trader-runtime:$SHA . >/dev/null && docker build -q -f services/tqkq-sidecar/Dockerfile.cloud --build-arg PIP_INDEX_URL='$NAS_PYPI_INDEX' --build-arg UV_DEFAULT_INDEX='$NAS_PYPI_INDEX' --build-arg APT_MIRROR='$NAS_APT_MIRROR' -t $NAS_PUSH_PREFIX/ai-trader-sidecar:$SHA . >/dev/null && docker push $NAS_PUSH_PREFIX/ai-trader-runtime:$SHA && docker push $NAS_PUSH_PREFIX/ai-trader-sidecar:$SHA"; then
      echo "NAS_BUILD_OR_PUSH_FAILED: check the NAS ($NAS_HOST: systemctl status ai-trader-tunnel, docker) or rerun with --build-on-host" >&2
      exit 1
    fi
    run "$HOST" "sudo docker pull $REGISTRY/ai-trader-runtime:$SHA && sudo docker pull $REGISTRY/ai-trader-sidecar:$SHA" || {
      echo "REGISTRY_PULL_FAILED: is the registry running on $HOST (docker compose -p ai-trader-registry ps)?" >&2
      exit 1
    }
    [ "$DRY" = 1 ] || verify_registry_images || exit 1
    [ "$DRY" = 1 ] || run "$HOST" "sudo docker tag $REGISTRY/ai-trader-runtime:$SHA ai-trader-runtime:$SHORT && sudo docker tag $REGISTRY/ai-trader-sidecar:$SHA ai-trader-sidecar:$SHORT" || {
      echo "REGISTRY_TAG_FAILED: the verified images are on $HOST under $REGISTRY/<repo>:$SHA; a rerun re-tags them" >&2
      exit 1
    }
    # The NAS build roots are scratch, not rollback state (that is the host's releases tree): keep
    # the newest five for rebuild-after-failure and drop the rest.
    [ "$DRY" = 1 ] || nas_ssh "cd $NAS_BUILD_ROOT && ls -1dt */ 2>/dev/null | tail -n +6 | xargs -r rm -rf"
  else
    run "$BUILD_HOST" "cd $BASE/releases/$SHA && docker build -q -f services/trader-runtime/Dockerfile.cloud -t ai-trader-runtime:$SHORT . >/dev/null && docker build -q -f services/tqkq-sidecar/Dockerfile.cloud -t ai-trader-sidecar:$SHORT . >/dev/null"
    printf '[%s→%s] docker save | gzip | docker load\n' "$BUILD_HOST" "$HOST"
    [ "$DRY" = 1 ] || ssh "$BUILD_HOST" "docker save ai-trader-runtime:$SHORT ai-trader-sidecar:$SHORT | gzip -1" | ssh "$HOST" "gunzip | sudo docker load"
  fi
else
  echo "   images already on $HOST"
fi
if [ "$BUILD_MODE" = nas-registry ]; then
  # NAS image retention (#668), outside the build branch so a same-sha rerun that reuses the host's
  # verified images (REUSE=1) reclaims too: every release left two 127.0.0.1:15000 full-sha refs and its
  # build cache on the NAS, and nothing removed them (136 GB by 2026-10-12). The maintenance
  # owner from this very release's source keeps this release, the host's current release (the
  # rollback once this deploy switches; still current if it fails), the newest three complete
  # release shas (by tag time) and anything tagged within the hour, whole shas with an image in use, then trims the build cache to its bound. It runs
  # after the images verified on the host, so a failure here never touches the release: the
  # images just stay until the next release retries.
  if [ "$DRY" = 1 ]; then
    printf '[%s] %s\n' "$NAS_HOST" "python3 $NAS_BUILD_ROOT/$SHA/scripts/maintain_update_host.py --nas --apply --protect $SHA --protect <current>"
  else
    host_current=$(ssh "$HOST" "readlink -f $BASE/current" 2>/dev/null | sed 's#.*/##') || host_current=
    if printf '%s\n' "$host_current" | grep -Eqx '[0-9a-f]{40}'; then
      nas_ssh "python3 $NAS_BUILD_ROOT/$SHA/scripts/maintain_update_host.py --nas --apply --protect $SHA --protect $host_current" \
        || echo "NAS_RETENTION_FAILED: NAS images and build cache kept; the release continues" >&2
    else
      echo "NAS_RETENTION_SKIPPED: could not read the current release on $HOST; NAS images kept" >&2
    fi
  fi
fi
# .env.production is root-only (0600): the overlay flags must be read with sudo, or the
# images.yml never gains the overlay services and `up --no-build` fails on them (2026-09-22).
# Both slots carry the same image and build SHA: only the slot serving traffic is started.
# The remote command writing images.yml into a release tree ($1) on the host; the dry-run plan
# writes the same file into its temporary tree.
images_yml_cmd() {
  printf '%s' "printf 'services:\n  trader-runtime-blue:\n    image: ai-trader-runtime:$SHORT\n    environment:\n      AI_TRADER_BUILD_SHA: $SHA\n  trader-runtime-green:\n    image: ai-trader-runtime:$SHORT\n    environment:\n      AI_TRADER_BUILD_SHA: $SHA\n  tqkq-sidecar:\n    image: ai-trader-sidecar:$SHORT\n' > $1/deploy/vps/images.yml; if sudo grep -q '^AI_TRADER_ACCOUNTS_OVERLAY=1\$' $BASE/.env.production 2>/dev/null; then printf '  tqkq-sidecar-2:\n    image: ai-trader-sidecar:$SHORT\n' >> $1/deploy/vps/images.yml; fi; if sudo grep -q '^AI_TRADER_TQKQ_POOL=1\$' $BASE/.env.production 2>/dev/null; then printf '  tqkq-pool:\n    image: ai-trader-sidecar:$SHORT\n' >> $1/deploy/vps/images.yml; fi; if sudo grep -q '^AI_TRADER_PAPER_ENGINE=1\$' $BASE/.env.production 2>/dev/null; then printf '  paper-engine:\n    image: ai-trader-sidecar:$SHORT\n' >> $1/deploy/vps/images.yml; fi"
}
run "$HOST" "$(images_yml_cmd $BASE/releases/$SHA)"

say "deploy script"
PREVIOUS=$([ "$DRY" = 1 ] && echo "<current>" || ssh "$HOST" "readlink -f $BASE/current")
[ -n "$PREVIOUS" ] || { echo "no current release on $HOST" >&2; exit 1; }
if [ "$PREVIOUS" = "$BASE/releases/$SHA" ]; then echo "   $SHORT is already current on $HOST"; release_guard; exit 0; fi
# The compose file set of a release tree ($release) with the overlays .env.production enables, and
# the compose command. One text, expanded on the host by both the deploy script and the dry-run plan.
COMPOSE_SH='compose_files="-f $release/deploy/vps/docker-compose.yml -f $release/deploy/vps/images.yml"
if grep -q "^AI_TRADER_ACCOUNTS_OVERLAY=1\$" $base/.env.production 2>/dev/null; then
  compose_files="$compose_files -f $release/deploy/vps/docker-compose.accounts.yml"
fi
if grep -q "^AI_TRADER_TQKQ_POOL=1\$" $base/.env.production 2>/dev/null; then
  compose_files="$compose_files -f $release/deploy/vps/docker-compose.pool.yml"
fi
if grep -q "^AI_TRADER_PAPER_ENGINE=1\$" $base/.env.production 2>/dev/null; then
  compose_files="$compose_files -f $release/deploy/vps/docker-compose.paper.yml"
fi
compose="docker compose -p ai-trader-production --env-file $base/.env.production"'
SCRIPT=$(cat <<EOF
#!/bin/sh
set -eu
base=$BASE
release=$BASE/releases/$SHA
previous=$PREVIOUS
test "\$(readlink -f \$base/current)" = "\$previous"
test ! -e \$base/.env.production.before-$SHORT
docker image inspect ai-trader-runtime:$SHORT ai-trader-sidecar:$SHORT >/dev/null
test -s \$base/secrets/voice-realtime-key
mkdir -p \$base/backups/$VERSION
# The Runtime opens the database in WAL mode (store/base.py), so copying the main file alone loses
# committed transactions that are still only in the -wal (verified 09-19: a cp backup of a live WAL
# database held 0 of 1 committed rows and still passed integrity_check). Take a SQLite-consistent
# backup instead and read it back from its own directory: integrity_check plus the row counts of the
# business tables, which must land between the source counts taken before and after the copy. A
# mismatch aborts the release here, before the env swap and before any container is touched.
db=/var/lib/docker/volumes/ai-trader-production_ai-trader-cloud-data/_data/ai-trader.sqlite3
backup=\$base/backups/$VERSION/before-$SHORT.sqlite3
rm -f "\$backup" "\$backup-wal" "\$backup-shm"
checker=\$(mktemp)
cat > "\$checker" <<'PY'
import sqlite3, sys

TABLES = ("sessions", "events", "proposals", "proposal_audit", "tqkq_sim_order_links",
          "ai_execution_authorizations", "ai_stop_orders", "account_trades",
          "strategy_drafts", "strategy_execution_attempts", "user_preferences")


def counts(connection):
    present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {name: connection.execute("SELECT count(*) FROM " + name).fetchone()[0]
            for name in TABLES if name in present}


source_path, backup_path = sys.argv[1], sys.argv[2]
source = sqlite3.connect(source_path)
backup = sqlite3.connect(backup_path)
before = counts(source)
source.backup(backup)
after = counts(source)
copied = counts(backup)
integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
backup.close()
source.close()
# A write landing during the copy may put the backup one row ahead of "before"; losing a committed
# row (the cp defect) puts it below, and that is what fails the release.
drifted = [name for name in before if not before[name] <= copied.get(name, -1) <= after[name]]
if integrity != "ok" or drifted or set(copied) != set(before):
    print("backup inconsistent: integrity=%s tables=%s before=%s copied=%s after=%s"
          % (integrity, drifted, before, copied, after))
    sys.exit(1)
print("backup verified: " + ", ".join("%s=%d" % (name, copied[name]) for name in sorted(copied)))
PY
if ! python3 "\$checker" "\$db" "\$backup"; then rm -f "\$checker"; echo BACKUP_NOT_CONSISTENT >&2; exit 1; fi
rm -f "\$checker"
nginx_include=/etc/nginx/ai-trader-runtime-upstream.conf
nginx_conf=/etc/nginx/conf.d/ai-trader-runtime.conf
include_existed=0
[ -f \$nginx_include ] && include_existed=1
include_before=\$(cat \$nginx_include 2>/dev/null || true)
# The include file names the serving slot. Before it exists (first migration) nginx still
# proxies 18000 directly, which is blue's port either way.
active=blue; active_port=18000
if printf '%s' "\$include_before" | grep -q 18001; then active=green; active_port=18001; fi
idle=blue; idle_port=18000
if [ \$active = blue ]; then idle=green; idle_port=18001; fi
legacy_runtime=\$(docker ps -aq --filter label=com.docker.compose.project=ai-trader-production --filter label=com.docker.compose.service=trader-runtime)
legacy_previous=0
if grep -q '^  trader-runtime:' \$previous/deploy/vps/docker-compose.yml; then legacy_previous=1; fi
# The rollback rebuilds from the previous release's own files: compose + images (its image SHAs)
# plus the overlays the restored env file enables. It is assembled inside rollback(), after the
# env file is back, so the previous release's own flags decide what gets restored.
$COMPOSE_SH
# What this release does to each container of the sidecar namespace, and why (service_plan.py:
# image source and compose definition against the running containers). Decided before anything
# is switched, so a failure here leaves the host as it was.
plan=\$(python3 \$release/deploy/vps/service_plan.py --project ai-trader-production --previous \$previous \
  --release \$release --idle \$idle -- \$compose \$compose_files) || { echo SERVICE_PLAN_FAILED >&2; exit 1; }
echo "service plan:"
printf '%s\n' "\$plan"
side=\$(printf '%s\n' "\$plan" | sed -n 's/^netns-recreate: *//p')
# Rollback restores the previous environment file and the previous containers only. The database is
# never rolled back: the older backup above is a forensic copy, and restoring it over the live file
# would delete every order, proposal and conversation written since this deploy started.
# What rollback restores is decided by what this deploy switched, recorded before each first
# command (partial execution counts): once the sidecar netns was recreated — the sidecar refresh
# or a first migration — the whole netns is rebuilt from the previous release's own
# compose/images/env (sidecar, enabled overlays, the old serving slot, the paper engine whenever
# the previous release ran it); with only the paper engine switched, the sidecar keeps its
# sessions and paper plus the old slot come back; an untouched switch keeps the lightweight
# slot-only restore. A legacy previous release (single-slot docker-compose.yml) is restored whole:
# both slots release the netns ports first, the saved pre-migration nginx template comes back, and
# force-recreate rebuilds the old layout including the sidecar without the second host port.
# Signals during the switch end the deploy non-zero after exactly one rollback (#715): the old
# trap ran rollback and returned, so the script resumed, deleted the restored old slot and still
# switched current and printed DEPLOY_OK. Rollback blocks further signals (a second one
# re-entering it is what deleted the restored old slot) and records its own failures: every
# restore step counts (the env file, the nginx config test and reload, the idle slot removal), an
# incomplete restore prints ROLLBACK_FAILED and exits non-zero, and only a complete one prints
# ROLLBACK_OK — the outer script calls it "rolled back" on that marker alone.
# Poll one slot's /healthz until it reports exactly the given release; a mismatch never heals
# by waiting, so the gate fails fast and the slot that serves traffic is never touched. It is
# also reused by rollback to verify the restored slot before ROLLBACK_OK, hence the parameters.
gate() {
  port=\$1; want_version=\${2:-$VERSION}; want_sha=\${3:-$SHA}; deadline=\$(date +%s)
  deadline=\$((deadline + 60))
  while :; do
    body=\$(curl -fsS http://127.0.0.1:\$port/healthz 2>/dev/null || true)
    if [ -n "\$body" ] && printf '%s' "\$body" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['status']=='ok' and d['version']=='\$want_version' and d['commit']=='\$want_sha' and d['adapter']=='native-agent' and d['mode']=='native' and d['liveTrading']=='disabled' and d['authMode']=='owner-password'" >/dev/null 2>&1; then
      printf '%s\n' "\$body"; return 0
    fi
    if [ "\$(date +%s)" -ge "\$deadline" ]; then return 1; fi
    sleep 5
  done
}
# A rebuilt netns member proves it came back through its own /healthz — reachable only inside
# the netns, so the check runs in the member's container (python + urllib, like the compose
# healthchecks). The Bearer token is read inside the container from the mounted secret the env
# points at; it never appears on the command line or in a log. Each member is judged by its real
# health semantics, not one forced enum: the pool answers {status ok, pool true}; sidecar/paper
# must name themselves, keep live trading disabled and report live data — normal or stale counts
# as restored (stale is data freshness, not availability), while connecting, unavailable or a
# wrong identity never passes as a completed restore.
member_gate() {
  svc=\$1; kind=\$2
  # if/elif rather than case: a case pattern's closing parenthesis cannot be parsed by the
  # outer script while it reads this unquoted heredoc (sh -n fails on it), and a backquoted
  # note here would even be executed by the outer shell at generation time.
  if [ \$kind = sidecar ]; then port=19001
  elif [ \$kind = side2 ]; then port=19002
  elif [ \$kind = pool ]; then port=19010
  else port=19011
  fi
  if [ \$kind = pool ]; then
    check="d['status']=='ok' and d.get('pool') is True"
  elif [ \$kind = paper ]; then
    check="d['service']=='ai-trader-paper-engine' and d['liveTrading']=='disabled' and d['dataStatus'] in ('normal','stale')"
  else
    check="d['service']=='ai-trader-tqkq-readonly-sidecar' and d['liveTrading']=='disabled' and d['dataStatus'] in ('normal','stale')"
  fi
  deadline=\$(date +%s)
  deadline=\$((deadline + 60))
  while :; do
    if \$compose \$prev_files exec -T \$svc python -c "import json,os,urllib.request
req=urllib.request.Request('http://127.0.0.1:\$port/healthz')
req.add_header('Authorization','Bearer '+open(os.environ['AI_TRADER_READONLY_SIDECAR_TOKEN_FILE']).read().strip())
d=json.load(urllib.request.urlopen(req,timeout=5))
assert \$check" >/dev/null 2>&1; then return 0; fi
    if [ "\$(date +%s)" -ge "\$deadline" ]; then return 1; fi
    sleep 5
  done
}
# Whether the previous release ran a sidecar-image member: its own images.yml names every one it
# started. A member this deploy created for the first time (its overlay was enabled just now) has
# no previous image, so rollback removes it and does not try to restore it.
prev_ran() { grep -q "^  \$1:" \$previous/deploy/vps/images.yml 2>/dev/null; }
# Checkpoint gate before the previous paper engine starts (#527/#510): since #813 the engine commits
# small head rows and the whole-book state_json older releases read is only a checkpoint, merged
# at a clean stop. A stop that was cut short (SIGKILL after the compose grace, a stuck actor, a
# failed merge) leaves it behind, and an older engine would serve that stale book. This release's
# image (it knows the head rows) merges the checkpoints on the stopped volume and verifies every
# account (revision = head revision = checkpoint_of); --checkpoint-db exits non-zero otherwise,
# and the rollback stops before the old engine instead of starting it on a stale snapshot.
paper_checkpoint_gate() {
  docker run --rm --network none -v ai-trader-production_ai-trader-paper-data:/data ai-trader-sidecar:$SHORT python -m tqkq_readonly_sidecar.paper --checkpoint-db /data/paper.sqlite3 && return 0
  echo "ROLLBACK_PAPER_CHECKPOINT_FAILED: paper checkpoints are not caught up; the previous paper engine was not started (run --checkpoint-db with ai-trader-sidecar:$SHORT on the stopped volume, then start it)" >&2
  return 1
}
rollback_failed=0
cutover_started=0
switched=0
sidecar_touched=0
paper_touched=0
rollback() {
  trap '' HUP INT TERM
  rollback_failed=0
  cp -p \$base/.env.production.before-$SHORT \$base/.env.production || rollback_failed=1
  # The restored env file decides what the previous release ran, and its own compose/images
  # files carry the previous image SHAs — the restore always selects from the previous release,
  # never from this deploy's files (which would leave a restored Runtime on the new sidecar).
  prev_files="-f \$previous/deploy/vps/docker-compose.yml -f \$previous/deploy/vps/images.yml"
  if grep -q '^AI_TRADER_ACCOUNTS_OVERLAY=1\$' \$base/.env.production 2>/dev/null; then
    prev_files="\$prev_files -f \$previous/deploy/vps/docker-compose.accounts.yml"
  fi
  if grep -q '^AI_TRADER_TQKQ_POOL=1\$' \$base/.env.production 2>/dev/null; then
    prev_files="\$prev_files -f \$previous/deploy/vps/docker-compose.pool.yml"
  fi
  if grep -q '^AI_TRADER_PAPER_ENGINE=1\$' \$base/.env.production 2>/dev/null; then
    prev_files="\$prev_files -f \$previous/deploy/vps/docker-compose.paper.yml"
  fi
  if [ \$legacy_previous = 1 ]; then
    for svc in trader-runtime-blue trader-runtime-green; do
      ids=\$(docker ps -aq --filter label=com.docker.compose.project=ai-trader-production --filter label=com.docker.compose.service=\$svc)
      [ -z "\$ids" ] || docker rm -f \$ids || rollback_failed=1
    done
    if [ -f \$nginx_conf.before-$SHORT ]; then cp -p \$nginx_conf.before-$SHORT \$nginx_conf || rollback_failed=1; nginx -t && nginx -s reload || rollback_failed=1; fi
    \$compose \$prev_files up -d --no-build --wait --force-recreate || rollback_failed=1
  else
    nginx_restored=0
    # The upstream include goes back to exactly its pre-deploy state: content if it existed
    # (an empty file stays empty), or removed entirely when this deploy created it (migration).
    if [ \$include_existed = 1 ]; then
      if [ -n "\$include_before" ]; then printf '%s\n' "\$include_before" > \$nginx_include || rollback_failed=1
      else : > \$nginx_include || rollback_failed=1; fi
      nginx_restored=1
    elif [ -f \$nginx_include ]; then
      rm -f \$nginx_include || rollback_failed=1
      nginx_restored=1
    fi
    # A migration run (needs_migration without legacy_previous) replaced the conf.d template;
    # the saved pre-migration template comes back so nginx serves exactly as before the deploy.
    if [ -f \$nginx_conf.before-$SHORT ]; then cp -p \$nginx_conf.before-$SHORT \$nginx_conf || rollback_failed=1; nginx_restored=1; fi
    if [ \$nginx_restored = 1 ]; then nginx -t && nginx -s reload || rollback_failed=1; fi
    if [ \${sidecar_touched:-0} = 1 ]; then
      # The sidecar stack — and with it the whole shared netns — was already switched to the new
      # release (sidecar refresh or migration): restoring only the Runtime slot would attach the
      # previous Runtime to a new, possibly interrupted sidecar. Every member is removed first so
      # nothing stays bound to the new namespace (the old serving slot was already stopped by the
      # switch), then the previous release's own files rebuild it in dependency order: sidecar
      # first, the enabled overlay members, the old serving slot, the paper engine last. The
      # paper engine shares the netns, so its old container may remain in the previous namespace
      # even before this deploy reaches its paper step — it is explicitly removed and restored whenever the
      # restored env file says the previous release ran it. All with --no-build: a missing
      # previous image fails the restore visibly instead of rebuilding.
      # Namespace consumers are removed first — both slots (the interrupted switch may already
      # have moved serving to the new slot: a sidecar refresh after the cutover, or a migration
      # that recreated the whole project) plus every member the restored env enables — then the
      # netns owner itself, in its own call. docker rm -f of the owner succeeds even while
      # consumers still reference its namespace (verified on a real Docker: the owner was
      # removed with rc 0 and the old consumers kept their old namespace — that detached end
      # state was not verified further), so the order cannot be left to Docker: every switched
      # consumer goes before the owner, and the rebuild starts only after both. A failed
      # consumer removal stops right there: the owner keeps running in its namespace instead
      # of being torn down around a half-removed consumer set.
      rm_side="trader-runtime-\$idle trader-runtime-\$active"
      grep -q '^AI_TRADER_ACCOUNTS_OVERLAY=1\$' \$base/.env.production 2>/dev/null && rm_side="\$rm_side tqkq-sidecar-2"
      grep -q '^AI_TRADER_TQKQ_POOL=1\$' \$base/.env.production 2>/dev/null && rm_side="\$rm_side tqkq-pool"
      grep -q '^AI_TRADER_PAPER_ENGINE=1\$' \$base/.env.production 2>/dev/null && rm_side="\$rm_side paper-engine"
      \$compose \$compose_files rm -sf \$rm_side || rollback_failed=1
      if [ \$rollback_failed = 0 ]; then
        \$compose \$compose_files rm -sf tqkq-sidecar || rollback_failed=1
      fi
      # Restored strictly in dependency order, one compose call per step: the netns owner
      # (tqkq-sidecar) first, then the members that live inside it, then the old serving slot,
      # paper last — never one multi-service up whose internal ordering compose would decide.
      # A failed step stops the chain: starting the Runtime inside a namespace its sidecar
      # never came back to would only stack a second failure on the first.
      if [ \$rollback_failed = 0 ]; then
        \$compose \$prev_files up -d --no-build --no-deps --wait --force-recreate tqkq-sidecar || rollback_failed=1
      fi
      if [ \$rollback_failed = 0 ]; then
        prev_side=
        grep -q '^AI_TRADER_ACCOUNTS_OVERLAY=1\$' \$base/.env.production 2>/dev/null && prev_ran tqkq-sidecar-2 && prev_side="\$prev_side tqkq-sidecar-2"
        grep -q '^AI_TRADER_TQKQ_POOL=1\$' \$base/.env.production 2>/dev/null && prev_ran tqkq-pool && prev_side="\$prev_side tqkq-pool"
        [ -z "\$prev_side" ] || \$compose \$prev_files up -d --no-build --no-deps --wait --force-recreate \$prev_side || rollback_failed=1
      fi
      if [ \$rollback_failed = 0 ]; then
        \$compose \$prev_files up -d --no-build --no-deps --wait --force-recreate trader-runtime-\$active || rollback_failed=1
      fi
      if [ \$rollback_failed = 0 ] && grep -q '^AI_TRADER_PAPER_ENGINE=1\$' \$base/.env.production 2>/dev/null && prev_ran paper-engine; then
        if paper_checkpoint_gate; then
          \$compose \$prev_files up -d --no-build --no-deps --wait --force-recreate paper-engine || rollback_failed=1
        else
          rollback_failed=1
        fi
      fi
    else
      \$compose \$prev_files up -d --no-build --no-deps --wait trader-runtime-\$active || rollback_failed=1
      if [ \${paper_touched:-0} = 1 ] && grep -q '^AI_TRADER_PAPER_ENGINE=1\$' \$base/.env.production 2>/dev/null; then
        # Only the paper engine was switched: the sidecar stack and its subscriptions stay as
        # they are; the previous paper engine comes back beside the restored serving slot.
        \$compose \$compose_files rm -sf paper-engine || rollback_failed=1
        if prev_ran paper-engine; then
          # A paper engine still running (its removal failed) holds the database lock: the gate
          # fails on it and the old engine is not started beside it.
          if paper_checkpoint_gate; then
            \$compose \$prev_files up -d --no-build --no-deps --wait --force-recreate paper-engine || rollback_failed=1
          else
            rollback_failed=1
          fi
        fi
      fi
      \$compose \$compose_files rm -sf trader-runtime-\$idle || rollback_failed=1
    fi
  fi
  if [ \$rollback_failed = 0 ] && [ \${sidecar_touched:-0} = 1 ]; then
    # The whole netns was rebuilt (sidecar refresh or either migration shape): each enabled
    # member must itself prove it is back through its own /healthz with its real business
    # semantics (see member_gate) — a legacy whole-stack restore is held to the same proof,
    # not just the serving slot's healthz.
    member_gate tqkq-sidecar sidecar || { echo "ROLLBACK_MEMBER_UNHEALTHY: tqkq-sidecar did not come back healthy" >&2; rollback_failed=1; }
    if [ \$rollback_failed = 0 ] && grep -q '^AI_TRADER_ACCOUNTS_OVERLAY=1\$' \$base/.env.production 2>/dev/null && prev_ran tqkq-sidecar-2; then
      member_gate tqkq-sidecar-2 side2 || { echo "ROLLBACK_MEMBER_UNHEALTHY: tqkq-sidecar-2 did not come back healthy" >&2; rollback_failed=1; }
    fi
    if [ \$rollback_failed = 0 ] && grep -q '^AI_TRADER_TQKQ_POOL=1\$' \$base/.env.production 2>/dev/null && prev_ran tqkq-pool; then
      member_gate tqkq-pool pool || { echo "ROLLBACK_MEMBER_UNHEALTHY: tqkq-pool did not come back healthy" >&2; rollback_failed=1; }
    fi
    if [ \$rollback_failed = 0 ] && grep -q '^AI_TRADER_PAPER_ENGINE=1\$' \$base/.env.production 2>/dev/null && prev_ran paper-engine; then
      member_gate paper-engine paper || { echo "ROLLBACK_MEMBER_UNHEALTHY: paper-engine did not come back healthy" >&2; rollback_failed=1; }
    fi
  elif [ \$rollback_failed = 0 ] && [ \${paper_touched:-0} = 1 ] && grep -q '^AI_TRADER_PAPER_ENGINE=1\$' \$base/.env.production 2>/dev/null && prev_ran paper-engine; then
    member_gate paper-engine paper || { echo "ROLLBACK_MEMBER_UNHEALTHY: paper-engine did not come back healthy" >&2; rollback_failed=1; }
  fi
  if [ \$rollback_failed = 0 ]; then
    # ROLLBACK_OK is earned, not assumed: the restored serving slot must itself report the
    # previous release (full SHA, its own package.json version, live trading disabled) before
    # the deploy may be called rolled back.
    prev_version=\$(python3 -c "import json; print(json.load(open('\$previous/package.json'))['version'])") || rollback_failed=1
    if [ \$rollback_failed = 0 ] && ! gate \$active_port "\$prev_version" "\$(basename \$previous)"; then
      echo "ROLLBACK_HEALTHZ_MISMATCH: the restored slot did not report \$(basename \$previous)" >&2
      rollback_failed=1
    fi
  fi
  if [ \$rollback_failed = 1 ]; then
    echo "ROLLBACK_FAILED: restoring \$previous did not complete — inspect the slots, sidecar/netns members, \$nginx_include and \$base/.env.production on the host" >&2
    return 1
  fi
  echo "ROLLBACK_OK: restored \$(basename \$previous)" >&2
  return 0
}
aborted_by_signal() {
  rc=\$1
  # Block further signals before anything else: a second one re-entering rollback is what once
  # deleted the restored old slot, and this path must not be interrupted either.
  trap '' HUP INT TERM
  # Stage evidence plus the atomic result. A standing current -> \$release must never alone read
  # as this run's cutover (a same-SHA rerun starts with it, before anything happened), so
  # cutover_started gates the decision to this run's switch; the readlink then confirms the
  # atomic mv really committed in the narrow window after mv returns and before the stage
  # assignment runs. Once it holds, current and the env file are both on the new release — a
  # rollback would split them — so the finished switch stands and the exit stays non-zero (the
  # signal code), blocking any outer chain; side_failed still classifies exactly as the normal
  # end of the script would (exit 4, DEPLOY_PARTIAL).
  if [ \$switched = 1 ] || { [ \$cutover_started = 1 ] && [ "\$(readlink -f \$base/current)" = "\$release" ]; }; then
    echo "DEPLOY_ABORTED_BY_SIGNAL_AFTER_CUTOVER: current is already $SHORT; the switch stands, no rollback ran" >&2
    if [ \${side_failed:-0} = 1 ]; then echo "DEPLOY_PARTIAL: Runtime switched to $SHORT, sidecar-side containers need attention" >&2; exit 4; fi
    exit "\$rc"
  fi
  echo "DEPLOY_ABORTED_BY_SIGNAL: interrupting the switch to $SHORT, restoring \$(basename \$previous)" >&2
  rollback || rc=1
  exit "\$rc"
}
cp -p \$base/.env.production \$base/.env.production.before-$SHORT
trap 'aborted_by_signal 129' HUP
trap 'aborted_by_signal 130' INT
trap 'aborted_by_signal 143' TERM
sed -i "s/^AI_TRADER_BUILD_SHA=.*/AI_TRADER_BUILD_SHA=$SHA/" \$base/.env.production
grep -q '^AI_TRADER_VOICE_REALTIME_KEY_FILE=' \$base/.env.production || echo 'AI_TRADER_VOICE_REALTIME_KEY_FILE=$BASE/secrets/voice-realtime-key' >> \$base/.env.production
switch_nginx() {
  printf 'upstream ai_trader_runtime { server 127.0.0.1:%s; }\n' "\$1" > \$nginx_include
  if ! nginx -t; then printf '%s\n' "\$include_before" > \$nginx_include; echo NGINX_TEST_FAILED >&2; return 1; fi
  if ! nginx -s reload; then
    printf '%s\n' "\$include_before" > \$nginx_include
    nginx -t && nginx -s reload || true
    echo NGINX_RELOAD_FAILED >&2; return 1
  fi
}
# After a slot switch the old nginx workers keep serving the browsers' long-lived HTTP/2
# connections with the old upstream; a reconnecting event stream rides the same connection, so
# once the old slot stops below those windows got 502 until the browser dropped the connection
# (2026-09-27: one window 502'd for an hour). A bounded worker shutdown closes them before the
# old slot stops. The workers must start with it, hence this reload ahead of the switch.
if ! grep -q '^worker_shutdown_timeout' /etc/nginx/nginx.conf; then
  cp -p /etc/nginx/nginx.conf /etc/nginx/nginx.conf.before-shutdown-timeout-$SHORT
  sed -i '/^worker_processes/a worker_shutdown_timeout 8s;' /etc/nginx/nginx.conf
  if nginx -t; then nginx -s reload; sleep 1; else cp -p /etc/nginx/nginx.conf.before-shutdown-timeout-$SHORT /etc/nginx/nginx.conf; fi
fi
needs_migration=0
if [ -n "\$legacy_runtime" ] || [ ! -f \$nginx_include ] || ! grep -q ai-trader-runtime-upstream \$nginx_conf 2>/dev/null; then needs_migration=1; fi
if [ \$needs_migration = 1 ]; then
  echo "first blue-green migration: replacing the single-slot layout (full stack restart)"
  # The legacy container must go before the sidecar recreates its netns; force-recreate
  # then rebuilds every netns member (slots, tqkq-sidecar-2, tqkq-pool) on the new image.
  [ -z "\$legacy_runtime" ] || docker rm -f \$legacy_runtime
  # A migration is a full-stack recreate even when the previous release was already blue-green
  # (needs_migration without legacy_previous, e.g. a missing upstream include): from here on the
  # sidecar netns — and with it every member, the paper engine included — is switched, so any
  # interruption must restore them all, not run the lightweight slot-only rollback.
  sidecar_touched=1
  grep -q '^AI_TRADER_PAPER_ENGINE=1\$' \$base/.env.production 2>/dev/null && paper_touched=1
  \$compose \$compose_files up -d --no-build --wait --force-recreate || { echo FULL_STACK_START_FAILED >&2; rollback; exit 1; }
  # The new template includes the upstream file, so the include is written first; the
  # pre-migration template stays beside it as the rollback copy.
  [ -f \$nginx_conf.before-$SHORT ] || cp -p \$nginx_conf \$nginx_conf.before-$SHORT
  printf 'upstream ai_trader_runtime { server 127.0.0.1:18000; }\n' > \$nginx_include
  cp -p \$release/deploy/vps/nginx-ai-trader-runtime.conf \$nginx_conf
  if ! nginx -t; then cp -p \$nginx_conf.before-$SHORT \$nginx_conf; echo NGINX_TEST_FAILED >&2; rollback; exit 1; fi
  if ! nginx -s reload; then cp -p \$nginx_conf.before-$SHORT \$nginx_conf; nginx -t && nginx -s reload || true; echo NGINX_RELOAD_FAILED >&2; rollback; exit 1; fi
  if ! gate 18000; then echo NEW_SLOT_HEALTHZ_MISMATCH >&2; rollback; exit 1; fi
  \$compose \$compose_files rm -sf trader-runtime-green || { echo OLD_SLOT_STOP_FAILED >&2; rollback; exit 1; }
else
  \$compose \$compose_files up -d --no-build --no-deps --wait trader-runtime-\$idle || { echo NEW_SLOT_START_FAILED >&2; rollback; exit 1; }
  if ! gate \$idle_port; then echo NEW_SLOT_HEALTHZ_MISMATCH >&2; rollback; exit 1; fi
  if ! switch_nginx \$idle_port; then rollback; exit 1; fi
  # In-flight requests drain through the old workers, which close their remaining connections
  # after worker_shutdown_timeout (8 s); the browsers reconnect to the new workers before the old
  # slot stops, and the client hides the blip behind its reconnect grace.
  sleep 10
  \$compose \$compose_files rm -sf trader-runtime-\$active || { echo OLD_SLOT_STOP_FAILED >&2; rollback; exit 1; }
fi
# Sidecar-side refresh (2026-10-04, #525 E1; definitions 2026-10-12): tqkq-sidecar and the
# containers in its netns keep their sessions across a deploy, so only the containers the service
# plan above names are recreated — the sidecar when its source (image) or definition changed, with
# the pool, tqkq-sidecar-2 and the serving slot that live in its netns; a member whose own
# definition changed alone. The paper engine joins the netns afterwards.
side_failed=0
if [ \$needs_migration = 0 ] && [ -n "\$side" ]; then
  echo "service plan: recreating \$side"
  # Recorded before the command: a partially executed recreate already switched its containers.
  sidecar_touched=1
  \$compose \$compose_files up -d --no-build --no-deps --wait --force-recreate \$side || { echo SIDECAR_UPDATE_FAILED >&2; side_failed=1; }
  gate \$idle_port || { echo SIDECAR_UPDATE_SLOT_UNHEALTHY >&2; side_failed=1; }
fi
# 本站模拟盘引擎（可选覆盖层）随发布换镜像：它不在蓝绿切换链路里，这里显式重建（在 sidecar
# 之后——它加入 sidecar 的网络命名空间）。失败不回滚已完成的运行时切换，但发布结果判失败。
if grep -q '^AI_TRADER_PAPER_ENGINE=1\$' \$base/.env.production 2>/dev/null; then
  paper_touched=1
  \$compose \$compose_files up -d --no-build --no-deps --wait --force-recreate paper-engine || { echo PAPER_ENGINE_UPDATE_FAILED >&2; side_failed=1; }
fi
# RSSHub 与隧道中继（2026-10-09 RSSHub 移到家里 NAS；2026-10-18 加 MT5 行情桥中继 mt5-tunnel）都不在蓝绿链路里：中继容器随本次 compose 起
# 来。它起不来、或 NAS 隧道断开，只少几个快讯来源（各源独立超时、快讯照常合并），Runtime 照常——
# 不回滚、不判发布失败。本地 rsshub 是回退项：仅当 .env.production 有 AI_TRADER_LOCAL_RSSHUB=1
# （local-rsshub profile）才起，否则移除旧版 compose 部署的本地副本容器。
\$compose \$compose_files up -d --no-build --no-deps --wait rsshub-tunnel || echo "RSSHUB_TUNNEL_START_FAILED: Runtime serves without the NAS RSSHub sources" >&2
\$compose \$compose_files up -d --no-build --no-deps --wait mt5-tunnel || echo "MT5_TUNNEL_START_FAILED: the 外盘 rows come from the other sources" >&2
if grep -q '^AI_TRADER_LOCAL_RSSHUB=1\$' \$base/.env.production 2>/dev/null; then
  \$compose \$compose_files --profile local-rsshub up -d --no-build --no-deps --wait rsshub || echo "RSSHUB_START_FAILED: Runtime serves without the RSSHub news sources" >&2
else
  \$compose \$compose_files --profile local-rsshub rm -sf rsshub 2>/dev/null || true
fi
cutover_started=1
ln -s \$release \$base/.current-$SHORT
mv -Tf \$base/.current-$SHORT \$base/current
switched=1
# The traps stay armed across the result lines: a signal in the narrow window after the atomic
# cutover lands in aborted_by_signal's post-cutover branch (switch stands, non-zero exit), which
# is safer than the default kill of a cleared trap — the outer script then sees the marker.
if [ \$side_failed = 1 ]; then echo "DEPLOY_PARTIAL: Runtime switched to $SHORT, sidecar-side containers need attention" >&2; exit 4; fi
echo DEPLOY_OK
trap - HUP INT TERM
EOF
)
if [ "$DRY" = 1 ]; then
  printf '[%s] write %s/deploy-%s.sh:\n%s\n' "$HOST" "$BASE" "$SHORT" "$SCRIPT"
  printf '[%s] systemd-run --wait --unit=ai-trader-deploy-%s-<invocation> (log %s/deploy-%s-<invocation>.log) -- /bin/sh ./deploy-%s.sh\n' "$HOST" "$SHA" "$BASE" "$SHA" "$SHORT"
  # The same service plan the deploy will print, read from the host as it is now (running
  # containers, .env.production, the current release) against this commit's compose files and
  # sidecar source, unpacked into a scratch tree under $BASE/tmp that is removed right after.
  say "service plan on $HOST (dry run: the running containers against $SHORT)"
  PLAN_TMP=$BASE/tmp/agent-plan-$SHORT-$$
  if ssh "$HOST" "sudo mkdir -p $PLAN_TMP && sudo chown \$(id -un) $PLAN_TMP" \
      && git -C "$REPO" archive "$SHA" deploy/vps services/tqkq-sidecar | ssh "$HOST" "tar -x -C $PLAN_TMP" \
      && ssh "$HOST" "$(images_yml_cmd "$PLAN_TMP")" \
      && ssh "$HOST" "sudo sh -s" <<PLAN
set -eu
base=$BASE
release=$PLAN_TMP
previous=\$(readlink -f \$base/current)
idle=green
if grep -q 18001 /etc/nginx/ai-trader-runtime-upstream.conf 2>/dev/null; then idle=blue; fi
$COMPOSE_SH
python3 \$release/deploy/vps/service_plan.py --project ai-trader-production --previous \$previous --release \$release --idle \$idle -- \$compose \$compose_files
PLAN
  then :; else echo "SERVICE_PLAN_UNAVAILABLE: no plan read from $HOST (dry run; nothing was changed)" >&2; fi
  ssh "$HOST" "sudo rm -rf $PLAN_TMP" || true
  exit 0
fi
printf '%s\n' "$SCRIPT" | ssh "$HOST" "sudo sh -c 'cat > $BASE/deploy-$SHORT.sh && chmod 700 $BASE/deploy-$SHORT.sh'"
say "deploying on $HOST (healthz must report $VERSION @ $SHORT; rollback target $(basename "$PREVIOUS" | cut -c1-7) — the outcome is classified from the unit log, not assumed)"
# The host script runs under a transient systemd unit (#715): with --wait the unit's exit code
# comes back over this ssh call, but the script itself never depends on the connection — its
# stdout/stderr append to a per-invocation host log (UMask 0077: root-only), so a dropped ssh
# session or a closed local pipe cannot stop it mid-switch (a SIGPIPE once left the deploy
# half-done with no rollback: old slot removed, upstream flipped, env on the new SHA, current
# still old). The unit and log names bind the full SHA plus a unique invocation, so a rerun
# never attaches to an old unit or overwrites the previous evidence; failed units stay loaded
# for inspection (no --collect). Verified against a real non-production systemd (user manager
# on the NAS, ssh caller terminated): the unit keeps running to its own terminal state and the
# exit code still propagates, while the killed ssh side reports 255. TimeoutStopSec=infinity
# disables only systemd's own default stop timeout (Ubuntu noble: 90 s, then SIGKILL), so the
# manager itself never cuts a unit mid-rollback; an external kill or power loss is still
# possible and keeps the unknown/rollback-failed guard semantics. The property was verified on
# a real user transient unit (TimeoutStopUSec reads back infinity, ExecMainStatus 0).
INVOCATION=$(date +%Y%m%dT%H%M%S)-$$
UNIT=ai-trader-deploy-$SHA-$INVOCATION
HOSTLOG=$BASE/deploy-$SHA-$INVOCATION.log
LOG=$(mktemp)
rc=0
printf '[%s] systemd-run --wait --unit=%s (output: %s)\n' "$HOST" "$UNIT" "$HOSTLOG"
ssh "$HOST" "sudo systemd-run --wait --unit=$UNIT --property=StandardOutput=append:$HOSTLOG --property=StandardError=append:$HOSTLOG --property=WorkingDirectory=$BASE --property=UMask=0077 --property=TimeoutStopSec=infinity /bin/sh $BASE/deploy-$SHORT.sh" || rc=$?
# Read the host log back whatever happened; the read status is recorded because a cat over a
# dying connection can emit partial output — including a complete-looking marker line — before
# failing (#715). A non-zero read status voids every marker decision below.
readback=0
ssh "$HOST" "sudo cat $HOSTLOG" >> "$LOG" 2>&1 || { readback=1; echo "(could not read back $HOSTLOG on $HOST)" >> "$LOG"; }
deploy_ok=$(grep -c '^DEPLOY_OK' "$LOG" || true)
after_cutover=$(grep -c 'DEPLOY_ABORTED_BY_SIGNAL_AFTER_CUTOVER' "$LOG" || true)
rollback_failed=$(grep -c 'ROLLBACK_FAILED' "$LOG" || true)
rollback_ok=$(grep -c 'ROLLBACK_OK' "$LOG" || true)
partial=$(grep -c 'DEPLOY_PARTIAL' "$LOG" || true)
aborted_by_signal=$(( $(grep -c 'DEPLOY_ABORTED_BY_SIGNAL' "$LOG" || true) - after_cutover ))
completed=$(( deploy_ok + after_cutover + rollback_failed + rollback_ok ))
grep -v -i 'token\|password' "$LOG" || true; rm -f "$LOG"
# The read-back itself failed: nothing in the (possibly partial) output is trusted — no success,
# no maintenance step, no guard release; 255 reads as UNKNOWN, everything else as UNREAD.
if [ "$readback" != 0 ]; then
  RETAIN_GUARD=1
  outcome=DEPLOY_RESULT_UNREAD
  if [ "$rc" = 255 ]; then outcome=DEPLOY_UNKNOWN; fi
  echo "$outcome (deploy unit ssh rc $rc; the log read-back ssh itself failed, so no marker in the partial output is trusted) — read back before touching $BASE/.deploying:" >&2
  echo "  ssh $HOST 'sudo systemctl status $UNIT --no-pager; sudo systemctl show $UNIT -p ActiveState -p SubState -p Result -p ExecMainStatus; sudo journalctl -u $UNIT -n 100 --no-pager; sudo cat $HOSTLOG'" >&2
  [ "$rc" != 0 ] || rc=1
  exit "$rc"
fi
# ssh 255 stays unknown no matter what stage results the log already holds: the unit may still
# be running. The same for a signal exit (rc >= 128) without a completion marker. Never guess,
# never kill or clear anything automatically; read the unit back.
if [ "$rc" = 255 ] || { [ "$rc" -ge 128 ] && [ "$completed" = 0 ]; }; then
  RETAIN_GUARD=1
  echo "DEPLOY_UNKNOWN (ssh rc $rc): the deploy unit may still be running on $HOST — read back before touching $BASE/.deploying:" >&2
  echo "  ssh $HOST 'sudo systemctl status $UNIT --no-pager; sudo systemctl show $UNIT -p ActiveState -p SubState -p Result -p ExecMainStatus; sudo journalctl -u $UNIT -n 100 --no-pager; sudo cat $HOSTLOG'" >&2
  exit "$rc"
fi
# Guard release needs a trusted terminal outcome in the log: verified success, verified partial,
# or a rollback that provably completed (ROLLBACK_OK). Everything else — no terminal marker, an
# unreadable log, a client-side anomaly, or an incomplete restore (ROLLBACK_FAILED) — keeps the
# marker for a real read-back; a client return alone does not prove the unit stopped or its
# terminal state, so retaining evidence is the default.
if [ "$rc" = 0 ] && { [ "$deploy_ok" -gt 0 ] || [ "$after_cutover" -gt 0 ]; }; then
  REMOTE_RESULT_KNOWN=1
  [ "$after_cutover" = 0 ] || echo "DEPLOY_OK (a signal arrived right after the cutover; $SHORT stands)" >&2
  # The maintenance script and its units ship with each release (migrate-to-host.sh installs the first copy).
  ssh "$HOST" "sudo install -m 644 $BASE/releases/$SHA/scripts/maintain_update_host.py $BASE/maintenance/maintain_update_host.py && sudo install -m 644 $BASE/releases/$SHA/deploy/vps/ai-trader-maintenance.service $BASE/releases/$SHA/deploy/vps/ai-trader-maintenance.timer /etc/systemd/system/ && sudo systemctl daemon-reload" || {
    echo "DEPLOY_OK_MAINTENANCE_INSTALL_FAILED: the previous maintenance script stays in place" >&2;
  }
  release_guard
  ssh "$HOST" "sudo systemctl start --no-block ai-trader-maintenance.service" || {
    echo "DEPLOY_OK_MAINTENANCE_START_FAILED: inspect the existing hourly timer" >&2;
  }
elif [ "$rc" = 4 ] && [ "$partial" -gt 0 ]; then
  # 4 = the Runtime switched but a sidecar-side container did not come up: nothing was rolled back.
  REMOTE_RESULT_KNOWN=1
  echo "DEPLOY_PARTIAL ($SHORT serving; recreate the sidecar-side containers by hand)" >&2
  exit 4
elif [ "$rollback_failed" -gt 0 ]; then
  RETAIN_GUARD=1
  echo "DEPLOY_FAILED_ROLLBACK_FAILED: $SHORT did not deploy and restoring $(basename "$PREVIOUS" | cut -c1-7) did not complete — inspect the slots, nginx upstream include and $BASE/.env.production on $HOST, then remove $BASE/.deploying" >&2
  exit "$rc"
elif [ "$after_cutover" -gt 0 ]; then
  # A signal after the committed cutover: the switch stands, no rollback ran — reported as
  # neither success nor "rolled back", and the non-zero exit blocks any outer chain.
  REMOTE_RESULT_KNOWN=1
  echo "DEPLOY_FAILED_AFTER_CUTOVER (interrupted by signal, rc $rc; $SHORT is serving and the switch stands — verify the sidecar-side containers on $HOST)" >&2
  exit "$rc"
elif [ "$aborted_by_signal" -gt 0 ] && [ "$rollback_ok" -gt 0 ]; then
  REMOTE_RESULT_KNOWN=1
  echo "DEPLOY_FAILED (interrupted by signal; the host log confirms the rollback to $(basename "$PREVIOUS" | cut -c1-7))" >&2
  exit "$rc"
elif [ "$rollback_ok" -gt 0 ]; then
  # "Rolled back" is claimed on the ROLLBACK_OK marker alone — never as the default failure label.
  REMOTE_RESULT_KNOWN=1
  echo "DEPLOY_FAILED (rolled back to $(basename "$PREVIOUS" | cut -c1-7))" >&2
  exit "$rc"
else
  # No terminal marker at all: exit 0 without DEPLOY_OK, a log that could not be read, or a unit
  # that failed before (or without starting) its switch — the marker stays for a real read-back.
  RETAIN_GUARD=1
  echo "DEPLOY_RESULT_UNREAD: no terminal outcome marker in the deploy unit's log (rc $rc, $HOSTLOG on $HOST) — read back before touching $BASE/.deploying:" >&2
  echo "  ssh $HOST 'sudo systemctl show $UNIT -p ActiveState -p SubState -p Result -p ExecMainStatus; sudo cat $HOSTLOG'" >&2
  [ "$rc" != 0 ] || rc=1
  exit "$rc"
fi
