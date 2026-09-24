# Unknown faces

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/unknown` at `https://face-detector.internal`. **Access:** Administrator or active user with assigned pipelines; individual actions remain permission-controlled.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Review unmatched identities, appearance timelines, image searches, promotion decisions, pairwise/multiple merges, merge suggestions, watchlist membership, and live-alert creation.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Pipeline / date filters + Apply filters | Reload identities matching the selected scope. |
| SHOW ALL | Toggle the broader listing mode; Reset filters returns the filter form to its default state. |
| Previous / Next | Page through matching identities. |
| Identity card / View details | Open the identity's detail dialog; timeline zoom in/out/fit changes its display. |
| Snapshot | Open the stored detection image. Copy identity ID copies the identifier for a search or support request. |
| Promote | Open naming and similar-person review. Selecting a candidate follows the existing-person path; NONE OF THESE opens new-person promotion. Submit PROMOTE to save. |
| Merge | Open source/target selection. SEARCH BY ID OR NAME finds a target; choosing a result fills it. |
| PREVIEW | Inspect the proposed merge's impact before execution. |
| MERGE / EXECUTE MERGE | Submit the merge. The target survives and records are consolidated; do not use on people who merely look similar. |
| MULTI-SELECT / identity selections / remove selection | Enter selection mode and select or remove candidate identities. |
| MERGE SELECTED | Start the multi-identity merge workflow for the selected records; review the proposed target before submitting. |
| MERGE SUGGESTIONS / pipeline suggestions | Load proposed duplicate pairs globally or within a pipeline. |
| Suggestion Approve / Reject | Approve executes the proposed merge; Reject records the negative review. Both change stored data. |
| QUICK SEARCH | Open the image search dialog; choose an image and scope, then SEARCH to find similar identities. |
| Add to Watchlist | Choose list, priority and notes; submit ADD TO WATCHLIST to save membership. |
| Create Live Alert | Choose name, threshold, severity, cameras and notification options; CREATE ALERT saves the rule. |
| Analyze in Security Intelligence | Open analysis with the selected identity. |
| Your live alerts | Open the alert-management page. |
| Close / CANCEL / OK, I UNDERSTAND | Close the relevant dialog; no unsaved promotion or merge is submitted. |

## Demo

1. Select one permitted camera and click Apply filters.
2. Open an unknown test identity and inspect the snapshot and appearance times.
3. Click Promote, inspect any suggested existing people, then Cancel for a read-only walkthrough.
4. Open Merge for a known duplicate test pair, choose the target and click PREVIEW. Review what survives, then Cancel.
5. Use QUICK SEARCH with an approved test photo; expect matching records or a clear no-match result.

## Behavior to know

Promotion and merge are real data changes. A suggestion is a review candidate, not an automatic identity conclusion. Use an isolated test dataset for an end-to-end merge demo. Pipeline permissions constrain records even when a page is visible.

## Source

[frontend/admin/unknown.html](../frontend/admin/unknown.html), [frontend/js/admin-unknown.js](../frontend/js/admin-unknown.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
