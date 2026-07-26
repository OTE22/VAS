#!/usr/bin/env bash
#
# Generate every production secret this deployment needs.
#
#   bash scripts/setup/generate-secrets.sh
#
# Writes Docker secret files under secrets/, the Redis ACL under
# docker/redis/users.acl, and prints the .env lines to add. Both directories are
# gitignored. Existing files are never overwritten — delete them deliberately to
# rotate.
#
# Rotating JWT_SECRET_KEY invalidates every issued token, logging everyone out.
# That is intended.

set -euo pipefail

cd "$(dirname "$0")/../.."

SECRETS_DIR="secrets"
ACL_DIR="docker/redis"
ACL_FILE="${ACL_DIR}/users.acl"

mkdir -p "$SECRETS_DIR" "$ACL_DIR"

if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required" >&2
    exit 1
fi

# 48+ characters of base64 keeps the JWT key well above HS256's useful key size.
gen() { openssl rand -base64 "${1:-32}" | tr -d '\n'; }
sha256_hex() { printf '%s' "$1" | openssl dgst -sha256 | awk '{print $NF}'; }

write_secret() {
    local name="$1" value="$2" path="${SECRETS_DIR}/$1"
    if [ -f "$path" ]; then
        echo "  = ${path} already exists, keeping it"
        return
    fi
    printf '%s' "$value" > "$path"
    chmod 600 "$path" 2>/dev/null || true
    echo "  + ${path}"
}

echo "Generating Docker secret files..."
write_secret jwt_secret "$(gen 48)"
write_secret bootstrap_admin_password "$(gen 24)"

# Database and Redis passwords go in .env, because compose interpolates them
# into connection URLs.
FR_APP_PASSWORD="$(gen 32)"
FR_MIGRATOR_PASSWORD="$(gen 32)"
FR_READONLY_PASSWORD="$(gen 32)"
FR_BACKUP_PASSWORD="$(gen 32)"
POSTGRES_SUPERUSER_PASSWORD="$(gen 32)"
REDIS_PASSWORD="$(gen 32)"
REDIS_MONITOR_PASSWORD="$(gen 32)"

echo
echo "Generating Redis ACL..."
if [ -f "$ACL_FILE" ]; then
    echo "  = ${ACL_FILE} already exists, keeping it (delete it to rotate)"
else
    sed -e "s/#REPLACE_WITH_SHA256_HEX/#$(sha256_hex "$REDIS_PASSWORD")/" \
        -e "s/#REPLACE_WITH_MONITOR_SHA256_HEX/#$(sha256_hex "$REDIS_MONITOR_PASSWORD")/" \
        "${ACL_DIR}/users.acl.example" > "$ACL_FILE"
    chmod 600 "$ACL_FILE" 2>/dev/null || true
    echo "  + ${ACL_FILE}"
fi

cat <<EOF

------------------------------------------------------------------------
Add these to .env (they are NOT written automatically, so an existing
deployment is never silently re-keyed):

PUBLIC_ORIGIN=https://face-detector.internal
POSTGRES_SUPERUSER_PASSWORD=${POSTGRES_SUPERUSER_PASSWORD}
FR_APP_PASSWORD=${FR_APP_PASSWORD}
FR_MIGRATOR_PASSWORD=${FR_MIGRATOR_PASSWORD}
FR_READONLY_PASSWORD=${FR_READONLY_PASSWORD}
FR_BACKUP_PASSWORD=${FR_BACKUP_PASSWORD}
REDIS_PASSWORD=${REDIS_PASSWORD}
REDIS_MONITOR_PASSWORD=${REDIS_MONITOR_PASSWORD}
------------------------------------------------------------------------

Next:
  1. Apply the database roles:  psql -f db/roles.sql   (see the header there)
  2. Generate TLS material:     bash scripts/tls/make-internal-ca.sh
  3. Verify the configuration:  docker compose -f docker/docker-compose.prod.yml config -q

This output contains live credentials. Do not paste it into a ticket or chat.
EOF
