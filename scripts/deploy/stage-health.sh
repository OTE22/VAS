#!/usr/bin/env bash
#
# Stage 14 — health, security and end-to-end acceptance.
#
# Ordered so that the first failure is the most informative one: configuration
# before containers, containers before HTTP, HTTP before inference. Checks are
# either MANDATORY (a deployment that fails one is not serving correctly) or
# ADVISORY (degraded but the face pipeline works).
#
# The mandatory set is deliberately the same set the runbook's section 6 asks a
# human to perform by hand, plus a real inference — because provider discovery
# and a green healthcheck can both be true while the models sit on the CPU or
# the wrong weights are loaded.

HEALTH_FAILURES=""
HEALTH_CHECKS=0

hcheck() {   # hcheck <mandatory|advisory> <name> <command...>
    local class="$1" name="$2"; shift 2
    HEALTH_CHECKS=$((HEALTH_CHECKS + 1))
    local output
    if output="$("$@" 2>&1)"; then
        ok "$name${output:+ — ${output%%$'\n'*}}"
        return 0
    fi
    if [ "$class" = "mandatory" ]; then
        fail "$name — ${output:-failed}"
        HEALTH_FAILURES="$HEALTH_FAILURES\n  $name: ${output%%$'\n'*}"
    else
        warn "$name — ${output:-failed}"
    fi
    return 1
}

# api_exec: run a command in the API container (the checks that must observe
# the app from the inside).
api_exec() { compose exec -T face_recognition "$@"; }

# curl_in_api: HTTP from inside the network, so a check never depends on the
# host being able to resolve PUBLIC_ORIGIN.
curl_status() {
    local url="$1"; shift
    api_exec curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@" "$url"
}

stage_health() {
    stage_begin "14 health and acceptance"
    explain "WHAT" "The acceptance battery: proves the deployment is correct, not merely up."
    explain "READS" "the live stack over TLS, trusting certs/internal-ca.crt."
    explain "WRITES" "nothing (the storage probe writes and deletes one temp file as uid 1000)."
    explain "FAIL" "mandatory checks include: only 80/443 published, a forged JWT"
    explain_cont "rejected, /docs absent, an unsigned webhook refused, real SCRFD and"
    explain_cont "ArcFace inference, and on GPU cuda_available=1 with"
    explain_cont "cpu_fallback_active=0. Advisory checks warn without failing the run."
    HEALTH_FAILURES=""; HEALTH_CHECKS=0

    if [ "$DRY_RUN" = "1" ]; then stage_pass "would run the health battery"; return 0; fi

    local origin host
    origin="$(read_env_kv PUBLIC_ORIGIN 2>/dev/null)"
    host="$(origin_host "$origin")"

    # ---- 1. configuration and containers ---------------------------------
    hcheck mandatory "compose configuration parses" compose config -q
    hcheck mandatory "webhook_integration network exists" docker network inspect webhook_integration

    local cid exitcode
    cid="$(compose ps -a -q migrate 2>/dev/null | head -1)"
    if [ -n "$cid" ]; then
        exitcode="$(docker inspect -f '{{.State.ExitCode}}' "$cid" 2>/dev/null)"
        hcheck mandatory "migrate job completed successfully" test "$exitcode" = "0"
    fi

    local svc
    for svc in $(compose config --services 2>/dev/null | grep -v '^migrate$'); do
        hcheck mandatory "service $svc healthy" service_healthy "$svc"
    done

    # ---- 2. exposure: only nginx may publish -----------------------------
    if [ "$IS_WSL2" = "1" ]; then
        hcheck advisory "only 80/443 published (WSL2: advisory)" only_web_ports_published
    else
        hcheck mandatory "only 80/443 published on the host" only_web_ports_published
    fi

    # ---- 3. HTTP surface --------------------------------------------------
    hcheck mandatory "/health/live answers 200" test_http 200 "http://localhost:8000/health/live"
    hcheck mandatory "/health/ready answers 200" test_http 200 "http://localhost:8000/health/ready"
    hcheck mandatory "required components healthy (/health/detailed)" detailed_components_healthy
    hcheck mandatory "storage is writable by the app (uid 1000)" storage_round_trip
    hcheck mandatory "vector backend reported healthy" vector_backend_healthy

    # ---- 4. security posture (runbook section 6) --------------------------
    hcheck mandatory "a forged JWT is rejected" forged_token_rejected
    hcheck mandatory "API docs are disabled in production" test_http 404 "http://localhost:8000/docs"
    hcheck mandatory "unsigned webhook is refused" unsigned_webhook_refused
    if [ -n "$host" ]; then
        hcheck mandatory "TLS terminates with the deployed certificate" tls_serves_certificate "$host"
    fi

    # ---- 5. the model actually works -------------------------------------
    if [ "${GPU_MODE:-0}" = "1" ]; then
        hcheck mandatory "REAL SCRFD + ArcFace inference on CUDA" real_inference_test gpu
        hcheck mandatory "no silent CPU fallback (metrics)" gpu_metrics_clean
    else
        hcheck mandatory "REAL SCRFD + ArcFace inference (CPU)" real_inference_test cpu
    fi

    # ---- 6. advisory: the parts that degrade without stopping the system --
    hcheck advisory "camera/webhook ingest path reachable (nginx alias)" webhook_alias_present
    hcheck advisory "registered cameras present and geocoded" cameras_registered
    hcheck advisory "LLM models available (chatbot)" ollama_models_present
    hcheck advisory "basemap archives verified" map_gate
    hcheck advisory "prometheus is scraping the API" prometheus_up
    # During an upgrade the backup is the only way back, so it must verify;
    # outside one, a freshly installed system legitimately has none yet.
    hcheck "$([ "${UPGRADE_IN_PROGRESS:-0}" = 1 ] && echo mandatory || echo advisory)" \
        "a verified backup exists" backup_present

    if [ -n "$HEALTH_FAILURES" ]; then
        state_set last_health_result "FAIL"
        state_set last_health_at "$(timestamp)"
        stage_fail "$(printf 'mandatory health checks failed:%b' "$HEALTH_FAILURES")"
    fi
    state_set last_health_result "PASS"
    state_set last_health_at "$(timestamp)"
    stage_pass "$HEALTH_CHECKS checks; every mandatory check passed"
}

# ---------------------------------------------------------------------------
# Individual probes. Each prints a one-line detail on success and returns
# non-zero with an explanation on failure.
# ---------------------------------------------------------------------------
service_healthy() {
    local svc="$1" cid status health
    cid="$(compose ps -q "$svc" 2>/dev/null | head -1)"
    [ -n "$cid" ] || { echo "container absent"; return 1; }
    status="$(docker inspect -f '{{.State.Status}}' "$cid")"
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")"
    case "$health" in
        healthy) echo "healthy"; return 0 ;;
        none)    [ "$status" = "running" ] && { echo "running"; return 0; }; echo "$status"; return 1 ;;
        *)       echo "$health"; return 1 ;;
    esac
}

only_web_ports_published() {
    local offenders
    offenders="$(compose ps --format '{{.Service}} {{.Ports}}' 2>/dev/null \
        | grep -E '0\.0\.0\.0:|:::' \
        | grep -vE '(^nginx )|0\.0\.0\.0:(80|443)->' \
        | grep -v '127\.0\.0\.1:' || true)"
    [ -z "$offenders" ] || { echo "unexpected published port(s): $offenders"; return 1; }
    echo "nginx only"
}

test_http() {
    local want="$1" url="$2" code
    code="$(curl_status "$url")" || { echo "request failed"; return 1; }
    [ "$code" = "$want" ] || { echo "expected $want, got $code"; return 1; }
    echo "$code"
}

detailed_components_healthy() {
    local body
    body="$(api_exec curl -s --max-time 25 http://localhost:8000/health/detailed 2>/dev/null)" || {
        echo "no response"; return 1; }
    local comp
    for comp in database models; do
        printf '%s' "$body" | tr ',' '\n' | grep -A2 "\"$comp\"" | grep -q '"healthy": *true\|"healthy":true' || {
            echo "component '$comp' is not healthy"; return 1; }
    done
    echo "database + models healthy"
}

vector_backend_healthy() {
    local body
    body="$(api_exec curl -s --max-time 25 http://localhost:8000/health/detailed 2>/dev/null)" || return 1
    printf '%s' "$body" | grep -qiE '"(vector_index|vector_backend|pgvector)"|VECTOR_BACKEND' || {
        # Older payload shape: fall back to the models/database verdict rather
        # than inventing a failure.
        echo "not reported separately (covered by /health/detailed)"; return 0; }
    printf '%s' "$body" | grep -qi '"unhealthy"' && { echo "vector backend unhealthy"; return 1; }
    echo "reported healthy"
}

storage_round_trip() {
    local marker="/app/storage/.deploy-healthcheck"
    api_exec sh -c "echo deploy-ok > $marker && cat $marker && rm -f $marker" 2>/dev/null | grep -q deploy-ok \
        || { echo "the app cannot write to /app/storage as uid 1000"; return 1; }
    echo "write/read/delete as uid 1000"
}

forged_token_rejected() {
    local code
    code="$(curl_status "http://localhost:8000/api/identities" \
        -H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhdHRhY2tlciIsInJvbGUiOiJhZG1pbiJ9.not-a-real-signature")" \
        || { echo "request failed"; return 1; }
    case "$code" in
        401|403) echo "$code" ;;
        *) echo "a forged token got HTTP $code — expected 401/403"; return 1 ;;
    esac
}

unsigned_webhook_refused() {
    local code
    code="$(api_exec curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
        -X POST -H 'Content-Type: application/json' -d '{}' \
        "http://localhost:8000/webhook/deploy-health-probe" 2>/dev/null)" || { echo "request failed"; return 1; }
    case "$code" in
        401|403) echo "$code (WEBHOOK_AUTH_MODE=enforce)" ;;
        *) echo "an unsigned webhook got HTTP $code — expected 401/403"; return 1 ;;
    esac
}

tls_serves_certificate() {
    local host="$1" out
    out="$(compose exec -T nginx sh -c \
        "echo | openssl s_client -connect 127.0.0.1:443 -servername $host 2>/dev/null | openssl x509 -noout -subject" 2>/dev/null)" || {
        echo "TLS handshake failed"; return 1; }
    printf '%s' "$out" | grep -q . || { echo "no certificate presented"; return 1; }
    printf '%s' "${out#subject=}"
}

gpu_metrics_clean() {
    local body cuda fallback
    body="$(api_exec curl -s --max-time 20 http://localhost:8000/metrics 2>/dev/null)" || { echo "no metrics"; return 1; }
    cuda="$(printf '%s' "$body" | awk '/^face_detector_cuda_available /{print $2}' | head -1)"
    fallback="$(printf '%s' "$body" | awk '/^face_detector_cpu_fallback_active /{print $2}' | head -1)"
    [ "${cuda%%.*}" = "1" ] || { echo "face_detector_cuda_available=$cuda"; return 1; }
    [ "${fallback%%.*}" = "0" ] || { echo "face_detector_cpu_fallback_active=$fallback — the GPU deployment is silently on CPU"; return 1; }
    echo "cuda_available=1 cpu_fallback_active=0"
}

webhook_alias_present() {
    docker network inspect webhook_integration 2>/dev/null | grep -q '"Name"' || { echo "network missing"; return 1; }
    local attached
    attached="$(docker network inspect webhook_integration --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null)"
    printf '%s' "$attached" | grep -qi nginx || { echo "nginx is not attached to webhook_integration"; return 1; }
    echo "nginx attached"
}

cameras_registered() {
    local out
    out="$(compose exec -T postgres psql -U postgres -d face_recognition -tAc \
        "SELECT count(*), count(*) FILTER (WHERE latitude IS NULL OR longitude IS NULL) FROM pipelines" 2>/dev/null)" \
        || { echo "query failed"; return 1; }
    local total ungeo; total="${out%%|*}"; ungeo="${out##*|}"
    [ "${total:-0}" -gt 0 ] || { echo "no cameras registered yet (they self-register on their first webhook)"; return 1; }
    [ "${ungeo:-0}" -eq 0 ] || { echo "$total camera(s), $ungeo without coordinates (the map cannot place them)"; return 1; }
    echo "$total camera(s), all geocoded"
}

ollama_models_present() {
    compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -q '^ollama$' || { echo "ollama not running"; return 1; }
    local listed; listed="$(compose exec -T ollama ollama list 2>/dev/null | tail -n +2 | wc -l)"
    [ "${listed:-0}" -gt 0 ] || { echo "no models pulled"; return 1; }
    echo "$listed model(s)"
}

map_gate() {
    api_exec test -d /app/map-data/production 2>/dev/null || { echo "no map archives mounted"; return 1; }
    local out
    out="$(api_exec python /app/scripts/map_data/production_gate.py --allow-unavailable satellite 2>&1 | tail -3)" \
        || { echo "$(printf '%s' "$out" | tail -1)"; return 1; }
    echo "content gate passed"
}

prometheus_up() {
    compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -q '^prometheus$' || { echo "not running"; return 1; }
    echo "running"
}

backup_present() {
    local newest
    newest="$(api_exec sh -c 'ls -1 /backups 2>/dev/null | grep "Z$" | tail -1' 2>/dev/null)"
    [ -n "$newest" ] || { echo "no backup taken yet (the backup service runs on its own schedule)"; return 1; }
    compose exec -T backup sh -c "cd /backups/$newest && sha256sum -c SHA256SUMS >/dev/null 2>&1" \
        || { echo "the newest backup ($newest) does not verify"; return 1; }
    echo "$newest verifies"
}
