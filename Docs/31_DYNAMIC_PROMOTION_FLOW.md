# Dynamic Promotion Flow: Unknown → Known
## How the System Manages Index Migration Dynamically

### 🔄 Overview

When you promote an unknown face to known (assign a name), the system **dynamically moves** all embeddings from the UNKNOWN FAISS index to the KNOWN FAISS index **immediately**. This ensures the person is recognized in future detections.

---

## 📋 Step-by-Step Flow

### **Step 1: User Promotes Identity**

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
- ✅ Identity type is UNKNOWN
- ✅ User has access (admin or pipeline access)
- ✅ Identity service is available

---

### **Step 3: Update Identity Record**

**Location:** `backend/core/identity_service.py` → `promote_unknown_to_known()`

**Database Changes:**
```python
identity.type = IdentityType.UNKNOWN → IdentityType.KNOWN
identity.status = IdentityStatus.ACTIVE → IdentityStatus.PROMOTED
identity.display_name = None → "John Doe"
identity.updated_at = datetime.utcnow()
```

**Result:** Identity record now marked as KNOWN in database

---

### **Step 4: Get Embeddings from UNKNOWN Index**

**What Happens:**
1. Query database for all `IdentityEmbedding` records with `faiss_index_type='unknown'`
2. Get FAISS IDs from `unknown_identity_to_faiss` mapping
3. **CRITICAL:** Get FAISS IDs **BEFORE** removing from index

**Code:**
```python
# Get faiss_ids BEFORE removing
identity_id_str = str(identity_id)
faiss_ids_to_move = list(
    self.identity_index.unknown_identity_to_faiss.get(identity_id_str, [])
)
```

**Example:**
- Identity has 5 embeddings in UNKNOWN index
- FAISS IDs: [100, 101, 102, 103, 104]

---

### **Step 5: Reconstruct Embeddings from UNKNOWN Index**

**What Happens:**
1. For each FAISS ID, reconstruct the embedding vector from UNKNOWN FAISS index
2. Store embeddings temporarily in memory

**Code:**
```python
embeddings_to_add = []
for faiss_id in faiss_ids_to_move:
    # Reconstruct embedding from UNKNOWN index
    embedding_vector = self.identity_index.unknown_index.reconstruct(int(faiss_id))
    embeddings_to_add.append((faiss_id, embedding_vector))
```

**Why:** FAISS doesn't support moving vectors directly, so we must:
- Extract vectors from UNKNOWN index
- Add them to KNOWN index
- Update metadata

---

### **Step 6: Remove from UNKNOWN Index**

**What Happens:**
1. Remove identity from `unknown_identity_to_faiss` mapping
2. Remove FAISS IDs from `unknown_metadata` mapping
3. **Note:** Vectors remain in FAISS index (FAISS doesn't support removal)
4. They're just marked as "removed" in metadata (ignored in searches)

**Code:**
```python
removed_count = self.identity_index.remove_from_unknown(identity_id_str)
```

**Result:**
- ✅ Identity no longer appears in UNKNOWN index searches
- ✅ Metadata cleaned up
- ⚠️ Vectors still in FAISS (will be cleaned up in periodic rebuild)

---

### **Step 7: Check KNOWN Index Training Status**

**What Happens:**
1. Check if KNOWN index is an IVF/IVFPQ index
2. Check if it's trained
3. If not trained, attempt to train it

**Code:**
```python
index_needs_training = (
    hasattr(self.identity_index.known_index, 'is_trained') and 
    not self.identity_index.known_index.is_trained
)

if index_needs_training:
    # Get existing known embeddings for training
    # Or use promoted embeddings if none exist
    # Train the index
    self.identity_index.train_known_index(training_embeddings)
```

**Scenarios:**
- **Flat/HNSW Index:** No training needed ✅
- **IVF Index (Trained):** Already trained, can add vectors ✅
- **IVF Index (Untrained):** Must train first (uses existing known embeddings or promoted ones)

---

### **Step 8: Add Embeddings to KNOWN Index**

**What Happens:**
1. For each reconstructed embedding:
   - Normalize embedding vector
   - Add to KNOWN FAISS index
   - Get new FAISS ID
   - Update `known_metadata` mapping
   - Update `known_identity_to_faiss` mapping

**Code:**
```python
for old_faiss_id, embedding_vector in embeddings_to_add:
    new_faiss_id = self.identity_index.add_known(identity_id_str, embedding_vector)
    
    # Update database record
    matching_emb.faiss_index_type = 'known'
    matching_emb.faiss_id = new_faiss_id
```

**Result:**
- ✅ Embeddings now in KNOWN index
- ✅ Can be found in future searches
- ✅ Database records updated

---

### **Step 9: Update Database Records**

**What Happens:**
1. Update all `IdentityEmbedding` records:
   - `faiss_index_type`: 'unknown' → 'known'
   - `faiss_id`: old_id → new_id

2. Update all `Face` records:
   - `name`: "Unknown" → "John Doe"
   - `label_state`: AUTO_UNKNOWN → MANUAL_LABELED

**Code:**
```python
# Update embedding records
for emb in embeddings:
    emb.faiss_index_type = 'known'
    emb.faiss_id = new_faiss_id  # Updated

# Update face records
await db.execute(
    update(Face).where(Face.identity_id == identity_id).values(
        name=display_name,
        label_state=LabelState.MANUAL_LABELED
    )
)
```

---

### **Step 10: Save Indexes to Disk**

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

## 🎯 Complete Flow Diagram

```
User Promotes Identity
         ↓
[1] Validate Request
         ↓
[2] Update Identity Record (type=KNOWN, name="John Doe")
         ↓
[3] Get FAISS IDs from UNKNOWN index
         ↓
[4] Reconstruct Embeddings from UNKNOWN index
         ↓
[5] Remove from UNKNOWN index metadata
         ↓
[6] Check KNOWN index training (if IVF)
         ↓
[7] Add Embeddings to KNOWN index
         ↓
[8] Update Database Records
         ↓
[9] Save Indexes to Disk
         ↓
✅ Promotion Complete!
```

---

## 🔍 Key Features

### **1. Immediate Migration**
- Embeddings moved **immediately** during promotion
- No waiting for next detection
- Person is recognized right away

### **2. Automatic Training**
- If KNOWN index is IVF and not trained:
  - System attempts to train with existing known embeddings
  - Or uses promoted embeddings if none exist
  - Fails gracefully with clear error if insufficient data

### **3. Error Handling**
- Handles missing embeddings gracefully
- Marks for re-indexing if reconstruction fails
- Logs all operations for debugging

### **4. Database Consistency**
- Updates all related records:
  - IdentityEmbedding records
  - Face records
  - Identity record
- All in single transaction

---

## 📊 Example: Promoting "John Doe"

### **Before Promotion:**
```
UNKNOWN Index:
  - FAISS ID 100: identity_abc123
  - FAISS ID 101: identity_abc123
  - FAISS ID 102: identity_abc123

KNOWN Index:
  - (empty or other identities)
```

### **After Promotion:**
```
UNKNOWN Index:
  - FAISS ID 100: (removed from metadata)
  - FAISS ID 101: (removed from metadata)
  - FAISS ID 102: (removed from metadata)

KNOWN Index:
  - FAISS ID 500: identity_abc123 (John Doe) ✅
  - FAISS ID 501: identity_abc123 (John Doe) ✅
  - FAISS ID 502: identity_abc123 (John Doe) ✅
```

### **Next Detection:**
```
1. Face detected → Embedding generated
2. Search KNOWN index → ✅ Found! (similarity=0.92)
3. Returns: identity_abc123, name="John Doe"
4. No new UNKNOWN identity created ✅
```

---

## ⚙️ Configuration Impact

### **Index Type: Flat (IndexFlatIP)**
- ✅ No training needed
- ✅ Immediate addition
- ✅ Works for any size

### **Index Type: IVF (IndexIVFFlat)**
- ⚠️ Must be trained first
- ✅ Can add vectors after training
- ✅ Faster search (10-50x)

### **Index Type: HNSW (IndexHNSWFlat)**
- ✅ No training needed
- ✅ Immediate addition
- ✅ Fastest search

---

## 🚨 Edge Cases Handled

### **1. IVF Index Not Trained**
**Problem:** Trying to add to untrained IVF index

**Solution:**
- Check training status before adding
- Attempt to train with existing embeddings
- Use promoted embeddings if no existing ones
- Fail gracefully with clear error

### **2. Embedding Reconstruction Fails**
**Problem:** Can't reconstruct embedding from UNKNOWN index

**Solution:**
- Mark embedding for re-indexing
- Set `faiss_id = None`
- Will be re-indexed on next detection

### **3. No Embeddings Found**
**Problem:** Identity has no embeddings in UNKNOWN index

**Solution:**
- Log warning
- Continue with promotion
- Embeddings will be added on next detection

---

## ✅ Verification

After promotion, verify it worked:

```bash
# Check identity type
GET /api/admin/identity/{identity_id}
# Should return: type="known"

# Verify indexes
GET /api/admin/identities/verify-indexes
# Should show identity in KNOWN index, not UNKNOWN
```

---

## 📝 Summary

**The system dynamically manages promotion by:**

1. ✅ **Extracting** embeddings from UNKNOWN index
2. ✅ **Adding** them to KNOWN index immediately
3. ✅ **Updating** all database records
4. ✅ **Saving** indexes to disk
5. ✅ **Handling** training requirements automatically
6. ✅ **Ensuring** person is recognized in next detection

**Result:** Promotion is **atomic** and **immediate** - no waiting, no rebuilds needed!

