# Identity Recognition System - Complete Explanation

> **Vector backend note.** Where this document says *FAISS*, the live
> system uses **PostgreSQL + pgvector**. PostgreSQL is authoritative and
> the index is a disposable acceleration layer — see
> [`70_VECTOR_INDEX_CONTRACT.md`](70_VECTOR_INDEX_CONTRACT.md). The
> surrounding explanation of *what* the index does is still accurate.

> **Storage note (2026-08):** face enrollment now lives ONLY in
> `storage/faces/<identity_uuid>/image_NNN.ext`. The old flat
> `assets/faces/<Name>.jpg` gallery was removed and is no longer read
> at startup; enroll through the upload API instead.


**Face Recognition Surveillance System**  
**Date:** January 2025

---

## 🔗 Database Model Relationships

### Core Models and Their Relationships

```
┌─────────────────────────────────────────────────────────────┐
│                    IDENTITY SYSTEM                          │
└─────────────────────────────────────────────────────────────┘

Identity (1) ──< (N) IdentityEmbedding
    │                    │
    │                    ├── faiss_id: Integer → FAISS index position
    │                    ├── faiss_index_type: 'known' | 'unknown'
    │                    └── identity_id: UUID → Identity.id
    │
    ├── type: KNOWN | UNKNOWN
    ├── display_name: "Person Name"
    ├── status: ACTIVE | MERGED | INACTIVE
    └── id: UUID (Primary Key)
    │
    ├──< (N) Face
    │   ├── identity_id: UUID → Identity.id
    │   ├── name: String (from Identity.display_name)
    │   └── label_state: AUTO_KNOWN | AUTO_UNKNOWN
    │
    └──< (N) IdentityAppearance
        └── Timeline of appearances
```

### Relationship Details

**1. Identity → IdentityEmbedding (One-to-Many)**
- One Identity can have multiple embeddings
- Each embedding represents one face detection
- Embeddings link to FAISS index via `faiss_id`

**2. IdentityEmbedding → FAISS Index**
- `faiss_id` = Position in FAISS index (0, 1, 2, ...)
- `faiss_index_type` = Which index ('known' or 'unknown')
- FAISS metadata: `{faiss_id: identity_id}`

**3. Identity → Face (One-to-Many)**
- One Identity can appear in multiple Face records
- Face records are created during detection
- Face.name comes from Identity.display_name

---

## 🔍 How Identity Recognition Works

### Complete Recognition Flow

```
1. Image Processing
   └── Extract face crop from person bbox
   └── SCRFD.detect(crop) → landmarks
   └── ArcFace.get_embedding(crop, landmarks) → 512-d vector
   └── Normalize: embedding / ||embedding||

2. FAISS Search (KNOWN Index)
   └── search_known(normalized_embedding, threshold=0.4)
   └── Returns: [(identity_id, similarity), ...]
   └── Checks:
       ├── similarity >= 0.4
       ├── faiss_id in known_metadata
       └── known_metadata[faiss_id] = identity_id

3. Database Lookup
   └── SELECT Identity WHERE id = identity_id
   └── Checks:
       ├── Identity exists
       ├── Identity.type == KNOWN
       └── Identity.status == ACTIVE

4. Return Result
   └── If all checks pass: Return Identity (KNOWN)
   └── If any check fails: Create new UNKNOWN Identity
```

### Critical Checkpoints

**Checkpoint 1: FAISS Search**
- **Location:** `backend/core/vector_index/`:search_known()`
- **Checks:**
  - Similarity >= threshold (0.4)
  - FAISS ID exists in metadata
  - Metadata maps to identity_id

**Checkpoint 2: Database Lookup**
- **Location:** `backend/core/identity_service.py:find_or_create_identity()`
- **Checks:**
  - Identity exists in database
  - Identity.type == KNOWN
  - Identity.status == ACTIVE

**Checkpoint 3: Type Verification**
- **Location:** `backend/core/identity_service.py:87-94`
- **Checks:**
  - FAISS says identity_id
  - Database says Identity.type == KNOWN
  - If mismatch → Recognition fails!

---

## 🐛 Why Some Known Faces Aren't Recognized

### Common Failure Scenarios

#### Scenario 1: Similarity Below Threshold

**Problem:**
- Face is in FAISS but similarity < 0.4
- Example: Similarity = 0.35 (below 0.4 threshold)

**Why:**
- Different lighting/angle
- Poor image quality
- Face partially occluded
- Different age/appearance

**Debug:**
- Check logs: `[FAISS_SEARCH] Result rejected: sim=0.35 < threshold=0.4`
- Check debug logs: `[IDENTITY_SEARCH] DEBUG: Found matches with lower threshold`

**Solution:**
- Improve image quality
- Add more training images
- Lower threshold (not recommended)

---

#### Scenario 2: FAISS ID Not in Metadata

**Problem:**
- FAISS search finds vector
- But `faiss_id` not in `known_metadata`
- Log: `Skipping orphaned vector (faiss_id=X, not in metadata)`

**Why:**
- FAISS index rebuilt but metadata not updated
- Metadata file corrupted
- Index/metadata out of sync

**Debug:**
- Check: `faiss_id in identity_index.known_metadata`
- Check metadata file: `database/identity_indexes/known_metadata.json`

**Solution:**
- Run FAISS repair
- Rebuild index from database

---

#### Scenario 3: Identity Not in Database

**Problem:**
- FAISS finds identity_id
- Database lookup returns None
- Log: `FAISS found identity_id=... but NOT FOUND in database!`

**Why:**
- Identity deleted but FAISS not cleaned
- Database/FAISS out of sync
- Wrong identity_id in metadata

**Debug:**
- Check database: `SELECT * FROM identities WHERE id = '...'`
- Check FAISS metadata: `known_metadata[faiss_id]`

**Solution:**
- Run FAISS repair
- Check database for identity
- Verify identity_id matches

---

#### Scenario 4: Identity Type Mismatch

**Problem:**
- FAISS finds identity in KNOWN index
- Database says `Identity.type = UNKNOWN`
- Log: `FAISS found identity in KNOWN index but database says type=UNKNOWN`

**Why:**
- Identity promoted but embeddings not moved
- Data corruption
- Manual database edit

**Debug:**
- Check: `Identity.type` in database
- Check: FAISS index type
- Check promotion logs

**Solution:**
- Auto-repair should move embeddings
- Check promotion logic
- Verify identity type

---

#### Scenario 5: Identity Status Not ACTIVE

**Problem:**
- Identity exists and is in FAISS
- But `Identity.status != ACTIVE`
- Recognition fails (new check added)

**Why:**
- Identity merged
- Identity marked inactive
- Status changed manually

**Debug:**
- Check: `Identity.status` in database
- Check logs: `Identity found but status=MERGED (not ACTIVE)`

**Solution:**
- Set status to ACTIVE
- Check if identity was merged

---

#### Scenario 6: Multiple Identities with Same Name

**Problem:**
- Multiple Identity records with same display_name
- Only one has FAISS embeddings
- Recognition finds wrong identity

**Why:**
- Duplicate identities created
- Identity not merged properly
- Name collision

**Debug:**
- Check: `SELECT * FROM identities WHERE display_name = '...'`
- Check which has FAISS embeddings

**Solution:**
- Check for duplicate identities
- Merge duplicates
- Verify unique display_name

---

## 🔧 Debugging Tools

### 1. Enhanced Logging (Already Added)

The system now logs:
- All known identities in FAISS before search
- All matches (even below threshold)
- Database lookup results
- Type and status checks
- Debug matches with lower threshold

**Check logs for:**
- `[IDENTITY_SEARCH] DEBUG:` - Debug information
- `[FAISS_SEARCH] DEBUG:` - FAISS search details
- `[IDENTITY_SEARCH] Match X:` - All matches found

---

### 2. Diagnostic Endpoint (New)

**Endpoint:** `GET /api/admin/identities/debug/{identity_id}`

**Returns:**
- Database state (type, status, display_name)
- FAISS state (in index, faiss_ids, metadata)
- Embeddings state (count, faiss_ids)
- Recognition status
- List of issues

**Example:**
```bash
curl -X GET "http://localhost:8000/api/admin/identities/debug/{identity_id}" \
  -H "Authorization: Bearer <token>"
```

---

### 3. Verify Indexes Endpoint

**Endpoint:** `GET /api/admin/identities/verify-indexes`

**Returns:**
- FAISS count vs Database count
- Match status
- Issues found

---

## 📊 Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│              IDENTITY RECOGNITION DATA FLOW                  │
└─────────────────────────────────────────────────────────────┘

storage/faces/<identity_uuid>/Joey.png
    ↓
IdentityLoader._load_single_face()
    ├── SCRFD.detect() → landmarks
    ├── ArcFace.get_embedding() → embedding
    ├── Create Identity (type=KNOWN)
    ├── identity_index.add_known(identity_id, embedding)
    │   ├── Add to FAISS KNOWN index → faiss_id
    │   ├── known_metadata[faiss_id] = identity_id
    │   └── known_identity_to_faiss[identity_id].append(faiss_id)
    └── Create IdentityEmbedding
        ├── identity_id → Identity.id
        ├── faiss_id → FAISS index position
        └── faiss_index_type = 'known'

Runtime Detection:
    ↓
Image Processing
    ├── Extract embedding
    └── identity_service.find_or_create_identity(embedding)
        ├── identity_index.search_known(embedding, threshold=0.4)
        │   ├── FAISS search → [(identity_id, similarity), ...]
        │   └── Filter by threshold and metadata
        ├── Database lookup: SELECT Identity WHERE id = identity_id
        ├── Check: type == KNOWN, status == ACTIVE
        └── Return Identity OR Create new UNKNOWN
```

---

## ✅ Troubleshooting Steps

### For Each Known Face Not Recognized:

**Step 1: Check FAISS Index**
```python
# Check if identity is in FAISS
identity_id_str = str(identity.id)
in_faiss = identity_id_str in identity_index.known_identity_to_faiss
faiss_ids = identity_index.known_identity_to_faiss.get(identity_id_str, [])
print(f"In FAISS: {in_faiss}, FAISS IDs: {faiss_ids}")
```

**Step 2: Check Database**
```sql
-- Check identity
SELECT id, type, status, display_name 
FROM identities 
WHERE display_name = 'Joey';

-- Check embeddings
SELECT id, faiss_id, faiss_index_type 
FROM identity_embeddings 
WHERE identity_id = '...';
```

**Step 3: Check Recognition Logs**
- Search logs for: `[IDENTITY_SEARCH]` + identity name
- Look for similarity scores
- Check for "rejected" or "skipped" messages

**Step 4: Use Diagnostic Endpoint**
```bash
GET /api/admin/identities/debug/{identity_id}
```

**Step 5: Test Manually**
- Extract embedding from image
- Search FAISS with lower threshold
- Check what matches are found

---

## 🎯 Summary

**The recognition system requires:**
1. ✅ Embedding in FAISS KNOWN index
2. ✅ FAISS ID in metadata
3. ✅ Metadata maps to identity_id
4. ✅ Identity exists in database
5. ✅ Identity.type == KNOWN
6. ✅ Identity.status == ACTIVE
7. ✅ Similarity >= threshold (0.4)

**If any step fails, recognition fails!**

The enhanced logging and diagnostic endpoint will help identify which step is failing for each known face.

---

**Last Updated:** January 2025  
**Version:** 1.0.0

