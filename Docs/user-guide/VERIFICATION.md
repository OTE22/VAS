# Verification record

Date: 7 October 2026. Application reports VAS 5.0.0. The tutorial reports backend build `817b58e9460749bf53ef3a6fc12e024a6693d231`, development mode. Frontend source came from working tree `52135d8-dirty`.

## Environment and safety

The user authorized an isolated local test instance with synthetic data. A separate database, cache, application, TLS proxy, storage directory and internal container network were used. The deployed API image was reused; the frontend came from the current checkout. No production records, settings or notifications were changed for this documentation.

Fixtures: three demonstration cameras, fourteen synthetic identities, 210 seeded observations and flat silhouette avatars. No real person was identified. Synthetic account/password rotation, camera location, display-name, watchlist, alert-rule, setting and credential actions occurred only in the disposable instance. The completed retention task was a **dry run**, not manually initiated deletion. Automatic fresh-instance cleanup jobs also appear in the execution history.

## Demonstrations completed

- Administrator login, mandatory password rotation, logout and re-login.
- Standard-user creation, first-login rotation, assigned-camera listing and redirect away from user administration. This was not an exhaustive authorization audit; aggregate counters are not proof of camera-level isolation.
- Pipeline location save/refresh; unknown filtering and detail opening; known-name update and profile verification.
- Movement overview, camera lanes, linked sighting/photo navigation and enlarged photo viewer.
- Watchlist creation and member save, verified in the named watchlist member panel.
- Live-alert rule creation, health inspection and pause. No trigger or outbound notification was sent.
- Negative image-quality/search flow: the synthetic silhouette produced no detected face. History was created; CSV export downloaded two synthetic rows with nine columns.
- Sender credential issuance and list verification; secret redacted. Clipboard transfer, sender ingestion and revocation were not tested.
- Retention setting saved/refreshed; retention dry run reached COMPLETED.
- Chatbot audit and application-log filtering.
- Social graph and suspicious-pattern generation; anomaly insufficient-baseline behavior; threat result/provenance and persistence; advanced status and empty saved-threshold review.
- ML worker/readiness gate and similarity dataset requirements; tutorial navigation.

## Observed limitations and remaining tests

- The identity profile displayed **Untitled watchlist** although the named list's member view confirmed the save.
- Watchlist and live-alert dialog submit buttons had poor text contrast. Genuine screenshots retain the issue; the application was not redesigned for documentation.
- Alert health reported **SNAPSHOT EXISTS: PROBLEM** with the custom fixture storage, despite the profile image rendering. This is a test-instance observation, not a conclusion about production snapshots.
- Anomaly analysis lacked earlier baseline history. Its negative readiness result is not a positive anomaly validation.
- The isolated ML worker, basemap dependency and assistant language model were unavailable. No training, tuning, notebook execution, model deployment, drift measurement, working basemap or assistant answer was demonstrated.
- The advanced database inspector did not populate cameras or produce a preview in this run. Do not treat displayed dataset options as extracted training data.
- Positive biometric matching, enrollment completion, promotion, merge, deletion, notification delivery, backup restoration, live ingestion and camera capacity remain untested.
- Observer and Analyzer options were visible; complete role boundaries remain untested.
- The Intelligence page was observed, but its tab/selector walkthrough was not completed. Identity-profile journeys were tested independently.
- `/admin/audit` is **Chatbot audit log**, not an all-actions administrator audit.
- No dedicated VAS video recording/playback archive, vehicle/license-plate workflow or multi-site orchestration was established. See the capability register for explicit gaps.

## Document quality checks

- 144 PDF pages rendered to review images and visually inspected; corrected pages re-inspected after regeneration.
- 33 workflow headings and all eight section destinations present; 47 internal PDF links checked.
- All 76 figures have their source, PNG, SVG and coordinate assets; every written step has a matching numbered arrow. Context/result figures are distinguished from action figures.
- Automated text/image page-bound checks found no clipping outside page margins; no invalid internal destinations or missing evidence assets were reported.
- Table-of-contents and task-finder page references stabilized after PDF rendering.
- Password/token redaction was applied to the underlying raster images before cropping or SVG generation. Distributed text and DOCX XML were checked for private test secrets and infrastructure addresses.
- DOCX is the editable source for the matching LibreOffice-rendered PDF. Canonical JSON/build scripts and editable annotation assets are included for maintenance.

This is documentation verification, not a certification of production performance, security, recognition accuracy or every optional subsystem.
