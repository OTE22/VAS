# Known faces

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/known` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Searchable directory of enrolled/promoted people, enrollment-photo galleries, primary-photo selection, renaming, activation, and deletion previews.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Search / status / sort | Filter the directory and choose current, active, promoted, inactive, merged, or all records and their order. |
| Refresh directory / Previous / Next | Reload or page through people. |
| Person card | Open that person's record and photo gallery. |
| Add person | Open the shared enrollment dialog for a new person. |
| Open full profile | Open the identity's appearance history and analysis links. |
| Save name | Persist the edited display name. |
| Add photo | Open enrollment for this selected identity. |
| Set as primary | Save the chosen enrolled image as the primary photo, then refresh the gallery/directory. |
| Retry photos / directory retry / reset filters | Retry a failed load or broaden an empty search. |
| Deactivate person / Reactivate person | Open an operation confirmation; Confirm saves the activation change. |
| Permanently delete | Fetch a deletion-impact preview and open confirmation. No deletion occurs just by opening it. |
| Refresh preview | Fetch current impact and a fresh confirmation token. |
| Confirm (delete) | Submit deletion after you type the exact displayed name and have a valid preview. This is destructive. |
| Cancel / Close person record | Close the operation or record without submitting unsaved changes. |

## Demo

1. Use Add person to enroll a consenting test subject as Demo Person, or use an existing test record.
2. Search for Demo Person and open the card. Inspect the enrollment-photo gallery.
3. If two approved photos exist, select Set as primary on the second; expect the primary marker to move.
4. Open full profile to review appearances.
5. For a deletion demonstration, open Permanently delete, read the impact, then Cancel. The person must remain in the directory.

## Behavior to know

Merged records and other read-only states disable edits according to server capabilities. Deletion previews can expire or become stale; refresh before confirming. Deactivation preserves a record and is different from permanent deletion.

## Source

[frontend/admin/known.html](../frontend/admin/known.html), [frontend/js/admin-known.js](../frontend/js/admin-known.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
