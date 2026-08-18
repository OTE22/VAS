#!/usr/bin/env python3
"""Freshness probe for an installed dataset — the two halves of the check that
`install_dataset.sh` runs after an atomic swap.

    pick   <archive.mbtiles>                 -> "Z X Y SHA256" (XYZ y)
    served <id> <z> <x> <y> <sha256>         -> exit 0 when Martin serves those bytes

Why this is not two lines of `curl | sha256sum`:

  * **The probe address must come from the archive.** A hard-coded z11 Beirut
    tile is inside every current dataset, but a future satellite build at z13-18
    would fail freshness verification for the sole reason that the probe address
    does not exist in it.
  * **Vector tiles are stored gzipped and served negotiated.** Martin decides
    per request whether to send the stored gzip blob or an inflated one, so a
    byte-comparison against the stored blob can never match for MVT — the
    install would report STALE forever on a perfectly good archive. Both sides
    are therefore normalised (decompressed) before hashing, which compares the
    tile's CONTENT rather than its transfer encoding.
"""
import gzip
import hashlib
import sqlite3
import sys
import urllib.request
import zlib

sys.path.insert(0, "/app/scripts/map_data")
try:
    from coverage_check import tile_xy
except ImportError:                                                # pragma: no cover
    import math

    def tile_xy(lon, lat, z):
        n = 2 ** z
        x = int((lon + 180.0) / 360.0 * n)
        lat_r = math.radians(lat)
        y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
        return x, y

BEIRUT = (35.4955, 33.8938)


def normalise(blob):
    """Tile payload with any compression removed — the comparable form."""
    if blob[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(blob)
        except Exception:                                          # noqa: BLE001
            return blob
    if blob[:1] == b"\x78":
        try:
            return zlib.decompress(blob)
        except Exception:                                          # noqa: BLE001
            return blob
    return blob


def pick(archive):
    con = sqlite3.connect(f"file:{archive}?mode=ro", uri=True)
    meta = {k: v for k, v in con.execute("SELECT name, value FROM metadata") if k}
    try:
        zmin, zmax = int(meta["minzoom"]), int(meta["maxzoom"])
    except (KeyError, TypeError, ValueError):
        row = con.execute("SELECT zoom_level, min(zoom_level) FROM tiles").fetchone()
        if not row or row[0] is None:
            sys.exit("archive declares no zoom range and holds no tiles")
        zmin = zmax = int(row[0])

    # Prefer a mid zoom (real detail, cheap tile), then anything in range.
    order = sorted(range(zmin, zmax + 1), key=lambda z: (abs(z - (zmin + zmax) // 2), z))
    lon, lat = BEIRUT
    for z in order:
        x, y = tile_xy(lon, lat, z)
        got = con.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? "
                          "AND tile_row=?", (z, x, 2 ** z - 1 - y)).fetchone()
        if got:
            return z, x, y, hashlib.sha256(normalise(got[0])).hexdigest()

    # No Lebanon anchor: any tile still proves freshness.
    got = con.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles LIMIT 1").fetchone()
    if not got:
        sys.exit("archive holds no tiles")
    z, x, row, blob = got
    return z, x, 2 ** z - 1 - row, hashlib.sha256(normalise(blob)).hexdigest()


def served(source_id, z, x, y, expected, base="http://nginx/maps"):
    catalog = urllib.request.urlopen(f"{base}/catalog", timeout=15).read().decode("utf-8", "replace")
    if f'"{source_id}"' not in catalog:
        print(f"catalog: MISSING {source_id}")
        return 1
    request = urllib.request.Request(f"{base}/{source_id}/{z}/{x}/{y}",
                                     headers={"Accept-Encoding": "gzip, identity"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
            body = gzip.decompress(body)
    if not body:
        print(f"probe tile {z}/{x}/{y}: server returned no bytes")
        return 1
    got = hashlib.sha256(normalise(body)).hexdigest()
    if got == expected:
        print(f"catalog: ok, probe tile {z}/{x}/{y} is FRESH")
        return 0
    print(f"probe tile {z}/{x}/{y} STALE (served {got[:16]}, archive {expected[:16]})")
    return 1


def main(argv):
    if len(argv) >= 3 and argv[1] == "pick":
        z, x, y, digest = pick(argv[2])
        print(f"{z} {x} {y} {digest}")
        return 0
    if len(argv) >= 7 and argv[1] == "served":
        return served(argv[2], int(argv[3]), int(argv[4]), int(argv[5]), argv[6])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
