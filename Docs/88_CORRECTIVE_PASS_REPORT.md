# Corrective implementation pass — schema, relationships, ML-Ops lineage (2026-08-16)

Owner brief: *full corrective implementation, not a documentation-only audit;
demo data is disposable; no fallbacks; no compatibility code; no feature flags
for broken legacy behaviour; historical/audit constraints not weakened;
migration path authoritative; never destructive on production.*
Plan of record: `~/.claude/plans/why-in-the-dashboard-prancy-sprout.md` (52-row
acceptance matrix + §14 API/frontend contract gate). Findings that were fixed:
`Docs/87` §O (kept with the original numbering, now "RESOLVED").

Result in one line: **one clean data model (90 physical FKs, head
`f6a7b8c9d0e1`), deterministic relationships, complete ML-Ops lineage, correct
API contracts (0 unintentional breaking changes), no dead schema, no silent
orphans, no fallbacks — proven by 2,008 passing tests (0 failures) in an isolated, freshly-migrated
stack, a fresh-DB parity test, an OpenAPI diff, and a headless-browser smoke
matrix.**

---

## 1. What changed

### 1.1 Migrations (Alembic is now the ONLY schema initializer)
| Revision | Purpose |
|---|---|
| `000_baseline` (new root) | the 24 create_all-era tables as a frozen literal (`scripts/dev/generate_baseline_migration.py` documents the shape rule); `001` now revises it |
| `c2d3e4f5a6b7` relationship integrity | `identity_embeddings.pipeline_id` nullable + FK RESTRICT (sentinels `uploaded`/`preloaded` → NULL first); `identity_appearances.pipeline_id` FK RESTRICT; `detections.pipeline_id` CASCADE → RESTRICT; `watchlist_alerts` / `live_alert_triggers` `.pipeline_id` FK SET NULL; `watchlist_alerts.search_id` FK SET NULL + partial unique `(watchlist_entry_id, detection_id)`; DROP `pending_enrollments.checksum_match_identity_id`, `detections.image_path`; `merge_suggestions` `INVALIDATED` + `invalidated_reason/at`; FKs `watchlists.deleted_by_user_id`, `similarity_model_registry.activated_by`, `ml_models.previous_production_id` (self, CHECK <> id); chat: `user_conversation_sessions.user_id` nullable SET NULL, missing parents backfilled, FKs `user_query_history.session_id`, `user_conversation_memory.source_session_id`, `conversations.legacy_session_id` SET NULL. Every constraint is preceded by `_require_zero()` — the migration REFUSES with the count and the repair command, never deletes. |
| `d4e5f6a7b8c9` ML lineage | `ml_model_thresholds` regrained to threshold SETS (`cutpoints`, `quantiles`, `source`, `retired_at/by`, `notes`; 6 constraints incl. bidirectional scope CHECK); `ml_predictions` partial indexes; `ml_drift_reports.model_id` NOT NULL + CASCADE (precondition immediately before); `ml_datasets.lineage_summary`; frozen seed of the 24 feature definitions |
| `e5f6a7b8c9d0` create_all residue alignment | defaults/sequence and duplicate `ix_*` indexes that only create_all-era databases carried; fresh == migrated |
| `f6a7b8c9d0e1` prediction lineage RESTRICT | `ml_predictions.model_id`, `.threshold_id`, `ml_shadow_comparisons.model_id` SET NULL → RESTRICT (a model / set with prediction history is archived / retired, never deleted) |

Operator path for a legacy dev/demo DB: `scripts/repair_relationship_integrity.py` (dry-run → `--apply --yes-i-understand`; refuses production and any DB not named `face_recognition`; deletes untrusted heuristic camera back-links through the canonical vector-removal path, lineage-less shadow successes, NULL-model drift reports, orphan alert search ids, evidence-free demo unknowns) → `alembic upgrade head`. `Base.metadata.create_all()` no longer exists (AST-asserted); `DatabaseManager.init_db()` verifies the exact head fail-closed in every environment; `MIGRATIONS_FAIL_CLOSED` **REMOVED** (see §6). `scripts/migrations/run_migration.py` retired.

### 1.2 Behaviour
* **One detection write path** — `backend/core/detection_evidence.persist_detection` (batch writer + direct path): CORE evidence (detection, faces, appearance, exact embedding back-link) atomic per detection; OPTIONAL enrichment in two independent savepoints (A live-alert triggers, B watchlist alerts, both idempotent by partial unique); `detection_alerts` WebSocket event only after commit; TX2/TX3/TX4 and the "newest NULL-linked embedding" heuristic deleted; frame-created embeddings compensated on core failure (ownership flag, never inferred); crash reconciliation `reconcile_orphan_camera_embeddings` (startup 2.2f + retention; grace `STALE_CAMERA_EMBEDDING_GRACE` = 10 min, canonical removal path).
* **Exact embedding → detection** — `IdentityResolution(identity, is_new_identity, similarity, embedding_id, identity_created)`; `link_embedding_to_detection` → `LINKED | ALREADY_LINKED | CROSS_LINK_REFUSED | EMBEDDING_MISSING` (errors propagate and fail the evidence transaction).
* **Merge re-points watchlist membership and live alerts** (retire-in-place dedupe keeps alert history); unmerge restores from provenance or refuses `post_merge_watchlist_conflict`; merge suggestions invalidated canonically; approve pre-checks (409 `SUGGESTION_STALE`).
* **One image-search audit writer** (`backend/core/search_audit.record_image_search`) for `/search/by-image` (bare array kept, `X-Search-Id` additive, zero-result searches audited too), `/search/advanced`, `/search/batch`; `input_image_hash` = sha256 of the uploaded bytes everywhere.
* **ML-Ops lineage** — threshold service (advisory lock + FOR UPDATE), inference bands with the ACTIVE set and persists `threshold_id/version` + `event_time`, FK-vanished retry removed (explicit failure rows), outcome linkage inside label transactions, label supersession endpoint, drift per shadow model, feature definitions verified at boot, feature-less snapshots refused, Parquet row lineage + read-back, `lineage_summary`; drift-report route no longer swallows its 422 into a 500.
* **Audit API** — `AuditLogResponse.user_id: Optional[int]` + `historical_user_id`; frontend null-safe.
* **Fresh-install fix found by the isolated run** — `ensure_membership` now enrols platform admins as workspace **admins** (the migration rule), so a bootstrap admin on a fresh database can read orphaned conversations.
* Frontend: `dashboard.js` (`detection_alerts`), `admin-live-alerts.js` (accepts `detection_alerts`, rejects `new_detection`; teardown aborts no longer surface as unhandled rejections), `admin-ml-ops.js` (threshold sets, lineage columns, supersede), `admin-audit.js` (null-safe); `/redoc` gets a scoped CSP location (`worker-src 'self' blob:`) so its offline search worker runs.

### 1.3 Tooling and proofs
`scripts/seed_ml_ops_demo.py` (deterministic ~1,750-row demo lineage; `--remove`; post-seed invariants), `scripts/openapi_diff.py` (ADDITIVE / INTENTIONAL / UNINTENTIONAL), `scripts/dev/browser_smoke.js` (playwright-core + installed Chrome), `docker/docker-compose.regression.yml` + `scripts/run_regression_isolated.sh` + `scripts/regression_isolation_check.py`, `scripts/dev/dump_schema.py`, `tests/test_pipeline_integrity.py`, `test_embedding_detection_link.py`, `test_detection_evidence.py`, `test_merge_watchlist_transfer.py`, `test_search_audit_parity.py`, `test_ml_thresholds.py`, `test_ml_outcome_linkage.py`, `test_ml_ops_lineage.py`, `test_audit_api_deleted_user.py`, `test_migration_schema_parity.py`.

---

## 2. Verification

### 2.1 Isolated full regression (`scripts/run_regression_isolated.sh tests/ -q`)
| Run | Result | Notes |
|---|---|---|
| Baseline (before the pass, dev DB) | 1915 passed / 3 known failures | historical reference |
| Run 1 (fresh scratch DB, dedicated Redis) | 1976 passed, 19 failed, 14 errors, 21 skipped, 28m48s | 33 fallout items, all diagnosed: 22 test fixtures inserting camera embeddings for unregistered pipelines (`qa`, `qa-probe`, `test`, `retention-test`) → fixtures now register the camera / use `pipeline_id=None` for enrollment; a real NameError in `merge_multiple_identities` (`to_identity_id`) → fixed; hard-coded `user_id=1` in `test_unmerge` → resolved by username; fresh-install workspace-admin enrolment (real bug) → fixed; `test_upload_match_integrity` source contract → points at the single write path; config/dependency guards → map scripts import GDAL dynamically and read the DSN from settings, isolation checker allow-listed with reason; `75_API_REFERENCE.md` regenerated; `test_glyphicons_font_is_embedded_not_fetched` → the dev container had drifted from its image (in-container vendoring) — image rebuilt |
| Run 2 (after fixes, rebuilt image, no concurrent dev activity) | **2008 passed, 0 failed, 0 errors, 22 skipped, 1h10m** (exit 0) | fresh scratch DB `face_recognition_regression_18788_89b78498`, rebuilt image `face-detector/api:dev` (2026-08-16), regression service under its own name; isolation assertions incl. the new nginx-upstream check all OK; teardown: dev database list unchanged YES, dev storage/models untouched YES, scratch DB dropped YES. Slower than run 1 because the host was also running the 12-hour satellite raster build. |

Isolation proofs (both runs): scratch DB name ≠ `face_recognition`; `current_database()` = scratch; Redis host `redis_regression`, IP ≠ dev Redis, sentinel key + PUBLISH invisible from `face_recognition_redis`; storage / ML mounts backed by regression volumes (markers never surface on the dev side); `ENVIRONMENT != production`; dev database list unchanged; scratch DB dropped; **new since run 1:** the regression API service has its own name so the dev nginx upstream `face_recognition` resolves ONLY to the dev API (asserted — run 1 proved the leak: dev traffic was load-balanced into the scratch stack). Run 1 reported "dev Redis DBSIZE 1 → 3": caused by the seed script bumping model version markers on the dev Redis *concurrently* on purpose, not by the regression container (its Redis assertions passed); run 2 was executed without concurrent tooling on the dev stack, yet reported "dev Redis DBSIZE 1 → 4": the three new keys are `cache:dashboard:user_1:*` and `cache:unknown:user_1:*` (response caches keyed by the DEV admin id) created by interactive browser sessions through the dev nginx at 12:23–12:35 and 15:00 local (dev API access log: source 172.21.0.3 = `face_recognition_nginx`, request pattern `/api/auth/me`, `/api/dashboard/config`, `/api/admin/identities`, `/api/users` — a person using the UI). The regression container never targets nginx for `/api/*` and the dev nginx upstream was proven to resolve only to the dev API, so no regression traffic reached the dev API or dev Redis. Repeatable check: `docker exec face_recognition_redis redis-cli --scan` before/after and the dev API access log by source IP.

### 2.2 Fresh-database parity (`tests/test_migration_schema_parity.py`, 8/8)
unique scratch DB → `alembic upgrade head` → schema dump == dev DB (0 diffs) · every new constraint by name (26) + FK rules asserted · `ml_model_thresholds` final column set · 24 feature definitions / 4 policies / `system` principal · `init_db` head check + full `DatabaseManager` boot subprocess PASS · migration preconditions refuse on a scratch DB with the create_all-era shape (rows kept, version unchanged) · ORM tables ⊆ live · no `create_all` call (AST).

### 2.3 New suites (dev container, all green)
pipeline integrity 11 · embedding link/compensation/reconcile 8 · detection evidence 12 (4 A/B combinations, defer_commit, single publisher) · merge/watchlist transfer 6 · search audit parity 5 · audit API deleted user 2 · ML thresholds 11 (uniqueness, scope CHECK, 8-way concurrency ×2, round-trip) · outcome linkage 6 · ML-Ops lineage API-by-API 12 · migration parity 8.

### 2.4 Data-quality SQL (all 0 on dev after seed; asserted by tests / seed)
dangling pipeline ids (appearances, embeddings, detections, alerts, triggers) · active watchlist entries on MERGED identities · orphan `watchlist_alerts.watchlist_entry_id` · stale unlinked camera embeddings (> grace) · camera embeddings linked to a detection of another pipeline · successful shadow predictions without model/threshold lineage · drift reports without model · empty-feature snapshots · Parquet rows without `snapshot_id` · > 1 active threshold set per (model, scope) · predictions pointing at ineligible labels.

### 2.5 API / frontend contract gate (§14)
* OpenAPI before (234 paths / 89 schemas) vs after (235 / 90) — `scripts/openapi_diff.py`: **7 ADDITIVE, 0 UNINTENTIONAL BREAKING** (`Docs/api-snapshots/openapi.before.json`, `openapi.after.json`). Additive: `GET /api/ml/drift/reports?model_id`, `POST /api/ml/labels/{id}/supersede` (+ `LabelSupersedeRequest`), `AuditLogResponse.historical_user_id`, `AuditLogResponse.user_id` nullable, `MergeSuggestionResponse.invalidated_reason/at`.
* Intentional changes outside typed schemas (dict responses / WebSocket), each with consumers updated in the same change: `GET /api/ml/models/{id}.thresholds[]` shape (`threshold`/`objective` → `cutpoints`, `quantiles`, `source`, `retired_*`, `version_label`) — `admin-ml-ops.js`, `test_ml_ops_page`; `GET /api/ml/predictions` gains lineage fields — `admin-ml-ops.js`; `new_detection` / `new_unknown_detection` WS payloads no longer carry `live_alerts` / `watchlist_matches` — replaced by the persisted `detection_alerts` event — `dashboard.js`, `admin-live-alerts.js` (`admin-unknown.js` never read them); `/api/watchlist-alerts` `detection_id` additive; `/search/by-image` `X-Search-Id` header additive.
* Frontend source scans (`live_alerts`, `watchlist_matches`, `new_detection`, `detection_alerts`, `image_path`, `threshold`, `objective`): every remaining occurrence justified — `data.live_alerts` reads are inside the `detection_alerts` handlers (persisted payload); `new_detection` remains the detection-overlay event; `image_path` in `admin-unknown.js` is `face_image_path` (crop path, unchanged); `threshold` in security-intelligence pages refers to `learned_thresholds` (risk platform); no `objective`; no `watchlist_matches`.
* Browser smoke matrix (`scripts/dev/browser_smoke.js`, headless Chrome, real sign-in, real detection through the webhook, WebSocket frames captured): Login, Home, Dashboard, Known Persons, Unknown Persons, Search by Image, Watchlists, Live Alerts, Audit Logs, ML-Ops, User Management, Identity detail, `/openapi.json`, `/docs`, `/redoc` — **all PASS: 0 console errors, 0 page errors, 0 unexpected ≥400 responses**; the injected detection reached the dashboard (`unknown_activity` frame for an unknown face); `detection_alerts` not expected (no membership/alert for the smoke face). Fixed along the way: an unhandled `AbortError` on the live-alerts page teardown; `/redoc` worker CSP.

---

## 3. Acceptance matrix (plan-level checks) — see the plan file for the full 52 rows; every row PASS. Highlights:

| Check | Status | Proof |
|---|---|---|
| no destructive Alembic cleanup; `_require_zero` before every constraint | PASS | migrations + `test_preconditions_refuse_without_deleting` |
| pipeline sentinel ordering (DROP NOT NULL → UPDATE → precondition → FK) | PASS | `c2d3e4f5a6b7` |
| threshold uniqueness / bidirectional scope CHECK / concurrency | PASS | `test_ml_thresholds` |
| feature definitions frozen in migration; boot fail-closed | PASS | `d4e5f6a7b8c9`, `lifespan` 1.1.1b, parity test |
| exact Alembic head verified at boot, everywhere | PASS | `verify_database_head`, `test_migration_guard` |
| CORE atomic / OPTIONAL savepoints A,B independent / broadcast post-commit | PASS | `test_detection_evidence` (all four A/B combinations) |
| frame-created embedding never survives a failed detection; pre-existing/enrollment never touched | PASS | `test_embedding_detection_link` |
| crash-safe reconciliation, canonical vector removal, single grace constant | PASS | `identity_retention.reconcile_orphan_camera_embeddings` |
| watchlist dedupe retires in place, alerts never orphaned | PASS | `test_merge_watchlist_transfer` |
| successful shadow prediction never loses lineage (write-time and delete-time) | PASS | `inference_service`, `f6a7b8c9d0e1`, `test_ml_ops_lineage` |
| full regression on an isolated freshly-migrated stack; dev DB / dev Redis untouched | PASS (run 2) | §2.1 |
| `MIGRATIONS_FAIL_CLOSED` audited | REMOVED | §6 |
| OpenAPI unexplained breaking = 0 · console errors = 0 · WS contracts · smoke matrix | PASS | §2.5 |

---

## 4. Fallback paths remaining: **0**
Repository sweep for the removed patterns (create_all bootstrap; "newest/latest embedding" ownership inference; `detection_id IS NULL` heuristics; broadcast-only watchlist path; MERGED-loser watchlist fallback; `detections.image_path`; runtime feature seeding; feature-less snapshot success; permissive schema boot; dev Redis in the isolated regression): the only textual matches left are the exact-guard `AND detection_id IS NULL` in the link / compensation / reconcile statements (by design), local variable names, and comments describing the removed behaviour. `inference_service`'s "bounded TTL re-check without Redis" is a cache revalidation interval, not a data fallback (documented in `config.py`).

## 5. Dead configuration variables remaining: **0** (inventory in `Docs/61` § 11.5)

## 6. `MIGRATIONS_FAIL_CLOSED`: **REMOVED**
Reason: with head verification unconditional in `DatabaseManager.init_db()`, the flag's only remaining effect was permitting a schema-mismatched boot, which is now forbidden. Removed from `config.py`, `.env.example`, compose files, `backend/utils/migrations.py`, `lifespan.py`, tests, docs 05/61/72/73/80.

## 7. Known limitations / deliberately not done
* Alert-enrichment reliability: after a class-B failure the alert is not recreated automatically (documented; a future outbox/reconciliation, not a fallback).
* Only `behavior_anomaly_model` is implemented; other anomaly types are reserved interfaces (422).
* The seed's supervised dataset build is refused by the validator (< 50 reviewed rows) — a truthful `failed` dataset record with its quality report; unsupervised datasets and models are built.
* Map thread (MapLibre/Martin) is a separate in-progress track (`Docs/85`, `Docs/86`); its scripts were only touched to satisfy the config/dependency guards.
* `threat_assessments.pipeline_id` and `ml_*.pipeline_id` stay logical (polymorphic scope) — out of scope, documented in `Docs/87`.
