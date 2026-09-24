# Identity profile

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/identity/{identity_id}` at `https://face-detector.internal`. **Access:** Administrator or active user with assigned pipelines; the identity API enforces record access.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

A full profile for a known or unknown identity: images, appearance evidence, timeline, watchlist context, and links to operational actions.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Back | Return to the originating workspace when supplied. |
| Copy identity ID / alert ID copy | Copy the identity identifier to the clipboard. |
| Timeline zoom in / zoom out / fit | Change the time scale; do not edit events. |
| Appearance image / snapshot | Inspect the stored evidence image where offered. |
| Add to Watchlist | Open the membership form. Choose a list, priority and notes, then Add to Watchlist to save. |
| Create Live Alert | Open a rule form for this identity; Create Alert saves the selected scope and options. |
| Analyze Threats | Open Security Intelligence with this identity selected. |
| Manage in Unknown Faces Center | Open the triage workspace for an unknown identity; promotion and merge happen there. |
| Close / Cancel in either form | Dismiss without submitting. |

## Demo

1. Open a person from Known faces or Unknown faces.
2. Copy the ID and zoom the timeline to a known test event.
3. Open Add to Watchlist, inspect the available lists, then Cancel.
4. Follow Analyze Threats and confirm the same identity is selected.

## Behavior to know

The URL requires a real identity ID; `/admin/identity` alone is not the profile route. A visible profile does not grant every mutation. Missing images and denied records should be shown as such, not interpreted as absent detections.

## Source

[frontend/admin/identity.html](../frontend/admin/identity.html), [frontend/js/admin-identity.js](../frontend/js/admin-identity.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
