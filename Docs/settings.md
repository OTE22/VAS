# Settings

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/settings` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Browse basic and advanced configuration, inspect stored/effective values and application timing, edit permitted settings, read change history, and manage retention.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Basic / Advanced · All settings | Switch the visible setting subset. |
| Category buttons / search / All Settings | Narrow or broaden the settings list. |
| Refresh / Clear filters | Reload settings or reset the view filters. |
| Edit (editable row) | Open a value editor with its validation and application hints. |
| Save Changes | Persist the new value and apply it as supported. Read saved/effective/restart-required feedback. |
| Cancel / Close dialog | Discard the pending edit. |
| Retention Status | Read retention state. |
| Retention Dry run | Calculate/report prospective cleanup without deleting data. |
| Retention Run now | Follow confirmation to request actual cleanup. |
| Audit history | Read who changed a setting and when. |

## Demo

1. Open Basic, choose one category, then switch to Advanced and compare the available settings.
2. Inspect an editable display setting and its stored/effective values.
3. Open Edit and read the application hint, then Cancel for a read-only demo.
4. Run the retention Dry run and inspect the results without selecting Run now.

## Behavior to know

Saved does not always mean active immediately: settings can require a future job, worker restart or index rebuild. Security-critical settings can be read-only here. Do not assume all settings can be changed simply by editing docker/.env; use the deployment configuration that actually feeds each service.

## Source

[frontend/admin/settings.html](../frontend/admin/settings.html), [frontend/js/admin-settings.js](../frontend/js/admin-settings.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
