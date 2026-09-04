#!/usr/bin/env bash
# Build the Lebanon streets VECTOR archive (Planetiler -> OpenMapTiles schema),
# then install it. This is the dataset Light AND Dark are drawn from: one
# archive, two palettes.
#
#   scripts/map_data/build_streets_vector.sh [--install] [--restart-martin]
#
# Runs alone. It waits for nothing and no other builder waits for it — the one
# run that produced the current archive sat idle for 16 h 07 m behind a
# satellite build that then failed, because the two were chained by hand.
#
# There is NO raster streets builder, deliberately. The raster archive this
# replaced was 145,718 copies of OpenStreetMap's "Access blocked" image,
# produced by scraping tile.openstreetmap.org ~145,000 times against their tile
# usage policy. Rebuilding it would mean scraping again. Vector tiles come from
# the Geofabrik PBF extract, which is published for exactly this purpose.
#
# Sources and licences:
#   OSM data      Geofabrik lebanon-*.osm.pbf, ODbL 1.0, (c) OpenStreetMap contributors
#   Schema        OpenMapTiles profile (Planetiler), CC-BY 4.0
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$ROOT/map-data/source/osm"
OUT="$ROOT/map-data/production/lebanon-streets-vector.mbtiles.new"
PBF_BASE="${PBF_BASE:-https://download.geofabrik.de/asia}"
PBF_NAME="${PBF_NAME:-lebanon-latest.osm.pbf}"
PLANETILER="${PLANETILER_IMAGE:-ghcr.io/onthegomap/planetiler:0.10.2}"
BBOX="34.80,32.84,36.92,34.89"
INSTALL=""
RESTART=""
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    --restart-martin) RESTART="--restart-martin" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$SRC"
cd "$SRC"

# 1. source extract + its published md5. Geofabrik ships the checksum beside
#    the file; a truncated PBF otherwise produces a partial map with no error.
if [ ! -f "$PBF_NAME" ]; then
  echo "== downloading $PBF_NAME"
  curl -fSL --retry 5 --retry-delay 5 --retry-all-errors \
       --connect-timeout 30 --max-time 3600 -o "$PBF_NAME.part" "$PBF_BASE/$PBF_NAME"
  mv -f "$PBF_NAME.part" "$PBF_NAME"
fi
curl -fsSL --retry 3 --connect-timeout 30 -o "$PBF_NAME.md5" "$PBF_BASE/$PBF_NAME.md5"
md5sum -c "$PBF_NAME.md5" || { echo "REFUSING to build: $PBF_NAME failed its published md5" >&2; exit 1; }

# 2. Planetiler's supporting data (Natural Earth, water polygons, lake
#    centrelines — ~1.4 GB). Separate step so the build itself needs no network
#    and can be repeated offline.
if [ ! -d planetiler-data ] || [ -z "$(ls -A planetiler-data 2>/dev/null)" ]; then
  echo "== downloading Planetiler supporting data (~1.4 GB, once)"
  docker run --rm -e JAVA_TOOL_OPTIONS=-Xmx3g -v "$PWD:/data" "$PLANETILER" \
    --osm-path="/data/$PBF_NAME" --download-dir=/data/planetiler-data --only-download --download
fi

# 3. build. NOTE force=false: the one previous run passed force=true, which
#    Planetiler documents as "overwriting output file and ignore disk/RAM
#    warnings" — it disabled the only disk guard in the whole chain.
echo "== building vector tiles"
rm -f "$OUT.part"
docker run --rm -e JAVA_TOOL_OPTIONS=-Xmx3g -v "$PWD:/data" -v "$ROOT/map-data:/map-data" "$PLANETILER" \
  --osm-path="/data/$PBF_NAME" --download-dir=/data/planetiler-data \
  --output="/map-data/production/lebanon-streets-vector.mbtiles.new.part" \
  --minzoom=0 --maxzoom=14 --bounds="$BBOX"

# 4. the builder validates its own output before anything can install it
docker exec "${API_CONTAINER:-face_recognition_api}" \
  python3 /app/scripts/map_data/coverage_check.py --archive \
  "/app/map-data/production/lebanon-streets-vector.mbtiles.new.part" \
  || { echo "REFUSING to publish: the archive this build produced did not validate" >&2
       rm -f "$OUT.part"; exit 1; }
mv -f "$OUT.part" "$OUT"
echo "built $OUT"

if [ -n "$INSTALL" ]; then
  "$ROOT/scripts/map_data/install_dataset.sh" "$OUT" lebanon-streets-vector $RESTART
else
  echo "install it with:"
  echo "  scripts/map_data/install_dataset.sh $OUT lebanon-streets-vector --restart-martin"
fi
