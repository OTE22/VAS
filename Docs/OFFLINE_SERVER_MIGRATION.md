# Moving VAS, VMS, and the chatbot to an offline server

## Current evidence and remaining acceptance

The final static IP and target hardware have not yet been assigned. The current
machine uses `192.168.1.111`; that is deployment data, not a permanent address.
On September 15, 2026, the live VAS offline policy was enforced with no findings.
VAS used local Ollama `qwen2.5:7b`; the standalone chatbot used local Ollama
`qwen3.5:9b-32k`. Both models were present. VMS had a local YOLO weight file.
The application containers used persistent mounts and `unless-stopped` restart
policies, and the Docker service was enabled at boot.

These checks do not prove camera reachability, migrated database integrity,
GPU compatibility, or operation after a disconnected cold boot on new hardware.
Those acceptance checks must run on the final server before handover.

## What must move

Copying the Git repositories alone is insufficient. Preserve:

- Built Docker images for all services, including the migration image. Export
  with `docker save`, then import with `docker load` on the target. Preserve tags.
- VAS and VMS PostgreSQL logical backups, roles, and their respective secrets.
  Use application-consistent database backups; do not copy a live PostgreSQL
  data directory. VAS provides `sudo ./deploy.sh backup`.
- Every named volume and bind-mounted data directory in the runtime inventory:
  model stores/caches, face and media storage, map data, ML artifacts, chatbot
  state/workspace/gate state, VMS artifacts and configuration, and both projects'
  secrets. Quiesce writers for the final consistent data snapshot.
- All three repositories: VAS, VMS, and `vas-assistant`, including its `gate`.
- The existing CA certificate and matching server private keys. Retain the CA
  signing key securely for certificate issuance/renewal; do not distribute it to
  client PCs. Reusing the same CA preserves existing workstation trust.
- Offline Ubuntu/Docker/Compose/GPU driver/container-toolkit packages compatible
  with the target hardware. Loading Docker images does not install host drivers.

Generate the current image/volume inventory without printing credentials:

```bash
sudo python3 scripts/deploy/offline-inventory.py > offline-inventory.json
```

The inventory is a checklist, not a backup. Running containers may reference
host paths tied to the current username. Recreate them from the restored Compose
files/scripts at the target paths; do not reuse stale Docker bind paths.

## Once the final static IPv4 address is assigned

1. Configure the server's static address, prefix, gateway, and internal DNS on
   the actual target interface. Do this at the server console. The certificate
   tool below does not modify network interfaces or routes.
2. On the restored deployment, stage new leaf certificates using the existing
   CA and private keys. Replace the example address with the assigned address:

   ```bash
   sudo python3 scripts/tls/prepare-static-ip.py \
     --ip 10.90.0.20 --output /etc/nginx/staged-static-ip
   ```

   This creates three verified public certificates plus address settings. It
   keeps DNS identities and loopback addresses, replaces old LAN IP SANs, uses
   unique serials, and refuses to overwrite an existing output folder. It
   neither changes live services nor copies private keys into the staging folder.
3. Keep a protected backup of the current certificates and configuration. Install
   the staged `server.crt`, `vms.crt`, and `laf-ai-chatbot.crt` into both `VAS/certs`
   and Ubuntu `/etc/nginx/certs`, mode 0644. Keep their matching original `.key`
   files mode 0600. Never replace the CA to change a server address.
4. Update only `VAS_INTRANET_ORIGIN=https://<assigned-ip>` in `VAS/docker/.env`.
   Do not replace the whole file with the staged `intranet.env`; the existing
   file contains other required deployment settings and secrets. Update
   `/etc/nginx/intranet.env` with `VAS_INTRANET_IP=<assigned-ip>` for optional
   VMS mDNS publication. The file may be mode 0644; it contains no secrets.
5. For the chatbot, update `ASSISTANT_LAN_IP` in `vas-assistant/assistant.env`.
   Keep its configured hostname and set an **internal DNS A record** pointing
   that hostname to the new address. Also resolve `face-detector.internal` on
   workstation clients that use it, including chatbot links back to VAS.
   Internal DNS works without internet. mDNS is an optional same-subnet
   convenience and is insufficient across a routed intranet.
6. Recreate the VAS API from the same CPU/GPU Compose file selection with
   `up -d --no-deps --no-build --pull never face_recognition`. Recreate the chatbot
   and gate using `assistant.sh start` from its restored repository, setting
   `VAS_REPO` to the restored VAS path. Its local image must already be loaded.
   Validate Nginx with `nginx -t` inside its container, then reload it. Restart
   the mDNS services only if that optional feature is used.

VAS browser access becomes `https://<assigned-ip>/`; VMS becomes
`https://<assigned-ip>:8443/`. Client PCs can have any source address allowed by
the intranet routing/firewall. VMS-to-VAS webhooks keep using Docker's stable
`face-detector.internal` network alias when both projects share the same host;
do not replace internal service names with the current workstation IP.

The existing chatbot static-IP script only migrates the chatbot/network profile;
it does not update VAS/VMS certificates and origins. Do not treat it as the
complete migration procedure for all three applications.

## Required disconnected acceptance test

With WAN access disconnected but intranet routing available:

- Cold-boot the final server and confirm all required containers become healthy.
- On a PC on another intranet subnet, trust the existing CA and open VAS, VMS,
  and the chatbot; sign in and verify permissions and logout.
- Confirm VAS and VMS URLs pass certificate verification (no bypass options),
  IP browser-origin checks, and secure-cookie behavior.
- Run a real camera stream, detection, and VMS-to-VAS webhook; inspect the result
  in VAS. Check local maps/media and stored records.
- Send a chatbot question and a database-backed question using the local model.
- Restore a backup into an isolated test database and verify it is readable.
- Verify the server and PCs have correct time, using internal time service or
  maintained clocks. Certificates and sign-in tokens depend on accurate time.

Internet searches, cloud APIs, new model downloads, package installation, and
image pulls will not work offline. Required assets must be staged beforehand.
Do not run an image build expecting internet package repositories to be present;
use the prebuilt images and `--no-build --pull never` for the restored stack.

The current VAS and VMS leaf certificates expire September 15, 2027. Certificate
renewal using the retained CA can be performed offline before expiry.
