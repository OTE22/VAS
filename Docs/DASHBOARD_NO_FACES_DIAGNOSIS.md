# Dashboard No Faces - Diagnosis & Solution

## ✅ Root Cause Identified

The dashboard endpoints are **working correctly**. The issue is that **all detected faces are UNKNOWN**, and the dashboard filters them out by default.

## 📊 Current Status

### Database State:
- ✅ **146 recent detections** (last 3 hours)
- ✅ **149 recent faces** detected
- ❌ **0 KNOWN faces** (all are UNKNOWN)
- ❌ **149 UNKNOWN faces** (all detected faces)

### Dashboard Configuration:
- `SHOW_UNKNOWN_FACES_ON_DASHBOARD = False` (default)
- `DASHBOARD_FACE_DISPLAY_HOURS = 3`

### Result:
- **Expected faces on dashboard: 0** (all filtered out as UNKNOWN)

## 🔍 Why Are All Faces UNKNOWN?

The faces are being detected, but **known face recognition is not working**. This means:

1. **Faces are detected** ✅ (149 faces found)
2. **Faces are saved to database** ✅ (all have identity_id linked to UNKNOWN identities)
3. **Known face matching is failing** ❌ (0 faces matched to KNOWN identities)

## 🔧 Possible Causes

### 1. **pgvector Search Not Working** (Most Likely)
- SQL syntax errors were fixed, but **backend needs restart** to apply changes
- If pgvector search fails, all faces default to UNKNOWN

### 2. **Similarity Threshold Too High**
- Current threshold: `0.4` (40% similarity)
- If detected faces don't match known faces with ≥40% similarity, they're marked UNKNOWN

### 3. **Known Faces Not Loaded Properly**
- ✅ 12 known identities exist in database
- ✅ 12 pgvector embeddings exist
- But embeddings might not be searchable if pgvector index is broken

### 4. **Face Quality Too Low**
- Detected faces might be too blurry/small
- Quality threshold might be filtering out matches

## 🛠️ Solution Steps

### Step 1: Restart Backend (CRITICAL)
```bash
docker restart face_recognition_api
```

This applies the SQL syntax fixes for pgvector search.

### Step 2: Verify Known Face Recognition
After restart, check logs for pgvector search activity:
```bash
docker logs face_recognition_api | grep -i "PGVECTOR.*SEARCH\|KNOWN search returned\|Found.*KNOWN identity"
```

Expected logs:
```
[IDENTITY_SEARCH] [PGVECTOR] KNOWN search returned 1 matches
[PROCESS] ✅ KNOWN identity recognized: trump (ID: ...)
```

### Step 3: Test Face Recognition
Upload a test image of a known person (e.g., trump) and verify it's recognized:
```bash
# Check if face is recognized as KNOWN
docker logs face_recognition_api | grep -i "trump\|KNOWN identity"
```

### Step 4: Check Similarity Scores
If faces are still UNKNOWN, check similarity scores:
```bash
docker logs face_recognition_api | grep -i "similarity\|threshold\|match"
```

If similarity is < 0.4, consider:
- Lowering threshold (not recommended for security)
- Improving image quality
- Adding more training images

### Step 5: Verify Endpoints Are Working
Test the REST API endpoint:
```bash
curl -H "Cookie: access_token=YOUR_TOKEN" \
  "http://localhost/api/detections/MD5AL_3EIN_7LWE?limit=10"
```

Should return detections with faces.

## 📋 Endpoint Verification

### ✅ Working Endpoints:

1. **WebSocket (`/ws`)**:
   - Sends initial_data on connection
   - Filters UNKNOWN faces (by design)
   - Returns empty array if no KNOWN faces

2. **REST API (`/api/detections/{pipeline_id}`)**:
   - Returns detections with faces
   - Filters UNKNOWN faces (line 111 in `detections.py`)
   - Returns empty if all faces are UNKNOWN

3. **Dashboard Stats (`/api/dashboard/config`)**:
   - Returns configuration
   - Working correctly

### 🔍 Endpoint Behavior:

**WebSocket Initial Data** (`backend/routes/websocket.py:241-274`):
- Filters faces by `label_state == AUTO_UNKNOWN`
- Filters faces by `identity.type == UNKNOWN`
- Filters faces by `name == "Unknown"`
- Only includes faces if `SHOW_UNKNOWN_FACES_ON_DASHBOARD = True`

**REST API Detections** (`backend/routes/detections.py:110-111`):
- Filters out faces where `name.lower() == "unknown"`
- Only returns detections with non-unknown faces

## 🎯 Expected Behavior After Fix

1. **Face detected** → Generate embedding
2. **Search KNOWN identities** (pgvector) → Find match (similarity ≥ 0.4)
3. **Create/Update KNOWN Identity** → Link face to KNOWN identity
4. **Dashboard shows face** → Face appears on dashboard

## ⚠️ Important Notes

### Why Dashboard Shows No Faces:
- **NOT a bug** - This is by design
- Dashboard is configured to show **ONLY KNOWN faces**
- All 149 detected faces are UNKNOWN (not recognized)
- Dashboard correctly filters them out

### To Show Unknown Faces on Dashboard:
Set in `config.py` or environment:
```python
SHOW_UNKNOWN_FACES_ON_DASHBOARD = True
```

Or via environment variable:
```bash
export SHOW_UNKNOWN_FACES_ON_DASHBOARD=true
```

Then restart backend.

## 📝 Summary

**The endpoints are working correctly!** The issue is:

1. ✅ Detections are happening (146 detections)
2. ✅ Faces are being detected (149 faces)
3. ✅ Data is being saved to database
4. ✅ Endpoints are returning data (filtered correctly)
5. ❌ **Known face recognition is not working** (all faces are UNKNOWN)
6. ❌ Dashboard filters out UNKNOWN faces (by design)

**Solution**: Restart backend to apply pgvector SQL fixes, then verify known face recognition works.

