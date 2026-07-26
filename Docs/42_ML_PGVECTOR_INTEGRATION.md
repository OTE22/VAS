# ML Similarity Model with pgvector Integration

## 📋 Overview

When using **pgvector** as the vector backend, integrating the ML similarity model is **simpler and more efficient** than with FAISS because all data is already in the PostgreSQL database.

---

## 🔄 Current Clustering Flow with pgvector

### **Step-by-Step Process**:

```
1. Find Candidate Pairs (Pattern-Based)
   ├─ Same pipeline detection
   └─ Cross-camera detection
   
2. Verify Face Similarity (pgvector SQL)
   ├─ Direct SQL query with <=> operator
   ├─ Gets: similarity_score, quality1, quality2
   └─ Returns: (is_similar, face_similarity)
   
3. Calculate Confidence (Heuristic - Current)
   ├─ Same-camera: (face_sim × 0.6) + (pattern × 0.4)
   └─ Cross-camera: (face_sim × 0.8) + 0.06
   
4. Create Merge Suggestion
   └─ confidence = calculated score
```

### **pgvector Similarity Calculation**:

```sql
-- Current implementation in _verify_face_similarity_pgvector()
WITH best_embeddings AS (
    SELECT 
        identity_id,
        embedding,
        quality,
        ROW_NUMBER() OVER (
            PARTITION BY identity_id 
            ORDER BY quality DESC NULLS LAST, created_at DESC
        ) as rn
    FROM identity_embeddings
    WHERE 
        identity_id IN (:id1, :id2)
        AND embedding IS NOT NULL
        AND faiss_index_type = 'unknown'
)
SELECT 
    1 - (e1.embedding <=> e2.embedding) as similarity,  -- <=> is cosine distance
    e1.quality as quality1,
    e2.quality as quality2
FROM best_embeddings e1
CROSS JOIN best_embeddings e2
WHERE 
    e1.identity_id = :id1
    AND e2.identity_id = :id2
    AND e1.rn = 1 
    AND e2.rn = 1
```

**Returns**:
- `similarity`: Cosine similarity (0.0-1.0)
- `quality1`: Quality score of identity 1
- `quality2`: Quality score of identity 2

---

## 🤖 ML Model Integration with pgvector

### **Why pgvector Makes ML Integration Easier**:

✅ **All data in one place**: Database contains embeddings, quality scores, pipeline info
✅ **Single query**: Can get all ML features in one SQL query
✅ **No reconstruction needed**: Embeddings already stored as vectors
✅ **Simpler code**: No need to fetch from FAISS index + database separately

### **ML Model Features (All Available from Database)**:

| Feature | Source | pgvector Query |
|---------|--------|----------------|
| `embedding_similarity` | pgvector `<=>` operator | ✅ Already calculated |
| `pipeline_overlap` | `IdentityAppearance` table | ✅ SQL JOIN |
| `quality_score_1` | `IdentityEmbedding.quality` | ✅ Already in query |
| `quality_score_2` | `IdentityEmbedding.quality` | ✅ Already in query |
| `appearances_diff` | `Identity.appearances_count` | ✅ Direct access |
| `is_cross_pipeline` | Pipeline comparison | ✅ SQL comparison |

---

## 🔧 Enhanced pgvector Query for ML

### **Current Query** (Similarity Only):

```sql
SELECT 
    1 - (e1.embedding <=> e2.embedding) as similarity,
    e1.quality as quality1,
    e2.quality as quality2
FROM best_embeddings e1, best_embeddings e2
WHERE e1.identity_id = :id1 AND e2.identity_id = :id2
```

### **Enhanced Query** (All ML Features):

```sql
WITH best_embeddings AS (
    SELECT 
        identity_id,
        embedding,
        quality,
        ROW_NUMBER() OVER (
            PARTITION BY identity_id 
            ORDER BY quality DESC NULLS LAST, created_at DESC
        ) as rn
    FROM identity_embeddings
    WHERE 
        identity_id IN (:id1, :id2)
        AND embedding IS NOT NULL
        AND faiss_index_type = 'unknown'
),
identity_pipelines AS (
    SELECT 
        identity_id,
        array_agg(DISTINCT pipeline_id) as pipelines
    FROM identity_appearances
    WHERE identity_id IN (:id1, :id2)
    GROUP BY identity_id
)
SELECT 
    -- Face similarity (from pgvector)
    1 - (e1.embedding <=> e2.embedding) as embedding_similarity,
    
    -- Quality scores (already available)
    e1.quality as quality_score_1,
    e2.quality as quality_score_2,
    
    -- Pipeline overlap (calculate from arrays)
    (
        SELECT COUNT(*)::float / NULLIF(GREATEST(
            array_length(ip1.pipelines, 1), 
            array_length(ip2.pipelines, 1)
        ), 0)
        FROM unnest(ip1.pipelines) p1
        WHERE p1 = ANY(ip2.pipelines)
    ) as pipeline_overlap,
    
    -- Cross-pipeline flag
    CASE 
        WHEN ip1.pipelines && ip2.pipelines THEN false  -- Has overlap
        ELSE true  -- No overlap = cross-pipeline
    END as is_cross_pipeline,
    
    -- Appearances difference (from Identity table)
    ABS(i1.appearances_count - i2.appearances_count)::float / 
        NULLIF(GREATEST(i1.appearances_count, i2.appearances_count, 1), 0) as appearances_diff
    
FROM best_embeddings e1
CROSS JOIN best_embeddings e2
JOIN identity_pipelines ip1 ON ip1.identity_id = :id1
JOIN identity_pipelines ip2 ON ip2.identity_id = :id2
JOIN identities i1 ON i1.id = :id1
JOIN identities i2 ON i2.id = :id2
WHERE 
    e1.identity_id = :id1
    AND e2.identity_id = :id2
    AND e1.rn = 1 
    AND e2.rn = 1
```

**Result**: All 6 ML features in **one query**! 🎯

---

## 💻 Integration Code

### **Enhanced `_verify_face_similarity_pgvector()` with ML**:

```python
async def _verify_face_similarity_pgvector(
    self,
    identity1: Identity,
    identity2: Identity,
    db
) -> Tuple[bool, float, Dict]:
    """
    Verify face similarity using pgvector and extract ML features.
    
    Returns:
        Tuple of (is_similar, similarity_score, ml_features)
    """
    from backend.core.similarity_model import similarity_model
    
    # Enhanced query to get all ML features
    query = text("""
        WITH best_embeddings AS (
            SELECT identity_id, embedding, quality,
                ROW_NUMBER() OVER (
                    PARTITION BY identity_id 
                    ORDER BY quality DESC NULLS LAST, created_at DESC
                ) as rn
            FROM identity_embeddings
            WHERE identity_id IN (:id1, :id2)
                AND embedding IS NOT NULL
                AND faiss_index_type = 'unknown'
        ),
        identity_pipelines AS (
            SELECT identity_id, array_agg(DISTINCT pipeline_id) as pipelines
            FROM identity_appearances
            WHERE identity_id IN (:id1, :id2)
            GROUP BY identity_id
        )
        SELECT 
            1 - (e1.embedding <=> e2.embedding) as embedding_similarity,
            e1.quality as quality_score_1,
            e2.quality as quality_score_2,
            (
                SELECT COUNT(*)::float / NULLIF(GREATEST(
                    array_length(ip1.pipelines, 1), 
                    array_length(ip2.pipelines, 1)
                ), 0)
                FROM unnest(ip1.pipelines) p1
                WHERE p1 = ANY(ip2.pipelines)
            ) as pipeline_overlap,
            CASE 
                WHEN ip1.pipelines && ip2.pipelines THEN false
                ELSE true
            END as is_cross_pipeline,
            ABS(i1.appearances_count - i2.appearances_count)::float / 
                NULLIF(GREATEST(i1.appearances_count, i2.appearances_count, 1), 0) as appearances_diff
        FROM best_embeddings e1
        CROSS JOIN best_embeddings e2
        JOIN identity_pipelines ip1 ON ip1.identity_id = :id1
        JOIN identity_pipelines ip2 ON ip2.identity_id = :id2
        JOIN identities i1 ON i1.id = :id1
        JOIN identities i2 ON i2.id = :id2
        WHERE e1.identity_id = :id1 AND e2.identity_id = :id2
            AND e1.rn = 1 AND e2.rn = 1
    """)
    
    result = await db.execute(query, {"id1": str(identity1.id), "id2": str(identity2.id)})
    row = result.fetchone()
    
    if not row:
        return False, 0.0, {}
    
    # Extract all features
    embedding_similarity = float(row[0])
    quality_score_1 = float(row[1]) if row[1] else 0.5
    quality_score_2 = float(row[2]) if row[2] else 0.5
    pipeline_overlap = float(row[3]) if row[3] else 0.0
    is_cross_pipeline = bool(row[4])
    appearances_diff = float(row[5]) if row[5] else 0.0
    
    # Check threshold (basic similarity check)
    threshold = 0.35
    is_similar = embedding_similarity >= threshold
    
    # Prepare ML features
    ml_features = {
        'embedding_similarity': embedding_similarity,
        'pipeline_overlap': pipeline_overlap,
        'quality_score_1': quality_score_1,
        'quality_score_2': quality_score_2,
        'appearances_diff': appearances_diff,
        'is_cross_pipeline': is_cross_pipeline
    }
    
    return is_similar, embedding_similarity, ml_features
```

### **Using ML in Clustering**:

```python
# In _hybrid_clustering() method, replace:
combined_confidence = (face_similarity * 0.6) + (pattern_confidence * 0.4)

# With:
is_similar, face_similarity, ml_features = await self._verify_face_similarity_pgvector(
    identity1, identity2, db
)

if is_similar:
    # Use ML model for confidence prediction
    from backend.core.similarity_model import similarity_model
    
    if similarity_model.is_trained:
        # ML prediction (learned from feedback)
        combined_confidence = similarity_model.predict(**ml_features)
    else:
        # Fallback to heuristic if model not trained
        if is_cross_camera:
            combined_confidence = (face_similarity * 0.8) + 0.06
        else:
            pattern_confidence = min(0.75, overlap_ratio + (0.1 if time_overlap else 0))
            combined_confidence = (face_similarity * 0.6) + (pattern_confidence * 0.4)
```

---

## 🎯 Advantages of pgvector + ML

### **1. Single Query Efficiency**:

**FAISS Approach**:
```python
# Step 1: Get embeddings from FAISS index
emb1 = identity_index.get_embedding(identity1.id)
emb2 = identity_index.get_embedding(identity2.id)

# Step 2: Calculate similarity
similarity = cosine_similarity(emb1, emb2)

# Step 3: Query database for quality scores
quality1 = db.query(IdentityEmbedding).filter(...).first().quality
quality2 = db.query(IdentityEmbedding).filter(...).first().quality

# Step 4: Query for pipeline info
pipelines1 = db.query(IdentityAppearance).filter(...).all()
pipelines2 = db.query(IdentityAppearance).filter(...).all()

# Step 5: Calculate pipeline_overlap
overlap = calculate_overlap(pipelines1, pipelines2)
```

**pgvector Approach**:
```python
# Single SQL query gets everything!
result = db.execute(enhanced_query)
row = result.fetchone()
# All 6 ML features ready to use!
```

### **2. Database Consistency**:

- ✅ **ACID transactions**: All data in same database
- ✅ **No sync issues**: No FAISS index to keep in sync
- ✅ **Atomic operations**: Everything in one transaction

### **3. Simpler Code**:

- ✅ **No embedding reconstruction**: Already stored as vectors
- ✅ **No FAISS ID management**: Direct database queries
- ✅ **Less error handling**: Single query vs multiple operations

---

## 📊 Performance Comparison

### **Feature Extraction**:

| Operation | FAISS | pgvector |
|-----------|-------|----------|
| Get embeddings | Index lookup | ✅ In query |
| Calculate similarity | Python code | ✅ SQL `<=>` |
| Get quality scores | DB query | ✅ In query |
| Get pipeline info | DB query | ✅ In query |
| Calculate overlap | Python code | ✅ SQL calculation |
| **Total queries** | **3-4 queries** | **1 query** |

### **ML Prediction**:

Both approaches use the same ML model:
```python
confidence = similarity_model.predict(**ml_features)
```

**No difference** - ML model is backend-agnostic!

---

## 🔄 Complete Flow with pgvector + ML

```
1. Pattern-Based Candidate Detection
   └─ Find potential merge pairs
   
2. pgvector Similarity + Feature Extraction (ONE QUERY)
   ├─ Calculate embedding similarity (<=> operator)
   ├─ Get quality scores
   ├─ Get pipeline overlap
   ├─ Get appearances difference
   └─ Determine cross-pipeline flag
   
3. ML Confidence Prediction
   ├─ Extract 6 features from query result
   ├─ Use similarity_model.predict()
   └─ Get learned confidence score
   
4. Create Merge Suggestion
   └─ confidence = ML prediction (or heuristic fallback)
```

---

## 🎓 Summary

### **Key Points**:

1. **pgvector makes ML integration simpler**:
   - All features available in one SQL query
   - No need to reconstruct embeddings
   - No separate FAISS index lookups

2. **Single query efficiency**:
   - FAISS: 3-4 separate operations
   - pgvector: 1 SQL query with all features

3. **ML model works the same**:
   - Same 6 input features
   - Same prediction logic
   - Backend-agnostic

4. **Better performance**:
   - Fewer database round-trips
   - Database-optimized calculations
   - Atomic operations

### **Integration Steps**:

1. ✅ Enhance `_verify_face_similarity_pgvector()` to return ML features
2. ✅ Modify clustering to use `similarity_model.predict()`
3. ✅ Train model with user feedback
4. ✅ Monitor accuracy and retrain periodically

**pgvector + ML = Perfect combination for accurate, efficient merge suggestions!** 🚀

