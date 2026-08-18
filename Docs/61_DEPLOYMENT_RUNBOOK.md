# Production Deployment Runbook

Exact commands to deploy, verify and roll back. Follow the order — several
steps are prerequisites for the ones after them, and the stack deliberately
refuses to start if you skip one.

**The application refuses to start in production with an unsafe configuration.**
That is by design. If a container exits with code `78`, read its stderr: the
preflight prints every problem it found, with the fix for each, and never
prints a secret value.

---

## 0. What changed, and why it matters

The repository shipped the placeholder JWT signing key
`your-secret-key-change-in-production`. A JWT is not a session ID the server
remembers — it is a claim plus a signature, and the signing key is the only
thing that makes it real. With that key public, an administrator token could be
forged in ten lines, and it was: `/api/auth/me` returned `role: admin`. No
password, no failed login to rate-limit, no audit entry.

Every control below exists because of a defect that was verified, not assumed.

| Before | After |
|---|---|
| Placeholder JWT key, no override anywhere | Fail-closed preflight, exit 78 |
| Application connected as the `postgres` superuser | `fr_app`, refused CREATE ROLE/EXTENSION/DATABASE |
| Postgres, Redis, Ollama published to `0.0.0.0` | Only nginx publishes |
| Redis unauthenticated, holding sessions and the revocation denylist | ACL user, password hashed in the ACL file |
| No TLS; port 443 published but unbound | TLS 1.2/1.3, 308 redirect, internal CA |
| `X-Forwarded-For` trusted from every LAN client | Not trusted; nginx is the edge |
| `/docs`, `/redoc`, `/openapi.json`, `/metrics` public | Docs off; metrics restricted |
| Bootstrap admin hardcoded, password logged in cleartext | Secret file, rotation forced, never logged |
| Missing `alembic.ini` reported as migration success | Fail closed, revision pinned |
| GPU: CUDA 11.8 + unpinned ORT → silent CPU fallback | CUDA 12.4 + pinned ORT, verified with real inference |
| GPU: `WORKERS: 16` against process-local state | `WORKERS: 1`, guarded at startup |
| No backups | Scheduled, checksummed, restore-tested |

---

## 1. Prerequisites

```bash
docker --version          # 24+
docker compose version    # v2
openssl version
nvidia-smi                # GPU deployments only; driver must be >= 525
```

Pick the hostname clients will use. It goes in the certificate and in
`PUBLIC_ORIGIN`, and they must match exactly or login will be rejected as
cross-origin.

```bash
export PUBLIC_HOST=face-detector.internal
```

Add it to DNS, or to each client's hosts file.

### 1.1 Assets that must be present before first start

The production server is offline; these are fetched on a connected machine and
transferred. **Without the model weights the API refuses to start** (ONNX load
failure at boot); without the map archives the basemap picker reports every
style unavailable; without the Ollama models the chatbot returns errors while
everything else works.

| Asset | Where it lives | How to obtain |
|---|---|---|
| Model weights | `weights/det_10g.onnx` (16 MB), `weights/w600k_r50.onnx` (166 MB) | `bash scripts/setup/download.sh` on a connected machine, then copy `weights/` over |
| Map archives | `map-data/production/*.mbtiles` (streets vector ~39 MB, DEM ~80 MB, satellite when built) | Build on a connected machine with `scripts/map_data/build_all.sh`, copy `map-data/production/` over, then **verify on the target**: `curl -X POST /api/maps/verify`. Copying the files is not enough — an archive whose content has not been measured is reported unavailable, by design (see `46_MAP_SERVICE_GUIDE.md` §4). |
| Chatbot LLMs | inside the `ollama_data` volume | after first start: `docker compose $COMPOSE_PROD exec ollama ollama pull qwen2.5:1.5b` and the SQL model named by `OLLAMA_SQL_MODEL` in the compose file — on an offline server, `ollama pull` on a connected machine and transfer the volume, or ship the models with the server |

Verify before deploying:

```bash
ls -la weights/         # det_10g.onnx and w600k_r50.onnx present

# Map archives: presence proves nothing. The Light basemap was once 145,718
# copies of an "Access blocked" image and passed every file-level check there
# was. Ask what the content actually IS:
docker exec face_recognition_api python3 /app/scripts/map_data/production_gate.py \
    --allow-unavailable satellite
```

---

## 2. Generate secrets

```bash
bash scripts/setup/generate-secrets.sh
```

Writes `secrets/jwt_secret`, `secrets/bootstrap_admin_password` and
`secrets/webhook_api_keys` (mode 600), generates `docker/redis/users.acl` with
SHA-256 password hashes, and writes **`docker/.env`** with the deployment
credentials. All are gitignored.

> **`docker/.env`, not the repository-root `.env`.** Compose reads `.env` from
> the *project directory* — the directory of the first `-f` file, i.e.
> `docker/`. A root `.env` is never consulted for `${VAR}` substitution. The
> generator used to print these values for you to paste into the root `.env`,
> which produced a deployment that still failed with
> "POSTGRES_SUPERUSER_PASSWORD is required" while you stared at a file that
> plainly contained it.
>
> Two files, two jobs: root `.env` is **application** config read inside the
> container; `docker/.env` is **deployment** credentials used only for
> interpolation. See [`docker/.env.example`](../docker/.env.example).

`secrets/webhook_api_keys` is the **break-glass ingest key**, mounted as a Docker
secret and required by both production compose files — the stack will not start
without it. Senders present it as `Authorization: Bearer <key>` or
`X-Webhook-Key: <key>`.

Treat it as break-glass, not as the fleet credential. After the first admin
login, issue one **named credential per external system** at
**Admin → Ingest Credentials** (`/admin/ingest-credentials`): the token is shown
once, only its SHA-256 is stored, and revoking one sender is a row deletion
rather than a fleet-wide rotation. Keeping the environment key configured is what
lets startup and a database outage stay independent of that table.

**Revocation latency.** Each worker caches issued credentials for
`WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS` (default 30). Deleting a credential takes
effect on every worker within that window, and a frame presented inside it may
still be accepted. For immediate effect, rotate the environment key and restart.

**Migrations.** `MIGRATIONS_EXPECTED_HEAD` in the compose files pins the expected
Alembic revision; bump it in the same change as any new migration.

`docker/.env` is written for you. Confirm it, and set `PUBLIC_ORIGIN` to the
host clients will actually use:

```bash
cat docker/.env          # contains live credentials — do not paste elsewhere
```

`ENVIRONMENT=production` is not set here: the production compose file pins it
per-service, so it cannot be switched off by an environment file.

> **On a Windows dev host the `chmod 600` silently does nothing** — NTFS
> ignores POSIX modes, so the generated files come out world-readable. It
> applies normally on the Linux production target. If you generate secrets on
> Windows and copy them across, re-apply the mode on the server:
>
> ```bash
> chmod 600 secrets/* docker/.env docker/redis/users.acl
> ```

Existing files are never overwritten — including `docker/.env`. To rotate one
credential, delete its line and re-run; to rotate a secret file, delete the
file and re-run.
**Rotating `jwt_secret` invalidates every issued token and logs everyone out.**
That is intended, and it is how you evict a stolen token.

---

## 3. Issue TLS certificates

```bash
bash scripts/tls/make-internal-ca.sh "$PUBLIC_HOST" 192.168.1.50
```

Second argument is the server's LAN IP, added as a SAN so `https://<ip>/` also
validates.

Install `certs/internal-ca.crt` on every client:

| Platform | Command |
|---|---|
| Windows | `certutil -addstore -f "ROOT" certs\internal-ca.crt` |
| macOS | `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain certs/internal-ca.crt` |
| Linux | `sudo cp certs/internal-ca.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates` |
| Firefox | Settings → Privacy & Security → Certificates → Authorities → Import |

Firefox keeps its own trust store and ignores the system one.

Then move `certs/internal-ca.key` to offline storage. It can mint a certificate
for *any* hostname that your clients will now trust.

---

## 4. Create the database roles

Start Postgres alone, then apply the roles:

```bash
docker compose -f docker/docker-compose.prod.yml up -d postgres

docker compose -f docker/docker-compose.prod.yml exec -T postgres \
  psql -U postgres -d face_recognition -v ON_ERROR_STOP=1 \
    -v app_password="'<FR_APP_PASSWORD>'" \
    -v migrator_password="'<FR_MIGRATOR_PASSWORD>'" \
    -v readonly_password="'<FR_READONLY_PASSWORD>'" \
    -v backup_password="'<FR_BACKUP_PASSWORD>'" \
    -f /db/roles.sql
```

Idempotent, and safe on a populated database. Expect four rows with every
privilege column `f`.

Verify the application role cannot escalate:

```bash
docker compose -f docker/docker-compose.prod.yml exec -T postgres \
  env PGPASSWORD='<FR_APP_PASSWORD>' psql -U fr_app -h 127.0.0.1 -d face_recognition \
  -c "CREATE ROLE probe LOGIN SUPERUSER"
# expected: ERROR: must be superuser to create superusers
```

---

## 5. Deploy

```bash
docker compose -f docker/docker-compose.prod.yml config -q   # fails if a secret is missing
docker compose -f docker/docker-compose.prod.yml up -d --build
docker compose -f docker/docker-compose.prod.yml ps
```

Start order is enforced by the compose file: `postgres` → `migrate` (runs to
completion) → `face_recognition` → `nginx`. API replicas cannot race on
migrations because they wait for `service_completed_successfully`.

**GPU production** does NOT swap compose files — it LAYERS an override on top,
so the backup, prometheus and grafana services and the edge/data/ai/monitoring
network segmentation defined in `prod.yml` are all retained:

```bash
docker compose -f docker/docker-compose.prod.yml \
               -f docker/docker-compose.prod.gpu.yml up -d
```

`docker/docker-compose.gpu.yml` is a **development** GPU override, layered
on `docker-compose.cpu.yml`. It is not a production stack and cannot be
used as one — it declares no database, no backup service and no monitoring.

### The four invocations — the only supported ones

Two base stacks × two hardware overrides. There is no other combination.

| Stack | Command | Project |
|---|---|---|
| Development, CPU | `docker compose -f docker/docker-compose.cpu.yml …` | `face_detector_dev` |
| Development, GPU | `docker compose -f docker/docker-compose.cpu.yml -f docker/docker-compose.gpu.yml …` | `face_detector_dev` |
| **Production, CPU** | `docker compose -f docker/docker-compose.prod.yml …` | `face_detector_prod` |
| **Production, GPU** | `docker compose -f docker/docker-compose.prod.yml -f docker/docker-compose.prod.gpu.yml …` | `face_detector_prod` |

The GPU files are **overrides**, never used alone — alone they declare no
database, no proxy and no application dependencies.

**The project names are load-bearing, not cosmetic.** Compose namespaces every
volume by project. Neither file used to declare one, so both defaulted to the
parent directory (`docker`) and both declared volumes called `postgres_data`,
`redis_data`, `face_database_data` and `chromadb_cache` — meaning **starting
production mounted the development database**. Never set
`COMPOSE_PROJECT_NAME`: it overrides the `name:` key and reintroduces exactly
this bug.

Define the pair once so every later command is identical:

```bash
export COMPOSE_PROD="-f docker/docker-compose.prod.yml"                       # CPU
export COMPOSE_PROD="-f docker/docker-compose.prod.yml -f docker/docker-compose.prod.gpu.yml"  # GPU
docker compose $COMPOSE_PROD ps
```

---

## 6. Verification

Run all of it. Each command targets a specific defect that was present before.

### 6.1 The forged token must be rejected

The single most important check:

```bash
docker compose -f docker/docker-compose.prod.yml exec -T face_recognition python -c "
import jwt, datetime, urllib.request, urllib.error
t = jwt.encode({'sub':'1','role':'admin',
                'exp': datetime.datetime.utcnow()+datetime.timedelta(hours=1)},
               'your-secret-key-change-in-production', algorithm='HS256')
r = urllib.request.Request('http://localhost:8000/api/auth/me')
r.add_header('Authorization', 'Bearer ' + t)
try:
    urllib.request.urlopen(r); print('FAIL - still forgeable')
except urllib.error.HTTPError as e: print('PASS - rejected', e.code)"
```

Expected: `PASS - rejected 401`.

### 6.2 TLS

```bash
curl -I http://$PUBLIC_HOST/                                  # 308 to https
curl --cacert certs/internal-ca.crt -I https://$PUBLIC_HOST/  # 200
```

Never use `curl -k` here — it disables the check you are performing.

On **Windows**, curl uses schannel, which requires a revocation endpoint that an
internal CA does not publish. `CERT_TRUST_REVOCATION_STATUS_UNKNOWN` is that
lookup failing, not a bad certificate; add `--ssl-no-revoke`. Browsers are
unaffected once the CA is installed.

Inspect the login cookie:

```bash
curl --cacert certs/internal-ca.crt -sS -D headers.txt \
  -H 'Content-Type: application/json' \
  -H 'X-Requested-With: XMLHttpRequest' \
  -H "Origin: https://$PUBLIC_HOST" \
  -d '{"username":"admin","password":"<bootstrap password>"}' \
  "https://$PUBLIC_HOST/api/auth/login"

grep -i '^set-cookie:' headers.txt
```

Must contain `Secure`, `HttpOnly`, `Path=/`, `SameSite`, and — for the
`__Host-` prefix — **no** `Domain` attribute.

### 6.3 Exposure, from a different machine on the LAN

```bash
nc -zv <host> 5432    # must fail
nc -zv <host> 6379    # must fail
nc -zv <host> 11434   # must fail
nc -zv <host> 443     # must succeed
```

This must be run from another machine. From the host itself, Docker's published
port path makes traffic appear to originate from the bridge gateway, so a local
test does not prove what a LAN client sees.

```bash
docker compose -f docker/docker-compose.prod.yml ps    # only nginx maps ports
sudo ss -lntp
```

### 6.4 Documentation and metrics

```bash
curl --cacert certs/internal-ca.crt -o /dev/null -w '%{http_code}\n' https://$PUBLIC_HOST/docs
# expected: 404

curl --cacert certs/internal-ca.crt -o /dev/null -w '%{http_code}\n' https://$PUBLIC_HOST/metrics
# expected: 403 from a LAN client
```

Prometheus reaches `/metrics` on the internal monitoring network and does not
depend on that path.

### 6.5 Migrations fail closed

```bash
docker compose -f docker/docker-compose.prod.yml exec -T face_recognition \
  python -m backend.utils.migrations --verify
echo "exit=$?"     # 0
```

Then prove the failure path — point at an outdated schema and confirm the
container exits non-zero instead of serving:

```bash
docker compose -f docker/docker-compose.prod.yml run --rm \
  -e MIGRATIONS_EXPECTED_HEAD=deadbeefcafe face_recognition \
  python -m backend.utils.migrations --verify
echo "exit=$?"     # 78
```

### 6.6 GPU (GPU deployments only)

Provider discovery alone is **not** acceptance. It reports what the build
supports, not what initialised.

```bash
docker compose $COMPOSE_PROD exec -T face_recognition python -c "
import onnxruntime as ort
print(ort.__version__, ort.get_available_providers())
assert 'CUDAExecutionProvider' in ort.get_available_providers()"

docker compose $COMPOSE_PROD exec -T face_recognition nvidia-smi
```

Then put real inference load through the service and confirm `nvidia-smi` shows
non-zero utilisation and memory. Startup already runs a smoke inference and
aborts if CUDA is not actually in use, but sustained load is what reveals OOM
and thermal limits.

```bash
docker compose $COMPOSE_PROD logs face_recognition | grep -i "running on"
# expected: SCRFD: running on CUDA / ArcFace: running on CUDA
```

### 6.7 Regression suite

Run this on the **development** stack. Do not run it against production, and do
not install the test runner into a production container: the suite creates and
deletes records, and the integration tests authenticate as `admin`/`admin123`,
an account that exists only in the development stack.

The development image already ships pytest — `docker-compose.cpu.yml` builds it
with `INSTALL_DEV=true` — so there is nothing to install first:

```bash
docker exec face_recognition_api python -m pytest tests/ -q
```

If pytest is genuinely absent, rebuild rather than installing by hand; anything
added ad-hoc is lost the next time the container is recreated:

```bash
docker compose -f docker/docker-compose.cpu.yml build \
  --build-arg INSTALL_DEV=true face_recognition
```

For production, use the read-only checks in the sections above.

---

## 7. First login

The bootstrap administrator is created only when no administrator exists, with
`must_change_password` set. Read the password from the secret file:

```bash
cat secrets/bootstrap_admin_password
```

It is never written to logs. Change it immediately after first login, then
delete the secret file if you do not need to re-bootstrap.

---

## 8. Backups

The `backup` service runs on a loop (default 24h). Take one manually:

```bash
docker compose -f docker/docker-compose.prod.yml exec -T backup sh /scripts/backup.sh /backups
docker compose -f docker/docker-compose.prod.yml exec -T backup ls -la /backups
```

Set `BACKUP_REMOTE_PATH` to an off-host mount. A backup that lives only on the
machine it protects does not survive the failure it exists for.

**Backups are not "working" until a restore has succeeded.** See
`Docs/60_BACKUP_AND_RESTORE.md`; rehearse quarterly into a scratch database.

---

## 9. Rollback

### Application

```bash
git log --oneline
git checkout <previous-commit>
docker compose -f docker/docker-compose.prod.yml up -d --build
```

Check whether the newer version applied a migration first:

```bash
docker compose -f docker/docker-compose.prod.yml exec -T face_recognition \
  python -m backend.utils.migrations --verify
```

If it did, the older code will refuse to start against the newer schema — that
is the fail-closed behavior working. Either downgrade the schema deliberately
(`alembic downgrade <revision>`) or restore from a backup taken before the
deployment. Do not force the application to start against a schema it does not
recognise.

### Configuration

`ENVIRONMENT=development` disables the production guard entirely. Use it to get
a stack up while diagnosing, **never** as a way to run production with a
configuration that failed the preflight — the checks exist because each one
corresponds to a real, demonstrated hole.

### Secrets

Rotating `jwt_secret` logs everyone out immediately. Rotating a database
password requires `ALTER ROLE ... PASSWORD` and a matching `.env` update in the
same maintenance window, or the application cannot connect.

### Emergency stop

```bash
docker compose -f docker/docker-compose.prod.yml stop nginx   # cut off traffic, keep data services
docker compose -f docker/docker-compose.prod.yml down         # stop everything (volumes survive)
```

`down -v` **deletes the volumes**, including the database. Do not use it to
"restart cleanly".

---

## 10. Known limitations

- **The GPU track is unverified on real hardware.** It was developed on a host
  with no NVIDIA GPU (Radeon 520 / Intel UHD 620). The CUDA 12.4 + ORT 1.20.1
  pairing is chosen for compatibility, and the policy is unit-tested with
  session doubles, but §6.6 must be run on the real GPU host before trusting it.
- **`WORKERS` must stay 1.** Runtime settings, the SQL-agent cancellation
  registry, the relationship/threshold/training single-flight guards, webhook
  dedup, FAISS autosave and the in-process revocation fallback are all
  process-local. Raising it silently breaks correctness rather than crashing.
  `ALLOW_MULTI_WORKER=true` exists only for after that state moves to
  Redis/Postgres.
- **HSTS is off.** Enable it only after confirming every client trusts the
  internal CA; it removes the browser's ability to fall back to HTTP.
- **The metrics IP allowlist depends on nginx seeing the true client address.**
  Verify §6.3 from a real LAN machine rather than assuming.
- **Prometheus and Grafana are not authenticated at the network edge.** Grafana
  is bound to loopback; reach it over an SSH tunnel.


## 11. Data-model corrective pass (2026-08-16) — operational semantics

### 11.1 Detection persistence: three failure classes
Every frame is persisted by ONE function, `backend/core/detection_evidence.persist_detection` (batch writer and direct path), one transaction per detection:

| Class | What failed | What is committed | Signal |
|---|---|---|---|
| **A — core evidence** | detection / faces / appearance insert, or the exact embedding back-link (`CROSS_LINK_REFUSED`, `EMBEDDING_MISSING`) | **nothing** — the whole per-detection transaction rolls back; no alert rows, no broadcast; the embedding THIS frame created is compensated (deleted) and an evidence-free frame-created unknown identity removed | metric `metrics_db_operation_failures{reason="detection_core"}` + structured error log; the frame is reported not persisted |
| **B — optional alert enrichment** | live-alert lookup/trigger insert (savepoint A) or watchlist lookup/alert insert (savepoint B) | core evidence + the OTHER subsystem's rows; only the failing savepoint rolls back | `reason="alert_enrichment_live"` / `"alert_enrichment_watchlist"`; nothing claims an alert that was not persisted |
| **C — post-commit broadcast** | the `detection_alerts` WebSocket send | everything — rows stay | `reason="alert_broadcast"`; the DB is authoritative (`GET /api/watchlist-alerts`, live-alert listing) |

**Reliability limitation (recorded, deliberately NOT implemented as a fallback):** after a class-B failure the alert is not recreated automatically. A future enhancement is a durable alert-evaluation retry / outbox / reconciliation over committed detections — not a fallback broadcast, not a duplicate legacy path, not a silent retry loop.

Crash safety: a worker dying between the identity/embedding commit and the detection commit leaves a camera embedding with `detection_id NULL`; `identity_retention.reconcile_orphan_camera_embeddings` (startup phase 2.2f + every retention cycle) removes such rows older than `STALE_CAMERA_EMBEDDING_GRACE` (10 min) through the canonical vector-removal path. Steady state: `SELECT count(*) FROM identity_embeddings WHERE pipeline_id IS NOT NULL AND detection_id IS NULL AND created_at < now() - interval '10 minutes'` = 0.

### 11.2 Camera (pipeline) delete policy
`identity_appearances`, `identity_embeddings`, `detections` reference `pipelines` with **RESTRICT**. A camera with evidence is deactivated (`is_active = 0`), never hard-deleted; the rename flow moves every child first; wipe scripts pre-clear. There is no delete route.

### 11.3 Schema lifecycle
Alembic is the only schema initializer (root `000_baseline`; head `f6a7b8c9d0e1`); `init_db` verifies the exact head fail-closed everywhere; `MIGRATIONS_FAIL_CLOSED` was **REMOVED**. Legacy dev/demo databases: `python scripts/repair_relationship_integrity.py` (dry-run → `--apply --yes-i-understand`) BEFORE `alembic upgrade head`; migrations refuse (never delete) when a precondition fails. Never run the repair on production.

### 11.4 Full regression — isolated only
`scripts/run_regression_isolated.sh [pytest args]`: unique scratch database on the dev PostgreSQL server, a dedicated `redis_regression`, a throwaway `face_recognition_regression` container (its own service name — the dev nginx upstream `face_recognition` never resolves to it, asserted), ephemeral volumes for storage / ML artifacts / database / logs / chroma; isolation assertions run inside the container BEFORE pytest (DB name, `current_database()`, Redis host + IP + sentinel key + pub/sub invisibility from the dev Redis, mount sources, `ENVIRONMENT`, DSN template) and abort on any failure; `trap teardown EXIT` drops the database and removes the stack on every exit path and prints whether the dev side is unchanged. The standing "wipe after regression" rule does not apply to isolated runs. Focused suites may still run in the dev container.

### 11.5 Configuration inventory (this pass)
| Variable | Status | Note |
|---|---|---|
| `MIGRATIONS_MODE` | ACTIVE | `run` (dev) / `verify` (prod) — the head check itself is unconditional |
| `MIGRATIONS_EXPECTED_HEAD` | ACTIVE | operator-visible second pin; must equal the scripts' head (`f6a7b8c9d0e1`) |
| `MIGRATIONS_FAIL_CLOSED` | REMOVED | no consumer controlled a distinct behaviour; schema mismatch is never permissive |
| `DATABASE_URL`, `REDIS_URL`, `ENVIRONMENT`, storage paths, `ML_ARTIFACT_DIR` | ACTIVE | one central `config.py`; scripts import `settings` (the regression isolation checker is the one allowed raw-environment reader — it reports ON the environment) |
| `REGRESSION_ISOLATION_ID` | ACTIVE (regression only) | run marker injected by the runner, asserted inside the container; not an application setting |
| `STALE_CAMERA_EMBEDDING_GRACE` | code constant | 10 minutes; deliberately not an operator setting |
