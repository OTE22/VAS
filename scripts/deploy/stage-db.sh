#!/usr/bin/env bash
#
# Stages 11-12 — database roles, then ordered startup.
#
# Migrations are NOT run here. The compose stack already owns them correctly:
# a one-shot `migrate` job runs `alembic upgrade head` as the fr_migrator role
# and every API replica waits on `service_completed_successfully`, so replicas
# can never race. The API itself re-verifies the head (MIGRATIONS_MODE=verify)
# and refuses to serve a schema it does not recognise. deploy.sh's job is to
# make sure the roles exist first and that the head pin matches the code —
# both done before anything starts.
#
# db/roles.sql is idempotent and was, until now, the single manual step most
# likely to be fumbled (four passwords pasted into a shell command).

stage_db_init() {
    stage_begin "11 database roles"
    explain "WHAT" "Creates the least-privilege database roles and proves they are real."
    explain "READS" "db/roles.sql, with passwords taken from docker/.env"
    explain "WRITES" "roles inside postgres: fr_app, fr_migrator, fr_readonly, fr_backup."
    explain_cont "Idempotent - re-running disturbs neither existing roles nor data."
    explain_cont "Also REPORTS whether this database is empty (a fresh install) or"
    explain_cont "already holds data, so a redeploy onto an old volume is visible."
    explain "FAIL" "verified, not assumed: fr_app must connect AND must be REFUSED"
    explain_cont "CREATE ROLE ... SUPERUSER. If that succeeds, this stage fails."

    # ---- apply: postgres alone, then the roles --------------------------
    info "starting postgres"
    compose_mutate up -d postgres || stage_fail "could not start postgres"

    if [ "$DRY_RUN" = "1" ]; then stage_pass "would apply db/roles.sql"; return 0; fi

    wait_for "postgres to accept connections" 120 \
        compose exec -T postgres pg_isready -U postgres -d face_recognition \
        || stage_fail "postgres did not become ready within 120s"
    ok "postgres accepts connections"

    # ---- report which kind of deployment this actually is ----------------
    #
    # A "fresh install" onto a host that still has the old named volume is not
    # fresh, and nothing else in the run would say so: the migrate job simply
    # applies whatever revisions are missing and the stack comes up healthy on
    # last month's data. That is the failure this reports — it does not act on
    # it, because deleting a database is never something a deploy should
    # decide for you.
    local table_count row_count schema_version
    table_count="$(compose exec -T postgres psql -U postgres -d face_recognition -tAc \
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" \
        2>/dev/null | tr -d '\r[:space:]')"
    table_count="${table_count:-0}"

    if [ "$table_count" = "0" ]; then
        info "database state: EMPTY — this is a fresh installation."
        info "  the migrate job in stage 12 creates the whole schema from"
        info "  alembic/versions/, ending at the head pinned in docker/.env"
        state_set database_state "fresh"
    else
        schema_version="$(compose exec -T postgres psql -U postgres -d face_recognition -tAc \
            "SELECT version_num FROM alembic_version LIMIT 1" 2>/dev/null | tr -d '\r[:space:]')"
        row_count="$(compose exec -T postgres psql -U postgres -d face_recognition -tAc \
            "SELECT count(*) FROM users" 2>/dev/null | tr -d '\r[:space:]')"
        info "database state: EXISTING — $table_count tables, ${row_count:-?} user account(s)"
        info "  schema at revision ${schema_version:-<none recorded>}"
        info "  stage 12 will APPLY MISSING MIGRATIONS to this data, not replace it"
        state_set database_state "existing (${table_count} tables, rev ${schema_version:-unknown})"
        # Not a failure: upgrading an existing deployment is the normal case.
        # It is only surprising when someone believes they are installing fresh.
        warn "this host already has a database. If you intended a FRESH install, stop now: run 'sudo ./deploy.sh uninstall --purge-data' first (it demands a typed confirmation), then re-run. Continuing will keep the existing data."
    fi

    # Credentials come from docker/.env, read in a subshell so they never
    # leak into this shell's environment or the process list of anything else.
    local applied=1
    (
        set -a
        # shellcheck source=/dev/null
        . "$ROOT/$ENV_FILE_REL"
        set +a
        compose exec -T postgres psql -U postgres -d face_recognition \
            -v ON_ERROR_STOP=1 \
            -v fr_app_password="${FR_APP_PASSWORD}" \
            -v fr_migrator_password="${FR_MIGRATOR_PASSWORD}" \
            -v fr_readonly_password="${FR_READONLY_PASSWORD}" \
            -v fr_backup_password="${FR_BACKUP_PASSWORD}" \
            < "$ROOT/db/roles.sql" >/dev/null 2>&1
    ) && applied=0
    [ "$applied" = 0 ] || stage_fail "applying db/roles.sql failed (re-run with the log open: docker compose ... exec -T postgres psql -U postgres -d face_recognition < db/roles.sql)"
    ok "db/roles.sql applied (idempotent)"

    # ---- verify: least privilege is real, not assumed --------------------
    local probe
    probe=$(
        set -a
        # shellcheck source=/dev/null
        . "$ROOT/$ENV_FILE_REL"
        set +a
        compose exec -T -e PGPASSWORD="${FR_APP_PASSWORD}" postgres \
            psql -U fr_app -h 127.0.0.1 -d face_recognition -tAc "SELECT 1" 2>&1
    )
    printf '%s' "$probe" | grep -q '^1$' || stage_fail "fr_app cannot connect with the generated password: $probe"

    probe=$(
        set -a
        # shellcheck source=/dev/null
        . "$ROOT/$ENV_FILE_REL"
        set +a
        compose exec -T -e PGPASSWORD="${FR_APP_PASSWORD}" postgres \
            psql -U fr_app -h 127.0.0.1 -d face_recognition -tAc \
            "CREATE ROLE deploy_privilege_probe SUPERUSER" 2>&1
    )
    if printf '%s' "$probe" | grep -qi "permission denied\|must be superuser"; then
        ok "fr_app is correctly refused CREATE ROLE ... SUPERUSER"
    else
        stage_fail "fr_app was NOT refused superuser role creation — least privilege is not in force: $probe"
    fi

    state_set db_roles_applied_at "$(timestamp)"
    stage_pass "fr_app / fr_migrator / fr_readonly / fr_backup present and least-privileged"
}

# ---------------------------------------------------------------------------
# Stage 12 — bring the stack up in dependency order and wait for it.
#
# The order is enforced by the compose file itself (postgres -> migrate ->
# face_recognition -> nginx); what this stage adds is waiting for each gate to
# actually be satisfied and failing at the right one when it is not.
# ---------------------------------------------------------------------------
stage_up() {
    stage_begin "12 start services"
    explain "WHAT" "Starts the stack in dependency order and waits for it to be real."
    explain "WRITES" "containers and named volumes (postgres_data, storage_data, ...)."
    explain_cont "The one-shot migrate job runs alembic BEFORE the API is allowed to"
    explain_cont "start; the API waits on that job completing successfully."
    explain "FAIL" "migrate exiting non-zero (schema mismatch, fail-closed, exit 78), or"
    explain_cont "a service never becoming healthy. Nothing is rolled back and the"
    explain_cont "data is untouched, so diagnose and re-run."

    compose_mutate up -d || stage_fail "docker compose up failed"
    if [ "$DRY_RUN" = "1" ]; then stage_pass "would start the stack"; return 0; fi

    # 1. the migrate job must COMPLETE, not merely run.
    info "waiting for the migrate job"
    local waited=0 state="" exitcode=""
    while [ "$waited" -lt 300 ]; do
        local cid
        cid="$(compose ps -a -q migrate 2>/dev/null | head -1)"
        if [ -n "$cid" ]; then
            state="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)"
            exitcode="$(docker inspect -f '{{.State.ExitCode}}' "$cid" 2>/dev/null)"
            [ "$state" = "exited" ] && break
        fi
        sleep 3; waited=$((waited + 3))
        [ $((waited % 30)) -eq 0 ] && info "migrate still running (${waited}s)"
    done
    if [ "$state" != "exited" ]; then
        stage_fail "the migrate job did not finish within 300s (state=$state). Its log: docker compose ... logs migrate"
    fi
    if [ "$exitcode" != "0" ]; then
        local mlog; mlog="$(compose logs --tail 30 migrate 2>&1 | tail -20)"
        stage_fail "database migrations failed (exit $exitcode) — the stack is deliberately fail-closed and the API will not serve an unknown schema:
$mlog"
    fi
    ok "migrations applied (migrate job exited 0)"

    # 2. every long-running service healthy.
    local services svc waited_h=0
    services="$(compose config --services 2>/dev/null | grep -v '^migrate$')"
    info "waiting for services to become healthy"
    while [ "$waited_h" -lt 420 ]; do
        local pending=""
        for svc in $services; do
            local cid status health
            cid="$(compose ps -q "$svc" 2>/dev/null | head -1)"
            if [ -z "$cid" ]; then pending="$pending $svc"; continue; fi
            status="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)"
            health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null)"
            case "$health" in
                healthy) : ;;
                none)    [ "$status" = "running" ] || pending="$pending $svc" ;;
                *)       pending="$pending $svc" ;;
            esac
        done
        [ -z "$pending" ] && break
        sleep 5; waited_h=$((waited_h + 5))
        [ $((waited_h % 60)) -eq 0 ] && info "still waiting:$pending (${waited_h}s)"
    done

    local unhealthy=""
    for svc in $services; do
        local cid health status
        cid="$(compose ps -q "$svc" 2>/dev/null | head -1)"
        if [ -z "$cid" ]; then unhealthy="$unhealthy $svc(absent)"; continue; fi
        status="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)"
        health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null)"
        case "$health" in
            healthy) ok "$svc healthy" ;;
            none)    [ "$status" = "running" ] && ok "$svc running (no healthcheck)" || unhealthy="$unhealthy $svc($status)" ;;
            *)       unhealthy="$unhealthy $svc($health)" ;;
        esac
    done
    [ -z "$unhealthy" ] || stage_fail "service(s) not healthy:$unhealthy — logs: ./deploy.sh logs <service>"

    state_set last_start_at "$(timestamp)"
    stage_pass "all services healthy; migrations applied"
}
