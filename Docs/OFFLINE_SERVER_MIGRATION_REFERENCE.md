# Offline deployment reference

Start with the [same-server checklist](OFFLINE_SERVER_MIGRATION.md).

## Current design

This is the same installed server. Its network address and internal DNS records
may change; the applications retain stable hostnames. Follow
[hostname-based intranet access](INTRANET_ACCESS.md). Do not run certificate
reissuance or application-origin updates merely to change the network address.

The Bash same-server mode is read-only. `--server-address` optionally chooses
a connection target without changing the HTTPS hostname or saving an IP.
The former `--apply --ip` mode is retired and rejected.

## Existing offline assets

The September 15, 2026 audit found local Ollama models `qwen2.5:7b` for VAS and
`qwen3.5:9b-32k` for the chatbot, local VMS YOLO weights, persistent application
storage, and container restart policies. VAS's live offline policy passed.
[The saved inventory](OFFLINE_RUNTIME_INVENTORY.json) records images and mounts
at the time of that audit; it is not a backup or a guarantee of current state.
Refresh it with:

```bash
sudo python3 scripts/deploy/offline-inventory.py > offline-inventory.json
```

A disconnected reboot, camera/webhook test, chatbot response, and cross-subnet
client login remain required after moving. Also check correct clocks, GPU
operation, stored data, and backup restoration. Internet-dependent features
cannot operate without internet.

## Only if the hardware is replaced later

Copying repositories alone is insufficient. Preserve all application images,
consistent database backups and roles, named volumes, model caches, map/media
files, configuration, secrets, and the existing CA/private keys. Include VAS,
VMS, and `vas-assistant` with its gate code. Install compatible host GPU drivers,
Docker, Compose, and container-toolkit packages before going offline.

Export images with `docker save` and import with `docker load`, preserving tags.
Use database-aware backups and quiesce writers for the final snapshot; do not
copy a live PostgreSQL data directory. VAS provides `sudo ./deploy.sh backup`.
Keep bind paths correct for the target filesystem. Use prebuilt images with
`--no-build --pull never` rather than expecting offline package downloads.

The separate artifact-export interface of `prepare_offline_bundle.sh` remains
available when explicitly given a spec and output directory. It is unnecessary
for moving this same server to another network.

## Certificates

Retain the existing internal CA and distribute only its public certificate to
clients. The VAS/VMS leaf certificates currently expire September 15, 2027.
Renew them using the retained CA before expiry; renewal can be performed offline.
The obsolete Python IP certificate-staging utility has been removed. Address
changes use internal DNS and do not require certificate reissuance.
