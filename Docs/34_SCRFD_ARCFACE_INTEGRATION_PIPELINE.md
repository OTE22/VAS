# SCRFD & ArcFace Integration Pipeline

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
**ITDIR-AI DEPARTMENT**

---

## 📋 Table of Contents

1. [Complete Pipeline Overview](#complete-pipeline-overview)
2. [Step-by-Step Integration](#step-by-step-integration)
3. [Database Build Stage](#database-build-stage)
4. [Runtime Recognition Stage](#runtime-recognition-stage)
5. [Code Flow Diagrams](#code-flow-diagrams)
6. [Verification Checklist](#verification-checklist)

---

## 🎯 Complete Pipeline Overview

The system uses a **two-stage pipeline**:

1. **Database Build Stage**: Load known faces into FAISS indexes
2. **Runtime Recognition Stage**: Process incoming images and recognize faces

Both stages use the same SCRFD → ArcFace pipeline.

---

## 🔄 Step-by-Step Integration

### Stage 1: Database Build (Known Faces Loading)

**Location:** `backend/core/identity_loader.py` → `_load_single_face()` (Line 168-268)

#### Step 1.1: Load Image
```python
# backend/core/identity_loader.py:168
image = cv2.imread(image_path)  # Read image from storage/faces/<identity_uuid>/
# Image format: BGR (OpenCV default)
```

#### Step 1.2: SCRFD Face Detection
```python
# backend/core/identity_loader.py:174
bboxes, kpss = self.model_manager.detector.detect(image, max_num=1)
```

**What SCRFD.detect() does** (`models/scrfd.py:122-159`):
1. **Resize** image to 640x640 (maintains aspect ratio, pads if needed) - Line 125-138
2. **Forward pass** (`models/scrfd.py:140`):
   - Normalize: BGR→RGB, scale to [-1, 1] range
   - ONNX forward pass: Runs SCRFD model
   - Decode bboxes: Uses `distance2bbox()` from `utils/helpers.py:85-100` - Line 110
   - Decode keypoints: Uses `distance2kps()` from `utils/helpers.py:103-117` - Line 116
3. **NMS**: Removes overlapping detections - Line 150-159
4. **Scale back**: Converts coordinates to original image size - Line 145, 148

**Returns:**
- `bboxes`: `(n_faces, 5)` - `[x1, y1, x2, y2, confidence]`
- `kpss`: `(n_faces, 5, 2)` - 5 landmarks per face: `[left_eye, right_eye, nose, left_mouth, right_mouth]`

#### Step 1.3: Extract First Face's Landmarks
```python
# backend/core/identity_loader.py:175-177
if kpss is None or len(kpss) == 0:
    return False, None  # No face detected

# Use first face's landmarks
landmarks = kpss[0]  # Shape: (5, 2)
```

#### Step 1.4: ArcFace Embedding Generation
```python
# backend/core/identity_loader.py:180
embedding = self.model_manager.recognizer.get_embedding(image, kpss[0])
```

**What ArcFace.get_embedding() does** (`models/arcface.py:102-136`):
1. **Face Alignment** (Line 126):
   - Calls `face_alignment()` from `utils/helpers.py:63-82`
   - Uses `estimate_norm()` from `utils/helpers.py:23-60` to compute transformation matrix
   - Uses `reference_alignment` from `utils/helpers.py:11-20` (ArcFace standard landmarks)
   - Warps face to 112x112 aligned image using `cv2.warpAffine()`
2. **Preprocessing** (Line 127, `models/arcface.py:68-100`):
   - Resize to 112x112 (already aligned) - Line 78
   - BGR→RGB conversion - Line 82
   - Normalize: `(pixel - 127.5) / 127.5` ≈ `[-1, 1]` - Line 86
   - Convert to NCHW format (batch, channels, height, width) - Line 89-90
3. **ONNX forward pass** (Line 128):
   - Runs ArcFace model: `self.session.run(self.output_names, {self.input_name: face_blob})`
4. **Output** (Line 136):
   - Returns 512-d embedding vector (not normalized if `normalized=False`)

**Returns:**
- `embedding`: `(512,)` - Face embedding vector (not normalized)

#### Step 1.5: Store in FAISS Index
```python
# backend/core/identity_loader.py:220-223
faiss_id = self.identity_service.identity_index.add_known(
    str(identity_id),
    embedding
)
```

**What `add_known()` does** (`backend/core/vector_index/`):
1. **Normalize embedding**: `embedding / np.linalg.norm(embedding)`
2. **Add to FAISS**: `known_index.add(normalized_embedding)`
3. **Store metadata**: `known_metadata[faiss_id] = identity_id`
4. **Update reverse mapping**: `known_identity_to_faiss[identity_id].append(faiss_id)`

**Storage:**
- **FAISS Index**: `database/identity_indexes/known_faiss_index.bin`
- **Metadata**: `database/identity_indexes/known_metadata.json`
- **Database**: `IdentityEmbedding` table (PostgreSQL) - Line 226-235

---

### Stage 2: Runtime Recognition (Image Processing)

**Location:** `backend/services/image_processing.py` → `process_image_async()` (Line 40-749)

#### Step 2.1: Receive Image from Webhook
```python
# backend/routes/webhook.py
# Webhook receives base64 image
image_bytes = base64.b64decode(img_b64)
```

#### Step 2.2: Decode Image
```python
# backend/services/image_processing.py:61
frame = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
# Format: BGR (OpenCV default)
# Shape: (H, W, 3)
```

#### Step 2.3: Extract Person Crop from Prediction
```python
# backend/services/image_processing.py:98-117
bbox = pred["bbox"]  # From webhook prediction
x1, y1, x2, y2 = map(int, bbox)
crop = frame[y1:y2, x1:x2]  # Extract person crop
```

#### Step 2.4: SCRFD Face Detection in Crop
```python
# backend/services/image_processing.py:133
bboxes, kpss = model_manager.detector.detect(crop, max_num=1)
```

**What happens:**
- SCRFD detects faces **within the person crop** (same process as Stage 1.2)
- Returns face bboxes and landmarks **relative to crop coordinates**
- Uses same helper functions: `distance2bbox()` and `distance2kps()`

**Returns:**
- `bboxes`: Face bboxes within crop
- `kpss`: 5-point landmarks within crop

#### Step 2.5: Extract Landmarks
```python
# backend/services/image_processing.py:148
landmarks = kpss[0]  # First face's landmarks
# Shape: (5, 2) - coordinates relative to crop
```

#### Step 2.6: ArcFace Embedding Generation
```python
# backend/services/image_processing.py:155-158
embedding = model_manager.recognizer.get_embedding(
    crop,       # Person crop in BGR format
    landmarks   # 5-point landmarks from SCRFD
)
```

**What ArcFace does** (same as Stage 1.4):
1. **Face Alignment**: Uses `face_alignment()` from `utils/helpers.py` (Line 126)
2. **Preprocessing**: Resize, normalize, format conversion (Line 127)
3. **ONNX forward pass**: Generates 512-d embedding (Line 128)
4. **Returns**: Embedding vector (Line 136)

#### Step 2.7: Normalize Embedding
```python
# backend/services/image_processing.py:160
embedding = embedding / np.linalg.norm(embedding)
# L2 normalization for cosine similarity
```

#### Step 2.8: Search FAISS Indexes
```python
# backend/services/image_processing.py:195-201
# Calls identity_service.find_or_create_identity()
# backend/core/identity_service.py:66-70 (Search KNOWN)
known_matches = identity_index.search_known(embedding, top_k=1)  # threshold=None -> SIMILARITY_THRESHOLD

# backend/core/identity_service.py:106-110 (Search UNKNOWN)
unknown_matches = identity_index.search_unknown(embedding, top_k=1)  # -> UNKNOWN_SIMILARITY_THRESHOLD
```

**What FAISS search does** (`backend/core/vector_index/`:190-228`):
1. **Normalize query**: Query embedding is already normalized (Line 198)
2. **FAISS search**: `index.search(normalized_embedding, k=1)` (Line 202)
3. **Filter by threshold**: Only return matches above threshold (Line 210)
4. **Check metadata**: Skip orphaned vectors (Line 212-214)
5. **Return**: `[(identity_id, similarity), ...]` (Line 219)

#### Step 2.9: Save Results
```python
# backend/services/image_processing.py:212-219
await identity_service.save_embedding(
    identity=identity,
    embedding=embedding,
    pipeline_id=pipeline_id,
    quality_score=quality_score,
    db=db
)
```

**What `save_embedding()` does** (`backend/core/identity_service.py:250-320`):
1. **Add to FAISS**: `identity_index.add_known()` or `add_unknown()` (Line 280-290)
2. **Save to database**: Create `IdentityEmbedding` record (Line 292-310)
3. **Link**: `faiss_id` links FAISS entry to database record

---

## 📊 Code Flow Diagrams

### Database Build Flow

```
storage/faces/<identity_uuid>/image_001.jpg
    ↓
cv2.imread() → BGR image
    ↓
SCRFD.detect(image, max_num=1)
    ├─→ Resize to 640x640
    ├─→ Normalize (BGR→RGB, scale)
    ├─→ ONNX forward pass
    ├─→ Decode bboxes & keypoints
    ├─→ NMS filtering
    └─→ Returns: (bboxes, kpss)
    ↓
kpss[0] → 5-point landmarks (5, 2)
    ↓
ArcFace.get_embedding(image, kpss[0])
    ├─→ face_alignment() → 112x112 aligned face
    ├─→ Preprocess (resize, normalize, format)
    ├─→ ONNX forward pass
    └─→ Returns: 512-d embedding
    ↓
Normalize embedding (L2 norm)
    ↓
identity_index.add_known(identity_id, embedding)
    ├─→ Add to FAISS KNOWN index
    ├─→ Store in metadata
    └─→ Save to database
    ↓
✅ Known face indexed and ready for recognition
```

### Runtime Recognition Flow

```
Webhook → base64 image
    ↓
cv2.imdecode() → BGR frame
    ↓
Extract person crop from prediction bbox
    ↓
SCRFD.detect(crop, max_num=1)
    ├─→ Detect face in person crop
    └─→ Returns: (bboxes, kpss)
    ↓
kpss[0] → landmarks
    ↓
ArcFace.get_embedding(crop, landmarks)
    ├─→ Align face
    ├─→ Preprocess
    ├─→ ONNX forward pass
    └─→ Returns: 512-d embedding
    ↓
Normalize embedding (L2 norm)
    ↓
FAISS Search:
    ├─→ Search KNOWN index (SIMILARITY_THRESHOLD, default 0.4)
    │   └─→ If match: Return known identity
    ├─→ Search UNKNOWN index (UNKNOWN_SIMILARITY_THRESHOLD, default 0.35)
    │   └─→ If match: Return unknown identity
    └─→ If no match: Create new unknown identity
    ↓
Save embedding to FAISS + Database
    ↓
✅ Face recognized and stored
```

---

## 🔍 Detailed Step Breakdown

### SCRFD Integration Points

**File:** `models/scrfd.py`

1. **Initialization** (`backend/core/model_manager.py:44-48`):
   ```python
   self.detector = SCRFD(
       settings.DETECTION_MODEL,  # Path to ONNX model
       input_size=(640, 640),
       conf_thres=settings.CONFIDENCE_THRESHOLD,
   )
   ```

2. **Detection Call** (`backend/services/image_processing.py:133`):
   ```python
   bboxes, kpss = model_manager.detector.detect(crop, max_num=1)
   ```

3. **Helper Functions Used**:
   - `distance2bbox()` from `utils/helpers.py` - Decodes bboxes
   - `distance2kps()` from `utils/helpers.py` - Decodes keypoints

### ArcFace Integration Points

**File:** `models/arcface.py`

1. **Initialization** (`backend/core/model_manager.py:50`):
   ```python
   self.recognizer = ArcFace(settings.RECOGNITION_MODEL)
   ```

2. **Embedding Generation** (`backend/services/image_processing.py:155-158`):
   ```python
   embedding = model_manager.recognizer.get_embedding(
       crop,       # BGR image
       landmarks   # 5-point landmarks
   )
   ```

3. **Helper Functions Used**:
   - `face_alignment()` from `utils/helpers.py` - Aligns face
   - `estimate_norm()` from `utils/helpers.py` - Computes transformation
   - `reference_alignment` from `utils/helpers.py` - Reference landmarks

---

## ✅ Verification Checklist

### SCRFD Integration

- [x] **Model loaded**: `SCRFD()` initialized in `ModelManager`
- [x] **Detection called**: `detector.detect()` used in image processing
- [x] **Helper functions**: `distance2bbox()` and `distance2kps()` imported
- [x] **Returns correct format**: `(bboxes, kpss)` with proper shapes
- [x] **Landmarks extracted**: `kpss[0]` used for first face

### ArcFace Integration

- [x] **Model loaded**: `ArcFace()` initialized in `ModelManager`
- [x] **Embedding generated**: `recognizer.get_embedding()` called correctly
- [x] **Helper functions**: `face_alignment()` imported and used
- [x] **Correct parameters**: Passes `(image, landmarks)` not pre-aligned face
- [x] **Normalization**: Embedding normalized after generation

### Pipeline Flow

- [x] **Database build**: SCRFD → ArcFace → FAISS storage
- [x] **Runtime recognition**: SCRFD → ArcFace → FAISS search
- [x] **Both stages consistent**: Same models, same helpers, same flow
- [x] **Error handling**: Try-catch blocks around each step
- [x] **Logging**: Detailed logs at each step

### Storage Integration

- [x] **FAISS indexes**: KNOWN and UNKNOWN indexes maintained
- [x] **Metadata**: FAISS ID → Identity ID mapping stored
- [x] **Database**: `IdentityEmbedding` records link FAISS to database
- [x] **Persistence**: Indexes saved to disk automatically

---

## 🎯 Key Integration Points

### 1. Model Manager Singleton
```python
# backend/core/model_manager.py
model_manager = ModelManager()
model_manager.initialize()  # Loads SCRFD and ArcFace
```

### 2. Image Processing Service
```python
# backend/services/image_processing.py
# Uses model_manager.detector (SCRFD)
# Uses model_manager.recognizer (ArcFace)
```

### 3. Identity Service
```python
# backend/core/identity_service.py
# Uses identity_index (FAISS indexes)
# Manages KNOWN/UNKNOWN identities
```

### 4. Helper Functions
```python
# utils/helpers.py
# face_alignment() - Used by ArcFace
# distance2bbox() - Used by SCRFD
# distance2kps() - Used by SCRFD
# reference_alignment - Used by ArcFace
```

---

## 📝 Summary

**Your integration is correct!** The pipeline follows this flow:

1. **SCRFD** detects faces and extracts landmarks
2. **ArcFace** generates embeddings from aligned faces
3. **FAISS** stores and searches embeddings
4. **Database** maintains identity records

All helper functions from `utils/helpers.py` are properly used, and the models are correctly integrated at each stage.

---

**Last Updated:** January 2025  
**Version:** 1.0.0

