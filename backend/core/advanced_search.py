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
import math
import time
import uuid
from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict
import numpy as np
import cv2

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, exists
from sqlalchemy.orm import selectinload

from config import settings
from db_models import (
    Identity, IdentityType, IdentityAppearance, Watchlist, WatchlistEntry,
    WatchlistAlert, SearchHistory, SearchType
)
from backend.core.face_quality import assess_face_quality, QualityAssessment
from backend.core.face_extraction import (DetectedFace, embed_face_normalized,
                                          extract_faces)

# Import path_to_url utility - handle import errors gracefully
try:
    from backend.utils.path_utils import path_to_url
except ImportError:
    # Fallback: define a simple path_to_url function if import fails
    def path_to_url(path: str) -> str:
        """Convert file path to URL format."""
        if not path:
            # Resolved at call time, so the constant defined below is available.
            return PLACEHOLDER_AVATAR_DATA_URI
        if path.startswith('storage/'):
            return f"/{path}"
        elif not path.startswith('/'):
            return f"/storage/{path}"
        else:
            return path

from backend.core.vector_index.access import search_similar_embeddings
from backend.core.vector_index.base import usable_score

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# [UPLOAD_MATCH] — one traceable line per stage of an image-match request
#
# Why a message-interpolated helper and not `extra={...}`: this project's
# formatter is plain text with no structured-field support, and the redaction
# filter in utils/logging.py inspects only the RENDERED message. Anything passed
# via `extra=` would be invisible in the output *and* skip redaction — the worst
# of both. So fields are rendered into the message, in the same `k=v` style as
# [AUTH_AUDIT] and [TASK].
#
# SAFE FIELDS ONLY: ids, counts, dimensions, scores, durations. Never raw bytes,
# never a full checksum, never an embedding, never an absolute path, never a
# token. Note that utils/logging.py collapses any bracketed list of 12+ numbers
# into [***EMBEDDING-REDACTED***], which is why per-candidate scores are emitted
# as individual `raw_candidate` lines rather than one array.
# ---------------------------------------------------------------------------

UPLOAD_MATCH_STAGES = (
    "request_started", "file_validated", "image_decoded", "face_detected",
    "embedding_created", "vector_search_started", "raw_candidate",
    "candidate_filtered", "final_candidates", "request_completed",
    "request_failed",
)

# Per-stage severity. The two per-candidate stages are DEBUG because they are
# the only ones whose volume scales with the result set — a top_k=50 search
# would otherwise write 50 INFO lines. They still reach logs/app.log, which is
# where a "why did this match?" investigation happens; utils/logging.py keeps
# them off stdout. request_failed is ERROR so a failed match is visible in
# Docker logs without reading the file.
UPLOAD_MATCH_LEVELS = {
    "request_started": logging.INFO,
    "file_validated": logging.INFO,
    "image_decoded": logging.INFO,
    "face_detected": logging.INFO,
    "embedding_created": logging.INFO,
    "vector_search_started": logging.INFO,
    "final_candidates": logging.INFO,
    "request_completed": logging.INFO,
    "raw_candidate": logging.DEBUG,
    "candidate_filtered": logging.DEBUG,
    "request_failed": logging.ERROR,
}


def _embedding_model_version() -> Optional[str]:
    """Which recognition weights produced this vector, derived from the path.

    Same rule as IdentityService._resolve_model_version, so a model swap shows
    up in the trace without anyone remembering to bump a constant.
    """
    import os as _os
    path = settings.RECOGNITION_MODEL or ""
    if not path:
        return None
    return _os.path.splitext(_os.path.basename(str(path)))[0][:64] or None


def _fmt_score(value) -> Optional[str]:
    """Six-dp string, or the literal 'nan'/'none' — never a silent default.

    A score that is not a real number is the thing this logging exists to make
    visible, so it is printed as what it is rather than coerced.
    """
    if value is None:
        return "none"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "invalid"
    if not math.isfinite(number):
        return "nan"
    return f"{number:.6f}"


def _match_log(stage: str, *, request_id: Optional[str] = None,
               duration_ms: Optional[float] = None, level: Optional[int] = None,
               **fields) -> None:
    """Emit one [UPLOAD_MATCH] line. Never raises — logging must not break a search.

    The severity comes from UPLOAD_MATCH_LEVELS, so a stage cannot be logged at
    two different levels from two call sites. `upload_match_stage` is attached
    to the record purely so the console filter in utils/logging.py can decide
    what reaches stdout — it is a fixed enum value, never user data, and is
    never rendered into the message.
    """
    try:
        resolved = level if level is not None else UPLOAD_MATCH_LEVELS.get(
            stage, logging.INFO)
        if not logger.isEnabledFor(resolved):
            return
        rendered = " ".join(f"{key}={value}" for key, value in fields.items()
                            if value is not None)
        logger.log(resolved,
                   "[UPLOAD_MATCH] stage=%s request_id=%s duration_ms=%s %s",
                   stage, request_id or "-",
                   f"{duration_ms:.1f}" if duration_ms is not None else "-",
                   rendered,
                   extra={"upload_match_stage": stage})
    except Exception:                                            # noqa: BLE001
        logger.debug("[UPLOAD_MATCH] failed to emit stage=%s", stage)


# Shown when an identity has no usable snapshot. Kept as one constant so the
# two places that emit it cannot drift apart.
PLACEHOLDER_AVATAR_DATA_URI = (
    'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="100" height="100"%3E'
    '%3Crect fill="%23333" width="100" height="100"/%3E'
    '%3Ccircle cx="50" cy="35" r="15" fill="%23999"/%3E'
    '%3Cpath d="M 25 70 Q 25 60 35 60 L 65 60 Q 75 60 75 70 L 75 85 L 25 85 Z" fill="%23999"/%3E'
    '%3C/svg%3E'
)


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
    # Watchlist-checked matches from BEFORE the pipeline/date filters were
    # applied. Alerts are collected from this, never from `matches`, so a
    # browse filter cannot suppress a persisted security record. Internal —
    # `to_dict` does not serialize it.
    alert_candidates: List[FaceSearchResult] = field(default_factory=list)


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

    # Recall floor for candidate retrieval. Deliberately looser than the
    # ingest-time thresholds: this is an investigative search where the
    # operator judges the ranked list, not an auto-identification decision.
    # It is applied to BOTH backends — the FAISS call sites used to omit it and
    # silently fall back to the index defaults (0.4 / 0.35), so the two
    # deployments searched to different depths.
    #
    # These are properties, not class constants: as constants they were a
    # second, unreachable declaration of values the settings page offers.
    # Read per call so an admin edit takes effect without a restart.
    @property
    def candidate_threshold(self) -> float:
        return float(settings.SEARCH_RETRIEVAL_FLOOR)

    # Candidate pool multiplier. The vector indexes return one row per
    # EMBEDDING, and an identity may own many, so the pool has to be wider than
    # the quota to have a chance of containing `top_k` distinct people. Widened
    # further when a filter is active, because filtering happens after the
    # index has already chosen the candidates.
    @property
    def candidate_multiplier(self) -> int:
        return int(settings.SEARCH_CANDIDATE_MULTIPLIER)

    @property
    def filtered_candidate_multiplier(self) -> int:
        return int(settings.SEARCH_FILTERED_CANDIDATE_MULTIPLIER)

    def __init__(self, model_manager=None, identity_index=None, pgvector_index=None):
        self.model_manager = model_manager
        # Retained so existing constructor calls keep working; the in-process
        # search path resolves the active index through the contract instead.
        self.identity_index = identity_index
        self.pgvector_index = pgvector_index  # pgvector backend
        self._initialized = False
        
        # Check which backend to use
        try:
            from config import settings
            self.use_pgvector = settings.VECTOR_BACKEND.lower() == 'pgvector' and pgvector_index is not None
        except:
            self.use_pgvector = False
    
    def initialize(self, model_manager, identity_index, pgvector_index=None):
        """Initialize with model manager and identity index."""
        self.model_manager = model_manager
        self.identity_index = identity_index
        self.pgvector_index = pgvector_index  # pgvector backend
        self._initialized = True
        
        # Check which backend to use
        try:
            from config import settings
            self.use_pgvector = settings.VECTOR_BACKEND.lower() == 'pgvector' and pgvector_index is not None
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
        user_agent: str = None,
        request_id: str = None,
        *,
        image_hash: str,
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
            request_id: Correlation id from the HTTP middleware, echoed on every
                [UPLOAD_MATCH] line so one request's stages can be grepped out
            
        Returns:
            MultiSearchResult with all faces and matches
        """
        start_time = time.time()
        stage_started = time.perf_counter()
        search_id = str(uuid.uuid4())
        height, width = (image.shape[:2] if image is not None and hasattr(image, "shape")
                         else (0, 0))
        _match_log("request_started", request_id=request_id, search_id=search_id,
                   user_id=user_id, scope=scope, top_k=top_k,
                   vector_backend=settings.VECTOR_BACKEND)
        _match_log("image_decoded", request_id=request_id, search_id=search_id,
                   width=int(width), height=int(height),
                   duration_ms=(time.perf_counter() - stage_started) * 1000)

        if min_quality is None:
            min_quality = settings.SEARCH_MIN_QUALITY_THRESHOLD
        
        if not self.is_initialized:
            raise RuntimeError("Advanced search service not initialized")
        
        # Detect all faces in image
        faces_data = []
        watchlist_alerts = []
        # image_hash: sha256 of the UPLOADED bytes, computed by the caller that
        # holds them (one convention for /search/by-image, /search/advanced and
        # /search/batch: search_history.input_image_hash is comparable across
        # the three). The decoded frame is never re-hashed here.
        
        try:
            # One shared extractor, the same one enrollment uses. What this
            # replaces synthesized five keypoints out of image geometry —
            # fractions of width and height around the image midpoint, plus a
            # frame-sized box with a hardcoded score of 1.0 — whenever detection
            # failed on a small squarish image.
            #
            # That never fails and never looks wrong — ArcFace warps the face
            # with the invented geometry and returns a well-formed vector
            # pointing somewhere else. Measured on a real 112x112 crop, the
            # fabricated-landmark embedding scores 0.665 against the real one for
            # the SAME face, with a 0.4 match threshold. Every such search was
            # querying with a probe that did not represent the uploaded person.
            #
            # The replacement retries detection on a padded canvas instead, which
            # finds the real keypoints of exactly the crops the fabrication was
            # trying to paper over. max_num=100 is passed through unchanged.
            faces = extract_faces(image, max_num=100, manager=self.model_manager)

            logger.info(f"[ADVANCED_SEARCH] Detected {len(faces)} faces in image")
            for _f in faces:
                _match_log("face_detected", request_id=request_id,
                           search_id=search_id, face_index=_f.index,
                           face_count=len(faces),
                           det_score=_fmt_score(getattr(_f, "score", None)),
                           has_bbox=getattr(_f, "bbox", None) is not None,
                           has_landmarks=getattr(_f, "landmarks", None) is not None,
                           padded_retry=getattr(_f, "padded_retry", False))

            for idx, face in enumerate(faces):
                face_data = await self._process_single_face(
                    image=image,
                    face=face,
                    face_index=idx,
                    db=db,
                    scope=scope,
                    top_k=top_k,
                    min_quality=min_quality,
                    check_watchlist=check_watchlist,
                    exclude_identity_ids=exclude_identity_ids,
                    exclude_watchlist_ids=exclude_watchlist_ids,
                    filters=filters,
                    request_id=request_id
                )
                faces_data.append(face_data)
                
                # Collect watchlist alerts from the alert candidates, NOT from
                # face_data.matches: these become persisted WatchlistAlert rows
                # and must not be suppressed by a camera or date filter.
                if face_data.alert_candidates:
                    for match in face_data.alert_candidates:
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
            # Type only, never the message: an exception string can carry a path
            # or a driver payload, and this line is operator-facing.
            _match_log("request_failed", request_id=request_id, search_id=search_id,
                       error_type=type(e).__name__,
                       duration_ms=(time.perf_counter() - stage_started) * 1000)
            raise

        processing_time_ms = int((time.time() - start_time) * 1000)
        
        # Build summary
        searchable_faces = [f for f in faces_data if not f.skipped]
        all_matches = [m for f in searchable_faces for m in f.matches]
        unique_identities = set(m.identity_id for m in all_matches)
        known_matches = [m for m in all_matches if m.type == "known"]
        unknown_matches = [m for m in all_matches if m.type == "unknown"]
        
        for _face in faces_data:
            _match_log("final_candidates", request_id=request_id,
                       search_id=search_id, face_index=_face.face_index,
                       returned=len(_face.matches),
                       top_similarity=_fmt_score(
                           _face.matches[0].similarity if _face.matches else None),
                       skipped=_face.skipped, skip_reason=_face.skip_reason)

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
            "faces_searchable": len(searchable_faces),
            # Whether any face needed the padded retry — i.e. the upload was a
            # tight crop the detector could not see without margin.
            "padded_retry": any(f.padded_retry for f in faces)
        }
        
        # Log search history — the ONE writer (backend/core/search_audit.py)
        from backend.core.search_audit import record_image_search
        await record_image_search(
            db,
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
        
        _match_log("request_completed", request_id=request_id, search_id=search_id,
                   face_count=len(faces_data),
                   faces_searchable=summary.get("faces_searchable"),
                   total_matches=summary.get("total_matches"),
                   unique_identities=summary.get("unique_identities_found"),
                   watchlist_alerts=len(watchlist_alerts),
                   duration_ms=(time.perf_counter() - stage_started) * 1000)

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
        face: DetectedFace,
        face_index: int,
        db: AsyncSession,
        scope: str,
        top_k: int,
        min_quality: float,
        check_watchlist: bool,
        exclude_identity_ids: List[str],
        exclude_watchlist_ids: List[str],
        filters: Dict,
        request_id: str = None
    ) -> FaceInImage:
        """Process a single detected face."""

        # bbox_int clamps to the image at BOTH ends. A face recovered by the
        # padded retry can have a box extending past the original frame, and
        # `image[max(0, y1):y2]` only guards the low end — an x1 beyond the
        # width silently yields an EMPTY crop, which then gets a confident
        # quality score about nothing.
        x1, y1, x2, y2 = face.bbox_int(image.shape)
        face_crop = image[y1:y2, x1:x2]
        landmarks = face.landmarks

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
        
        # Generate embedding. The None-landmarks guard that used to stand here
        # is gone because it cannot fire: every DetectedFace carries landmarks
        # that came from the detector and passed validate_landmarks.
        try:
            _emb_started = time.perf_counter()
            embedding = embed_face_normalized(image, face, manager=self.model_manager)
            face_data.embedding = embedding
            _match_log("embedding_created", request_id=request_id,
                       face_index=face_index,
                       embedding_dim=int(np.asarray(embedding).size),
                       embedding_norm=_fmt_score(float(np.linalg.norm(embedding))),
                       embedding_model_version=_embedding_model_version(),
                       duration_ms=(time.perf_counter() - _emb_started) * 1000)
        except Exception as e:
            logger.error(f"[ADVANCED_SEARCH] Failed to generate embedding for face {face_index}: {e}")
            face_data.skipped = True
            face_data.skip_reason = f"Failed to generate embedding: {str(e)}"
            return face_data
        
        # Search the vector indexes. Two lists come back: what to show, and
        # what to raise alerts on (see _search_indexes for why they differ).
        matches, alert_candidates = await self._search_indexes(
            embedding=embedding,
            db=db,
            scope=scope,
            top_k=top_k,
            exclude_identity_ids=exclude_identity_ids,
            filters=filters,
            request_id=request_id
        )

        # Check watchlists against the UNFILTERED candidates. A camera or date
        # filter narrows a browse; it must not decide whether a threat alert is
        # recorded.
        if check_watchlist and alert_candidates:
            alert_candidates = await self._check_watchlists(
                db=db,
                matches=alert_candidates,
                exclude_watchlist_ids=exclude_watchlist_ids
            )

            # Mirror the result onto the displayed matches so the badge still
            # renders next to a match the user can see. `matches` and
            # `alert_candidates` hold different FaceSearchResult objects when a
            # filter is active, so this is a copy, not aliasing.
            if matches is not alert_candidates:
                by_identity = {
                    m.identity_id: m.watchlist_match
                    for m in alert_candidates if m.watchlist_match
                }
                for match in matches:
                    if match.identity_id in by_identity:
                        match.watchlist_match = by_identity[match.identity_id]

        face_data.matches = matches
        face_data.alert_candidates = alert_candidates
        return face_data
    
    async def _search_indexes(
        self,
        embedding: np.ndarray,
        db: AsyncSession,
        scope: str,
        top_k: int,
        exclude_identity_ids: List[str],
        filters: Dict,
        request_id: str = None
    ) -> Tuple[List[FaceSearchResult], List[FaceSearchResult]]:
        """Search vector indexes (pgvector or FAISS) and return matches.

        Returns TWO lists:
          - `display`: what the user asked for — filters applied, capped at top_k.
          - `alert_candidates`: the same ranking WITHOUT the pipeline/date
            filters, capped at top_k.

        They are separate on purpose. Watchlist alerts are persisted security
        records (`WatchlistAlert` rows, `SearchHistory.watchlist_alerts_count`),
        and they used to be derived from whatever survived the filters. That
        meant narrowing the Camera dropdown to "Lobby" would silently suppress
        the alert — and its audit trail — for a THREAT-list subject who matched
        the uploaded face but had only ever been captured on "Garage".
        Filtering is a view concern; an alert is a record. Different lists.
        """
        _search_started = time.perf_counter()
        _match_log("vector_search_started", request_id=request_id,
                   scope=scope, top_k=top_k,
                   vector_backend=settings.VECTOR_BACKEND,
                   retrieval_floor=self.candidate_threshold,
                   display_floor=float(settings.SIMILARITY_THRESHOLD))

        # Determine which indexes to search
        search_known = scope in ("known", "both")
        search_unknown = scope in ("unknown", "both")

        has_db_filter = bool(filters and (
            filters.get('pipeline_id') or filters.get('date_from') or filters.get('date_to')))
        fetch_k = top_k * (self.filtered_candidate_multiplier if has_db_filter
                           else self.candidate_multiplier)

        identity_ids_scores = []

        # Use pgvector if enabled, otherwise use FAISS
        if self.use_pgvector and self.pgvector_index:
            logger.debug(f"[ADVANCED_SEARCH] Using pgvector backend for search (scope={scope}, top_k={top_k})")

            if search_known:
                known_results = await self.pgvector_index.search_known(
                    embedding=embedding,
                    top_k=fetch_k,
                    threshold=self.candidate_threshold,
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
                    top_k=fetch_k,
                    threshold=self.candidate_threshold,
                    db=db
                )
                # pgvector returns List[Tuple[str, float]]: (identity_id, similarity)
                for identity_id, score in unknown_results:
                    if exclude_identity_ids and str(identity_id) in exclude_identity_ids:
                        continue
                    identity_ids_scores.append((identity_id, score, "unknown"))
        else:
            # In-process index, through the contract.
            #
            # The threshold is passed explicitly. Omitting it fell back to the
            # legacy index defaults (0.4 known / 0.35 unknown), so this branch
            # searched roughly half as deep as the pgvector branch while the
            # comment above claimed a lower threshold.
            logger.debug(f"[ADVANCED_SEARCH] Using the in-process vector index (scope={scope}, top_k={top_k})")

            for wanted, enabled in (("known", search_known), ("unknown", search_unknown)):
                if not enabled:
                    continue
                hits = await search_similar_embeddings(
                    db, embedding, top_k=fetch_k,
                    threshold=self.candidate_threshold, identity_type=wanted)
                if not hits:
                    logger.debug(f"[ADVANCED_SEARCH] no {wanted} candidates")
                for hit in hits:
                    identity_id = hit["identity_id"]
                    if exclude_identity_ids and str(identity_id) in exclude_identity_ids:
                        continue
                    identity_ids_scores.append((identity_id, hit["similarity"], wanted))

        # Collapse to one entry per identity, keeping its best score.
        #
        # The indexes rank EMBEDDINGS, not people, and an identity may own many
        # (6 do in the live database). Without this, one person can occupy
        # several result slots — and once truncation moves after filtering,
        # a filter that removes competing identities would backfill the freed
        # slots with more copies of the survivor.
        best_by_identity = {}
        for identity_id, score, idx_type in identity_ids_scores:
            key = str(identity_id)
            current = best_by_identity.get(key)
            if current is None or score > current[1]:
                best_by_identity[key] = (identity_id, score, idx_type)
        identity_ids_scores = list(best_by_identity.values())

        # Rank. NOTE: no truncation here — the DB filters below must be applied
        # across the whole candidate pool, or a narrow filter silently shrinks
        # an already-cut list instead of promoting the next valid match.
        for _iid, _score, _itype in identity_ids_scores:
            _match_log("raw_candidate", request_id=request_id,
                       identity_id=str(_iid), index_type=_itype,
                       raw_similarity=_fmt_score(_score),
                       raw_distance=_fmt_score(
                           None if _score is None else 1.0 - float(_score)
                           if isinstance(_score, (int, float)) and math.isfinite(float(_score))
                           else None))
        identity_ids_scores.sort(key=lambda x: x[1], reverse=True)

        if not identity_ids_scores:
            return [], []

        # Fetch identity details from database
        identity_ids = [str(i[0]) for i in identity_ids_scores]
        base_ids = [uuid.UUID(i) for i in identity_ids]
        query = select(Identity).where(Identity.id.in_(base_ids))
        unfiltered_query = query

        # Apply date filters if provided.
        #
        # Deliberately still on Identity.last_seen_at rather than on an
        # appearance timestamp: 5 of 10 KNOWN identities have no appearance rows
        # at all (enrolled but never captured), and moving this predicate would
        # silently drop every one of them. Note that last_seen_at carries
        # onupdate=utcnow, so it behaves more like updated_at than like a
        # sighting time — changing that is a separate, deliberate decision.
        if filters:
            if filters.get('date_from'):
                query = query.where(Identity.last_seen_at >= filters['date_from'])
            if filters.get('date_to'):
                query = query.where(Identity.last_seen_at <= filters['date_to'])

            # Camera / location filter. Correlated EXISTS so the planner can
            # drive from the (already small) candidate list and stop at the
            # first matching appearance, rather than materialising every
            # appearance row for the camera.
            #
            # Consequence worth knowing: an identity with no appearance rows
            # fails this on every camera. That is correct for "seen on camera X"
            # but it does mean enrolled-but-never-captured people disappear
            # whenever a camera is selected — which is exactly why watchlist
            # alerts are computed from the unfiltered set instead.
            #
            # Also note this predicate carries no time bound, so combining it
            # with the date range above reads as "ever seen on camera X" AND
            # "last_seen_at within range" — a looser conjunction than it looks.
            if filters.get('pipeline_id'):
                query = query.where(exists().where(
                    (IdentityAppearance.identity_id == Identity.id) &
                    (IdentityAppearance.pipeline_id == filters['pipeline_id'])
                ))

        result = await db.execute(query)
        identities = {str(i.id): i for i in result.scalars().all()}

        # The alert path must not inherit the filters. Only re-query when a
        # filter actually narrowed something.
        if has_db_filter:
            unfiltered_result = await db.execute(unfiltered_query)
            unfiltered_identities = {str(i.id): i for i in unfiltered_result.scalars().all()}
        else:
            unfiltered_identities = identities

        # RETRIEVAL floor vs DISPLAY floor. SEARCH_RETRIEVAL_FLOOR exists to
        # search deeply enough that filtering has something to work with; it was
        # also, accidentally, the only floor, so a 0.21 match was rendered as a
        # result card labelled VERY_LOW. Anything the operator's configured
        # recognition threshold would reject is not a match and is not shown.
        display_floor = float(settings.SIMILARITY_THRESHOLD)

        def build(pool, limit):
            """Walk candidates in score order, keeping those the pool admits."""
            built = []
            for identity_id, score, idx_type in identity_ids_scores:
                if len(built) >= limit:
                    break
                if not usable_score(score, display_floor):
                    # Ordered by score, so everything after this is also below
                    # the floor — but log the first rejection with its reason
                    # rather than dropping the tail silently.
                    _match_log("candidate_filtered", request_id=request_id,
                               identity_id=str(identity_id),
                               raw_similarity=_fmt_score(score),
                               threshold=display_floor,
                               rejection_reason="below_display_threshold")
                    break
                identity = pool.get(str(identity_id))
                if not identity:
                    _match_log("candidate_filtered", request_id=request_id,
                               identity_id=str(identity_id),
                               raw_similarity=_fmt_score(score),
                               rejection_reason="not_in_filtered_pool")
                    continue
                built.append(self._build_match(identity, identity_id, score, idx_type))
            return built

        results = build(identities, top_k)
        alert_candidates = build(unfiltered_identities, top_k) if has_db_filter else results

        if has_db_filter and len(results) < top_k:
            # Say so, rather than letting a short list read as "no more matches
            # exist". The pool is finite and the filter ran after the index
            # already chose the candidates.
            logger.info(
                "[ADVANCED_SEARCH] filtered result under-filled: %d/%d from %d candidates "
                "(scope=%s, pipeline=%s) — widen the candidate pool if this is common",
                len(results), top_k, len(identity_ids_scores), scope,
                (filters or {}).get('pipeline_id'))

        return results, alert_candidates

    def _build_match(self, identity, identity_id, score, idx_type) -> FaceSearchResult:
        """Build one API-facing match from a hydrated Identity row."""
        # Backend constructs snapshot URL using path_to_url utility (all logic in backend)
        if identity.best_snapshot_path:
            snapshot_url = path_to_url(identity.best_snapshot_path)
        else:
            # Backend provides fallback URL (SVG data URI with user icon)
            snapshot_url = PLACEHOLDER_AVATAR_DATA_URI

        return FaceSearchResult(
            identity_id=str(identity_id),
            display_name=identity.display_name,
            type=identity.type.value if identity.type else idx_type,
            similarity=float(round(score, 4)),  # Convert numpy type to Python float
            confidence_band=self._get_confidence_band(score),
            best_snapshot_path=identity.best_snapshot_path,  # Keep original path for reference
            snapshot_url=snapshot_url,  # Backend provides ready-to-use URL
            last_seen_at=identity.last_seen_at,
            appearances_count=identity.appearances_count or 0
        )
    
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


