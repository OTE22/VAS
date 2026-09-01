# Production Security Checklist

Work through this before go-live, and again after any configuration change.

**The good news first:** most of this list is enforced by code, not by your
discipline. A production deployment **refuses to start** if the important
things are wrong — exit code `78`, with a report naming the setting and the
fix. The startup preflight lives in `backend/security/config_guard.py`.

This document tells you three things for each item: what the system already
enforces, what you must still do yourself, and how to verify it.

Sections marked **⚠️ GAP** are real weaknesses found by auditing the current
code. They are listed honestly rather than omitted.

---

## 0. The one-minute verification

Run this on the production host after `up -d`. If the stack is running at all,
the preflight already passed, which covers most of §1–§6.

```bash
export DC="docker compose -f docker/docker-compose.prod.yml"

$DC ps                                          # all healthy, and see §7 for ports
$DC logs face_recognition | grep -i "preflight" # "✅ Configuration preflight passed"
curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost/health/live   # 200
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost/docs           # 404 = correct
```

`/docs` returning **404 in production is correct** — see §9.

---

## 1. Default credentials

**Enforced.** There is no hardcoded admin password anywhere in the current
code. The bootstrap admin is created only when no active admin exists, from
`BOOTSTRAP_ADMIN_USERNAME` (default `admin`) and `BOOTSTRAP_ADMIN_PASSWORD`
(default **empty**). In production, an unsupplied or weak password aborts
startup.

The weak-password rule rejects a password that is empty, is a known default,
is under 12 characters, or has fewer than 6 distinct characters. The rejected
list includes `admin`, `admin123`, `password`, `changeme`, `letmein`, `root`,
`12345678`, `qwerty`, `secret`, `test123` and similar.

> `admin123` **is** the seeded development password
> (`docker-compose.cpu.yml`). It is rejected twice over in production — once as
> a known default, once for being 8 characters. Any documentation showing
> `admin`/`admin123` is describing the development stack only.

**You must:**

- [ ] Generate a real password and supply it as a **file**, not an env var:
      `secrets/bootstrap_admin_password` (see §2).
- [ ] Log in once and change it. You cannot forget this one: the account
      authenticates but every gated endpoint answers
      `403 PASSWORD_ROTATION_REQUIRED` until the password is changed at
      `/change-password`. `docker/docker-compose.prod.yml` hardcodes
      `BOOTSTRAP_ADMIN_REQUIRE_ROTATION: "true"` (it is not read from `.env`,
      and there is no config-guard rule for it — the compose file is what
      pins it).
- [ ] Confirm no other account still has a shared or handover password. Any
      account an admin created or reset carries `must_change_password` until
      its owner replaces the credential — check the **MUST CHANGE PASSWORD**
      badge in Admin → Users, or:
      `SELECT username FROM users WHERE must_change_password;`
      An admin resetting their *own* password is exempt by design.

**Verify**

```bash
$DC logs face_recognition | grep -i "bootstrap"
```

---

## 2. Secrets

**Enforced.** These abort a production start: a missing, placeholder,
short (< 48 chars) or low-entropy `JWT_SECRET_KEY`; missing `WEBHOOK_API_KEYS`;
empty `AUTH_ALLOWED_ORIGINS`; a default `POSTGRES_PASSWORD`; a `DATABASE_URL`
carrying a default password or connecting as a superuser role.

**Generate everything with the script — never by hand, never reused:**

```bash
bash scripts/setup/generate-secrets.sh
```

It writes `chmod 600` files and **never overwrites an existing one**:

| File | Contents |
|---|---|
| `secrets/jwt_secret` | 48 bytes base64 |
| `secrets/bootstrap_admin_password` | 24 bytes base64 |
| `secrets/webhook_api_keys` | 48 bytes base64 |
| `docker/redis/users.acl` | rendered from the example, SHA-256 password hashes |

It prints the database and Redis passwords to stdout for you to paste into
`.env`; it does not write `.env` itself.

For anything else: `openssl rand -hex 32`.

**These three are mounted as Docker secrets** (files under `/run/secrets/`, not
visible in `docker inspect`): `jwt_secret`, `bootstrap_admin_password`,
`webhook_api_keys`, consumed via `JWT_SECRET_KEY_FILE`,
`BOOTSTRAP_ADMIN_PASSWORD_FILE`, `WEBHOOK_API_KEYS_FILE`.

**⚠️ GAP — database and Redis passwords are plain environment variables.**
`POSTGRES_SUPERUSER_PASSWORD`, `REDIS_PASSWORD`, `FR_APP_PASSWORD`,
`FR_MIGRATOR_PASSWORD`, `FR_READONLY_PASSWORD`, `FR_BACKUP_PASSWORD` and
`GRAFANA_ADMIN_PASSWORD` are interpolated from `.env` into the container
environment, so they are readable via `docker inspect` and `/proc/<pid>/environ`
by anyone with Docker access. Compose refuses to render without them, so they
cannot be forgotten — but they are not file-based secrets.
*Mitigation:* restrict Docker daemon access to the administrator (§14) and keep
`.env` at mode `600`. Converting these to Docker secrets is the proper fix.

**You must:**

- [ ] Run `generate-secrets.sh` and confirm **all three** secret files exist.
      Only `secrets/webhook_api_keys` is present in a fresh checkout; a missing
      `jwt_secret` or `bootstrap_admin_password` makes `up` fail.
- [ ] `chmod 600 .env` and confirm it is not tracked by git.
- [ ] Store `.env` and `secrets/` in your password manager or an encrypted
      backup — losing `jwt_secret` logs everyone out; losing the DB password
      locks you out of your own data.
- [ ] Never paste a real secret into documentation, a ticket, or a chat.

**Verify**

```bash
ls -l secrets/ .env
git check-ignore -v secrets certs .env      # all three must be ignored
git ls-files secrets certs                  # must print nothing
```

---

## 3. TLS

**Configured, in `nginx.prod.conf` only.** TLS 1.2/1.3 with AEAD ciphers,
HTTP/2, session tickets off, and a **308 redirect from port 80 to HTTPS** (308
so redirected POSTs keep their method and body). `server_tokens off`.

> The development `nginx.conf` is **HTTP only** — no TLS, no redirect. Do not
> run it on anything reachable.

**⚠️ GAP — HSTS is not sent.** `Strict-Transport-Security` is commented out in
`nginx.prod.conf:179`. The rationale is in the file: enabling HSTS before every
client trusts `certs/internal-ca.crt` locks users out with no browser override,
which on an internal CA is a self-inflicted outage.

**You must:**

- [ ] Install `certs/internal-ca.crt` on every machine that will use the system.
- [ ] **Only then**, uncomment the HSTS header, starting at a low `max-age`
      (86400) and raising it once you are confident.
- [ ] Note the certificate expiry date somewhere you will actually see it.

**Verify**

```bash
curl -sSI https://<your-host>/ | grep -iE 'strict-transport|x-frame|x-content-type|referrer'
echo | openssl s_client -connect <your-host>:443 2>/dev/null | openssl x509 -noout -dates
```

---

## 4. Port exposure

**Enforced by the production compose file.** Only nginx publishes to the
network:

| Service | Production | Development (`cpu.yml`) |
|---|---|---|
| nginx | `80`, `443` on 0.0.0.0 | `80`, `443` on 0.0.0.0 |
| **PostgreSQL** | **not published** (`expose` only) | **`5432` on 0.0.0.0** |
| **Redis** | **not published** | **`6379` on 0.0.0.0** |
| API | not published | not published |
| Prometheus | not published | `127.0.0.1:9090` |
| Grafana | `127.0.0.1:3000` (loopback) | `127.0.0.1:3000` |
| Ollama | not published | `11434` on 0.0.0.0 |

Production additionally isolates networks: the `data` and `monitoring` networks
are `internal: true`, and nginx is attached to neither — it has no route to
PostgreSQL or Redis at all.

> **The development stack is the danger.** It publishes PostgreSQL on
> `0.0.0.0:5432` with password `admin`, and an unauthenticated Redis on
> `0.0.0.0:6379`. Never run `docker-compose.cpu.yml` or `.gpu.yml` on a machine
> reachable from an untrusted network.

**You must:**

- [ ] Confirm you are running a `prod` compose file. For GPU, that is
      `prod.yml` **plus** `prod.gpu.yml` — layered. Using `gpu.yml` alone
      silently drops backups, monitoring and network isolation.
- [ ] Put a host firewall in front regardless: allow 80/443, deny everything
      else inbound.
- [ ] Reach Grafana over an SSH tunnel, not by opening the port:
      `ssh -L 3000:127.0.0.1:3000 you@server`

**Verify**

```bash
$DC ps                                  # PORTS column — expect only nginx and 127.0.0.1:3000
sudo ss -tlnp | grep -E '5432|6379|8000|9090'   # should show nothing on 0.0.0.0
sudo ufw status                         # or your firewall of choice
```

---

## 5. Redis

**Enforced.** Production Redis uses an **ACL file**, not `--requirepass`,
deliberately: a `--requirepass` value shows up in `docker inspect` and the host
process list, whereas ACL passwords are stored as SHA-256 hashes.
`user default off` denies unauthenticated access outright; the app connects as
`fr_app` with `-@admin -@dangerous -flushall -flushdb -shutdown -config` removed.
An unauthenticated non-loopback `REDIS_URL` is a fatal startup error.

Persistence is on (`appendonly yes`) because Redis holds the **JWT revocation
denylist** — losing it un-revokes logged-out tokens.

**You must:**

- [ ] Confirm `docker/redis/users.acl` exists (generated in §2) and is not the
      `.example`.
- [ ] Understand the degradation: with Redis down, logout revocation and login
      throttling fall back to per-process state. This is why `WORKERS=1` is
      load-bearing — do not raise it without Redis.

**Verify**

```bash
$DC exec redis redis-cli PING                       # (error) NOAUTH ... = correct
$DC exec redis redis-cli -u "redis://fr_app:$REDIS_PASSWORD@localhost:6379" PING   # PONG
```

---

## 6. PostgreSQL

**Enforced.** The application connects as a **least-privilege role**, not a
superuser: `fr_app` for the app, `fr_migrator` for migrations, `fr_backup` for
backups, and `fr_readonly` for LLM-generated SQL. A `DATABASE_URL` whose
username is `postgres` or `root` is a fatal startup error, as is a default
password.

> The `config.py` defaults are `postgres`/`admin` and the development stack uses
> them. Those defaults exist for local work and are rejected in production.

**You must:**

- [ ] Confirm `DATABASE_URL` uses `fr_app`, and that each role has its own
      distinct password.
- [ ] Confirm the SQL agent uses `fr_readonly` — sharing the app role would let
      generated SQL write.

**Verify**

```bash
$DC exec postgres psql -U postgres -d face_recognition -c "\du"
$DC exec face_recognition sh -c 'echo "$DATABASE_URL" | sed "s#://[^@]*@#://***@#"'
```

---

## 7. Authentication and sessions

**Enforced in production:** `AUTH_COOKIE_SECURE=true`,
`AUTH_COOKIE_SAMESITE=strict`, `AUTH_COOKIE_HOST_PREFIX=true` (so the cookie is
named `__Host-access_token`), `AUTH_SAME_HOST_ORIGIN_TRUSTED=false`, and a
non-empty, non-wildcard, HTTPS `AUTH_ALLOWED_ORIGINS`. **`HttpOnly` is
hardcoded and cannot be turned off by configuration.**

Login responses carry `Cache-Control: no-store`. Unknown-user and wrong-password
paths run a dummy bcrypt verify and return an identical message, so neither
timing nor wording reveals whether an account exists.

**Rate limiting exists at two layers:** nginx caps
`/api/auth/(login|logout|change-password)` at **10 r/m** (burst 5, 429), and the
application throttles **8 account failures / 900 s**, **30 IP failures / 900 s**,
and a **600 attempts / 60 s** global surge cap. `change-password` sits behind
both — it calls `check_rate_limits` on entry and `record_failure` on a wrong
current password — so it is not a way to guess a password without the throttle
that guards login.

**⚠️ Not a gap, but know it: there is no persistent account lockout.** No
`locked_until`, no failed-attempt column. Counters expire on their own, and a
successful login clears them — deliberately, so an attacker cannot lock a real
user out by burning their counter.

Token lifetime is `ACCESS_TOKEN_EXPIRE_MINUTES`, default **1440 (24 h)**. There
is **no refresh token**; renewal means logging in again.

**A password nobody else chose is a precondition for using the system.** An
account carrying a seeded or admin-assigned password can authenticate but
nothing more: `must_change_password` is checked in the single dependency every
gated route already builds on, so the refusal reaches all of them at once
rather than route by route. The four exemptions are `GET /api/auth/me`,
`POST /api/auth/logout`, `POST /api/auth/change-password` and
`GET /change-password`. Enforcing it server-side is the point — a client that
ignores the redirect gains nothing.

**A password change ends every other session for that account.** Each password
write stamps `users.password_changed_at`, and any token whose `iat` predates it
is refused 401. This is a **second revocation channel alongside the Redis jti
denylist**, and unlike that denylist it is database-side: it keeps working
while Redis is down, which matters because a password change is often the
reaction to a suspected compromise.

**You must:**

- [ ] Set `AUTH_ALLOWED_ORIGINS` to the exact browser origin, e.g.
      `https://face-detector.internal`. Getting this wrong shows as
      `CSRF_FAILED` (403) on login.
- [ ] Consider lowering `ACCESS_TOKEN_EXPIRE_MINUTES` for a surveillance system;
      24 h is generous. Above 1440 the preflight warns.
- [ ] Review accounts and roles: remove leavers, and confirm nobody has admin
      who does not need it.

**Verify**

```bash
curl -sSI https://<host>/api/auth/login -X POST | grep -i 'set-cookie\|cache-control'
$DC exec face_recognition sh -c "grep '\[AUTH_AUDIT\]' /var/log/face-recognition/app.log | tail -20"
```

---

## 8. Webhook / camera ingest

**Enforced.** Keys are compared **constant-time**, over fixed-length SHA-256
digests, with no early return — neither key length nor match position leaks.
The header is `X-Webhook-Key`; `Authorization: Bearer <key>` is always accepted;
query parameters never are.

In production: **`WEBHOOK_AUTH_MODE=off` is unconditionally fatal** (it cannot
be acknowledged away), `log_only` is fatal unless explicitly acknowledged,
missing keys are fatal, and weak keys, keys reused as the JWT secret or DB
password, and the published development key are each fatal.

**You must:**

- [ ] Give each camera its own key, so one can be revoked without touching the
      others.
- [ ] Confirm `WEBHOOK_AUTH_MODE=enforce`.
- [ ] Keep `WEBHOOK_MAX_BODY_MB` and nginx's webhook `client_max_body_size` in
      sync (both 25 by default).
- [ ] Turn off `WEBHOOK_DEBUG_IMAGES` if it was ever enabled — it writes
      received frames to disk and the preflight warns about it.

**Verify**

```bash
curl -o /dev/null -w '%{http_code}\n' http://localhost/webhook/test                       # 401
curl -o /dev/null -w '%{http_code}\n' -H "X-Webhook-Key: $KEY" http://localhost/webhook/test  # 200
```

---

## 9. API documentation visibility

**Enforced, twice.** The gate is
`ENABLE_API_DOCS and not settings.is_production` — so even setting
`ENABLE_API_DOCS=true` cannot re-enable docs in production. Additionally, a
truthy `ENABLE_API_DOCS` in production is a **fatal startup error**, and
`ENABLE_API_DOCS` is in `SECURITY_CRITICAL_KEYS`, so the admin settings API
cannot flip it at runtime.

This matters because the spec publishes every admin route, including the exact
shape of privileged endpoints.

### The policy, explicitly

**Interactive docs stay disabled in production. This is deliberate, not a
gap** — and the administrator is not left without a reference:

1. **[`75_API_REFERENCE.md`](75_API_REFERENCE.md)** — generated from the
   application's own OpenAPI document, committed to the repository, readable
   offline. A test fails if it drifts from the code.
2. **Extract the full spec from the production system itself** — no server
   route is exposed, nothing is enabled, the double gate stays shut
   (the path count grows as routes are added — verify the extraction works,
   not that it matches a number, while `/docs` remains unregistered):

   ```bash
   docker compose -f docker/docker-compose.prod.yml exec face_recognition \
     python -c "from backend.main import app; import json; print(json.dumps(app.openapi()))" \
     > openapi-prod.json
   ```

   Load `openapi-prod.json` into any Swagger/ReDoc viewer on your own
   workstation if you want interactivity.

**Temporarily enabling `/docs` on a production deployment is not a supported
procedure.** It would require defeating two independent gates (the
`is_production` check in code and the fatal preflight rule), and the
extraction above removes the need. If you find yourself wanting it, use the
command above instead.

**You must:**

- [ ] Confirm `/docs` and `/openapi.json` return **404** in production.
- [ ] Read the API from `75_API_REFERENCE.md` or the extraction command above.

**Verify**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://<host>/docs           # 404
curl -sS -o /dev/null -w '%{http_code}\n' https://<host>/openapi.json   # 404
```

---

## 10. Debug mode and error disclosure

**Enforced.** `DEBUG` defaults to `false`, is pinned `false` in production, is a
**fatal** startup error if truthy in production, and is in
`SECURITY_CRITICAL_KEYS` so it cannot be enabled at runtime. `ENVIRONMENT`
itself defaults to `production` — the fail-safe direction. No full traceback is
ever returned: tracebacks go to the log only.

**⚠️ GAP — exception *messages* do reach authenticated clients.** 47 places in
`backend/routes/*.py` interpolate the caught exception into the HTTP detail,
e.g. `detail=f"Internal server error: {str(e)}"` or `detail=str(e)`. The worst
concentrations are `identities.py` (21), `users.py` (8) and `batch_export.py`
(7). These are not gated by `DEBUG`, so a database or driver error can surface
SQL fragments and internal identifiers to any authenticated user.

*Risk:* moderate — it requires a valid session, and secrets are redacted from
logs but **not** from these strings. *Fix:* replace the interpolation with a
fixed message plus the request's reference id, which is already available. Until
then, treat authenticated access as more sensitive than it looks.

**You must:**

- [ ] Confirm `DEBUG=false` and `ENVIRONMENT=production`.
- [ ] Track the above as a real remediation item, not a note.

---

## 11. CORS and CSP

**Enforced.** The dangerous wildcard-with-credentials combination is
**structurally impossible**: `cors_allow_credentials` is derived as
`not cors_allows_wildcard`, so a wildcard origin automatically disables
credentials. A wildcard in production is fatal anyway, and a malformed origin
is fatal in every environment.

The CSP sends **`script-src 'self'`** with no `'unsafe-inline'` — inline
scripts do not run, which is why the API docs are served from vendored
same-origin assets, bootstrapped by a separate **local** script file
(`/frontend/js/docs-init.js`) rather than FastAPI's stock inline one. No
request ever leaves the host: the automated docs tests assert zero
`http(s)://` references and zero inline scripts on both pages.

**⚠️ GAP — some nginx locations drop the security headers.** nginx's
`add_header` does not merge across levels, so any location defining its own
`add_header` loses the five server-level headers. Affected:
`/frontend/`, `/icons/`, `/tiles/`, `/favicon.ico`, and — most notably — the
**login location**, which sets `Cache-Control`/`Pragma` and therefore serves
login responses without CSP, `X-Frame-Options` or `Referrer-Policy`.
`/frontend/` additionally sets `Access-Control-Allow-Origin *`.

*Risk:* the static locations serve fixed assets, so the practical exposure is
limited; the login location is the one worth fixing. *Fix:* repeat the five
server-level headers inside each of those location blocks.

**Verify**

```bash
curl -sSI https://<host>/ | grep -i 'content-security-policy'
curl -sSI https://<host>/api/auth/login -X POST | grep -ic 'content-security-policy'   # currently 0
```

---

## 12. File permissions and the container user

**Enforced by compose.** Every stack pins `user: "1000:1000"`, so the container
runs as the unprivileged `appuser`. `STORAGE_DIR` not being writable is a
**fatal startup error in every environment**.

**⚠️ GAP — the Dockerfile's final `USER` is `root`.** Non-root depends entirely
on compose's `user:` override. Anyone running the image directly with
`docker run` gets a root container.

**⚠️ GAP — the entrypoint chmods to 777.** `chmod 777 /app/database` and
`chmod 777 "$STORAGE_ROOT"` make those world-writable, which is broader than
needed. `chown 1000:1000` with `750` would be correct.

**⚠️ Local file exposure — TLS private keys are world-readable.**
`certs/internal-ca.key` and `certs/server.key` exist with mode `-rw-r--r--`.
They are **correctly excluded from git** (`.gitignore` covers `certs/` and
`secrets/`, and nothing under either is tracked — verified), so this is a local
filesystem issue, not a repository leak. Note `certs/` is also bind-mounted
read-only into the API container, so the application process can read
`server.key`.

**You must:**

- [ ] `chmod 600 certs/*.key` and `chown` them to the deploying user.
- [ ] `chmod 600 .env` and `chmod 600 secrets/*`.
- [ ] Confirm named volumes are owned by uid 1000.

**Verify**

```bash
ls -l certs/ secrets/ .env
$DC exec face_recognition id                    # uid=1000(appuser)
$DC exec face_recognition ls -ld /app/storage /var/log/face-recognition
```

---

## 13. Dangerous and development endpoints

**Verified clean.** There are no `/dev/*`, `/debug/*`, `/seed/*` or demo routes,
and no endpoint is gated only by `DEBUG`.

Everything that resets or clears is admin-gated at the **router** level:

| Endpoint | Guard |
|---|---|
| `POST /api/cache/clear` | admin (router-level), plus a pattern allowlist |
| `POST /api/face-tracker/reset/{pipeline_id}` | admin (router-level); in-memory tracker state only |
| `POST /api/users/{user_id}/reset-password` | admin **and** CSRF — forces rotation on the target and ends its sessions, unless the target is the caller |

`GET /webhook/test` **is** registered in production, deliberately — it is
guarded by the ingest key and gives an installer a "200 means your key works"
probe.

A previously-existing second, unauthenticated registration of the webhook
router was removed, and a route-inventory test now fails if any webhook
path+method pair is ever registered twice.

**You must:**

- [ ] Confirm no page is reachable without a session. This audit found and fixed
      one such page (`/tracking-people`, which documented an access requirement
      it did not enforce); re-check after adding any new page. The newest is
      `/change-password`, which does require a session — it is exempt from the
      password-rotation gate, not from authentication, so an anonymous request
      gets 401 and is redirected to `/signin`.

---

## 14. Docker socket and host access

**Verified clean — no gap.** `/var/run/docker.sock` is **not** mounted into any
container in any compose file. A repository-wide search returns zero matches.

**You must:**

- [ ] Treat Docker group membership as root-equivalent. Because DB and Redis
      passwords are environment variables (§2), anyone who can run `docker
      inspect` can read them.
- [ ] Keep the administrator account's SSH access key-only, and disable
      password authentication.

---

## 15. Monitoring and logs as a security control

- Prometheus publishes **no host port** in production and lives on an internal
  network; nginx restricts `/metrics` by IP.
- Grafana is loopback-only and requires `GRAFANA_ADMIN_PASSWORD` — there is no
  default and no `admin/admin`.
- Logs are **redacted before any handler sees them**: `Authorization`,
  `x-webhook-key`, passwords, bearer tokens, bare JWTs and raw embeddings all
  become `***REDACTED***`. Usernames and IPs in `[AUTH_AUDIT]` lines are
  pseudonymized.

**You must:**

- [ ] Set `GRAFANA_ADMIN_PASSWORD` to something generated.
- [ ] Review `[AUTH_AUDIT]` lines periodically for `RATE_LIMITED` and
      `INVALID_CREDENTIALS` bursts.
- [ ] Remember logs still contain identity names and camera identifiers —
      review before sharing a diagnostic bundle.

---

## Remediation backlog

The gaps above, in the order worth fixing:

| # | Gap | Effort | Risk if left |
|---|---|---|---|
| 1 | `chmod 600` the TLS private keys | seconds | any local user reads the server key |
| 2 | Exception messages returned to clients (47 sites) | medium | internal detail disclosed to authenticated users |
| 3 | Repeat security headers in the nginx **login** location | small | login page served without CSP / X-Frame-Options |
| 4 | Enable HSTS once the internal CA is distributed | small | TLS downgrade / stripping remains possible |
| 5 | Move DB and Redis passwords to Docker secrets | medium | credentials readable via `docker inspect` |
| 6 | `chown 1000:1000` + `750` instead of `chmod 777` in the entrypoint | small | world-writable storage and database directories |
| 7 | Set `USER appuser` as the Dockerfile's final `USER` | small | `docker run` without compose yields a root container |
| 8 | Add `SQL_AGENT_DB_PASSWORD` to the redaction list | seconds | that credential is not masked in logs |

None of these block a go-live behind a firewall on a trusted network. Items 1
and 3 are quick enough to do now.

---

## Sign-off

- [ ] `generate-secrets.sh` run; all three secret files present, mode 600
- [ ] `.env` mode 600, not tracked by git
- [ ] Bootstrap admin password changed after first login
- [ ] Running a **prod** compose file (GPU = both files, layered)
- [ ] Host firewall: 80/443 in, everything else denied
- [ ] TLS certificate installed; internal CA distributed to clients
- [ ] `/docs` returns 404
- [ ] `DEBUG=false`, `ENVIRONMENT=production`
- [ ] `AUTH_ALLOWED_ORIGINS` set to the exact browser origin
- [ ] Every camera has its own ingest key; `WEBHOOK_AUTH_MODE=enforce`
- [ ] `GRAFANA_ADMIN_PASSWORD` set; Grafana reached over an SSH tunnel
- [ ] A backup has been taken **and a restore has been tested** —
      [`60_BACKUP_AND_RESTORE.md`](60_BACKUP_AND_RESTORE.md)
- [ ] Remediation backlog items 1 and 3 done

---

**See also:** [`61_DEPLOYMENT_RUNBOOK.md`](61_DEPLOYMENT_RUNBOOK.md) ·
[`73_TROUBLESHOOTING.md`](73_TROUBLESHOOTING.md) ·
[`72_ADMIN_CHEAT_SHEET.md`](72_ADMIN_CHEAT_SHEET.md) ·
[`36_CONFIGURATION_GUIDE.md`](36_CONFIGURATION_GUIDE.md)
