#!/usr/bin/env bash
# Keep production's named volumes by default. A new installation may back the
# same volume names with explicit host directories using the storage overlay.
WIZARD_STORAGE_READY=0
WIZARD_DATA_ROOT=""

storage_root() {
    if [ "$WIZARD_STORAGE_READY" = 1 ]; then
        printf '%s' "$WIZARD_DATA_ROOT"
    elif [ "${VAS_DATA_ROOT+x}" = x ]; then
        printf '%s' "$VAS_DATA_ROOT"
    else
        deployment_env_value VAS_DATA_ROOT
    fi
}

storage_tool() {
    python3 "$ROOT/scripts/deploy/storage_layout.py" "$1" --checkout "$ROOT" --data-root "$(storage_root)"
}

wizard_storage_settings() {
    printf '\nStep 7 of 8 — Storage and model/map files\n'
    printf 'Production data includes face photos, database, trained models, caches, logs and backups.\n'
    printf '  1) Keep existing storage (Docker-managed volumes on a new server)\n'
    printf '  2) Choose a dedicated host directory (new installations only)\n'
    local current choice
    current="$(storage_root)"
    printf 'Current data directory: %s\n' "${current:-Docker-managed; inspect actual locations with sudo ./deploy.sh storage}"
    while :; do
        wizard_prompt 'Storage option' 1
        choice="$WIZARD_REPLY"
        case "$choice" in 1|2) break ;; *) printf 'Please enter 1 or 2.\n' ;; esac
    done
    WIZARD_DATA_ROOT="$current"
    WIZARD_STORAGE_READY=1
    if [ "$choice" = 2 ]; then
        printf 'Mount your data disk first. Use an absolute path, for example /srv/vas-data.\n'
        printf 'Existing volumes will be checked; a different location is refused, never silently migrated.\n'
        while :; do
            wizard_prompt 'Persistent data directory' "${current:-/srv/vas-data}"
            WIZARD_DATA_ROOT="$WIZARD_REPLY"
            storage_tool check-root && break
        done
    fi
    storage_tool plan || die 'Cannot display storage plan'
    printf 'Copy the two verified ONNX files plus manifest to weights/ before installation.\n'
    printf 'Copy maps to map-data/production/ and fonts to its fonts/ subdirectory.\n'
    printf 'Missing maps disable map features; missing or corrupt required weights block startup.\n'
}

wizard_apply_storage() {
    [ "$WIZARD_STORAGE_READY" = 1 ] || [ "${VAS_DATA_ROOT+x}" = x ] || return 0
    local selected; selected="$(storage_root)"
    storage_tool check-root || die "Invalid VAS_DATA_ROOT"
    # The restricted path alphabet permits dotenv single-quoting without expansion.
    upsert_env_kv VAS_DATA_ROOT "'$selected'" || die 'Cannot save VAS_DATA_ROOT'
}

stage_storage() {
    stage_begin '03 persistent storage'
    storage_tool check || stage_fail 'Storage does not match the existing volumes. See the paths and instructions above.'
    if [ "$DRY_RUN" = 1 ]; then
        info 'DRY: would create only missing host storage directories with service ownership.'
    else
        storage_tool prepare || stage_fail 'Cannot prepare persistent storage directories'
    fi
    stage_pass 'Storage layout verified; existing data preserved'
}
