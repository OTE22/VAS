# Server IP is operator-selected: never infer the camera/LAN interface as the server network.
WIZARD_SERVICES_READY=0
WIZARD_SERVICE_KEYS=(OLLAMA_MODEL OLLAMA_SQL_MODEL ML_NOTEBOOK_URL)
declare -A WIZARD_SERVICES=()

deployment_env_value() {
    local value
    value="$(read_env_kv "$1" 2>/dev/null || true)"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    printf '%s' "$value"
}

valid_server_ip() {
    python3 - "$1" <<'PY'
import ipaddress, sys
try:
    ip = ipaddress.IPv4Address(sys.argv[1])
    assert not (ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local or ip.is_reserved)
except (ValueError, AssertionError):
    sys.exit(1)
PY
}

wizard_server_network() {
    local existing_ip primary default
    existing_ip="$(deployment_env_value SERVER_IP)"
    primary="$(deployment_env_value PUBLIC_ORIGIN)"
    default="${SERVER_IP_FLAG:-$existing_ip}"
    if [ -z "$default" ]; then
        default="$(origin_host "$primary")"
        valid_server_ip "$default" || default=""
    fi
    printf 'Enter the IP assigned to THIS SERVER on the network used by clients.\n'
    printf 'Do not enter a camera address; no operating-system network settings will be changed.\n'
    while :; do
        wizard_prompt 'SERVER_IP (example: 10.21.5.22)' "$default"
        if valid_server_ip "$WIZARD_REPLY"; then
            SERVER_IP_FLAG="$WIZARD_REPLY"; break
        fi
        printf 'Enter a valid unicast IPv4 server address, not localhost or 0.0.0.0.\n'
    done
}

deployment_browser_origins() {
    local primary ip previous candidate normalized
    primary="$(resolve_public_origin)" || return 1
    ip="${SERVER_IP_FLAG:-$(deployment_env_value SERVER_IP)}"
    previous="$(read_env_kv PUBLIC_ORIGINS 2>/dev/null || true)"
    previous="${previous%\"}"; previous="${previous#\"}"
    previous="${previous%\'}"; previous="${previous#\'}"
    local -a candidates=("$primary") aliases=()
    local combined=""
    if [ -n "$ip" ]; then
        valid_server_ip "$ip" || { fail 'SERVER_IP must be a unicast IPv4 address'; return 1; }
        candidates+=("https://$ip")
    fi
    IFS=',' read -r -a aliases <<< "$previous"
    candidates+=("${aliases[@]}")
    for candidate in "${candidates[@]}"; do
        [ -n "$candidate" ] || continue
        normalized="$(wizard_origin "$candidate")" || return 1
        case ",$combined," in *",$normalized,"*) ;; *) combined="${combined:+$combined,}$normalized" ;; esac
    done
    printf '%s' "$combined"
}

verify_deployment_certificate() {
    local origin host check
    local -a origins=()
    IFS=',' read -r -a origins <<< "$1"
    for origin in "${origins[@]}"; do
        host="$(origin_host "$origin")"
        check=-checkhost
        [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && check=-checkip
        if ! openssl x509 -in "$ROOT/certs/server.crt" -noout "$check" "$host" >/dev/null 2>&1; then
            fail "certificate does not cover '$host'. Provision a certificate covering PUBLIC_ORIGINS, including SERVER_IP."
            return 1
        fi
    done
}

deployment_apply_network() {
    [ -n "${SERVER_IP_FLAG:-}" ] || return 0
    local origins primary
    origins="$(deployment_browser_origins)" || die 'Invalid browser origin configuration'
    primary="$(resolve_public_origin)" || return 1
    upsert_env_kv PUBLIC_ORIGIN "$primary" || return 1
    upsert_env_kv SERVER_IP "$SERVER_IP_FLAG" || return 1
    upsert_env_kv PUBLIC_ORIGINS "$origins" || return 1
    export PUBLIC_ORIGIN="$primary" PUBLIC_ORIGINS="$origins"
}

wizard_service_valid() {
    local key="$1" value="$2"
    case "$key" in
        OLLAMA_MODEL|OLLAMA_SQL_MODEL)
            [[ "$value" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}$ ]] ;;
        ML_NOTEBOOK_URL)
            [ -z "$value" ] && return 0
            python3 - "$value" <<'PY'
import sys
from urllib.parse import urlsplit
try:
    value=sys.argv[1]; url=urlsplit(value)
    assert url.scheme == 'https' and url.hostname and url.port in (None,443)
    assert not url.username and not url.password and not url.query and not url.fragment
    assert not any(c.isspace() for c in value)
except (ValueError, AssertionError):
    sys.exit(1)
PY
            ;;
        *) return 1 ;;
    esac
}

wizard_service_settings() {
    local key value choice
    printf '\nStep 6 of 8 — Service connections\n'
    printf 'Automatic: private PostgreSQL, Redis, Ollama and map-service connections use Docker service names.\n'
    printf 'Database roles, secret files, webhook authentication and migration settings are generated/checked.\n'
    for key in "${WIZARD_SERVICE_KEYS[@]}"; do
        value="$(read_env_kv "$key" 2>/dev/null || true)"
        [ -n "$value" ] || { [ "$key" = ML_NOTEBOOK_URL ] || value=gpt-oss:20b; }
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        WIZARD_SERVICES[$key]="$value"
        printf '  %s = %s\n' "$key" "${value:-disabled until notebook is deployed}"
    done
    printf 'Language models must fit your hardware and exist in the Ollama volume; image archives do not include them.\n'
    printf 'Notebook URLs point to an already deployed HTTPS notebook service; this installer does not start one.\n'
    printf 'Offline maps need prepared archives/fonts under map-data/production; missing assets will be reported.\n'
    printf '  1) Keep these values (recommended)\n  2) Choose language models / notebook URL\n'
    while :; do
        wizard_prompt 'Service configuration option' 1
        choice="$WIZARD_REPLY"
        case "$choice" in 1|2) break ;; *) printf 'Please enter 1 or 2.\n' ;; esac
    done
    for key in "${WIZARD_SERVICE_KEYS[@]}"; do
        if [ "$choice" = 2 ] || ! wizard_service_valid "$key" "${WIZARD_SERVICES[$key]}"; then
            while :; do
                wizard_prompt "$key (use - to disable notebook only)" "${WIZARD_SERVICES[$key]}"
                value="$WIZARD_REPLY"
                [ "$key" != ML_NOTEBOOK_URL ] || [ "$value" != - ] || value=""
                if wizard_service_valid "$key" "$value"; then WIZARD_SERVICES[$key]="$value"; break; fi
                printf 'Use a model tag such as gpt-oss:20b, or an HTTPS notebook URL without credentials/query tokens.\n'
            done
        fi
    done
    WIZARD_SERVICES_READY=1
}

wizard_apply_services() {
    [ "$WIZARD_SERVICES_READY" = 1 ] || return 0
    local key
    for key in "${WIZARD_SERVICE_KEYS[@]}"; do
        wizard_service_valid "$key" "${WIZARD_SERVICES[$key]}" || die "Invalid service setting: $key"
        upsert_env_kv "$key" "${WIZARD_SERVICES[$key]}" || return 1
        export "$key=${WIZARD_SERVICES[$key]}"
    done
}

wizard_asset_readiness() {
    [ "${GUIDED_INSTALL:-0}" = 1 ] || return 0
    stage_begin '09 network and optional features'
    local missing=0
    if have ip && [ -n "${SERVER_IP_FLAG:-}" ]; then
        if ! ip -o -4 address show | awk -v target="$SERVER_IP_FLAG" '
            { split($4, address, "/"); if (address[1] == target) found=1 }
            END { exit !found }'; then
            warn "SERVER_IP=$SERVER_IP_FLAG is not assigned to a local interface. Configure the server network before clients can use it; the installer does not change network interfaces."
            missing=1
        fi
    fi
    if ! compgen -G "$ROOT/map-data/production/*.mbtiles" >/dev/null; then
        warn 'Maps need prepared .mbtiles archives in map-data/production. See Docs/offline-deployment.md.'
        missing=1
    fi
    if ! compgen -G "$ROOT/map-data/production/fonts/*.ttf" >/dev/null \
       && ! compgen -G "$ROOT/map-data/production/fonts/*.otf" >/dev/null; then
        warn 'Offline map labels need font files under map-data/production/fonts.'
        missing=1
    fi
    if [ -z "${WIZARD_SERVICES[ML_NOTEBOOK_URL]:-}" ]; then
        info 'Notebook opening is disabled until a notebook service and ML_NOTEBOOK_URL are configured.'
    fi
    if [ "$missing" = 1 ]; then
        stage_warn 'Follow-up needed for the network or map assets listed above; see the final report.'
    else
        stage_pass 'Map archives found; post-start map health checks must verify their contents.'
    fi
}
