# pgvector Workflow Verification

## ✅ Verification Results

All checks passed! pgvector is properly configured and used throughout the system.

## Critical Code Paths

### 1. **Identity Search** (`find_or_create_identity`)

**Location**: `backend/core/identity_service.py:84`

```python
async def find_or_create_identity(...):
    # Use pgvector backend if configured
    if self.use_pgvector and self.pgvector_index:
        return await self._find_or_create_identity_pgvector(...)
    
    # FAISS backend (default) - only used if pgvector not enabled
    ...
```

**✅ Verified**: Checks `self.use_pgvector` and `self.pgvector_index` before using pgvector

### 2. **Embedding Saving** (`save_embedding`)

**Location**: `backend/core/identity_service.py:557`

```python
async def save_embedding(...):
    if self.use_pgvector and self.pgvector_index:
        # pgvector backend - store embedding directly in PostgreSQL
        emb_id = await self.pgvector_index.add_embedding(...)
    else:
        # FAISS backend - add to in-memory index
        ...
```

**✅ Verified**: Uses pgvector when enabled, FAISS otherwise

### 3. **Face Recognition Workflow** (`process_image_async`)

**Location**: `backend/services/image_processing.py:312`

```python
identity, is_new_identity, similarity = await identity_service.find_or_create_identity(
    embedding=embedding,
    pipeline_id=pipeline_id,
    detection_id=None,
    db=db,
    quality_score=quality_score
)
```

**✅ Verified**: Uses `identity_service.find_or_create_identity()` which routes to pgvector

### 4. **Known Faces Loading** (`load_known_faces_from_directory`)

**Location**: `backend/core/identity_loader.py:319`

```python
if USE_PGVECTOR and self.identity_service.use_pgvector:
    # pgvector backend
    emb_id = await self.identity_service.pgvector_index.add_embedding(...)
else:
    # FAISS backend
    faiss_id = self.identity_service.identity_index.add_known(...)
```

**✅ Verified**: Checks for pgvector before using FAISS

## Initialization Flow

### Startup Sequence (`backend/lifespan.py`)

1. **Identity Index Service** (FAISS) - Always initialized for compatibility
2. **pgvector Index** - Initialized if `VECTOR_BACKEND=pgvector`
3. **Identity Service** - Initialized with both indexes
4. **Identity Service checks**: `if self.use_pgvector and self.pgvector_index:`

## Configuration Check

Run verification script:
```bash
docker exec face_recognition_api python scripts/verify_pgvector_usage.py
```

**Expected Output**:
```
✅ pgvector is configured as the vector backend
✅ pgvector module imported successfully
✅ Global identity_index_pgvector instance exists
✅ IdentityService will use pgvector when initialized
✅ All critical code paths check for pgvector
```

## Potential Issues

### Issue 1: pgvector_index is None

**Symptom**: Logs show "⚠️ pgvector enabled but pgvector_index is None"

**Cause**: `IdentityIndexPgVector` not properly initialized

**Fix**: Check that `identity_index_pgvector` global instance is created on module import

### Issue 2: Falls Back to FAISS

**Symptom**: Logs show "Using FAISS backend" even when `VECTOR_BACKEND=pgvector`

**Cause**: `self.use_pgvector` is False or `self.pgvector_index` is None

**Fix**: 
1. Check `VECTOR_BACKEND` environment variable
2. Verify `pgvector_index` is passed to `IdentityService()`
3. Check logs for initialization errors

### Issue 3: Identity Loader Checks FAISS

**Symptom**: Logs show "Skipping ... with FAISS embeddings" when using pgvector

**Fix**: Already fixed - now checks pgvector embeddings in database when using pgvector

## Verification Checklist

- [x] `VECTOR_BACKEND=pgvector` in config
- [x] `IdentityIndexPgVector` module imports successfully
- [x] Global `identity_index_pgvector` instance exists
- [x] `IdentityService` initialized with `pgvector_index`
- [x] `find_or_create_identity()` checks for pgvector
- [x] `save_embedding()` uses pgvector when enabled
- [x] Identity loader checks pgvector embeddings
- [x] No direct FAISS calls when pgvector enabled

## Summary

✅ **pgvector is used throughout the workflow when activated**

The system properly:
1. Checks `self.use_pgvector` and `self.pgvector_index` before using pgvector
2. Falls back to FAISS only if pgvector is not enabled or not available
3. Uses pgvector for all identity searches and embedding saves
4. Initializes pgvector index on startup

**No FAISS is used when pgvector is enabled** - all code paths check for pgvector first.

