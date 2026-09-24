# Intelligence

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/intelligence` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Explore a selected identity's related identities, temporal patterns, cross-camera movement, map and combined analysis with decision provenance.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Identity selector | Set the subject for subsequent analysis. |
| Analyze in Security Intelligence | Open the security-analysis workspace for that subject. |
| Related Identities / Temporal Patterns / Cross-Camera Tracking / Complete Analysis | Switch analysis views. |
| Related Refresh | Reload related identities. |
| Calculate All Relationships | Request relationship computation across the available dataset; this can write derived results/start background work. |
| Temporal date window + Analyze | Compute/load temporal analysis for the selected history window. |
| Track Movement | Load cross-camera movement evidence. |
| Timeline / Map | Switch movement presentation. |
| Map style / Refresh Map | Choose an installed basemap and reload map content. |
| Refresh All | Reload combined analysis. |
| Decision engine badge | Open ML Operations. The badge describes current configuration; each result's provenance describes how it was produced. |

## Demo

1. Choose a test identity with sightings on two cameras.
2. Open Cross-Camera Tracking and click Track Movement.
3. Compare Timeline with Map; camera coordinates and offline map assets must exist.
4. Open Temporal Patterns, choose a date window and Analyze.
5. Read provenance and evidence counts before interpreting a pattern.

## Behavior to know

Coappearance and temporal patterns are derived observations. Sparse data may not support a useful result. Map positions reflect camera locations, not continuous GPS tracking of the person.

## Source

[frontend/admin/intelligence.html](../frontend/admin/intelligence.html), [frontend/js/admin-intelligence.js](../frontend/js/admin-intelligence.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
