# Tutorial

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/tutorial` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

An in-app learning guide with server-provided sections and examples. Use the page-specific Markdown guides for the current control descriptions.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Quick Start / generated section tabs | Select the requested tutorial section. |
| Example and expandable content controls | Reveal the example/explanation rendered by the tutorial; reading it does not execute its API request. |
| Links in tutorial content | Navigate to the referenced workspace or resource. |

## Demo

1. Open Tutorial and choose Quick Start.
2. Read the first workflow and compare it with the relevant page guide in this documentation index.
3. Open its target workspace and use an approved test record to follow the documented demo.

## Behavior to know

Tutorial content is supplied by `/api/admin/tutorial` and `/api/admin/tutorial/examples`. Embedded examples are instructional; their presence is not proof that a production action has been run.

## Source

[frontend/admin/tutorial.html](../frontend/admin/tutorial.html), [frontend/js/admin-tutorial.js](../frontend/js/admin-tutorial.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
