#!/usr/bin/env bash
# DNS-only helper for the existing intranet DNS server. Never edits applications.
# Preview by default. Authenticated updates require an RFC 2136-capable DNS server.
set -euo pipefail
MODE=preview
DNS_SERVER=10.0.16.1
SERVER_IP=''
KEY_FILE=''
GSS=0
usage() {
    cat <<'HELP'
Application DNS records on your EXISTING internal DNS server:
  bash scripts/intranet_dns.sh --server-ip ASSIGNED_SERVER_IP
  bash scripts/intranet_dns.sh --check [--server-ip ASSIGNED_SERVER_IP]
  bash scripts/intranet_dns.sh --apply --server-ip ASSIGNED_SERVER_IP --tsig-key /path/to/dns.key
  bash scripts/intranet_dns.sh --apply --server-ip ASSIGNED_SERVER_IP --gss-tsig
Options:
  --dns-server IP    internal DNS server (default 10.0.16.1)
  --check            query all three names; makes no changes
  --apply            authenticated DNS update; requires server support/authorization
  --tsig-key FILE    DNS administrator's update key (not your Ubuntu password)
  --gss-tsig         use an existing authorized Kerberos login, e.g. Windows DNS
Without --apply or --check, print the proposed records only.
The DNS administrator must create/authorize the internal. and armyeye-chatbot.
zones first. Router DNS may require its own management interface instead.
No DNS service, client settings, application settings, or certificates are changed.
HELP
}
while [ $# -gt 0 ]; do
    case "$1" in
        --check) MODE=check; shift ;;
        --apply) MODE=apply; shift ;;
        --server-ip) SERVER_IP="${2:?--server-ip needs an IPv4 address}"; shift 2 ;;
        --dns-server) DNS_SERVER="${2:?--dns-server needs an IP address}"; shift 2 ;;
        --tsig-key) KEY_FILE="${2:?--tsig-key needs a path}"; shift 2 ;;
        --gss-tsig) GSS=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
python3 - "$DNS_SERVER" "$SERVER_IP" "$MODE" <<'PY'
import ipaddress,sys
try:
    dns=ipaddress.ip_address(sys.argv[1])
    if dns.is_unspecified or dns.is_multicast: raise ValueError()
    if sys.argv[2]:
        ip=ipaddress.IPv4Address(sys.argv[2])
        if ip.is_loopback or ip.is_multicast or ip.is_unspecified or int(ip)==0xffffffff: raise ValueError()
    elif sys.argv[3]!='check':
        sys.exit('Provide --server-ip with the actual assigned server IP. No changes made.')
except ValueError:
    sys.exit('Invalid DNS/server IP address. No changes made.')
PY
NAMES=(face-detector.internal armyeye-vms.internal armyeye-chatbot)
if [ "$MODE" = preview ]; then
    printf 'DNS server: %s\nProposed A records (preview only):\n' "$DNS_SERVER"
    for name in "${NAMES[@]}"; do printf '  %-26s %s\n' "$name" "$SERVER_IP"; done
    echo 'Give these records to the DNS administrator, or use --apply with an authorized DNS update credential.'
    echo 'No changes made. Clients must use internal DNS for these private names.'
    exit 0
fi
command -v dig >/dev/null || { echo 'dig is required (Ubuntu dnsutils package).' >&2; exit 1; }
check_records() {
    local name answer failed=0
    for name in "${NAMES[@]}"; do
        if ! answer=$(dig @"$DNS_SERVER" "$name." A +time=3 +tries=1 +short); then
            echo "$name: DNS query failed" >&2; failed=1; continue
        fi
        if ! printf '%s\n' "$answer" | python3 -c '
import ipaddress,sys
addresses=[]
for line in sys.stdin:
    try: addresses.append(str(ipaddress.IPv4Address(line.strip())))
    except ValueError: pass
want=sys.argv[1]
sys.exit(0 if addresses and (not want or set(addresses)=={want}) else 1)
' "$SERVER_IP"; then
            echo "$name: missing, failed, or does not match ${SERVER_IP:-an IPv4 address}" >&2
            failed=1
        else printf '%s -> %s\n' "$name" "$answer"; fi
    done
    return "$failed"
}
if [ "$MODE" = check ]; then check_records; exit $?; fi
command -v nsupdate >/dev/null || { echo 'nsupdate is required (Ubuntu bind9-dnsutils package).' >&2; exit 1; }
if [ -n "$KEY_FILE" ] && [ "$GSS" = 1 ]; then echo 'Choose one authentication method.' >&2; exit 2; fi
if [ -n "$KEY_FILE" ]; then
    [ -r "$KEY_FILE" ] || { echo 'Cannot read DNS update key.' >&2; exit 1; }
    AUTH=(-k "$KEY_FILE")
elif [ "$GSS" = 1 ]; then AUTH=(-g)
else echo 'DNS updates need --tsig-key or --gss-tsig authorized by your DNS administrator. No changes made.' >&2; exit 2
fi
# Check both zone prerequisites before submitting either update.
for zone in internal armyeye-chatbot; do
    answer=$(dig @"$DNS_SERVER" "$zone." SOA +time=3 +tries=1 +noall +answer)
    if ! printf '%s\n' "$answer" | awk -v expected="$zone." '$1==expected && $4=="SOA" {found=1} END {exit !found}'; then
        echo "DNS zone $zone. is unavailable. Ask the DNS administrator to create/authorize it; no updates sent." >&2
        exit 1
    fi
done
umask 077
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# Retain the pre-update answers for the administrator if a later zone update fails.
BACKUP=$(mktemp "${TMPDIR:-/tmp}/vas-dns-before.XXXXXXXX.txt")
for name in "${NAMES[@]}"; do
    dig @"$DNS_SERVER" "$name." A +time=3 +tries=1 +noall +answer >> "$BACKUP"
done
cat > "$TMP/update" <<UPDATE
server $DNS_SERVER
zone internal.
update delete face-detector.internal. A
update add face-detector.internal. 60 A $SERVER_IP
update delete armyeye-vms.internal. A
update add armyeye-vms.internal. 60 A $SERVER_IP
send
zone armyeye-chatbot.
update delete armyeye-chatbot. A
update add armyeye-chatbot. 60 A $SERVER_IP
send
UPDATE
if ! nsupdate -v -t 15 "${AUTH[@]}" "$TMP/update"; then
    echo "DNS update failed or was only partly applied; zones update separately. Previous answers: $BACKUP" >&2
    exit 1
fi
check_records
echo "DNS answers verified. Previous answers: $BACKUP"
echo 'Applications and certificates were not changed.'
