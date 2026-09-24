# Change password

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/change-password` at `https://face-detector.internal`. **Access:** Signed-in account, including one awaiting password rotation.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Replace a bootstrap, administrator-reset, or existing password. The server enforces the password policy.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Current / new / confirmation fields | Supply the current password and enter the new password twice. |
| CHANGE PASSWORD | Validate the form and submit the change. On success the page returns to sign-in; use the new password. |
| Try again | Dismiss the error popup so you can correct the form. |

## Demo

1. Enter the temporary password as Current password.
2. Choose a new password meeting the displayed requirements, then repeat it.
3. Click CHANGE PASSWORD and sign in again with the new password.

## Behavior to know

Changing a password invalidates older sessions. Closing an error popup does not change the password.

## Source

[frontend/change-password.html](../frontend/change-password.html), [frontend/js/change-password.js](../frontend/js/change-password.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
