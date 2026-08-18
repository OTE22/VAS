# Map Dataset Acquisition — Lebanon (offline basemaps)

How the geographic datasets behind the MapLibre map are obtained, built,
installed and updated. Everything here happens on an **internet-connected
preparation machine**; the air-gapped server receives finished archives only.
`docker compose up` never downloads or processes map data.

```
INTERNET-CONNECTED PREPARATION MACHINE          AIR-GAPPED SERVER
  download sources                                copy archives into map-data/production/
  → process (throwaway Planetiler / GDAL           → Martin serves them
    containers)                                    → MapLibre renders them
  → verify coverage → checksum → transfer          → zero internet
```

## Components (they are different things)

| Component | Role |
|---|---|
| MapLibre GL JS 6.3.0 (vendored) | renderer |
| Martin 1.13.0 (`ghcr.io/maplibre/martin:1.13.0`) | tile/font server, internal-only behind nginx `/maps/` |
| Geofabrik OSM extract | **street data** (Light + Dark) |
| Copernicus Sentinel-2 L2A | **satellite imagery** |
| Copernicus DEM GLO-30 | **elevation** (terrain + hillshade) |

## Layout

```
map-data/
├── production/                      ← the only directory Martin reads (config/martin.yaml)
│   ├── lebanon-streets-vector.mbtiles   FINAL streets (Light + Dark)
│   ├── lebanon-satellite.mbtiles        Sentinel-2 true colour
│   ├── lebanon-dem.mbtiles              terrarium raster-dem (terrain + hillshade)
│   └── fonts/                           Noto Sans + Noto Sans Arabic (OFL) — Martin renders glyphs from these
├── metadata/
│   ├── datasets.json                    provenance, bbox, zooms, sizes, SHA-256, licence per dataset
│   └── checksums.txt
└── source/                              stays on the preparation machine; NOT transferred
    ├── osm/       lebanon-<date>.osm.pbf + planetiler-data/ (Natural Earth, water polygons, lake centrelines)
    ├── satellite/ sentinel2_manifest.json (scene list — the COGs are streamed, not stored)
    └── terrain/   Copernicus_DSM_COG_10_N3x_00_E03x_00_DEM.tif ×5 + SHA256SUMS
```

`map-data/production/` and `map-data/source/` are git-ignored and
docker-ignored; only `metadata/` is committed.

## Preparation images (never in runtime containers)

| Tool | Image | Purpose |
|---|---|---|
| Planetiler 0.10.2 | `ghcr.io/onthegomap/planetiler:0.10.2` | OSM PBF → OpenMapTiles-schema vector MBTiles |
| GDAL 3.13.2 | `ghcr.io/osgeo/gdal:ubuntu-full-3.13.2` | DEM and satellite processing (Python bindings included) |

Air-gap transfer of images: `docker save <image> -o <name>.tar` on the connected
machine, `docker load -i <name>.tar` on the target.

## 0. The committed builders

Each dataset has one committed entry point and builds **independently** —
nothing waits for anything else. The commands in the sections below are what
those scripts run; use the scripts.

```bash
scripts/map_data/build_streets_vector.sh [--install] [--restart-martin]
scripts/map_data/build_dem.sh            [--install] [--restart-martin]
scripts/map_data/build_satellite.sh      [--install] [--restart-martin]

# all three, independently: every requested builder runs even if one fails,
# the artifacts that succeeded are kept, and the exit code is non-zero only
# if a requested dataset failed
scripts/map_data/build_all.sh [--install] [streets-vector|satellite|dem ...]
```

This replaces the hand-chaining that made `streets_chain.log` read
`waiting for satellite build to finish (link serialized)` — 16 h 07 m of the
streets build idling behind a satellite build that then exited 1.

Common to every builder: disk space is checked before the first byte; downloads
are verified (status, content type, declared length, checksum, and a sniff for
error pages saved as data) before anything is promoted; retries are bounded
with exponential backoff and full jitter, and permanent refusals are not
retried; each builder validates its own output with the same gate the installer
runs, and writes `<name>.mbtiles.new` only once that passes — so an interrupted
build leaves nothing installable behind.

## 1. Streets — Geofabrik OpenStreetMap Lebanon (Light + Dark)

Source: `https://download.geofabrik.de/asia/lebanon-<YYMMDD>.osm.pbf` (~52 MB)
+ its `.md5`. Use the dated file, not `-latest`, so the build is reproducible.
Licence: **ODbL 1.0** — attribution "© OpenStreetMap contributors" (present in
the archive metadata and every style).

There is deliberately **no raster streets builder**. The raster archive this
replaced was 145,718 copies of OpenStreetMap's "Access blocked" image, produced
by scraping their tile server ~145,000 times against its usage policy;
rebuilding it would mean scraping again. Vector tiles come from the PBF
extract, which is published for this purpose.

```bash
cd map-data/source/osm
curl -sLO https://download.geofabrik.de/asia/lebanon-260814.osm.pbf
curl -sLO https://download.geofabrik.de/asia/lebanon-260814.osm.pbf.md5
md5sum -c lebanon-260814.osm.pbf.md5

# Planetiler pulls ~1.4 GB of supporting data (Natural Earth, OSM water polygons,
# lake centrelines) the first time. Do it as a separate step so the build itself
# can run offline / be repeated:
docker run --rm -e JAVA_TOOL_OPTIONS=-Xmx3g -v "$PWD:/data" \
  ghcr.io/onthegomap/planetiler:0.10.2 \
  --osm-path=/data/lebanon-260814.osm.pbf --download-dir=/data/planetiler-data \
  --only-download --download

# Build (OpenMapTiles profile — the schema light.json/dark.json are written for):
docker run --rm -e JAVA_TOOL_OPTIONS=-Xmx3g -v "$PWD:/data" \
  ghcr.io/onthegomap/planetiler:0.10.2 \
  --osm-path=/data/lebanon-260814.osm.pbf --download-dir=/data/planetiler-data \
  --output=/data/lebanon-streets-vector.mbtiles.new --minzoom=0 --maxzoom=14 \
  --bounds=34.80,32.84,36.92,34.89
```

Light and Dark are **two styles over this one archive**, generated from a single
layer definition (`scripts/map_data/build_vector_styles.py`) so they cannot
drift. `tests/test_maplibre_stack.py` enumerates the archive's `vector_layers`
and asserts every `source-layer` the styles reference exists — the styles are
proven against the data. Labels use `name:en` with `name` (Arabic) fallback;
glyphs come from the vendored Noto TTFs via Martin (`/maps/font/…`).

## 2. Satellite — Copernicus Sentinel-2 L2A

Source: **AWS Open Data**, `sentinel-cogs` bucket via the Element84 STAC API
(`earth-search.aws.element84.com/v1`). Public — **no credentials**. The
`visual` asset of each scene is the L2A True Colour Image (B04/B03/B02, 10 m,
atmospherically corrected), as a Cloud-Optimised GeoTIFF.

Scene selection: `map-data/source/satellite/sentinel2_manifest.json` — the
12 MGRS tiles covering Lebanon from **one acquisition day** (so the mosaic has
no seasonal seams), filtered to ≤ ~5 % cloud. Current manifest: 2026-08-12,
0.0–5.2 % cloud, ~3.0 GB of scenes.

**The scenes are downloaded to verified local files first, then built from
disk.** Streaming only the windows intersecting Lebanon over `/vsicurl/`
transfers far less — roughly 300-500 MB of the 2.80 GB — and was tried. It was
refuted by measurement on a link that truncates responses:

```
TIFFFillTile:Read error at row 8192, col 8192, tile 119;
got 607463 bytes, expected 1274276
```

That is the same defect class as the original 16-hour failure (a COG truncated
at 1,820,566 of 2,067,792 bytes), and GDAL's HTTP retries did not recover it:
a range read that comes up short cannot be resumed mid-build, so the whole
build is lost. A download can be resumed, so that is what the builder does.

The pipeline, in order:

1. **HEAD every scene first** — reachable, allowed host, not an error page.
   A licence or availability problem then fails in seconds rather than hours,
   and the measured `Content-Length` values size the disk preflight.
2. **Disk preflight** from those measured sizes plus the estimated archive.
   Nothing is hard-coded: the archive estimate comes from the real tile
   capacity of the declared bbox and zoom range.
3. **Download each scene** to a `.part` file with HTTP status, `Content-Length`,
   error-page sniffing and a `gdal.Open` check, then atomically promote it.
   Interrupted transfers **resume** via `Range` instead of restarting.
4. **Record `sha256`** for every scene into the manifest, now that whole files
   exist. Later runs verify against it, so `CHECKSUM_MISMATCH` is a real guard
   rather than a branch that never fires.
5. **Build from local files** — no network I/O during warping or tiling, so a
   flaky link cannot corrupt a read mid-build.
6. **Validate then promote**: the archive is assembled at `<out>.part` and
   becomes `<out>.mbtiles.new` only after passing the same gate the installer
   runs. An interrupted build leaves nothing installable behind, and the old
   "refusing to overwrite" guard — which turned a failed build into one that
   could not be re-run — is gone.

GDAL HTTP timeouts (`GDAL_HTTP_TIMEOUT`, `CONNECTTIMEOUT`, `LOW_SPEED_TIME`,
`LOW_SPEED_LIMIT`, `MAX_RETRY`, `RETRY_DELAY`) are applied **both** to this
process and to every child, from one dict so they cannot drift. The original
set them with `gdal.SetConfigOption`, which is in-process only, so its twelve
`gdalwarp` children ran unbounded; the mirror mistake matters just as much,
because the tiling loop reads in-process. Every child also runs under a
wall-clock `timeout`, so a stall dies instead of hanging.

```bash
scripts/map_data/build_satellite.sh          # or, directly:

docker run --rm -v "$PWD/map-data:/map-data" -v "$PWD/scripts:/scripts:ro" \
  -e MAP_BUILD_DISK_RESERVE_GB=10 \
  ghcr.io/osgeo/gdal:ubuntu-full-3.13.2 \
  python3 /scripts/map_data/build_satellite.py \
  --manifest /map-data/source/satellite/sentinel2_manifest.json \
  --out /map-data/production/lebanon-satellite.mbtiles.new
```

Only `sentinel-cogs.s3.*.amazonaws.com` may be downloaded from; a manifest
pointing anywhere else is refused with `SOURCE_BLOCKED`. An open-ended manifest
is a way to smuggle in imagery whose licence forbids offline storage.

**Provenance.** The HEAD probe records each scene `size_bytes`, `etag` and
`last_modified`; the download records `sha256`. The manifest previously carried
none of these. The ETag is a multipart composite (the observed one ends `-35`)
and S3 publishes no `x-amz-checksum-sha256` for these assets, so the ETag
identifies the object *version*, not its bytes; the `sha256` is our measurement
of what we received, on first use. What proves the content of the archive that
actually ships is its verdict in `map-data/metadata/content_verdicts.json`.

Output: JPEG raster MBTiles, z8-14, EPSG:3857, clipped to Lebanon (15,177 tiles
at most, ~356 MB estimated). Licence: Copernicus Sentinel data - free, full and
open; attribution **"Contains modified Copernicus Sentinel data 2026"** (in the
archive metadata and `satellite.json`).

**Documented limitation:** 10 m GSD is genuine satellite imagery, not
aerial-photo resolution — a car is roughly one pixel. Higher resolution later
means a licensed commercial provider delivered as local tiles; never
bulk-scraped consumer map services.

## 3. Terrain — Copernicus DEM GLO-30

Source: **AWS Open Data**, `copernicus-dem-30m` bucket. Public — no
credentials. Five 1°×1° tiles cover Lebanon: `N32_E035, N33_E035, N33_E036,
N34_E035, N34_E036` (~162 MB). Verify each download against the server's
`Content-Length` — one tile arrived truncated during the first acquisition
and was caught only by that check.

Concepts, kept distinct:

| Term | Meaning |
|---|---|
| **DEM** | elevation values per pixel — the source data |
| **terrain** | MapLibre's 3-D surface geometry, computed client-side from the DEM |
| **hillshade** | shaded-relief visualisation, also computed client-side from the same source |
| contours | optional elevation lines — not built |

```bash
docker run --rm -v "$PWD/map-data:/map-data" -v "$PWD/scripts:/scripts:ro" \
  ghcr.io/osgeo/gdal:ubuntu-full-3.13.2 \
  python3 /scripts/map_data/build_dem_terrarium.py \
  --source /map-data/source/terrain \
  --out /map-data/production/lebanon-dem.mbtiles.new
```

Output: PNG raster-dem MBTiles, z6–12, **terrarium** encoding
(`R*256 + G + B/256 − 32768 = metres`) — one of the two encodings MapLibre's
`raster-dem` source decodes natively. The tile bytes *are* the elevation, which
is what lets MapLibre extrude terrain and light the hillshade itself; there is
no separate pre-rendered hillshade dataset. `terrain.json` declares
`"encoding": "terrarium"`, `terrain: {source: lebanon-dem}` and a `hillshade`
layer on the same source.

Licence: Copernicus DEM — free/open; attribution "Copernicus DEM © DLR e.V.
2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under
COPERNICUS by the European Union and ESA".

## 4. Coverage acceptance (before any dataset is called installed)

Per dataset, tiles must exist at: Beirut, Tripoli, Sidon, Tyre, Bekaa/Baalbek,
the northern border, the south, Mount Lebanon — **and at every camera/pipeline
coordinate in the database**. A single Beirut tile proves nothing.
`tests/test_maplibre_stack.py` performs the operational-site check.

## 5. Install / update — atomic, verified

```bash
scripts/map_data/install_dataset.sh map-data/production/lebanon-dem.mbtiles.new lebanon-dem [--restart-martin]
```

The transaction lives in `scripts/map_data/install_dataset.py`; the shell
script is the operator entry point and adds the Martin restart. In order:

```
stage to <dest>.staged  →  validate (structure AND content) + sha256  →
write a PENDING verdict  →  fsync, retain the current archive as <dest>.previous  →
atomic rename  →  activate the verdict + checksums.txt + datasets.json  →
refresh availability, drop <dest>.previous
```

Validation is not `quick_check` alone. Structure and content are both required
and evaluated independently: a readable, well-formed SQLite file full of error
images passes every structural check, which is exactly what happened. The gate
runs inside the api container, because that is where the decoders live.

Every crash point leaves either the **old archive serving and available**, or
the **new bytes in place and reported UNAVAILABLE** — never new bytes wearing
the previous archive's authorization. `--rollback <id>` restores the retained
copy. Never copy over a file Martin may be serving.

**Martin 1.13.0 hot reload: FAIL (proven, not assumed).** Neither a newly
added archive nor an atomically replaced one is picked up — Martin discovers
sources at startup, keeps the SQLite handle to the replaced file and an
in-memory tile cache, so after a swap it keeps serving the *old* data while the
catalog still lists the id (full log in `Docs/85_MAP_MIGRATION_INVENTORY.md`).
Therefore:

* the script verifies **freshness** — the served probe tile must be
  byte-identical to that tile inside the archive it just installed — and
  refuses to call an install done on presence alone. The probe address is
  derived from the archive itself (`tile_probe.py pick`), not hard-coded: a
  fixed coordinate does not exist in every dataset, and vector payloads are
  stored gzipped but served content-negotiated, so both sides are normalised
  before comparison;
* every real update needs `--restart-martin`, which restarts the tile server
  **only** — never the backend, frontend, PostgreSQL, Redis or workers. Map data
  updates remain independent of application releases; the restart costs a few
  seconds of `/maps/` 502s (nginx keeps serving everything else).

## 6. What must never happen

* No runtime request to any external host: no OSM tile servers, MapTiler,
  Mapbox, Google/Bing/Esri/Carto, CDNs, npm, GitHub, **or Copernicus** —
  Copernicus is acquisition-time only.
* No credentials in git, images, docs, or on the air-gapped server. The
  datasets above need none; if a future source does, use environment
  variables or a git-ignored file on the preparation machine only.
* No fallback: a missing dataset is `OFFLINE_MAP_DATASET_UNAVAILABLE` in the
  UI, never a substituted style, never a CSS filter, never a coloured grid.
