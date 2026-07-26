"""
Production-Ready Face Recognition Service v1.0.0 - OPTIMIZED & FIXED
Features:
- Face tracking to avoid re-identifying same faces
- Only saves detected faces (not full frames)
- Uses helper functions from helpers.py
- Resource-optimized for production
- Real-time updates only for new faces
- All critical fixes applied
"""

import os
import cv2
import asyncio
import logging
import base64
import time
import hashlib
import numpy as np
import uuid
import re
import json
from datetime import datetime, timedelta
from typing import Dict, Optional, Any, Set, List, Tuple
from contextlib import asynccontextmanager
from collections import defaultdict, deque
from enum import Enum
import random
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, BackgroundTasks, File, UploadFile, Form
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, delete, insert, update, text
from sqlalchemy.orm import selectinload
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from functools import lru_cache, wraps

# Redis imports
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# System metrics imports
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

from config import settings
from db_connection import db_manager, get_db
from db_models import Pipeline, Detection, Face, SystemMetrics
from database import FaceDatabase
from models import SCRFD, ArcFace

# Import helper functions
from utils.helpers import (
    extract_images_from_payload,
    face_alignment,
    compute_similarity,
    draw_bbox_info,
    reference_alignment
)

from utils.logging import setup_logging
from chatbot.chatbot import router as chatbot_router
# Setup logging - unified to logs/app.log
setup_logging(log_to_file=True)  # Enable file logging to logs/app.log
logger = logging.getLogger(__name__)

# Create storage directory
os.makedirs(settings.STORAGE_DIR, exist_ok=True)

# Configuration - Load from settings
DATA_RETENTION_DAYS = settings.DATA_RETENTION_DAYS
CLEANUP_INTERVAL_HOURS = settings.CLEANUP_INTERVAL_HOURS
BATCH_WRITE_SIZE = settings.BATCH_WRITE_SIZE
CACHE_ENABLED = REDIS_AVAILABLE and settings.REDIS_URL is not None

# Face tracking configuration - Load from settings
FACE_TRACKING_ENABLED = settings.FACE_TRACKING_ENABLED
FACE_TRACKING_WINDOW_SECONDS = settings.FACE_TRACKING_WINDOW_SECONDS
FACE_TRACKING_MAX_ENTRIES = settings.FACE_TRACKING_MAX_ENTRIES
FACE_TRACKING_SIMILARITY_THRESHOLD = settings.FACE_TRACKING_SIMILARITY_THRESHOLD
FACE_TRACKING_MAX_MEMORY_MB = settings.FACE_TRACKING_MAX_MEMORY_MB if hasattr(settings, 'FACE_TRACKING_MAX_MEMORY_MB') else 500

# Security configuration
MAX_FILE_SIZE = settings.MAX_FILE_SIZE if hasattr(settings, 'MAX_FILE_SIZE') else 10 * 1024 * 1024  # 10MB
ALLOWED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
# =====================================================
# Prometheus Metrics (ALL IN ONE PLACE)
# =====================================================

def initialize_metrics():
    """Initialize all Prometheus metrics with duplicate protection"""
    global metrics_requests_total, metrics_processing_time, metrics_queue_size
    global metrics_faces_detected, metrics_faces_skipped, metrics_faces_batch_skipped
    global metrics_active_pipelines, metrics_cache_hits, metrics_cache_misses
    global metrics_db_operations, metrics_cleanup_operations, metrics_websocket_connections
    global metrics_worker_active, metrics_cache_hit_rate, metrics_cache_size
    global metrics_cache_write_behind, metrics_cache_circuit_state

    # Helper function to safely create metrics
    def safe_metric(metric_class, name, documentation, **kwargs):
        try:
            return metric_class(name, documentation, **kwargs)
        except ValueError as e:
            if "Duplicated timeseries" in str(e):
                # Get existing metric
                from prometheus_client import REGISTRY
                return REGISTRY.get(name)
            else:
                raise

    # Initialize all metrics
    metrics_requests_total = safe_metric(
        Counter, 'face_recognition_requests_total', 'Total webhook requests', labelnames=['pipeline_id', 'status']
    )
    metrics_processing_time = safe_metric(
        Histogram, 'face_recognition_processing_seconds', 'Processing time in seconds'
    )
    metrics_queue_size = safe_metric(
        Gauge, 'face_recognition_queue_size', 'Current queue size'
    )
    metrics_faces_detected = safe_metric(
        Counter, 'face_recognition_faces_detected_total', 'Total faces detected', labelnames=['name']
    )
    metrics_faces_skipped = safe_metric(
        Counter, 'face_recognition_faces_skipped_total', 'Faces skipped (already tracked)', labelnames=['name']
    )
    metrics_faces_batch_skipped = safe_metric(
        Counter, 'face_recognition_batch_duplicates_total', 'Faces skipped (duplicate in same batch)', labelnames=['name']
    )
    metrics_active_pipelines = safe_metric(
        Gauge, 'face_recognition_active_pipelines', 'Number of active pipelines'
    )
    metrics_cache_hits = safe_metric(
        Counter, 'face_recognition_cache_hits_total', 'Cache hits'
    )
    metrics_cache_misses = safe_metric(
        Counter, 'face_recognition_cache_misses_total', 'Cache misses'
    )
    metrics_db_operations = safe_metric(
        Histogram, 'face_recognition_db_operations_seconds', 'Database operation time'
    )
    metrics_cleanup_operations = safe_metric(
        Counter, 'face_recognition_cleanup_total', 'Total cleanup operations'
    )
    metrics_websocket_connections = safe_metric(
        Gauge, 'face_recognition_websocket_connections', 'Active WebSocket connections'
    )
    metrics_worker_active = safe_metric(
        Gauge, 'face_recognition_worker_active', 'Active worker count', labelnames=['worker_id']
    )
    metrics_cache_hit_rate = safe_metric(
        Gauge, 'face_recognition_cache_hit_rate', 'Cache hit rate percentage', labelnames=['type']
    )
    metrics_cache_size = safe_metric(
        Gauge, 'face_recognition_cache_size', 'Cache size', labelnames=['type']
    )
    metrics_cache_write_behind = safe_metric(
        Counter, 'face_recognition_cache_write_behind_total', 'Write-behind operations'
    )
    metrics_cache_circuit_state = safe_metric(
        Gauge, 'face_recognition_cache_circuit_state', 'Cache circuit breaker state (0=closed, 1=open, 2=half_open)'
    )

# Call this function at module level
initialize_metrics()
#==================================
# Helper Functions
# =====================================================
def validate_pipeline_id(pipeline_id: str) -> str:
    """Validate pipeline ID format"""
    if not re.match(r'^[a-zA-Z0-9_-]{3,100}$', pipeline_id):
        raise HTTPException(status_code=400, detail="Invalid pipeline ID format. Must be 3-100 characters, alphanumeric, dashes, or underscores.")
    return pipeline_id

def validate_image_content(image_bytes: bytes) -> bool:
    """Validate that bytes contain a valid image"""
    try:
        img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        return img is not None
    except:
        return False

# =====================================================
# Face Tracking Manager (IMPROVED)
# =====================================================
class FaceTracker:
    """
    Tracks identified faces to avoid re-processing the same person
    Uses embedding similarity + time-based expiration with memory limits
    
    Separate tracking for:
    - Database writes: Uses window_seconds (default 30s) to prevent duplicate DB entries
    - Frontend notifications: Uses frontend_notification_window (2 hours) to allow re-notifications
    """

    __slots__ = ['window_seconds', 'frontend_notification_window', 'max_entries', 'max_memory_mb', 'tracked_faces', 
                'frontend_notification_times', 'max_memory_bytes', '_lock', '_cleanup_task', 
                'total_faces_added', 'total_faces_skipped', 'total_duplicates_prevented', '_memory_estimate_bytes']

    def __init__(self, window_seconds: int = FACE_TRACKING_WINDOW_SECONDS,
                max_entries: int = FACE_TRACKING_MAX_ENTRIES,
                max_memory_mb: int = FACE_TRACKING_MAX_MEMORY_MB,
                frontend_notification_window: int = 2 * 60 * 60):  # 2 hours for frontend notifications
        self.window_seconds = window_seconds
        self.frontend_notification_window = frontend_notification_window  # 2 hours = 7200 seconds
        self.max_entries = max_entries
        self.max_memory_mb = max_memory_mb
        self.max_memory_bytes = max_memory_mb * 1024 * 1024
        self.tracked_faces: Dict[str, Dict[str, Tuple[np.ndarray, str, float]]] = defaultdict(dict)
        # Track last notification time per pipeline+name for frontend
        # Format: "pipeline_id:name" -> timestamp
        self.frontend_notification_times: Dict[str, float] = {}
        self._memory_estimate_bytes = 0
        self.total_faces_added = 0
        self.total_faces_skipped = 0
        self.total_duplicates_prevented = 0
        self._lock = asyncio.Lock()
        self._cleanup_task = None

    async def start(self):
        """Start periodic cleanup of expired faces"""
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        logger.info(f"Face tracker started (window: {self.window_seconds}s, max_memory: {self.max_memory_mb}MB)")

    async def stop(self):
        """Stop cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    async def is_face_tracked(
        self,
        pipeline_id: str,
        embedding: np.ndarray,
        name: str,
        similarity_threshold: float = FACE_TRACKING_SIMILARITY_THRESHOLD
    ) -> bool:
        """
        Check if this face was recently identified
        Returns True if face should be SKIPPED (already tracked)
        """
        async with self._lock:
            current_time = time.time()

            # Get tracked faces for this pipeline
            pipeline_faces = self.tracked_faces.get(pipeline_id, {})
            if not pipeline_faces:
                return False

            # Check against all tracked faces WITH THE SAME NAME
            best_similarity = 0.0
            best_match_hash = None

            for face_hash, (tracked_emb, tracked_name, last_seen) in list(pipeline_faces.items()):
                # Skip if expired
                if current_time - last_seen > self.window_seconds:
                    continue

                # Skip if different person
                if tracked_name != name:
                    continue

                # Compute similarity
                sim = compute_similarity(embedding, tracked_emb)

                # Track best match for this name
                if sim > best_similarity:
                    best_similarity = sim
                    best_match_hash = face_hash

            # If we found a good match, update it with the newer embedding
            if best_similarity >= similarity_threshold and best_match_hash:
                # Update with new embedding to adapt to appearance changes
                pipeline_faces[best_match_hash] = (embedding, name, current_time)
                logger.info(f"[TRACKER] Skipping {name} (already tracked, sim={best_similarity:.3f}, updated embedding)")
                metrics_faces_skipped.labels(name=name).inc()
                self.total_faces_skipped += 1
                return True

            # Log why face was NOT tracked
            if best_similarity > 0:
                logger.debug(f"[TRACKER] NOT skipping {name} - similarity {best_similarity:.3f} below threshold {similarity_threshold:.3f}")

            return False

    async def add_face(
        self,
        pipeline_id: str,
        embedding: np.ndarray,
        name: str
    ):
        """
        Add a newly identified face to tracking
        Removes old entries for the same name to prevent duplicates
        """
        async with self._lock:
            current_time = time.time()

            # Generate hash for this face
            face_hash = hashlib.md5(embedding.tobytes()).hexdigest()[:16]

            # Initialize pipeline entry if needed
            if pipeline_id not in self.tracked_faces:
                self.tracked_faces[pipeline_id] = {}

            pipeline_faces = self.tracked_faces[pipeline_id]

            # Remove any existing entries for this person (same name)
            existing_hashes_for_name = [
                h for h, (_, n, _) in pipeline_faces.items()
                if n == name
            ]

            for old_hash in existing_hashes_for_name:
                del pipeline_faces[old_hash]
                logger.debug(f"[TRACKER] Removed old entry for {name} before adding new one")
                self.total_duplicates_prevented += 1
                self._memory_estimate_bytes -= self._estimate_embedding_size(512)  # Assume 512-dim embedding

            # Add new entry
            pipeline_faces[face_hash] = (embedding, name, current_time)
            self.total_faces_added += 1
            self._memory_estimate_bytes += self._estimate_embedding_size(embedding.shape[0])

            # Limit size per pipeline
            if len(pipeline_faces) > self.max_entries:
                # Remove oldest
                oldest_hash = min(
                    pipeline_faces.keys(),
                    key=lambda k: pipeline_faces[k][2]
                )
                del pipeline_faces[oldest_hash]
                self._memory_estimate_bytes -= self._estimate_embedding_size(512)

            # Check memory limit and enforce if needed
            if self._memory_estimate_bytes > self.max_memory_bytes:
                await self._enforce_memory_limit()

            logger.debug(f"[TRACKER] Added {name} to tracking (pipeline: {pipeline_id})")

    def _estimate_embedding_size(self, dims: int) -> int:
        """Estimate memory usage of an embedding"""
        # Embedding (float32) + name (str) + timestamp (float) + hash key overhead
        return dims * 4 + 100  # Rough estimate

    async def _enforce_memory_limit(self):
        """Enforce memory limit by removing oldest entries"""
        all_entries = []
        for pipeline_id, faces in self.tracked_faces.items():
            for face_hash, (_, name, last_seen) in faces.items():
                all_entries.append((pipeline_id, face_hash, name, last_seen))

        # Sort by age (oldest first)
        all_entries.sort(key=lambda x: x[3])

        # Remove until under limit
        removed = 0
        while self._memory_estimate_bytes > self.max_memory_bytes * 0.8 and all_entries:
            pipeline_id, face_hash, name, _ = all_entries.pop(0)
            if pipeline_id in self.tracked_faces and face_hash in self.tracked_faces[pipeline_id]:
                del self.tracked_faces[pipeline_id][face_hash]
                self._memory_estimate_bytes -= self._estimate_embedding_size(512)
                removed += 1

        if removed > 0:
            logger.warning(f"[TRACKER] Enforced memory limit: removed {removed} oldest entries")

    async def _periodic_cleanup(self):
        """Remove expired tracked faces and frontend notification times"""
        while True:
            try:
                await asyncio.sleep(60)  # Cleanup every minute

                async with self._lock:
                    current_time = time.time()
                    removed_count = 0

                    for pipeline_id in list(self.tracked_faces.keys()):
                        pipeline_faces = self.tracked_faces[pipeline_id]

                        # Remove expired faces
                        expired_hashes = [
                            face_hash
                            for face_hash, (_, _, last_seen) in pipeline_faces.items()
                            if current_time - last_seen > self.window_seconds
                        ]

                        for face_hash in expired_hashes:
                            del pipeline_faces[face_hash]
                            removed_count += 1
                            self._memory_estimate_bytes -= self._estimate_embedding_size(512)

                        # Remove empty pipelines
                        if not pipeline_faces:
                            del self.tracked_faces[pipeline_id]

                    # Clean up old frontend notification times (older than 3 hours)
                    frontend_cleanup_threshold = 3 * 60 * 60  # 3 hours
                    expired_notifications = [
                        key for key, timestamp in self.frontend_notification_times.items()
                        if current_time - timestamp > frontend_cleanup_threshold
                    ]
                    for key in expired_notifications:
                        del self.frontend_notification_times[key]

                    if removed_count > 0 or expired_notifications:
                        logger.info(f"[TRACKER] Cleaned up {removed_count} expired faces, {len(expired_notifications)} expired notification times")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[TRACKER] Cleanup error: {e}")

    async def should_send_frontend_notification(self, pipeline_id: str, name: str) -> bool:
        """
        Check if we should send frontend notification
        Returns True if we should send (either first time or after 2 hours)
        """
        async with self._lock:
            current_time = time.time()
            key = f"{pipeline_id}:{name}"
            
            if key not in self.frontend_notification_times:
                # First time seeing this person in this pipeline
                self.frontend_notification_times[key] = current_time
                logger.debug(f"[FRONTEND NOTIF] First notification for {name} in {pipeline_id}")
                return True
            
            last_notification = self.frontend_notification_times[key]
            time_since_last = current_time - last_notification
            
            if time_since_last >= self.frontend_notification_window:
                # More than 2 hours since last notification - send again
                self.frontend_notification_times[key] = current_time
                logger.info(f"[FRONTEND NOTIF] Re-notifying {name} in {pipeline_id} after {time_since_last/3600:.1f} hours")
                return True
            
            # Less than 2 hours - don't send notification
            logger.debug(f"[FRONTEND NOTIF] Skipping notification for {name} in {pipeline_id} (last sent {time_since_last/60:.1f} min ago)")
            return False

    async def get_stats(self) -> dict:
        """Get tracking statistics"""
        async with self._lock:
            total_tracked = sum(len(faces) for faces in self.tracked_faces.values())
            return {
                "enabled": FACE_TRACKING_ENABLED,
                "active_pipelines": len(self.tracked_faces),
                "total_tracked_faces": total_tracked,
                "window_seconds": self.window_seconds,
                "frontend_notification_window_seconds": self.frontend_notification_window,
                "memory_usage_mb": self._memory_estimate_bytes / (1024 * 1024),
                "memory_limit_mb": self.max_memory_mb,
                "total_faces_added": self.total_faces_added,
                "total_faces_skipped": self.total_faces_skipped,
                "total_duplicates_prevented": self.total_duplicates_prevented,
            }


face_tracker = FaceTracker(
    window_seconds=FACE_TRACKING_WINDOW_SECONDS,
    max_entries=FACE_TRACKING_MAX_ENTRIES,
    max_memory_mb=FACE_TRACKING_MAX_MEMORY_MB
)


# =====================================================
# Redis Cache Manager
# =====================================================
class CacheManager:
    """Redis cache manager for face recognition results"""

    def __init__(self):
        self.redis_client: Optional[redis.Redis] = None
        self._enabled = CACHE_ENABLED

    async def initialize(self):
        """Initialize Redis connection"""
        if not self._enabled:
            logger.info("Cache disabled (Redis not available)")
            return

        try:
            self.redis_client = await redis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                max_connections=settings.REDIS_MAX_CONNECTIONS if hasattr(settings, 'REDIS_MAX_CONNECTIONS') else 10,
                socket_keepalive=True,
                socket_connect_timeout=5,
                retry_on_timeout=True,
            )
            await self.redis_client.ping()
            logger.info("✅ Redis cache initialized")
        except Exception as e:
            logger.error(f"Redis initialization failed: {e}")
            self._enabled = False

    async def get(self, key: str) -> Optional[str]:
        """Get value from cache"""
        if not self._enabled or not self.redis_client:
            return None

        try:
            value = await self.redis_client.get(key)
            if value:
                metrics_cache_hits.inc()
            else:
                metrics_cache_misses.inc()
            return value
        except Exception as e:
            logger.error(f"Cache get error: {e}")
            return None

    async def set(self, key: str, value: str, ttl: int = None):
        """Set value in cache"""
        if not self._enabled or not self.redis_client:
            return

        try:
            ttl = ttl or getattr(settings, 'CACHE_TTL', 300)  # Default 5 minutes
            await self.redis_client.setex(key, ttl, value)
        except Exception as e:
            logger.error(f"Cache set error: {e}")

    async def delete(self, key: str):
        """Delete value from cache"""
        if not self._enabled or not self.redis_client:
            return

        try:
            await self.redis_client.delete(key)
        except Exception as e:
            logger.error(f"Cache delete error: {e}")

    async def health_check(self) -> bool:
        """Check if cache is healthy"""
        if not self._enabled:
            return False

        try:
            if self.redis_client:
                await self.redis_client.ping()
                return True
        except:
            pass
        return False

    async def close(self):
        """Close Redis connection"""
        if self.redis_client:
            await self.redis_client.close()
            logger.info("Redis connection closed")


cache_manager = CacheManager()


# =====================================================
# Circuit Breaker Pattern
# =====================================================
class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Circuit breaker for database operations"""

    def __init__(self, failure_threshold: int = 5, timeout: int = 60, half_open_timeout: int = None):
        self.failure_threshold = failure_threshold
        self.half_open_timeout = half_open_timeout
        self.timeout = timeout
        self.failures = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED
        self._lock = asyncio.Lock()

    async def call_succeeded(self):
        """Record successful call"""
        async with self._lock:
            self.failures = 0
            self.state = CircuitState.CLOSED

    async def call_failed(self):
        """Record failed call"""
        async with self._lock:
            self.failures += 1
            self.last_failure_time = time.time()

            if self.failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit breaker opened after {self.failures} failures")

    async def can_execute(self) -> bool:
        """Check if operation can be executed"""
        async with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                if time.time() - self.last_failure_time >= self.timeout:
                    self.state = CircuitState.HALF_OPEN
                    logger.info("Circuit breaker half-open, attempting recovery")
                    return True
                return False

            # HALF_OPEN state
            return True

    async def get_status(self) -> dict:
        """Get circuit breaker status"""
        async with self._lock:
            return {
                "state": self.state.value,
                "failures": self.failures,
                "threshold": self.failure_threshold,
                "timeout": self.timeout,
                "last_failure_time": self.last_failure_time,
                "time_since_last_failure": time.time() - self.last_failure_time if self.last_failure_time else None
            }


db_circuit_breaker = CircuitBreaker()


# =====================================================
# WebSocket Manager (FIXED)
# =====================================================
class WebSocketManager:
    """Manage WebSocket connections for real-time dashboard updates"""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)
            metrics_websocket_connections.set(len(self.active_connections))
            logger.info(f"[WS] Client connected. Total: {len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.discard(websocket)
                metrics_websocket_connections.set(len(self.active_connections))
                logger.info(f"[WS] Client disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """Broadcast message to all active connections safely"""
        # Get copy of connections under lock
        async with self._lock:
            connections = list(self.active_connections)

        if not connections:
            return

        disconnected_connections = []

        async def safe_send(conn: WebSocket):
            try:
                await conn.send_json(message)
                return None
            except Exception as e:
                logger.error(f"[WS] Send error: {e}")
                return conn  # Mark for removal

        # Send to all connections
        tasks = [safe_send(conn) for conn in connections]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Remove failed connections
        async with self._lock:
            for i, result in enumerate(results):
                if isinstance(result, WebSocket):
                    self.active_connections.discard(result)
                    disconnected_connections.append(result)
                elif isinstance(result, Exception):
                    # If there was an exception, remove the connection
                    if i < len(connections):
                        self.active_connections.discard(connections[i])
                        disconnected_connections.append(connections[i])

        if disconnected_connections:
            metrics_websocket_connections.set(len(self.active_connections))
            logger.info(f"[WS] Removed {len(disconnected_connections)} disconnected clients. Total: {len(self.active_connections)}")

    async def get_stats(self) -> dict:
        """Get WebSocket statistics"""
        async with self._lock:
            return {
                "active_connections": len(self.active_connections),
                "connections": list(self.active_connections),
            }


ws_manager = WebSocketManager()


# =====================================================
# Processing Queue with Semaphore for Concurrency Control
# =====================================================
class ProcessingQueue:
    """High-performance async queue with concurrency control"""

    def __init__(self, max_size: int = 1000, max_concurrent: int = 100):
        self.queue = asyncio.Queue(maxsize=max_size)
        self.semaphore = asyncio.Semaphore(max_concurrent)

        # Stats
        self.total_received = 0
        self.total_processed = 0
        self.total_skipped = 0
        self.processing_count = 0

        self._lock = asyncio.Lock()

    async def add(self, item: dict) -> bool:
        """Add item to queue (non-blocking)"""
        async with self._lock:
            self.total_received += 1

        try:
            self.queue.put_nowait(item)
            metrics_queue_size.set(self.queue.qsize())
            return True
        except asyncio.QueueFull:
            async with self._lock:
                self.total_skipped += 1
            logger.warning(f"Queue full! Dropped request from pipeline: {item.get('pipeline_id')}")
            return False

    async def get(self) -> dict:
        """Get item from queue"""
        return await self.queue.get()

    async def get_stats(self) -> dict:
        """Get queue statistics"""
        async with self._lock:
            return {
                "queue_size": self.queue.qsize(),
                "processing": self.processing_count,
                "total_received": self.total_received,
                "total_processed": self.total_processed,
                "total_skipped": self.total_skipped,
            }


processing_queue = ProcessingQueue(
    max_size=settings.MAX_QUEUE_SIZE,
    max_concurrent=settings.MAX_CONCURRENT_REQUESTS
)


# =====================================================
# Face Recognition Models (Singleton)
# =====================================================
class ModelManager:
    """Singleton for managing ML models"""

    def __init__(self):
        self.detector: Optional[SCRFD] = None
        self.recognizer: Optional[ArcFace] = None
        self.face_db: Optional[FaceDatabase] = None
        self._initialized = False
        self._lock = asyncio.Lock()

    def initialize(self):
        """Initialize models (thread-safe)"""
        if self._initialized:
            return

        logger.info("🔄 Initializing models...")

        self.detector = SCRFD(
            settings.DETECTION_MODEL,
            input_size=(640, 640),
            conf_thres=settings.CONFIDENCE_THRESHOLD,
        )

        self.recognizer = ArcFace(settings.RECOGNITION_MODEL)

        self.face_db = FaceDatabase(
            db_path=settings.DB_PATH,
            max_workers=4,
        )

        # Load or build face database
        loaded = self.face_db.load()
        logger.info(f"Face database loaded: {loaded}, total faces: {self.face_db.index.ntotal if loaded else 0}")
        if not loaded or getattr(self.face_db, "index", None) is None or self.face_db.index.ntotal == 0:
            self._build_face_database()

        self._initialized = True
        logger.info("✅ Models initialized successfully")

    def _build_face_database(self):
        """Build face database from images"""
        if not os.path.exists(settings.FACES_DIR):
            logger.warning(f"Faces directory not found: {settings.FACES_DIR}")
            self.face_db.save()
            return

        added = 0
        for filename in os.listdir(settings.FACES_DIR):
            if not filename.lower().endswith((".jpg", ".jpeg", ".png")):
                continue

            name = filename.rsplit(".", 1)[0]
            image_path = os.path.join(settings.FACES_DIR, filename)

            image = cv2.imread(image_path)
            if image is None:
                continue

            bboxes, kpss = self.detector.detect(image, max_num=1)
            if kpss is None or len(kpss) == 0:
                logger.warning(f"No face in {filename}")
                continue

            try:
                embedding = self.recognizer.get_embedding(image, kpss[0])
                self.face_db.add_face(embedding, name)
                added += 1
                logger.info(f"Added face: {name}")
            except Exception as e:
                logger.error(f"Error adding {filename}: {e}")

        self.face_db.save()
        logger.info(f"✅ Face DB built: {added} faces added")

    def health_check(self) -> bool:
        """Check if models are healthy"""
        return self._initialized and self.detector is not None and self.recognizer is not None


model_manager = ModelManager()

# Batch Database Writer (PRODUCTION SAFE)
# =====================================================
class BatchDatabaseWriter:
    """
    Batch database writer with:
    - Short-lived transactions
    - No long locks
    - Safe under high concurrency
    """

    def __init__(self, batch_size: int = BATCH_WRITE_SIZE, flush_interval: float = 2.0):
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        self.pending_detections: list[dict] = []
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None

    async def start(self):
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.info(
            f"Batch DB writer started "
            f"(batch_size={self.batch_size}, flush_interval={self.flush_interval}s)"
        )

    async def stop(self):
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self.flush()
        logger.info("Batch DB writer stopped")

    async def add_detection(self, detection_data: dict):
        async with self._lock:
            self.pending_detections.append(detection_data)

            if len(self.pending_detections) >= self.batch_size:
                await self._flush_internal()

    async def flush(self):
        async with self._lock:
            await self._flush_internal()

    async def _flush_internal(self):
        if not self.pending_detections:
            return

        if not await db_circuit_breaker.can_execute():
            logger.warning("DB circuit open — dropping batch")
            self.pending_detections.clear()
            return

        batch = self.pending_detections
        self.pending_detections = []

        start_time = time.time()

        # Add timeout to prevent hanging connections
        DB_OPERATION_TIMEOUT = 150.0  # seconds

        try:
            async with asyncio.timeout(DB_OPERATION_TIMEOUT):
                # =====================================================
                # TX 1 — ensure pipelines exist (SHORT TX)
                # =====================================================
                pipeline_ids = list({d["pipeline_id"] for d in batch})

                async with db_manager.get_session() as db:
                    existing = await db.execute(
                        select(Pipeline.pipeline_id).where(
                            Pipeline.pipeline_id.in_(pipeline_ids)
                        )
                    )
                    existing_ids = {row[0] for row in existing.fetchall()}

                    for pid in pipeline_ids:
                        if pid not in existing_ids:
                            db.add(Pipeline(pipeline_id=pid, total_detections=0))

                # =====================================================
                # TX 2 — bulk insert detections + faces (SHORT TX)
                # =====================================================
                async with db_manager.get_session() as db:
                    detection_rows = []
                    face_rows = []

                    for d in batch:
                        detection_rows.append(d["detection"])

                    result = await db.execute(
                        insert(Detection).returning(Detection.id),
                        detection_rows
                    )
                    detection_ids = [r[0] for r in result.fetchall()]

                    for det_id, d in zip(detection_ids, batch):
                        for face in d["faces"]:
                            face_copy = face.copy()
                            face_copy["detection_id"] = det_id
                            face_rows.append(face_copy)

                    if face_rows:
                        await db.execute(insert(Face), face_rows)

                # =====================================================
                # TX 3 — update pipeline counters (SHORT TX)
                # =====================================================
                async with db_manager.get_session() as db:
                    for pid in pipeline_ids:
                        await db.execute(
                            update(Pipeline)
                            .where(Pipeline.pipeline_id == pid)
                            .values(
                                total_detections=Pipeline.total_detections + 1,
                                updated_at=datetime.utcnow()
                            )
                        )

            await db_circuit_breaker.call_succeeded()
            metrics_db_operations.observe(time.time() - start_time)

            logger.info(
                f"✅ BULK flushed {len(batch)} detections "
                f"in {time.time() - start_time:.3f}s"
            )

        except asyncio.CancelledError:
            # Shutdown in progress, re-raise to allow proper cleanup
            logger.info("⚠️  Batch write cancelled (shutdown in progress)")
            self.pending_detections = batch + self.pending_detections  # Re-add to pending
            raise
        except asyncio.TimeoutError:
            await db_circuit_breaker.call_failed()
            logger.error(f"❌ Batch write timeout after {DB_OPERATION_TIMEOUT}s - database may be overloaded")
            # Don't re-add to pending as this might cause infinite retries
        except Exception as e:
            await db_circuit_breaker.call_failed()
            logger.exception("❌ Batch write failed")

    async def _periodic_flush(self):
        while True:
            try:
                await asyncio.sleep(self.flush_interval)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Batch periodic flush error")


batch_writer = BatchDatabaseWriter()


# =====================================================
# Data Retention and Cleanup Manager
# =====================================================
class DataRetentionManager:
    """Manages automatic cleanup of old data"""

    def __init__(self, retention_days: int = DATA_RETENTION_DAYS):
        self.retention_days = retention_days
        self._cleanup_task = None

    async def start(self):
        """Start periodic cleanup"""
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        logger.info(f"Data retention manager started (retention: {self.retention_days} days)")

    async def stop(self):
        """Stop cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("Data retention manager stopped")

    async def _periodic_cleanup(self):
        """Run cleanup periodically"""
        # Wait a bit before first cleanup
        await asyncio.sleep(60)

        while True:
            try:
                await self.cleanup_old_data()
                await asyncio.sleep(CLEANUP_INTERVAL_HOURS * 3600)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
                await asyncio.sleep(3600)  # Retry in 1 hour

    async def cleanup_old_data(self):
        """Delete old detections and images"""
        cutoff_date = datetime.utcnow() - timedelta(days=self.retention_days)

        logger.info(f"Starting cleanup of data older than {cutoff_date}")

        start_time = time.time()
        deleted_detections = 0
        deleted_files = 0
        freed_space_mb = 0

        try:
            async with db_manager.get_session() as db:
                # Find old detections
                result = await db.execute(
                    select(Detection)
                    .where(Detection.timestamp < cutoff_date)
                    .limit(1000)  # Process in batches
                )
                old_detections = result.scalars().all()

                for detection in old_detections:
                    # Delete face image files if they exist
                    if detection.faces:
                        for face in detection.faces:
                            if face.face_image_path and os.path.exists(face.face_image_path):
                                try:
                                    file_size = os.path.getsize(face.face_image_path)
                                    os.remove(face.face_image_path)
                                    freed_space_mb += file_size / (1024 * 1024)
                                    deleted_files += 1
                                except Exception as e:
                                    logger.error(f"Failed to delete face file {face.face_image_path}: {e}")

                    # Delete from database (cascade will delete faces)
                    await db.delete(detection)
                    deleted_detections += 1

                await db.commit()

                # Clean up empty directories
                self._cleanup_empty_directories()

                duration = time.time() - start_time

                logger.info(
                    f"Cleanup completed: {deleted_detections} detections, "
                    f"{deleted_files} files, {freed_space_mb:.2f} MB freed "
                    f"in {duration:.2f}s"
                )

                metrics_cleanup_operations.inc()

        except Exception as e:
            logger.error(f"Cleanup failed: {e}")

    def _cleanup_empty_directories(self):
        """Remove empty pipeline directories"""
        if not os.path.exists(settings.STORAGE_DIR):
            return

        for pipeline_dir in os.listdir(settings.STORAGE_DIR):
            dir_path = os.path.join(settings.STORAGE_DIR, pipeline_dir)

            if os.path.isdir(dir_path) and not os.listdir(dir_path):
                try:
                    os.rmdir(dir_path)
                    logger.info(f"Removed empty directory: {dir_path}")
                except Exception as e:
                    logger.error(f"Failed to remove directory {dir_path}: {e}")
    async def get_storage_stats(self) -> dict:
        """Get storage statistics"""
        total_size = 0
        file_count = 0

        for root, dirs, files in os.walk(settings.STORAGE_DIR):
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    total_size += os.path.getsize(file_path)
                    file_count += 1
                except Exception:
                    pass

        max_storage_gb = getattr(settings, 'MAX_STORAGE_GB', 100)

        return {
            "total_size_mb": total_size / (1024 * 1024),
            "total_size_gb": total_size / (1024 * 1024 * 1024),
            "file_count": file_count,
            "max_storage_gb": max_storage_gb,
            "usage_percent": min(100, (total_size / (1024 * 1024 * 1024)) / max_storage_gb * 100) if max_storage_gb > 0 else 0
        }


retention_manager = DataRetentionManager()

"""
Production-Scale Redis Integration for Face Recognition
Features:
- Multi-layer caching strategy
- Cache warming and pre-fetching
- Write-behind cache updates
- Circuit breaker for cache failures
- Cache stampede prevention
- TTL with jitter for load distribution
- Cache versioning for schema migrations
- Monitoring and metrics
"""

# =====================================================
# Production Cache Manager (ADD TO EXISTING CODE)
# =====================================================
class ProductionCacheManager:
    """
    Enterprise-grade cache manager with:
    - Multi-layer caching (in-memory + Redis)
    - Cache warming
    - Write-behind updates
    - Circuit breaker pattern
    - Graceful degradation
    """

    def __init__(self, redis_client=None, local_cache_size: int = 10000):
        self.redis_client = redis_client
        self._enabled = redis_client is not None

        # Local LRU cache for hot items (reduces Redis load)
        self.local_cache = {}
        self.local_cache_order = deque(maxlen=local_cache_size)
        self.local_cache_size = local_cache_size

        # Circuit breaker for Redis
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            timeout=30,
            half_open_timeout=60
        )

        # Statistics
        self.stats = {
            'hits': {'local': 0, 'redis': 0},
            'misses': 0,
            'errors': 0,
            'write_behind': 0
        }

        # Cache version for schema migrations
        self.cache_version = "v2"

        # Background tasks
        self._write_behind_queue = asyncio.Queue(maxsize=10000)
        self._write_behind_task = None
        self._warming_task = None

        logger.info(f"ProductionCacheManager initialized (local_cache: {local_cache_size})")

    async def start(self):
        """Start background workers"""
        if self._enabled:
            self._write_behind_task = asyncio.create_task(self._write_behind_worker())
            self._warming_task = asyncio.create_task(self._cache_warming_worker())
            logger.info("ProductionCacheManager background workers started")

    async def stop(self):
        """Stop background workers gracefully"""
        if self._write_behind_task:
            self._write_behind_task.cancel()
        if self._warming_task:
            self._warming_task.cancel()
        logger.info("ProductionCacheManager stopped")

    # ==================== CORE CACHE METHODS ====================

    async def get_with_fallback(self, key: str, fallback_func=None, fallback_args=None, ttl: int = 300):
        """
        Production-grade cache get with fallback
        Implements: Local cache → Redis → Fallback function → Cache population
        """
        # 1. Check local cache (fastest)
        if key in self.local_cache:
            value, expiry = self.local_cache[key]
            if expiry > time.time():
                self.stats['hits']['local'] += 1
                metrics_cache_hits.inc()
                return value

        # 2. Check Redis (if enabled and circuit breaker allows)
        redis_value = None
        if self._enabled and await self.circuit_breaker.can_execute():
            try:
                redis_value = await self._safe_redis_get(key)
                if redis_value:
                    # Parse Redis value (includes metadata)
                    value, metadata = self._deserialize(redis_value)

                    # Store in local cache
                    self._set_local_cache(key, value, ttl)

                    self.stats['hits']['redis'] += 1
                    metrics_cache_hits.inc()
                    return value
            except Exception as e:
                self.stats['errors'] += 1
                logger.warning(f"[CACHE] Redis get error (falling back): {e}")
                await self.circuit_breaker.call_failed()

        # 3. Use fallback function if provided
        if fallback_func and callable(fallback_func):
            try:
                # Execute fallback (e.g., database query)
                if fallback_args:
                    result = await fallback_func(*fallback_args) if asyncio.iscoroutinefunction(fallback_func) else fallback_func(*fallback_args)
                else:
                    result = await fallback_func() if asyncio.iscoroutinefunction(fallback_func) else fallback_func()

                # 4. Populate cache asynchronously (write-behind)
                if result is not None:
                    asyncio.create_task(self._async_set(key, result, ttl))

                self.stats['misses'] += 1
                metrics_cache_misses.inc()
                return result
            except Exception as e:
                logger.error(f"[CACHE] Fallback function error: {e}")

        # 5. Complete cache miss
        self.stats['misses'] += 1
        metrics_cache_misses.inc()
        return None

    async def set_with_ttl_jitter(self, key: str, value, base_ttl: int = 300, jitter_percent: float = 0.1):
        """
        Set cache with TTL jitter to prevent cache stampedes
        """
        # Add jitter to prevent all keys expiring at once
        jitter = base_ttl * jitter_percent
        actual_ttl = base_ttl + random.uniform(-jitter, jitter)

        await self._async_set(key, value, int(actual_ttl))

    # ==================== FACE RECOGNITION SPECIFIC ====================

    async def get_face_match(self, embedding: np.ndarray, threshold: float, face_db_search_func):
        """
        Specialized cache for face matching with optimized key generation
        """
        # Generate deterministic cache key
        embedding_hash = self._generate_embedding_hash(embedding)
        cache_key = f"{self.cache_version}:face:{embedding_hash}:{threshold:.2f}"

        # Try cache with fallback to face search
        result = await self.get_with_fallback(
            key=cache_key,
            fallback_func=face_db_search_func,
            fallback_args=(embedding, threshold),
            ttl=600  # 10 minutes for face matches
        )

        # Parse result if from cache
        if isinstance(result, str):
            name, similarity = result.split(":")
            return name, float(similarity)

        return result

    async def cache_pipeline_stats(self, pipeline_id: str, stats: dict, ttl: int = 60):
        """
        Cache pipeline statistics with short TTL
        """
        if not self._enabled:
            return

        cache_key = f"{self.cache_version}:stats:pipeline:{pipeline_id}"
        await self._async_set(cache_key, stats, ttl)

    async def get_pipeline_stats(self, pipeline_id: str, fallback_func=None):
        """
        Get cached pipeline statistics
        """
        if not self._enabled:
            return await fallback_func() if fallback_func else None

        cache_key = f"{self.cache_version}:stats:pipeline:{pipeline_id}"
        return await self.get_with_fallback(
            key=cache_key,
            fallback_func=fallback_func,
            ttl=60
        )

    # ==================== PRIVATE METHODS ====================

    def _generate_embedding_hash(self, embedding: np.ndarray) -> str:
        """Generate fast hash for embedding"""
        # Use first 8 bytes for speed (collisions acceptable for cache)
        return hashlib.md5(embedding.tobytes()).hexdigest()[:16]

    def _set_local_cache(self, key: str, value, ttl: int):
        """Set value in local LRU cache"""
        if len(self.local_cache) >= self.local_cache_size:
            # Remove oldest
            oldest = self.local_cache_order.popleft()
            if oldest in self.local_cache:
                del self.local_cache[oldest]

        expiry = time.time() + ttl
        self.local_cache[key] = (value, expiry)
        self.local_cache_order.append(key)

    async def _safe_redis_get(self, key: str):
        """Safe Redis get with timeout"""
        try:
            return await asyncio.wait_for(self.redis_client.get(key), timeout=1.0)
        except asyncio.TimeoutError:
            logger.warning(f"[CACHE] Redis get timeout for key: {key}")
            return None
        except Exception as e:
            raise e

    async def _async_set(self, key: str, value, ttl: int):
        """
        Asynchronous cache set (write-behind pattern)
        """
        if not self._enabled:
            return

        try:
            # Add to write-behind queue
            self._write_behind_queue.put_nowait((key, value, ttl))
            self.stats['write_behind'] += 1
        except asyncio.QueueFull:
            # Queue full, set directly (blocking)
            await self._direct_set(key, value, ttl)

    async def _direct_set(self, key: str, value, ttl: int):
        """Direct Redis set"""
        try:
            serialized = self._serialize(value)
            await self.redis_client.setex(key, ttl, serialized)

            # Also update local cache
            self._set_local_cache(key, value, ttl)
        except Exception as e:
            logger.warning(f"[CACHE] Direct set error: {e}")

    def _serialize(self, value) -> str:
        """Serialize value with metadata"""
        if isinstance(value, tuple) and len(value) == 2:
            # Face match result: (name, similarity)
            name, similarity = value
            return f"face:{name}:{similarity}"
        elif isinstance(value, dict):
            # JSON for complex objects
            return json.dumps({
                'data': value,
                'version': self.cache_version,
                'timestamp': time.time()
            })
        else:
            # Default string serialization
            return str(value)

    def _deserialize(self, serialized: str):
        """Deserialize cached value"""
        if serialized.startswith('face:'):
            # Face match result
            return serialized[5:], {'type': 'face_match'}
        elif serialized.startswith('{'):
            # JSON object
            data = json.loads(serialized)
            return data['data'], {'version': data.get('version'), 'timestamp': data.get('timestamp')}
        else:
            # Plain string
            return serialized, {}

    # ==================== BACKGROUND WORKERS ====================

    async def _write_behind_worker(self):
        """Background worker for write-behind cache updates"""
        logger.info("[CACHE] Write-behind worker started")

        while True:
            try:
                key, value, ttl = await self._write_behind_queue.get()

                try:
                    await self._direct_set(key, value, ttl)
                except Exception as e:
                    logger.warning(f"[CACHE] Write-behind error: {e}")

                self._write_behind_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CACHE] Write-behind worker error: {e}")
                await asyncio.sleep(1)

    async def _cache_warming_worker(self):
        """Warm cache with frequently accessed items"""
        logger.info("[CACHE] Warming worker started")

        while True:
            try:
                # Run every 5 minutes
                await asyncio.sleep(300)

                # Here you could implement cache warming logic
                # Example: Pre-cache hot embeddings, pipeline stats, etc.

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CACHE] Warming worker error: {e}")
                await asyncio.sleep(60)

    # ==================== MONITORING ====================

    async def get_stats(self) -> dict:
        """Get cache statistics"""
        async with self._lock:
            return {
                'enabled': self._enabled,
                'circuit_breaker': await self.circuit_breaker.get_status(),
                'hits': self.stats['hits'],
                'misses': self.stats['misses'],
                'errors': self.stats['errors'],
                'write_behind_queue_size': self._write_behind_queue.qsize() if self._write_behind_queue else 0,
                'local_cache_size': len(self.local_cache),
                'local_cache_capacity': self.local_cache_size,
                'cache_version': self.cache_version
            }


# =====================================================
# Production Face Recognition Cache (MAIN INTEGRATION)
# =====================================================
class FaceRecognitionCache:
    """
    Main integration point - wraps existing functionality with caching
    Zero breaking changes to existing code
    """

    def __init__(self, model_manager, cache_manager):
        self.model_manager = model_manager
        self.cache_manager = cache_manager

        # Track cache performance
        self.cache_performance = {
            'embedding_hits': 0,
            'embedding_misses': 0,
            'avg_similarity': 0.0,
            'last_reset': time.time()
        }

        logger.info("FaceRecognitionCache initialized")

    async def search_face_with_cache(self, embedding: np.ndarray, threshold: float):
        """
        Production-grade face search with multi-layer caching
        """
        start_time = time.time()

        # Use production cache manager
        result = await self.cache_manager.get_face_match(
            embedding=embedding,
            threshold=threshold,
            face_db_search_func=lambda e, t: self.model_manager.face_db.search(e, t)
        )

        processing_time = time.time() - start_time

        # Track performance
        if result:
            name, similarity = result
            self.cache_performance['avg_similarity'] = (
                self.cache_performance['avg_similarity'] * 0.9 + similarity * 0.1
            )

            logger.info(f"[PRODUCTION CACHE] Face match: {name} (sim={similarity:.3f}) in {processing_time*1000:.1f}ms")

        return result

    async def get_pipeline_stats_with_cache(self, pipeline_id: str, fallback_func):
        """
        Get pipeline stats with caching
        """
        return await self.cache_manager.get_pipeline_stats(pipeline_id, fallback_func)

    async def cache_detection_result(self, pipeline_id: str, detection_id: int, faces: list, ttl: int = 300):
        """
        Cache detection results for quick retrieval
        """
        if not self.cache_manager._enabled:
            return

        cache_key = f"{self.cache_manager.cache_version}:detection:{pipeline_id}:{detection_id}"
        await self.cache_manager.set_with_ttl_jitter(cache_key, {'faces': faces}, base_ttl=ttl)

    async def get_performance_stats(self) -> dict:
        """Get cache performance statistics"""
        total_searches = self.cache_performance['embedding_hits'] + self.cache_performance['embedding_misses']
        hit_rate = (self.cache_performance['embedding_hits'] / total_searches * 100) if total_searches > 0 else 0

        return {
            **self.cache_performance,
            'total_searches': total_searches,
            'hit_rate_percent': hit_rate,
            'uptime_hours': (time.time() - self.cache_performance['last_reset']) / 3600,
            'cache_manager_stats': await self.cache_manager.get_stats() if self.cache_manager else None
        }


# =====================================================
# INTEGRATION WITH EXISTING CODE (SAFE)
# =====================================================

# 1. INITIALIZE (Add after existing managers)
production_cache_manager = ProductionCacheManager(
    redis_client= None
)

face_recognition_cache = FaceRecognitionCache(model_manager, production_cache_manager)


# =====================================================
# OPTIMIZED Core Processing Function (FIXED)
# =====================================================
async def process_image_async(
    image_bytes: bytes,
    pipeline_id: str,
    predictions: list,
    use_batch_write: bool = True,
    send_realtime_updates: bool = True,
    worker_id: Optional[int] = None
) -> Optional[dict]:
    """
    OPTIMIZED VERSION with fixes:
    - Improved deduplication logic (embeddings first, then names)
    - Better error handling
    - Fixed race conditions in batch tracking
    - More efficient memory usage
    - Consistent Unknown face handling
    """

    start_time = time.time()

    # Decode image
    try:
        frame = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            logger.error(f"[PROCESS] Failed to decode image for pipeline {pipeline_id}")
            return None
    except Exception as e:
        logger.error(f"[PROCESS] Image decode error for pipeline {pipeline_id}: {e}")
        return None

    H, W = frame.shape[:2]
    detected_faces = []
    new_faces_count = 0

    # Track embeddings for intra-batch deduplication
    # Structure: name -> list of embeddings for that person in this image
    batch_face_embeddings = {}

    logger.info(f"[PROCESS] Processing {len(predictions)} predictions for pipeline {pipeline_id}")

    for pred_idx, pred in enumerate(predictions):
        class_name = pred.get("class_name", "").lower()
        if class_name not in ("person", "face"):
            continue

        # Extract and validate bbox
        try:
            bbox = pred["bbox"]
            if len(bbox) != 4:
                logger.warning(f"[PROCESS] Invalid bbox length in prediction {pred_idx}: {bbox}")
                continue

            x1, y1, x2, y2 = map(int, bbox)
            x1 = max(0, min(x1, W - 1))
            y1 = max(0, min(y1, H - 1))
            x2 = max(0, min(x2, W))
            y2 = max(0, min(y2, H))

            if x2 <= x1 or y2 <= y1:
                logger.debug(f"[PROCESS] Invalid bbox dimensions: ({x1},{y1})-({x2},{y2})")
                continue
        except (ValueError, KeyError, IndexError, TypeError) as e:
            logger.warning(f"[PROCESS] Invalid bbox in prediction {pred_idx}: {e}, bbox={pred.get('bbox')}")
            continue

        # Extract face crop
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            logger.debug(f"[PROCESS] Empty crop at bbox ({x1},{y1})-({x2},{y2})")
            continue

        # Detect face in crop
        try:
            bboxes, kpss = model_manager.detector.detect(crop, max_num=1)
            if kpss is None or len(kpss) == 0:
                logger.debug(f"[PROCESS] No face detected in crop {pred_idx}")
                continue
        except Exception as e:
            logger.error(f"[PROCESS] Face detection error in crop {pred_idx}: {e}")
            continue

        landmarks = kpss[0]

        # Align face
        try:
            aligned_face, M_inv = face_alignment(crop, landmarks, image_size=112)
            aligned_face_rgb = cv2.cvtColor(aligned_face, cv2.COLOR_BGR2RGB)
            aligned_face_rgb = np.clip(aligned_face_rgb, 0, 255).astype(np.uint8)
        except Exception as e:
            logger.error(f"[PROCESS] Face alignment error in crop {pred_idx}: {e}")
            continue

        aligned_landmarks = reference_alignment.copy()

        # Generate embedding
        try:
            embedding = model_manager.recognizer.get_embedding(
                aligned_face_rgb,
                aligned_landmarks
            )
            embedding = embedding / np.linalg.norm(embedding)  # Normalize

            if np.isnan(embedding).any() or np.linalg.norm(embedding) == 0:
                logger.warning(f"[PROCESS] Invalid embedding generated for crop {pred_idx}")
                continue

            logger.debug(f"[PROCESS] Embedding generated: shape={embedding.shape}, norm={np.linalg.norm(embedding):.4f}")
        except Exception as e:
            logger.error(f"[PROCESS] Embedding generation error in crop {pred_idx}: {e}")
            continue

        # Search face database
        try:
            name, similarity = await face_recognition_cache.search_face_with_cache(
                embedding=embedding,
                threshold=settings.SIMILARITY_THRESHOLD
            )
            logger.info(f"[PROCESS] Match: {name} (similarity={similarity:.4f})")
        except Exception as e:
            logger.error(f"[PROCESS] Database search error: {e}")
            continue

        # Optionally skip Unknown faces (make this configurable)
        if getattr(settings, 'SKIP_UNKNOWN_FACES', False) and name == "Unknown":
            logger.debug(f"[PROCESS] Skipping Unknown face (config: SKIP_UNKNOWN_FACES={getattr(settings, 'SKIP_UNKNOWN_FACES', False)})")
            continue

        # =====================================================
        # IMPROVED DEDUPLICATION LOGIC
        # =====================================================

        # 1. FIRST: Check if this is a duplicate in CURRENT batch using embeddings
        # This checks against ALL faces in current image, regardless of name
        duplicate_in_batch = False

        if batch_face_embeddings:
            all_embeddings = []
            all_names = []
            for name_key, emb_list in batch_face_embeddings.items():
                all_embeddings.extend(emb_list)
                all_names.extend([name_key] * len(emb_list))

            if all_embeddings:
                # Vectorized similarity computation
                embeddings_array = np.array(all_embeddings)
                similarities = np.dot(embeddings_array, embedding)

                # Find the highest similarity
                max_sim_idx = np.argmax(similarities)
                max_sim = similarities[max_sim_idx]

                if max_sim >= FACE_TRACKING_SIMILARITY_THRESHOLD:
                    duplicate_name = all_names[max_sim_idx]
                    logger.info(f"[BATCH] Skipping face - high similarity ({max_sim:.3f}) with {duplicate_name} in same image")
                    metrics_faces_batch_skipped.labels(name=duplicate_name).inc()
                    duplicate_in_batch = True

        if duplicate_in_batch:
            continue

        # 2. SECOND: Check temporal face tracker (across time/frames)
        if FACE_TRACKING_ENABLED:
            try:
                is_tracked = await face_tracker.is_face_tracked(
                    pipeline_id=pipeline_id,
                    embedding=embedding,
                    name=name,
                    similarity_threshold=FACE_TRACKING_SIMILARITY_THRESHOLD
                )

                if is_tracked:
                    logger.info(f"[PROCESS] Skipping {name} - already tracked recently")
                    continue
            except Exception as e:
                logger.error(f"[PROCESS] Face tracker error: {e}")
                # Continue processing anyway - don't let tracker failure block detection

        # 3. THIRD: Store in batch tracking for future comparisons
        if name not in batch_face_embeddings:
            batch_face_embeddings[name] = []
        batch_face_embeddings[name].append(embedding)

        # 4. Add to temporal tracker to prevent duplicates in future frames
        if FACE_TRACKING_ENABLED:
            try:
                await face_tracker.add_face(
                    pipeline_id=pipeline_id,
                    embedding=embedding,
                    name=name
                )
            except Exception as e:
                logger.error(f"[PROCESS] Failed to add face to tracker: {e}")
                # Continue processing - tracker failure shouldn't block saving

        # =====================================================
        # SAVE THE FACE IMAGE
        # =====================================================
        face_filename = None
        if getattr(settings, 'SAVE_IMAGES', True):
            try:
                pipeline_dir = os.path.join(settings.STORAGE_DIR, pipeline_id, "faces")
                os.makedirs(pipeline_dir, exist_ok=True)

                timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                safe_name = "".join(c for c in name if c.isalnum() or c in ('-', '_')).lower()
                face_filename = os.path.join(pipeline_dir, f"{safe_name}_{timestamp}.jpg")

                # Save the aligned face image
                cv2.imwrite(face_filename, aligned_face, [cv2.IMWRITE_JPEG_QUALITY, 90])
                logger.info(f"[PROCESS] Saved face image: {face_filename}")
            except Exception as e:
                logger.error(f"[PROCESS] Failed to save face image: {e}")
                # Continue without the saved file

        # Encode aligned face for database/frontend
        try:
            ok, buf = cv2.imencode(".jpg", aligned_face)
            if not ok:
                logger.warning(f"[PROCESS] Failed to encode face image")
                continue
            face_b64 = base64.b64encode(buf).decode()
        except Exception as e:
            logger.error(f"[PROCESS] Face encoding error: {e}")
            continue

        # Prepare face data
        face_data = {
            "name": name,
            "similarity": float(similarity),
            "image": face_b64,
            "bbox": [float(x1), float(y1), float(x2), float(y2)],
            "face_image_path": face_filename,  # Path to saved face image
        }

        detected_faces.append(face_data)
        new_faces_count += 1
        metrics_faces_detected.labels(name=name).inc()

        # =====================================================
        # REAL-TIME UPDATE: Send NEW face immediately (if allowed by 2-hour rule)
        # =====================================================
        if send_realtime_updates:
            try:
                # Check if we should send frontend notification (2-hour window)
                should_notify = True
                if FACE_TRACKING_ENABLED:
                    should_notify = await face_tracker.should_send_frontend_notification(pipeline_id, name)
                
                if should_notify:
                    realtime_result = {
                        "pipeline_id": pipeline_id,
                        "timestamp": datetime.utcnow().isoformat(),
                        "processing_time_ms": (time.time() - start_time) * 1000,
                        "faces": [face_data],
                    }

                    # Get stats for context
                    stats = await processing_queue.get_stats()
                    tracker_stats = await face_tracker.get_stats() if FACE_TRACKING_ENABLED else {"enabled": False}

                    await ws_manager.broadcast({
                        "type": "new_detection",
                        "data": realtime_result,
                        "stats": stats,
                        "tracker_stats": tracker_stats,
                    })

                    logger.info(f"✅ Real-time: Sent NEW face {name} (sim={similarity:.2f}) from {pipeline_id}")
                else:
                    logger.debug(f"⏭️  Skipping frontend notification for {name} in {pipeline_id} (within 2-hour window)")
            except Exception as e:
                logger.error(f"❌ Real-time broadcast error: {e}")
                # Don't fail the whole process due to WebSocket error

    # =====================================================
    # BATCH PROCESSING COMPLETION
    # =====================================================
    if not detected_faces:
        logger.info(f"[PROCESS] No new faces detected (all were tracked or no faces found)")
        return None

    processing_time = (time.time() - start_time) * 1000
    logger.info(f"[PROCESS] Detected {new_faces_count} NEW faces (pipeline: {pipeline_id}) in {processing_time:.1f}ms")

    # =====================================================
    # PREPARE DATABASE DATA
    # =====================================================
    detection_data = {
        "pipeline_id": pipeline_id,
        "detection": {
            "pipeline_id": pipeline_id,
            "timestamp": datetime.utcnow(),
            "image_path": None,  # No full frame saved
            "image_size_bytes": 0,
            "processing_time_ms": processing_time,
            "worker_id": worker_id,
        },
        "faces": [
            {
                "name": f["name"],
                "similarity": f["similarity"],
                "face_image_path": f.get("face_image_path"),
                "bbox_x1": f["bbox"][0],
                "bbox_y1": f["bbox"][1],
                "bbox_x2": f["bbox"][2],
                "bbox_y2": f["bbox"][3],
            }
            for f in detected_faces
        ]
    }

    # =====================================================
    # SAVE TO DATABASE
    # =====================================================
    if use_batch_write:
        # Add to batch writer
        try:
            await batch_writer.add_detection(detection_data)
            logger.debug(f"[PROCESS] Added {len(detected_faces)} faces to batch writer")
        except Exception as e:
            logger.error(f"[PROCESS] Batch writer error: {e}")
            # Fall back to direct write on batch writer failure
            use_batch_write = False

    if not use_batch_write:
        # Direct write to database
        try:
            async with db_manager.get_session() as db:
                # Get or create pipeline
                result = await db.execute(
                    select(Pipeline).where(Pipeline.pipeline_id == pipeline_id)
                )
                pipeline = result.scalar_one_or_none()

                if not pipeline:
                    pipeline = Pipeline(pipeline_id=pipeline_id, total_detections=0)
                    db.add(pipeline)
                    await db.flush()

                # Create detection record
                detection = Detection(**detection_data["detection"])
                db.add(detection)
                await db.flush()

                # Create face records
                for face_data in detection_data["faces"]:
                    face_data["detection_id"] = detection.id
                    db.add(Face(**face_data))

                # Update pipeline stats
                pipeline.total_detections += new_faces_count
                pipeline.updated_at = datetime.utcnow()

                await db.commit()
                logger.info(f"✅ Saved {new_faces_count} faces to DB for pipeline {pipeline_id}")

        except Exception as e:
            logger.error(f"[PROCESS] Database error: {e}")
            # Don't re-raise - we still want to return the detection result
            # The frontend should know faces were detected even if DB save failed

    # =====================================================
    # RETURN RESULT
    # =====================================================
    return {
        "pipeline_id": pipeline_id,
        "timestamp": datetime.utcnow().isoformat(),
        "processing_time_ms": processing_time,
        "faces": detected_faces,
        "new_faces_count": new_faces_count,
        "total_faces_processed": len(batch_face_embeddings),
    }


# =====================================================
# Background Worker (Optimized)
# =====================================================
async def queue_worker(worker_id: int):
    """Background worker to process queued requests"""
    logger.info(f"[WORKER-{worker_id}] Started")

    while True:
        try:
            # Get item from queue
            item = await processing_queue.get()

            # Acquire semaphore for concurrency control
            async with processing_queue.semaphore:
                processing_queue.processing_count += 1
                metrics_worker_active.labels(worker_id=str(worker_id)).set(1)

                try:
                    result = await process_image_async(
                        item["image_bytes"],
                        item["pipeline_id"],
                        item["predictions"],
                        use_batch_write=True,  # Use batch writing for efficiency
                        send_realtime_updates=True,  # Enable real-time face updates
                        worker_id=worker_id  # Pass worker ID for tracking
                    )

                    if result:
                        processing_queue.total_processed += 1
                        logger.info(f"[WORKER-{worker_id}] ✅ Completed: {result['pipeline_id']} ({result.get('new_faces_count', 0)} new faces)")
                    else:
                        processing_queue.total_skipped += 1

                except Exception as e:
                    logger.error(f"[WORKER-{worker_id}] Error: {e}")
                    processing_queue.total_skipped += 1

                finally:
                    processing_queue.processing_count -= 1
                    metrics_worker_active.labels(worker_id=str(worker_id)).set(0)
                    processing_queue.queue.task_done()
                    metrics_processing_time.observe(time.time() - item.get("timestamp", time.time()))

        except Exception as e:
            logger.error(f"[WORKER-{worker_id}] Fatal error: {e}")
            await asyncio.sleep(1)


# =====================================================
# System Metrics Collector
# =====================================================
class SystemMetricsCollector:
    """Collects and saves system metrics to database periodically"""

    def __init__(self, collection_interval: int = 60):
        """
        Args:
            collection_interval: Interval in seconds between metric collections (default: 60s)
        """
        self.collection_interval = collection_interval
        self._collector_task = None

    async def start(self):
        """Start periodic metrics collection"""
        self._collector_task = asyncio.create_task(self._periodic_collection())
        logger.info(f"System metrics collector started (interval: {self.collection_interval}s)")

    async def stop(self):
        """Stop metrics collection task"""
        if self._collector_task:
            self._collector_task.cancel()
            try:
                await self._collector_task
            except asyncio.CancelledError:
                pass
        logger.info("System metrics collector stopped")

    async def _periodic_collection(self):
        """Run metrics collection periodically"""
        # Wait a bit before first collection
        await asyncio.sleep(10)

        while True:
            try:
                await self.collect_and_save_metrics()
                await asyncio.sleep(self.collection_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Metrics collection error: {e}")
                await asyncio.sleep(self.collection_interval)

    async def collect_and_save_metrics(self):
        """Collect current metrics and save to database"""
        try:
            # Get queue stats
            queue_stats = await processing_queue.get_stats()

            # # Get active pipelines count
            # async with db_manager.get_session() as db:
            #     # Count active pipelines
            #     pipelines_result = await db.execute(
            #         select(func.count(Pipeline.id)).where(Pipeline.is_active == 1)
            #     )
            #     active_pipelines_count = pipelines_result.scalar() or 0

            #     # Count total faces detected
            #     faces_result = await db.execute(select(func.count(Face.id)))
            #     total_faces_count = faces_result.scalar() or 0
            # Use proper session for read queries
            async with db_manager.get_session() as db:
                pipelines_result = await db.execute(
                    select(func.count(Pipeline.id)).where(Pipeline.is_active == 1)
                )
                active_pipelines_count = pipelines_result.scalar() or 0

                faces_result = await db.execute(
                    select(func.count(Face.id))
                )
                total_faces_count = faces_result.scalar() or 0

                # Get average processing time (from recent detections)
                avg_time_result = await db.execute(
                    select(func.avg(Detection.processing_time_ms))
                    .where(Detection.timestamp >= datetime.utcnow() - timedelta(seconds=self.collection_interval))
                )
                avg_processing_time = avg_time_result.scalar()

            # Get system resource usage (CPU, Memory) if psutil available
            if PSUTIL_AVAILABLE:
                try:
                    cpu_percent = psutil.cpu_percent(interval=1)
                    memory = psutil.virtual_memory()
                    memory_percent = memory.percent
                    disk_usage_gb = psutil.disk_usage(settings.STORAGE_DIR).used / (1024**3) if os.path.exists(settings.STORAGE_DIR) else 0
                except Exception as e:
                    logger.error(f"System metrics collection error: {e}")
                    cpu_percent = None
                    memory_percent = None
                    disk_usage_gb = None
            else:
                cpu_percent = None
                memory_percent = None
                disk_usage_gb = None

            # Create metrics record in separate transaction
            async with db_manager.get_session() as db:
                # Create metrics record
                metrics_record = SystemMetrics(
                    timestamp=datetime.utcnow(),
                    queue_size=queue_stats["queue_size"],
                    processing_count=queue_stats["processing"],
                    total_received=queue_stats["total_received"],
                    total_processed=queue_stats["total_processed"],
                    total_skipped=queue_stats["total_skipped"],
                    avg_processing_time_ms=avg_processing_time,
                    active_pipelines=active_pipelines_count,
                    total_faces_detected=total_faces_count,
                    cpu_percent=cpu_percent,
                    memory_percent=memory_percent,
                    disk_usage_gb=disk_usage_gb,
                )

                db.add(metrics_record)
                await db.commit()

                logger.debug(
                    f"[METRICS] Saved: queue={queue_stats['queue_size']}, "
                    f"processing={queue_stats['processing']}, "
                    f"pipelines={active_pipelines_count}, "
                    f"faces={total_faces_count}"
                )

        except Exception as e:
            logger.error(f"Failed to collect/save metrics: {e}")


metrics_collector = SystemMetricsCollector(collection_interval=60)  # Collect every 60 seconds

# =====================================================
# Application Lifespan (Production-Ready)
# =====================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Production-grade application lifecycle management with Redis container support
    """

    startup_time = time.time()
    startup_success = False
    initialized_components = []
    worker_tasks = []

    # ==================== STARTUP PHASE ====================
    try:
        logger.info("=" * 70)
        logger.info("🚀 Starting Face Recognition Service v5.1 (Production)")
        logger.info(f"📊 Face Tracking: {'ENABLED' if FACE_TRACKING_ENABLED else 'DISABLED'}")
        logger.info(f"💾 Redis Cache: {'ENABLED' if CACHE_ENABLED else 'DISABLED'}")
        logger.info("=" * 70)

        # ==================== PHASE 1: Infrastructure ====================
        logger.info("📦 Phase 1: Initializing Infrastructure...")

        # 1.1 Database
        logger.info("  🔄 Initializing database...")
        try:
            await db_manager.init_db()
            logger.info("  ✅ Database initialized")
            initialized_components.append("database")
        except Exception as e:
            logger.error(f"  ❌ Database initialization failed: {e}")
            raise

        # 1.2 Redis Cache (for container)
        logger.info("  🔄 Initializing Redis cache (container)...")
        try:
            # Get Redis URL from settings or environment
            redis_url = getattr(settings, 'REDIS_URL', "redis://redis:6379/0")
            logger.info(f"  📍 Connecting to Redis at: {redis_url}")

            await cache_manager.initialize()
            if cache_manager._enabled:
                logger.info("  ✅ Redis cache connected")
                initialized_components.append("redis_cache")
            else:
                logger.warning("  ⚠️  Redis cache disabled - using in-memory only")
        except Exception as e:
            logger.error(f"  ❌ Redis cache initialization failed: {e}")
            # Non-critical - continue without cache
            cache_manager._enabled = False

        # 1.3 Production Cache Manager
        logger.info("  🔄 Initializing production cache manager...")
        try:
            # Pass Redis client if available
            if cache_manager._enabled and hasattr(cache_manager, 'redis_client'):
                production_cache_manager.redis_client = cache_manager.redis_client
                await production_cache_manager.start()
                logger.info("  ✅ Production cache manager started")
                initialized_components.append("production_cache")
            else:
                logger.warning("  ⚠️  Production cache disabled (no Redis)")
        except Exception as e:
            logger.error(f"  ❌ Production cache failed: {e}")
            # Non-critical

        # ==================== PHASE 2: Machine Learning Models ====================
        logger.info("🧠 Phase 2: Loading ML Models...")

        # 2.1 Face recognition models (synchronous, run in thread pool)
        logger.info("  🔄 Loading face detection models...")
        try:
            # Run model initialization in thread pool (it's CPU intensive)
            await run_in_threadpool(model_manager.initialize)

            # Verify models
            if model_manager.detector and model_manager.recognizer:
                face_count = model_manager.face_db.index.ntotal if model_manager.face_db and model_manager.face_db.index else 0
                logger.info(f"  ✅ Models loaded (detector: {settings.DETECTION_MODEL})")
                logger.info(f"    • Face database: {face_count} faces")
                initialized_components.append("models")
            else:
                raise RuntimeError("Models not properly initialized")
        except Exception as e:
            logger.error(f"  ❌ Model loading failed: {e}")
            raise  # Critical failure

        # ==================== PHASE 3: Core Services ====================
        logger.info("⚙️ Phase 3: Starting Core Services...")

        # 3.1 Face Tracker
        if FACE_TRACKING_ENABLED:
            logger.info("  🔄 Starting face tracker...")
            try:
                await face_tracker.start()
                logger.info(f"  ✅ Face tracker started (window: {FACE_TRACKING_WINDOW_SECONDS}s)")
                initialized_components.append("face_tracker")
            except Exception as e:
                logger.error(f"  ❌ Face tracker failed: {e}")
                # Non-critical, continue without tracker
        else:
            logger.info("  ⏭️  Face tracker disabled")

        # 3.2 Batch Database Writer
        logger.info("  🔄 Starting batch database writer...")
        try:
            await batch_writer.start()
            logger.info(f"  ✅ Batch writer started (batch: {BATCH_WRITE_SIZE})")
            initialized_components.append("batch_writer")
        except Exception as e:
            logger.error(f"  ❌ Batch writer failed: {e}")
            # Performance impact only

        # 3.3 Data Retention Manager
        logger.info("  🔄 Starting data retention manager...")
        try:
            await retention_manager.start()
            logger.info(f"  ✅ Data retention started (keep: {DATA_RETENTION_DAYS} days)")
            initialized_components.append("retention_manager")
        except Exception as e:
            logger.error(f"  ❌ Data retention failed: {e}")
            # Non-critical

        # 3.4 System Metrics Collector
        logger.info("  🔄 Starting system metrics collector...")
        try:
            await metrics_collector.start()
            logger.info(f"  ✅ Metrics collector started (interval: {metrics_collector.collection_interval}s)")
            initialized_components.append("metrics_collector")
        except Exception as e:
            logger.error(f"  ❌ Metrics collector failed: {e}")
            # Non-critical

        # 3.5 Cache Metrics Updater
        logger.info("  🔄 Starting cache metrics updater...")
        try:
            cache_metrics_task = asyncio.create_task(update_cache_metrics())
            logger.info("  ✅ Cache metrics updater started")
            initialized_components.append("cache_metrics")
        except Exception as e:
            logger.error(f"  ❌ Cache metrics failed: {e}")
            # Non-critical

        # ==================== PHASE 4: Worker Pool ====================
        logger.info("👷 Phase 4: Starting Worker Pool...")

        worker_count = getattr(settings, 'QUEUE_WORKERS', 4)

        for i in range(worker_count):
            try:
                worker_task = asyncio.create_task(queue_worker(i + 1))
                worker_tasks.append(worker_task)
                logger.info(f"  ✅ Worker {i + 1}/{worker_count} started")
            except Exception as e:
                logger.error(f"  ❌ Worker {i + 1} failed: {e}")

        if worker_tasks:
            initialized_components.append(f"workers_{len(worker_tasks)}")

        # ==================== PHASE 5: Health Verification ====================
        logger.info("🏥 Phase 5: Health Verification...")

        health_status = {}

        # Database health
        try:
            db_healthy = await db_manager.health_check()
            health_status["database"] = db_healthy
            logger.info(f"  {'✅' if db_healthy else '❌'} Database: {'Healthy' if db_healthy else 'Unhealthy'}")
        except Exception as e:
            health_status["database"] = False
            logger.error(f"  ❌ Database health check failed: {e}")

        # Models health
        try:
            models_healthy = model_manager.health_check()
            health_status["models"] = models_healthy
            logger.info(f"  {'✅' if models_healthy else '❌'} Models: {'Healthy' if models_healthy else 'Unhealthy'}")
        except Exception as e:
            health_status["models"] = False
            logger.error(f"  ❌ Models health check failed: {e}")

        # Redis health (if enabled)
        if CACHE_ENABLED:
            try:
                redis_healthy = await cache_manager.health_check()
                health_status["redis"] = redis_healthy
                logger.info(f"  {'✅' if redis_healthy else '❌'} Redis: {'Healthy' if redis_healthy else 'Unhealthy'}")
            except Exception as e:
                health_status["redis"] = False
                logger.error(f"  ❌ Redis health check failed: {e}")

        # Queue health
        try:
            queue_stats = await processing_queue.get_stats()
            queue_healthy = True  # Just check if we can get stats
            health_status["queue"] = queue_healthy
            logger.info(f"  ✅ Queue: Healthy (size: {queue_stats['queue_size']})")
        except Exception as e:
            health_status["queue"] = False
            logger.error(f"  ❌ Queue health check failed: {e}")

        # Check critical components
        critical_healthy = all([
            health_status.get("database", False),
            health_status.get("models", False)
        ])

        if critical_healthy:
            startup_success = True
            startup_duration = time.time() - startup_time

            logger.info("=" * 70)
            logger.info(f"✅ Service started successfully in {startup_duration:.2f}s")
            logger.info("=" * 70)

            # Emit startup metrics
            metrics_queue_size.set(processing_queue.queue.qsize())

        else:
            logger.error("=" * 70)
            logger.error("❌ Critical components unhealthy - service may not function correctly")
            logger.error("=" * 70)
            # We'll still start but log warning

    except Exception as startup_error:
        logger.error(f"🚨 Startup failed: {startup_error}")
        logger.error("Stack trace:", exc_info=True)

        # Emergency shutdown of initialized components
        await _emergency_shutdown(initialized_components, worker_tasks)

        raise startup_error

    # ==================== RUNNING PHASE ====================
    try:
        # Service is now running
        yield

    finally:
        # ==================== SHUTDOWN PHASE ====================
        shutdown_time = time.time()
        logger.info("=" * 70)
        logger.info("🛑 Starting graceful shutdown...")
        logger.info("=" * 70)

        shutdown_results = {}

        # Stop components in REVERSE order of initialization
        components_to_stop = [
            ("workers", "Stopping worker pool", lambda: worker_tasks),
            ("cache_metrics", "Stopping cache metrics", lambda: cache_metrics_task if 'cache_metrics' in initialized_components else None),
            ("metrics_collector", "Stopping metrics collector", lambda: metrics_collector if 'metrics_collector' in initialized_components else None),
            ("retention_manager", "Stopping data retention", lambda: retention_manager if 'retention_manager' in initialized_components else None),
            ("batch_writer", "Stopping batch writer", lambda: batch_writer if 'batch_writer' in initialized_components else None),
            ("face_tracker", "Stopping face tracker", lambda: face_tracker if 'face_tracker' in initialized_components else None),
            ("production_cache", "Stopping production cache", lambda: production_cache_manager if 'production_cache' in initialized_components else None),
            ("redis_cache", "Closing Redis cache", lambda: cache_manager if 'redis_cache' in initialized_components else None),
            ("models", "Cleaning up models", lambda: model_manager if 'models' in initialized_components else None),
            ("database", "Closing database", lambda: db_manager if 'database' in initialized_components else None),
        ]

        for component_name, description, get_component in components_to_stop:
            try:
                if get_component is None:
                    logger.info(f"  📝 {description}")
                    shutdown_results[component_name] = "skipped"
                    continue

                component = get_component()
                if not component:
                    logger.info(f"  ⚠️  {description} (not initialized)")
                    shutdown_results[component_name] = "not_initialized"
                    continue

                logger.info(f"  🔄 {description}...")
                start = time.time()

                if component_name == "workers":
                    # Cancel all worker tasks
                    for worker_task in component:
                        worker_task.cancel()

                    # Wait for workers to finish (with timeout)
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*component, return_exceptions=True),
                            timeout=15.0
                        )
                        logger.info(f"    ✅ Workers stopped")
                    except asyncio.TimeoutError:
                        logger.warning(f"    ⚠️  Workers shutdown timeout")
                    except Exception as e:
                        logger.error(f"    ❌ Workers shutdown error: {e}")

                elif component_name == "cache_metrics":
                    component.cancel()
                    try:
                        await asyncio.wait_for(component, timeout=5.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass

                elif hasattr(component, 'stop'):
                    # Components with async stop() method
                    await component.stop()
                    logger.info(f"    ✅ Stopped in {(time.time() - start):.2f}s")

                elif hasattr(component, 'close'):
                    # Components with async close() method
                    await component.close()
                    logger.info(f"    ✅ Closed in {(time.time() - start):.2f}s")

                elif component_name == "models":
                    # Models cleanup (synchronous)
                    # No special cleanup needed, Python GC will handle
                    logger.info(f"    ✅ Cleaned up in {(time.time() - start):.2f}s")

                shutdown_results[component_name] = "success"

            except Exception as e:
                logger.error(f"  ❌ Failed to stop {component_name}: {e}")
                shutdown_results[component_name] = f"error: {str(e)}"
                # Continue shutdown even if one component fails

        # Final flush of any pending batch writes
        if 'batch_writer' in initialized_components and hasattr(batch_writer, 'pending_detections'):
            pending_count = len(batch_writer.pending_detections)
            if pending_count > 0:
                logger.info(f"  🔄 Flushing {pending_count} pending batch writes...")
                try:
                    await batch_writer.flush()
                    logger.info(f"    ✅ Batch writes flushed")
                except Exception as e:
                    logger.error(f"    ❌ Batch flush failed: {e}")

        # Wait for any remaining async tasks (with timeout)
        logger.info("  🔄 Cleaning up remaining tasks...")
        try:
            pending_tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending_tasks:
                logger.info(f"    ⏳ Waiting for {len(pending_tasks)} remaining tasks...")
                done, pending = await asyncio.wait(pending_tasks, timeout=10.0, return_when=asyncio.ALL_COMPLETED)
                if pending:
                    logger.warning(f"    ⚠️  {len(pending)} tasks still pending after timeout")
                    # Cancel remaining tasks
                    for task in pending:
                        task.cancel()
        except Exception as e:
            logger.error(f"    ❌ Task cleanup failed: {e}")

        shutdown_duration = time.time() - shutdown_time

        logger.info("=" * 70)
        logger.info(f"✅ Shutdown completed in {shutdown_duration:.2f}s")
        logger.info("=" * 70)

        # Shutdown summary
        successful = sum(1 for r in shutdown_results.values() if r in ["success", "skipped", "not_initialized"])
        total = len(shutdown_results)

        logger.info("📊 Shutdown Summary:")
        logger.info(f"  • Components: {successful}/{total} successful")
        logger.info(f"  • Duration: {shutdown_duration:.2f}s")


# =====================================================
# Emergency Shutdown Helper (UPDATED)
# =====================================================
async def _emergency_shutdown(initialized_components: list, worker_tasks: list = None):
    """Emergency shutdown when startup fails"""
    logger.error("🚨 EMERGENCY SHUTDOWN INITIATED")

    # Stop components in reverse order
    for component in reversed(initialized_components):
        try:
            if component == "database" and hasattr(db_manager, 'close'):
                await db_manager.close()
            elif component == "redis_cache" and hasattr(cache_manager, 'close'):
                await cache_manager.close()
            elif component == "production_cache" and hasattr(production_cache_manager, 'stop'):
                await production_cache_manager.stop()
            elif component == "batch_writer" and hasattr(batch_writer, 'stop'):
                await batch_writer.stop()
            elif component.startswith("workers_") and worker_tasks:
                for task in worker_tasks:
                    task.cancel()
        except Exception as e:
            logger.error(f"Failed to shutdown {component}: {e}")

    logger.error("🛑 Emergency shutdown completed")
# =====================================================
# Cache Metrics Updater (Background Task)
# =====================================================
async def update_cache_metrics():
    """
    Background task to update cache performance metrics
    Runs every 30 seconds
    """
    while True:
        try:
            await asyncio.sleep(30)  # Update every 30 seconds

            if not production_cache_manager._enabled:
                continue

            # Get cache statistics
            cache_stats = await production_cache_manager.get_stats()
            face_cache_stats = await face_recognition_cache.get_performance_stats() if hasattr(face_recognition_cache, 'get_performance_stats') else {}

            # Calculate hit rates
            total_hits = cache_stats['hits']['local'] + cache_stats['hits']['redis']
            total_requests = total_hits + cache_stats['misses']

            if total_requests > 0:
                local_hit_rate = (cache_stats['hits']['local'] / total_requests) * 100
                redis_hit_rate = (cache_stats['hits']['redis'] / total_requests) * 100
                overall_hit_rate = (total_hits / total_requests) * 100
            else:
                local_hit_rate = redis_hit_rate = overall_hit_rate = 0

            # Update Prometheus metrics
            metrics_cache_hit_rate.labels(type='local').set(local_hit_rate)
            metrics_cache_hit_rate.labels(type='redis').set(redis_hit_rate)
            metrics_cache_hit_rate.labels(type='overall').set(overall_hit_rate)
            metrics_cache_size.labels(type='local').set(cache_stats['local_cache_size'])
            metrics_cache_write_behind.inc(cache_stats.get('write_behind_queue_size', 0))

            # Circuit breaker state
            circuit_state_map = {
                "closed": 0,
                "open": 1,
                "half_open": 2
            }
            cb_state = cache_stats['circuit_breaker']['state']
            metrics_cache_circuit_state.set(circuit_state_map.get(cb_state, 3))

            # Log cache performance periodically
            if random.random() < 0.1:  # 10% chance to log
                logger.debug(
                    f"[CACHE METRICS] Hits: local={cache_stats['hits']['local']}, "
                    f"redis={cache_stats['hits']['redis']}, "
                    f"misses={cache_stats['misses']}, "
                    f"hit_rate={overall_hit_rate:.1f}%"
                )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cache metrics update error: {e}")
            await asyncio.sleep(60)  # Wait longer on error

# =====================================================
# FastAPI Application
# =====================================================
app = FastAPI(
    title=getattr(settings, 'APP_NAME', 'Face Recognition Service'),
    version=getattr(settings, 'VERSION', 'V1.0.0'),
    lifespan=lifespan,
)
app.include_router(chatbot_router)
logger.info("Chatbot router included in FastAPI application")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=getattr(settings, 'CORS_ORIGINS', ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)

# =====================================================
# API Endpoints
# =====================================================
@app.post("/webhook/{pipeline_id}")
async def webhook(pipeline_id: str, payload: dict, background_tasks: BackgroundTasks):
    """
    Webhook endpoint to receive images from pipelines
    Handles up to 100 concurrent requests with queuing

    OPTIMIZED: Uses face tracking to avoid duplicate processing
    """
    request_id = str(uuid.uuid4())[:8] # short unique ID for logging

    try:
        # Validate pipeline ID
        pipeline_id = validate_pipeline_id(pipeline_id)

        logger.info(f"[WEBHOOK] Received request {request_id} for pipeline: {pipeline_id}")

        # Extract images (supports multiple formats)
        images_b64 = extract_images_from_payload(payload)
        logger.info(f"[WEBHOOK] Extracted {len(images_b64)} images")

        # Extract predictions
        predictions = payload.get("predictions", [])

        # Handle nested results format
        if not predictions and "results" in payload:
            results = payload.get("results", {})
            predictions = results.get("predictions", [])

        if not images_b64:
            metrics_requests_total.labels(pipeline_id=pipeline_id, status="no_images").inc()
            return {"status": "ok", "message": "No images", "request_id": request_id}

        queued = 0
        dropped = 0

        # Process each image
        for idx, img_b64 in enumerate(images_b64):
            if not img_b64:
                continue

            # Remove data URL prefix
            if "," in img_b64:
                img_b64 = img_b64.split(",", 1)[1]

            try:
                image_bytes = base64.b64decode(img_b64)

                # Validate image content
                if not validate_image_content(image_bytes):
                    logger.warning(f"[WEBHOOK] Invalid image content in request {request_id}, image {idx}")
                    dropped += 1
                    continue

            except Exception as e:
                logger.error(f"[WEBHOOK] Image decode error: {e}")
                dropped += 1
                continue

            # Add to queue
            success = await processing_queue.add({
                "pipeline_id": pipeline_id,
                "image_bytes": image_bytes,
                "predictions": predictions,
                "timestamp": time.time(),
                "request_id": request_id,
            })

            if success:
                queued += 1
            else:
                dropped += 1

        status = "queued" if queued > 0 else "dropped"
        metrics_requests_total.labels(pipeline_id=pipeline_id, status=status).inc()

        return {
            "status": status,
            "request_id": request_id,
            "pipeline_id": pipeline_id,
            "queued": queued,
            "dropped": dropped,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        metrics_requests_total.labels(pipeline_id=pipeline_id, status="error").inc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/detections")
async def get_all_detections(
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db)
):
    """Get recent detections across all pipelines"""
    try:
        result = await db.execute(
            select(Detection)
            .order_by(Detection.timestamp.desc())
            .limit(limit)
            .offset(offset)
        )
        detections = result.scalars().all()

        return {
            "total": len(detections),
            "detections": [
                {
                    "id": d.id,
                    "pipeline_id": d.pipeline_id,
                    "timestamp": d.timestamp.isoformat(),
                    "processing_time_ms": d.processing_time_ms,
                    "faces_count": len(d.faces) if d.faces else 0,
                }
                for d in detections
            ]
        }
    except Exception as e:
        logger.error(f"Error getting detections: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/detections/{pipeline_id}")
async def get_pipeline_detections(
    pipeline_id: str,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """Get detections for specific pipeline with face details"""
    try:
        pipeline_id = validate_pipeline_id(pipeline_id)

        result = await db.execute(
            select(Detection)
            .options(selectinload(Detection.faces))
            .where(Detection.pipeline_id == pipeline_id)
            .order_by(Detection.timestamp.desc())
            .limit(limit)
        )
        detections = result.scalars().all()

        output = []
        for detection in detections:
            # Get faces for this detection
            faces_result = await db.execute(
                select(Face).where(Face.detection_id == detection.id)
            )
            faces = faces_result.scalars().all()

            output.append({
                "detection_id": detection.id,
                "pipeline_id": detection.pipeline_id,
                "timestamp": detection.timestamp.isoformat(),
                "processing_time_ms": detection.processing_time_ms,
                "worker_id": detection.worker_id,
                "faces": [
                    {
                        "id": f.id,
                        "name": f.name,
                        "similarity": f.similarity,
                        "face_image_path": f.face_image_path,
                        "bbox": [f.bbox_x1, f.bbox_y1, f.bbox_x2, f.bbox_y2],
                    }
                    for f in faces
                ]
            })

        return {
            "pipeline_id": pipeline_id,
            "total_detections": len(output),
            "detections": output
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting pipeline detections: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    """Get system statistics (FIXED VERSION)"""
    try:
        # Get database counts
        pipelines_result = await db.execute(
            select(func.count(Pipeline.id)).where(Pipeline.is_active == 1)
        )
        detections_result = await db.execute(select(func.count(Detection.id)))
        faces_result = await db.execute(select(func.count(Face.id)))

        active_pipelines = pipelines_result.scalar() or 0
        total_detections = detections_result.scalar() or 0
        total_faces = faces_result.scalar() or 0

        # Get async stats
        queue_stats = await processing_queue.get_stats()
        storage_stats = await retention_manager.get_storage_stats()

        # Get tracker stats
        tracker_stats = {"enabled": False}
        if FACE_TRACKING_ENABLED:
            try:
                tracker_stats = await face_tracker.get_stats()
            except Exception as e:
                tracker_stats = {"enabled": True, "error": str(e)}

        # Get cache stats
        cache_stats = {"enabled": CACHE_ENABLED, "healthy": False}
        if CACHE_ENABLED:
            try:
                cache_stats["healthy"] = await cache_manager.health_check()
            except Exception:
                pass

        # Get database connection stats
        db_stats = {}
        try:
            db_stats = db_manager.get_connection_stats()
        except Exception:
            pass

        return {
            "service": getattr(settings, 'APP_NAME', 'Face Recognition Service'),
            "version": getattr(settings, 'VERSION', '5.1'),
            "timestamp": datetime.utcnow().isoformat(),
            "pipelines": {
                "active": active_pipelines,
                "total_detections": total_detections,
            },
            "faces": {"total": total_faces},
            "queue": queue_stats,
            "storage": storage_stats,
            "database": db_stats,
            "cache": cache_stats,
            "tracker": tracker_stats,
            "retention_days": DATA_RETENTION_DAYS,
        }

    except Exception as e:
        logger.error(f"Stats error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")



@app.post("/api/upload-person")
async def upload_person(
    person_name: str = Form(...),
    photo: UploadFile = File(...)
):
    """Upload a new person's photo to the face recognition database"""
    try:
        # Validate file type
        if not photo.content_type or not photo.content_type.startswith('image/'):
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Invalid file type. Please upload an image (JPG, PNG)."}
            )

        # Validate file size
        contents = await photo.read()
        if len(contents) > MAX_FILE_SIZE:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": f"File too large. Maximum size is {MAX_FILE_SIZE/1024/1024:.1f}MB."}
            )

        # Validate image content
        if not validate_image_content(contents):
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Invalid image file. Please upload a valid image."}
            )

        # Sanitize person name for filename
        safe_name = "".join(c for c in person_name if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_name = safe_name.replace(' ', '_').lower()

        if not safe_name or len(safe_name) < 2:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Invalid person name. Name must be at least 2 characters."}
            )

        # Determine file extension
        file_extension = ".jpg"
        if photo.filename:
            ext = os.path.splitext(photo.filename)[1].lower()
            if ext in ALLOWED_IMAGE_EXTENSIONS:
                file_extension = ext

        # Create faces directory if it doesn't exist
        faces_dir = settings.FACES_DIR
        os.makedirs(faces_dir, exist_ok=True)

        # Save file with unique name to avoid overwrites
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = os.path.join(faces_dir, f"{safe_name}{file_extension}")

        # Save the uploaded file
        with open(file_path, "wb") as f:
            f.write(contents)

        logger.info(f"✅ Saved new person photo: {file_path}")

        # Rebuild face database to include new person
        try:
            model_manager._build_face_database()
            logger.info(f"✅ Face database rebuilt with new person: {safe_name}")

            return JSONResponse(
                content={
                    "success": True,
                    "message": f"Successfully added {person_name} to tracking database.",
                    "filename": f"{safe_name}{file_extension}",
                    "total_faces": model_manager.face_db.index.ntotal if model_manager.face_db and model_manager.face_db.index else 0
                }
            )
        except Exception as e:
            logger.error(f"❌ Error rebuilding face database: {e}")
            return JSONResponse(
                content={
                    "success": True,
                    "message": f"Photo saved but face database rebuild failed. Please restart the service.",
                    "filename": f"{safe_name}{file_extension}",
                    "warning": "Face database needs to be rebuilt"
                }
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error uploading person: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"Upload failed: {str(e)}"}
        )


@app.post("/api/cleanup/manual")
async def manual_cleanup(background_tasks: BackgroundTasks):
    """Manually trigger cleanup of old data"""
    background_tasks.add_task(retention_manager.cleanup_old_data)
    return {"status": "cleanup_scheduled", "message": "Cleanup task started in background"}


@app.get("/api/circuit-breaker/status")
async def circuit_breaker_status():
    """Get circuit breaker status"""
    return await db_circuit_breaker.get_status()


# @app.get("/api/face-tracker/stats")
# async def face_tracker_stats():
#     """Get face tracker statistics"""
#     if not FACE_TRACKING_ENABLED:
#         return {"enabled": False, "message": "Face tracking is disabled"}

#     return await face_tracker.get_stats()
@app.get("/api/face-tracker/stats")
async def face_tracker_stats_fixed():
    """Get face tracker statistics (FIXED)"""
    if not FACE_TRACKING_ENABLED:
        return {"enabled": False, "message": "Face tracking is disabled"}

    try:
        return await face_tracker.get_stats()
    except Exception as e:
        logger.error(f"Face tracker stats error: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/api/face-tracker/reset/{pipeline_id}")
async def reset_face_tracker(pipeline_id: str):
    """Reset face tracker for a specific pipeline"""
    if not FACE_TRACKING_ENABLED:
        raise HTTPException(status_code=400, detail="Face tracking is disabled")

    try:
        pipeline_id = validate_pipeline_id(pipeline_id)
        async with face_tracker._lock:
            if pipeline_id in face_tracker.tracked_faces:
                del face_tracker.tracked_faces[pipeline_id]
                logger.info(f"Reset face tracker for pipeline: {pipeline_id}")
                return {"success": True, "message": f"Face tracker reset for pipeline {pipeline_id}"}
            else:
                return {"success": True, "message": f"No tracked faces for pipeline {pipeline_id}"}
    except Exception as e:
        logger.error(f"Error resetting face tracker: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring"""
    try:
        # Check database health
        db_healthy = await db_manager.health_check()

        # Check cache health
        cache_healthy = await cache_manager.health_check() if CACHE_ENABLED else True

        # Check models health
        models_healthy = model_manager.health_check()

        # Check queue health
        queue_healthy = True
        try:
            queue_stats = await processing_queue.get_stats()
            queue_healthy = queue_stats["processing"] < getattr(settings, 'MAX_CONCURRENT_REQUESTS', 100) * 2
        except:
            queue_healthy = False

        # Check face tracker health
        tracker_healthy = True
        if FACE_TRACKING_ENABLED:
            try:
                await face_tracker.get_stats()
            except:
                tracker_healthy = False

        # Overall status
        all_healthy = db_healthy and cache_healthy and models_healthy and queue_healthy and tracker_healthy
        overall_status = "healthy" if all_healthy else "degraded"

        return {
            "status": overall_status,
            "version": getattr(settings, 'VERSION', '5.1'),
            "timestamp": datetime.utcnow().isoformat(),
            "components": {
                "database": "healthy" if db_healthy else "unhealthy",
                "cache": "healthy" if cache_healthy else "unhealthy" if CACHE_ENABLED else "disabled",
                "models": "healthy" if models_healthy else "unhealthy",
                "queue": "healthy" if queue_healthy else "unhealthy",
                "face_tracker": "healthy" if tracker_healthy else "unhealthy" if FACE_TRACKING_ENABLED else "disabled",
            },
            "queue_size": processing_queue.queue.qsize(),
            "processing": processing_queue.processing_count,
            "websocket_connections": len(ws_manager.active_connections),
            "circuit_breaker": db_circuit_breaker.state.value,
        }
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


@app.get("/health/detailed")
async def detailed_health_check():
    """Detailed health check for all components"""
    try:
        checks = {}

        # Database health
        db_healthy = await db_manager.health_check()
        checks["database"] = {
            "healthy": db_healthy,
            "connections": db_manager.get_connection_stats()
        }

        # Redis health
        if CACHE_ENABLED:
            cache_healthy = await cache_manager.health_check()
            checks["redis"] = {
                "healthy": cache_healthy,
                "enabled": True
            }
        else:
            checks["redis"] = {"enabled": False}

        # Face tracker health
        checks["face_tracker"] = {
            "enabled": FACE_TRACKING_ENABLED,
            "healthy": True if not FACE_TRACKING_ENABLED else await face_tracker.get_stats() is not None
        }

        # Model health
        checks["models"] = {
            "healthy": model_manager.health_check(),
            "initialized": model_manager._initialized
        }

        # Queue health
        queue_stats = await processing_queue.get_stats()
        checks["queue"] = {
            "healthy": queue_stats["processing"] < getattr(settings, 'MAX_CONCURRENT_REQUESTS', 100) * 2,
            "stats": queue_stats
        }

        # Storage health
        storage_stats = await retention_manager.get_storage_stats()
        checks["storage"] = {
            "healthy": storage_stats["usage_percent"] < 95,
            "stats": storage_stats
        }

        # Overall health
        overall_healthy = all(
            check.get("healthy", True) if isinstance(check, dict) else check
            for check in checks.values()
        )

        return {
            "healthy": overall_healthy,
            "timestamp": datetime.utcnow().isoformat(),
            "components": checks
        }
    except Exception as e:
        logger.error(f"Detailed health check error: {e}")
        return {
            "healthy": False,
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint"""
    try:
        return Response(
            content=generate_latest().decode('utf-8'),
            media_type=CONTENT_TYPE_LATEST
        )
    except Exception as e:
        logger.error(f"Metrics generation error: {e}")
        return Response(content="", media_type=CONTENT_TYPE_LATEST)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time dashboard updates"""
    await ws_manager.connect(websocket)
    logger.info(f"[WS] New connection established")

    try:
        # Send initial data from database
        async with db_manager.get_session() as db:
            # Get recent detections with faces
            result = await db.execute(
                select(Detection)
                .order_by(Detection.timestamp.desc())
                .limit(50)
            )
            detections = result.scalars().all()

            # Build detection data with faces
            initial_data = []
            for detection in detections:
                faces_result = await db.execute(
                    select(Face).where(Face.detection_id == detection.id).limit(10)
                )
                faces = faces_result.scalars().all()

                initial_data.append({
                    "detection_id": detection.id,
                    "pipeline_id": detection.pipeline_id,
                    "timestamp": detection.timestamp.isoformat(),
                    "processing_time_ms": detection.processing_time_ms,
                    "faces_count": len(faces),
                    "faces_preview": [
                        {
                            "name": f.name,
                            "similarity": f.similarity,
                        }
                        for f in faces[:3]  # Only send first 3 for preview
                    ]
                })

            # Get system stats
            queue_stats = await processing_queue.get_stats()
            try:
                tracker_stats = await face_tracker.get_stats() if FACE_TRACKING_ENABLED else {"enabled": False}
            except Exception as e:
                logger.error(f"Face tracker stats error: {e}")
                tracker_stats = {"enabled": FACE_TRACKING_ENABLED, "error": str(e)}

            try:
                storage_stats = await retention_manager.get_storage_stats()
            except Exception as e:
                logger.error(f"Storage stats error: {e}")
                storage_stats = {"total_size_mb": 0, "file_count": 0, "error": str(e)}

            await websocket.send_json({
                "type": "initial_data",
                "data": {
                    "detections": initial_data,
                    "stats": queue_stats,
                    "tracker_stats": tracker_stats,
                    "storage_stats": storage_stats,
                    "timestamp": datetime.utcnow().isoformat()
                }
            })

        # Keep alive loop
        while True:
            try:
                # Wait for ping message or timeout
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)

                # Handle different message types
                if data == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": datetime.utcnow().isoformat()})
                elif data.startswith("subscribe:"):
                    # Handle subscription requests
                    channel = data.split(":", 1)[1]
                    logger.info(f"[WS] Client subscribed to channel: {channel}")
                    await websocket.send_json({
                        "type": "subscription_confirmed",
                        "channel": channel,
                        "timestamp": datetime.utcnow().isoformat()
                    })

            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                try:
                    await websocket.send_json({"type": "ping", "timestamp": datetime.utcnow().isoformat()})
                except:
                    break  # Connection lost
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"[WS] Error: {e}")
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"[WS] Fatal error: {e}")
    finally:
        await ws_manager.disconnect(websocket)


@app.get("/dashboard")
async def dashboard():
    """Production Dashboard HTML page"""
    try:
        return FileResponse("dashboard_production.html")
    except:
        return JSONResponse(
            content={
                "message": "Dashboard HTML file not found. Running in API-only mode.",
                "api_endpoints": {
                    "webhook": "/webhook/{pipeline_id}",
                    "detections": "/api/detections",
                    "stats": "/api/stats",
                    "health": "/health",
                    "metrics": "/metrics",
                    "upload_person": "/api/upload-person"
                }
            }
        )


@app.get("/")
async def root():
    """API information and documentation"""
    return {
        "service": getattr(settings, 'APP_NAME', 'Face Recognition Service'),
        "version": getattr(settings, 'VERSION', '5.1'),
        "status": "running",
        "documentation": "Visit /docs for Swagger UI or /redoc for ReDoc",
        "optimizations": {
            "face_tracking": FACE_TRACKING_ENABLED,
            "batch_writes": True,
            "save_only_faces": getattr(settings, 'SAVE_IMAGES', True),
            "deduplication": "enabled",
            "real_time_updates": "enabled",
        },
        "configuration": {
            "data_retention_days": DATA_RETENTION_DAYS,
            "queue_workers": getattr(settings, 'QUEUE_WORKERS', 4),
            "max_concurrent": getattr(settings, 'MAX_CONCURRENT_REQUESTS', 100),
            "max_queue_size": getattr(settings, 'MAX_QUEUE_SIZE', 1000),
            "similarity_threshold": getattr(settings, 'SIMILARITY_THRESHOLD', 0.6),
            "confidence_threshold": getattr(settings, 'CONFIDENCE_THRESHOLD', 0.5),
        },
        "endpoints": {
            "webhook": {
                "method": "POST",
                "path": "/webhook/{pipeline_id}",
                "description": "Receive images from pipelines for face recognition"
            },
            "detections": {
                "method": "GET",
                "path": "/api/detections",
                "description": "Get recent detections across all pipelines"
            },
            "stats": {
                "method": "GET",
                "path": "/api/stats",
                "description": "Get system statistics"
            },
            "health": {
                "method": "GET",
                "path": "/health",
                "description": "Health check endpoint"
            },
            "metrics": {
                "method": "GET",
                "path": "/metrics",
                "description": "Prometheus metrics"
            },
            "upload_person": {
                "method": "POST",
                "path": "/api/upload-person",
                "description": "Upload new person to face database"
            },
            "websocket": {
                "method": "GET",
                "path": "/ws",
                "description": "WebSocket for real-time updates"
            },
            "dashboard": {
                "method": "GET",
                "path": "/dashboard",
                "description": "Production dashboard"
            }
        }
    }


@app.get("/docs/overview")
async def api_overview():
    """API overview with examples"""
    return {
        "overview": "Face Recognition Service API",
        "quick_start": {
            "1_webhook": {
                "description": "Send images for face recognition",
                "curl_example": """curl -X POST "http://localhost:8000/webhook/pipeline123" \\
  -H "Content-Type: application/json" \\
  -d '{
    "predictions": [
      {
        "class_name": "person",
        "bbox": [100, 100, 200, 200],
        "confidence": 0.95
      }
    ],
    "image": "base64_encoded_image_data"
  }'"""
            },
            "2_check_detections": {
                "description": "Get recent detections",
                "curl_example": 'curl "http://localhost:8000/api/detections/pipeline123"'
            },
            "3_upload_person": {
                "description": "Add new person to database",
                "curl_example": """curl -X POST "http://localhost:8000/api/upload-person" \\
  -F "person_name=John Doe" \\
  -F "photo=@/path/to/photo.jpg" """
            }
        }
    }


# =====================================================
# Error Handlers
# =====================================================
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Handle HTTP exceptions"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "path": request.url.path,
            "timestamp": datetime.utcnow().isoformat()
        }
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """Handle general exceptions"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "path": request.url.path,
            "timestamp": datetime.utcnow().isoformat()
        }
    )

# =====================================================
# Add these endpoints to your FastAPI app

@app.get("/api/cache/stats")
async def get_cache_stats():
    """Get detailed cache statistics"""
    return {
        "cache_manager": await production_cache_manager.get_stats(),
        "face_cache": await face_recognition_cache.get_performance_stats(),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.post("/api/cache/warm/{pipeline_id}")
async def warm_cache_for_pipeline(pipeline_id: str, limit: int = 100, db: AsyncSession = Depends(get_db)):
    """Warm cache for a specific pipeline"""
    # Get recent detections
    result = await db.execute(
        select(Detection)
        .where(Detection.pipeline_id == pipeline_id)
        .order_by(Detection.timestamp.desc())
        .limit(limit)
    )
    detections = result.scalars().all()

    warmed = 0
    for detection in detections:
        # Cache detection metadata
        await face_recognition_cache.cache_detection_result(
            pipeline_id,
            detection.id,
            [{"name": f.name, "similarity": f.similarity} for f in detection.faces]
        )
        warmed += 1

    return {
        "status": "success",
        "pipeline_id": pipeline_id,
        "detections_warmed": warmed,
        "message": f"Warmed cache for {warmed} detections"
    }

@app.post("/api/cache/clear")
async def clear_cache(background_tasks: BackgroundTasks, pattern: str = "*"):
    """Clear cache (with safety limits)"""
    if not production_cache_manager._enabled:
        raise HTTPException(status_code=400, detail="Cache not enabled")

    # Add safety: only allow clearing specific patterns
    allowed_patterns = ["stats:*", "detection:*", "face:*"]
    if pattern not in allowed_patterns and pattern != "*":
        raise HTTPException(
            status_code=400,
            detail=f"Pattern not allowed. Allowed: {allowed_patterns}"
        )

    async def safe_clear():
        try:
            keys = await production_cache_manager.redis_client.keys(pattern)
            if keys:
                await production_cache_manager.redis_client.delete(*keys)
                logger.info(f"Cleared {len(keys)} cache keys with pattern: {pattern}")
        except Exception as e:
            logger.error(f"Cache clear error: {e}")

    background_tasks.add_task(safe_clear)

    return {
        "status": "clearing_started",
        "pattern": pattern,
        "message": "Cache clear initiated in background"
    }

@app.get("/api/cache/health")
async def cache_health():
    """Detailed cache health check"""
    cache_stats = await production_cache_manager.get_stats()

    healthy = (
        production_cache_manager._enabled and
        production_cache_manager.circuit_breaker.state == CircuitState.CLOSED
    )
    import asyncio
    assert not asyncio.iscoroutine(cache_stats["circuit_breaker"])


    return {
        "healthy": healthy,
        "enabled": production_cache_manager._enabled,
        "circuit_breaker": cache_stats['circuit_breaker'],
        "local_cache": {
            "size": cache_stats['local_cache_size'],
            "capacity": cache_stats['local_cache_capacity'],
            "usage_percent": (cache_stats['local_cache_size'] / cache_stats['local_cache_capacity'] * 100) if cache_stats['local_cache_capacity'] > 0 else 0
        },
        "performance": await face_recognition_cache.get_performance_stats()
    }

# @app.on_event("startup")
# async def load_models():
#     model_manager.initialize()
#     await batch_writer.start()
#     logger.info("✅ Batch writer started")

# @app.on_event("shutdown")
# async def shutdown_event():
#     logger.info("🛑 Starting graceful shutdown...")
#     try:
#         await batch_writer.stop()
#         logger.info("✅ Batch writer stopped and flushed")
#     except Exception as e:
#         logger.error(f"❌ Error during shutdown: {e}")
@app.get("/ping")
def ping():
    return {"status": "ok"}
