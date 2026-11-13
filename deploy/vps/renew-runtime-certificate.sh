#!/bin/bash
# DNS credentials stay in the management Mac's Keychain; the TLS key stays on Tencent.
set -euo pipefail
umask 077
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
domain=ai-runtime.qilinshuzhi.com
base="$HOME/.local/share/ai-trader-certificates"
lego="$HOME/.local/libexec/lego/5.6.0/lego"
mode=${1:-production}
case "$mode" in production|staging) ;; *) echo 'Use production or staging' >&2; exit 2 ;; esac
mkdir -p "$base"
ssh -o BatchMode=yes -o ConnectTimeout=15 tencent-ai-trader \
  "sudo -n openssl req -new -sha256 -key /etc/letsencrypt/live/$domain/privkey.pem -subj /CN=$domain -addext subjectAltName=DNS:$domain" > "$base/runtime.csr"
raw=$(security find-generic-password -a "$USER" -s ai-trader-aliyun-ak -w)
export ALICLOUD_ACCESS_KEY="${raw%%:*}" ALICLOUD_SECRET_KEY="${raw#*:}"
export ALICLOUD_PROPAGATION_TIMEOUT=180
unset raw
args=(--server letsencrypt)
if [ "$mode" = staging ]; then args=(--server letsencrypt-staging --renew-force); fi
"$lego" --log.level warn run --accept-tos --account-id ai-trader-runtime \
  --csr "$base/runtime.csr" --dns alidns --dns.resolvers 223.5.5.5:53 \
  --path "$base/$mode" "${args[@]}"
unset ALICLOUD_ACCESS_KEY ALICLOUD_SECRET_KEY
if [ "$mode" = staging ]; then echo 'Staging DNS renewal succeeded; nothing deployed'; exit 0; fi
tar -C "$base/production/certificates" -cf - "$domain.crt" "$domain.issuer.crt" | \
  ssh -o BatchMode=yes -o ConnectTimeout=15 tencent-ai-trader \
    'sudo -n /usr/local/sbin/ai-trader-install-certificate'
