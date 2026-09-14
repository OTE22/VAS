# Quick Search

Quick Search uploads an image, runs the shared face detector (including the padded-crop retry), embeds the largest detected face, and compares it with stored identity embeddings. Group photos now display the detected face count and explain that the largest face was searched. To search another person, crop the image to that face.

The endpoint remains admin-only. It does not enroll, promote, merge, or add embeddings; successful searches record the normal search-history audit using the image hash. The result's Promote button uses the independently protected promotion endpoint.

Corrections:

- Server-side scope and result-count validation (1–100), bounded upload reads using `MAX_FILE_SIZE`, header dimension checks before OpenCV decoding, and shared decoded-image validation.
- ISO timestamps normalized to UTC; invalid or reversed date ranges return 422.
- Exact PostgreSQL ranking by each person's best cosine similarity in the current verified model space. Camera/date filters apply to matching appearance records before ranking and limiting. Duplicate photos cannot consume other people's slots, and multiple matching appearances do not raise a scalar-query error.
- Active/unmerged identities only; configured known/unknown thresholds remain authoritative. Null last-seen values serialize correctly, and snapshot URLs use the shared normalizer.
- Face extraction runs outside the async event loop. Search failures return an error rather than silently treating unavailable index results as no matches.
- Duplicate submissions blocked; file/scope changes and modal closure abort and invalidate pending responses. Names, errors, and identifiers are rendered through text/DOM properties rather than executable HTML.

Validation: 73 backend regression tests passed, including seven Quick Search cases. JavaScript checks cover duplicate submissions, cancellation, stale responses, group-photo notices and button recovery. The browser scenario checks that HTML-like names remain text and the group-photo explanation appears. Live verification uses existing test photos and retains only normal search audits; it does not modify identities.

Compatibility note: embeddings with missing or different model provenance no longer contribute to Quick Search matches. This avoids comparing incompatible face representations. Exact ranking favors complete filtered results over the approximate index shortcut and may cost more on very large galleries. Other recognition/search endpoints are unchanged.
