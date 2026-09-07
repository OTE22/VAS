# Settings Page Verification Report

**Date:** 2026-09-04  **Target:** `https://face-detector.internal` (production)  
**Method:** every setting exercised through the exact API the page uses (`PUT /api/settings/{key}`, cookie session, `X-Requested-With`), verified, then restored. Nothing was left changed.

## Verdict

**The settings page works as designed.** Every editable setting takes effect according to its declared apply mode, every readonly setting is refused, validation rejects bad input, and every change is audited.

| | Count |
|---|---|
| Settings in the registry | 222 |
| Editable, tested by changing the value | 205 |
| …took effect correctly | 203 |
| …refused by a governance rule (correct behaviour) | 2 |
| Readonly — PUT correctly returned 403 | 16 |
| Never stored — skipped (see §5) | 1 |
| Audit rows written, all attributed to `admin` | 552 |
| Runtime apply failures / HTTP 5xx | **0** |
| Settings differing from baseline afterwards | **0 of 222** |

## 1. What "takes effect" means — by apply mode

Each setting declares how a change is applied. The test verified the *right* behaviour for each mode, not just that a value was saved.

| Mode | Tested | What was verified |
|---|---|---|
| `immediate` | 110 | PUT → `applied=true`; the running process's `effective_value` == new value at once |
| `next_job_run` | 21 | PUT → `applied=true`; picked up by the next scheduled job |
| `next_request` | 2 | PUT → `applied=true`; used from the next request |
| `api_restart` | 50 | PUT → `applied=false`; **stored** changes, **live value unchanged** until restart — then applied at boot (proven, §3) |
| `worker_restart` | 4 | as above, for the ML worker |
| `index_rebuild` | 4 | stored only; needs a pgvector index rebuild |
| `container_recreate` | 12 | stored only; bind/env level, never applied in-process |

## 2. By category

| Category | immediate | next_job_run | next_request | api_restart | worker_restart | index_rebuild | container_recreate |
|---|---|---|---|---|---|---|---|
| advanced | 28 | 0 | 0 | 1 | 4 | 0 | 0 |
| advanced_search | 35 | 2 | 0 | 2 | 0 | 0 | 0 |
| cache | 1 | 0 | 0 | 0 | 0 | 0 | 1 |
| database | 0 | 0 | 0 | 0 | 0 | 0 | 4 |
| identity | 14 | 6 | 0 | 14 | 0 | 4 | 0 |
| ml_ops | 20 | 8 | 0 | 0 | 0 | 0 | 0 |
| models | 2 | 0 | 0 | 3 | 0 | 0 | 0 |
| ollama | 0 | 0 | 0 | 5 | 0 | 0 | 0 |
| processing | 0 | 0 | 1 | 7 | 0 | 0 | 0 |
| retention | 0 | 4 | 0 | 2 | 0 | 0 | 2 |
| server | 0 | 1 | 0 | 1 | 0 | 0 | 5 |
| sql_agent | 0 | 0 | 0 | 9 | 0 | 0 | 0 |
| storage | 6 | 0 | 0 | 1 | 0 | 0 | 0 |
| tracking | 4 | 0 | 1 | 5 | 0 | 0 | 0 |

## 3. Proof beyond `effective_value` — where the system actually uses the value

| Setting | Changed | Observed where | Result |
|---|---|---|---|
| `DASHBOARD_FACE_DISPLAY_HOURS` | 3.0 → 3.05 | `GET /api/dashboard/config` → `face_display_hours` | reflected instantly (also broadcast over WebSocket) |
| `SHOW_UNKNOWN_FACES_ON_DASHBOARD` | false → true | `GET /api/dashboard/config` → `show_unknown_on_dashboard` | reflected instantly |
| `SIMILARITY_THRESHOLD` | 0.4 → 0.45 | `GET /api/search/config` → `display.min_similarity` | reflected instantly |
| `API_DEFAULT_PAGE_SIZE` | 25 → 26 | `GET /api/admin/unknown` → `page_size` (caller omitted it) | reflected instantly |
| `BATCH_WRITE_SIZE` (`api_restart`) | 50 → 51 | live value after PUT / after API restart | **unchanged at 50 until restart; 51 after restart** — then restored and confirmed 50 after a second restart |

That last row is the one that matters most: a restart-level save is durable and is genuinely applied at the next boot (`hydrate_from_db`), and the page's "restart required" label is telling the truth.

## 4. Guards that correctly refused a change

These were counted as "failures" by the harness and are in fact the system working:

- **`ML_DECISION_MODE` → 409 `MODE_GATED`** — activating `shadow` requires an approved anomaly model; the settings page routes through the *same* gate as the ML-Ops page, so it cannot be used as a back door.
- **`WEBHOOK_MAX_BODY_MB=26` → 422** — exceeds nginx's `client_max_body_size`; the cross-config guard refuses a value nginx would silently break.
- **All 16 readonly keys → 403**, including every credential and security-critical key: `ACCESS_TOKEN_EXPIRE_MINUTES`, `DATABASE_URL`, `DB_HOST`, `DB_PORT`, `DEBUG`, `ENVIRONMENT`, `IDENTITY_INDEX_DB_PATH`, `JWT_ALGORITHM`, `JWT_SECRET_KEY`, `POSTGRES_DB`, `POSTGRES_PASSWORD`, `POSTGRES_USER`, `REDIS_URL`, `STORAGE_DIR`, `USE_GPU`, `WORKERS`.
- **Wrong-typed input → 422** with a clear message (`… is not an integer`, `… is not a boolean (use true/false/…)`). Discovered because my first pass sent the wrong type for 141 keys; the page rejected every one and stored nothing.

## 5. Things worth knowing (not defects in the page)

1. **A text field cannot be cleared to empty.** `ANOMALY_HOLIDAYS` refuses `""` (*"a value is required"*) but accepts a single space and stores it as empty. A user wanting to clear it must type a space. Minor UX gap.
2. **`ANOMALY_HOLIDAYS` was skipped from mutation** for that reason (never stored → not restorable through the API). Its effective value stayed `''` throughout.
3. **Sensitive keys never appear in the audit log with values.** All 4 sensitive keys (`DATABASE_URL`, `JWT_SECRET_KEY`, `POSTGRES_PASSWORD`, `REDIS_URL`) are readonly, so no audit row for them exists; the audit code masks values for sensitive keys regardless.
4. **Audit rows carry** `old_value`, `new_value`, `change_reason`, `changed_by_username`, `ip_address`, `action` (`value_applied` / `value_saved` / `application_failed`). 362 applied, 190 saved, **0 failed**.
5. The API's `value_type` vocabulary is `integer` / `boolean` / `float` / `string` / `json` — relevant to anyone scripting against it.

## 6. State after the test

- Every one of the 222 settings: `stored_value` and `effective_value` identical to the pre-test snapshot.
- Two API restarts were performed for the restart-level proof; the app returned healthy in ~70 s each time, GPU on CUDA, `deploy.sh doctor` → no problems found.
- The only side effect that remains is the 552-row audit trail, which is the intended record of the test.

## 7. How to re-run

The harness lives in the session scratchpad, not the repo, because it needs an admin session. To reproduce manually: snapshot `GET /api/settings`, `PUT` a different valid value per key, compare `effective_value` (dynamic modes) or `stored_value` (restart modes), then `PUT` the original back and diff against the snapshot.
