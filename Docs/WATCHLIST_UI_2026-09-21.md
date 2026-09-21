# Watchlist UI improvements — 2026-09-21

Implemented after deploying and verifying the Security Intelligence fixes.

- Page overview explicitly summarizes only the current filtered page. Cards show eligible identities, UTC-day alerts, all-time alerts, monitoring state and last activity.
- View opens a structured, responsive drawer: list status, matching explanation, metric cards, acknowledgement backlog, latest eight alerts, all entries with eligibility/expiry/priority, addition controls and timestamps.
- Recent alerts resolve camera names through `/api/pipelines`; raw camera UUIDs are not displayed. Missing names show “Camera name unavailable”; missing camera references show “No camera recorded”.
- Entries include inactive and expired records with clear labels. Deleted lists hide modification controls. Load more appends entries; additions/removals refresh the drawer counts.
- Shared ModalStack manages nested confirmations, focus, Escape and background scrolling. Async cancellation prevents closed/replaced drawers from displaying stale responses. Timeouts show errors.
- Existing backend APIs and data storage are unchanged. No migration is required.

Validation: 67 Python watchlist/layering checks passed, one existing test skipped; four jsdom runtime checks passed; Chromium checked desktop (1440px) and mobile (390px) views, entry pagination, confirmation/cancel behavior, focus containment, no horizontal overflow, and clean console. UI browser fixtures use synthetic data and intercept all requests; no production watchlist mutations were performed.

Screenshots and browser probe: `logs/watchlist-ui-20260921/` (local ignored artifacts). Runtime test: `tests/admin_watchlists_ui.test.cjs` (requires jsdom in the test environment).

Deployment completed on 2026-09-21. Production API image `5b7bad47ae6c` includes all three watchlist files with `wl-3` cache versions. Container file hashes and public CSS/JS hashes match the workspace. Authenticated page, list, detail, entry, statistics, recent-alert and camera-name source endpoints returned HTTP 200. Production health is healthy. Existing runtime environment, bind mounts and device requests were preserved; the ML worker did not require a restart for this frontend release. Rollback image: `face_detector_prod-face_recognition:before-watchlist-ui-20260921`.
