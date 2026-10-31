#!/bin/sh
# Build the qilinshuzhi.com web candidate for one commit and switch /var/www/ai-trader/app on the
# site host atomically. Output goes to apps/client/dist-web, never to the desktop bundle's dist.
#
#   deploy/web/deploy-web.sh <full-sha> [--host aliyun-qilinshuzhi] [--dry-run]
set -eu

SHA=${1:?full git sha}; shift
HOST=aliyun-qilinshuzhi
ROOT=/var/www/ai-trader
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST=$2; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
REPO=$(cd "$(dirname "$0")/../.." && pwd)
[ "$(git -C "$REPO" rev-parse HEAD)" = "$SHA" ] || { echo "the checked-out tree is not $SHA" >&2; exit 2; }

cd "$REPO"
set -a; . deploy/web/.env.production.example; set +a
if [ "$DRY" = 1 ]; then
  echo "[local] AI_TRADER_BUILD_SHA=$SHA npm run build:web && npm run check:web"
  echo "[local→$HOST] tar dist-web → $ROOT/releases/$SHA; assets/ → ${VITE_ASSET_ORIGIN}assets/ (OSS); mv -Tf → $ROOT/app"
  echo "[readback] https://qilinshuzhi.com/trader/version.json commit == $SHA"
  exit 0
fi
# All three release paths build exactly what their version stamp claims: the Runtime archives the
# SHA, the desktop publisher refuses a dirty tree (check_release_source.mjs), and this direct
# entry reuses that same shared check — before the build and again after it, right before the
# upload — so neither uncommitted edits and untracked files, nor a concurrent modification that
# lands while the build runs, ship as $SHA (#715). (--dry-run above stays plan-only.)
release_source_check() {
  node --input-type=module -e "import { assertCleanReleaseSource } from '$REPO/scripts/check_release_source.mjs'; assertCleanReleaseSource('$SHA');" \
    || { echo "build a clean checkout of $SHA and retry" >&2; exit 2; }
}
release_source_check
AI_TRADER_BUILD_SHA=$SHA npm run build:web
npm run check:web
release_source_check
tar -C apps/client/dist-web -cz . | ssh "$HOST" "set -eu; mkdir -p $ROOT/releases/$SHA && tar -xz -C $ROOT/releases/$SHA 2>/dev/null"
# The page loads its scripts and styles from OSS (VITE_ASSET_ORIGIN): they must be there before
# the page switches. Scripts and styles go up as their level-9 .gz with Content-Encoding: gzip.
# OSS keys come from the local Keychain, as in publish_desktop_update.mjs, and reach the site host
# only through this ssh session's stdin (#715): in the argv they were visible to `ps` on both
# machines for the whole upload loop. `read -r` takes each line verbatim, so keys containing
# quotes or metacharacters need no shell escaping.
raw=$(security find-generic-password -a "$USER" -s ai-trader-aliyun-ak -w 2>/dev/null || true)
case "$raw" in *:*) ;; *) echo "oss key missing (Keychain ai-trader-aliyun-ak); page not switched" >&2; exit 1 ;; esac
prefix=${VITE_ASSET_ORIGIN#https://ai-trader-updates-hz.oss-cn-hangzhou.aliyuncs.com/}
printf '%s\n%s\n' "${raw%%:*}" "${raw#*:}" | ssh "$HOST" "set -eu
IFS= read -r oss_key_id
IFS= read -r oss_key_secret
export ALIBABA_CLOUD_ACCESS_KEY_ID=\"\$oss_key_id\" ALIBABA_CLOUD_ACCESS_KEY_SECRET=\"\$oss_key_secret\" ALIBABA_CLOUD_REGION_ID=cn-hangzhou
cd $ROOT/releases/$SHA/assets
for f in *; do
  case \$f in *.gz) continue ;; *.js) type='application/javascript; charset=utf-8' ;; *.css) type='text/css; charset=utf-8' ;; *) type='' ;; esac
  meta='Cache-Control:public,max-age=31536000,immutable'
  [ -n \"\$type\" ] && meta=\"\$meta#Content-Type:\$type\"
  src=\$f
  if [ -f \$f.gz ]; then src=\$f.gz; meta=\"\$meta#Content-Encoding:gzip\"; fi
  aliyun oss cp \$src oss://ai-trader-updates-hz/${prefix}assets/\$f --endpoint oss-cn-hangzhou-internal.aliyuncs.com --meta \"\$meta\" -f >/dev/null
done"
unset raw
main=$(sed -n 's/.*<script type="module" crossorigin src="\([^"]*\)".*/\1/p' apps/client/dist-web/index.html)
headers=$(curl -sI --noproxy '*' -m 20 -H "Origin: https://qilinshuzhi.com" -H "Accept-Encoding: gzip" "$main")
printf '%s' "$headers" | grep -qi '^HTTP/[0-9.]* 200' || { echo "asset not on OSS: $main" >&2; exit 1; }
printf '%s' "$headers" | grep -qi '^access-control-allow-origin: https://qilinshuzhi.com' || { echo "OSS CORS does not allow the site: $main" >&2; exit 1; }
ssh "$HOST" "set -eu; ln -sfn $ROOT/releases/$SHA $ROOT/.app-$SHA && mv -Tf $ROOT/.app-$SHA $ROOT/app && readlink -f $ROOT/app"
rm -rf apps/client/dist-web
live=$(curl -s --noproxy '*' -m 15 https://qilinshuzhi.com/trader/version.json)
printf '%s\n' "$live"
printf '%s' "$live" | grep -q "\"commit\": *\"$SHA\"" || { echo "web readback does not show $SHA" >&2; exit 1; }
