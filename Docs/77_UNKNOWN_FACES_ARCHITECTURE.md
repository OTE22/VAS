# Unknown Faces Handling - System Architecture & Production Best Practices

> **Vector backend note.** Where this document says *FAISS*, the live
> system uses **PostgreSQL + pgvector**. PostgreSQL is authoritative and
> the index is a disposable acceleration layer — see
> [`70_VECTOR_INDEX_CONTRACT.md`](70_VECTOR_INDEX_CONTRACT.md). The
> surrounding explanation of *what* the index does is still accurate.

## Overview

This document explains how the Face Recognition System handles **unknown faces** (faces that don't match any known person in the database), including database storage, vector embeddings, and production best practices.

---

## How Unknown Faces Are Handled

### 1. Detection Workflow

When a face is detected in a video stream:

```
Face Detected
    ↓
Generate Embedding (512-dim vector)
    ↓
Search KNOWN faces index (threshold: 0.65)
    ↓
[No Match] → Search UNKNOWN faces index (threshold: 0.35)
    ↓
[No Match] → Create NEW UNKNOWN Identity
```

### 2. Database Storage

**YES, unknown faces ARE added to the database!**

#### Identity Record (`identities` table)
- **Type**: `IdentityType.UNKNOWN`
- **Status**: `IdentityStatus.ACTIVE`
- **Display Name**: `NULL` (no name assigned yet)
- **UUID**: Unique identifier for this unknown person
- **Timestamps**: `first_seen_at`, `last_seen_at`, `created_at`, `updated_at`
- **Appearances Count**: Tracks how many times this person was seen

#### Embedding Record (`identity_embeddings` table)
- **Identity ID**: Links to the Identity record
- **Embedding Vector**: 512-dimensional face embedding
- **Index Type**: `'unknown'` (distinguishes from `'known'`)
- **Pipeline ID**: Where the face was detected
- **Detection ID**: Links to the detection event
- **Quality Score**: Face quality (blur, size, confidence, pose)

### 3. Vector Storage Backend

The system supports **two backends** for storing embeddings:

#### A. **pgvector (Recommended for Production)**

When `VECTOR_BACKEND=pgvector` (default):

```python
# Embedding is stored directly in PostgreSQL
IdentityEmbedding(
    identity_id=uuid,
    embedding=[0.123, 0.456, ...],  # 512-dim vector in pgvector column
    faiss_index_type='unknown',
    pipeline_id='camera_01',
    quality_score=0.85
)
```

**Benefits:**
- ✅ **ACID Compliance**: Database transactions ensure data integrity
- ✅ **Persistence**: Embeddings survive server restarts
- ✅ **Simpler Architecture**: No need to sync FAISS index with database
- ✅ **Scalability**: PostgreSQL handles millions of vectors efficiently
- ✅ **Backup/Recovery**: Standard database backup includes embeddings
- ✅ **Query Flexibility**: Can query embeddings with SQL

**Storage Location:**
- Database: `identity_embeddings.embedding` column (pgvector `Vector(512)` type)
- Index: HNSW index on `embedding` column for fast similarity search

#### B. **FAISS (Legacy/Fast In-Memory)**

When `VECTOR_BACKEND=faiss`:

```python
# Embedding stored in in-memory FAISS index
faiss_id = identity_index.add_unknown(identity_id, embedding)

# Database record only stores metadata
IdentityEmbedding(
    identity_id=uuid,
    faiss_id=12345,  # Reference to FAISS index position
    faiss_index_type='unknown',
    embedding=NULL,  # Not stored in database
    pipeline_id='camera_01'
)
```

**Limitations:**
- ❌ **No Persistence**: FAISS index is lost on server restart
- ❌ **Sync Required**: Must rebuild index from database on startup
- ❌ **Memory Only**: Limited by available RAM
- ❌ **No ACID**: Risk of data loss if server crashes

---

## Search Process for Unknown Faces

### Step 1: Search KNOWN Faces
```python
# Threshold: 0.65 (stricter - must be very similar)
known_matches = search_known(embedding, threshold=0.65)
if known_matches:
    return known_identity
```

### Step 2: Search UNKNOWN Faces
```python
# Threshold: 0.35 (looser - allows more variation)
unknown_matches = search_unknown(embedding, threshold=0.35)
if unknown_matches:
    return existing_unknown_identity
```

### Step 3: Create New UNKNOWN Identity
```python
# No match found - create new unknown person
new_identity = create_unknown_identity(embedding)
```

**Why Different Thresholds?**
- **Known (0.65)**: Stricter to avoid false positives (misidentifying someone)
- **Unknown (0.35)**: Looser to group similar unknown faces together (same person seen multiple times)

---

## Image Storage

### Configuration Options

```python
# config.py or environment variables
SAVE_UNKNOWN_FACES = True  # Save unknown face images to disk
SKIP_UNKNOWN_FACES = False  # Skip processing unknown faces entirely
MAX_PHOTOS_PER_PERSON = 10  # Limit for known faces (not applied to unknown)
```

### Storage Structure

```
storage/
├── {pipeline_id}/
│   ├── unknown/
│   │   ├── unknown_20260110_143022_123456.jpg
│   │   ├── unknown_20260110_143045_234567.jpg
│   │   └── ...
│   ├── joey/
│   │   └── joey_20260110_140000_111111.jpg
│   └── ...
```

**Important Notes:**
- Unknown faces are saved to `storage/{pipeline_id}/unknown/` directory
- **No limit** on unknown face images (allows multiple different unknown people)
- Images are linked to Identity records via `best_snapshot_path`

---

## Production Best Practices

### 1. **Use pgvector Backend** ✅

**Recommended Configuration:**
```yaml
# docker-compose.yml
environment:
  VECTOR_BACKEND: pgvector  # Use pgvector, not FAISS
```

**Why?**
- Data persistence and integrity
- Simpler architecture (no index sync needed)
- Better for production reliability
- Easier backup and recovery

### 2. **Quality Threshold**

```python
# Only save high-quality embeddings
QUALITY_THRESHOLD = 0.5  # Minimum quality score

# Quality score considers:
# - Face size (larger = better)
# - Detection confidence
# - Blur level
# - Pose angle
```

**Best Practice:**
- Set `QUALITY_THRESHOLD` to filter out low-quality faces
- Prevents database bloat from blurry/distant faces
- Improves search accuracy

### 3. **Unknown Face Management**

#### A. **Promote to Known**
When an unknown person is identified:
```python
# Admin action: Promote unknown to known
POST /api/admin/unknown/{identity_id}/promote
{
    "display_name": "John Doe"
}
```

This:
- Changes `Identity.type` from `UNKNOWN` to `KNOWN`
- Updates `display_name`
- Moves embeddings from unknown to known index (if using FAISS)
- Updates all related detections

#### B. **Merge Unknown Identities**
If the same unknown person has multiple Identity records:
```python
# Admin action: Merge identities
POST /api/admin/identities/merge
{
    "from_identity_id": "uuid-1",
    "to_identity_id": "uuid-2"
}
```

#### C. **Cleanup Old Unknown Faces**
```sql
-- ⚠️ DESTRUCTIVE. Take a backup first (Docs/60_BACKUP_AND_RESTORE.md).
-- Prefer the retention system over ad-hoc SQL — it also removes the files.
-- Archive or delete unknown faces older than 90 days
DELETE FROM identities 
WHERE type = 'unknown' 
  AND last_seen_at < NOW() - INTERVAL '90 days'
  AND status = 'active';
```

### 4. **Database Indexing**

Ensure proper indexes for performance:

```sql
-- Index on identity type and status
CREATE INDEX idx_identity_type_status ON identities(type, status);

-- Index on last_seen_at for cleanup queries
CREATE INDEX idx_identity_last_seen ON identities(last_seen_at);

-- pgvector HNSW index for fast similarity search
CREATE INDEX idx_embedding_vector_hnsw 
ON identity_embeddings 
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

### 5. **Monitoring & Alerts**

**Key Metrics to Monitor:**
- Number of unknown faces created per day
- Unknown face storage growth rate
- Search performance (query latency)
- Database size growth

**Alert Thresholds:**
- Unknown faces > 10,000: Review cleanup policy
- Database size > 50GB: Consider archiving old data
- Search latency > 500ms: Optimize indexes

### 6. **Storage Management**

**Image Storage:**
```python
# Recommended: Enable unknown face saving for investigation
SAVE_UNKNOWN_FACES = True

# Optional: Limit storage per pipeline
MAX_UNKNOWN_FACES_PER_PIPELINE = 1000  # Custom setting
```

**Database Storage:**
- Each embedding: ~2KB (512 floats × 4 bytes)
- 1,000 unknown faces ≈ 2MB
- 100,000 unknown faces ≈ 200MB

**Best Practice:**
- Archive old unknown faces (>90 days) to separate table
- Keep recent unknown faces for investigation
- Regular cleanup of low-quality embeddings

### 7. **Security & Privacy**

**GDPR/Privacy Considerations:**
- Unknown faces are personal data
- Implement retention policies
- Provide deletion mechanisms
- Log access to unknown face data

**Access Control:**
- Only authorized users can view unknown faces
- Admin-only promotion/merge actions
- Audit logging for all operations

---

## Current System Implementation

### Database Schema

```sql
-- Identity record
CREATE TABLE identities (
    id UUID PRIMARY KEY,
    type VARCHAR(20) NOT NULL,  -- 'unknown' or 'known'
    display_name VARCHAR(255),  -- NULL for unknown
    status VARCHAR(20) DEFAULT 'active',
    first_seen_at TIMESTAMP,
    last_seen_at TIMESTAMP,
    appearances_count INTEGER DEFAULT 0,
    best_snapshot_path VARCHAR(512)
);

-- Embedding record
CREATE TABLE identity_embeddings (
    id SERIAL PRIMARY KEY,
    identity_id UUID REFERENCES identities(id),
    embedding VECTOR(512),  -- pgvector type
    faiss_index_type VARCHAR(50),  -- 'unknown' or 'known'
    pipeline_id VARCHAR(255),
    detection_id INTEGER,
    quality FLOAT,
    created_at TIMESTAMP
);
```

### Code Flow

```python
# 1. Face detected → Generate embedding
embedding = recognizer.get_embedding(face_image, landmarks)

# 2. Search for identity
identity, is_new, similarity = await identity_service.find_or_create_identity(
    embedding=embedding,
    pipeline_id=pipeline_id,
    detection_id=detection_id,
    db=db,
    quality_score=quality_score
)

# 3. If new unknown identity created:
#    - Identity record created in database
#    - Embedding saved to pgvector (or FAISS)
#    - Image saved to storage/{pipeline_id}/unknown/
```

---

## Summary

### ✅ Unknown Faces ARE Stored in Database

1. **Identity Record**: Created in `identities` table with `type='unknown'`
2. **Embedding Record**: Stored in `identity_embeddings` table
3. **Vector Storage**: 
   - **pgvector**: Embedding stored in PostgreSQL `Vector(512)` column
   - **FAISS**: Embedding stored in in-memory index (metadata in DB)

### ✅ Production Best Practices

1. **Use pgvector** for persistence and reliability
2. **Set quality threshold** to filter low-quality faces
3. **Implement cleanup policies** for old unknown faces
4. **Monitor storage growth** and set alerts
5. **Enable image saving** for investigation (`SAVE_UNKNOWN_FACES=True`)
6. **Regular archiving** of old unknown faces (>90 days)
7. **Proper indexing** for performance
8. **Security & privacy** compliance (GDPR, retention policies)

### 🔄 Workflow

```
Unknown Face Detected
    ↓
Create Identity (type=UNKNOWN)
    ↓
Save Embedding (pgvector or FAISS)
    ↓
Save Image (storage/{pipeline_id}/unknown/)
    ↓
Link to Detection Record
    ↓
[Later] Admin can:
    - Promote to Known (assign name)
    - Merge with other Unknown identities
    - Delete/Archive old unknown faces
```

---

## Configuration Reference

```python
# config.py
VECTOR_BACKEND = "pgvector"  # Recommended: pgvector
SAVE_UNKNOWN_FACES = True  # Save unknown face images
SKIP_UNKNOWN_FACES = False  # Process unknown faces
QUALITY_THRESHOLD = 0.5  # Minimum quality to save embedding
KNOWN_THRESHOLD = 0.65  # Similarity threshold for known faces
UNKNOWN_THRESHOLD = 0.35  # Similarity threshold for unknown faces
```

---

## Related Documentation

- `70_VECTOR_INDEX_CONTRACT.md` - Why pgvector is recommended
- `06_PROMOTE_AND_MERGE_GUIDE.md` - How to promote unknown to known
- Database schema: `db_models.py`
- Identity service: `backend/core/identity_service.py`

