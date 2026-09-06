#!/usr/bin/env bash
# Import a verified bundle on the AIR-GAPPED production host: load images,
# place model files, wheels, assets and certs at their destinations.
#   scripts/import_offline_bundle.sh <bundle-dir> [dest-root]
# Refuses a bundle that does not verify. Then:
#   pip install --no-index --find-links <bundle>/wheel -r requirements-*.txt   (if rebuilding)
#   docker compose -f docker/docker-compose.prod.yml -f docker/docker-compose.prod.gpu.yml up -d
set -euo pipefail
BUNDLE="${1:?bundle directory}"; DEST="${2:-/}"
python3 "$(dirname "$0")/offline_bundle.py" verify --bundle "$BUNDLE"
python3 "$(dirname "$0")/offline_bundle.py" import --bundle "$BUNDLE" --dest "$DEST"
