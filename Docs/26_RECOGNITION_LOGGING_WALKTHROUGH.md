# Step-by-Step Logging Guide - pgvector Known User Recognition

## Overview

Comprehensive step-by-step logging has been added to trace the entire flow:

**Face Recognition Pipeline:**
1. **SCRFD** (Face Detection Model) - Detects faces and landmarks
2. **ArcFace** (Recognition Model) - Generates 512-dim embeddings
3. **pgvector** (PostgreSQL Extension) - Searches for similar embeddings
4. **Identity Recognition** - Matches to KNOWN/UNKNOWN identities
5. **WebSocket** - Sends data to dashboard

The system uses **SCRFD for detection**, **ArcFace for embeddings**, and **pgvector for similarity search**.

## Logging Flow

### Step 1: Face Detection (SCRFD Model)
**Location**: `backend/services/image_processing.py`

```
[PROCESS] [STEP-BY-STEP] ========================================
[PROCESS] [STEP-BY-STEP] 🔍 Step 1: Face Detection (SCRFD Model)
[PROCESS] [STEP-BY-STEP] Running SCRFD detector on crop {pred_idx}...
[PROCESS] [STEP-BY-STEP] ✅ SCRFD: Face detected successfully (bboxes: {N}, landmarks: {N})
```

### Step 2: Embedding Generation (ArcFace Model)
**Location**: `backend/services/image_processing.py`

```
[PROCESS] [STEP-BY-STEP] 🔍 Step 2: Embedding Generation (ArcFace Model)
[PROCESS] [STEP-BY-STEP] Running ArcFace recognizer to generate 512-dim embedding...
[PROCESS] [STEP-BY-STEP] ✅ ArcFace: Embedding generated successfully
[PROCESS] [STEP-BY-STEP] Embedding shape: (512,), norm: {norm}
```

### Step 3: Identity Recognition (pgvector Search)
**Location**: `backend/services/image_processing.py`

```
[PROCESS] [STEP-BY-STEP] ========================================
[PROCESS] [STEP-BY-STEP] 🔍 Step 3: Identity Recognition (pgvector Search)
[PROCESS] [STEP-BY-STEP] Pipeline: {pipeline_id}
[PROCESS] [STEP-BY-STEP] Using pgvector backend for similarity search
[PROCESS] [STEP-BY-STEP] Embedding shape: (512,), norm: {norm}
[PROCESS] [STEP-BY-STEP] Quality score: {quality_score}
[PROCESS] [STEP-BY-STEP] ========================================
```

### Step 4: pgvector KNOWN Identity Search
**Location**: `backend/core/identity_index_pgvector.py`

```
[PGVECTOR] [SEARCH_KNOWN] ========================================
[PGVECTOR] [SEARCH_KNOWN] 🔍 Starting KNOWN Identity Search (pgvector)
[PGVECTOR] [SEARCH_KNOWN] Using: SCRFD (detection) → ArcFace (embedding) → pgvector (search)
[PGVECTOR] [SEARCH_KNOWN] Parameters: top_k=5, threshold=0.4
[PGVECTOR] [SEARCH_KNOWN] Embedding shape: (512,), norm: {norm}
[PGVECTOR] [SEARCH_KNOWN] Searching PostgreSQL with pgvector extension...
[PGVECTOR] [SEARCH_KNOWN] ========================================
[PGVECTOR] [SEARCH_KNOWN] ✅ Embedding normalized (norm: 1.0000)
[PGVECTOR] [SEARCH_KNOWN] Normalized embedding (first 5 values): [...]
[PGVECTOR] [SEARCH_KNOWN] Executing PostgreSQL query with pgvector...
[PGVECTOR] [SEARCH_KNOWN] ✅ Query executed successfully
[PGVECTOR] [SEARCH_KNOWN] Query returned {N} rows
[PGVECTOR] [SEARCH_KNOWN] ✅ Match #1: identity={id}... name='{name}' sim={sim} quality={quality}
```

**If match found:**
```
[PGVECTOR] [SEARCH_KNOWN] ========================================
[PGVECTOR] [SEARCH_KNOWN] ✅ SUCCESS: Found {N} KNOWN matches (best similarity: {sim}) in {ms}ms
[PGVECTOR] [SEARCH_KNOWN] This face will be recognized as KNOWN and shown on dashboard
[PGVECTOR] [SEARCH_KNOWN] ========================================
```

**If no match:**
```
[PGVECTOR] [SEARCH_KNOWN] ========================================
[PGVECTOR] [SEARCH_KNOWN] ❌ No matches found above threshold 0.4 in {ms}ms
[PGVECTOR] [SEARCH_KNOWN] This means the face will be marked as UNKNOWN
[PGVECTOR] [SEARCH_KNOWN] UNKNOWN faces are filtered out from dashboard by default
[PGVECTOR] [SEARCH_KNOWN] ========================================
```

### Step 5: Identity Recognition Result
**Location**: `backend/services/image_processing.py`

```
[PROCESS] [STEP-BY-STEP] ========================================
[PROCESS] [STEP-BY-STEP] ✅ Identity Recognition Complete
[PROCESS] [STEP-BY-STEP] Identity ID: {uuid}
[PROCESS] [STEP-BY-STEP] Identity Type: {KNOWN|UNKNOWN}
[PROCESS] [STEP-BY-STEP] Display Name: {name}
[PROCESS] [STEP-BY-STEP] Is New Identity: {true|false}
[PROCESS] [STEP-BY-STEP] Similarity Score: {similarity}
[PROCESS] [STEP-BY-STEP] ========================================
```

**If KNOWN:**
```
[PROCESS] [STEP-BY-STEP] ✅ Face will be shown on DASHBOARD (KNOWN identity: {name})
```

**If UNKNOWN:**
```
[PROCESS] [STEP-BY-STEP] ⚠️ Face will NOT be shown on DASHBOARD (UNKNOWN identity)
[PROCESS] [STEP-BY-STEP] ⚠️ UNKNOWN faces are filtered out by default (SHOW_UNKNOWN_FACES_ON_DASHBOARD=False)
```

### Step 6: WebSocket Data Filtering & Sending
**Location**: `backend/routes/websocket.py`

```
[WS] [STEP-BY-STEP] Filtering faces for detection {id}: show_unknown_faces={false|true}, total_faces={N}
[WS] [STEP-BY-STEP] ✅ Including KNOWN face '{name}' (identity.type=KNOWN) - will appear on dashboard
[WS] [STEP-BY-STEP] Filtering out UNKNOWN face '{name}' (label_state=AUTO_UNKNOWN)
```

**When sending to dashboard:**
```
[WS] [STEP-BY-STEP] ========================================
[WS] [STEP-BY-STEP] 📤 Preparing to send data to dashboard
[WS] [STEP-BY-STEP] Pipeline entries: {N}
[WS] [STEP-BY-STEP] Total faces: {N}
[WS] [STEP-BY-STEP]   Pipeline #1: {pipeline_id} - {N} faces
[WS] [STEP-BY-STEP]     Face #1: {name} (similarity: {sim})
[WS] [STEP-BY-STEP] ========================================
[WS] 📤 Sending initial_data: {N} pipeline entries, {N} total faces
[WS] [STEP-BY-STEP] ✅ Successfully sent initial_data to dashboard
[WS] [STEP-BY-STEP] Dashboard will now display {N} faces
```

## How to Use Logs for Debugging

### 1. Check if Face Detection is Working
```bash
docker logs face_recognition_api | grep "\[PROCESS\] \[STEP-BY-STEP\] 🔍 Starting Identity Recognition"
```

### 2. Check if pgvector Search is Executing
```bash
docker logs face_recognition_api | grep "\[PGVECTOR\] \[SEARCH_KNOWN\] 🔍 Starting KNOWN Identity Search"
```

### 3. Check Search Results
```bash
docker logs face_recognition_api | grep "\[PGVECTOR\] \[SEARCH_KNOWN\] ✅ SUCCESS\|❌ No matches found"
```

### 4. Check Identity Recognition Result
```bash
docker logs face_recognition_api | grep "\[PROCESS\] \[STEP-BY-STEP\] ✅ Identity Recognition Complete"
```

### 5. Check Dashboard Filtering
```bash
docker logs face_recognition_api | grep "\[WS\] \[STEP-BY-STEP\]"
```

### 6. Check WebSocket Data Sending
```bash
docker logs face_recognition_api | grep "\[WS\] 📤 Sending initial_data"
```

## Common Issues & Log Patterns

### Issue 1: All Faces Are UNKNOWN
**Log Pattern:**
```
[PGVECTOR] [SEARCH_KNOWN] ❌ No matches found above threshold 0.4
[PROCESS] [STEP-BY-STEP] ⚠️ Face will NOT be shown on DASHBOARD (UNKNOWN identity)
```

**Possible Causes:**
- pgvector search failing (check for SQL errors)
- Similarity threshold too high (0.4 = 40%)
- Known faces not loaded properly
- Embedding quality too low

### Issue 2: Known Faces Not Appearing on Dashboard
**Log Pattern:**
```
[PGVECTOR] [SEARCH_KNOWN] ✅ SUCCESS: Found 1 KNOWN matches
[PROCESS] [STEP-BY-STEP] ✅ Face will be shown on DASHBOARD (KNOWN identity)
[WS] [STEP-BY-STEP] ✅ Including KNOWN face '{name}' (identity.type=KNOWN)
[WS] 📤 Sending initial_data: 1 pipeline entries, 1 total faces
```

**If you see this but dashboard is empty:**
- Check frontend WebSocket connection
- Check browser console for errors
- Verify WebSocket message format

### Issue 3: pgvector Search Not Executing
**Log Pattern:**
```
[PROCESS] [STEP-BY-STEP] 🔍 Starting Identity Recognition
(No [PGVECTOR] [SEARCH_KNOWN] logs)
```

**Possible Causes:**
- Identity service not initialized
- pgvector backend not enabled
- Database connection issue

## Full Flow Example

### Successful KNOWN Recognition:
```
[PROCESS] [STEP-BY-STEP] 🔍 Step 1: Face Detection (SCRFD Model)
[PROCESS] [STEP-BY-STEP] ✅ SCRFD: Face detected successfully
[PROCESS] [STEP-BY-STEP] 🔍 Step 2: Embedding Generation (ArcFace Model)
[PROCESS] [STEP-BY-STEP] ✅ ArcFace: Embedding generated successfully
[PROCESS] [STEP-BY-STEP] 🔍 Step 3: Identity Recognition (pgvector Search)
[PROCESS] [STEP-BY-STEP] Pipeline: camera_01
[PROCESS] [STEP-BY-STEP] Embedding shape: (512,), norm: 1.0000
[PROCESS] [STEP-BY-STEP] Quality score: 0.85

[PGVECTOR] [SEARCH_KNOWN] 🔍 Starting KNOWN Identity Search
[PGVECTOR] [SEARCH_KNOWN] Parameters: top_k=5, threshold=0.4
[PGVECTOR] [SEARCH_KNOWN] ✅ Embedding normalized
[PGVECTOR] [SEARCH_KNOWN] Executing PostgreSQL query with pgvector...
[PGVECTOR] [SEARCH_KNOWN] ✅ Query executed successfully
[PGVECTOR] [SEARCH_KNOWN] Query returned 1 rows
[PGVECTOR] [SEARCH_KNOWN] ✅ Match #1: identity=abc12345... name='trump' sim=0.7234
[PGVECTOR] [SEARCH_KNOWN] ✅ SUCCESS: Found 1 KNOWN matches (best similarity: 0.7234) in 15.23ms
[PGVECTOR] [SEARCH_KNOWN] This face will be recognized as KNOWN and shown on dashboard

[PROCESS] [STEP-BY-STEP] ✅ Identity Recognition Complete
[PROCESS] [STEP-BY-STEP] Identity Type: KNOWN
[PROCESS] [STEP-BY-STEP] Display Name: trump
[PROCESS] [STEP-BY-STEP] Similarity Score: 0.7234
[PROCESS] [STEP-BY-STEP] ✅ Face will be shown on DASHBOARD (KNOWN identity: trump)

[WS] [STEP-BY-STEP] ✅ Including KNOWN face 'trump' (identity.type=KNOWN) - will appear on dashboard
[WS] [STEP-BY-STEP] 📤 Preparing to send data to dashboard
[WS] [STEP-BY-STEP] Pipeline entries: 1
[WS] [STEP-BY-STEP] Total faces: 1
[WS] 📤 Sending initial_data: 1 pipeline entries, 1 total faces
[WS] [STEP-BY-STEP] ✅ Successfully sent initial_data to dashboard
```

### UNKNOWN Recognition (No Match):
```
[PROCESS] [STEP-BY-STEP] 🔍 Starting Identity Recognition
[PGVECTOR] [SEARCH_KNOWN] 🔍 Starting KNOWN Identity Search
[PGVECTOR] [SEARCH_KNOWN] Executing PostgreSQL query with pgvector...
[PGVECTOR] [SEARCH_KNOWN] Query returned 0 rows
[PGVECTOR] [SEARCH_KNOWN] ❌ No matches found above threshold 0.4
[PGVECTOR] [SEARCH_KNOWN] This means the face will be marked as UNKNOWN
[PGVECTOR] [SEARCH_KNOWN] UNKNOWN faces are filtered out from dashboard by default

[PROCESS] [STEP-BY-STEP] Identity Type: UNKNOWN
[PROCESS] [STEP-BY-STEP] ⚠️ Face will NOT be shown on DASHBOARD (UNKNOWN identity)

[WS] [STEP-BY-STEP] Filtering out UNKNOWN face 'Unknown' (label_state=AUTO_UNKNOWN)
[WS] 📤 Sending initial_data: 0 pipeline entries, 0 total faces
```

## Summary

The logging now provides complete visibility into:
1. ✅ Face detection and embedding generation
2. ✅ pgvector search execution and results
3. ✅ Identity recognition decision (KNOWN vs UNKNOWN)
4. ✅ Dashboard filtering logic
5. ✅ WebSocket data preparation and sending

Use these logs to diagnose why known faces aren't appearing on the dashboard!

