# 89 — Offline Map Remediation

The record of a defect that every check passed, and of what replaced those
checks. Operational documentation for the map stack is
[`46_MAP_SERVICE_GUIDE.md`](46_MAP_SERVICE_GUIDE.md); dataset acquisition is
[`86_MAP_DATASET_ACQUISITION.md`](86_MAP_DATASET_ACQUISITION.md).

---

## 1. What was wrong

Selecting **Light** painted a grid of OpenStreetMap's *"Access blocked — App is
not following the tile usage policy"* image.

Measured, not inferred:

| Artefact | Measurement |
|---|---|
| `lebanon-streets-raster.mbtiles` | 1,197,756,416 B · 145,718 tiles · **1 distinct image** (sha256 `6eabebf6…4edfd`) |
| `tiles/` pyramid | ~1.2 GB · 145,718 PNGs · z10–16 · the same image |
| `scripts/download_lebanon_tiles.py` | ~145,000 requests to `https://tile.openstreetmap.org/{z}/{x}/{y}.png` |

The scraper breached the OSM tile usage policy, OSM refused it, and every
refusal was saved as a "tile". `scripts/tiles_to_mbtiles.py` then converted
145,718 refusals into an archive, and the archive was installed.

## 2. Why nothing caught it

Every check measured **structure**:

- the tile is present at every acceptance site and zoom ✔
- the archive is byte-identical to the source pyramid ✔
- the tile count matches the pyramid ✔
- the checksum is recorded ✔
- coverage is complete ✔
- a representative tile returns HTTP 200 ✔

Not one of them looked at what a tile depicts. The availability rule was
explicitly *"a representative tile 200s → sufficient"*, and a placeholder 200s.

The byte-identity check was the most misleading of all: it compared the archive
against the pyramid it was built from, and both were the same error image. It
asserted poison matched poison, and passed for the entire life of the defect.

Two further faults were found while measuring:

- **The satellite build had never finished**, leaving a 29-tile archive
  (z8:9, z9:20, 294,912 B) with an empty metadata table under the final
  filename — where the builder's own "refusing to overwrite" guard made the
  next run impossible without a manual `rm`. It also had no timeouts, no
  status checks, no length checks and no checksums.

  > **Correction (2026-08-18).** This was first recorded here as a failure *on*
  > a COG truncated at 1,820,566 of 2,067,792 bytes. That was wrong, and the
  > correction matters because it changes the fix.
  >
  > Rebuilding from checksum-verified local files, with no network involved,
  > reproduced the same 29-tile fragment byte for byte. So the fragment marks
  > where the pipeline **stalls**, not where it crashed, and the truncated read
  > was a symptom of the resulting 16-hour grind rather than its cause.
  >
  > Root cause: the tiling loop used `gdal.ReprojectImage`, which **ignores
  > overviews**, so every one of the 15,177 tiles read the full
  > 19,830 × 23,096 × 3 mosaic. Measured on a raster with overviews:
  >
  > | API | per tile |
  > |---|---|
  > | `gdal.ReprojectImage` | 1991.9 ms |
  > | `gdal.Translate` | 5.9 ms |
  >
  > 338×. At ~2 s/tile that is ~8.4 hours of pure CPU — which is what the
  > "16-hour hang" actually was. The fix has three parts: materialise the
  > mosaic to a tiled GeoTIFF, `gdaladdo` an overview pyramid, and tile with
  > `gdal.Translate` so each zoom reads a right-sized level. Measured after:
  > **all 15,177 tiles in 215 seconds.**
  >
  > A third defect surfaced during the fix: the builder's self-validation needs
  > image decoders, which the GDAL preparation container lacks, and the error
  > handler reported that `ARCHIVE_CORRUPT` and **deleted a correct archive**.
  > A validator that cannot run is not evidence that the data is bad —
  > `coverage_check` now exposes `validate_structure()` alongside
  > `validate_archive()`, and an environment failure keeps the artifact.
- **The builds were chained by hand.** `streets_chain.log` records
  `19:53:30Z waiting for satellite build to finish (link serialized)` and
  `12:00:48Z satellite build finished (exit 1)` — the streets build idled
  16 h 07 m behind a satellite build that then failed.

## 3. What replaced it

### Content is measured, and unmeasured is unusable

`scripts/map_data/coverage_check.py` decodes a deterministic, geographically
stratified sample of every archive — never `ORDER BY random()`, so a refusal
reproduces exactly for the operator who has to act on it — and applies rules
per tile kind:

| Kind | Rules |
|---|---|
| imagery | decodes (`.load()`, so truncation is caught), dimensions ∈ {256, 512} and uniform, ≤30% blank, median entropy ≥2.0 bits, ≥60% of tiles ≥1.0 bit, and separate cities do not look identical |
| dem | decodes as non-palettised PNG, elevations within [−500, 9000] m, ≥20% of tiles carry >10 m of relief |
| vector | decompresses, parses as MVT, extent ∈ {256…4096}, layer names intersect the archive's own declared `vector_layers`, ≥25% of tiles carry ≥2 layers |
| any | not a registered placeholder, not a server deny page, <98% identical, and more than a handful of distinct images |

Thresholds are justified against measured values from the real archives, which
is why they do not reject genuine data: the valid DEM is legitimately 43.2% one
image, because Lebanon's bounding box contains sea.

### The ledger, and fail-closed availability

`map-data/metadata/content_verdicts.json` records each verdict bound to the
bytes it was taken from — sha256 plus size, mtime, ctime and inode. At runtime
one `os.stat` answers "is this still the archive that was measured?", and any
mismatch yields `CONTENT_NOT_VERIFIED`.

```python
usable = (in_catalog and tile_ok and placeholder is None
          and content_ok is True and resources_ok is True)
```

`content_ok` is a tri-state. `None` — nobody has ever looked — is **not**
usable. That is the whole change: the previous rule treated "no evidence of a
problem" as "no problem".

SHA-256 is recomputed at exactly four points (installation, boot verification,
`POST /api/maps/verify`, `production_gate.py`) and never on an availability
refresh; hashing a multi-GB archive every few minutes would be its own outage.

### Every refusal has a name

An unavailable style always carries a machine-readable code —
`CONTENT_MISSING`, `CONTENT_NOT_VERIFIED`, `PLACEHOLDER_CONTENT`,
`CONTENT_DEGENERATE`, `CHECKSUM_MISMATCH`, `ARCHIVE_CORRUPT`,
`TILE_COUNT_INVALID`, `METADATA_INVALID`, `BUILD_INCOMPLETE`,
`DOWNLOAD_FAILED`, `SOURCE_BLOCKED`, `DISK_SPACE_INSUFFICIENT`,
`RESOURCES_MISSING`, `MARTIN_UNREACHABLE`, `PROBE_FAILED` — and never `null`.

`{"available": false, "reason": null}` is unreachable by construction:
`map_availability.style_entry()` is the only place a style entry is built and
it derives one field from the other. A reason that cannot be composed reports
`AVAILABILITY_STATE_INVALID`, logs at ERROR and increments
`fr_map_availability_state_invalid_total`. Deliberately **not** an `assert`,
because `python -O` removes those — a guarantee that evaporates under an
optimisation flag is not a guarantee, and the test proves it under `-O`.

The frontend was already written for `detail[style].reason` and the backend
emitted no such key at any level, so every failure rendered as the bare code
regardless of cause.

### Replacing a dataset is crash-safe

```
stage → validate + hash → PENDING verdict → retain previous → rename →
activate verdict + catalogs → drop previous
```

Every crash point leaves either the old archive serving and available, or the
new bytes in place and reported UNAVAILABLE. Never new bytes wearing the
previous archive's authorization — that property falls out of binding each
verdict to a file identity, and is proven by fault injection at all eight
transition points (`MAP_INSTALL_FAIL_AT`).

Two defects were found by that fault injection and fixed:

1. Writing the PENDING verdict under the same key as the ACTIVE one revoked
   the incumbent's authorization, taking a working dataset offline for the
   duration of an install and leaving it offline if the install then failed.
   Pending verdicts now live in their own map.
2. Retaining the previous archive with a hard link bumped its inode's ctime,
   which is part of the identity authorizing it — so making a backup of a
   healthy archive revoked it. The retention is a copy, and the space is
   accounted for in the preflight.

### Builds are independent and hardened

One committed builder per dataset, each runnable alone; `build_all.sh` runs
every requested builder even when one fails, keeps what succeeded, and exits
non-zero only if a requested dataset failed. Downloads go to a `.part` file and
are checked for HTTP status, content type, declared length, checksum and block
pages before promotion; retries are bounded with exponential backoff and full
jitter; permanent refusals are not retried; disk space is checked before the
first byte, and ENOSPC is reported as `DISK_SPACE_INSUFFICIENT` rather than a
stray `OSError`.

## 4. Deliberate positions

- **The OSM scraper is never recreated.** Rebuilding a raster street archive
  would mean scraping again. Streets come from the Geofabrik PBF extract via
  Planetiler, which is published for that purpose. There is no raster streets
  builder.
- **Satellite is built and available (2026-08-18).** 15,177 tiles, z8-14,
  193.3 MB, from 12 Sentinel-2 L2A scenes acquired 2026-08-12 (cloud 0.0-5.2%).
  Content-verified: 15,176 of 15,177 tiles distinct, top share 0.0001, blank
  share 0.0, median entropy 5.33 bits — the exact inverse of the poisoned
  archive, which was 1 distinct tile at top share 1.0.

  Satellite unavailable remains a **valid production state**: an honest
  `CONTENT_MISSING` is shippable, a silent substitution is not. Availability
  is decided by the ledger, not by whether the file happens to exist.
- **Commercial imagery is not an option.** Google, Bing, Esri and Mapbox terms
  do not permit bulk offline storage. Copernicus Sentinel-2 and Copernicus DEM
  are free, full and open with attribution.
- **Light and Dark are one dataset.** Two palettes over one archive, enforced by
  test.

## 5. Intentional breaking changes

| Change | Why |
|---|---|
| `GET /api/identities/{id}/map`, `/map/geojson`, `/api/map/stats` removed | the server-side renderer is gone; the browser draws the map. No frontend, backend, test or documented consumer remained. |
| `SourceState.usable` requires `content_ok is True` | the permissive rule is exactly what served 145,718 error images |
| `Docs/47`, `Docs/49`, `Docs/55`, `Docs/56` deleted | wholesale documents about the removed renderer; 55 documented the coloured-grid fallback that is now forbidden |

Two existing tests were changed, both strengthenings, both recorded here:

- `test_transitional_raster_archive_matches_the_pyramid_byte_for_byte` — deleted.
  It asserted poison matched poison and had skipped silently since both
  artifacts were removed.
- `test_a_dataset_of_placeholder_tiles_is_never_reported_available` — its
  assertion that `SourceState(in_catalog=True, tile_ok=True).usable is True`
  encoded the permissive semantic being removed. It now asserts the
  fail-closed one, plus the `content_ok=None` mirror.

## 6. Checking it yourself

```bash
docker exec face_recognition_api python3 /app/scripts/map_data/production_gate.py \
    --allow-unavailable satellite
```

Thirteen rules, each printed with the value that was measured. The gate is what
the acceptance claim rests on; it is not prose.
