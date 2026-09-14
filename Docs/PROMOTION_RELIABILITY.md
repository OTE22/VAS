# Unknown identity promotion reliability

Promotion keeps the identity ID and its camera-specific detection history. It now shares the identity mutation lock with merging, reloads and locks the source row, and requires an active, unmerged unknown with usable embeddings from the current recognition model.

The final submission checks stored embeddings against known identities and checks normalized names. Strong matches or existing names return `409 PROMOTION_REVIEW_REQUIRED`; the dialog offers candidate review or explicit creation of a separate person. This confirmation is recorded in the promotion audit. Similarity is a comparison score, not a probability that two people are identical.

Identity changes and the success audit commit together. Failed operations roll back and clean up copied gallery files. Committed promotion invalidates caches and sends a camera-scoped notification to remove the unknown card from other open pages. Queued unknown detections resolve the current identity state before persistence and retain their original camera and timestamp.

The frontend prevents duplicate submissions, ignores candidate responses for another open identity, and captures the source identity before candidate-merge confirmation.

Validation: 59 backend regression tests passed across promotion, merging, camera occurrences, detection persistence, known-face lifecycle, and enrollment review. Database fixtures roll back. Promotion dialog tests cover review cancellation, explicit confirmation, duplicate submissions and button recovery. Browser checks cover desktop/mobile dashboard and unknown cards, including delayed events after promotion. No real person was promoted or merged for testing. Existing historical inconsistencies are not rewritten by this change.

Final follow-up: 17 focused promotion and known-face tests passed after adding a gallery-copy failure test and ensuring journaled copy errors propagate to the transaction owner. This brings the distinct backend tests exercised to 60.

To check the UI, refresh `/admin/unknown` with Ctrl+F5. Promote a test unknown with usable detections. If a known person matches its face or name, review the candidate or explicitly choose a separate person. Successful promotion removes the unknown card and retains the same identity and appearance history under Known Faces. A merged or already-promoted source from a stale dialog must be refused.
