# Administrator Cheat Sheet

Every command you need to run this system day to day, on one page.

Set the compose target once per shell — every command below reuses it:

```bash
cd /path/to/FACE_DETECTOR

# Production, CPU:
export DC="docker compose -f docker/docker-compose.prod.yml"
# Production, GPU (LAYERED — keeps backup + monitoring + network isolation):
export DC="docker compose -f docker/docker-compose.prod.yml -f docker/docker-compose.prod.gpu.yml"
# Development:
export DC="docker compose -f docker/docker-compose.cpu.yml"
```

---

## Start / stop / restart

```bash
$DC up -d                       # start everything
$DC ps                          # what is running, and is it healthy
$DC stop                        # stop, keep containers and data
$DC restart face_recognition    # restart backend only
$DC restart nginx               # restart proxy only
$DC up -d --force-recreate face_recognition   # recreate backend from image
$DC restart nginx               # ALWAYS follow a recreate with this — see below
$DC down                        # remove containers, KEEP named volumes
```

> **`$DC down -v` DELETES THE DATABASE, STORED FACES AND LOGS.** Never run it
> on a system with real data. There is no undo.

> **Two things that change after a recreate.** A recreated container gets a new
> IP, and nginx keeps using the old one until restarted — so a recreate without
> `$DC restart nginx` gives you 502 while the API is perfectly healthy. And
> `restart` **keeps the container's original environment**: a compose or `.env`
> change needs `up -d --force-recreate`, not `restart`, to take effect.

Controlled host reboot:

```bash
$DC stop && sudo reboot         # containers restart via `restart: unless-stopped`
```

---

## Health

```bash
curl -fsS http://localhost/health/live                       # alive? (zero I/O)
curl -fsS http://localhost/health/ready                       # ready to serve? 503 if DB/models down
curl -fsS http://localhost/health/detailed | python3 -m json.tool | head -60
```

`/health/detailed` is the one that matters: queue depth vs capacity, DB pool,
Redis, storage %, models, and every background service with its last success.

Quick reads of the important numbers:

```bash
curl -fsS http://localhost/health/detailed \
  | python3 -c "import sys,json; d=json.load(sys.stdin)['components']; \
print('queue :', d['queue']); print('db    :', d['database']['healthy']); \
print('redis :', d['redis']); print('storage:', d['storage'])"
```

---

## Logs

Application logs go to stdout (captured by Docker) **and** to a rotating file
inside the container at `$LOG_DIR/app.log` (`/var/log/face-recognition`).

```bash
$DC logs -f --tail 100 face_recognition      # follow backend
$DC logs --tail 200 nginx                    # proxy
$DC logs --tail 200 postgres                 # database
```

> **Do not filter by `--since`.** Host and container clocks have drifted in this
> environment, so `--since 5m` has silently returned nothing while the service
> was actively logging. Use `--tail N` and read the timestamps in the lines
> themselves, which are written by the application in UTC.

Search the persisted file (survives `docker logs` truncation):

```bash
$DC exec face_recognition sh -c "grep -n 'ERROR' /var/log/face-recognition/app.log | tail -50"
$DC exec face_recognition sh -c "grep -n 'INTEL-' /var/log/face-recognition/app.log | tail -20"  # by reference id
```

Every 500 shown to a user carries a reference id; grep that id to find the
traceback without exposing internals to the browser.

---

## Database

```bash
$DC exec postgres psql -U postgres -d face_recognition -c "SELECT version();"
$DC exec postgres psql -U postgres -d face_recognition -c "\dt" | head -30
$DC exec postgres psql -U postgres -d face_recognition -c \
  "SELECT count(*) FROM identities; SELECT count(*) FROM detections;"
$DC exec postgres psql -U postgres -d face_recognition -c \
  "SELECT extname FROM pg_extension WHERE extname='vector';"   # pgvector present
```

Connections in use vs pool:

```bash
$DC exec postgres psql -U postgres -d face_recognition -c \
  "SELECT count(*), state FROM pg_stat_activity GROUP BY state;"
```

Migrations. **Normally you never run these by hand** — the application applies
them at startup according to `MIGRATIONS_MODE` (`backend/lifespan.py`). Use
these only to inspect state or to recover:

```bash
# alembic.ini lives in /app/alembic, NOT /app — without -w you get
# "No 'script_location' key found in configuration"
$DC exec -w /app/alembic face_recognition alembic current    # e.g. f6a7b8c9d0e1 (head)
$DC exec -w /app/alembic face_recognition alembic heads      # what head should be
$DC exec -w /app/alembic face_recognition alembic upgrade head
```

---

## Redis

```bash
$DC exec redis redis-cli PING              # PONG
$DC exec redis redis-cli INFO memory | grep used_memory_human
$DC exec redis redis-cli DBSIZE
```

---

## GPU

```bash
nvidia-smi                                             # host: driver + processes
$DC exec face_recognition nvidia-smi                   # inside the container
$DC exec face_recognition python -c \
  "import onnxruntime as ort; print(ort.get_available_providers())"
```

On the **CPU stack** this correctly prints
`['AzureExecutionProvider', 'CPUExecutionProvider']` — no CUDA, as intended.

On a **GPU deployment** `CUDAExecutionProvider` must appear. If it does not,
inference is silently running on CPU — correct results, far slower, and every
other signal looks healthy. That is what the `CudaProviderUnavailable` alert
watches for; it is gated on `face_detector_cpu_fallback_active`, so a CPU
deployment does not trigger it.

```bash
$DC logs face_recognition | grep -i "running on"       # SCRFD/ArcFace: running on CUDA
```

---

## Cameras and ingest

```bash
curl -fsS http://localhost/api/pipelines -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
curl -fsS -H "X-Webhook-Key: $INGEST_KEY" http://localhost/webhook/test      # credential check
```

Get a token first:

```bash
TOKEN=$(curl -fsS -X POST http://localhost/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<your password>"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

Recent detections:

```bash
curl -fsS "http://localhost/api/detections?limit=10" -H "Authorization: Bearer $TOKEN"
```

---

## Cross-camera tracking

```bash
# 1. find an identity
curl -fsS "http://localhost/api/admin/unknown?page=1&page_size=5" -H "Authorization: Bearer $TOKEN"

# 2. its camera-to-camera history, chronological
curl -fsS "http://localhost/api/identities/<ID>/cross-camera?days_back=7" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# 3. the offline map for the same track (HTML)
curl -fsS -o /tmp/map.html "http://localhost/api/identities/<ID>/map?days_back=7" \
  -H "Authorization: Bearer $TOKEN" && wc -c /tmp/map.html
```

A camera only appears on the map if its pipeline has latitude/longitude set
(Admin → Pipelines → Set Coordinates). Cameras without coordinates are reported
as `coordinates: null` and skipped — never placed at 0,0.

---

## Offline map tiles

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost/tiles/13/4892/3253.png   # 200
du -sh tiles/                                                                        # ~1 GB
ls tiles/ | sort -n                                                                  # 10..16
```

Tiles are XYZ `tiles/{z}/{x}/{y}.png`, zooms 10–16, Lebanon. They are mounted
read-only into nginx, never baked into the image. Backing up `tiles/` is
optional — it is reproducible from the download script.

---

## Storage and disk

```bash
df -h /                                       # host disk
docker system df                              # image/volume usage
$DC exec face_recognition du -sh /app/storage/faces /app/storage/pending
curl -fsS http://localhost/health/detailed | grep -o '"usage_percent":[0-9.]*'
```

---

## Monitoring

Only on production, or dev with `--profile monitoring`.

```bash
$DC --profile monitoring up -d prometheus grafana      # dev
curl -fsS http://localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"'   # expect "up"
curl -fsS http://localhost:9090/api/v1/rules | head -5
```

Grafana: <http://127.0.0.1:3000> (loopback only), user `admin`, password from
`GRAFANA_ADMIN_PASSWORD` in `.env`. There is no default password — the stack
refuses to start without it. Reach it remotely over an SSH tunnel:

```bash
ssh -L 3000:127.0.0.1:3000 you@server
```

---

## Backup

```bash
$DC exec -T postgres pg_dump -U postgres face_recognition | gzip > backup-$(date +%F).sql.gz
tar czf faces-$(date +%F).tar.gz storage/faces/
cp .env env-backup-$(date +%F)                # contains secrets — store securely
```

Production runs `scripts/backup/backup.sh` in the `backup` service. Full
procedure and restore drill: [`60_BACKUP_AND_RESTORE.md`](60_BACKUP_AND_RESTORE.md).

**Restore** (destructive — the target database is replaced):

```bash
$DC stop face_recognition                     # stop writers first
gunzip -c backup-2026-08-11.sql.gz | $DC exec -T postgres psql -U postgres -d face_recognition
$DC start face_recognition
curl -fsS http://localhost/health/detailed
```

---

## Update the application

```bash
# 1. back up first (above) — always
# 2. get the new code
git pull
# 3. review config changes
git diff HEAD@{1} -- .env.example config.py
# 4. build and restart
$DC build face_recognition
$DC up -d
# 5. migrations apply themselves at startup (MIGRATIONS_MODE); confirm:
$DC exec -w /app/alembic face_recognition alembic current
# 6. verify
curl -fsS http://localhost/health/detailed
```

Roll back:

```bash
git checkout <previous-tag>
$DC build face_recognition && $DC up -d
$DC exec -w /app/alembic face_recognition alembic downgrade -1   # only if the update migrated
```

Migrations that drop columns are not reversible by downgrade alone — restore
from backup instead. Check the migration before assuming it can be undone.

---

## The five things that go wrong most

| Symptom | First command | Usual cause |
|---|---|---|
| Site down | `$DC ps` | a container exited — read its logs |
| Backend exits code 78 | `$DC logs face_recognition \| tail -30` | config preflight rejected a setting (reason is printed) |
| Frames rejected 503 | `curl .../health/detailed` → queue | ingest outrunning inference; queue at capacity |
| Map blank | `curl -o /dev/null -w '%{http_code}' .../tiles/13/4892/3253.png` | tiles not mounted |
| Login fails | `$DC logs face_recognition \| grep AUTH` | wrong password, or account locked out |

Full decision tree: [`73_TROUBLESHOOTING.md`](73_TROUBLESHOOTING.md).
