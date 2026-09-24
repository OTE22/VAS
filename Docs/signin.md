# Sign in

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/signin` at `https://face-detector.internal`. **Access:** Anyone; valid credentials required to continue.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Start a browser session using your assigned username and password. Errors distinguish failed credentials, temporary rate limits, and unavailable authentication.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Username / password | Enter your account credentials; these are not camera ingest credentials. |
| Show password | Reveal or hide the password in the input field. |
| Continue securely | Submit credentials. Successful login follows the server-provided destination; required password rotation opens Change password. |
| Skip to sign in form | Move keyboard focus past decorative content. |

## Demo

1. Open the hostname URL and enter your assigned credentials.
2. Click Continue securely. Expect the permitted landing page or Change password.
3. If rotation is required, complete the change-password guide before opening other pages.

## Behavior to know

Use the hostname and trust the internal CA on the client. A bare IP can fail certificate/origin checks. Do not repeatedly retry while a rate-limit message is active.

## Source

[frontend/signin.html](../frontend/signin.html), [frontend/js/signin.js](../frontend/js/signin.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
