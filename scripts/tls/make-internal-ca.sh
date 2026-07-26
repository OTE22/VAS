#!/usr/bin/env bash
#
# Create an internal CA and a server certificate for the LAN deployment.
#
#   bash scripts/tls/make-internal-ca.sh [hostname] [extra-ip]
#   bash scripts/tls/make-internal-ca.sh face-detector.internal 192.168.1.50
#
# Writes to certs/ (gitignored):
#   internal-ca.crt   distribute this to every client that must trust the server
#   internal-ca.key   KEEP OFFLINE — anyone holding it can mint trusted certs
#   server.crt        server certificate, mounted read-only into nginx
#   server.key        server private key, mode 600
#
# An internal CA is preferred over a bare self-signed certificate: clients trust
# the CA once, and server certificates can then be reissued on renewal without
# touching any client again.
#
# Do NOT enable HSTS until every client trusts internal-ca.crt. HSTS makes the
# browser refuse to fall back to HTTP, so enabling it before trust is
# established locks users out with no in-browser override.

set -euo pipefail

cd "$(dirname "$0")/../.."

HOSTNAME_ARG="${1:-face-detector.internal}"
EXTRA_IP="${2:-}"
CERT_DIR="certs"
CA_DAYS=3650      # 10 years — the CA is distributed to clients, rotate rarely
SERVER_DAYS=825   # ~27 months, the maximum most clients accept

mkdir -p "$CERT_DIR"

if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required" >&2
    exit 1
fi

if [ -f "${CERT_DIR}/server.crt" ]; then
    echo "${CERT_DIR}/server.crt already exists."
    echo "Delete certs/ deliberately to reissue, then re-run."
    exit 1
fi

# Subject DNs come from a config file rather than -subj: Git Bash on Windows
# rewrites a leading "/CN=..." into a filesystem path, which fails with a
# confusing "subject name is expected to be in the format" error.
CONF_FILE="$(mktemp)"
EXT_FILE="$(mktemp)"
trap 'rm -f "$CONF_FILE" "$EXT_FILE"' EXIT

# --- Certificate authority -------------------------------------------------
if [ ! -f "${CERT_DIR}/internal-ca.crt" ]; then
    echo "Creating internal CA..."
    cat > "$CONF_FILE" <<'EOF'
[req]
distinguished_name = dn
prompt = no
x509_extensions = ca_ext

[dn]
CN = Face Detector Internal CA
O  = Face Detector
OU = Internal

[ca_ext]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF

    [ -f "${CERT_DIR}/internal-ca.key" ] || openssl genrsa -out "${CERT_DIR}/internal-ca.key" 4096
    chmod 600 "${CERT_DIR}/internal-ca.key"
    openssl req -x509 -new -nodes \
        -key "${CERT_DIR}/internal-ca.key" \
        -sha256 -days "$CA_DAYS" \
        -config "$CONF_FILE" \
        -out "${CERT_DIR}/internal-ca.crt"
    echo "  + ${CERT_DIR}/internal-ca.crt"
fi

# --- Server certificate ----------------------------------------------------
# SANs, not CN: every current browser ignores CN entirely.
SAN="DNS:${HOSTNAME_ARG},DNS:localhost,IP:127.0.0.1"
if [ -n "$EXTRA_IP" ]; then
    SAN="${SAN},IP:${EXTRA_IP}"
fi

echo "Creating server certificate for ${HOSTNAME_ARG} (SAN: ${SAN})..."

cat > "$CONF_FILE" <<EOF
[req]
distinguished_name = dn
prompt = no

[dn]
CN = ${HOSTNAME_ARG}
O  = Face Detector
EOF

cat > "$EXT_FILE" <<EOF
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = ${SAN}
subjectKeyIdentifier = hash
EOF

openssl genrsa -out "${CERT_DIR}/server.key" 2048
chmod 600 "${CERT_DIR}/server.key"

openssl req -new \
    -key "${CERT_DIR}/server.key" \
    -config "$CONF_FILE" \
    -out "${CERT_DIR}/server.csr"

openssl x509 -req \
    -in "${CERT_DIR}/server.csr" \
    -CA "${CERT_DIR}/internal-ca.crt" \
    -CAkey "${CERT_DIR}/internal-ca.key" \
    -CAcreateserial \
    -out "${CERT_DIR}/server.crt" \
    -days "$SERVER_DAYS" -sha256 \
    -extfile "$EXT_FILE"

rm -f "${CERT_DIR}/server.csr"
chmod 644 "${CERT_DIR}/server.crt" "${CERT_DIR}/internal-ca.crt"

EXPIRY="$(openssl x509 -in "${CERT_DIR}/server.crt" -noout -enddate | cut -d= -f2)"

cat <<EOF

------------------------------------------------------------------------
Certificates written to ${CERT_DIR}/
  server.crt      expires: ${EXPIRY}
  internal-ca.crt distribute to clients

Install the CA on every client that will use the service:

  Windows   certutil -addstore -f "ROOT" certs\\internal-ca.crt
  macOS     sudo security add-trusted-cert -d -r trustRoot \\
              -k /Library/Keychains/System.keychain certs/internal-ca.crt
  Linux     sudo cp certs/internal-ca.crt \\
              /usr/local/share/ca-certificates/face-detector-internal.crt
            sudo update-ca-certificates
  Firefox   Settings > Privacy & Security > Certificates > View Certificates
            > Authorities > Import  (Firefox uses its own trust store)

Point clients at the hostname in the certificate, not at the IP:
  ${HOSTNAME_ARG}   ->   add to DNS, or to each client's hosts file

Verify (never use curl -k, which would hide exactly what you are testing):
  curl --cacert ${CERT_DIR}/internal-ca.crt -I https://${HOSTNAME_ARG}/

On WINDOWS clients, curl uses the schannel TLS backend, which insists on
checking certificate revocation. An internal CA publishes no CRL or OCSP
responder, so schannel reports:

  CertGetCertificateChain trust error CERT_TRUST_REVOCATION_STATUS_UNKNOWN

That is the revocation lookup failing, not a bad certificate. For command-line
verification on Windows add --ssl-no-revoke:

  curl --ssl-no-revoke --cacert ${CERT_DIR}/internal-ca.crt -I https://${HOSTNAME_ARG}/

Browsers are unaffected once internal-ca.crt is installed as a trusted root.

Renewal: reissue before ${EXPIRY}. Delete certs/server.* (keep the CA files),
re-run this script, then restart nginx. Clients need no change, because the CA
is unchanged. Set a calendar reminder ~60 days ahead; the Prometheus rule
CertificateExpiringSoon also fires at 30 days.

internal-ca.key can mint a certificate for ANY hostname that your clients will
trust. Move it to offline storage once the server certificate is issued.
------------------------------------------------------------------------
EOF
