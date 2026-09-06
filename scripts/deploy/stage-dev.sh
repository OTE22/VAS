#!/usr/bin/env bash
#
# Guided DEVELOPMENT deployment — `./deploy.sh dev [stop|status|logs]`.
#
# The development stack (docker/docker-compose.cpu.yml, project
# face_detector_dev) is deliberately a different animal from production:
# plain HTTP, seeded admin credentials, /docs enabled, password rotation off,
# permissive config — the fail-closed preflight is inert outside production.
# So most of the production stages simply do not apply: no secrets, no TLS,
# no database roles, no GPU overlay, no migrate gate (the API runs
# `alembic upgrade head` itself at startup in this stack).
#
# What a developer DOES need, and what this guides them through:
#   D1  docker + compose present (no root, Docker Desktop is fine)
#   D2  the two model weights present and matching the manifest
#   D3  the compose file renders
#   D4  whether the optional NVIDIA NIM dev LLM is on, and how to turn it on
#   D4b the self-hosted Opik tracer, started only when enabled (dev only)
#   D5  start the stack
#   D6  wait until it actually answers, then print where to log in
#
# Uses its own compose wrapper: compose()/compose_mutate() in lib.sh are
# hard-wired to the PRODUCTION files on purpose, and reusing them here would
# be exactly the dev/prod volume mix-up the project names exist to prevent.

DEV_COMPOSE_FILE="$ROOT/docker/docker-compose.cpu.yml"

dev_compose() {
    docker compose --project-directory "$ROOT/docker" -f "$DEV_COMPOSE_FILE" "$@"
}

dev_compose_mutate() {
    run docker compose --project-directory "$ROOT/docker" -f "$DEV_COMPOSE_FILE" "$@"
}

cmd_dev_up() {
    printf '%sDEVELOPMENT deployment%s (project face_detector_dev)\n' "$C_BOLD" "$C_RESET"
    printf 'Production is a different stack and a different command: sudo ./deploy.sh\n'

    # ---- D1 prerequisites -------------------------------------------------
    stage_begin "D1 prerequisites"
    explain "WHAT" "Checks docker and the compose v2 plugin. Nothing else is needed:"
    explain_cont "no root, no secrets, no TLS - the dev stack ships seeded"
    explain_cont "credentials (admin/admin123, rotation off) and speaks plain HTTP."
    explain "WRITES" "nothing."
    docker info >/dev/null 2>&1 \
        || stage_fail "Docker is not running. Start Docker Desktop (or the docker service) and re-run."
    docker compose version >/dev/null 2>&1 \
        || stage_fail "the docker compose v2 plugin is missing"
    stage_pass "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null), compose $(docker compose version --short 2>/dev/null)"

    # ---- D2 model weights -------------------------------------------------
    stage_begin "D2 model weights"
    explain "WHAT" "Verifies the two ONNX weights the dev stack bind-mounts read-only."
    explain "READS" "weights/det_10g.onnx, weights/w600k_r50.onnx,"
    explain_cont "weights/WEIGHTS_MANIFEST.json (the sha256 each must match)"
    explain "NEVER" "downloads or replaces a weight. Fetch on a connected machine"
    explain_cont "with: bash scripts/setup/download.sh"
    # The same fail-closed verification production stage 08 runs — shared,
    # not copied, so the two flows cannot drift.
    verify_weights_core

    # ---- D3 compose validation --------------------------------------------
    stage_begin "D3 compose validation"
    explain "WHAT" "Renders docker/docker-compose.cpu.yml and checks it is coherent."
    explain "WRITES" "nothing."
    local output
    if ! output="$(dev_compose config -q 2>&1)"; then
        stage_fail "the development compose file does not render: $output"
    fi
    stage_pass "docker-compose.cpu.yml renders"

    # ---- D4 optional dev LLM (NVIDIA NIM) ----------------------------------
    stage_begin "D4 SQL-agent LLM"
    explain "WHAT" "Reports which LLM will serve the SQL agent. Informational only."
    explain "READS" "docker/.env  (LLM_DEV_PROVIDER, NVIDIA_NIM_API_KEY)"
    local dev_provider nim_key
    dev_provider="$(read_env_kv LLM_DEV_PROVIDER 2>/dev/null)"
    nim_key="$(read_env_kv NVIDIA_NIM_API_KEY 2>/dev/null)"
    if [ "$dev_provider" = "nim" ] && [ -n "$nim_key" ]; then
        stage_pass "NVIDIA NIM enabled (hosted, dev-only) with Ollama as fallback"
    else
        info "local Ollama only. To test SQL generation against the free hosted"
        info "NIM endpoint, add BOTH lines to docker/.env and re-run:"
        info "    LLM_DEV_PROVIDER=nim"
        info "    NVIDIA_NIM_API_KEY=nvapi-...   (free key: build.nvidia.com)"
        info "Production refuses that configuration at boot - it is dev-only."
        stage_pass "local Ollama (default)"
    fi

    # ---- D4b Opik tracing (DEVELOPMENT ONLY) ------------------------------
    # The tracer records prompts, SQL and rows: a development convenience the
    # production path never starts (deploy.sh stage 06b refuses it).
    stage_begin "D4b Opik tracing (dev only)"
    explain "WHAT" "Starts the self-hosted Opik stack when tracing is enabled."
    explain "READS" "docker/.env (SQL_AGENT_OPIK_ENABLED), \$OPIK_HOME/docker-compose.yaml"
    explain_cont "(OPIK_HOME defaults to \$HOME/opik: a checkout of comet-ml/opik)"
    explain "NEVER" "runs in production; never enables tracing by itself."
    local opik_enabled opik_home
    opik_enabled="$(read_env_kv SQL_AGENT_OPIK_ENABLED 2>/dev/null | tr '[:upper:]' '[:lower:]')"
    opik_home="${OPIK_HOME:-$HOME/opik}"
    case "$opik_enabled" in
        1|true|yes|on)
            if [ -f "$opik_home/docker-compose.yaml" ]; then
                # Always name the file: Opik's checkout ships a second compose
                # file whose Redis port collides with the dev stack's.
                if run docker compose -f "$opik_home/docker-compose.yaml" up -d; then
                    stage_pass "Opik starting from $opik_home (UI: http://localhost:5173); traces go to project $(read_env_kv OPIK_PROJECT_NAME 2>/dev/null || echo face-detector-sql-agent)"
                else
                    stage_warn "SQL_AGENT_OPIK_ENABLED=true but the Opik stack at $opik_home did not start - the API runs untraced"
                fi
            else
                stage_warn "SQL_AGENT_OPIK_ENABLED=true but no Opik checkout at $opik_home (set OPIK_HOME or git clone https://github.com/comet-ml/opik) - the API runs untraced"
            fi ;;
        *)
            info "tracing off. To trace every SQL-agent turn in development add to docker/.env:"
            info "    SQL_AGENT_OPIK_ENABLED=true"
            info "    OPIK_URL_OVERRIDE=http://host.docker.internal:5173/api/"
            info "and keep a self-hosted Opik checkout at \$HOME/opik (or set OPIK_HOME)."
            stage_pass "Opik not requested" ;;
    esac

    # ---- D5 start ----------------------------------------------------------
    stage_begin "D5 start"
    explain "WHAT" "Starts the whole dev stack in dependency order."
    explain "WRITES" "containers + the face_detector_dev named volumes (postgres,"
    explain_cont "redis, storage, chroma, ollama models). Re-running is safe:"
    explain_cont "existing volumes and their data are reused, never recreated."
    explain "FAIL" "ports 80/443/5432/6379 already taken - usually the production"
    explain_cont "stack or another postgres on this host. Stop it or free the port."
    if ! dev_compose_mutate up -d; then
        # A port bind is far and away the usual cause; name the exact
        # container holding it so the operator does not have to dig.
        local holder port
        for port in 80 443 5432 6379 3000; do
            holder="$(docker ps --filter "publish=$port" --format '{{.Names}}' 2>/dev/null | grep -v '^face_recognition' | head -1)"
            [ -n "$holder" ] && fail "port $port is held by container '$holder' (not part of this stack)"
        done
        stage_fail "could not start the development stack. If a port above is the cause: stop that container (docker stop <name>) or its stack, then re-run './deploy.sh dev'."
    fi
    if [ "$DRY_RUN" = "1" ]; then stage_pass "would start the stack"; else stage_pass "stack starting"; fi

    # ---- D6 readiness ------------------------------------------------------
    stage_begin "D6 readiness"
    explain "WHAT" "Waits until the API answers through nginx, then prints where"
    explain_cont "to log in. First boot is the slow one: migrations run, models"
    explain_cont "load, and ollama may still be pulling its LLMs in the background."
    if [ "$DRY_RUN" = "1" ]; then
        stage_pass "would wait for http://localhost/health/detailed"
    else
        wait_for "the API to answer via nginx" 300 \
            curl -fsS -o /dev/null http://localhost/health/detailed \
            || stage_fail "the API did not answer within 300s - read: docker compose -f docker/docker-compose.cpu.yml logs face_recognition"

        # Answering is not the same as healthy: the endpoint returns 200 with
        # "healthy": false when a component is degraded (a nearly full host
        # disk being the classic). Read the verdict, not just the status code.
        # Parsed inside the API container - the host may have no python.
        local unhealthy
        unhealthy="$(dev_compose exec -T face_recognition python -c "
import json, urllib.request
d = json.load(urllib.request.urlopen('http://localhost:8000/health/detailed', timeout=10))
bad = [n for n, c in d.get('components', {}).items()
       if isinstance(c, dict) and c.get('healthy') is False]
print('OK' if d.get('healthy') else ','.join(bad) or 'unknown')
" 2>/dev/null | tr -d '\r')"
        if [ "$unhealthy" = "OK" ]; then
            stage_pass "http://localhost/health/detailed answers, every component healthy"
        elif [ -n "$unhealthy" ]; then
            stage_warn "the stack answers but reports unhealthy component(s): $unhealthy - see http://localhost/health/detailed (a nearly full HOST disk shows up here as 'storage')"
        else
            # The probe itself failed; the 200 above still stands.
            stage_pass "http://localhost/health/detailed answers"
        fi

        printf '\n%sDevelopment stack is up.%s\n' "$C_GREEN" "$C_RESET"
        printf '  sign in     http://localhost/signin   (admin / admin123 - dev stack only,\n'
        printf '              password rotation is deliberately off here)\n'
        printf '  API docs    http://localhost/docs   (enabled in dev, absent in prod)\n'
        printf '  health      http://localhost/health/detailed\n'
        printf '  grafana     http://localhost:3000\n'
        printf '  logs        ./deploy.sh dev logs [service]\n'
        printf '  stop        ./deploy.sh dev stop      (containers only; data survives)\n'
        printf '  tests       must use the ISOLATED stack: bash scripts/run_regression_isolated.sh\n'
    fi
}

cmd_dev_stop() {
    info "stopping the development stack (containers only - every volume and its data survives)"
    dev_compose_mutate stop || die "stop failed"
    local opik_home="${OPIK_HOME:-$HOME/opik}"
    if [ -f "$opik_home/docker-compose.yaml" ] && docker compose -f "$opik_home/docker-compose.yaml" ps -q 2>/dev/null | grep -q .; then
        info "stopping the development Opik stack at $opik_home"
        run docker compose -f "$opik_home/docker-compose.yaml" stop || warn "Opik stop failed"
    fi
    ok "stopped. Start again with: ./deploy.sh dev"
}

cmd_dev_status() {
    printf '%sDEVELOPMENT stack%s (project face_detector_dev)\n\n' "$C_BOLD" "$C_RESET"
    dev_compose ps
}

cmd_dev_logs() {
    dev_compose logs --tail=100 -f ${1:+"$1"}
}

cmd_dev() {
    case "${1:-up}" in
        up|"")   cmd_dev_up ;;
        stop)    cmd_dev_stop ;;
        status)  cmd_dev_status ;;
        logs)    cmd_dev_logs "${2:-}" ;;
        *)       die "unknown dev subcommand: $1 (use: dev [stop|status|logs])" ;;
    esac
}
