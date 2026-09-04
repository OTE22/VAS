#!/usr/bin/env bash
#
# Fetch the runtime model weights, verified against weights/WEIGHTS_MANIFEST.json.
#
#   bash scripts/setup/download.sh                 # verify + fetch what is missing
#   bash scripts/setup/download.sh --force         # re-fetch even verified files
#   bash scripts/setup/download.sh --insecure-no-verify   # no manifest (see below)
#
# Run this on a CONNECTED machine, then copy weights/ to the production host —
# which is expected to be offline. `deploy.sh model-check` re-measures the files
# there, so a truncated or substituted download cannot reach a container.
#
# WHY IT LOOKS LIKE THIS
# The previous version began with `rm -rf weights/*.onnx` and then wget'd five
# files with no integrity check at all. That combination can delete a working
# deployment's weights and replace them with a half-downloaded file that still
# "exists" — and for the recogniser, whose FILENAME is the embedding-version
# stamp written into every stored vector, a wrong-but-loadable file silently
# invalidates comparability with every embedding already in the database.
#
# So: download to a temporary name, verify sha256 against the manifest, and only
# then move into place. Nothing is deleted; an already-verified file is left
# alone unless --force is given.

set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT="$(pwd)"
WEIGHTS_DIR="$ROOT/weights"
MANIFEST="$WEIGHTS_DIR/WEIGHTS_MANIFEST.json"
BASE_URL="https://github.com/yakhyo/face-reidentification/releases/download/v0.0.1"

FORCE=0
VERIFY=1
for arg in "$@"; do
    case "$arg" in
        --force)               FORCE=1 ;;
        --insecure-no-verify)  VERIFY=0 ;;
        -h|--help)             sed -n '2,28p' "$0" | sed 's/^#//; s/^ //'; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

mkdir -p "$WEIGHTS_DIR"

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    else shasum -a 256 "$1" | awk '{print $1}'; fi
}

manifest_sha() {
    tr -d '\n' < "$MANIFEST" \
        | sed 's/},/}\n/g' \
        | grep "\"filename\"[[:space:]]*:[[:space:]]*\"$1\"" \
        | sed -n 's/.*"sha256"[[:space:]]*:[[:space:]]*"\([a-f0-9]*\)".*/\1/p' \
        | head -1
}

if [ "$VERIFY" = "1" ] && [ ! -f "$MANIFEST" ]; then
    cat >&2 <<EOF
No $MANIFEST.

The manifest is the reference every deployment measures its weights against; a
file that has never been measured is not usable (deploy.sh model-check refuses
it). Either ship the manifest with this checkout, or — if you are bootstrapping
the very first copy from a source you trust — re-run with:

    bash scripts/setup/download.sh --insecure-no-verify
    ./deploy.sh model-manifest        # then measure what you just fetched

EOF
    exit 1
fi

# filename|url-suffix — only the two files the application actually loads.
# The release also carries det_2.5g / det_500m / w600k_mbf; nothing in this
# codebase reads them, so they are not fetched.
FILES="det_10g.onnx w600k_r50.onnx"

status=0
for name in $FILES; do
    target="$WEIGHTS_DIR/$name"
    want=""
    [ "$VERIFY" = "1" ] && want="$(manifest_sha "$name")"

    if [ -f "$target" ] && [ "$FORCE" != "1" ]; then
        if [ "$VERIFY" != "1" ]; then
            echo "= $name already present (unverified: --insecure-no-verify)"
            continue
        fi
        if [ -n "$want" ] && [ "$(sha256_of "$target")" = "$want" ]; then
            echo "= $name already present and VERIFIED"
            continue
        fi
        echo "! $name present but does NOT match the manifest — re-downloading"
    fi

    tmp="$target.download.$$"
    echo "> fetching $name"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --retry-delay 2 -o "$tmp" "$BASE_URL/$name" || { rm -f "$tmp"; echo "  download failed" >&2; status=1; continue; }
    else
        wget -q --tries=3 -O "$tmp" "$BASE_URL/$name" || { rm -f "$tmp"; echo "  download failed" >&2; status=1; continue; }
    fi

    if [ "$VERIFY" = "1" ] && [ -n "$want" ]; then
        got="$(sha256_of "$tmp")"
        if [ "$got" != "$want" ]; then
            rm -f "$tmp"
            echo "  CHECKSUM_MISMATCH for $name" >&2
            echo "    manifest: $want" >&2
            echo "    download: $got" >&2
            echo "  The existing file (if any) was left untouched." >&2
            status=1
            continue
        fi
        echo "  sha256 verified"
    elif [ "$VERIFY" = "1" ]; then
        echo "  WARNING: the manifest does not describe $name — cannot verify" >&2
    fi

    mv -f "$tmp" "$target"      # atomic: a partial file is never visible as the weight
    echo "  installed weights/$name"
done

if [ "$status" -eq 0 ]; then
    echo
    echo "Done. Verify on the target host with:  ./deploy.sh model-check"
else
    echo
    echo "One or more downloads failed or did not verify — nothing was replaced." >&2
fi
exit "$status"
