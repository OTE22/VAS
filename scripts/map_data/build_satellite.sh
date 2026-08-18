#!/usr/bin/env bash
# Build the Lebanon satellite archive (Sentinel-2 L2A true colour), then
# install it. Backs the Satellite style.
#
#   scripts/map_data/build_satellite.sh [--install] [--restart-martin]
#
# Runs alone. Nothing else waits for it: a failed satellite build must never
# again hold up Light and Dark, which is what happened when the two were
# chained by hand and the vector build idled 16 h 07 m behind this one.
#
# Licence: Copernicus Sentinel-2 via AWS Open Data (public sentinel-cogs
# bucket, no credentials). "Free, full and open" — offline storage and
# redistribution in a derived product are permitted with the attribution the
# archive carries. Google, Bing, Esri and Mapbox imagery is NOT a substitute:
# none of their terms permit bulk offline storage.
#
# This is the slowest and most network-dependent build here — roughly 3 GB of
# scenes. The builder downloads every scene to a checksum-verified local file
# BEFORE interpreting any of it, refuses up front if the volume cannot take the
# result, and promotes its output only after that output validates.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MANIFEST="$ROOT/map-data/source/satellite/sentinel2_manifest.json"
OUT="$ROOT/map-data/production/lebanon-satellite.mbtiles.new"
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

[ -f "$MANIFEST" ] || { echo "no scene manifest at $MANIFEST (see Docs/86_MAP_DATASET_ACQUISITION.md)" >&2; exit 1; }
rm -f "$OUT"

docker run --rm \
  -v "$ROOT/map-data:/map-data" -v "$ROOT/scripts:/scripts:ro" \
  -e PYTHONUNBUFFERED=1 \
  -e MAP_BUILD_DISK_RESERVE_GB="${MAP_BUILD_DISK_RESERVE_GB:-5}" \
  "$GDAL" python3 /scripts/map_data/build_satellite.py \
  --manifest /map-data/source/satellite/sentinel2_manifest.json \
  --out /map-data/production/lebanon-satellite.mbtiles.new

if [ -n "$INSTALL" ]; then
  "$ROOT/scripts/map_data/install_dataset.sh" "$OUT" lebanon-satellite $RESTART
else
  echo "install it with:"
  echo "  scripts/map_data/install_dataset.sh $OUT lebanon-satellite --restart-martin"
fi
