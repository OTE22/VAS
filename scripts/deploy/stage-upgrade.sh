#!/usr/bin/env bash
#
# Lifecycle: status, stop, restart, logs, backup, restore, upgrade, uninstall.
#
# THE DATA RULE, stated once and enforced everywhere below:
# restart, redeploy, upgrade, rollback and a normal uninstall NEVER remove a
# volume. `docker compose down -v` appears exactly once in this file, behind
# --purge-data and a typed confirmation, and even then the on-disk secrets,
# certificates and backups are left alone.

# ---------------------------------------------------------------------------
# status — what is deployed, from what configuration, and is it still current
# ---------------------------------------------------------------------------
cmd_status() {
    stage_gpu_detect_quiet
    printf '%sFACE_DETECTOR deployment status%s\n\n' "$C_BOLD" "$C_RESET"

    printf '%sRecorded state (.deployment/state.json)%s\n' "$C_BOLD" "$C_RESET"
    if [ -f "$(state_file)" ]; then
        local key
        # last_result and last_failed_at are shown deliberately: without them a
        # last_failed_stage left over from an older run reads as though the
        # most recent run had failed.
        for key in deployed_version migration_head gpu_assignment model_manifest_sha \
                   config_fingerprint last_result last_successful_deployment \
                   last_health_result last_health_at last_failed_stage last_failed_at; do
            local value; value="$(state_get "$key" 2>/dev/null)"
            [ -n "$value" ] && printf '  %-28s %s\n' "$key" "$value"
        done
    else
        printf '  (nothing deployed by deploy.sh yet)\n'
    fi

    local recorded current
    recorded="$(state_get config_fingerprint 2>/dev/null)"
    current="$(config_fingerprint)"
    printf '\n%sConfiguration%s\n' "$C_BOLD" "$C_RESET"
    printf '  fingerprint now              %s\n' "$current"
    if [ -n "$recorded" ] && [ "$recorded" != "$current" ]; then
        printf '  %sthe configuration on disk has changed since the last successful deployment%s\n' "$C_YELLOW" "$C_RESET"
        printf '  run: sudo ./deploy.sh start   (or upgrade) to apply it\n'
    fi
    printf '  stack files                  %s\n' "$(compose_files | tr '\n' ' ')"

    printf '\n%sServices%s\n' "$C_BOLD" "$C_RESET"
    compose ps 2>/dev/null || printf '  (stack is not running)\n'

    printf '\n%sImages%s\n' "$C_BOLD" "$C_RESET"
    docker images --filter "reference=face_detector_prod*" \
        --format '  {{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedSince}}' 2>/dev/null | head -10
}

cmd_stop() {
    printf 'stopping services (containers stop; volumes and data are untouched)\n'
    compose_mutate stop
    ok "stopped — data preserved. Start again with: sudo ./deploy.sh start"
}

cmd_restart() {
    stage_preflight
    stage_gpu_detect
    stage_compose_validate
    stage_begin "restart"
    compose_mutate up -d || stage_fail "restart failed"
    stage_pass "services restarted (no volume touched)"
    stage_up_wait_light
    stage_health
}

# A light wait used by restart: the migrate job may be a no-op, so only the
# long-running services are polled.
stage_up_wait_light() {
    [ "$DRY_RUN" = "1" ] && return 0
    local svc waited=0
    while [ "$waited" -lt 240 ]; do
        local pending=""
        for svc in $(compose config --services 2>/dev/null | grep -v '^migrate$'); do
            service_healthy "$svc" >/dev/null 2>&1 || pending="$pending $svc"
        done
        [ -z "$pending" ] && return 0
        sleep 5; waited=$((waited + 5))
    done
    return 0
}

cmd_logs() {
    local service="${1:-}"
    if [ -n "$service" ]; then
        compose logs --tail 200 -f "$service"
    else
        compose logs --tail 100 -f
    fi
}

# ---------------------------------------------------------------------------
# backup — delegate to the tested script inside the backup service
# ---------------------------------------------------------------------------
stage_backup() {
    stage_begin "backup"
    if [ "$DRY_RUN" = "1" ]; then stage_pass "would take a backup"; return 0; fi

    compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -q '^postgres$' \
        || stage_fail "postgres is not running — a backup needs the database up"

    local before after
    before="$(compose exec -T backup sh -c 'ls -1 /backups 2>/dev/null | grep "Z$" | wc -l' 2>/dev/null || echo 0)"
    info "running scripts/backup/backup.sh inside the backup service"
    compose_mutate exec -T backup sh /scripts/backup.sh /backups || stage_fail "backup failed"

    after="$(compose exec -T backup sh -c 'ls -1 /backups 2>/dev/null | grep "Z$" | wc -l' 2>/dev/null || echo 0)"
    [ "${after:-0}" -gt "${before:-0}" ] || stage_fail "no new backup directory appeared"

    local newest
    newest="$(compose exec -T backup sh -c 'ls -1 /backups | grep "Z$" | tail -1' 2>/dev/null | tr -d '\r')"
    compose exec -T backup sh -c "cd /backups/$newest && sha256sum -c SHA256SUMS" >/dev/null 2>&1 \
        || stage_fail "the new backup $newest does not verify against its own SHA256SUMS"

    state_set last_backup "$newest"
    state_set last_backup_at "$(timestamp)"
    LAST_BACKUP_ID="$newest"
    stage_pass "$newest (checksums verified)"
}

# ---------------------------------------------------------------------------
# restore — never silent, never partial
# ---------------------------------------------------------------------------
cmd_restore() {
    local stamp="${1:-}"
    if [ -z "$stamp" ]; then
        printf 'usage: sudo ./deploy.sh restore <backup-id> [--force]\n\navailable backups:\n'
        compose exec -T backup sh -c 'ls -1 /backups 2>/dev/null | grep "Z$"' 2>/dev/null | sed 's/^/  /' \
            || printf '  (cannot list — is the stack running?)\n'
        exit 2
    fi

    printf '%sRestoring %s%s\n' "$C_BOLD" "$stamp" "$C_RESET"
    printf 'This replaces the CURRENT database contents with the backup.\n'
    printf 'scripts/backup/restore.sh verifies every checksum first and refuses a\n'
    printf 'populated database unless --force is given.\n\n'

    confirm_phrase "RESTORE $stamp" \
        "A restore is not reversible without another backup." \
        || die "restore cancelled (nothing was changed)"

    local -a args=("$stamp")
    [ "$RESTORE_FORCE" = "1" ] && args+=("--force")
    compose_mutate exec -T backup sh /scripts/restore.sh "${args[@]}" || die "restore failed — the database was left as restore.sh found it"

    ok "restore completed; restarting services so they re-read the database"
    compose_mutate restart face_recognition || true
    state_set last_restore "$stamp"
    state_set last_restore_at "$(timestamp)"
    ok "restored $stamp"
}

# ---------------------------------------------------------------------------
# upgrade — preflight, backup, snapshot, build, migrate, verify, rollback
#
# What can be rolled back automatically and what cannot:
#   code only (schema unchanged)  -> automatic: retag :rollback -> :latest, up
#   schema advanced               -> NOT automatic. The older code refuses a
#                                    newer schema by design (fail-closed), so
#                                    "rolling back the code" would produce a
#                                    stack that will not start. That is an
#                                    operator decision between rolling forward
#                                    and restoring the pre-upgrade backup.
# ---------------------------------------------------------------------------
cmd_upgrade() {
    stage_preflight
    stage_gpu_detect
    stage_compose_validate

    # ---- 1. do not upgrade a broken stack unless told to ------------------
    stage_begin "upgrade preflight"
    local running_before=0
    if compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -q '^face_recognition$'; then
        running_before=1
        if ! service_healthy face_recognition >/dev/null 2>&1; then
            if [ "$ASSUME_YES" != "1" ]; then
                stage_fail "the running stack is not healthy. Fix it first, or re-run with --yes to upgrade anyway."
            fi
            stage_warn "upgrading an unhealthy stack (--yes)"
        fi
    else
        info "no running stack — this upgrade is also the first start"
    fi
    local old_head; old_head="$(read_env_kv MIGRATIONS_EXPECTED_HEAD 2>/dev/null)"
    local old_version; old_version="$(state_get deployed_version 2>/dev/null)"
    info "current: version=${old_version:-unknown} head=${old_head:-unset}"
    stage_pass "preflight complete"

    # ---- 2. database backup ----------------------------------------------
    if [ "$running_before" = "1" ]; then
        LAST_BACKUP_ID=""
        stage_backup
        UPGRADE_BACKUP="${LAST_BACKUP_ID:-}"
    else
        UPGRADE_BACKUP=""
        info "no running stack: no pre-upgrade backup to take"
    fi

    # ---- 3. configuration snapshot ---------------------------------------
    stage_begin "configuration snapshot"
    local snap
    snap="$ROOT/backups/config-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
    if [ "$DRY_RUN" = "1" ]; then
        info "DRY: would write $snap"
    else
        run mkdir -p "$ROOT/backups"
        local members=(docker/.env docker/docker-compose.prod.yml docker/docker-compose.prod.gpu.yml)
        [ -f "$GPU_OVERLAY" ] && members+=(docker/gpu-allocation.generated.yml)
        [ -d "$ROOT/secrets" ] && members+=(secrets)
        [ -d "$ROOT/certs" ] && members+=(certs)
        [ -f "$ROOT/weights/WEIGHTS_MANIFEST.json" ] && members+=(weights/WEIGHTS_MANIFEST.json)
        tar czf "$snap" -C "$ROOT" "${members[@]}" \
            2>/dev/null || stage_fail "could not write the configuration snapshot"
        chmod 600 "$snap" 2>/dev/null || true
        warn "$(basename "$snap") contains secrets and private keys — mode 600, keep it where backups live"
    fi
    UPGRADE_CONFIG_SNAPSHOT="$snap"
    stage_pass "$(basename "$snap")"

    # ---- 4. model weights still verified ---------------------------------
    stage_model_check

    # ---- 5. remember what we can roll back to ----------------------------
    stage_begin "rollback point"
    local images image tagged=0
    images="$(compose config --images 2>/dev/null | grep -E '^face_detector_prod|^face-detector' || true)"
    if [ "$DRY_RUN" = "1" ]; then
        info "DRY: would retag current images as :rollback"
    else
        for image in $images; do
            local repo="${image%%:*}"
            docker image inspect "$image" >/dev/null 2>&1 || continue
            docker tag "$image" "${repo}:rollback" 2>/dev/null && { info "tagged ${repo}:rollback"; tagged=$((tagged + 1)); }
        done
    fi
    state_set rollback_version "${old_version:-unknown}"
    state_set rollback_head "${old_head:-unset}"
    stage_pass "$tagged image(s) tagged :rollback; head $old_head recorded"

    # ---- 6. build the new version ----------------------------------------
    stage_build
    local new_head; new_head="$(derive_migrations_head)" || stage_fail "cannot derive the new alembic head"
    if [ "$new_head" != "$old_head" ]; then
        info "schema will advance: $old_head -> $new_head"
    fi
    upsert_env_kv MIGRATIONS_EXPECTED_HEAD "$new_head"
    state_set migration_head "$new_head"

    # ---- 7. migrate + restart, then verify -------------------------------
    UPGRADE_OLD_HEAD="$old_head"; UPGRADE_NEW_HEAD="$new_head"
    # read by stage-health.sh, which makes the backup check mandatory here
    # shellcheck disable=SC2034
    UPGRADE_IN_PROGRESS=1
    stage_up_or_rollback
    stage_ollama_models
    stage_health_or_rollback
    # shellcheck disable=SC2034
    UPGRADE_IN_PROGRESS=0

    state_set last_upgrade_at "$(timestamp)"
    state_set last_upgrade_from "${old_version:-unknown}"
}

# stage_up_or_rollback / stage_health_or_rollback: the upgrade's failure paths.
# They deliberately do NOT reuse stage_fail's abort, because an upgrade must
# attempt recovery before it reports.
stage_up_or_rollback() {
    stage_begin "12 start services (upgrade)"
    if ! compose_mutate up -d; then
        upgrade_rollback "the new version failed to start"
        return
    fi
    [ "$DRY_RUN" = "1" ] && { stage_pass "would start"; return; }

    local cid state exitcode waited=0
    while [ "$waited" -lt 300 ]; do
        cid="$(compose ps -a -q migrate 2>/dev/null | head -1)"
        if [ -n "$cid" ]; then
            state="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)"
            [ "$state" = "exited" ] && break
        fi
        sleep 3; waited=$((waited + 3))
    done
    exitcode="$(docker inspect -f '{{.State.ExitCode}}' "$cid" 2>/dev/null || echo 1)"
    if [ "$exitcode" != "0" ]; then
        # Migrations failed: the schema did not advance, so the code rollback
        # is safe and automatic.
        upgrade_rollback "database migrations failed (migrate exit $exitcode)"
        return
    fi
    stage_up_wait_light
    stage_pass "new version started; migrations applied"
}

stage_health_or_rollback() {
    stage_health || true
    if [ "${STAGE_RESULT[14 health and acceptance]:-}" = "FAIL" ]; then
        upgrade_rollback "post-upgrade health checks failed"
    fi
}

# upgrade_rollback: automatic ONLY when the schema did not advance.
upgrade_rollback() {
    local reason="$1"
    printf '\n%s== upgrade failed: %s%s\n' "$C_RED" "$reason" "$C_RESET"

    if [ "${UPGRADE_OLD_HEAD:-}" != "${UPGRADE_NEW_HEAD:-}" ]; then
        # The schema advanced (or may have). Older code refuses a newer schema
        # by design — rolling the code back would produce a stack that cannot
        # start, and silently downgrading a schema loses data.
        cat <<EOF

${C_BOLD}The database schema advanced during this upgrade${C_RESET}
  before: ${UPGRADE_OLD_HEAD:-unknown}
  after:  ${UPGRADE_NEW_HEAD:-unknown}

deploy.sh will NOT roll the code back automatically: the previous version
refuses to start against a newer schema (that fail-closed behaviour is what
stops it corrupting data). This is an operator decision:

  roll forward   fix the problem and re-run:  sudo ./deploy.sh upgrade
  roll back      restore the pre-upgrade database, then the previous code:
                     sudo ./deploy.sh restore ${UPGRADE_BACKUP:-<backup-id>} --force
                     git checkout <previous-commit> && sudo ./deploy.sh start

Configuration snapshot: ${UPGRADE_CONFIG_SNAPSHOT:-<none>}
Database backup:        ${UPGRADE_BACKUP:-<none taken: the stack was not running>}
EOF
        state_set last_result "FAIL"
        state_set last_failed_stage "upgrade (schema advanced; manual decision required)"
        print_report
        printf '\n%sDEPLOY RESULT: FAIL%s — upgrade halted, no automatic rollback (schema advanced)\n' "$C_RED" "$C_RESET"
        [ -n "$DEPLOY_LOG" ] && printf 'log: %s\n' "$DEPLOY_LOG"
        exit 1
    fi

    # Code-only: retag and bring the previous images back.
    printf '%sthe schema did not advance — rolling the code back automatically%s\n' "$C_YELLOW" "$C_RESET"
    local image repo restored=0
    for image in $(compose config --images 2>/dev/null | grep -E '^face_detector_prod|^face-detector' || true); do
        repo="${image%%:*}"
        if docker image inspect "${repo}:rollback" >/dev/null 2>&1; then
            docker tag "${repo}:rollback" "$image" && restored=$((restored + 1))
        fi
    done
    upsert_env_kv MIGRATIONS_EXPECTED_HEAD "${UPGRADE_OLD_HEAD}"
    compose_mutate up -d || true
    stage_up_wait_light

    if service_healthy face_recognition >/dev/null 2>&1; then
        ok "rolled back to the previous images ($restored restored); the stack is healthy again"
        state_set last_rollback_at "$(timestamp)"
        state_set last_result "FAIL"
        state_set last_failed_stage "upgrade (rolled back: $reason)"
        print_report
        printf '\n%sDEPLOY RESULT: FAIL%s — upgrade rolled back safely. Data intact.\n' "$C_YELLOW" "$C_RESET"
        printf 'reason: %s\n' "$reason"
        [ -n "$DEPLOY_LOG" ] && printf 'log: %s\n' "$DEPLOY_LOG"
        exit 1
    fi

    fail "the rollback did not bring the stack back to health — restore the database backup: sudo ./deploy.sh restore ${UPGRADE_BACKUP:-<backup-id>} --force"
    state_set last_result "FAIL"
    state_set last_failed_stage "upgrade (rollback incomplete)"
    exit 1
}

# ---------------------------------------------------------------------------
# uninstall — containers go, data stays (unless explicitly purged)
# ---------------------------------------------------------------------------
cmd_uninstall() {
    printf '%sUninstall%s\n\n' "$C_BOLD" "$C_RESET"
    printf 'This removes the containers. By default it keeps EVERYTHING that holds data:\n'
    printf '  volumes   postgres_data redis_data storage_data logs_data face_database_data\n'
    printf '            ml_artifacts_data chromadb_cache hf_cache_data ollama_models backup_data\n'
    printf '  on disk   secrets/  certs/  backups/  docker/.env  weights/\n'
    printf '  network   webhook_integration (shared with the VMS project)\n\n'

    if [ "$PURGE_DATA" = "1" ]; then
        printf '%s--purge-data was given: every named volume above will be DELETED.%s\n' "$C_RED" "$C_RESET"
        printf 'That includes the database, the face gallery, the ML artifacts and the backups volume.\n'
        printf 'It cannot be undone from inside this system.\n\n'
        confirm_phrase "DELETE face_detector_prod DATA" \
            "Type the phrase exactly to confirm irreversible data loss." \
            || die "purge cancelled — nothing was removed"
    else
        confirm "Remove the containers (data preserved)?" || die "cancelled"
    fi

    if [ "$PURGE_DATA" = "1" ]; then
        compose_mutate down --remove-orphans -v || die "uninstall failed"
        warn "named volumes deleted"
    else
        compose_mutate down --remove-orphans || die "uninstall failed"
        ok "containers removed; every volume kept"
    fi

    if [ "$REMOVE_IMAGES" = "1" ]; then
        local image repo
        for image in $(compose config --images 2>/dev/null | grep -E '^face_detector_prod|^face-detector' || true); do
            repo="${image%%:*}"
            run docker image rm -f "$image" "${repo}:rollback" 2>/dev/null || true
        done
        ok "images removed"
    fi

    state_set last_uninstall_at "$(timestamp)"
    printf '\n%sKept on disk%s (delete by hand if you really mean to):\n' "$C_BOLD" "$C_RESET"
    printf '  %s/secrets  %s/certs  %s/backups  %s/docker/.env\n' "$ROOT" "$ROOT" "$ROOT" "$ROOT"
    [ "$PURGE_DATA" = "1" ] || printf '  and every named volume — reinstall with: sudo ./deploy.sh\n'
}
