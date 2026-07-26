# Promotion Flow: UNKNOWN → KNOWN Explained

## Overview

When you promote an unknown identity to known (assign a name), the system performs several critical operations to ensure the person is recognized in future detections. The process differs significantly between **FAISS** and **pgvector** backends.

---

## 🎯 Quick Summary

### **pgvector Backend (Simple & Fast)**
1. ✅ Update `Identity.type`: `UNKNOWN` → `KNOWN`
2. ✅ Update `Identity.status`: `ACTIVE` → `PROMOTED`
3. ✅ Set `Identity.display_name` to the provided name
4. ✅ Update all `IdentityEmbedding` records: `faiss_index_type='unknown'` → `'known'`
5. ✅ Copy best image to `storage/faces/` directory
6. ✅ Update all `Face` records with the new name
7. ✅ **Done!** No index manipulation needed - embeddings stay in PostgreSQL

### **FAISS Backend (Complex)**
1. ✅ Update identity record (same as pgvector)
2. ✅ Reconstruct embeddings from UNKNOWN FAISS index
3. ✅ Add embeddings to KNOWN FAISS index
4. ✅ Remove embeddings from UNKNOWN FAISS index
5. ✅ Update database embedding records with new FAISS IDs
6. ✅ Copy best image to `storage/faces/` directory
7. ✅ Update all `Face` records
8. ✅ Save indexes to disk

---

## 📋 Detailed Step-by-Step Flow

### **Step 1: User Initiates Promotion**

**Action:** User clicks "PROMOTE" button and enters a name (e.g., "John Doe")

**Endpoint:** `POST /api/admin/unknown/{identity_id}/promote`

**Request:**
```json
{
  "display_name": "John Doe",
  "person_code": "EMP-001"  // Optional
}
```

---

### **Step 2: Backend Validates Request**

**Location:** `backend/routes/identities.py` → `promote_unknown_to_known()`

**Checks:**
- ✅ Identity exists
- ✅ Identity type is `UNKNOWN`
- ✅ User has access (admin or pipeline access)
- ✅ Identity service is available
- ✅ Display name is not empty

---

### **Step 3: Route to Appropriate Backend**

**Location:** `backend/core/identity_service.py` → `promote_unknown_to_known()`

The system checks which backend is active:
- **If `VECTOR_BACKEND=pgvector`**: Calls `_promote_with_pgvector()` (simple)
- **If `VECTOR_BACKEND=faiss`**: Calls FAISS promotion logic (complex)

---

## 🔵 PGVECTOR Promotion Flow (Simple)

### **Step 3.1: Update Identity Record**

**Database Changes:**
```python
identity.type = IdentityType.UNKNOWN → IdentityType.KNOWN
identity.status = IdentityStatus.ACTIVE → IdentityStatus.PROMOTED
identity.display_name = None → "John Doe"
identity.updated_at = datetime.utcnow()
```

**Result:** Identity record now marked as KNOWN in database

---

### **Step 3.2: Update Embedding Records**

**What Happens:**
- Query all `IdentityEmbedding` records for this identity with `faiss_index_type='unknown'`
- Update them to `faiss_index_type='known'`

**SQL Equivalent:**
```sql
UPDATE identity_embeddings
SET faiss_index_type = 'known'
WHERE identity_id = :identity_id
  AND faiss_index_type = 'unknown'
```

**Key Point:** 
- ✅ **No vector movement needed!** 
- ✅ Embeddings stay in the same PostgreSQL table
- ✅ pgvector searches both KNOWN and UNKNOWN by filtering on `faiss_index_type`
- ✅ Much faster and simpler than FAISS

**Result:**
- All embeddings now marked as KNOWN
- Future searches will find them in the KNOWN index

---

### **Step 3.3: Copy Best Image to storage/faces/**

**What Happens:**
1. Get `identity.best_snapshot_path` (e.g., `/app/storage/pipeline_id/unknown/image.jpg`)
2. Create safe filename from display name (e.g., `John_Doe.jpg`)
3. Copy image to `storage/faces/John_Doe.jpg`
4. Update `identity.best_snapshot_path` to new location
5. Update all `IdentityAppearance` records that reference the old path

**Result:**
- Image now in `storage/faces/` directory
- Will be loaded on next application startup as a known face

---

### **Step 3.4: Update Face Records**

**What Happens:**
- Update all `Face` records with this `identity_id`:
  - `name`: "Unknown" → "John Doe"
  - `label_state`: `AUTO_UNKNOWN` → `MANUAL_LABELED`

**SQL Equivalent:**
```sql
UPDATE faces
SET name = 'John Doe',
    label_state = 'MANUAL_LABELED'
WHERE identity_id = :identity_id
```

**Result:**
- All historical face detections now show the correct name
- Dashboard will display "John Doe" instead of "Unknown"

---

### **Step 3.5: Verify Promotion**

**What Happens:**
- Count embeddings with `faiss_index_type='known'` for this identity
- Log success message with embedding count

**Result:**
- ✅ Promotion complete!
- ✅ Identity is now KNOWN and will be recognized in future detections

---

## 🟡 FAISS Promotion Flow (Complex)

### **Step 3.1: Update Identity Record**

Same as pgvector (Step 3.1)

---

### **Step 3.2: Get Embeddings from UNKNOWN Index**

**What Happens:**
1. Get FAISS IDs from `unknown_identity_to_faiss` mapping
2. **CRITICAL:** Get FAISS IDs **BEFORE** removing from index
3. Reconstruct embedding vectors from UNKNOWN FAISS index

**Code:**
```python
# Get faiss_ids BEFORE removing
identity_id_str = str(identity_id)
faiss_ids_to_move = list(
    self.identity_index.unknown_identity_to_faiss.get(identity_id_str, [])
)

# Reconstruct embeddings
for faiss_id in faiss_ids_to_move:
    embedding_vector = self.identity_index.unknown_index.reconstruct(int(faiss_id))
    embeddings_to_add.append((faiss_id, embedding_vector, pipeline_id, None))
```

**Example:**
- Identity has 5 embeddings in UNKNOWN index
- FAISS IDs: [100, 101, 102, 103, 104]
- Reconstruct all 5 embedding vectors

**Fallback:** If no embeddings in FAISS index:
- Try to extract embeddings from stored images
- Use `best_snapshot_path` or `IdentityAppearance` records
- Run SCRFD detection + ArcFace embedding generation

---

### **Step 3.3: Remove from UNKNOWN Index**

**What Happens:**
1. Remove identity from `unknown_identity_to_faiss` mapping
2. Remove vectors from UNKNOWN FAISS index (if possible)
3. Update metadata

**Code:**
```python
self.identity_index.remove_from_unknown(identity_id_str)
```

**Result:**
- Identity no longer in UNKNOWN index
- Vectors removed (or marked for removal)

---

### **Step 3.4: Add to KNOWN Index**

**What Happens:**
1. For each reconstructed embedding:
   - Normalize embedding vector
   - Add to KNOWN FAISS index
   - Get new FAISS ID
   - Update `known_identity_to_faiss` mapping

**Code:**
```python
for old_faiss_id, embedding_vector, pipeline_id, image_path in embeddings_to_add:
    new_faiss_id = self.identity_index.add_known(identity_id_str, embedding_vector)
    
    # Update database record
    matching_emb.faiss_index_type = 'known'
    matching_emb.faiss_id = new_faiss_id
```

**Result:**
- ✅ Embeddings now in KNOWN index
- ✅ Can be found in future searches
- ✅ Database records updated with new FAISS IDs

**Note:** If KNOWN index uses IVF (trained index), it must be trained first. If insufficient data, promotion may fail.

---

### **Step 3.5: Update Database Records**

**What Happens:**
1. Update all `IdentityEmbedding` records:
   - `faiss_index_type`: 'unknown' → 'known'
   - `faiss_id`: old_id → new_id

2. Update all `Face` records:
   - `name`: "Unknown" → "John Doe"
   - `label_state`: AUTO_UNKNOWN → MANUAL_LABELED

**Result:**
- Database consistent with FAISS indexes
- All historical detections updated

---

### **Step 3.6: Copy Best Image to storage/faces/**

Same as pgvector (Step 3.3)

---

### **Step 3.7: Save Indexes to Disk**

**What Happens:**
1. Save KNOWN index to disk (includes new embeddings)
2. Save UNKNOWN index to disk (metadata updated)
3. Save metadata JSON files

**Code:**
```python
identity_index.save()  # Called in route handler
```

**Result:**
- ✅ Changes persisted to disk
- ✅ Survives server restart

---

## 🔍 Key Differences: FAISS vs pgvector

| Aspect | FAISS | pgvector |
|--------|-------|----------|
| **Complexity** | Complex (reconstruct, move, re-index) | Simple (just update database) |
| **Speed** | Slower (index manipulation) | Faster (SQL UPDATE) |
| **Reliability** | Can fail if index not trained | Always works |
| **Storage** | Separate indexes on disk | All in PostgreSQL |
| **Embedding Movement** | Physical move between indexes | Just update metadata field |
| **Best For** | High-performance in-memory search | Production (ACID, simpler) |

---

## ✅ What Gets Updated

### **Database Tables:**

1. **`identities`**
   - `type`: `UNKNOWN` → `KNOWN`
   - `status`: `ACTIVE` → `PROMOTED`
   - `display_name`: `None` → "John Doe"
   - `updated_at`: Current timestamp

2. **`identity_embeddings`**
   - `faiss_index_type`: `'unknown'` → `'known'`
   - `faiss_id`: Updated (FAISS only) or unchanged (pgvector)

3. **`faces`**
   - `name`: "Unknown" → "John Doe"
   - `label_state`: `AUTO_UNKNOWN` → `MANUAL_LABELED`

4. **`identity_appearances`**
   - `best_snapshot_path`: Updated if image was moved

### **File System:**

1. **`storage/faces/`**
   - New file: `John_Doe.jpg` (or `John_Doe_1.jpg` if conflict)
   - Copied from original location (not moved)

### **Vector Indexes (FAISS only):**

1. **KNOWN Index**
   - New vectors added
   - Metadata updated

2. **UNKNOWN Index**
   - Vectors removed
   - Metadata updated

---

## 🎯 After Promotion

### **Immediate Effects:**

1. ✅ Identity is now `KNOWN` type
2. ✅ All embeddings searchable in KNOWN index
3. ✅ Future detections will recognize this person
4. ✅ Historical detections show correct name
5. ✅ Image available in `storage/faces/` for startup loading

### **Future Detections:**

When this person is detected again:
1. System searches KNOWN index first
2. Finds match (similarity >= 0.4)
3. Recognizes as "John Doe" (not "Unknown")
4. Shows on dashboard with correct name
5. Updates `last_seen_at` timestamp

---

## ⚠️ Important Notes

### **pgvector Advantages:**

1. **No Index Training:** Works immediately, no training required
2. **ACID Compliance:** Database transaction ensures consistency
3. **Simpler Code:** Just SQL UPDATE, no complex index manipulation
4. **No Disk I/O:** All in database, no separate index files
5. **Easier Debugging:** Can query embeddings directly in PostgreSQL

### **FAISS Considerations:**

1. **Index Training:** IVF indexes must be trained (needs 100+ known faces)
2. **Disk Persistence:** Must save indexes after promotion
3. **Reconstruction:** Must reconstruct vectors before moving
4. **Complexity:** More code paths, more potential failure points

---

## 🔧 Troubleshooting

### **Problem: Promotion succeeds but person still appears as Unknown**

**Possible Causes:**
1. **FAISS:** Embeddings not moved to KNOWN index (check logs)
2. **FAISS:** KNOWN index not trained (set `KNOWN_INDEX_TYPE=flat`)
3. **Both:** Similarity threshold too high (check `KNOWN_THRESHOLD`)
4. **Both:** Embedding quality too low (check quality scores)

**Solution:**
- Check logs for `[IDENTITY_PROMOTE]` messages
- Verify embeddings exist in KNOWN index
- Check similarity scores in detection logs

---

## 📊 Summary

**Promotion is a critical operation that:**
1. ✅ Changes identity type from UNKNOWN to KNOWN
2. ✅ Updates all embeddings to be searchable as KNOWN
3. ✅ Updates all historical detections with the new name
4. ✅ Copies image to `storage/faces/` for startup loading
5. ✅ Ensures future detections recognize the person correctly

**pgvector makes this much simpler and more reliable than FAISS!**

