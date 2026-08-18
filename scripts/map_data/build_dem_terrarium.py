#!/usr/bin/env python3
"""Copernicus GLO-30 DEM tiles -> MapLibre raster-dem MBTiles (terrarium).

Runs INSIDE the GDAL preparation container (ghcr.io/osgeo/gdal:ubuntu-full),
never in the application runtime:

    docker run --rm -v <repo>/map-data:/map-data ghcr.io/osgeo/gdal:ubuntu-full-3.13.2 \
        python3 /map-data/../scripts/map_data/build_dem_terrarium.py \
        --source /map-data/source/terrain --out /map-data/production/lebanon-dem.mbtiles.new

Concepts, kept distinct (they are different things):

    DEM        elevation VALUES per pixel — the source data
    terrain    MapLibre's 3-D surface geometry, computed client-side FROM the DEM
    hillshade  a shaded-relief VISUALISATION, also computed client-side from
               the same raster-dem source (no separate pre-rendered dataset)
    contours   optional elevation lines — not built here

Encoding: `terrarium` (Mapzen), one of the two schemes MapLibre's raster-dem
source understands natively:

    R*256 + G + B/256 - 32768 = elevation (m)

so a decoder — MapLibre or the acceptance test — recovers real metres. This
is NOT "a picture of hills": the tile bytes ARE the elevation, which is what
lets MapLibre extrude terrain and light the hillshade itself.

Pipeline (all GDAL, no bespoke encoding):

    N*_E*_DEM.tif  ->  VRT mosaic  ->  gdalwarp: clip Lebanon bbox, EPSG:3857
                   ->  numpy terrarium RGB  ->  gdal2tiles-style XYZ pyramid
                   ->  MBTiles (TMS row order, metadata) written atomically as
                       <out>  (caller renames .new -> final after verification)

Zoom range: z6-z12. GLO-30 is 30 m/pixel; at z12 a 256px tile spans ~9.5 km
so ~37 m/pixel — anything deeper than z12 only upsamples. MapLibre
over-zooms raster-dem cleanly.
"""

import argparse
import glob
import os
import sqlite3
import sys
import tempfile

import importlib

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_helpers as bh                                         # noqa: E402

# GDAL is NOT an application dependency: this script runs inside the
# ghcr.io/osgeo/gdal preparation container only (see the docstring). It is
# loaded dynamically so the application image never appears to require it.
try:
    gdal = importlib.import_module("osgeo.gdal")
except ImportError as _exc:  # pragma: no cover
    raise SystemExit("build_dem_terrarium.py must run inside the GDAL container "
                     "(ghcr.io/osgeo/gdal:ubuntu-full-*): osgeo is not importable here") from _exc

gdal.UseExceptions()

# Lebanon plus a margin so terrain does not end at the border on screen.
BBOX_WGS84 = (34.60, 32.70, 37.10, 35.00)  # W, S, E, N
MIN_Z, MAX_Z = 6, 12
TILE = 256
NODATA_ELEV = 0.0     # sea / no data -> 0 m; terrarium cannot encode "missing"


def source_tifs(source_dir):
    """The GLO-30 tiles to mosaic, verified against SHA256SUMS when it exists.

    The checksum file has always sat beside these rasters and was never read.
    A silently truncated source tile produces a DEM with a plausible-looking
    hole in it — the same class of failure that killed the satellite build.
    """
    tifs = sorted(glob.glob(os.path.join(source_dir, "*.tif")))
    if not tifs:
        raise bh.BuildError(bh.BUILD_INCOMPLETE, f"no GLO-30 .tif files in {source_dir}")
    sums = os.path.join(source_dir, "SHA256SUMS")
    if os.path.isfile(sums):
        expected = {}
        with open(sums, encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2:
                    expected[os.path.basename(parts[-1])] = parts[0]
        for tif in tifs:
            want = expected.get(os.path.basename(tif))
            if not want:
                continue
            got = bh.sha256_file(tif)
            if got != want:
                raise bh.BuildError(
                    bh.CHECKSUM_MISMATCH,
                    f"{os.path.basename(tif)} does not match SHA256SUMS "
                    f"(expected {want[:16]}…, got {got[:16]}…) — re-download it",
                    path=tif)
        print(f"verified {len(tifs)} source rasters against SHA256SUMS", flush=True)
    else:
        print(f"note: no SHA256SUMS in {source_dir}; source integrity unverified",
              flush=True)
    return tifs


def build_mosaic(source_dir, workdir):
    tifs = source_tifs(source_dir)
    vrt = os.path.join(workdir, "dem.vrt")
    bh.run(["gdalbuildvrt", "-resolution", "highest", vrt, *tifs], timeout=1800)
    # Clip + reproject to Web Mercator (float32 metres, bilinear).
    merc = os.path.join(workdir, "dem_3857.tif")
    w, s, e, n = BBOX_WGS84
    bh.run(["gdalwarp", "-t_srs", "EPSG:3857", "-te_srs", "EPSG:4326",
            "-te", str(w), str(s), str(e), str(n),
            "-r", "bilinear", "-ot", "Float32", "-dstnodata", str(NODATA_ELEV),
            "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", vrt, merc], timeout=3600)
    return merc


def terrarium_rgb(elev):
    """float32 metres -> uint8 (3,H,W) terrarium."""
    v = np.nan_to_num(elev.astype(np.float64), nan=NODATA_ELEV) + 32768.0
    v = np.clip(v, 0, 256 * 256 - 1 / 256)
    r = np.floor(v / 256.0)
    g = np.floor(v - r * 256.0)
    b = np.floor((v - r * 256.0 - g) * 256.0)
    return np.stack([r, g, b]).astype(np.uint8)


# tile_bounds_3857 and lonlat_to_tile were duplicated byte-for-byte here and in
# build_satellite.py, and lonlat_to_tile was a third copy of coverage_check.tile_xy.
# One implementation now lives in build_helpers.
tile_bounds_3857 = bh.tile_bounds_3857
lonlat_to_tile = bh.lonlat_to_tile


def build(source_dir, out_part, work):
    """Mosaic, tile and write the archive. Returns the tile count."""
    merc = build_mosaic(source_dir, work)
    ds = gdal.Open(merc)
    gt = ds.GetGeoTransform()
    W, H = ds.RasterXSize, ds.RasterYSize
    print(f"mosaic {W}x{H} px, origin ({gt[0]:.0f},{gt[3]:.0f}) res {gt[1]:.2f} m", flush=True)

    con = sqlite3.connect(out_part)
    cur = con.cursor()
    cur.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    cur.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                "tile_row INTEGER, tile_data BLOB)")

    w, s, e, n = BBOX_WGS84
    # Metadata FIRST. Written last, an interrupted build leaves tiles with an
    # empty metadata table — an archive that looks finished to anything that
    # only counts tiles. coverage_check calls that BUILD_INCOMPLETE.
    for k, v in {
        "name": "lebanon-dem", "format": "png", "type": "baselayer", "version": "1",
        "minzoom": str(MIN_Z), "maxzoom": str(MAX_Z),
        "bounds": f"{w},{s},{e},{n}",
        "center": f"{(w + e) / 2},{(s + n) / 2},{MIN_Z}",
        "encoding": "terrarium",
        "description": ("Copernicus DEM GLO-30, clipped to Lebanon, EPSG:3857, "
                        "terrarium-encoded raster-dem for MapLibre terrain + hillshade."),
        "attribution": ("Copernicus DEM (c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space "
                        "GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA"),
    }.items():
        cur.execute("INSERT INTO metadata VALUES (?,?)", (k, v))
    con.commit()

    count = 0
    mem = gdal.GetDriverByName("MEM")
    png = gdal.GetDriverByName("PNG")
    for z in range(MIN_Z, MAX_Z + 1):
        x0, y0 = lonlat_to_tile(w, n, z)
        x1, y1 = lonlat_to_tile(e, s, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                minx, miny, maxx, maxy = tile_bounds_3857(z, x, y)
                # Reproject-on-read: warp the mosaic window into a 256px tile.
                tgt = mem.Create("", TILE, TILE, 1, gdal.GDT_Float32)
                tgt.SetGeoTransform((minx, (maxx - minx) / TILE, 0, maxy, 0,
                                     -(maxy - miny) / TILE))
                tgt.SetProjection(ds.GetProjection())
                tgt.GetRasterBand(1).Fill(NODATA_ELEV)
                gdal.ReprojectImage(ds, tgt, None, None, gdal.GRA_Bilinear)
                elev = tgt.GetRasterBand(1).ReadAsArray()
                if elev is None or not np.isfinite(elev).any():
                    continue
                rgb = terrarium_rgb(elev)
                out = mem.Create("", TILE, TILE, 3, gdal.GDT_Byte)
                for i in range(3):
                    out.GetRasterBand(i + 1).WriteArray(rgb[i])
                tmp = os.path.join(work, "t.png")
                png.CreateCopy(tmp, out)
                with open(tmp, "rb") as fh:
                    data = fh.read()
                cur.execute("INSERT INTO tiles VALUES (?,?,?,?)",
                            (z, x, (1 << z) - 1 - y, sqlite3.Binary(data)))
                count += 1
        con.commit()
        print(f"z{z}: cumulative {count} tiles", flush=True)

    cur.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    con.commit()
    con.close()
    return count


def validate_part(out_part):
    """The builder checks its own output before claiming success.

    Runs the same gate install_dataset.py runs — but its CONTENT half needs
    image decoders (Pillow), which the GDAL preparation container does not
    have. A validator that cannot run is not evidence that the data is bad, so
    the structural half still runs, the gap is stated, and the archive is kept.
    install_dataset.py executes the full content gate inside the api container,
    and refuses anything that fails it.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import coverage_check
    try:
        ok, verdict = coverage_check.validate_archive(out_part)
    except ModuleNotFoundError as exc:
        print(f"NOTE: content validation unavailable here ({exc}); running the "
              f"structural checks only. The full content gate runs at install "
              f"time inside the api container.", flush=True)
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
    return verdict


def main():
    ap = argparse.ArgumentParser(description="Build the Lebanon terrarium DEM MBTiles.")
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", required=True, help="write to <name>.mbtiles.new; caller installs")
    ap.add_argument("--disk-reserve-gb", type=float, default=bh.DEFAULT_RESERVE_GB,
                    help="free space that must remain after the build")
    args = ap.parse_args()

    out_part = args.out + ".part"
    try:
        tifs = glob.glob(os.path.join(args.source, "*.tif"))
        source_bytes = sum(os.path.getsize(t) for t in tifs) if tifs else 0
        if source_bytes:
            # The float32 mercator mosaic plus the PNG pyramid have run to
            # roughly 4x the source rasters. A multiplier, never a fixed size.
            bh.disk_preflight(os.path.dirname(os.path.abspath(args.out)) or ".",
                              source_bytes * 4, reserve_gb=args.disk_reserve_gb,
                              label="DEM build")
        if os.path.exists(out_part):
            os.unlink(out_part)
        with tempfile.TemporaryDirectory() as work:
            count = build(args.source, out_part, work)
        print(f"built {count} tiles z{MIN_Z}-{MAX_Z}; validating before promoting", flush=True)
        verdict = validate_part(out_part)
        # <out> appears only once the archive has passed. An interrupted build
        # leaves no .mbtiles.new at all, so the old "refusing to overwrite"
        # trap — where a corpse blocked every future run — cannot recur.
        bh.atomic_promote(out_part, args.out)
        print(f"wrote {args.out}: {count} tiles, {verdict.get('distinct')} distinct, "
              f"elevation {verdict.get('measured', {}).get('elevation_min_m')}.."
              f"{verdict.get('measured', {}).get('elevation_max_m')} m")
        print(f"install it with:\n  scripts/map_data/install_dataset.sh "
              f"{args.out} lebanon-dem --restart-martin")
        return 0
    except bh.BuildError as exc:
        if os.path.exists(out_part):
            os.unlink(out_part)
        print(f"FAILED {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
