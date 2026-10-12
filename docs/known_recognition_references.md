# Known-person recognition references

Live recognition uses current-model embeddings linked to a completed gallery image owned by the same person. Camera observations remain stored for historical search but do not automatically become recognition references. This applies to every identity, including previously promoted identities. No schema migration is required.

- **Upload/enrollment:** the image and its embedding are linked in one transaction. Enrollment suggestions also compare approved references.
- **Promotion:** only the selected representative photo is embedded as a reference. Previous unknown-cluster vectors are retained as observations. If the photo is missing or does not contain one detectable face, promotion stops with a useful error; add a clear photo through Known Faces.
- **Automatic enrichment:** disabled by default. When explicitly enabled, candidates must meet quality, live similarity, and enrichment thresholds against an approved photo. Learned views are marked `known_enriched` and cannot bootstrap a chain of unrelated references. Disabling enrichment excludes these views immediately.
- **Historical search:** Quick Search and Advanced Search continue to search retained observations. A historical association may reflect an old false recognition; this fix does not retroactively verify or relabel sightings.
- **Alternative FAISS backend:** unknown/history indexing remains supported. The small approved Known gallery is ranked exactly from PostgreSQL to avoid retained observations crowding references out of an index candidate pool.

## Repair a legacy profile

Review the actual gallery photo first. Never assume the largest or most recent camera cluster is correct. An old profile without a linked approved reference will not match live until a reference is enrolled or explicitly repaired.

The maintenance tool defaults to a read-only preview:

```sh
python scripts/repair_known_reference.py --identity-id UUID --image-id IMAGE_ID
```

After an administrator confirms that exact photo:

```sh
python scripts/repair_known_reference.py --identity-id UUID --image-id IMAGE_ID \
  --apply --confirm-photo-correct --actor-user-id ADMIN_ID
```

Run in the application's configured runtime. The tool checks image ownership and checksum, uses the existing face-extraction pipeline, adds at most one reference per image/model, and writes an audit record. It does not delete or relabel historical person data. Repeating the repair does not duplicate the reference.

Old Known Live Feeds events can remain visible within the configured display window. Check event timestamps when validating new matches. Thresholds alone cannot establish accuracy; use representative labeled camera samples to assess false accepts and missed matches after deployment.

## Verification

`tests/test_known_reference_isolation.py` uses a disposable PostgreSQL server and synthetic vectors to cover live ingestion, both backend configurations, historical search, model mismatch, deleted/moved/failed gallery images, bounded enrichment, future promotion, explicit repair, checksum validation, enrollment review, and startup loader provenance. The test environment has no production database or storage mounts.
