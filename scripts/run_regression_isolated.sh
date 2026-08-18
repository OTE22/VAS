#!/usr/bin/env bash
# =============================================================================
# Full regression on a fully ISOLATED stack — the only supported acceptance run.
#
#   scripts/run_regression_isolated.sh [pytest args...]        (default: tests/ -q)
#
# SELF-CONTAINED. The stack brings up its own PostgreSQL, Redis, API, Martin and
# nginx on a private network. NO development container needs to be running, and
# the run proves it rather than assuming it:
#
#   docker compose -f docker/docker-compose.cpu.yml stop
#   scripts/run_regression_isolated.sh
#
# It used to create a scratch database on the DEVELOPER'S postgres and join the
# dev network, starting only redis + api — so every map assertion was really
# measuring the dev Martin serving the dev map-data, and the run could not
# start at all when the dev stack was down. A regression that validates another
# stack's data proves nothing about the code under test.
#
# In order (teardown is armed FIRST and runs on every exit path — migration
# failure, isolation failure, pytest failure, container crash, Ctrl-C):
#
#   1. build the immutable map fixtures (map-data-test/) if they are absent
#   2. docker compose -p fr_regression_<id> up: postgres, redis, martin, api,
#      nginx — private network, ephemeral volumes, the API's own startup runs
#      `alembic upgrade head` against its own database
#   3. isolation assertions INSIDE the container, PLUS host-side proof that the
#      regression volumes are not the dev directories, PLUS proof that not one
#      container in this project is a dev container
#      -> any failure ABORTS before pytest
#   4. pytest inside the regression container
#   5. teardown: compose down -v; then, if the dev stack happens to be running,
#      a final check that its database list, redis and storage are unchanged.
#
# The development database, redis, storage and models are never touched — and
# with this stack they are never even reachable.
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export MSYS_NO_PATHCONV=1

DEV_DB_CONTAINER="${DEV_DB_CONTAINER:-face_recognition_db}"
DEV_REDIS_CONTAINER="${DEV_REDIS_CONTAINER:-face_recognition_redis}"
DEV_API_CONTAINER="${DEV_API_CONTAINER:-face_recognition_api}"
ID="$$_$(head -c 4 /dev/urandom | od -An -tx1 | tr -dc a-f0-9)"
export REGRESSION_ID="$ID"
PROJECT="fr_regression_${ID}"
API="fr_regression_${ID}_api"
COMPOSE=(docker compose -p "$PROJECT" -f docker/docker-compose.regression.yml)
SERVICES=(postgres_regression redis_regression martin_regression face_recognition_regression nginx_regression)
LOG_DIR="${REGRESSION_LOG_DIR:-$ROOT/logs/regression}"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/regression_${ID}.log"

echo "== regression id $ID  log $RUN_LOG"

# ---- baseline of the dev side, IF it is running. The point of this stack is
#      that it does not need the dev stack; when the dev stack is up anyway,
#      still prove nothing of its state moved.
dev_running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
psql_dev() { docker exec "$DEV_DB_CONTAINER" psql -U postgres -d postgres -Atc "$1" 2>/dev/null; }
redis_dev() { docker exec "$DEV_REDIS_CONTAINER" redis-cli "$@" 2>/dev/null; }

DEV_DBS_BEFORE=""; DEV_REDIS_BEFORE=""
if dev_running "$DEV_DB_CONTAINER"; then
  DEV_DBS_BEFORE="$(psql_dev "SELECT string_agg(datname, ',' ORDER BY datname) FROM pg_database")"
fi
if dev_running "$DEV_REDIS_CONTAINER"; then
  DEV_REDIS_BEFORE="$(redis_dev DBSIZE | tr -d '\r')"
fi
DEV_STORAGE_MARKERS_BEFORE="$(find storage models/ml -maxdepth 2 -name '.regression_marker_*' 2>/dev/null | wc -l)"

STATUS=1
teardown() {
  set +e
  echo "== teardown ($PROJECT)"
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
  if [ -n "$DEV_DBS_BEFORE" ]; then
    local dbs_after; dbs_after="$(psql_dev "SELECT string_agg(datname, ',' ORDER BY datname) FROM pg_database")"
    echo "   dev databases unchanged: $([ "$dbs_after" = "$DEV_DBS_BEFORE" ] && echo YES || echo "NO ($DEV_DBS_BEFORE -> $dbs_after)")"
  else
    echo "   dev postgres was not running: nothing of it could be touched"
  fi
  if [ -n "$DEV_REDIS_BEFORE" ]; then
    local redis_after; redis_after="$(redis_dev DBSIZE | tr -d '\r')"
    if [ "$redis_after" = "$DEV_REDIS_BEFORE" ]; then
      echo "   dev redis DBSIZE unchanged: YES ($redis_after)"
    else
      # Informational, NOT a failure. The regression stack cannot resolve the
      # dev redis at all — that is asserted before pytest runs — so a delta
      # here is the running dev stack expiring its own keys, not this run
      # writing to it. Left in because a SURPRISING delta is still worth
      # seeing; with the dev stack stopped (the supported acceptance run) the
      # question does not arise.
      echo "   dev redis DBSIZE moved ($DEV_REDIS_BEFORE -> $redis_after) — the dev stack"
      echo "     was live during this run; the regression cannot reach it (asserted above)"
    fi
  else
    echo "   dev redis was not running: nothing of it could be touched"
  fi
  local markers_after; markers_after="$(find storage models/ml -maxdepth 2 -name '.regression_marker_*' 2>/dev/null | wc -l)"
  echo "   dev storage/models untouched by markers: $([ "$markers_after" = "$DEV_STORAGE_MARKERS_BEFORE" ] && echo YES || echo NO)"
  echo "== regression exit status $STATUS (log: $RUN_LOG)"
  exit "$STATUS"
}
trap teardown EXIT INT TERM

# ---- 1. immutable map fixtures. Built with the dev API when it is available,
#         otherwise with the regression API once it is up (step 2b).
if [ ! -f "$ROOT/map-data-test/metadata/content_verdicts.json" ]; then
  if dev_running "$DEV_API_CONTAINER"; then
    echo "== building map fixtures (map-data-test/)"
    docker exec "$DEV_API_CONTAINER" python3 /app/scripts/map_data/make_test_fixtures.py \
      >>"$RUN_LOG" 2>&1 || { echo "fixture build failed (see $RUN_LOG)"; tail -20 "$RUN_LOG"; exit 1; }
  else
    echo "== map fixtures absent and the dev API is not running; building them"
    echo "   inside the regression container after it starts"
    mkdir -p "$ROOT/map-data-test/production" "$ROOT/map-data-test/metadata"
  fi
fi

# ---- 2. stack up (the API's own startup migrates its own database)
echo "== starting the isolated stack (${SERVICES[*]})"
"${COMPOSE[@]}" up -d --no-build "${SERVICES[@]}" >>"$RUN_LOG" 2>&1 || {
  echo "compose up failed (see $RUN_LOG)"; tail -30 "$RUN_LOG"; exit 1; }

# ---- 2b. fixtures, if they could not be built before the stack existed
if [ ! -f "$ROOT/map-data-test/metadata/content_verdicts.json" ]; then
  for i in $(seq 1 60); do
    docker exec "$API" true 2>/dev/null && break
    sleep 2
  done
  docker exec "$API" python3 /app/scripts/map_data/make_test_fixtures.py >>"$RUN_LOG" 2>&1 \
    || { echo "fixture build failed inside the regression container (see $RUN_LOG)"; exit 1; }
  # Martin discovers archives at startup only (1.13.0 has no hot reload, proven),
  # so it has to be restarted now that the fixtures exist.
  "${COMPOSE[@]}" restart martin_regression >>"$RUN_LOG" 2>&1
fi

echo "== waiting for the regression API to become healthy"
for i in $(seq 1 90); do
  if docker exec "$API" curl -s -m 3 http://localhost:8000/health/detailed 2>/dev/null | grep -q '"database"'; then break; fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$API" 2>/dev/null)" != "true" ]; then
    echo "regression API container exited during startup:"; docker logs "$API" 2>&1 | tail -40; exit 1
  fi
  sleep 4
done
docker exec "$API" curl -s -m 3 http://localhost:8000/health/detailed 2>/dev/null | grep -q '"database"' \
  || { echo "regression API never became healthy:"; docker logs "$API" 2>&1 | tail -40; exit 1; }
docker logs "$API" 2>&1 | grep -E "schema verified at Alembic head|feature definitions verified" | tail -2

# ---- 3. isolation assertions (abort before pytest)
echo "== isolation assertions"
docker exec -w /app "$API" python scripts/regression_isolation_check.py \
    --expect-id "$ID" --expect-db face_recognition \
    --dev-redis-host redis --dev-db face_recognition \
  || { echo "ABORT: isolation assertions failed"; exit 1; }

# 3a. the regression volumes are not the dev directories
docker exec "$API" sh -c "touch /app/storage/.regression_marker_$ID /app/models/ml/.regression_marker_$ID" \
  || { echo "ABORT: cannot write regression marker"; exit 1; }
if [ -e "storage/.regression_marker_$ID" ] || [ -e "models/ml/.regression_marker_$ID" ]; then
  echo "ABORT: regression storage/ml volumes are the dev directories"; exit 1
fi
echo "   storage/ml markers invisible on the dev side: OK"

# 3b. NO DEV CONTAINER IS PART OF THIS RUN. Every container the regression API
#     can reach must belong to this compose project — the assertion that makes
#     "self-contained" a measured fact rather than a claim about the yaml.
PROJECT_CONTAINERS="$("${COMPOSE[@]}" ps -q | while read -r cid; do docker inspect -f '{{.Name}}' "$cid"; done | tr -d '/' | sort)"
echo "   containers in this project:"; echo "$PROJECT_CONTAINERS" | sed 's/^/     /'
if echo "$PROJECT_CONTAINERS" | grep -qv "^fr_regression_${ID}_"; then
  echo "ABORT: a container outside this regression project is part of the run"; exit 1
fi
NET_ID="$(docker network ls --filter "name=${PROJECT}_regression" -q | head -1)"
[ -n "$NET_ID" ] || { echo "ABORT: the private regression network does not exist"; exit 1; }
ATTACHED="$(docker network inspect "$NET_ID" -f '{{range .Containers}}{{.Name}} {{end}}' | tr ' ' '\n' | grep -v '^$' | sort)"
echo "   attached to the private network:"; echo "$ATTACHED" | sed 's/^/     /'
if echo "$ATTACHED" | grep -qv "^fr_regression_${ID}_"; then
  echo "ABORT: a foreign container is attached to the regression network"; exit 1
fi
if docker exec "$API" getent hosts face_recognition 2>/dev/null | grep -q .; then
  RESOLVED="$(docker exec "$API" getent hosts face_recognition | awk '{print $1}' | sort -u)"
  OWN="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$API" | tr ' ' '\n' | grep -v '^$' | sort -u)"
  if [ "$RESOLVED" != "$OWN" ]; then
    echo "ABORT: 'face_recognition' resolves to [$RESOLVED] but this API is [$OWN] — a dev container is in scope"; exit 1
  fi
fi
echo "   no development container or network is involved: OK"

# 3c. the map data under test is the FIXTURE tree, not the developer's
docker exec "$API" python3 -c "
import sys
sys.path.insert(0, '/app')
from config import settings
root = settings.MAP_DATA_DIR
assert root.rstrip('/').endswith('map-data-test'), f'MAP_DATA_DIR is {root}, not the fixtures'
print('   map data under test: ' + settings.MAP_PRODUCTION_DIR)
print('   content ledger:      ' + settings.MAP_CONTENT_LEDGER_PATH)
" || { echo "ABORT: the regression stack is not using the map fixtures"; exit 1; }

# ---- 4. pytest
ARGS=("$@"); [ ${#ARGS[@]} -eq 0 ] && ARGS=(tests/ -q)
echo "== pytest ${ARGS[*]}"
docker exec -w /app "$API" python -m pytest "${ARGS[@]}" -p no:cacheprovider 2>&1 | tee -a "$RUN_LOG"
STATUS=${PIPESTATUS[0]}
echo "== pytest exit $STATUS"
