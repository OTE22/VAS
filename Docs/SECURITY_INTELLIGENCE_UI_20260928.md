# Security Intelligence interface update — 28 September 2026

The identity picker was clipped by the tab scroll container when it opened upward.
It now uses a native modal dialog above the page, with an opaque background,
72px desktop portraits, visible search/filter controls, independently scrolling
results, a selection count, Clear selection and Done. All seven identity pickers
and the advanced camera multi-picker use the same component. Keyboard navigation,
Escape, focus return and selected-state accessibility were checked.

Map fixes preserve the MapLibre canvas on repeated Load Map clicks, resize it
when returning to the tab and clear failed controller state before retrying.
The network and pattern tabs now explain how to start analysis instead of showing
a blank graph or a loading spinner before any request has started.

Validation: 7 JavaScript regression tests and 44 frontend layering/script-order
checks passed. Isolated Firefox checks covered a short 1266x574 viewport and a
narrow 500x758 viewport, selection, pagination, server search, keyboard selection,
pattern cards/details, anomaly results, threat assessment, trajectory/correlation
controls and all tab layouts. Real MapLibre rendering was exercised with synthetic
routes and heatmaps, repeated loads, tab return, overlay flags and 503 recovery.
No browser JavaScript errors were observed. These are interface checks with
sample data, not validation of detection accuracy or real-world threat scores.
No production learning jobs or synthetic identity records were created.

The updated HTML is installed in the running API container, and nginx serves the
updated JS/CSS from its frontend bind mount. Cache versions were increased.
Hashes of both assets were verified over TLS from VMS. The GPU image was rebuilt
and its embedded HTML/JS/CSS hashes verified, preserving the update for future
container recreation. The API did not need a restart and remains healthy.

Rollback image: `face_detector_prod-face_recognition:before-security-picker-20260928`.
The previous served HTML and temporary browser screenshots are under
`/tmp/security-ui/` on this workstation. Frontend source changes remain reviewable
in Git; reverting a frontend release also requires restoring the host bind-mounted
JS/CSS, not only the API image.
