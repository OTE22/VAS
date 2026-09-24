# Dataset pipeline diagnostics and JupyterLab

In **ML Operations → Overview → Work in progress**, expand **Pipeline diagnostics & notebook** on a dataset job. New builds record configuration, source counts, extraction, label matching, feature selection, validation, splitting, population checks, artifact writing, manifest writing and registration. The timeline includes timings, row counts, exclusions, failed checks and application code locations. Full exception messages and locals are not copied into diagnostics; use the job ID to correlate worker logs. Process termination leaves the last stage interrupted. Historical jobs are explicitly marked as lacking stage evidence.

**Download debug notebook** exports a valid `.ipynb` for a dataset job, including failures before a dataset exists. The dataset detail drawer also offers an export. Export requires the existing ML management capability. It performs no extraction, training or database writes.

New saved datasets include frozen validation feature definitions, split fractions and helper-source hashes. The notebook uses the same pure validation, fingerprint and split source as the application. It verifies the Parquet file and logical checksums before inspecting rows, then runs validation and split checks in separate cells. A failed assertion or exception identifies the cell to debug. Enable JupyterLab's debugger and set a breakpoint in a helper cell to inspect intermediate variables.

Exports are evidence and snapshot rechecks, **not replays of database extraction**. Pre-label source rows are not retained. A build that failed before writing a snapshot has a diagnostics-only notebook. Legacy builds may lack definitions, file hashes or helper hashes; cells explain missing prerequisites rather than infer them. To obtain complete new evidence, run a new build. If helper hashes changed, use the matching application release. Existing datasets are never rewritten.

## Start the separate workspace

The notebook service uses its own network, no database/API credentials, a read-only artifact volume, a persistent writable workspace and CPU/memory limits. A small proxy publishes its port on localhost only; the kernel stays on the internal network without outbound access. It is a single-administrator workspace; use separate instances or a managed JupyterHub deployment for independent users.

Create a strong token in a protected file **outside the repository**. Grant the notebook UID (1000) read access; do not make it world-readable. Set `NOTEBOOK_TOKEN_FILE` to its absolute path. For example, on a UID 1000 deployment host:

```bash
install -d -m 700 "$HOME/.config/vas-notebook"
(umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "$HOME/.config/vas-notebook/token")
export NOTEBOOK_TOKEN_FILE="$HOME/.config/vas-notebook/token"
docker compose --env-file /dev/null -f docker/docker-compose.notebook.yml up -d --build
```

The default read-only volume is `face_detector_prod_ml_artifacts_data`. Override `NOTEBOOK_ARTIFACT_VOLUME` for a different environment or a dedicated snapshot copy. Never mount the Docker socket, application secrets or the application repository into a notebook kernel.

For remote access, tunnel from your workstation:

```bash
ssh -L 8888:127.0.0.1:8888 administrator@your-server
```

Open `http://localhost:8888/lab`, sign in with the token, upload the downloaded notebook, and run its cells. Its default `ARTIFACT_ROOT` is `/artifacts`. The workspace shuts down after one idle hour and disconnected idle kernels are culled after 30 minutes; restart with Compose when needed. Stop it with `docker compose --env-file /dev/null -f docker/docker-compose.notebook.yml stop`.

To show **Open JupyterLab** in ML Ops, set `ML_NOTEBOOK_URL` in the API deployment to the separately authenticated HTTPS workspace URL. For an SSH-only workflow, `http://localhost:8888/lab` is allowed, but every browser user needs their own tunnel. URLs containing credentials, query tokens or fragments are rejected. No service is automatically exposed through the production reverse proxy. Do not disable Jupyter token authentication or XSRF protection.

The notebook contains metadata and executable helper source, not embedded dataset rows or credentials. Running it displays records from the mounted snapshot; clear outputs before sharing it. Rechecks cannot approve a model or modify the production registry.
