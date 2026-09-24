# Dashboard — live feeds

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/dashboard` at `https://face-detector.internal`. **Access:** Signed-in active user; data limited to permitted pipelines.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Live detection cards grouped by pipeline, face previews, session alert history, sound controls, and a persistent inbox grouping live-alert and watchlist detections.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Face image (single click) | Open a larger detection preview with camera, time, and similarity. |
| Face name / image double-click | Resolve the known person and open the full identity profile if available and permitted. |
| Enable Alert Sound / mute toggle | Opt in to or mute browser alert audio; browser interaction is required before audio can play. |
| Test sound | Try the dashboard alert tone; this does not create a detection or a database alert. |
| Popup Acknowledge | Close the popup only. This control does not acknowledge the persistent inbox record. |
| View History / alert-count button | Open or toggle this browser session's popup history. |
| History row | Replay its preview. |
| Close history / Escape / popup backdrop | Dismiss the history panel or preview. |
| Review alerts / Hide alerts | Expand or collapse the persistent Detection alerts inbox. |
| Inbox details disclosure | Expand the grouped alert's evidence and instructions. |
| Inbox View person | Open the linked identity profile. |
| Inbox Acknowledge | Save acknowledgement on the server for the displayed group through its latest displayed event. Refresh the inbox after success; newer events can still appear. |
| Inbox Previous / Next / Refresh alerts | Browse groups in pages of 50 or reload current server state. |
| Ctrl+R | The page intercepts this shortcut to reconcile displayed cards and pipeline names. Use the browser's reload control for a full page reload. |

## Demo

1. Arrange one approved test detection from an existing camera pipeline.
2. Open Dashboard and confirm the camera card updates. Click the face image to inspect the preview.
3. Enable Alert Sound and click Test sound; expect audio only if the browser allows it.
4. If the test person belongs to an active watchlist or live alert, expand Review alerts.
5. Read the evidence, click the inbox Acknowledge, then Refresh alerts. The acknowledged group should disappear unless a newer event arrived.
6. Open View History separately: its session history is distinct from the persistent inbox.

## Behavior to know

Detection ingestion writes the face events; opening a preview does not. Similarity is a model score, not proof of identity. An idle camera or expired display window can leave a feed empty. The persistent acknowledgement endpoint is `/api/detection-alerts/inbox/{id}/acknowledge`.

## Source

[frontend/dashboard.html](../frontend/dashboard.html), [frontend/js/dashboard.js](../frontend/js/dashboard.js), [frontend/js/detection-alert-inbox.js](../frontend/js/detection-alert-inbox.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
