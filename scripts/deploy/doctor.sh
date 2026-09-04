# ---------------------------------------------------------------------------
# ./deploy.sh doctor — one read-only command that answers "what is wrong?"
#
# Written after a first production deployment where the answer was scattered:
# a container reported "GPU unavailable" for a bad tensor shape, redis said
# nothing while blocking the whole app tier, migrate exited 78 with its real
# reason four screens up, and `docker compose up` silently skipped the two
# stages that create secrets and directories.
#
# Every check here is READ-ONLY and prints the exact command that fixes what
# it found. Nothing is repaired implicitly — a diagnostic that mutates is a
# diagnostic you cannot trust to tell you the truth twice.
#
# Ordered by dependency: a failure early explains failures later, so read the
# FIRST problem, not the last.
# ---------------------------------------------------------------------------

DOCTOR_PROBLEMS=0

# Can this process actually SEE the things it is about to judge? docker/.env is
# 0600 root and the daemon socket is root:docker, so an unprivileged run cannot
# read either — and a check that cannot see its subject must say so, not report
# it as broken. The first version of this file reported 9 false problems
# (including "nothing is running" one line above "the site serves HTTP 200").
DOCTOR_CAN_READ_ENV=0
DOCTOR_CAN_SEE_DOCKER=0
[ -r "$ROOT/docker/.env" ] && DOCTOR_CAN_READ_ENV=1
docker info >/dev/null 2>&1 && DOCTOR_CAN_SEE_DOCKER=1
_d_ok()   { printf '  %-42s %sok%s\n' "$1" "$C_GREEN" "$C_RESET"; }
_d_bad()  { printf '  %-42s %s%s%s\n' "$1" "$C_RED" "$2" "$C_RESET"
            [ -n "${3:-}" ] && printf '       fix: %s\n' "$3"
            DOCTOR_PROBLEMS=$((DOCTOR_PROBLEMS + 1)); }
_d_skip() { printf '  %-42s %s? cannot check%s  (%s)\n' "$1" "$C_DIM" "$C_RESET" "$2"; }
_d_warn() { printf '  %-42s %s%s%s\n' "$1" "$C_YELLOW" "$2" "$C_RESET"
            [ -n "${3:-}" ] && printf '       note: %s\n' "$3"; }

# --- 1. the env file compose actually reads --------------------------------
#
# Compose reads .env from the PROJECT directory (docker/), never the repo
# root. A variable referenced as ${FOO:?...} that is missing aborts the whole
# render with a message that names the variable but not the file.
doctor_env() {
    printf '\n%s1. docker/.env — the file compose actually reads%s\n' "$C_BLUE" "$C_RESET"
    local envf="$ROOT/docker/.env"
    if [ "$DOCTOR_CAN_READ_ENV" = "0" ]; then
        if [ -e "$envf" ]; then
            _d_skip "docker/.env contents" "0600 root — re-run: sudo ./deploy.sh doctor"
        else
            _d_bad "docker/.env" "MISSING" "sudo ./deploy.sh install   (stage 04 writes it)"
        fi
        return
    fi
    if [ ! -f "$envf" ]; then
        _d_bad "docker/.env" "MISSING" "sudo ./deploy.sh install   (stage 04 writes it)"
        return
    fi
    _d_ok "docker/.env exists"

    # Every ${VAR} the compose files reference must be resolvable.
    local referenced missing="" var
    referenced="$(grep -ohE '\$\{[A-Z_][A-Z0-9_]*' \
        "$ROOT/docker/docker-compose.prod.yml" \
        "$ROOT/docker/docker-compose.prod.gpu.yml" 2>/dev/null \
        | sed 's/\${//' | sort -u)"
    for var in $referenced; do
        # ${FOO:-default} is satisfied by its default; VAR is the literal
        # placeholder used in this file's own documentation comments. Only a
        # variable with NO default and NO value is actually missing.
        [ "$var" = "VAR" ] && continue
        # -F, not -E: in a double-quoted bash string \$ becomes a literal $,
        # which ERE then reads as an end-of-line anchor, so the default-value
        # test silently never matched and three optional vars were reported
        # missing.
        grep -qF "\${${var}:-" "$ROOT/docker/docker-compose.prod.yml" \
             "$ROOT/docker/docker-compose.prod.gpu.yml" 2>/dev/null && continue
        grep -qE "^${var}=" "$envf" 2>/dev/null || missing="$missing $var"
    done
    if [ -n "$missing" ]; then
        _d_bad "variables referenced but not set" "$(echo $missing | tr ' ' ',')" \
               "add them to docker/.env, or re-run sudo ./deploy.sh install"
    else
        _d_ok "every referenced variable is set"
    fi

    # The other direction: a variable SET here but referenced by nothing is how
    # a rename hides. db/roles.sql once read :app_password while .env defined
    # FR_APP_PASSWORD; both files looked correct in isolation and the role was
    # created with a literal password. A dangling name is the visible symptom.
    local dangling="" v
    for v in $(grep -oE '^[A-Z_][A-Z0-9_]*=' "$envf" 2>/dev/null | tr -d '='); do
        printf '%s\n' "$referenced" | grep -qx "$v" && continue
        grep -rqs -- "$v" "$ROOT/scripts" "$ROOT/deploy.sh" "$ROOT/db" && continue
        dangling="$dangling $v"
    done
    if [ -n "$dangling" ]; then
        _d_warn "set in .env, referenced nowhere" "$(echo $dangling | tr ' ' ',')" \
                "harmless, but usually a rename that left one side behind"
    else
        _d_ok "no dangling variables"
    fi

    # Empty values are the subtler failure: compose renders, the app starts,
    # and something authenticates with an empty password.
    # grep -c prints 0 and EXITS 1 when it matches nothing, so `|| echo 0`
    # appended a second zero and the && / || chain then reported a clean file
    # as a problem. Count with an explicit if.
    local empties
    empties="$(grep -cE '^[A-Z_]+=$' "$envf" 2>/dev/null)"
    [ -n "$empties" ] || empties=0
    if [ "$empties" -eq 0 ]; then
        _d_ok "no empty values"
    else
        _d_bad "empty values in docker/.env" "$empties" "grep -nE '^[A-Z_]+=\$' docker/.env"
    fi
}

# --- 2. secrets and paths --------------------------------------------------
doctor_paths() {
    printf '\n%s2. paths and secrets (scripts/deploy/paths.sh is the manifest)%s\n' "$C_BLUE" "$C_RESET"
    if verify_deployment_paths >/dev/null 2>&1; then
        _d_ok "all paths match the manifest"
    else
        _d_bad "path/permission drift" "see ./deploy.sh paths" \
               "sudo ./deploy.sh install   (stage 03 re-applies the manifest)"
    fi
}

# --- 3. containers ---------------------------------------------------------
#
# A container that is Up-but-unhealthy blocks everything that waits on it,
# and compose reports that as a generic "dependency failed to start".
doctor_containers() {
    printf '\n%s3. containers%s\n' "$C_BLUE" "$C_RESET"
    if [ "$DOCTOR_CAN_SEE_DOCKER" = "0" ]; then
        _d_skip "container states" "no daemon access — re-run: sudo ./deploy.sh doctor"
        return
    fi
    local expected="postgres redis ollama martin face_recognition ml_worker nginx"
    local name status
    for name in $expected; do
        status="$(docker ps --filter "name=face_detector_prod-${name}-1" \
                  --format '{{.Status}}' 2>/dev/null | head -1)"
        case "$status" in
            "")            _d_bad  "$name" "NOT RUNNING" "sudo ./deploy.sh start; then: sudo docker logs face_detector_prod-${name}-1" ;;
            *unhealthy*)   _d_bad  "$name" "UNHEALTHY"   "sudo docker logs --tail 50 face_detector_prod-${name}-1" ;;
            *health:\ starting*) _d_warn "$name" "starting" "give it a minute; models load on first boot" ;;
            *)             _d_ok   "$name" ;;
        esac
    done
}

# --- 4. does it actually serve? --------------------------------------------
doctor_endpoint() {
    printf '\n%s4. serving%s\n' "$C_BLUE" "$C_RESET"
    local code
    code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 https://localhost/health/live 2>/dev/null)"
    case "$code" in
        200) _d_ok "https://localhost/health/live -> 200" ;;
        000) _d_bad "https endpoint" "no response" "check nginx: sudo docker logs face_detector_prod-nginx-1" ;;
        *)   _d_bad "https endpoint" "HTTP $code" "sudo docker logs --tail 50 face_detector_prod-face_recognition-1" ;;
    esac
}

# --- 5. assets that must be SUPPLIED, not generated ------------------------
doctor_assets() {
    printf '\n%s5. supplied assets%s\n' "$C_BLUE" "$C_RESET"
    local m="$ROOT/map-data/metadata/checksums.txt" name missing=""
    if [ -f "$m" ]; then
        while read -r _sum name; do
            [ -n "$name" ] || continue
            [ -f "$ROOT/map-data/production/$name" ] || missing="$missing $name"
        done < "$m"
    fi
    [ -d "$ROOT/map-data/production/fonts" ] || missing="$missing fonts/"
    [ -z "$missing" ] && _d_ok "map archives and glyphs present" \
        || _d_bad "map data" "missing:$missing" "copy them to map-data/production/ (martin exits without fonts/)"

    # Present is not the same as USABLE. The map gate fails closed: an archive
    # with no recorded verdict is reported UNAVAILABLE, so all four basemaps go
    # dark while every file sits correctly on disk and nothing looks wrong here.
    if [ -f "$ROOT/map-data/metadata/content_verdicts.json" ]; then
        _d_ok "map datasets content-verified"
    elif [ -d "$ROOT/map-data/production" ]; then
        _d_bad "map content verdicts" "MISSING — basemaps will be UNAVAILABLE" \
               "sudo docker run --rm --network none -v \"$ROOT/map-data\":/app/map-data:rw --entrypoint python face_detector_prod-face_recognition:latest -c \"import sys;sys.path.insert(0,'/app');from backend.core import map_content_ledger as l;l.verify_installed(verifier='operator')\" && sudo chown 1000:1000 map-data/metadata/content_verdicts.json"
    fi

    if [ "$DOCTOR_CAN_SEE_DOCKER" = "0" ]; then
        _d_skip "ollama models" "no daemon access — re-run: sudo ./deploy.sh doctor"
        return
    fi
    local want got_models
    got_models="$(docker exec face_detector_prod-ollama-1 ollama list 2>/dev/null | tail -n +2 | awk '{print $1}')"
    for want in "$(grep -oE 'OLLAMA_MODEL: .*' "$ROOT/docker/docker-compose.prod.yml" | head -1 | sed 's/OLLAMA_MODEL: //')" \
                "$(grep -oE 'OLLAMA_SQL_MODEL: .*' "$ROOT/docker/docker-compose.prod.yml" | head -1 | sed 's/OLLAMA_SQL_MODEL: //')"; do
        [ -n "$want" ] || continue
        if printf '%s\n' "$got_models" | grep -qF "$want"; then
            _d_ok "ollama model $want"
        else
            _d_bad "ollama model $want" "NOT PULLED" \
                   "sudo docker exec face_detector_prod-ollama-1 ollama pull $want   (chat/SQL only; face pipeline unaffected)"
        fi
    done
}

# --- 0. which daemon are we talking to? ------------------------------------
#
# `docker ps` returning an EMPTY table is not the same as "nothing is running".
# Docker Desktop installs its own engine and switches the CLI context to it;
# that engine has none of these containers, so every check below reports
# NOT RUNNING while the stack is serving traffic. This exact confusion cost
# real time during the first deployment.
doctor_daemon() {
    printf '\n%s0. docker daemon%s\n' "$C_BLUE" "$C_RESET"
    if [ "$DOCTOR_CAN_SEE_DOCKER" = "0" ]; then
        _d_skip "daemon identity" "no daemon access — re-run: sudo ./deploy.sh doctor"
        return
    fi
    local rootdir ctx
    rootdir="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
    ctx="$(docker context show 2>/dev/null)"
    case "$rootdir" in
        */desktop/*|*/.docker/*)
            _d_bad "connected to Docker Desktop (context=$ctx)" "WRONG ENGINE" \
                   "docker context use default    # Desktop's engine does not run this stack" ;;
        "")
            _d_bad "daemon" "no root dir reported" "sudo systemctl status docker" ;;
        *)
            _d_ok "daemon root $rootdir (context=$ctx)" ;;
    esac
}

# --- 6. can a browser actually reach it? -----------------------------------
#
# Every check above can pass while login is impossible. The app enforces an
# Origin check against PUBLIC_ORIGIN, so a browser must arrive at that exact
# name — and on a fresh host that name resolves nowhere. curl -k against
# localhost hides this completely, which is why it is checked separately.
doctor_access() {
    printf '\n%s6. browser access%s\n' "$C_BLUE" "$C_RESET"
    if [ "$DOCTOR_CAN_READ_ENV" = "0" ]; then
        _d_skip "PUBLIC_ORIGIN reachability" "cannot read docker/.env — re-run with sudo"
        return
    fi
    local origin host
    origin="$(grep '^PUBLIC_ORIGIN=' "$ROOT/docker/.env" 2>/dev/null | cut -d= -f2-)"
    host="$(printf '%s' "$origin" | sed -E 's#^https?://##; s#[:/].*##')"
    if [ -z "$host" ]; then
        _d_bad "PUBLIC_ORIGIN" "not set" "add PUBLIC_ORIGIN=https://face-detector.internal to docker/.env"
        return
    fi
    if getent hosts "$host" >/dev/null 2>&1; then
        _d_ok "$host resolves"
    else
        _d_bad "$host does not resolve" "LOGIN WILL FAIL" \
               "echo '127.0.0.1 $host' | sudo tee -a /etc/hosts"
    fi
    local code
    code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "$origin/health/live" 2>/dev/null)"
    [ "$code" = "200" ] && _d_ok "$origin/health/live -> 200" \
        || _d_bad "$origin/health/live" "HTTP $code" "check /etc/hosts and the nginx container"
}

doctor_run() {
    printf '%sFACE_DETECTOR doctor — read-only. Nothing below changes the host.%s\n' "$C_DIM" "$C_RESET"
    if [ "$DOCTOR_CAN_SEE_DOCKER" = "0" ] || [ "$DOCTOR_CAN_READ_ENV" = "0" ]; then
        printf '%s  running unprivileged — some checks are skipped, not failed.%s\n' "$C_YELLOW" "$C_RESET"
        printf '%s  for the full picture: sudo ./deploy.sh doctor%s\n' "$C_YELLOW" "$C_RESET"
    fi
    doctor_daemon
    doctor_env
    doctor_paths
    doctor_containers
    doctor_endpoint
    doctor_assets
    doctor_access
    printf '\n'
    if [ "$DOCTOR_PROBLEMS" -eq 0 ]; then
        printf '%s  no problems found%s\n' "$C_GREEN" "$C_RESET"
        return 0
    fi
    printf '%s  %d problem(s) — fix the FIRST one; later ones are often its consequence%s\n' \
        "$C_RED" "$DOCTOR_PROBLEMS" "$C_RESET"
    return 1
}
