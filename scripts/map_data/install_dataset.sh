#!/usr/bin/env bash
# Install or replace a production map dataset as one crash-safe transaction,
# then verify Martin serves it. This is the only supported way to put a dataset
# into map-data/production on a running system.
#
#   scripts/map_data/install_dataset.sh <built-archive.mbtiles.new> <dataset-id> [--restart-martin]
#   scripts/map_data/install_dataset.sh --rollback <dataset-id>
#
# The transaction itself lives in scripts/map_data/install_dataset.py. It is
# Python and not shell for one reason: every step has a crash window that has
# to be PROVEN safe, and the fault-injection hook that proves it
# (MAP_INSTALL_FAIL_AT) cannot be driven through a pipeline of cp/mv. The
# ordering guarantee it implements:
#
#   stage -> validate+hash -> PENDING verdict -> retain previous -> rename ->
#   activate verdict + catalogs -> drop previous
#
# so that a crash leaves either the OLD archive serving and available, or the
# new bytes in place and reported UNAVAILABLE (CONTENT_NOT_VERIFIED) — never
# new bytes wearing the old archive's authorization. See that module's header.
#
# Validation is structure AND content, before anything is copied: the
# transitional street archive passed every structural check with 145,718 tiles
# that were all the same OSM "Access blocked" PNG.
#
# Verification after the swap checks FRESHNESS, not presence. Presence alone
# lies: Martin 1.13.0 keeps an open handle to the replaced file and an
# in-memory tile cache, so after an atomic swap it keeps serving the OLD data
# while the catalog still lists the id. Its hot reload is PROVEN ABSENT
# (Docs/86_MAP_DATASET_ACQUISITION.md), so `--restart-martin` restarts ONLY the
# tile server; never the backend, frontend, database, Redis or workers. Without
# the flag the script reports the stale state and exits 1.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PROD="$ROOT/map-data/production"
API="${API_CONTAINER:-face_recognition_api}"

# Every SQLite/Python step runs INSIDE the api container, not on the host: it is
# the environment that will actually serve the data, it has the decoders the
# content gate needs (Pillow), and the repo is bind-mounted there at /app. The
# host is not assumed to carry a working python3, a sqlite3 CLI or Pillow — this
# dev host has none of the three.
container_path() {
  local dir base abs
  dir="$(cd "$(dirname "$1")" && pwd)"
  base="$(basename "$1")"
  abs="$dir/$base"
  case "$abs" in
    "$ROOT"/*) echo "/app${abs#$ROOT}" ;;
    *) echo "" ;;
  esac
}

if [ "${1:-}" = "--rollback" ]; then
  ID="${2:?dataset id}"
  docker exec "$API" python3 /app/scripts/map_data/install_dataset.py --rollback "$ID"
  echo "restart martin to serve the restored archive:"
  echo "  docker compose -f docker/docker-compose.cpu.yml restart martin"
  exit 0
fi

SRC="${1:?built archive (.mbtiles.new)}"
ID="${2:?dataset id, e.g. lebanon-dem}"
RESTART="${3:-}"
DEST="$PROD/$ID.mbtiles"

[ -f "$SRC" ] || { echo "no such file: $SRC" >&2; exit 1; }
SRC_C="$(container_path "$SRC")"
[ -n "$SRC_C" ] || { echo "archive must live inside the repository so the api container can read it: $SRC" >&2; exit 1; }
docker exec "$API" test -f "$SRC_C" || { echo "api container cannot see $SRC_C" >&2; exit 1; }

# stage -> validate (structure + content) -> hash -> pending verdict -> rename
# -> activate verdict -> checksums.txt + datasets.json. Refuses before copying
# if the volume cannot take the archive plus its safety reserve.
docker exec -e MAP_INSTALL_DISK_RESERVE_GB="${MAP_INSTALL_DISK_RESERVE_GB:-2}" "$API" \
  python3 /app/scripts/map_data/install_dataset.py "$SRC_C" "$ID" \
  || { echo "REFUSING to install: the archive did not pass (see above). The installed archive is untouched." >&2; exit 1; }

# Probe address and expected payload come from the archive itself: a hard-coded
# z11 tile does not exist in every dataset, and vector tiles are stored gzipped
# but served content-negotiated, so both sides are normalised before hashing
# (scripts/map_data/tile_probe.py explains why).
DEST_C="$(container_path "$DEST")"
read -r PROBE_Z PROBE_X PROBE_Y EXPECTED <<<"$(docker exec "$API" python3 /app/scripts/map_data/tile_probe.py pick "$DEST_C")"
[ -n "${EXPECTED:-}" ] || { echo "could not pick a probe tile from $DEST" >&2; exit 1; }
echo "probe tile z$PROBE_Z/$PROBE_X/$PROBE_Y expects ${EXPECTED:0:16}"

# verify through nginx (the browser's path), from the api container: catalog
# lists the id AND the served probe tile carries the archive's payload
verify() {
  docker exec "$API" python3 /app/scripts/map_data/tile_probe.py \
    served "$ID" "$PROBE_Z" "$PROBE_X" "$PROBE_Y" "$EXPECTED"
}
if verify; then
  echo "martin serves $ID (fresh; no restart needed)"
elif [ "$RESTART" = "--restart-martin" ]; then
  echo "martin is not serving the new $ID; restarting martin only"
  (cd "$ROOT" && docker compose -f docker/docker-compose.cpu.yml restart martin >/dev/null)
  for i in $(seq 1 12); do sleep 5; verify && { echo "martin serves $ID (fresh) after restart"; exit 0; }; done
  echo "martin still not serving the new $ID" >&2; exit 1
else
  echo "martin is not serving the new $ID; re-run with --restart-martin" >&2; exit 1
fi
