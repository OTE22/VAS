# Troubleshooting Decision Tree

For one administrator, at 2 a.m., with no one to ask.

Every command here has been run against this system. Every log string is
quoted from the source, so you can grep for it literally.

Set your stack once:

```bash
export DC="docker compose -f docker/docker-compose.prod.yml"          # production CPU
# GPU production LAYERS both files:
# export DC="docker compose -f docker/docker-compose.prod.yml -f docker/docker-compose.prod.gpu.yml"
# export DC="docker compose -f docker/docker-compose.cpu.yml"         # development
```

---

## Read your own settings before trusting any number here

Defaults in `config.py` are frequently overridden by `.env` and by the compose
file, and they differ between the development and production stacks. Where this
document gives a number it is the **`config.py` default**, which may not be what
your deployment runs. Check the live values:

```bash
$DC exec -e PYTHONPATH=/app -w /app face_recognition python -c \
  "from config import settings; \
   print('queue      ', settings.MAX_QUEUE_SIZE); \
   print('concurrent ', settings.MAX_CONCURRENT_REQUESTS); \
   print('db pool    ', settings.DB_POOL_SIZE, '+', settings.DB_MAX_OVERFLOW); \
   print('dedup ttl  ', settings.WEBHOOK_DEDUP_TTL_SECONDS); \
   print('storage gb ', settings.MAX_STORAGE_GB); \
   print('similarity ', settings.SIMILARITY_THRESHOLD)"
```

On the stack this document was verified against, **five of those six differed
from the `config.py` default** — so make a habit of running it rather than
trusting a documented number.

---

## Two rules that will save you

**1. Do not use `docker logs --since`.** Host and container clocks have drifted
in this environment, so `--since 5m` has returned nothing while the service was
actively logging. Use `--tail N` and read the timestamps in the lines, which the
application writes in UTC.

**2. `docker logs` is incomplete by design.** Under gunicorn with the uvicorn
worker, records emitted *during startup* reach `docker logs`, but records
emitted *while serving a request* go to the rotating file and **not** to stdout.
This is documented in `backend/utils/logging.py:59-88` with measurements. The
authoritative log is:

```bash
$DC exec face_recognition sh -c "tail -200 /var/log/face-recognition/app.log"
```

Rotated siblings are `app.log.1` … `app.log.5` (`.1` is most recent), 10 MiB
each — about 60 MiB total (`LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`).

---

## Start here

```bash
$DC ps                                                  # is everything up?
curl -fsS http://localhost/health/live                  # is the app alive?
curl -fsS http://localhost/health/ready -o /dev/null -w '%{http_code}\n'   # 200 or 503?
curl -fsS http://localhost/health/detailed | python3 -m json.tool | head -60
```

| `/health/ready` says | Meaning | Go to |
|---|---|---|
| **200 `ready`** | database and models are both fine | the symptom sections below |
| **200 `degraded`** | a non-required component is down — cache, queue, or a background service. **Never causes 503**, deliberately: a dead cleanup loop must not make a load balancer pull the whole API | §5 Redis, §7 background services |
| **503 `not_ready`** | **only two causes**: the database check or the models check failed or timed out (2 s each) | §3 database, §4 models/GPU |
| **connection refused** | the API is not running or not listening | §1 |

---

## 1. The container will not start / keeps restarting

```bash
$DC ps                                          # look at STATUS and the exit code
$DC logs --tail 80 face_recognition
```

### Exit code 78 — configuration was rejected

This is the most common production failure, and it is **deliberate**: the
system refuses to boot rather than run insecurely. `78` is `EX_CONFIG`.

Look for this block in the logs:

```
========================================================================
PRODUCTION CONFIGURATION PREFLIGHT FAILED — N blocking problem(s)
========================================================================
 1. [CODE]  SETTING_NAME
    message
    fix: remedy
```

**The report tells you the setting and the fix.** No secret values ever appear
in it, by design. Common codes:

| Code | Setting | Fix |
|---|---|---|
| `JWT_SECRET_MISSING` / `_PLACEHOLDER` / `_TOO_SHORT` / `_LOW_ENTROPY` | `JWT_SECRET_KEY` | `openssl rand -hex 32` — minimum 48 characters |
| `DB_PASSWORD_DEFAULT` | `POSTGRES_PASSWORD` | it is `admin`/`postgres`/similar; change it |
| `DATABASE_URL_SUPERUSER` | `DATABASE_URL` | the app must not connect as `postgres`/`root`; use `fr_app` |
| `REDIS_NO_AUTH` | `REDIS_URL` | non-loopback Redis needs a password |
| `AUTH_COOKIE_INSECURE` | `AUTH_COOKIE_SECURE` | must be `true` in production |
| `CORS_WILDCARD_WITH_CREDENTIALS` | `CORS_ORIGINS` | `*` is not allowed in production |
| `DEBUG_ENABLED` | `DEBUG` | must be `false` |
| `DOCS_ENABLED` | `ENABLE_API_DOCS` | must be `false` |
| `WEBHOOK_AUTH_KEYS_MISSING` / `_DISABLED` / `_LOG_ONLY` | `WEBHOOK_API_KEYS`, `WEBHOOK_AUTH_MODE` | supply keys; mode must be `enforce` |
| `WEBHOOK_KEY_FILE_PERMISSIONS` | `WEBHOOK_API_KEYS_FILE` | mode must be `0444` or tighter, and readable by uid 1000 |
| `BOOTSTRAP_ADMIN_PASSWORD_WEAK` | `BOOTSTRAP_ADMIN_PASSWORD` | 12+ chars, 6+ distinct, not a known default |
| `STORAGE_DIR_NOT_WRITABLE` | `STORAGE_DIR` | **fatal in every environment** — see §6 |
| `WORKERS_GT_ONE` | `WORKERS` | must be 1 unless `ALLOW_MULTI_WORKER` is set deliberately |
| `GPU_WITHOUT_CUDA_PROVIDER` | `USE_GPU` | see §4 |

Exit 78 also comes from an unwritable model cache:

```
❌ Model cache is NOT writable as appuser: /home/appuser/.cache, ...
```

→ the `chromadb_cache` volume is owned by the wrong uid. See §6.

> The preflight can be skipped with `CONFIG_PREFLIGHT=0`. **Do not do this in
> production.** It disables the only thing standing between you and a
> misconfigured deployment.

### The database was not reachable at boot

There **is** a retry loop — 60 s budget, 2 s apart (~30 attempts), from
`MIGRATION_DB_WAIT_SECONDS` / `MIGRATION_DB_RETRY_INTERVAL_SECONDS`.

```
🔌 Verifying database connectivity for migrations: <redacted>
   ⏳ Database not reachable yet for migrations (attempt N, retrying in 2s): ...
   ❌ Database was not reachable before migration timeout
      Last error: ...
```

Check: `$DC ps postgres` and `$DC logs --tail 50 postgres`. Postgres normally
takes longer than the API to become ready; if this only happens on a cold boot
and clears on restart, raise `MIGRATION_DB_WAIT_SECONDS`.

Note `db_manager.init_db()` has **no** retry — one attempt, then
`❌ Database initialization failed` and startup aborts.

### Migrations blocked startup

| Outcome | Meaning |
|---|---|
| `revision_mismatch` | `database is at X but code expects Y`, or the head does not match the pinned `MIGRATIONS_EXPECTED_HEAD` |
| `multiple_heads` | `branched migration history: [...]` — two migrations claim the same parent |
| `db_unreachable` | above |
| `config_missing` / `tooling_missing` | `alembic.ini not found` / `Alembic not found` |

`❌ Database migrations not satisfied: <outcome> — <detail>` is fatal in
every environment (there is no permissive mode; the former
`MIGRATIONS_FAIL_CLOSED` flag was removed), and `init_db` additionally refuses
to start unless `alembic_version` equals the code's head. In production the API runs
`MIGRATIONS_MODE=verify` (read-only) and a separate one-shot `migrate` job runs
`MIGRATIONS_MODE=run`. So if this fires, check the migrate job first:

```bash
$DC logs migrate
$DC exec -w /app/alembic face_recognition alembic current   # e.g. f6a7b8c9d0e1 (head)
$DC exec -w /app/alembic face_recognition alembic heads
```

> `alembic.ini` lives in `/app/alembic`, **not** `/app`. Without
> `-w /app/alembic` you get `No 'script_location' key found in configuration`.

### Model weights are missing

There is **no explicit file-existence check**. It surfaces as an ONNX Runtime
load failure naming the path:

```
Failed to load the SCRFD model: <Type>: <onnxruntime NoSuchFile message>
Failed to load face encoder model from '/app/weights/w600k_r50.onnx'
  ❌ Model loading failed: ...
```

Expected paths: `/app/weights/det_10g.onnx` (`DETECTION_MODEL`) and
`/app/weights/w600k_r50.onnx` (`RECOGNITION_MODEL`).

```bash
$DC exec face_recognition ls -la /app/weights/
bash scripts/setup/download.sh          # re-download if absent
```

---

## 2. Cameras get 503 / frames are being dropped

**Check**

```bash
curl -fsS http://localhost/health/detailed \
  | python3 -c "import sys,json; q=json.load(sys.stdin)['components']['queue']['stats']; print(q)"
```

**Expected:** `queue_size` well below `max_size`, `total_skipped` not climbing.

Field meanings: `queue_size` = depth now · `max_size` = capacity
(`MAX_QUEUE_SIZE`; `config.py` default 10000, but commonly overridden — read
`max_size` from this response, it is the live value) · `total_skipped` =
**frames dropped** · `processing` = in flight.

> There is no dedicated Prometheus counter for dropped frames. Use
> `total_skipped` here, or `face_recognition_requests_total{status="queue_full"}`.

**The 503 is returned only when *every* image in a request was rejected:**

```
[WEBHOOK] 🚦 Queue full - rejecting request <id> (pipeline <id>)
```
→ body `{"status":"queue_full", ...}` with `Retry-After: 2`.

Partial acceptance is **202**, not 503 — `{"status":"queued","queued":n,"dropped":m}`.

Per-item drops:

```
Queue full (N/M pending)! Rejected item from pipeline: <id>
```

**Probable cause:** ingest is outrunning inference. Either cameras are sending
faster than the models can process, or inference has fallen back to CPU (§4).

**Fix, in order:**
1. Rule out CPU fallback first (§4) — that is the usual cause and it is silent.
2. Reduce camera frame rate or resolution at the source. This is the real fix.
3. Raise `MAX_QUEUE_SIZE` only to absorb bursts. It does not add throughput; it
   just delays the drop and increases latency.
4. Concurrency knobs: `MAX_CONCURRENT_INFERENCE` (3),
   `MAX_CONCURRENT_INFERENCE_PER_PIPELINE` (2), `INFERENCE_WORKERS` (3).

### Other webhook responses

| Status | Body / log | Cause |
|---|---|---|
| **401** | `{"error":{"code":"WEBHOOK_AUTH_REQUIRED"}}`, log `[WEBHOOK] ingest credential missing\|invalid - rejected path=... client=...` | wrong or absent ingest key. **401 is the only auth rejection — there is no 403 on ingest.** |
| **413** | `Request body exceeds N MB limit` | over `WEBHOOK_MAX_BODY_MB` (default 25). nginx also caps the webhook location at `25m` — **keep the two equal**. |
| **400** | `Invalid JSON: ...` | malformed payload |
| **200 `duplicate`** | `[WEBHOOK] 🔁 Duplicate request <id> ... - acknowledged, not re-queued` | same frame re-sent inside `WEBHOOK_DEDUP_TTL_SECONDS` (`config.py` default 600). Not an error. **Check the live value** — if it is shorter than your VMS's retry horizon, a retried frame is re-processed as a new sighting rather than deduplicated. |

Test a camera's key without sending a frame:

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' \
  -H "X-Webhook-Key: $INGEST_KEY" http://localhost/webhook/test    # 200 = key works, 401 = fix it
```

`Authorization: Bearer <key>` is always accepted too. Query parameters never are.

---

## 3. Database problems

### Pool exhaustion

**Check**

```bash
curl -fsS http://localhost/health/detailed \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['components']['database'])"
$DC exec postgres psql -U postgres -d face_recognition -c \
  "SELECT count(*), state FROM pg_stat_activity GROUP BY state;"
```

**Expected:** `active_sessions` well under `max_connections`
(= `DB_POOL_SIZE` + `DB_MAX_OVERFLOW`; `config.py` defaults are 50 + 100 = 150,
but both are commonly overridden — `max_connections` in this response is the
live value), `failed_sessions` not climbing.

**Log strings:**

```
⚠️  Connection pool is 40/50 (80%) - consider increasing pool_size     ← early warning
DB connection timeout - database may be overloaded. Active sessions: N ← exhausted
QueuePool limit of size 50 overflow 100 reached, connection timed out, timeout 60.00
```

**Probable cause:** a slow query holding connections, or genuine load.
**Fix:** find the slow query in `pg_stat_activity` first. Raising
`DB_POOL_SIZE` without doing that just moves the wall — and PostgreSQL has its
own `max_connections` ceiling you can exceed.

### pgvector extension missing

**This one is nasty: `/health` still reports the database healthy** (its check
is `SELECT 1`), while every face fails to match.

**Check**

```bash
$DC exec postgres psql -U postgres -d face_recognition -c \
  "SELECT extname FROM pg_extension WHERE extname='vector';"
```

**Expected:** one row, `vector`.

**Log string at startup:**

```
[PGVECTOR] [INDEX] ❌ pgvector extension not installed! Run: CREATE EXTENSION vector;
```

Note the code **logs and continues** — it does not raise. Downstream you see
`[PROCESS] Error in identity operations: ...` per face.

**Fix:** `CREATE EXTENSION vector;` then restart the API. The production image
is `pgvector/pgvector:pg15`, which ships it; a missing extension almost always
means the volume was initialised by a plain `postgres` image.

Related, slower rather than broken:

```
  ⚠️  pgvector ANN index could NOT be verified - similarity searches will seq-scan
```

---

## 4. GPU: correct results, terrible throughput

This is the highest-value check on a GPU deployment, because **nothing looks
broken**. Results stay correct; only speed collapses.

**Check**

```bash
$DC exec face_recognition python -c \
  "import onnxruntime as ort; print(ort.get_available_providers())"
$DC logs face_recognition | grep -i "running on"
nvidia-smi
```

**Expected on GPU:** `CUDAExecutionProvider` in the list, and
`SCRFD: running on CUDA` / `ArcFace: running on CUDA`.

**Expected on the CPU stack:** `['AzureExecutionProvider', 'CPUExecutionProvider']`
and `SCRFD: running on CPUExecutionProvider`. That is correct, not a fault.

**The silent-fallback log line:**

```
SCRFD: GPU mode is enabled but the session is running on CPUExecutionProvider
(providers=[...]). This is the silent-fallback case: results stay correct while
throughput collapses. Check that the installed onnxruntime-gpu build matches the
image's CUDA major version.
```

or, when the provider is not even registered:

```
GPU mode is enabled but CUDAExecutionProvider is not registered (available: [...]).
The usual cause is an onnxruntime-gpu build compiled against a different CUDA
major version than the base image provides.
```

**Metric / alert:** `face_detector_cpu_fallback_active == 1` fires
`CudaProviderUnavailable` (critical, after 5 m).

| Probable cause | How to confirm | Fix |
|---|---|---|
| Container has no GPU access | `$DC exec face_recognition nvidia-smi` fails | you are on the CPU stack, or the NVIDIA container toolkit is missing — see `04_SETUP_NVIDIA_DOCKER.md`. Production GPU **layers** `prod.gpu.yml` on `prod.yml`; using `gpu.yml` alone silently drops backups and monitoring. |
| onnxruntime-gpu / CUDA major mismatch | the message above names it | rebuild the GPU image |
| Driver older than the container CUDA | `nvidia-smi` on the host shows the driver version | update the host driver |

**To make this fatal instead of silent**, set `ALLOW_CPU_FALLBACK=false`.
Startup then aborts with `GPU readiness verified` absent and a
`GpuUnavailableError`. On a GPU box that is usually what you want.

### CUDA out of memory

There is **no OOM-specific handling** — it is not caught by type and never
retried. It appears as:

```
SCRFD: inference smoke test FAILED: <Type>: <message>          ← at startup
[PROCESS] ❌ Inference error in crop N at stage 'embed': ...    ← at runtime, face skipped
```

Proactive alert: `GpuMemoryNearlyExhausted` at >92 % — *"CUDA OOM is likely.
Reduce GPU_BATCH_SIZE or concurrency."* Those gauges come from `nvidia-smi` and
**go stale exactly when CPU fallback is active**, so check §4 first.

---

## 5. Redis is down

Redis is **optional for startup** — nothing aborts. But several things degrade
quietly.

**Check**

```bash
$DC exec redis redis-cli PING                     # PONG
curl -fsS http://localhost/health/detailed | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['components']['redis'])"
```

| What degrades | Log line to grep |
|---|---|
| Object cache → in-memory only | `⚠️  Redis cache disabled - using in-memory only` |
| Page caching → slower | `⚠️  Redis cache service disabled - page caching will be slower` |
| WebSocket broadcasts → single worker only | `⚠️  WebSocket Redis pub/sub disabled - WebSocket broadcasts will only work within single worker` |
| Cross-worker locks → **fail open** | `cross-worker single-flight locks are DEGRADED to per-process guards` |
| Login rate limiting → per-process counters | `[AUTH] Redis rate-limit unavailable, using local counters:` |
| Logout / token revocation → in-process denylist | `[AUTH] Redis revocation write failed, using local denylist:` |

The last two matter for security: with Redis down, **a logged-out token may
still be accepted by another worker**, and login throttling no longer counts
across workers. It never fails open on the revocation check within a worker.

This is why `WORKERS=1` is load-bearing here. Do not raise it without Redis.

---

## 6. Storage

**Check**

```bash
curl -fsS http://localhost/health/detailed | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['components']['storage']['stats'])"
df -h /
$DC exec face_recognition du -sh /app/storage/faces /app/storage/pending
```

**Expected:** `usage_percent` < 95. At ≥ 95 the component reports unhealthy.

Read `capacity_source`: `"disk"` means `usage_percent` is real volume
utilisation. `"configured"` means the mount could not be read and it fell back
to a percentage of `MAX_STORAGE_GB`.

> **`MAX_STORAGE_GB` enforces nothing.** No code deletes, blocks or rejects on
> it — it is a reporting-only budget. Real capacity comes from the disk. Do not
> rely on it to prevent a full volume; use retention settings and monitoring.

### `STORAGE_DIR_NOT_WRITABLE` at startup

Fatal in **every** environment. The container runs as **uid 1000 / gid 1000**
(`appuser`), pinned by compose.

```bash
$DC exec face_recognition id                       # uid=1000(appuser)
$DC exec face_recognition ls -ld /app/storage
```

**Fix:** `chown -R 1000:1000` the host path, or mount it read-write. The same
uid owns `storage_data`, `logs_data`, `face_database_data` and
`chromadb_cache`.

---

## 7. A background service has stopped

`/health/detailed` → `components.background_services`. It reports
`healthy:false` when any service is not `running`/`starting`, **or** when any
is stale — no successful run for `interval*3 + 120 s`.

```bash
curl -fsS http://localhost/health/detailed | python3 -c "
import sys,json
for name, s in json.load(sys.stdin)['components']['background_services']['services'].items():
    print(f\"{s['status']:10} fails={s['consecutive_failures']:<3} restarts={s['restarts']:<3} last_success={s['last_success']} {name}\")"
```

Each entry carries `status`, `last_success`, `last_error`,
`consecutive_failures`, `restarts`, `interval`. Read `last_error` first.

Remember this **never** causes a 503 — deliberately. A degraded cleanup loop
must not take the API out of the load balancer.

---

## 8. Nobody can log in

**Check**

```bash
$DC exec face_recognition sh -c "grep '\[AUTH_AUDIT\]' /var/log/face-recognition/app.log | tail -20"
```

Every attempt writes one line:

```
[AUTH_AUDIT] event=login result=failure request_id=<id> failure_code=<CODE>
  actor=<hash> source=<hash> duration_ms=<n> reference_id=AUTH-<8hex>
```

Usernames and IPs are pseudonymized — raw values never appear in logs, by design.

| `failure_code` | HTTP | Meaning | Fix |
|---|---|---|---|
| `INVALID_CREDENTIALS` | 401 | wrong password, or no such user — **identical response for both**, deliberately | reset the password (note: this forces the user through `/change-password` at their next sign-in) |
| `RATE_LIMITED` | 429 | too many failures in the window | wait `retry_after_seconds`, or see below |
| `CSRF_FAILED` | 403 | the sign-in came from an untrusted origin | `AUTH_ALLOWED_ORIGINS` must contain the exact browser origin |
| `SESSION_CREATION_FAILED` | 500 | token could not be issued | check Redis and the logs for `reference_id` |
| `AUTH_SERVICE_UNAVAILABLE` | 500 | unhandled error | grep the `reference_id` |

`POST /api/auth/change-password` writes to the same `[AUTH_AUDIT]` channel with
`event=change_password`, so these codes appear there too:

| `failure_code` | HTTP | Meaning |
|---|---|---|
| `INVALID_CURRENT_PASSWORD` | 403 | the current password was wrong; counts against the login throttle |
| `PASSWORD_REUSED` | 400 | the new password equals the current one |
| `WEAK_PASSWORD` | 400 | under 12 chars, fewer than 6 distinct, or a known default — the same rule the bootstrap password is judged by |
| `RATE_LIMITED` | 429 | too many attempts |
| `PASSWORD_UPDATE_FAILED` | 500 | the write failed; nothing changed |

## 8b. Everyone can log in, but nothing works

**Symptom:** login returns 200, every other call returns 403, and browsers
bounce to `/change-password` no matter which page they ask for.

```
[AUTH] Blocked: password rotation pending for user=<username>
```

**Cause:** the account still holds a password somebody else chose — the
deployment seed, or one an administrator typed. This is the rotation gate doing
its job, not a fault.

```sql
SELECT username, must_change_password, password_changed_at FROM users
WHERE must_change_password;
```

**Fix:** the user changes it at `/change-password` (or
`POST /api/auth/change-password`). Only that, logout and `/api/auth/me` work
until they do.

**The related 401**, seen by scripts rather than browsers:

```
[AUTH] Token rejected (issued before the password changed) user=<username>
```

The token predates that account's `password_changed_at`. A password change ends
every other session for the account, deliberately — log in again.

### There is no account lockout

Worth knowing, because people look for one. There is no `locked_until`, no
`failed_login_attempts` column, no lockout setting. What exists is a **sliding
throttle** on expiring counters:

| Setting | Default |
|---|---|
| `AUTH_RATE_LIMIT_ACCOUNT_MAX` / `_WINDOW` | 8 failures / 900 s |
| `AUTH_RATE_LIMIT_IP_MAX` / `_WINDOW` | 30 failures / 900 s |
| `AUTH_RATE_LIMIT_GLOBAL_MAX` / `_WINDOW` | 600 attempts / 60 s |

**A successful login clears the account and IP counters**, so an attacker
cannot lock a real user out by burning their counter. Nothing to "unlock" —
either wait out the window, or restart Redis to clear counters in an emergency.

nginx throttles independently: `/api/auth/(login|logout|change-password)` at
**10 r/m**, `burst=5`, returning 429. `change-password` is in that list because
it verifies the *current* password — outside the zone it would be an
unthrottled password oracle sitting beside a throttled one. If you see 429 with
no `[AUTH_AUDIT]` line at all, you were stopped at the proxy, not the app.

Token lifetime is `ACCESS_TOKEN_EXPIRE_MINUTES` (default 1440 = 24 h). **There
is no refresh token** — renewal means logging in again.

---

## 9. The map is blank

**Ask the API first.** It knows which basemaps are usable and why, and it will
name the dataset and a machine-readable reason:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost/api/maps/availability \
  | python -m json.tool
```

```jsonc
"satellite": { "available": false, "reason": "CONTENT_MISSING",
               "reason_text": "lebanon-satellite is not installed" }
```

| Reason code | What it means | Fix |
|---|---|---|
| `CONTENT_MISSING` | the archive is not installed | build it (`scripts/map_data/build_*.sh`) and install it |
| `CONTENT_NOT_VERIFIED` | it is installed but its content has never been measured, or it changed since it was | `curl -X POST /api/maps/verify` (admin) |
| `PLACEHOLDER_CONTENT` | its tiles are an upstream error image | **rebuild it** — this archive contains no map |
| `CONTENT_DEGENERATE` | the tiles decode but carry no map (blank, uniform, all identical) | rebuild |
| `SOURCE_BLOCKED` | the tiles are a server deny page saved as data | the source refused the download; check the licence/credentials, then rebuild |
| `ARCHIVE_CORRUPT` / `METADATA_INVALID` / `BUILD_INCOMPLETE` | the archive is damaged or half-built | rebuild |
| `RESOURCES_MISSING` | a glyph stack the style needs is not served | check `map-data/production/fonts/` and restart Martin |
| `MARTIN_UNREACHABLE` | the tile server is down | `docker compose -f docker/docker-compose.cpu.yml ps martin` |

Then, for everything at once, with the measured value behind each rule:

```bash
docker exec face_recognition_api python3 /app/scripts/map_data/production_gate.py \
    --allow-unavailable satellite
```

> **Do not diagnose this by looking for files.** A tile request returning 200
> and an archive of the right size prove only that bytes exist. This section
> used to tell you to `curl /tiles/13/4892/3253.png` and expect `200` — and it
> returned `200` for months while the basemap was 145,718 copies of
> OpenStreetMap's "Access blocked" image. Content is measured, not inferred;
> see `46_MAP_SERVICE_GUIDE.md` §4.

**Nothing renders at all, every style unavailable:** Martin is not reachable.
It is proxied at `/maps/` by nginx with a variable upstream and a Docker
resolver, so nginx starts even when Martin is down.

```bash
docker exec face_recognition_api curl -s http://martin:3000/catalog | head -c 300
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/maps/catalog
```

**A style is available but shows nothing:** the archive serves, but the style
may reference a source layer it does not contain, or a zoom outside its range.

```bash
docker exec face_recognition_api python3 -m pytest \
  /app/tests/test_maplibre_stack.py -k "source_layer or zoom_range" -q
```

**After replacing an archive:** Martin 1.13.0 has **no hot reload**. It keeps an
open handle to the replaced file and its own tile cache, so it keeps serving the
OLD data while the catalog still lists the id. Always install through
`scripts/map_data/install_dataset.sh ... --restart-martin`, which verifies
freshness rather than presence.

**A "blank" map that is actually correct:** when no movement has usable
coordinates the service renders an empty map centred on `MAP_DEFAULT_LAT/LON`.
It never plots at 0,0.

**A camera missing from the map** is reported, never invented. The response
carries:

```json
"tracking": {"status": "partial", "reason_code": "NO_COORDINATES",
             "movement_count": 12, "days_with_activity": 3}
```

`reason_code: "NO_MOVEMENT_DATA"` means there was nothing to plot at all.
**Fix:** Admin → Pipelines → Set Coordinates for that camera. Coordinates are
never fabricated and missing ones are never converted to 0,0.

---

## 10. Faces are not being recognised

**Check the per-face summary line** — the single best grep:

```bash
$DC exec face_recognition sh -c \
  "grep '\[PROCESS\] Identity:' /var/log/face-recognition/app.log | tail -30"
```

```
[PROCESS] Identity: <id> (<type>), name: <name>, sim: 0.3812, new: True
```

| Symptom in the log | Meaning | Fix |
|---|---|---|
| `sim` just under threshold, `new: True` | below `SIMILARITY_THRESHOLD` (default **0.4**) | enrol more images of that person at varied angles; lower the threshold only deliberately |
| `[IDENTITY_SEARCH] [PGVECTOR] No match in KNOWN identities (threshold=0.4)` | as above | as above |
| `⚠️ N person crops had no faces detected` | SCRFD found no face inside the upstream box | see below |
| `New identity ... created but embedding NOT saved (quality 0.31 < threshold 0.5)` | image quality gate rejected it | improve camera placement/lighting, or adjust `IDENTITY_QUALITY_THRESHOLD_*` |
| `❌❌❌ CRITICAL: Identity with name '<x>' is UNKNOWN type!` | data inconsistency | investigate; do not ignore |

A face below `SIMILARITY_THRESHOLD` is **not** re-searched at a lower bar. That
second-chance search was removed on purpose: below the threshold means "not
this person".

### "No faces detected"

The per-crop line is **DEBUG**, so invisible at the default `LOG_LEVEL=INFO`.
The visible aggregate is:

```
[PROCESS] ⚠️ N person crops had no faces detected - possible reasons:
```

Causes, in order of likelihood: person facing away · crop too small · occlusion
· a bad upstream bounding box. Also check for `⚠️ Crop too small: WxH pixels
(minimum 10x10 required)` and `❌❌❌ CRITICAL: Bbox N is WAY outside image bounds!`
— the latter means the camera is sending coordinates that do not match the image.

To see the detail without flooding `docker logs`, set **`LOG_FILE_LEVEL=DEBUG`**
and leave `LOG_LEVEL=INFO`. That keeps a verbose on-disk trace only.

---

## 11. A user hit a 500

Every response carries **`X-Request-ID`** (12 hex chars), and every log line
emitted while serving that request carries `req=<id>`.

```bash
curl -sS -D- -o /dev/null http://localhost/api/whatever | grep -i x-request-id

$DC exec face_recognition sh -c \
  "grep 'req=a1b2c3d4e5f6' /var/log/face-recognition/app.log"
```

The 500 body from the middleware is
`{"error": "Internal server error", "request_id": "<12 hex>"}`.

> Caveat: 500s raised as an explicit `HTTPException(500, ...)` return
> `{"detail": ..., "status_code": 500}` with **no request_id in the body**. The
> `X-Request-ID` header is still present and `req=` is still in the log — use
> the header.

Auth endpoints use a second scheme: **`AUTH-` + 8 hex**, in
`error.reference_id` and in the `[AUTH_AUDIT]` line.

```bash
$DC exec face_recognition sh -c "grep 'AUTH-1a2b3c4d' /var/log/face-recognition/app.log"
```

Slow requests (>2 s) log `[REQUEST] 🐢 SLOW request_id=...`. Health and metrics
paths are excluded from request logging entirely.

**Secrets are redacted before any handler sees them** — `Authorization`,
`x-webhook-key`, passwords, bearer tokens, bare JWTs and raw embeddings become
`***REDACTED***`. Do not expect to confirm what a camera sent by reading its key
in the log.

---

## 12. The site is up but pages 404 or misbehave through nginx

```bash
$DC logs --tail 100 nginx
$DC exec nginx nginx -t                       # config syntax
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost/health/live
```

### 502 immediately after recreating the API container

**This one bites every time and looks like a broken deployment.** After
`up -d --force-recreate face_recognition`, the new container gets a **new IP**,
but nginx resolved the upstream once at startup and keeps connecting to the old
address:

```
connect() failed (111: Connection refused) while connecting to upstream,
upstream: "http://172.22.0.4:8000/health/live"
```

The give-away is that the API itself is healthy while nginx returns 502:

```bash
$DC exec face_recognition curl -s -o /dev/null -w '%{http_code}\n' \
  http://localhost:8000/health/live      # 200 — the app is fine
curl -o /dev/null -w '%{http_code}\n' http://localhost/health/live   # 502 — nginx is stale
```

**Fix:** restart the proxy.

```bash
$DC restart nginx
```

> Related: `docker restart` **preserves the container's original environment**.
> If you change a value in the compose file, a plain `restart` will not pick it
> up — you need `up -d --force-recreate <service>` (and then the nginx restart
> above). A setting that "won't take effect" is usually this.

### A feature that worked yesterday breaks after a recreate

**Cause: the running container had drifted from the image.** If anyone ever
fixed something by running a command *inside* a live container, that fix exists
only in that container. `restart` keeps it; **`up -d --force-recreate` and any
update discard it**, reverting to whatever is baked into the image.

This is not hypothetical — it happened here twice. The map renderer once
depended on vendored Leaflet assets installed into `offline_folium` at build
time; the image in use predated that step, so the assets existed only because
someone had installed them by hand inside the running container, and recreating
it produced maps that silently fell back to a CDN — on an offline deployment,
a blank map. (That renderer has since been removed entirely; the browser now
draws the map from local tiles.) The same drift later left a running container
importing a package that the requirements files no longer declared.

**Check whether your image is older than your Dockerfile:**

```bash
docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedSince}}' | grep face
git log -1 --format=%cr -- docker/Dockerfile.cpu requirements-*.txt
```

**Fix — rebuild, do not just recreate:**

```bash
$DC build face_recognition && $DC up -d && $DC restart nginx
```

**Prevention:** always `build` as part of an update, and never leave a fix
applied only to a live container. If you must patch a running container to
restore service, put the same change in the Dockerfile the same day.

### Everything else

| Symptom | Cause |
|---|---|
| 502 / 504 (not just after a recreate) | the API is down or slower than the proxy timeout — check §1 first |
| 413 on upload | over `client_max_body_size`: global 200M, webhook 25m, auth 16k |
| 429 on normal browsing | the 20 r/s API zone — usually means static assets are being proxied instead of served by nginx |
| WebSocket disconnects | the `/ws` location has the upgrade map; other locations deliberately set `Connection ""` |
| `/docs` 404 in production | **correct** — docs are disabled in production by design |
| Login page loads but the browser blocks scripts | CSP is `script-src 'self'` with no `'unsafe-inline'` — an inline `<script>` will not run. This is intentional. |

---

## When you have to restart

```bash
$DC restart face_recognition          # backend only — first thing to try
$DC restart nginx                     # proxy only
$DC up -d --force-recreate face_recognition   # rebuild the container from the image
$DC stop && $DC up -d                 # whole stack, keeping data
```

> **Never `$DC down -v`.** The `-v` deletes the named volumes: the PostgreSQL
> database, stored faces, and logs. There is no undo, and recovery then depends
> entirely on your last backup. It is never part of normal administration.

Before anything destructive: [`60_BACKUP_AND_RESTORE.md`](60_BACKUP_AND_RESTORE.md).

---

## Escalation checklist

Collect these before asking anyone for help — they answer most questions on
their own:

```bash
$DC ps                                                        > /tmp/diag-ps.txt
curl -fsS http://localhost/health/detailed                    > /tmp/diag-health.json
$DC logs --tail 300 face_recognition                          > /tmp/diag-api.log
$DC exec face_recognition sh -c "tail -500 /var/log/face-recognition/app.log" > /tmp/diag-app.log
$DC exec -w /app/alembic face_recognition alembic current     > /tmp/diag-alembic.txt
```

`diag-app.log` is already redacted of secrets by the logging filter, but
**review it before sending it anywhere** — it contains identity names and
camera identifiers.

---

**See also:** [`72_ADMIN_CHEAT_SHEET.md`](72_ADMIN_CHEAT_SHEET.md) ·
[`61_DEPLOYMENT_RUNBOOK.md`](61_DEPLOYMENT_RUNBOOK.md) ·
[`74_SECURITY_CHECKLIST.md`](74_SECURITY_CHECKLIST.md) ·
[`21_WEBHOOK_TROUBLESHOOTING.md`](21_WEBHOOK_TROUBLESHOOTING.md) ·
[`35_IDENTITY_RECOGNITION_DEBUG_GUIDE.md`](35_IDENTITY_RECOGNITION_DEBUG_GUIDE.md)
