"""
Face Tracking Manager
=====================
Tracks identified faces to avoid re-processing the same person.
"""

import asyncio
import hashlib
import time
import logging
from typing import Dict, Tuple
from collections import defaultdict
import numpy as np

from backend.config import (
    FACE_TRACKING_ENABLED,
    FACE_TRACKING_WINDOW_SECONDS,
    FACE_TRACKING_MAX_ENTRIES,
    FACE_TRACKING_SIMILARITY_THRESHOLD,
    FACE_TRACKING_MAX_MEMORY_MB
)
from config import settings
from backend.core.metrics import metrics_faces_skipped
from utils.helpers import compute_similarity

logger = logging.getLogger(__name__)

# Face Tracking Manager (IMPROVED)
class FaceTracker:
    """
    Tracks identified faces to avoid re-processing the same person
    Uses embedding similarity + time-based expiration with memory limits
    
    Separate tracking for:
    - Database writes: Uses window_seconds (default 30s) to prevent duplicate DB entries
    - Frontend notifications: Uses frontend_notification_window (2 hours) to allow re-notifications
    """

    __slots__ = ['window_seconds', 'max_entries', 'max_memory_mb', 'tracked_faces',
                'frontend_notification_times', 'max_memory_bytes', '_lock', '_cleanup_task',
                'total_faces_added', 'total_faces_skipped', 'total_duplicates_prevented', '_memory_estimate_bytes',
                '_notification_window_override']

    def __init__(self, window_seconds: int = FACE_TRACKING_WINDOW_SECONDS,
                max_entries: int = FACE_TRACKING_MAX_ENTRIES,
                max_memory_mb: int = FACE_TRACKING_MAX_MEMORY_MB,
                frontend_notification_window: int = None):  # Configurable - defaults from settings
        self.window_seconds = window_seconds
        # Explicit ctor override (tests); otherwise the window is a LIVE property
        # read from settings so admin changes to ALERT_NOTIFICATION_WINDOW_HOURS
        # take effect immediately (previously frozen at import into the singleton).
        self._notification_window_override = frontend_notification_window
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

    @property
    def frontend_notification_window(self) -> int:
        """Alert dedup window in seconds — LIVE read from settings each use,
        so ALERT_NOTIFICATION_WINDOW_HOURS changes apply without restart."""
        if self._notification_window_override is not None:
            return self._notification_window_override
        notification_hours = settings.ALERT_NOTIFICATION_WINDOW_HOURS
        return int(float(notification_hours) * 60 * 60)

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
                metrics_faces_skipped.inc()
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
                    expired_notifications = []
                    for key, value in self.frontend_notification_times.items():
                        # Handle both tuple (time, similarity) and old float format
                        if isinstance(value, tuple):
                            timestamp, _ = value
                        else:
                            timestamp = value  # Old format (just timestamp)
                        
                        if current_time - timestamp > frontend_cleanup_threshold:
                            expired_notifications.append(key)
                    
                    for key in expired_notifications:
                        del self.frontend_notification_times[key]

                    if removed_count > 0 or expired_notifications:
                        logger.info(f"[TRACKER] Cleaned up {removed_count} expired faces, {len(expired_notifications)} expired notification times")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[TRACKER] Cleanup error: {e}")

    async def should_send_frontend_notification(self, pipeline_id: str, name: str, current_similarity: float) -> bool:
        """
        Check if we should send frontend notification
        Returns True if we should send:
        - First time seeing this person
        - After 1 hour since last notification (to save bandwidth)
        
        Within 1 hour: Drop notification completely (don't send to frontend)
        """
        async with self._lock:
            current_time = time.time()
            key = f"{pipeline_id}:{name}"
            
            if key not in self.frontend_notification_times:
                # First time seeing this person in this pipeline
                self.frontend_notification_times[key] = (current_time, current_similarity)
                logger.debug(f"[FRONTEND NOTIF] First notification for {name} in {pipeline_id} (sim={current_similarity:.3f})")
                return True
            
            last_notification_time, last_similarity = self.frontend_notification_times[key]
            time_since_last = current_time - last_notification_time
            
            # Check if 1 hour has passed (changed from 2 hours to save bandwidth)
            if time_since_last >= self.frontend_notification_window:
                # More than 1 hour since last notification - send again
                self.frontend_notification_times[key] = (current_time, current_similarity)
                logger.info(f"[FRONTEND NOTIF] Re-notifying {name} in {pipeline_id} after {time_since_last/3600:.1f} hours")
                return True
            
            # Less than 1 hour - drop notification to save bandwidth (regardless of confidence)
            logger.debug(f"[FRONTEND NOTIF] ⏭️ Dropping notification for {name} in {pipeline_id} (within 1-hour window, last sent {time_since_last/60:.1f} min ago) - saving bandwidth")
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


# =====================================================
# Global Instance
# =====================================================
face_tracker = FaceTracker(
    window_seconds=FACE_TRACKING_WINDOW_SECONDS,
    max_entries=FACE_TRACKING_MAX_ENTRIES,
    max_memory_mb=FACE_TRACKING_MAX_MEMORY_MB
    # frontend_notification_window is now loaded from settings.ALERT_NOTIFICATION_WINDOW_HOURS
)