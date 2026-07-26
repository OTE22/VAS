# pgvector Integration Guide

## Overview

This document describes the pgvector integration for face embedding similarity search. pgvector is a PostgreSQL extension that adds support for vector similarity search, providing an alternative to FAISS that stores vectors directly in PostgreSQL.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Vector Search Backends                    │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│   ┌─────────────────┐        ┌─────────────────┐           │
│   │     FAISS       │   OR   │    pgvector     │           │
│   │  (In-Memory)    │        │  (PostgreSQL)   │           │
│   └────────┬────────┘        └────────┬────────┘           │
│            │                          │                     │
│            ▼                          ▼                     │
│   ┌─────────────────┐        ┌─────────────────┐           │
│   │ identity_index  │        │identity_index   │           │
│   │    .py          │        │ _pgvector.py    │           │
│   └─────────────────┘        └─────────────────┘           │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## Configuration

### Enabling pgvector

Set the `VECTOR_BACKEND` environment variable:

```bash
# In .env or docker-compose environment
VECTOR_BACKEND=pgvector
```

### pgvector Configuration Options

| Variable | Default | Description |
|----------|---------|-------------|
| `VECTOR_BACKEND` | `faiss` | Vector backend: `faiss` or `pgvector` |
| `PGVECTOR_INDEX_TYPE` | `hnsw` | Index type: `hnsw` or `ivfflat` |
| `PGVECTOR_HNSW_M` | `16` | HNSW connections per node (16-64) |
| `PGVECTOR_HNSW_EF_CONSTRUCTION` | `64` | HNSW build-time search width |
| `PGVECTOR_IVFFLAT_LISTS` | `100` | IVFFlat clusters (use √N) |
| `PGVECTOR_IVFFLAT_PROBES` | `10` | IVFFlat clusters to search |

## Docker Setup

### Updated PostgreSQL Image

The `docker-compose.gpu.yml` and `docker-compose.cpu.yml` files now use:

```yaml
postgres:
  image: pgvector/pgvector:pg15  # Includes pgvector extension
```

### init-db.sql

The vector extension is automatically created:

```sql
CREATE EXTENSION IF NOT EXISTS "vector";
```

## Database Schema

### IdentityEmbedding Model

The `IdentityEmbedding` model now includes a `embedding` column:

```python
class IdentityEmbedding(Base):
    # Existing FAISS fields
    faiss_id = Column(Integer, nullable=True)
    faiss_index_type = Column(String(50), nullable=True)
    
    # NEW: pgvector field
    embedding = Column(Vector(512), nullable=True)  # 512-dim for ArcFace
```

## Migration

### Fresh Installation

For new installations, pgvector works out of the box:

1. Start containers: `docker compose -f docker/docker-compose.gpu.yml up -d`
2. Set environment: `VECTOR_BACKEND=pgvector`
3. Restart the face recognition service

### Existing Installation (FAISS → pgvector)

To migrate existing FAISS embeddings:

```bash
# 1. Run database migration
cd /app
alembic upgrade head

# 2. Migrate FAISS embeddings to pgvector
# First, do a dry run:
python scripts/migrate_faiss_to_pgvector.py --dry-run

# Then, run the actual migration:
python scripts/migrate_faiss_to_pgvector.py

# 3. Enable pgvector backend
export VECTOR_BACKEND=pgvector

# 4. Restart the service
```

## Performance Comparison

| Metric | FAISS (Flat) | pgvector (HNSW) |
|--------|-------------|-----------------|
| Search Speed (10k vectors) | ~1ms | ~2-5ms |
| Search Speed (1M vectors) | ~50ms | ~5-20ms |
| Memory Usage | High (in-memory) | Low (disk-based) |
| Persistence | Manual save/load | Automatic (PostgreSQL) |
| Transactions | No | Yes (ACID) |
| Sync Issues | Possible | None |
| Maintenance | Rebuild required | Automatic |

## When to Use pgvector

**Use pgvector when:**
- You want simpler architecture (single data store)
- ACID compliance is important
- You have moderate scale (< 5M embeddings)
- You need transactional consistency

**Use FAISS when:**
- You need maximum search speed
- You have very large scale (> 5M embeddings)
- GPU acceleration is available
- Memory is not a constraint

## API Compatibility

The `IdentityService` automatically uses the configured backend. All existing APIs work unchanged:

```python
# Works with both backends
identity, is_new, similarity = await identity_service.find_or_create_identity(
    embedding=face_embedding,
    pipeline_id="camera_1",
    detection_id=123,
    db=session
)
```

## Logging

pgvector operations are logged with the `[PGVECTOR]` prefix:

```log
[PGVECTOR] [SEARCH_KNOWN] Starting search: top_k=5, threshold=0.4
[PGVECTOR] [SEARCH_KNOWN] Found 2 matches (best: 0.8542) in 3.21ms
[PGVECTOR] [ADD] ✅ Added embedding: id=42 identity=abc123... type=known quality=0.85
```

## Troubleshooting

### Extension Not Found

```
ERROR: extension "vector" is not available
```

**Solution:** Use the `pgvector/pgvector:pg15` Docker image instead of `postgres:15-alpine`.

### Slow Searches

If searches are slow, ensure the HNSW index exists:

```sql
-- Check if index exists
SELECT indexname FROM pg_indexes 
WHERE tablename = 'identity_embeddings' 
AND indexname LIKE '%embedding%';

-- Create if missing
CREATE INDEX idx_embedding_vector_hnsw
ON identity_embeddings 
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

### Migration Failures

If migration fails:

```bash
# Check prerequisites
python -c "import pgvector; print('pgvector installed')"

# Verify extension
psql -c "SELECT * FROM pg_extension WHERE extname = 'vector';"

# Re-run migration with verbose logging
python scripts/migrate_faiss_to_pgvector.py --dry-run
```

## Health Check

The pgvector service includes a health check endpoint:

```python
async def health_check(self, db: AsyncSession) -> Dict:
    return {
        'healthy': True,
        'pgvector_extension': True,
        'vector_index_exists': True,
        'can_search': True,
        'embedding_count': 12345
    }
```

## References

- [pgvector GitHub](https://github.com/pgvector/pgvector)
- [pgvector SQLAlchemy integration](https://github.com/pgvector/pgvector-python)
- [HNSW Algorithm](https://arxiv.org/abs/1603.09320)

