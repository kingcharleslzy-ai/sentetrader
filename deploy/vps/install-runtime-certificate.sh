#!/bin/bash
# Accept only public certificate files. Never import or transfer the TLS private key.
set -euo pipefail
umask 077
domain=ai-runtime.qilinshuzhi.com
live=/etc/letsencrypt/live/$domain
tmp=$(mktemp -d /etc/letsencrypt/.ai-trader-cert.XXXXXX)
trap 'rm -rf "$tmp"' EXIT
tar -xf - -C "$tmp" --no-same-owner -- "$domain.crt" "$domain.issuer.crt"
openssl x509 -in "$tmp/$domain.crt" -out "$tmp/cert.pem"
openssl x509 -in "$tmp/cert.pem" -noout -checkend 2592000 >/dev/null
openssl verify -verify_hostname "$domain" -CAfile /etc/ssl/certs/ca-certificates.crt \
  -untrusted "$tmp/$domain.issuer.crt" "$tmp/cert.pem" >/dev/null
cert_key=$(openssl x509 -in "$tmp/cert.pem" -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum)
live_key=$(openssl pkey -in "$live/privkey.pem" -pubout -outform DER | sha256sum)
[ "$cert_key" = "$live_key" ] || { echo 'Certificate key mismatch' >&2; exit 1; }
new_serial=$(openssl x509 -in "$tmp/cert.pem" -noout -serial)
old_serial=$(openssl x509 -in "$live/cert.pem" -noout -serial)
listener_serial() {
  timeout 15 openssl s_client -connect 127.0.0.1:8443 -servername "$domain" \
    </dev/null 2>/dev/null | openssl x509 -noout -serial
}
if [ "$new_serial" != "$old_serial" ]; then
  backup=/opt/ai-trader/backups/cert-dns-$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -m 700 "$backup"
  for name in cert chain fullchain; do cp -L "$live/$name.pem" "$backup/$name.pem"; done
  rollback() {
    for name in cert chain fullchain; do
      dest=$(readlink -f "$live/$name.pem")
      install -m 644 "$backup/$name.pem" "$dest.pending"
      mv -f "$dest.pending" "$dest"
    done
    nginx -t && systemctl reload nginx
  }
  trap 'rollback; rm -rf "$tmp"' ERR
  cp "$tmp/$domain.issuer.crt" "$tmp/chain.pem"
  cp "$tmp/$domain.crt" "$tmp/fullchain.pem"
  for name in cert chain fullchain; do
    dest=$(readlink -f "$live/$name.pem")
    install -m 644 "$tmp/$name.pem" "$dest.pending"
    mv -f "$dest.pending" "$dest"
  done
fi
if [ "$new_serial" != "$old_serial" ] || [ "$(listener_serial)" != "$new_serial" ]; then
  nginx -t
  systemctl reload nginx
  echo 'Certificate loaded with nginx reload'
else
  echo 'Certificate already current; no reload'
fi
loaded=false
for attempt in {1..10}; do
  if [ "$(listener_serial)" = "$new_serial" ]; then loaded=true; break; fi
  sleep 1
done
[ "$loaded" = true ] || { echo 'Listener certificate mismatch' >&2; false; }
curl --noproxy '*' -fsS --max-time 15 --resolve "$domain:8443:127.0.0.1" \
  "https://$domain:8443/healthz" >/dev/null
trap - ERR
echo 'Listener certificate and health verified'
openssl x509 -in "$live/cert.pem" -noout -serial -enddate
