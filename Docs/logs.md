# Logs

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/logs` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Read application/available service logs, level counts, source selection and paginated diagnostic records.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Source / level / page size + Apply Filters | Load logs matching the selected source and level. |
| Clear | Reset the filter controls; this is not a log-file deletion button. |
| Refresh | Reload logs and statistics. |
| Previous / Next | Navigate result pages. |
| View execution history | Open Background tasks for job-level status and errors. |

## Demo

1. Open Logs and select the application source.
2. Select an available warning or error level and Apply Filters.
3. Inspect timestamp and message for an existing test issue.
4. Clear filters, then open View execution history to compare a related job.

## Behavior to know

No matching log rows is different from a failed log request. The API may expose maintenance operations that are not buttons on this page.

## Source

[frontend/admin/logs.html](../frontend/admin/logs.html), [frontend/js/admin-logs.js](../frontend/js/admin-logs.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
