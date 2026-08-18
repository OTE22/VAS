#!/usr/bin/env python3
"""Build the immutable map fixtures the isolated regression stack serves.

    docker exec face_recognition_api python3 /app/scripts/map_data/make_test_fixtures.py

Writes map-data-test/ — a complete, tiny, deterministic dataset tree with its
own content ledger:

    map-data-test/production/lebanon-streets-vector.mbtiles   ~40 MVT tiles
    map-data-test/production/lebanon-dem.mbtiles              ~40 terrarium tiles
    map-data-test/production/lebanon-satellite.mbtiles        ~40 JPEG tiles
    map-data-test/production/fonts/*.ttf                      the glyph stacks
    map-data-test/metadata/content_verdicts.json              their verdicts

Why a separate tree at all: the regression stack used to point at the
DEVELOPMENT map-data through the dev Martin, so every map assertion measured
whatever the developer happened to have installed — and the run could not
start when the dev stack was down. Fixtures the suite owns make the result
depend on the code under test and nothing else.

Deterministic on purpose: the same seed produces byte-identical archives, so a
verdict recorded once stays valid and a diff in a failing run means the code
changed, not the fixture. Generated, not committed, because a few MB of binary
in git for something reproducible in seconds is a bad trade — the regression
runner builds this before it starts the stack.

The fonts are COPIED from map-data/production/fonts when it exists. That is a
file read, not a running service, so it does not reintroduce a dependency on
the dev stack; without them the glyph gate would have nothing to check and the
resource rule would be untested rather than passing vacuously.
"""

import argparse
import gzip
import io
import json
import math
import os
import random
import shutil
import sqlite3
import struct
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Lebanon, matching MAP_BOUNDS_* so coverage checks land inside the fixtures.
BBOX = (34.80, 32.84, 36.92, 34.89)      # W, S, E, N
SEED = 20260817


def tile_xy(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def _addresses(zmin, zmax):
    """Every (z, x, y) covering the bbox, so coverage checks find a tile at
    each acceptance site."""
    west, south, east, north = BBOX
    for z in range(zmin, zmax + 1):
        x0, y0 = tile_xy(west, north, z)
        x1, y1 = tile_xy(east, south, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def _write(path, tiles, meta):
    """An MBTiles with metadata written FIRST, as the real builders now do."""
    if os.path.exists(path):
        os.unlink(path)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                "tile_row INTEGER, tile_data BLOB)")
    con.executemany("INSERT INTO metadata VALUES (?,?)", list(meta.items()))
    con.executemany("INSERT INTO tiles VALUES (?,?,?,?)",
                    [(z, x, (1 << z) - 1 - y, sqlite3.Binary(blob)) for z, x, y, blob in tiles])
    con.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    con.commit()
    con.close()
    return path


# --- vector -----------------------------------------------------------------

def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number, wire, payload):
    return _varint((number << 3) | wire) + payload


def _layer(name, extent=4096):
    body = _field(15, 0, _varint(2))                       # version
    body += _field(1, 2, _varint(len(name)) + name.encode())
    body += _field(5, 0, _varint(extent))
    return _field(3, 2, _varint(len(body)) + body)


def _mvt(layer_names):
    """A minimal but genuinely parseable vector tile, gzipped as Planetiler does."""
    return gzip.compress(b"".join(_layer(n) for n in layer_names))


def build_vector(path):
    rng = random.Random(SEED)
    # The layer names the shipped styles reference, so the source-layer test is
    # meaningful against the fixture too.
    pool = ["water", "landcover", "transportation", "building", "place",
            "boundary", "landuse", "park", "waterway", "transportation_name"]
    tiles = []
    for z, x, y in _addresses(0, 8):
        count = rng.choice([1, 2, 2, 3, 3, 4])             # >25% multi-layer
        tiles.append((z, x, y, _mvt(rng.sample(pool, count))))
    meta = {
        "name": "lebanon-streets-vector", "format": "pbf", "type": "baselayer",
        "version": "1", "minzoom": "0", "maxzoom": "8",
        "bounds": ",".join(str(v) for v in BBOX),
        "json": json.dumps({"vector_layers": [{"id": n} for n in pool]}),
        "description": "Regression fixture: synthetic OpenMapTiles-shaped vector tiles.",
        "attribution": "Fixture data. Not a map.",
    }
    return _write(path, tiles, meta)


# --- raster helpers ---------------------------------------------------------

def _pil():
    from PIL import Image
    return Image


def build_imagery(path):
    """Varied JPEG tiles: decode, distinct, entropic, and different per site."""
    Image = _pil()
    rng = random.Random(SEED + 1)
    tiles = []
    for z, x, y in _addresses(6, 10):
        image = Image.new("RGB", (256, 256))
        image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                       for _ in range(256 * 256)])
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=85)
        tiles.append((z, x, y, buf.getvalue()))
    meta = {
        "name": "lebanon-satellite", "format": "jpg", "type": "baselayer",
        "version": "1", "minzoom": "6", "maxzoom": "10",
        "bounds": ",".join(str(v) for v in BBOX),
        "description": "Regression fixture: synthetic imagery. NOT satellite data.",
        "attribution": "Fixture data. Not imagery.",
    }
    return _write(path, tiles, meta)


def build_dem(path):
    """Terrarium tiles with real relief: R*256 + G + B/256 - 32768 metres."""
    Image = _pil()
    tiles = []
    for z, x, y in _addresses(6, 10):
        image = Image.new("RGB", (256, 256))
        pixels = []
        for row in range(256):
            for col in range(256):
                # A deterministic ridge: hundreds of metres of relief per tile,
                # so the >10 m variation rule is satisfied by real structure.
                elevation = 400.0 + 900.0 * math.sin((col + x * 7) / 40.0) \
                    * math.cos((row + y * 11) / 40.0)
                value = max(0.0, min(65535.0, elevation + 32768.0))
                r = int(value // 256)
                g = int(value - r * 256)
                b = int((value - r * 256 - g) * 256)
                pixels.append((r, g, b))
        image.putdata(pixels)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        tiles.append((z, x, y, buf.getvalue()))
    meta = {
        "name": "lebanon-dem", "format": "png", "type": "baselayer", "version": "1",
        "minzoom": "6", "maxzoom": "10",
        "bounds": ",".join(str(v) for v in BBOX),
        "encoding": "terrarium",
        "description": "Regression fixture: synthetic terrarium elevations.",
        "attribution": "Fixture data. Not elevation data.",
    }
    return _write(path, tiles, meta)


def copy_fonts(dest):
    """Glyph sources, so the resource gate has something real to resolve."""
    source = "/app/map-data/production/fonts"
    os.makedirs(dest, exist_ok=True)
    if not os.path.isdir(source):
        print(f"note: {source} is absent; the fixture stack will have no glyphs "
              f"and the resource gate will report RESOURCES_MISSING")
        return 0
    copied = 0
    for name in sorted(os.listdir(source)):
        if name.lower().endswith((".ttf", ".otf")):
            shutil.copy2(os.path.join(source, name), os.path.join(dest, name))
            copied += 1
    return copied


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="/app/map-data-test")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if the fixtures are already present")
    args = ap.parse_args()

    production = os.path.join(args.out, "production")
    metadata = os.path.join(args.out, "metadata")
    os.makedirs(production, exist_ok=True)
    os.makedirs(metadata, exist_ok=True)

    ledger_path = os.path.join(metadata, "content_verdicts.json")
    if os.path.exists(ledger_path) and not args.force:
        print(f"fixtures already built at {args.out} (use --force to rebuild)")
        return 0

    print(f"building map fixtures in {args.out}")
    built = {
        "lebanon-streets-vector": build_vector(
            os.path.join(production, "lebanon-streets-vector.mbtiles")),
        "lebanon-dem": build_dem(os.path.join(production, "lebanon-dem.mbtiles")),
        "lebanon-satellite": build_imagery(
            os.path.join(production, "lebanon-satellite.mbtiles")),
    }
    fonts = copy_fonts(os.path.join(production, "fonts"))
    print(f"  {fonts} font file(s)")

    # Verify what was just written with the SAME validator production uses, and
    # record the verdicts in the fixture tree's own ledger. A fixture that
    # cannot pass the real gate is a broken fixture, and finding that out here
    # is much cheaper than a mystery failure inside the regression run.
    from backend.core import map_content_ledger as ledger
    entries = {}
    for source_id, path in built.items():
        entry = ledger.build_entry(source_id, path, verifier="make_test_fixtures")
        if not entry["pass"]:
            print(f"FAILED: the {source_id} fixture does not pass validation: "
                  f"[{entry['code']}] {entry['message']}", file=sys.stderr)
            return 1
        entries[source_id] = entry
        print(f"  {source_id}: {entry['kind']}, "
              f"{os.path.getsize(path) / 1024:.0f} KB, sha256 {entry['archive_sha256'][:12]}…")
    ledger.save(entries, {}, path=ledger_path)
    print(f"wrote {ledger_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
