# Face Recognition Service - Production Deployment Guide

## 🚀 Production-Ready Features

This optimized version handles **100+ concurrent webhook requests** and runs **24/7** with:

- ✅ **Async Processing**: Non-blocking I/O with asyncio and FastAPI
- ✅ **Worker Pool**: 10+ configurable background workers
- ✅ **PostgreSQL Database**: Persistent storage for all detections
- ✅ **Redis Caching**: High-performance in-memory caching
- ✅ **Queue System**: 1000+ request queue with overflow handling
- ✅ **Concurrency Control**: Semaphore-based request limiting
- ✅ **Prometheus Metrics**: Real-time monitoring and alerting
- ✅ **WebSocket Dashboard**: Live updates to frontend
- ✅ **Health Checks**: Automatic service monitoring
- ✅ **Graceful Shutdown**: Proper resource cleanup
- ✅ **Docker Ready**: Full containerization support

---

## 📊 Architecture Overview

```
┌─────────────────┐
│  Webhook Calls  │ (100+ concurrent)
└────────┬────────┘
         │
    ┌────▼─────┐
    │  FastAPI │ (Async handlers)
    └────┬─────┘
         │
    ┌────▼─────────┐
    │ Queue System │ (1000 items)
    └────┬─────────┘
         │
    ┌────▼────────────────┐
    │  Worker Pool (10+)  │ (Concurrent processing)
    └────┬────────────────┘
         │
    ┌────▼──────────────────────────┐
    │  Face Detection & Recognition │
    │    (SCRFD + ArcFace)          │
    └────┬──────────────────────────┘
         │
    ┌────▼─────────┬──────────────┐
    │  PostgreSQL  │    Redis     │
    │  (Persistent)│   (Cache)    │
    └──────────────┴──────────────┘
         │
    ┌────▼────────────┐
    │  WebSocket      │ (Real-time dashboard)
    │  Broadcast      │
    └─────────────────┘
```

---

## 🔧 Installation

### Option 1: Docker Deployment (Recommended for Production)

1. **Clone and setup:**
```bash
cd /opt
git clone <your-repo> face-recognition
cd face-recognition
```

2. **Configure environment:**
```bash
cp .env.example .env
nano .env  # Edit configuration
```

3. **Download model weights:**
```bash
bash scripts/setup/download.sh
```

4. **Add face images:**
```bash
# Place images in assets/faces/
# Example: assets/faces/john_doe.jpg
cp /path/to/faces/* assets/faces/
```

5. **Start services:**
```bash
docker-compose up -d
```

6. **Check logs:**
```bash
docker-compose logs -f face_recognition
```

7. **Access dashboard:**
- API: http://localhost:8000
- Dashboard: http://localhost:8000/dashboard
- Metrics: http://localhost:9090/metrics
- Grafana: http://localhost:3000 (admin/admin)

---

### Option 2: Manual Installation

1. **Install dependencies:**
```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y python3.10 python3-venv postgresql-15 redis-server

# macOS
brew install python@3.10 postgresql@15 redis
```

2. **Setup database:**
```bash
# PostgreSQL
sudo -u postgres psql
CREATE DATABASE face_recognition;
CREATE USER face_user WITH PASSWORD 'secure_password';
GRANT ALL PRIVILEGES ON DATABASE face_recognition TO face_user;
\q

# Update DATABASE_URL in .env
```

3. **Install Python dependencies:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

4. **Download models:**
```bash
bash scripts/setup/download.sh
```

5. **Start application:**
```bash
bash start_production.sh
```

---

## ⚙️ Configuration

### Environment Variables (.env)

```bash
# Core Settings
QUEUE_WORKERS=10              # Number of background workers
MAX_QUEUE_SIZE=1000           # Maximum queue size
MAX_CONCURRENT_REQUESTS=100   # Concurrent request limit

# Database
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/face_recognition
DB_POOL_SIZE=20              # Connection pool size
DB_MAX_OVERFLOW=40           # Max overflow connections

# Redis
REDIS_URL=redis://localhost:6379/0
REDIS_MAX_CONNECTIONS=50

# Face Recognition
SIMILARITY_THRESHOLD=0.4     # Face matching threshold (0.0-1.0)
CONFIDENCE_THRESHOLD=0.5     # Detection confidence threshold

# Storage
SAVE_IMAGES=true             # Save received images to disk
MAX_STORAGE_GB=50           # Maximum storage usage
```

---

## 🔄 Running as System Service (24/7)

### Systemd Service

1. **Create service file:**
```bash
sudo nano /etc/systemd/system/face-recognition.service
```

2. **Paste configuration:**
```ini
[Unit]
Description=Face Recognition Service
After=network.target postgresql.service redis.service

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/face-recognition
Environment="PATH=/opt/face-recognition/venv/bin"
EnvironmentFile=/opt/face-recognition/.env

ExecStart=/opt/face-recognition/venv/bin/uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 4

Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

3. **Enable and start:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable face-recognition
sudo systemctl start face-recognition
sudo systemctl status face-recognition
```

4. **View logs:**
```bash
sudo journalctl -u face-recognition -f
```

---

## 📡 API Endpoints

### Webhook (Main Endpoint)
```bash
POST /webhook/{pipeline_id}

# Example:
curl -X POST http://localhost:8000/webhook/pipeline_001 \
  -H "Content-Type: application/json" \
  -d '{
    "images": ["data:image/jpeg;base64,..."],
    "predictions": [
      {
        "class_name": "person",
        "bbox": [100, 100, 300, 400],
        "confidence": 0.95
      }
    ]
  }'

# Response:
{
  "status": "queued",
  "pipeline_id": "pipeline_001",
  "queued": 1,
  "dropped": 0
}
```

### Get Detections
```bash
# All detections
GET /api/detections?limit=100&offset=0

# Pipeline-specific
GET /api/detections/pipeline_001

# Response:
[
  {
    "pipeline_id": "pipeline_001",
    "timestamp": "2024-12-16T10:30:00",
    "processing_time_ms": 45.2,
    "faces": [
      {
        "name": "john_doe",
        "similarity": 0.87,
        "image": "base64_encoded_face_image"
      }
    ]
  }
]
```

### System Stats
```bash
GET /api/stats

# Response:
{
  "pipelines": 5,
  "total_detections": 1523,
  "total_faces": 2847,
  "queue": {
    "queue_size": 12,
    "processing": 8,
    "total_received": 15234,
    "total_processed": 15102,
    "total_skipped": 120
  }
}
```

### Health Check
```bash
GET /health

# Response:
{
  "status": "healthy",
  "version": "3.0.0",
  "queue_size": 5,
  "processing": 3
}
```

---

## 📊 Monitoring

### Prometheus Metrics

Available at: `http://localhost:9090/metrics`

Key metrics:
- `face_recognition_requests_total` - Total requests by pipeline
- `face_recognition_processing_seconds` - Processing time histogram
- `face_recognition_queue_size` - Current queue size
- `face_recognition_faces_detected_total` - Faces detected by name
- `face_recognition_active_pipelines` - Active pipeline count

### Grafana Dashboard

1. Access: http://localhost:3000
2. Login: admin/admin
3. Add Prometheus data source: http://prometheus:9090
4. Import dashboard or create custom panels

---

## 🎯 Performance Optimization

### Handling 100+ Concurrent Requests

**Queue System:**
- Max queue size: 1000 requests
- Worker pool: 10 concurrent processors
- Semaphore limit: 100 concurrent operations
- Drop-on-full policy prevents memory overflow

**Database Optimization:**
- Connection pooling (20 base + 40 overflow)
- Async operations (non-blocking I/O)
- Indexed queries for fast retrieval
- Batch inserts for efficiency

**Model Optimization:**
- Singleton pattern (load once)
- Thread-safe operations
- Efficient ONNX runtime
- FAISS vector search

**System Tuning:**
```bash
# Increase file descriptor limits
ulimit -n 65536

# PostgreSQL tuning
max_connections = 200
shared_buffers = 2GB
effective_cache_size = 6GB
work_mem = 16MB

# Redis tuning
maxmemory 2gb
maxmemory-policy allkeys-lru
```

---

## 🔍 Troubleshooting

### Queue Full Errors
```bash
# Increase queue size
MAX_QUEUE_SIZE=2000

# Add more workers
QUEUE_WORKERS=20
```

### Database Connection Issues
```bash
# Check PostgreSQL status
sudo systemctl status postgresql

# Check connections
psql -U face_user -d face_recognition -c "SELECT count(*) FROM pg_stat_activity;"

# Increase pool size
DB_POOL_SIZE=30
DB_MAX_OVERFLOW=60
```

### High Memory Usage
```bash
# Monitor
docker stats face_recognition_api

# Reduce workers if needed
QUEUE_WORKERS=5

# Enable Redis eviction
redis-cli CONFIG SET maxmemory-policy allkeys-lru
```

### Slow Processing
```bash
# Check model inference time
# Consider using GPU:
pip install onnxruntime-gpu

# Reduce image quality before sending
# Lower detection threshold
CONFIDENCE_THRESHOLD=0.6
```

---

## 📦 Database Schema

```sql
-- Pipelines
CREATE TABLE pipelines (
    id SERIAL PRIMARY KEY,
    pipeline_id VARCHAR(255) UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    total_detections INT DEFAULT 0,
    is_active INT DEFAULT 1
);

-- Detections
CREATE TABLE detections (
    id SERIAL PRIMARY KEY,
    uuid VARCHAR(36) UNIQUE,
    pipeline_id VARCHAR(255) REFERENCES pipelines(pipeline_id),
    timestamp TIMESTAMP DEFAULT NOW(),
    image_path VARCHAR(512),
    processing_time_ms FLOAT
);

-- Faces
CREATE TABLE faces (
    id SERIAL PRIMARY KEY,
    detection_id INT REFERENCES detections(id),
    name VARCHAR(255) NOT NULL,
    similarity FLOAT NOT NULL,
    face_image_base64 TEXT
);
```

---

## 🔐 Security Recommendations

1. **Change default passwords** in .env
2. **Enable SSL/TLS** for production
3. **Use firewall** to restrict access
4. **Regular backups** of PostgreSQL
5. **Monitor logs** for suspicious activity
6. **Rate limiting** per IP (Nginx/CloudFlare)
7. **API authentication** (add JWT tokens)

---

## 📈 Scaling

### Horizontal Scaling

**Load Balancer:**
```nginx
upstream face_recognition {
    server 10.0.1.10:8000 weight=1;
    server 10.0.1.11:8000 weight=1;
    server 10.0.1.12:8000 weight=1;
}

server {
    listen 80;
    location / {
        proxy_pass http://face_recognition;
    }
}
```

**Shared Database:**
- All instances connect to same PostgreSQL
- Redis for shared caching
- NFS/S3 for shared storage

---

## 📝 License

Production-optimized Face Recognition Service v3.0

---

## 🆘 Support

For issues:
1. Check logs: `docker-compose logs -f`
2. Verify health: `curl http://localhost:8000/health`
3. Monitor metrics: http://localhost:9090/metrics
4. Check queue stats: `curl http://localhost:8000/api/stats`

---

**Ready for 100+ concurrent requests, 24/7 operation! 🚀**
