# Guided VAS installation

From the VAS release directory on the target Linux server, run:

```bash
./deploy.sh
```

No arguments are required. Run it in an interactive terminal. On Linux, the script
requests your sudo password when administrator access is needed, then opens the
eight-step guide. Review the plan and choose **1 — Install now** to proceed through
installation, service startup and health checks. Pressing Enter at the review
chooses **Preview checks only**, so select 1 when you want to install.

`sudo ./deploy.sh` and `./deploy.sh wizard` also open the same guide. Without a
terminal, a bare invocation stops with instructions rather than deploying silently.
Use explicit subcommands for automation; `--help` lists them.

## Choose one installation method

| Choice | What happens | What you need |
| --- | --- | --- |
| **1 — Load saved Docker images** | Imports archives, checks every image required by Compose, and starts with `--no-build --pull never`. | Matching VAS release checkout, saved images (or images already loaded), and model weights. |
| **2 — Fresh build** | Uses the existing host setup, configuration and build stages, then starts and checks the services. | VAS source checkout, model weights, and access to build dependencies; offline builds require those dependencies locally. |

Both choices preserve existing volumes. On an existing deployment, the protected
backup/upgrade flow is used. “Fresh build” never means deleting the database.

The guide takes you through these eight steps, in order:

1. Installation method.
2. Optional release-package directory and, for load mode, one or more image archives.
3. **Server IP and browser URL:** enter the server-network IPv4 address, then use the IP or a DNS name for HTTPS.
4. **GPU/CPU setup:** require NVIDIA acceleration or use CPU; verify existing GPU prerequisites or opt into installation/repair, then choose installation connectivity.
5. Keep the displayed default configuration or customize it.
6. **Service connections:** keep automatic internal connections and default language models, or choose model names and an already deployed notebook URL.
7. **Storage:** keep Docker-managed volumes (recommended), or choose a dedicated directory before a new installation. Review the exact ONNX/map destinations.
8. **Review:** install, preview checks only, or cancel.

Enter file paths without surrounding quotes; spaces are supported. Press Enter
to accept the displayed default. The review defaults to preview checks. Cancelling
or closing input during the questions makes no installation changes.

After **Install now**, the installer runs one sequence:

1. Check the host and Docker prerequisites.
2. Prepare directories, secret files and HTTPS certificates.
3. Save configuration; check GPU allocation, weights, network and optional assets.
4. Load and verify all required images, or build the application images.
5. Prepare the database and preserve/back up existing data when applicable.
6. Start services in dependency order, with migrations gating application startup.
7. Check configured language models, run health checks, and print login/integration instructions.

Each stage names what it is doing and reports its result. A mandatory failure stops
at that stage with the reason and log location. Fix that issue and rerun the guide;
completed setup is reused. An existing installation follows the protected upgrade
sequence, including a configuration snapshot before applying new guided settings.

## Files to bring

Keep the release checkout with `deploy.sh`, `scripts/`, `docker/`, `alembic/`,
and the other application files. An image archive alone is not an installation package.

Supply these verified weights in the checkout or under the package's `weights/`:

```text
weights/det_10g.onnx
weights/w600k_r50.onnx
weights/WEIGHTS_MANIFEST.json
```

A package can have this layout:

```text
vas-release/
  images/
    vas-images.tar
    dependencies.tar.gz
  weights/
    det_10g.onnx
    w600k_r50.onnx
    WEIGHTS_MANIFEST.json
```

Use archives made with **`docker save`**, not container filesystems made with
`docker export`. Keep the exact release tags, including the application, worker,
migration and supporting service images. The installer prints every missing tag
and stops before database startup if the set is incomplete. Load mode does not
silently build, pull, or retag a different image. Match CPU/GPU images to the
hardware option and use the checkout shipped with that release.

Docker images do **not** contain the production database, saved face photos,
Ollama model volume or other persistent data. Moving an existing installation
requires its data backups as well. This guide installs VAS; VMS and separately
managed chatbot services retain their own deployment procedures.

## Server configuration

The guide delegates configuration to the existing deployment stages:

- Docker/Compose prerequisites and GPU allocation checks.
- Generated secrets, preserving existing passwords.
- HTTPS certificates for the selected DNS name or IPv4 address.
- `docker/.env`, production offline policy and migration revision.
- Weight verification, image preparation, database initialization/migrations,
  service startup and health/acceptance checks.

Python 3 is required to run the guide. GPU prerequisite installation is optional
and configurable as described below. CPU mode has lower throughput. A fully offline installation also needs host packages,
all Docker images and model data supplied locally. The built-in checks stop with
instructions when a prerequisite is missing.

The installer records `SERVER_IP`, `PUBLIC_ORIGIN` and `PUBLIC_ORIGINS` in
`docker/.env`. It adds `https://<SERVER_IP>` to the explicit allowed browser origins
while preserving existing entries. The certificate must cover **every** allowed
origin; a new certificate includes the chosen primary hostname and server IP.
An existing certificate missing the new IP is reported and never silently replaced.
If you provision a certificate yourself, include every hostname/IP in its SANs.

Use the server address clients can actually reach. The guide supports HTTPS on
port 443; it does not assign an operating-system IP, change DNS or open firewall
ports. Existing installations keep their configured URL in the guide. Address
changes require a separate certificate/origin update, preserving any configured
additional origins. Existing certificates are validated and never overwritten.

Internet availability in the guide concerns installation dependencies. Production
application runtime retains the existing offline policy. Missing optional LLM
models are reported separately; a Docker image import does not populate them.

## GPU setup, in order

At hardware step 4, choose **NVIDIA GPU required** or **CPU only**. Choosing GPU
means a missing driver cannot silently turn the deployment into a CPU installation.
Then select one GPU setup policy:

| Policy | Behavior |
| --- | --- |
| **Verify existing** (default) | Checks the driver and Docker NVIDIA runtime without changing them. Best for an already working server. |
| **Install/repair** | Preserves a compatible driver; installs a missing/incompatible driver on supported Ubuntu x86_64 hosts, or installs/configures the Container Toolkit when needed. |

With install/repair, choose **auto** for Ubuntu's hardware-recommended driver, or
enter an explicit package such as `nvidia-driver-580-open`. This is an example,
not a universal package recommendation. The package must be reported as supported
by `ubuntu-drivers devices`, available from the configured repositories, and meet
the image's driver policy. Unsupported cards/packages stop with instructions.
The installer installs matching kernel headers and never disables Secure Boot.

The steps are:

1. Read the CUDA version from `docker/Dockerfile.gpu` and inspect the active driver.
2. Keep a compatible driver. If repair is requested and needed, select/check the
   Ubuntu package and install it with the matching kernel headers.
3. **Stop for a manual reboot after a driver installation.** Exit code `75` and
   `REBOOT_REQUIRED` mean deployment is incomplete. Save your work, reboot, complete
   MOK enrollment if prompted, then rerun the guide and choose NVIDIA GPU again.
   Rerunning before reboot will not reinstall the driver. A driver still broken
   after reboot stops for diagnosis rather than entering a reinstall loop.
4. Check/install NVIDIA Container Toolkit and configure Docker's NVIDIA runtime.
   Runtime repair restarts Docker and can interrupt other containers; this is
   stated before the final installation review. An existing working runtime is kept.
5. Start the application and require real SCRFD/ArcFace inference on CUDA. The
   `gpu-test` command uses the application container, without pulling another
   CUDA image. A driver version or `nvidia-smi` alone is not acceptance.

CUDA and cuDNN remain in the application image; the host does not need a separate
CUDA compiler toolkit. The current image is CUDA **12.4.1**. This installer's
conservative Linux driver baseline is **550.54.15**, matching CUDA 12.4 Update 1's
corresponding driver release. NVIDIA also documents older-driver minor-version
compatibility with restrictions; this installer does not assume that path. Newer
compatible drivers are retained. A changed CUDA base image requires a review of
this policy. See [NVIDIA's CUDA 12.4.1 release notes](https://docs.nvidia.com/cuda/archive/12.4.1/cuda-toolkit-release-notes/index.html).

The version baseline does not certify GPU architecture support, sufficient VRAM,
or camera throughput. Actual application inference remains the required test.

Automatic kernel-driver installation is limited to **native Ubuntu x86_64** with
network access. Other operating systems and fully offline hosts receive manual
prerequisite instructions. WSL must use the Windows-side NVIDIA driver and Docker
Desktop integration; the installer refuses a Linux kernel-driver installation there.
Container Toolkit follows [NVIDIA's installation procedure](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
For signing/enrollment, follow [Ubuntu's Secure Boot guidance](https://documentation.ubuntu.com/security/security-features/platform-protections/secure-boot/).

Automation examples:

```bash
# Require GPU, checking existing prerequisites only:
sudo ./deploy.sh deploy --gpu --gpu-setup=verify --yes \
  --server-ip=10.21.5.22 --public-origin=https://10.21.5.22

# Opt into missing-driver/runtime installation or repair:
sudo ./deploy.sh deploy --gpu --gpu-setup=install --nvidia-driver=auto --yes \
  --server-ip=10.21.5.22 --public-origin=https://10.21.5.22
```

`--dry-run` and `validate` never install drivers or restart Docker. If a prerequisite
is missing, they explain what must be installed and stop before dependent GPU checks.
No automatic reboot is performed.

## Default configuration

The guide displays existing values from `docker/.env`, or these defaults for a
new installation. Press Enter to keep them, or select **Customize**:

| Setting | New installation default | Meaning |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | Normal operational logging; `WARNING` and `ERROR` are quieter. |
| `DATA_RETENTION_DAYS` | `365` | Detections/events become eligible for cleanup after this age. |
| `BACKUP_RETENTION_DAYS` | `14` | Older automatic backups become eligible for deletion. |
| `MAX_STORAGE_GB` | `5000` | Storage capacity used for dashboard reporting; not a disk quota or reservation. |

Choose retention periods according to your storage and operational requirements.
Reducing a retention period can cause older records or backups to be removed by
the existing cleanup jobs. Storage reporting should reflect the capacity available
to this installation; the default does not mean the machine has 5 TB available.

Values are validated and included in the review before installation. Settings are
written to `docker/.env` only during the deployment stages; a dry run prints the
planned changes. Existing deployments snapshot their configuration before applying
the guided choices. Keep this file private because it also contains credentials.
Application settings previously saved through the Settings page may override these
startup defaults. This step does not reset those settings or change recognition
thresholds, GPU concurrency, camera FPS, or the database schema.

## Connections configured automatically

PostgreSQL, Redis, Ollama and Martin use private Docker service names. Users do not
need to enter database/cache IPs or database passwords. The existing secret generator
and role setup supply them. HTTPS remains on port 443, webhook authentication stays
required, and the migration revision is derived from the release checkout.

`OLLAMA_MODEL` and `OLLAMA_SQL_MODEL` default to the existing production value
`gpt-oss:20b`. The guide preserves configured model names and offers customization;
choose models that fit the target hardware. The model stage checks these actual
configured values, not an unrelated manifest list. Missing models are fetched only
when installation connectivity allows it; an offline host must have them preloaded.

`ML_NOTEBOOK_URL` stays empty until a notebook service is separately deployed. Use
its HTTPS URL without embedded credentials or token query strings. Setting a URL
does not install Jupyter. Prepared `.mbtiles` archives and map fonts must also be
supplied separately; the guide reports missing assets. The final runtime checks,
not the existence of files, determine service health.

For VMS ingestion, configure the VMS publisher after installation:

```text
URL: https://<VAS-server-IP-or-hostname>/api/webhook/<pipeline_id>
Header: X-Webhook-Key: <VAS ingest credential>
```

Read the generated token privately from `secrets/webhook_api_keys`, or issue a
credential in VAS. Trust the VAS public CA certificate on the VMS sender and verify
an actual camera detection. The installer prints these directions but does not
change VMS pipelines, create camera records, or expose the credential in its log.

## After installation

The final report shows results and the deployment log location. The guide prints:

1. The public CA certificate to trust on client PCs: `certs/internal-ca.crt`.
2. The browser URL.
3. The initial admin password file: `secrets/bootstrap_admin_password`. Read it
   privately with `sudo`; passwords are not printed in the wizard or its log.
4. How to check status and logs, then add cameras and confirm a real detection.

New installations use the default `admin` account unless bootstrap configuration
was customized. Existing accounts and passwords remain in effect. Never distribute
`internal-ca.key` or other private keys to client PCs.

```bash
./deploy.sh status
./deploy.sh health
./deploy.sh logs face_recognition
```

If Docker access requires privileges, prefix those commands with `sudo`.
Fix the failing prerequisite and rerun the same guided installation. A dry run
checks what it can without importing images or starting services; it is not a
successful installation or a hardware acceptance test.

## Automation

Build from source:

```bash
sudo ./deploy.sh deploy --yes --image-mode=build \
  --server-ip=10.21.5.22 --public-origin=https://vas.example
```

Load a release without building or pulling Docker images:

```bash
sudo ./deploy.sh deploy --yes --image-mode=load \
  --image-archive=/media/vas-images.tar \
  --deploy-package=/media/vas-release \
  --server-ip=10.21.5.22 --public-origin=https://10.21.5.22
```

Repeat `--image-archive` for additional archives. Add `--offline` when host/package
network installation must also be disabled, or `--cpu` for CPU-only deployment.
Use `--dry-run` first to inspect the plan.

## Installer validation

```bash
bash deploy.sh --self-test
python3 -m unittest discover -s tests -p test_deploy_wizard.py -v
```

These use isolated fixtures and fake Docker calls; the certificate regression
creates temporary certificates only. GPU tests replace package managers, driver
probes and systemctl with stubs; they never install drivers or restart Docker. They verify installer behavior, not a full
installation on a fresh server. Target-host service, GPU and camera checks remain
part of deployment acceptance.

## Where files are saved

Docker named volumes are production-appropriate persistent storage: they survive
container replacement. They are directories on the Docker host, separate from the
container writable layer. Moving to bind mounts does not itself improve durability
or provide high availability. See [Docker volumes](https://docs.docker.com/engine/storage/volumes/).

Run this read-only report from the release checkout:

```bash
sudo ./deploy.sh storage
```

It lists the actual Docker volume locations, or their names before first startup,
plus all model/map destinations. It does not display credential contents. Docker
must be running and accessible. Default locations are inspected rather than assumed
to be under `/var/lib/docker`; a daemon can use a different data directory.

| What | Default storage / files to supply |
|---|---|
| PostgreSQL / Redis | `postgres_data` / `redis_data` volumes |
| Face photos, snapshots, application artifacts | `storage_data` volume |
| Local face indexes | `face_database_data` volume |
| Trained ML service models and manifests | `ml_artifacts_data` volume, shared by API and ML worker |
| Application logs | `logs_data` volume |
| Ollama models | `ollama_models` volume |
| Embedding model caches | `chromadb_cache` / `hf_cache_data` volumes |
| Automatic backups | `backup_data` volume |
| Metrics / Grafana | `prometheus_data` / `grafana_data` volumes |
| Optional vLLM / Milvus | `vllm_models`, `milvus_etcd`, `milvus_minio`, `milvus_data` volumes |
| Face detector ONNX | `<checkout>/weights/det_10g.onnx` |
| Face recognizer ONNX | `<checkout>/weights/w600k_r50.onnx` |
| Trusted weight checksums | `<checkout>/weights/WEIGHTS_MANIFEST.json` |
| Map archives | `<checkout>/map-data/production/*.mbtiles` |
| Map fonts | `<checkout>/map-data/production/fonts/*.ttf` or `*.otf` |
| Map metadata / verification results | `<checkout>/map-data/metadata/` |
| Host deployment backups / configuration snapshots | `<checkout>/backups/` |
| Installer logs / state | `<checkout>/logs/deploy/` and `<checkout>/.deployment/` |
| HTTPS / credentials / environment | `<checkout>/certs/`, `secrets/`, `docker/.env` |

Volume names have the `face_detector_prod_` prefix. Supply both ONNX weights and
the matching trusted manifest before installation, or use `--deploy-package` to
import the release's `weights/`. Do not put the face detector weights into the ML
artifact volume: trained tabular models and face recognition ONNX weights have
different consumers. Maps and fonts are copied by the operator to the paths above;
map content checks still determine whether each archive is usable.

Weights and map archives are mounted read-only into consumers; map metadata has
a separate writable mount. These paths deliberately stay in the checkout in this
release. Preserve them when replacing the checkout. A stable managed checkout is
acceptable; relocating these assets needs corresponding mount and verification
changes, not just moving the files.

### Optional: choose a dedicated data disk before a fresh install

Mount the disk on the host first and ensure it is mounted at boot. In the guide's
storage step choose option 2 and enter, for example, `/srv/vas-data`. The installer
saves `VAS_DATA_ROOT='/srv/vas-data'` in `docker/.env` and includes
`docker/docker-compose.prod.storage.yml`. It creates a directory for each volume:

```text
/srv/vas-data/postgres_data/
/srv/vas-data/storage_data/
/srv/vas-data/ml_artifacts_data/
/srv/vas-data/backup_data/
... one directory for every volume in the table ...
```

These are still named Docker volumes, backed by your chosen host directories.
Container paths and sharing between services stay the same. New directories receive
service ownership; existing files are never moved or recursively re-owned. The
installer refuses a changed layout for existing volumes, nonempty unregistered
data directories, symlinks, and missing directories behind existing volumes. If
a disk is missing, remount it; do not create empty replacements. Storage on a
remote Docker daemon is not supported by this host-directory setup.

For automation, configure `VAS_DATA_ROOT` in `docker/.env` before first installation
(or pass it as an environment variable). `deploy.sh` selects the overlay for all
its Compose operations. If using Docker Compose directly, include the storage
overlay explicitly, and the GPU overlay when applicable:

```bash
docker compose --project-directory docker   -f docker/docker-compose.prod.yml   -f docker/docker-compose.prod.storage.yml config --quiet
```

Prefer `deploy.sh` for installation because direct Compose bypasses its path and
existing-volume checks. Leave `VAS_DATA_ROOT` empty for the default layout. Never
clear/change it on an existing custom-storage deployment to try a different disk.
Relocation is a separate, backed-up migration with services stopped and a verified
restore; this guide does not perform one.

This setting covers the declared production data volumes, not every byte Docker
writes: image layers, build cache and container stdout logs remain in daemon
storage. Checkout files and separately deployed Jupyter storage also remain at
their own locations. Monitor both the data disk and Docker's disk. Keep protected
backups on a separate disk/server and test restores: an on-disk volume or a backup
on the same disk alone does not protect against disk loss.

Storage regression tests (temporary fixtures; no live volume migration):

```bash
python3 -m unittest discover -s tests -p test_deploy_storage.py -v
```
