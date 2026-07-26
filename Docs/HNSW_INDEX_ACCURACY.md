# HNSW Index Accuracy & Similarity Scores

## Overview

pgvector uses **HNSW (Hierarchical Navigable Small World)** algorithm for approximate nearest neighbor search. This is an **approximate** algorithm, which means it may return slightly different results than exact search.

## How HNSW Affects Similarity

### ✅ What HNSW Does:
- **Fast approximate search** - Much faster than exact search
- **High accuracy** - Usually finds the correct nearest neighbors
- **Scalable** - Works well with millions of vectors

### ⚠️ What HNSW Might Do:
- **Approximate results** - May explore fewer candidates than exact search
- **Slightly lower similarity** - Might miss the absolute best match if `ef_search` is too low
- **Trade-off** - Speed vs. accuracy

## HNSW Parameters

### Index Parameters (set during index creation):
- **`m`** (default: 16): Maximum connections per layer
  - Higher = more accurate but slower index build
  - Current: 16 (good balance)

- **`ef_construction`** (default: 64): Candidate list size during index build
  - Higher = more accurate index but slower build
  - Current: 64 (good balance)

### Search Parameters (set per query):
- **`ef_search`** (default: 40): Number of candidates to explore during search
  - **Higher = more accurate but slower**
  - **Lower = faster but may miss best matches**
  - **Current: 40** (can be increased for better accuracy)

## Current Configuration

```python
# config.py (or environment variables)
PGVECTOR_INDEX_TYPE = "hnsw"  # Using HNSW index
PGVECTOR_HNSW_M = 16          # Index build parameter
PGVECTOR_HNSW_EF_CONSTRUCTION = 64  # Index build parameter
PGVECTOR_HNSW_EF_SEARCH = 40  # Search-time parameter (NEW)
```

## Improving Accuracy

### Option 1: Increase `ef_search` (Recommended)
Higher `ef_search` = more candidates explored = better accuracy

```python
# In config.py or environment
PGVECTOR_HNSW_EF_SEARCH = 100  # Increase from 40 to 100 for better accuracy
```

**Trade-off:**
- ✅ More accurate similarity scores
- ✅ Better chance of finding true nearest neighbors
- ⚠️ Slightly slower search (usually negligible)

### Option 2: Use Exact Search (Not Recommended)
Remove HNSW index and use exact search (very slow for large datasets)

```sql
-- Don't do this for production!
DROP INDEX idx_embedding_vector_hnsw;
-- Then queries will use exact search (slow!)
```

## Why Similarity Might Be Lower

### 1. **HNSW Approximation** (if `ef_search` is too low)
- HNSW might explore fewer candidates
- May miss the absolute best match
- **Solution**: Increase `ef_search` to 100 or higher

### 2. **Actual Face Differences**
- Different angles, lighting, age, appearance
- **This is normal** - not an HNSW issue

### 3. **Threshold Too High**
- Current: 0.4 (40% similarity required)
- If actual similarity is 0.35, it won't match
- **Solution**: Lower threshold (not recommended for security)

## Verification

### Check Current `ef_search`:
```bash
docker exec face_recognition_api python -c "
from config import settings
print(f'HNSW ef_search: {getattr(settings, \"PGVECTOR_HNSW_EF_SEARCH\", 40)}')
"
```

### Test with Higher `ef_search`:
1. Set `PGVECTOR_HNSW_EF_SEARCH=100` in `config.py` or environment
2. Restart backend
3. Check logs for similarity scores
4. Compare with previous results

## Recommended Settings

### For Production (Balance Speed/Accuracy):
```python
PGVECTOR_HNSW_EF_SEARCH = 40  # Default - good balance
```

### For Maximum Accuracy:
```python
PGVECTOR_HNSW_EF_SEARCH = 100  # Higher accuracy, slightly slower
```

### For Maximum Speed:
```python
PGVECTOR_HNSW_EF_SEARCH = 20  # Faster, may miss some matches
```

## Summary

✅ **HNSW is configured correctly**
✅ **All embeddings are normalized (norm = 1.0)**
✅ **Cosine similarity calculation is correct**

If similarity is still lower than expected:
1. **Increase `ef_search`** to 100 for better accuracy
2. **Check actual similarity scores** in logs
3. **Verify threshold** (0.4 = 40% similarity required)
4. **Consider face quality** (blurry, side view, etc.)

HNSW approximation is usually very accurate, but increasing `ef_search` can help ensure we find the best matches.

