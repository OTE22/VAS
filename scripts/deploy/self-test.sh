#!/usr/bin/env bash
#
# ./deploy.sh --self-test
#
# Unit tests for deploy.sh's own decision logic, run against temporary
# fixtures. Touches nothing outside a scratch directory: no Docker, no GPU, no
# containers, no host mutation — so the logic that decides how a production
# host is configured can be verified on a laptop, and in CI, before it is ever
# pointed at a server.
#
# What is deliberately NOT here: anything that needs a real Docker daemon, a
# real NVIDIA driver or a running stack. Those are what `validate`, `gpu-test`
# and `health` are for, on the target host.

SELF_TEST_PASS=0
SELF_TEST_FAIL=0
SELF_TEST_DIR=""

t_ok()   { SELF_TEST_PASS=$((SELF_TEST_PASS + 1)); printf '  %sPASS%s %s\n' "$C_GREEN" "$C_RESET" "$1"; }
t_fail() { SELF_TEST_FAIL=$((SELF_TEST_FAIL + 1)); printf '  %sFAIL%s %s\n     %s\n' "$C_RED" "$C_RESET" "$1" "${2:-}"; }

t_eq() {
    local name="$1" want="$2" got="$3"
    [ "$want" = "$got" ] && t_ok "$name" || t_fail "$name" "expected [$want], got [$got]"
}

t_contains() {
    local name="$1" haystack="$2" needle="$3"
    case "$haystack" in *"$needle"*) t_ok "$name" ;; *) t_fail "$name" "expected to contain [$needle]" ;; esac
}

t_not_contains() {
    local name="$1" haystack="$2" needle="$3"
    case "$haystack" in *"$needle"*) t_fail "$name" "should NOT contain [$needle]" ;; *) t_ok "$name" ;; esac
}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
make_nvidia_stub() {
    # $1 = target path, remaining args = "index|uuid|name|vram" rows
    local path="$1"; shift
    {
        printf '#!/usr/bin/env bash\n'
        printf 'if [ "$1" = "--query-gpu=index,uuid,name,memory.total" ]; then\n'
        printf '  cat <<ROWS\n'
        local row
        for row in "$@"; do
            printf '%s\n' "$(printf '%s' "$row" | tr '|' ',' | sed 's/,/, /g')"
        done
        printf 'ROWS\n'
        printf '  exit 0\nfi\n'
        printf 'if [ "$1" = "--query-gpu=driver_version" ]; then echo 550.54.15; exit 0; fi\n'
        printf 'exit 0\n'
    } > "$path"
    chmod +x "$path"
}

# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------
test_migration_head_derivation() {
    printf '\n%salembic head derivation%s\n' "$C_BOLD" "$C_RESET"

    # Against the real repository: exactly one head, and it is the revision no
    # other file names as its down_revision.
    local head; head="$(derive_migrations_head 2>&1)"
    if [ -n "$head" ] && [ ${#head} -ge 8 ]; then
        t_ok "derives a single head from alembic/versions ($head)"
    else
        t_fail "derives a single head from alembic/versions" "$head"
    fi

    # Cross-check with an independent method (grep for the revision that never
    # appears as a down_revision) so a bug in the parser cannot agree with
    # itself.
    local independent
    independent="$(python3 - "$ROOT/alembic/versions" <<'PY' 2>/dev/null || true
import glob, os, re, sys
d = sys.argv[1]
revs, downs = set(), set()
for path in glob.glob(os.path.join(d, "*.py")):
    text = open(path, encoding="utf-8").read()
    m = re.search(r"^revision(?::[^=]+)?\s*=\s*['\"]([A-Za-z0-9_]+)['\"]", text, re.M)
    if m:
        revs.add(m.group(1))
    for line in text.splitlines():
        if line.startswith("down_revision"):
            downs.update(re.findall(r"['\"]([A-Za-z0-9_]{6,})['\"]", line))
heads = sorted(revs - downs)
print(heads[0] if len(heads) == 1 else "MULTIPLE:" + ",".join(heads))
PY
)"
    if [ -n "$independent" ]; then
        t_eq "matches an independent derivation" "$independent" "$head"
    else
        t_ok "independent derivation skipped (no python3 on this host)"
    fi

    # A repository with two heads must FAIL loudly, never pick one.
    local fixture="$SELF_TEST_DIR/two-heads"
    mkdir -p "$fixture"
    printf 'revision = "aaaaaa1"\ndown_revision = None\n' > "$fixture/a.py"
    printf 'revision = "bbbbbb2"\ndown_revision = "aaaaaa1"\n' > "$fixture/b.py"
    printf 'revision = "cccccc3"\ndown_revision = "aaaaaa1"\n' > "$fixture/c.py"
    local out; out="$(derive_migrations_head "$fixture" 2>&1)"
    if [ $? -ne 0 ] || printf '%s' "$out" | grep -q "expected exactly one"; then
        t_ok "refuses a two-head repository instead of guessing"
    else
        t_fail "refuses a two-head repository" "got [$out]"
    fi

    # Merge revisions express down_revision as a tuple; both parents must count
    # as referenced or the parser reports phantom heads.
    local merged="$SELF_TEST_DIR/merged"
    mkdir -p "$merged"
    printf 'revision = "aaaaaa1"\ndown_revision = None\n' > "$merged/a.py"
    printf 'revision = "bbbbbb2"\ndown_revision = "aaaaaa1"\n' > "$merged/b.py"
    printf 'revision = "cccccc3"\ndown_revision = "aaaaaa1"\n' > "$merged/c.py"
    printf 'revision = "dddddd4"\ndown_revision = ("bbbbbb2", "cccccc3")\n' > "$merged/d.py"
    t_eq "handles a merge revision's tuple down_revision" "dddddd4" "$(derive_migrations_head "$merged" 2>&1)"
}

test_env_editing() {
    printf '\n%sdocker/.env editing%s\n' "$C_BOLD" "$C_RESET"

    local saved_root="$ROOT"
    ROOT="$SELF_TEST_DIR/envtest"; mkdir -p "$ROOT/docker"
    cat > "$ROOT/docker/.env" <<'EOF'
# a comment that must survive
FR_APP_PASSWORD=keepme
PUBLIC_ORIGIN=https://old.example

# another comment
GRAFANA_ADMIN_PASSWORD=alsokeep
EOF

    upsert_env_kv PUBLIC_ORIGIN "https://new.example"
    upsert_env_kv MIGRATIONS_EXPECTED_HEAD "abc123def456"

    local content; content="$(cat "$ROOT/docker/.env")"
    t_eq  "replaces an existing key in place" "https://new.example" "$(read_env_kv PUBLIC_ORIGIN)"
    t_eq  "leaves other keys untouched"       "keepme"              "$(read_env_kv FR_APP_PASSWORD)"
    t_eq  "leaves later keys untouched"       "alsokeep"            "$(read_env_kv GRAFANA_ADMIN_PASSWORD)"
    t_eq  "appends a new key"                 "abc123def456"        "$(read_env_kv MIGRATIONS_EXPECTED_HEAD)"
    t_contains "preserves comments" "$content" "# a comment that must survive"
    t_not_contains "does not duplicate the replaced key" \
        "$(grep -c '^PUBLIC_ORIGIN=' "$ROOT/docker/.env")" "2"

    # Idempotence: writing the same value twice changes nothing.
    local before; before="$(sha256_of "$ROOT/docker/.env")"
    upsert_env_kv PUBLIC_ORIGIN "https://new.example"
    t_eq "re-writing the same value is a no-op" "$before" "$(sha256_of "$ROOT/docker/.env")"

    # A dry run must not touch the file at all.
    local guard; guard="$(sha256_of "$ROOT/docker/.env")"
    DRY_RUN=1 upsert_env_kv PUBLIC_ORIGIN "https://SHOULD-NOT-BE-WRITTEN" >/dev/null
    t_eq "--dry-run never writes" "$guard" "$(sha256_of "$ROOT/docker/.env")"

    ROOT="$saved_root"
}

test_gpu_allocation() {
    printf '\n%sGPU detection and allocation%s\n' "$C_BOLD" "$C_RESET"

    local stub_dir="$SELF_TEST_DIR/bin"; mkdir -p "$stub_dir"
    local saved_smi="$NVIDIA_SMI"

    # 0 GPUs -> CPU mode, no overlay
    make_nvidia_stub "$stub_dir/none"
    NVIDIA_SMI="$stub_dir/none"
    t_eq "no GPU -> empty inventory" "" "$(gpu_inventory | tr -d '[:space:]')"

    # 1 GPU -> API gets it, ollama shares, no ollama block in the overlay
    make_nvidia_stub "$stub_dir/one" "0|GPU-1111|NVIDIA RTX A4000|16376"
    NVIDIA_SMI="$stub_dir/one"
    t_eq "1 GPU inventory line count" "1" "$(gpu_inventory | grep -c '|')"
    t_eq "resolves index 0 to its UUID" "GPU-1111" "$(uuid_for_index 0)"
    local one; one="$(render_gpu_overlay "GPU-1111" "")"
    t_contains "1 GPU: face_recognition pinned to the card" "$one" 'device_ids: ["GPU-1111"]'
    t_not_contains "1 GPU: no separate ollama reservation" "$one" "ollama:"
    t_contains "1 GPU: WORKERS stays 1" "$one" 'WORKERS: "1"'
    t_contains "1 GPU: overrides rather than appends" "$one" "devices: !override"

    # 2 GPUs -> API on 0, ollama on 1
    make_nvidia_stub "$stub_dir/two" "0|GPU-1111|NVIDIA RTX A4000|16376" "1|GPU-2222|NVIDIA RTX A4000|16376"
    NVIDIA_SMI="$stub_dir/two"
    t_eq "2 GPU inventory line count" "2" "$(gpu_inventory | grep -c '|')"
    t_eq "resolves the second index" "GPU-2222" "$(uuid_for_index 1)"
    local two; two="$(render_gpu_overlay "GPU-1111" "GPU-2222")"
    t_contains "2 GPUs: API on the first card"   "$two" 'device_ids: ["GPU-1111"]'
    t_contains "2 GPUs: ollama gets its own"     "$two" 'device_ids: ["GPU-2222"]'
    t_eq "2 GPUs: exactly two reservations" "2" "$(printf '%s' "$two" | grep -c 'device_ids')"

    # 3 GPUs -> unchanged assignment; the third is reported, never handed out
    make_nvidia_stub "$stub_dir/three" \
        "0|GPU-1111|NVIDIA RTX A4000|16376" "1|GPU-2222|NVIDIA RTX A4000|16376" "2|GPU-3333|NVIDIA RTX A4000|16376"
    NVIDIA_SMI="$stub_dir/three"
    t_eq "3 GPU inventory line count" "3" "$(gpu_inventory | grep -c '|')"
    local three; three="$(render_gpu_overlay "GPU-1111" "GPU-2222")"
    t_not_contains "3 GPUs: the third card is never assigned" "$three" "GPU-3333"

    # An explicit GPU_IDS request is honoured verbatim
    local explicit; explicit="$(render_gpu_overlay "$(uuid_for_index 2)" "$(uuid_for_index 0)")"
    t_contains "GPU_IDS override: API on the requested card"    "$explicit" 'device_ids: ["GPU-3333"]'
    t_contains "GPU_IDS override: ollama on the requested card" "$explicit" 'device_ids: ["GPU-1111"]'

    # The overlay is deterministic apart from its timestamp comment.
    local a b
    a="$(render_gpu_overlay "GPU-1111" "GPU-2222" | grep -v generated_at)"
    b="$(render_gpu_overlay "GPU-1111" "GPU-2222" | grep -v generated_at)"
    t_eq "overlay rendering is deterministic" "$a" "$b"

    NVIDIA_SMI="$saved_smi"
}

test_model_manifest_verification() {
    printf '\n%smodel weight verification (fail-closed)%s\n' "$C_BOLD" "$C_RESET"

    local saved_root="$ROOT"
    ROOT="$SELF_TEST_DIR/models"; mkdir -p "$ROOT/weights"

    # No manifest at all -> CONTENT_NOT_VERIFIED
    printf 'not-the-real-model' > "$ROOT/weights/det_10g.onnx"
    printf 'not-the-real-model' > "$ROOT/weights/w600k_r50.onnx"
    local out
    # ASSUME_YES is read by confirm() in lib.sh
    # shellcheck disable=SC2034
    out="$( (ASSUME_YES=1; stage_model_check) 2>&1 )" || true
    t_contains "missing manifest is CONTENT_NOT_VERIFIED" "$out" "CONTENT_NOT_VERIFIED"

    # Generate a manifest from these files, then they verify.
    ASSUME_YES=1 cmd_model_manifest >/dev/null 2>&1
    out="$( (stage_model_check) 2>&1 )" || true
    t_contains "generated manifest verifies its own files" "$out" "VERIFIED"

    # Tamper with one byte -> CHECKSUM_MISMATCH (same size, different content)
    printf 'NOT-the-real-model' > "$ROOT/weights/w600k_r50.onnx"
    out="$( (stage_model_check) 2>&1 )" || true
    t_contains "a tampered file is CHECKSUM_MISMATCH" "$out" "CHECKSUM_MISMATCH"
    t_contains "the tampered file is named" "$out" "w600k_r50.onnx"

    # Remove a file -> CONTENT_MISSING
    printf 'not-the-real-model' > "$ROOT/weights/w600k_r50.onnx"   # restore
    rm -f "$ROOT/weights/det_10g.onnx"
    out="$( (stage_model_check) 2>&1 )" || true
    t_contains "an absent file is CONTENT_MISSING" "$out" "CONTENT_MISSING"

    # Truncation (different size) is caught before the hash is even computed.
    printf 'x' > "$ROOT/weights/det_10g.onnx"
    out="$( (stage_model_check) 2>&1 )" || true
    t_contains "a truncated file is CHECKSUM_MISMATCH" "$out" "CHECKSUM_MISMATCH"

    ROOT="$saved_root"
}

test_state_file() {
    printf '\n%sdeployment state%s\n' "$C_BOLD" "$C_RESET"

    local saved_root="$ROOT"
    ROOT="$SELF_TEST_DIR/state"; mkdir -p "$ROOT"

    state_set deployed_version "v1.2.3"
    state_set migration_head "abc123"
    state_set gpu_assignment "face_recognition=GPU0 ollama=GPU1"
    t_eq "records the deployed version" "v1.2.3" "$(state_get deployed_version)"
    t_eq "records the migration head"   "abc123" "$(state_get migration_head)"
    t_eq "records the GPU assignment"   "face_recognition=GPU0 ollama=GPU1" "$(state_get gpu_assignment)"

    state_set deployed_version "v1.3.0"
    t_eq "updates a key without duplicating it" "v1.3.0" "$(state_get deployed_version)"
    t_eq "other keys survive an update" "abc123" "$(state_get migration_head)"
    t_eq "exactly one entry per key" "1" "$(grep -c '"deployed_version"' "$(state_file)")"

    state_set last_health_result "PASS"
    t_eq "records the last health result" "PASS" "$(state_get last_health_result)"

    # A failure detail is routinely multi-line. Written raw it makes the file
    # invalid JSON, and the line-based reader then drops every key after it.
    state_set last_failed_detail "$(printf 'line one
line two	tabbed')"
    t_eq "multi-line values are escaped, not embedded"         "line one\nline two\ttabbed" "$(state_get last_failed_detail)"
    t_eq "keys written before a multi-line value survive"         "abc123" "$(state_get migration_head)"
    state_set deployed_version "v1.4.0"
    t_eq "keys can still be written after a multi-line value"         "v1.4.0" "$(state_get deployed_version)"
    t_eq "the state file stays a single JSON object"         "1" "$(grep -c '^}' "$(state_file)")"
    # Pick an interpreter that actually RUNS: Windows ships a python3.exe
    # execution-alias stub that exists on PATH and does nothing.
    local py=""
    for candidate in python3 python; do
        if "$candidate" -c "import json" >/dev/null 2>&1; then py="$candidate"; break; fi
    done
    if [ -n "$py" ]; then
        if "$py" -c "import json,sys; json.load(sys.stdin)" < "$(state_file)" 2>/dev/null; then
            t_ok "state file parses as JSON after a multi-line value"
        else
            t_fail "state file parses as JSON after a multi-line value" "json.load rejected $(state_file)"
        fi
    else
        t_ok "state file JSON validation skipped (no working python)"
    fi

    ROOT="$saved_root"
}

test_fingerprint_has_no_secrets() {
    printf '\n%sconfiguration fingerprint%s\n' "$C_BOLD" "$C_RESET"

    local saved_root="$ROOT"
    ROOT="$SELF_TEST_DIR/fp"; mkdir -p "$ROOT/docker"
    printf 'FR_APP_PASSWORD=SUPERSECRETVALUE\nPUBLIC_ORIGIN=https://x\n' > "$ROOT/docker/.env"
    cp "$saved_root/docker/docker-compose.prod.yml" "$ROOT/docker/" 2>/dev/null || true

    local fp1 fp2
    fp1="$(config_fingerprint)"
    t_eq "fingerprint is short and stable" "$fp1" "$(config_fingerprint)"
    t_not_contains "fingerprint never contains a secret value" "$fp1" "SUPERSECRET"

    # Changing a secret VALUE must not change the fingerprint (keys only);
    # changing the set of keys must.
    printf 'FR_APP_PASSWORD=DIFFERENTSECRET\nPUBLIC_ORIGIN=https://x\n' > "$ROOT/docker/.env"
    t_eq "rotating a secret does not change the fingerprint" "$fp1" "$(config_fingerprint)"
    printf 'FR_APP_PASSWORD=DIFFERENTSECRET\nPUBLIC_ORIGIN=https://x\nNEW_KEY=1\n' > "$ROOT/docker/.env"
    fp2="$(config_fingerprint)"
    [ "$fp1" != "$fp2" ] && t_ok "adding a configuration key changes the fingerprint" \
        || t_fail "adding a configuration key changes the fingerprint" "both were $fp1"

    ROOT="$saved_root"
}

test_origin_parsing() {
    printf '\n%sorigin parsing%s\n' "$C_BOLD" "$C_RESET"
    t_eq "https origin"          "face-detector.internal" "$(origin_host https://face-detector.internal)"
    t_eq "origin with a port"    "face-detector.internal" "$(origin_host https://face-detector.internal:8443)"
    t_eq "origin with a path"    "face-detector.internal" "$(origin_host https://face-detector.internal/app)"
    t_eq "bare hostname"         "face-detector.internal" "$(origin_host face-detector.internal)"
    t_eq "http origin"           "10.0.0.5"               "$(origin_host http://10.0.0.5)"
}

test_merged_env_value() {
    printf '\n%smerged configuration reader%s\n' "$C_BOLD" "$C_RESET"

    # Shaped exactly like `docker compose config` output: services at indent 2,
    # environment keys at indent 6, values quoted, keys sorted.
    local rendered
    rendered="$(cat <<'EOF'
services:
  backup:
    environment:
      WORKERS: "9"
  face_recognition:
    environment:
      INFERENCE_WORKERS: "3"
      QUEUE_WORKERS: "15"
      WORKERS: "1"
    image: face-detector/api
  nginx:
    environment:
      WORKERS: "4"
EOF
)"
    t_eq "reads the value from the right service" "1" \
        "$(merged_env_value "$rendered" face_recognition WORKERS)"
    # The trap this exists for: a substring match returns INFERENCE_WORKERS=3
    # and the run passes while the service is misconfigured.
    t_eq "does not match a key that merely ends in the name" "3" \
        "$(merged_env_value "$rendered" face_recognition INFERENCE_WORKERS)"
    t_eq "does not leak into the next service" "4" \
        "$(merged_env_value "$rendered" nginx WORKERS)"
    t_eq "absent key yields nothing" "" \
        "$(merged_env_value "$rendered" face_recognition NOT_SET)"
    t_eq "absent service yields nothing" "" \
        "$(merged_env_value "$rendered" no_such_service WORKERS)"

    # An overlay that raised WORKERS must be readable as such, so the stage can
    # fail on it rather than start a second CUDA session.
    local raised="${rendered/WORKERS: \"1\"/WORKERS: \"4\"}"
    t_eq "an overridden value is reported, not the base one" "4" \
        "$(merged_env_value "$raised" face_recognition WORKERS)"
}

test_dry_run_is_inert() {
    printf '\n%s--dry-run inertness%s\n' "$C_BOLD" "$C_RESET"
    local marker="$SELF_TEST_DIR/must-not-exist"
    DRY_RUN=1 run touch "$marker" >/dev/null
    [ -f "$marker" ] && t_fail "run() honours --dry-run" "the command actually ran" || t_ok "run() honours --dry-run"
    DRY_RUN=0 run touch "$marker" >/dev/null
    [ -f "$marker" ] && t_ok "run() executes when not dry" || t_fail "run() executes when not dry" "nothing happened"
}

# ---------------------------------------------------------------------------
run_self_tests() {
    printf '%sdeploy.sh self-test%s (no Docker, no GPU, no host mutation)\n' "$C_BOLD" "$C_RESET"
    SELF_TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/deploy-selftest.XXXXXX")"
    trap 'rm -rf "$SELF_TEST_DIR"' EXIT

    test_migration_head_derivation
    test_env_editing
    test_gpu_allocation
    test_model_manifest_verification
    test_state_file
    test_fingerprint_has_no_secrets
    test_origin_parsing
    test_merged_env_value
    test_dry_run_is_inert

    printf '\n%s%d passed, %d failed%s\n' \
        "$([ "$SELF_TEST_FAIL" -eq 0 ] && printf '%s' "$C_GREEN" || printf '%s' "$C_RED")" \
        "$SELF_TEST_PASS" "$SELF_TEST_FAIL" "$C_RESET"
    [ "$SELF_TEST_FAIL" -eq 0 ]
}
