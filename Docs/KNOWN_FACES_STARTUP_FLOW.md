# Known Faces Startup Flow

## Overview

This document explains the complete flow of how known faces are loaded, processed, and saved when the application starts.

## Startup Sequence

### Phase 1: Infrastructure Setup
1. **Database Migrations** - Run Alembic migrations
2. **Database Initialization** - Connect to PostgreSQL
3. **Redis Cache** - Initialize cache (if enabled)

### Phase 2: Machine Learning Models
1. **Model Manager Initialization** - Load SCRFD (detection) and ArcFace (recognition) models
2. **Identity Index Service** - Initialize FAISS or pgvector indexes
3. **Identity Service** - Initialize identity management service

### Phase 3: Known Faces Loading (Critical Step)

**Location**: `backend/lifespan.py` lines 367-458

```python
# Load known faces from storage/faces into Identity system
if identity_service and model_manager:
    from backend.core.identity_loader import IdentityLoader
    identity_loader = IdentityLoader(identity_service, model_manager)
    
    faces_dir = getattr(settings, 'FACES_DIR', './storage/faces')
    
    async with db_manager.get_session() as db:
        loaded, skipped, errors = await identity_loader.load_known_faces_from_directory(
            faces_dir=faces_dir,
            db=db,
            force_reload=False  # Don't reload existing identities
        )
```

## Detailed Flow: `load_known_faces_from_directory()`

**Location**: `backend/core/identity_loader.py`

### Step 1: Directory Scanning
```python
# Get all image files from storage/faces/
image_files = [
    f for f in os.listdir(faces_dir)
    if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))
]
```

**Example**: Finds `trump.jpg`, `biden.jpg`, etc.

### Step 2: Check Existing Identities
```python
# For each image file:
person_name = filename.rsplit(".", 1)[0]  # Extract "trump" from "trump.jpg"

# Check if identity already exists in database
existing = await db.execute(
    select(Identity).where(
        Identity.type == IdentityType.KNOWN,
        Identity.display_name == person_name
    )
)
```

**If exists and `force_reload=False`**: Skip (already loaded)

### Step 3: Load and Process Image
```python
# Read image file
image = cv2.imread(image_path)
if image is None:
    error_count += 1
    continue

# Convert BGR to RGB
image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
```

### Step 4: Face Detection (SCRFD Model)
```python
# Use SCRFD detector to find faces
bboxes, landmarks = self.model_manager.detector.detect(image_rgb, max_num=1)

if len(bboxes) == 0:
    logger.warning(f"No face detected in {filename}")
    error_count += 1
    continue

# Get first face bounding box
bbox = bboxes[0]
x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
confidence = bbox[4]

# Crop face from image
face_crop = image_rgb[y1:y2, x1:x2]
```

**Result**: Face bounding box and cropped face image

### Step 5: Face Alignment
```python
# Align face using landmarks (if available)
if len(landmarks) > 0:
    # Use landmarks for alignment
    aligned_face = align_face(face_crop, landmarks[0])
else:
    aligned_face = face_crop
```

**Purpose**: Normalize face orientation for better recognition

### Step 6: Embedding Generation (ArcFace Model)
```python
# Generate 512-dimensional embedding using ArcFace
# Note: recognizer.get_embedding() takes full image + landmarks
embedding = self.model_manager.recognizer.get_embedding(image, kpss[0])

# Normalize embedding (L2 norm = 1.0) - CRITICAL for similarity search
embedding_normalized = embedding / np.linalg.norm(embedding)
```

**Result**: 512-dim normalized embedding vector (norm = 1.0)

**Note**: ArcFace recognizer uses the full image with landmarks for better alignment

### Step 7: Create Identity Record
```python
# Create Identity record in database
identity = Identity(
    id=uuid.uuid4(),
    type=IdentityType.KNOWN,
    status=IdentityStatus.ACTIVE,
    display_name=person_name,  # e.g., "trump"
    first_seen_at=datetime.utcnow(),
    last_seen_at=datetime.utcnow(),
    appearances_count=0,
    best_snapshot_path=image_path,  # Path to original image
    created_at=datetime.utcnow(),
    updated_at=datetime.utcnow()
)

db.add(identity)
await db.flush()  # Get identity.id
```

**Result**: Identity record in `identities` table

### Step 8: Save Embedding

#### Option A: pgvector Backend
```python
if USE_PGVECTOR and self.identity_service.use_pgvector:
    # Normalize embedding (ensure norm = 1.0)
    embedding_normalized = embedding / np.linalg.norm(embedding)
    
    # Save embedding to PostgreSQL using pgvector
    emb_id = await self.identity_service.pgvector_index.add_embedding(
        identity_id=str(identity.id),
        embedding=embedding_normalized,  # Already normalized
        detection_id=None,
        pipeline_id="preloaded",
        quality_score=None,
        index_type='known',
        db=db
    )
```

**What happens**:
1. Embedding is normalized (L2 norm = 1.0) - **CRITICAL**
2. Saved to `identity_embeddings` table
3. `embedding` column is `vector(512)` type (pgvector)
4. HNSW index is automatically used for fast similarity search
5. Returns `emb_id` (embedding record ID)

**Database Record**:
- `identity_embeddings.embedding` = vector(512) with normalized values
- `identity_embeddings.identity_id` = UUID of identity
- `identity_embeddings.pipeline_id` = "preloaded"
- `identity_embeddings.faiss_index_type` = 'known'

#### Option B: FAISS Backend
```python
else:
    # Save embedding to FAISS index (in-memory)
    # Note: add_known() normalizes the embedding internally
    faiss_id = self.identity_service.identity_index.add_known(
        identity_id=str(identity.id),
        embedding=embedding  # Will be normalized inside add_known()
    )
    
    # Save embedding record to database
    embedding_record = IdentityEmbedding(
        identity_id=identity.id,
        detection_id=None,
        pipeline_id="preloaded",
        faiss_id=faiss_id,  # FAISS index ID (not embedding vector)
        faiss_index_type='known',
        embedding=None,  # Not stored in DB for FAISS (stored in FAISS index)
        quality=None,
        created_at=datetime.utcnow()
    )
    db.add(embedding_record)
    await db.flush()
```

**What happens**:
1. Embedding normalized inside `add_known()` (L2 norm = 1.0)
2. Added to FAISS `known_index` (IndexFlatIP - exact search)
3. Returns `faiss_id` (position in FAISS index)
4. Metadata stored: `faiss_id -> identity_id` mapping in `known_identity_to_faiss`
5. Embedding record saved to database (with `faiss_id`, not the vector)

**FAISS Storage**:
- Embedding stored in **in-memory** FAISS index
- Database only stores `faiss_id` (reference to FAISS index position)
- Requires sync between FAISS and database

### Step 9: Commit Transaction
```python
if loaded > 0:
    await db.commit()  # Commit all new identities and embeddings
```

## Complete Flow Diagram

```
STARTUP
  │
  ├─> Phase 1: Infrastructure
  │   ├─> Database Migrations
  │   ├─> Database Connection
  │   └─> Redis Cache
  │
  ├─> Phase 2: ML Models
  │   ├─> Model Manager (SCRFD + ArcFace)
  │   ├─> Identity Index Service (FAISS/pgvector)
  │   └─> Identity Service
  │
  └─> Phase 3: Known Faces Loading
      │
      ├─> Scan storage/faces/ directory
      │   └─> Find: trump.jpg, biden.jpg, ...
      │
      ├─> For each image file:
      │   │
      │   ├─> Check if identity exists
      │   │   └─> If exists: SKIP
      │   │
      │   ├─> Load image (cv2.imread)
      │   │
      │   ├─> Face Detection (SCRFD)
      │   │   └─> Get bounding box + landmarks
      │   │
      │   ├─> Face Alignment
      │   │   └─> Normalize face orientation
      │   │
      │   ├─> Embedding Generation (ArcFace)
      │   │   └─> 512-dim vector (normalized)
      │   │
      │   ├─> Create Identity Record
      │   │   └─> Save to identities table
      │   │
      │   └─> Save Embedding
      │       ├─> pgvector: Save to identity_embeddings.embedding (vector)
      │       └─> FAISS: Add to known_index + save faiss_id to DB
      │
      └─> Commit Transaction
```

## Key Points

### 1. **Normalization**
- All embeddings are **L2-normalized** (norm = 1.0)
- This is critical for cosine similarity to work correctly
- Done in both FAISS and pgvector paths

### 2. **Database Storage**

**pgvector**:
- Embedding stored in `identity_embeddings.embedding` (vector type)
- HNSW index for fast similarity search
- All data in PostgreSQL (ACID compliant)

**FAISS**:
- Embedding stored in FAISS index (in-memory)
- `faiss_id` stored in `identity_embeddings.faiss_id`
- Requires sync between FAISS and database

### 3. **Skip Logic**
- If identity already exists, it's skipped (unless `force_reload=True`)
- Prevents duplicate identities
- Speeds up startup on subsequent runs

### 4. **Error Handling**
- Images without faces: Logged as errors, skipped
- Invalid images: Logged as errors, skipped
- Database errors: Rolled back, logged

## Verification

After startup, check logs for:
```
[IDENTITY_LOADER] ✅ Loaded 12 known faces from /app/storage/faces into Identity system
```

Or verify in database:
```sql
-- Check identities
SELECT COUNT(*) FROM identities WHERE type = 'known';

-- Check embeddings (pgvector)
SELECT COUNT(*) FROM identity_embeddings WHERE faiss_index_type = 'known';

-- Check embeddings (FAISS)
SELECT COUNT(*) FROM identity_embeddings WHERE faiss_id IS NOT NULL;
```

## Troubleshooting

### No faces loaded:
1. Check `FACES_DIR` path in logs
2. Verify images exist in directory
3. Check for face detection errors in logs

### Embeddings not saved:
1. Check vector backend (`VECTOR_BACKEND=pgvector` or `faiss`)
2. Verify database connection
3. Check for normalization errors

### Duplicate identities:
- Use `force_reload=True` to reload all faces
- Or manually delete identities from database first

