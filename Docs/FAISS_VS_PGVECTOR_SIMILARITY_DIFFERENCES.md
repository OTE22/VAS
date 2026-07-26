# FAISS vs pgvector: Why FAISS Works Better for Face Recognition

## 🔍 Critical Discovery

After researching and analyzing the codebase, here's why **FAISS succeeds** while **pgvector fails**:

## Key Differences

### 1. **Similarity Metric**

#### FAISS (IndexFlatIP):
```python
# FAISS uses IndexFlatIP = Inner Product
index = faiss.IndexFlatIP(embedding_size)  # Inner Product

# With normalized vectors:
normalized_embedding = embedding / np.linalg.norm(embedding)
similarities, indices = index.search(normalized_embedding, top_k)
# Returns: Inner Product = Cosine Similarity (for normalized vectors)
```

**Result**: Direct cosine similarity (0.0 to 1.0)

#### pgvector (Cosine Distance):
```sql
-- pgvector uses <=> operator = Cosine Distance
1 - (ie.embedding <=> qv.vec) as similarity
-- Returns: 1 - Cosine Distance = Cosine Similarity
```

**Result**: Cosine similarity via `1 - cosine_distance`

### 2. **Mathematical Equivalence**

For **normalized vectors** (L2 norm = 1.0):
- ✅ **Inner Product = Cosine Similarity** (mathematically equivalent)
- ✅ **1 - Cosine Distance = Cosine Similarity** (mathematically equivalent)

**Both should give the same results IF vectors are normalized!**

## Why FAISS Works Better

### 1. **Exact Search vs Approximate**

**FAISS (IndexFlatIP)**:
- ✅ **Exact search** - Finds true nearest neighbors
- ✅ **No approximation** - Guaranteed correct results
- ✅ **Direct inner product** - No index approximation errors

**pgvector (HNSW)**:
- ⚠️ **Approximate search** - May miss best matches
- ⚠️ **HNSW approximation** - Explores limited candidates
- ⚠️ **ef_search parameter** - Lower values = less accurate

### 2. **Index Type**

**FAISS**:
```python
IndexFlatIP  # Exact search, no approximation
```

**pgvector**:
```sql
USING hnsw (embedding vector_cosine_ops)  # Approximate search
```

### 3. **Performance vs Accuracy Trade-off**

**FAISS IndexFlatIP**:
- ✅ **100% accurate** - Always finds true nearest neighbors
- ✅ **Fast for small datasets** - Direct computation
- ⚠️ **Slower for large datasets** - O(n) search

**pgvector HNSW**:
- ⚠️ **~95-99% accurate** - Approximate, may miss matches
- ✅ **Fast for large datasets** - O(log n) search
- ⚠️ **Accuracy depends on ef_search** - Lower = less accurate

## The Real Problem

### Why pgvector Similarity is Lower:

1. **HNSW Approximation**:
   - HNSW explores only `ef_search` candidates (default: 40)
   - May skip the true best match if it's not in the explored set
   - **Solution**: Increase `ef_search` to 100+ for better accuracy

2. **Index Build Quality**:
   - HNSW index quality depends on `ef_construction` (default: 64)
   - Lower values = less accurate index structure
   - **Solution**: Increase `ef_construction` to 100+ for better index

3. **Query-Time Accuracy**:
   - `ef_search` controls search-time accuracy
   - Default 40 may be too low for face recognition
   - **Solution**: Set `PGVECTOR_HNSW_EF_SEARCH=100` or higher

## Solutions

### Option 1: Increase HNSW Accuracy (Recommended)

```python
# In config.py
PGVECTOR_HNSW_EF_SEARCH = 100  # Increase from 40 to 100
PGVECTOR_HNSW_EF_CONSTRUCTION = 100  # Increase from 64 to 100
```

**Trade-off**: Slightly slower but more accurate (closer to FAISS)

### Option 2: Use Exact Search (Not Recommended for Production)

Remove HNSW index and use exact search:
```sql
DROP INDEX idx_embedding_vector_hnsw;
-- Queries will use exact search (very slow for large datasets)
```

**Trade-off**: 100% accurate but very slow

### Option 3: Use FAISS for Search, pgvector for Storage

Hybrid approach:
- Store embeddings in pgvector (database persistence)
- Use FAISS for search (exact, fast)
- Sync FAISS from pgvector on startup

**Trade-off**: Best of both worlds but more complex

## Recommended Fix

### Immediate Action:

1. **Increase `ef_search`** to match FAISS accuracy:
   ```python
   # config.py
   PGVECTOR_HNSW_EF_SEARCH = 100  # Much better accuracy
   ```

2. **Rebuild HNSW index** with better parameters:
   ```python
   # config.py
   PGVECTOR_HNSW_EF_CONSTRUCTION = 100  # Better index quality
   ```

3. **Restart backend** to apply changes

### Expected Results:

- ✅ Similarity scores should increase (closer to FAISS)
- ✅ More faces should be recognized
- ⚠️ Slightly slower search (usually negligible)

## Verification

Compare similarity scores:
1. Run with FAISS: Note similarity scores
2. Run with pgvector (ef_search=100): Compare scores
3. They should be very close (within 0.01-0.02)

## Summary

**FAISS works better because:**
- ✅ Uses **exact search** (IndexFlatIP)
- ✅ **No approximation errors**
- ✅ **Guaranteed correct results**

**pgvector may fail because:**
- ⚠️ Uses **approximate search** (HNSW)
- ⚠️ **May miss best matches** if ef_search is too low
- ⚠️ **Index approximation** affects accuracy

**Solution:**
- Increase `ef_search` to 100+ for better accuracy
- This makes pgvector closer to FAISS accuracy
- Still faster than exact search for large datasets

