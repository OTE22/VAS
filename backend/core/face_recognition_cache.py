"""
Face Recognition Cache
======================
Main integration point - wraps existing functionality with caching.
"""

import os
import sys
import logging
import time
import numpy as np

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

logger = logging.getLogger(__name__)


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


# Global instance (will be set during startup in lifespan.py)
face_recognition_cache = None