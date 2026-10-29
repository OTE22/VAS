# Isolated PaddleOCR GPU worker

This worker now serves the [vehicle investigation pilot](vehicle-workspace.md).
IT-DIRECT-CAM-1 vehicle events are durably accepted by VAS, localized by the
separate LPR GPU worker, then submitted here as plate crops. Face processing
continues through the existing VMS webhook and VAS face workflow.

## Selected models

| Stage/profile | Explicit model |
| --- | --- |
| Text regions inside a plate crop | PP-OCRv5_mobile_det |
| Arabic and related scripts | arabic_PP-OCRv5_mobile_rec |
| English letters and numbers | en_PP-OCRv5_mobile_rec |
| Latin-script languages | latin_PP-OCRv5_mobile_rec |

Choose the profile explicitly (`ar`, `en`, `latin`). This service does not
guess a language or select the largest confidence score across unrelated
recognizers. Test the relevant scripts and numeral styles on real plate crops.
The models are general OCR models, not a guarantee of license-plate accuracy.
Recognition scores are model scores, not calibrated probabilities that a plate
number is correct. Mixed Arabic/Latin plates may need separate region-specific
readers and country-format validation during operator review.

The detector locates individual text regions **inside an already cropped
plate**. It does not replace LPR.PT's plate localization. Outputs preserve
each text region, polygon, and recognition score without inventing a combined
plate number or a Known Car match. All results require review during this pilot.

## Isolation and worker management

- Separate Compose project `vas_plate_ocr`; independently managed GPU workers.
- One process, one visible GPU, one inference thread; all models loaded once.
- Queue holds four waiting requests. Full queues return 429 with Retry-After.
- Starts at most four jobs/second; each job uses recognition batch size one.
- Inputs are JPEG/PNG plate crops up to 2 MB, maximum side 1280 and 921600 pixels.
- Detector inference has maximum side 640; no full-resolution camera-frame OCR.
- CPU limit 2 cores, container RAM limit 4 GB, `/dev/shm` 256 MB.
- GPU memory grows as needed. The RAM limit is **not a GPU-memory quota**.
  The RTX 5090 is shared with existing workloads; serialized execution and
  bounded inputs reduce contention but do not guarantee GPU isolation.
- No published ports; private internal Docker network, bearer-authenticated API, read-only filesystem.
- A worker-only bearer secret is mounted; no camera credentials, production storage, database or Redis mounts.
- Official model files are downloaded during image preparation and included in the image.
  Runtime inference is offline. Model hashes are in `models/manifest.json`;
  resolved package versions are in `installed-packages.txt` inside the image.
- Explicit GPU selection and real inference warmups gate startup. CPU fallback
  is not accepted. Health reports failed/stalled inference as unavailable.
- Process crashes restart automatically. Docker marks a stalled/unhealthy
  process unhealthy; it does not automatically restart merely for that status.
  Inspect its logs and restart this worker if recovery is needed.

## Build/start

```bash
python3 docker/plate-ocr/download_runtime.py
python3 docker/plate-ocr/download_cuda.py
python3 docker/plate-ocr/download_models.py --output docker/plate-ocr/wheels/models
docker compose --env-file /dev/null -f docker/docker-compose.plate-ocr.yml build
docker compose --env-file /dev/null -f docker/docker-compose.plate-ocr.yml up -d
docker compose --env-file /dev/null -f docker/docker-compose.plate-ocr.yml ps
docker compose --env-file /dev/null -f docker/docker-compose.plate-ocr.yml logs --tail 60
```

`PLATE_OCR_GPU_DEVICE` accepts the GPU index or UUID and defaults to GPU 0.
There is currently one GPU on this host. This standalone Compose file does not
load the production `.env` or modify its GPU-allocation file.

## Test

```bash
docker compose --env-file /dev/null -f docker/docker-compose.plate-ocr.yml exec -T plate_ocr \
  python -m unittest -v test_worker
docker compose --env-file /dev/null -f docker/docker-compose.plate-ocr.yml exec -T plate_ocr \
  python smoke_test.py
```

Smoke tests use synthetic English, Latin and Arabic text, not retained camera
photos. They verify inference execution and the expected reading in each profile; their
timings include the intentional rate limiter and are not an accuracy benchmark.
The Arabic fixture is rendered with RAQM shaping for systems whose Pillow build
lacks Arabic shaping support; drawing unshaped Arabic would invalidate the test.

Loopback API: `POST /recognize?profile=ar&camera=IT-DIRECT-CAM-1` with raw JPEG/PNG
bytes. `GET /health/ready` reports GPU, models, queue depth, completed/failed jobs,
and `live_ingestion: false` (this OCR service itself does not ingest camera events). No URLs, filesystem paths or PDFs are accepted.
VAS owns the durable job store, event deduplication, captured timestamps, evidence
storage, and bounded retries. The LPR worker sends `Authorization: Bearer ...`
from the mounted worker secret. Health is unauthenticated only on the private
network; OCR requests require the token. The smoke script reads it from the
mounted file without displaying it.

Stopping this Compose project stops both new GPU workers. Disable the vehicle
pilot first; existing vehicle jobs remain stored and may need retry after a
long outage. This does not stop VAS or VMS.

Before operational reliance, benchmark exact plate readings on labeled day/night
camera samples, language profiles, mixed scripts, and multiline layouts. Measure
face processing and GPU latency under concurrent real traffic. The current
pilot feed did not provide a fresh frame during deployment verification.

## Installation validation — 2026-10-27

- Installed PaddleOCR 3.7.0, PaddleX 3.7.2 and PaddlePaddle GPU 3.3.1 / CUDA 12.9.
- RTX 5090 (compute capability 12.0): actual GPU tensor execution and warmup of
  all four local models succeeded, with runtime network disabled.
- Five worker-control tests passed; `pip check` reported no broken requirements.
- Nine synthetic requests succeeded. English `B 123456`, Latin `AB 123 CD`, and
  Arabic `لبنان ١٢٣٤٥` were read exactly. This is not a field accuracy claim.
- Observed process GPU memory: 654 MiB after inference. Container host-memory
  usage was about 1.3–1.5 GiB during validation, below its 4 GiB limit.
- First reported inference times were approximately 40 ms (English), 11 ms
  (Latin), and 10 ms (Arabic). HTTP median was about 249 ms because requests
  are deliberately limited to four starts per second. These small synthetic
  samples do not establish latency or peak memory under production load.
- Production VAS, VMS, ML worker, PostgreSQL and Redis remained healthy. Only
  the new OCR container was created/recreated for this installation.
- These measurements describe the initial isolated installation. The subsequent authenticated integration and pilot deployment are documented in the vehicle workspace guide; labeled real-camera evaluation remains outstanding.

Official references:
- https://www.paddleocr.ai/main/en/version3.x/module_usage/text_detection.html
- https://www.paddleocr.ai/main/en/version3.x/module_usage/text_recognition.html
- https://www.paddleocr.ai/main/en/version3.x/paddlepaddle_installation.html
