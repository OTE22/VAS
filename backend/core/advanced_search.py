"""
Advanced Search Service
========================
Provides production-grade face search capabilities including:
- Multi-face detection and search
- Quality scoring
- Watchlist checking
- Search history logging
- Confidence band grouping
- Batch search support
"""

import hashlib
import logging
import time
import uuid
from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict
import numpy as np
import cv2

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
from sqlalchemy.orm import selectinload

from config import settings
from db_models import (
    Identity, IdentityType, Watchlist, WatchlistEntry, WatchlistAlert,
    SearchHistory, SearchType
)
from backend.core.face_quality import assess_face_quality, QualityAssessment

# Import path_to_url utility - handle import errors gracefully
try:
    from backend.utils.path_utils import path_to_url
except ImportError:
    # Fallback: define a simple path_to_url function if import fails
    def path_to_url(path: str) -> str:
        """Convert file path to URL format."""
        if not path:
            return 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="100" height="100"%3E%3Crect fill="%23333" width="100" height="100"/%3E%3Ccircle cx="50" cy="35" r="15" fill="%23999"/%3E%3Cpath d="M 25 70 Q 25 60 35 60 L 65 60 Q 75 60 75 70 L 75 85 L 25 85 Z" fill="%23999"/%3E%3C/svg%3E'
        if path.startswith('storage/'):
            return f"/{path}"
        elif not path.startswith('/'):
            return f"/storage/{path}"
        else:
            return path

logger = logging.getLogger(__name__)


@dataclass
class FaceSearchResult:
    """Individual face match result"""
    identity_id: str
    display_name: Optional[str]
    type: str  # "known" or "unknown"
    similarity: float
    confidence_band: str
    best_snapshot_path: Optional[str] = None
    snapshot_url: Optional[str] = None  # Backend provides ready-to-use URL
    last_seen_at: Optional[datetime] = None
    appearances_count: int = 0
    watchlist_match: Optional[Dict] = None


@dataclass
class FaceInImage:
    """Detected face in uploaded image"""
    face_index: int
    bounding_box: Dict[str, int]  # x1, y1, x2, y2
    quality_score: float
    quality_details: Dict
    quality_warning: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None
    matches: List[FaceSearchResult] = field(default_factory=list)
    embedding: Optional[np.ndarray] = None


@dataclass
class WatchlistAlertInfo:
    """Watchlist alert information"""
    face_index: int
    identity_id: str
    identity_name: Optional[str]
    watchlist_id: str
    list_name: str
    alert_level: str
    priority: str
    notes: Optional[str]
    action_instructions: Optional[str]
    similarity: float


@dataclass
class MultiSearchResult:
    """Result of multi-face search"""
    search_id: str
    image_info: Dict
    faces: List[FaceInImage]
    watchlist_alerts: List[WatchlistAlertInfo]
    processing_time_ms: int
    summary: Dict


class AdvancedSearchService:
    """
    Advanced search service with multi-face detection, quality scoring,
    watchlist checking, and search history logging.
    """
    
    def __init__(self, model_manager=None, identity_index=None, pgvector_index=None):
        self.model_manager = model_manager
        self.identity_index = identity_index  # FAISS backend
        self.pgvector_index = pgvector_index  # pgvector backend
        self._initialized = False
        
        # Check which backend to use
        try:
            from config import settings
            self.use_pgvector = getattr(settings, 'VECTOR_BACKEND', 'faiss').lower() == 'pgvector' and pgvector_index is not None
        except:
            self.use_pgvector = False
    
    def initialize(self, model_manager, identity_index, pgvector_index=None):
        """Initialize with model manager and identity index."""
        self.model_manager = model_manager
        self.identity_index = identity_index  # FAISS backend
        self.pgvector_index = pgvector_index  # pgvector backend
        self._initialized = True
        
        # Check which backend to use
        try:
            from config import settings
            self.use_pgvector = getattr(settings, 'VECTOR_BACKEND', 'faiss').lower() == 'pgvector' and pgvector_index is not None
        except:
            self.use_pgvector = False
        
        if self.use_pgvector:
            logger.info("[ADVANCED_SEARCH] Service initialized with pgvector backend")
        else:
            logger.info("[ADVANCED_SEARCH] Service initialized with FAISS backend")
    
    @property
    def is_initialized(self) -> bool:
        return self._initialized and self.model_manager is not None
    
    async def search_multi_face(
        self,
        image: np.ndarray,
        db: AsyncSession,
        user_id: int,
        scope: str = "both",
        top_k: int = 10,
        min_quality: float = None,
        check_watchlist: bool = True,
        exclude_identity_ids: List[str] = None,
        exclude_watchlist_ids: List[str] = None,
        filters: Dict = None,
        ip_address: str = None,
        user_agent: str = None
    ) -> MultiSearchResult:
        """
        Search for all faces detected in an image.
        
        Args:
            image: Input image (BGR)
            db: Database session
            user_id: ID of user performing search
            scope: "known", "unknown", or "both"
            top_k: Number of results per face
            min_quality: Minimum quality threshold (uses config default if None)
            check_watchlist: Whether to check against watchlists
            exclude_identity_ids: IDs to exclude from results
            exclude_watchlist_ids: Watchlist IDs to exclude from checking
            filters: Additional filters (date_from, date_to, pipeline_id)
            ip_address: Client IP for audit
            user_agent: Client user agent for audit
            
        Returns:
            MultiSearchResult with all faces and matches
        """
        start_time = time.time()
        search_id = str(uuid.uuid4())
        
        if min_quality is None:
            min_quality = settings.SEARCH_MIN_QUALITY_THRESHOLD
        
        if not self.is_initialized:
            raise RuntimeError("Advanced search service not initialized")
        
        # Detect all faces in image
        faces_data = []
        watchlist_alerts = []
        image_hash = hashlib.sha256(image.tobytes()).hexdigest()[:16]
        
        try:
            # Use model manager to detect faces (detect up to 100 faces for multi-face search)
            bboxes, kpss = self.model_manager.detector.detect(image, max_num=100)
            
            # If no faces detected, check if this is a small pre-cropped face image
            if (bboxes is None or len(bboxes) == 0) or (kpss is None or len(kpss) == 0):
                h, w = image.shape[:2]
                # Common face crop sizes: 112x112, 128x128, 160x160, etc.
                # If image is small and roughly square, treat as face image
                is_small_image = (h <= 200 and w <= 200) and (abs(h - w) <= 20)
                
                if is_small_image:
                    logger.info(f"[ADVANCED_SEARCH] No face detected, but image is small ({h}x{w}) - treating as pre-cropped face image")
                    logger.info(f"[ADVANCED_SEARCH] Using entire image as face region for search")
                    
                    # Create approximate keypoints at image center (for face alignment)
                    center_x, center_y = w // 2, h // 2
                    kpss = np.array([[
                        [center_x - w * 0.15, center_y - h * 0.1],  # Left eye
                        [center_x + w * 0.15, center_y - h * 0.1],  # Right eye
                        [center_x, center_y],                         # Nose
                        [center_x - w * 0.1, center_y + h * 0.15],  # Left mouth corner
                        [center_x + w * 0.1, center_y + h * 0.15]   # Right mouth corner
                    ]], dtype=np.float32)
                    bboxes = np.array([[0, 0, w, h, 1.0]], dtype=np.float32)
                    logger.info(f"[ADVANCED_SEARCH] Created approximate keypoints for pre-cropped face image")
            
            logger.info(f"[ADVANCED_SEARCH] Detected {len(bboxes)} faces in image")
            
            for idx, (bbox, kps) in enumerate(zip(bboxes, kpss if kpss is not None else [None] * len(bboxes))):
                face_data = await self._process_single_face(
                    image=image,
                    bbox=bbox,
                    landmarks=kps,
                    face_index=idx,
                    db=db,
                    scope=scope,
                    top_k=top_k,
                    min_quality=min_quality,
                    check_watchlist=check_watchlist,
                    exclude_identity_ids=exclude_identity_ids,
                    exclude_watchlist_ids=exclude_watchlist_ids,
                    filters=filters
                )
                faces_data.append(face_data)
                
                # Collect watchlist alerts
                if face_data.matches:
                    for match in face_data.matches:
                        if match.watchlist_match:
                            watchlist_alerts.append(WatchlistAlertInfo(
                                face_index=idx,
                                identity_id=match.identity_id,
                                identity_name=match.display_name,
                                watchlist_id=match.watchlist_match['watchlist_id'],
                                list_name=match.watchlist_match['list_name'],
                                alert_level=match.watchlist_match['alert_level'],
                                priority=match.watchlist_match['priority'],
                                notes=match.watchlist_match.get('notes'),
                                action_instructions=match.watchlist_match.get('action_instructions'),
                                similarity=match.similarity
                            ))
            
        except Exception as e:
            logger.error(f"[ADVANCED_SEARCH] Error during face detection: {e}")
            raise
        
        processing_time_ms = int((time.time() - start_time) * 1000)
        
        # Build summary
        searchable_faces = [f for f in faces_data if not f.skipped]
        all_matches = [m for f in searchable_faces for m in f.matches]
        unique_identities = set(m.identity_id for m in all_matches)
        known_matches = [m for m in all_matches if m.type == "known"]
        unknown_matches = [m for m in all_matches if m.type == "unknown"]
        
        summary = {
            "total_faces_detected": len(faces_data),
            "faces_searchable": len(searchable_faces),
            "faces_skipped": len(faces_data) - len(searchable_faces),
            "total_matches": len(all_matches),
            "unique_identities_found": len(unique_identities),
            "known_matches": len(known_matches),
            "unknown_matches": len(unknown_matches),
            "watchlist_alerts": len(watchlist_alerts)
        }
        
        image_info = {
            "dimensions": f"{image.shape[1]}x{image.shape[0]}",
            "faces_detected": len(faces_data),
            "faces_searchable": len(searchable_faces)
        }
        
        # Log search history
        await self._log_search_history(
            db=db,
            user_id=user_id,
            search_id=search_id,
            search_type=SearchType.MULTI if len(faces_data) > 1 else SearchType.SINGLE,
            scope=scope,
            top_k=top_k,
            filters=filters,
            exclude_identity_ids=exclude_identity_ids,
            exclude_watchlist_ids=exclude_watchlist_ids,
            image_hash=image_hash,
            faces_count=len(faces_data),
            quality_scores=[float(f.quality_score) for f in faces_data],  # Convert numpy types to Python float
            results_count=len(all_matches),
            watchlist_alerts_count=len(watchlist_alerts),
            unique_identities=len(unique_identities),
            processing_time_ms=processing_time_ms,
            ip_address=ip_address,
            user_agent=user_agent
        )
        
        # Create and save watchlist alerts to database
        if watchlist_alerts:
            await self._save_watchlist_alerts(
                db=db,
                search_id=search_id,
                alerts=watchlist_alerts
            )
        
        return MultiSearchResult(
            search_id=search_id,
            image_info=image_info,
            faces=faces_data,
            watchlist_alerts=watchlist_alerts,
            processing_time_ms=processing_time_ms,
            summary=summary
        )
    
    async def _process_single_face(
        self,
        image: np.ndarray,
        bbox: np.ndarray,
        landmarks: Optional[np.ndarray],
        face_index: int,
        db: AsyncSession,
        scope: str,
        top_k: int,
        min_quality: float,
        check_watchlist: bool,
        exclude_identity_ids: List[str],
        exclude_watchlist_ids: List[str],
        filters: Dict
    ) -> FaceInImage:
        """Process a single detected face."""
        
        # Extract face crop
        x1, y1, x2, y2 = map(int, bbox[:4])
        face_crop = image[max(0, y1):y2, max(0, x1):x2]
        
        # Assess quality
        quality = assess_face_quality(
            face_image=face_crop,
            bbox=(x1, y1, x2, y2),
            landmarks=landmarks,
            full_image=image
        )
        
        face_data = FaceInImage(
            face_index=face_index,
            bounding_box={"x1": x1, "y1": y1, "x2": x2, "y2": y2},
            quality_score=float(quality.overall_score),  # Convert numpy type to Python float
            quality_details=quality.details
        )
        
        # Check if quality is sufficient
        if quality.overall_score < min_quality:
            face_data.skipped = True
            face_data.skip_reason = f"Quality below minimum threshold ({quality.overall_score:.2f} < {min_quality:.2f})"
            return face_data
        
        # Add quality warning if below warning threshold
        if quality.overall_score < settings.SEARCH_QUALITY_WARNING_THRESHOLD:
            face_data.quality_warning = quality.warnings[0] if quality.warnings else "Low quality image"
        
        # Generate embedding
        try:
            if landmarks is None:
                raise ValueError("Landmarks are required for embedding generation")
            embedding = self.model_manager.recognizer.get_embedding(image, landmarks)
            if embedding is not None:
                # Normalize embedding
                embedding = embedding / np.linalg.norm(embedding)
            face_data.embedding = embedding
        except Exception as e:
            logger.error(f"[ADVANCED_SEARCH] Failed to generate embedding for face {face_index}: {e}")
            face_data.skipped = True
            face_data.skip_reason = f"Failed to generate embedding: {str(e)}"
            return face_data
        
        # Search in FAISS indexes
        matches = await self._search_indexes(
            embedding=embedding,
            db=db,
            scope=scope,
            top_k=top_k,
            exclude_identity_ids=exclude_identity_ids,
            filters=filters
        )
        
        # Check watchlists
        if check_watchlist and matches:
            matches = await self._check_watchlists(
                db=db,
                matches=matches,
                exclude_watchlist_ids=exclude_watchlist_ids
            )
        
        face_data.matches = matches
        return face_data
    
    async def _search_indexes(
        self,
        embedding: np.ndarray,
        db: AsyncSession,
        scope: str,
        top_k: int,
        exclude_identity_ids: List[str],
        filters: Dict
    ) -> List[FaceSearchResult]:
        """Search vector indexes (pgvector or FAISS) and return matches."""
        
        results = []
        
        # Determine which indexes to search
        search_known = scope in ("known", "both")
        search_unknown = scope in ("unknown", "both")
        
        identity_ids_scores = []
        
        # Use pgvector if enabled, otherwise use FAISS
        if self.use_pgvector and self.pgvector_index:
            logger.debug(f"[ADVANCED_SEARCH] Using pgvector backend for search (scope={scope}, top_k={top_k})")
            
            if search_known:
                known_results = await self.pgvector_index.search_known(
                    embedding=embedding,
                    top_k=top_k * 2,
                    threshold=0.2,  # Lower threshold for advanced search
                    db=db
                )
                # pgvector returns List[Tuple[str, float]]: (identity_id, similarity)
                for identity_id, score in known_results:
                    if exclude_identity_ids and str(identity_id) in exclude_identity_ids:
                        continue
                    identity_ids_scores.append((identity_id, score, "known"))
            
            if search_unknown:
                unknown_results = await self.pgvector_index.search_unknown(
                    embedding=embedding,
                    top_k=top_k * 2,
                    threshold=0.2,  # Lower threshold for advanced search
                    db=db
                )
                # pgvector returns List[Tuple[str, float]]: (identity_id, similarity)
                for identity_id, score in unknown_results:
                    if exclude_identity_ids and str(identity_id) in exclude_identity_ids:
                        continue
                    identity_ids_scores.append((identity_id, score, "unknown"))
        else:
            # FAISS backend (fallback)
            logger.debug(f"[ADVANCED_SEARCH] Using FAISS backend for search (scope={scope}, top_k={top_k})")
            
            if search_known:
                if not self.identity_index or not self.identity_index.known_index:
                    logger.warning("[ADVANCED_SEARCH] FAISS KNOWN index is not available")
                else:
                    known_results = self.identity_index.search_known(embedding, top_k=top_k * 2)
                    for identity_id, score in known_results:
                        if exclude_identity_ids and str(identity_id) in exclude_identity_ids:
                            continue
                        identity_ids_scores.append((identity_id, score, "known"))
            
            if search_unknown:
                if not self.identity_index or not self.identity_index.unknown_index:
                    logger.warning("[ADVANCED_SEARCH] FAISS UNKNOWN index is not available")
                else:
                    unknown_results = self.identity_index.search_unknown(embedding, top_k=top_k * 2)
                    for identity_id, score in unknown_results:
                        if exclude_identity_ids and str(identity_id) in exclude_identity_ids:
                            continue
                        identity_ids_scores.append((identity_id, score, "unknown"))
        
        # Sort by score and take top_k
        identity_ids_scores.sort(key=lambda x: x[1], reverse=True)
        identity_ids_scores = identity_ids_scores[:top_k]
        
        if not identity_ids_scores:
            return results
        
        # Fetch identity details from database
        identity_ids = [str(i[0]) for i in identity_ids_scores]
        query = select(Identity).where(
            Identity.id.in_([uuid.UUID(i) for i in identity_ids])
        )
        
        # Apply date filters if provided
        # Note: This would filter based on last_seen_at
        if filters:
            if filters.get('date_from'):
                query = query.where(Identity.last_seen_at >= filters['date_from'])
            if filters.get('date_to'):
                query = query.where(Identity.last_seen_at <= filters['date_to'])
        
        result = await db.execute(query)
        identities = {str(i.id): i for i in result.scalars().all()}
        
        # Build results
        for identity_id, score, idx_type in identity_ids_scores:
            identity = identities.get(str(identity_id))
            if not identity:
                continue
            
            confidence_band = self._get_confidence_band(score)
            
            # Backend constructs snapshot URL using path_to_url utility (all logic in backend)
            snapshot_url = None
            if identity.best_snapshot_path:
                # Use path_to_url utility to properly convert path to URL
                snapshot_url = path_to_url(identity.best_snapshot_path)
            else:
                # Backend provides fallback URL (SVG data URI with user icon)
                snapshot_url = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="100" height="100"%3E%3Crect fill="%23333" width="100" height="100"/%3E%3Ccircle cx="50" cy="35" r="15" fill="%23999"/%3E%3Cpath d="M 25 70 Q 25 60 35 60 L 65 60 Q 75 60 75 70 L 75 85 L 25 85 Z" fill="%23999"/%3E%3C/svg%3E'
            
            results.append(FaceSearchResult(
                identity_id=str(identity_id),
                display_name=identity.display_name,
                type=identity.type.value if identity.type else idx_type,
                similarity=float(round(score, 4)),  # Convert numpy type to Python float
                confidence_band=confidence_band,
                best_snapshot_path=identity.best_snapshot_path,  # Keep original path for reference
                snapshot_url=snapshot_url,  # Backend provides ready-to-use URL
                last_seen_at=identity.last_seen_at,
                appearances_count=identity.appearances_count or 0
            ))
        
        return results
    
    async def _check_watchlists(
        self,
        db: AsyncSession,
        matches: List[FaceSearchResult],
        exclude_watchlist_ids: List[str]
    ) -> List[FaceSearchResult]:
        """Check if any matches are on watchlists."""
        
        if not matches:
            return matches
        
        identity_ids = [uuid.UUID(m.identity_id) for m in matches]
        
        # Query watchlist entries for these identities
        query = select(WatchlistEntry).options(
            selectinload(WatchlistEntry.watchlist)
        ).where(
            and_(
                WatchlistEntry.identity_id.in_(identity_ids),
                WatchlistEntry.is_active == True,
                or_(
                    WatchlistEntry.expires_at == None,
                    WatchlistEntry.expires_at > datetime.utcnow()
                )
            )
        )
        
        if exclude_watchlist_ids:
            query = query.where(
                WatchlistEntry.watchlist_id.notin_([uuid.UUID(w) for w in exclude_watchlist_ids])
            )
        
        result = await db.execute(query)
        entries = result.scalars().all()
        
        # Build lookup
        watchlist_lookup = {}
        for entry in entries:
            if entry.watchlist and entry.watchlist.is_active:
                watchlist_lookup[str(entry.identity_id)] = {
                    'watchlist_id': str(entry.watchlist_id),
                    'list_name': entry.watchlist.name,
                    'alert_level': entry.watchlist.alert_level.value,
                    'color': entry.watchlist.color,
                    'icon': entry.watchlist.icon,
                    'priority': entry.priority.value,
                    'notes': entry.notes,
                    'action_instructions': entry.action_instructions
                }
        
        # Update matches with watchlist info
        for match in matches:
            if match.identity_id in watchlist_lookup:
                match.watchlist_match = watchlist_lookup[match.identity_id]
        
        return matches
    
    def _get_confidence_band(self, similarity: float) -> str:
        """Get confidence band for a similarity score."""
        if similarity >= settings.CONFIDENCE_VERY_HIGH_MIN:
            return "VERY_HIGH"
        elif similarity >= settings.CONFIDENCE_HIGH_MIN:
            return "HIGH"
        elif similarity >= settings.CONFIDENCE_MEDIUM_MIN:
            return "MEDIUM"
        elif similarity >= settings.CONFIDENCE_LOW_MIN:
            return "LOW"
        else:
            return "VERY_LOW"
    
    async def _log_search_history(
        self,
        db: AsyncSession,
        user_id: int,
        search_id: str,
        search_type: SearchType,
        scope: str,
        top_k: int,
        filters: Dict,
        exclude_identity_ids: List[str],
        exclude_watchlist_ids: List[str],
        image_hash: str,
        faces_count: int,
        quality_scores: List[float],
        results_count: int,
        watchlist_alerts_count: int,
        unique_identities: int,
        processing_time_ms: int,
        ip_address: str,
        user_agent: str
    ):
        """Log search to history table."""
        try:
            history = SearchHistory(
                id=uuid.UUID(search_id),
                user_id=user_id,
                search_type=search_type,
                scope=scope,
                top_k=top_k,
                filters=filters,
                exclude_identity_ids=exclude_identity_ids,
                exclude_watchlist_ids=exclude_watchlist_ids,
                input_image_hash=image_hash,
                input_faces_count=faces_count,
                input_quality_scores=quality_scores,
                results_count=results_count,
                watchlist_alerts_count=watchlist_alerts_count,
                unique_identities_count=unique_identities,
                processing_time_ms=processing_time_ms,
                ip_address=ip_address,
                user_agent=user_agent
            )
            db.add(history)
            await db.commit()
            logger.debug(f"[ADVANCED_SEARCH] Logged search history: {search_id}")
        except Exception as e:
            logger.error(f"[ADVANCED_SEARCH] Failed to log search history: {e}")
            await db.rollback()
    
    async def _save_watchlist_alerts(
        self,
        db: AsyncSession,
        search_id: str,
        alerts: List[WatchlistAlertInfo]
    ):
        """Save watchlist alerts to database."""
        try:
            for alert_info in alerts:
                # Get the watchlist entry
                query = select(WatchlistEntry).where(
                    and_(
                        WatchlistEntry.watchlist_id == uuid.UUID(alert_info.watchlist_id),
                        WatchlistEntry.identity_id == uuid.UUID(alert_info.identity_id)
                    )
                )
                result = await db.execute(query)
                entry = result.scalar_one_or_none()
                
                if entry:
                    alert = WatchlistAlert(
                        watchlist_entry_id=entry.id,
                        triggered_by="search",
                        search_id=uuid.UUID(search_id),
                        similarity_score=float(alert_info.similarity)  # Convert numpy type to Python float
                    )
                    db.add(alert)
            
            await db.commit()
            logger.info(f"[ADVANCED_SEARCH] Saved {len(alerts)} watchlist alerts")
        except Exception as e:
            logger.error(f"[ADVANCED_SEARCH] Failed to save watchlist alerts: {e}")
            await db.rollback()
    
    def _convert_numpy_types(self, obj: Any) -> Any:
        """Recursively convert numpy types to Python native types for JSON serialization."""
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, dict):
            return {k: self._convert_numpy_types(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._convert_numpy_types(item) for item in obj]
        elif isinstance(obj, tuple):
            return tuple(self._convert_numpy_types(item) for item in obj)
        else:
            return obj
    
    def to_dict(self, result: MultiSearchResult) -> Dict:
        """Convert MultiSearchResult to dictionary for JSON serialization."""
        return {
            "search_id": result.search_id,
            "image_info": result.image_info,
            "faces": [
                {
                    "face_index": f.face_index,
                    "bounding_box": f.bounding_box,
                    "quality_score": self._convert_numpy_types(f.quality_score),
                    "quality_details": self._convert_numpy_types(f.quality_details),
                    "quality_warning": f.quality_warning,
                    "skipped": f.skipped,
                    "skip_reason": f.skip_reason,
                    "matches": [
                        {
                            "identity_id": m.identity_id,
                            "display_name": m.display_name,
                            "type": m.type,
                            "similarity": self._convert_numpy_types(m.similarity),
                            "confidence_band": m.confidence_band,
                            "best_snapshot_path": m.best_snapshot_path,  # Keep for reference
                            "snapshot_url": m.snapshot_url,  # Backend provides ready-to-use URL
                            "last_seen_at": m.last_seen_at.isoformat() if m.last_seen_at else None,
                            "appearances_count": m.appearances_count,
                            "watchlist_match": self._convert_numpy_types(m.watchlist_match) if m.watchlist_match else None
                        }
                        for m in f.matches
                    ]
                }
                for f in result.faces
            ],
            "watchlist_alerts": [
                {
                    "face_index": a.face_index,
                    "identity_id": a.identity_id,
                    "identity_name": a.identity_name,
                    "watchlist_id": a.watchlist_id,
                    "list_name": a.list_name,
                    "alert_level": a.alert_level,
                    "priority": a.priority,
                    "notes": a.notes,
                    "action_instructions": a.action_instructions,
                    "similarity": self._convert_numpy_types(a.similarity)
                }
                for a in result.watchlist_alerts
            ],
            "processing_time_ms": result.processing_time_ms,
            "summary": self._convert_numpy_types(result.summary) if isinstance(result.summary, dict) else result.summary
        }


# Global instance
advanced_search_service = AdvancedSearchService()


