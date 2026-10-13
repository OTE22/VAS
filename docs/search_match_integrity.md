# Image-search match integrity

Main image search, batch search, Quick Search and the intelligence photo pickers use the same evidence-aware matcher.

## What a result means

- `similarity` belongs to `matched_embedding_id`. The image is that vector's owned enrollment image or its uniquely linked camera Face, never the identity group's representative portrait.
- Both Known and Unknown image-search results must meet the current `SIMILARITY_THRESHOLD` (0.80 in this deployment). Quick Search no longer uses the lower live Unknown grouping threshold.
- Only vectors from the current recognition model are compared. Inactive person history remains searchable; merged-away identities do not produce duplicate results.
- Camera and date filters qualify the observation that produced the result. Filters apply before ranking and limiting distinct identities. Enrollment-only images have no camera sighting and do not satisfy camera/date filters.
- A Known name is shown only when both the uploaded query and the matching historical observation meet the threshold against the same completed, identity-owned enrollment reference. Enrichment and historical group membership do not establish an approved reference.
- Otherwise a Known-associated observation is labeled **Unverified historical match**, without the name. It does not trigger a Known-person watchlist alert. Unknown watchlist matching additionally requires direct agreement with the stable current-model group reference.
- These checks establish reference consistency, not independently verified human identity. A similarity score is not a probability, and no threshold guarantees zero false matches. Approved enrollment references must themselves be correct.

## Opening and reviewing a match

The client passes `search_embedding_id` to identity details and profile links. The backend checks ownership and model compatibility; stale or mismatched IDs return 404. A query's unverified status is preserved as a downgrade with `search_unverified=true`.

The header retains the exact matched photo. Sighting comparisons use the matched vector and the image-search threshold, without chaining through intermediate observations. Other historical associations stay stored and are marked for review; they are not silently included in the journey. Group-wide analysis and alert actions are withheld in photo-search contexts where the group's reference is unverified. A selected Unknown sighting can still be reviewed and promoted through the existing selected-sighting workflow.

The main page also distinguishes multiple query faces that hit the same identity: the selected vector ID, not the identity ID alone, chooses the match's score and photo.

## Missing or ambiguous evidence

If the retained schema cannot unambiguously associate a vector with a Face (for example multiple faces/vectors in the same detection), search does not guess a photo. A deleted camera image does not remove the search embedding. The existing browser-only uploaded-photo fallback remains explicitly labeled **Uploaded search image** and is not camera evidence.

Intelligence photo pickers show an unverified group's matching observation for review but do not select the whole group as a verified person.

## Configuration and storage

Exact database ranking replaces the lossy identity-only index result in image-search workflows. Existing FAISS/pgvector live-recognition behavior is unchanged. `SEARCH_RETRIEVAL_FLOOR`, `SEARCH_CANDIDATE_MULTIPLIER` and `SEARCH_FILTERED_CANDIDATE_MULTIPLIER` remain accepted for old configuration compatibility but are labeled legacy and are unused by image search. `SEARCH_DEFAULT_TOP_K` and `SEARCH_MAX_TOP_K` still control result depth.

No database schema migration, reclassification, deletion or image/embedding rewriting is required. Normal search audit logging continues. Deployment does not repair historical group membership automatically.

## Verification

`tests/test_search_filters.py` exercises exact photos, incorrect Known associations, reference ownership/status/model checks, sighting filters before ranking, deduplication, inactive history, ambiguity, stale/cross-identity context, single/batch serialization and HTTP response-model preservation. Quick Search and frontend regressions cover the same contract, including multi-face clicks and unverified picker selections.

Integration fixtures use a disposable PostgreSQL instance named `retention-test-db`, database `retention_test`, isolated from production. Tests never seed the application database. The candidate was also checked with read-only transactions against the previously investigated historical cases and a synthetic Firefox desktop/mobile session. Two read-only searches measured roughly 65–81 ms; this is a spot check, not a throughput or recognition-accuracy benchmark.
