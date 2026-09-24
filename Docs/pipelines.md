# Pipelines

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/pipelines` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Inspect registered camera pipelines, rename their identifiers, and save locations used by map views. Pipelines are registered through ingestion.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Refresh / Update List | Reload camera status, latest activity and counters. |
| Rename | Submit a new pipeline identifier. The backend normalizes it and maintains an alias from the old ID so ingestion can continue. |
| Coordinates / location action | Open the location form and map picker. |
| Map click / latitude / longitude / location name | Choose or edit the proposed location. |
| Save Location | Persist the coordinates and location label. |
| Close / Cancel | Discard unsaved location edits. |

## Demo

1. Select an existing test camera and open Coordinates.
2. Enter its known test location or select it on the map, then Save Location.
3. Refresh the list and verify the location remains.
4. Open Intelligence Map for a test identity seen by that camera; expect the location to be available.

## Behavior to know

Rename changes the pipeline key, not just a decorative label. User access is assigned on Users. A blank map can indicate missing offline tiles rather than missing coordinates.

## Source

[frontend/admin/pipelines.html](../frontend/admin/pipelines.html), [frontend/js/admin-pipelines.js](../frontend/js/admin-pipelines.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
