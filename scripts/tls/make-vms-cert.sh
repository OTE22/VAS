#!/usr/bin/env bash
# Issue VMS HTTPS certificates from the existing VAS CA.
# Usage: sudo bash scripts/tls/make-vms-cert.sh
# Store the public chain and leaf/key in Ubuntu's Nginx directory, and the
# leaf/key in the directory mounted by the current Docker Nginx deployment.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CERTS="$ROOT/certs"
DEST=/etc/nginx/certs
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo.' >&2; exit 1; }
umask 077
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/extensions.cnf" <<EXT
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:armyeye-vms.internal,DNS:armyeye-vms,DNS:armyeye-vms.local
EXT
if [ -e "$CERTS/vms.crt" ] || [ -e "$CERTS/vms.key" ]; then
  echo 'VMS certificate already exists; refusing to replace it. Review renewal explicitly.' >&2
  exit 1
fi
for file in vms.crt vms.key; do
  [ ! -e "$DEST/$file" ] || { echo "Already exists: $DEST/$file" >&2; exit 1; }
done
openssl req -new -newkey rsa:2048 -nodes -sha256 \
  -keyout "$TMP/vms.key" -out "$TMP/vms.csr" \
  -subj '/CN=armyeye-vms.internal/O=Face Detector' 2>/dev/null
openssl x509 -req -in "$TMP/vms.csr" \
  -CA "$CERTS/internal-ca.crt" -CAkey "$CERTS/internal-ca.key" \
  -set_serial "0x$(openssl rand -hex 16)" -days 365 -sha256 \
  -extfile "$TMP/extensions.cnf" -out "$TMP/vms.crt"
openssl verify -purpose sslserver -verify_hostname armyeye-vms.internal \
  -CAfile "$CERTS/internal-ca.crt" "$TMP/vms.crt"
install -d -o root -g root -m 0700 "$DEST"
install -o root -g root -m 0644 "$CERTS/internal-ca.crt" "$DEST/internal-ca.crt"
for target in "$CERTS" "$DEST"; do
  install -o root -g root -m 0600 "$TMP/vms.key" "$target/vms.key"
  install -o root -g root -m 0644 "$TMP/vms.crt" "$target/vms.crt"
done
openssl x509 -in "$DEST/vms.crt" -noout -subject -issuer -dates -ext subjectAltName
