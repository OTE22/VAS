# Background tasks and logs UI — 2026-09-21

Updated `/admin/background-tasks` and `/admin/logs` with a shared, responsive monitoring stylesheet and clearer descriptions of the data shown.

- Task statistics explicitly describe their time window and distinguish it from history filters. An empty completed/failed window no longer displays an invented 0% success rate.
- Task details load fresh data, ignore obsolete responses, and show errors instead of silently substituting cached information. Status explanations, retention results, and collapsible technical references make details easier to read.
- Retention text distinguishes record age from the cleanup interval and explains dry runs.
- Logs distinguish source statistics from filtered events. Expandable events show complete escaped messages and source metadata. Local search covers full messages on the currently loaded page, with that scope explicitly stated.
- Fixed the inherited error-state styling that oversized and centered error events.

## Validation

- Isolated Python regressions: 72 passed (`test_background_tasks.py`, `test_optional_log_sources.py`, `test_frontend_layering.py`).
- JavaScript checks: 8 existing logs tests and 5 monitoring behavior tests passed.
- Chromium fixture checks passed at desktop and mobile sizes: detail dialog and Escape, expanded log events, full-message page search, escaped markup, no page overflow or console errors. Screenshots are under `logs/monitor-ui-20260921/`.
- Browser checks use synthetic data; production verification uses authenticated read-only requests. No production cleanup, cancel, or retry actions are exercised.

## Deployment

Production API image `ddbe56a4d52e` includes the two HTML pages, two scripts, and new shared stylesheet. Previous image and stopped container are retained under `before-monitor-ui-20260921`. Runtime environment, bind mounts, and device requests were verified unchanged. No database migration or ML worker restart was required.

All five deployed files match workspace hashes; all three public CSS/JS assets match as well.

Authenticated production checks returned HTTP 200 for both pages, task history/stats, log configuration/events, and background service status. Both API and ML worker are healthy after deployment.
