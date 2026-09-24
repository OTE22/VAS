# Home

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/home` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

System overview, pipeline summaries, service health, storage/retention information, and shortcuts to operational pages. Home is an administrator workspace.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Refresh / Try again | Reload the overview data and freshness indicators. |
| Overview / Workspace / System health / Pipelines / Data retention | Jump to the corresponding section on this page. |
| Open live feeds / Live Feeds | Open Dashboard. |
| Search identities / Find an identity | Open Search. |
| Review watchlists / Explore intelligence | Open Watchlists or Intelligence. |
| Add person | Open enrollment; see shared-controls.md for file selection and duplicate decisions. |
| Unknown faces | Open the Unknown Faces Center. |
| Tracking people | Launch the configured chatbot through VAS single sign-on. |
| Pipelines / Manage pipelines | Open pipeline management. |
| System status | Scroll to subsystem health details. |
| Pipeline selector | Choose the pipeline whose detail is shown in the pipeline section. |
| Manage users | Open account and pipeline-access management. |

## Demo

1. Sign in as administrator and open Home.
2. Click Refresh, then System status. Read the actual component states and freshness times.
3. Choose an existing pipeline and inspect its latest activity.
4. Click Open live feeds to inspect that pipeline on Dashboard.

## Behavior to know

Overview totals and health are different from live face cards. A stale or failed request should be investigated before interpreting its numbers.

## Source

[frontend/home.html](../frontend/home.html), [frontend/js/home.js](../frontend/js/home.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
