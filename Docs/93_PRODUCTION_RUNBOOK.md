# How the System Fits Together — Order of Use

**This is an orientation map, not a command list.** It answers "what is each
piece, and in what order does it matter?" so the detailed documents make sense.

It does not replace any of them, and defers to them on every specific:

| For | Read |
|---|---|
| exact deploy commands, go-live, rollback | [`61_DEPLOYMENT_RUNBOOK.md`](61_DEPLOYMENT_RUNBOOK.md) — **the production authority; anything contradicting it is wrong** |
| day-to-day commands | [`72_ADMIN_CHEAT_SHEET.md`](72_ADMIN_CHEAT_SHEET.md) |
| something is broken | [`73_TROUBLESHOOTING.md`](73_TROUBLESHOOTING.md) |
| hardening before go-live | [`74_SECURITY_CHECKLIST.md`](74_SECURITY_CHECKLIST.md) |
| backup and restore drills | [`60_BACKUP_AND_RESTORE.md`](60_BACKUP_AND_RESTORE.md) |

Every path below is relative to the repository root. Every command shown was run
and verified on this host.

---

## 0. The mental model

Ten long-running containers plus one job that runs and exits.

```
        nginx :443            the only door in — terminates TLS
          |
          +-- face_recognition   the API + face pipeline (GPU)
          |     |
          |     +-- postgres     schema + identities + audit
          |     +-- redis        sessions, rate limits, queues
          |     +-- ollama       chat + SQL models (local, offline)
          |
          +-- martin             offline basemap tiles

        ml_worker                serialized ML job runner (training, drift)
        migrate                  one-shot: applies the schema, then exits
        backup                   pg_dump on a timer
        prometheus + grafana     metrics, bound to loopback only
```

Two rules explain most of the behaviour:

1. **Nothing starts before what it depends on is *healthy*.** A broken
   healthcheck does not fail loudly — it blocks the tier above it silently.
2. **The model proposes, Python disposes.** Configuration is declared in
   compose; `config.py` is the only thing that reads it.

`ml_worker` is deliberately different from the API: it sets `CONFIG_PREFLIGHT=0`
because it serves no traffic, so it takes only its database and runtime
configuration and stays unreachable from the edge network.

---

## 1. Order of use — what `./deploy.sh install` does

`deploy.sh` runs these stages in this order. Each is idempotent: **no stage ever
overwrites a secret or certificate that already exists**, so re-running is safe.

| # | Stage | What it produces |
|---|-------|------------------|
| 1 | `stage_preflight` | checks disk, RAM, docker, GPU driver |
| 2 | `stage_sys_install` | host packages (docker, nvidia toolkit) |
| 3 | `stage_workspace` | the directory tree, owners and modes (§3) |
| 4 | `stage_secrets` | `secrets/*` + `docker/redis/users.acl` + `docker/.env` (§4) |
| 5 | `stage_tls` | internal CA + server certificate (§5) |
| 6 | `stage_env_config` | fills `docker/.env` values |
| 7 | `stage_gpu_detect` | writes `docker/gpu-allocation.generated.yml` |
| 8 | `stage_model_check` | verifies ONNX weights and map archives are present |
| 9 | `stage_compose_validate` | renders compose; fails if any variable is unset |
| 10 | `stage_build` | builds the images |
| 11 | `stage_db_init` | applies `db/roles.sql` — the four least-privilege roles (§6) |
| 12 | `stage_up` | `docker compose up -d` |
| 13 | `stage_ollama_models` | pulls the chat and SQL models into the ollama volume |

```bash
sudo ./deploy.sh install     # first time, interactive
sudo ./deploy.sh doctor      # read-only: what is wrong, and the fix for it
sudo ./deploy.sh health      # the 31-check acceptance battery, no rebuild
sudo ./deploy.sh paths       # ownership/mode drift vs the manifest
sudo ./deploy.sh upgrade     # backup -> build -> pin head -> migrate -> restart
```

Full command sequence and go-live checks:
[`61_DEPLOYMENT_RUNBOOK.md`](61_DEPLOYMENT_RUNBOOK.md).

---

## 2. Why the start order is what it is

```
postgres   redis   ollama   martin   prometheus      (no dependencies)
    |        |                 |          |
  migrate    |                 |       grafana
    |  \     |                 |
    |   ml_worker              |
    +--------+--- face_recognition
                       |
                     nginx
```

Read as: `face_recognition` waits for **postgres healthy**, **redis healthy**,
and **migrate completed successfully**. `ml_worker` waits for postgres healthy
and migrate completed. `nginx` waits for face_recognition and martin healthy.

**The failure this causes.** A redis healthcheck that cannot authenticate leaves
redis `Up (unhealthy)` forever. face_recognition then never starts, nginx never
starts, and compose reports only *"dependency failed to start"*. The site is
down and nothing names redis. If the stack is stuck, check health from the
bottom up, not the top down:

```bash
sudo docker ps --format '{{.Names}}\t{{.Status}}'
```

---

## 3. The directory manifest — one table, one truth

`scripts/deploy/paths.sh` is the single source of ownership and permissions.
The rule it encodes:

| Kind of path | Owner / mode |
|---|---|
| a container **writes** it | uid 1000 (`itdirect-ai`, the service uid) |
| a container **reads** it | group 1000, readable, **never** writable |
| a secret | `0440 root:1000` |
| host-only | root, as tight as the tooling allows |

Verify at any time — this changes nothing:

```bash
sudo ./deploy.sh paths       # every row must say "ok"
```

**Note on `storage/`, `logs/`, `database/`:** production does **not** mount
these host directories — it uses named volumes. They stay empty by design. They
exist because the development stack (`docker-compose.cpu.yml`) binds them. This
is why `docker compose up` appears not to "generate" them.

---

## 4. Secrets — what each one is, and what breaks without it

Created once by `scripts/setup/generate-secrets.sh`. Mounted read-only into the
container at `/run/secrets/<name>`; the app is given the **path**, never the
value, via `*_FILE` variables.

| File | Read by | Purpose | If lost |
|---|---|---|---|
| `secrets/jwt_secret` | api, migrate | signs session cookies | **every session breaks**; everyone is logged out |
| `secrets/bootstrap_admin_password` | api | the first admin password | you cannot make the first login |
| `secrets/webhook_api_keys` | api, migrate | keys the VMS presents when posting | camera ingest stops |
| `docker/redis/users.acl` | redis | redis ACL — SHA-256 hashes, not plaintext | redis crash-loops, taking the app tier with it |
| `docker/.env` | **compose only** | 8 DB / Redis / Grafana passwords | **the data is unreachable** |

All are `0440 root:1000` except `docker/.env` (`0600 root:root`) and `users.acl`
(`0640 999:1000` — uid 999 is redis inside `redis:7-alpine`).

`secrets/` itself is `0750 root:1000`, so you can read a secret without sudo:

```bash
cat secrets/bootstrap_admin_password
```

> **Back these up before anything else.** They are not regenerable: a new
> `jwt_secret` invalidates every session, and new DB passwords do not match the
> roles already created inside postgres.

### JWT, specifically

```
secrets/jwt_secret  ->  JWT_SECRET_KEY_FILE=/run/secrets/jwt_secret  ->  config.py
```

The application never receives the secret through an environment variable — only
the file path — so it cannot leak into `docker inspect`, a crash dump, or a log
line. On login the API sets an `HttpOnly; Secure; SameSite=strict` cookie named
`__Host-access_token`. The `__Host-` prefix is enforced by the browser: the
cookie is rejected unless it is Secure, path `/`, and has no `Domain` attribute.

The bootstrap admin password is **single-use**: `BOOTSTRAP_ADMIN_REQUIRE_ROTATION`
forces a change on first login, and the file stops working the moment you set a
real password. There is no self-service reset and **no account lockout** — only a
sliding throttle (8 failures per account, 30 per IP, 15-minute windows).

---

## 5. TLS — an internal CA, not a public one

`stage_tls` issues two things:

| File | What to do with it |
|---|---|
| `certs/internal-ca.crt` | **distribute to every client machine** that will open the UI |
| `certs/internal-ca.key` | **move offline once issued** — it can mint a certificate for *any* hostname your clients trust |
| `certs/server.crt` / `.key` | served by nginx; the key stays `0600 root:root` |

The certificate covers `face-detector.internal`, `localhost` and `127.0.0.1`.

**The hostname must resolve, or login fails.** The API enforces an Origin check
against `PUBLIC_ORIGIN`, so a browser has to arrive at that exact name — reaching
it as `localhost` is refused. On a fresh host:

```bash
echo '127.0.0.1 face-detector.internal' | sudo tee -a /etc/hosts
```

`./deploy.sh doctor` section 6 checks this. `curl -k https://localhost` hides the
problem completely, which is why it is checked separately.

---

## 6. Database credentials — four roles, least privilege

`db/roles.sql` (applied by stage 11) creates four roles, with passwords read
from `docker/.env`. None of them is the superuser.

| Role | Used by | Privilege |
|---|---|---|
| `fr_app` | the API, ml_worker | read/write application tables |
| `fr_migrator` | the migrate job | DDL — the only role that may alter schema |
| `fr_readonly` | **the SQL agent** | SELECT only |
| `fr_backup` | the backup job | read for `pg_dump` |

The SQL agent runs generated SQL as `fr_readonly`. That is the last line of
defence behind the AST validator: even a query that passes validation cannot
write, because the role cannot.

`pg_hba.conf` allows `trust` only on the postgres container's *own* loopback.
Across the docker network it is `scram-sha-256` — an empty password is refused.

> **`fr_backup` needs SELECT on SEQUENCES, not just tables.** `pg_dump` reads
> `last_value` from every sequence. The default privileges originally granted
> sequences only to `fr_app`, so any sequence a later migration created was
> invisible to the backup role and `pg_dump` failed closed — which blocks
> `./deploy.sh upgrade` at the backup stage. Both the point-in-time grant and
> the `ALTER DEFAULT PRIVILEGES ... FOR ROLE fr_migrator` form are in
> `db/roles.sql` now.

---

## 7. Volumes — where the data actually lives

Production stores **nothing** in bind mounts. Twelve named volumes, all prefixed
`face_detector_prod_`:

| Volume | Mounted at | Holds |
|---|---|---|
| `postgres_data` | `/var/lib/postgresql/data` | the database |
| `redis_data` | `/data` | sessions, queues |
| `storage_data` | `/app/storage` | face crops and artifacts |
| `logs_data` | `/var/log/face-recognition` | rotating logs — `/api/logs` reads these back |
| `face_database_data` | `/app/database` | local index files (incl. the SQL agent's vector store) |
| `ml_artifacts_data` | `/app/models/ml` | model registry artifacts referenced by hash from the DB |
| `chromadb_cache` | `~/.cache/chroma` | ONNX embedding models (**a cache, not the store**) |
| `hf_cache_data` | `~/.cache/huggingface` | HuggingFace downloads |
| `ollama_models` | `/root/.ollama` | the chat + SQL models (~5 GB) |
| `backup_data` | `/backups` | pg_dump output |
| `prometheus_data`, `grafana_data` | | metrics and dashboards |

Read-only by design: `weights/`, `map-data/`, `certs/`, `secrets/`, and
`/backups` as seen by the API. A container that could write these could rewrite
the model it is verified against, or its own secret.

> **The project name is load-bearing.** Both compose files declare
> `name:`. Without it Compose derives the project from the parent directory —
> `docker` — for *both* stacks, and starting production would mount the
> development database.

---

## 8. First login

```
https://face-detector.internal
username: admin
password: cat secrets/bootstrap_admin_password     # single use, see §4
```

The browser will warn about the certificate until `certs/internal-ca.crt` is
trusted on that machine. The certificate is correct — it is signed by your own
CA, which the browser has not been told to trust yet.

---

## 9. Verifying a deployment

```bash
sudo ./deploy.sh doctor      # read-only; every failure prints its own fix
sudo ./deploy.sh health      # 31-check acceptance battery, no rebuild
```

`doctor` checks, in dependency order:

0. **which daemon** you are talking to — an empty `docker ps` from Docker
   Desktop's engine is not the same as "nothing is running"
1. `docker/.env` — every referenced variable set, no empty values, no dangling
2. paths and secrets vs the manifest
3. container health
4. `https://localhost/health/live`
5. supplied assets — map archives, **content verdicts**, ollama models
6. browser access — `PUBLIC_ORIGIN` resolves and serves

Schema state, separately:

```bash
sudo docker logs face_detector_prod-migrate-1 | tail -5
# -> "Database is up to date (no migrations needed)"
```

---

## 10. Day-to-day

```bash
sudo ./deploy.sh start                  # start / restart the stack
sudo ./deploy.sh stop
sudo ./deploy.sh logs face_recognition
sudo ./deploy.sh backup                 # pg_dump into backup_data
sudo ./deploy.sh restore <file>
sudo ./deploy.sh upgrade                # pins the new alembic head in docker/.env
```

Restart policy: every long-running service is `unless-stopped`, and Docker is
enabled at boot, so the stack returns after a power cut. `migrate` is
deliberately `restart: "no"` — it is a one-shot job, and restarting it would
re-run migrations on every daemon start.

---

## 11. Failure playbook — the ones that actually happened here

A short index of causes, not a substitute for the decision tree in
[`73_TROUBLESHOOTING.md`](73_TROUBLESHOOTING.md).

| Symptom | Real cause |
|---|---|
| "dependency failed to start", site down | a healthcheck failing one tier below; check redis first |
| `docker ps` empty while the site serves traffic | the CLI is pointed at Docker Desktop's engine — `docker context use default` |
| "GPU unavailable" at startup on healthy hardware | not the GPU: a smoke-test tensor with a dynamic spatial dim collapsing to 1×1 |
| `upgrade` fails at **backup**, `permission denied for sequence` | `fr_backup` lacks SELECT on a migration-created sequence (§6) |
| image build dies at step 2, `ln: File exists` | `Dockerfile.gpu` needs `ln -sf`; the CUDA base already ships `/usr/bin/python3` |
| acceptance says "TLS handshake failed" while HTTPS serves 200 | the check ran `openssl` inside nginx — `nginx:alpine` has no openssl binary |
| migrate exits 78 | the schema head does not match `MIGRATIONS_EXPECTED_HEAD`; the real reason is several screens up in its log |
| a mounted secret is ignored | check the field name — the app reads `REDIS_URL_FILE`, not `REDIS_PASSWORD_FILE` |
| all four basemaps UNAVAILABLE, no error | `map-data/metadata/content_verdicts.json` missing; the gate fails closed (§12) |
| login refused in a browser but `curl -k` works | `PUBLIC_ORIGIN` does not resolve, or you browsed to `localhost` |

> **Never run `alembic revision --autogenerate` and apply it blind.** It cannot
> see partial-unique indexes or pgvector HNSW indexes, so it proposes dropping
> `idx_embedding_vector_hnsw` and `idx_user_query_embeddings_hnsw` — your vector
> search — along with constraints such as `uq_watchlists_name_live`.

---

## 12. Map data — present is not the same as usable

The map gate **fails closed**: an archive nobody has measured is reported
UNAVAILABLE. Copying `.mbtiles` files into `map-data/production/` is therefore
not enough — they also need a recorded verdict, which
`scripts/map_data/install_dataset.py` normally writes at install time.

If the data was copied in by hand, measure it once. Production mounts
`map-data` read-only, so the write happens in a throwaway container:

```bash
sudo docker run --rm --network none \
  -v "$PWD/map-data":/app/map-data:rw \
  --entrypoint python face_detector_prod-face_recognition:latest -c \
  "import sys;sys.path.insert(0,'/app');from backend.core import map_content_ledger as l;l.verify_installed(verifier='operator')"
sudo chown 1000:1000 map-data/metadata/content_verdicts.json   # else uid 1000 cannot read it
```

Then confirm with the project's own measurement tool:

```bash
sudo docker exec face_detector_prod-face_recognition-1 \
  python3 /app/scripts/map_data/production_gate.py
# -> PRODUCTION READY: all 13 rules pass
```

---

## 13. Syncing code from the development host

App code is developed on a separate (Windows) machine and copied onto this
host. **The copy is a full-tree replace**: it brings the new application code
but overwrites or deletes deployment work that exists only here, and strips
execute bits. Nothing is committed to git, so git cannot restore it.

After any copy, before deploying:

```bash
chmod +x deploy.sh docker-entrypoint.sh docker-pusher.sh
find scripts -name '*.sh' -exec chmod +x {} \;
sudo ./deploy.sh doctor
```

Re-check these specifically — all have been reverted by a copy before:
`backend/core/gpu_runtime.py` (smoke-test shape), the compose redis healthcheck
and migrate environment, `db/roles.sql` quoting and sequence grants,
`docker/Dockerfile.gpu` (`ln -sf`), and `scripts/deploy/stage-health.sh`.

The **running containers are the ground truth** for the last-good compose
values — `docker inspect` them rather than guessing.

---

## 14. What to back up

Procedures and the restore drill:
[`60_BACKUP_AND_RESTORE.md`](60_BACKUP_AND_RESTORE.md).

In priority order:

1. `secrets/` and `docker/.env` — **not regenerable**; without them the data is unreachable
2. `certs/internal-ca.*` — reissuing means re-trusting the CA on every client
3. `postgres_data` — use `./deploy.sh backup`, not a volume copy
4. `storage_data` — face crops; large, and referenced by the database
5. `ml_artifacts_data` — the DB references these files by hash

`weights/`, `map-data/` and `ollama_models` are re-downloadable and need no
backup, but keep a copy if the site is to stay offline.
