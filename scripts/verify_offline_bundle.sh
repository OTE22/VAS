#!/usr/bin/env bash
# Verify a bundle's manifest (every file present, size and SHA-256 intact).
#   scripts/verify_offline_bundle.sh <bundle-dir>
# Exit 0 = intact; non-zero lists every mismatch. Run this on the production
# host BEFORE import and before every start (OFFLINE_BUNDLE_MANIFEST).
set -euo pipefail
python3 "$(dirname "$0")/offline_bundle.py" verify --bundle "${1:?bundle directory}"
