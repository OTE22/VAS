# DEPLOYMENT — Plain-English Guide

How this system is deployed, where every value comes from, and how the
certificates, IP addresses and names actually work.

Written to be read start to finish once. For exact commands see
[`Docs/61_DEPLOYMENT_RUNBOOK.md`](Docs/61_DEPLOYMENT_RUNBOOK.md) (the production
authority) and [`Docs/93_PRODUCTION_RUNBOOK.md`](Docs/93_PRODUCTION_RUNBOOK.md)
(the orientation map).

---

## 1. What was done

The stack runs **10 long-running containers plus one job that runs and exits**,
on one Ubuntu host with an RTX 5090.

```
Browser ──HTTPS:443──> nginx ──> face_recognition (API + GPU face pipeline)
                         │            │
                         │            ├── postgres   schema, identities, audit
                         │            ├── redis      sessions, rate limits
                         │            └── ollama     chat + SQL models (offline)
                         └──────> martin             offline map tiles

  ml_worker   ML jobs (training, drift)      migrate   applies schema, exits
  backup      pg_dump on a timer             prometheus + grafana   metrics
```

Work completed:

- **Deployed and verified** — `deploy.sh upgrade` passes all 14 stages;
  the acceptance battery passes **31/31 mandatory checks**.
- **Database schema** at Alembic head `fbb2c3d4e5f6`, applied by the one-shot
  `migrate` job. Zero structural drift between the models and the live schema.
- **GPU confirmed real** — both models report `running on CUDA` and pass an
  inference smoke test; metrics show `cuda_available=1 cpu_fallback_active=0`.
  Fixed a startup crash where a dynamic tensor dimension collapsed to 1×1 and
  was misreported as "GPU unavailable" on perfectly healthy hardware.
- **Offline maps working** — all four basemaps verified; the map gate reports
  `PRODUCTION READY: all 13 rules pass`.
- **Least-privilege database roles** — four roles, none of them superuser.
- **Secrets and certificates issued**, with an ownership manifest so a
  permission drift cannot silently break the stack.
- **Diagnostics added** — `./deploy.sh doctor` (read-only, prints the fix for
  whatever it finds) and `./deploy.sh paths` (ownership drift).
- **Guard tests added** — 25 cases covering variables, volumes and restart
  policy. Full deployment suite: **225 passing**.

---

## 2. Variables — where every value comes from

There are **two** template files, and they are not interchangeable.

| File | Who reads it | Contains |
|---|---|---|
| `docker/.env` | **Compose only** — never mounted into a container | 10 deployment values |
| `.env.example` | a template for the *application* settings | documents ~120 of the 325 settings in `config.py` |

> Compose reads `.env` from the **`docker/` directory**, not the repo root.
> A value put in a root `.env` is silently ignored by Compose.

### The 10 values in `docker/.env`, and what each becomes

Every one is generated once by `scripts/setup/generate-secrets.sh` and **never
overwritten** on a re-run.

| Variable | Becomes, inside the containers |
|---|---|
| `PUBLIC_ORIGIN` | `AUTH_ALLOWED_ORIGINS` + `CORS_ORIGINS` (4 places) |
| `POSTGRES_SUPERUSER_PASSWORD` | `POSTGRES_PASSWORD` — postgres first-boot only |
| `FR_APP_PASSWORD` | `DATABASE_URL` for the API and `ml_worker` |
| `FR_MIGRATOR_PASSWORD` | `DATABASE_URL` for the `migrate` job |
| `FR_READONLY_PASSWORD` | `SQL_AGENT_DB_PASSWORD` — the SQL agent |
| `FR_BACKUP_PASSWORD` | `PGPASSWORD` for the backup job |
| `REDIS_PASSWORD` | `REDIS_URL`, `REDISCLI_AUTH`, and the health check |
| `REDIS_MONITOR_PASSWORD` | **not used by Compose** — `generate-secrets.sh` hashes it into `docker/redis/users.acl` |
| `GRAFANA_ADMIN_PASSWORD` | `GF_SECURITY_ADMIN_PASSWORD` |
| `MIGRATIONS_EXPECTED_HEAD` | the schema version the app refuses to start without |

**Audited: all 10 are used.** Nothing is set and forgotten. A test
(`tests/test_env_example_contract.py`) now checks this in both directions,
because a variable set but referenced nowhere is how a rename hides.

### Secrets are passed as file *paths*, never values

Three credentials are too sensitive for an environment variable:

```
secrets/jwt_secret                → JWT_SECRET_KEY_FILE=/run/secrets/jwt_secret
secrets/bootstrap_admin_password  → BOOTSTRAP_ADMIN_PASSWORD_FILE=...
secrets/webhook_api_keys          → WEBHOOK_API_KEYS_FILE=...
```

The app receives a **filename** and opens it itself. That is why no credential
appears in `docker inspect`, a crash dump, or a log line.

Files are `0440 root:1000` — readable by the service, writable by nobody. The
`secrets/` directory is `0750 root:1000`, so you can read one without sudo:

```bash
cat secrets/bootstrap_admin_password
```

### Order of resolution

```
1. environment: in docker-compose.prod.yml     ← highest priority, always wins
2. docker/.env                                 ← only for ${VAR} substitution
3. the default declared in config.py           ← lowest
```

`.env.example` deliberately holds **development** values (`ENVIRONMENT=development`,
`AUTH_COOKIE_SECURE=false`). That is correct for the file — production compose
overrides every one of them, and a test now proves it still does.

---

## 3. Certificates — how HTTPS works here

This is a **private certificate authority**, not a public one like Let's Encrypt.
No internet is involved, which is what lets the system run fully offline.

### What exists

```
certs/internal-ca.crt   the CA certificate  — give this to every client machine
certs/internal-ca.key   the CA private key  — MOVE THIS OFFLINE
certs/server.crt        the server certificate nginx presents
certs/server.key        its private key (0600 root, never leaves the host)
```

Current validity:

| | Subject | Valid until |
|---|---|---|
| CA | `CN=Face Detector Internal CA` | **2036-08-29** (10 years) |
| Server | `CN=face-detector.internal` | **2028-12-04** |

The server certificate is valid for these names only:

```
DNS:face-detector.internal    DNS:localhost    IP:127.0.0.1
```

### How they reach nginx

`certs/` is bind-mounted **read-only** into the nginx container:

```
/home/itdirect-ai/Desktop/VAS/certs  →  /etc/nginx/certs  (ro)
```

and `nginx.prod.conf` points at them:

```nginx
ssl_certificate     /etc/nginx/certs/server.crt;
ssl_certificate_key /etc/nginx/certs/server.key;
```

Read-only matters: a container that could rewrite its own certificate could
present any identity it liked.

### Using them on a client machine

Your browser warns because it has never been told to trust *your* CA. The
certificate is correct — the trust is missing. Install the CA once per machine:

**Ubuntu / Debian**
```bash
sudo cp internal-ca.crt /usr/local/share/ca-certificates/face-detector-ca.crt
sudo update-ca-certificates
```

**Windows** (as Administrator)
```powershell
Import-Certificate -FilePath internal-ca.crt -CertStoreLocation Cert:\LocalMachine\Root
```

**Firefox** keeps its own store: Settings → Privacy & Security → Certificates →
View Certificates → Authorities → Import → tick *"Trust this CA to identify
websites"*.

Copy **`internal-ca.crt`** only. Never copy `internal-ca.key` — whoever holds it
can mint a certificate for *any* hostname your clients now trust.

> **Do this now:** move `certs/internal-ca.key` to offline storage. You only
> need it to issue a new server certificate, which is rare.

### Renewing before 2028

```bash
rm certs/server.crt certs/server.key      # keep the CA files
sudo bash scripts/tls/make-internal-ca.sh
sudo ./deploy.sh start
```

Clients keep working — they trust the CA, not the individual certificate.

---

## 4. IP addresses — who assigns them

**Docker assigns every container IP automatically.** Nothing is hand-configured
and nothing needs to be.

The stack uses five isolated private networks so that services can only reach
what they legitimately need:

| Network | Subnet | Who is on it |
|---|---|---|
| `edge` | `172.20.0.0/16` | nginx, face_recognition, martin |
| `data` | `172.21.0.0/16` | postgres, redis, backup, ml_worker, face_recognition |
| `monitoring` | `172.22.0.0/16` | prometheus, grafana, face_recognition |
| `ai` | `172.23.0.0/16` | ollama, face_recognition |
| `webhook_integration` | `172.19.0.0/16` | nginx (where cameras post) |

Current assignment (Docker picks these; they change on recreate):

```
nginx             edge 172.20.0.4      webhook_integration 172.19.0.2
face_recognition  edge 172.20.0.3      data 172.21.0.6   ai 172.23.0.3   monitoring 172.22.0.4
postgres          data 172.21.0.3      redis  data 172.21.0.2
ml_worker         data 172.21.0.5      backup data 172.21.0.4
ollama            ai   172.23.0.2      martin edge 172.20.0.2
```

Read that table as the security model: **postgres is on `data` only**. It has no
route to the edge network, so nothing outside can reach the database even if
nginx were compromised.

### The only ports reachable from outside

```
0.0.0.0:80  ->  nginx
0.0.0.0:443 ->  nginx
```

That is the whole external surface. Postgres (5432), Redis (6379) and Ollama
(11434) publish **nothing** — they are reachable only from inside their network.
Grafana is bound to loopback only.

### The host's own address

```
wlp130s0f0   192.168.1.111/24      ← the LAN address other machines would use
```

> **Note the gap:** the certificate does *not* include `192.168.1.111`, so
> another machine browsing to `https://192.168.1.111` gets a name-mismatch
> warning even after trusting the CA. Use the **name**, not the IP (§5).
> If you need IP access, reissue the certificate with that IP in its SAN list.

---

## 5. DNS — how names are resolved

Two completely separate mechanisms. This is the part people usually trip on.

### Inside the containers — automatic

Docker runs an embedded DNS server at `127.0.0.11` in every container. It
resolves **service names from the compose file** to current container IPs:

```
postgres  →  172.21.0.3
redis     →  172.21.0.2
ollama    →  172.23.0.2
martin    →  172.20.0.2
```

This is why `DATABASE_URL` says `@postgres:5432` and not an IP address. IPs
change when containers are recreated; the name never does. **Nothing to
configure.**

### On your machine — one line in `/etc/hosts`

There is no DNS server for `face-detector.internal`. It resolves because of a
single line on this host:

```
127.0.0.1    face-detector.internal
```

`getent hosts face-detector.internal` → `127.0.0.1`

**Why the name is required, not optional.** The API checks the browser's
`Origin` header against `PUBLIC_ORIGIN`. Arriving as `https://localhost` fails
that check and **login is refused** — even though the page loads. `curl -k`
hides this completely, which is why `./deploy.sh doctor` checks it separately
(section 6).

### To reach it from another computer

On each client machine, point the name at this host's LAN IP:

- **Linux / macOS** — add to `/etc/hosts`:
  ```
  192.168.1.111    face-detector.internal
  ```
- **Windows** — same line in `C:\Windows\System32\drivers\etc\hosts`
  (edit as Administrator).
- **Whole network** — add one `A` record on your router or internal DNS server:
  `face-detector.internal → 192.168.1.111`. Then no client edits are needed.

Then trust the CA (§3) and browse to **https://face-detector.internal**.

---

## 6. Logging in the first time

```
https://face-detector.internal
username: admin
password: cat secrets/bootstrap_admin_password
```

That password is **single-use**. `BOOTSTRAP_ADMIN_REQUIRE_ROTATION=true` forces
a change on first login, and the file stops working once you set a real one.

There is **no account lockout** and no self-service reset — only a sliding
throttle (8 failed attempts per account, 30 per IP, over 15 minutes).

---

## 7. Everyday commands

```bash
sudo ./deploy.sh doctor     # read-only: what is wrong, and the exact fix
sudo ./deploy.sh health     # 31-check acceptance battery, no rebuild
sudo ./deploy.sh paths      # file ownership drift
sudo ./deploy.sh start      # bring the stack up
sudo ./deploy.sh stop
sudo ./deploy.sh backup     # pg_dump into the backup volume
sudo ./deploy.sh upgrade    # backup → build → pin schema → migrate → restart
```

`upgrade` is the safe path for new code: it takes a verified backup first, tags
a rollback point, and rolls back automatically if the new version fails.

Every long-running service is `restart: unless-stopped` and Docker starts at
boot, so **the stack returns by itself after a power cut**. `migrate` is
deliberately `restart: "no"` — it is a one-shot job.

---

## 8. If something breaks

| Symptom | Where to look |
|---|---|
| site down, "dependency failed to start" | a health check one tier below — check redis first |
| login refused in a browser, `curl -k` works | the name does not resolve, or you used `localhost` (§5) |
| certificate warning | the CA is not trusted on that machine (§3) |
| all four basemaps missing | `map-data/metadata/content_verdicts.json` absent; the gate fails closed |
| a mounted secret ignored | check the field name — it is `REDIS_URL_FILE`, not `REDIS_PASSWORD_FILE` |

Start with `sudo ./deploy.sh doctor`. It is read-only, orders findings by
dependency, and prints the command that fixes each one. **Read the first
problem, not the last** — later ones are usually its consequence.

Deeper trees: [`Docs/73_TROUBLESHOOTING.md`](Docs/73_TROUBLESHOOTING.md).

---

## 9. Back up these, in this order

1. **`secrets/` and `docker/.env`** — not regenerable. A new `jwt_secret` logs
   everyone out; new DB passwords no longer match the roles inside postgres.
2. **`certs/internal-ca.*`** — reissuing means re-trusting the CA on every client.
3. **`postgres_data`** — via `./deploy.sh backup`, not a volume copy.
4. **`storage_data`** — face crops, referenced by the database.
5. **`ml_artifacts_data`** — the database references these files by hash.

`weights/`, `map-data/` and the Ollama models are re-downloadable — but keep a
copy if this site must stay offline.
