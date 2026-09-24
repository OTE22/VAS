# Search history

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/search-history` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Review prior image searches, filter by type and age, export records, and clear history.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Search type / date window + Apply Filters | Reload Single, Multi-Face, Batch, or all available history for the chosen period. |
| History result/details controls | Inspect the saved search information where rendered. |
| Load More | Append the next page of history. |
| Export | Open format and date-window options. |
| Export dialog: CSV / JSON + Export | Download history in the selected format. |
| Clear | Request deletion of history through the page confirmation flow. This changes stored history. |
| Cancel / close export | Dismiss the export dialog. |

## Demo

1. Run a test image search from Search.
2. Open Search history, select the recent date window and click Apply Filters.
3. Locate the test entry and inspect its recorded result information.
4. Export JSON. Avoid Clear unless you intend to delete the history.

## Behavior to know

Clearing history does not mean deleting the identities returned by those searches. History visibility and deletion scope are enforced by the server.

## Source

[frontend/admin/search-history.html](../frontend/admin/search-history.html), [frontend/js/admin-search-history.js](../frontend/js/admin-search-history.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
