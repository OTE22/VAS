# Quick Start Deployment Guide

## 🚀 Deploy Face Recognition Service in 10 Minutes

This guide will get your optimized Face Recognition Service v4.0 running in production.

---

## Prerequisites

- Ubuntu 20.04+ / Debian 11+ / RHEL 8+
- Python 3.9+
- PostgreSQL 12+
- Redis 6.0+ (recommended)
- 4+ CPU cores
- 8+ GB RAM
- 50+ GB disk space

---

## Step 1: Install System Dependencies

### Ubuntu/Debian

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and dependencies
sudo apt install -y \
    python3.11 \
    python3.11-venv \
    python3-pip \
    postgresql \
    postgresql-contrib \
    redis-server \
    nginx \
    build-essential \
    libpq-dev \
    git

# Install OpenCV dependencies
sudo apt install -y \
    libopencv-dev \
    libgl1-mesa-glx \
    libglib2.0-0
```

### RHEL/CentOS

```bash
# Install EPEL
sudo dnf install -y epel-release

# Install dependencies
sudo dnf install -y \
    python3.11 \
    python3-pip \
    postgresql-server \
    postgresql-contrib \
    redis \
    nginx \
    gcc \
    gcc-c++ \
    make
```

---

## Step 2: Setup PostgreSQL

```bash
# Start PostgreSQL
sudo systemctl start postgresql
sudo systemctl enable postgresql

# Create database and user
sudo -u postgres psql << EOF
CREATE DATABASE face_recognition;
CREATE USER postgres WITH ENCRYPTED PASSWORD 'admin';
GRANT ALL PRIVILEGES ON DATABASE face_recognition TO postgres;
ALTER USER postgres CREATEDB;
\q
EOF

# Test connection
psql -h localhost -U postgres -d face_recognition -c "SELECT 1;"
```

---

## Step 3: Setup Redis

```bash
# Start Redis
sudo systemctl start redis
sudo systemctl enable redis

# Test Redis
redis-cli ping
# Should return: PONG

# Configure Redis for production (optional)
sudo nano /etc/redis/redis.conf
# Set: maxmemory 2gb
# Set: maxmemory-policy allkeys-lru

sudo systemctl restart redis
```

---

## Step 4: Setup Application

```bash
# Create application directory
sudo mkdir -p /opt/face-recognition
sudo chown $USER:$USER /opt/face-recognition
cd /opt/face-recognition

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Create necessary directories
mkdir -p logs storage weights assets/faces database/face_database
```

---

## Step 5: Configure Environment

```bash
# Copy and edit .env file
cp .env.example .env
nano .env
```

**Edit the following values:**

```ini
# Database
DATABASE_URL=postgresql+asyncpg://postgres:admin@localhost:5432/face_recognition

# Redis (if available)
REDIS_URL=redis://localhost:6379/0

# Storage
STORAGE_DIR=/opt/face-recognition/storage
FACES_DIR=/opt/face-recognition/assets/faces
DB_PATH=/opt/face-recognition/database/face_database

# Models
DETECTION_MODEL=/opt/face-recognition/weights/det_10g.onnx
RECOGNITION_MODEL=/opt/face-recognition/weights/w600k_r50.onnx

# Workers (adjust based on CPU cores)
WORKERS=8
QUEUE_WORKERS=10

# Data Retention
DATA_RETENTION_DAYS=30
CLEANUP_INTERVAL_HOURS=24
```

---

## Step 6: Add Face Recognition Models

```bash
# Download or copy your ONNX models
# Place in /opt/face-recognition/weights/

# Example structure:
# weights/
#   ├── det_10g.onnx         # Detection model
#   └── w600k_r50.onnx       # Recognition model
```

---

## Step 7: Add Known Faces

```bash
# Add face images to assets/faces/
# Filename should be the person's name
# Example:
#   assets/faces/john_doe.jpg
#   assets/faces/jane_smith.jpg

# The system will automatically build the face database on first run
```

---

## Step 8: Test the Application

```bash
# Activate virtual environment
source venv/bin/activate

# Test with Uvicorn (development)
uvicorn backend.main:app --host 0.0.0.0 --port 8000

# In another terminal, test health endpoint
curl http://localhost:8000/health

# Expected response:
# {
#   "status": "healthy",
#   "version": "4.0.0",
#   "components": {...}
# }
```

**Press Ctrl+C to stop the test server.**

---

## Step 9: Setup Gunicorn Service

### Create systemd service

```bash
sudo nano /etc/systemd/system/face-recognition.service
```

**Paste this configuration:**

```ini
[Unit]
Description=Face Recognition Service v4.0
After=network.target postgresql.service redis.service
Wants=postgresql.service redis.service

[Service]
Type=notify
User=www-data
Group=www-data
WorkingDirectory=/opt/face-recognition
Environment="PATH=/opt/face-recognition/venv/bin"
EnvironmentFile=/opt/face-recognition/.env

ExecStart=/opt/face-recognition/venv/bin/gunicorn backend.main:app -c gunicorn.conf.py

Restart=always
RestartSec=5
StartLimitInterval=0

LimitNOFILE=65535
LimitNPROC=4096

NoNewPrivileges=true
PrivateTmp=true

StandardOutput=journal
StandardError=journal
SyslogIdentifier=face-recognition

KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

### Set permissions

```bash
# Create www-data user if doesn't exist
sudo useradd -r -s /bin/false www-data || true

# Set ownership
sudo chown -R www-data:www-data /opt/face-recognition

# Set permissions
sudo chmod -R 755 /opt/face-recognition
sudo chmod -R 777 /opt/face-recognition/storage
sudo chmod -R 777 /opt/face-recognition/logs
```

### Enable and start service

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable service
sudo systemctl enable face-recognition

# Start service
sudo systemctl start face-recognition

# Check status
sudo systemctl status face-recognition

# View logs
sudo journalctl -u face-recognition -f
```

---

## Step 10: Setup Nginx Reverse Proxy

```bash
# Create Nginx configuration
sudo nano /etc/nginx/sites-available/face-recognition
```

**Paste this configuration:**

```nginx
upstream face_recognition {
    server 127.0.0.1:8000;
    keepalive 32;
}

server {
    listen 80;
    server_name your-domain.com;  # Change this

    client_max_body_size 50M;

    location / {
        proxy_pass http://face_recognition;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Connection "";

        proxy_connect_timeout 120s;
        proxy_send_timeout 120s;
        proxy_read_timeout 120s;
    }

    location /ws {
        proxy_pass http://face_recognition;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;

        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location /health {
        proxy_pass http://face_recognition;
        access_log off;
    }
}
```

**Enable the site:**

```bash
# Create symlink
sudo ln -s /etc/nginx/sites-available/face-recognition /etc/nginx/sites-enabled/

# Test configuration
sudo nginx -t

# Restart Nginx
sudo systemctl restart nginx
```

---

## Step 11: Setup SSL (Optional but Recommended)

```bash
# Install Certbot
sudo apt install -y certbot python3-certbot-nginx

# Get SSL certificate
sudo certbot --nginx -d your-domain.com

# Auto-renewal is set up automatically
# Test renewal
sudo certbot renew --dry-run
```

---

## Step 12: Setup Firewall

```bash
# Install UFW
sudo apt install -y ufw

# Allow SSH (important!)
sudo ufw allow ssh

# Allow HTTP and HTTPS
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Enable firewall
sudo ufw enable

# Check status
sudo ufw status
```

---

## Step 13: Verify Deployment

### Check all services

```bash
# Check PostgreSQL
sudo systemctl status postgresql

# Check Redis
sudo systemctl status redis

# Check Face Recognition Service
sudo systemctl status face-recognition

# Check Nginx
sudo systemctl status nginx
```

### Test endpoints

```bash
# Health check
curl http://localhost/health

# Stats
curl http://localhost/api/stats

# Dashboard
curl http://localhost/dashboard

# Metrics
curl http://localhost/metrics
```

### Test webhook

```bash
curl -X POST http://localhost/webhook/test-pipeline \
  -H "Content-Type: application/json" \
  -d '{
    "image": "base64_encoded_image_here",
    "results": {
      "predictions": [
        {
          "class_name": "face",
          "bbox": [100, 100, 200, 200],
          "confidence": 0.95
        }
      ]
    }
  }'
```

---

## 📊 Monitoring

### View logs

```bash
# Application logs
sudo journalctl -u face-recognition -f

# Nginx access logs
sudo tail -f /var/log/nginx/access.log

# Nginx error logs
sudo tail -f /var/log/nginx/error.log

# Application logs (if file logging enabled)
tail -f /opt/face-recognition/logs/app.log
```

### Check metrics

```bash
# System stats
curl http://localhost/api/stats | jq

# Circuit breaker status
curl http://localhost/api/circuit-breaker/status | jq

# Prometheus metrics
curl http://localhost/metrics
```

---

## 🔧 Common Commands

```bash
# Restart service
sudo systemctl restart face-recognition

# View real-time logs
sudo journalctl -u face-recognition -f

# Graceful reload (zero downtime)
sudo systemctl reload face-recognition

# Stop service
sudo systemctl stop face-recognition

# Start service
sudo systemctl start face-recognition

# Manual cleanup
curl -X POST http://localhost/api/cleanup/manual
```

---

## 🎯 Performance Tuning

### For High Traffic

Edit `.env`:
```ini
WORKERS=16
QUEUE_WORKERS=20
DB_POOL_SIZE=30
DB_MAX_OVERFLOW=70
BATCH_WRITE_SIZE=20
MAX_CONCURRENT_REQUESTS=200
```

### For Limited Resources

Edit `.env`:
```ini
WORKERS=4
QUEUE_WORKERS=5
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
BATCH_WRITE_SIZE=5
SAVE_IMAGES=false
```

---

## 🐛 Troubleshooting

### Service won't start

```bash
# Check logs
sudo journalctl -u face-recognition -n 50

# Check permissions
ls -la /opt/face-recognition

# Test manually
cd /opt/face-recognition
source venv/bin/activate
gunicorn backend.main:app -c gunicorn.conf.py
```

### Database connection errors

```bash
# Test database connection
psql -h localhost -U postgres -d face_recognition -c "SELECT 1;"

# Check DATABASE_URL in .env
cat .env | grep DATABASE_URL
```

### Redis connection errors

```bash
# Check Redis
redis-cli ping

# Check Redis URL
cat .env | grep REDIS_URL
```

### High memory usage

```bash
# Check memory
free -h

# Reduce workers
# Edit .env: WORKERS=4

# Restart service
sudo systemctl restart face-recognition
```

---

## 📚 Next Steps

1. **Setup Monitoring**: Install Prometheus + Grafana
2. **Setup Backups**: Schedule PostgreSQL backups
3. **Load Testing**: Test with realistic traffic
4. **Optimize**: Tune based on your workload
5. **Scale**: Add more instances with load balancer

---

## 📖 Documentation

- [Full Optimizations Guide](OPTIMIZATIONS.md)
- [Migration Guide](MIGRATION_GUIDE.md)
- [Production Servers Guide](PRODUCTION_SERVERS.md)
- [API Documentation](http://your-domain.com/docs)

---

## ✅ Deployment Complete!

Your Face Recognition Service v4.0 is now running in production mode with:

✅ Gunicorn + Uvicorn for high performance
✅ PostgreSQL with optimized connection pooling
✅ Redis caching for faster lookups
✅ Automatic data retention and cleanup
✅ Circuit breaker for resilience
✅ Nginx reverse proxy with SSL
✅ Systemd process management
✅ Comprehensive monitoring

**Service URL:** `http://your-domain.com` or `https://your-domain.com`
**Dashboard:** `http://your-domain.com/dashboard`
**API Docs:** `http://your-domain.com/docs`
**Metrics:** `http://your-domain.com/metrics`
