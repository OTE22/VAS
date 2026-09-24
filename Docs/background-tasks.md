# Background tasks

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/background-tasks` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Operational monitoring for scheduled and manual jobs: execution history, upcoming/running/failed work, retention status and cleanup controls.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Enable Task Alerts | Ask the browser for notification permission for task alerts. |
| REFRESH | Reload task statistics and the active history view. |
| Task type / date filters | Narrow the displayed execution history. |
| All Tasks / Upcoming / Running / Completed / Failed / Cancelled / Overdue | Switch the status/schedule view. |
| Clear Filters | Reset task filters. |
| Previous / Next | Navigate history pages. |
| Details (row) | Open task status, timings, result and error information. |
| Close task details | Dismiss the task dialog. |
| Refresh Retention Status | Read current retention configuration/status. |
| Run Retention Dry Test | Request a dry run reporting prospective cleanup without deleting matching data. |
| Run Retention Now | Open cleanup confirmation. |
| Execute Cleanup | Submit actual retention cleanup; eligible data can be deleted. |
| Cancel retention run | Close confirmation without starting cleanup. |
| Open system logs | Open Logs for process-level diagnostics. |

## Demo

1. Open the Completed tab and inspect a recent task's Details.
2. Switch to Failed; if there are no failures, expect an empty result rather than inventing one.
3. Click Run Retention Dry Test and read the prospective cleanup output.
4. Open Run Retention Now, then Cancel. No actual cleanup should be requested by this demo.

## Behavior to know

The current page centers on monitoring and retention. A Cancelled status tab does not itself cancel a job. Start/cancel ML work from ML Operations; use only actions actually offered for a job.

## Source

[frontend/admin/background-tasks.html](../frontend/admin/background-tasks.html), [frontend/js/admin-background-tasks.js](../frontend/js/admin-background-tasks.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
