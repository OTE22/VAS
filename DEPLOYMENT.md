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

