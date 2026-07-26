# How ML Helps with Clustering & Merge Suggestions

## 📋 Overview

The system has **two approaches** for generating merge suggestions:

1. **Heuristic-Based** (Current Default): Uses fixed formulas based on face similarity + pattern matching
2. **ML-Based** (Available but Optional): Uses a trained neural network that learns from user feedback

---

## 🎯 Current Clustering Process (Heuristic)

### **How It Works Now**:

```
1. Find Candidate Pairs
   ├─ Pattern-based filtering (same cameras, time overlap)
   └─ Cross-camera detection
   
2. Verify Face Similarity
   ├─ Calculate cosine similarity between embeddings (FAISS/pgvector)
   └─ Filter by threshold (0.35 same-camera, 0.50 cross-camera)
   
3. Calculate Confidence Score (HEURISTIC)
   ├─ Same-camera: (face_similarity × 0.6) + (pattern × 0.4)
   └─ Cross-camera: (face_similarity × 0.8) + 0.06
   
4. Create Merge Suggestion
   └─ confidence = calculated score
```

### **Current Formula** (in `identity_clustering.py`):

```python
# Same-camera merge
pattern_confidence = min(0.75, overlap_ratio + (0.1 if time_overlap else 0))
combined_confidence = (face_similarity * 0.6) + (pattern_confidence * 0.4)
combined_confidence = min(0.95, combined_confidence)  # Cap at 0.95

# Cross-camera merge
combined_confidence = (face_similarity * 0.8) + 0.06  # Max 0.86
```

**Limitations**:
- Fixed weights (60/40, 80/20) - not adaptive
- Doesn't learn from user feedback
- May over/under-estimate confidence for certain cases

---

## 🤖 ML Similarity Model (Available but Not Integrated Yet)

### **What It Does**:

The ML model learns from **user feedback** (approved/rejected merge suggestions) to predict merge confidence more accurately.

### **Model Architecture**:

```
Input Features (6):
├─ embedding_similarity: Cosine similarity between face embeddings (0.0-1.0)
├─ pipeline_overlap: Ratio of common pipelines (0.0-1.0)
├─ quality_score_1: Average quality of identity 1 (0.0-1.0)
├─ quality_score_2: Average quality of identity 2 (0.0-1.0)
├─ appearances_diff: Difference in appearance counts (normalized)
└─ is_cross_pipeline: Boolean (1.0 if different pipelines, 0.0 if same)

Neural Network:
├─ Input Layer: 6 neurons
├─ Hidden Layer 1: 64 neurons (ReLU)
├─ Hidden Layer 2: 32 neurons (ReLU)
└─ Output Layer: 1 neuron (Sigmoid) → Confidence (0.0-1.0)
```

### **Training Process**:

1. **Collect Training Data**:
   - When user **approves** a merge → Positive sample (label = 1.0)
   - When user **rejects** a merge → Negative sample (label = 0.0)
   - Features extracted from the merge suggestion

2. **Train Model**:
   - Minimum 50 samples required
   - Uses scikit-learn MLPRegressor
   - 80% training, 20% validation split
   - Early stopping to prevent overfitting

3. **Use Model**:
   - Predicts confidence for new merge suggestions
   - More accurate than heuristic formula
   - Adapts to your specific data patterns

---

## 🔄 How ML Would Improve Clustering

### **Current Flow (Heuristic)**:

```python
# Step 1: Find candidates
candidate_pairs = find_pattern_matches(identities)

# Step 2: Verify similarity
for pair in candidate_pairs:
    is_similar, face_sim = verify_face_similarity(pair)
    if is_similar:
        # Step 3: Calculate confidence (FIXED FORMULA)
        confidence = (face_sim * 0.6) + (pattern * 0.4)
        
        # Step 4: Create suggestion
        create_suggestion(pair, confidence)
```

### **Improved Flow (ML-Enhanced)**:

```python
# Step 1: Find candidates
candidate_pairs = find_pattern_matches(identities)

# Step 2: Verify similarity
for pair in candidate_pairs:
    is_similar, face_sim = verify_face_similarity(pair)
    if is_similar:
        # Step 3: Calculate confidence (ML PREDICTION)
        features = extract_features(pair)  # 6 features
        confidence = similarity_model.predict(**features)  # ML prediction
        
        # Step 4: Create suggestion
        create_suggestion(pair, confidence)
```

### **Benefits of ML Approach**:

1. **Learns from Feedback**:
   - If users consistently reject certain types of merges, model learns
   - If users approve cross-camera merges with lower similarity, model adapts

2. **More Accurate Confidence**:
   - Heuristic: Fixed formula may be wrong for your data
   - ML: Learns optimal weights from your actual decisions

3. **Handles Edge Cases**:
   - Low quality faces: ML learns to reduce confidence
   - Cross-pipeline: ML learns when it's reliable vs not
   - Appearance differences: ML learns acceptable thresholds

4. **Improves Over Time**:
   - More feedback = better predictions
   - Model retrains periodically with new data

---

## 📊 Example: ML vs Heuristic

### **Scenario**: Two identities with:
- Face similarity: 0.45 (moderate)
- Same pipeline: Yes
- Quality scores: 0.6, 0.7 (medium)
- Appearances: 10 vs 15 (similar)

**Heuristic Calculation**:
```python
pattern_confidence = 0.75  # Same pipeline
combined = (0.45 * 0.6) + (0.75 * 0.4) = 0.57
# Result: 57% confidence
```

**ML Prediction** (after training):
```python
features = {
    'embedding_similarity': 0.45,
    'pipeline_overlap': 1.0,
    'quality_score_1': 0.6,
    'quality_score_2': 0.7,
    'appearances_diff': 0.2,
    'is_cross_pipeline': False
}
confidence = model.predict(**features)
# Result: 0.72 (72% confidence)
# ML learned that medium similarity + same pipeline + decent quality = good merge
```

**Why ML is Better**:
- If users frequently approve merges like this, ML learns to increase confidence
- If users reject them, ML learns to decrease confidence
- Heuristic stays fixed at 57% regardless of feedback

---

## 🛠️ Integration Status

### **Current State**:

✅ **ML Model Exists**: `backend/core/similarity_model.py`
✅ **Training Endpoint**: `/api/admin/merge-suggestions/train-model`
✅ **Data Collection**: User feedback is collected when approving/rejecting merges
❌ **Not Used in Clustering**: Clustering still uses heuristic formula

### **To Enable ML in Clustering**:

You would need to modify `backend/core/identity_clustering.py`:

```python
# In _hybrid_clustering() method, replace:
combined_confidence = (face_similarity * 0.6) + (pattern_confidence * 0.4)

# With:
from backend.core.similarity_model import similarity_model

# Extract features
features = {
    'embedding_similarity': face_similarity,
    'pipeline_overlap': overlap_ratio,
    'quality_score_1': identity1.avg_quality or 0.5,
    'quality_score_2': identity2.avg_quality or 0.5,
    'appearances_diff': abs(identity1.appearances_count - identity2.appearances_count) / max(identity1.appearances_count, identity2.appearances_count, 1),
    'is_cross_pipeline': is_cross_camera
}

# Use ML prediction (falls back to heuristic if not trained)
combined_confidence = similarity_model.predict(**features)
```

---

## 📈 Training the Model

### **Step 1: Collect Feedback** (Automatic):

Every time a user approves or rejects a merge suggestion, training data is collected:

```python
# When user approves merge
similarity_model.add_training_sample(
    embedding_similarity=0.65,
    pipeline_overlap=0.8,
    quality_score_1=0.7,
    quality_score_2=0.75,
    appearances_diff=0.1,
    is_cross_pipeline=False,
    label=1.0  # Approved = positive sample
)

# When user rejects merge
similarity_model.add_training_sample(
    embedding_similarity=0.40,
    pipeline_overlap=0.3,
    quality_score_1=0.5,
    quality_score_2=0.6,
    appearances_diff=0.5,
    is_cross_pipeline=True,
    label=0.0  # Rejected = negative sample
)
```

### **Step 2: Train Model** (Manual or Auto):

**Manual Training**:
```bash
POST /api/admin/merge-suggestions/train-model
```

**Auto-Training** (if enabled):
- Trains automatically when enough samples collected
- Configurable via `SIMILARITY_MODEL_AUTO_TRAIN` setting

### **Step 3: Use Model** (After Integration):

Once integrated, clustering will automatically use ML predictions instead of heuristic.

---

## 🎓 Summary

### **How ML Helps Clustering**:

1. **Better Confidence Scores**: ML learns optimal weights from your feedback
2. **Adaptive**: Improves as you approve/reject more merges
3. **Handles Complexity**: Considers 6 features together, not just fixed formulas
4. **Reduces False Positives**: Learns which merges you actually want

### **Current vs ML**:

| Aspect | Heuristic (Current) | ML (Available) |
|--------|---------------------|----------------|
| **Confidence Calculation** | Fixed formula | Learned from feedback |
| **Adaptability** | None | Improves over time |
| **Accuracy** | Good for general cases | Better for your specific data |
| **Integration** | ✅ Active | ❌ Needs integration |
| **Training Required** | No | Yes (50+ samples) |

### **Next Steps**:

1. **Collect Feedback**: Approve/reject merge suggestions to build training data
2. **Train Model**: Use `/api/admin/merge-suggestions/train-model` endpoint
3. **Integrate**: Modify clustering code to use `similarity_model.predict()`
4. **Monitor**: Check model accuracy and retrain periodically

The ML model is **ready to use** - it just needs to be integrated into the clustering confidence calculation!

