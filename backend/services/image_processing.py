"""
Image Processing Service
========================
Core image processing function for face recognition.
"""

import os
import sys
import asyncio
import logging
import base64
import time
import uuid
import cv2
import numpy as np
from typing import Optional
from datetime import datetime

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from backend.config import FACE_TRACKING_ENABLED, FACE_TRACKING_SIMILARITY_THRESHOLD
from backend.core import (
    model_manager, face_tracker, batch_writer,
    processing_queue, ws_manager
)
# We'll import it inside the function to get the current instance
from backend.core.metrics import metrics_faces_detected, metrics_faces_batch_skipped
from db_connection import db_manager
from db_models import Pipeline, IdentityType, LabelState
from sqlalchemy import select, update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from utils.helpers import face_alignment, reference_alignment
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# =====================================================
# INFERENCE OFF-LOOP EXECUTION
# =====================================================
# ONNX Runtime and OpenCV release the GIL during their C-level work, so a
# THREAD pool gives real CPU parallelism without duplicating model memory
# (a process pool would load SCRFD+ArcFace once per process).
#
# CRITICAL: before this existed, session.run()/cv2 calls executed directly on
# the single uvicorn event loop - every frame froze ALL endpoints (health,
# auth, websockets), causing the 499/502/timeout production symptoms.
INFERENCE_POOL = ThreadPoolExecutor(
    max_workers=int(settings.INFERENCE_WORKERS),
    thread_name_prefix="inference",
)

# Bounded concurrency: global cap + per-pipeline cap so one busy camera
# cannot starve the others.
_inference_semaphore = asyncio.Semaphore(int(settings.MAX_CONCURRENT_INFERENCE))
_pipeline_semaphores: dict = {}


def _get_pipeline_semaphore(pipeline_id: str) -> asyncio.Semaphore:
    sem = _pipeline_semaphores.get(pipeline_id)
    if sem is None:
        sem = asyncio.Semaphore(int(settings.MAX_CONCURRENT_INFERENCE_PER_PIPELINE))
        _pipeline_semaphores[pipeline_id] = sem
    return sem


def _decode_frame_sync(image_bytes: bytes):
    """CPU-bound JPEG decode - must run in INFERENCE_POOL, never on the loop."""
    return cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)


def _process_crop_sync(crop: np.ndarray) -> dict:
    """Full per-crop CPU sequence in ONE executor hop: detect -> align -> embed.

    Returns a dict describing the outcome so the async caller does only cheap
    branching/logging on the loop. Never raises.
    """
    result = {"stage": None, "bboxes": None, "kpss": None,
              "aligned_face": None, "embedding": None, "error": None,
              "face_bbox": None, "det_score": None,
              "quality": None, "quality_details": None,
              "quality_scorer": None}
    try:
        result["stage"] = "detect"
        bboxes, kpss = model_manager.detector.detect(crop, max_num=1)
        result["bboxes"], result["kpss"] = bboxes, kpss
        if kpss is None or len(kpss) == 0:
            return result  # no face - caller inspects kpss

        landmarks = kpss[0]
        # SCRFD's own box and score, in crop coordinates. Both matter for
        # quality: the FACE box is the right size signal (the upstream person
        # box saturated it), and the detector's own confidence is a face signal
        # where the upstream person-detector confidence was not.
        face_bbox = [int(v) for v in bboxes[0][:4]]
        result["face_bbox"] = face_bbox
        if len(bboxes[0]) > 4:
            result["det_score"] = float(bboxes[0][4])

        result["stage"] = "align"
        aligned_face, _m_inv = face_alignment(crop, landmarks, image_size=112)
        result["aligned_face"] = aligned_face

        result["stage"] = "embed"
        # Raw BGR crop + real landmarks - identical to the enrollment paths
        embedding = model_manager.recognizer.get_embedding(crop, landmarks)
        norm = np.linalg.norm(embedding)
        if norm == 0 or np.isnan(embedding).any():
            result["error"] = "invalid_embedding"
            return result
        result["embedding"] = embedding / norm

        # Quality, computed HERE because this function already holds the crop,
        # the face box and the landmarks, and already runs in INFERENCE_POOL —
        # so the blur/lighting/angle work costs nothing on the event loop.
        result["stage"] = "quality"
        from backend.core.face_quality import quality_score_for_detection
        (result["quality"], result["quality_details"],
         result["quality_scorer"]) = quality_score_for_detection(
            crop, face_bbox, landmarks, result["det_score"])

        result["stage"] = "done"
        return result
    except Exception as e:  # noqa: BLE001 - report, don't crash the worker
        result["error"] = f"{type(e).__name__}: {e}"
        return result

logger = logging.getLogger(__name__)


def _save_cropped_image(crop: np.ndarray, pipeline_id: str, pred_idx: int):
    """
    Save cropped person image to disk for debugging.
    
    Args:
        crop: Cropped image array (BGR format)
        pipeline_id: Pipeline ID
        pred_idx: Prediction index
    """
    try:
        # Get save directory from config
        save_dir = Path(settings.CROPPED_IMAGES_DIR)
        save_dir = save_dir / pipeline_id
        save_dir.mkdir(parents=True, exist_ok=True)
        
        # Create filename with timestamp and prediction index
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')[:-3]  # UTC; filename only
        filename = f"{timestamp}_pred{pred_idx}.jpg"
        filepath = save_dir / filename
        
        # Save image
        cv2.imwrite(str(filepath), crop)
        
        # Log image info
        h, w = crop.shape[:2]
        file_size = filepath.stat().st_size
        logger.debug(f"[PROCESS] 💾 Saved cropped image: {filepath}")
        logger.debug(f"[PROCESS]    Crop size: {w}x{h} pixels, file size: {file_size / 1024:.2f} KB")
        
    except Exception as e:
        logger.error(f"[PROCESS] Error saving cropped image: {e}", exc_info=True)


# Display-name write cache: avoids a DB write per frame. pipeline_id -> (effective_name, ts)
_display_name_cache: dict = {}
_DISPLAY_NAME_CACHE_TTL = 60.0  # seconds


async def ensure_pipeline_registered(db, pipeline_id: str, location_name: Optional[str] = None):
    """Race-safe pipeline registration: INSERT ... ON CONFLICT DO NOTHING.

    Guarantees the pipelines row exists before ANY dependent row (detections FK,
    plus the free-string pipeline_id columns in identity_embeddings/appearances)
    references it - safe under concurrent workers first-sighting the same name.

    If `location_name` is provided, it fills the pipeline's display name ONLY when
    empty - an admin-set name is never overwritten (precedence: admin > webhook).
    """
    now = datetime.utcnow()
    stmt = pg_insert(Pipeline).values(
        pipeline_id=pipeline_id,
        total_detections=0,
        is_active=1,
        created_at=now,
        updated_at=now,
        location_name=location_name,
    ).on_conflict_do_nothing(index_elements=["pipeline_id"])
    await db.execute(stmt)

    if location_name:
        # Fill-empty-only: never clobber an admin-defined display name
        await db.execute(
            sa_update(Pipeline)
            .where(
                Pipeline.pipeline_id == pipeline_id,
                (Pipeline.location_name.is_(None)) | (Pipeline.location_name == ''),
            )
            .values(location_name=location_name, updated_at=now)
        )


async def resolve_display_name(pipeline_id: str, webhook_location_name: Optional[str]) -> Optional[str]:
    """Effective display name for broadcasts. Precedence: DB/admin > webhook > None.

    Cached ~60s per pipeline to avoid a query per frame.
    """
    now_ts = time.time()
    cached = _display_name_cache.get(pipeline_id)
    if cached and now_ts - cached[1] < _DISPLAY_NAME_CACHE_TTL:
        return cached[0] or webhook_location_name

    db_name = None
    try:
        async with db_manager.get_session() as db:
            result = await db.execute(
                select(Pipeline.location_name).where(Pipeline.pipeline_id == pipeline_id)
            )
            db_name = result.scalar()
    except Exception as e:
        logger.debug(f"[PROCESS] Display-name lookup failed (non-fatal): {e}")

    _display_name_cache[pipeline_id] = (db_name, now_ts)
    return db_name or webhook_location_name


async def process_image_async(
    image_bytes: bytes,
    pipeline_id: str,
    predictions: list,
    use_batch_write: bool = True,
    send_realtime_updates: bool = True,
    worker_id: Optional[int] = None,
    location_name: Optional[str] = None
) -> Optional[dict]:
    """
    OPTIMIZED VERSION with fixes:
    - Improved deduplication logic (embeddings first, then names)
    - Better error handling
    - Fixed race conditions in batch tracking
    - More efficient memory usage
    - Consistent Unknown face handling

    `location_name` is the display-only friendly name from the webhook payload;
    it is persisted (fill-empty-only) and attached to realtime broadcasts.
    """

    start_time = time.time()

    # EARLY pipeline registration: ensures every row written during this request
    # (identity embeddings/appearances included) references a registered pipeline.
    # Also persists the webhook-provided display name (fill-empty-only, cached).
    try:
        cached = _display_name_cache.get(pipeline_id)
        needs_write = location_name and not (cached and cached[0])
        async with db_manager.get_session() as db:
            await ensure_pipeline_registered(
                db, pipeline_id, location_name=location_name if needs_write else None
            )
    except Exception as e:
        logger.warning(f"[PROCESS] Early pipeline registration failed (will retry at save): {e}")

    # Effective display name for this request's broadcasts (admin > webhook > None)
    display_name = await resolve_display_name(pipeline_id, location_name)
    logger.info("[PROCESS] pipeline_id=%s location_name=%r", pipeline_id, display_name)

    # Decode image OFF the event loop (CPU-bound JPEG decode)
    loop = asyncio.get_running_loop()
    try:
        frame = await loop.run_in_executor(INFERENCE_POOL, _decode_frame_sync, image_bytes)
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
    logger.debug(f"[PROCESS] Image dimensions: {W}x{H}, frame shape: {frame.shape}")
    
    # Log first prediction structure for debugging
    if predictions and len(predictions) > 0:
        first_pred = predictions[0] if isinstance(predictions[0], dict) else {}
        logger.debug(f"[PROCESS] First prediction keys: {list(first_pred.keys()) if isinstance(first_pred, dict) else 'N/A'}")
        if isinstance(first_pred, dict) and "bbox" in first_pred:
            logger.debug(f"[PROCESS] First prediction bbox: {first_pred.get('bbox')}, image_index: {first_pred.get('image_index', 'N/A')}")
    
    # Validate that bboxes are within image bounds
    invalid_bbox_count = 0
    
    valid_predictions = 0
    skipped_predictions = 0
    no_face_in_crop = 0  # Track crops with no faces detected
    faces_detected = 0  # Track successful face detections

    for pred_idx, pred in enumerate(predictions):
        class_name = pred.get("class_name", "").lower()
        confidence = pred.get("confidence", 0.0)
        
        logger.debug(f"[PROCESS] Prediction {pred_idx}: class='{class_name}', confidence={confidence:.3f}, bbox={pred.get('bbox', 'N/A')}")
        
        if class_name not in ("person", "face"):
            logger.debug(f"[PROCESS] Skipping prediction {pred_idx}: class '{class_name}' is not 'person' or 'face'")
            skipped_predictions += 1
            continue
        
        valid_predictions += 1

        # Extract and validate bbox
        try:
            bbox = pred["bbox"]
            if len(bbox) != 4:
                logger.warning(f"[PROCESS] Invalid bbox length in prediction {pred_idx}: {bbox}")
                continue

            x1, y1, x2, y2 = map(int, bbox)
            
            # Validate bbox is within image bounds
            original_x1, original_y1, original_x2, original_y2 = x1, y1, x2, y2
            
            # CRITICAL: Check if bbox is WAY outside image bounds (likely wrong image)
            # This indicates bbox belongs to a different image
            bbox_width = abs(original_x2 - original_x1)
            bbox_height = abs(original_y2 - original_y1)
            way_outside = (
                original_x2 < -W * 0.5 or original_x1 > W * 1.5 or  # Way outside horizontally
                original_y2 < -H * 0.5 or original_y1 > H * 1.5     # Way outside vertically
            )
            
            if way_outside:
                invalid_bbox_count += 1
                logger.error(f"[PROCESS] ❌❌❌ CRITICAL: Bbox {pred_idx} is WAY outside image bounds!")
                logger.error(f"[PROCESS] Bbox: ({original_x1},{original_y1})-({original_x2},{original_y2}), size: {bbox_width}x{bbox_height}")
                logger.error(f"[PROCESS] Image: {W}x{H}")
                logger.error(f"[PROCESS] Prediction image_index: {pred.get('image_index', 'N/A')}, frame_index: {pred.get('frame_index', 'N/A')}")
                logger.error(f"[PROCESS] This bbox likely belongs to a DIFFERENT image! SKIPPING this prediction.")
                continue
            
            x1 = max(0, min(x1, W - 1))
            y1 = max(0, min(y1, H - 1))
            x2 = max(0, min(x2, W))
            y2 = max(0, min(y2, H))
            
            # Check if bbox was clipped (outside image bounds but not way outside)
            if (original_x1 != x1 or original_y1 != y1 or original_x2 != x2 or original_y2 != y2):
                invalid_bbox_count += 1
                logger.warning(f"[PROCESS] ⚠️ Bbox {pred_idx} was clipped: original=({original_x1},{original_y1})-({original_x2},{original_y2}), clipped=({x1},{y1})-({x2},{y2}), image_size=({W}x{H})")
                logger.warning(f"[PROCESS] Prediction image_index: {pred.get('image_index', 'N/A')}, frame_index: {pred.get('frame_index', 'N/A')}")
                logger.warning(f"[PROCESS] This suggests bbox coordinates don't match the image dimensions!")

            if x2 <= x1 or y2 <= y1:
                logger.warning(f"[PROCESS] ⚠️ Invalid bbox dimensions after clipping: ({x1},{y1})-({x2},{y2}) for prediction {pred_idx}")
                logger.warning(f"[PROCESS] Original bbox: ({original_x1},{original_y1})-({original_x2},{original_y2}), image size: {W}x{H}")
                continue
        except (ValueError, KeyError, IndexError, TypeError) as e:
            logger.warning(f"[PROCESS] Invalid bbox in prediction {pred_idx}: {e}, bbox={pred.get('bbox')}")
            continue

        # Extract face crop
        crop = frame[y1:y2, x1:x2]
        crop_h, crop_w = crop.shape[:2] if len(crop.shape) >= 2 else (0, 0)
        
        logger.debug(f"[PROCESS] Crop {pred_idx}: bbox=({x1},{y1})-({x2},{y2}), crop_size=({crop_w}x{crop_h}), crop_shape={crop.shape if crop.size > 0 else 'empty'}")
        
        if crop.size == 0:
            logger.warning(f"[PROCESS] ⚠️ Empty crop at bbox ({x1},{y1})-({x2},{y2}) for prediction {pred_idx}")
            continue
        
        if crop_h < 10 or crop_w < 10:
            logger.warning(f"[PROCESS] ⚠️ Crop too small: {crop_w}x{crop_h} pixels (minimum 10x10 required) for prediction {pred_idx}")
            continue
        
        # Save cropped image for debugging (if enabled) - off-loop
        if settings.SAVE_CROPPED_IMAGES:
            try:
                await loop.run_in_executor(INFERENCE_POOL, _save_cropped_image, crop, pipeline_id, pred_idx)
            except Exception as e:
                logger.debug(f"[PROCESS] Failed to save cropped image: {e}")

        # =====================================================
        # OFF-LOOP INFERENCE: detect -> align -> embed in ONE executor hop,
        # bounded globally and per pipeline so cameras can't starve each other
        # or the event loop.
        # =====================================================
        async with _inference_semaphore:
            async with _get_pipeline_semaphore(pipeline_id):
                # Utilization of the hard 3-wide inference bottleneck. When
                # this gauge sits at MAX_CONCURRENT_INFERENCE while the queue
                # grows, inference is the limiting resource — previously that
                # diagnosis required guesswork.
                from backend.core.metrics import metrics_inference_in_flight
                if metrics_inference_in_flight:
                    metrics_inference_in_flight.inc()
                infer_start = time.time()
                try:
                    crop_result = await loop.run_in_executor(INFERENCE_POOL, _process_crop_sync, crop)
                finally:
                    if metrics_inference_in_flight:
                        metrics_inference_in_flight.dec()
                infer_ms = (time.time() - infer_start) * 1000

        if crop_result["error"]:
            logger.error(f"[PROCESS] ❌ Inference error in crop {pred_idx} at stage '{crop_result['stage']}': {crop_result['error']}")
            continue

        kpss = crop_result["kpss"]
        bboxes = crop_result["bboxes"]
        if kpss is None or len(kpss) == 0:
            no_face_in_crop += 1
            logger.debug(
                f"[PROCESS] No face landmarks in crop {pred_idx} "
                f"(bbox=({x1},{y1})-({x2},{y2}), size={crop_w}x{crop_h}) - "
                f"person facing away / crop too small / occluded / bad upstream bbox"
            )
            continue

        faces_detected += 1
        landmarks = kpss[0]
        aligned_face = crop_result["aligned_face"]
        embedding = crop_result["embedding"]
        logger.debug(f"[PROCESS] Crop {pred_idx}: detect+align+embed done in {infer_ms:.1f}ms (norm={np.linalg.norm(embedding):.4f})")

        # =====================================================
        # IDENTITY-BASED FACE RECOGNITION
        # =====================================================
        identity = None
        name = "Unknown"
        similarity = 0.0
        is_new_identity = False
        label_state = LabelState.AUTO_UNKNOWN
        
        try:
            # Try to use identity service (new system)
            from backend.core.identity_service import identity_service
            
            if identity_service is not None:
                # Quality score.
                #
                # This used to be computed from `(x2-x1)*(y2-y1)` — the UPSTREAM
                # PERSON box — plus `pred["confidence"]`, the upstream object
                # detector's score. Neither describes the face. The person box
                # is always far larger than 100x100, so the size term saturated
                # and the whole score collapsed to `0.3 + 0.2*confidence`, i.e.
                # BELOW the 0.5 KNOWN threshold for any confidence under 1.0.
                # Known people were not accumulating embeddings at all.
                quality_score = crop_result.get("quality")
                quality_scorer = crop_result.get("quality_scorer")
                if quality_score is None:
                    # Fallback must yield a NUMBER: every gate downstream reads
                    # `is not None and < threshold`, so None admits everything.
                    fb = crop_result.get("face_bbox")
                    face_size = ((fb[2] - fb[0]) * (fb[3] - fb[1])) if fb else 0
                    quality_score = identity_service.compute_quality_score(
                        face_size=face_size,
                        confidence=crop_result.get("det_score"),
                    )
                    from backend.core.face_quality import LEGACY_SCORER_VERSION
                    quality_scorer = LEGACY_SCORER_VERSION
                
                frame_embedding_id = None
                frame_identity_created = False
                frame_secondary_embedding_ids = []
                # Get database session for identity operations
                async with db_manager.get_session() as db:
                    try:
                        # Find or create identity
                        # ===== STEP-BY-STEP LOGGING: Identity Recognition Flow =====
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] ========================================")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] 🔍 Step 3: Identity Recognition (pgvector Search)")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] Pipeline: {pipeline_id}")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] Using pgvector backend for similarity search")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] Embedding shape: {embedding.shape}, norm: {np.linalg.norm(embedding):.4f}")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] Quality score: {quality_score:.4f}")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] ========================================")
                        
                        # Verify identity_service is available
                        if identity_service is None:
                            logger.error(f"[PROCESS] [STEP-BY-STEP] ❌ CRITICAL: identity_service is None!")
                            raise RuntimeError("identity_service is not initialized")
                        
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] Calling identity_service.find_or_create_identity()...")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] identity_service.use_pgvector: {identity_service.use_pgvector}")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] identity_service.pgvector_index: {identity_service.pgvector_index is not None}")
                        
                        resolution = await identity_service.find_or_create_identity(
                            embedding=embedding,
                            pipeline_id=pipeline_id,
                            detection_id=None,  # linked EXACTLY by persist_detection via embedding_id
                            db=db,
                            quality_score=quality_score,
                            quality_scorer_version=quality_scorer
                        )
                        identity, is_new_identity, similarity = (
                            resolution.identity, resolution.is_new_identity, resolution.similarity)
                        # Ownership metadata: the embedding row(s) THIS frame inserted.
                        # Only ids produced by this processing attempt ever land
                        # here, so "created by this frame" is structural, never
                        # inferred from pipeline_id.
                        frame_embedding_id = resolution.embedding_id
                        frame_identity_created = resolution.identity_created
                        
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] ✅ find_or_create_identity() returned successfully")
                        
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] ========================================")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] ✅ Identity Recognition Complete")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] Identity ID: {identity.id}")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] Identity Type: {identity.type.value}")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] Display Name: {identity.display_name}")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] Is New Identity: {is_new_identity}")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] Similarity Score: {similarity:.4f}")
                        logger.debug(f"[PROCESS] [STEP-BY-STEP] ========================================")
                        
                        # Determine name and label state
                        if identity.type == IdentityType.KNOWN:
                            name = identity.display_name or "Unknown"
                            label_state = LabelState.AUTO_KNOWN
                            logger.debug(f"[PROCESS] [STEP-BY-STEP] ✅ Face will be shown on DASHBOARD (KNOWN identity: {name})")
                        else:
                            name = "Unknown"
                            label_state = LabelState.AUTO_UNKNOWN
                            logger.debug(f"[PROCESS] [STEP-BY-STEP] ⚠️ Face will NOT be shown on DASHBOARD (UNKNOWN identity)")
                            logger.debug(f"[PROCESS] [STEP-BY-STEP] ⚠️ UNKNOWN faces are filtered out by default (SHOW_UNKNOWN_FACES_ON_DASHBOARD=False)")
                        
                        # Save embedding (if quality is good)
                        # NOTE: For NEW identities, embedding is already saved in _create_unknown_identity() or _create_known_identity()
                        # Only save embedding for EXISTING identities (when is_new_identity=False)
                        # This prevents duplicate embeddings and ensures pgvector works correctly
                        if not is_new_identity:
                            # For existing identities, save additional embedding (for tracking multiple appearances)
                            saved = await identity_service.save_embedding(
                                identity=identity,
                                embedding=embedding,
                                detection_id=None,  # linked EXACTLY by persist_detection via embedding_id
                                pipeline_id=pipeline_id,
                                quality_score=quality_score,
                                quality_scorer_version=quality_scorer,
                                db=db
                            )
                            if saved is not None and getattr(saved, "id", None) is not None:
                                # save_embedding's row is the frame's primary embedding;
                                # an enrichment row (rare, gated) is also frame-created
                                # and travels in the secondary list.
                                if frame_embedding_id is not None and frame_embedding_id != saved.id:
                                    frame_secondary_embedding_ids.append(frame_embedding_id)
                                frame_embedding_id = saved.id
                        else:
                            # For new identities, embedding was already saved in _create_unknown_identity() or _create_known_identity()
                            # Verify embedding was saved (check if quality threshold was met)
                            if quality_score is not None and quality_score < identity_service.quality_threshold:
                                logger.warning(f"[PROCESS] New identity {identity.id} created but embedding NOT saved (quality {quality_score:.2f} < threshold {identity_service.quality_threshold})")
                            else:
                                logger.debug(f"[PROCESS] New identity {identity.id} created with embedding saved during creation")
                        
                        await db.commit()
                    except Exception as identity_error:
                        # Rollback transaction on any error to prevent "transaction aborted" state
                        logger.error(f"[PROCESS] Error in identity operations: {identity_error}", exc_info=True)
                        try:
                            await db.rollback()
                            logger.debug(f"[PROCESS] Transaction rolled back after identity error")
                        except Exception as rollback_error:
                            logger.error(f"[PROCESS] Failed to rollback transaction: {rollback_error}", exc_info=True)
                        # Re-raise to be caught by outer exception handler
                        raise
                
                logger.info(f"[PROCESS] Identity: {identity.id} ({identity.type.value}), name: {name}, sim: {similarity:.4f}, new: {is_new_identity}")
                
                # CRITICAL LOGGING: Check if known identity is being created as unknown
                if identity.type == IdentityType.KNOWN:
                    logger.info(f"[PROCESS] ✅ KNOWN identity recognized: {name} (ID: {identity.id})")
                elif identity.type == IdentityType.UNKNOWN:
                    logger.warning(f"[PROCESS] ⚠️ UNKNOWN identity created/found: {name} (ID: {identity.id})")
                    if name and name.lower() != "unknown":
                        logger.error(f"[PROCESS] ❌❌❌ CRITICAL: Identity with name '{name}' is UNKNOWN type! This should be KNOWN!")
                        logger.error(f"[PROCESS] This means the person was not recognized from KNOWN index!")
            else:
                # No identity service — skip this face. The "legacy fallback"
                # that used to run here searched the display-name-keyed
                # FaceDatabase, which enrollment stopped writing when pgvector
                # became authoritative. It was empty by construction: the
                # fallback answered "Unknown" for every face, 100% of the time,
                # while logging "Match (legacy)" as if it had checked something.
                # A face processed without the identity system is a face NOT
                # recognized — say so and move on.
                logger.error("[PROCESS] identity_service unavailable — face skipped, "
                             "not silently misclassified")
                continue
        except Exception as e:
            logger.error(f"[PROCESS] Identity/face search error: {e}", exc_info=True)
            continue

        # Optionally skip Unknown faces (make this configurable)
        if settings.SKIP_UNKNOWN_FACES and name == "Unknown":
            logger.debug(f"[PROCESS] Skipping Unknown face (config: SKIP_UNKNOWN_FACES={settings.SKIP_UNKNOWN_FACES})")
            continue

        # =====================================================
        # IMPROVED DEDUPLICATION LOGIC
        # =====================================================

        # 1. FIRST: Check if this is a duplicate in CURRENT batch using embeddings.
        # IMPORTANT: only compare against faces recognized as the SAME name.
        # Comparing across names could silently drop a genuinely different person
        # detected in the same frame (two people must never dedup each other).
        duplicate_in_batch = False

        same_name_embeddings = batch_face_embeddings.get(name, [])
        if same_name_embeddings:
            # Vectorized similarity computation (same-name candidates only)
            embeddings_array = np.array(same_name_embeddings)
            similarities = np.dot(embeddings_array, embedding)

            max_sim = similarities[np.argmax(similarities)]

            if max_sim >= FACE_TRACKING_SIMILARITY_THRESHOLD:
                logger.info(f"[BATCH] Skipping face - high similarity ({max_sim:.3f}) with same-name '{name}' in same image")
                metrics_faces_batch_skipped.labels(name=name).inc()
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
        # SAVE THE FACE IMAGE (ORGANIZED BY PERSON FOLDER)
        # =====================================================
        face_filename = None
        if settings.SAVE_IMAGES:
            # Check if we should save unknown faces
            is_unknown = name.lower() == "unknown"
            if is_unknown and not settings.SAVE_UNKNOWN_FACES:
                # Still generate path for database, but don't save file to disk
                safe_name = "unknown"
                person_dir = os.path.join(settings.STORAGE_DIR, pipeline_id, safe_name)
                timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                face_filename = os.path.normpath(os.path.join(person_dir, f"{safe_name}_{timestamp}.jpg"))
                logger.debug(f"[PROCESS] Unknown face - path saved to DB: {face_filename}, but file NOT saved to disk (SAVE_UNKNOWN_FACES=False)")
            else:
                try:
                    # Create folder structure: storage/pipeline_id/name/ (or storage/pipeline_id/unknown/)
                    if is_unknown:
                        safe_name = "unknown"
                    else:
                        safe_name = "".join(c for c in name if c.isalnum() or c in ('-', '_')).lower()
                    
                    person_dir = os.path.join(settings.STORAGE_DIR, pipeline_id, safe_name)
                    os.makedirs(person_dir, exist_ok=True)

                    # Count existing images in the person's folder
                    existing_images = []
                    if os.path.exists(person_dir):
                        for filename in os.listdir(person_dir):
                            if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
                                existing_images.append(filename)
                    
                    # Get threshold from settings
                    max_photos_per_person = settings.MAX_PHOTOS_PER_PERSON
                    
                    # For unknown faces, don't apply the threshold limit (save all unknown faces)
                    # This allows multiple different unknown people to have their images saved
                    apply_threshold = not is_unknown
                    
                    # Always generate path for database (regardless of threshold)
                    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                    face_filename = os.path.join(person_dir, f"{safe_name}_{timestamp}.jpg")
                    face_filename = os.path.normpath(face_filename)
                    
                    # Threshold only controls whether to save the file to disk (NOT database)
                    # For unknown faces, always save (no threshold)
                    if apply_threshold and len(existing_images) >= max_photos_per_person:
                        # The log used to say "Path saved to DB: <path>" on the
                        # line above `face_filename = None`, so it advertised a
                        # stored path while the column received NULL — anyone
                        # debugging from logs went looking for a row that says
                        # something it does not say.
                        logger.info(
                            "[PROCESS] MAX_PHOTOS_PER_PERSON reached for %s "
                            "(%d/%d): no file written and no path stored.",
                            name, len(existing_images), max_photos_per_person)
                        face_filename = None
                    else:
                        # Save the aligned face image to disk (off-loop: JPEG encode + disk write)
                        success = await loop.run_in_executor(
                            INFERENCE_POOL, cv2.imwrite, face_filename, aligned_face,
                            [cv2.IMWRITE_JPEG_QUALITY, 90]
                        )
                        if success:
                            logger.info(f"[PROCESS] Saved face image to disk: {face_filename} ({len(existing_images) + 1}/{max_photos_per_person if apply_threshold else 'unlimited'})")
                        else:
                            logger.error(f"[PROCESS] Failed to write image to {face_filename}, but path still saved to database")
                            # face_filename is still set for database even if write failed
                except Exception as e:
                    logger.error(f"[PROCESS] Error processing face image for {name}: {e}", exc_info=True)
                    # Try to still generate a path for database even on error
                    try:
                        if is_unknown:
                            safe_name = "unknown"
                        else:
                            safe_name = "".join(c for c in name if c.isalnum() or c in ('-', '_')).lower()
                        person_dir = os.path.join(settings.STORAGE_DIR, pipeline_id, safe_name)
                        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                        face_filename = os.path.normpath(os.path.join(person_dir, f"{safe_name}_{timestamp}.jpg"))
                        logger.warning(f"[PROCESS] Generated path for database despite error: {face_filename}")
                    except:
                        face_filename = None
                        logger.error(f"[PROCESS] Could not generate path for database")

        # Encode aligned face for database/frontend (off-loop JPEG encode)
        try:
            ok, buf = await loop.run_in_executor(INFERENCE_POOL, cv2.imencode, ".jpg", aligned_face)
            if not ok:
                logger.warning(f"[PROCESS] Failed to encode face image")
                continue
            face_b64 = base64.b64encode(buf).decode()
        except Exception as e:
            logger.error(f"[PROCESS] Face encoding error: {e}")
            continue

        # Prepare face data (simple - same as unknown images)
        # Use Identity.display_name (folder name) for consistency - ensures all images from same person show same name
        display_name_for_dashboard = identity.display_name if identity and identity.display_name else name
        face_data = {
            "name": display_name_for_dashboard,  # Use Identity.display_name (folder name) for consistency
            "similarity": float(similarity),
            "image": face_b64,  # Base64 image for frontend
            "bbox": [float(x1), float(y1), float(x2), float(y2)],
            "face_image_path": face_filename,  # Path to saved face image (None if not saved)
            "identity_id": identity.id if identity else None,
            "label_state": label_state,
            # The quality THIS crop scored at ingest, carried forward so
            # create_appearance can compare like-for-like. Without these the
            # batch writer re-read quality from identity_embeddings by
            # detection_id — but the embedding is only back-linked to the
            # detection AFTER create_appearance runs, so the read always
            # missed and the snapshot decision fell through to the
            # similarity-only branch.
            "quality": quality_score,
            "quality_scorer": quality_scorer,
            # Provenance carried to persist_detection: the exact embedding row(s)
            # this frame inserted (never pre-existing), whether the identity was
            # created by this frame, and a stable per-face event id that the
            # post-commit `detection_alerts` event echoes as detection_event_id.
            "_embedding_id": frame_embedding_id,
            "_embedding_created_by_this_frame": frame_embedding_id is not None,
            "_secondary_embedding_ids": list(frame_secondary_embedding_ids),
            "_identity_created_by_this_frame": bool(frame_identity_created),
            "_event_id": uuid.uuid4().hex,
            "_is_known": bool(identity and identity.type == IdentityType.KNOWN),
        }
        
        # Simple logging
        if face_filename:
            logger.info(f"[PROCESS] ✅ Saved image for {name}: {face_filename}")
        else:
            logger.debug(f"[PROCESS] No image saved for {name} (SAVE_IMAGES={settings.SAVE_IMAGES})")

        detected_faces.append(face_data)
        new_faces_count += 1
        metrics_faces_detected.labels(name=name).inc()

        # =====================================================
        # REAL-TIME UPDATE: Send NEW face immediately
        # =====================================================
        if send_realtime_updates:
            try:
                # CRITICAL BACKEND FILTER: Only send KNOWN faces to dashboard
                # Check both label_state and identity.type for maximum security
                is_known_face = (
                    label_state == LabelState.AUTO_KNOWN or
                    (identity and identity.type == IdentityType.KNOWN)
                )
                
                # Also check name as fallback (for legacy data)
                is_unknown_by_name = name.lower() == "unknown"
                
                if is_known_face and not is_unknown_by_name:
                    # For KNOWN persons: ALWAYS send WebSocket message (alerts must work)
                    # Check notification window to determine if this is a "new" detection for alert purposes
                    should_show_alert = True
                    is_new_detection = True  # Track if this is truly a new detection
                    
                    if FACE_TRACKING_ENABLED:
                        should_show_alert = await face_tracker.should_send_frontend_notification(pipeline_id, name, similarity)
                        # If should_show_alert is True, it means this is a new detection (first time or after 1-hour window)
                        is_new_detection = should_show_alert
                    
                    logger.info(f"[PROCESS] Known person detected: {name} (sim={similarity:.3f}) in {pipeline_id} - should_show_alert={should_show_alert}, is_new_detection={is_new_detection}")
                    
                    # Live-alert triggers and watchlist alerts are PERSISTED and
                    # broadcast AFTER the detection row is committed
                    # (backend/core/detection_evidence.py → `detection_alerts`).
                    # This pre-commit event carries the detection only.
                    realtime_result = {
                        # Stable event id: frontends deduplicate popups/sounds on
                        # this across reconnects and duplicate deliveries; the
                        # post-commit detection_alerts event echoes it.
                        "event_id": face_data["_event_id"],
                        "created_at": datetime.utcnow().isoformat() + "Z",
                        "pipeline_id": pipeline_id,
                        "location_name": display_name,  # friendly display title (may be None)
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "processing_time_ms": (time.time() - start_time) * 1000,
                        "faces": [{k: v for k, v in face_data.items() if not k.startswith("_")}],
                        "should_show_alert": should_show_alert,  # Flag for frontend to decide if alert should be shown
                        "is_new_detection": is_new_detection,  # Backend flag: True if first time or after 1-hour window
                    }

                    # Get stats for context
                    stats = await processing_queue.get_stats()
                    tracker_stats = await face_tracker.get_stats() if FACE_TRACKING_ENABLED else {"enabled": False}

                    # REDIS CACHE INVALIDATION: Invalidate cache when new KNOWN face is detected
                    try:
                        from backend.core.redis_cache import redis_cache_service
                        if redis_cache_service._enabled:
                            # Invalidate dashboard cache for all users (new detection affects all)
                            deleted = await redis_cache_service.invalidate_dashboard_cache(user_id=None)
                            if deleted > 0:
                                logger.debug(f"[PROCESS] [CACHE] 🗑️  Invalidated {deleted} dashboard cache entries (new KNOWN face detected)")
                    except Exception as cache_error:
                        logger.debug(f"[PROCESS] [CACHE] Cache invalidation error (non-critical): {cache_error}")
                    
                    # Broadcast to all connected clients (filtered by pipeline access)
                    logger.debug(f"[PROCESS] 📡 Broadcasting new_detection for '{name}' (sim={similarity:.3f}) in pipeline '{pipeline_id}' to WebSocket clients")
                    logger.debug(f"[PROCESS] 📦 Message data: pipeline_id={pipeline_id}, location_name={display_name!r}, timestamp={realtime_result['timestamp']}, is_new_detection={is_new_detection}, should_show_alert={should_show_alert}")
                    try:
                        await ws_manager.broadcast({
                            "type": "new_detection",
                            "data": realtime_result,
                            "stats": stats,
                            "tracker_stats": tracker_stats,
                        }, pipeline_id=pipeline_id)
                        logger.debug(f"[PROCESS] ✅ Broadcast completed for '{name}' in '{pipeline_id}'")
                    except Exception as broadcast_error:
                        logger.error(f"[PROCESS] ❌ Broadcast error for '{name}' in '{pipeline_id}': {broadcast_error}", exc_info=True)

                    if should_show_alert:
                        logger.debug(f"✅ Real-time: Sent KNOWN face {name} (sim={similarity:.2f}) from {pipeline_id} [ALERT ENABLED]")
                    else:
                        logger.debug(f"✅ Real-time: Sent KNOWN face {name} (sim={similarity:.2f}) from {pipeline_id} [UPDATE ONLY - within 1-hour window]")
                else:
                    # For UNKNOWN faces: Send to unknown page via WebSocket (grouped by pipeline)
                    # Unknown faces should appear in real-time on the unknown page, organized by pipeline
                    logger.info(f"[PROCESS] Unknown person detected: {name} (sim={similarity:.3f}) in {pipeline_id} - sending to unknown page")
                    
                    # Watchlist alerts for unknown identities are persisted and
                    # broadcast after commit as `detection_alerts` — not here.
                    # Prepare unknown face data for unknown page
                    # Include identity_id if available so frontend can update existing identity or create new one
                    unknown_realtime_result = {
                        "pipeline_id": pipeline_id,
                        "location_name": display_name,  # friendly display title (may be None)
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "processing_time_ms": (time.time() - start_time) * 1000,
                        "face": {k: v for k, v in face_data.items() if not k.startswith("_")},  # Single face object (not array)
                        "identity_id": str(identity.id) if identity else None,  # Include identity ID if available
                        "label_state": label_state.value if label_state else None,
                    }
                    
                    # Get stats for context
                    stats = await processing_queue.get_stats()
                    tracker_stats = await face_tracker.get_stats() if FACE_TRACKING_ENABLED else {"enabled": False}
                    
                    # REDIS CACHE INVALIDATION: Invalidate cache when new UNKNOWN face is detected
                    try:
                        from backend.core.redis_cache import redis_cache_service
                        if redis_cache_service._enabled:
                            # Invalidate both dashboard and unknown page caches
                            deleted_dashboard = await redis_cache_service.invalidate_dashboard_cache(user_id=None)
                            deleted_unknown = await redis_cache_service.invalidate_unknown_cache(user_id=None)
                            if deleted_dashboard > 0 or deleted_unknown > 0:
                                logger.debug(f"[PROCESS] [CACHE] 🗑️  Invalidated {deleted_dashboard} dashboard + {deleted_unknown} unknown cache entries (new UNKNOWN face detected)")
                    except Exception as cache_error:
                        logger.debug(f"[PROCESS] [CACHE] Cache invalidation error (non-critical): {cache_error}")
                    
                    # Broadcast to all connected clients (filtered by pipeline access)
                    # Unknown faces go to unknown page, so we use a different message type
                    logger.debug(f"[PROCESS] 📡 Broadcasting new_unknown_detection for '{name}' (sim={similarity:.3f}) in pipeline '{pipeline_id}' to WebSocket clients")
                    logger.debug(f"[PROCESS] 📦 Unknown message data: pipeline_id={pipeline_id}, location_name={display_name!r}, timestamp={unknown_realtime_result['timestamp']}, identity_id={unknown_realtime_result.get('identity_id')}")
                    try:
                        await ws_manager.broadcast({
                            "type": "new_unknown_detection",
                            "data": unknown_realtime_result,
                            "stats": stats,
                            "tracker_stats": tracker_stats,
                        }, pipeline_id=pipeline_id)
                        logger.debug(f"[PROCESS] ✅ Broadcast completed for UNKNOWN '{name}' in '{pipeline_id}'")
                        logger.debug(f"✅ Real-time: Sent UNKNOWN face {name} (sim={similarity:.2f}) from {pipeline_id} to unknown page")

                        # Also notify dashboard clients with a LIGHTWEIGHT event (no image
                        # data) so they can show an "N unknown" badge on the camera card.
                        # new_unknown_detection itself is filtered to unknown-page clients.
                        await ws_manager.broadcast({
                            "type": "unknown_activity",
                            # Stable id so clients can deduplicate badge bumps
                            "event_id": face_data["_event_id"],
                            "created_at": datetime.utcnow().isoformat() + "Z",
                            "pipeline_id": pipeline_id,
                            "location_name": display_name,
                            "identity_id": unknown_realtime_result.get("identity_id"),
                        }, pipeline_id=pipeline_id)
                    except Exception as broadcast_error:
                        logger.error(f"[PROCESS] ❌ Broadcast error for UNKNOWN '{name}' in '{pipeline_id}': {broadcast_error}", exc_info=True)
            except Exception as e:
                logger.error(f"❌ Real-time broadcast error: {e}")
                # Don't fail the whole process due to WebSocket error

    # =====================================================
    # BATCH PROCESSING COMPLETION
    # =====================================================
    processing_time = (time.time() - start_time) * 1000
    
    # Ensure pipeline exists in database even if no faces detected
    # This allows pipelines to be registered and assigned to users
    if not detected_faces:
        logger.debug(f"[PROCESS] No new faces detected (all were tracked or no faces found)")
        logger.debug(f"[PROCESS] Summary: {valid_predictions} valid predictions processed, {skipped_predictions} skipped (wrong class)")
        logger.debug(f"[PROCESS] Face Detection Stats: {faces_detected} faces detected, {no_face_in_crop} crops with no face")
        if invalid_bbox_count > 0:
            logger.warning(f"[PROCESS] ⚠️ {invalid_bbox_count} bboxes were clipped (outside image bounds) - bbox/image mismatch detected!")
        if valid_predictions > 0:
            face_detection_rate = (faces_detected / valid_predictions) * 100
            logger.debug(f"[PROCESS] Face Detection Rate: {face_detection_rate:.1f}% ({faces_detected}/{valid_predictions})")
            if no_face_in_crop > 0:
                logger.warning(f"[PROCESS] ⚠️ {no_face_in_crop} person crops had no faces detected - possible reasons:")
                logger.warning(f"[PROCESS]   • Person facing away (back/side view)")
                logger.warning(f"[PROCESS]   • Person bbox from upstream doesn't contain face")
                logger.warning(f"[PROCESS]   • Crop too small or poor quality")
                logger.warning(f"[PROCESS]   • Face occluded or not visible")
                logger.warning(f"[PROCESS]   • Bbox coordinates don't match image (check invalid_bbox_count above)")
        try:
            async with db_manager.get_session() as db:
                await ensure_pipeline_registered(db, pipeline_id)
        except Exception as e:
            logger.error(f"[PROCESS] Error creating pipeline: {e}", exc_info=True)
        
        return None
    logger.info(f"[PROCESS] Detected {new_faces_count} NEW faces (pipeline: {pipeline_id}) in {processing_time:.1f}ms")

    # =====================================================
    # PREPARE DATABASE DATA
    # =====================================================
    detection_data = {
        "pipeline_id": pipeline_id,
        "location_name": display_name,
        "detection": {
            "pipeline_id": pipeline_id,
            "timestamp": datetime.utcnow(),
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
                "identity_id": f.get("identity_id"),
                "label_state": f.get("label_state"),
                "quality": f.get("quality"),
                "quality_scorer": f.get("quality_scorer"),
                # provenance for persist_detection (stripped before insert(Face))
                "_embedding_id": f.get("_embedding_id"),
                "_embedding_created_by_this_frame": f.get("_embedding_created_by_this_frame"),
                "_secondary_embedding_ids": f.get("_secondary_embedding_ids") or [],
                "_identity_created_by_this_frame": f.get("_identity_created_by_this_frame"),
                "_event_id": f.get("_event_id"),
                "_is_known": f.get("_is_known"),
            }
            for f in detected_faces
        ]
    }

    # =====================================================
    # SAVE TO DATABASE — one write path (detection_evidence.persist_detection)
    # =====================================================
    if use_batch_write:
        try:
            await batch_writer.add_detection(detection_data)
            logger.debug(f"[PROCESS] Added {len(detected_faces)} faces to batch writer")
        except Exception as e:
            logger.error(f"[PROCESS] Batch writer error: {e}")
            use_batch_write = False

    if not use_batch_write:
        # Direct write: the SAME function the batch writer runs, in one
        # transaction; broadcast only after commit; compensate on core failure.
        from backend.core.detection_evidence import (
            persist_detection, broadcast_detection_alerts, compensate_failed_detection)
        outcome = None
        try:
            async with db_manager.get_session() as db:
                await ensure_pipeline_registered(db, pipeline_id)
                outcome = await persist_detection(db, detection_data=detection_data)
            logger.info(f"✅ Saved {new_faces_count} faces to DB for pipeline {pipeline_id}")
        except Exception as e:
            from backend.core.metrics import metrics_db_operation_failures
            if metrics_db_operation_failures:
                metrics_db_operation_failures.labels(reason="detection_core").inc()
            logger.error(f"[PROCESS] detection NOT persisted (core failure): {e}", exc_info=True)
            await compensate_failed_detection(detection_data)
        if outcome is not None and outcome.bundles:
            await broadcast_detection_alerts(outcome.bundles, location_name=display_name)

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
# STANDALONE TEST/DEBUG MODE
# =====================================================
if __name__ == "__main__":
    """
    Standalone test mode for debugging image processing.
    
    Usage:
        python backend/services/image_processing.py <image_path> [pipeline_id]
    
    Example:
        python backend/services/image_processing.py test_image.jpg TEST_PIPELINE
        python backend/services/image_processing.py storage/faces/Joey.png TEST_PIPELINE
    """
    import argparse
    from pathlib import Path
    
    # Central logger even in standalone CLI mode. A private basicConfig here
    # would bypass the redaction filter and write nothing to the rotating file,
    # so a debugging run would leak what a served request never would.
    from utils.logging import setup_logging
    setup_logging(log_to_file=True)
    
    parser = argparse.ArgumentParser(description='Test image processing pipeline standalone')
    parser.add_argument('image_path', type=str, help='Path to image file to test')
    parser.add_argument('--pipeline-id', type=str, default='TEST_PIPELINE', help='Pipeline ID (default: TEST_PIPELINE)')
    parser.add_argument('--bbox', type=float, nargs=4, metavar=('X1', 'Y1', 'X2', 'Y2'), 
                       help='Custom bounding box [x1 y1 x2 y2]. If not provided, will use full image')
    parser.add_argument('--no-db', action='store_true', help='Skip database operations (faster testing)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose logging')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)
    
    async def test_image_processing():
        """Test image processing with provided image"""
        print("=" * 80)
        print("IMAGE PROCESSING STANDALONE TEST MODE")
        print("=" * 80)
        
        # Check if image exists
        image_path = Path(args.image_path)
        if not image_path.exists():
            print(f"❌ Error: Image file not found: {args.image_path}")
            return
        
        print(f"📸 Loading image: {args.image_path}")
        
        # Load image
        image = cv2.imread(str(image_path))
        if image is None:
            print(f"❌ Error: Could not read image: {args.image_path}")
            return
        
        h, w = image.shape[:2]
        print(f"✅ Image loaded: {w}x{h} pixels")
        
        # Encode image to bytes (simulating webhook payload)
        _, img_encoded = cv2.imencode('.jpg', image)
        image_bytes = img_encoded.tobytes()
        print(f"✅ Image encoded: {len(image_bytes)} bytes")
        
        # Create predictions (mock person detection)
        if args.bbox:
            x1, y1, x2, y2 = args.bbox
            print(f"📦 Using custom bbox: ({x1}, {y1}) - ({x2}, {y2})")
        else:
            # Use full image as person bbox
            x1, y1, x2, y2 = 0, 0, w, h
            print(f"📦 Using full image as bbox: (0, 0) - ({w}, {h})")
        
        predictions = [{
            "type": "detection",
            "class_id": 0,
            "class_name": "person",
            "confidence": 0.9,
            "bbox": [float(x1), float(y1), float(x2), float(y2)],
            "bbox_format": "xyxy",
            "track_id": 1,
            "face_detected": False,  # Will be detected by internal SCRFD
            "face_confidence": 0.0
        }]
        
        print(f"📋 Created {len(predictions)} mock prediction(s)")
        print(f"   Prediction 0: class='person', confidence=0.9, bbox=[{x1}, {y1}, {x2}, {y2}]")
        
        # Initialize services if needed
        print("\n" + "=" * 80)
        print("INITIALIZING SERVICES")
        print("=" * 80)
        
        try:
            # Initialize model manager
            print("🔄 Initializing model manager...")
            from backend.core.model_manager import ModelManager
            test_model_manager = ModelManager()
            await asyncio.get_event_loop().run_in_executor(None, test_model_manager.initialize)
            
            if not test_model_manager.detector or not test_model_manager.recognizer:
                print("❌ Error: Model manager initialization failed!")
                return
            
            print("✅ Model manager initialized")
            print(f"   Detector: {test_model_manager.detector.__class__.__name__}")
            print(f"   Recognizer: {test_model_manager.recognizer.__class__.__name__}")
            
            # Initialize identity services if not in no-db mode
            test_identity_service = None
            
            if not args.no_db:
                print("\n🔄 Initializing identity services...")
                try:
                    from backend.core.identity_service import IdentityService
                    from backend.core.identity_index_pgvector import get_pgvector_index

                    # This harness reads and writes through the database, so it
                    # needs no index of its own. Building a private FAISS index
                    # here created a second copy of the vectors that diverged
                    # from PostgreSQL the moment the harness exited.
                    test_identity_service = IdentityService(
                        None, pgvector_index=get_pgvector_index())
                    print(f"✅ Identity services initialized "
                          f"(vectors read from identity_embeddings)")
                except Exception as e:
                    print(f"⚠️  Warning: Could not initialize identity services: {e}")
                    print("   Continuing without identity recognition...")
            else:
                print("⏭️  Skipping identity services (--no-db mode)")
            
            # Initialize face tracker (mock)
            print("\n🔄 Initializing face tracker...")
            try:
                from backend.core.face_tracker import FaceTracker
                test_face_tracker = FaceTracker()
                await test_face_tracker.start()
                print("✅ Face tracker initialized")
            except Exception as e:
                print(f"⚠️  Warning: Could not initialize face tracker: {e}")
                test_face_tracker = None
            
            # Initialize batch writer (mock)
            print("\n🔄 Initializing batch writer...")
            try:
                from backend.core.batch_writer import BatchWriter
                test_batch_writer = BatchWriter(db_manager)
                await test_batch_writer.start()
                print("✅ Batch writer initialized")
            except Exception as e:
                print(f"⚠️  Warning: Could not initialize batch writer: {e}")
                test_batch_writer = None
            
            # Initialize database connection
            if not args.no_db:
                print("\n🔄 Initializing database connection...")
                try:
                    await db_manager.initialize()
                    print("✅ Database connection initialized")
                except Exception as e:
                    print(f"⚠️  Warning: Could not initialize database: {e}")
                    print("   Some features may not work")
            
            print("\n" + "=" * 80)
            print("TESTING IMAGE PROCESSING")
            print("=" * 80)
            
            # Temporarily replace global services for testing
            import backend.services.image_processing as img_proc_module
            original_model_manager = img_proc_module.model_manager
            original_identity_service = None
            original_face_tracker = img_proc_module.face_tracker
            original_batch_writer = img_proc_module.batch_writer
            
            # Replace with test instances
            img_proc_module.model_manager = test_model_manager
            if test_identity_service:
                # Set identity_service in the identity_service module (where it's imported from)
                try:
                    from backend.core.identity_service import identity_service as orig_identity_service
                    import backend.core.identity_service as identity_service_module
                    original_identity_service = orig_identity_service
                    identity_service_module.identity_service = test_identity_service
                    # Also set in backend.core module
                    import backend.core as core_module
                    core_module.identity_service = test_identity_service
                except Exception as e:
                    print(f"⚠️  Warning: Could not set identity_service: {e}")
                    original_identity_service = None
            else:
                original_identity_service = None
            img_proc_module.face_tracker = test_face_tracker
            img_proc_module.batch_writer = test_batch_writer
            
            try:
                # Run image processing
                print(f"\n🚀 Processing image with pipeline_id: {args.pipeline_id}")
                print(f"   Predictions: {len(predictions)}")
                print(f"   Image size: {len(image_bytes)} bytes")
                
                result = await process_image_async(
                    image_bytes=image_bytes,
                    pipeline_id=args.pipeline_id,
                    predictions=predictions,
                    use_batch_write=not args.no_db,
                    send_realtime_updates=False,  # Disable WebSocket for testing
                    worker_id=999  # Test worker ID
                )
                
                print("\n" + "=" * 80)
                print("PROCESSING RESULTS")
                print("=" * 80)
                
                if result:
                    print(f"✅ Processing completed successfully!")
                    print(f"   Processing time: {result.get('processing_time_ms', 0):.2f} ms")
                    print(f"   Faces detected: {len(result.get('faces', []))}")
                    print(f"   New faces: {result.get('new_faces_count', 0)}")
                    print(f"   Total processed: {result.get('total_faces_processed', 0)}")
                    
                    if result.get('faces'):
                        print("\n📋 Detected Faces:")
                        for idx, face in enumerate(result['faces']):
                            print(f"   Face {idx + 1}:")
                            print(f"      Name: {face.get('name', 'N/A')}")
                            print(f"      Similarity: {face.get('similarity', 0):.4f}")
                            print(f"      Bbox: {face.get('bbox', [])}")
                            print(f"      Image path: {face.get('face_image_path', 'N/A')}")
                else:
                    print("⚠️  Processing returned None (no faces detected or error occurred)")
                    
            finally:
                # Restore original services
                img_proc_module.model_manager = original_model_manager
                if original_identity_service is not None:
                    try:
                        import backend.core.identity_service as identity_service_module
                        import backend.core as core_module
                        identity_service_module.identity_service = original_identity_service
                        core_module.identity_service = original_identity_service
                    except Exception as e:
                        print(f"⚠️  Warning: Could not restore identity_service: {e}")
                img_proc_module.face_tracker = original_face_tracker
                img_proc_module.batch_writer = original_batch_writer
            
            print("\n" + "=" * 80)
            print("TEST COMPLETE")
            print("=" * 80)
            
        except Exception as e:
            print(f"\n❌ Error during testing: {e}")
            import traceback
            traceback.print_exc()
    
    # Run the test
    try:
        asyncio.run(test_image_processing())
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()

