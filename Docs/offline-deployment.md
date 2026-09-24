# Offline deployment — move this same server

[Documentation index](README.md) · Reviewed 2026-09-24

This is the path you selected: **move the existing installed server to an offline
intranet**. Keep its current application directories, Docker images, containers,
volumes, models, certificates and secrets. An IP change alone does not require a
reinstall, build, image import, secret regeneration or certificate reissue.

The previous guide was `Docs/OFFLINE_SERVER_MIGRATION.md`; this guide replaces it.
The implementation is [prepare_offline_bundle.sh](../scripts/prepare_offline_bundle.sh),
[move_to_intranet.sh](../scripts/move_to_intranet.sh) and
[intranet_dns.sh](../scripts/intranet_dns.sh). The commands below are instructions
for the move; they were not executed as part of this documentation refresh.

## 1. Prepare while the current network still works

From a terminal on this server:

```bash
cd /home/itdirect-ai/Desktop/VAS
sudo docker ps -a --format 'table {{.Names}}	{{.Status}}	{{.Image}}'
sudo python3 scripts/deploy/offline-inventory.py > /tmp/vas-offline-inventory.json
sudo bash scripts/prepare_offline_bundle.sh --same-server
```

Expected: your normal serving containers are running and the same-server check
ends with `Read-only checks passed`. A completed migration container may be
exited successfully; it is a one-shot job. The inventory lists actual images,
mounts and restart policies without exporting data. Save it with your move notes.

**Checker scope:** it expects the deployed container names
`face_detector_prod-face_recognition-1`, `face_detector_prod-nginx-1`,
`face_detector_prod-ollama-1`, `VMS`, `VMS-db`, `vas-assistant` and
`vas-assistant-gate`. It checks VAS offline/origin configuration, certificates and
HTTPS endpoints for all three applications. It is not a generic checker for a
fresh VAS-only installation, and it stops on the first failure. Missing VMS or
chatbot containers must be investigated in their separate deployments.

Confirm the local face weights, language-model caches and map data already work:
open a stored face, perform a search, load a map and ask the local chatbot a
stored-data question. Read the actual inventory and runtime configuration;
old inventory model names may no longer match the installed models.

## 2. Take a backup and copy it off this server

```bash
sudo bash deploy.sh backup
```

Expected: a new backup timestamp and successful checksum verification. Follow
[Backup and recovery](backup-and-recovery.md) to copy the timestamped directory
to external media. This VAS backup is not a backup of the separate VMS/chatbot
applications or all model caches. Preserve those deployments and their data with
their own backup procedures. Keep configuration, secrets and private keys in a
restricted backup; clients receive only the public CA certificate.

For consistent database-plus-image recovery, arrange a maintenance window and
stop incoming writers before the final snapshot. A normal live database dump
plus later image archives are not an atomic cross-service snapshot.

## 3. Record the new intranet settings

Obtain the server's assigned IP, subnet/prefix, gateway, internal DNS address,
and camera-network routes from the network administrator. Reserve a stable
address or DHCP reservation. Do not guess values from these examples.

The existing application names stay the same:

| Application | Browser URL | Internal DNS A record |
|---|---|---|
| VAS | `https://face-detector.internal/` | `face-detector.internal` → server IP |
| VMS | `https://armyeye-vms.internal/` | `armyeye-vms.internal` → server IP |
| Chatbot | `https://armyeye-chatbot/` | `armyeye-chatbot` → server IP |

Clients and the server must resolve the names through internal DNS. Client TCP
443 access is required; TCP 80 provides HTTP-to-HTTPS redirection. Preserve routes
needed for cameras and internal webhook traffic. Keep the server clock correct,
using an internal time source if available.

## 4. Shut down, move and connect

Use Ubuntu's normal shutdown before transporting the machine. Connect it to the
intranet and power it on. Configure the assigned network values through Ubuntu's
network settings or your existing network-management procedure. Perform network
changes at the local console so loss of SSH does not strand the configuration.

Keep the existing Docker daemon storage and project directories. Do not run
`docker compose down -v`, delete volumes, or recreate the production stack using
development Compose files. The production project name is `face_detector_prod`.

## 5. Update internal DNS and client certificate trust

Preview the records using the **actual** assigned addresses. These example values
must be replaced:

```bash
bash scripts/intranet_dns.sh --server-ip 10.90.0.20 --dns-server 10.0.16.1
```

This command only prints records. Give them to the DNS administrator to apply on
the existing internal DNS service. The script's authenticated `--apply` mode
requires DNS-server support and an authorized TSIG/Kerberos credential; an Ubuntu
password is not a DNS-update credential.

Once records exist, verify them:

```bash
bash scripts/intranet_dns.sh --check --server-ip 10.90.0.20 --dns-server 10.0.16.1
getent hosts face-detector.internal armyeye-vms.internal armyeye-chatbot
```

Expected: all three names resolve to the assigned server address. The script's
DNS query needs `dig`; install any missing diagnostic utilities before the move.
`getent` checks the server's normal resolver, which can differ from querying a
specific DNS server. Also test resolution on a client PC.

Install `certs/internal-ca.crt` in each client's trusted-root store using that
client's operating-system/browser procedure. Never distribute the CA private key.
Continue browsing by hostname. An IP change does not invalidate a certificate
for the same hostname; certificate expiry still requires normal renewal.

## 6. Check services with the internet disconnected

```bash
sudo docker ps -a --format 'table {{.Names}}	{{.Status}}'
sudo bash scripts/prepare_offline_bundle.sh --same-server
```

If DNS is not ready yet, test the new address without changing application
settings:

```bash
sudo bash scripts/prepare_offline_bundle.sh --same-server --server-address 10.90.0.20
```

This bypasses DNS only for the HTTPS probes while preserving hostname/TLS
verification. It does not prove client DNS works. To probe VAS alone:

```bash
curl --noproxy '*' --fail --show-error --cacert certs/internal-ca.crt \
  https://face-detector.internal/health/ready
```

If an existing serving container is stopped, inspect its logs before starting it:

```bash
sudo docker logs --tail 100 face_detector_prod-face_recognition-1
```

Restore the existing deployment's normal start procedure. Preserve its CPU/GPU
overlay and any saved overrides. Do not run a fresh `up --build` merely to change
networks. When recreation is actually needed offline, use the exact installed
Compose configuration with local images and `--no-build --pull never`; a missing
image must be supplied, not silently downloaded. VMS/chatbot have separate stacks.

## 7. Verify from another intranet PC

| Test | Expected result |
|---|---|
| Open all three stable URLs | Trusted HTTPS pages; no external internet needed |
| Sign in to VAS | Normal authorized landing page; existing users/data remain |
| Dashboard + a test camera event | Correct pipeline and recent detection |
| Search with an approved known test photo | Matches or an honest no-match result |
| Watchlist/live alert test | Stored trigger and persistent acknowledgement |
| Intelligence map | Local basemap and configured camera positions |
| VMS playback | Internal camera video reachable |
| Chatbot stored-data question | Local answer or a specific error to investigate |
| Background tasks / Logs | No new migration, model, storage or queue failure |

Use the [end-to-end demo](demo.md) for the VAS UI sequence. Local health checks
alone do not establish camera routing, client access or end-to-end processing.

## 8. Reboot offline and repeat the check

Reboot normally while still disconnected from the internet. Repeat step 6 and
key client tests from step 7. Confirm existing records, map assets and local
models survive and serving containers restart as intended. Keep the off-host
backup and inventory until the move is accepted.

## If something fails

| Symptom | First check |
|---|---|
| Name does not resolve | Client/server DNS settings and new A records |
| Certificate warning | Correct hostname, client CA trust, server clock, certificate expiry |
| HTTPS refused or 502 | Nginx/API/Martin container status and their logs |
| Dashboard has no events | Camera route, webhook credential, pipeline permissions, ingest logs |
| Map is blank | Martin health, existing map archives, metadata and camera coordinates |
| Chatbot fails offline | Its separate stack, installed model and local service reachability |
| Checker reports missing container | Its fixed names and whether VMS/chatbot are actually deployed |
| ML job remains queued | ML worker health and job readiness; internet is not a substitute for missing artifacts |

## Different hardware later

The artifact scripts still exist:

```bash
bash scripts/prepare_offline_bundle.sh /path/to/reviewed-spec.json /media/bundle
bash scripts/verify_offline_bundle.sh /media/bundle
```

A bundle copies only explicitly listed artifacts; it is not a full deployment
or database backup. The example spec contains placeholder paths and image tags
that differ from current production, and omits parts of the stack. Do not use it
unchanged. A replacement server also needs consistent databases/files, all named
volumes and caches, host drivers/runtime, actual images, configuration, secrets,
map data and the separate applications. `import_offline_bundle.sh` writes the
manifest destinations and loads images, so review those paths before import.
This additional migration is unnecessary for the same-server move above.
