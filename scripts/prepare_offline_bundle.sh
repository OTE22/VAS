#!/usr/bin/env bash
# Prepare the offline artifact bundle on an ONLINE machine.
#   scripts/prepare_offline_bundle.sh <spec.json> <out-dir>
# The spec lists models, wheels, images, frontend assets, drivers, certs and
# config templates (see docker/offline_bundle.spec.example.json). Every file
# is copied with a SHA-256 entry in manifest.json; images are `docker save`d.
set -euo pipefail
SPEC="${1:?spec json}"; OUT="${2:?output directory}"
python3 "$(dirname "$0")/offline_bundle.py" prepare --spec "$SPEC" --out "$OUT"
echo "verify with: scripts/verify_offline_bundle.sh $OUT"
