# Search

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/search` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Image-based identity matching with scope, camera, similarity and exclusion controls; quality checking; result details, summaries and export.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Choose image / drag and drop | Select the query photo; the preview confirms which file will be searched. |
| Remove image | Remove the selected query image. |
| Scope / minimum similarity / top matches / camera / exclusions | Configure which identities and results the next search may return. |
| Search | Submit the image and selected filters; show matching identities and record the search as supported by the backend. |
| Quality Check | Analyze the photo's quality; this does not enroll a person. |
| Matches / Alerts / Summary | Switch between result views. |
| Result card / View identity | Open the match details. |
| Analyze threats / Open full profile | Navigate to analysis or the selected identity profile. |
| Done / close identity details | Dismiss the match dialog. |
| Clear | Clear the displayed search results. |
| Export | Open export options. Choose CSV, JSON or PDF, then Export to download; Cancel closes without exporting. |
| Close batch progress | Dismiss the progress view if it is shown; closing a view is not evidence that a server job was cancelled. |
| Search history / Intelligence / Watchlists / Live alerts | Open the named workspace. |

## Demo

1. Choose an approved test photo of a person already in your test data.
2. Select All Identities and 10 matches, then run Quality Check.
3. Click Search; inspect similarity, camera scope and match evidence.
4. Open a match and follow Open full profile to compare appearances.
5. Return to results and export CSV. Expect only the available export data, not a newly enrolled person.

## Behavior to know

Search is not enrollment. A low-quality image, narrow filter, or high threshold can produce no matches. The visible page centers on image search; do not assume API batch or multi-face capabilities have a separate visible workflow here.

## Source

[frontend/admin/search.html](../frontend/admin/search.html), [frontend/js/admin-search.js](../frontend/js/admin-search.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
