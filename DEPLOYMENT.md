# DEPLOYMENT — Plain-English Guide

How this system is deployed, where every value comes from, and how the
certificates, IP addresses and names actually work.

Written to be read start to finish once. For exact commands see
[`Docs/61_DEPLOYMENT_RUNBOOK.md`](Docs/61_DEPLOYMENT_RUNBOOK.md) (the production
authority) and [`Docs/93_PRODUCTION_RUNBOOK.md`](Docs/93_PRODUCTION_RUNBOOK.md)
(the orientation map).

**Last updated: 2026-09-07** — sections 10–16 added (offline policy, rebuild
cache, the `migrate` preflight, the generated variable inventory, volumes, the
SQL bot, faces and enrolment); §2 counts corrected; §6b added.

### Contents

- [1. What was done](#1-what-was-done)
- [2. Variables — where every value comes from](#2-variables--where-every-value-comes-from)
- [3. Certificates — how HTTPS works here](#3-certificates--how-https-works-here)
- [4. IP addresses — who assigns them](#4-ip-addresses--who-assigns-them)
- [4a. Giving the server a static IP](#4a-giving-the-server-a-static-ip)
- [5. DNS — how names are resolved](#5-dns--how-names-are-resolved)
- [5a. File ownership — who owns what, and why](#5a-file-ownership--who-owns-what-and-why)
- [6. Logging in the first time](#6-logging-in-the-first-time)
- [6a. The chatbot, the GPU, and why it was slow](#6a-the-chatbot-the-gpu-and-why-it-was-slow)
- [7. Everyday commands](#7-everyday-commands)
- [7a. Seeing Docker in a GUI](#7a-seeing-docker-in-a-gui)
- [8. If something breaks](#8-if-something-breaks)
- [9. Back up these, in this order](#9-back-up-these-in-this-order)
- [10. Offline policy (production is air-gapped)](#10-offline-policy-production-is-air-gapped)
- [11. Rebuild time: the pip wheel cache](#11-rebuild-time-the-pip-wheel-cache)
- [12. The `migrate` job and the config preflight](#12-the-migrate-job-and-the-config-preflight)
- [13. Production variable inventory (generated 2026-09-07)](#13-production-variable-inventory-generated-2026-09-07)
- [14. Volumes and mounts — what every container can read and write (verified 2026-09-07)](#14-volumes-and-mounts--what-every-container-can-read-and-write-verified-2026-09-07)
- [15. The SQL bot (chat agent): how it runs, and its variables](#15-the-sql-bot-chat-agent-how-it-runs-and-its-variables)
- [16. Faces: how a person is stored, matched and added](#16-faces-how-a-person-is-stored-matched-and-added)

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
- **File ownership unified** (§5a) — everything in the tree belongs to the host
  user except 19 credential files that are deliberately root-owned.
- **Docker reachable without sudo** (§7a) — a systemd drop-in keeps the socket
  accessible to the operator across every restart, so GUI tools connect.
- **The GPU is now actually used for the LLMs** (§6a) — ollama had been running
  every model on CPU. A chatbot question went from **94s to 2–8s**.
- **Python fixed to a final release** (§6a) — the GPU image was building on
  3.11.0rc1, which silently broke semantic search over query history.
- **Database credentials moved out of the environment** (§2) — seven new secret
  files; `docker inspect` now exposes no credential on any service.

---

### Added on 2026-09-07 (after the code pull)

- Production **offline mode** made explicit and enforced (§10): the policy block in
  `docker/.env`, stage 06b of `deploy.sh`, the config guard, `/health/offline-policy`.
- The image rebuild fixed at its roots (§11): pip's cache was never used
  (`PIP_NO_CACHE_DIR` semantics) and BuildKit was discarding the cache after 48 h;
  the CUDA base's `blinker` conflict with `mlflow` resolved in `Dockerfile.gpu`.
- A six-minute outage understood and closed (§12): the `migrate` job no longer runs
  the inference-artifact preflight it cannot satisfy; how to roll back safely.
- Three generated references (§13–§14) and two explanations (§15–§16) so the whole
  configuration surface, every mount and both AI subsystems are documented from the
  running system rather than from memory.

## 2. Variables — where every value comes from

There are **two** template files, and they are not interchangeable.

| File | Who reads it | Contains |
|---|---|---|
| `docker/.env` | **Compose only** — never mounted into a container (the profile-gated `mcp-sql` is the one exception) | 36 deployment values: the 10 credentials/pins below plus the 26 offline-policy keys of §10 — full inventory in §13 |
| `.env.example` | a template for the *application* settings | documents ~120 of the 325 settings in `config.py` |

> Compose reads `.env` from the **`docker/` directory**, not the repo root.
> A value put in a root `.env` is silently ignored by Compose.

### The 10 original values in `docker/.env`, and what each becomes

(The 26 offline-policy keys added on 2026-09-07 are listed in §10; §13 is the complete, generated inventory of everything every container receives.)

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

**Every** credential a container needs arrives as a mounted file, never as an
environment value — ten of them:

```
secrets/jwt_secret                  → JWT_SECRET_KEY_FILE
secrets/bootstrap_admin_password    → BOOTSTRAP_ADMIN_PASSWORD_FILE
secrets/webhook_api_keys            → WEBHOOK_API_KEYS_FILE
secrets/database_url_app            → DATABASE_URL_FILE        (api, ml_worker)
secrets/database_url_migrator       → DATABASE_URL_FILE        (migrate)
secrets/postgres_password_app       → POSTGRES_PASSWORD_FILE   (api, ml_worker)
secrets/postgres_password_migrator  → POSTGRES_PASSWORD_FILE   (migrate)
secrets/redis_url                   → REDIS_URL_FILE
secrets/sql_agent_db_password       → SQL_AGENT_DB_PASSWORD_FILE
secrets/backup_db_password          → read by backup-loop.sh AND backup.sh (deploy.sh backup), exported in-process
```

The app receives a **filename** and opens it itself (`config.py` resolves every
`*_FILE` in `Settings.__init__`, and the file wins over any inline value). That
is why `docker inspect` on any service shows **no credential value** — verified
across all eleven containers. The one exception is the postgres image's own
first-boot `POSTGRES_PASSWORD`, which the image itself requires in its
environment.

The database credentials used to be inline (`DATABASE_URL` with the password
embedded, `POSTGRES_PASSWORD`, `PGPASSWORD` on the backup service) and were
therefore visible to anyone who could run `docker inspect` — and in every
support bundle or pasted terminal that output lands in. The `_FILE` variants
already existed in `config.py`; compose simply never used them.

The DB files are **derived from `docker/.env`** by `generate-secrets.sh`, which
stays the source the postgres init and `db/roles.sql` read. Two copies of one
password, deliberately: `.env` is never mounted, and the files are never in
`environment:`.

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

### The two CA files do OPPOSITE things

This is the one thing to get right.

| File | What it is | Where it goes |
|---|---|---|
| `internal-ca.crt` | **public** certificate, `BEGIN CERTIFICATE`, 0644 | **copy to every client**, import into the browser/OS trust store |
| `internal-ca.key` | **secret** private key, `BEGIN PRIVATE KEY`, 0600 | **move OFFLINE.** Never onto a client, never into a browser |

Tell them apart at a glance:

```bash
head -1 certs/internal-ca.crt     # -----BEGIN CERTIFICATE-----   → distribute
head -1 certs/internal-ca.key     # -----BEGIN PRIVATE KEY-----   → offline
```

Anyone holding `internal-ca.key` can mint a trusted certificate for **any**
hostname your clients accept — including your bank's. That is why it leaves the
building.

### Where to move `internal-ca.key`

Somewhere offline and backed up: a USB stick in a drawer or safe, or an
encrypted password-manager attachment. It is 3 KB.

```bash
sudo cp certs/internal-ca.key /media/$USER/<your-usb>/face-detector-ca.key
sudo shred -u certs/internal-ca.key        # secure-delete from the server
```

**You need it again only to issue a new server certificate** — roughly every
27 months, or to add a name/IP. Bring it back to `certs/`, run the script, then
remove it again.

**This is safe.** The script creates a CA only when `internal-ca.crt` is
missing. With the certificate still present and the key absent, signing fails
with a clear error instead of silently generating a *new* CA — which would
invalidate trust on every client you had already set up.

> Losing this key is survivable but annoying: you cannot issue new server
> certificates, so you would create a fresh CA and re-install it everywhere.
> Losing `internal-ca.crt` costs nothing — it can be re-exported from the key,
> or copied off any client that already has it.

### Getting `internal-ca.crt` to a client machine

```bash
# from the client, pull it over the network
scp itdirect-ai@192.168.1.111:/home/itdirect-ai/Desktop/VAS/certs/internal-ca.crt .
```

Or copy it onto a USB stick. It is public — email, chat and file shares are all
fine.

Verify it is the right file before trusting it (compare on both machines):

```bash
openssl x509 -in internal-ca.crt -noout -fingerprint -sha256
# sha256 Fingerprint=74:B1:2F:13:96:1B:A6:81:8B:6E:9E:AF:14:51:20:EF:
#                    D2:49:00:B0:7E:02:14:88:CB:D8:20:DF:D3:47:80:1F
```

### Where exactly to install it

**Windows — GUI (covers Chrome and Edge)**

1. Double-click `internal-ca.crt` → **Install Certificate**
2. Choose **Local Machine** (all users) → Next → *accept the UAC prompt*
3. Select **Place all certificates in the following store** → **Browse**
4. Choose **Trusted Root Certification Authorities** → OK → Next → Finish
5. Restart the browser

Getting step 3 wrong is the usual failure — if you leave it on "Automatically
select", Windows files it somewhere that is not trusted and nothing changes.

To check: run `certlm.msc` → **Trusted Root Certification Authorities** →
**Certificates** → look for *Face Detector Internal CA*.

**Windows — PowerShell (as Administrator)**

```powershell
Import-Certificate -FilePath .\internal-ca.crt `
  -CertStoreLocation Cert:\LocalMachine\Root
```

**Ubuntu / Debian (covers curl, wget, and system tools)**

```bash
sudo cp internal-ca.crt /usr/local/share/ca-certificates/face-detector-ca.crt
sudo update-ca-certificates          # must report "1 added"
```

The `.crt` extension and the `/usr/local/share/ca-certificates/` path are both
required — the tool ignores anything else.

**Chrome / Chromium on Linux** keeps its **own** store, so the step above does
*not* cover it:

```bash
sudo apt install libnss3-tools
certutil -d sql:$HOME/.pki/nssdb -A -t "C,," \
  -n "Face Detector Internal CA" -i internal-ca.crt
certutil -d sql:$HOME/.pki/nssdb -L      # confirm it is listed
```

**Firefox (every OS)** also keeps its own store:

Settings → **Privacy & Security** → scroll to **Certificates** → **View
Certificates** → **Authorities** tab → **Import** → pick `internal-ca.crt` →
tick **"Trust this CA to identify websites"** → OK → restart Firefox.

**macOS**

```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain internal-ca.crt
```

Or: double-click the file → Keychain Access → **System** → find *Face Detector
Internal CA* → double-click → **Trust** → *When using this certificate* →
**Always Trust**.

**Android** — Settings → Security → **Encryption & credentials** → **Install a
certificate** → **CA certificate** → accept the warning → pick the file.

**iOS / iPadOS** — AirDrop or email the file, open it, Settings → **Profile
Downloaded** → Install. Then the step everyone misses: Settings → General →
About → **Certificate Trust Settings** → enable the switch for this CA.

### Confirming it worked

```bash
curl https://face-detector.internal/health/live      # note: NO -k flag
```

No certificate warning and a `200` means trust is working. In a browser, the
padlock appears with no interstitial. If it still warns, you almost certainly
installed into the wrong store (Windows step 3), or the browser keeps its own
store (Firefox, Chrome-on-Linux) and needs its own import.

### Renewing before 2028

The script **refuses to overwrite an existing certificate**, so remove the
server pair first. Keep the two CA files — reissuing those would force every
client to re-trust.

```bash
rm certs/server.crt certs/server.key          # keep internal-ca.*
sudo bash scripts/tls/make-internal-ca.sh
sudo ./deploy.sh start
```

Clients keep working — they trust the CA, not the individual certificate.

### Adding an IP address to the certificate

> **You almost certainly do not need this.** If you set up DNS (§5 — the
> recommended path), the existing certificate already covers
> `face-detector.internal` and **nothing about the certificates changes at
> all**. Skip this section.
>
> **The CA is never removed.** Neither this procedure nor renewal touches
> `internal-ca.crt` or `internal-ca.key`. Only the *server* pair is replaced,
> and clients keep trusting it because they trust the CA, not the server
> certificate. You never re-distribute anything.

Only needed if you want to browse to the raw IP, `https://192.168.1.111`.
The script takes the IP as a **second argument**:

```bash
rm certs/server.crt certs/server.key
sudo bash scripts/tls/make-internal-ca.sh face-detector.internal 192.168.1.111
sudo ./deploy.sh start
```

That produces `SAN: DNS:face-detector.internal, DNS:localhost, IP:127.0.0.1, IP:192.168.1.111`.

> Even then, browsing by IP still fails **login**, because the app checks the
> browser's `Origin` against `PUBLIC_ORIGIN` (§5). To make IP access work end
> to end you must also set `PUBLIC_ORIGIN=https://192.168.1.111` in
> `docker/.env` and restart. **Using the name is simpler and is what this
> deployment is built around.**

Certificate lifetimes are set in the script: CA 3650 days (10 years), server
825 days (~27 months, the maximum most browsers accept).

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
wlp130s0f0   192.168.1.111/24  (WiFi)   ← the LAN address other machines use
enp129s0     DOWN, no cable             ← wired port, currently unused
default via 192.168.1.1
```

---

## 4a. Giving the server a static IP

**Nothing in the stack needs changing.** Container IPs are Docker's own private
networks, nginx binds every interface (`0.0.0.0:80`, `0.0.0.0:443`), and the app
identifies itself by *name*, not address. No config edit, no rebuild, no restart.

**But you should do it anyway.** The address is currently handed out by DHCP:

```
inet 192.168.1.111/24 ... scope global dynamic
                                       ^^^^^^^
```

Every client reaching this server maps `face-detector.internal → 192.168.1.111`.
When DHCP hands out a different address, **every one of those clients breaks
silently** — a connection timeout, with nothing in the server logs, because the
request never arrives.

> **Before you start:** make sure the router will not give `.111` to something
> else. Either exclude it from the DHCP pool, or choose an address outside the
> pool. Do this at the console — reconfiguring the interface you are connected
> over will drop your session.

### Linux — desktop (NetworkManager)

This host uses NetworkManager; the active connection is named `Tiger`.

```bash
sudo nmcli con mod "Tiger" \
  ipv4.method manual \
  ipv4.addresses 192.168.1.111/24 \
  ipv4.gateway 192.168.1.1 \
  ipv4.dns "192.168.1.1"
sudo nmcli con up "Tiger"
```

Verify — the word `dynamic` must be gone:

```bash
ip -4 addr show wlp130s0f0        # expect: scope global noprefixroute
ip route | grep default           # expect: default via 192.168.1.1
```

To revert: `sudo nmcli con mod "Tiger" ipv4.method auto && sudo nmcli con up "Tiger"`

*Prefer the wired port* if you can run a cable — replace `"Tiger"` with the
wired connection name (`nmcli con show` lists them) and `wlp130s0f0` with
`enp129s0`. For a server taking camera webhooks and doing GPU inference, wired
avoids WiFi roaming and interference.

### Linux — server install (netplan, no NetworkManager)

Ubuntu Server uses netplan instead. Edit `/etc/netplan/*.yaml`:

```yaml
network:
  version: 2
  ethernets:
    enp129s0:
      dhcp4: false
      addresses: [192.168.1.111/24]
      routes:
        - to: default
          via: 192.168.1.1
      nameservers:
        addresses: [192.168.1.1]
```

```bash
sudo netplan try      # applies, then auto-reverts in 120s unless you confirm
sudo netplan apply
```

`netplan try` is the safe one — if the change locks you out, it undoes itself.

### Windows — GUI

1. **Settings → Network & Internet** → click the adapter (Ethernet or Wi-Fi)
2. Next to **IP assignment**, click **Edit**
3. Change **Automatic (DHCP)** to **Manual**, turn **IPv4** on
4. Fill in:
   - **IP address** `192.168.1.111`
   - **Subnet mask** `255.255.255.0`   *(this is what `/24` means)*
   - **Gateway** `192.168.1.1`
   - **Preferred DNS** `192.168.1.1`
5. **Save**

### Windows — PowerShell (as Administrator)

```powershell
# find the adapter name first
Get-NetAdapter

New-NetIPAddress -InterfaceAlias "Ethernet" `
  -IPAddress 192.168.1.111 -PrefixLength 24 -DefaultGateway 192.168.1.1

Set-DnsClientServerAddress -InterfaceAlias "Ethernet" -ServerAddresses 192.168.1.1
```

Verify with `ipconfig /all` — **DHCP Enabled** should read **No**.

To revert to DHCP:

```powershell
Set-NetIPInterface -InterfaceAlias "Ethernet" -Dhcp Enabled
Set-DnsClientServerAddress -InterfaceAlias "Ethernet" -ResetServerAddresses
```

### After the change

Nothing to do on the server. Confirm the stack is still reachable:

```bash
sudo ./deploy.sh doctor
curl -sk -o /dev/null -w '%{http_code}\n' https://face-detector.internal/health/live
```

If you changed to a **different** address, update wherever the name is mapped —
each client's hosts file, or the single A record on your router (§5).

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

### Static IP + DNS — the recommended setup

This is the combination you want, and it needs **no certificate work whatsoever**.

```
static IP on the server        192.168.1.111        (§4a)
one DNS A record               face-detector.internal → 192.168.1.111
existing certificate           already covers that name — unchanged
internal-ca.crt / .key         untouched, never re-distributed
```

Why it needs nothing from the certificates: the server certificate is issued
for the **name**, and the name is what clients keep using. The IP behind the
name is a DNS concern, invisible to TLS. Change the IP, update one A record,
and every client follows — no re-issuing, no re-trusting, no downtime.

**Option A — your router (simplest).** Most home/office routers can map a name
to an address. Look for *Local DNS*, *DNS Host Names*, *Static DNS*, or
*Address Reservation* in the admin page at `http://192.168.1.1`, and add:

```
Name: face-detector.internal        Address: 192.168.1.111
```

While you are there, reserve `192.168.1.111` for this server's MAC so DHCP
never offers it to anything else.

**Option B — a real DNS server** (dnsmasq, Pi-hole, or Windows Server DNS), if
the router cannot do it.

*dnsmasq or Pi-hole* — one line in `/etc/dnsmasq.d/face-detector.conf`:

```
address=/face-detector.internal/192.168.1.111
```
```bash
sudo systemctl restart dnsmasq        # or: pihole restartdns
```

*Windows Server DNS* — in **DNS Manager**, right-click your forward lookup
zone → **New Host (A or AAAA)** → Name `face-detector`, IP `192.168.1.111`.
Or in PowerShell as Administrator:

```powershell
Add-DnsServerResourceRecordA -ZoneName "internal" `
  -Name "face-detector" -IPv4Address "192.168.1.111"
```

Then point clients' DNS at that server (usually handed out by DHCP).

**Option C — per-machine hosts file**, only if you have no DNS control. This
does not scale: every client needs editing again if the IP ever changes.

- **Linux / macOS** — `/etc/hosts`:
  ```
  192.168.1.111    face-detector.internal
  ```
- **Windows** — the same line in `C:\Windows\System32\drivers\etc\hosts`
  (open Notepad as Administrator).

### Verifying DNS works

From a client machine:

```bash
nslookup face-detector.internal        # must return 192.168.1.111
curl -I https://face-detector.internal/health/live
```

```powershell
Resolve-DnsName face-detector.internal     # Windows
ipconfig /flushdns                         # if it still returns the old address
```

Then trust the CA once per machine (§3) and browse to
**https://face-detector.internal**.

> Keep the server's own `/etc/hosts` line (`127.0.0.1 face-detector.internal`)
> even after DNS is working. It lets the server reach itself without leaving the
> box, which is what `./deploy.sh doctor` and the health checks rely on.

---

## 5a. File ownership — who owns what, and why

`scripts/deploy/paths.sh` is the **single source of truth** for ownership and
permissions. It exists because these values were previously set in four
different places, and three separate production failures came out of the
disagreement — including one where redis could not read its own ACL file and
crash-looped, silently taking the whole application tier down with it.

The rule it encodes:

| Kind of path | Owner |
|---|---|
| a container **writes** it | `1000:1000` (the service uid = your user) |
| a container **reads** it | readable by the service, **never writable** |
| a secret | `0440 root:1000` — you can read it, nothing can modify it |
| a credential store | `root`, as tight as the tooling allows |

### Almost everything is yours

The entire source tree, docs, scripts, tests, weights, map data and logs are
owned by `itdirect-ai`. You do not need `sudo` to edit code, add a model, drop
in a `.mbtiles` archive, or read a deploy log.

Reference data (`weights/`, `map-data/production`, `map-data/metadata`) is
yours **and** still safe from containers, because those are bind-mounted
**read-only** (`:ro`). The `:ro` flag is what actually prevents a container
writing to them — host ownership was never doing that job.

### The 19 files that stay root, and what each protects

```
docker/.env              all 8 database / Redis / Grafana passwords
certs/server.key         the TLS private key nginx serves
certs/internal-ca.key    the CA key — can mint a cert for ANY hostname (§3)
secrets/*                jwt, bootstrap admin, webhook keys
backups/*                database dumps + config snapshots containing secrets
.deployment/state.json   deployment state
```

These are not tidiness — they are the boundary that keeps a stray script, a
compromised dependency, or a mistyped command from reading every credential in
the system. You can still *read* `secrets/` without sudo (the directory is
`0750 root:1000`); you simply cannot modify them.

> **One exception is not root at all:** `docker/redis/users.acl` is owned by
> **uid 999** — that is the redis user *inside* the container. Change it and
> redis cannot read its own ACL, crash-loops, and every service that waits on
> redis being healthy fails to start behind a generic
> `dependency failed to start` message that never mentions redis.

### Checking and repairing ownership

```bash
sudo ./deploy.sh paths       # read-only: every row must say "ok"
sudo ./deploy.sh install     # stage 03 re-applies the manifest
```

If you deliberately change an owner, **update `paths.sh` in the same step**.
Otherwise `paths` and `doctor` report DRIFT forever, and the next `install`
silently reverts your change.

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

### 6b. Accounts survive rebuilds — what the bootstrap password does and does not do

The bootstrap password is used **once in the life of a database**: at the first
boot with an empty `users` table the API creates `admin` from
`secrets/bootstrap_admin_password` and sets `must_change_password`, so the first
login is redirected to `/change-password`. Changing it there clears the flag and
stamps `password_changed_at`. From then on:

- **A rebuild, `deploy.sh upgrade`, restart or rollback never resets a password
  or re-arms the forced change.** Accounts live in the `postgres_data` volume,
  which every deploy path preserves (§14); the image only carries code. After a
  rebuild you log in with whatever password the account had before it.
- **To change your own password:** log in and open `/change-password`
  (`POST /api/auth/change-password`). Do this whenever a password has been
  written down, pasted into a chat or shared.
- **To reset another user's password as an administrator:** the Users page
  (`POST /api/users/{id}/reset-password`). An administrator cannot delete or
  demote the last administrator.
- **To see the state of an account** (read-only, from the host):

  ```bash
  sudo docker exec -e PGPASSWORD="$(sudo grep '^FR_READONLY_PASSWORD=' docker/.env | cut -d= -f2-)" \
    face_detector_prod-postgres-1 psql -h 127.0.0.1 -U fr_readonly -d face_recognition \
    -c "select username, role, is_active, must_change_password, password_changed_at, last_login from users"
  ```

- **The bootstrap flow happens again only on a fresh database.** A new
  production install starts empty and therefore forces the change on the first
  login automatically (`BOOTSTRAP_ADMIN_REQUIRE_ROTATION=true` on the API). To
  rehearse it on an existing host you would have to destroy the database
  (`sudo ./deploy.sh uninstall --purge-data`, then install again) — there is no
  switch that re-arms the bootstrap on a populated database, by design.

---

## 6a. The chatbot, the GPU, and why it was slow

### What runs where

Two different GPU workloads share the one RTX 5090:

| Workload | Runs on | Used for |
|---|---|---|
| SCRFD + ArcFace | ONNX Runtime + CUDA | face detection and recognition |
| ollama (3 models) | llama.cpp + CUDA | the chatbot and SQL generation |

Three models stay resident (`OLLAMA_KEEP_ALIVE=-1`), about 15 GB of the card's
32 GB:

```
qwen2.5:7b                      the chat / tool-selecting model  (OLLAMA_MODEL)
Arctic-Text2SQL-R1-7B           SQL generation                   (OLLAMA_SQL_MODEL)
qwen2.5:1.5b                    the previous chat model, still cached
```

### The defect that made it slow

A chatbot question took **94 seconds**. The models were not the problem —
`ollama ps` was:

```
NAME                    PROCESSOR
Arctic-Text2SQL-R1-7B   100% CPU     <- on a host with an idle RTX 5090
qwen2.5:7b              100% CPU
qwen2.5:1.5b            100% CPU
```

Root cause was in `scripts/deploy/stage-gpu.sh`:

```bash
if [ -n "$ollama_uuid" ]; then      # only ever true on a 2-GPU host
```

On a single-GPU host that variable is empty, so the generated overlay printed

```
#   ollama           -> (shares the face_recognition GPU)
```

in its header **and emitted no device reservation for ollama at all**. Compose
needs a `deploy.resources.reservations.devices` entry before the NVIDIA runtime
will inject the GPU; `NVIDIA_VISIBLE_DEVICES=all` on the service does nothing on
its own. The file documented sharing and configured none.

ollama now always gets a reservation — its own card on a 2-GPU host, the *same*
UUID as face_recognition on a 1-GPU host.

### The difference it made

| | on CPU | on GPU |
|---|---|---|
| Arctic SQL 7B, warm | 24.1s | **1.0s** |
| qwen2.5:7b, warm | 3.0s | **0.1s** |
| **a full chatbot question** | **94s** | **2–8s** |

Verify it yourself at any time — `PROCESSOR` must say GPU, not CPU:

```bash
sudo docker exec face_detector_prod-ollama-1 ollama ps
nvidia-smi --query-gpu=memory.used,memory.free --format=csv
```

### Why the chat model is qwen2.5:7b and not 1.5b

Measured on this host with the real 11-tool payload:

| | native tool calls | wrong tool |
|---|---|---|
| qwen2.5:1.5b | 3 of 6 (2 unparseable) | 1 |
| qwen2.5:7b | **6 of 6** | 0 |

The 1.5B advertises function calling but often returns prose the fallback
parser has to rescue, and once chose `list_my_documents` for "list the people
seen at the north gate". The 7B answers both correctly and honestly — *"No
person named 'Joey' is enrolled"* rather than inventing one.

Once ollama is on the GPU the 7B costs nothing in warm latency (0.1s for both).
End to end it is a little slower — **7–8s vs 2–3s** — because it generates more
tokens. That is the trade: correct in 8s over wrong in 3s. To revert, set
`OLLAMA_MODEL: qwen2.5:1.5b` in `docker/docker-compose.prod.yml` and recreate.

### Python must be a FINAL 3.11, never an RC

`docker/Dockerfile.gpu` installs Python from the **deadsnakes** PPA, not from
Ubuntu. This is deliberate: jammy's `python3.11` package is
`3.11.0~rc1-1~22.04.1`, a release *candidate*. It lacks
`sys.get_int_max_str_digits`, which torch's `_dynamo` polyfill needs at import,
and the failure surfaced three layers away:

```
sentence_transformers -> "Could not import PreTrainedModel"
transformers          -> AttributeError
torch/_dynamo         -> sys.get_int_max_str_digits      <- the real cause
```

Every chatbot question then logged *"no embedding produced — semantic query
search degraded"* while everything else looked healthy. The CPU image never had
this: it builds `FROM python:3.11-slim`.

The Dockerfile now asserts at build time, so an RC cannot return silently:

```dockerfile
RUN ... && python3.11 -c "import sys; assert hasattr(sys, 'get_int_max_str_digits')"
```

### torch is pinned to the CPU build on purpose

Both requirements files pin `torch==2.13.0+cpu`. torch exists only because
`sentence-transformers` needs it, for one 384-dim embedding that runs on CPU in
milliseconds. The CUDA build would add several GB and contend for the same card
SCRFD and ArcFace need.

The pin is load-bearing: `--extra-index-url` **adds** an index, it does not
prefer one. pip resolves the highest version across PyPI *and* the CPU index, so
an unpinned `torch` silently resolved to the PyPI CUDA build and began pulling
`nvidia_cublas_cu12` (581 MB) and friends. The `+cpu` local version exists only
on the pytorch index, so naming it forces the CPU wheel.

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

## 7a. Seeing Docker in a GUI

### VS Code — simplest

Install the **Container Tools** extension (Microsoft). You get a sidebar with
containers, images, volumes and networks, plus right-click **View Logs**,
**Attach Shell** and **Inspect**. It uses your existing engine — no new daemon.

### Why it may say "permission denied"

```
Failed to connect. Is Docker running?
permission denied while trying to connect to the docker API at
unix:///var/run/docker.sock
```

Docker is fine. The socket is `srw-rw---- root:docker`, and **Linux applies
group membership only at login**. Any program started before your account was
added to the `docker` group — including your desktop session, and anything
launched from it — still runs without that group.

Check it:

```bash
id -nG                  # groups THIS session actually has
id -nG $USER            # groups the ACCOUNT is configured with
```

If `docker` appears in the second but not the first, that is the whole problem.

**Permanent fix, already applied here** — a systemd drop-in at
`/etc/systemd/system/docker.socket.d/10-acl.conf`:

```ini
[Socket]
ExecStartPost=-/usr/bin/setfacl -m u:itdirect-ai:rw /run/docker.sock
```

It hooks `docker.socket` (the unit that creates the socket), so the ACL is
reapplied on every Docker restart and every reboot. The leading `-` means a
`setfacl` failure is ignored — a permissions problem must never stop Docker
itself from starting.

This grants nothing beyond what the `docker` group already grants; it just
applies without waiting for a fresh login. After your next logout/login, group
membership covers it and the drop-in is redundant but harmless.

### Portainer — a full web dashboard

Run it **standalone**, not in the production compose file:

```bash
docker volume create portainer_data
docker run -d --name portainer --restart unless-stopped \
  -p 127.0.0.1:9443:9443 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v portainer_data:/data \
  portainer/portainer-ce:latest
```

Then open **https://localhost:9443**.

Two deliberate choices there:

- **`127.0.0.1:9443`, not `0.0.0.0`.** Mounting the Docker socket gives
  Portainer root-equivalent control of the host — anyone who reaches that port
  can start a privileged container and own the machine. For remote access,
  tunnel it: `ssh -L 9443:127.0.0.1:9443 user@192.168.1.111`.
- **Not added to `docker-compose.prod.yml`.** The test
  `test_only_web_ports_are_published` allows 80/443/3000 only, so adding it
  would fail the suite — correctly, since Portainer is not part of the product.

### lazydocker — terminal, zero exposure

```bash
sudo apt install lazydocker && lazydocker
```

### Do NOT install Docker Desktop

It ships **its own engine** and switches the CLI context to it. That engine has
none of these containers, so `docker ps` returns an empty list while the stack
is serving traffic — a confusion that cost real time on this deployment.
`./deploy.sh doctor` **section 0** detects it:

```
connected to Docker Desktop (context=...)   WRONG ENGINE
fix: docker context use default
```

Rancher Desktop has the same problem, for the same reason.

---

## 8. If something breaks

| Symptom | Where to look |
|---|---|
| site down, "dependency failed to start" | a health check one tier below — check redis first |
| login refused in a browser, `curl -k` works | the name does not resolve, or you used `localhost` (§5) |
| certificate warning | the CA is not trusted on that machine (§3) |
| all four basemaps missing | `map-data/metadata/content_verdicts.json` absent; the gate fails closed |
| a mounted secret ignored | check the field name — it is `REDIS_URL_FILE`, not `REDIS_PASSWORD_FILE` |
| Docker GUI: "permission denied" on the socket | your session predates the `docker` group (§7a) |
| `docker ps` empty while the site serves | CLI pointed at Docker Desktop's engine — `docker context use default` |
| `deploy.sh paths` reports DRIFT | someone changed an owner without updating `paths.sh` (§5a) |
| chatbot takes ~90s to answer | ollama is on CPU — `ollama ps` must say GPU (§6a) |
| "semantic query search degraded" in the log | Python is an RC, not a final 3.11 (§6a) |
| image build pulls GB of `nvidia_*` wheels | `torch` lost its `+cpu` pin (§6a) |

Start with `sudo ./deploy.sh doctor`. It is read-only, orders findings by
dependency, and prints the command that fixes each one. **Read the first
problem, not the last** — later ones are usually its consequence.

Deeper trees: [`Docs/73_TROUBLESHOOTING.md`](Docs/73_TROUBLESHOOTING.md).

---

## 9. Back up these, in this order

(§14 lists every volume with what it holds and which of them the backup job covers.)

1. **`secrets/` and `docker/.env`** — not regenerable. A new `jwt_secret` logs
   everyone out; new DB passwords no longer match the roles inside postgres.
2. **`certs/internal-ca.*`** — reissuing means re-trusting the CA on every client.
3. **`postgres_data`** — via `./deploy.sh backup`, not a volume copy.
4. **`storage_data`** — face crops, referenced by the database.
5. **`ml_artifacts_data`** — the database references these files by hash.

`weights/`, `map-data/` and the Ollama models are re-downloadable — but keep a
copy if this site must stay offline.

## 10. Offline policy (production is air-gapped)

Production **enforces** offline operation; development may use the internet.
The switch is a block of plain settings in `docker/.env` (no secrets), copied
from `docker/env.production.example`:

```
ENVIRONMENT=production          OFFLINE_MODE=true
LLM_PROVIDER=ollama             OLLAMA_MODEL=qwen2.5:7b
OLLAMA_SQL_MODEL=hf.co/mradermacher/Arctic-Text2SQL-R1-7B-GGUF:Q4_K_M
LLM_DEV_PROVIDER=               NVIDIA_NIM_API_KEY=          (empty: no hosted NIM)
EMBEDDING_PROVIDER=local        EMBEDDING_MODEL_PATH=/home/appuser/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx
VECTOR_STORE=chroma  MCP_SQL_URL=  MILVUS_URI=  STT_PROVIDER=none  OTEL_EXPORTER_ENDPOINT=
ALLOW_EXTERNAL_APIS=false  ALLOW_MODEL_DOWNLOADS=false  ALLOW_EXTERNAL_TELEMETRY=false
SQL_AGENT_OPIK_ENABLED=false    HF_HUB_OFFLINE=1  TRANSFORMERS_OFFLINE=1
```

What enforces it:

- **`deploy.sh` stage 06b** refuses to deploy when `docker/.env` carries a
  development value (`OFFLINE_MODE=false`, any `ALLOW_*=true`, `LLM_DEV_PROVIDER`
  set, a hosted `LLM_PROVIDER`, the Opik tracer, or any endpoint pointing at a
  known cloud host), and writes the missing mode keys when the file has none.
- **The config guard** (`backend/security/offline_policy.py`) refuses the boot
  (exit 78, `/health/ready` never green) if any endpoint is not internal or a
  required local artifact is missing (the ONNX embedding model is baked into
  the image; detection/recognition weights are verified by stage 08).
- **`/health/offline-policy`** (admin) shows the `[PASS]`/`[FAIL]` checklist
  the guard evaluated.

The `vllm`, `mcp-sql` and `milvus` services in the compose file are
**profile-gated** and never start unless you ask for that profile; the
default production stack is Ollama + embedded Chroma, all on the box.

Development stays separate: `./deploy.sh dev` and `docker/docker-compose.cpu.yml`
may set `ALLOW_*=true` and `LLM_DEV_PROVIDER=nim` (NVIDIA NIM for testing the
SQL agent); none of that is accepted by the production path.

## 11. Rebuild time: the pip wheel cache

A full image rebuild downloads about 2.4 GB of wheels (CUDA libraries for
`faiss-gpu`/`xgboost`, `onnxruntime-gpu`, torch CPU, the ML stack). Two things
decide whether the *next* rebuild takes 40 minutes or 3:

1. **The Dockerfiles cache wheels in a BuildKit cache mount**
   (`--mount=type=cache,target=/root/.cache/pip`). pip must not see
   `PIP_NO_CACHE_DIR` on those steps: pip disables its cache for **any** value
   of that variable, even `0` or `false` (deliberate legacy behaviour in
   `pip/_internal/cli/cmdoptions.py`). The cached `RUN` lines therefore use
   `env -u PIP_NO_CACHE_DIR pip install …`. Verify after a build with
   `sudo docker buildx du --verbose` — the `exec.cachemount` record should be
   about 1.3 GB, not 0 B.
2. **BuildKit's default garbage collection deletes that cache after 48 h
   unused.** `/etc/docker/daemon.json` on this host carries a builder GC policy
   that keeps cache mounts by size instead of age (copy:
   `docker/daemon.json.production.example`):

   ```
   rule 0  type=exec.cachemount   keep up to 60 GB, no age limit
   rule 1  unused-for=720h        keep up to 60 GB
   rule 2  everything             keep up to 120 GB
   ```
   Note the single `=` in `type=exec.cachemount`: dockerd splits daemon.json
   filters at the first `=`, so `type==…` silently becomes a filter that
   matches nothing. Applying the file needs `sudo systemctl restart docker`
   (all containers restart; they come back on their own within ~20 s).

The first rebuild after either fix still downloads everything once; every
rebuild after that reuses the cache.

## 12. The `migrate` job and the config preflight

Since 2026-09-06 the entrypoint's config preflight also verifies that the
inference artifacts (detection/recognition weights, the embedding model) exist
inside every production container. The one-shot `migrate` job runs the CPU
image with no weights mount and no embedding model — it never runs inference —
so it refused to start (exit 78) and, because the app tier waits for it, took
production down for six minutes on 2026-09-07. It now sets
`CONFIG_PREFLIGHT: "0"`, the bypass the entrypoint documents for one-shot
maintenance commands (`ml_worker` already used it). The migration tool keeps its
own fail-closed checks (`MIGRATIONS_EXPECTED_HEAD`, revision mismatch), and
`face_recognition` still runs the full guard; `/health/offline-policy` shows
its checklist.

Two lessons for the next failed upgrade:

- **Check the database before believing "schema advanced".** `deploy.sh`
  compares the *intended* heads, not `alembic_version`. If
  `select version_num from alembic_version` still shows the old head, a
  code-only rollback is safe.
- **The `:rollback` tags are whatever was `:latest` when the rollback point was
  taken.** After a build that failed part-way, one of them (here `migrate`)
  can already hold the new code. The working rollback was: tag the genuine old
  `face_recognition:rollback` image as `migrate:latest` (same code base; its
  migration is a no-op), restore `MIGRATIONS_EXPECTED_HEAD` in `docker/.env`,
  then `docker compose … up -d --no-build`.

## 13. Production variable inventory (generated 2026-09-07)

Everything below was produced from the *rendered* production configuration
(`docker compose … config`) and the host file, not typed by hand. Three layers:

1. **`docker/.env`** — read by Compose and by `deploy.sh`; never mounted into
   the application containers.
2. **Docker secrets** — files under `secrets/`, mounted at `/run/secrets/…`; the
   containers receive only the *path* through a `*_FILE` variable.
3. **Container environment** — the exact variables each production container
   starts with. Anything not listed here comes from `config.py` defaults.

### 13.0 How a value reaches the app, and how to change one

**Resolution order inside a container** (highest wins):

```
1. environment: in docker-compose.prod.yml   (literal, or ${VAR} filled from docker/.env)
2. a row saved by the Settings page          (application_settings table — only for the
                                              187 settings registered in runtime_settings.py;
                                              hydrated at boot, ignored if it equals the env value)
3. the default in config.py
```
Secrets are the exception: the container gets only `NAME_FILE=/run/secrets/<file>`,
and `config.py` reads the file into `NAME` through `backend/security/secrets.py`
(`resolve_secret`). A secret value never appears in `docker inspect`.

**To change a value**

| You want to… | Do this | Takes effect |
|---|---|---|
| change a runtime-changeable setting (column "Runtime change" says yes) | Settings page as admin, or `PUT /api/settings/<KEY>` with header `X-Requested-With: XMLHttpRequest` and body `{"value": "<text>", "change_reason": "…"}` | per its apply mode: immediately / next request / next job run / after an API restart |
| change anything set in the compose file | edit `docker/docker-compose.prod.yml`, then `sudo ./deploy.sh upgrade --yes` (full path with backup and health battery) — or, for an environment-only change, `cd docker && sudo docker compose -f docker-compose.prod.yml -f docker-compose.prod.gpu.yml -f gpu-allocation.generated.yml up -d <service>` | on container recreate |
| change a `docker/.env` value that compose interpolates | edit `docker/.env` (root, mode 0600), then recreate the services that use it (column "Consumed by") | on container recreate |
| rotate a credential | edit the value in `docker/.env`, delete the corresponding file(s) under `secrets/`, run `sudo ./deploy.sh upgrade --yes` — it regenerates the files, re-applies `db/roles.sql` so database roles follow, and recreates the containers | on container recreate |
| see the effective value | `GET /api/settings` (admin; shows stored, env and effective value and the source) · `docker inspect <container> --format '{{range .Config.Env}}{{println .}}{{end}}'` · `/health/offline-policy` for the offline checklist | — |

A Settings-page row that differs from the compose value **overrides it until the row is deleted** (the page shows it as overridden). A row equal to the compose value is a harmless mirror and is skipped at boot.

### 13.1 `docker/.env` — every key, how to obtain it, where it goes

Host file, mode 0600 root. Read by Docker Compose for `${…}` and by `deploy.sh` stage 06b. Never mounted into the application containers (the profile-gated `mcp-sql` is the only exception).

| Key | Value | How to obtain / set it | Consumed by |
|---|---|---|---|
| `PUBLIC_ORIGIN` | `https://face-detector.internal` | the https:// name users type (must match the certificate); set with 'sudo ./deploy.sh --public-origin=https://<host>' — feeds 'CORS_ORIGINS'/'AUTH_ALLOWED_ORIGINS' | face_recognition, migrate |
| `POSTGRES_SUPERUSER_PASSWORD` | `<secret>` | generated by 'scripts/setup/generate-secrets.sh' (openssl rand); used by the postgres image on FIRST boot only — changing it later does not change the role | postgres |
| `FR_APP_PASSWORD` | `<secret>` | generated by generate-secrets.sh → 'secrets/postgres_password_app' + 'secrets/database_url_app'; rotate: edit here, delete those two files, 'sudo ./deploy.sh upgrade --yes' (re-applies db/roles.sql so the role follows) | generate-secrets.sh → secret file |
| `FR_MIGRATOR_PASSWORD` | `<secret>` | as FR_APP_PASSWORD, for 'secrets/postgres_password_migrator' + 'secrets/database_url_migrator' | generate-secrets.sh → secret file |
| `FR_READONLY_PASSWORD` | `<secret>` | as FR_APP_PASSWORD, for 'secrets/sql_agent_db_password' (the SQL agent's read-only role) | generate-secrets.sh → secret file |
| `FR_BACKUP_PASSWORD` | `<secret>` | as FR_APP_PASSWORD, for 'secrets/backup_db_password' | generate-secrets.sh → secret file |
| `REDIS_PASSWORD` | `<secret>` | generated by generate-secrets.sh → 'secrets/redis_url' and 'docker/redis/users.acl'; rotate: edit, delete both, re-run generate-secrets.sh, recreate redis + API | redis |
| `REDIS_MONITOR_PASSWORD` | `<secret>` | generated by generate-secrets.sh; hashed into 'docker/redis/users.acl' only | generate-secrets.sh → secret file |
| `GRAFANA_ADMIN_PASSWORD` | `<secret>` | generated by generate-secrets.sh; becomes 'GF_SECURITY_ADMIN_PASSWORD' (grafana admin login) | grafana |
| `MIGRATIONS_EXPECTED_HEAD` | `fcc3d4e5f6a7` | pinned automatically by 'deploy.sh upgrade' to the checkout's alembic head; never edit by hand — the migrate job refuses a mismatch | face_recognition, migrate, ml_worker |
| `ENVIRONMENT` | `production` | keep 'production'; the services set it themselves — this copy is what stage 06b checks | deploy.sh stage 06b (gate) |
| `OFFLINE_MODE` | `true` | keep 'true' (or empty = follow ENVIRONMENT); 'false' is refused by stage 06b and the config guard | deploy.sh stage 06b (gate) |
| `LLM_PROVIDER` | `ollama` | 'ollama' in production; 'vllm'/'nim_local' need the 'vllm' profile; hosted providers are refused | deploy.sh stage 06b (gate) |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | internal URL of the ollama service; the compose file also sets it literally on the API | deploy.sh stage 06b (gate) |
| `OLLAMA_MODEL` | `qwen2.5:7b` | chat/tool model; must be present in 'ollama list' (stage 13 checks); pull on a connected host or import via the offline bundle | deploy.sh stage 06b (gate) |
| `OLLAMA_SQL_MODEL` | `hf.co/mradermacher/Arctic-Text2SQL-R1-7B-GGUF:Q4_K_M` | SQL specialist model; same presence rule as OLLAMA_MODEL | deploy.sh stage 06b (gate) |
| `LLM_DEV_PROVIDER` | (empty) | must stay EMPTY in production ('nim' is development only) | deploy.sh stage 06b (gate) |
| `NVIDIA_NIM_API_KEY` | (empty) | must stay EMPTY in production | generate-secrets.sh → secret file |
| `EMBEDDING_PROVIDER` | `local` | 'local' — the ONNX MiniLM baked into the GPU image | deploy.sh stage 06b (gate) |
| `EMBEDDING_BASE_URL` | (empty) | empty unless a local embedding service is deployed | deploy.sh stage 06b (gate) |
| `EMBEDDING_MODEL_PATH` | `/home/appuser/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx` | path inside the API container; the config guard verifies the file exists at boot | deploy.sh stage 06b (gate) |
| `VECTOR_STORE` | `chroma` | 'chroma' (embedded); 'milvus' needs the 'milvus' profile and MILVUS_URI | deploy.sh stage 06b (gate) |
| `MILVUS_URI` | (empty) | empty unless VECTOR_STORE=milvus | deploy.sh stage 06b (gate) |
| `MCP_SQL_URL` | (empty) | empty = in-process SQL tools; set only with the 'mcp-sql' profile | deploy.sh stage 06b (gate) |
| `AGENT_ORCHESTRATOR` | `langgraph` | 'langgraph' (built in); 'nemo' only if that toolkit is installed | deploy.sh stage 06b (gate) |
| `STT_PROVIDER` | `none` | 'none' until a local speech model is bundled | deploy.sh stage 06b (gate) |
| `STT_BASE_URL` | (empty) | empty unless STT_PROVIDER points at a local service | deploy.sh stage 06b (gate) |
| `STT_MODEL_PATH` | (empty) | empty unless STT_PROVIDER=local; then the file must exist (guard) | deploy.sh stage 06b (gate) |
| `OTEL_EXPORTER_ENDPOINT` | (empty) | empty (Prometheus/Grafana are the telemetry); an internal OTLP collector URL is allowed, a cloud one is refused | deploy.sh stage 06b (gate) |
| `SQL_AGENT_OPIK_ENABLED` | `false` | must be 'false' in production (Opik tracer is development only) | deploy.sh stage 06b (gate) |
| `ALLOW_EXTERNAL_APIS` | `false` | must be 'false' | deploy.sh stage 06b (gate) |
| `ALLOW_MODEL_DOWNLOADS` | `false` | must be 'false' | deploy.sh stage 06b (gate) |
| `ALLOW_EXTERNAL_TELEMETRY` | `false` | must be 'false' | deploy.sh stage 06b (gate) |
| `OFFLINE_BUNDLE_MANIFEST` | (empty) | empty unless the offline bundle directory is also mounted in the API container | deploy.sh stage 06b (gate) |
| `HF_HUB_OFFLINE` | `1` | keep '1' | deploy.sh stage 06b (gate) |
| `TRANSFORMERS_OFFLINE` | `1` | keep '1' | deploy.sh stage 06b (gate) |

### 13.2 Docker secrets — created by `scripts/setup/generate-secrets.sh`, files in `secrets/` (0440 root:1000)

| Secret file | Derived from | Mounted by | App reads it as | Rotate |
|---|---|---|---|---|
| `backup_db_password` | FR_BACKUP_PASSWORD | backup | read directly by the service | change 'FR_BACKUP_PASSWORD' in docker/.env, delete the file, 'deploy.sh upgrade' |
| `bootstrap_admin_password` | random; first admin login, rotation forced on first login | face_recognition | `BOOTSTRAP_ADMIN_PASSWORD` | delete the file, re-run generate-secrets.sh, recreate the services |
| `database_url_app` | FR_APP_PASSWORD | face_recognition, ml_worker | `DATABASE_URL` | change 'FR_APP_PASSWORD' in docker/.env, delete the file, 'deploy.sh upgrade' |
| `database_url_migrator` | FR_MIGRATOR_PASSWORD | migrate | `DATABASE_URL` | change 'FR_MIGRATOR_PASSWORD' in docker/.env, delete the file, 'deploy.sh upgrade' |
| `jwt_secret` | random (openssl rand) | face_recognition, migrate | `JWT_SECRET_KEY` | delete the file, re-run generate-secrets.sh, recreate the services |
| `postgres_password_app` | FR_APP_PASSWORD | face_recognition, ml_worker | `POSTGRES_PASSWORD` | change 'FR_APP_PASSWORD' in docker/.env, delete the file, 'deploy.sh upgrade' |
| `postgres_password_migrator` | FR_MIGRATOR_PASSWORD | migrate | `POSTGRES_PASSWORD` | change 'FR_MIGRATOR_PASSWORD' in docker/.env, delete the file, 'deploy.sh upgrade' |
| `redis_url` | REDIS_PASSWORD | face_recognition, migrate | `REDIS_URL` | change 'REDIS_PASSWORD' in docker/.env, delete the file, 'deploy.sh upgrade' |
| `sql_agent_db_password` | FR_READONLY_PASSWORD | face_recognition, migrate | `SQL_AGENT_DB_PASSWORD` | change 'FR_READONLY_PASSWORD' in docker/.env, delete the file, 'deploy.sh upgrade' |
| `webhook_api_keys` | random; the keys cameras present as Bearer tokens | face_recognition, migrate | `WEBHOOK_API_KEYS` | delete the file, re-run generate-secrets.sh, recreate the services |

### 13.3 Every production container: variable, where it is set, who reads it, how to change it

Values are what `docker compose config` renders. "compose line N" refers to `docker/docker-compose.prod.yml`. "Read by" lists the code that consumes the value (found by scanning for `settings.NAME`); for foreign images it names the program.


#### `face_recognition` — built from `docker/Dockerfile.gpu` · 71 variable(s)

| Variable | Value | Set in | Read by | Runtime change | Meaning |
|---|---|---|---|---|---|
| `ALLOW_CPU_FALLBACK` | `false` | compose (overlay) | backend/core/gpu_runtime.py, backend/security/config_guard.py | no — compose/.env only | Permit silent CPU inference when USE_GPU is set but CUDA is unavailable. Set False on real GPU deployments |
| `AUTH_ALLOWED_ORIGINS` | `https://face-detector.internal` | compose line 234 ← 'docker/.env' 'PUBLIC_ORIGIN' | backend/security/config_guard.py, backend/security/origins.py | no — compose/.env only | Comma-separated hosts allowed to submit credentials (the request Host is always allowed) |
| `AUTH_COOKIE_HOST_PREFIX` | `true` | compose line 233 (literal) | backend/auth/auth_security.py | no — compose/.env only | Use the __Host- cookie prefix when Secure is enabled |
| `AUTH_COOKIE_SAMESITE` | `strict` | compose line 232 (literal) | backend/auth/auth_security.py, backend/security/config_guard.py | no — compose/.env only | Auth cookie SameSite policy: lax, strict or none |
| `AUTH_COOKIE_SECURE` | `true` | compose line 231 (literal) | backend/auth/auth_security.py, backend/security/config_guard.py | no — compose/.env only | Set True in production (HTTPS). Enables the Secure flag and the __Host- cookie prefix |
| `AUTH_SAME_HOST_ORIGIN_TRUSTED` | `false` | compose line 235 (literal) | backend/security/config_guard.py, backend/security/origins.py | no — compose/.env only | Treat the request Host as a valid credential-submission origin. Set False in production once AUTH_ALLOWED_ORIG |
| `BACKUP_RETENTION_DAYS` | `14` | compose line 288 ← 'docker/.env' 'BACKUP_RETENTION_DAYS' | backend/routes/stats.py, scripts/backup/backup-loop.sh, scripts/backup/backup.sh | stored by the Settings page, but only a container recreate applies it | Days of backups to keep (also consumed by scripts/backup/backup.sh) |
| `BOOTSTRAP_ADMIN_PASSWORD_FILE` | `<secret>` | compose line 206 → secret file | config.py resolves it into 'BOOTSTRAP_ADMIN_PASSWORD' via backend/security/secrets.py:resolve_secret | no — compose/.env only | Path to a Docker secret holding the first-admin password |
| `BOOTSTRAP_ADMIN_REQUIRE_ROTATION` | `true` | compose line 207 (literal) | backend/services/bootstrap_admin.py | no — compose/.env only | Force a password change on the bootstrapped account's first login |
| `CACHE_TTL` | `3600` | compose line 228 (literal) | backend/core/cache_manager.py, backend/core/redis_cache.py, backend/routes/websocket.py | yes — Settings page, effective at once | Cache TTL for dashboard data in seconds (default: 3600 = 1 hour) |
| `CHROMADB_PATH` | `/app/database/chromadb` | compose line 323 (literal) | sql_agent/config.py | no — compose/.env only | config.py default "./sql_agent/chromadb_data" |
| `CONFIDENCE_THRESHOLD` | `0.5` | compose line 253 (literal) | backend/core/model_manager.py | yes — Settings page, stored; takes effect after the API restarts | config.py default 0.5 |
| `CORS_ORIGINS` | `https://face-detector.internal` | compose line 236 ← 'docker/.env' 'PUBLIC_ORIGIN' | backend/security/config_guard.py, backend/security/origins.py | no — compose/.env only | config.py default "*" |
| `DATABASE_URL_FILE` | `/run/secrets/database_url_app` | compose line 210 → secret file | config.py resolves it into 'DATABASE_URL' via backend/security/secrets.py:resolve_secret | no — compose/.env only | config.py default: empty |
| `DATA_RETENTION_DAYS` | `365` | compose line 286 (literal) | backend/core/data_retention.py, backend/lifespan.py, backend/routes/stats.py | yes — Settings page, effective at the next job run | config.py default 30 |
| `DB_HOST` | `postgres` | compose line 214 (literal) | scripts/maintenance/dedupe_identity_embeddings.py, scripts/maintenance/wipe_pipelines.py, sql_agent/config.py | no — compose/.env only | config.py default "postgres" |
| `DB_MAX_OVERFLOW` | `60` | compose line 217 (literal) | db_connection.py | no — compose/.env only | config.py default 100 |
| `DB_POOL_PRE_PING` | `true` | compose line 219 (literal) | db_connection.py | no — compose/.env only | config.py default True |
| `DB_POOL_RECYCLE` | `3600` | compose line 218 (literal) | db_connection.py | no — compose/.env only | config.py default 3600 |
| `DB_POOL_SIZE` | `30` | compose line 216 (literal) | backend/core/batch_search_service.py, backend/routes/admin_tutorial.py, db_connection.py | no — compose/.env only | config.py default 50 |
| `DB_PORT` | `5432` | compose line 215 (literal) | sql_agent/config.py | no — compose/.env only | config.py default 5432 |
| `DEBUG` | `false` | compose line 194 (literal) | backend/security/config_guard.py, db_connection.py | no — compose/.env only | config.py default False |
| `DETECTION_MODEL` | `/app/weights/det_10g.onnx` | compose line 250 (literal) | backend/core/enrollment_service.py, backend/core/model_manager.py, backend/lifespan.py | no — compose/.env only | config.py default "/app/weights/det_10g.onnx" |
| `ENABLE_API_DOCS` | `false` | compose line 237 (literal) | backend/main.py, backend/security/config_guard.py | no — compose/.env only | Serve /docs, /redoc and /openapi.json. Must be false in production |
| `ENVIRONMENT` | `production` | compose line 193 (literal) | backend/core/runtime_fingerprint.py, backend/routes/health.py, backend/security/config_guard.py (+7 more) | no — compose/.env only | config.py default "production" |
| `FACE_TRACKING_WINDOW_SECONDS` | `30` | compose line 276 (literal) | backend/config.py | no — compose/.env only | config.py default 0 |
| `HF_HOME` | `/home/appuser/.cache/huggingface` | compose line 300 (literal) | docker-entrypoint.sh | no — image / entrypoint variable | not an application setting |
| `HF_HUB_DISABLE_PROGRESS_BARS` | `1` | compose line 311 (literal) | huggingface_hub library | no — image / entrypoint variable | not an application setting |
| `HF_HUB_OFFLINE` | `1` | compose line 307 (literal) | huggingface_hub library | no — image / entrypoint variable | not an application setting |
| `HOST` | `0.0.0.0` | compose line 196 (literal) | gunicorn.conf.py, scripts/setup/start_production.sh | no — compose/.env only | config.py default "0.0.0.0" |
| `INFERENCE_WORKERS` | `3` | compose line 270 (literal) | backend/services/image_processing.py | yes — Settings page, stored; takes effect after the API restarts | config.py default 3 |
| `JWT_SECRET_KEY_FILE` | `<secret>` | compose line 205 → secret file | config.py resolves it into 'JWT_SECRET_KEY' via backend/security/secrets.py:resolve_secret | no — compose/.env only | config.py default: empty |
| `LOG_DIR` | `/var/log/face-recognition` | compose line 295 (literal) | backend/core/log_cleanup.py, backend/routes/logs.py, scripts/map_data/build_all.sh (+2 more) | no — compose/.env only | config.py default "/var/log/face-recognition" |
| `LOG_LEVEL` | `INFO` | compose line 294 (literal) | backend/ml/worker.py, gunicorn.conf.py, scripts/setup/start_production.sh (+1 more) | yes — Settings page, stored; takes effect after the API restarts | config.py default "INFO" |
| `MAX_CONCURRENT_INFERENCE` | `3` | compose line 271 (literal) | backend/services/image_processing.py | yes — Settings page, stored; takes effect after the API restarts | config.py default 3 |
| `MAX_QUEUE_SIZE` | `2000` | compose line 268 (literal) | backend/core/processing_queue.py, backend/routes/admin_tutorial.py | yes — Settings page, stored; takes effect after the API restarts | config.py default 10000 |
| `MAX_STORAGE_GB` | `5000` | compose line 293 (literal) | backend/core/data_retention.py | yes — Settings page, effective at once | config.py default 500 |
| `MIGRATIONS_EXPECTED_HEAD` | `fcc3d4e5f6a7` | compose line 248 ← 'docker/.env' 'MIGRATIONS_EXPECTED_HEAD' | backend/core/runtime_fingerprint.py, backend/utils/migrations.py | no — compose/.env only | Pin the expected Alembic head so a drifted schema cannot serve traffic |
| `MIGRATIONS_MODE` | `verify` | compose line 247 (literal) | backend/core/runtime_fingerprint.py, backend/lifespan.py, backend/utils/migrations.py | no — compose/.env only | config.py default "run" |
| `MLFLOW_HTTP_REQUEST_MAX_RETRIES` | `1` | compose line 306 (literal) | mlflow SDK (HTTP transport) | no — compose/.env only | MLflow SDK HTTP retries; process restart required |
| `MLFLOW_HTTP_REQUEST_TIMEOUT` | `10` | compose line 305 (literal) | mlflow SDK (HTTP transport) | no — compose/.env only | MLflow SDK HTTP timeout in seconds; process restart required |
| `NVIDIA_DRIVER_CAPABILITIES` | `compute,utility` | compose (overlay) | NVIDIA container runtime | no — image / entrypoint variable | not an application setting |
| `NVIDIA_VISIBLE_DEVICES` | `all` | compose (overlay) | NVIDIA container runtime | no — image / entrypoint variable | not an application setting |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | compose line 312 (literal) | backend/routes/health.py, sql_agent/config.py | no — compose/.env only | config.py default "http://ollama:11434" |
| `OLLAMA_MODEL` | `qwen2.5:7b` | compose line 321 (literal) | sql_agent/config.py | no — compose/.env only | config.py default "llama3.2:3b" |
| `OLLAMA_SQL_MODEL` | `hf.co/mradermacher/Arctic-Text2SQL-R1-7B-GGUF:Q4_K_M` | compose line 322 (literal) | sql_agent/config.py | no — compose/.env only | config.py default: empty |
| `PGVECTOR_HNSW_EF_CONSTRUCTION` | `128` | compose line 267 (literal) | backend/core/identity_index_pgvector.py, scripts/verify_pgvector_usage.py | yes — Settings page, stored; takes effect at the next index rebuild | HNSW efConstruction (build-time search width, 64-200, higher = better index quality) |
| `PGVECTOR_HNSW_M` | `32` | compose line 266 (literal) | backend/core/identity_index_pgvector.py | yes — Settings page, stored; takes effect at the next index rebuild | HNSW M parameter (connections per node, 16-64) |
| `PGVECTOR_INDEX_TYPE` | `hnsw` | compose line 260 (literal) | backend/core/identity_index_pgvector.py, scripts/verify_pgvector_usage.py | yes — Settings page, stored; takes effect at the next index rebuild | pgvector index type: 'hnsw' (fast, recommended) or 'ivfflat' (memory efficient) |
| `PORT` | `8000` | compose line 197 (literal) | gunicorn.conf.py, scripts/setup/start_production.sh | no — compose/.env only | config.py default 8000 |
| `POSTGRES_DB` | `face_recognition` | compose line 213 (literal) | scripts/maintenance/dedupe_identity_embeddings.py, scripts/maintenance/wipe_pipelines.py, sql_agent/config.py | no — compose/.env only | config.py default "face_recognition" |
| `POSTGRES_PASSWORD_FILE` | `<secret>` | compose line 212 → secret file | config.py resolves it into 'POSTGRES_PASSWORD' via backend/security/secrets.py:resolve_secret | no — compose/.env only | config.py default: empty |
| `POSTGRES_USER` | `fr_app` | compose line 211 (literal) | backend/security/config_guard.py, scripts/maintenance/dedupe_identity_embeddings.py, scripts/maintenance/wipe_pipelines.py (+1 more) | no — compose/.env only | config.py default "postgres" |
| `PYTHONUNBUFFERED` | `1` | compose line 195 (literal) | the Python interpreter | no — image / entrypoint variable | not an application setting |
| `QUEUE_WORKERS` | `15` | compose line 269 (literal) | backend/lifespan.py, scripts/setup/start_production.sh | yes — Settings page, stored; takes effect after the API restarts | config.py default 50 |
| `RECOGNITION_MODEL` | `/app/weights/w600k_r50.onnx` | compose line 251 (literal) | backend/core/advanced_search.py, backend/core/enrollment_service.py, backend/core/identity_service.py (+2 more) | no — compose/.env only | config.py default "/app/weights/w600k_r50.onnx" |
| `REDIS_MAX_CONNECTIONS` | `50` | compose line 227 (literal) | backend/core/cache_manager.py, backend/core/redis_cache.py | no — compose/.env only | config.py default 100 |
| `REDIS_URL_FILE` | `/run/secrets/redis_url` | compose line 221 → secret file | config.py resolves it into 'REDIS_URL' via backend/security/secrets.py:resolve_secret | no — compose/.env only | config.py default: empty |
| `SAVE_CROPPED_IMAGES` | `false` | compose line 278 (literal) | backend/services/image_processing.py | yes — Settings page, effective at once | Save cropped person images for debugging |
| `SAVE_WEBHOOK_IMAGES` | `false` | compose line 277 (literal) | backend/routes/webhook.py, backend/security/config_guard.py, backend/services/queue_worker.py | yes — Settings page, effective at once | Save all images received via webhook for debugging |
| `SIMILARITY_THRESHOLD` | `0.4` | compose line 252 (literal) | backend/core/advanced_search.py, backend/core/identity_index_pgvector.py, backend/core/identity_service.py (+3 more) | yes — Settings page, effective at once | config.py default 0.4 |
| `SQL_AGENT_DB_PASSWORD_FILE` | `<secret>` | compose line 244 → secret file | config.py resolves it into 'SQL_AGENT_DB_PASSWORD' via backend/security/secrets.py:resolve_secret | no — compose/.env only | Path to a Docker secret holding the read-only role's password |
| `SQL_AGENT_DB_USER` | `fr_readonly` | compose line 243 (literal) | backend/security/config_guard.py, sql_agent/config.py | no — compose/.env only | Read-only role used to execute generated SQL. Must differ from POSTGRES_USER |
| `STORAGE_DIR` | `/app/storage` | compose line 258 (literal) | backend/config.py, backend/core/data_retention.py, backend/core/enrollment_service.py (+15 more) | no — compose/.env only | config.py default "/app/storage" |
| `TASK_HISTORY_RETENTION_DAYS` | `365` | compose line 287 (literal) | backend/core/data_retention.py, backend/routes/stats.py | yes — Settings page, effective at the next job run | Days to retain background-task history records. Default: 30 |
| `TRANSFORMERS_OFFLINE` | `1` | compose line 308 (literal) | transformers library | no — image / entrypoint variable | not an application setting |
| `USE_GPU` | `true` | compose line 202 (literal) | backend/core/gpu_runtime.py, backend/core/operational_metrics.py, backend/core/runtime_fingerprint.py (+2 more) | no — compose/.env only | config.py default False |
| `VECTOR_BACKEND` | `pgvector` | compose line 259 (literal) | backend/core/advanced_search.py, backend/core/enrollment_service.py, backend/core/identity_clustering.py (+12 more) | yes — Settings page, stored; takes effect after the API restarts | Vector search backend: 'pgvector' (RECOMMENDED for production) or 'faiss' (faster but requires sync logic) |
| `WEBHOOK_API_KEYS_FILE` | `<secret>` | compose line 285 → secret file | config.py resolves it into 'WEBHOOK_API_KEYS' via backend/security/secrets.py:resolve_secret | no — compose/.env only | Path to a Docker secret holding WEBHOOK_API_KEYS |
| `WEBHOOK_AUTH_MODE` | `enforce` | compose line 284 (literal) | backend/security/config_guard.py, backend/security/webhook_auth.py | no — compose/.env only | enforce \| log_only \| off. log_only exists purely to migrate a fleet of already-deployed cameras; production r |
| `WORKERS` | `1` | compose line 201 (literal) | backend/core/runtime_fingerprint.py, backend/lifespan.py, backend/security/config_guard.py (+2 more) | stored by the Settings page, but only a container recreate applies it | config.py default 4 |

#### `ml_worker` — built from `docker/Dockerfile.gpu` · 26 variable(s)

| Variable | Value | Set in | Read by | Runtime change | Meaning |
|---|---|---|---|---|---|
| `CONFIG_PREFLIGHT` | `0` | compose line 417 (literal) | docker-entrypoint.sh | no — image / entrypoint variable | not an application setting |
| `DATABASE_URL_FILE` | `/run/secrets/database_url_app` | compose line 420 → secret file | config.py resolves it into 'DATABASE_URL' via backend/security/secrets.py:resolve_secret | no — compose/.env only | config.py default: empty |
| `DB_HOST` | `postgres` | compose line 424 (literal) | scripts/maintenance/dedupe_identity_embeddings.py, scripts/maintenance/wipe_pipelines.py, sql_agent/config.py | no — compose/.env only | config.py default "postgres" |
| `DB_MAX_OVERFLOW` | `5` | compose line 427 (literal) | db_connection.py | no — compose/.env only | config.py default 100 |
| `DB_POOL_PRE_PING` | `true` | compose line 428 (literal) | db_connection.py | no — compose/.env only | config.py default True |
| `DB_POOL_SIZE` | `5` | compose line 426 (literal) | backend/core/batch_search_service.py, backend/routes/admin_tutorial.py, db_connection.py | no — compose/.env only | config.py default 50 |
| `DB_PORT` | `5432` | compose line 425 (literal) | sql_agent/config.py | no — compose/.env only | config.py default 5432 |
| `ENVIRONMENT` | `production` | compose line 412 (literal) | backend/core/runtime_fingerprint.py, backend/routes/health.py, backend/security/config_guard.py (+7 more) | no — compose/.env only | config.py default "production" |
| `HF_HOME` | `/home/appuser/.cache/huggingface` | compose line 435 (literal) | docker-entrypoint.sh | no — image / entrypoint variable | not an application setting |
| `HF_HUB_DISABLE_PROGRESS_BARS` | `1` | compose line 444 (literal) | huggingface_hub library | no — image / entrypoint variable | not an application setting |
| `HF_HUB_OFFLINE` | `1` | compose line 442 (literal) | huggingface_hub library | no — image / entrypoint variable | not an application setting |
| `LOG_DIR` | `/var/log/face-recognition` | compose line 434 (literal) | backend/core/log_cleanup.py, backend/routes/logs.py, scripts/map_data/build_all.sh (+2 more) | no — compose/.env only | config.py default "/var/log/face-recognition" |
| `LOG_LEVEL` | `INFO` | compose line 433 (literal) | backend/ml/worker.py, gunicorn.conf.py, scripts/setup/start_production.sh (+1 more) | yes — Settings page, stored; takes effect after the API restarts | config.py default "INFO" |
| `MIGRATIONS_EXPECTED_HEAD` | `fcc3d4e5f6a7` | compose line 419 ← 'docker/.env' 'MIGRATIONS_EXPECTED_HEAD' | backend/core/runtime_fingerprint.py, backend/utils/migrations.py | no — compose/.env only | Pin the expected Alembic head so a drifted schema cannot serve traffic |
| `MIGRATIONS_MODE` | `verify` | compose line 418 (literal) | backend/core/runtime_fingerprint.py, backend/lifespan.py, backend/utils/migrations.py | no — compose/.env only | config.py default "run" |
| `MLFLOW_HTTP_REQUEST_MAX_RETRIES` | `1` | compose line 441 (literal) | mlflow SDK (HTTP transport) | no — compose/.env only | MLflow SDK HTTP retries; process restart required |
| `MLFLOW_HTTP_REQUEST_TIMEOUT` | `10` | compose line 440 (literal) | mlflow SDK (HTTP transport) | no — compose/.env only | MLflow SDK HTTP timeout in seconds; process restart required |
| `ML_ARTIFACT_DIR` | `/app/models/ml` | compose line 432 (literal) | backend/ml/capabilities.py, backend/ml/dataset_builder.py, backend/ml/mlflow_tracking.py (+4 more) | no — compose/.env only | Approved internal directory for ML artifacts; loads outside this prefix are refused. Default: models/ml |
| `ML_WORKER_ID` | `ml-worker-production-primary` | compose line 431 (literal) | backend/ml/worker.py | no — compose/.env only | Stable identity for the ML worker, so a container replacement updates one heartbeat row instead of creating a |
| `NVIDIA_DRIVER_CAPABILITIES` | `compute,utility` | compose (overlay) | NVIDIA container runtime | no — image / entrypoint variable | not an application setting |
| `NVIDIA_VISIBLE_DEVICES` | `all` | compose (overlay) | NVIDIA container runtime | no — image / entrypoint variable | not an application setting |
| `POSTGRES_DB` | `face_recognition` | compose line 423 (literal) | scripts/maintenance/dedupe_identity_embeddings.py, scripts/maintenance/wipe_pipelines.py, sql_agent/config.py | no — compose/.env only | config.py default "face_recognition" |
| `POSTGRES_PASSWORD_FILE` | `<secret>` | compose line 422 → secret file | config.py resolves it into 'POSTGRES_PASSWORD' via backend/security/secrets.py:resolve_secret | no — compose/.env only | config.py default: empty |
| `POSTGRES_USER` | `fr_app` | compose line 421 (literal) | backend/security/config_guard.py, scripts/maintenance/dedupe_identity_embeddings.py, scripts/maintenance/wipe_pipelines.py (+1 more) | no — compose/.env only | config.py default "postgres" |
| `PYTHONUNBUFFERED` | `1` | compose line 413 (literal) | the Python interpreter | no — image / entrypoint variable | not an application setting |
| `TRANSFORMERS_OFFLINE` | `1` | compose line 443 (literal) | transformers library | no — image / entrypoint variable | not an application setting |

#### `migrate` — built from `docker/Dockerfile.cpu` · 18 variable(s)

| Variable | Value | Set in | Read by | Runtime change | Meaning |
|---|---|---|---|---|---|
| `AUTH_ALLOWED_ORIGINS` | `https://face-detector.internal` | compose line 150 ← 'docker/.env' 'PUBLIC_ORIGIN' | backend/security/config_guard.py, backend/security/origins.py | no — compose/.env only | Comma-separated hosts allowed to submit credentials (the request Host is always allowed) |
| `AUTH_COOKIE_SECURE` | `true` | compose line 152 (literal) | backend/auth/auth_security.py, backend/security/config_guard.py | no — compose/.env only | Set True in production (HTTPS). Enables the Secure flag and the __Host- cookie prefix |
| `AUTH_SAME_HOST_ORIGIN_TRUSTED` | `false` | compose line 151 (literal) | backend/security/config_guard.py, backend/security/origins.py | no — compose/.env only | Treat the request Host as a valid credential-submission origin. Set False in production once AUTH_ALLOWED_ORIG |
| `CONFIG_PREFLIGHT` | `0` | compose line 169 (literal) | docker-entrypoint.sh | no — image / entrypoint variable | not an application setting |
| `CORS_ORIGINS` | `https://face-detector.internal` | compose line 149 ← 'docker/.env' 'PUBLIC_ORIGIN' | backend/security/config_guard.py, backend/security/origins.py | no — compose/.env only | config.py default "*" |
| `DATABASE_URL_FILE` | `/run/secrets/database_url_migrator` | compose line 145 → secret file | config.py resolves it into 'DATABASE_URL' via backend/security/secrets.py:resolve_secret | no — compose/.env only | config.py default: empty |
| `ENABLE_API_DOCS` | `false` | compose line 153 (literal) | backend/main.py, backend/security/config_guard.py | no — compose/.env only | Serve /docs, /redoc and /openapi.json. Must be false in production |
| `ENVIRONMENT` | `production` | compose line 142 (literal) | backend/core/runtime_fingerprint.py, backend/routes/health.py, backend/security/config_guard.py (+7 more) | no — compose/.env only | config.py default "production" |
| `JWT_SECRET_KEY_FILE` | `<secret>` | compose line 154 → secret file | config.py resolves it into 'JWT_SECRET_KEY' via backend/security/secrets.py:resolve_secret | no — compose/.env only | config.py default: empty |
| `MIGRATIONS_EXPECTED_HEAD` | `fcc3d4e5f6a7` | compose line 144 ← 'docker/.env' 'MIGRATIONS_EXPECTED_HEAD' | backend/core/runtime_fingerprint.py, backend/utils/migrations.py | no — compose/.env only | Pin the expected Alembic head so a drifted schema cannot serve traffic |
| `MIGRATIONS_MODE` | `run` | compose line 143 (literal) | backend/core/runtime_fingerprint.py, backend/lifespan.py, backend/utils/migrations.py | no — compose/.env only | config.py default "run" |
| `POSTGRES_PASSWORD_FILE` | `<secret>` | compose line 147 → secret file | config.py resolves it into 'POSTGRES_PASSWORD' via backend/security/secrets.py:resolve_secret | no — compose/.env only | config.py default: empty |
| `POSTGRES_USER` | `fr_migrator` | compose line 146 (literal) | backend/security/config_guard.py, scripts/maintenance/dedupe_identity_embeddings.py, scripts/maintenance/wipe_pipelines.py (+1 more) | no — compose/.env only | config.py default "postgres" |
| `REDIS_URL_FILE` | `/run/secrets/redis_url` | compose line 148 → secret file | config.py resolves it into 'REDIS_URL' via backend/security/secrets.py:resolve_secret | no — compose/.env only | config.py default: empty |
| `SQL_AGENT_DB_PASSWORD_FILE` | `<secret>` | compose line 159 → secret file | config.py resolves it into 'SQL_AGENT_DB_PASSWORD' via backend/security/secrets.py:resolve_secret | no — compose/.env only | Path to a Docker secret holding the read-only role's password |
| `SQL_AGENT_DB_USER` | `fr_readonly` | compose line 158 (literal) | backend/security/config_guard.py, sql_agent/config.py | no — compose/.env only | Read-only role used to execute generated SQL. Must differ from POSTGRES_USER |
| `WEBHOOK_API_KEYS_FILE` | `<secret>` | compose line 160 → secret file | config.py resolves it into 'WEBHOOK_API_KEYS' via backend/security/secrets.py:resolve_secret | no — compose/.env only | Path to a Docker secret holding WEBHOOK_API_KEYS |
| `WORKERS` | `1` | compose line 161 (literal) | backend/core/runtime_fingerprint.py, backend/lifespan.py, backend/security/config_guard.py (+2 more) | stored by the Settings page, but only a container recreate applies it | config.py default 4 |

#### `postgres` — image `pgvector/pgvector:pg15` · 4 variable(s)

| Variable | Value | Set in | Read by | Runtime change | Meaning |
|---|---|---|---|---|---|
| `POSTGRES_DB` | `face_recognition` | compose line 65 (literal) | scripts/maintenance/dedupe_identity_embeddings.py, scripts/maintenance/wipe_pipelines.py, sql_agent/config.py | no — compose/.env only | config.py default "face_recognition" |
| `POSTGRES_INITDB_ARGS` | `-E UTF8` | compose line 66 (literal) | postgres image initdb | no — image / entrypoint variable | not an application setting |
| `POSTGRES_PASSWORD` | `<secret>` | compose line 64 ← 'docker/.env' 'POSTGRES_SUPERUSER_PASSWORD' | backend/security/config_guard.py, scripts/maintenance/dedupe_identity_embeddings.py, scripts/maintenance/wipe_pipelines.py (+1 more) | no — compose/.env only | config.py default "admin" |
| `POSTGRES_USER` | `postgres` | compose line 63 (literal) | backend/security/config_guard.py, scripts/maintenance/dedupe_identity_embeddings.py, scripts/maintenance/wipe_pipelines.py (+1 more) | no — compose/.env only | config.py default "postgres" |

#### `redis` — image `redis:7-alpine` · 1 variable(s)

| Variable | Value | Set in | Read by | Runtime change | Meaning |
|---|---|---|---|---|---|
| `REDISCLI_AUTH` | `<secret>` | compose line 97 ← 'docker/.env' 'REDIS_PASSWORD' | redis-cli in the redis healthcheck | no — image / entrypoint variable | not an application setting |

#### `ollama` — image `ollama/ollama:latest` · 3 variable(s)

| Variable | Value | Set in | Read by | Runtime change | Meaning |
|---|---|---|---|---|---|
| `OLLAMA_HOST` | `0.0.0.0:11434` | compose (overlay) | ollama server | no — image / entrypoint variable | not an application setting |
| `OLLAMA_KEEP_ALIVE` | `-1` | compose (overlay) | ollama server (how long a model stays loaded) | no — image / entrypoint variable | not an application setting |
| `OLLAMA_MODELS` | `/root/.ollama/models` | compose (overlay) | ollama server (model store path) | no — image / entrypoint variable | not an application setting |

#### `backup` — image `postgres:15-alpine` · 5 variable(s)

| Variable | Value | Set in | Read by | Runtime change | Meaning |
|---|---|---|---|---|---|
| `BACKUP_INTERVAL_SECONDS` | `86400` | compose line 599 ← 'docker/.env' 'BACKUP_INTERVAL_SECONDS' | backend/routes/stats.py, scripts/backup/backup-loop.sh | stored by the Settings page, but only a container recreate applies it | Interval between backup runs (also consumed by backup-loop.sh) |
| `BACKUP_RETENTION_DAYS` | `14` | compose line 600 ← 'docker/.env' 'BACKUP_RETENTION_DAYS' | backend/routes/stats.py, scripts/backup/backup-loop.sh, scripts/backup/backup.sh | stored by the Settings page, but only a container recreate applies it | Days of backups to keep (also consumed by scripts/backup/backup.sh) |
| `PGDATABASE` | `face_recognition` | compose line 598 (literal) | scripts/backup/backup.sh, scripts/backup/restore.sh | no — image / entrypoint variable | not an application setting |
| `PGHOST` | `postgres` | compose line 592 (literal) | libpq — pg_dump in scripts/backup/backup.sh | no — image / entrypoint variable | not an application setting |
| `PGUSER` | `fr_backup` | compose line 593 (literal) | libpq — pg_dump in scripts/backup/backup.sh | no — image / entrypoint variable | not an application setting |

#### `grafana` — image `grafana/grafana:13.2.0` · 4 variable(s)

| Variable | Value | Set in | Read by | Runtime change | Meaning |
|---|---|---|---|---|---|
| `GF_AUTH_ANONYMOUS_ENABLED` | `false` | compose line 648 (literal) | grafana image | no — image / entrypoint variable | not an application setting |
| `GF_SECURITY_ADMIN_PASSWORD` | `<secret>` | compose line 646 ← 'docker/.env' 'GRAFANA_ADMIN_PASSWORD' | grafana image | no — image / entrypoint variable | not an application setting |
| `GF_SERVER_ROOT_URL` | `http://localhost:3000` | compose line 649 ← 'docker/.env' 'GRAFANA_ROOT_URL' | grafana image | no — image / entrypoint variable | not an application setting |
| `GF_USERS_ALLOW_SIGN_UP` | `false` | compose line 647 (literal) | grafana image | no — image / entrypoint variable | not an application setting |

#### `prometheus` — image `prom/prometheus:v3.14.0` · 0 variable(s)

(no environment variables; configured by mounted files — see §3 for nginx TLS, `docker/prometheus/`, `map-data/` for martin)

#### `nginx` — image `nginx:alpine` · 0 variable(s)

(no environment variables; configured by mounted files — see §3 for nginx TLS, `docker/prometheus/`, `map-data/` for martin)

#### `martin` — image `ghcr.io/maplibre/martin:1.13.0` · 0 variable(s)

(no environment variables; configured by mounted files — see §3 for nginx TLS, `docker/prometheus/`, `map-data/` for martin)

**Regenerate** after any compose or `.env` change: `sudo docker compose --project-directory docker -f docker/docker-compose.prod.yml -f docker/docker-compose.prod.gpu.yml -f docker/gpu-allocation.generated.yml config`, then rebuild these tables from it (the generator scans `settings.NAME` reads across the code base and the apply modes in `backend/core/runtime_settings.py`).

## 14. Volumes and mounts — what every container can read and write (verified 2026-09-07)

Everything below was read from the running containers (`docker inspect`) and then **probed**: each read-only mount was written to from inside the container (as that container's own user) and refused the write; each writable application volume accepted a write-and-delete as uid 1000; engine data directories are owned by their engines. 47 mounts, 0 problems. `tests/test_volume_contract.py` pins the compose side, `./deploy.sh paths` the host ownership.

### 14.1 Named volumes (`face_detector_prod_<name>`, driver local, survive restart/upgrade/rollback/uninstall)

| Volume | Holds | Mounted by (container:path mode) | Size now | Backed up by |
|---|---|---|---|---|
| `postgres_data` | PostgreSQL cluster: every table, pgvector embeddings, users, settings, task history | postgres-1:/var/lib/postgresql/data (rw) | 81.09MB | `deploy.sh backup` and the daily backup job: `pg_dump --format=custom`, compressed, checksummed; restore with `sudo ./deploy.sh restore <id> --force` |
| `storage_data` | face crops, snapshots and uploaded enrolment photos (what `STORAGE_DIR` points at) | backup-1:/data/storage (ro); face_recognition-1:/app/storage (rw) | 9.156kB | daily backup job: `storage.tar.gz` (mounted read-only at /data/storage) |
| `face_database_data` | local index files and the embedded Chroma store used by the chatbot knowledge base (`/app/database`) | backup-1:/data/database (ro); face_recognition-1:/app/database (rw) | 7.414MB | daily backup job: `artifacts.tar.gz` (mounted read-only at /data/database) |
| `ml_artifacts_data` | trained ML models, candidates and datasets shared by the API and the ML worker | backup-1:/data/ml (ro); face_recognition-1:/app/models/ml (rw); ml_worker-1:/app/models/ml (rw) | 0B | daily backup job: `ml_artifacts.tar.gz` (mounted read-only at /data/ml) |
| `logs_data` | application log files of the API and the ML worker (pruned by log cleanup every 48 h) | face_recognition-1:/var/log/face-recognition (rw); ml_worker-1:/var/log/face-recognition (rw) | 4.684MB | not backed up — operational logs |
| `chromadb_cache` | Chroma's model cache: the ONNX MiniLM embedding model the offline policy verifies at boot | face_recognition-1:/home/appuser/.cache/chroma (rw) | 174.5MB | not backed up — recreated from the image on first mount |
| `hf_cache_data` | huggingface / sentence-transformers cache (query-history embeddings model) | face_recognition-1:/home/appuser/.cache/huggingface (rw); ml_worker-1:/home/appuser/.cache/huggingface (rw) | 91.62MB | not backed up — rebuildable cache |
| `redis_data` | Redis persistence for the cache and queues | redis-1:/data (rw) | 23.2kB | not backed up — cache, rebuilt at runtime |
| `ollama_models` | the LLM weights: qwen2.5:7b, Arctic-Text2SQL, qwen2.5:1.5b | ollama-1:/root/.ollama (rw) | 10.35GB | not in the daily backup (10 GB): re-import from the offline bundle or `ollama pull` on a connected host |
| `backup_data` | the backup job's output: one directory per run with the dump, the tarballs and SHA256SUMS; pruned after `BACKUP_RETENTION_DAYS` | backup-1:/backups (rw); face_recognition-1:/backups (ro) | 19.45MB | **this is the volume to copy off the host** (§9) |
| `prometheus_data` | metrics time series (retention set in `monitoring/prometheus.yml`) | prometheus-1:/prometheus (rw) | 38.85MB | not backed up |
| `grafana_data` | Grafana state (sessions, preferences); dashboards themselves are provisioned from the repo | grafana-1:/var/lib/grafana (rw) | 1.651MB | not backed up |
| `vllm_models` | (profile `vllm` only) vLLM model store | — (profile-gated service not running) | not created (profile) | — |
| `milvus_etcd` | (profile `milvus` only) | — (profile-gated service not running) | not created (profile) | — |
| `milvus_minio` | (profile `milvus` only) | — (profile-gated service not running) | not created (profile) | — |
| `milvus_data` | (profile `milvus` only) | — (profile-gated service not running) | not created (profile) | — |

### 14.2 Bind mounts (host paths from the checkout)

| Host path | Purpose | Mounted by | Expected owner / mode on the host (`./deploy.sh paths`) |
|---|---|---|---|
| `./secrets` | Docker secrets, one file each (§13.2) | backup-1:/run/secrets/backup_db_password (ro); face_recognition-1:/run/secrets/bootstrap_admin_password (ro); face_recognition-1:/run/secrets/database_url_app (ro); face_recognition-1:/run/secrets/jwt_secret (ro); face_recognition-1:/run/secrets/postgres_password_app (ro); face_recognition-1:/run/secrets/redis_url (ro); face_recognition-1:/run/secrets/sql_agent_db_password (ro); face_recognition-1:/run/secrets/webhook_api_keys (ro); ml_worker-1:/run/secrets/database_url_app (ro); ml_worker-1:/run/secrets/postgres_password_app (ro) | 0440 root:1000, directory 0750 |
| `./scripts/backup` | `backup.sh` and `backup-loop.sh` (bind-mounted, so a fix needs no rebuild) | backup-1:/scripts (ro) | repo files, read-only |
| `./certs` | TLS: `server.crt`/`server.key` for nginx, `internal-ca.crt` for clients | face_recognition-1:/etc/nginx/certs (ro); nginx-1:/etc/nginx/certs (ro) | 0755 root; `server.key` and `internal-ca.key` 0600 root — move the CA key offline (§3) |
| `./map-data` | offline basemap archives (.mbtiles), fonts and the content-verdict ledger | face_recognition-1:/app/map-data (ro); martin-1:/map-data (ro) | 0755 1000:1000; `map-data/production` read by martin |
| `./weights` | SCRFD + ArcFace ONNX weights, verified against `weights/WEIGHTS_MANIFEST.json` by stage 08 | face_recognition-1:/app/weights (ro) | 0755 1000:1000 (files 0755) |
| `./monitoring/grafana/provisioning` | datasources + dashboard providers | grafana-1:/etc/grafana/provisioning (ro) | repo files, read-only |
| `./monitoring/grafana/dashboards` | dashboard JSON | grafana-1:/var/lib/grafana/dashboards (ro) | repo files, read-only |
| `./config/martin.yaml` | Martin tile-server configuration | martin-1:/config/martin.yaml (ro) | repo file, read-only |
| `./nginx.prod.conf` | the nginx configuration (TLS, security headers, proxy rules) | nginx-1:/etc/nginx/nginx.conf (ro) | repo file, read-only |
| `./frontend` | the web UI, served by nginx straight from the checkout (no rebuild for HTML/JS/CSS changes) | nginx-1:/usr/share/nginx/html/frontend (ro) | repo files, read-only |
| `./icons` | static icons served by nginx | nginx-1:/usr/share/nginx/html/icons (ro) | repo files, read-only |
| `./init-db.sql` | first-boot database initialisation (extensions) | postgres-1:/docker-entrypoint-initdb.d/init-db.sql (ro) | repo file, read-only |
| `./db` | `db/roles.sql` — least-privilege database roles applied by deploy.sh | postgres-1:/db (ro) | repo files, read-only |
| `./monitoring/alerts` | alert rules | prometheus-1:/etc/prometheus/alerts (ro) | repo files, read-only |
| `./monitoring/prometheus.yml` | scrape configuration | prometheus-1:/etc/prometheus/prometheus.yml (ro) | repo file, read-only |
| `./docker/redis/users.acl` | Redis ACL with the app and monitor users (hashed passwords) | redis-1:/etc/redis/users.acl (ro) | 0640 999:1000 |

### 14.3 Per container


The one-shot `migrate` job mounts only its six secret files (§13.2); it has no data volume, which is why it must not run the inference-artifact preflight (§12).

### 14.4 Notes and how to verify yourself

- **`backup` carries an anonymous volume at `/var/lib/postgresql/data`.** It runs the postgres image only for `pg_dump`, and that image declares a data volume; the anonymous volume stays empty. Harmless; a `tmpfs` on that path would silence it.
- **`ml_artifacts_data` is 0 B** until the first ML training run; `chromadb_cache` (~175 MB) is where the offline embedding model lives; `ollama_models` (~10 GB) is the largest thing on the host after the images.
- **Networks:** `data` and `monitoring` are `internal: true` — postgres, redis, prometheus and grafana have no route off the host; only nginx publishes ports 80/443.
- List a container's mounts: `docker inspect <container> --format '{{range .Mounts}}{{.Type}} {{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}} -> {{.Destination}} rw={{.RW}}{{"\n"}}{{end}}'`
- Prove a read-only mount is read-only: `docker exec -u 1000:1000 face_detector_prod-face_recognition-1 sh -c 'touch /app/weights/x'` must fail with *Read-only file system*.
- Prove a data volume is writable by the app: `docker exec -u 1000:1000 face_detector_prod-face_recognition-1 sh -c 'touch /app/storage/.probe && rm /app/storage/.probe'` must succeed silently.
- Sizes: `sudo docker system df -v | grep face_detector_prod_`.

## 15. The SQL bot (chat agent): how it runs, and its variables

The "SQL bot" is the chat assistant on the Tracking pages. It turns a question
into a **read-only** SQL query against this deployment's own PostgreSQL,
executes it as a dedicated SELECT-only role, and writes the answer as text, a
table, a chart or a PDF/Word export. In production every part of it is local:
two Ollama models on the GPU, an embedded Chroma knowledge base, the internal
database. Nothing leaves the box; the offline policy (§10) refuses to boot
otherwise. Live check today: `/api/sql-agent/health` → `operational`
(model ready, database ready, history ready).

### 15.0 The four models, at a glance

| Role in the bot | Model | Runs where | Size | Setting |
|---|---|---|---|---|
| Reasoning, chat, tool selection, reading each turn (interpreter), writing the answer | `qwen2.5:7b` — Qwen2.5 7B Instruct, 4-bit | Ollama container, GPU | 4.7 GB | `OLLAMA_MODEL` (`OLLAMA_INTERPRETER_MODEL` empty = same model) |
| SQL generation (`generate_sql`, `modify_sql` nodes) | `hf.co/mradermacher/Arctic-Text2SQL-R1-7B-GGUF:Q4_K_M` — Snowflake Arctic-Text2SQL-R1 7B, GGUF 4-bit | Ollama container, GPU | 4.7 GB | `OLLAMA_SQL_MODEL` |
| Embeddings for the knowledge base (similar verified question→SQL examples in Chroma) | `all-MiniLM-L6-v2` as ONNX — Chroma's default embedding function, 384 dimensions | API container, CPU (onnxruntime); file in the `chromadb_cache` volume | 90 MB | `EMBEDDING_PROVIDER=local`, `EMBEDDING_MODEL_PATH` |
| Embeddings for query-history similarity ("questions like this one you asked before") | `sentence-transformers/all-MiniLM-L6-v2` — same model, PyTorch format, loaded offline-only | API container, CPU (torch CPU); cached in `hf_cache_data` | 90 MB | `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` |

Also on disk but unused: `qwen2.5:1.5b` (986 MB). `qwen2.5:7b` was chosen over
it because it passed 6 of 6 native tool-call tests against 3 of 6. The two
embedding entries are one model in two formats; neither uses the GPU, and
embedding a sentence takes milliseconds. The two LLMs share the GPU with face
recognition through Ollama's device reservation (face recognition itself uses
SCRFD and ArcFace, unrelated to the bot). Presence is enforced: the config
guard verifies the MiniLM ONNX file at boot and `deploy.sh` stage 13 verifies
both Ollama models against `ollama list`, so a host cannot start with one
missing.

### 15.1 How a question is answered

```
browser ──HTTPS──▶ nginx ──▶ API  POST /api/sql-agent/query  (or /query/stream, or WS /ws/sql-agent)
                                  │  login required + the CHATBOT_USE capability (admin grants/withdraws it per user)
                                  ▼
   ingest_query ─▶ detect_malicious_intent ─▶ plan_action (interpreter reads the turn: what, shape, format)
        │                                          │
        ▼                                          ▼
   chat_response (small talk / help)      check_schema ─▶ retrieve_examples  ◀── Chroma knowledge base
   with OLLAMA_MODEL                                          │                   (~1 070 verified question→SQL seeds,
                                                              ▼                    MiniLM embeddings, RAG_TOP_K=5)
                                                      generate_sql  ◀── OLLAMA_SQL_MODEL (Arctic-Text2SQL)
                                                              │
                                                              ▼
                                                      validate_and_fix_sql  ── sqlglot AST guard (§15.2)
                                                              │
                                                              ▼
                                                      execute_sql  ── role fr_readonly · default_transaction_read_only=on
                                                              │        · statement_timeout ≤ 30 s · 500-row cap
                                                              ▼
                                                      observe_and_replan (≤ SQL_AGENT_MAX_REPLANS) ─▶ story_response
                                                                                                    with OLLAMA_MODEL
                                                              ▼
                                                      render_artifact / translate_artifact (table, chart, PDF, Word)
```

The graph lives in `sql_agent/graph.py`; the nodes are in `sql_agent/tools/agent_tools.py`.
Every question runs under the budgets in §15.3: the run stops with a clear
message when it exceeds them instead of looping.

### 15.2 What makes it safe

| Layer | Rule | Where |
|---|---|---|
| Who may ask | a logged-in user holding the `CHATBOT_USE` capability; revoked instantly when an admin withdraws it (`require_chatbot_access`) | `backend/auth/auth_service.py` |
| What SQL is allowed | `SELECT` only; table allow-list; ≤ 10 joins; subquery depth ≤ 5; `LIMIT` enforced (max 500 rows); functions policy; no `EXPLAIN` for users | `sql_agent/security/sql_guard.py` (sqlglot AST, not regex) |
| Whose data | the caller's camera scope is injected into every scoped table; administrators are unrestricted; an EMPTY scope fails closed (refuse, never widen) | `sql_guard.py` camera scope |
| Which role executes | `SQL_AGENT_DB_USER=fr_readonly`, password from a secret file; the connection is opened with `default_transaction_read_only=on` and a `statement_timeout` derived from the remaining run budget | `sql_agent/database.py`, `db/roles.sql` |
| Boot-time guard | refuses a shared or superuser role for the bot, any external LLM/embedding/MCP/vector/STT endpoint, the hosted NIM provider, the Opik tracer | `backend/security/config_guard.py`, `offline_policy.py` |
| Prompt injection | `detect_malicious_intent` node before any planning; the model never sees credentials; generated SQL is validated regardless of what the model wrote | `agent_tools.py` |
| Audit | every request has an id; `GET /api/sql-agent/requests/{id}/trace` shows the steps; SQL-agent audit rows are kept `AUDIT_LOG_RETENTION_DAYS` | `sql_agent/api/routes.py` |

### 15.3 Variables the bot uses, with production values

**Models and inference**

| Variable | Production value | Set in | Runtime change | Meaning |
|---|---|---|---|---|
| `LLM_PROVIDER` | `ollama` | config.py default | no | ollama \| vllm \| nim_local \| nvidia_cloud |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | compose | no | — |
| `OLLAMA_MODEL` | `qwen2.5:7b` | compose | no | chat, tool selection and turn reading (interpreter) — qwen2.5:7b answered 6/6 native tool calls |
| `OLLAMA_SQL_MODEL` | `hf.co/mradermacher/Arctic-Text2SQL-R1-7B-GGUF:Q4_K_M` | compose | no | the SQL specialist used only by the generate/modify-SQL nodes |
| `OLLAMA_INTERPRETER_MODEL` | (empty) | config.py default | no | empty = OLLAMA_MODEL; a larger reader may be set if short follow-ups get misread |
| `OLLAMA_TEMPERATURE` | `0.1` | config.py default | no | low = deterministic SQL |
| `OLLAMA_TIMEOUT` | `120` | config.py default | no | seconds per model call |
| `LLM_BASE_URL` | (empty) | config.py default | no | OpenAI-compatible base URL for vllm / nim_local, e.g. http://vllm:8000/v1 |
| `LLM_MODEL` | (empty) | config.py default | no | Model id served at LLM_BASE_URL (vllm / nim_local) |
| `LLM_SQL_MODEL` | (empty) | config.py default | no | Optional SQL specialist at LLM_BASE_URL; empty = LLM_MODEL |
| `LLM_API_KEY_FILE` | (empty) | config.py default | no | Docker secret holding LLM_API_KEY |

**Database access (generated SQL)**

| Variable | Production value | Set in | Runtime change | Meaning |
|---|---|---|---|---|
| `SQL_AGENT_DB_USER` | `fr_readonly` | compose | no | MUST be a dedicated SELECT-only role, different from POSTGRES_USER; the config guard refuses a shared or superuser role (rule SQL_AGENT_DB_ROLE_SHARED) |
| `SQL_AGENT_DB_PASSWORD` | `/run/secrets/…` via `SQL_AGENT_DB_PASSWORD_FILE` | compose → secret file | no | comes from the secret file; never in the environment |
| `SQL_AGENT_MAX_EXECUTION_RETRIES` | `1` | config.py default | yes, after API restart | Retries of the SAME SQL after a TRANSIENT database error (dropped connection, pool timeout). Infrastructure, |

**Knowledge base and retrieval**

| Variable | Production value | Set in | Runtime change | Meaning |
|---|---|---|---|---|
| `EMBEDDING_PROVIDER` | `local` | config.py default | no | local (bundled ONNX MiniLM through Chroma) \| a local service name; remote embedding APIs are refused offline |
| `EMBEDDING_MODEL_PATH` | `/home/appuser/.cache/chroma/onnx_models/` | config.py default | no | the ONNX MiniLM in the 'chromadb_cache' volume; verified at boot |
| `EMBEDDING_BASE_URL` | (empty) | config.py default | no | Base URL of a local embedding service, if any |
| `VECTOR_STORE` | `chroma` | config.py default | no | chroma (default, embedded) \| milvus |
| `MILVUS_URI` | (empty) | config.py default | no | Milvus endpoint when VECTOR_STORE=milvus, e.g. http://milvus:19530 |
| `CHROMADB_PATH` | `/app/database/chromadb` | compose | no | the Chroma knowledge base; inside the 'face_database_data' volume |
| `CHROMA_COLLECTION_NAME` | `sql_knowledge_base` | config.py default | no | — |
| `RAG_TOP_K` | `5` | config.py default | no | — |
| `RAG_SIMILARITY_THRESHOLD` | `0.3` | config.py default | no | — |
| `SQL_AGENT_LEARN_FROM_QUERIES` | `False` | config.py default | no | false: verified answers are NOT written back into the knowledge base automatically |

**Run limits (fail-closed budgets per question)**

| Variable | Production value | Set in | Runtime change | Meaning |
|---|---|---|---|---|
| `SQL_AGENT_MAX_MODEL_CALLS` | `24` | config.py default | yes, after API restart | Total model attempts per agent run, including retries and fallback |
| `SQL_AGENT_MAX_TOOL_CALLS` | `12` | config.py default | yes, after API restart | Maximum selected tools per agent run |
| `SQL_AGENT_MAX_RUN_TOKENS` | `65536` | config.py default | yes, after API restart | Reported token budget; checked before each subsequent model/tool call |
| `SQL_AGENT_TOTAL_TIMEOUT` | `300` | config.py default | yes, after API restart | wall-clock budget for one question; the SQL statement_timeout is derived from what remains (≤ 30 s) |
| `SQL_AGENT_MAX_REASONING_STEPS` | `8` | config.py default | yes, after API restart | Total reasoning steps per turn (tool look-ups + re-plans). The upper bound on how long one turn may think. |
| `SQL_AGENT_MAX_ACTIONS_PER_TURN` | `3` | config.py default | no | — |
| `SQL_AGENT_MAX_REPLANS` | `1` | config.py default | yes, after API restart | Corrective re-plans after a failed action. Each needs a reason from the Observation; there is no blind retry. |
| `SQL_AGENT_MAX_CONCURRENT` | `2` | config.py default | yes, after API restart | questions the API answers at once; further ones wait |
| `SQL_AGENT_MAX_QUERY_CHARS` | `8000` | config.py default | no | Maximum natural-language SQL-agent query length across REST, SSE and WebSocket transports. |
| `SQL_AGENT_TRACE_CONTEXT` | `False` | config.py default | no | — |

**Memory and history**

| Variable | Production value | Set in | Runtime change | Meaning |
|---|---|---|---|---|
| `SQL_AGENT_MEMORY_RETENTION_DAYS` | `30` | config.py default | yes, after API restart | explicit user memories and idle conversation context expire after this |
| `SEARCH_HISTORY_RETENTION_DAYS` | `90` | config.py default | yes, next job run | Days to retain search history records. Default: 90 |
| `AUDIT_LOG_RETENTION_DAYS` | `180` | config.py default | yes, next job run | Days to retain audit-log records (chatbot, identity, settings). Default: 180 |

**Orchestration and optional services**

| Variable | Production value | Set in | Runtime change | Meaning |
|---|---|---|---|---|
| `AGENT_ORCHESTRATOR` | `langgraph` | config.py default | no | langgraph = the built-in ReAct graph (below) |
| `MCP_SQL_URL` | (empty) | config.py default | no | empty = tools run in-process; the 'mcp-sql' profile serves them over HTTP on port 9901 |
| `STT_PROVIDER` | `none` | config.py default | no | none \| local \| whisper \| faster_whisper \| riva \| external (dev only) |
| `STT_BASE_URL` | (empty) | config.py default | no | Endpoint of the STT service, if any |
| `STT_MODEL_PATH` | (empty) | config.py default | no | Local STT model path (offline check) |

**Development only — refused in production**

| Variable | Production value | Set in | Runtime change | Meaning |
|---|---|---|---|---|
| `LLM_DEV_PROVIDER` | (empty) | config.py default | no | 'nim' routes to NVIDIA's hosted API for development; stage 06b and the guard refuse it here |
| `NVIDIA_NIM_API_KEY` | (empty) | config.py default | no | API key from build.nvidia.com (free tier). Never logged; redacted everywhere a setting is rendered. |
| `NVIDIA_NIM_BASE_URL` | `https://integrate.api.nvidia.com/v1` | config.py default | no | OpenAI-compatible base URL of the NIM endpoint |
| `NVIDIA_NIM_MODEL` | `meta/llama-3.2-11b-vision-instruct` | config.py default | no | NIM model for chat/intent tasks |
| `NVIDIA_NIM_SQL_MODEL` | `openai/gpt-oss-120b` | config.py default | no | — |
| `NVIDIA_NIM_INTERPRETER_MODEL` | (empty) | config.py default | no | — |
| `NVIDIA_NIM_TIMEOUT` | `60` | config.py default | no | Per-attempt timeout in seconds for NIM calls |
| `SQL_AGENT_OPIK_ENABLED` | `False` | config.py default | no | per-turn tracing to a self-hosted Opik, development only |
| `OPIK_URL_OVERRIDE` | `http://host.docker.internal:5173/api/` | config.py default | no | Opik API URL. Default: a self-hosted instance on the workstation, reached from the container as host.docker. |
| `OPIK_API_KEY` | (empty) | config.py default | no | Account key for the hosted service (or an authenticated self-hosted instance); the open-source instance needs |
| `OPIK_WORKSPACE` | `default` | config.py default | no | Workspace name; the hosted service shows it in its settings page. Open-source Opik has exactly one, 'default |
| `OPIK_PROJECT_NAME` | `face-detector-sql-agent` | config.py default | no | Opik project the agent's traces are filed under. |

Runtime changes are made on the Settings page (or `PUT /api/settings/<KEY>`, §13.0); the ten run-limit and memory keys apply after an API restart. `OLLAMA_MODEL` and `OLLAMA_SQL_MODEL` are compose values: change them in `docker/docker-compose.prod.yml` **and** make sure the model is present (`docker exec face_detector_prod-ollama-1 ollama list`) — stage 13 of `deploy.sh` checks that.

### 15.4 Operating it

- **Models on disk** (`ollama_models` volume, 10 GB): `qwen2.5:7b` (chat/tools/reading), `hf.co/mradermacher/Arctic-Text2SQL-R1-7B-GGUF:Q4_K_M` (SQL), `qwen2.5:1.5b` (kept, unused). Ollama holds a GPU reservation on the same RTX 5090 as face recognition; `OLLAMA_KEEP_ALIVE` (ollama container) decides how long a model stays loaded after the last question — the first question after idle pays the load time.
- **Health:** `GET /api/sql-agent/health` (any chatbot user) · `/health/offline-policy` shows "local LLM reachable" (admin) · `docker exec face_detector_prod-ollama-1 ollama ps` shows what is loaded on the GPU right now.
- **Knowledge base:** Chroma at `/app/database/chromadb` (volume `face_database_data`, backed up daily as `artifacts.tar.gz`); the seed catalogue is in `sql_agent/seed_catalog*.py` and is re-applied on boot, so the volume can be recreated.
- **History, sessions, memories:** in PostgreSQL (`user_query_history`, sessions, explicit memories); users manage theirs through `/api/sql-agent/history`, `/sessions`, `/memory`; memories expire after `SQL_AGENT_MEMORY_RETENTION_DAYS`.
- **Exports:** `POST /api/sql-agent/export/pdf|word` produce artifacts fetched by `GET /api/sql-agent/artifacts/{id}`.
- **Alternatives, all local:** `LLM_PROVIDER=vllm` with the `vllm` compose profile (`LLM_BASE_URL=http://vllm:8000/v1`, `LLM_MODEL`); `VECTOR_STORE=milvus` with the `milvus` profile; the `mcp-sql` profile to serve the tools over MCP. None of them is started by the default production stack.
- **Development only:** NVIDIA NIM (`LLM_DEV_PROVIDER=nim`, `NVIDIA_NIM_*`) and Opik tracing exist to test query quality on the workstation; the production deploy path refuses both.

## 16. Faces: how a person is stored, matched and added

### 16.1 One person, many face vectors

A person is an **identity**; each face view of that person is one row in
`identity_embeddings`: a 512-dimensional ArcFace vector (`RECOGNITION_MODEL`
`w600k_r50.onnx`, faces found by the SCRFD detector `det_10g.onnx`), plus where
it came from — `pipeline_id`/`detection_id` for a camera sighting, `image_id`
for an uploaded photo — a quality score and the embedding-model version. There
is no fixed number of vectors per person and no averaging: **matching compares a
new face against every stored vector of every active known person** (pgvector
HNSW index, cosine distance) and the identity of the single best vector wins if
it clears `SIMILARITY_THRESHOLD` (0.4). A person with five views is therefore
recognised if *any* view is close enough, which is what several views buy you:
different angles, lighting, glasses, age.

Uploaded photos are kept in `identity_images` (one row per photo, at most
1 000 per person, the same file never twice thanks to a checksum index, exactly
one marked *primary* for display: `PUT /api/identities/{id}/images/{image_id}/primary`).

### 16.2 How vectors get added to a person

| Path | What happens | Guards |
|---|---|---|
| **Add Person (upload)** — the "Enrol a new face" modal, `POST /api/upload-person` | the photo is decoded, exactly one face must be found (two faces → refused), an embedding is computed and compared with the best score per existing person (pool of 25 nearest vectors) | ≥ `ENROLL_STRONG_MATCH_MIN` (0.75): "this is an existing person — add the photo to them" is recommended · between `ENROLL_CANDIDATE_MIN` (0.40) and 0.75: the admin is shown up to `ENROLL_MAX_CANDIDATES` (5) candidates and must choose · below 0.40: enrolled directly as a new person. The choice is committed with `POST /api/enrollment/confirm` (`add_to_existing` / `create_new` / `cancel`); an unconfirmed upload is parked and can be cancelled (`POST /api/enrollment/cancel`) |
| **More photos for an existing person** — `POST /api/identities/{id}/images` | same decoding/embedding, added to that person | checksum duplicate check, 1 000-photo cap, one face per photo |
| **Camera sighting of an unknown face** | if no known person clears 0.4 the sighting creates an **unknown identity** with that vector (`faiss_index_type=unknown`); later sightings of the same face attach to it | unknown identities are clustered nightly into merge suggestions and expire: snapshots after `SNAPSHOT_RETENTION_DAYS` (90), vectors after `EMBEDDING_RETENTION_MONTHS` (12), marked inactive after `INACTIVE_THRESHOLD_DAYS` (180) unseen |
| **Promote** — `POST /api/admin/unknown/{identity_id}/promote` | an unknown identity becomes a known person, keeping every vector it collected | — |
| **Merge** — `POST /api/admin/identities/merge` (preview first with `…/merge-preview`; nightly suggestions are approved via `…/merge-suggestions/{id}/approve`, and a merge can be undone with `…/merges/{merge_id}/unmerge`) | two identities become one; all vectors move to the survivor | the merge checker compares up to 8 newest vectors per side by *median* similarity, so one lucky pair cannot justify a merge; vectors from different embedding-model versions are never compared |
| **Auto-enrichment from cameras** (`IDENTITY_AUTO_ENRICH_ENABLED`) | a confidently recognised live face is appended to the known person so the person "learns" new views | **off in production (default).** When on: similarity ≥ 0.75, quality ≥ 0.5, skipped if ≥ 0.95 similar to a stored view, hard cap `MAX_EMBEDDINGS_PER_IDENTITY` (10). Off because one wrong attribution would become a permanent, self-reinforcing vector |

### 16.3 Housekeeping (identity retention, daily)

- Camera-derived vectors are capped at **10 per person** (highest quality kept, then newest); vectors from uploaded photos are **never pruned** — they live as long as the photo is in the gallery.
- A camera vector whose detection was never persisted (a crash mid-write) is removed after a grace period; an unknown identity left with no evidence goes with it.
- `SIMILARITY_THRESHOLD` (0.4) is the same bar the enrolment review uses as its floor, on purpose: anything recognition would confuse, enrolment must ask about, otherwise a duplicate person is created silently and recognition reports either name at random.

### 16.4 The settings involved, with production values

| Setting | Production | Set in | Runtime change | Meaning |
|---|---|---|---|---|
| `DETECTION_MODEL` / `RECOGNITION_MODEL` | `det_10g.onnx` / `w600k_r50.onnx` under `/app/weights` | compose (bind mount, verified by stage 08) | no | SCRFD face detector; ArcFace recogniser producing the 512-d vector |
| `VECTOR_BACKEND` | `pgvector` | compose | no | vectors live in PostgreSQL and are searched there (FAISS is a disposable in-memory index rebuilt from the table) |
| `PGVECTOR_INDEX_TYPE` / `PGVECTOR_HNSW_M` / `PGVECTOR_HNSW_EF_CONSTRUCTION` | `hnsw` / 32 / 128 | compose | index rebuild | approximate-nearest-neighbour index parameters |
| `SIMILARITY_THRESHOLD` | 0.4 | compose | no | minimum cosine similarity for a match (measured: same person across photos ≈ 0.43, unrelated faces < 0.05) |
| `CONFIDENCE_THRESHOLD` | 0.5 | compose | no | minimum detector confidence for a face to be considered at all |
| `IDENTITY_INGEST_TOP_K` | 5 | default | no | how many nearest vectors are fetched per live face before the threshold filters |
| `ENROLL_CANDIDATE_MIN` / `ENROLL_STRONG_MATCH_MIN` | 0.40 / 0.75 | default | no | the review band for Add Person (must not be inverted; startup refuses that) |
| `ENROLL_CANDIDATE_POOL` / `ENROLL_MAX_CANDIDATES` | 25 / 5 | default | no | vectors fetched, then collapsed to one row per person; candidates shown |
| `IDENTITY_AUTO_ENRICH_ENABLED` | false | default | no | camera views added to known people automatically (see 16.2) |
| `IDENTITY_ENRICH_MIN_SIMILARITY` / `IDENTITY_ENRICH_MIN_QUALITY` / `IDENTITY_NEAR_DUPLICATE_MIN` | 0.75 / 0.5 / 0.95 | default | yes / yes / no | enrichment guards (only matter when enrichment is on) |
| `MAX_EMBEDDINGS_PER_IDENTITY` | 10 | default | yes, next job run | cap on camera-derived vectors per person |
| `SNAPSHOT_RETENTION_DAYS` / `EMBEDDING_RETENTION_MONTHS` / `INACTIVE_THRESHOLD_DAYS` | 90 / 12 / 180 | default | yes / no / yes | unknown-identity housekeeping (§9 of the home page's retention panel shows the first two) |
| `IDENTITY_CLEANUP_INTERVAL_HOURS` / `CLUSTER_INTERVAL_HOURS` / `CLUSTER_STARTUP_DELAY_HOURS` | 24 / 24 / 7 | default | yes / no / yes | cadence of identity retention and of the nightly clustering into merge suggestions |
| `FACE_TRACKING_WINDOW_SECONDS` | 30 | compose | no | in-memory suppression of the same face re-detected within 30 s (0 would write every frame) |
| `SAVE_CROPPED_IMAGES` | false | compose | yes, immediate | keep a crop of every detected face on disk |

### 16.5 Merge, merge suggestions ("auto-merge") and promote

**Nothing merges automatically.** The only code that merges identities is
`merge_identities` / `merge_multiple_identities`, and their only callers are
three administrator endpoints: the pair merge, the multi-merge and *approve
suggestion*. What the system does automatically is **propose**.

**How suggestions are produced (the nightly clustering job, §9 of Docs/95)**

1. Runs 7 h after boot (shortened to the next 24 h slot when it already ran
   recently) and then every `CLUSTER_INTERVAL_HOURS` (24). It looks only at
   **unknown** identities seen in the last `CLUSTER_ACTIVE_WINDOW_DAYS` (90) and
   needs at least `CLUSTER_MIN_SIZE` (2) of them.
2. Hybrid matching: same-camera candidates come from co-appearance/time patterns
   and are confirmed by a vector check ≥ `UNKNOWN_SIMILARITY_THRESHOLD` (0.35);
   cross-camera candidates are found vector-first and must clear the stricter
   `CROSS_PIPELINE_SIMILARITY_THRESHOLD` (0.50), because two people seen only on
   different cameras have no co-appearance evidence for or against. DBSCAN
   (`CLUSTER_EPS` 0.35, `CLUSTER_MIN_SAMPLES` 2) groups them.
3. Each candidate group gets a **confidence**: the trained similarity model from
   the ML-Ops page when one is active (`PIPELINE_AWARE_CLUSTERING_ENABLED`,
   allowed to sit `CLUSTER_TRAINED_MODEL_MARGIN` 0.05 below the threshold),
   otherwise a heuristic (0.8 × face similarity + 0.06; pattern evidence capped
   at 0.75).
4. The result is a `MergeSuggestion` row with status **pending**. It waits for a
   person. Statuses: `pending` → `approved` / `rejected`, or `invalidated` when
   one of its identities was merged or deleted in the meantime. Suggestions can
   also be regenerated on demand (`POST /api/admin/merge-suggestions/generate-pipeline-aware`).

**The gate every merge passes** (`backend/core/merge_compatibility.py`) — pair
merge, multi-merge, preview, suggestion approval alike, by construction:

- only stored, validated vectors count (finite, unit-norm), and only vectors
  sharing an `embedding_model_version` are compared — never across model
  spaces; up to the 8 newest vectors per identity;
- for each pair of identities the score is the **median** of all cross
  similarities, never the maximum, so one mis-attributed frame cannot vouch for
  merging two strangers; for a group the score is the **minimum** of the pair
  medians, so one unrelated member drags the whole group down;
- `compatible` when that score ≥ `MERGE_WARNING_MIN_SIMILARITY` (0.40);
  otherwise `high_risk`, which the API refuses unless the caller sends
  `confirm_merge_risk: true`; `unavailable` when nothing is comparable.

**Merging** (`POST /api/admin/identities/merge` with `from_identity_id`,
`to_identity_id`, `decision`; `…/merge-multiple` for several; preview first with
`…/merge-preview`, which shows the target, the sources, the per-camera
distribution, the type outcome and which snapshot will represent the result).
Allowed for administrators, or for users whose camera scope covers every
identity involved. In one transaction the loser's appearances, vectors, faces,
watch-list memberships and live-search alerts move to the winner; if the winner
is a known person the moved vectors are relabelled into the known search space;
the loser stays in the database as `MERGED` and inactive so history and audit
keep pointing at something real. Everything moved is written to the merge's
**provenance**, which is what makes **unmerge**
(`POST /api/admin/identities/merges/{merge_id}/unmerge`) exact: it puts back
exactly the rows listed there and restores the loser's previous status. Every
unmerge refusal (for example the merge record no longer existing) happens in a
verification phase before any write, so a refusal never leaves a half-undone
merge.

**Approving a suggestion** (`POST /api/admin/merge-suggestions/{id}/approve`,
optional `confirm_merge_risk`) runs the same gate and the same merge; rejecting
(`…/reject`) records the decision so the pair is not proposed again in that form.

**Promote** (`POST /api/admin/unknown/{identity_id}/promote` with
`display_name` (required), optional `person_code` (must be unique; refused if
taken) and `decision: create_new`) turns an **unknown** identity into a **new
known person**: its type becomes known, its status `PROMOTED` (treated as active
and known by matching), and every vector it collected is relabelled into the
known search space, so the person is recognised from the next sighting on. If
the unknown face is really someone already enrolled, do not promote: use
`GET /api/admin/unknown/{identity_id}/match-candidates` (read-only ranking of
known people against that face) and **merge the unknown into that person**, which
keeps one identity. Administrators can promote any unknown identity; other users
only those from cameras in their scope.

Related settings not listed in 16.4: `UNKNOWN_SIMILARITY_THRESHOLD` 0.35,
`CROSS_PIPELINE_SIMILARITY_THRESHOLD` 0.50 (Settings page, immediate),
`MERGE_WARNING_MIN_SIMILARITY` 0.40, `CLUSTER_EPS` 0.35, `CLUSTER_MIN_SAMPLES` 2,
`CLUSTER_MIN_SIZE` 2, `CLUSTER_ACTIVE_WINDOW_DAYS` 90 (next job run),
`PIPELINE_AWARE_CLUSTERING_ENABLED` true (immediate), `CLUSTER_TRAINED_MODEL_MARGIN`
0.05 (immediate) — all `config.py` defaults in production.

Code: `backend/core/identity_service.py` (matching, unknown identities, enrichment, merge), `backend/core/enrollment_service.py` (Add Person), `backend/routes/upload.py` and `backend/routes/enrollment_review.py` (the endpoints), `backend/core/identity_index_pgvector.py` (the search), `backend/core/identity_retention.py` and `identity_clustering.py` (housekeeping), `backend/core/merge_compatibility.py` (merge checks).

