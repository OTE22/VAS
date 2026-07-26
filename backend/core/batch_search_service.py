"""
Batch Search Service
=====================
Handle batch search operations with multiple images.

NOTE: This service handles database session management carefully for concurrent operations.
Each concurrent image processing task gets its own database session to avoid 
"another operation is in progress" errors from asyncpg.
"""

import logging
import uuid
import asyncio
import hashlib
from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field

import numpy as np
import cv2

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db_models import SearchHistory, SearchType
from backend.core.face_quality import assess_face_quality
from config import settings
from db_connection import db_manager

logger = logging.getLogger(__name__)


@dataclass
class BatchImageResult:
    """Result for a single image in batch"""
    image_index: int
    image_name: str
    status: str  # "success", "error", "no_face", "low_quality"
    error_message: Optional[str] = None
    faces_detected: int = 0
    faces_searched: int = 0
    matches: List[Dict] = field(default_factory=list)
    quality_scores: List[float] = field(default_factory=list)
    processing_time_ms: int = 0


@dataclass
class BatchSearchResult:
    """Complete batch search result"""
    batch_id: str
    total_images: int
    successful_images: int
    failed_images: int
    total_faces_detected: int
    total_matches: int
    unique_identities: int
    watchlist_alerts: int
    results: List[BatchImageResult]
    processing_time_ms: int


class BatchSearchService:
    """
    Service for batch face search operations.
    
    Features:
    - Process multiple images in parallel
    - Aggregate results across images
    - Track unique identities found
    - Watchlist checking across all images
    """
    
    def __init__(self, advanced_search_service=None):
        self.advanced_search_service = advanced_search_service
        self._initialized = False
    
    def initialize(self, advanced_search_service):
        """Initialize with advanced search service."""
        self.advanced_search_service = advanced_search_service
        self._initialized = True
        logger.info("[BATCH_SEARCH] Service initialized")
    
    @property
    def is_initialized(self) -> bool:
        return self._initialized and self.advanced_search_service is not None
    
    async def search_batch(
        self,
        db: AsyncSession,
        images: List[Tuple[str, np.ndarray]],  # [(filename, image), ...]
        user_id: int,
        scope: str = "both",
        top_k: int = 5,
        min_quality: float = None,
        check_watchlist: bool = True,
        ip_address: str = None,
        user_agent: str = None
    ) -> BatchSearchResult:
        """
        Search multiple images for faces.
        
        Args:
            db: Database session (used only for final logging, not concurrent operations)
            images: List of (filename, image_array) tuples
            user_id: User performing the search
            scope: "known", "unknown", or "both"
            top_k: Results per face
            min_quality: Minimum quality threshold
            check_watchlist: Check against watchlists
            
        Returns:
            BatchSearchResult with aggregated results
            
        NOTE: Each concurrent image processing task gets its own database session
        to avoid asyncpg "another operation is in progress" errors.
        """
        import time
        start_time = time.time()
        batch_id = str(uuid.uuid4())
        
        logger.info(f"[BATCH_SEARCH] Starting batch {batch_id} with {len(images)} images")
        
        if not self.is_initialized:
            raise RuntimeError("Batch search service not initialized")
        
        max_images = settings.BATCH_SEARCH_MAX_IMAGES
        if len(images) > max_images:
            images = images[:max_images]
            logger.warning(f"[BATCH_SEARCH] Truncated batch to {max_images} images")
        
        results = []
        all_identity_ids = set()
        total_watchlist_alerts = 0
        total_faces = 0
        total_matches = 0
        
        # Process images with concurrency limit
        # IMPORTANT: Limit concurrency to avoid overwhelming the database pool
        max_concurrent = min(5, settings.DB_POOL_SIZE // 10 if hasattr(settings, 'DB_POOL_SIZE') else 5)
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def process_single_image(idx: int, filename: str, image: np.ndarray):
            """Process a single image with its own database session."""
            async with semaphore:
                img_start = time.time()
                
                try:
                    # CRITICAL: Get a fresh database session for each concurrent task
                    # This avoids asyncpg "another operation is in progress" errors
                    async with db_manager.get_session() as task_db:
                        # Use the advanced search service with task-specific session
                        search_result = await self.advanced_search_service.search_multi_face(
                            image=image,
                            db=task_db,
                            user_id=user_id,
                            scope=scope,
                            top_k=top_k,
                            min_quality=min_quality,
                            check_watchlist=check_watchlist,
                            ip_address=ip_address,
                            user_agent=user_agent
                        )
                        
                        # Extract matches
                        matches = []
                        quality_scores = []
                        for face in search_result.faces:
                            quality_scores.append(face.quality_score)
                            for match in face.matches:
                                matches.append({
                                    'face_index': face.face_index,
                                    'identity_id': match.identity_id,
                                    'display_name': match.display_name,
                                    'similarity': match.similarity,
                                    'confidence_band': match.confidence_band,
                                    'type': match.type,
                                    'watchlist_match': match.watchlist_match,
                                    'snapshot_url': match.snapshot_url,  # Include snapshot URL for image display
                                    'best_snapshot_path': match.best_snapshot_path,  # Include path as fallback
                                    'last_seen_at': match.last_seen_at.isoformat() if match.last_seen_at else None,
                                    'appearances_count': match.appearances_count or 0
                                })
                        
                        img_time = int((time.time() - img_start) * 1000)
                        logger.debug(f"[BATCH_SEARCH] Image {idx} ({filename}) processed in {img_time}ms")
                        
                        return BatchImageResult(
                            image_index=idx,
                            image_name=filename,
                            status="success" if search_result.faces else "no_face",
                            faces_detected=search_result.summary['total_faces_detected'],
                            faces_searched=search_result.summary['faces_searchable'],
                            matches=matches,
                            quality_scores=quality_scores,
                            processing_time_ms=img_time
                        ), search_result.summary['watchlist_alerts'], matches, search_result.summary['total_faces_detected']
                    
                except Exception as e:
                    logger.error(f"[BATCH_SEARCH] Error processing image {idx} ({filename}): {e}", exc_info=True)
                    return BatchImageResult(
                        image_index=idx,
                        image_name=filename,
                        status="error",
                        error_message=str(e),
                        processing_time_ms=int((time.time() - img_start) * 1000)
                    ), 0, [], 0
        
        # Process all images
        tasks = [
            process_single_image(idx, filename, image)
            for idx, (filename, image) in enumerate(images)
        ]
        
        batch_results = await asyncio.gather(*tasks)
        
        # Aggregate results
        successful = 0
        failed = 0
        
        for result, watchlist_count, matches_list, face_count in batch_results:
            results.append(result)
            total_watchlist_alerts += watchlist_count
            total_faces += face_count
            
            # Aggregate matches and unique identities
            if isinstance(matches_list, list):
                total_matches += len(matches_list)
                for match in matches_list:
                    if match.get('identity_id'):
                        all_identity_ids.add(match['identity_id'])
            
            if result.status == "success":
                successful += 1
            elif result.status == "error":
                failed += 1
        
        total_time = int((time.time() - start_time) * 1000)
        
        logger.info(
            f"[BATCH_SEARCH] Batch {batch_id} completed: "
            f"{successful}/{len(images)} successful, "
            f"{total_faces} faces, {total_matches} matches, "
            f"{len(all_identity_ids)} unique identities in {total_time}ms"
        )
        
        # Log batch search history
        await self._log_batch_history(
            db=db,
            user_id=user_id,
            batch_id=batch_id,
            total_images=len(images),
            results_count=total_matches,
            unique_identities=len(all_identity_ids),
            watchlist_alerts=total_watchlist_alerts,
            processing_time_ms=total_time,
            ip_address=ip_address,
            user_agent=user_agent
        )
        
        return BatchSearchResult(
            batch_id=batch_id,
            total_images=len(images),
            successful_images=successful,
            failed_images=failed,
            total_faces_detected=total_faces,
            total_matches=total_matches,
            unique_identities=len(all_identity_ids),
            watchlist_alerts=total_watchlist_alerts,
            results=results,
            processing_time_ms=total_time
        )
    
    async def _log_batch_history(
        self,
        db: AsyncSession,
        user_id: int,
        batch_id: str,
        total_images: int,
        results_count: int,
        unique_identities: int,
        watchlist_alerts: int,
        processing_time_ms: int,
        ip_address: str,
        user_agent: str
    ):
        """Log batch search to history."""
        try:
            history = SearchHistory(
                id=uuid.UUID(batch_id),
                user_id=user_id,
                search_type=SearchType.BATCH,
                input_faces_count=total_images,  # Using this field for image count
                results_count=results_count,
                unique_identities_count=unique_identities,
                watchlist_alerts_count=watchlist_alerts,
                processing_time_ms=processing_time_ms,
                ip_address=ip_address,
                user_agent=user_agent
            )
            db.add(history)
            await db.commit()
            logger.debug(f"[BATCH_SEARCH] Logged batch history: {batch_id}")
        except Exception as e:
            logger.error(f"[BATCH_SEARCH] Failed to log batch history: {e}")
            await db.rollback()
    
    def to_dict(self, result: BatchSearchResult) -> Dict:
        """Convert BatchSearchResult to dictionary."""
        return {
            "batch_id": result.batch_id,
            "total_images": result.total_images,
            "successful_images": result.successful_images,
            "failed_images": result.failed_images,
            "total_faces_detected": result.total_faces_detected,
            "total_matches": result.total_matches,
            "unique_identities": result.unique_identities,
            "watchlist_alerts": result.watchlist_alerts,
            "results": [
                {
                    "image_index": r.image_index,
                    "image_name": r.image_name,
                    "status": r.status,
                    "error_message": r.error_message,
                    "faces_detected": r.faces_detected,
                    "faces_searched": r.faces_searched,
                    "matches": r.matches,
                    "quality_scores": r.quality_scores,
                    "processing_time_ms": r.processing_time_ms
                }
                for r in result.results
            ],
            "processing_time_ms": result.processing_time_ms
        }


# Global instance
batch_search_service = BatchSearchService()

