# Production training build provenance

`require_clean_git` accepts either a clean Git checkout or verified release-image evidence. Production images continue to exclude `.git`, credentials and runtime data.

`deploy.sh` records the checkout revision, dirty state and application source hash automatically when it builds images. It does not commit changes or call a dirty tree clean. Before a release build, review and commit intended changes and leave the checkout clean (including untracked files reported by Git).

For a direct Compose build from the repository root:

```bash
provenance_output=$(python3 backend/ml/build_provenance.py args) || exit 1
mapfile -t provenance_args <<< "$provenance_output"
docker compose -f docker/docker-compose.prod.yml \
  -f docker/docker-compose.prod.gpu.yml \
  -f docker/gpu-allocation.generated.yml \
  build "${provenance_args[@]}" face_recognition ml_worker
```

The CPU deployment omits the two GPU overlays. Generate arguments immediately before building, after edits finish. Do not manually force `VAS_BUILD_GIT_DIRTY=false` or supply another revision.

Both Dockerfiles seal `/usr/local/share/vas/build-provenance.json` after application/dependency installation. It contains the commit and actual dirty state, verified context source hash, requirements hashes and installed package versions. The path is outside writable application/data mounts, owned by root and read-only to the application. No environment export is involved. The image labels expose the revision, dirty state and source hash. `build_id` is the manifest's SHA-256; it is not the Docker image digest or a cryptographic signature.

At training preflight, the running source and package versions must match the sealed build. Unknown revisions, dirty builds, missing manifests, changed source and changed dependencies remain unverified. A live Git checkout takes precedence, so mounting a dirty checkout cannot hide behind a clean image manifest. Direct builds without recorded arguments remain runnable but cannot satisfy the optional clean-code training requirement.

Check a running image without importing application startup:

```bash
docker exec face_detector_prod-ml_worker-1 \
  python backend/ml/build_provenance.py check
```

Use the service container's actual name. Exit code 0 means verified; exit code 1 includes a specific status and remedy. API and worker must both be rebuilt/recreated from the intended release. Do not hot-copy Python or frontend source into a sealed image and expect its identity to remain verified.

Standalone regression tests (no application, database or network):

```bash
python3 tests/isolated_ml/test_build_provenance.py
```
