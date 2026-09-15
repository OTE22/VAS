#!/usr/bin/env bash
# Same-server intranet checks. Network addresses belong to DNS, not app settings.
# This script never rewrites certificates, origins, interfaces, or services.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_ADDRESS=''
usage() {
    cat <<'HELP'
Same-server offline intranet check (read-only):
  sudo bash scripts/move_to_intranet.sh
  sudo bash scripts/move_to_intranet.sh --server-address 10.90.0.20
The optional IPv4 address is used only for HTTPS connection tests, bypassing DNS.
It is never saved in application configuration or certificates.
After moving the server, update internal DNS for these stable names:
  face-detector.internal   armyeye-vms.internal   armyeye-chatbot
No application IP update, certificate reissue, reinstall, or image pull is needed.
HELP
}
while [ $# -gt 0 ]; do
    case "$1" in
        --check|--dry-run) shift ;;
        --server-address) SERVER_ADDRESS="${2:?--server-address needs an IPv4 address}"; shift 2 ;;
        --apply|--ip) echo 'IP-based application updates have been retired. Update internal DNS, then run --check.' >&2; exit 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
if [ -n "$SERVER_ADDRESS" ]; then
    SERVER_ADDRESS="$(python3 - "$SERVER_ADDRESS" <<'PY'
import ipaddress,sys
try:
    ip=ipaddress.IPv4Address(sys.argv[1])
    if ip.is_multicast or ip.is_unspecified or int(ip)==0xffffffff: raise ValueError()
except ValueError:
    sys.exit('Supply a unicast IPv4 server address for the connection test.')
print(ip)
PY
)"
fi
for tool in docker openssl curl python3; do command -v "$tool" >/dev/null; done
API=face_detector_prod-face_recognition-1
for name in "$API" face_detector_prod-nginx-1 VMS VMS-db vas-assistant vas-assistant-gate face_detector_prod-ollama-1; do
    [ "$(docker inspect -f '{{.State.Running}}' "$name")" = true ] || { echo "$name is not running." >&2; exit 1; }
done
docker exec -i "$API" python - <<'PY'
from config import settings
from backend.security.offline_policy import collect_offline_violations, offline_mode
from backend.security.origins import approved_origins
assert offline_mode(settings, bool(settings.is_production)), 'VAS offline policy is disabled'
fatal=[f.code for f in collect_offline_violations(settings, production=bool(settings.is_production)) if f.severity=='fatal']
assert not fatal, 'Offline policy failures: '+', '.join(fatal)
assert approved_origins(settings)==['https://face-detector.internal'], 'VAS should approve only its stable hostname'
print('VAS offline and hostname-origin policies: passed')
PY
check_https() {
    local host="$1" leaf="$2" path="$3"
    local resolve=()
    openssl verify -purpose sslserver -verify_hostname "$host" -CAfile "$ROOT/certs/internal-ca.crt" "$ROOT/certs/$leaf.crt"
    if [ -n "$SERVER_ADDRESS" ]; then resolve=(--resolve "$host:443:$SERVER_ADDRESS"); fi
    curl --noproxy '*' --fail --silent --show-error --max-time 20 \
        --cacert "$ROOT/certs/internal-ca.crt" "${resolve[@]}" \
        -o /dev/null -w "https://$host$path HTTP %{http_code}\n" "https://$host$path"
}
check_https face-detector.internal server /signin
check_https face-detector.internal server /health/ready
check_https armyeye-vms.internal vms /login
check_https armyeye-chatbot laf-ai-chatbot /login
check_https armyeye-chatbot laf-ai-chatbot /_gate/health
echo 'Read-only checks passed. No application, certificate, or network settings changed.'
if [ -n "$SERVER_ADDRESS" ]; then echo 'DNS was bypassed for these probes; test actual DNS separately from an intranet PC.'; fi
echo 'Internal DNS must point all three stable hostnames to the server address; clients need CA trust and TCP 443 access.'
echo 'After an offline reboot, also test camera playback, detections/webhooks, maps, and local chatbot responses.'
