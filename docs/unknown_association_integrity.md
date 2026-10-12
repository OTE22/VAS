# Unknown reference and timeline integrity

Live Unknown matching uses the earliest stored non-null vector for each identity
in the current recognition model space. Later camera observations remain in the
database and historical searches, but cannot become live reference vectors. This
prevents transitive matching (A matches B, B matches C, while A does not match C).
The same gate applies when pgvector or FAISS is configured. A reference query
failure propagates; it must not be interpreted as a successful lookup with no match.

`UNKNOWN_SIMILARITY_THRESHOLD` remains independently configurable from the Known
`SIMILARITY_THRESHOLD`. This change does not raise either saved threshold. Anchor
matching can create additional Unknown identities when pose or image quality
reduces direct similarity. It does not guarantee identity correctness; assess
thresholds with representative labeled camera samples. Model changes never reuse
unversioned or previous-model vectors as live references.

Unknown Faces cards send the currently displayed `appearance_id` to
`GET /api/admin/identity/{id}`. The endpoint rejects a sighting belonging to a
different identity. Without this parameter, it uses the initial current-model
reference. Details retain every appearance and add an `association_status`:

- `selected`: the reference sighting, not an independently verified identity.
- `consistent`: direct similarity meets the current Unknown threshold.
- `review_required`: direct similarity is below that threshold.
- `unverified`: missing, invalid, previous-model or ambiguous vector/photo linkage.

Only selected/consistent sightings enter the shared movement graph. Other rows
remain in the expandable review gallery. Operators can inspect them or select a
different sighting as context; this does not change database assignments or live
references. A missing photo cannot fall back to an arbitrary camera-folder image.
Rapid clicks and closing the modal invalidate pending requests.

No schema migration, retrospective reassignment, deletion, threshold change or
notification is part of this fix. Existing historical associations and downstream
analyses still need review; a displayed filtered journey is not a repaired database.
Person-history retention must remain enabled to preserve anchors and search history.

Validation: `tests/test_unknown_reference_isolation.py` uses a disposable
PostgreSQL database and synthetic vectors; `tests/unknown_identity_context.test.cjs`
uses jsdom. Neither suite should be run against a production database.
