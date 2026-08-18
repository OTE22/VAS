#!/usr/bin/env python3
"""Acceptance for installed map datasets: COVERAGE and CONTENT (Docs/86 §4).

Two independent questions, both required before a dataset may be served:

    COVERAGE  is a tile present at every acceptance site — eight Lebanon
              locations spanning coast, mountain, Bekaa, north and south — plus
              every camera coordinate in the database, at min/mid/max zoom?
    CONTENT   are those tiles actually map data?

The second question exists because the first one, and every other check the
project had (byte-identical to the source pyramid, tile count matches, checksum
recorded, HTTP 200), is a statement about STRUCTURE. An archive of 145,718
identical copies of OpenStreetMap's "Access blocked" placeholder passed all of
them and served a wall of that image as the Light basemap.

Content validation therefore decodes tiles and reasons about what they contain,
per archive kind:

    imagery  decodes · expected dimensions · not blank · has entropy · varies
             across geography
    dem      decodes · terrarium elevations in a plausible range · relief present
    vector   decompresses · parses as MVT · declares layers the metadata knows

Sampling is DETERMINISTIC (a stratified lattice plus the acceptance sites), so a
refusal reproduces exactly on the operator's next run, and it never touches
`rowid` — Planetiler writes `tiles` as a VIEW over a deduplicated table, where a
rowid sample silently returns nothing.

    python3 scripts/map_data/coverage_check.py [--production DIR] [--dsn URL]
                                               [--json] [--coverage-only]

Exit 0 = every installed dataset passes both; exit 1 = at least one failed
(the reason is printed). Runs inside the api container or on the preparation
machine against a copy.
"""
import argparse
import glob
import gzip
import hashlib
import json
import math
import os
import sqlite3
import statistics
import sys
import time
import zlib

SITES = {
    "Beirut": (35.4955, 33.8938),
    "Tripoli": (35.8497, 34.4367),
    "Sidon": (35.3689, 33.5571),
    "Tyre": (35.1953, 33.2704),
    "Baalbek (Bekaa)": (36.2181, 34.0058),
    "Northern border (Arida)": (36.0069, 34.6300),
    "South (Naqoura)": (35.1333, 33.1167),
    "Mount Lebanon (Bcharre)": (36.0114, 34.2506),
}

# sha256 of upstream placeholder / error images that must never be installed as
# map data. Keep in sync with backend/core/map_availability.PLACEHOLDER_TILES
# (tests/test_maplibre_stack.py asserts the two registries are identical).
PLACEHOLDER_TILES = {
    "6eabebf6e8f2ff16f9109c808d7e7a0228fed0235a05c074b4a1ef99f964edfd":
        "OpenStreetMap 'Access blocked' tile-usage-policy placeholder (osm.wiki/Blocked)",
}

# --- refusal codes ----------------------------------------------------------
# Machine-readable reason for every refusal, assigned where the refusal is
# decided rather than recovered later by matching the wording. These reach an
# API client through the content ledger, so they are a wire contract: keep them
# a subset of backend/core/map_content_ledger.REASON_CODES (asserted by
# tests/test_maplibre_stack.py). The distinction the four data codes draw:
#
#   ARCHIVE_CORRUPT      the bytes are not usable tiles (won't decode, wrong
#                        geometry, unparseable, truncated)
#   CONTENT_DEGENERATE   they decode perfectly and carry no map (blank, uniform,
#                        flat, single-layer, the same image everywhere)
#   METADATA_INVALID     the archive's own metadata contradicts its tiles
#   SOURCE_BLOCKED       the payload is somebody's HTML error page
PLACEHOLDER_CONTENT = "PLACEHOLDER_CONTENT"
CONTENT_DEGENERATE = "CONTENT_DEGENERATE"
ARCHIVE_CORRUPT = "ARCHIVE_CORRUPT"
METADATA_INVALID = "METADATA_INVALID"
TILE_COUNT_INVALID = "TILE_COUNT_INVALID"
SOURCE_BLOCKED = "SOURCE_BLOCKED"
BUILD_INCOMPLETE = "BUILD_INCOMPLETE"

# Payload prefixes that mean "a server refused you and the refusal got saved as
# a tile". This is how the original incident would look today if the scraper
# had stored the deny page instead of OSM's placeholder PNG.
_BLOCK_PAGE_MARKERS = (b"<html", b"<!doctype", b"<?xml", b"{\"error", b"{\n  \"error",
                       b"access denied", b"forbidden", b"error 403", b"rate limit")

# --- thresholds -------------------------------------------------------------
# Every number below is justified against MEASURED values from this project's
# own archives, recorded beside it. Tightening one to "feel safer" rejects real
# data; the valid DEM is 43.2% one image because Lebanon's bbox includes sea.
DEGENERATE_RATIO = 0.98          # measured: DEM 43.2%, vector 25.2%, poison 100%
DISTINCT_FLOOR_MIN = 8           # a handful of alternating placeholders beats 0.98
DISTINCT_FLOOR_RATIO = 0.02      # measured distinct share: DEM 57%, vector 54%
BLANK_TILE_RATIO = 0.30          # Lebanon fills ~55% of its bbox; a dead build is 100%
ENTROPY_MEDIAN_MIN = 2.0         # solid 0 · OSM blocked graphic ~0.5-1.2 · street 2.5-5 · S2 5-7
ENTROPY_TILE_MIN = 1.0
ENTROPY_TILE_MIN_SHARE = 0.60
DEM_VARIED_MIN_SHARE = 0.20      # measured: 57% of real DEM tiles have relief
DEM_RELIEF_METRES = 10.0
DEM_MIN_M, DEM_MAX_M = -500.0, 9000.0
VECTOR_MULTILAYER_MIN_SHARE = 0.25   # measured: 55% of the vector archive is multi-layer
VALID_EXTENTS = {256, 512, 1024, 2048, 4096}
VALID_DIMENSIONS = {(256, 256), (512, 512)}

# A full hash pass is exact and cheap for small archives (measured: DEM 1,322
# tiles / 79 MB = 0.30 s; vector 9,606 / 38 MB = 0.16 s). The 1.2 GB raster took
# 54 s, so anything that large is judged from the stratified sample instead.
FULL_SCAN_MAX_TILES = 20_000
FULL_SCAN_MAX_BYTES = 256 * 1024 * 1024
SAMPLE_BUDGET = 1200
DECODE_BUDGET = 240


def tile_xy(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def _settings_dsn():
    """The application's DATABASE_URL (config.py is the ONE configuration
    interface — this script never reads the environment itself). Empty when the
    application package is not importable (host run without the repo on the path)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        from config import settings
        return str(settings.DATABASE_URL)
    except Exception as exc:  # pragma: no cover - host runs without the app package
        print(f"settings unavailable ({exc}); camera coordinates need --dsn", file=sys.stderr)
        return ""


def camera_sites(dsn):
    """(name, lon, lat) for every pipeline with coordinates. Empty if no DSN."""
    if not dsn:
        return []
    import psycopg2  # the installed sync driver (Alembic uses it too)
    con = psycopg2.connect(dsn)
    try:
        cur = con.cursor()
        cur.execute("SELECT pipeline_id, longitude, latitude FROM pipelines "
                    "WHERE latitude IS NOT NULL AND longitude IS NOT NULL")
        return [(f"camera {pid}", float(lon), float(lat)) for pid, lon, lat in cur.fetchall()]
    finally:
        con.close()


# ---------------------------------------------------------------- archive I/O

def _open(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only = 1")
    return con


def _metadata(con):
    try:
        return {k: v for k, v in con.execute("SELECT name, value FROM metadata") if k}
    except sqlite3.Error:
        return {}


def _zoom_survey(con):
    """(zoom, count, min_col, max_col, min_row, max_row) — index columns only."""
    return con.execute(
        "SELECT zoom_level, count(*), min(tile_column), max(tile_column), "
        "       min(tile_row), max(tile_row) "
        "FROM tiles GROUP BY zoom_level ORDER BY zoom_level").fetchall()


def _tile(con, z, x, row):
    got = con.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? "
                      "AND tile_row=? LIMIT 1", (z, x, row)).fetchone()
    return got[0] if got else None


def sample_tiles(con, *, budget=SAMPLE_BUDGET, anchors=SITES):
    """Deterministic, geographically stratified (z, x, row, blob) samples.

    Per zoom the budget is proportional to sqrt(count) — linear weighting would
    hand two-thirds of the sample to the deepest zoom and never look at the
    shallow ones — and the addresses come from a lattice laid over that zoom's
    own column/row extent, so an archive that is correct in Beirut and blank in
    the Bekaa cannot pass. Acceptance sites are always included, which makes the
    coverage answer and the content answer statements about the same tiles.

    Never uses rowid: Planetiler's `tiles` is a view over a deduplicated table.
    """
    survey = _zoom_survey(con)
    picked, seen, empty_cells = [], set(), 0
    if not survey:
        return picked, {"cells_empty": 0, "zooms": {}}

    weights = {row[0]: math.sqrt(row[1]) for row in survey}
    total_weight = sum(weights.values()) or 1.0
    zooms = sorted(weights)

    for z, count, cmin, cmax, rmin, rmax in survey:
        share = max(3, int(round(budget * weights[z] / total_weight)))
        side = max(1, int(math.ceil(math.sqrt(share))))
        cols = _lattice(cmin, cmax, side)
        rows = _lattice(rmin, rmax, side)
        for ci, (c0, c1) in enumerate(cols):
            for ri, (r0, r1) in enumerate(rows):
                got = con.execute(
                    "SELECT tile_column, tile_row, tile_data FROM tiles "
                    "WHERE zoom_level=? AND tile_column BETWEEN ? AND ? "
                    "AND tile_row BETWEEN ? AND ? LIMIT 1", (z, c0, c1, r0, r1)).fetchone()
                if not got:
                    empty_cells += 1        # a coverage hole, never an abort
                    continue
                key = (z, got[0], got[1])
                if key not in seen:
                    seen.add(key)
                    picked.append((z, got[0], got[1], got[2]))

    # acceptance sites at min / mid / max zoom
    anchor_zooms = sorted({zooms[0], zooms[len(zooms) // 2], zooms[-1]})
    for lon, lat in anchors.values():
        for z in anchor_zooms:
            x, y = tile_xy(lon, lat, z)
            row = 2 ** z - 1 - y
            key = (z, x, row)
            if key in seen:
                continue
            blob = _tile(con, z, x, row)
            if blob is None:
                continue
            seen.add(key)
            picked.append((z, x, row, blob))

    return picked, {"cells_empty": empty_cells, "zooms": {r[0]: r[1] for r in survey}}


def _lattice(lo, hi, side):
    """`side` contiguous, non-overlapping index bands covering [lo, hi]."""
    if lo is None or hi is None:
        return []
    span = max(1, hi - lo + 1)
    step = max(1, span // side)
    bands, start = [], lo
    while start <= hi and len(bands) < side:
        end = min(hi, start + step - 1)
        bands.append((start, end))
        start = end + 1
    if bands and bands[-1][1] < hi:
        bands[-1] = (bands[-1][0], hi)
    return bands


# ---------------------------------------------------------------- kind

def detect_kind(meta, first_blob):
    """('vector'|'dem'|'imagery'|'unknown', evidence). Metadata first, bytes as
    the fallback for an archive whose metadata table is empty (a failed build),
    and a disagreement between the two is a failure rather than a guess."""
    fmt = (meta.get("format") or "").strip().lower()
    enc = (meta.get("encoding") or "").strip().lower()
    declared = None
    # encoding BEFORE format: the terrarium DEM declares format=png, so a
    # format-first ladder calls it imagery and then rejects it for uniformity.
    if enc in ("terrarium", "mapbox"):
        declared = "dem"
    elif fmt in ("pbf", "mvt"):
        declared = "vector"
    elif fmt in ("png", "jpg", "jpeg", "webp"):
        declared = "imagery"

    observed = _kind_from_bytes(first_blob) if first_blob else None
    if declared and observed and declared != observed:
        # dem is stored as png, so png bytes under a dem declaration agree
        if not (declared == "dem" and observed == "imagery"):
            return "unknown", f"metadata says {declared} but the tiles are {observed}"
    if declared:
        return declared, f"metadata format={fmt or '?'} encoding={enc or '-'}"
    if observed:
        return observed, "magic bytes (metadata carries no format)"
    return "unknown", "no metadata format and unrecognised tile bytes"


def _kind_from_bytes(blob):
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "imagery"
    if blob[:3] == b"\xff\xd8\xff":
        return "imagery"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "imagery"
    payload, _how = _decompress(blob)
    if payload is not blob or payload[:1] == b"\x1a":
        try:
            if _scan_mvt(payload):
                return "vector"
        except Exception:                                          # noqa: BLE001
            return None
    return None


def _decompress(blob):
    if blob[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(blob), "gzip"
        except Exception:                                          # noqa: BLE001
            return blob, "gzip-failed"
    if blob[:1] == b"\x78":
        try:
            return zlib.decompress(blob), "zlib"
        except Exception:                                          # noqa: BLE001
            pass
    return blob, "raw"


# ---------------------------------------------------------------- MVT scanner

def _varint(buf, i):
    result, shift = 0, 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _skip(buf, i, wire):
    if wire == 0:
        _, i = _varint(buf, i)
        return i
    if wire == 1:
        return i + 8
    if wire == 5:
        return i + 4
    if wire == 2:
        length, i = _varint(buf, i)
        return i + length
    raise ValueError(f"unsupported wire type {wire}")


def _scan_mvt(buf):
    """[(layer_name, extent)] from a Mapbox Vector Tile. Strict wire-type
    walking (stdlib only) so random bytes cannot accidentally 'parse'."""
    layers, i, n = [], 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if field == 3 and wire == 2:
            length, i = _varint(buf, i)
            if length < 0 or i + length > n:
                raise ValueError("truncated layer")
            layers.append(_scan_layer(buf[i:i + length]))
            i += length
        else:
            i = _skip(buf, i, wire)
            if i > n:
                raise ValueError("truncated field")
    return layers


def _scan_layer(buf):
    name, extent, i, n = None, 4096, 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if field == 1 and wire == 2:
            length, i = _varint(buf, i)
            name = buf[i:i + length].decode("utf-8")
            i += length
        elif field == 5 and wire == 0:
            extent, i = _varint(buf, i)
        else:
            i = _skip(buf, i, wire)
            if i > n:
                raise ValueError("truncated layer field")
    return name, extent


# ---------------------------------------------------------------- per-kind checks

def _pil():
    from PIL import Image
    return Image


def _decode_image(blob):
    """PIL image, fully loaded. `.load()` is load-bearing: `open()` is lazy and
    a truncated tile only raises when the pixels are actually read."""
    import io
    image = _pil().open(io.BytesIO(blob))
    image.load()
    return image


def _entropy(gray):
    counts = gray.histogram()
    total = sum(counts) or 1
    out = 0.0
    for count in counts:
        if count:
            p = count / total
            out -= p * math.log2(p)
    return out


def _is_block_page(blob):
    """Is this payload somebody's error page rather than a tile?

    Checked before decoding, because a deny page saved as `.png` fails to
    decode and would otherwise be reported as a corrupt archive — sending the
    operator to rebuild when the real problem is that the source refused them.
    Case-insensitive over the first bytes only; a legitimate tile never starts
    with markup.
    """
    if not blob:
        return False
    head = bytes(blob[:64]).lower().lstrip()
    return any(head.startswith(marker) for marker in _BLOCK_PAGE_MARKERS)


def check_imagery(tiles):
    """Raster imagery: decodes, right size, not blank, has detail, and differs
    across the country."""
    Image = _pil()
    failures, sizes, entropies, blank, per_zoom, decoded = [], set(), [], 0, {}, 0
    codes = []

    def fail(code, message):
        codes.append(code)
        failures.append(message)

    for z, x, row, blob in tiles:
        if _is_block_page(blob):
            fail(SOURCE_BLOCKED,
                 f"tile z{z}/{x}/{row} is not an image at all — it is a server "
                 f"error/deny page stored as a tile ({blob[:40]!r})")
            continue
        try:
            image = _decode_image(blob)
        except Exception as exc:                                   # noqa: BLE001
            fail(ARCHIVE_CORRUPT,
                 f"tile z{z}/{x}/{row} does not decode as an image: {type(exc).__name__}")
            continue
        decoded += 1
        sizes.add(image.size)
        gray = image.convert("L").resize((32, 32), Image.NEAREST)
        lo, hi = gray.getextrema()
        if lo == hi:
            blank += 1
        entropies.append(_entropy(gray))
        per_zoom.setdefault(z, set()).add(hashlib.sha256(blob).hexdigest())

    if not decoded:
        # Ordering matters: if the reason nothing decoded is that every payload
        # is a deny page, say THAT rather than "corrupt" — they call for
        # different fixes (credentials/licence vs rebuild).
        return {"pass": False, "code": codes[0] if codes else ARCHIVE_CORRUPT,
                "failures": failures or ["no sampled tile could be decoded as an image"],
                "measured": {}}
    bad_sizes = sizes - VALID_DIMENSIONS
    if bad_sizes:
        fail(ARCHIVE_CORRUPT,
             f"unexpected tile dimensions {sorted(bad_sizes)} (expected 256 or 512 px)")
    if len(sizes) > 1:
        fail(ARCHIVE_CORRUPT,
             f"mixed tile dimensions {sorted(sizes)} — MapLibre needs one tileSize")
    blank_share = blank / decoded
    if blank_share > BLANK_TILE_RATIO:
        fail(CONTENT_DEGENERATE,
             f"{100.0 * blank_share:.0f}% of sampled tiles are blank/uniform "
             f"(limit {100.0 * BLANK_TILE_RATIO:.0f}%)")
    median_entropy = statistics.median(entropies)
    if median_entropy < ENTROPY_MEDIAN_MIN:
        fail(CONTENT_DEGENERATE,
             f"median tile entropy {median_entropy:.2f} bits is below {ENTROPY_MEDIAN_MIN} "
             f"— these are graphics, not map imagery")
    detailed = sum(1 for e in entropies if e >= ENTROPY_TILE_MIN) / len(entropies)
    if detailed < ENTROPY_TILE_MIN_SHARE:
        fail(CONTENT_DEGENERATE,
             f"only {100.0 * detailed:.0f}% of tiles carry any detail "
             f"(need {100.0 * ENTROPY_TILE_MIN_SHARE:.0f}%)")
    for z, hashes in per_zoom.items():
        if len(per_zoom[z]) < 2 and sum(1 for t in tiles if t[0] == z) >= 8:
            fail(CONTENT_DEGENERATE, f"every sampled tile at zoom {z} is the same image")
    return {"pass": not failures, "failures": failures,
            "code": codes[0] if codes else None,
            "measured": {"decoded": decoded, "dimensions": sorted(str(s) for s in sizes),
                         "blank_share": round(blank_share, 4),
                         "median_entropy": round(median_entropy, 2),
                         "detailed_share": round(detailed, 4)}}


def check_dem(tiles):
    """Terrarium raster-dem: real elevations, real relief."""
    Image = _pil()
    failures, varied, decoded, lo_all, hi_all = [], 0, 0, None, None
    codes = []

    def fail(code, message):
        codes.append(code)
        failures.append(message)

    for z, x, row, blob in tiles:
        if _is_block_page(blob):
            fail(SOURCE_BLOCKED,
                 f"tile z{z}/{x}/{row} is a server error/deny page, not elevation data")
            continue
        try:
            image = _decode_image(blob)
        except Exception as exc:                                   # noqa: BLE001
            fail(ARCHIVE_CORRUPT,
                 f"tile z{z}/{x}/{row} does not decode as PNG: {type(exc).__name__}")
            continue
        if image.mode == "P":
            fail(ARCHIVE_CORRUPT,
                 f"tile z{z}/{x}/{row} is palettised — quantised elevations are silently wrong")
            continue
        decoded += 1
        rgb = image.convert("RGB").resize((32, 32), Image.NEAREST)
        elevations = [r * 256 + g + b / 256 - 32768 for r, g, b in rgb.getdata()]
        tile_lo, tile_hi = min(elevations), max(elevations)
        lo_all = tile_lo if lo_all is None else min(lo_all, tile_lo)
        hi_all = tile_hi if hi_all is None else max(hi_all, tile_hi)
        if tile_hi - tile_lo > DEM_RELIEF_METRES:
            varied += 1
    if not decoded:
        return {"pass": False, "code": codes[0] if codes else ARCHIVE_CORRUPT,
                "failures": failures or ["no sampled tile decoded as a DEM tile"],
                "measured": {}}
    if lo_all < DEM_MIN_M or hi_all > DEM_MAX_M:
        fail(METADATA_INVALID,
             f"decoded elevations span {lo_all:.0f}..{hi_all:.0f} m, outside "
             f"[{DEM_MIN_M:.0f}, {DEM_MAX_M:.0f}] — the encoding is wrong")
    relief_share = varied / decoded
    if relief_share < DEM_VARIED_MIN_SHARE:
        fail(CONTENT_DEGENERATE,
             f"only {100.0 * relief_share:.0f}% of tiles have relief > {DEM_RELIEF_METRES:.0f} m "
             f"(need {100.0 * DEM_VARIED_MIN_SHARE:.0f}%) — a flat DEM is a failed warp")
    return {"pass": not failures, "failures": failures,
            "code": codes[0] if codes else None,
            "measured": {"decoded": decoded, "relief_share": round(relief_share, 4),
                         "elevation_min_m": round(lo_all, 1), "elevation_max_m": round(hi_all, 1)}}


def check_vector(tiles, declared_layers=()):
    """MVT: decompresses, parses, names layers the metadata declares."""
    failures, parsed, multilayer, names = [], 0, 0, set()
    codes = []

    def fail(code, message):
        codes.append(code)
        failures.append(message)

    for z, x, row, blob in tiles:
        payload, how = _decompress(blob)
        if not payload:
            fail(ARCHIVE_CORRUPT, f"tile z{z}/{x}/{row} decompressed ({how}) to zero bytes")
            continue
        if _is_block_page(payload):
            fail(SOURCE_BLOCKED,
                 f"tile z{z}/{x}/{row} is a server error/deny page, not a vector tile")
            continue
        try:
            layers = _scan_mvt(payload)
        except Exception as exc:                                   # noqa: BLE001
            fail(ARCHIVE_CORRUPT, f"tile z{z}/{x}/{row} is not a parseable vector tile: {exc}")
            continue
        if not layers:
            continue                       # an empty tile is legal (sea, gaps)
        parsed += 1
        if len(layers) >= 2:
            multilayer += 1
        for name, extent in layers:
            if name:
                names.add(name)
            if extent not in VALID_EXTENTS:
                fail(ARCHIVE_CORRUPT,
                     f"tile z{z}/{x}/{row} layer '{name}' has extent {extent}")
    if not parsed:
        return {"pass": False, "code": codes[0] if codes else CONTENT_DEGENERATE,
                "failures": failures or ["no sampled tile parsed as a vector tile"],
                "measured": {}}
    multilayer_share = multilayer / parsed
    if multilayer_share < VECTOR_MULTILAYER_MIN_SHARE:
        fail(CONTENT_DEGENERATE,
             f"only {100.0 * multilayer_share:.0f}% of tiles carry more than one layer "
             f"(need {100.0 * VECTOR_MULTILAYER_MIN_SHARE:.0f}%)")
    if declared_layers and not (names & set(declared_layers)):
        fail(METADATA_INVALID,
             f"tile layers {sorted(names)} do not intersect the declared "
             f"vector_layers {sorted(declared_layers)} — metadata and tiles disagree")
    return {"pass": not failures, "failures": failures,
            "code": codes[0] if codes else None,
            "measured": {"parsed": parsed, "layers": sorted(names),
                         "multilayer_share": round(multilayer_share, 4)}}


# ---------------------------------------------------------------- the engine

def inspect_archive(path, *, sample=SAMPLE_BUDGET, decode_budget=DECODE_BUDGET, anchors=SITES):
    """Full content verdict for one archive. Deterministic."""
    started = time.monotonic()
    con = _open(path)
    try:
        total = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
        meta = _metadata(con)
        if not total:
            return _verdict(False, "archive contains zero tiles", total=0, checked=0,
                            distinct=0, top_share=0.0, kind="unknown",
                            kind_evidence="no tiles", started=started,
                            code=TILE_COUNT_INVALID)

        tiles, survey = sample_tiles(con, budget=sample, anchors=anchors)
        if not tiles:
            return _verdict(False, "no tile could be read from the archive", total=total,
                            checked=0, distinct=0, top_share=0.0, kind="unknown",
                            kind_evidence="sampling returned nothing", started=started,
                            code=ARCHIVE_CORRUPT)

        # --- byte statistics: exact for small archives, sampled for large ones
        size_bytes = os.path.getsize(path)
        full = total <= FULL_SCAN_MAX_TILES and size_bytes <= FULL_SCAN_MAX_BYTES
        digests = {}
        if full:
            for (blob,) in con.execute("SELECT tile_data FROM tiles"):
                digest = hashlib.sha256(blob).hexdigest()
                digests[digest] = digests.get(digest, 0) + 1
        else:
            for _z, _x, _row, blob in tiles:
                digest = hashlib.sha256(blob).hexdigest()
                digests[digest] = digests.get(digest, 0) + 1
        checked = sum(digests.values())
        top_hash, top_n = max(digests.items(), key=lambda kv: kv[1])
        top_share = top_n / checked
        distinct = len(digests)

        kind, evidence = detect_kind(meta, tiles[0][3])

        placeholder = next((PLACEHOLDER_TILES[h] for h in digests if h in PLACEHOLDER_TILES), None)
        if placeholder:
            return _verdict(False, f"tiles are an upstream placeholder image, not map data: {placeholder}",
                            total=total, checked=checked, distinct=distinct, top_share=top_share,
                            kind=kind, kind_evidence=evidence, started=started, survey=survey,
                            code=PLACEHOLDER_CONTENT)
        # Before the degenerate rule, not after: an archive of deny pages is
        # usually ALSO 100% one image, and "98% identical" would send the
        # operator to rebuild when the actual problem is that the source
        # refused them. Name the cause, not the symptom.
        blocked = sum(1 for _z, _x, _row, blob in tiles if _is_block_page(blob))
        if blocked:
            return _verdict(False,
                            f"{blocked} of {len(tiles)} sampled tiles are a server error/deny "
                            f"page rather than map data — the source refused this download",
                            total=total, checked=checked, distinct=distinct, top_share=top_share,
                            kind=kind, kind_evidence=evidence, started=started, survey=survey,
                            code=SOURCE_BLOCKED)
        if top_share >= DEGENERATE_RATIO:
            return _verdict(False,
                            f"{100.0 * top_share:.1f}% of sampled tiles are the SAME image "
                            f"(sha256 {top_hash[:16]}…) — this archive carries no map data",
                            total=total, checked=checked, distinct=distinct, top_share=top_share,
                            kind=kind, kind_evidence=evidence, started=started, survey=survey,
                            code=CONTENT_DEGENERATE)
        floor = max(DISTINCT_FLOOR_MIN, int(DISTINCT_FLOOR_RATIO * checked))
        if distinct < floor:
            return _verdict(False,
                            f"only {distinct} distinct images across {checked} tiles (need {floor}) "
                            f"— an archive of a few repeated graphics is not a map",
                            total=total, checked=checked, distinct=distinct, top_share=top_share,
                            kind=kind, kind_evidence=evidence, started=started, survey=survey,
                            code=CONTENT_DEGENERATE)

        # --- decode a bounded, geographically spread subset
        step = max(1, len(tiles) // decode_budget)
        subset = tiles[::step][:decode_budget]
        if kind == "vector":
            declared = _declared_vector_layers(meta)
            result = check_vector(subset, declared)
        elif kind == "dem":
            result = check_dem(subset)
        elif kind == "imagery":
            result = check_imagery(subset)
            result["failures"].extend(_geographic_variation(subset, anchors))
            result["pass"] = not result["failures"]
        else:
            # Neither the metadata nor the magic bytes identify these payloads.
            # If they are markup, the archive is full of somebody's error page.
            blocked = any(_is_block_page(t[3]) for t in subset)
            result = {"pass": False,
                      "code": SOURCE_BLOCKED if blocked else METADATA_INVALID,
                      "failures": [f"tiles are a server error/deny page, not map data: {evidence}"
                                   if blocked else f"unrecognised archive kind: {evidence}"],
                      "measured": {}}

        reason = None if result["pass"] else result["failures"][0]
        return _verdict(result["pass"], reason, total=total, checked=checked, distinct=distinct,
                        top_share=top_share, kind=kind, kind_evidence=evidence, started=started,
                        survey=survey, failures=result["failures"], measured=result["measured"],
                        decoded=len(subset), code=result.get("code"))
    finally:
        con.close()


def _declared_vector_layers(meta):
    try:
        return [layer["id"] for layer in json.loads(meta.get("json", "{}")).get("vector_layers", [])]
    except Exception:                                              # noqa: BLE001
        return []


def _geographic_variation(subset, anchors):
    """The check that catches a poisoned archive from two sites alone: the
    deepest sampled zoom must not look identical across the country."""
    if not subset or not anchors:
        return []
    deepest = max(t[0] for t in subset)
    hashes = {hashlib.sha256(t[3]).hexdigest() for t in subset if t[0] == deepest}
    if len(hashes) < 3 and sum(1 for t in subset if t[0] == deepest) >= 3:
        return [f"tiles across Lebanon at zoom {deepest} are only {len(hashes)} distinct image(s) "
                f"— separate cities cannot legitimately look identical"]
    return []


def _verdict(ok, reason, *, total, checked, distinct, top_share, kind, kind_evidence,
             started, survey=None, failures=None, measured=None, decoded=0, code=None):
    out = {
        # --- keys the existing callers read; meaning unchanged
        "pass": bool(ok), "total": total, "checked": checked,
        "distinct": distinct, "top_share": round(top_share, 4),
        # --- additive
        "kind": kind, "kind_evidence": kind_evidence,
        # The machine-readable half of `reason`. None on success; on failure it
        # is what the content ledger stores and the availability API emits.
        "code": None if ok else (code or ARCHIVE_CORRUPT),
        "zooms": (survey or {}).get("zooms", {}),
        "cells_empty": (survey or {}).get("cells_empty", 0),
        "decoded": decoded,
        "failures": list(failures or ([] if ok else [reason] if reason else [])),
        "measured": measured or {},
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if reason:
        out["reason"] = reason
    return out


def content_check(path, sample=SAMPLE_BUDGET):
    """Is this archive real map data? (name and return keys preserved — the
    installer and the tests read `pass`, `reason`, `total`, `checked`,
    `distinct`, `top_share`)."""
    return inspect_archive(path, sample=sample)


def archive_sha256(path, chunk=4 * 1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------- coverage

def check_dataset(path, sites):
    con = _open(path)
    try:
        meta = _metadata(con)
        name = meta.get("name") or os.path.basename(path)
        try:
            zmin, zmax = int(meta["minzoom"]), int(meta["maxzoom"])
        except (KeyError, TypeError, ValueError):
            # A failed build leaves an archive with an empty metadata table.
            # That is a structural failure with a reason, not a crash.
            return {"id": name, "zooms_checked": [], "sites_checked": len(sites), "misses": [],
                    "metadata_valid": False,
                    "content": {"pass": False, "total": 0, "checked": 0, "distinct": 0,
                                "top_share": 0.0, "kind": "unknown", "failures": [],
                                "code": METADATA_INVALID,
                                "reason": "archive metadata declares no minzoom/maxzoom — "
                                          "incomplete or failed build"},
                    "pass": False}
        zooms = sorted({zmin, (zmin + zmax) // 2, zmax})
        misses = []
        for site, lon, lat in sites:
            for z in zooms:
                x, y = tile_xy(lon, lat, z)
                if _tile(con, z, x, 2 ** z - 1 - y) is None:
                    misses.append({"site": site, "z": z, "x": x, "y": y})
    finally:
        con.close()
    content = content_check(path)
    return {"id": name, "zooms_checked": zooms, "sites_checked": len(sites), "misses": misses,
            "metadata_valid": True, "content": content,
            "pass": (not misses) and content["pass"]}


def bbox_capacity(meta):
    """How many tiles the archive's OWN declared bbox × zoom range can hold.

    Returns None when the metadata does not declare enough to compute it. Used
    only to reject counts that cannot be explained by the declared footprint —
    an archive holding a fraction of a percent of its own extent is a build
    that died partway (the failed satellite run left 29 tiles of ~50,000).
    """
    try:
        zmin, zmax = int(meta["minzoom"]), int(meta["maxzoom"])
        west, south, east, north = [float(v) for v in str(meta["bounds"]).split(",")]
    except (KeyError, TypeError, ValueError):
        return None
    if zmax < zmin or east < west or north < south:
        return None
    total = 0
    for z in range(zmin, min(zmax, 20) + 1):
        x0, y0 = tile_xy(west, north, z)
        x1, y1 = tile_xy(east, south, z)
        total += (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
    return total


def validate_structure(path):
    """The half of validation that needs nothing but sqlite3.

    Split out so a preparation container without image decoders can still
    check what it is able to, rather than either skipping validation entirely
    or reporting an environment problem as corrupt data. Returns (ok, verdict).

    NEVER sufficient on its own: an archive of 145,718 error images passes
    every check in here. The content half is what catches that.
    """
    return _validate(path, content=False)


def validate_archive(path):
    """Structure + content for ONE archive, as the install gate runs it.

    Structure and content are BOTH required and are evaluated independently:
    a readable, well-formed SQLite file full of error images passes every
    structural check, and a genuine tile set inside a corrupt container is
    equally unusable. Returns (ok, verdict).
    """
    return _validate(path, content=True)


def _validate(path, content=True):
    con = _open(path)
    try:
        integrity = con.execute("PRAGMA quick_check").fetchone()[0]
        total = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
        meta = _metadata(con)
    finally:
        con.close()

    def refuse(code, reason):
        return False, {"pass": False, "code": code, "reason": reason, "kind": "unknown",
                       "total": total, "checked": 0, "distinct": 0, "top_share": 0.0,
                       "failures": [reason], "measured": {}}

    if integrity != "ok":
        return refuse(ARCHIVE_CORRUPT, f"SQLite quick_check failed: {integrity}")
    if not total:
        return refuse(TILE_COUNT_INVALID, "archive has no tiles")
    if not meta:
        # Both builders write the metadata table LAST, so tiles without any
        # metadata is the exact signature of a build that was interrupted.
        return refuse(BUILD_INCOMPLETE,
                      f"archive holds {total} tiles but its metadata table is empty — "
                      f"the build did not finish")
    missing = [k for k in ("minzoom", "maxzoom", "format") if not meta.get(k)]
    if missing:
        return refuse(METADATA_INVALID,
                      f"archive metadata declares no {', '.join(missing)} — "
                      f"incomplete or failed build")

    # The declared zoom range must actually be populated. This is the sharpest
    # signal of an interrupted build and the one that catches the real case:
    # the failed satellite run left 29 tiles at z8-z9 while its metadata
    # declared z8-z14, so every count-based rule found it plausible.
    con = _open(path)
    try:
        present = {row[0] for row in con.execute("SELECT DISTINCT zoom_level FROM tiles")}
    finally:
        con.close()
    try:
        zmin, zmax = int(meta["minzoom"]), int(meta["maxzoom"])
    except (TypeError, ValueError):
        return refuse(METADATA_INVALID, "archive metadata declares unparseable zoom levels")
    empty = [z for z in range(zmin, zmax + 1) if z not in present]
    if empty:
        return refuse(BUILD_INCOMPLETE,
                      f"archive declares zooms {zmin}-{zmax} but has no tiles at "
                      f"{empty} — the build stopped before finishing "
                      f"(tiles exist only at {sorted(present)})")

    capacity = bbox_capacity(meta)
    if capacity:
        # Measured ratios of tiles held to footprint capacity: DEM 1.00,
        # vector 0.81, the interrupted satellite fragment 0.0025. One percent
        # is far below any legitimately sparse archive and far above a fragment.
        floor = max(8, capacity // 100)
        if total < floor:
            return refuse(TILE_COUNT_INVALID,
                          f"archive holds {total} tiles but its declared bbox and zoom "
                          f"range span {capacity} — at least {floor} are needed for this "
                          f"to be a finished build")
        if total > capacity * 3:
            return refuse(TILE_COUNT_INVALID,
                          f"archive holds {total} tiles, more than 3x the {capacity} its "
                          f"declared bbox and zoom range can cover — the metadata does "
                          f"not describe these tiles")

    if not content:
        return True, {"pass": True, "code": None, "reason": None,
                      "kind": "unchecked", "total": total, "checked": 0,
                      "distinct": 0, "top_share": 0.0, "failures": [],
                      "measured": {}, "structural_only": True}

    verdict = inspect_archive(path)
    return bool(verdict["pass"]), verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--production", default="/app/map-data/production")
    ap.add_argument("--dsn", default=None, help="sync or async PostgreSQL URL; default: settings.DATABASE_URL")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--coverage-only", action="store_true",
                    help="exit code reflects tile COVERAGE only; content verdicts are still printed")
    ap.add_argument("--archive", default=None,
                    help="validate ONE archive (structure + content) and exit; the install gate")
    args = ap.parse_args()

    if args.archive:
        ok, verdict = validate_archive(args.archive)
        if args.json:
            print(json.dumps(verdict, indent=1))
        elif ok:
            print(f"content ok [{verdict['kind']}] — {verdict['distinct']} distinct images in "
                  f"{verdict['checked']} tiles (most common {100.0 * verdict['top_share']:.1f}%), "
                  f"{verdict.get('decoded', 0)} decoded in {verdict.get('elapsed_ms', 0)} ms")
            if verdict.get("measured"):
                print(f"  {verdict['measured']}")
        else:
            print(f"content check FAILED [{verdict.get('kind', '?')}]: {verdict.get('reason')}", file=sys.stderr)
            for extra in verdict.get("failures", [])[1:5]:
                print(f"  also: {extra}", file=sys.stderr)
        return 0 if ok else 1

    dsn = (args.dsn if args.dsn is not None else _settings_dsn()).replace("postgresql+asyncpg://", "postgresql://")
    sites = [(n, lon, lat) for n, (lon, lat) in SITES.items()] + camera_sites(dsn)
    archives = sorted(glob.glob(os.path.join(args.production, "*.mbtiles")))
    if not archives:
        print(f"no .mbtiles in {args.production}", file=sys.stderr)
        return 1
    results = [check_dataset(a, sites) for a in archives]
    if args.json:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            # the headline label reflects what the exit code reflects
            headline = (not r["misses"]) if args.coverage_only else r["pass"]
            print(f"{r['id']}: {'PASS' if headline else 'FAIL'} — {r['sites_checked']} sites × z{r['zooms_checked']}")
            for m in r["misses"]:
                print(f"    MISSING {m['site']} z{m['z']}/{m['x']}/{m['y']}")
            c = r["content"]
            if c["pass"]:
                print(f"    content OK [{c['kind']}] — {c['distinct']} distinct in {c['checked']} tiles "
                      f"(most common {100.0 * c['top_share']:.1f}%), {c.get('decoded', 0)} decoded, "
                      f"{c.get('elapsed_ms', 0)} ms")
                if c.get("measured"):
                    print(f"      {c['measured']}")
            else:
                print(f"    CONTENT FAIL [{c.get('kind', '?')}] — {c.get('reason', 'unknown')}")
                for extra in c.get("failures", [])[1:4]:
                    print(f"      also: {extra}")
    if args.coverage_only:
        return 0 if all(not r["misses"] for r in results) else 1
    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
