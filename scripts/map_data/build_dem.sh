#!/usr/bin/env bash
# Build the Lebanon terrarium DEM archive (Copernicus GLO-30), then install it.
# Backs the Terrain style: 3-D terrain and client-side hillshade from this one
# raster-dem source.
#
#   scripts/map_data/build_dem.sh [--install] [--restart-martin]
#
# Runs alone; nothing else waits for it and it waits for nothing.
#
# Source: Copernicus DEM GLO-30 COGs already present in map-data/source/terrain/
# (AWS Open Data bucket copernicus-dem-30m, public, no credentials). Free and
# open with attribution, which the archive carries in its metadata. The builder
# now verifies them against the SHA256SUMS that has always sat beside them and
# was never read.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$ROOT/map-data/source/terrain"
OUT="$ROOT/map-data/production/lebanon-dem.mbtiles.new"
GDAL="${GDAL_IMAGE:-ghcr.io/osgeo/gdal:ubuntu-full-3.13.2}"
INSTALL=""
RESTART=""
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    --restart-martin) RESTART="--restart-martin" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

[ -d "$SRC" ] || { echo "no source rasters at $SRC (see Docs/86_MAP_DATASET_ACQUISITION.md)" >&2; exit 1; }
rm -f "$OUT"

docker run --rm \
  -v "$ROOT/map-data:/map-data" -v "$ROOT/scripts:/scripts:ro" \
  -e MAP_BUILD_DISK_RESERVE_GB="${MAP_BUILD_DISK_RESERVE_GB:-10}" \
  "$GDAL" python3 /scripts/map_data/build_dem_terrarium.py \
  --source /map-data/source/terrain \
  --out /map-data/production/lebanon-dem.mbtiles.new

if [ -n "$INSTALL" ]; then
  "$ROOT/scripts/map_data/install_dataset.sh" "$OUT" lebanon-dem $RESTART
else
  echo "install it with:"
  echo "  scripts/map_data/install_dataset.sh $OUT lebanon-dem --restart-martin"
fi
