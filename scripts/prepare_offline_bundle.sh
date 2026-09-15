#!/usr/bin/env bash
# Same server: no arguments or --same-server checks the deployed installation.
#   sudo bash scripts/prepare_offline_bundle.sh --same-server --server-address NEW_IP
# Different machine: retain the artifact-export interface below.
#   scripts/prepare_offline_bundle.sh <spec.json> <out-dir>
# The spec lists models, wheels, images, frontend assets, drivers, certs and
# config templates (see docker/offline_bundle.spec.example.json). Every file
# is copied with a SHA-256 entry in manifest.json; images are `docker save`d.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ $# -eq 0 ]; then
    exec bash "$HERE/move_to_intranet.sh" --check
fi
if [ "$1" = --same-server ]; then
    shift
    exec bash "$HERE/move_to_intranet.sh" "$@"
fi
if [ "$1" = --help ] || [ "$1" = -h ]; then
    echo 'Same server (check): sudo bash scripts/prepare_offline_bundle.sh --same-server'
    echo 'Same server (probe without DNS): sudo bash scripts/prepare_offline_bundle.sh --same-server --server-address NEW_IP'
    echo 'Different machine (export): bash scripts/prepare_offline_bundle.sh <spec.json> <out-dir>'
    exit 0
fi
[ $# -eq 2 ] || { echo 'Use --help for the same-server and artifact-export commands.' >&2; exit 2; }
SPEC="${1:?spec json}"; OUT="${2:?output directory}"
python3 "$HERE/offline_bundle.py" prepare --spec "$SPEC" --out "$OUT"
echo "verify with: scripts/verify_offline_bundle.sh $OUT"
