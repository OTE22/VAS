# Map Migration Inventory — Folium/Leaflet → MapLibre GL JS + Martin

Phase 0 of the offline map migration. Every map-related component in the
repository, classified before any code changed, so the cutover deletes
exactly what is obsolete and nothing that is load-bearing.

Migration rule: **BUILD NEW → PROVE PARITY → CUT OVER → REMOVE OLD → PROVE
AGAIN.** Nothing in the DELETE-AFTER-PARITY table is removed until the
parity suite in `tests/test_map_rendering.py` (behavioural, not
library-bound) passes against MapLibre.

Target architecture:

```
Browser (MapLibre GL JS 6.3.0, vendored) → nginx → /api/  → FastAPI → PostgreSQL
                                                 → /maps/ → Martin 1.13.0 → map-data/production/
```

## Facts that shaped the plan

* **`generate_pipelines_map` and `_generate_map_html` do not exist** — they
  were never written. Earlier drafts named them; strike them from any
  checklist. The real symbols are `generate_folium_map` (one caller:
  `backend/routes/intelligence.py`), `_generate_map_internal`, and
  `_generate_empty_map`.
* **A GeoJSON endpoint already exists**:
  `GET /api/identities/{id}/map/geojson` → `map_service.generate_geojson()`,
  which is library-agnostic. The MapLibre data API extends it (adds cameras,
  route ordering, risk points, security zones, patterns, metadata) rather than
  building from scratch.
* **The pipelines page** (`frontend/js/admin-pipelines.js`) is the only
  client-side Leaflet use: a coordinate picker — one draggable marker, no
  popups, no clustering — with tile URL, zooms, bounds and centre
  **hard-coded**, drifting from `config.py` `MAP_*`. Smallest port; the four
  constants get threaded from config during it.
* **The `/map` route is the ONLY caller** of the Folium renderer; the entire
  `nginx ^/api/identities/[^/]+/map$` location, the sandboxed iframes on two
  pages, and the header-stripping trick exist solely because Folium emits an
  inline-script HTML document. MapLibre renders in the parent page — that
  whole surface disappears at cutover.

## Functional contract (frozen)

Route: `GET /api/identities/{identity_id}/map`. All security/expensive
overlays are opt-in server-side (`intelligence.py` ~709: "A missing checkbox
in some client must never silently enable them").

| # | Feature | Guard | intelligence page | security-intel page | Verdict |
|---|---|---|---|---|---|
| 1 | Raster tile layer | `has_tiles` probe | — | — | ACTIVE → Martin source |
| 2 | Colored-grid fallback | else-branch | — | — | LEGACY — delete; explicit unavailable state instead |
| 3 | Markers + icons | — | always | always | ACTIVE → circle/symbol layers |
| 4 | Popups | `include_popups` | `map-include-popups` | same | ACTIVE → safe DOM popups |
| 5 | Routes | `show_routes` ∧ len>1 | `map-show-routes` | same | ACTIVE → LineString layer |
| 6 | MarkerCluster | `cluster_markers` ∧ len>5 ∧ plugin | `map-cluster-markers` | same | ACTIVE → native `cluster:true` |
| 7 | Title/legend + risk badge | identity_name | implicit | implicit | ACTIVE → DOM overlay |
| 8 | Security zones | `enable_security_features` | `map-enable-security` | same | ACTIVE → fill layer |
| 9 | Patterns (loiter/backtrack/rapid) | `detect_patterns` | `map-detect-patterns` | same | ACTIVE → circle/line layers |
| 10 | Threat indicators (watchlist) | via security | via security | same | ACTIVE → symbol layer |
| 11 | Risk HeatMap | `show_risk_heatmap` ∧ plugin | `map-show-heatmap` | same | ACTIVE → heatmap layer |
| 12 | Timeline control | `show_timeline` | `map-show-timeline` | **not sent** | PARTIAL — intelligence page only |
| 13 | Animated avatar (TimestampedGeoJson) | `show_animated_avatar` ∧ ≥2 coords ∧ plugin | `map-show-animated-avatar` | **not sent** | PARTIAL — intelligence page only |
| 14 | Multi-identity tracking | `multi_identity_data` | **never sent** | **never sent** | DEAD — no port |
| 15 | LayerControl | skipped when avatar on | — | — | ACTIVE → layer toggles |
| 16 | fit_bounds | len>1 | — | — | ACTIVE → `fitBounds` |

Port 1, 3–13, 15, 16. Features 2 and 14 get no MapLibre equivalent.

## Classification

### MIGRATE (ported to MapLibre)

| Location | What |
|---|---|
| `backend/core/map_service.py` `generate_folium_map` / `_generate_map_internal` (~469–956) | Folium construction: `folium.Map`, `TileLayer`, `MarkerCluster`, `PolyLine`, `Marker/Icon/Popup`, `fit_bounds`, `LayerControl` |
| `map_service.py` ~1026–1042, 1076–1114, 1116–1194, 1197–1222 | Zones (Polygon), patterns (Circle/PolyLine), threat markers, heatmap + timeline hooks — inlined here, NOT in `security_map_features.py` |
| `backend/core/security_map_features.py:867` | `plugins.HeatMap` |
| `backend/core/animated_map_features.py:301` | `plugins.TimestampedGeoJson` |
| `map_service.py:1278` `generate_geojson` / `_generate_geojson_internal` | **Reuse** as the base of the map-data API |
| `frontend/js/admin-pipelines.js` ~258–323 | Leaflet coordinate picker → MapLibre draggable Marker |
| `frontend/admin/pipelines.html:194,196` | Leaflet CSS/JS includes → MapLibre |
| `frontend/js/admin-intelligence.js` / `admin-security-intelligence.js` | iframe blocks + duplicated `buildMapUrl` → `IdentityMapController` |

### ACTIVE (supporting infrastructure that changes shape)

`backend/routes/intelligence.py` (`/map`, `/map/geojson`, `/api/map/stats`,
`MAP_STYLE_ALLOWLIST`); `backend/main.py:420-432` `/tiles` StaticFiles
fallback mount; `nginx.conf` + `nginx.prod.conf` `/tiles/` and `/map$`
locations; CSP `frame-src` for iframes; compose `../tiles` mounts;
`config.py:284-328` `MAP_*` (single import surface `map_service.py:136-159`).

### DELETE-AFTER-PARITY (Folium-era offline-asset plumbing — one atomic unit)

`backend/core/offline_map_assets.py` (whole module, incl. the glyphicons
data-URI embed); `scripts/vendor_folium_assets.py`; `map_service.py`
`plugin_available`, `OFFLINE_*` flags, grid-background CSS injection
(~616–658, ~1239–1247), the never-served MBTiles branch (~567–571);
`requirements-base.txt` `folium`, `offline-folium` (`branca` is
transitive-only); Dockerfile.cpu/gpu `python -m offline_folium` + both COPYs
+ vendoring RUN; the nginx `/map$` locations; both sandboxed iframe blocks;
`frontend/vendor/leaflet/*`; `admin-intelligence.css:1144-1309`
Leaflet/cluster styles; `admin_tutorial.py:3812-3815` prose. Tests 8–15 of
`tests/test_offline_maps.py`.

**Delete-order dependency:** these assert on each other through
`test_offline_maps.py`; remove as one change with that file rewritten.

### DEAD (zero callers today — delete regardless)

`SecurityMapRenderer.add_security_zones/add_threat_indicators/
add_pattern_indicators` (map_service inlines its own copies);
`frontend/vendor/leaflet/MarkerCluster.*` (never loaded by any page);
`map_service.py` `multi_identity_data` branch + `animated_map_features.
add_multi_identity_tracking` + `_add_co_appearance_indicators`; `config.py`
`MAP_REGION`, `MAP_MAX_TRACKS`, `MAP_GENERATION_TIMEOUT`,
`MAP_SHOW_ANIMATED_AVATAR` (route hard-codes the default), `MAP_CO_APPEARANCE_*`.

### DUPLICATE (collapse during port)

`buildMapUrl` (byte-identical in two JS files); `MAP_STYLES` (two JS files);
style allowlist ×3 (`config.py`, `intelligence.py`, `map_service.py`);
`/tiles/` and `/map$` nginx blocks ×2 configs; Dockerfile vendoring ×2;
`Docs/55` vs `Docs/56` offline-map guides; `/tiles` served by nginx AND FastAPI.

## Tests

* **Parity suite (keep, re-point):** `tests/test_map_rendering.py` — 12
  behavioural tests (usable map, every camera present, centred on track,
  clustering, heatmap, timeline/animation, all-features, camera without
  coords skipped, no-coords identity answers, chronological sequence, auth,
  tiles exist for displayed area). One assertion is Folium-bound (`CSP ==
  "sandbox allow-scripts"`) and changes with the iframe's removal.
* **`tests/test_offline_maps.py`** — 19 tests; 1–7 and 16–19 survive
  re-targeted to Martin; 8–15 are Folium internals → rewritten.
* Contradiction to resolve: `test_intelligence_system.py:343` asserts
  `"srcdoc" in src` while `test_offline_maps.py:303` asserts
  `"iframe.srcdoc" not in source` — both pass only via a comment. Both go
  with the iframe.
* `test_legacy_retirement.py:80-81` keeps `MAP_ENABLE_SECURITY_FEATURES`
  etc. in `DELETED_SETTINGS` — must stay deleted.

## Docs to rewrite / consolidate

46, 47, 48, 49, 52, 53, 54, 55, 56 (nine dedicated map docs; 55/56 overlap
and merge), plus map sections in 00, 39, 50, 51, 73, 75 and
`scripts/README_LEBANON_TILES.md`. New: this file (85) and
`86_MAP_DATASET_ACQUISITION.md`.

## Risks logged before implementation

1. `map-data/` must be excluded from git and Docker images (like `tiles/`),
   guarded by test — the raster archive is hundreds of MB.
2. The TMS y-flip in `tiles_to_mbtiles.py` is the highest-risk correctness
   detail; the conversion-integrity test gates any pyramid retirement.
3. Four hard-coded geo constants in `admin-pipelines.js` drift from config.
4. Two orphaned artefacts (`SecurityMapRenderer.*`, the multi-identity path)
   are dead today and are simply removed — no port.

## Verification results (running log — the final report matrix supersedes this)

### Martin 1.13.0 hot reload — **FAIL (proven), fallback = restart Martin only**

Two operational tests, both through nginx from the api container, no restart in between:

| Test | Method | Observation | Verdict |
|---|---|---|---|
| New archive appears | `lebanon-dem.mbtiles` atomically renamed into `map-data/production/`; `/catalog` polled every 5 s for 60 s | never listed | not picked up |
| Served archive replaced | archive B (same DEM, description `HOTRELOAD-B`, one tile changed) atomically renamed over the served A; probe tile + catalog polled for 30 s | Martin kept serving A's tile bytes and A's description | not picked up |
| Fallback | `docker compose restart martin` (Martin only) | B served within ~3 s; later, A re-installed the same way → A served | works |

Cause: Martin discovers sources at startup, holds the SQLite handle to the
replaced inode, and keeps an in-memory tile cache. Consequences applied:

* `scripts/map_data/install_dataset.sh` verifies **freshness** (the served
  probe tile must be byte-identical to that tile inside the just-installed
  archive), not presence — presence lied during the test (catalog listed the id
  and a tile served, but from the old file). Without `--restart-martin` it
  reports STALE and exits 1; with it, it restarts **Martin only** and re-verifies.
  The probe address is no longer a constant: `scripts/map_data/tile_probe.py pick`
  derives it from the archive's own zoom range, because a hard-coded tile does
  not exist in every dataset (and the vector archive stores gzipped payloads
  that Martin serves content-negotiated, so both sides are normalised before
  they are compared).
* The probe coordinate was corrected: the previous `11/1226/830` is ~32.3 °N,
  outside Lebanon (it only "worked" because the transitional raster pyramid
  extends further south) — 204 from a healthy dataset.
* Atomic rename is still the only supported swap: Martin never observed a torn
  archive in either test.

### Terrain (D2) — **PASS**

* `lebanon-dem.mbtiles`: 1,322 terrarium PNG tiles z6–12, 76 MB, sha256
  `87aba6cd657dc558140a581f8d26ed4c4bb6e1ede012feef1a39865a1510d36b`, built from
  the five Copernicus GLO-30 tiles by `build_dem_terrarium.py` in
  `ghcr.io/osgeo/gdal:ubuntu-full-3.13.2`.
* DEM sanity (decoded, z12): Beirut coast −4…109 m; Qurnat as Sawda tile max
  **3,085 m** (summit 3,088); Bekaa/Baalbek 1,153–2,234 m; Mount Hermon tile
  max **2,809 m** (summit 2,814); open Mediterranean exactly 0; no NaN; no
  constant land tile. Guarded by `test_dem_archive_decodes_to_real_lebanon_elevations`.
* `/api/maps/availability` → `terrain: AVAILABLE, source lebanon-dem` (dark and
  satellite still truthfully `OFFLINE_MAP_DATASET_UNAVAILABLE`).
* Headless Chrome, Light → Terrain: `map.getTerrain().source == lebanon-dem`,
  hillshade layer present, 13 `/maps/lebanon-dem/` tile requests, **0 external
  requests, 0 CSP violations**, all 9 overlay layers present after the switch.
* Bug found and fixed by this proof: `_restoreOverlays` was gated on
  `map.isStyleLoaded()`, which is false on `style.load` while tiles are still
  loading, and `style.load` never fires again → every overlay vanished after a
  *real* style switch (earlier proofs only exercised refused switches). Gate
  removed; `test_restore_path_is_not_gated_on_isStyleLoaded` guards it.
* nginx `/maps/` now sends `X-Rewrite-URL`, so Martin's TileJSON advertises
  `/maps/<id>/{z}/{x}/{y}` (was the stripped path). Styles use explicit `tiles`
  arrays and never depended on it; the fix makes `/maps/<id>` self-consistent.

### Air-gap verification — Light + Terrain **PASS** (Dark / Satellite pending their datasets)

Method (repeatable; final run will cover all four styles): headless Chrome with a
fresh profile, cache disabled, **every non-localhost hostname forced to
NXDOMAIN** (`--host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE localhost"`), full
`--log-net-log` capture; harness page logs in → loads an identity with all
overlays → switches basemap → reports terrain state, overlay layers, CSP
violations and off-origin resource entries. The net-log is then attributed per
request by `initiator`.

Result: page-initiated requests (`initiator = http://localhost`, 41) — all to
`localhost` (`/frontend/`, `/api/`, `/maps/`); `EXTERNAL_REQUESTS []`; no CSP
violation; terrain + hillshade + all 9 overlays rendered from `lebanon-dem` and
`lebanon-streets-raster` with DNS dead. The only off-host entries in the log
are Chrome's own browser-process traffic (`accounts.google.com`,
`clients2.google.com`, `www.gstatic.com`, `www.google.com`,
`android.clients.google.com`) — every one tagged `initiator = "not an origin"`
with browser-internal traffic annotations, none reachable (NXDOMAIN), none from
the page. Explicit negative checks: no mapbox / maptiler / tile.openstreetmap /
google maps / bing / esri / carto / copernicus / cdn / npm / github host appears.

### Coverage acceptance (D4) — **PASS (presence only)** for both installed datasets

> **Superseded in part (2026-08-16):** this check proves a tile EXISTS at every
> site/zoom. It cannot see what the tile contains — and `lebanon-streets-raster`
> turned out to be 145,718 copies of one placeholder image, so it passed here
> while carrying no map. Coverage and content are now separate checks; see
> "Transitional raster archive is placeholder images" below.

`scripts/map_data/coverage_check.py` (also `test_every_installed_dataset_covers_lebanon_sites_and_every_camera`):
Beirut, Tripoli, Sidon, Tyre, Baalbek, northern border (Arida), south
(Naqoura), Mount Lebanon (Bcharre) **plus every pipeline coordinate in the DB**
(currently one), at min / mid / max zoom — `lebanon-dem` z6/9/12 and
`lebanon-streets-raster` z10/13/16: no missing tile.

### Style zoom/bounds must equal the archive — **DEFECT FOUND AND FIXED (2026-08-16)**

Symptom (Martin log, recurring since the DEM went in):

```
ERROR martin::srv::tiles::content: error="Zoom 15 is outside the supported range: lebanon-dem supports zoom 6-12"
ERROR martin::srv::tiles::content: error="Zoom 3 is outside the supported range: lebanon-dem supports zoom 6-12"
```

Cause: `terrain.json` declared the DEM source with an explicit `tiles` array and
**no `minzoom`/`maxzoom`**. For a `tiles` source MapLibre assumes z0-22, so
terrain + hillshade requested z13-16 (and z3-5 when zoomed out) and Martin 404ed
every one. `satellite.json` had the same omission (latent — its archive is not
installed yet); `light.json` was already correct (z10-16) and `dark.json`
declares z0-14 for the vector archive.

Impact was **not** limited to log noise: with the pre-fix style the terrain map
never fired `load` at all (30 s timeout in a headless probe).

Fix: declare the real range on every `tiles` source —
`terrain.json` z6-12 (matches `build_dem_terrarium.py` / the served TileJSON),
`satellite.json` z8-14 (`build_satellite.py` `MIN_Z`/`MAX_Z`). MapLibre now
over-zooms the deepest available tile instead of requesting one that does not
exist. Because `/frontend/` is served `expires 1y; immutable`, the style URLs
also gained the repo's `?v=` convention (`STYLE_VERSION = 'styles-2'` in
`identity-map.js`, mirrored in `admin-pipelines.js`) — bump it whenever a style
JSON changes, or corrected styles never reach a browser that cached the old one.

Proof (headless Chrome against the live stack, `scripts/dev/terrain_zoom_probe.js`,
jumping to z12-16 then z8-3):

| | requests outside z6-12 | non-200 tile responses | Martin range errors | console errors | map `load` |
|---|---|---|---|---|---|
| pre-fix style | — | — | 5 | — | **never fired** |
| post-fix style | **0** | **0** | **0** | **0** | fires, zooms clean |

Regression guard: `tests/test_maplibre_stack.py::test_every_style_source_declares_the_zoom_range_its_dataset_actually_serves`
— every `tiles` source must declare both bounds, and for any dataset installed in
Martin the declared zoom range must equal the served range and the style bbox must
not reach outside the archive bbox. Verified to fail on a deliberately wrong bound
(`maxzoom 16` → `declares (6, 16) but lebanon-dem serves (6, 12)`) and to pass as shipped.

### Transitional raster archive is placeholder images — **DEFECT FOUND (2026-08-16), basemap unusable**

Reported symptom: choosing **Light** shows a grid of

> **Access blocked** — App is not following the tile usage policy of
> OpenStreetMap's volunteer-run servers: osm.wiki/Blocked

Measured, not inferred — every tile in the installed archive was hashed:

| Archive | Tiles | Distinct images | Verdict |
|---|---|---|---|
| `lebanon-streets-raster.mbtiles` (1.2 GB) | 145,718 | **1** | every tile is the same 7,412-byte PNG, sha256 `6eabebf6…4edfd` = OSM's "Access blocked" placeholder |
| `lebanon-dem.mbtiles` | 1,322 | 752 | real elevation data (the 43% repeat is the flat sea-level tile) |

The source `tiles/` z/x/y pyramid in the repo (1.2 GB, 145,718 PNGs) is the same
image — spot-checked tiles hash to `8f4f0f59…` (md5) identically. So the pyramid
was scraped from OSM's public servers, OSM refused it with the blocked graphic,
and every refusal was saved as a "tile". `tiles_to_mbtiles.py` then faithfully
converted the refusals into the archive.

**Why every existing check passed:** all of them measured structure —
tile present ✔, byte-identical to the pyramid ✔, tile count matches ✔, coverage
complete ✔, checksum recorded ✔ — and none looked at what a tile depicts. The
availability rule was explicitly *"a representative tile 200s → sufficient"*; a
placeholder 200s. (Its probe also aimed at a fixed z11 address the archive
answers 204 for, so no byte was ever examined.)

**What changed now**

| Layer | Rule |
|---|---|
| `backend/core/map_availability.py` | probe tile derived from each source's own TileJSON (centre of its bounds at its minzoom, then the deployment bbox centre, then the fixed z11 tile) so the probe lands on a tile that EXISTS; its bytes are hashed against `PLACEHOLDER_TILES`; a match ⇒ source not usable ⇒ style `OFFLINE_MAP_DATASET_UNAVAILABLE` with the reason on the wire |
| `frontend/js/identity-map.js` | opens on the first style whose data is real instead of hard-defaulting to Light (`_firstUsableStyle`), and no longer assumes "if availability is unknown, Light works" (`?v=maplibre-3`) |
| `scripts/map_data/coverage_check.py` | `content_check()` — refuses a known placeholder hash, or an archive where ≥98% of sampled tiles are one image; reported per dataset; `--coverage-only` keeps the coverage acceptance step meaning coverage |
| `scripts/map_data/install_dataset.sh` | runs the content gate BEFORE the atomic move — such an archive can no longer be installed |
| `tests/test_maplibre_stack.py` | `test_no_installed_dataset_serves_placeholder_tiles` (fails while the poison archive is installed — that is the intent), `test_a_dataset_of_placeholder_tiles_is_never_reported_available`, `test_installing_an_archive_of_one_repeated_image_is_refused` (synthetic degenerate/varied/empty archives) |

Live result: `GET /api/maps/availability` → `{light: false, dark: false, satellite: false, terrain: true}`,
light's reason `tiles are a placeholder image, not map data: OpenStreetMap 'Access blocked'…`.
The UI now opens Terrain (real DEM) instead of painting blocked tiles.

**Resolved (2026-08-17).** The Planetiler vector archive was validated and
installed, Light and Dark now render from that one source, and the poisoned
artifacts were deleted: `lebanon-streets-raster.mbtiles` (1,197,756,416 B), the
`tiles/` pyramid (~1.2 GB), the 29-tile satellite fragment (294,912 B), the
scraper and its converter, along with their `checksums.txt` and `datasets.json`
entries. Structural checking was replaced by measured content verification with
a fail-closed ledger; see
[`89_OFFLINE_MAP_REMEDIATION.md`](89_OFFLINE_MAP_REMEDIATION.md) for the full
record and [`46_MAP_SERVICE_GUIDE.md`](46_MAP_SERVICE_GUIDE.md) for how the
stack works now.
