# Vehicle investigation pilot

Open **Management → Vehicles / Known Cars**, or `/admin/vehicles` as an administrator.
The admin privileges endpoint explicitly includes this Management link; the navbar uses a versioned script and refreshed session menu cache so a page reload picks up menu updates.

## Operator workflow

- **Sightings:** filter by plate text, camera, vehicle type, review state, processing state, and a From/To date-time interval. Entered times use the browser's displayed timezone; stored/report timestamps are UTC.
- **Review:** open a vehicle card. Compare vehicle, plate crop, full scene, and a Known Car reference. Correct the plate and choose **Confirm & next**, **Unreadable & next**, or reject a selected car association. The modal stays open. The filmstrip and arrow keys move within the loaded results. Decisions have an audit trail and reject stale record versions.
- **Known Cars:** create a reference with plate, country, make, model, color, year, notes, and a photo. References can be archived. Link a known person only with a reviewed relationship and evidence; camera proximity does not automatically establish a driver or owner.
- **Search by photo:** returns plate-text matches separately from possible visual matches, including sightings with unreadable plates. These are candidates for review. The current visual descriptor is a generic YOLO feature vector, not a validated vehicle re-identification model or an identity probability.
- **Excel:** exports the filtered sightings, with a criteria sheet, UTC timestamps, review state, and reference specifications. Image-search export includes the returned candidates only. Ordinary exports are capped at 10,000 rows; narrow the interval for larger sets. Photos remain accessible in VAS rather than being embedded in the spreadsheet.

## Pilot pipeline

```text
IT-DIRECT-CAM-1 → existing VMS YOLOv8n / tracked event publisher
  ├─ person → existing VAS face workflow
  └─ car / truck / bus / motorcycle → same authenticated webhook
       → durable vehicle row + scene and vehicle images
       → shared GPU vehicle worker: uploaded LPR.pt + appearance descriptor
       → shared PaddleOCR GPU worker: en / ar / latin
       → stored plate crops + raw text / scores → operator review
```

Only pipeline `4c79dc55-8ef0-4cbe-bbd6-dcd01d4d755b` is permitted to enable vehicle processing. Other cameras remain disabled. The pilot starts with English OCR and country `LB`; select Arabic or Latin in **Pipeline flow** when appropriate. Profiles are explicit, not automatic language detection. Multiline/mixed-script plate interpretation requires operator review.

VMS keeps its existing multiclass camera model and webhook, including original frame imagery. Its uploaded LPR artifact is copied into the private vehicle worker, with its hash recorded in `docker/vehicle-vision/models-manifest.json`. This permits face and vehicle processing from the same camera without replacing its face/person pipeline. No VMS camera configuration change is required.

The five additive tables are `known_vehicles`, `vehicle_pipelines`, `vehicle_sightings`, `vehicle_person_links`, and `vehicle_audit`; migration head is `a027b001cars`. Existing face tables are unchanged. Person links preserve a name/ID snapshot if the corresponding identity is later deleted. Images are stored as separately named JPEGs under the existing backed-up storage volume's `vehicles/` directory. Scene images preserve the captured visual content; they are JPEG re-encodes, not byte-for-byte source archives. OCR failure does not delete a sighting or its images.

Vehicle events require a stable event ID and are committed before HTTP acceptance. Retries deduplicate by pipeline/event/detection. Processing uses database leases, five attempts, and bounded retry delays; failed items expose a retry action. GPU inference runs outside the face queue. A worker outage preserves evidence and pending/failed jobs.

## Runtime and deployment

The API joins the private, internal `vas_vehicle_processing` network. Neither GPU worker publishes a host port. Both require a bearer secret from `secrets/vehicle_worker_token`; never put that value in source control or logs. The API alone accesses the vehicle database/storage; workers receive images, not camera URLs or database credentials.

Worker services are in `docker/docker-compose.plate-ocr.yml`, project `vas_plate_ocr`. Each has 2 CPU / 4 GiB host-RAM limits, one GPU execution at a time, read-only roots, and 256 MiB shared memory. These are not VRAM quotas. See [OCR worker details](plate-ocr-worker.md).

Production uses the existing production/GPU/allocation files plus `docker/docker-compose.vehicles.yml`. `VEHICLE_PILOT_ENABLED=1` in the protected production environment tells `deploy.sh` to retain this overlay during later deployments. `MIGRATIONS_EXPECTED_HEAD` must match the deployed migration head. Worker deployment remains a separate Compose project and must be started first.

The current incremental release is `vas-vehicles:20261027`; a fresh build uses the ordinary GPU Dockerfile with the new sources. The pre-change API image is tagged `face_detector_prod-face_recognition:before-vehicles-20261027`. A database dump and original production environment were saved privately in `backups/vehicles-20261027/` before migration. Do not downgrade/drop the new tables to disable the feature: that would remove retained vehicle evidence. Disable the pilot in the dashboard instead. Existing queued jobs may finish; the face workflow remains active.

## Validation on 2026-10-27

- Actual migration and API integration tests passed against a disposable PostgreSQL/pgvector database: authorization, CSRF, normalized duplicate plates, photo upload, reviewed person links, edit conflicts, concurrent webhook deduplication, image retrieval, time filters, search including unreadable plates, review/rejection, XLSX, worker retry and expired leases.
- Five OCR control/authentication tests passed. Nine authenticated synthetic GPU requests read English, Arabic, and Latin examples exactly. Earlier burst tests verified queue rejection and recovery.
- A full synthetic event passed durable ingestion → LPR.pt GPU → PaddleOCR GPU → stored crop/text/256D descriptor; `B123456` was read and left awaiting operator review, with no automatic Known Car link.
- Firefox desktop/mobile tests passed: images load, two consecutive review decisions retain the modal, no horizontal overflow, and no JavaScript errors. Rapid navigation is guarded against stale responses.
- Live authenticated dashboard, Known Cars, sightings, and pipeline endpoints returned 200; anonymous vehicle access was denied. VAS readiness reported 34 background services with no degraded/stale services after the first rollout. Final readiness after the UI update again reported 34 services, with none degraded or stale.
- Observed final GPU usage was 5,388 MiB of 32,607 MiB across the host. Each new worker used about 1.4 GiB host RAM. This idle/light-test snapshot is not a sustained traffic benchmark.
- The VAS API and ML worker were restarted for the release. VMS, PostgreSQL, and Redis were not stopped or disabled. Temporary model-inspection dependency changes inside VMS were removed.

**Field limitation:** a separate direct read from IT-DIRECT-CAM-1 timed out, and its saved VMS preview was last updated at 06:55 UTC during the 12:20 UTC check. No fresh live vehicle event was available for camera accuracy validation. Synthetic success does not establish day/night accuracy, plate readability, or sustained performance alongside active face traffic. The pilot is ready to receive fresh events when the camera feed recovers; review actual labeled day/night plate examples before relying on OCR operationally.

Test entry points: `tests/vehicle_workflow_integration.py` (disposable `vehicle_tests` only), `tests/vehicle_gpu_smoke.py` (opt-in real private GPU workers, disposable DB and DejaVu font), and `docker/plate-ocr/test_worker.py` / `smoke_test.py`. Never point the test database setup at production.
