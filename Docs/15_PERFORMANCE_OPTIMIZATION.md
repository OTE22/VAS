# Performance Optimization Guide
## For 50+ Cameras and 20 Concurrent Users with GPU

This document outlines the optimizations applied to handle high-load scenarios.

## Overview

The system has been optimized to handle:
- **50+ cameras** (pipeline_ids) streaming simultaneously
- **20 concurrent users** accessing the system
- **GPU acceleration** for face recognition processing

## Key Optimizations

### 1. Server Workers (Gunicorn)

**GPU Mode:**
- Workers: **16** (2x CPU cores + 10)
- Worker connections: **2000** per worker
- Max requests: **1000** per worker
- Timeout: **180 seconds**
- Preload app: **Enabled** (shares GPU resources efficiently)

**CPU Mode:**
- Workers: **8** (1.5x CPU cores)
- Worker connections: **1000** per worker
- Max requests: **500** per worker
- Timeout: **120 seconds**

### 2. Database Connection Pool

**GPU Mode:**
- Pool size: **50** connections
- Max overflow: **100** connections
- Total max: **150** connections
- Pool recycle: **3600 seconds** (1 hour)
- Pool pre-ping: **Enabled**

**CPU Mode:**
- Pool size: **30** connections
- Max overflow: **60** connections
- Total max: **90** connections

### 3. Redis Cache

**GPU Mode:**
- Max connections: **100**
- Pool size: **50**
- Local cache size: **50,000** entries

**CPU Mode:**
- Max connections: **50**
- Pool size: **25**
- Local cache size: **20,000** entries

### 4. Processing Queue

**GPU Mode:**
- Max queue size: **5,000** items
- Queue workers: **30** workers
- Batch size: **20** items
- GPU batch size: **32** items (optimal for GPU)
- Max concurrent requests: **300**

**CPU Mode:**
- Max queue size: **2,000** items
- Queue workers: **15** workers
- Batch size: **10** items
- CPU batch size: **10** items
- Max concurrent requests: **150**

### 5. Batch Database Writing

**GPU Mode:**
- Batch write size: **50** detections
- Write interval: **1.0 second**
- Max wait: **5.0 seconds**

**CPU Mode:**
- Batch write size: **25** detections
- Write interval: **2.0 seconds**
- Max wait: **5.0 seconds**

### 6. Face Tracking (Deduplication)

**GPU Mode:**
- Max entries: **5,000** tracked faces
- Max memory: **2,000 MB**
- Cleanup interval: **300 seconds** (5 minutes)

**CPU Mode:**
- Max entries: **2,000** tracked faces
- Max memory: **1,000 MB**
- Cleanup interval: **300 seconds**

### 7. FAISS Face Database

**GPU Mode:**
- Workers: **16** (GPU can handle more parallel searches)
- Uses: **faiss-gpu** for 10-50x faster searches

**CPU Mode:**
- Workers: **8** (CPU parallel processing)
- Uses: **faiss-cpu**

## Performance Expectations

### With GPU:
- **Face Detection**: 3-5x faster than CPU
- **Face Recognition**: 4-6x faster than CPU
- **FAISS Search**: 10-50x faster than CPU
- **Throughput**: Can handle 50+ cameras at 1-2 FPS each
- **Latency**: < 100ms per face detection + recognition

### With CPU:
- **Throughput**: Can handle 20-30 cameras at 1 FPS each
- **Latency**: 200-500ms per face detection + recognition

## Configuration Files

### Docker Compose (GPU)
See: `docker/docker-compose.gpu.yml`

All optimizations are pre-configured in the environment variables.

### Manual Configuration
See: `config.py` for all available settings.

### Auto-Detection
The system automatically detects GPU and applies optimizations via:
- `utils/performance_config.py` - Auto-adjusts configuration
- `utils/gpu_detection.py` - Detects GPU availability

## Monitoring

### Key Metrics to Watch:
1. **Queue Size**: Should stay below 80% of MAX_QUEUE_SIZE
2. **Database Pool**: Should stay below 80% of pool_size
3. **Worker Utilization**: Monitor via `/metrics` endpoint
4. **GPU Utilization**: Use `nvidia-smi` to monitor GPU usage

### Health Checks:
- `/health` - Overall system health
- `/metrics` - Prometheus metrics
- Database pool status logged at startup

## Troubleshooting

### High Queue Size
- Increase `MAX_QUEUE_SIZE`
- Increase `QUEUE_WORKERS`
- Check if GPU is being utilized

### Database Connection Pool Exhausted
- Increase `DB_POOL_SIZE`
- Increase `DB_MAX_OVERFLOW`
- Check database server resources

### High Memory Usage
- Reduce `FACE_TRACKING_MAX_ENTRIES`
- Reduce `CACHE_LOCAL_SIZE`
- Reduce `BATCH_WRITE_SIZE`

### GPU Not Being Used
- Verify GPU is detected: Check logs for "GPU detected"
- Verify `USE_GPU=true` in environment
- Check `nvidia-smi` for GPU availability

## Scaling Recommendations

### For 100+ Cameras:
1. Increase `WORKERS` to 24-32
2. Increase `QUEUE_WORKERS` to 50
3. Increase `DB_POOL_SIZE` to 75
4. Consider multiple GPU instances

### For 50+ Concurrent Users:
1. Increase `MAX_CONCURRENT_REQUESTS` to 500
2. Increase `worker_connections` to 3000
3. Add load balancer (Nginx)
4. Consider horizontal scaling

## Best Practices

1. **Monitor Regularly**: Check metrics dashboard frequently
2. **Gradual Scaling**: Increase settings incrementally
3. **Test Under Load**: Use load testing tools before production
4. **GPU Memory**: Monitor GPU memory usage with `nvidia-smi`
5. **Database Indexes**: Ensure proper indexes on frequently queried columns

