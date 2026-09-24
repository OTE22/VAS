# Ingest credentials

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/ingest-credentials` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Issue and revoke camera webhook tokens, and inspect token metadata without retrieving saved secrets.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Label + Issue token | Create a credential; the raw token is displayed once. |
| Copy token | Copy the newly issued secret for configuration of the intended sender. |
| I have saved it | Dismiss the one-time token display. |
| Revoke (row) | Open the revocation confirmation. |
| Revoke (confirmation) | Invalidate that credential; senders using it will lose ingest access. |
| Keep it | Cancel revocation. |

## Demo

1. In a test setup, enter Demo camera and click Issue token.
2. Copy it into the approved test sender's credential configuration before dismissing the display.
3. Send a test frame and verify pipeline activity on Dashboard.
4. After the demo, revoke that test credential if it is no longer needed.

## Behavior to know

A token is a secret. The list cannot reveal it later. Revoking a production camera token interrupts that camera until its sender uses another valid credential.

## Source

[frontend/admin/ingest-credentials.html](../frontend/admin/ingest-credentials.html), [frontend/js/admin-ingest-credentials.js](../frontend/js/admin-ingest-credentials.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
