# 46 — Map Service Guide

**Canonical map documentation.** Supersedes the former 47 (map data flow), 49
(map service in production) and 55 (offline map setup), which described a
server-side Folium renderer that no longer exists. Dataset acquisition and
build procedure live in [`86_MAP_DATASET_ACQUISITION.md`](86_MAP_DATASET_ACQUISITION.md);
the migration record and the defect post-mortem live in
[`85_MAP_MIGRATION_INVENTORY.md`](85_MAP_MIGRATION_INVENTORY.md) and
[`89_OFFLINE_MAP_REMEDIATION.md`](89_OFFLINE_MAP_REMEDIATION.md).

---

## 1. What draws the map

The browser draws it. MapLibre GL JS renders vector and raster tiles that a
local Martin tile server reads out of MBTiles archives on disk. Nothing is
fetched from the internet at runtime, and no map HTML is generated on the
server.

```
   browser                     nginx (same origin)            containers
   ───────                     ───────────────────            ──────────
   MapLibre GL JS  ──GET──►  /frontend/maps/styles/*.json  ──►  static file
   (vendored, no CDN)        /maps/<source>/{z}/{x}/{y}    ──►  martin ──► *.mbtiles
                             /maps/font/{stack}/{range}    ──►  martin ──► *.ttf
                             /api/identities/{id}/map-data ──►  api ──► PostgreSQL
```

Everything is same-origin: there is no CORS anywhere in the map path, and the
CSP grants `worker-src 'self'` with no `blob:` and no `unsafe-eval`.

| Piece | Where |
|---|---|
| Renderer | `frontend/vendor/maplibre/` (vendored 6.3.0, worker as a same-origin `.mjs`) |
| Controller | `frontend/js/identity-map.js` — one controller, no iframes |
| Styles | `frontend/maps/styles/{light,dark,satellite,terrain}.json` |
| Tile server | `ghcr.io/maplibre/martin:1.13.0`, config `config/martin.yaml` |
| Archives | `map-data/production/*.mbtiles` (mounted read-only into Martin) |
| Data endpoint | `GET /api/identities/{id}/map-data` → GeoJSON, `backend/core/map_data_service.py` |
| Analysis | `backend/core/security_analysis.py` — zones, threats, patterns |

**There is no server-rendered map.** `GET /api/identities/{id}/map`,
`/map/geojson` and `/api/map/stats` were removed with the Folium stack; they
return 404 and have no replacement, because the browser now does that work.

## 2. The four styles and the datasets behind them

| Style | Dataset | Kind | What it is |
|---|---|---|---|
| Light | `lebanon-streets-vector` | vector | OpenMapTiles-schema MVT, built by Planetiler from a Geofabrik OSM extract |
| Dark | `lebanon-streets-vector` | vector | **the same archive**, different palette |
| Satellite | `lebanon-satellite` | raster | Sentinel-2 L2A true colour, 10 m — 15,177 tiles z8-14, acquired 2026-08-12 |
| Terrain | `lebanon-dem` | dem | Copernicus GLO-30, terrarium-encoded |

Light and Dark are **one dataset with two palettes** — identical layer lists,
identical `sources` blocks, one archive. Generating two street archives would
double the disk cost and let the two drift apart;
`tests/test_maplibre_stack.py::test_light_and_dark_are_the_same_layers_over_the_same_vector_source`
fails if they ever diverge.

Styles are generated, not hand-edited: `scripts/map_data/build_vector_styles.py`
writes both from one layer definition.

## 3. Availability: a style is offered only when it can actually be drawn

`GET /api/maps/availability` reports, per style, whether it is usable. The
dropdown disables the ones that are not. **There is no fallback** — an
unavailable style is never silently replaced by another, never a CSS-filtered
street map pretending to be satellite, never a coloured grid.

The gate ladder, first failure wins:

| Gate | Question | Failure code |
|---|---|---|
| installed | is it in Martin's catalog? | `CONTENT_MISSING` |
| readable | does its TileJSON parse and declare zooms? | `METADATA_INVALID` |
| serving | does a representative tile answer? | `PROBE_FAILED` |
| not a placeholder | is it a known upstream error image? | `PLACEHOLDER_CONTENT` |
| **content measured** | has its content been decoded and verified? | `CONTENT_NOT_VERIFIED` and others |
| resources | are its fonts and sprites served? | `RESOURCES_MISSING` |

```jsonc
{
  // illustrative: this is what an UNAVAILABLE style looks like. On this
  // deployment all four are currently available.
  "styles": { "light": true, "dark": true, "satellite": false, "terrain": true },
  "detail": {
    "satellite": {
      "available": false, "source": null, "source_type": null,
      "state": "OFFLINE_MAP_DATASET_UNAVAILABLE",
      "reason": "CONTENT_MISSING",                 // never null when available is false
      "reason_text": "lebanon-satellite is not installed",
      "candidates": { "lebanon-satellite": {
        "usable": false, "error": "not in Martin catalog", "code": "CONTENT_MISSING",
        "checks": { "installed": false, "readable": null, "metadata_valid": null,
                    "content_ok": null, "resources_ok": null } } }
    }
  },
  "martin_reachable": true, "checked_at": 1786969974.5,
  "unavailable_state": "OFFLINE_MAP_DATASET_UNAVAILABLE"
}
```

`styles` is a flat `{name: bool}` map and must stay one — `identity-map.js`
does `opt.disabled = !ok` over it. Everything else is additive. A `checks`
value of `null` means that gate was never reached because an earlier one
failed.

**An unavailable style always names a registered reason code.** That is
enforced by construction, not by an assertion: `map_availability.style_entry()`
is the only place a style entry is built, and it derives `reason` from
`available`. If a reason cannot be composed it reports the internal code
`AVAILABILITY_STATE_INVALID`, logs at ERROR and increments
`fr_map_availability_state_invalid_total`. Deliberately not an `assert` —
`python -O` strips those.

The verdict is cached and refreshed every `MAP_AVAILABILITY_REFRESH_SECONDS`
under the service supervisor; the endpoint never triggers a fresh probe.

## 4. Content verification: why "the file is there" means nothing

The Light basemap was once 145,718 tiles that were all the same image:
OpenStreetMap's *"Access blocked — App is not following the tile usage policy"*
PNG. Every check of the day passed — tile present, byte-identical to its
source pyramid, count correct, coverage complete, checksum recorded — because
every one of them measured **structure**. None looked at what a tile depicts.

So an archive is usable only once its **content** has been measured:

- `scripts/map_data/coverage_check.py` decodes a deterministic, geographically
  stratified sample (never `ORDER BY random()`, so a refusal reproduces) and
  applies per-kind rules: imagery must decode, have plausible dimensions, not
  be blank, carry entropy and differ across the country; DEM must decode to
  elevations in range with real relief; vector must decompress, parse as MVT,
  and name layers its own metadata declares.
- `map-data/metadata/content_verdicts.json` records the verdict for each
  archive, bound to **that archive's bytes**: sha256 plus size, mtime, ctime
  and inode.
- `backend/core/map_content_ledger.verdict_for()` answers at runtime with one
  `os.stat`. Any mismatch means the archive on disk is not the one that was
  measured, so it is `CONTENT_NOT_VERIFIED` until something measures it again.

**Never measured is not usable.** `content_ok` is a tri-state: `True` (measured
and passed), `False` (measured and rejected), `None` (nobody has looked). Only
`True` serves.

SHA-256 is recomputed at exactly four points — installation, boot verification,
`POST /api/maps/verify`, and `production_gate.py` — never on an availability
refresh, because hashing a multi-GB archive every few minutes is its own
outage.

At start-up the API measures any installed archive that has no valid verdict,
in the background, and writes the ledger. Until that check *passes* the dataset
stays unavailable. This is what makes a hand-copied `map-data/production/` safe
rather than merely convenient.

### Verifying on demand

```bash
curl -X POST http://localhost/api/maps/verify \
     -H "Authorization: Bearer $TOKEN" -H "X-Requested-With: XMLHttpRequest"
```

Admin only, single-flight, runs on a worker thread. Re-measures every installed
archive, rewrites the ledger and refreshes availability. This is the way to
make a dataset usable again after replacing it by hand.

## 5. Installing a dataset

`scripts/map_data/install_dataset.sh <archive.mbtiles.new> <id> [--restart-martin]`

One crash-safe transaction (`scripts/map_data/install_dataset.py`):

```
stage → validate + hash → PENDING verdict → retain previous → rename →
activate verdict + checksums.txt + datasets.json → drop previous
```

Every crash point leaves either the **old archive serving and available**, or
the **new bytes in place and reported UNAVAILABLE** — never new bytes wearing
the previous archive's authorization. `--rollback <id>` restores the retained
copy.

Martin 1.13.0 has **no hot reload** (proven): it keeps an open handle to a
replaced file and its own tile cache, so after a swap it serves the OLD data
while the catalog still lists the id. `--restart-martin` restarts the tile
server *only* — never the backend, database, Redis or workers. The installer
then verifies **freshness**, not presence: the tile served through nginx must
be byte-identical to that tile inside the archive just installed.

## 6. Building datasets

Each dataset builds **independently**; nothing waits for anything else.

```bash
scripts/map_data/build_streets_vector.sh [--install] [--restart-martin]
scripts/map_data/build_dem.sh            [--install] [--restart-martin]
scripts/map_data/build_satellite.sh      [--install] [--restart-martin]
scripts/map_data/build_all.sh            [--install] [streets-vector|satellite|dem ...]
```

`build_all.sh` runs every requested builder even if one fails, prints a
PASS/FAIL table, keeps the artifacts that succeeded, and exits non-zero only if
a requested dataset failed. The datasets were once chained by hand and the
streets build sat idle for 16 h 07 m behind a satellite build that then failed.

Builders are hardened against the failure that produced that: downloads go to
a `.part` file and are checked for HTTP status, content type, declared length,
checksum and block pages before being promoted; retries are bounded with
exponential backoff and full jitter; permanent refusals (403/404, wrong type,
bad checksum) are not retried; disk space is checked before the first byte;
and each builder validates its own output before promoting it.

**There is no raster streets builder, deliberately.** The raster archive this
replaced was produced by scraping `tile.openstreetmap.org` ~145,000 times
against their tile usage policy. Rebuilding it would mean scraping again. See
§8.

## 7. Settings

Documented **once**, here.

| Setting | Default | Meaning |
|---|---|---|
| `MAP_DATA_DIR` | `/app/map-data` | Root of the dataset tree. The only settable map path. |
| `MAP_MARTIN_INTERNAL_URL` | `http://martin:3000` | Backend-only address for availability probes. Browsers use `/maps/`. |
| `MAP_AVAILABILITY_REFRESH_SECONDS` | `300` (min 30) | How often the cached verdict is re-derived. |
| `MAP_BOUNDS_{SOUTH,WEST,NORTH,EAST}` | Lebanon | Panning bounds; also the fallback probe centre. |
| `MAP_DEFAULT_{LAT,LON,ZOOM}` | 33.87 / 35.85 / 10 | Initial view. |
| `MAP_MAX_COORDINATES` | `10000` | Cap on points returned by `/map-data`. |

Derived and **not settable** — production archives and the ledger that
authorizes them must never come from different trees:
`MAP_PRODUCTION_DIR` = `<MAP_DATA_DIR>/production`,
`MAP_METADATA_DIR` = `<MAP_DATA_DIR>/metadata`,
`MAP_CONTENT_LEDGER_PATH` = `<MAP_METADATA_DIR>/content_verdicts.json`.

Retired with the Folium renderer: `MAP_CACHE_*`, `MAP_GENERATION_TIMEOUT`,
`MAP_MAX_TRACKS`, `MAP_ANIMATION_*`, `MAP_REGION`, `MAP_TILE_URL`,
`MAP_OFFLINE_TILES_*`, `MAP_MIN_ZOOM`, `MAP_MAX_ZOOM`.

## 8. What must never happen

- **Never scrape a public tile server.** That produced the original defect and
  breaches the OSM tile usage policy. Vector tiles come from the Geofabrik PBF
  extract, which is published for this purpose.
- **Never store commercial imagery offline without a licence that permits it.**
  Google, Bing, Esri and Mapbox terms do not. Copernicus Sentinel-2 and
  Copernicus DEM are free, full and open with attribution — which is why they
  are what ships.
- **Never substitute a style for an unavailable one**, and never fake one with
  a CSS filter or a coloured grid. Report `OFFLINE_MAP_DATASET_UNAVAILABLE`
  with its reason code.
- **Never treat file existence as validity.** An archive that has not been
  content-verified is not usable, whatever its name, size or checksum file says.

## 9. Checking the whole thing

```bash
# every rule, with the measured value behind each one
docker exec face_recognition_api python3 /app/scripts/map_data/production_gate.py

# content + coverage of the installed archives
docker exec face_recognition_api python3 /app/scripts/map_data/coverage_check.py \
    --production /app/map-data/production

# what the browser sees
curl -s -H "Authorization: Bearer $TOKEN" http://localhost/api/maps/availability | python -m json.tool
```

Blank map, or a style missing from the picker? Ask
`/api/maps/availability` first — it names the dataset and the reason code. See
[`73_TROUBLESHOOTING.md`](73_TROUBLESHOOTING.md) §9.
