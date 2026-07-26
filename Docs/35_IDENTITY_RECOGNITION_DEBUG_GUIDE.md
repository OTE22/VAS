# Identity Recognition Debug Guide

**Face Recognition Surveillance System**  
**ITDIR-AI DEPARTMENT**

---

## 📋 Table of Contents

1. [Database Model Relationships](#database-model-relationships)
2. [How Identity Recognition Works](#how-identity-recognition-works)
3. [Common Recognition Failures](#common-recognition-failures)
4. [Debugging Steps](#debugging-steps)
5. [Troubleshooting Checklist](#troubleshooting-checklist)

---

## 🔗 Database Model Relationships

### Core Models

```
Identity (1) ──< (N) IdentityEmbedding
    │
    ├── type: KNOWN | UNKNOWN
    ├── display_name: "Person Name"
    └── id: UUID

IdentityEmbedding (N) ──> (1) Identity
    │
    ├── faiss_id: Integer (index in FAISS)
    ├── faiss_index_type: 'known' | 'unknown'
    ├── identity_id: UUID → Identity.id
    └── quality: Float

Face (N) ──> (1) Identity
    │
    ├── identity_id: UUID → Identity.id
    ├── name: String
    └── label_state: AUTO_KNOWN | AUTO_UNKNOWN
```

### Relationship Flow

```
1. Identity (KNOWN) ← Created from assets/faces
   └── display_name: "Joey"
   
2. IdentityEmbedding ← Created when face detected
   ├── identity_id → Identity.id
   ├── faiss_id → Index in FAISS KNOWN index
   └── faiss_index_type: 'known'
   
3. FAISS Index
   ├── Vector at position faiss_id
   └── Metadata: {faiss_id: identity_id}
   
4. Face ← Created during detection
   ├── identity_id → Identity.id
   └── name: "Joey" (from Identity.display_name)
```

---

## 🔍 How Identity Recognition Works

### Step-by-Step Recognition Flow

```
1. Image Processing
   └── Extract face crop
   └── SCRFD.detect() → landmarks
   └── ArcFace.get_embedding() → 512-d vector
   └── Normalize embedding (L2 norm)

2. FAISS Search (KNOWN Index)
   └── search_known(embedding, threshold=0.4)
   └── Returns: [(identity_id, similarity), ...]
   
3. Database Lookup
   └── SELECT Identity WHERE id = identity_id
   └── Check: identity.type == KNOWN
   
4. Return Result
   └── If found: Return Identity (KNOWN)
   └── If not found: Create new UNKNOWN Identity
```

### Critical Checkpoints

**Checkpoint 1: FAISS Search**
- Embedding must match above threshold (0.4 for KNOWN)
- FAISS ID must exist in metadata
- Metadata must map to valid identity_id

**Checkpoint 2: Database Lookup**
- Identity must exist in database
- Identity.type must be KNOWN
- Identity.status must be ACTIVE

**Checkpoint 3: Type Verification**
- FAISS metadata says identity_id
- Database says Identity.type = KNOWN
- If mismatch → Recognition fails!

---

## ⚠️ Common Recognition Failures

### Failure 1: Similarity Below Threshold

**Symptom:**
- Face is in FAISS but similarity < 0.4
- Log: `❌ Result rejected: sim=0.35 < threshold=0.4`

**Causes:**
- Poor image quality
- Different lighting/angle
- Face partially occluded
- Embedding not normalized correctly

**Solution:**
- Check image quality
- Lower threshold (not recommended)
- Add more training images

---

### Failure 2: FAISS ID Not in Metadata

**Symptom:**
- FAISS search finds vector
- But `idx not in known_metadata`
- Log: `Skipping orphaned vector (faiss_id=X, not in metadata)`

**Causes:**
- FAISS index rebuilt but metadata not updated
- Metadata file corrupted
- Index/metadata out of sync

**Solution:**
- Run FAISS repair
- Rebuild index from database

---

### Failure 3: Identity Not Found in Database

**Symptom:**
- FAISS finds identity_id
- Database lookup returns None
- Log: `FAISS found identity_id=... but NOT FOUND in database!`

**Causes:**
- Identity deleted but FAISS not cleaned
- Database/FAISS out of sync
- Wrong identity_id in metadata

**Solution:**
- Run FAISS repair
- Check database for identity
- Verify identity_id matches

---

### Failure 4: Identity Type Mismatch

**Symptom:**
- FAISS finds identity in KNOWN index
- Database says `Identity.type = UNKNOWN`
- Log: `FAISS found identity in KNOWN index but database says type=UNKNOWN`

**Causes:**
- Identity promoted but embeddings not moved
- Data corruption
- Manual database edit

**Solution:**
- Run auto-repair (moves embeddings)
- Check promotion logic
- Verify identity type

---

### Failure 5: Multiple Identities with Same Name

**Symptom:**
- Multiple Identity records with same display_name
- Only one has FAISS embeddings
- Recognition finds wrong identity

**Causes:**
- Duplicate identities created
- Identity not merged properly
- Name collision

**Solution:**
- Check for duplicate identities
- Merge duplicates
- Verify unique display_name

---

## 🔧 Debugging Steps

### Step 1: Verify FAISS Index State

```python
# Check index size
known_index_size = identity_index.known_index.ntotal
known_metadata_size = len(identity_index.known_metadata)

# Check identity mappings
for identity_id, faiss_ids in identity_index.known_identity_to_faiss.items():
    print(f"Identity {identity_id}: {len(faiss_ids)} embeddings in FAISS")
```

**Expected:**
- `known_index_size == known_metadata_size`
- Each identity has at least 1 FAISS embedding

---

### Step 2: Verify Database State

```sql
-- Check identities
SELECT id, type, display_name, status 
FROM identities 
WHERE type = 'known';

-- Check embeddings
SELECT identity_id, faiss_id, faiss_index_type, COUNT(*) 
FROM identity_embeddings 
WHERE faiss_index_type = 'known'
GROUP BY identity_id, faiss_id, faiss_index_type;
```

**Expected:**
- All KNOWN identities have embeddings
- All embeddings have valid faiss_id

---

### Step 3: Test Recognition Manually

```python
# Extract embedding from image
embedding = model_manager.recognizer.get_embedding(image, landmarks)
embedding = embedding / np.linalg.norm(embedding)

# Search FAISS
matches = identity_index.search_known(embedding, top_k=5, threshold=0.3)
print(f"Matches: {matches}")

# Check each match
for identity_id_str, similarity in matches:
    identity_id = uuid.UUID(identity_id_str)
    identity = db.query(Identity).filter(Identity.id == identity_id).first()
    print(f"Identity: {identity.display_name}, Type: {identity.type}, Similarity: {similarity}")
```

---

### Step 4: Check Logs for Specific Person

Search logs for:
- `[IDENTITY_SEARCH]` - Search process
- `[FAISS_SEARCH]` - FAISS search results
- `identity_id=` - Specific identity
- `similarity=` - Match scores

**Look for:**
- Similarity scores below threshold
- "Skipping orphaned vector"
- "NOT FOUND in database"
- "type mismatch"

---

## ✅ Troubleshooting Checklist

### For Each Known Face Not Recognized:

- [ ] **FAISS Index:**
  - [ ] Is embedding in FAISS? (`known_index.ntotal`)
  - [ ] Is faiss_id in metadata? (`known_metadata[faiss_id]`)
  - [ ] Does metadata map to correct identity_id?

- [ ] **Database:**
  - [ ] Does Identity exist? (`SELECT * FROM identities WHERE display_name = '...'`)
  - [ ] Is Identity.type = KNOWN?
  - [ ] Is Identity.status = ACTIVE?
  - [ ] Does IdentityEmbedding exist? (`SELECT * FROM identity_embeddings WHERE identity_id = ...`)

- [ ] **Recognition:**
  - [ ] What similarity score? (Check logs)
  - [ ] Above threshold? (0.4 for KNOWN)
  - [ ] Any errors in logs?

- [ ] **Data Consistency:**
  - [ ] FAISS metadata matches database?
  - [ ] identity_id matches between FAISS and database?
  - [ ] No duplicate identities?

---

## 🐛 Debugging Commands

### Check FAISS State
```python
# In Python console or debug endpoint
from backend.core.identity_index import identity_index

# Known index
print(f"KNOWN index size: {identity_index.known_index.ntotal}")
print(f"KNOWN metadata size: {len(identity_index.known_metadata)}")
print(f"KNOWN identities: {len(identity_index.known_identity_to_faiss)}")

# List all known identities
for identity_id, faiss_ids in identity_index.known_identity_to_faiss.items():
    print(f"Identity {identity_id}: {faiss_ids} embeddings")
```

### Check Database State
```sql
-- All known identities
SELECT id, display_name, type, status 
FROM identities 
WHERE type = 'known'
ORDER BY display_name;

-- Embeddings per identity
SELECT 
    i.display_name,
    i.id,
    COUNT(ie.id) as embedding_count,
    COUNT(ie.faiss_id) as faiss_count
FROM identities i
LEFT JOIN identity_embeddings ie ON i.id = ie.identity_id
WHERE i.type = 'known'
GROUP BY i.id, i.display_name;
```

### Test Recognition for Specific Person
```python
# Get identity
identity = db.query(Identity).filter(Identity.display_name == "Joey").first()

# Get FAISS IDs
faiss_ids = identity_index.known_identity_to_faiss.get(str(identity.id), [])

# Reconstruct embeddings
for faiss_id in faiss_ids:
    embedding = identity_index.known_index.reconstruct(faiss_id)
    print(f"FAISS ID {faiss_id}: embedding norm = {np.linalg.norm(embedding)}")
```

---

## 📊 Recognition Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    RECOGNITION FLOW                         │
└─────────────────────────────────────────────────────────────┘

Image → Face Crop → Embedding (512-d)
    ↓
Normalize (L2)
    ↓
FAISS Search (KNOWN index)
    ├─→ Search returns: [(identity_id, similarity), ...]
    ├─→ Filter by threshold (0.4)
    └─→ Check metadata (faiss_id → identity_id)
    ↓
Database Lookup
    ├─→ SELECT Identity WHERE id = identity_id
    ├─→ Check: type == KNOWN
    └─→ Check: status == ACTIVE
    ↓
✅ Return Identity (KNOWN)
    OR
❌ Create new UNKNOWN Identity
```

---

## 🔍 Common Issues and Solutions

### Issue: "FAISS has embedding but not recognized"

**Possible Causes:**
1. Similarity below threshold (0.4)
2. Identity.type != KNOWN in database
3. FAISS ID not in metadata
4. Identity deleted from database

**Debug:**
1. Check similarity score in logs
2. Verify Identity.type in database
3. Check known_metadata[faiss_id]
4. Verify Identity exists

---

### Issue: "Some known faces recognized, others not"

**Possible Causes:**
1. Different image quality
2. Different lighting/angle
3. Some embeddings not in FAISS
4. Some identities have wrong type

**Debug:**
1. Compare similarity scores
2. Check FAISS index for all identities
3. Verify all identities have embeddings
4. Check Identity.type for all

---

### Issue: "Identity in FAISS but type=UNKNOWN in database"

**Possible Causes:**
1. Promotion didn't move embeddings
2. Manual database edit
3. Data corruption

**Solution:**
- Auto-repair should move embeddings
- Check promotion logic
- Verify identity type

---

**Last Updated:** January 2025  
**Version:** 1.0.0

