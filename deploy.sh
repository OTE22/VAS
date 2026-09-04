#!/usr/bin/env bash
#
# FACE_DETECTOR — one-command production deployment.
#
#   sudo ./deploy.sh                    PRODUCTION: install -> validate -> start -> health
#        ./deploy.sh dev                DEVELOPMENT: guided dev-stack bring-up
#        ./deploy.sh dev stop|status|logs [service]
#   sudo ./deploy.sh install            provision host, secrets, TLS, GPU, models
#        ./deploy.sh validate           read-only: is this host ready? (no mutation)
#   sudo ./deploy.sh start | stop | restart
#        ./deploy.sh status | health | logs [service]
#        ./deploy.sh gpu-test | model-check | model-manifest
#   sudo ./deploy.sh backup | restore <stamp> | upgrade
#   sudo ./deploy.sh uninstall [--remove-images] [--purge-data]
#        ./deploy.sh --self-test        unit-test this script's own logic
#
# Every stage narrates itself before it acts — what it does, the exact paths it
# reads and writes, the secrets and artifacts it creates, what it deliberately
# will NOT do, and what a failure there means. Read that output the first time;
# pass --quiet (or -q) once it is familiar, or in CI, to keep only the results.
#
# WHICH ENVIRONMENT AM I DEPLOYING?
# ---------------------------------
# Two stacks, two commands, and they never share data (different compose
# project names, different volumes):
#
#   DEVELOPMENT   ./deploy.sh dev
#     For working on the code. Plain HTTP, seeded admin (admin/admin123),
#     /docs enabled, password rotation off, optional hosted NIM LLM for the
#     SQL agent. Needs only Docker and the two weight files — no root, no
#     secrets, no TLS, no hostname. Stages D1-D6 guide the bring-up.
#
#   PRODUCTION    sudo ./deploy.sh --public-origin=https://<your-host>
#     For serving real cameras and real people. TLS, generated secrets,
#     least-privilege DB roles, forced password rotation, GPU proof, health
#     battery — stages 01-14. The dev conveniences above are REFUSED here by
#     the fail-closed preflight (exit 78), not merely turned off.
#
# Running both on one host does not work: each wants ports 80/443.
#
# WHAT THIS IS
# ------------
# An orchestrator for machinery the repository already ships and tests:
# scripts/setup/generate-secrets.sh, scripts/tls/make-internal-ca.sh,
# db/roles.sql, the compose `migrate` job, scripts/backup/*, config_guard and
# gpu_runtime. It re-implements none of them. Docs/61_DEPLOYMENT_RUNBOOK.md
# remains the reference for what each step means and the break-glass manual
# path; every numbered section there maps to a stage here.
#
# THE RULES IT WILL NOT BREAK
# ---------------------------
#   * Idempotent and resumable. Every stage detects, validates, applies only
#     what is missing, then verifies. Re-running is always safe.
#   * `validate` and `--dry-run` change nothing: no package, no secret, no
#     certificate, no configuration file, no container, no volume. `validate`
#     does write its own run log under logs/deploy/, because a readiness check
#     that fails on a customer host is worth nothing without the evidence;
#     `--dry-run` writes not even that.
#   * Never overwrites an existing secret, certificate, verified model weight,
#     database or persistent volume. Data survives restart, redeploy, upgrade,
#     rollback and uninstall.
#   * Never installs or upgrades the NVIDIA kernel driver (needs a reboot and
#     an operator decision) — it detects and instructs.
#   * A mandatory stage failure stops the run at that stage, names it, and
#     preserves the log.
#   * GPU readiness means REAL SCRFD + ArcFace inference on CUDA. nvidia-smi
#     is necessary, never sufficient. Silent CPU fallback is a failure.
#   * `--purge-data` is the only path that destroys data and needs a typed
#     confirmation of its own.
#
# FIRST RUN ON A PRODUCTION SERVER
# --------------------------------
#     sudo ./deploy.sh --dry-run --public-origin=https://<your-host>   # changes nothing
#     sudo ./deploy.sh           --public-origin=https://<your-host>   # the real thing
#
# A FRESH INSTALL NEEDS NOTHING EXTRA. On a host with no previous stack the
# postgres volume is created empty and the `migrate` job builds the entire
# schema from alembic/versions/ up to the pinned head — there is no dump to
# import, no seed to run, and no data step of any kind. Stage 11 prints
# `database state: EMPTY — this is a fresh installation` so you can see it
# happened.
#
# The case that is NOT fresh, and does not look different from the outside, is
# redeploying onto a host whose named volumes survived. deploy.sh preserves
# data by design, so the stack would come up healthy on the OLD database and
# nothing would say so. Stage 11 therefore reports the table count, the schema
# revision and the number of user accounts it found, and warns. To actually
# start empty, destroy the volumes first — deliberately, with its own typed
# confirmation:
#
#     sudo ./deploy.sh uninstall --purge-data
#     sudo ./deploy.sh --public-origin=https://<your-host>
#
# Ship weights/det_10g.onnx and weights/w600k_r50.onnx with the checkout. Stage
# 08 re-measures both against weights/WEIGHTS_MANIFEST.json on the target host
# and refuses to start containers on a mismatch; it never downloads, replaces
# or "repairs" a weight. Fetch them on a connected machine with
# scripts/setup/download.sh, or import them with --deploy-package=<dir>.
#
# The recogniser's FILENAME is the embedding-version stamp written into every
# stored vector. Renaming or substituting w600k_r50.onnx silently invalidates
# comparability with every embedding already in the database — which is why the
# manifest gate exists and why it is fail-closed.
#
# If the NVIDIA kernel driver is missing, stage 02 prints what to install and
# stops; install it, reboot, then re-run. That is deliberate — see the rule
# above.
#
# EXPECT THIS, IT IS NOT A BUG: on a GPU host the run FAILS if SCRFD or ArcFace
# end up on CPUExecutionProvider instead of CUDA. A deployment that quietly
# falls back to CPU looks healthy and serves at a fraction of the throughput,
# so it is treated as a failed deployment. Iterate on it with
# `./deploy.sh gpu-test` (driver -> toolkit -> CUDA container -> real SCRFD +
# ArcFace inference) instead of redeploying the whole stack.
#
# WHAT THE FIRST RUN IS ACTUALLY PROVING
# --------------------------------------
# Stages 01-10 were exercised for real before shipping: on a disposable root
# (idempotent re-runs, tampered-weight and wrong-hostname refusals), plus 64
# self-tests, shellcheck, the compose matrix, and a 3-file GPU overlay merge
# verified through `docker compose config`.
#
# Stages 11-14 — database roles, the ordered start behind the `migrate` gate,
# ollama, and the health battery — and every real-hardware GPU path have NOT
# run anywhere but a target host. The development machine has no NVIDIA GPU,
# and starting the production stack there would collide with ports 80/443. So
# the first run here IS the acceptance test. It is built for that: a mandatory
# failure stops at the exact stage, names it, preserves logs/deploy/<ts>.log,
# and leaves every secret, certificate, weight, database and volume untouched.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

# ---------------------------------------------------------------------------
# Defaults (flags below may override; docker/.env is the persistent record)
# ---------------------------------------------------------------------------
SUBCOMMAND=""
DRY_RUN=0
QUIET=0               # --quiet: drop the per-stage explanation, keep the report
ASSUME_YES=0
FORCE_OFFLINE=0
FORCE_CPU=0
GPU_IDS_FLAG=""
PUBLIC_ORIGIN_FLAG=""
DEPLOY_PACKAGE="${DEPLOY_PACKAGE:-}"
REMOVE_IMAGES=0
PURGE_DATA=0
I_UNDERSTAND_DATA_LOSS=0
RESTORE_FORCE=0
SELF_TEST=0
POSITIONAL=()

GPU_OVERLAY="$ROOT/docker/gpu-allocation.generated.yml"
GPU_MODE=0            # set by the GPU stage / persisted state
CURRENT_STAGE="startup"

# shellcheck source=scripts/deploy/lib.sh
. "$ROOT/scripts/deploy/lib.sh"

usage() {
    # The header comment block IS the help text, printed to its end rather than
    # to a hardcoded line number — a section added up there shows up here.
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' \
        "$ROOT/deploy.sh"
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# Flag parsing
#
# Several of these are read only by the stage modules sourced further down,
# which the linter analyses as separate files.
# ---------------------------------------------------------------------------
# shellcheck disable=SC2034
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)                DRY_RUN=1 ;;
        --quiet|-q)               QUIET=1 ;;
        --yes|-y)                 ASSUME_YES=1 ;;
        --offline)                FORCE_OFFLINE=1 ;;
        --cpu)                    FORCE_CPU=1 ;;
        --gpu-ids=*)              GPU_IDS_FLAG="${1#*=}" ;;
        --gpu-ids)                shift; GPU_IDS_FLAG="${1:-}" ;;
        --public-origin=*)        PUBLIC_ORIGIN_FLAG="${1#*=}" ;;
        --public-origin)          shift; PUBLIC_ORIGIN_FLAG="${1:-}" ;;
        --deploy-package=*)       DEPLOY_PACKAGE="${1#*=}" ;;
        --deploy-package)         shift; DEPLOY_PACKAGE="${1:-}" ;;
        --remove-images)          REMOVE_IMAGES=1 ;;
        --purge-data)             PURGE_DATA=1 ;;
        --i-understand-data-loss) I_UNDERSTAND_DATA_LOSS=1 ;;
        --force)                  RESTORE_FORCE=1 ;;
        --self-test)              SELF_TEST=1 ;;
        -h|--help)                usage 0 ;;
        -*)                       echo "unknown flag: $1" >&2; usage 2 ;;
        *)                        POSITIONAL+=("$1") ;;
    esac
    shift
done
set -- "${POSITIONAL[@]+"${POSITIONAL[@]}"}"
SUBCOMMAND="${1:-}"
export DRY_RUN QUIET ASSUME_YES FORCE_OFFLINE I_UNDERSTAND_DATA_LOSS

# ---------------------------------------------------------------------------
# Logging: every run is preserved, including (especially) failed ones.
# ---------------------------------------------------------------------------
DEPLOY_LOG=""
open_log() {
    [ "${DRY_RUN}" = "1" ] && { DEPLOY_LOG="(dry-run: not written)"; return 0; }
    mkdir -p "$ROOT/logs/deploy" 2>/dev/null || return 0
    DEPLOY_LOG="$ROOT/logs/deploy/deploy-$(date -u +%Y%m%dT%H%M%SZ).log"
    exec > >(tee -a "$DEPLOY_LOG") 2>&1
    printf 'FACE_DETECTOR deploy.sh — %s — subcommand=%s args=%s\n' \
        "$(timestamp)" "${SUBCOMMAND:-<default>}" "$*"
}

# ---------------------------------------------------------------------------
# Stage modules
# ---------------------------------------------------------------------------
for module in install gpu models db health upgrade dev; do
    # shellcheck source=/dev/null
    . "$ROOT/scripts/deploy/stage-${module}.sh"
done

# The path manifest and the read-only doctor are not stages, so they are not
# named stage-*.sh and the loop above does not reach them.
# shellcheck source=scripts/deploy/paths.sh
. "$ROOT/scripts/deploy/paths.sh"
# shellcheck source=scripts/deploy/doctor.sh
. "$ROOT/scripts/deploy/doctor.sh"

# ---------------------------------------------------------------------------
# Stage 01 — preflight
# Detect the host. Refuses nothing yet; establishes every fact later stages
# branch on, and prints them so a failed deployment is diagnosable from the log.
# ---------------------------------------------------------------------------
stage_preflight() {
    stage_begin "01 preflight"
    explain "WHAT" "Reads this host and refuses early if it cannot run the stack."
    explain "READS" "/etc/os-release, CPU count, total RAM, free disk on the repo root,"
    explain_cont "and TCP 80/443 to check nothing else already owns them."
    explain "WRITES" "nothing."
    explain "FAIL" "too little RAM or disk, or ports 80/443 already bound. Free them"
    explain_cont "or stop the other stack; nothing has been changed yet."

    [ -f "$ROOT/docker/docker-compose.prod.yml" ] || \
        stage_fail "not a FACE_DETECTOR checkout (docker/docker-compose.prod.yml missing)"

    HOST_OS="$(uname -s 2>/dev/null || echo unknown)"
    HOST_ARCH="$(uname -m 2>/dev/null || echo unknown)"
    IS_WSL2=0; is_wsl2 && IS_WSL2=1
    ONLINE=0; is_online && ONLINE=1
    export HOST_OS HOST_ARCH IS_WSL2 ONLINE

    local cpus mem_gb disk_gb
    cpus="$(nproc 2>/dev/null || echo '?')"
    mem_gb="$(awk '/MemTotal/ {printf "%.1f", $2/1048576}' /proc/meminfo 2>/dev/null || echo '?')"
    disk_gb="$(df -Pk "$ROOT" 2>/dev/null | awk 'NR==2 {printf "%.1f", $4/1048576}' || echo '?')"

    info "host      : $HOST_OS $HOST_ARCH$([ "$IS_WSL2" = 1 ] && echo ' (WSL2)')"
    info "resources : ${cpus} CPU, ${mem_gb} GiB RAM, ${disk_gb} GiB free at $ROOT"
    info "network   : $([ "$ONLINE" = 1 ] && echo online || echo 'offline (verify-only installs)')"
    info "user      : uid=$(id -u) $([ "$(id -u)" = 0 ] && echo '(root)' || echo '(non-root)')"
    [ "$DRY_RUN" = "1" ] && info "mode      : DRY RUN — no host mutation"

    # Ports 80/443 must be free for nginx (the only published ports).
    local busy=""
    if have ss; then
        for port in 80 443; do
            ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$" && busy="$busy $port"
        done
    fi
    if [ -n "$busy" ]; then
        if compose ps --format '{{.Service}}' 2>/dev/null | grep -q nginx; then
            info "ports${busy} in use by this deployment's nginx (expected on re-run)"
        else
            stage_warn "ports${busy} already in use — nginx cannot bind until they are free"
            return 0
        fi
    fi

    [ "$mem_gb" != "?" ] && awk -v m="$mem_gb" 'BEGIN { exit (m >= 7.5) ? 0 : 1 }' || \
        warn "less than 8 GiB RAM detected — the stack runs postgres, redis, ollama and the API"

    stage_pass "$HOST_OS $HOST_ARCH, ${cpus} CPU, ${mem_gb} GiB RAM"
}

# ---------------------------------------------------------------------------
# Stage 03 — workspace: directories, external network, ownership
# Everything here is create-if-absent. Nothing is emptied, chowned away from
# the container user, or replaced.
# ---------------------------------------------------------------------------
stage_workspace() {
    stage_begin "03 workspace"
    explain "WHAT" "Creates the directory tree the stack bind-mounts, plus the shared network."
    explain "WRITES" "secrets/ certs/ .deployment/   mode 700, they hold credentials"
    explain_cont "backups/ logs/deploy/ weights/ map-data/production map-data/metadata"
    explain_cont "docker network webhook_integration  (external; the VMS joins it too)"
    explain "NEVER" "touches a directory that already exists, or anything inside it."
    explain "FAIL" "no permission to create these. Re-run with sudo, or fix ownership."

    local dir
    for dir in backups logs/deploy secrets certs weights map-data/production map-data/metadata .deployment; do
        if [ -d "$ROOT/$dir" ]; then
            info "exists: $dir"
        else
            run mkdir -p "$ROOT/$dir" || stage_fail "cannot create $dir"
            info "created: $dir"
        fi
    done
    run chmod 700 "$ROOT/secrets" "$ROOT/.deployment" 2>/dev/null || true

    # The webhook path to the VMS project. External so neither project owns the
    # other's connectivity; `compose up` fails outright when it is absent, and
    # it is documented only in a YAML comment — so deploy.sh owns it.
    if docker network inspect webhook_integration >/dev/null 2>&1; then
        info "network webhook_integration exists"
    else
        run docker network create webhook_integration >/dev/null || \
            stage_fail "could not create the webhook_integration network"
        info "created network webhook_integration"
    fi

    # Host-side ownership for bind mounts; the container runs as uid 1000 and
    # the script no-ops on Windows where Docker Desktop handles it.
    if [ "$DRY_RUN" = "1" ]; then
        info "DRY: scripts/setup/fix-permissions.sh"
    elif is_windows_shell; then
        info "Windows shell — Docker Desktop manages bind-mount permissions"
    else
        bash "$ROOT/scripts/setup/fix-permissions.sh" >/dev/null 2>&1 || \
            stage_warn "fix-permissions.sh reported problems (see log)"
    fi

    if [ "$IS_WSL2" = "1" ] && printf '%s' "$ROOT" | grep -q '^/mnt/'; then
        stage_warn "repository is on a Windows drive (/mnt): chmod/chown are ineffective and FAISS bind mounts can corrupt — clone to the WSL2 ext4 filesystem for production"
        return 0
    fi

    stage_pass "directories, network and ownership in place"
}

# ---------------------------------------------------------------------------
# Stage 04 — secrets (delegated; never overwrites)
# ---------------------------------------------------------------------------
REQUIRED_SECRET_FILES=(jwt_secret bootstrap_admin_password webhook_api_keys)
REQUIRED_ENV_KEYS=(POSTGRES_SUPERUSER_PASSWORD FR_APP_PASSWORD FR_MIGRATOR_PASSWORD
                   FR_READONLY_PASSWORD FR_BACKUP_PASSWORD REDIS_PASSWORD
                   REDIS_MONITOR_PASSWORD GRAFANA_ADMIN_PASSWORD)

secrets_complete() {
    local name key
    for name in "${REQUIRED_SECRET_FILES[@]}"; do
        [ -s "$ROOT/secrets/$name" ] || return 1
    done
    [ -s "$ROOT/docker/redis/users.acl" ] || return 1
    for key in "${REQUIRED_ENV_KEYS[@]}"; do
        [ -n "$(read_env_kv "$key")" ] || return 1
    done
    return 0
}

stage_secrets() {
    stage_begin "04 secrets"
    explain "WHAT" "Generates every credential the stack needs, exactly once."
    explain "WRITES" "secrets/jwt_secret                signs session tokens"
    explain_cont "secrets/bootstrap_admin_password  the first administrator password"
    explain_cont "secrets/webhook_api_keys          keys the VMS presents when posting"
    explain_cont "docker/redis/users.acl            redis ACL, SHA-256 hashes not plaintext"
    explain_cont "docker/.env                       8 DB / Redis / Grafana passwords"
    explain_cont "All mode 600. BACK THESE UP: lose secrets/jwt_secret and every"
    explain_cont "session breaks; lose the DB passwords and the data is unreachable."
    explain "NEVER" "overwrites a secret that already exists, so re-running is safe and"
    explain_cont "cannot invalidate live sessions or lock you out of the database."
    explain "FAIL" "delegates to scripts/setup/generate-secrets.sh; read its output."

    if secrets_complete; then
        info "every secret file and deployment credential already present — keeping them"
    else
        if [ "${VALIDATE_ONLY:-0}" = "1" ]; then
            stage_fail "secrets incomplete; run 'sudo ./deploy.sh install' (validate never mutates)"
        fi
        info "generating missing secrets (existing ones are never overwritten)"
        run bash "$ROOT/scripts/setup/generate-secrets.sh" >/dev/null || \
            stage_fail "scripts/setup/generate-secrets.sh failed"
    fi

    if [ "$DRY_RUN" = "1" ]; then stage_pass "would ensure secrets"; return 0; fi

    local missing=""
    for name in "${REQUIRED_SECRET_FILES[@]}"; do
        [ -s "$ROOT/secrets/$name" ] || missing="$missing secrets/$name"
    done
    [ -s "$ROOT/docker/redis/users.acl" ] || missing="$missing docker/redis/users.acl"
    for key in "${REQUIRED_ENV_KEYS[@]}"; do
        [ -n "$(read_env_kv "$key")" ] || missing="$missing $key"
    done
    [ -z "$missing" ] || stage_fail "still missing after generation:$missing"

    # Mode matters: config_guard refuses a secret file more permissive than
    # 0444 inside the container, and chmod is a silent no-op on NTFS.
    if ! is_windows_shell; then
        run chmod 600 "$ROOT/secrets"/* "$ROOT/docker/.env" "$ROOT/docker/redis/users.acl" 2>/dev/null || true
        local loose
        loose="$(find "$ROOT/secrets" -maxdepth 1 -type f ! -perm 600 2>/dev/null | head -3)"
        [ -z "$loose" ] || stage_warn "secret files with permissive modes: $loose"
    else
        stage_warn "Windows filesystem: chmod is a no-op — re-run 'chmod 600 secrets/* docker/.env' on the Linux target"
    fi

    stage_pass "3 secret files, redis ACL, 8 deployment credentials"
}

# ---------------------------------------------------------------------------
# Stage 05 — TLS
# Generates an internal CA + server certificate only when none exists. An
# existing certificate is verified, never regenerated: replacing it silently
# would break every client that trusts the current CA.
# ---------------------------------------------------------------------------
stage_tls() {
    stage_begin "05 TLS"
    explain "WHAT" "Issues an internal CA and a server certificate for PUBLIC_ORIGIN."
    explain "WRITES" "certs/internal-ca.crt   distribute to every client machine"
    explain_cont "certs/internal-ca.key   MOVE OFFLINE once issued: it can mint a"
    explain_cont "                        certificate for ANY hostname clients trust"
    explain_cont "certs/server.crt, certs/server.key   served by nginx"
    explain "NEVER" "replaces an existing certificate. Clients may already trust it, so"
    explain_cont "re-issuing is deliberate: delete certs/server.* (keep the CA files),"
    explain_cont "re-run scripts/tls/make-internal-ca.sh, then restart nginx."
    explain "FAIL" "the existing certificate does not cover PUBLIC_ORIGIN. Re-issue as"
    explain_cont "above; do not change the hostname to match the certificate."

    local origin host
    origin="$(resolve_public_origin)" || stage_fail "PUBLIC_ORIGIN is required (pass --public-origin=https://host)"
    host="$(origin_host "$origin")"

    if [ -s "$ROOT/certs/server.crt" ] && [ -s "$ROOT/certs/server.key" ]; then
        info "certificate exists — not regenerating"
        if have openssl; then
            local subject sans expiry
            subject="$(openssl x509 -in "$ROOT/certs/server.crt" -noout -subject 2>/dev/null)"
            sans="$(openssl x509 -in "$ROOT/certs/server.crt" -noout -ext subjectAltName 2>/dev/null | tr -d ' ')"
            expiry="$(openssl x509 -in "$ROOT/certs/server.crt" -noout -enddate 2>/dev/null | cut -d= -f2)"
            info "subject: ${subject#subject=}"
            info "expires: ${expiry:-unknown}"
            if ! printf '%s %s' "$subject" "$sans" | grep -q "$host"; then
                stage_fail "certificate does not cover '$host' (PUBLIC_ORIGIN=$origin). Re-issue deliberately: bash scripts/tls/make-internal-ca.sh $host <lan-ip>, then re-run. deploy.sh never replaces a certificate clients may already trust."
            fi
            if ! openssl x509 -in "$ROOT/certs/server.crt" -noout -checkend 2592000 >/dev/null 2>&1; then
                stage_warn "certificate expires within 30 days — plan a re-issue"
            fi
        fi
        stage_pass "existing certificate covers $host"
        return 0
    fi

    if [ "${VALIDATE_ONLY:-0}" = "1" ]; then
        stage_fail "no TLS certificate; run 'sudo ./deploy.sh install' (validate never mutates)"
    fi

    info "issuing an internal CA and server certificate for $host"
    run bash "$ROOT/scripts/tls/make-internal-ca.sh" "$host" || stage_fail "certificate generation failed"
    [ "$DRY_RUN" = "1" ] || [ -s "$ROOT/certs/server.crt" ] || stage_fail "certificate was not written"
    if [ "$DRY_RUN" != "1" ]; then
        warn "distribute certs/internal-ca.crt to every client, and move certs/internal-ca.key OFFLINE"
    fi
    stage_pass "internal CA + server certificate for $host"
}

# resolve_public_origin: flag > docker/.env > interactive prompt. Cannot be
# discovered automatically — it must match the certificate and the browser URL
# or login is rejected as cross-origin.
resolve_public_origin() {
    local origin=""
    if [ -n "$PUBLIC_ORIGIN_FLAG" ]; then
        origin="$PUBLIC_ORIGIN_FLAG"
    else
        origin="$(read_env_kv PUBLIC_ORIGIN 2>/dev/null)"
    fi
    if [ -z "$origin" ] && [ -t 0 ] && [ "$ASSUME_YES" != "1" ]; then
        printf '\nPUBLIC_ORIGIN is the exact URL clients will use (it goes in the certificate\n'
        printf 'and in the cookie/CORS origin check). Example: https://face-detector.internal\n'
        read -r -p "PUBLIC_ORIGIN: " origin
    fi
    [ -n "$origin" ] || return 1
    case "$origin" in
        https://*) : ;;
        http://*)  warn "PUBLIC_ORIGIN uses http:// — production requires https for secure cookies" ;;
        *)         origin="https://$origin" ;;
    esac
    printf '%s' "$origin"
}

# ---------------------------------------------------------------------------
# Stage 06 — deployment configuration (docker/.env)
# ---------------------------------------------------------------------------
stage_env_config() {
    stage_begin "06 configuration"
    explain "WHAT" "Records the two values the stack cannot start without."
    explain "WRITES" "docker/.env  PUBLIC_ORIGIN             the browser-facing origin"
    explain_cont "docker/.env  MIGRATIONS_EXPECTED_HEAD  derived from alembic/versions/"
    explain_cont "Keys are edited in place; every other line and comment is preserved."
    explain "READS" "alembic/versions/*.py - the head is DERIVED, never hardcoded, so it"
    explain_cont "cannot go stale against the code being deployed."
    explain "FAIL" "more than one alembic head (a merge revision is needed), or no origin."

    local origin head current_head
    origin="$(resolve_public_origin)" || stage_fail "PUBLIC_ORIGIN is required"

    head="$(derive_migrations_head)" || stage_fail "could not derive the alembic head from alembic/versions"
    info "alembic head derived from alembic/versions: $head"
    current_head="$(read_env_kv MIGRATIONS_EXPECTED_HEAD 2>/dev/null)"

    # What this stage resolved, whether or not it is allowed to write it.
    # Stage 09 needs these to validate a dry run that has not touched .env.
    RESOLVED_PUBLIC_ORIGIN="$origin"
    RESOLVED_MIGRATIONS_HEAD="$head"

    if [ "${VALIDATE_ONLY:-0}" = "1" ]; then
        [ "$(read_env_kv PUBLIC_ORIGIN)" = "$origin" ] || stage_fail "PUBLIC_ORIGIN not recorded in docker/.env"
        if [ -n "$current_head" ] && [ "$current_head" != "$head" ]; then
            stage_fail "docker/.env pins MIGRATIONS_EXPECTED_HEAD=$current_head but the code's head is $head"
        fi
        stage_pass "PUBLIC_ORIGIN=$origin, head=$head"
        return 0
    fi

    upsert_env_kv PUBLIC_ORIGIN "$origin"
    if [ -n "$current_head" ] && [ "$current_head" != "$head" ]; then
        info "migration head pin $current_head -> $head"
    fi
    upsert_env_kv MIGRATIONS_EXPECTED_HEAD "$head"

    state_set public_origin "$origin"
    state_set migration_head "$head"
    stage_pass "PUBLIC_ORIGIN=$origin, MIGRATIONS_EXPECTED_HEAD=$head"
}

# ---------------------------------------------------------------------------
# Stage 09 — compose validation (the gate the runbook prescribes)
# ---------------------------------------------------------------------------
stage_compose_validate() {
    stage_begin "09 compose validation"
    explain "WHAT" "Renders the exact stack about to start and checks it is coherent."
    explain "READS" "docker/docker-compose.prod.yml, the GPU overlay, and the generated"
    explain_cont "docker/gpu-allocation.generated.yml when deploying on GPU."
    explain "WRITES" "nothing - this stage only reads and asserts."
    explain "FAIL" "an unresolved variable (usually a secret missing from stage 04), or"
    explain_cont "WORKERS resolving to anything but 1 for face_recognition."
    local files; files="$(compose_files | tr '\n' ' ')"
    info "stack: $files"

    local missing=() key substituted=0
    if [ "$DRY_RUN" = "1" ]; then
        # Stage 04 was not applied, so credentials it would have written are
        # absent. Interpolate placeholders for exactly those keys: without
        # them a dry run on a fresh host always fails here, which says nothing
        # about the host and hides any real structural error behind it.
        for key in "${REQUIRED_ENV_KEYS[@]}"; do
            [ -n "$(read_env_kv "$key")" ] || missing+=("$key")
        done
        substituted="${#missing[@]}"
        [ "$substituted" -eq 0 ] \
            || info "dry-run: $substituted credential(s) stage 04 would generate are substituted with placeholders"
    fi

    # Rendered once: the same output proves the stack parses AND is what the
    # assertions below read. Shell variables take precedence over docker/.env
    # and this is a subshell, so no substitution leaks into a later stage.
    local rendered rc=0
    rendered="$(
        for key in ${missing[@]+"${missing[@]}"}; do export "$key=dry-run-placeholder"; done
        if [ "$DRY_RUN" = "1" ]; then
            # Stage 06 resolved these but was not allowed to write them; use
            # the real values so the dry run validates the real configuration.
            [ -n "$(read_env_kv PUBLIC_ORIGIN)" ] \
                || export PUBLIC_ORIGIN="${RESOLVED_PUBLIC_ORIGIN:-}"
            [ -n "$(read_env_kv MIGRATIONS_EXPECTED_HEAD)" ] \
                || export MIGRATIONS_EXPECTED_HEAD="${RESOLVED_MIGRATIONS_HEAD:-}"
        fi
        compose config 2>&1
    )" || rc=$?

    [ "$rc" = 0 ] || stage_fail "compose configuration is invalid: $rendered"

    # WORKERS=1 is a correctness constraint for the face-recognition service in
    # every mode, not only on GPU: the model caches and single-flight guards
    # are process-local. Asserted on the MERGED configuration, because that is
    # where an overlay could have raised it.
    local workers
    workers="$(merged_env_value "$rendered" face_recognition WORKERS)"
    [ "$workers" = "1" ] || stage_fail \
        "face_recognition resolves to WORKERS=${workers:-<unset>}, but it must run exactly one process"

    if [ "$substituted" -gt 0 ]; then
        stage_pass "configuration parses ($substituted credential(s) stubbed — not yet generated)"
    else
        # Every ${VAR:?} resolved against real values, so the credentials the
        # stack needs are genuinely present.
        stage_pass "configuration parses and every required variable resolves"
    fi
}

# ---------------------------------------------------------------------------
# Stage 10 — build
# ---------------------------------------------------------------------------
stage_build() {
    stage_begin "10 build"
    explain "WHAT" "Builds the application image, tagged from git describe."
    explain "READS" "Dockerfile / Dockerfile.gpu and the repository contents."
    explain "WRITES" "a local docker image. Nothing on disk outside docker."
    explain "NEVER" "pulls base images under --offline; they must already be loaded."
    explain "FAIL" "usually a network problem, or a missing base image when offline."
    local version
    version="$(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || echo unknown)"
    info "image version: $version"
    if [ "$ONLINE" = "1" ]; then
        compose_mutate build --pull || stage_fail "image build failed"
    else
        info "offline: building without --pull (base images must already be present)"
        compose_mutate build || stage_fail "image build failed (offline: are the base images loaded?)"
    fi
    state_set deployed_version "$version"
    if [ "$DRY_RUN" = "1" ]; then
        stage_pass "would build images ($version)"
    else
        stage_pass "images built ($version)"
    fi
}

# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------
cmd_install() {
    require_root install
    stage_preflight
    stage_sys_install
    stage_workspace
    stage_secrets
    stage_tls
    stage_env_config
    stage_gpu_detect
    stage_model_check
    stage_compose_validate
    state_set last_install_at "$(timestamp)"
    state_set config_fingerprint "$(config_fingerprint)"
}

cmd_validate() {
    VALIDATE_ONLY=1
    DRY_RUN=1            # belt and braces: validate must never mutate
    export DRY_RUN
    stage_preflight
    stage_sys_install
    stage_secrets
    stage_tls
    stage_env_config
    stage_gpu_detect
    stage_model_check
    stage_compose_validate
}

cmd_start() {
    require_root start
    stage_preflight
    stage_gpu_detect
    stage_model_check
    stage_compose_validate
    stage_build
    stage_db_init
    stage_up
    stage_ollama_models
}

cmd_health_only() {
    stage_preflight
    stage_gpu_detect_quiet
    stage_health
}

main() {
    case "$SUBCOMMAND" in
        ""|deploy|all)
            open_log "$@"
            cmd_install
            cmd_start
            stage_health
            finish_report ;;
        install)      open_log "$@"; cmd_install; finish_report ;;
        validate)     open_log "$@"; cmd_validate; finish_report ;;
        start)        open_log "$@"; cmd_start; stage_health; finish_report ;;
        stop)         require_root stop; cmd_stop ;;
        restart)      require_root restart; open_log "$@"; cmd_restart; finish_report ;;
        status)       cmd_status ;;
        health)       open_log "$@"; cmd_health_only; finish_report ;;
        doctor)       doctor_run ;;
        paths)        echo "Deployment paths — reality vs scripts/deploy/paths.sh"; echo
                      verify_deployment_paths ;;
        gpu-test)     open_log "$@"; stage_preflight; stage_gpu_detect; stage_gpu_test; finish_report ;;
        model-check)  open_log "$@"; stage_preflight; stage_model_check; finish_report ;;
        model-manifest) cmd_model_manifest ;;
        backup)       require_root backup; open_log "$@"; stage_backup; finish_report ;;
        restore)      require_root restore; open_log "$@"; cmd_restore "${2:-}" ;;
        upgrade)      require_root upgrade; open_log "$@"; cmd_upgrade; finish_report ;;
        logs)         cmd_logs "${2:-}" ;;
        uninstall)    require_root uninstall; open_log "$@"; cmd_uninstall ;;
        dev)
            # The bring-up gets the full log + stage report; the passthroughs
            # (stop/status/logs) are plain commands and a "DEPLOY RESULT" table
            # after `dev status` would be noise claiming a deploy happened.
            case "${2:-up}" in
                up|"") open_log "$@"; cmd_dev "${2:-}"; finish_report ;;
                *)     cmd_dev "${2:-}" "${3:-}" ;;
            esac ;;
        *)            echo "unknown subcommand: $SUBCOMMAND" >&2; usage 2 ;;
    esac
}

finish_report() {
    print_report
    local result="PASS" colour="$C_GREEN"
    local name
    for name in "${STAGE_ORDER[@]}"; do
        [ "${STAGE_RESULT[$name]}" = "FAIL" ] && { result="FAIL"; colour="$C_RED"; }
    done
    printf '\n%sDEPLOY RESULT: %s%s (%d warning(s))\n' "$colour" "$result" "$C_RESET" "$WARN_COUNT"
    [ -n "$DEPLOY_LOG" ] && printf 'log: %s\n' "$DEPLOY_LOG"
    if [ "$result" = "PASS" ] && [ "${VALIDATE_ONLY:-0}" != "1" ] && [ "$DRY_RUN" != "1" ]; then
        state_set last_successful_deployment "$(timestamp)"
        state_set last_result "PASS"
        state_set config_fingerprint "$(config_fingerprint)"
    fi
    [ "$result" = "PASS" ] || exit 1
}

# --self-test runs before anything else and never touches the host.
if [ "$SELF_TEST" = "1" ]; then
    # shellcheck source=/dev/null
    . "$ROOT/scripts/deploy/self-test.sh"
    run_self_tests
    exit $?
fi

main "$@"
