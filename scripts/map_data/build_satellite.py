#!/usr/bin/env python3
"""Sentinel-2 L2A true-color COGs -> Lebanon satellite raster MBTiles.

Runs INSIDE the GDAL preparation container (never the app runtime). Use the
wrapper, which mounts the right volumes and installs the result:

    scripts/map_data/build_satellite.sh

or directly:

    docker run --rm -v <repo>/map-data:/map-data -v <repo>/scripts:/scripts:ro \
        ghcr.io/osgeo/gdal:ubuntu-full-3.13.2 \
        python3 /scripts/map_data/build_satellite.py \
        --manifest /map-data/source/satellite/sentinel2_manifest.json \
        --out /map-data/production/lebanon-satellite.mbtiles.new

REAL imagery: the `visual` asset of each scene is the L2A True Colour Image
(bands B04/B03/B02, 10 m GSD, atmospherically corrected). Documented limit:
10 m is genuine satellite imagery, not aerial-photo resolution — a car is a
pixel. Higher resolution later means a licensed commercial provider, never
scraped consumer tiles.

Provenance and licence: Copernicus Sentinel-2, distributed by AWS Open Data
(Element84 earth-search STAC + the public sentinel-cogs bucket). Copernicus
data is "free, full and open"; offline storage and redistribution in a derived
product are permitted with attribution, which the archive carries in its
metadata as "Contains modified Copernicus Sentinel data <year>". No
credentials are involved. Google, Bing, Esri and Mapbox imagery is NOT an
alternative here: none of their terms permit bulk offline storage.

Download, then build
--------------------
The scenes are downloaded to local files FIRST, and the mosaic is built from
those files. The previous version streamed them through GDAL's /vsicurl with
no timeout, no status check, no length check and no checksum; it hung for
about 16 hours, exited 1 on a COG truncated at 1,820,566 of 2,067,792 bytes,
and left a 29-tile archive with an empty metadata table under the final
filename — which its own "refusing to overwrite" guard then turned into a
build that could not be re-run without a manual rm.

Downloading first means every byte is checked before any of it is interpreted
(scripts/map_data/build_helpers.fetch), a failed scene is named exactly, and a
re-run resumes instead of restarting. It costs local disk, which is why the
disk preflight is sized from the actual Content-Lengths and refuses before the
first byte rather than filling the volume.

The archive is assembled at <out>.part and promoted to <out> only after it
passes the same structural + content validation the installer runs. An
interrupted build therefore leaves NO <out> at all.
"""

import argparse
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

import importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_helpers as bh                                         # noqa: E402

# GDAL is NOT an application dependency: this script runs inside the
# ghcr.io/osgeo/gdal preparation container only. Loaded dynamically so the
# application image never appears to require it.
try:
    gdal = importlib.import_module("osgeo.gdal")
except ImportError as _exc:  # pragma: no cover
    raise SystemExit("build_satellite.py must run inside the GDAL container "
                     "(ghcr.io/osgeo/gdal:ubuntu-full-*): osgeo is not importable here") from _exc

gdal.UseExceptions()
gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
gdal.SetConfigOption("VSI_CACHE", "TRUE")
gdal.SetConfigOption("VSI_CACHE_SIZE", "268435456")
gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")

# Passed to CHILD processes explicitly. gdal.SetConfigOption above is
# in-process only, so the twelve gdalwarp children of the previous version ran
# with none of it — no retries and, more importantly, no timeouts.
# Applied to BOTH the child processes and this process. Splitting them is how
# the original failed: gdal.SetConfigOption is in-process only, so its twelve
# gdalwarp children ran unbounded — and the reverse mistake is just as bad,
# because the tiling loop below reads the mosaic over /vsicurl IN-PROCESS.
# One dict, applied twice, so the two cannot drift apart.
GDAL_CHILD_ENV = {
    "GDAL_HTTP_TIMEOUT": "120",
    "GDAL_HTTP_CONNECTTIMEOUT": "30",
    "GDAL_HTTP_LOW_SPEED_TIME": "60",
    "GDAL_HTTP_LOW_SPEED_LIMIT": "1000",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "5",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
}

# Same bounds the in-process reads must honour.
for _key, _value in GDAL_CHILD_ENV.items():
    gdal.SetConfigOption(_key, _value)

BBOX_WGS84 = (34.80, 32.84, 36.92, 34.89)   # W, S, E, N — Lebanon, matches MAP_BOUNDS_*
MIN_Z, MAX_Z = 8, 14
TILE = 256
JPEG_QUALITY = "85"

# Only these hosts may be downloaded from. An open-ended manifest is a way to
# smuggle in imagery whose licence forbids offline storage.
ALLOWED_HOSTS = {
    "sentinel-cogs.s3.us-west-2.amazonaws.com",
    "sentinel-cogs.s3.amazonaws.com",
}

# Average size of one JPEG q85 256px true-colour tile, MEASURED from the
# archives this pipeline produces. Used to size the disk requirement from the
# real tile capacity of the bbox rather than from a guess.
AVG_TILE_BYTES = 24 * 1024

# Headroom for GDAL's temporary rasters and the SQLite journal while the
# archive is assembled. The archive itself is the dominant cost.
WORKING_MULTIPLIER = 2.0

# The materialised mosaic is the Lebanon-clipped subset of the sources, DEFLATE
# compressed, so it is a fraction of their total size; gdaladdo then adds the
# usual ~1/3 for the overview pyramid. Both are working files inside the
# throwaway build directory, but they have to fit.
MOSAIC_FRACTION = 0.6
OVERVIEW_OVERHEAD = 1.4


def estimate_output_bytes():
    """Bytes the finished archive is expected to occupy.

    Derived from the real tile capacity of the declared bbox and zoom range —
    the same arithmetic coverage_check.bbox_capacity() uses to judge whether an
    archive is plausibly complete — times a measured average tile size. Nothing
    here is a constant that goes stale when the bbox or zoom range changes.
    """
    west, south, east, north = BBOX_WGS84
    tiles = 0
    for z in range(MIN_Z, MAX_Z + 1):
        x0, y0 = bh.lonlat_to_tile(west, north, z)
        x1, y1 = bh.lonlat_to_tile(east, south, z)
        tiles += (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
    return tiles * AVG_TILE_BYTES


def load_manifest(path):
    """Read and CHECK the manifest. Every field this build depends on."""
    try:
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as exc:
        raise bh.BuildError(bh.BUILD_INCOMPLETE,
                            f"manifest {path} is missing or unreadable: {exc}") from exc
    scenes = manifest.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise bh.BuildError(bh.BUILD_INCOMPLETE,
                            f"manifest {path} lists no scenes; there is nothing to build")
    for index, scene in enumerate(scenes):
        href = (scene or {}).get("visual_href")
        if not href:
            raise bh.BuildError(bh.BUILD_INCOMPLETE,
                                f"scene {index} in {path} has no visual_href")
        host = urlsplit(href).hostname
        if host not in ALLOWED_HOSTS:
            raise bh.BuildError(
                bh.SOURCE_BLOCKED,
                f"scene {index} points at {host}, which is not an allowed imagery "
                f"source {sorted(ALLOWED_HOSTS)}. Imagery from consumer map providers "
                f"may not be stored offline.", host=host)
    if not manifest.get("acquisition_date"):
        raise bh.BuildError(bh.BUILD_INCOMPLETE,
                            f"manifest {path} declares no acquisition_date; the archive "
                            f"could not carry correct attribution")
    return manifest


def _open_raster(path):
    """Validation callback for fetch(): a COG that GDAL cannot open is not a COG."""
    ds = gdal.Open(path)
    if ds is None or ds.RasterCount < 3:
        raise ValueError(f"{path} did not open as a 3-band raster")
    ds = None


def probe_scenes(manifest, log=print):
    """HEAD every scene BEFORE a single pixel is read.

    Streaming means a licence, availability or host problem would otherwise
    surface deep inside a multi-hour warp. One round-trip per scene turns that
    into a failure in seconds, and gives the provenance record its measured
    size / ETag / Last-Modified without downloading anything.

    Returns [{index, href, size_bytes, etag, last_modified}, ...].
    """
    import urllib.request

    scenes = manifest["scenes"]
    records, failures = [], []
    log(f"probing {len(scenes)} scenes (HEAD)")
    for index, scene in enumerate(scenes):
        href = scene["visual_href"]
        try:
            request = urllib.request.Request(href, method="HEAD")
            with urllib.request.urlopen(request, timeout=bh.CONNECT_TIMEOUT) as resp:
                ctype = resp.headers.get("Content-Type") or ""
                if "html" in ctype.lower() or "json" in ctype.lower():
                    raise bh.BuildError(
                        bh.SOURCE_BLOCKED,
                        f"scene {index} answered Content-Type {ctype!r} — that is an "
                        f"error page, not imagery", url=href)
                records.append({
                    "index": index,
                    "href": href,
                    "size_bytes": int(resp.headers.get("Content-Length") or 0) or None,
                    "etag": (resp.headers.get("ETag") or "").strip('"') or None,
                    "last_modified": resp.headers.get("Last-Modified"),
                    "content_type": ctype or None,
                })
        except bh.BuildError:
            raise
        except Exception as exc:                                   # noqa: BLE001
            failures.append(f"scene {index} ({scene.get('id')}): "
                            f"{type(exc).__name__}: {exc}")

    if failures:
        raise bh.BuildError(
            bh.DOWNLOAD_FAILED,
            f"{len(failures)} of {len(scenes)} scenes are not reachable; the first "
            f"was {failures[0]}. Nothing has been read.",
            failed=len(failures), total=len(scenes))

    total = sum(r["size_bytes"] or 0 for r in records)
    log(f"  all {len(records)} scenes reachable, {total / 1024 ** 3:.2f} GB of source "
        f"imagery (only the windows intersecting Lebanon are read)")
    return records


def record_provenance(manifest_path, manifest, records, log=print):
    """Write the measured HEAD facts back into the manifest, atomically.

    The manifest recorded 0/12 sizes and 0/12 checksums, so a re-download on
    another machine could verify nothing at all. These are values a HEAD gives
    us for free.

    Deliberately NOT a content digest: streaming never reads a whole file, so a
    per-scene sha256 cannot be computed here, and S3 returns no
    x-amz-checksum-sha256 for these objects. The ETag is a multipart composite
    (the observed one ends "-35", i.e. 35 parts), which identifies the object
    VERSION but is not an MD5 of its bytes. What proves the CONTENT of what
    ships is the ledger verdict on the built archive.
    """
    by_index = {r["index"]: r for r in records}
    changed = 0
    for index, scene in enumerate(manifest["scenes"]):
        record = by_index.get(index)
        if not record:
            continue
        for key in ("size_bytes", "etag", "last_modified"):
            if record.get(key) and scene.get(key) != record[key]:
                scene[key] = record[key]
                changed = 1
    manifest["provenance_note"] = (
        "size_bytes / etag / last_modified are MEASURED from HTTP HEAD at build "
        "time. The ETag is a multipart composite, not a content digest, and S3 "
        "publishes no sha256 for these assets. Per-scene sha256 is only "
        "available via the download path (build_helpers.fetch); the content of "
        "the archive this build produces is proven by its ledger verdict in "
        "map-data/metadata/content_verdicts.json."
    )
    if not changed:
        log("  provenance already current")
        return
    tmp = manifest_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, indent=1)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, manifest_path)
    log(f"  recorded size/etag/last_modified for {len(records)} scenes")


def download_scenes(manifest, cache_dir, records, *, reserve_gb, log=print):
    """Fetch every scene to a local file, verified, before anything is read.

    Chosen over streaming because this link demonstrably truncates responses:
    the streamed build died on "TIFFFillTile:Read error ... got 607463 bytes,
    expected 1274276" - the same defect class as the original 16-hour failure.
    A range read that comes up short is unrecoverable mid-build; a download
    RESUMES from where it stopped.

    Serial on purpose. Concurrency helps a fast link and hurts a fragile one,
    and the failure mode here is short responses, not queueing.
    """
    os.makedirs(cache_dir, exist_ok=True)
    scenes = manifest["scenes"]
    by_index = {r["index"]: r for r in records}
    paths = []

    for index, scene in enumerate(scenes):
        href = scene["visual_href"]
        name = f"{scene.get('id') or 'scene'}_{index:02d}_visual.tif"
        dest = os.path.join(cache_dir, name)
        expected = by_index.get(index, {}).get("size_bytes")
        have = os.path.getsize(dest) if os.path.exists(dest) else 0
        log(f"scene {index + 1}/{len(scenes)} {name} "
            f"({(expected or 0) / 1024 ** 2:.0f} MB)"
            + (f" - {have / 1024 ** 2:.0f} MB already local" if have else ""))
        bh.fetch(href, dest, allowed_hosts=ALLOWED_HOSTS,
                 expected_sha256=scene.get("sha256"), validate=_open_raster,
                 reserve_gb=reserve_gb, log=log)
        actual = os.path.getsize(dest)
        if expected and actual != expected:
            raise bh.BuildError(
                bh.DOWNLOAD_FAILED,
                f"{name} is {actual} bytes but the server declared {expected}",
                url=href)
        paths.append(dest)
    return paths


def record_digests(manifest_path, manifest, paths, log=print):
    """Record each scene sha256 now that whole files exist locally.

    This is what streaming could not do. Once written, every later run
    verifies against it and CHECKSUM_MISMATCH becomes a real guard rather than
    a branch that never fires. Trust-on-first-use: it is a measurement of the
    bytes we received, not a publisher digest, because S3 publishes none for
    these assets.
    """
    changed = 0
    for scene, path in zip(manifest["scenes"], paths):
        digest = bh.sha256_file(path)
        if scene.get("sha256") != digest:
            scene["sha256"] = digest
            changed += 1
    if not changed:
        log("  digests already recorded")
        return
    tmp = manifest_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, indent=1)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, manifest_path)
    log(f"  recorded sha256 for {changed} scene(s)")


def build_archive(sources, out_part, manifest, work, log=print):
    """Warp, mosaic and tile into `out_part`. Writes metadata FIRST.

    Reads LOCAL, already-verified files: no network I/O happens here at all,
    so a flaky link cannot truncate a read mid-build. The GDAL HTTP timeouts
    are still applied to this process and to every child, because they cost
    nothing and any future remote read must stay bounded.
    """
    import sqlite3

    warped = []
    for i, src in enumerate(sources):
        wv = os.path.join(work, f"w{i}.vrt")
        # Scenes are in several UTM zones (36N/37N); the VRT needs one CRS,
        # so warp each to 3857 lazily via a warped VRT first.
        log(f"  warping scene {i + 1}/{len(sources)}")
        bh.run(["gdalwarp", "-of", "VRT", "-t_srs", "EPSG:3857", "-r", "cubic",
                "-te_srs", "EPSG:4326", "-te", *map(str, BBOX_WGS84), src, wv],
               timeout=1800, env=GDAL_CHILD_ENV, log=log)
        warped.append(wv)

    vrt = os.path.join(work, "s2.vrt")
    bh.run(["gdalbuildvrt", "-resolution", "highest", "-srcnodata", "0",
            "-vrtnodata", "0", vrt, *warped], timeout=1800, env=GDAL_CHILD_ENV, log=log)

    # Materialise the mosaic, then build overviews. This is the difference
    # between a build that finishes and one that does not.
    #
    # A VRT has no overviews, so EVERY tile - including z8, where one tile
    # covers the whole country - reads and downsamples the full
    # 19830x23096x3 source. Measured without this step: 29 tiles (z8:9, z9:20)
    # in ~23 minutes at 99% CPU, which is byte-for-byte the same 294,912-byte
    # fragment the original build left behind after ~16 hours. That fragment
    # was never evidence of an early crash; it is where this pipeline stalls.
    #
    # gdaladdo gives each zoom a right-sized level to read: z8 comes off a
    # /256 overview of a few hundred pixels instead of 458 megapixels.
    mosaic = os.path.join(work, "s2.tif")
    log("materialising the mosaic (one full pass over the sources)")
    bh.run(["gdal_translate", "-of", "GTiff", "-co", "TILED=YES",
            "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=2",
            "-co", "NUM_THREADS=ALL_CPUS", "-co", "BIGTIFF=IF_SAFER",
            vrt, mosaic], timeout=7200, env=GDAL_CHILD_ENV, log=log)
    log("building overviews (this is what makes the low zooms cheap)")
    bh.run(["gdaladdo", "-r", "average", "--config", "COMPRESS_OVERVIEW", "DEFLATE",
            "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
            mosaic, "2", "4", "8", "16", "32", "64", "128", "256"],
           timeout=7200, env=GDAL_CHILD_ENV, log=log)

    ds = gdal.Open(mosaic)
    band = ds.GetRasterBand(1)
    log(f"mosaic {ds.RasterXSize}x{ds.RasterYSize} px, {ds.RasterCount} bands, "
        f"{band.GetOverviewCount()} overview levels")
    if band.GetOverviewCount() == 0:
        raise bh.BuildError(
            bh.BUILD_INCOMPLETE,
            "the mosaic has no overviews, so every tile would read the full "
            "resolution source - the build would take days. gdaladdo did not "
            "produce them.")

    con = sqlite3.connect(out_part)
    cur = con.cursor()
    cur.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    cur.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                "tile_row INTEGER, tile_data BLOB)")

    w, s, e, n = BBOX_WGS84
    year = str(manifest.get("acquisition_date", ""))[:4]
    # Metadata FIRST, not last. Written last, an interrupted build produced an
    # archive with tiles and an empty metadata table — indistinguishable from a
    # finished one to anything that only looks at the tile table.
    for k, v in {
        "name": "lebanon-satellite", "format": "jpg", "type": "baselayer", "version": "1",
        "minzoom": str(MIN_Z), "maxzoom": str(MAX_Z), "bounds": f"{w},{s},{e},{n}",
        "center": f"{(w + e) / 2},{(s + n) / 2},{MIN_Z}",
        "description": (f"Sentinel-2 L2A true colour (10 m), {len(sources)} scenes acquired "
                        f"{manifest.get('acquisition_date')}, clipped to Lebanon, EPSG:3857."),
        "attribution": f"Contains modified Copernicus Sentinel data {year}",
    }.items():
        cur.execute("INSERT INTO metadata VALUES (?,?)", (k, v))
    con.commit()

    jpeg = gdal.GetDriverByName("JPEG")
    count = 0
    for z in range(MIN_Z, MAX_Z + 1):
        x0, y0 = bh.lonlat_to_tile(w, n, z)
        x1, y1 = bh.lonlat_to_tile(e, s, z)
        started = time.monotonic()
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                minx, miny, maxx, maxy = bh.tile_bounds_3857(z, x, y)
                # gdal.Translate, NOT gdal.ReprojectImage. Translate selects an
                # overview level appropriate to the requested output size;
                # ReprojectImage always reads full resolution. Measured on a
                # raster with overviews: 5.9 ms/tile versus 1991.9 ms/tile, a
                # 338x difference, and the reason the original build produced
                # exactly 29 tiles before it looked like it had hung.
                #
                # The mosaic is already EPSG:3857, so this is a windowed read
                # plus a resize rather than a reprojection.
                try:
                    tgt = gdal.Translate(
                        "", ds, format="MEM",
                        projWin=[minx, maxy, maxx, miny],
                        width=TILE, height=TILE, resampleAlg="average")
                except RuntimeError:
                    continue                     # window entirely off the mosaic
                if tgt is None:
                    continue
                arr = tgt.ReadAsArray()
                if arr is None or not arr.any():
                    continue                     # entirely outside imagery
                tmp = os.path.join(work, "t.jpg")
                jpeg.CreateCopy(tmp, tgt, options=["QUALITY=" + JPEG_QUALITY])
                with open(tmp, "rb") as fh:
                    data = fh.read()
                cur.execute("INSERT INTO tiles VALUES (?,?,?,?)",
                            (z, x, (1 << z) - 1 - y, sqlite3.Binary(data)))
                count += 1
        con.commit()
        log(f"z{z}: cumulative {count} tiles ({time.monotonic() - started:.0f}s)")

    cur.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    con.commit()
    con.close()
    return count


def validate_part(out_part):
    """The builder checks its own output before claiming success.

    Runs the same gate install_dataset.py runs — but the CONTENT half needs
    image decoders (Pillow), and the GDAL preparation container has none.
    A validator that cannot run is not evidence that the data is bad, so in
    that case the structural half still runs, the gap is stated plainly, and
    the archive is kept. Nothing can be installed without passing the full
    gate: install_dataset.py executes it inside the api container, which does
    have the decoders.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import coverage_check
    try:
        ok, verdict = coverage_check.validate_archive(out_part)
    except ModuleNotFoundError as exc:
        print(f"NOTE: content validation unavailable here ({exc}); running the "
              f"structural checks only. The full content gate runs at install "
              f"time inside the api container, and refuses anything that fails.",
              flush=True)
        ok, verdict = coverage_check.validate_structure(out_part)
        if not ok:
            raise bh.BuildError(verdict.get("code") or bh.ARCHIVE_CORRUPT,
                                f"the archive this build produced failed its "
                                f"structural checks: {verdict.get('reason')}")
        verdict["content_validated"] = False
        return verdict
    if not ok:
        raise bh.BuildError(verdict.get("code") or bh.ARCHIVE_CORRUPT,
                            f"the archive this build produced did not pass validation: "
                            f"{verdict.get('reason')}")
    verdict["content_validated"] = True
    return verdict


def _discard_part(out_part):
    """Never leave a partial archive where a caller might install it."""
    if os.path.exists(out_part):
        try:
            os.unlink(out_part)
            print(f"removed the partial archive {os.path.basename(out_part)}", flush=True)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="Build the Lebanon satellite MBTiles.")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True, help="write to <name>.mbtiles.new; caller installs")
    ap.add_argument("--cache-dir", default=None,
                    help="where scene COGs are kept (default: beside the manifest)")
    ap.add_argument("--disk-reserve-gb", type=float, default=bh.DEFAULT_RESERVE_GB,
                    help="free space that must remain after the build")
    args = ap.parse_args()

    out_part = args.out + ".part"
    try:
        manifest = load_manifest(args.manifest)
        cache = args.cache_dir or os.path.join(os.path.dirname(args.manifest), "cogs")
        print(f"{len(manifest['scenes'])} scenes, acquisition "
              f"{manifest.get('acquisition_date')}", flush=True)

        # Preflight from what this build will actually WRITE. Streaming reads
        # only the windows intersecting Lebanon, so the 2.80 GB of source COGs
        # are never stored locally — the cost is the archive plus GDAL's temp.
        #
        # The previous constant (sources x 6) was a guess: against 2.80 GB of
        # sources it demanded 26.8 GB including the reserve, and would have
        # REFUSED a build that needs about 3.5 GB. A guard that blocks correct
        # work is as bad as one that never fires, so the number is derived.
        # HEAD first: reachable, allowed host, not an error page - and the
        # measured sizes the disk estimate below needs.
        records = probe_scenes(manifest)
        record_provenance(args.manifest, manifest, records)

        source_bytes = sum(r["size_bytes"] or 0 for r in records)
        # Only what still has to be fetched. Scenes already downloaded and
        # checksum-verified are skipped by fetch(), so charging for them again
        # made this guard refuse a build whose real remaining cost was ~3 GB.
        already = 0
        for index, scene in enumerate(manifest["scenes"]):
            name = f"{scene.get('id') or 'scene'}_{index:02d}_visual.tif"
            local = os.path.join(cache, name)
            if os.path.exists(local) and scene.get("sha256"):
                already += os.path.getsize(local)
        to_fetch = max(0, source_bytes - already)
        if already:
            print(f"{already / 1024 ** 3:.2f} GB of sources already local and verified; "
                  f"{to_fetch / 1024 ** 3:.2f} GB still to fetch", flush=True)
        archive_bytes = estimate_output_bytes()
        print(f"estimated archive: {archive_bytes / 1024 ** 2:.0f} MB "
              f"({int(archive_bytes / AVG_TILE_BYTES):,} tiles at "
              f"{AVG_TILE_BYTES // 1024} KB)", flush=True)
        # Sources are stored locally now, so they count. Derived from the
        # measured Content-Lengths, never a constant.
        # Sources are stored locally, the mosaic is materialised, and gdaladdo
        # adds roughly a third again in overviews. Derived from the measured
        # Content-Lengths and the real tile capacity, never a constant.
        mosaic_bytes = int(source_bytes * MOSAIC_FRACTION * OVERVIEW_OVERHEAD)
        bh.disk_preflight(cache,
                          to_fetch + mosaic_bytes
                          + int(archive_bytes * WORKING_MULTIPLIER),
                          reserve_gb=args.disk_reserve_gb, label="satellite build")

        sources = download_scenes(manifest, cache, records,
                                  reserve_gb=args.disk_reserve_gb)
        record_digests(args.manifest, manifest, sources)

        if os.path.exists(out_part):
            os.unlink(out_part)
        with tempfile.TemporaryDirectory() as work:
            count = build_archive(sources, out_part, manifest, work)
        print(f"built {count} tiles z{MIN_Z}-{MAX_Z}; validating before promoting", flush=True)

        verdict = validate_part(out_part)
        bh.atomic_promote(out_part, args.out)
        print(f"wrote {args.out}: {count} tiles, {verdict.get('distinct')} distinct, "
              f"kind={verdict.get('kind')}")
        print(f"install it with:\n  scripts/map_data/install_dataset.sh "
              f"{args.out} lebanon-satellite --restart-martin")
        return 0
    except bh.BuildError as exc:
        _discard_part(out_part)
        print(f"FAILED {exc}", file=sys.stderr)
        return 1
    except Exception as exc:                                       # noqa: BLE001
        # GDAL raises bare RuntimeErrors — a truncated range read surfaces as
        # "TIFFFillTile:Read error ... got 607463 bytes, expected 1274276",
        # which is the ORIGINAL defect class. Catching only BuildError left
        # that uncaught, so the partial archive survived: measured, a failed
        # run left lebanon-satellite.mbtiles.new.part behind, which
        # production_gate.py then flags as an orphan.
        #
        # The message is re-raised verbatim, never swallowed: it names the
        # exact byte counts, which is what tells an operator this was a short
        # read rather than corrupt source data.
        # An ENVIRONMENT failure is not a data failure. A missing decoder or
        # import says nothing about the archive, and discarding it there threw
        # away 215 seconds of correct tiling once already. Keep the artifact,
        # name the real problem, and let the installer gate decide.
        if isinstance(exc, (ModuleNotFoundError, ImportError)):
            print(f"FAILED [{bh.BUILD_INCOMPLETE}] the build environment is "
                  f"incomplete: {type(exc).__name__}: {exc}. "
                  f"{os.path.basename(out_part)} was KEPT — it is unvalidated, "
                  f"not known-bad.", file=sys.stderr)
            return 1
        _discard_part(out_part)
        code = (bh.DOWNLOAD_FAILED if "Read error" in str(exc) or "IReadBlock" in str(exc)
                else bh.ARCHIVE_CORRUPT)
        print(f"FAILED [{code}] the build did not complete: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
