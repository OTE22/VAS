# Face Detection Database Storage

> **Vector backend note.** Where this document says *FAISS*, the live
> system uses **PostgreSQL + pgvector**. PostgreSQL is authoritative and
> the index is a disposable acceleration layer — see
> [`70_VECTOR_INDEX_CONTRACT.md`](70_VECTOR_INDEX_CONTRACT.md). The
> surrounding explanation of *what* the index does is still accurate.

## Overview

**YES - Every face detected is saved to the database!** The system creates multiple database records for each face detection to maintain a complete audit trail and enable advanced features like cross-camera tracking, temporal analysis, and identity management.

---

## Database Records Created Per Face Detection

When a face is detected in a video stream or image, the following database records are created:

### 1. **Detection Record** (`detections` table)
- **One per image/frame** (not per face)
- Contains:
  - `pipeline_id`: Which camera/pipeline detected it — a real FK to `pipelines` (RESTRICT)
  - `timestamp`: When the detection occurred
  - `processing_time_ms`: How long processing took
  - `worker_id`: Which worker processed it
  - (there is no `image_path` column: full frames are never persisted — the
    sighting crop lives in `faces.face_image_path`, the enrolled gallery in
    `identity_images.storage_path`, the representative in `identities.best_snapshot_path`)

**Example:**
```sql
INSERT INTO detections (pipeline_id, timestamp, processing_time_ms)
VALUES ('camera_01', '2026-01-10 10:30:00', 45.2);
```

### 2. **Face Record** (`faces` table)
- **One per face detected** in the image
- Links to the `Detection` record
- Contains:
  - `detection_id`: Links to the Detection record
  - `name`: Person's name (or "Unknown")
  - `similarity`: Match confidence score
  - `identity_id`: Links to Identity record (UUID)
  - `label_state`: AUTO_KNOWN, AUTO_UNKNOWN, MANUAL, etc.
  - `face_image_path`: Path to cropped face image
  - `bbox_x1, bbox_y1, bbox_x2, bbox_y2`: Face bounding box coordinates

**Example:**
```sql
INSERT INTO faces (detection_id, name, similarity, identity_id, label_state, face_image_path)
VALUES (123, 'John Doe', 0.92, 'uuid-here', 'AUTO_KNOWN', 'storage/camera_01/john_doe/john_doe_20260110_103000.jpg');
```

### 3. **Identity Record** (`identities` table)
- **One per unique person** (created once, then reused)
- Contains:
  - `id`: Unique UUID for this person
  - `type`: KNOWN or UNKNOWN
  - `display_name`: Person's name (NULL for unknown)
  - `status`: ACTIVE, INACTIVE, etc.
  - `first_seen_at`: First detection timestamp
  - `last_seen_at`: Most recent detection timestamp
  - `appearances_count`: Total number of times seen
  - `best_snapshot_path`: Path to best quality image

**Example:**
```sql
-- First time seeing this person:
INSERT INTO identities (id, type, display_name, status, first_seen_at, last_seen_at, appearances_count)
VALUES ('uuid-here', 'KNOWN', 'John Doe', 'ACTIVE', '2026-01-10 10:30:00', '2026-01-10 10:30:00', 1);

-- Subsequent detections:
UPDATE identities 
SET last_seen_at = '2026-01-10 10:35:00', appearances_count = 2
WHERE id = 'uuid-here';
```

### 4. **IdentityEmbedding Record** (`identity_embeddings` table)
- **One per face detection** (with pgvector backend)
- Contains:
  - `identity_id`: Links to Identity record
  - `detection_id`: the EXACT detection this embedding came from — written by
    `detection_evidence.link_embedding_to_detection` with the embedding id the
    frame produced (never "the newest NULL-linked embedding"); NULL only for
    enrolled / preloaded embeddings or for a few seconds before the detection commits
  - `pipeline_id`: the camera (FK RESTRICT); NULL = enrolled photo / preloaded gallery
  - `embedding`: 512-dimensional face embedding vector (pgvector)
  - `quality`: Face quality score
  - `faiss_index_type`: 'known' or 'unknown'
  - `created_at`: When embedding was saved

**Example:**
```sql
INSERT INTO identity_embeddings (identity_id, detection_id, pipeline_id, embedding, quality, faiss_index_type)
VALUES (
  'uuid-here',
  123,
  'camera_01',
  '[0.123, 0.456, ...]'::vector(512),  -- pgvector column
  0.85,
  'known'
);
```

### 5. **IdentityAppearance Record** (`identity_appearances` table)
- **One per detection** (tracks when/where person was seen)
- Contains:
  - `identity_id`: Links to Identity record
  - `pipeline_id`: Where person was seen
  - `track_id`: Optional tracking ID for video streams
  - `start_time`: When appearance started
  - `end_time`: When appearance ended (for video tracks)
  - `best_snapshot_path`: Best quality image from this appearance

**Example:**
```sql
INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, best_snapshot_path)
VALUES ('uuid-here', 'camera_01', '2026-01-10 10:30:00', 'storage/camera_01/john_doe/john_doe_20260110_103000.jpg');
```

---

## Complete Workflow Example

When a face is detected in a video frame:

```
1. Face Detected in Frame
   ↓
2. Generate 512-dim Embedding Vector
   ↓
3. Search Database for Matching Identity
   ├─ If KNOWN match found → Use existing Identity
   └─ If no match → Create new UNKNOWN Identity
   ↓
4. Identity resolution COMMITS FIRST (identity + its embedding exist before
   the detection; the frame keeps the embedding id and an ownership flag)
   ↓
5. Save Face Image to Disk
   └─ storage/{pipeline_id}/{person_name}/face_image.jpg
   ↓
6. ONE transaction per detection — backend/core/detection_evidence.persist_detection
   (batch writer and direct path use the same function):
   ├─ CORE (atomic, no savepoint): Detection → Faces → IdentityAppearance +
   │        identity counters → exact embedding back-link
   │        (LINKED | ALREADY_LINKED; CROSS_LINK_REFUSED / EMBEDDING_MISSING → whole TX rolls back,
   │         nothing committed, the frame-created embedding is compensated)
   ├─ SAVEPOINT A: live-alert triggers   (failure rolls back A only)
   ├─ SAVEPOINT B: watchlist alerts      (failure rolls back B only)
   └─ pipelines.total_detections += 1
   ↓
7. COMMIT → `detection_alerts` WebSocket event (post-commit only; the DB is authoritative)
```

Failure classes (see Docs/61): **A** core failure = nothing committed;
**B** optional alert-enrichment failure = core committed, that subsystem's rows
absent, counted (`alert_enrichment_live` / `alert_enrichment_watchlist`);
**C** post-commit broadcast failure = rows stay. A worker crash between the
identity commit and the detection commit is reconciled by
`identity_retention.reconcile_orphan_camera_embeddings` (startup + every
retention cycle; grace `STALE_CAMERA_EMBEDDING_GRACE` = 10 minutes).

---

## Database Schema Relationships

```
Detection (1) ──→ (N) Face
                      │
                      └──→ (1) Identity (1) ──→ (N) IdentityEmbedding
                      │                              │
                      │                              └──→ (1) Detection (optional)
                      │
                      └──→ (1) Identity (1) ──→ (N) IdentityAppearance
```

---

## Key Points

### ✅ **Every Face is Saved**
- Every face detected creates a `Face` record
- Every face creates an `IdentityEmbedding` record (with pgvector)
- Every face creates/updates an `IdentityAppearance` record

### ✅ **Deduplication**
- **In-memory tracking**: Prevents saving the same person multiple times within a short window (default: 30 seconds)
- **Database**: Still maintains historical records of all appearances
- **Identity reuse**: Same person across different times/dates uses the same `Identity` record

### ✅ **Unknown Faces**
- Unknown faces are **fully tracked** in the database
- They get:
  - `Identity` record (type=UNKNOWN)
  - `IdentityEmbedding` record (with pgvector embedding)
  - `Face` record
  - `IdentityAppearance` record
- Can be promoted to KNOWN later by an admin

### ✅ **Image Storage**
- Face images are saved to: `storage/{pipeline_id}/{person_name}/`
- Unknown faces: `storage/{pipeline_id}/unknown/`
- Path is stored in `Face.face_image_path` and `Identity.best_snapshot_path`

---

## Query Examples

### Get all faces detected today:
```sql
SELECT f.*, d.timestamp, d.pipeline_id, i.display_name, i.type
FROM faces f
JOIN detections d ON f.detection_id = d.id
LEFT JOIN identities i ON f.identity_id = i.id
WHERE DATE(d.timestamp) = CURRENT_DATE
ORDER BY d.timestamp DESC;
```

### Get all unknown faces with their embeddings:
```sql
SELECT i.id, i.first_seen_at, i.last_seen_at, i.appearances_count,
       COUNT(ie.id) as embedding_count
FROM identities i
LEFT JOIN identity_embeddings ie ON i.id = ie.identity_id
WHERE i.type = 'UNKNOWN'
GROUP BY i.id
ORDER BY i.last_seen_at DESC;
```

### Get all appearances of a specific person:
```sql
SELECT ia.*, p.pipeline_name
FROM identity_appearances ia
JOIN pipelines p ON ia.pipeline_id = p.pipeline_id
WHERE ia.identity_id = 'uuid-here'
ORDER BY ia.start_time DESC;
```

---

## Summary

**YES - Each face detected is saved to the database with:**
1. ✅ Detection record (per image/frame)
2. ✅ Face record (per face)
3. ✅ Identity record (per unique person, reused)
4. ✅ IdentityEmbedding record (per face, with pgvector)
5. ✅ IdentityAppearance record (per detection)

This comprehensive storage enables:
- **Historical tracking**: See when/where each person was detected
- **Cross-camera tracking**: Track same person across multiple cameras
- **Temporal analysis**: Analyze movement patterns over time
- **Identity management**: Promote unknown to known, merge identities
- **Audit trail**: Complete record of all face detections
- **Advanced search**: Search by similarity, time range, location, etc.

---

## Related Documentation

- `77_UNKNOWN_FACES_ARCHITECTURE.md` - How unknown faces are handled
- `70_VECTOR_INDEX_CONTRACT.md` - pgvector vs FAISS
- `06_PROMOTE_AND_MERGE_GUIDE.md` - How to promote unknown to known
- Database schema: `db_models.py`

