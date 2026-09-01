#!/usr/bin/env bash
#
# deploy.sh shared library — logging, stage bookkeeping, compose wrapper,
# docker/.env editing, alembic head derivation, deployment state.
#
# Sourced by deploy.sh and every scripts/deploy/stage-*.sh module. Nothing
# here executes on its own.
#
# Two rules everything else depends on:
#
#   1. Mutation goes through run() or is skipped in --dry-run / validate.
#      Probes (reads) always execute, so a dry run still tells the truth.
#   2. Nothing overwrites an existing secret, certificate, verified weight,
#      database or volume. Those are created-if-absent, verified otherwise.

# ---------------------------------------------------------------------------
# Colours (only when stdout is a terminal)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
else
    C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_DIM=""; C_BOLD=""
fi

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log()   { printf '%s[%s]%s %s\n' "$C_DIM" "$(date -u +%H:%M:%S)" "$C_RESET" "$*"; }
info()  { printf '%s  ->%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
ok()    { printf '%s  OK%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn()  { printf '%sWARN%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; WARN_COUNT=$((WARN_COUNT + 1)); }
fail()  { printf '%sFAIL%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
die()   { fail "$*"; exit 1; }

# ---------------------------------------------------------------------------
# explain(): the step-by-step narration under each stage heading.
#
# A deployment is mostly opaque — it either works or it prints an error — and
# the operator is left guessing which file on their disk just changed. Each
# stage therefore says, before it does anything:
#
#   WHAT    one sentence on the job
#   READS   inputs it needs to already exist
#   WRITES  every path it creates or modifies, with permissions where they
#           matter. Anything a backup must include is named here.
#   NEVER   the destructive thing it deliberately will not do
#   FAIL    what a failure at this stage means and how to fix it
#
# Call it as: explain "WHAT" "..." "WRITES" "..." — label/text pairs, so a
# stage only prints the rows it actually has.
#
# Suppressed by --quiet for scripted/CI runs, where the report table is the
# interesting part. The log file always gets it.
# ---------------------------------------------------------------------------
explain() {
    [ "${QUIET:-0}" = "1" ] && return 0
    local label text
    while [ "$#" -ge 2 ]; do
        label="$1"; text="$2"; shift 2
        # Blank-line-separated paragraphs let a WRITES list break cleanly
        # without this function needing to know about widths.
        printf '%s   %-7s%s %s\n' "$C_DIM" "$label" "$C_RESET" "$text"
    done
}

# explain_cont(): a continuation line, aligned under the text of the row above.
# Used for multi-path WRITES lists so each artifact gets its own line.
explain_cont() {
    [ "${QUIET:-0}" = "1" ] && return 0
    printf '%s           %s%s\n' "$C_DIM" "$*" "$C_RESET"
}

# ---------------------------------------------------------------------------
# run(): the ONLY way a mutating command is executed.
# In --dry-run it prints what would happen and returns success, so later
# probe-only stages still run and the plan is complete.
# ---------------------------------------------------------------------------
run() {
    if [ "${DRY_RUN:-0}" = "1" ]; then
        printf '%s  DRY%s %s\n' "$C_YELLOW" "$C_RESET" "$*"
        return 0
    fi
    "$@"
}

# run_quiet(): same contract, output suppressed unless it fails.
run_quiet() {
    if [ "${DRY_RUN:-0}" = "1" ]; then
        printf '%s  DRY%s %s\n' "$C_YELLOW" "$C_RESET" "$*"
        return 0
    fi
    local output status
    output="$("$@" 2>&1)"; status=$?
    [ $status -eq 0 ] || printf '%s\n' "$output" >&2
    return $status
}

# ---------------------------------------------------------------------------
# Stage bookkeeping
#
# Every stage records exactly one result. MANDATORY failures stop the run at
# the stage that failed (the report then names it precisely); OPTIONAL ones
# degrade to a warning and the run continues.
# ---------------------------------------------------------------------------
STAGE_ORDER=()
declare -A STAGE_RESULT=()
declare -A STAGE_DETAIL=()
WARN_COUNT=0

stage_begin() {
    CURRENT_STAGE="$1"
    printf '\n%s== %s%s\n' "$C_BOLD" "$1" "$C_RESET"
    # A stage can run twice in one invocation — the default flow is install
    # then start, and start re-checks GPU, models and compose. The report must
    # show each stage once, carrying its latest result, or the operator reads a
    # table where some stages appear twice and others do not.
    local seen name
    seen=0
    for name in ${STAGE_ORDER[@]+"${STAGE_ORDER[@]}"}; do
        [ "$name" = "$1" ] && { seen=1; break; }
    done
    [ "$seen" = 1 ] || STAGE_ORDER+=("$1")
    STAGE_RESULT["$1"]="RUNNING"
    STAGE_DETAIL["$1"]=""
}

stage_pass() { STAGE_RESULT["$CURRENT_STAGE"]="PASS"; STAGE_DETAIL["$CURRENT_STAGE"]="${1:-}"; ok "${1:-done}"; }
stage_skip() { STAGE_RESULT["$CURRENT_STAGE"]="SKIP"; STAGE_DETAIL["$CURRENT_STAGE"]="${1:-}"; info "skipped: ${1:-}"; }
stage_warn() { STAGE_RESULT["$CURRENT_STAGE"]="WARN"; STAGE_DETAIL["$CURRENT_STAGE"]="${1:-}"; warn "${1:-}"; }

# stage_fail: mandatory failure. Records, names the stage, and aborts the run —
# later stages are never attempted on a broken foundation.
stage_fail() {
    STAGE_RESULT["$CURRENT_STAGE"]="FAIL"
    STAGE_DETAIL["$CURRENT_STAGE"]="${1:-}"
    fail "${1:-stage failed}"
    print_report
    printf '\n%sDEPLOY RESULT: FAIL%s at stage %s%s%s\n' "$C_RED" "$C_RESET" "$C_BOLD" "$CURRENT_STAGE" "$C_RESET"
    printf 'log preserved: %s\n' "${DEPLOY_LOG:-<none>}"
    state_set last_result "FAIL"
    state_set last_failed_stage "$CURRENT_STAGE"
    state_set last_failed_at "$(timestamp)"
    state_set last_failed_detail "${1:-}"
    exit 1
}

print_report() {
    local name result detail
    printf '\n%s%-34s %-6s %s%s\n' "$C_BOLD" "STAGE" "RESULT" "DETAIL" "$C_RESET"
    printf '%s\n' "----------------------------------------------------------------------------"
    for name in "${STAGE_ORDER[@]}"; do
        result="${STAGE_RESULT[$name]}"
        detail="${STAGE_DETAIL[$name]}"
        case "$result" in
            PASS) printf '%-34s %s%-6s%s %s\n' "$name" "$C_GREEN" "$result" "$C_RESET" "$detail" ;;
            FAIL) printf '%-34s %s%-6s%s %s\n' "$name" "$C_RED" "$result" "$C_RESET" "$detail" ;;
            WARN) printf '%-34s %s%-6s%s %s\n' "$name" "$C_YELLOW" "$result" "$C_RESET" "$detail" ;;
            *)    printf '%-34s %s%-6s%s %s\n' "$name" "$C_DIM" "$result" "$C_RESET" "$detail" ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------
timestamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

is_wsl2() { grep -qiE "microsoft|wsl" /proc/version 2>/dev/null; }

is_windows_shell() {
    case "${OSTYPE:-}" in msys*|win32*|cygwin*) return 0 ;; esac
    return 1
}

# is_online: one bounded probe. Never blocks a deployment for long — an
# offline host is a supported deployment target, not an error.
is_online() {
    [ "${FORCE_OFFLINE:-0}" = "1" ] && return 1
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 4 -o /dev/null https://registry-1.docker.io/v2/ 2>/dev/null && return 0
        curl -fsS --max-time 4 -o /dev/null https://deb.debian.org 2>/dev/null && return 0
    fi
    return 1
}

# require_root: root is needed for what root is actually for — installing host
# packages, creating directories the container user must own, and writing
# /etc-level configuration. Where none of that applies (Docker Desktop on
# Windows/WSL2, which has no root and manages bind-mount permissions itself)
# the real requirement is simply that docker answers for this user.
require_root() {
    [ "$(id -u)" = "0" ] && return 0
    if is_windows_shell || is_wsl2; then
        docker info >/dev/null 2>&1 && {
            info "running without root (Docker Desktop host: no package installs or chown are performed here)"
            return 0
        }
        die "$1 needs a working Docker: start Docker Desktop (enable WSL integration if applicable) and re-run"
    fi
    die "$1 needs root: re-run as 'sudo ./deploy.sh $SUBCOMMAND'"
}

# confirm: interactive yes/no. --yes answers yes; a non-interactive shell
# without --yes answers NO (never assume consent for a mutation).
confirm() {
    local prompt="$1"
    [ "${ASSUME_YES:-0}" = "1" ] && { info "$prompt -> yes (--yes)"; return 0; }
    [ -t 0 ] || { warn "$prompt -> no (non-interactive, --yes not given)"; return 1; }
    local reply
    read -r -p "$prompt [y/N] " reply
    case "$reply" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

# confirm_phrase: for destructive operations. The operator must type the exact
# phrase; --yes alone is deliberately NOT enough.
confirm_phrase() {
    local phrase="$1" prompt="$2" reply
    if [ ! -t 0 ]; then
        [ "${I_UNDERSTAND_DATA_LOSS:-0}" = "1" ] || return 1
        warn "non-interactive destructive confirmation accepted via --i-understand-data-loss"
        return 0
    fi
    printf '%s\n' "$prompt"
    read -r -p "Type exactly '$phrase' to proceed: " reply
    [ "$reply" = "$phrase" ]
}

# ---------------------------------------------------------------------------
# docker compose wrapper
#
# --project-directory docker is load-bearing: compose reads `.env` from the
# PROJECT directory, and every deployment credential lives in docker/.env.
# COMPOSE_PROJECT_NAME is never set — the `name:` key in the compose file owns
# the project namespace, and overriding it re-introduces the bug where prod
# mounted the development database.
# ---------------------------------------------------------------------------
compose_files() {
    local -a files=(-f "$ROOT/docker/docker-compose.prod.yml")
    if [ "${GPU_MODE:-0}" = "1" ]; then
        files+=(-f "$ROOT/docker/docker-compose.prod.gpu.yml")
        [ -f "$GPU_OVERLAY" ] && files+=(-f "$GPU_OVERLAY")
    fi
    printf '%s\n' "${files[@]}"
}

compose() {
    local -a files=()
    while IFS= read -r line; do files+=("$line"); done < <(compose_files)
    docker compose --project-directory "$ROOT/docker" "${files[@]}" "$@"
}

# compose_mutate: compose calls that change container state go through run().
compose_mutate() {
    local -a files=()
    while IFS= read -r line; do files+=("$line"); done < <(compose_files)
    run docker compose --project-directory "$ROOT/docker" "${files[@]}" "$@"
}

# merged_env_value <rendered-config> <service> <KEY>
#
# One environment value as the stack will actually see it, read from `docker
# compose config` output — i.e. after every overlay has been merged, which is
# the only place an override can be caught before a container starts.
#
# That output is canonical YAML: services at indent 2, their environment keys
# at indent 6, values quoted. The key is matched whole: face_recognition also
# defines INFERENCE_WORKERS and QUEUE_WORKERS, and a substring match would
# happily return one of those instead of WORKERS.
merged_env_value() {
    printf '%s\n' "$1" | awk -v svc="$2" -v key="$3" '
        $0 == "  " svc ":"          { inside = 1; next }
        inside && /^  [^ ]/         { inside = 0 }
        inside && index($0, "      " key ": ") == 1 {
            value = substr($0, length("      " key ": ") + 1)
            gsub(/^"|"$/, "", value)
            print value
            exit
        }'
}

# ---------------------------------------------------------------------------
# docker/.env editing (atomic, comment-preserving, never clobbers other keys)
# ---------------------------------------------------------------------------
ENV_FILE_REL="docker/.env"

read_env_kv() {
    local key="$1" file="${2:-$ROOT/$ENV_FILE_REL}"
    [ -f "$file" ] || return 1
    sed -n "s/^[[:space:]]*${key}=//p" "$file" | tail -n 1
}

# upsert_env_kv: set KEY=VALUE, replacing an existing uncommented assignment in
# place (so ordering and comments survive) or appending under a managed
# section. Atomic: temp file + mv, mode preserved at 600.
upsert_env_kv() {
    local key="$1" value="$2" file="$ROOT/$ENV_FILE_REL"
    if [ "${DRY_RUN:-0}" = "1" ]; then
        printf '%s  DRY%s set %s=%s in %s\n' "$C_YELLOW" "$C_RESET" "$key" "$value" "$ENV_FILE_REL"
        return 0
    fi
    [ -f "$file" ] || { : > "$file"; chmod 600 "$file" 2>/dev/null || true; }
    local tmp; tmp="$(mktemp "${file}.XXXXXX")"
    if grep -qE "^[[:space:]]*${key}=" "$file"; then
        awk -v k="$key" -v v="$value" '
            $0 ~ "^[[:space:]]*" k "=" && !done { print k "=" v; done = 1; next }
            { print }
        ' "$file" > "$tmp"
    else
        cat "$file" > "$tmp"
        if ! grep -q "^# --- managed by deploy.sh ---$" "$tmp"; then
            printf '\n# --- managed by deploy.sh ---\n' >> "$tmp"
        fi
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    chmod 600 "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$file"
}

# ---------------------------------------------------------------------------
# Alembic head derivation
#
# The head is the one revision that no other revision names as its
# down_revision. Pure sed/grep: works on a host with no virtualenv, no alembic
# and no python. down_revision may be a tuple (merge revisions), so every
# quoted token on the line counts.
#
# This value is only a PIN: the migrate job re-verifies it inside the image
# (backend/utils/migrations.py, fail-closed), so a derivation bug can make a
# deployment stop loudly but can never let a wrong schema through.
# ---------------------------------------------------------------------------
derive_migrations_head() {
    local dir="${1:-$ROOT/alembic/versions}"
    [ -d "$dir" ] || { echo "alembic versions directory not found: $dir" >&2; return 1; }

    local revisions downs r heads=""
    revisions="$(sed -n "s/^revision[^=]*=[[:space:]]*['\"]\([A-Za-z0-9_]*\)['\"].*/\1/p" "$dir"/*.py 2>/dev/null)"
    downs="$(grep -h "^down_revision" "$dir"/*.py 2>/dev/null | grep -o "['\"][A-Za-z0-9_]\{6,\}['\"]" | tr -d "\"'")"

    for r in $revisions; do
        printf '%s\n' "$downs" | grep -qx "$r" || heads="$heads $r"
    done
    # shellcheck disable=SC2086
    set -- $heads
    if [ "$#" -ne 1 ]; then
        echo "expected exactly one alembic head, found $#: $*" >&2
        return 1
    fi
    printf '%s' "$1"
}

# ---------------------------------------------------------------------------
# Deployment state — .deployment/state.json
#
# Flat JSON so it is readable by humans, greppable without jq, and safe to
# hand-edit. Recreated key-by-key; never truncated on failure.
# ---------------------------------------------------------------------------
state_file() { printf '%s' "$ROOT/.deployment/state.json"; }

state_init() {
    local file; file="$(state_file)"
    [ "${DRY_RUN:-0}" = "1" ] && return 0
    mkdir -p "$(dirname "$file")"
    [ -f "$file" ] || printf '{\n}\n' > "$file"
    chmod 600 "$file" 2>/dev/null || true
}

state_get() {
    local key="$1" file; file="$(state_file)"
    [ -f "$file" ] || return 1
    sed -n "s/^[[:space:]]*\"${key}\"[[:space:]]*:[[:space:]]*\"\(.*\)\",\{0,1\}$/\1/p" "$file" | tail -n 1
}

state_set() {
    local key="$1" value="$2" file; file="$(state_file)"
    if [ "${DRY_RUN:-0}" = "1" ]; then
        printf '%s  DRY%s state %s=%s\n' "$C_YELLOW" "$C_RESET" "$key" "$value"
        return 0
    fi
    state_init
    # Escape for JSON. Newlines matter twice over: an unescaped one makes
    # the file invalid JSON, and it also breaks the line-based reader below,
    # which would silently drop every key after a multi-line value (a failure
    # detail is routinely multi-line).
    value="${value//\\/\\\\}"; value="${value//\"/\\\"}"
    value="${value//$'\r'/}"
    value="${value//$'\t'/\\t}"
    value="${value//$'\n'/\\n}"
    local tmp; tmp="$(mktemp "${file}.XXXXXX")"
    {
        printf '{\n'
        # existing keys except the one being replaced, values re-emitted verbatim
        sed -n 's/^[[:space:]]*"\([A-Za-z0-9_]*\)"[[:space:]]*:[[:space:]]*"\(.*\)",\{0,1\}$/\1\t\2/p' "$file" \
            | while IFS="$(printf '\t')" read -r k v; do
                  [ "$k" = "$key" ] && continue
                  printf '  "%s": "%s",\n' "$k" "$v"
              done
        printf '  "%s": "%s"\n' "$key" "$value"
        printf '}\n'
    } > "$tmp"
    chmod 600 "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$file"
}

state_show() {
    local file; file="$(state_file)"
    [ -f "$file" ] || { echo "(no deployment state recorded yet)"; return 0; }
    cat "$file"
}

# config_fingerprint: what "the configuration this deployment is running" means.
# Compose files + generated GPU overlay + the env KEYS (never their values) +
# the weights manifest. Changing any of them changes the fingerprint, which is
# how `status` can say the running stack no longer matches the files on disk.
config_fingerprint() {
    {
        for f in "$ROOT/docker/docker-compose.prod.yml" \
                 "$ROOT/docker/docker-compose.prod.gpu.yml" \
                 "$GPU_OVERLAY" \
                 "$ROOT/weights/WEIGHTS_MANIFEST.json"; do
            [ -f "$f" ] && sha256_of "$f"
        done
        # keys only: values are secrets and must never reach a fingerprint that
        # gets printed or stored in plain text
        [ -f "$ROOT/$ENV_FILE_REL" ] && grep -oE '^[[:space:]]*[A-Za-z0-9_]+=' "$ROOT/$ENV_FILE_REL" | sort
    } 2>/dev/null | sha256_stdin | cut -c1-16
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    else shasum -a 256 "$1" | awk '{print $1}'; fi
}

sha256_stdin() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum | awk '{print $1}'
    else shasum -a 256 | awk '{print $1}'; fi
}

# ---------------------------------------------------------------------------
# Small helpers used across stages
# ---------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

# wait_for: poll a command until it succeeds or the budget expires.
wait_for() {
    local desc="$1" budget="$2"; shift 2
    local waited=0
    while [ "$waited" -lt "$budget" ]; do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 3; waited=$((waited + 3))
        [ $((waited % 30)) -eq 0 ] && info "waiting for $desc (${waited}s/${budget}s)"
    done
    return 1
}

# origin_host: strip scheme and any port from PUBLIC_ORIGIN for cert/CN work.
origin_host() {
    local origin="${1:-}"
    origin="${origin#http://}"; origin="${origin#https://}"
    origin="${origin%%/*}"; origin="${origin%%:*}"
    printf '%s' "$origin"
}
