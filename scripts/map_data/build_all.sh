#!/usr/bin/env bash
# Build every map dataset, INDEPENDENTLY.
#
#   scripts/map_data/build_all.sh [--install] [--restart-martin] [dataset ...]
#
#   datasets: streets-vector | satellite | dem   (default: all three)
#
# Failure semantics, which are the whole point of this script:
#
#   * every requested builder RUNS, even if an earlier one failed;
#   * each dataset gets a PASS/FAIL line and the run prints a summary table;
#   * artifacts from builders that succeeded are KEPT and installed;
#   * exit 0 only if every requested builder succeeded, non-zero otherwise.
#
# The datasets are genuinely independent — different sources, different tools,
# different failure modes — and the previous arrangement chained them by hand.
# The log of the one real run reads:
#
#     2026-08-15T19:53:30Z waiting for satellite build to finish (link serialized)
#     2026-08-16T12:00:48Z satellite build finished (exit 1); starting downloads
#
# 16 h 07 m of the streets build waiting on a satellite build that then failed.
# A satellite failure must cost you satellite, and nothing else.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/scripts/map_data"
PASSTHRU=()
REQUESTED=()
for arg in "$@"; do
  case "$arg" in
    --install|--restart-martin) PASSTHRU+=("$arg") ;;
    streets-vector|satellite|dem) REQUESTED+=("$arg") ;;
    *) echo "unknown argument: $arg" >&2
       echo "usage: build_all.sh [--install] [--restart-martin] [streets-vector|satellite|dem ...]" >&2
       exit 2 ;;
  esac
done
[ ${#REQUESTED[@]} -gt 0 ] || REQUESTED=(streets-vector satellite dem)

builder_for() {
  case "$1" in
    streets-vector) echo "$HERE/build_streets_vector.sh" ;;
    satellite)      echo "$HERE/build_satellite.sh" ;;
    dem)            echo "$HERE/build_dem.sh" ;;
  esac
}

LOG_DIR="$ROOT/logs/map_builds"
mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

declare -a NAMES=() RESULTS=() LOGS=()
FAILED=0

for dataset in "${REQUESTED[@]}"; do
  script="$(builder_for "$dataset")"
  log="$LOG_DIR/${dataset}_${STAMP}.log"
  echo
  echo "=== $dataset -> $log"
  # `set -e` must not abort the loop: every requested dataset gets its turn,
  # whatever the ones before it did.
  if "$script" "${PASSTHRU[@]+"${PASSTHRU[@]}"}" 2>&1 | tee "$log"; then
    status=PASS
  else
    status=FAIL
    FAILED=$((FAILED + 1))
  fi
  NAMES+=("$dataset"); RESULTS+=("$status"); LOGS+=("$log")
  echo "=== $dataset: $status"
done

echo
echo "================ build summary ($STAMP) ================"
for i in "${!NAMES[@]}"; do
  printf "  %-16s %-5s %s\n" "${NAMES[$i]}" "${RESULTS[$i]}" "${LOGS[$i]}"
done
echo "========================================================"

if [ "$FAILED" -gt 0 ]; then
  echo "$FAILED of ${#NAMES[@]} requested dataset(s) failed; the ones that passed were built and kept." >&2
  exit 1
fi
echo "all ${#NAMES[@]} requested dataset(s) built."
