# 50 Cameras Scalability Analysis
> **Vector backend note.** Where this document says *FAISS*, the live
> system uses **PostgreSQL + pgvector**. PostgreSQL is authoritative and
> the index is a disposable acceleration layer — see
> [`70_VECTOR_INDEX_CONTRACT.md`](70_VECTOR_INDEX_CONTRACT.md). The
> surrounding explanation of *what* the index does is still accurate.

## Can the System Handle 50 Concurrent Camera Streams?

### ✅ **YES, with proper configuration!**

The system is **designed** to handle 50+ cameras, but requires **optimized configuration** for production.

---

## 📊 Current Architecture Analysis

### **1. Processing Queue** ✅

**Current Configuration:**
- Max Queue Size: **10,000** items
- Queue Workers: **50** workers
- Max Concurrent: **500** requests
- Pipeline Batching: **5** images per batch

**Capacity Calculation:**
- 50 cameras × 1 FPS = **50 images/second**
- 50 cameras × 5 FPS = **250 images/second** (peak)
- Queue can handle: **10,000 items** = **200 seconds** at 50 FPS
- **✅ SUFFICIENT** for 50 cameras

**Recommendation:**
```bash
MAX_QUEUE_SIZE=20000        # Increase for safety
QUEUE_WORKERS=50           # Keep at 50 (one per camera)
MAX_CONCURRENT_REQUESTS=500 # Sufficient
PIPELINE_BATCH_SIZE=5       # Optimal for GPU
```

---

### **2. Database Connection Pool** ⚠️ **NEEDS ATTENTION**

**Current Configuration:**
- Pool Size: **50** (GPU) / **30** (CPU)
- Max Overflow: **100** (GPU) / **60** (CPU)
- Total Max: **150** (GPU) / **90** (CPU)

**Capacity Calculation:**
- 50 cameras × 1 request/second = **50 concurrent DB operations**
- Each detection: **3-5 DB queries** (Detection, Face, Identity, Embedding)
- Total: **150-250 concurrent queries** at peak
- **⚠️ TIGHT** - May need increase

**Recommendation:**
```bash
# For 50 cameras with GPU
DB_POOL_SIZE=75            # Increase from 50
DB_MAX_OVERFLOW=150        # Increase from 100
# Total: 225 connections (sufficient)
```

**PostgreSQL Configuration:**
```sql
-- In postgresql.conf
max_connections = 300              # Allow 300 total connections
shared_buffers = 4GB               # Increase for better performance
work_mem = 64MB                     # Per-query memory
maintenance_work_mem = 1GB
effective_cache_size = 12GB         # 75% of RAM
```

---

### **3. FAISS Index Thread Safety** ✅ **GOOD**

**Current Implementation:**
- Uses `threading.RLock()` for thread-safe access
- All operations are locked: `with self.lock:`
- **✅ SAFE** for concurrent access

**However, Gunicorn Workers Issue:**
- Each Gunicorn worker is a **separate process**
- Each process loads its own copy of FAISS index
- **Memory:** 2GB × 16 workers = **32GB** (if 1M vectors)
- **Updates:** Index updates in one worker not visible to others

**Solution:**
1. **Use `preload_app = True`** (already configured) - Shares GPU resources
2. **Auto-save interval:** 5 minutes (indexes saved frequently)
3. **Consider:** Shared memory for FAISS indexes (advanced)

**Recommendation:**
```bash
# Keep preload_app = True for GPU efficiency
# Monitor memory usage
# Consider reducing workers if memory constrained
```

---

### **4. Gunicorn Workers** ✅

**Current Configuration:**
- Workers: **16** (GPU) / **8** (CPU)
- Worker Connections: **2000** (GPU) / **1000** (CPU)
- Max Requests: **1000** per worker
- Timeout: **600 seconds** (10 minutes)

**Capacity Calculation:**
- 16 workers × 2000 connections = **32,000 concurrent connections**
- 50 cameras × 1 connection = **50 connections**
- **✅ MORE THAN SUFFICIENT**

**Recommendation:**
```bash
# GPU Mode (Recommended)
WORKERS=16                # Keep at 16
worker_connections=2000   # Keep at 2000

# CPU Mode (If no GPU)
WORKERS=8                 # Keep at 8
worker_connections=1000   # Keep at 1000
```

---

### **5. Memory Usage** ⚠️ **MONITOR CLOSELY**

**Estimated Memory per Component:**

| Component | Memory Usage |
|-----------|-------------|
| FAISS Index (1M vectors) | 2GB |
| Model (w600k_r50) | 500MB |
| Database Pool (150 conn) | 300MB |
| Queue (10k items) | 200MB |
| Workers (16 × 200MB) | 3.2GB |
| **Total (GPU)** | **~6.2GB** |
| **Total (CPU)** | **~5GB** |

**With 16 Workers:**
- Each worker loads FAISS index: **2GB × 16 = 32GB** (if not shared)
- **⚠️ CRITICAL:** Use `preload_app = True` to share memory

**Recommendation:**
- **Minimum RAM:** 16GB (GPU) / 12GB (CPU)
- **Recommended RAM:** 32GB (GPU) / 16GB (CPU)
- **Monitor:** Use `htop` or `docker stats` to track memory

---

### **6. CPU Usage** ✅

**Estimated CPU per Camera:**
- Face Detection: **50-100ms** (CPU) / **10-20ms** (GPU)
- Face Recognition: **20-50ms** (CPU) / **5-10ms** (GPU)
- Database: **5-10ms**
- **Total:** **75-160ms** (CPU) / **15-30ms** (GPU)

**50 Cameras at 1 FPS:**
- CPU Mode: **3.75-8 CPU cores** (50 × 75-160ms)
- GPU Mode: **0.75-1.5 CPU cores** (50 × 15-30ms)

**✅ SUFFICIENT** with modern CPUs

---

### **7. GPU Usage** ✅ (If Available)

**Estimated GPU per Camera:**
- Face Detection: **10-20ms**
- Face Recognition: **5-10ms**
- **Total:** **15-30ms** per image

**50 Cameras at 1 FPS:**
- **0.75-1.5 GPU utilization** (50 × 15-30ms)
- **✅ VERY EFFICIENT** - GPU can handle 100+ cameras

**Recommendation:**
- **Minimum GPU:** NVIDIA GTX 1060 (6GB) or better
- **Recommended:** NVIDIA RTX 3060 (12GB) or better
- **Monitor:** Use `nvidia-smi` to track GPU usage

---

## 🚨 Potential Bottlenecks

### **1. FAISS Index Lock Contention** ⚠️

**Problem:**
- 50 workers accessing same FAISS index
- Lock contention on `self.lock`
- Searches may queue up

**Impact:**
- Search latency: **1-5ms** → **10-50ms** (under load)
- Still acceptable for real-time

**Mitigation:**
- ✅ Already using `RLock` (allows nested locks)
- ✅ Read operations are fast (1-5ms)
- ✅ Write operations are rare (only on new faces)

**Recommendation:**
- Monitor lock contention
- Consider read replicas if needed (advanced)

---

### **2. Database Connection Exhaustion** ⚠️

**Problem:**
- 50 cameras × 3-5 queries = **150-250 concurrent queries**
- Pool size: **50** (may be insufficient)

**Impact:**
- Requests wait for available connection
- Timeout errors if pool exhausted

**Mitigation:**
- ✅ Increase `DB_POOL_SIZE` to **75**
- ✅ Increase `DB_MAX_OVERFLOW` to **150**
- ✅ Use connection pooling in PostgreSQL

**Recommendation:**
```bash
DB_POOL_SIZE=75
DB_MAX_OVERFLOW=150
```

---

### **3. Queue Overflow** ✅ **UNLIKELY**

**Problem:**
- Queue fills up faster than workers can process
- Images dropped

**Impact:**
- Lost detections
- Poor user experience

**Mitigation:**
- ✅ Large queue size (10,000)
- ✅ 50 workers processing
- ✅ Batching reduces overhead

**Recommendation:**
- Monitor queue size
- Increase `MAX_QUEUE_SIZE` if needed

---

### **4. Memory Exhaustion** ⚠️

**Problem:**
- 16 workers × 2GB FAISS = **32GB** (if not shared)
- System runs out of memory

**Impact:**
- OOM (Out of Memory) kills
- System crashes

**Mitigation:**
- ✅ Use `preload_app = True` (shares memory)
- ✅ Monitor memory usage
- ✅ Reduce workers if needed

**Recommendation:**
- **Minimum:** 16GB RAM
- **Recommended:** 32GB RAM
- Monitor with `docker stats` or `htop`

---

## 📋 Recommended Configuration for 50 Cameras

### **.env File:**

```bash
# Server Configuration
WORKERS=16                    # GPU: 16, CPU: 8
HOST=0.0.0.0
PORT=8000

# Database Pool (INCREASED)
DB_POOL_SIZE=75              # Increased from 50
DB_MAX_OVERFLOW=150          # Increased from 100

# Queue Configuration
MAX_QUEUE_SIZE=20000         # Increased from 10000
QUEUE_WORKERS=50             # One per camera
MAX_CONCURRENT_REQUESTS=500  # Keep at 500
PIPELINE_BATCH_SIZE=5        # Optimal for GPU

# GPU Configuration
USE_GPU=true                  # Enable if available
PIPELINE_BATCH_SIZE=20       # GPU_BATCH_SIZE is not read by anything

# FAISS Index
KNOWN_INDEX_TYPE=ivf         # Use IVF for 1M+ vectors
# KNOWN_INDEX_NLIST / KNOWN_INDEX_NPROBE are NOT settings. They belonged to
# the deleted FAISS IdentityIndexService. pgvector tuning uses
# PGVECTOR_HNSW_M / _EF_CONSTRUCTION / _EF_SEARCH — see 70_VECTOR_INDEX_CONTRACT.md.


# Memory Management
FACE_TRACKING_MAX_ENTRIES=5000
FACE_TRACKING_MAX_MEMORY_MB=2000
```

---

## 🧪 Load Testing Recommendations

### **Test Scenario:**
1. **50 cameras** streaming at **1 FPS** each
2. **Peak load:** 50 cameras at **5 FPS** each
3. **Duration:** 1 hour

### **Metrics to Monitor:**

```bash
# Queue Metrics
curl http://localhost:8000/api/stats/queue
# Check: queue_size, processing_count, total_processed

# Database Metrics
# Check: connection pool usage, query latency

# System Metrics
docker stats <container_name>
# Check: CPU, Memory, GPU usage

# FAISS Index Metrics
# Check: search latency, lock contention
```

### **Expected Results:**
- ✅ Queue size: **< 1000** (under normal load)
- ✅ Processing latency: **< 100ms** per image
- ✅ Database connections: **< 150** (within pool)
- ✅ Memory usage: **< 16GB** (with preload_app)
- ✅ CPU usage: **< 50%** (GPU mode) / **< 80%** (CPU mode)
- ✅ GPU usage: **< 30%** (if GPU available)

---

## ✅ Summary: Can It Handle 50 Cameras?

### **YES, with these conditions:**

1. ✅ **Proper Configuration:**
   - Increase `DB_POOL_SIZE` to **75**
   - Increase `MAX_QUEUE_SIZE` to **20000**
   - Use `preload_app = True`

2. ✅ **Sufficient Resources:**
   - **RAM:** 16GB minimum, 32GB recommended
   - **CPU:** 8+ cores (GPU mode) / 16+ cores (CPU mode)
   - **GPU:** Recommended (10x faster)

3. ✅ **Database Optimization:**
   - PostgreSQL: `max_connections = 300`
   - Proper indexing on frequently queried columns
   - Connection pooling enabled

4. ✅ **Monitoring:**
   - Monitor queue size, DB connections, memory
   - Set up alerts for bottlenecks

### **Current System Status:**

| Component | Status | Notes |
|-----------|--------|-------|
| Queue | ✅ Ready | 10k size sufficient |
| Workers | ✅ Ready | 50 workers sufficient |
| Database | ⚠️ Needs Tuning | Increase pool size |
| FAISS | ✅ Ready | Thread-safe, efficient |
| Memory | ⚠️ Monitor | Use preload_app |
| GPU | ✅ Ready | Handles 100+ cameras |

### **Action Items:**

1. **Increase database pool size** (critical)
2. **Monitor memory usage** (important)
3. **Test with 50 cameras** (validation)
4. **Set up monitoring** (production)

---

## 🚀 Next Steps

1. **Update Configuration:**
   ```bash
   # Edit .env file with recommended values
   DB_POOL_SIZE=75
   DB_MAX_OVERFLOW=150
   MAX_QUEUE_SIZE=20000
   ```

2. **Restart Services:**
   ```bash
   docker compose -f docker/docker-compose.cpu.yml restart
   ```

3. **Monitor Performance:**
   ```bash
   # Watch queue stats
   watch -n 1 'curl -s http://localhost:8000/api/stats/queue | jq'
   
   # Watch system resources
   docker stats
   ```

4. **Load Test:**
   - Start with 10 cameras
   - Gradually increase to 50
   - Monitor metrics at each step

---

## 📚 Additional Resources

- **Performance Optimization Guide:** `Docs/15_PERFORMANCE_OPTIMIZATION.md`
- **FAISS Scaling Guide:** `Docs/70_VECTOR_INDEX_CONTRACT.md`
- **Database Tuning:** PostgreSQL documentation

---

**Conclusion:** The system **CAN handle 50 cameras** with proper configuration and sufficient resources. The main areas to focus on are **database connection pooling** and **memory management**.

