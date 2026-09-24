# Watchlists

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/watchlists` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Create monitoring lists, manage members, set priority/alert level, inspect recent hits, pause monitoring, and restore soft-deleted lists.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Search / level / status / sort | Filter and sort watchlists; Previous/Next changes the result page. |
| Create watchlist card | Open the creation form. |
| Name / description / color / alert level / monitoring options | Set the list metadata and monitoring behavior. |
| Save Watchlist | Create the list or persist edits. |
| View | Open the detail drawer with membership, activity and review statistics. |
| Edit | Open the existing list in the form. |
| Activate / Deactivate | Save the monitoring state; a paused list keeps its membership. |
| Delete | Open the deletion review/confirmation flow; confirmation soft-deletes the list. |
| Restore (deleted list) | Restore a soft-deleted list through its confirmation flow. |
| Drawer identity search / priority / notes + Add | Search for a person and add that identity to the list. |
| Remove member | Confirm removal of that membership; it does not delete the person. |
| Load more members | Fetch another page of membership entries. |
| SEARCH | Open image search. |
| Close drawer / close form / Cancel confirmation | Dismiss the current view without submitting its pending operation. |

## Demo

1. Create a list named Demo Watchlist with a description identifying it as test data.
2. Click View, search for an approved test identity, choose a priority and Add.
3. With monitoring enabled, arrange a test detection at a permitted camera.
4. Inspect recent activity and the Dashboard Detection alerts inbox.
5. Deactivate the test list when the demonstration is finished; the membership should remain visible.

## Behavior to know

Monitoring status, list alert level, and per-member priority are separate fields. A configured notification is not proof of delivery. Confirmations are real writes; cancelling after an earlier Save does not undo that save.

## Source

[frontend/admin/watchlists.html](../frontend/admin/watchlists.html), [frontend/js/admin-watchlists.js](../frontend/js/admin-watchlists.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
