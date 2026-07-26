# FAISS Index Production Scaling Guide
## Best Practices for 1 Million+ Known Images

### 📊 Current Implementation Analysis

**Current Index Type:** `IndexFlatIP` (Inner Product, exact search)

**Performance Characteristics:**
- **Memory:** ~2GB for 1M vectors (512 dim × 4 bytes × 1M)
- **Search Time:** 10-50ms per query (CPU), 1-5ms (GPU)
- **Accuracy:** 100% (exact search)
- **Scalability:** Good up to ~5M vectors, then becomes slow

**Limitations:**
- Linear search complexity O(n)
- High memory usage
- Slow for very large datasets (>10M vectors)

---

## 🎯 Recommended Index Types for Production

### Option 1: IndexIVFFlat (Recommended for 1M-10M)

**Best for:** Production systems with 1M-10M known faces

**Characteristics:**
- **Memory:** ~2GB (same as Flat)
- **Search Time:** 1-5ms (CPU), 0.5-2ms (GPU)
- **Accuracy:** 95-99% (approximate, configurable)
- **Training:** Required (one-time, ~5-10 minutes for 1M vectors)

**Implementation:**
```python
import faiss

# Create quantizer (coarse clustering)
quantizer = faiss.IndexFlatIP(512)  # 512 = embedding dimension

# Create IVF index with nlist clusters
# nlist = sqrt(N) is a good starting point
# For 1M vectors: nlist = 1000-2000
nlist = 1000  # Number of clusters
index = faiss.IndexIVFFlat(quantizer, 512, nlist)

# Train index (required before adding vectors)
# Use 10-20% of data for training
training_vectors = embeddings[:100000]  # 100k training samples
index.train(training_vectors)

# Add vectors
index.add(embeddings)

# Search with nprobe (number of clusters to search)
# Higher nprobe = better accuracy, slower search
# nprobe = 10-50 is good for production
index.nprobe = 20  # Search in 20 nearest clusters
```

**Pros:**
- ✅ 10-50x faster than Flat
- ✅ Same memory usage
- ✅ High accuracy (95-99%)
- ✅ Good for production

**Cons:**
- ❌ Requires training phase
- ❌ Slight accuracy loss (configurable)

---

### Option 2: IndexHNSW (Best for Speed)

**Best for:** Systems requiring fastest search (<1ms)

**Characteristics:**
- **Memory:** ~2-3GB (slightly more than Flat)
- **Search Time:** 0.5-2ms (CPU), 0.1-0.5ms (GPU)
- **Accuracy:** 95-99% (approximate)
- **Training:** Not required

**Implementation:**
```python
import faiss

# Create HNSW index
# M = number of connections (16-64, higher = better accuracy, more memory)
# efConstruction = search width during construction (200-400)
# efSearch = search width during query (16-128)
M = 32
index = faiss.IndexHNSWFlat(512, M)
index.hnsw.efConstruction = 200
index.hnsw.efSearch = 64

# Add vectors (no training needed)
index.add(embeddings)
```

**Pros:**
- ✅ Fastest search speed
- ✅ No training required
- ✅ High accuracy

**Cons:**
- ❌ Higher memory usage
- ❌ Slower to add vectors

---

### Option 3: IndexIVFPQ (Best for Memory)

**Best for:** Systems with memory constraints

**Characteristics:**
- **Memory:** ~500MB-1GB (4-8x compression)
- **Search Time:** 2-10ms (CPU)
- **Accuracy:** 90-95% (slight loss)
- **Training:** Required

**Implementation:**
```python
import faiss

quantizer = faiss.IndexFlatIP(512)
nlist = 1000
m = 64  # Number of subquantizers (8, 16, 32, 64)
bits = 8  # Bits per subquantizer (8 is standard)

index = faiss.IndexIVFPQ(quantizer, 512, nlist, m, bits)

# Train
index.train(training_vectors)

# Add
index.add(embeddings)
index.nprobe = 20
```

**Pros:**
- ✅ 4-8x less memory
- ✅ Still fast search

**Cons:**
- ❌ Lower accuracy (90-95%)
- ❌ Requires training

---

## 🏗️ Production Architecture Recommendations

### For 1 Million Known Images:

**Recommended:** **IndexIVFFlat** for KNOWN index

**Configuration:**
```python
# KNOWN Index (1M+ vectors) - Use IVF
quantizer_known = faiss.IndexFlatIP(512)
known_index = faiss.IndexIVFFlat(quantizer_known, 512, nlist=1000)
known_index.nprobe = 20  # Balance speed/accuracy

# UNKNOWN Index (<100k vectors) - Keep Flat (fast enough)
unknown_index = faiss.IndexFlatIP(512)  # Keep simple for dynamic data
```

**Why:**
- KNOWN index: Large, stable, needs speed → IVF
- UNKNOWN index: Small, changes frequently → Flat is fine

---

## 💾 Storage Best Practices

### 1. **Storage Location**

**Recommended:** Fast SSD (NVMe preferred)

**File Structure:**
```
/storage/faiss/
├── known_index.faiss          # Main index file (~2GB for 1M)
├── known_index_backup.faiss   # Backup
├── known_metadata.json        # Metadata (~10-50MB)
├── unknown_index.faiss        # Small (~100MB)
└── unknown_metadata.json      # Small (~1MB)
```

### 2. **Backup Strategy**

**Critical:** Index files are large and expensive to rebuild

**Recommended:**
- **Daily backups** to separate storage (S3, NFS, etc.)
- **Version control:** Keep last 3-7 days of backups
- **Incremental backups:** Only backup when index changes
- **Backup before major operations:** Promotion, merge, bulk load

**Implementation:**
```python
def backup_index(index_path: str, backup_dir: str):
    """Backup index with timestamp"""
    import shutil
    from datetime import datetime
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"index_{timestamp}.faiss")
    
    shutil.copy2(index_path, backup_path)
    logger.info(f"Backed up index to {backup_path}")
```

### 3. **Storage Optimization**

**Compression:**
- FAISS indexes are already binary (not compressible)
- Metadata JSON can be compressed (gzip) - saves ~50%

**Sharding (for 10M+ vectors):**
```python
# Split into multiple indexes
# Example: 10M vectors = 10 indexes of 1M each
indexes = []
for i in range(10):
    index = faiss.IndexIVFFlat(quantizer, 512, nlist=1000)
    indexes.append(index)

# Search all indexes in parallel
results = []
for index in indexes:
    results.extend(index.search(query, k=10))
# Merge and deduplicate results
```

---

## ⚡ Performance Optimization

### 1. **GPU Acceleration**

**For 1M+ vectors, GPU is highly recommended:**

```python
# GPU setup
gpu_resource = faiss.StandardGpuResources()

# Move index to GPU
cpu_index = faiss.IndexIVFFlat(quantizer, 512, nlist=1000)
gpu_index = faiss.index_cpu_to_gpu(gpu_resource, 0, cpu_index)

# 10-50x speedup on GPU
```

**Requirements:**
- NVIDIA GPU with CUDA
- 4GB+ VRAM for 1M vectors
- FAISS GPU support compiled

### 2. **Batch Operations**

**Add vectors in batches:**
```python
# Bad: One at a time
for embedding in embeddings:
    index.add(embedding.reshape(1, -1))

# Good: Batch add
batch_size = 1000
for i in range(0, len(embeddings), batch_size):
    batch = embeddings[i:i+batch_size]
    index.add(batch)
```

### 3. **Memory Management**

**For large indexes:**
- Use memory-mapped files (mmap) for read-only access
- Keep only active index in RAM
- Load indexes on-demand

```python
# Memory-mapped index (read-only)
index = faiss.read_index("index.faiss", faiss.IO_FLAG_MMAP)
```

---

## 🔄 Index Update Strategy

### Current Approach: In-Place Updates

**Problem:** FAISS doesn't support efficient removal/updates

**Solutions:**

### 1. **Append-Only with Periodic Rebuild** (Recommended)

```python
# Add new vectors to index
index.add(new_embeddings)

# Periodically rebuild (weekly/monthly)
# Remove deleted identities from metadata
# Rebuild index from active identities only
def rebuild_index():
    # Load all active identities from database
    active_identities = get_active_identities()
    
    # Rebuild index
    new_index = faiss.IndexIVFFlat(quantizer, 512, nlist=1000)
    new_index.train(training_data)
    
    for identity in active_identities:
        embeddings = get_embeddings(identity.id)
        new_index.add(embeddings)
    
    # Replace old index
    faiss.write_index(new_index, "index.faiss")
```

### 2. **Metadata-Based Removal**

```python
# Mark as removed in metadata (current approach)
# Index still contains vectors but they're ignored
def remove_identity(identity_id: str):
    # Remove from metadata
    faiss_ids = identity_to_faiss[identity_id]
    for faiss_id in faiss_ids:
        del metadata[faiss_id]
    
    # Rebuild periodically to actually remove
```

---

## 📈 Scaling Beyond 1 Million

### For 10M+ Vectors:

1. **Sharding:** Split into multiple indexes
2. **Distributed:** Use multiple servers
3. **Hierarchical:** Coarse index → Fine index
4. **Caching:** Cache frequent queries

### Example Sharding:

```python
class ShardedIndex:
    def __init__(self, n_shards=10):
        self.shards = []
        for i in range(n_shards):
            index = faiss.IndexIVFFlat(quantizer, 512, nlist=1000)
            self.shards.append(index)
    
    def add(self, embedding, shard_id=None):
        if shard_id is None:
            shard_id = hash(embedding) % len(self.shards)
        self.shards[shard_id].add(embedding)
    
    def search(self, query, k=10):
        # Search all shards in parallel
        results = []
        for shard in self.shards:
            results.extend(shard.search(query, k=k))
        # Merge and return top k
        return sorted(results, key=lambda x: x[1], reverse=True)[:k]
```

---

## ✅ Implementation Checklist

### For Production with 1M Known Images:

- [ ] **Switch KNOWN index to IndexIVFFlat**
  - nlist = 1000-2000
  - nprobe = 20-50
  - Train with 100k-200k samples

- [ ] **Keep UNKNOWN index as IndexFlatIP**
  - Small enough, changes frequently

- [ ] **Enable GPU if available**
  - 4GB+ VRAM recommended

- [ ] **Set up daily backups**
  - Backup to separate storage
  - Keep 7 days of backups

- [ ] **Implement periodic rebuild**
  - Weekly/monthly rebuild to remove deleted identities
  - During low-traffic hours

- [ ] **Monitor performance**
  - Track search latency
  - Monitor memory usage
  - Alert on slow queries

- [ ] **Optimize storage**
  - Use fast SSD (NVMe)
  - Compress metadata JSON
  - Consider sharding if >10M

---

## 🚀 Migration Path

### Step 1: Test with Sample Data
```python
# Test IVF with 100k vectors first
test_index = faiss.IndexIVFFlat(quantizer, 512, nlist=500)
test_index.train(training_data[:10000])
test_index.add(test_data[:100000])
# Verify accuracy and speed
```

### Step 2: Migrate KNOWN Index
```python
# Load existing Flat index
old_index = faiss.read_index("known_index.faiss")

# Extract all vectors
vectors = []
for i in range(old_index.ntotal):
    vectors.append(old_index.reconstruct(i))

# Create new IVF index
new_index = faiss.IndexIVFFlat(quantizer, 512, nlist=1000)
new_index.train(vectors[:100000])  # Train
new_index.add(vectors)  # Add all

# Save
faiss.write_index(new_index, "known_index_ivf.faiss")
```

### Step 3: Update Code
```python
# Update _initialize_indexes() in identity_index.py
# Change KNOWN index to IVF
# Keep UNKNOWN as Flat
```

---

## 📊 Performance Benchmarks (Estimated)

### IndexFlatIP (Current):
- 1M vectors: 10-50ms search, 2GB RAM
- 10M vectors: 100-500ms search, 20GB RAM ❌ Too slow

### IndexIVFFlat (Recommended):
- 1M vectors: 1-5ms search, 2GB RAM ✅
- 10M vectors: 5-20ms search, 20GB RAM ✅

### IndexHNSW:
- 1M vectors: 0.5-2ms search, 2-3GB RAM ✅ Fastest
- 10M vectors: 2-10ms search, 20-30GB RAM ✅

### IndexIVFPQ:
- 1M vectors: 2-10ms search, 500MB RAM ✅ Smallest
- 10M vectors: 10-50ms search, 5GB RAM ✅

---

## 🎯 Final Recommendation

**For 1 Million Known Images:**

1. **KNOWN Index:** `IndexIVFFlat` with nlist=1000, nprobe=20
2. **UNKNOWN Index:** Keep `IndexFlatIP` (small, changes frequently)
3. **Storage:** Fast SSD with daily backups
4. **GPU:** Enable if available (10-50x speedup)
5. **Rebuild:** Weekly to remove deleted identities
6. **Monitoring:** Track search latency and memory

**Expected Performance:**
- Search: 1-5ms per query (CPU), 0.5-2ms (GPU)
- Memory: ~2GB for 1M vectors
- Accuracy: 95-99% (configurable via nprobe)
- Storage: ~2GB index file + ~50MB metadata

---

## ⚙️ Configuration

### Environment Variables (.env)

```bash
# Index Type Configuration
KNOWN_INDEX_TYPE=ivf          # Options: flat, ivf, hnsw, ivfpq
UNKNOWN_INDEX_TYPE=flat       # Keep flat for unknown (recommended)

# IVF Configuration (for IndexIVFFlat)
KNOWN_INDEX_NLIST=1000        # Number of clusters (sqrt(N) recommended)
KNOWN_INDEX_NPROBE=20         # Clusters to search (10-50, higher = better accuracy)

# HNSW Configuration (for IndexHNSWFlat)
KNOWN_INDEX_HNSW_M=32         # Connections (16-64)
KNOWN_INDEX_HNSW_EF_CONSTRUCTION=200
KNOWN_INDEX_HNSW_EF_SEARCH=64

# IVFPQ Configuration (for IndexIVFPQ)
KNOWN_INDEX_PQ_M=64           # Subquantizers (8, 16, 32, 64)
KNOWN_INDEX_PQ_BITS=8         # Bits per subquantizer
```

### Migration Steps

1. **Set configuration in .env:**
   ```bash
   KNOWN_INDEX_TYPE=ivf
   KNOWN_INDEX_NLIST=1000
   KNOWN_INDEX_NPROBE=20
   ```

2. **Restart application** - New index type will be used

3. **Load known faces** - System will automatically train index:
   ```bash
   POST /api/admin/identities/load-known-faces
   ```

4. **Verify indexes:**
   ```bash
   GET /api/admin/identities/verify-indexes
   ```

---

## 🔍 Index Verification

Use the verification endpoint to check both indexes:

```bash
curl -X GET "http://localhost:8000/api/admin/identities/verify-indexes" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Response:**
```json
{
  "success": true,
  "verification": {
    "known_index": {
      "faiss_count": 1000000,
      "database_count": 1000000,
      "match": true,
      "issues": []
    },
    "unknown_index": {
      "faiss_count": 5000,
      "database_count": 5000,
      "match": true,
      "issues": []
    },
    "assets_faces": {
      "directory_exists": true,
      "file_count": 1000,
      "loaded_count": 1000
    }
  }
}
```

---

## 📝 Summary

**Your approach is correct!** The system now:

1. ✅ **Loads known faces from `assets/faces`** into Identity system on startup
2. ✅ **Supports multiple index types** (Flat, IVF, HNSW, IVFPQ) via configuration
3. ✅ **Automatically trains** IVF indexes when loading known faces
4. ✅ **Verifies both indexes** match database counts
5. ✅ **Moves embeddings** from UNKNOWN to KNOWN when promoting (fixed)
6. ✅ **Handles 1M+ vectors** efficiently with IVF indexes

**Best Practice:** Use `IndexIVFFlat` for KNOWN index when you have 1M+ images. It provides 10-50x speedup with 95-99% accuracy.

