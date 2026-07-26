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

---

## 2. Generate secrets

```bash
bash scripts/setup/generate-secrets.sh
```

Writes `secrets/jwt_secret` and `secrets/bootstrap_admin_password` (mode 600),
generates `docker/redis/users.acl` with SHA-256 password hashes, and prints the
`.env` lines to add. Both directories are gitignored.

Copy the printed block into `.env`, and add:

```bash
ENVIRONMENT=production
PUBLIC_ORIGIN=https://face-detector.internal
GRAFANA_ADMIN_PASSWORD=<generate one>
```

Existing files are never overwritten. To rotate, delete the file and re-run.
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

GPU deployments use `docker/docker-compose.gpu.yml` instead.

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
docker compose -f docker/docker-compose.gpu.yml exec -T face_recognition python -c "
import onnxruntime as ort
print(ort.__version__, ort.get_available_providers())
assert 'CUDAExecutionProvider' in ort.get_available_providers()"

docker compose -f docker/docker-compose.gpu.yml exec -T face_recognition nvidia-smi
```

Then put real inference load through the service and confirm `nvidia-smi` shows
non-zero utilisation and memory. Startup already runs a smoke inference and
aborts if CUDA is not actually in use, but sustained load is what reveals OOM
and thermal limits.

```bash
docker compose -f docker/docker-compose.gpu.yml logs face_recognition | grep -i "running on"
# expected: SCRFD: running on CUDA / ArcFace: running on CUDA
```

### 6.7 Regression suite

```bash
docker compose -f docker/docker-compose.prod.yml exec -T face_recognition \
  pip install -r /app/requirements-dev.txt
docker compose -f docker/docker-compose.prod.yml exec -T face_recognition \
  python -m pytest tests/ -q
```

Note the integration tests authenticate as `admin`/`admin123`, which exists only
in the development stack. Against production they will fail on login — run the
suite on the development stack, and use the checks above for production.

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
