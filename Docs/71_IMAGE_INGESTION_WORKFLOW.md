# How an image gets into the system

Three different things can be meant by "upload an image", and they do very
different work. Everything below follows from telling them apart.

| Path | Entry point | Synchronous? | Creates |
|---|---|---|---|
| **Enroll** a person you know | `POST /api/identities/{id}/images`, `POST /api/upload-person` *(deprecated)* | inline, or `202` + a decision | identity, `identity_images`, `identity_embeddings` |
| **Ingest** from a camera | `POST /webhook/{pipeline_id}` | queued, answers `202` | `detections`, `faces`, `identity_appearances`, possibly a new UNKNOWN identity |
| **Search** by image | `POST /api/search/by-image`, `POST /api/search/advanced` | inline | **nothing** |

The upload button in the UI uses the first one. A camera pipeline uses the
second. They share the detector and the embedding model and nothing else.

---

## 1. Enrollment

`backend/core/enrollment_service.py::enroll_image` is the single implementation
behind every upload route. Both endpoints funnel into it; the deprecated one
just resolves the person by name first.

### Everything that can fail cheaply fails first

No file is written and no row is created until the image has fully proven
itself:

1. **Bytes** — non-empty, ≤ `MAX_FILE_SIZE` (10 MB), `Content-Type` starts with
   `image/`. The decode is the real content gate; the declared type is a hint.
2. **SHA-256 of the ORIGINAL uploaded bytes**, taken before any decode or
   re-encode. This is the dedup key, and hashing the original is what makes it
   stable — re-encoding would produce a different digest for the same upload.
3. **Decode** with `cv2.imdecode`; rejects sides < 32 px, > 10000 px, or
   > 60 MP (a small file can still decode into a decompression bomb).
4. **Face detection** — SCRFD (`det_10g.onnx`), called with **`max_num=0`**.
   That is deliberate: a "keep the best face" cap would let a second person hide
   in the photo.
   - **more than one face → `multiple_faces` (400).** Enrollment needs exactly one.
   - **zero faces, `is_face_image=false` → `no_face` (400).**
   - **zero faces, `is_face_image=true` → a padded retry.** The image is padded
     by half its longest side with a black border and re-detected, then the
     landmarks are translated back into original coordinates. A tight 112×112
     crop often gives the detector no margin to work with. If that still finds
     nothing → `no_landmarks` (400).

   **Landmarks are never fabricated.** An earlier version invented five
   keypoints for `is_face_image` uploads, which produced garbage embeddings that
   still enrolled successfully.
5. **Embedding** — ArcFace (`w600k_r50.onnx`), validated as exactly 512 finite
   dimensions with non-zero norm, then L2-normalized.

### Then, and only then, the durable work

6. The photo is staged in `FACES_DIR/.incoming/` via `mkstemp` — the same
   filesystem as its destination, so the final placement can be an atomic
   rename rather than a copy.
7. The identity is resolved or created.
8. **Deduplication.** If this identity already has an image with that checksum,
   the temp file is removed and the call returns `duplicate: true` — HTTP 200,
   not an error, nothing written. Dedup is **per identity**, enforced by a
   unique index on `(identity_id, file_checksum)`: the same photo may legitimately
   be enrolled under two different people.
9. Destination is `storage/faces/<identity_uuid>/image_NNN.ext`. The UUID is
   re-parsed rather than interpolated, so nothing caller-supplied can become a
   path component, and the final path is checked with `commonpath` to be inside
   `STORAGE_DIR`. **The folder is the immutable UUID, never the display name** —
   renaming a person must not move their files, and two people may share a name.
10. Rows: `identity_images` (with `source_type = "cropped_face"` when
    `is_face_image` was set, else `"upload"`), then `identity_embeddings`, then
    the embedding is linked back to the image and **re-read from the database**
    to confirm what actually landed: right identity, right image, 512 dims,
    non-zero norm.
11. **The file moves last, immediately before the commit:**

```python
os.replace(temp_path, final_path)
temp_path = None            # ownership transferred
await db.commit()
```

If the commit fails, cleanup unlinks `final_path` too. The invariant is: **never
a row without a file, never a file without a row.**

### One transaction, on both backends

`save_embedding` is called with **`defer_commit=True`**. Its FAISS branch
otherwise commits the vector itself — required by the vector-index contract
(persist → commit → index, see `70_VECTOR_INDEX_CONTRACT.md`) — but that commit
landed *before* `os.replace`, splitting enrollment across two transactions. The
rollback then had nothing left to undo, and a failure in between left a
committed `identity_images` row pointing at a file that never landed.

The deferred call withholds **both** the commit and the index write. Withholding
only the commit would be worse than the original bug: `index.add()` against an
uncommitted row strands a phantom entry under a key the rollback then erases.

After the commit, `sync_pending_embedding()` indexes the vector. A failure there
is a **warning**, never an error — the enrollment is already durable, and
reconciliation adds any pending vector within its interval anyway. The inline
call exists so a freshly enrolled person is findable immediately rather than
within the hour.

### Resolving a person by name (deprecated endpoint)

Names are normalized by collapsing whitespace and casefolding, so `Ali Abbass`,
`ali abbass` and `ALI   ABBASS` are the same person. If **several** active
people share that name the call returns **409 with a `candidates[]` list**
rather than guessing.

### The identity decision (name-based uploads only)

Name lookup is **textual**, and on its own that meant any spelling the system
had not seen minted a fresh identity UUID. A second photo of an already-enrolled
person, uploaded as `Jon Smith` instead of `John Smith`, became a **second
identity holding a second embedding of the same face** — and recognition then
answered with whichever vector scored higher, non-deterministically. The upload
returned HTTP 200 with a success toast. Worse, `identity_created=True` skipped
the checksum check entirely, so even byte-identical bytes re-enrolled.

So `POST /api/upload-person` now checks before it creates. After step 5 above,
and **only when the name resolves to nobody**:

1. **Exact checksum scan** across every searchable identity. "You already have
   this precise file" is a fact; a similarity score is an estimate, and the fact
   is established first. Reported as `duplicate_of_identity_id`.
2. **Vector search** over ACTIVE + PROMOTED (`SEARCHABLE_STATUSES`), collapsed
   to the best score per identity.
3. **Band** (`classify_match`):

   | Similarity | Band | Response |
   |---|---|---|
   | ≥ `ENROLL_STRONG_MATCH_MIN` (0.75) | `strong` | `202`, `recommended_action: add_to_existing` |
   | ≥ `ENROLL_CANDIDATE_MIN` (0.40) | `uncertain` | `202`, `recommended_action: review` |
   | below | `none` | enrolls immediately — **identical to the old behaviour** |

`ENROLL_CANDIDATE_MIN` deliberately equals `SIMILARITY_THRESHOLD`, the bar at
which recognition itself calls two faces one person: anything recognition would
confuse, enrollment must ask about. A floor **above** it reopens the defect, so
`config_guard` reports that configuration at startup. Measured on the enrollment
fixtures, two different photos of one person score **0.4299** and unrelated
faces score below **0.05**.

**202, not an error.** A review prompt is the workflow working. Routed through
the error path it would be counted as a client error by every dashboard watching
this endpoint. `success` stays `false`, because no person was enrolled.

#### While a decision is pending, nothing durable exists

No identity, no `identity_images` row, no `identity_embeddings` row, no gallery
folder, no vector-index entry. The photo sits in **`STORAGE_DIR/pending/`** —
outside `FACES_DIR`, which holds one directory per identity UUID and nothing
else, but on the same filesystem so the confirmed photo still lands by rename.
The claim ticket is a `pending_enrollments` row: SHA-256 of the token (never the
token), the uploading user, a 15-minute expiry, the frozen candidate list, and
the **recognition and detection model versions** that produced the embedding.

#### Resolving it

```
POST /api/enrollment/confirm   {"action": "add_to_existing", "identity_id": "…", "upload_token": "…"}
                               {"action": "create_new", "display_name": "…", "upload_token": "…",
                                "confirm_create_new": true}
                               {"action": "cancel", "upload_token": "…"}
POST /api/enrollment/cancel    {"upload_token": "…"}
```

`backend/routes/enrollment_review.py` **validates, then consumes**. Every check
runs against a ticket that is only read, so a refusal the administrator can act
on leaves the dialog answerable; the ticket is then consumed by a single
`DELETE … RETURNING`, which no concurrent request can also match. Re-checked
against **live** state, never the frozen copy:

- the chosen identity exists, is ACTIVE or PROMOTED, and has no `merged_into_id`
- the administrator still holds `IDENTITY_MANAGE`, re-read from the user row
- the models have not changed since the review
- the file still matches the checksum recorded at review
- the chosen identity **was actually offered** — the frozen list's only job

`create_new` after a **strong** match needs `confirm_create_new: true`. That
action reintroduces the exact duplicate this flow prevents, and identical twins
are real, so it is available but not one click.

**Identities are never merged automatically.** `merge_identities` is not called
from this path at all, and a test asserts its absence from the module.

#### What this does not do

It does not block anything, and it does not stand between an identity and its
second photo. **Different images of one person under one UUID is the supported
case**: the checksum rule is scoped to byte-identical uploads, and
`POST /api/identities/{id}/images` — where the administrator has already chosen
who this is — is not gated at all.

Only the first image an identity ever receives becomes primary; a newer photo
never silently displaces it. `PUT /api/identities/{id}/images/{image_id}/primary`
is the explicit way to change it.

---

## 2. Camera ingest

```
POST /webhook/{id}   ->  202 in milliseconds, nothing decoded
      |
      v
ProcessingQueue      ->  per-pipeline micro-batching (size 5 / 0.5 s)
      |
      v
queue workers (4)    ->  decode + validate in INFERENCE_POOL, off the event loop
      |
      v
process_image_async  ->  the pipeline below
```

The handler does **no** work: it validates the pipeline id, checks a 60-second
dedup window, and enqueues the raw base64 string. Backpressure is explicit — a
full queue answers `503` with `Retry-After: 2` rather than growing without
bound.

### Authentication

The webhook **requires a key**. It accepts frames that become identities,
embeddings and stored face images; nginx rate-limits it but does not
authenticate it, so without a key anyone able to reach the port could create
people and fill storage.

| Setting | Meaning |
|---|---|
| `WEBHOOK_API_KEYS` | Comma-separated **set**. Multiple keys is the rotation story: append the new one, roll it out, drop the old one — with no window where either is rejected. |
| `WEBHOOK_AUTH_TOKEN` | Bearer token for an **external sender**. An *alias*, not a second store: it is appended to `WEBHOOK_API_KEYS` when settings are built, so there is one credential set at runtime. Consequence: guard violations name it positionally under `WEBHOOK_API_KEYS` ("Ingest key #2 is guessable"), not by this name. |
| `WEBHOOK_AUTH_TOKEN_FILE` | Docker-secret path for the above. |
| `WEBHOOK_AUTH_MODE` | `enforce` \| `log_only` \| `off`. `log_only` checks and logs what it *would* reject while letting traffic through, for migrating an already-deployed fleet. |
| `WEBHOOK_AUTH_HEADER` | Default `X-Webhook-Key`. Some camera firmware can only send one fixed custom header — set this to whatever name it can emit. `Authorization: Bearer` is accepted regardless. |
| `WEBHOOK_AUTH_INSECURE_ACK` | Acknowledge `log_only` in production. It can **not** authorize `off`. |

All of these are **restart-only**. They are in `SECURITY_CRITICAL_KEYS`, so
`apply_to_runtime` refuses them and the Settings page renders them read-only —
otherwise an admin token could reopen the endpoint that `config_guard` refuses to
start without.

#### What an external sender must send

```
POST /api/webhook/<pipeline_id>
Content-Type: application/json
Authorization: Bearer YOUR_WEBHOOK_TOKEN
```

Cameras may instead send `X-Webhook-Key: <key>`. **Never a query parameter** —
nginx, gunicorn and the access-log middleware all record the request line, so a
credential there lands in three logs on the first request.

The scheme is matched case-insensitively and extra spaces after `Bearer` are
tolerated, per RFC 7235. Everything else — a bare token with no scheme, `Bearer`
with no token, `Basic`, `Token` — is a 401. The rejection is byte-identical for
missing, malformed and wrong credentials, so it cannot be used as an oracle;
`WWW-Authenticate: Bearer, WebhookKey` tells a client which schemes are accepted.

#### Production postures

| Mode | Production behaviour |
|---|---|
| `enforce` | Required. The only destination. |
| `log_only` | Permitted **only** with `WEBHOOK_AUTH_INSECURE_ACK=true`. Keys are still required and still verified; a bad one is logged and accepted. Warned about on every boot. |
| `off` | **Refused unconditionally.** Exit 78 regardless of the acknowledgement. |

Production also refuses to start with no key — including when the mode is `off` —
or on a weak, reused or published key (`config_guard`, exit 78).

`off` used to be acknowledgeable, and the missing-key check used to be skipped
whenever the mode was `off`. Together those two exemptions let a production stack
start with **no credential and no enforcement**, reporting a single warning. Both
are closed. The rollout for an existing fleet is: deploy with `log_only` + ack +
keys set → watch `fr_webhook_auth_total{result="would_reject"}` fall to zero →
switch to `enforce`.

`GET /webhook/test` is key-gated too, which makes it a credential self-check for
an installer: 200 means the key works, 401 means fix it.

### The production secret file

Production supplies the key through `WEBHOOK_API_KEYS_FILE`, mounted as a Docker
secret at `/run/secrets/webhook_api_keys` from `secrets/webhook_api_keys`. The
value never appears in a compose file, an image layer, or an environment
listing.

`config_guard` refuses to start unless the file:

| Requirement | Violation code if not met |
|---|---|
| exists | `WEBHOOK_KEY_FILE_MISSING` |
| is a regular file, not a directory | `WEBHOOK_KEY_FILE_NOT_A_FILE` |
| is readable by the service account | `WEBHOOK_KEY_FILE_UNREADABLE` |
| is no looser than `0444` | `WEBHOOK_KEY_FILE_PERMISSIONS` |
| contains at least one key | `WEBHOOK_KEY_FILE_EMPTY` |
| every key is ≥ 32 chars and passes the strength check | `WEBHOOK_KEY_FILE_WEAK` |
| no key is the one committed to this repo | `WEBHOOK_KEY_IS_PUBLISHED` |
| no key is the JWT secret or the database password | `WEBHOOK_KEY_REUSED` |

The readability check matters more than it looks: the container runs as a
non-root user, so a secret written `0400 root:root` on the host is present,
correctly permissioned, and still unreadable to the service. That failure would
otherwise surface as "no keys configured" — which, mid-rollout, means running
wide open.

`scripts/setup/generate-secrets.sh` writes it, along with the other secret files
compose requires. To create one by hand:

```bash
mkdir -p secrets
openssl rand -base64 48 > secrets/webhook_api_keys
chmod 0400 secrets/webhook_api_keys
```

### Issued credentials — how a third party actually gets a token

The environment key above is **break-glass**. The credential you hand to an
external system is minted at **Admin → Ingest Credentials**
(`/admin/ingest-credentials`), and both are accepted on the same wire format.

| | Environment key | Issued credential |
|---|---|---|
| Created by | an operator with shell access | an admin, in the UI |
| Stored as | a file on the host | SHA-256 in `webhook_credentials` |
| Recoverable | yes, `cat` the file | **no** — shown once |
| Identifies a sender | no, shared by everyone | yes, by name |
| Revoke one sender | rotate the whole fleet | delete the row |
| Survives a DB outage | yes | no |
| Required in production | yes, by `config_guard` | no |

Why both exist: `config_guard` requires an environment key in production so that
startup and ingest never depend on the credentials table. If Postgres is
unreachable, issued credentials stop authenticating and the environment key keeps
the cameras alive. The failure mode is never "accept everything" — a refresh that
raises keeps the previous snapshot, and the dependency's unauthenticated
development branch requires **both** credential sets to be empty.

**Mechanics worth knowing.** Each worker caches the issued set for
`WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS` (default 30, restart-only and
security-critical), so verification costs no query per frame. That TTL *is* the
revocation latency, and it is per-worker: the worker that handled the DELETE
drops it immediately, the others within the window. `last_used_at` is flushed on
the same cycle, so it lags by up to one TTL and "never used" is only meaningful
once a credential is older than that.

There is deliberately no `revoked_at` and no `expires_at`. A flag would create a
second "unusable" state the verifier must distinguish from "absent", and any
distinction the verifier can make is one the `401` can leak. Deletion collapses
both into one state, so a revoked credential is indistinguishable from a token
that was never valid.

Credentials are **not** bound to a pipeline: `batch_writer` inserts the
`pipelines` row on the first detection, so at issuance time there is usually no
pipeline to bind to. A credential names the *sender*, not the camera, and may
post to any `pipeline_id`.

Attribution lives in the logs, not in Prometheus: the first use of each
credential per process logs one INFO line, subsequent uses are DEBUG, and
`fr_webhook_auth_source_total{source="env"|"db"}` answers "can the environment
keys be retired yet?" at a cardinality of two. A credential-name label was
rejected because `prometheus_client` never reclaims a series within a process.

### Rotating the ingest key

`WEBHOOK_API_KEYS` is a **set**, and that is the entire reason rotation needs no
downtime and no flag day. Both keys are valid simultaneously, so cameras can be
updated one at a time.

**1. Add the new key alongside the old one.** Append, do not replace:

```bash
printf '%s,%s' "$(cat secrets/webhook_api_keys)" "$(openssl rand -base64 48)" \
  > secrets/webhook_api_keys.new
mv secrets/webhook_api_keys.new secrets/webhook_api_keys
chmod 0400 secrets/webhook_api_keys
docker compose -f docker/docker-compose.prod.yml up -d face_recognition
```

Both keys now authenticate. Nothing has broken, and nothing has been cut over.

**2. Update the camera clients.** Point each pipeline at the new key, in
whatever order suits the fleet. Verify one before doing the rest:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "X-Webhook-Key: <new key>" https://<host>/webhook/test
# 200 = accepted, 401 = wrong key
```

**3. Monitor until nothing is using the old key.** Every request records its
outcome:

```
fr_webhook_auth_total{result="ok"}            accepted
fr_webhook_auth_total{result="would_reject"}  would have been rejected (log_only)
fr_webhook_auth_total{result="invalid"}       rejected
```

Under `enforce`, watch `result="invalid"` — a camera still holding the old key
will already be failing, so rotate under `log_only` if the fleet cannot be
updated quickly:

```bash
WEBHOOK_AUTH_MODE=log_only
WEBHOOK_AUTH_INSECURE_ACK=true      # required, and reported every boot
```

Do not stay there. `log_only` accepts invalid credentials by design, and
`config_guard` emits `WEBHOOK_AUTH_UNENFORCED_ACKNOWLEDGED` on every start
precisely so it cannot be forgotten.

**4. Remove the old key.** Once no camera presents it — `would_reject`/`invalid`
at zero for a full duty cycle, including any camera that only reports on motion:

```bash
printf '%s' "<new key only>" > secrets/webhook_api_keys
chmod 0400 secrets/webhook_api_keys
docker compose -f docker/docker-compose.prod.yml up -d face_recognition
```

The old key stops working at this point, which is the moment rotation is
actually complete. Restore `WEBHOOK_AUTH_MODE=enforce` and drop
`WEBHOOK_AUTH_INSECURE_ACK` if you used them.

### Where the key is masked

| Surface | How |
|---|---|
| Application logs | `SensitiveDataFilter` redacts `X-Webhook-Key`, `Authorization`, `WEBHOOK_API_KEYS`, `WEBHOOK_AUTH_TOKEN`, any `Bearer <token>` anywhere in a record, and — independently of the field name — the literal key value wherever it appears |
| `config_guard` reports | Violations name the key by POSITION (`key #2`), never by value |
| Admin settings API | Not in any settings category, so never written to the `settings` table or served |
| Runtime settings changes | All five `WEBHOOK_*` auth settings plus `WEBHOOK_AUTH_TOKEN{,_FILE}` are in `SECURITY_CRITICAL_KEYS`, so none can be read back or altered through the admin API |
| Diagnostics / config export | In `SECRET_SETTINGS`, which every export path consults |
| 401 responses | The body is a fixed string. It never echoes the presented credential, and it is byte-identical for missing / malformed / wrong |

The literal-value redaction is the backstop for the case field-name patterns
cannot catch: a key interpolated into a sentence, or logged next to a generic
field name like `key=`.

This is the reason `WEBHOOK_AUTH_TOKEN` folds into `WEBHOOK_API_KEYS` rather than
being a second credential store. The literal-value harvest reads
`settings.WEBHOOK_API_KEYS` to learn what to redact, and the field-name patterns
are each anchored to their own prefix — none of `access_token`, `refresh_token`,
`session_token` or `id_token` matches `WEBHOOK_AUTH_TOKEN`. A token kept in its
own field would have been written to `app.log` in full.

### The pipeline, stage by stage

Per person-box from the upstream detector:

1. **Decode** `cv2.imdecode`, in `INFERENCE_POOL`.
2. **Bbox gate** — `class_name` must be `person` or `face`; boxes far outside
   the frame are rejected as belonging to a different image; the rest are
   clipped; degenerate and sub-10 px crops are dropped.
3. **Detect → align → embed, in one executor hop** (`_process_crop_sync`):
   SCRFD finds the face inside the person crop (`max_num=1`, largest), a
   five-point similarity transform aligns it to 112×112, and ArcFace produces a
   512-dim vector which is L2-normalized.
4. **Quality** — see below.
5. **Recognition** — see §3.
6. **Deduplication** — intra-frame cosine dedup against same-name embeddings at
   `FACE_TRACKING_SIMILARITY_THRESHOLD` (0.95), then the temporal face tracker.
7. **Snapshot** — only the aligned 112×112 face crop is written.
   **The full frame is never stored.**
8. **WebSocket** — `new_detection` for a known face, `new_unknown_detection`
   plus a lightweight `unknown_activity` for an unknown one.
9. **Persistence** — normally via the batch writer, falling back to a direct
   write.

### Quality scoring

Computed inside `_process_crop_sync`, which already holds the crop, the face box
and the landmarks and already runs off the event loop.

The score measures **blur, lighting, absolute face pixel resolution and pose**,
taken from the **real SCRFD face sub-crop** — not from the person crop and not
from the aligned 112×112 image. Both alternatives sound reasonable and are
wrong: scoring the person crop makes the face 1–3 % of the area and collapses
the size term to a constant, and the aligned face is resampled to a fixed size,
so its size term is always 1.0 and its blur is measured on interpolated pixels.

> **This was broken.** The score used to come from the **upstream person
> bounding box** and the **upstream object-detector confidence**. The person box
> is always far bigger than the 100×100 px the size term saturates at, so the
> whole score collapsed to `0.3 + 0.2 × confidence` — below 0.5 for any real
> confidence, against a `IDENTITY_QUALITY_THRESHOLD_KNOWN` that defaults to
> exactly 0.5. **Known people were not accumulating embeddings from camera
> detections at all**, and the stored values carried no image-quality
> information. A real face now scores ≈ 0.75 where the old path gave ≈ 0.47.

Two rules the code depends on:

- **The score is always a number.** Every gate is written
  `if quality_score is not None and quality_score < threshold`, so returning
  `None` on a scorer failure would bypass all of them and admit everything. A
  failure falls back to the legacy score, never to `None`.
- **Values from different scorers are never compared.**
  `quality_scorer_version` records which scorer produced each value (`NULL` =
  legacy, semantics unknown). Old values are not converted — no monotone map
  exists from `0.3 + 0.2 × confidence` to a blur/lighting/pose score, so any
  conversion would be invented data. `create_appearance` and the clustering
  features filter by version, because ranking a new-scale score against a legacy
  one near 0.5 would make every new frame win and thrash `best_snapshot_path`.

`FACE_QUALITY_SCORER=legacy` restores the old behaviour without a redeploy.

---

## 3. Recognition

`IdentityService.find_or_create_identity`, three steps:

1. **KNOWN search** at `SIMILARITY_THRESHOLD` (0.4). On a hit, the identity may
   also be *enriched* with this embedding — but only if similarity ≥ 0.55,
   quality ≥ `IDENTITY_ENRICH_MIN_QUALITY`, the person has fewer than
   `MAX_EMBEDDINGS_PER_IDENTITY` (10 — the same cap the retention job prunes
   to; enrichment used to grow to 20 and have the nightly sweep undo it), and
   it is not a near-duplicate (≥ `IDENTITY_NEAR_DUPLICATE_MIN`, 0.95).
2. **UNKNOWN search** at `UNKNOWN_SIMILARITY_THRESHOLD` (0.35). On a hit it
   re-checks KNOWN at a **relaxed** threshold (`known − 0.1`, floor 0.2) and
   prefers the known person if that hits. This is the anti-duplicate rule: it
   stops a known person accumulating a shadow UNKNOWN identity.
3. **Neither matched** → a new `UNKNOWN` identity with `display_name = None`.

Both thresholds are **properties read per call**, so the admin settings page and
the environment actually move them. They were hardcoded once, which made
`SIMILARITY_THRESHOLD` a dead knob.

Search always resolves through the database: the index returns embedding keys,
and `identity_embeddings` → `identities` decides who they belong to and whether
they are searchable (`ACTIVE` or `PROMOTED`). A key with no live row cannot win
a match.

---

## 4. Reference

### Enrollment error codes

| Code | HTTP | Meaning |
|---|---|---|
| `no_file`, `empty_file` | 400 | Nothing was uploaded |
| `file_too_large` | 400 | Over `MAX_FILE_SIZE` |
| `invalid_file_type`, `invalid_image` | 400 | Not an image, or undecodable |
| `image_too_small`, `image_too_large` | 400 | Outside the decoded-dimension guards |
| `multiple_faces` | 400 | More than one face; enrollment needs exactly one |
| `no_face`, `no_landmarks` | 400 | No detectable face (the second after the padded retry) |
| `embedding_failed`, `invalid_embedding` | 400 | The model produced nothing usable |
| `invalid_name` | 400 | Empty, under 2 chars, or over 255 |
| `too_many_images` | 400 | 1000 per identity |
| `ambiguous_identity` | **409** | Several active people share the name; `candidates[]` is returned |
| `identity_not_active` | 409 | The target is merged or inactive |
| `identity_not_found` | 404 | No such identity |
| `embedding_save_failed`, `database_update_failed`, `invalid_storage_path` | 500 | Persistence refused; nothing is left behind |
| `service_unavailable` | 503 | The identity service is not up yet |

### Where images live

| What | Path | Flag |
|---|---|---|
| Enrolled photos | `storage/faces/<identity_uuid>/image_NNN.ext` | always |
| Aligned face crops from detections | `STORAGE_DIR/<pipeline>/<name>/...` | `SAVE_IMAGES`, `SAVE_UNKNOWN_FACES` |
| Raw webhook frames (debug) | `WEBHOOK_IMAGES_DIR/<pipeline>/...` | `SAVE_WEBHOOK_IMAGES` |
| Person crops (debug) | `CROPPED_IMAGES_DIR/<pipeline>/...` | `SAVE_CROPPED_IMAGES` |

Debug snapshots are readable only by an administrator, and only from inside the
configured base directory. The pipeline id is validated before it becomes a path
component and containment is asserted against a **fixed** base — the previous
check compared against the already-traversed directory, which made
`pipeline_id="../../storage/faces"` an unauthenticated read of the face gallery.

### Metrics

| Metric | Meaning |
|---|---|
| `fr_webhook_auth_total{result}` | Ingest credential checks: `ok`, `missing`, `invalid`, `would_reject`, `unenforced`. No pipeline label — the caller chooses that value, so it would be unbounded cardinality. |
| `fr_vector_index_pending{backend}` | Committed vectors not yet in the index |

### Tests

| File | Covers |
|---|---|
| `test_identity_multi_image_enrollment.py` | the whole enrollment path, including atomicity under both backends |
| `test_webhook_auth.py` | ingest credentials, route inventory, path traversal, oversized unauthenticated bodies |
| `test_face_quality_scoring.py` | the scoring cap, real-image ordering, numeric fallback, provenance |
| `test_config_guard.py` | the production refusal rules |

---

## Why multiple photos per person, and how the match is chosen

Enrolment stores **one embedding row per image**, all sharing the same
`identity_id`. Recognition does not pick "the" embedding for a person — it
searches every embedding and keeps the best.

```
storage/faces/<identity_uuid>/
├── image_001.jpg     -> identity_embeddings row 1
├── image_002.jpg     -> identity_embeddings row 2
└── image_003.jpg     -> identity_embeddings row 3
```

(The folder is the identity's **UUID**, not the person's name. Names change,
are not unique, and are not safe as paths.)

At detection time the query embedding is compared against all rows and the
highest similarity wins:

```sql
SELECT ie.identity_id,
       1 - (ie.embedding <=> :query) AS similarity
FROM identity_embeddings ie
JOIN identities i ON i.id = ie.identity_id
WHERE i.type = 'KNOWN'
ORDER BY ie.embedding <=> :query
LIMIT :top_k;
```

So a person enrolled from four angles is matched if **any one** of those four
is close enough. A face captured in profile can score 0.92 against the profile
photo while scoring 0.71 against the front-facing one — and 0.92 is what the
system uses.

**Practical guidance:** three to ten photos, chosen for *variety* — different
angles, lighting and expressions. Ten near-identical photos add rows and
latency without adding recall. Image quality still gates enrolment: a photo
below `IDENTITY_QUALITY_THRESHOLD_KNOWN` is stored but its embedding is not,
and the log says so explicitly.
