# Embedding Normalization Verification

## ✅ Verification Results

**All embeddings in the database are properly normalized!**

- **Total embeddings**: 12
- **✅ Correct norm (≈1.0)**: 12 (100.0%)
- **❌ Incorrect norm (≠1.0)**: 0 (0.0%)
- **Min norm**: 1.000000
- **Max norm**: 1.000000
- **Avg norm**: 1.000000

## Why Normalization Matters

For **cosine similarity** to work correctly with pgvector:

1. **ALL stored embeddings** must be L2-normalized (norm = 1.0)
2. **ALL query embeddings** must be L2-normalized (norm = 1.0)
3. **pgvector uses cosine distance**: `embedding <=> query_embedding`
4. **Cosine similarity** = `1 - cosine_distance`

If embeddings are NOT normalized:
- Similarity scores will be **incorrect**
- Scores may be **lower** than expected
- Faces may not match even when they should

## Current Implementation

### ✅ When Saving Embeddings:
```python
# backend/core/identity_index_pgvector.py:add_embedding()
norm = np.linalg.norm(embedding)
normalized = (embedding / norm).astype(np.float32)  # L2 normalize
# Saved to database with norm = 1.0
```

### ✅ When Searching:
```python
# backend/core/identity_index_pgvector.py:search_known()
norm = np.linalg.norm(embedding)
normalized = (embedding / norm).astype(np.float32)  # L2 normalize
# Query uses normalized embedding
```

### ✅ When Generating Embeddings:
```python
# backend/services/image_processing.py
embedding = model_manager.recognizer.get_embedding(...)
embedding = embedding / np.linalg.norm(embedding)  # Normalize
# Already normalized before search
```

## Verification Script

Run to check all embedding norms:
```bash
docker exec face_recognition_api python scripts/check_embedding_norms.py
```

## If Similarity is Still Low

If embeddings are normalized but similarity is still low:

1. **Check actual similarity scores** in logs:
   ```
   [PGVECTOR] [SEARCH_KNOWN] Similarity: 0.3500 (35.00%)
   [PGVECTOR] [SEARCH_KNOWN] Threshold: 0.4000 (40.00%)
   ```

2. **Possible causes**:
   - Face quality is low (blurry, side view, occluded)
   - Different lighting conditions
   - Different age/appearance (beard, glasses, etc.)
   - Threshold might be too high (0.4 = 40%)

3. **Solutions**:
   - Lower threshold (not recommended for security)
   - Improve image quality
   - Add more training images with different angles/lighting
   - Check if face alignment is correct

## Summary

✅ **Normalization is correct** - All embeddings have norm = 1.0
✅ **Cosine similarity calculation is correct** - Using `1 - (embedding <=> query)`
✅ **Query embeddings are normalized** - Before search

If similarity is low, it's likely due to:
- **Actual face differences** (not a normalization issue)
- **Threshold too high** (0.4 = 40% similarity required)
- **Image quality** (blurry, side view, etc.)

