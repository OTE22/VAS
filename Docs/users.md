# Users

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/users` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Create accounts, edit roles and camera access, grant chatbot access, reset passwords, review blocks, and manage account lifecycle.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Create New User / Add User Account | Open a new account form. |
| Save user | Create or update the account, chosen assignable role, status, chatbot permission and pipeline assignments. |
| Edit (row) | Open the selected account with its current permissions. |
| Reset password (row) | Open the reset form. Reset Password saves a temporary password and requires rotation at the next login. |
| Unblock (row) | Clear the account block and reactivate it through the server operation. |
| Delete (row) | Open confirmation. Delete User deletes the selected account, subject to protected-account checks. |
| All users / Blocked users | Switch the displayed account subset. |
| RESTORE SYSTEM PRINCIPAL | Repair/recreate the protected system account used for machine attribution. |
| Cancel / close dialog | Dismiss unsaved account, password or deletion forms. |

## Demo

1. Create a test observer account and assign only one test pipeline.
2. Save user, then sign in with that account in a separate browser session and complete any required password rotation.
3. Verify Dashboard shows only the assigned scope.
4. Return as administrator and inspect Edit. Change permissions only if the demo requires it.

## Behavior to know

Administrator is shown in the role selector for existing admin accounts but is not assignable through this UI. Resetting a password invalidates existing sessions. Deleting an account is different from disabling it; protected principals and self-deletion are guarded.

## Source

[frontend/admin/users.html](../frontend/admin/users.html), [frontend/js/admin-users.js](../frontend/js/admin-users.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
