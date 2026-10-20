# Search

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/search` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-10-15, current working tree. Availability on a server requires deployment of this revision. Examples use demonstration data.

## Features

Image-based identity matching with scope, camera, similarity and exclusion controls; quality checking; result details, summaries and export.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Choose image / drag and drop | Select the query photo; the preview confirms which file will be searched. |
| Remove image | Remove the selected query image. |
| Search Scope / Identity Groups Per Face / camera / exclusions | Configure the next search. The group limit controls summary cards; matching-appearance pages are not capped by it. |
| Date From / Date To | Select local calendar days. Date To includes the entire selected day. |
| View matching appearances across cameras | Open direct image matches and explicitly operator-confirmed sightings within the submitted filters, 50 sightings per page. |
| Previous page / Next page | Browse earlier/later matching sightings in chronological order. |
| Search | Submit the image and selected filters; show matching identities and record the search as supported by the backend. |
| Quality Check | Analyze the photo's quality; this does not enroll a person. |
| Matches / Alerts / Summary | Switch between result views. |
| Result card | In a single-image search, open appearances compared with the original uploaded face. Batch results retain identity details. |
| Analyze threats / Open full profile | Available in identity-detail contexts when permitted; the cross-group appearance viewer does not assert that the entire group belongs to one person. |
| Done / close identity details | Dismiss the match dialog. |
| Clear | Clear the displayed search results. |
| Export | Export the search summary (not the complete paginated appearance list). Open export options. Choose CSV, JSON or PDF, then Export to download; Cancel closes without exporting. |
| Close batch progress | Dismiss the progress view if it is shown; closing a view is not evidence that a server job was cancelled. |
| Search history / Intelligence / Watchlists / Live alerts | Open the named workspace. |

## Demo

1. Choose an approved test photo of a person already in your test data.
2. Select All Identities and 10 matches, then run Quality Check.
3. Click Search; inspect similarity, camera scope and match evidence.
4. Open a match and follow Open full profile to compare appearances.
5. Return to results and export CSV. Expect only the available export data, not a newly enrolled person.

## Behavior to know

Advanced image search separates **Strong matches** (at or above `SIMILARITY_THRESHOLD`)
from **Possible matches — review required** (`SEARCH_POSSIBLE_MIN_SIMILARITY`, default
0.50, up to but excluding the strong cutoff). With a 0.60 strong cutoff this means
60% and above versus 50%–under 60%. These are similarity scores, not probabilities
of identity. If the lower cutoff equals or exceeds the strong cutoff, the possible
band is disabled.

Each band has its own per-face group limit and uses the same scope, camera, date
and exclusions. A group appears only in the band containing its highest eligible
score. Possible cards open a comparison of the uploaded face and the exact matched
photo; they do not inherit a Known name, enter the strong-match journey, enroll a
face, or trigger a watchlist alert. Existing strong-match totals and exports stay
separate from the new possible-candidate count.

Search is not enrollment. A low-quality image, narrow filter, or high threshold can produce no matches. The visible page centers on image search; do not assume API batch or multi-face capabilities have a separate visible workflow here.

## Source

[frontend/admin/search.html](../frontend/admin/search.html), [frontend/js/admin-search.js](../frontend/js/admin-search.js). Page access: [dashboard routes](../backend/routes/dashboard.py).

## Review matching appearances across cameras

1. Open `/admin/search` as an administrator.
2. Upload a clear image, then choose **All Identities (Known + Unknown)** to include sightings stored in either category.
3. Leave the camera filter on all cameras for a cross-camera search. Select the desired Date From and Date To; an empty range searches all retained dates. Excluded identities remain excluded.
4. Click **Search**. For a multi-face photo, locate the face you intend to review.
5. Click **View matching appearances across cameras** for that face. Direct image matches meet the configured threshold against the uploaded face. Sightings explicitly confirmed against a directly matching reference can also appear, labeled **Operator confirmed**.
6. Read the total matching-appearance count and applied filters. Select a visit/photo in the journey to inspect its camera name and local timestamp. The diagram covers the current page only.
7. Use **Next page** until disabled to reach the final page; **Previous page** returns to earlier matches. The identity-group summary limit does not limit these pages.
8. Open **Review match scores and labels** to inspect direct similarity values or the reviewer, time and supporting reference for an operator confirmation. **Open reference and review decision** opens that reference in a new tab, where an administrator can inspect or undo the saved decision. Names without an approved reference match remain unverified.
9. Click **Done** to return to search. Rerun Search after confirming, rejecting or undoing a review, to include newly recorded sightings, or to change filters.

The view becomes unavailable after its 30-minute context expires or recognition settings or saved reviews change; rerun the image search. If the cache service is unavailable, the page explains that appearance browsing is unavailable. A missing historical photo stays unavailable even when its vector matches. Neither a similarity score nor a linked group guarantees identity; unusable/missing embeddings cannot establish direct image matches. A valid saved operator confirmation can still include its reviewed sighting. Saving snapshots and fixing pagination improve evidence availability, not biometric recognition accuracy.


### Use your saved confirmations in search

1. In Identity Details, compare both photos and use **Confirm same person** only for the individual sighting you have verified.
2. Return to Advanced Search and run **Search** again with the query photo and desired filters.
3. Open **View matching appearances across cameras**. The summary separates direct matches from operator-confirmed sightings. This button remains useful when there are no direct match cards within your filters.
4. Select a confirmed sighting to see its camera, timestamp and **Operator confirmed** badge. Its supporting reference may be from a different date/camera; returned sightings still respect your filters.
5. To correct a decision, open **Review match scores and labels → Open reference and review decision**, select the reviewed sighting in the reference journey, then use **Review / undo decision**. Rerun Search afterwards.

Confirmation can recover a reviewed alternate view; it does not train the recognition model or guarantee every appearance. Only a direct query match can support a confirmed association; confirmations do not expand through other confirmed sightings. Rejection/undo removes the reviewed link from expansion. A sighting that independently matches the uploaded image may still appear as a direct match. Batch results and summary exports do not include this additional appearance list.
