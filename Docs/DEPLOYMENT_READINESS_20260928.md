# VAS deployment readiness — 28 September 2026

## Verdict and deployment state

The updated GPU image and nginx settings are deployed and healthy for the current
30-camera VMS target. This is not certification of 30 or more physical cameras
at 5 analysis FPS with every feature active. Representative cameras have not
been added yet, and a complete HTTP/recognition/database/storage endurance test
has not been performed.

The audit initially built and tested the fixes without restarting VAS. Following
explicit deployment authorization, **the API and nginx were recreated successfully
on 28 September 2026 at approximately 10:36 UTC**. The fixes are now live.
The acceptance limits below still apply.

- Built image: `face_detector_prod-face_recognition:latest`,
  `sha256:d370a948d5f35002d9b0143eb7b246dafb23fc5ce1dac438a95b903e683870f4`.
- Previous API image at initial audit time:
  `sha256:cedbd26bf06686a6e009bd19cde4cbec42bc595b31d990ef2524896bb854418a`.
- No database records, camera configurations or credentials were changed.
- Unit tests used fake events and isolated processes, never live webhook uploads.

## Deployment completed

- Running API now matches the prepared `d370a948d5f3...` image. Both modified
  receiver source files were hash-checked against the reviewed working tree.
- API and nginx healthy; `/health/ready` and `/health/detailed` passed.
  All 34 supervised background services report no degraded or stale services.
- CUDA available = 1; CPU fallback active = 0; queue capacity = 2,000;
  pending and processing counts = 0 after deployment.
- nginx configuration passes with no open-file warning; soft/hard limit 16,384.
- Both VMS workers passed health, CUDA execution, NVDEC prerequisite and
  TLS/authenticated VAS status checks. Invalid credentials returned 401 and
  a valid credential with an unknown status handle returned 410 as intended.
- Both existing saved VMS pipelines remain stopped. No test detection images
  were sent to the live receiver.
- Verified backup: `/backups/20260928T102848Z` in the production backup volume;
  checksums passed for database, gallery, indexes and ML artifacts.
- API rollback tag:
  `face_detector_prod-face_recognition:before-readiness-deploy-20260928`,
  digest `sha256:35dd4c38e67975f9e4e33e6cf73325a0e5e2d03d39d14a29ba58b62611f1ae42`.
  Docker no longer retained the original image layers, so this is an imported
  snapshot of the prior running filesystem with explicit nonsecret startup
  metadata. Source hashes match the prior receiver; mounted credentials are
  excluded. Isolated SCRFD and ArcFace inference on CUDA also passed in this
  rollback image. Production Compose supplies its normal environment and secrets.
- nginx rollback tag: `nginx:before-vas-readiness-deploy-20260928`.
- No schema migration or datastore recreation was needed for this update.

## Verified on this host

RTX 5090, 32,607 MiB VRAM, NVIDIA driver 595.91.07, 20 CPU cores and approximately
123 GiB system RAM. VAS shares this GPU with VMS; its API has a 4-CPU / 8-GiB limit.

| Check | Result |
| --- | --- |
| Deployment validation | PASS: host prerequisites, TLS, offline policy, model checksums and merged Compose configuration |
| API readiness | Ready; required components healthy; no degraded/stale background services |
| Inference runtime metrics | CUDA available = 1; CPU fallback active = 0 |
| Queue at audit time | 0 pending images, 0 processing; capacity 2,000 |
| Saved and Compose tuning | WORKERS=1; INFERENCE_WORKERS=4; MAX_CONCURRENT_INFERENCE=4; QUEUE_WORKERS=15; per-pipeline inference limit=2 |
| Database migration | `ff17b8c9d0e1`, matching the source head |
| Focused regression checks | 153 passed in an isolated network-disabled container, including deployment and receiver tests |
| Deploy shell self-test | 64 passed; also covered by the focused suite |
| GPU image build | PASS |

The image was also checked separately for receiver regressions and exclusion of
host credentials, backups and deployment state. Test details are recorded in
`/tmp/vas-readiness-regression.log`, `/tmp/vas-baked-image-tests.log`, and
`/tmp/vas-readiness-build.log` on the audited workstation. These are temporary
local evidence files, not part of the deployment package.

## Corrections prepared by this audit

1. The first feedback-enabled HTTP 202 response now returns `status_path`.
   VMS can immediately poll with GET, instead of uploading the JPEG again to
   discover the handle. HTTP 202 remains acceptance, not a committed face result.
2. Feedback metadata previously retained only 5,000 results for 600 seconds.
   That exhausted its capacity at sustained rates above approximately 8.3
   events/second, even with fast processing. It now holds at most 100,000
   metadata records, with efficient expiration in timestamp order. Unexpired
   records are preserved; a full cache still applies backpressure. Expired
   records cannot be revived by a late completion.
3. The regression test exercises 99,000 metadata events over eleven simulated
   minutes at 150/second. It is a cache correctness test, not a throughput or
   face-recognition benchmark.
4. Production nginx now declares soft and hard `nofile` limits of 16,384.
   The initial container had a soft limit of 1,024 despite
   `worker_connections=8192`. After deployment, both actual limits are 16,384.
5. Docker build exclusions now cover host backup directories, deployment state,
   nested deployment environment files and generated GPU allocation. The GPU
   allocation stays a host-specific Compose overlay.
6. Fixed the stale single-GPU deployment self-test and documented the notebook
   setting and the difference between development and production concurrency.

## Capacity interpretation and constraints

- VMS receives RTSP, uses NVDEC for supported streams and runs person tracking.
  VAS receives selected images/crops over HTTP and performs face processing.
  VAS does not decode the 30 RTSP streams itself.
- 30 cameras at 5 analysis FPS means 150 VMS analysis frames/second. It does
  **not** establish 150 complete VAS recognition/database writes per second.
  Face-event traffic depends on people per scene, capture policy and retries.
- nginx allows 60 ingest requests/second and 120 status requests/second **per
  source IP**, plus bursts. Posting every camera frame from one source at
  150 requests/second exceeds that ingest budget. The intended configuration
  uses selective tracked-person captures, not unconditional frame uploads.
- Keep VAS `WORKERS=1`. Feedback, deduplication and other coordination remain
  process-local. Additional API workers/replicas are not a supported shortcut.
- Some pools and queue objects capture settings during module import, before
  database hydration. The checked deployment has matching Compose and saved
  values. Before deployment on an existing database, align both sources for
  inference workers, concurrency and queue capacity; do not assume a saved
  setting alone resized those resources.
- Receiver queues and feedback are in memory. Pause sending and drain before
  planned restarts. Sender retries help recover interruptions, but this is
  not exactly-once persistence across crashes. A 202 response alone is not
  proof the evidence reached the database.
- Keep heavyweight ML training and GPU LLM workloads out of the shared-GPU
  camera capacity test. Current production Ollama settings force CPU use.
- A bounded image count is not a byte-based memory limit. Monitor actual crop
  sizes, memory, queue growth, disk latency and database latency under crowds.

## Controlled rollout

1. Record running image IDs and retain rollback tags; take a verified normal
   backup using `sudo ./deploy.sh backup`. Preserve existing secrets and data.
2. Pause camera sending and wait for both `queue_size` and `processing` in
   `/health/detailed` to reach zero. Include pending batch writes when draining.
3. Run `sudo ./deploy.sh validate --quiet`. For a full production upgrade use
   `sudo ./deploy.sh upgrade`, which has the repository's backup/migration gates.
   Preserve the generated GPU overlay and the existing deployment environment.
4. For this receiver-only update (no schema changes), the prepared image can be
   applied with the existing three Compose files and a targeted recreation:

   ```bash
   sudo docker compose \
     -f docker/docker-compose.prod.yml \
     -f docker/docker-compose.prod.gpu.yml \
     -f docker/gpu-allocation.generated.yml \
     up -d --no-deps --wait --wait-timeout 240 face_recognition

   sudo docker compose \
     -f docker/docker-compose.prod.yml \
     -f docker/docker-compose.prod.gpu.yml \
     -f docker/gpu-allocation.generated.yml \
     up -d --no-deps --force-recreate --wait --wait-timeout 60 nginx
   ```

   Run from the VAS root. nginx recreation applies the file limit and refreshes
   its upstream address after API recreation. Expect a brief maintenance gap.
5. Verify `/health/ready`, model CUDA/fallback metrics, nginx configuration and
   actual file limits, both VMS workers' authenticated status connectivity, and
   the running API image ID before resuming cameras.

## Remaining production acceptance

Add representative real cameras in stages (5, 15, 30, then beyond 30). Use the
intended resolution, codec, lighting and crowd density. Run an extended soak
(e.g. 24 hours) with the required previews, alerts, tracking, recognition and
journey views active. Agree acceptable latency and error budgets first.

Measure actual per-camera analysis FPS, recognition accuracy, end-to-end
capture-to-commit latency, 429/503 rates, queue stability, GPU/CPU/RAM, storage
and database load. Exercise camera disconnect/reconnect, sender retry and
planned restart recovery. Increase the camera count only after those measures
pass; the isolated synthetic VMS/model benchmarks are useful evidence, not a
replacement for this acceptance test.
