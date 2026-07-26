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
| `run_migrations_in_container.sh` / `.ps1` | Check migration status and run pending Alembic migrations inside the running container (Linux/Mac and Windows versions) |
| `run_migration.py` | Manual `create_all` for User/UserPipelineAccess/ChatbotAuditLog tables (pre-Alembic era) |
| `add_blocked_columns_migration.py` | One-off: add `blocked_reason`/`blocked_at` columns to users |
| `add_identity_id_column.py` | One-off: add `identity_id`/`label_state` columns to faces |
| `fix_database_types.py` | One-off: clean up PostgreSQL type conflicts blocking table creation |

## 📁 maintenance/ — Data Maintenance

| Script | Purpose |
|---|---|
| `cleanup_unknown_identities.py` | Remove all unknown identities and their related data (interactive, destructive) |
| `../clear_all_data.py` | Wipe all detection data (destructive) |
| `../backfill_pgvector_embeddings.py` | Backfill missing pgvector embeddings |
| `../backfill_unknown_embeddings.py` | Backfill embeddings for unknown faces |
| `../fix_null_embeddings.py` | Repair faces rows with NULL embeddings |
| `../remove_backfilled_embeddings.py` | Undo a backfill run |
| `../migrate_faiss_to_pgvector.py` | One-time FAISS → pgvector migration |
| `../download_lebanon_tiles.py` | Download offline map tiles (see `README_LEBANON_TILES.md`) |

## 📁 debug/ — Diagnostics & Verification

| Script | Purpose |
|---|---|
| `verify_intelligence_router.py` | Verify the intelligence router imports and lists its routes |
| `verify_intelligence_router_simple.py` | Syntax-only check of the intelligence router file |
| `../check_dashboard_data.py` | Inspect what the dashboard queries return |
| `../check_embedding_norms.py` | Validate embedding normalization |
| `../check_identity_types.py` | Inspect identity type distribution |
| `../check_known_faces_status.py` | Verify known faces are loaded correctly |
| `../check_null_embeddings.py` | Count faces with NULL embeddings |
| `../check_startup_embeddings.py` | Verify embeddings load at startup |
| `../check_unknown_embeddings.py` | Inspect unknown-face embeddings |
| `../debug_pgvector_search.py` | Debug pgvector similarity search results |
| `../show_database_stats.py` | Print database table statistics |
| `../verify_pgvector_usage.py` | Confirm pgvector is the active vector backend |

## 📁 legacy/ — Archived Code

| Script | Purpose |
|---|---|
| `app_production.py` | The original monolithic application (v1.0) — superseded by the modular `backend/` package. Kept for reference only; do not run. |

---

**Note:** scripts listed with a `../` prefix still live flat in `scripts/` — they predate this organization and are referenced by path in the documentation, so they were left in place.
