# Merge suggestions and multi-select

Suggestions offer **Select & Review**. This replaces the current selection with the group's identities, opens the multi-merge dialog, and lets the operator remove members before requesting a preview. Direct approval continues to apply to the entire stored suggestion, independently of manual card selection.

The preview captures the identities and the backend-selected survivor. Execution sends that exact survivor instead of asking for automatic selection again. Changed selections invalidate the preview. Older preview/detail responses cannot overwrite newer state; reading identity details no longer invalidates unknown-list requests. Selection styling follows the identity across cameras and is restored when cards are recreated. Removal updates counts and button availability.

Suggestion approval and rejection acquire the same exclusive transaction lock as identity mutations, followed by a refreshed suggestion row lock. Both require a pending suggestion. Review submissions are guarded through confirmation, and camera suggestion lists refresh after review.

Camera suggestions use the shared validated embedding sampler. Comparisons require matching verified model versions. Deterministic grouping admits a member only when every pair clears the configured similarity threshold; chaining through an intermediate face cannot qualify an incompatible group. The displayed score is the weakest pair's median similarity, without an artificial confidence floor. These groups are conservative suggestions, not proof of identity, and the backend merge gate reassesses submitted groups before writes.

Feedback is measured before embeddings move, staged in the review transaction, and upserted using the database's ordered unique identity-pair constraint. Missing measured quality or comparable embeddings skips feedback rather than inventing values. Group approval records comparable pairs; group rejection only supplies a negative label for a two-person group because dismissing a larger group does not establish that every pair differs. Training uses the persisted dataset; no uncommitted samples are added to process memory.

Regression coverage: `tests/test_suggestion_workflow.py`, `scripts/dev/suggestion_workflow_test.js`, and the suggestion-to-selection browser scenario in `scripts/dev/face_cards_ui_test.js`. Database fixtures roll back, and browser requests are mocked; testing does not merge real identities. Detection records, timestamps, and historical merges are not rewritten.

Validation: 66 distinct backend regression cases passed across the broad suite and focused reruns, including the six new suggestion cases. Both desktop/mobile page checks and the suggestion/preview and promotion JavaScript checks passed. Existing dependency deprecation warnings remain.
