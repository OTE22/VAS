# Scripts Directory

**Face Recognition Surveillance System — Operational Scripts**

All helper scripts live here, organized by purpose. The project root contains only the core application files (`main.py`, `config.py`, `db_connection.py`, `db_models.py`, `gunicorn.conf.py`, `docker-entrypoint.sh`).

> **How to run:** Python scripts are designed to run inside the API container, e.g.
> `docker exec face_recognition_api python /app/scripts/<category>/<script>.py`
> Shell scripts run from the repo root on the host.

---

## 📁 setup/ — Installation & Deployment

| Script | Purpose |
|---|---|
| `install_dependencies.py` | Detects GPU availability and installs the right dependency set (CPU vs GPU) |
| `download.sh` | Downloads the ONNX model weights into `weights/` |
| `fix-permissions.sh` | Fixes host permissions for Docker volumes (run before first `docker compose up`) |
| `start_production.sh` | Non-Docker production startup (gunicorn, 24/7 operation) — legacy path, Docker is the primary deployment |

## 📁 migrations/ — Database Migrations

> ⚠️ Since automatic Alembic migrations run at startup ([backend/lifespan.py](../backend/lifespan.py)), these are **manual/emergency tools only**.

| Script | Purpose |
|---|---|
| ~~`run_migrations_in_container.sh` / `.ps1`~~ | **Removed.** Nothing referenced them, and they `docker cp`'d two migration files by name, so any newer migration was silently skipped. Migrations now run as the `migrate` service at startup; use `docker/run_alembic_migration.sh` to inspect or run them by hand. |
| `add_blocked_columns_migration.py` | One-off: add `blocked_reason`/`blocked_at` columns to users |
| `add_identity_id_column.py` | One-off: add `identity_id`/`label_state` columns to faces |
| `fix_database_types.py` | One-off: clean up PostgreSQL type conflicts blocking table creation |

## 📁 maintenance/ — Data Maintenance

| Script | Purpose |
|---|---|
| `cleanup_unknown_identities.py` | Remove all unknown identities and their related data (interactive, destructive) |

## 📁 map_data/ — Offline basemaps

Full documentation: [`Docs/46_MAP_SERVICE_GUIDE.md`](../Docs/46_MAP_SERVICE_GUIDE.md).
Builders run in preparation containers (GDAL, Planetiler); gates and probes run
inside the api container, which has the decoders they need.

| Script | Purpose |
|---|---|
| `build_streets_vector.sh` | Build the Light/Dark vector archive (Planetiler from a Geofabrik OSM extract) |
| `build_dem.sh` | Build the Terrain archive (Copernicus DEM GLO-30, terrarium-encoded) |
| `build_satellite.sh` | Build the Satellite archive (Sentinel-2 L2A via AWS Open Data) |
| `build_all.sh` | Run every builder independently; non-zero exit only if a requested dataset failed |
| `build_helpers.py` | Shared disk preflight, hardened download, atomic promotion (stdlib only) |
| `install_dataset.sh` / `.py` | Install an archive as one crash-safe transaction, then verify Martin serves it |
| `coverage_check.py` | Structure **and content** validation; the install gate |
| `tile_probe.py` | Derive a probe tile from an archive and check the served bytes match |
| `production_gate.py` | Every production rule, with the measured value behind each |
| `make_test_fixtures.py` | Build the immutable map fixtures the isolated regression serves |
| `build_vector_styles.py` | Generate `light.json` / `dark.json` from one layer definition |

> `download_lebanon_tiles.py` and `tiles_to_mbtiles.py` were **deleted and must
> never be recreated**. They scraped `tile.openstreetmap.org` ~145,000 times
> against its usage policy; OSM refused, and 145,718 refusals were saved as a
> basemap. See [`Docs/89_OFFLINE_MAP_REMEDIATION.md`](../Docs/89_OFFLINE_MAP_REMEDIATION.md).

## 📁 debug/ — Diagnostics & Verification

| Script | Purpose |
|---|---|
| `verify_intelligence_router.py` | Verify the intelligence router imports and lists its routes |
| `verify_intelligence_router_simple.py` | Syntax-only check of the intelligence router file |
| `../check_dashboard_data.py` | Inspect what the dashboard queries return |
| `../check_embedding_norms.py` | Validate embedding normalization |
| `../check_known_faces_status.py` | Verify known faces are loaded correctly |
| `../verify_pgvector_usage.py` | Confirm pgvector is the active vector backend |

## Retired scripts (2026-08 demo-data cleanup)

* `scripts/migrations/run_migration.py` — pre-Alembic `create_all` for three tables; the application no longer calls `Base.metadata.create_all()` anywhere (Alembic is the only schema initializer, verified at boot).

Deleted rather than archived — git history has all of them. Two groups:

- **pgvector-migration era one-offs**, all obsolete now that the migration is
  complete and `identity_embeddings.embedding` is authoritative:
  `migrate_faiss_to_pgvector.py` (read v1 index files no deployment has),
  `backfill_pgvector_embeddings.py` / `fix_null_embeddings.py` (matched
  identities by display-name-as-filename, a representation the UUID gallery
  layout eliminated), `backfill_unknown_embeddings.py`,
  `remove_backfilled_embeddings.py`, and the `check_identity_types` /
  `check_null_embeddings` / `check_startup_embeddings` /
  `check_unknown_embeddings` / `debug_pgvector_search` diagnostics.
- **Superseded tools**: `clear_all_data.py` (would abort on RESTRICT FKs it
  never handled; `purge_face_storage.py` is the safe replacement),
  `show_database_stats.py` (duplicated by `/api/stats` and the purge dry-run),
  and earlier `scripts/legacy/app_production.py`, the v1.0 monolith.

`tests/test_legacy_retirement.py` pins that none of them come back.

---

**Note:** scripts listed with a `../` prefix still live flat in `scripts/` — they predate this organization and are referenced by path in the documentation, so they were left in place.
