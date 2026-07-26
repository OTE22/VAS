# ML Integration & Architecture
## How Machine Learning Works in the Face Detection System

---

## 📋 Overview

The system uses **two main ML models** for face detection and recognition, plus an **optional ML similarity model** for improving merge suggestions. All models run on **ONNX Runtime** with GPU/CPU support.

---

## 🧠 Core ML Models

### 1. **SCRFD - Face Detection Model**

**Purpose**: Detects faces in images and extracts facial landmarks (keypoints)

**Model Details**:
- **Architecture**: SCRFD (Sample and Computation Redistribution for Efficient Face Detection)
- **Paper**: https://arxiv.org/abs/2105.04714
- **Input Size**: 640x640 pixels
- **Output**: 
  - Bounding boxes (x1, y1, x2, y2, confidence)
  - Facial landmarks/keypoints (5 points: eyes, nose, mouth corners)
- **Format**: ONNX (Open Neural Network Exchange)
- **Execution**: ONNX Runtime with CUDA/CPU providers

**How It Works**:
```python
# 1. Load model on startup
detector = SCRFD(
    model_path="models/scrfd_500m_bnkps.onnx",
    input_size=(640, 640),
    conf_thres=0.5  # Confidence threshold
)

# 2. Detect faces in image
bboxes, kpss = detector.detect(image, max_num=1)
# bboxes: [[x1, y1, x2, y2, confidence], ...]
# kpss: [[[x1, y1], [x2, y2], ...], ...]  # 5 keypoints per face
```

**Integration Points**:
- `backend/core/model_manager.py` - Initializes SCRFD on startup
- `backend/services/image_processing.py` - Uses detector for each person crop
- `models/scrfd.py` - Model implementation

---

### 2. **ArcFace - Face Recognition Model**

**Purpose**: Generates 512-dimensional face embeddings for identity matching

**Model Details**:
- **Architecture**: ArcFace (Additive Angular Margin Loss)
- **Input Size**: 112x112 pixels (aligned face)
- **Output**: 512-dimensional normalized embedding vector
- **Format**: ONNX
- **Execution**: ONNX Runtime with CUDA/CPU providers

**How It Works**:
```python
# 1. Load model on startup
recognizer = ArcFace(model_path="models/arcface_w600k_r50.onnx")

# 2. Align face using landmarks from SCRFD
aligned_face = face_alignment(image, landmarks, image_size=112)

# 3. Generate embedding
embedding = recognizer.get_embedding(aligned_face, landmarks)
# embedding: [512-dim vector], normalized (L2 norm = 1.0)
```

**Integration Points**:
- `backend/core/model_manager.py` - Initializes ArcFace on startup
- `backend/services/image_processing.py` - Generates embeddings for each detected face
- `models/arcface.py` - Model implementation
- `utils/helpers.py` - Face alignment function

---

## 🔄 ML Pipeline Flow

### **Step-by-Step Processing**:

```
1. Image Received
   ↓
2. Person Detection (YOLO/External)
   ↓
3. Crop Person Region
   ↓
4. SCRFD Face Detection
   ├─→ Detect face in crop
   ├─→ Extract bounding box
   └─→ Extract 5 facial landmarks
   ↓
5. Face Alignment
   ├─→ Use landmarks to align face
   └─→ Resize to 112x112
   ↓
6. ArcFace Embedding Generation
   ├─→ Generate 512-dim embedding
   └─→ Normalize (L2 norm)
   ↓
7. Identity Matching
   ├─→ Search FAISS/pgvector index
   ├─→ Calculate cosine similarity
   └─→ Match if similarity > threshold
   ↓
8. Store Results
   ├─→ Save to database
   ├─→ Update identity indexes
   └─→ Broadcast via WebSocket
```

---

## 🚀 Model Initialization

### **Startup Sequence** (`backend/lifespan.py`):

```python
# 1. Detect GPU availability
has_gpu, gpu_type = detect_gpu()

# 2. Initialize models in thread pool (CPU/GPU intensive)
await run_in_threadpool(model_manager.initialize)

# 3. ModelManager.initialize() does:
#    - Load SCRFD detector
#    - Load ArcFace recognizer
#    - Build face database from assets/faces/
#    - Load FAISS/pgvector indexes
```

### **GPU/CPU Support**:

- **GPU**: Uses `CUDAExecutionProvider` if CUDA available
- **CPU**: Falls back to `CPUExecutionProvider` automatically
- **Auto-detection**: System detects GPU on startup and logs status

---

## 📊 Face Database (Known Faces)

### **Purpose**: Store embeddings of known people for recognition

**Location**: `assets/faces/` directory

**How It Works**:
```python
# 1. On startup, scan assets/faces/ directory
# 2. For each image:
#    - Detect face (SCRFD)
#    - Generate embedding (ArcFace)
#    - Add to FAISS index
# 3. Save index to disk

# Example structure:
assets/faces/
  ├─ john_doe.jpg      → Added to index as "john_doe"
  ├─ jane_smith.jpg    → Added to index as "jane_smith"
  └─ ...
```

**Storage**:
- **FAISS Backend**: In-memory FAISS index + metadata file
- **pgvector Backend**: PostgreSQL `identity_embeddings` table with vector column

---

## 🎯 Identity Recognition Flow

### **When Processing a Detection**:

```python
# 1. Generate embedding from detected face
embedding = recognizer.get_embedding(aligned_face, landmarks)

# 2. Search in KNOWN identities index
known_results = identity_index.search_known(embedding, top_k=1, threshold=0.4)
# Returns: [(identity_id, similarity_score), ...]

# 3. If match found (similarity > threshold):
if known_results and known_results[0][1] > 0.4:
    identity_id = known_results[0][0]
    similarity = known_results[0][1]
    name = identity.display_name
    type = IdentityType.KNOWN
else:
    # 4. Search in UNKNOWN identities index
    unknown_results = identity_index.search_unknown(embedding, top_k=1, threshold=0.35)
    
    if unknown_results and unknown_results[0][1] > 0.35:
        # Match existing unknown identity
        identity_id = unknown_results[0][0]
        similarity = unknown_results[0][1]
        type = IdentityType.UNKNOWN
    else:
        # Create new unknown identity
        identity = create_new_unknown_identity()
        type = IdentityType.UNKNOWN
```

---

## 🤖 ML Similarity Model (Optional)

### **Purpose**: Improve merge suggestion accuracy using user feedback

**Architecture**:
- **Type**: Multi-Layer Perceptron (MLP) Regressor
- **Input Features**: 6 features
  - `embedding_sim`: Cosine similarity between embeddings
  - `pipeline_overlap`: Number of shared pipelines
  - `quality1`, `quality2`: Quality scores of both identities
  - `appearances_diff`: Difference in appearance counts
  - `is_cross_pipeline`: Boolean (1 if different pipelines)
- **Hidden Layers**: 2 layers (64 → 32 neurons)
- **Output**: Confidence score (0.0-1.0)
- **Activation**: ReLU (hidden), Sigmoid (output)

**Training**:
- **Data Source**: User feedback from merge suggestions
  - Approved merges → Positive samples
  - Rejected merges → Negative samples
- **Minimum Samples**: 50 (configurable)
- **Auto-training**: Can be enabled to train automatically

**Integration**:
- `backend/core/similarity_model.py` - Model implementation
- `backend/core/identity_clustering.py` - Uses model for merge suggestions
- `backend/routes/identities.py` - Training endpoint

---

## ⚙️ Configuration

### **Model Paths** (`config.py`):

```python
DETECTION_MODEL: str = "models/scrfd_500m_bnkps.onnx"
RECOGNITION_MODEL: str = "models/arcface_w600k_r50.onnx"
CONFIDENCE_THRESHOLD: float = 0.5  # SCRFD confidence
SIMILARITY_THRESHOLD: float = 0.4  # ArcFace matching threshold
```

### **GPU/CPU Settings**:

- **Auto-detection**: System detects GPU automatically
- **ONNX Providers**: `["CUDAExecutionProvider", "CPUExecutionProvider"]`
- **Fallback**: If GPU fails, automatically uses CPU

---

## 🔍 Vector Search Backends

### **FAISS** (Default):
- **Index Types**: 
  - `IndexFlatIP`: Exact search (small datasets)
  - `IndexIVFFlat`: Approximate search (1M+ vectors)
  - `IndexHNSWFlat`: Fastest search
  - `IndexIVFPQ`: Memory-efficient
- **GPU Support**: Yes (if available)
- **Storage**: In-memory + disk files

### **pgvector** (Alternative):
- **Index Types**:
  - `HNSW`: Hierarchical Navigable Small World (fastest)
  - `IVFFlat`: Inverted File Index (memory-efficient)
- **Storage**: PostgreSQL database (unified storage)
- **Benefits**: Transactional safety, simpler architecture

---

## 📈 Performance Optimizations

### **1. Model Caching**:
- Models loaded once on startup
- Shared across all requests (singleton pattern)
- GPU memory allocated once

### **2. Batch Processing**:
- FAISS supports batch similarity search
- Multiple embeddings processed in parallel

### **3. Worker Threads**:
- **GPU**: 16 workers (can handle more parallel ops)
- **CPU**: 8 workers (optimized for CPU cores)

### **4. Face Database Caching**:
- FAISS index loaded in memory
- Fast similarity search (microseconds)

---

## 🛠️ Model Management

### **Health Checks**:
```python
# Check if models are loaded
model_manager.health_check()  # Returns: True/False

# Verify components
- detector is not None
- recognizer is not None
- face_db is initialized
```

### **Adding New Known Faces**:
```python
# Incremental addition (best practice)
model_manager.add_face_from_image(
    image_path="assets/faces/new_person.jpg",
    person_name="new_person"
)
# Automatically:
# 1. Detects face
# 2. Generates embedding
# 3. Adds to FAISS index
# 4. Saves to disk
```

---

## 🔧 Troubleshooting

### **Model Loading Issues**:
- **Check model files exist**: `models/scrfd_*.onnx`, `models/arcface_*.onnx`
- **Check ONNX Runtime**: `pip install onnxruntime-gpu` (GPU) or `onnxruntime` (CPU)
- **Check GPU drivers**: If using GPU, ensure CUDA is installed

### **Low Recognition Accuracy**:
- **Adjust thresholds**: Lower `SIMILARITY_THRESHOLD` for more matches
- **Improve face quality**: Ensure faces are clear, front-facing
- **Add more training data**: More known faces = better recognition

### **Performance Issues**:
- **Use GPU**: Significantly faster (10-50x speedup)
- **Optimize FAISS index**: Use `IndexHNSWFlat` for large datasets
- **Reduce image size**: Smaller images = faster processing

---

## 📚 Key Files

- `backend/core/model_manager.py` - Model initialization & management
- `models/scrfd.py` - SCRFD face detection model
- `models/arcface.py` - ArcFace face recognition model
- `backend/services/image_processing.py` - Main processing pipeline
- `backend/core/identity_service.py` - Identity matching logic
- `backend/core/similarity_model.py` - ML similarity model
- `utils/helpers.py` - Face alignment utilities

---

## 🎓 Summary

**The ML system works in 3 stages**:

1. **Detection** (SCRFD): Finds faces in images → bounding boxes + landmarks
2. **Recognition** (ArcFace): Converts faces → 512-dim embeddings
3. **Matching** (FAISS/pgvector): Compares embeddings → finds similar identities

**All models run on ONNX Runtime** with automatic GPU/CPU detection and fallback.

