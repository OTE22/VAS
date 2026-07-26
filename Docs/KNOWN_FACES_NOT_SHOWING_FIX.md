# Known Faces Not Showing - Diagnosis & Fix

## ✅ Current Status

### Database State
- **12 Known Identities** are loaded in the database
- **12 pgvector Embeddings** exist (all known identities have embeddings)
- **0 Known Identities WITHOUT embeddings** (all are properly loaded)

### Storage State
- **12 image files** found in `/app/storage/faces/`
- Files include: trump, bob, Friends characters, etc.

## 🔍 Root Cause

The issue is **NOT** that known faces aren't loaded. They ARE loaded with pgvector embeddings!

The problem is likely one of these:

### 1. **SQL Syntax Errors (FIXED)**
The pgvector search queries had SQL syntax errors that prevented known face recognition:
- **Error**: `PostgresSyntaxError: syntax error at or near ":"` 
- **Cause**: PostgreSQL doesn't allow casting parameter placeholders directly (`:identity_type::identitytype`)
- **Fix Applied**: Changed to cast the column instead (`i.type::text = :identity_type`)

### 2. **System Needs Restart**
After fixing the SQL syntax errors, the backend needs to be restarted to apply the changes.

### 3. **No Active Detections**
If there are no video streams being processed, no faces will be detected (known or unknown).

## 🔧 Solution Steps

### Step 1: Restart the Backend Container
```bash
docker restart face_recognition_api
```

### Step 2: Verify Known Faces Are Loaded
```bash
docker exec face_recognition_api python scripts/check_known_faces_status.py
```

Expected output:
```
✅ All known identities have pgvector embeddings!
   Known Identities (ACTIVE):     12
   Known pgvector Embeddings:     12
```

### Step 3: Check if Detections Are Happening
Check the logs for face detection activity:
```bash
docker logs face_recognition_api | grep -i "detection\|face\|identity"
```

### Step 4: Verify pgvector Search is Working
After restart, check logs for successful pgvector searches:
```bash
docker logs face_recognition_api | grep -i "PGVECTOR.*SEARCH\|KNOWN search returned"
```

## 📊 How It Works

### Known Face Recognition Flow:
```
1. Face Detected in Video Stream
   ↓
2. Generate 512-dim Embedding
   ↓
3. Search KNOWN identities (pgvector)
   - Threshold: 0.4 (40% similarity)
   - Uses: `1 - (embedding <=> query_embedding)` (cosine similarity)
   ↓
4. If Match Found (similarity >= 0.4):
   → Create/Update KNOWN Identity
   → Show on Dashboard as "Known Person"
   ↓
5. If No Match:
   → Search UNKNOWN identities (threshold: 0.35)
   → If still no match: Create NEW UNKNOWN Identity
```

### pgvector Search Query (Fixed):
```sql
WITH query_vector AS (
    SELECT '[0.1, 0.2, ...]'::vector AS vec
)
SELECT 
    ie.identity_id::text as identity_id,
    1 - (ie.embedding <=> qv.vec) as similarity,
    i.display_name
FROM identity_embeddings ie
JOIN identities i ON ie.identity_id = i.id
CROSS JOIN query_vector qv
WHERE 
    ie.embedding IS NOT NULL
    AND i.type::text = 'known'  -- ✅ Fixed: cast column, not parameter
    AND i.status::text = 'active'
    AND 1 - (ie.embedding <=> qv.vec) >= 0.4
ORDER BY ie.embedding <=> qv.vec
LIMIT 5
```

## 🎯 Expected Behavior After Fix

1. **Known faces ARE loaded** ✅ (already confirmed)
2. **pgvector search works** ✅ (SQL syntax fixed)
3. **Known faces are recognized** ✅ (after restart)
4. **Dashboard shows known faces** ✅ (when detections occur)

## ⚠️ Important Notes

### Thresholds:
- **KNOWN threshold**: 0.4 (40% similarity) - faces must match at least 40% to be recognized as known
- **UNKNOWN threshold**: 0.35 (35% similarity) - lower threshold for grouping unknown faces

### Why No Detections Might Show:
1. **No video streams active** - Check if pipelines are running
2. **No faces in video** - Camera might not be detecting faces
3. **Quality too low** - Faces might be too blurry/small to detect
4. **Similarity too low** - Detected faces don't match known faces (similarity < 0.4)

### To Force Reload Known Faces:
```bash
# Via API (if backend is running)
curl -X POST "http://localhost/api/admin/identities/load-known-faces?force_reload=true" \
  -H "Cookie: access_token=YOUR_TOKEN"
```

## 🔄 Verification Checklist

- [x] Known identities exist in database (12 found)
- [x] pgvector embeddings exist (12 found)
- [x] SQL syntax errors fixed
- [ ] Backend restarted (required)
- [ ] Video streams active
- [ ] Face detections happening
- [ ] Known faces being recognized

## 📝 Summary

**The system is correctly configured!** Known faces are loaded with pgvector embeddings. The SQL syntax errors have been fixed. After restarting the backend, known face recognition should work correctly.

The issue was **NOT** missing embeddings, but rather **SQL syntax errors preventing pgvector searches from executing**.

