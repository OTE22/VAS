# Audit log

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/audit` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Inspect chatbot question/audit records and aggregate success/failure counts; filter by user, dates and outcome.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Refresh data | Reload statistics and rows. |
| User / username / success / date filters + Apply filters | Request matching audit entries. |
| Reset filters | Restore the default query. |
| Details (row) | Open the recorded request/response details available for that entry. |
| Previous / Next / page size | Navigate the filtered history. |
| Close details | Dismiss the read-only detail dialog. |

## Demo

1. Ask a simple stored-data question through an authorized chatbot account.
2. Open Audit as administrator and click Refresh data.
3. Filter by the test username and recent date range.
4. Open Details and compare the recorded question, source and outcome.

## Behavior to know

This page is the chatbot audit workspace, not a universal list of every settings or ML change. The separate LAF-AI integration may record the question and source without storing its full answer here.

## Source

[frontend/admin/audit.html](../frontend/admin/audit.html), [frontend/js/admin-audit.js](../frontend/js/admin-audit.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
