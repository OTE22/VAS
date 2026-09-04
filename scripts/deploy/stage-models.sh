#!/usr/bin/env bash
#
# Stage 08 — model weights: manifest, verification, import.
#
# WHAT THIS SYSTEM ACTUALLY LOADS
#   weights/det_10g.onnx    SCRFD face detector    -> face_recognition, onnxruntime
#   weights/w600k_r50.onnx  ArcFace recogniser     -> face_recognition, onnxruntime
# Both are bind-mounted read-only at /app/weights, are never baked into the
# image, and are never downloaded at runtime: a missing or corrupt file is a
# hard startup failure. Nothing else in this codebase loads DL weights —
# object detection, LPR and vehicle analytics are not part of it (person boxes
# come from the external VMS), and the ML registry's sklearn artifacts are
# produced by the system and verified by the database against their own hashes.
#
# WHY A MANIFEST
# The recogniser's FILENAME is the embedding-version stamp written into every
# stored vector. A swapped or truncated file therefore does not merely degrade
# accuracy — it silently invalidates comparability with every embedding already
# in the database. So the check is fail-closed and content-based, mirroring the
# map-content ledger's vocabulary: a file that has never been MEASURED is not
# usable, no matter that it exists.
#
#   CONTENT_NOT_VERIFIED  no manifest, or unparseable
#   CONTENT_MISSING       manifest names a file that is not on disk
#   CHECKSUM_MISMATCH     size or sha256 differs from the manifest
#   VERIFIED              measured, and the bytes are the expected bytes

MANIFEST_PATH_REL="weights/WEIGHTS_MANIFEST.json"

REQUIRED_WEIGHTS=(
    "scrfd_10g_detection|det_10g.onnx|face_recognition|onnxruntime|DETECTION_MODEL"
    "arcface_w600k_r50_recognition|w600k_r50.onnx|face_recognition|onnxruntime|RECOGNITION_MODEL"
)

manifest_path() { printf '%s' "$ROOT/$MANIFEST_PATH_REL"; }

# manifest_field <filename> <field>: read one value from the flat JSON without
# needing jq on the target host.
manifest_field() {
    local filename="$1" field="$2" file; file="$(manifest_path)"
    [ -f "$file" ] || return 1
    awk -v fn="$filename" -v key="$field" '
        $0 ~ "\"filename\"[[:space:]]*:[[:space:]]*\"" fn "\"" { found = 1 }
        found && $0 ~ "\"" key "\"[[:space:]]*:" {
            line = $0
            sub(/^.*"" key ""[[:space:]]*:[[:space:]]*/, "", line)
            gsub(/[",]/, "", line)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
            print line
            exit
        }
        found && /^[[:space:]]*}/ { found = 0 }
    ' "$file"
}

# Simpler, more robust extractor: pull the object for a filename, then the key.
manifest_value() {
    local filename="$1" key="$2" file; file="$(manifest_path)"
    [ -f "$file" ] || return 1
    tr -d '\n' < "$file" \
        | sed 's/},/}\n/g' \
        | grep "\"filename\"[[:space:]]*:[[:space:]]*\"$filename\"" \
        | sed -n "s/.*\"$key\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^,\"}]*\)\"\{0,1\}.*/\1/p" \
        | head -1 | tr -d ' '
}

file_bytes() {
    if stat -c %s "$1" >/dev/null 2>&1; then stat -c %s "$1"
    else stat -f %z "$1"; fi
}

# ---------------------------------------------------------------------------
# model-manifest: generate the manifest from weights known to be good.
# Run on a TRUSTED machine, then commit the result — it is the reference every
# deployment measures against.
# ---------------------------------------------------------------------------
cmd_model_manifest() {
    local file; file="$(manifest_path)"
    if [ -f "$file" ] && [ "$ASSUME_YES" != "1" ]; then
        printf 'A manifest already exists at %s\n' "$MANIFEST_PATH_REL"
        printf 'Regenerating it makes whatever is on disk RIGHT NOW the reference.\n'
        confirm "Regenerate from the current weights/ contents?" || { echo "kept the existing manifest"; return 0; }
    fi

    local entry name filename consumer runtime setting path bytes digest missing=""
    for entry in "${REQUIRED_WEIGHTS[@]}"; do
        IFS='|' read -r name filename consumer runtime setting <<<"$entry"
        path="$ROOT/weights/$filename"
        [ -f "$path" ] || missing="$missing $filename"
    done
    [ -z "$missing" ] || die "cannot generate a manifest: missing weight file(s)$missing"

    local tmp; tmp="$(mktemp)"
    {
        printf '{\n'
        printf '  "schema_version": 1,\n'
        printf '  "generated_at": "%s",\n' "$(timestamp)"
        printf '  "generated_by": "deploy.sh model-manifest",\n'
        printf '  "note": "Fail-closed reference for the runtime DL weights. A file that has never been measured is not usable. The recogniser filename is the embedding-version stamp: renaming or swapping it invalidates comparability with every stored embedding.",\n'
        printf '  "weights": [\n'
        local first=1
        for entry in "${REQUIRED_WEIGHTS[@]}"; do
            IFS='|' read -r name filename consumer runtime setting <<<"$entry"
            path="$ROOT/weights/$filename"
            bytes="$(file_bytes "$path")"
            digest="$(sha256_of "$path")"
            [ "$first" = 1 ] || printf ',\n'
            first=0
            printf '    {\n'
            printf '      "name": "%s",\n' "$name"
            printf '      "filename": "%s",\n' "$filename"
            printf '      "bytes": "%s",\n' "$bytes"
            printf '      "sha256": "%s",\n' "$digest"
            printf '      "consumer": "%s",\n' "$consumer"
            printf '      "runtime": "%s",\n' "$runtime"
            printf '      "setting": "%s",\n' "$setting"
            printf '      "mount": "/app/weights/%s (read-only)",\n' "$filename"
            printf '      "gpu_capable": "true"\n'
            printf '    }'
        done
        printf '\n  ],\n'
        printf '  "ollama_models": [\n'
        printf '    "qwen2.5:1.5b",\n'
        printf '    "hf.co/mradermacher/Arctic-Text2SQL-R1-7B-GGUF:Q4_K_M"\n'
        printf '  ],\n'
        printf '  "auto_downloaded_at_first_run": [\n'
        printf '    "chromadb all-MiniLM-L6-v2 ONNX (volume chromadb_cache)",\n'
        printf '    "sentence-transformers/all-MiniLM-L6-v2 (volume hf_cache_data)"\n'
        printf '  ],\n'
        printf '  "not_in_this_system": "object detection / LPR / vehicle / VLM weight files - person boxes are supplied by the external VMS over webhooks",\n'
        printf '  "map_archives": "map-data/production/*.mbtiles, verified separately by scripts/map_data/production_gate.py"\n'
        printf '}\n'
    } > "$tmp"

    mv -f "$tmp" "$file"
    ok "wrote $MANIFEST_PATH_REL"
    cat "$file"
}

# ---------------------------------------------------------------------------
# Stage 08 — model check (host-side, BEFORE any container starts)
# ---------------------------------------------------------------------------
# verify_weights_core: the manifest verification itself, reporting into the
# CURRENT stage (stage_begin must already have run). Shared by the
# production stage 08 and the development D2 stage so the fail-closed
# behaviour cannot drift between the two flows.
verify_weights_core() {
    local file; file="$(manifest_path)"
    if [ ! -f "$file" ]; then
        stage_fail "CONTENT_NOT_VERIFIED: no $MANIFEST_PATH_REL. Generate it on a trusted machine ('./deploy.sh model-manifest') and ship it with the deployment — an unmeasured weight file is not usable."
    fi

    local entry name filename consumer runtime setting path
    local want_bytes want_sha have_bytes have_sha problems="" verified=0
    for entry in "${REQUIRED_WEIGHTS[@]}"; do
        IFS='|' read -r name filename consumer runtime setting <<<"$entry"
        path="$ROOT/weights/$filename"
        want_bytes="$(manifest_value "$filename" bytes)"
        want_sha="$(manifest_value "$filename" sha256)"

        if [ -z "$want_sha" ]; then
            problems="$problems\n  CONTENT_NOT_VERIFIED $filename (not described by the manifest)"
            continue
        fi
        if [ ! -f "$path" ]; then
            problems="$problems\n  CONTENT_MISSING      $filename (expected at weights/$filename)"
            continue
        fi
        have_bytes="$(file_bytes "$path")"
        if [ "$have_bytes" != "$want_bytes" ]; then
            problems="$problems\n  CHECKSUM_MISMATCH    $filename (size $have_bytes, manifest says $want_bytes)"
            continue
        fi
        have_sha="$(sha256_of "$path")"
        if [ "$have_sha" != "$want_sha" ]; then
            problems="$problems\n  CHECKSUM_MISMATCH    $filename (sha256 ${have_sha:0:16}..., manifest says ${want_sha:0:16}...)"
            continue
        fi
        info "VERIFIED $filename  ${have_bytes} bytes  sha256 ${have_sha:0:16}...  -> $consumer ($runtime, $setting)"
        verified=$((verified + 1))
    done

    if [ -n "$problems" ]; then
        # Fail closed. The API would abort at startup on a missing file and,
        # worse, could load a wrong-but-loadable one; neither is allowed to
        # reach a container.
        stage_fail "model weight verification failed:$(printf '%b' "$problems")

Ship the correct files (or import them: --deploy-package=/path/to/package) and re-run.
deploy.sh never downloads, replaces or 'repairs' a weight file on its own."
    fi

    # Extra .onnx files are reported, never removed: they may be alternates an
    # operator keeps deliberately.
    local extras
    extras="$(find "$ROOT/weights" -maxdepth 1 -name '*.onnx' -printf '%f\n' 2>/dev/null \
              | grep -vE '^(det_10g|w600k_r50)\.onnx$' | tr '\n' ' ')"
    [ -n "$extras" ] && info "additional (unused) weight files present: $extras"

    state_set model_manifest_sha "$(sha256_of "$file" | cut -c1-16)"
    state_set model_weights_verified "$verified"
    stage_pass "$verified/2 runtime weights VERIFIED against $MANIFEST_PATH_REL"
}

stage_model_check() {
    stage_begin "08 model weights"
    explain "WHAT" "Verifies the two ONNX weights before any container can load them."
    explain "READS" "weights/det_10g.onnx           SCRFD face detection"
    explain_cont "weights/w600k_r50.onnx         ArcFace embeddings"
    explain_cont "weights/WEIGHTS_MANIFEST.json  the sha256 each file must match"
    explain "WRITES" "nothing."
    explain "NEVER" "downloads, replaces or repairs a weight. Ship them alongside the"
    explain_cont "checkout, or import them with --deploy-package=<dir>."
    explain "FAIL" "fail-closed on any mismatch. The recogniser FILENAME is the"
    explain_cont "embedding-version stamp written into every stored vector, so a"
    explain_cont "wrong-but-loadable file silently invalidates every embedding already"
    explain_cont "in the database - which no health check downstream would catch."

    # ---- optional import from a deployment package -------------------------
    if [ -n "$DEPLOY_PACKAGE" ]; then
        import_deploy_package || stage_fail "import from $DEPLOY_PACKAGE failed"
    fi

    verify_weights_core
}

# ---------------------------------------------------------------------------
# import_deploy_package: copy assets from an offline deployment package.
# Never overwrites a file that is already VERIFIED.
# ---------------------------------------------------------------------------
import_deploy_package() {
    [ -d "$DEPLOY_PACKAGE" ] || { fail "deployment package not found: $DEPLOY_PACKAGE"; return 1; }
    info "importing from $DEPLOY_PACKAGE"

    local entry name filename _c _r _s src dst
    for entry in "${REQUIRED_WEIGHTS[@]}"; do
        IFS='|' read -r name filename _c _r _s <<<"$entry"
        src="$DEPLOY_PACKAGE/weights/$filename"
        dst="$ROOT/weights/$filename"
        [ -f "$src" ] || continue
        if [ -f "$dst" ]; then
            local want; want="$(manifest_value "$filename" sha256 2>/dev/null)"
            if [ -n "$want" ] && [ "$(sha256_of "$dst")" = "$want" ]; then
                info "keeping verified weights/$filename (package copy not needed)"
                continue
            fi
            info "replacing unverified weights/$filename from the package"
        fi
        run cp -f "$src" "$dst.tmp" || return 1
        run mv -f "$dst.tmp" "$dst" || return 1
        info "imported weights/$filename"
    done

    # A manifest travelling with the package becomes the reference only when
    # the target has none — never silently replacing the committed one.
    if [ -f "$DEPLOY_PACKAGE/$MANIFEST_PATH_REL" ] && [ ! -f "$(manifest_path)" ]; then
        run cp -f "$DEPLOY_PACKAGE/$MANIFEST_PATH_REL" "$(manifest_path)"
        info "imported $MANIFEST_PATH_REL"
    fi

    # Pre-built images for an air-gapped host.
    local tar
    for tar in "$DEPLOY_PACKAGE"/images/*.tar; do
        [ -f "$tar" ] || continue
        info "docker load < $(basename "$tar")"
        run bash -c "docker load -i '$tar' >/dev/null" || return 1
    done
    return 0
}

# ---------------------------------------------------------------------------
# Stage 13 — Ollama models (chatbot/SQL agent; the face pipeline never waits
# for these).
# ---------------------------------------------------------------------------
stage_ollama_models() {
    stage_begin "13 LLM models"
    explain "WHAT" "Ensures the chat and SQL models are present in the ollama volume."
    explain "WRITES" "the named volume ollama_models (not a path inside this repo)."
    explain "FAIL" "advisory only. Chat and SQL answers stay unavailable; detection,"
    explain_cont "recognition and the rest of the system are unaffected."

    if ! compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -q '^ollama$'; then
        stage_warn "ollama is not running — chat and SQL answers stay unavailable until it is"
        return 0
    fi

    local wanted present missing=""
    wanted="$(tr -d '\n' < "$(manifest_path)" 2>/dev/null \
             | sed -n 's/.*"ollama_models"[[:space:]]*:[[:space:]]*\[\([^]]*\)\].*/\1/p' \
             | tr ',' '\n' | tr -d '"' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -v '^$')"
    [ -n "$wanted" ] || { stage_skip "manifest lists no LLM models"; return 0; }

    present="$(compose exec -T ollama ollama list 2>/dev/null | tail -n +2 | awk '{print $1}')"
    local model
    while IFS= read -r model; do
        [ -n "$model" ] || continue
        if printf '%s\n' "$present" | grep -qx "$model"; then
            info "present: $model"
        else
            missing="$missing $model"
        fi
    done <<<"$wanted"

    if [ -z "$missing" ]; then
        stage_pass "every LLM model in the manifest is present"
        return 0
    fi

    if [ "$ONLINE" != "1" ]; then
        stage_warn "offline: missing LLM model(s)$missing — the chatbot degrades; the face pipeline is unaffected. Seed the ollama_models volume (or set OLLAMA_MODELS_PATH) from a connected machine."
        return 0
    fi

    for model in $missing; do
        info "pulling $model (this can take several minutes)"
        compose_mutate exec -T ollama ollama pull "$model" || {
            stage_warn "could not pull $model — the chatbot degrades; the face pipeline is unaffected"
            return 0
        }
    done
    stage_pass "LLM models present"
}
