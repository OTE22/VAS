#!/usr/bin/env bash
# Guided startup/input; existing deployment stages own host/container changes.

wizard_require_privileges() {
    # Re-launch before collecting choices, so sudo never discards a completed plan.
    [ "${DRY_RUN:-0}" = 1 ] && return 0
    [ "$(id -u)" = 0 ] && return 0
    if is_windows_shell || is_wsl2; then
        require_root 'guided installation'
        return
    fi
    have sudo || die 'Administrator access is required. Ask an administrator to run ./deploy.sh as root (or install sudo).'
    printf '\nVAS installation needs administrator access for Docker, system packages and storage.\n'
    printf 'Enter your Linux sudo password if asked. The setup guide will open next.\n'
    printf 'You will review your choices before any installation changes.\n'
    exec sudo -- bash "$ROOT/deploy.sh" "$@"
}


wizard_prompt() {
    local label="$1" default="${2:-}" reply
    printf '\n%s%s: ' "$label" "${default:+ [$default]}" >&2
    IFS= read -r reply || die "Input closed. Re-run ./deploy.sh to continue."
    WIZARD_REPLY="${reply:-$default}"
}

wizard_origin() {
    python3 - "$1" <<'PY'
import ipaddress, re, sys
from urllib.parse import urlsplit
value = sys.argv[1].strip()
if '://' not in value:
    value = 'https://' + value
try:
    url = urlsplit(value)
    host = url.hostname or ''
    assert url.scheme == 'https' and url.port in (None, 443)
    assert not url.username and not url.password and not url.query and not url.fragment
    assert url.path in ('', '/') and not any(c.isspace() for c in value)
    # The shipped host/TLS helpers currently support DNS names and IPv4 only.
    assert ':' not in host and len(host) <= 253
    if re.fullmatch(r'[0-9.]+', host):
        ipaddress.IPv4Address(host)
    else:
        assert all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', part)
                   for part in host.split('.'))
    print('https://' + host)
except (AssertionError, ValueError):
    print('Enter a DNS name or IPv4 address, using HTTPS on port 443, without a path or credentials.', file=sys.stderr)
    sys.exit(1)
PY
}

# Only these non-secret deployment defaults are exposed by the guide.
WIZARD_SETTING_KEYS=(LOG_LEVEL DATA_RETENTION_DAYS BACKUP_RETENTION_DAYS MAX_STORAGE_GB)
declare -A WIZARD_SETTINGS=()
WIZARD_DEFAULTS_READY=0

wizard_setting_valid() {
    local key="$1" value="$2"
    if [ "$key" = LOG_LEVEL ]; then
        case "$value" in INFO|WARNING|ERROR) return 0 ;; *) return 1 ;; esac
    fi
    [[ "$value" =~ ^[1-9][0-9]{0,6}$ ]] || return 1
    case "$key" in
        DATA_RETENTION_DAYS|BACKUP_RETENTION_DAYS) [ "$value" -le 36500 ] ;;
        MAX_STORAGE_GB) [ "$value" -le 1048576 ] ;;
        *) return 1 ;;
    esac
}

wizard_default_settings() {
    local key value choice
    local -A defaults=([LOG_LEVEL]=INFO [DATA_RETENTION_DAYS]=365
                      [BACKUP_RETENTION_DAYS]=14 [MAX_STORAGE_GB]=5000)
    printf '\nStep 5 of 8 — Default configuration\n'
    printf 'Press Enter to keep the existing configuration, or use the shipped defaults on a new server.\n'
    printf '  LOG_LEVEL: INFO records normal operations; WARNING/ERROR produce fewer messages.\n'
    printf '  DATA_RETENTION_DAYS: detections/events become eligible for cleanup after this many days.\n'
    printf '  BACKUP_RETENTION_DAYS: older automatic backups become eligible for deletion.\n'
    printf '  MAX_STORAGE_GB: dashboard reporting capacity only; it does not reserve space or enforce a disk quota.\n'
    for key in "${WIZARD_SETTING_KEYS[@]}"; do
        value="$(read_env_kv "$key" 2>/dev/null || true)"
        if [ -n "$value" ]; then
            # Compose accepts quoted dotenv values; keep their meaning when displaying/editing.
            value="${value%\"}"; value="${value#\"}"
            value="${value%\'}"; value="${value#\'}"
            WIZARD_SETTINGS[$key]="$value"
            printf '  %s = %s (existing)\n' "$key" "$value"
        else
            WIZARD_SETTINGS[$key]="${defaults[$key]}"
            printf '  %s = %s (default)\n' "$key" "${defaults[$key]}"
        fi
    done
    printf 'HTTPS, generated passwords, required password rotation and offline runtime stay enabled.\n'
    printf 'Recognition thresholds and GPU concurrency keep their existing settings; no camera capacity is assumed.\n'
    printf 'These are startup defaults. Settings saved through the application may override them.\n'
    printf '  1) Keep the values shown (recommended)\n  2) Customize these four settings\n'
    while :; do
        wizard_prompt 'Configuration option' 1
        choice="$WIZARD_REPLY"
        case "$choice" in 1|2) break ;; *) printf 'Please enter 1 or 2.\n' ;; esac
    done
    for key in "${WIZARD_SETTING_KEYS[@]}"; do
        if [ "$choice" = 2 ] || ! wizard_setting_valid "$key" "${WIZARD_SETTINGS[$key]}"; then
            while :; do
                wizard_prompt "$key" "${WIZARD_SETTINGS[$key]}"
                value="$WIZARD_REPLY"
                if wizard_setting_valid "$key" "$value"; then
                    WIZARD_SETTINGS[$key]="$value"; break
                fi
                printf 'Use INFO/WARNING/ERROR for LOG_LEVEL, 1–36500 for retention days, or 1–1048576 for storage GB.\n'
            done
        fi
    done
    WIZARD_DEFAULTS_READY=1
}

wizard_apply_defaults() {
    [ "${WIZARD_DEFAULTS_READY:-0}" = 1 ] || return 0
    local key
    for key in "${WIZARD_SETTING_KEYS[@]}"; do
        wizard_setting_valid "$key" "${WIZARD_SETTINGS[$key]}" || die "Invalid guided configuration: $key"
        upsert_env_kv "$key" "${WIZARD_SETTINGS[$key]}" || die "Cannot save $key in docker/.env"
        # Dry-run Compose validation must use the same choices without writing files.
        export "$key=${WIZARD_SETTINGS[$key]}"
    done
}

wizard_collect() {
    printf '\nVAS guided installation\n'
    printf 'Answer eight setup steps, review once, then follow the numbered installation stages.\n'
    printf 'Press Enter for the displayed default. Setup questions do not change the server.\n'
    printf 'Use the release checkout and its matching model weights on the target Linux server.\n'
    printf 'Existing data, passwords and certificates are preserved.\n'
    printf '\nStep 1 of 8 — Installation method\n'
    printf '  1) Load saved Docker images (no application build or image pull)\n'
    printf '  2) Fresh installation: build images from this checkout\n'
    local default=2 origin existing selection
    [ "$IMAGE_MODE" = load ] && default=1
    while :; do
        wizard_prompt 'Choose 1 or 2' "$default"
        case "$WIZARD_REPLY" in
            1) IMAGE_MODE=load; break ;;
            2) IMAGE_MODE=build; break ;;
            *) printf 'Please enter 1 or 2.\n' ;;
        esac
    done
    printf '\nStep 2 of 8 — Installation files\n'
    printf 'Required weights: weights/det_10g.onnx and weights/w600k_r50.onnx, with WEIGHTS_MANIFEST.json.\n'
    printf 'A release package may contain weights/ and images/ directories.\n'
    while :; do
        wizard_prompt 'Release package directory (Enter to use files already here)' "$DEPLOY_PACKAGE"
        DEPLOY_PACKAGE="$WIZARD_REPLY"
        [ -z "$DEPLOY_PACKAGE" ] || [ -d "$DEPLOY_PACKAGE" ] && break
        printf 'That directory does not exist. Enter its full path, without surrounding quotes.\n'
    done
    if [ "$IMAGE_MODE" = load ]; then
        printf 'Add docker save archives (.tar, .tar.gz, .tgz). Repeat for multiple archives.\n'
        printf 'Enter leaves images already loaded in Docker available for validation.\n'
        while :; do
            wizard_prompt 'Image archive path (Enter when finished)'
            [ -n "$WIZARD_REPLY" ] || break
            if [ -f "$WIZARD_REPLY" ] && [ -r "$WIZARD_REPLY" ]; then
                IMAGE_ARCHIVES+=("$WIZARD_REPLY")
            else
                printf 'File not found or unreadable. Enter the full path, without surrounding quotes.\n'
            fi
        done
        printf 'The installer will list ALL missing service image tags before starting the database.\n'
    elif [ "${#IMAGE_ARCHIVES[@]}" -gt 0 ]; then
        die 'Image archives were supplied with build mode. Choose load mode or remove --image-archive.'
    fi
    printf '\nStep 3 of 8 — Server network and browser address\n'
    wizard_server_network
    printf 'Use the SERVER address reachable by your users, not a camera address.\n'
    printf 'Example: https://10.21.5.22 or https://face-detector.internal\n'
    printf 'DNS must point to this server. The installer does not change its network address.\n'
    existing="$(deployment_env_value PUBLIC_ORIGIN)"
    default="${PUBLIC_ORIGIN_FLAG:-${existing:-https://$SERVER_IP_FLAG}}"
    while :; do
        wizard_prompt 'VAS browser URL' "$default"
        if origin="$(wizard_origin "$WIZARD_REPLY")"; then
            if [ -n "$existing" ] && [ "$origin" != "$existing" ]; then
                printf 'This is an existing installation at %s. Keep that URL here.\n' "$existing"
                printf 'Changing an existing address requires a planned certificate and origin update.\n'
                default="$existing"
                continue
            fi
            PUBLIC_ORIGIN_FLAG="$origin"; break
        fi
    done
    printf '\nStep 4 of 8 — Hardware and connectivity\n'
    printf '  1) NVIDIA GPU required (recommended for camera workloads)\n'
    printf '  2) CPU only (lower throughput)\n'
    default=1; [ "$FORCE_CPU" = 1 ] && default=2
    while :; do
        wizard_prompt 'Hardware option' "$default"
        case "$WIZARD_REPLY" in
            1) FORCE_CPU=0; REQUIRE_GPU=1; break ;;
            2) FORCE_CPU=1; REQUIRE_GPU=0; break ;;
            *) printf 'Please enter 1 or 2.\n' ;;
        esac
    done
    wizard_gpu_setup
    printf '  1) Internet available for host packages/build dependencies\n'
    printf '  2) Offline: required packages, images and models must already be supplied\n'
    default=1; [ "$FORCE_OFFLINE" = 1 ] && default=2
    while :; do
        wizard_prompt 'Installation connectivity' "$default"
        case "$WIZARD_REPLY" in
            1) FORCE_OFFLINE=0; break ;;
            2) FORCE_OFFLINE=1; break ;;
            *) printf 'Please enter 1 or 2.\n' ;;
        esac
    done
    wizard_default_settings
    wizard_service_settings
    wizard_storage_settings
    printf '\nStep 8 of 8 — Review\n'
    printf 'Method: %s | Browser: %s | CPU only: %s | Offline install: %s\n' \
        "$IMAGE_MODE" "$PUBLIC_ORIGIN_FLAG" "$FORCE_CPU" "$FORCE_OFFLINE"
    printf 'GPU setup: %s | driver package: %s (used only if a driver change is needed)\n' "$GPU_SETUP_POLICY" "$NVIDIA_DRIVER_PACKAGE"
    printf 'SERVER_IP=%s\n' "$SERVER_IP_FLAG"
    local reviewed_origins
    reviewed_origins="$(deployment_browser_origins)" || die "Fix invalid browser origins before installing"
    printf 'PUBLIC_ORIGINS=%s\n' "$reviewed_origins"
    printf 'VAS_DATA_ROOT=%s\n' "${WIZARD_DATA_ROOT:-Docker-managed volumes}"
    printf 'Package: %s | Additional image archives: %s\n' "${DEPLOY_PACKAGE:-none}" "${#IMAGE_ARCHIVES[@]}"
    local setting
    for setting in "${WIZARD_SETTING_KEYS[@]}"; do
        printf '%s=%s\n' "$setting" "${WIZARD_SETTINGS[$setting]}"
    done
    for setting in "${WIZARD_SERVICE_KEYS[@]}"; do
        printf '%s=%s\n' "$setting" "${WIZARD_SERVICES[$setting]}"
    done
    printf 'The installer will check the host, configure secrets/TLS, verify weights/images,\n'
    printf 'prepare the database, start services and run acceptance checks.\n'
    printf 'An existing deployment uses the backup/upgrade flow; fresh mode never erases data.\n'
    printf 'Production runtime stays offline in both installation modes.\n'
    printf '\nInstallation order: host checks -> files/secrets/TLS -> configuration/GPU/weights\n'
    printf ' -> load or build images -> database -> services -> model checks -> health report.\n'
    printf 'If a required step fails, stop at its instructions, fix the issue and rerun this guide.\n'
    printf '\n  1) Install now\n  2) Preview checks only (--dry-run)\n  3) Cancel\n'
    while :; do
        wizard_prompt 'Continue' 2
        selection="$WIZARD_REPLY"
        case "$selection" in
            1) break ;;
            2) DRY_RUN=1; break ;;
            3) printf 'Cancelled. No installation changes made.\n'; exit 0 ;;
            *) printf 'Please enter 1, 2 or 3.\n' ;;
        esac
    done
    GUIDED_INSTALL=1
    SUBCOMMAND=deploy
    export DRY_RUN FORCE_OFFLINE
}

load_deployment_images() {
    [ "${IMAGES_IMPORTED:-0}" = 1 ] && return 0
    local archive
    local -a archives=("${IMAGE_ARCHIVES[@]}")
    if [ -n "$DEPLOY_PACKAGE" ]; then
        [ -d "$DEPLOY_PACKAGE" ] || { fail "Package directory not found: $DEPLOY_PACKAGE"; return 1; }
        for archive in "$DEPLOY_PACKAGE"/images/*.tar "$DEPLOY_PACKAGE"/images/*.tar.gz "$DEPLOY_PACKAGE"/images/*.tgz; do
            [ -f "$archive" ] && archives+=("$archive")
        done
    fi
    for archive in "${archives[@]}"; do
        [ -f "$archive" ] && [ -r "$archive" ] || { fail "Image archive not readable: $archive"; return 1; }
        info "Loading Docker archive: $archive"
        run docker load --input "$archive" || return 1
    done
    IMAGES_IMPORTED=1
}

stage_prebuilt_images() {
    stage_begin '10 prebuilt images'
    load_deployment_images || stage_fail 'Image import failed. Supply a docker save archive from the matching release.'
    if [ "$DRY_RUN" = 1 ]; then
        stage_skip 'Would check all Compose image tags after import; image contents have not been validated.'
        return 0
    fi
    local inventory image missing=0
    inventory="$(compose config --images)" || stage_fail 'Cannot resolve required image tags from Compose.'
    [ -n "$inventory" ] || stage_fail 'Compose returned no images.'
    while IFS= read -r image; do
        [ -n "$image" ] || continue
        if docker image inspect "$image" >/dev/null 2>&1; then
            ok "Available: $image"
        else
            fail "Missing image: $image"
            missing=$((missing + 1))
        fi
    done < <(printf '%s\n' "$inventory" | sort -u)
    [ "$missing" = 0 ] || stage_fail "$missing required image(s) missing. Export those exact tags with docker save on the release host, supply --image-archive=/path/images.tar, and rerun. No build/pull fallback is permitted in load mode."
    PENDING_DEPLOY_VERSION="loaded-images ($(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || echo release-package))"
    stage_pass 'Required images are present; startup uses --no-build --pull never.'
}

wizard_next_steps() {
    [ "${GUIDED_INSTALL:-0}" = 1 ] || return 0
    if [ "$DRY_RUN" = 1 ]; then
        printf '\nPreview only. To install, rerun ./deploy.sh and select Install now.\n'
        return 0
    fi
    printf '\nNext steps\n'
    printf '1. Trust %s/certs/internal-ca.crt on each client PC (never distribute the .key).\n' "$ROOT"
    printf '2. Open %s in the browser. Allow HTTPS port 443 through your server firewall.\n' "$PUBLIC_ORIGIN_FLAG"
    printf '3. For a NEW install, the admin password is in %s/secrets/bootstrap_admin_password.\n' "$ROOT"
    printf '   Read it privately with sudo; sign in as admin and change it when prompted.\n'
    printf '   Existing installations keep their current accounts and passwords.\n'
    printf '4. In VMS, configure the publisher URL: %s/api/webhook/<pipeline_id>\n' "$PUBLIC_ORIGIN_FLAG"
    printf '   Send X-Webhook-Key from secrets/webhook_api_keys (read privately), or an issued VAS ingest credential.\n'
    printf '   Trust the VAS CA on the VMS sender; verify a real camera detection after setup.\n'
    printf '5. Check optional maps, language models and notebooks using the readiness report.\n'
    printf 'Configuration: %s/docker/.env (contains secrets; keep it private).\n' "$ROOT"
    printf 'Storage locations and model/map destinations: sudo ./deploy.sh storage\n'
    printf 'Support: ./deploy.sh status | ./deploy.sh health | ./deploy.sh logs face_recognition\n'
}
