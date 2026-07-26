"""
Webhook Routes
==============
Webhook endpoint for receiving images from pipelines.
"""

import os
import re
import sys
import base64
import hashlib
import time
import uuid
import logging
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Body
from fastapi.responses import JSONResponse
import json
import cv2
import numpy as np

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from backend.core import processing_queue
from backend.core.metrics import metrics_requests_total
from backend.routes.utils import validate_pipeline_id, validate_image_content
from utils.helpers import extract_images_from_payload
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _save_webhook_image(image_bytes: bytes, pipeline_id: str, request_id: str, image_index: int):
    """
    Save webhook image to disk for debugging.
    
    Args:
        image_bytes: Decoded image bytes
        pipeline_id: Pipeline ID
        request_id: Request ID
        image_index: Index of image in the request
    """
    try:
        # Get save directory from config
        save_dir = Path(getattr(settings, 'WEBHOOK_IMAGES_DIR', './debug/webhook_images'))
        save_dir = save_dir / pipeline_id
        save_dir.mkdir(parents=True, exist_ok=True)
        
        # Create filename with timestamp and request ID
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]  # Include milliseconds
        filename = f"{timestamp}_{request_id}_{image_index}.jpg"
        filepath = save_dir / filename
        
        # Decode image to verify it's valid
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if img is None:
            logger.warning(f"[WEBHOOK] Could not decode image for saving: {filepath}")
            return
        
        # Save image
        cv2.imwrite(str(filepath), img)
        
        # Log image info
        h, w = img.shape[:2]
        file_size = filepath.stat().st_size
        logger.info(f"[WEBHOOK] 💾 Saved webhook image: {filepath}")
        logger.info(f"[WEBHOOK]    Size: {w}x{h} pixels, {file_size / 1024:.2f} KB")
        
    except Exception as e:
        logger.error(f"[WEBHOOK] Error saving webhook image: {e}", exc_info=True)


# Keys that may carry the pipeline/camera name inside the payload, in priority order
_PIPELINE_NAME_KEYS = (
    "pipeline_name", "node_name", "pipeline", "camera_name",
    "stream_name", "source_name", "camera", "source", "name", "pipeline_id",
)

# Structural logging: re-log whenever the payload STRUCTURE changes (keyed by the
# key-tree), so tool-config changes surface immediately without per-frame spam
_logged_payload_structures = set()

# ---- Pipeline alias cache (old_id -> new_id after a rename) -------------------
_alias_cache: dict = {}
_alias_cache_loaded_at: float = 0.0
_ALIAS_CACHE_TTL = 30.0  # seconds


def invalidate_alias_cache():
    """Force the alias map to reload on the next webhook (called after renames)."""
    global _alias_cache_loaded_at
    _alias_cache_loaded_at = 0.0


async def _resolve_alias(pipeline_id: str) -> str:
    """Translate a renamed pipeline's old id to its new id (cached, ~30s TTL)."""
    global _alias_cache, _alias_cache_loaded_at

    now_ts = time.time()
    if now_ts - _alias_cache_loaded_at > _ALIAS_CACHE_TTL:
        try:
            from db_connection import db_manager
            from sqlalchemy import text as sa_text
            async with db_manager.get_session() as db:
                result = await db.execute(sa_text(
                    "SELECT old_pipeline_id, new_pipeline_id FROM pipeline_aliases"
                ))
                _alias_cache = {row[0]: row[1] for row in result.fetchall()}
            _alias_cache_loaded_at = now_ts
        except Exception as e:
            # Table may not exist yet (fresh install pre-migration) - fail open
            logger.debug(f"[WEBHOOK] Alias cache refresh failed (non-fatal): {e}")
            _alias_cache_loaded_at = now_ts  # don't retry every frame

    target = _alias_cache.get(pipeline_id)
    if target and target != pipeline_id:
        logger.info(f"[WEBHOOK] 🔁 Alias: '{pipeline_id}' -> '{target}'")
        return target
    return pipeline_id


# ---- Idempotency / dedup (short in-memory TTL set) ---------------------------
_dedup_seen: dict = {}  # job_key -> expiry timestamp
_DEDUP_MAX_KEYS = 5000


def _dedup_is_duplicate(key: str) -> bool:
    """Return True if this job key was seen within the dedup TTL; record it otherwise."""
    now = time.time()
    ttl = int(getattr(settings, 'WEBHOOK_DEDUP_TTL_SECONDS', 60))
    if len(_dedup_seen) > _DEDUP_MAX_KEYS:
        for k, exp in list(_dedup_seen.items()):
            if exp < now:
                _dedup_seen.pop(k, None)
    expiry = _dedup_seen.get(key)
    if expiry and expiry > now:
        return True
    _dedup_seen[key] = now + ttl
    return False


def _job_key(payload: dict, pipeline_id: str, images_b64: list):
    """Idempotency key: sender-provided id when present, else a content hash."""
    if isinstance(payload, dict):
        for k in ("request_id", "frame_id", "event_id"):
            v = payload.get(k)
            if isinstance(v, (str, int)) and str(v).strip():
                return f"{pipeline_id}:{k}:{v}"
    if images_b64 and images_b64[0]:
        h = hashlib.sha256()
        first = images_b64[0]
        h.update(first[:4096].encode("utf-8", "ignore"))
        h.update(str(len(first)).encode())
        h.update(str(len(images_b64)).encode())
        h.update(pipeline_id.encode("utf-8", "ignore"))
        return f"{pipeline_id}:sha:{h.hexdigest()[:32]}"
    return None


async def parse_webhook_request(request: Request) -> dict:
    """Shared body parsing for all webhook routes: size guard (413) + JSON parse (400)."""
    max_bytes = int(getattr(settings, 'WEBHOOK_MAX_BODY_MB', 25)) * 1024 * 1024
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Request body exceeds {max_bytes // (1024*1024)} MB limit")
    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Request body exceeds {max_bytes // (1024*1024)} MB limit")
    if not body:
        logger.warning("[WEBHOOK] Empty request body")
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    return payload if isinstance(payload, dict) else {}


def _payload_structure(payload: dict):
    """Return (top_keys, results_keys, prediction0_keys, node_id_value) for logging."""
    top = list(payload.keys()) if isinstance(payload, dict) else []
    results = payload.get("results") if isinstance(payload, dict) else None
    results_keys = list(results.keys()) if isinstance(results, dict) else type(results).__name__
    pred0_keys = None
    if isinstance(results, dict):
        preds = results.get("predictions")
        if isinstance(preds, list) and preds and isinstance(preds[0], dict):
            pred0_keys = list(preds[0].keys())
    node_id_value = payload.get("node_id") if isinstance(payload, dict) else None
    return top, results_keys, pred0_keys, node_id_value


def _extract_location_name(payload: dict):
    """Extract the DISPLAY-ONLY location name from the webhook payload.

    Supported shapes: top-level `location_name`, or `results.location_name`.
    This value is untrusted display text: trimmed, capped at 150 chars, control
    characters stripped, SPACES PRESERVED ("Main Entrance" stays readable).
    It must NEVER be used as the internal pipeline_id, in file paths, or for
    authorization - the URL path parameter remains the permanent identifier.
    """
    if not isinstance(payload, dict):
        return None

    candidates = [payload.get("location_name")]
    results = payload.get("results")
    if isinstance(results, dict):
        candidates.append(results.get("location_name"))

    for value in candidates:
        if isinstance(value, str) and value.strip():
            cleaned = re.sub(r'[\x00-\x1f\x7f]', '', value).strip()
            if cleaned:
                return cleaned[:150]
    return None


def _extract_pipeline_name(payload: dict):
    """DIAGNOSTIC ONLY: find any name-ish key in the payload for the structural log.

    NOTE: this no longer influences the pipeline identity. The internal
    pipeline_id always comes from the webhook URL path (plus alias resolution);
    display names travel separately via `location_name`.
    """
    if not isinstance(payload, dict):
        return None

    # (scope, allowed_keys): inside a prediction object, generic keys like 'name'
    # usually mean the CLASS label ("person") - only unambiguous keys are allowed there.
    prediction_safe_keys = ("pipeline_name", "node_name", "camera_name", "stream_name", "source_name")

    scopes = [(payload, _PIPELINE_NAME_KEYS)]
    results = payload.get("results")
    if isinstance(results, dict):
        scopes.append((results, _PIPELINE_NAME_KEYS))
        preds = results.get("predictions")
        if isinstance(preds, list) and preds and isinstance(preds[0], dict):
            scopes.append((preds[0], prediction_safe_keys))

    for scope, keys in scopes:
        for key in keys:
            value = scope.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


async def webhook_handler(pipeline_id: str, payload: dict, background_tasks: BackgroundTasks):
    """
    Webhook endpoint to receive images from pipelines
    Handles up to 100 concurrent requests with queuing

    OPTIMIZED: Uses face tracking to avoid duplicate processing
    """
    request_id = str(uuid.uuid4())[:8]  # short unique ID for logging

    try:
        logger.info(f"[WEBHOOK] 📥 Incoming request {request_id} for pipeline: {pipeline_id}")
        logger.debug(f"[WEBHOOK] Payload keys: {list(payload.keys()) if payload else 'None'}")

        # PIPELINE IDENTITY: the URL path parameter is the PERMANENT internal id
        # used for DB relationships, grouping, permissions and processing. The
        # payload's `location_name` is a display-only label and never mutates it.
        url_pipeline_id = pipeline_id

        # Validate pipeline ID (URL value)
        pipeline_id = validate_pipeline_id(pipeline_id)

        # Translate through rename aliases (old id -> renamed id)
        pipeline_id = await _resolve_alias(pipeline_id)

        # Display-only friendly name (may be None)
        location_name = _extract_location_name(payload)

        logger.info(
            "[WEBHOOK] request_id=%s pipeline_id=%s location_name=%r",
            request_id, pipeline_id, location_name,
        )

        # Diagnostic only - which name-ish keys the tool sends (for the 🧭 log)
        payload_name = _extract_pipeline_name(payload)

        # Structural log: shows exactly which keys arrive (all 3 levels + node_id
        # value) so payload/name issues are diagnosable from the logs alone.
        # Keyed by the structure itself, so a tool-config change re-logs immediately.
        top_keys, results_keys, pred0_keys, node_id_value = _payload_structure(payload)
        structure_sig = (pipeline_id, str(top_keys), str(results_keys), str(pred0_keys))
        if structure_sig not in _logged_payload_structures:
            _logged_payload_structures.add(structure_sig)
            logger.info(
                f"[WEBHOOK] 🧭 Payload structure for '{pipeline_id}': "
                f"top={top_keys} results={results_keys} prediction[0]={pred0_keys} "
                f"node_id={node_id_value!r} resolved_name={payload_name!r} (url was '{url_pipeline_id}')"
            )

        logger.info(f"[WEBHOOK] ✅ Validated pipeline ID: {pipeline_id}")

        # Extract images (supports multiple formats)
        images_b64 = extract_images_from_payload(payload)
        logger.info(f"[WEBHOOK] Extracted {len(images_b64)} images")

        # Extract predictions - support both global and per-image predictions
        # Global predictions (applies to all images)
        global_predictions = payload.get("predictions", [])
        
        # Handle nested results format
        if not global_predictions and "results" in payload:
            results = payload.get("results", {})
            global_predictions = results.get("predictions", [])
        
        # Per-image predictions (if payload has frames with predictions)
        per_image_predictions = {}
        if isinstance(payload.get("frames"), list):
            for frame_idx, frame in enumerate(payload["frames"]):
                if frame.get("predictions"):
                    per_image_predictions[frame_idx] = frame["predictions"]
                elif frame.get("results") and frame["results"].get("predictions"):
                    per_image_predictions[frame_idx] = frame["results"]["predictions"]
        
        logger.info(f"[WEBHOOK] Found {len(global_predictions)} global predictions, {len(per_image_predictions)} per-image predictions")

        if not images_b64:
            metrics_requests_total.labels(pipeline_id=pipeline_id, status="no_images").inc()
            return {"status": "ok", "message": "No images", "request_id": request_id}

        # Idempotency: same sender id / same content within the TTL -> acknowledge
        # without re-enqueueing (protects against sender retries under load).
        job_key = _job_key(payload, pipeline_id, images_b64)
        if job_key and _dedup_is_duplicate(job_key):
            metrics_requests_total.labels(pipeline_id=pipeline_id, status="duplicate").inc()
            logger.info(f"[WEBHOOK] 🔁 Duplicate request {request_id} (key={job_key[:64]}) - acknowledged, not re-queued")
            return JSONResponse(status_code=200, content={
                "status": "duplicate",
                "job_id": request_id,
                "pipeline_id": pipeline_id,
                "location_name": location_name,
                "queued": 0,
                "dropped": 0,
            })

        queued = 0
        dropped = 0
        queue_full = False

        # FAST PATH: no base64 decode, no image validation, no disk writes here.
        # The raw b64 string is enqueued; the worker decodes/validates in the
        # inference thread pool so this handler never blocks the event loop.
        for idx, img_b64 in enumerate(images_b64):
            if not img_b64:
                continue

            # Remove data URL prefix (cheap string split only)
            if "," in img_b64[:64]:
                img_b64 = img_b64.split(",", 1)[1]

            # Determine which predictions to use for this image
            # Priority: 1) Per-image predictions, 2) Filtered global predictions by image_index, 3) All global predictions
            image_predictions = per_image_predictions.get(idx, [])
            
            if idx in per_image_predictions:
                logger.debug(f"[WEBHOOK] Using per-image predictions for image {idx}: {len(image_predictions)} predictions")
            else:
                # Check if global predictions have image_index/frame_index fields to filter
                if global_predictions and len(global_predictions) > 0:
                    # Check if first prediction has image_index or frame_index field
                    first_pred = global_predictions[0] if isinstance(global_predictions[0], dict) else {}
                    if "image_index" in first_pred or "frame_index" in first_pred or "image_id" in first_pred:
                        # Filter predictions by image index
                        filtered_predictions = []
                        for pred in global_predictions:
                            if isinstance(pred, dict):
                                pred_image_idx = pred.get("image_index") or pred.get("frame_index") or pred.get("image_id")
                                if pred_image_idx == idx or pred_image_idx is None:
                                    filtered_predictions.append(pred)
                            else:
                                filtered_predictions.append(pred)
                        image_predictions = filtered_predictions
                        logger.info(f"[WEBHOOK] ✅ Filtered {len(image_predictions)} predictions for image {idx} (from {len(global_predictions)} total) using image_index")
                        if len(image_predictions) == 0:
                            logger.warning(f"[WEBHOOK] ⚠️ No predictions matched image_index={idx}! All predictions may be for different images.")
                    else:
                        # No image_index field - use all global predictions (assume they apply to all images)
                        image_predictions = global_predictions
                        logger.info(f"[WEBHOOK] Using all {len(image_predictions)} global predictions for image {idx} (no image_index field in predictions)")
                        if len(images_b64) > 1:
                            logger.warning(f"[WEBHOOK] ⚠️ WARNING: {len(images_b64)} images exist but predictions have no image_index field!")
                            logger.warning(f"[WEBHOOK] All images will use same predictions - may cause bbox mismatches!")
                else:
                    image_predictions = []
                    logger.warning(f"[WEBHOOK] No predictions found for image {idx}")

            # Add to queue with correct predictions for this image.
            # NOTE: raw b64 string, not decoded bytes - decoding happens in the worker.
            success = await processing_queue.add({
                "pipeline_id": pipeline_id,
                "location_name": location_name,  # display-only, carried to broadcasts
                "image_b64": img_b64,
                "predictions": image_predictions,
                "timestamp": time.time(),
                "request_id": request_id,
                "image_index": idx,  # Track which image this is
            })

            if success:
                queued += 1
            else:
                dropped += 1
                queue_full = True

        if queued == 0 and queue_full:
            metrics_requests_total.labels(pipeline_id=pipeline_id, status="queue_full").inc()
            logger.warning(f"[WEBHOOK] 🚦 Queue full - rejecting request {request_id} (pipeline {pipeline_id})")
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "2"},
                content={
                    "status": "queue_full",
                    "job_id": request_id,
                    "pipeline_id": pipeline_id,
                    "queued": 0,
                    "dropped": dropped,
                },
            )

        status = "queued" if queued > 0 else "dropped"
        metrics_requests_total.labels(pipeline_id=pipeline_id, status=status).inc()

        # 202 Accepted: work happens asynchronously; senders checking 2xx keep working.
        return JSONResponse(status_code=202, content={
            "status": status,
            "job_id": request_id,
            "request_id": request_id,  # backward compat
            "pipeline_id": pipeline_id,
            "location_name": location_name,
            "queued": queued,
            "dropped": dropped,
        })

    except HTTPException as he:
        logger.error(f"[WEBHOOK] ❌ HTTPException in request {request_id}: {he.status_code} - {he.detail}")
        raise
    except Exception as e:
        logger.error(f"[WEBHOOK] ❌ Error processing request {request_id}: {str(e)}", exc_info=True)
        error_pipeline_id = pipeline_id if 'pipeline_id' in locals() else "unknown"
        metrics_requests_total.labels(pipeline_id=error_pipeline_id, status="error").inc()
        raise HTTPException(status_code=500, detail=f"Webhook processing error: {str(e)}")


# Register webhook endpoint at both /webhook/{pipeline_id} and /api/webhook/{pipeline_id}
@router.post("/webhook/{pipeline_id}")
async def webhook(pipeline_id: str, request: Request, background_tasks: BackgroundTasks):
    """Webhook endpoint - /webhook/{pipeline_id}"""
    payload = await parse_webhook_request(request)
    return await webhook_handler(pipeline_id, payload, background_tasks)


@router.post("/api/webhook/{pipeline_id}")
async def webhook_api(pipeline_id: str, request: Request, background_tasks: BackgroundTasks):
    """Webhook endpoint - /api/webhook/{pipeline_id} (alias for compatibility)"""
    payload = await parse_webhook_request(request)
    return await webhook_handler(pipeline_id, payload, background_tasks)


# Add a simple test endpoint to verify connectivity
@router.get("/webhook/test")
@router.get("/api/webhook/test")
async def webhook_test():
    """Test endpoint to verify webhook routes are accessible"""
    logger.info("[WEBHOOK] Test endpoint accessed")
    return {
        "status": "ok",
        "message": "Webhook endpoint is accessible",
        "endpoints": [
            "/webhook/{pipeline_id}",
            "/api/webhook/{pipeline_id}"
        ]
    }


@router.get("/api/webhook/images/{pipeline_id}")
async def list_webhook_images(pipeline_id: str):
    """
    List all saved webhook images for a pipeline.
    Returns list of image files with metadata.
    """
    try:
        save_dir = Path(getattr(settings, 'WEBHOOK_IMAGES_DIR', './debug/webhook_images')) / pipeline_id
        
        if not save_dir.exists():
            return {
                "pipeline_id": pipeline_id,
                "images": [],
                "count": 0,
                "message": "No images saved yet for this pipeline. Enable SAVE_WEBHOOK_IMAGES in config."
            }
        
        # Get all image files
        image_files = []
        for img_file in sorted(save_dir.glob("*.jpg"), key=lambda x: x.stat().st_mtime, reverse=True):
            stat = img_file.stat()
            image_files.append({
                "filename": img_file.name,
                "path": str(img_file.relative_to(Path.cwd())),
                "size_bytes": stat.st_size,
                "size_kb": round(stat.st_size / 1024, 2),
                "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "url": f"/api/webhook/images/{pipeline_id}/{img_file.name}"
            })
        
        return {
            "pipeline_id": pipeline_id,
            "images": image_files,
            "count": len(image_files),
            "directory": str(save_dir)
        }
    except Exception as e:
        logger.error(f"[WEBHOOK] Error listing webhook images: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error listing images: {str(e)}")


@router.get("/api/webhook/images/{pipeline_id}/{filename}")
async def get_webhook_image(pipeline_id: str, filename: str):
    """
    Get a specific webhook image file.
    """
    from fastapi.responses import FileResponse
    
    try:
        save_dir = Path(getattr(settings, 'WEBHOOK_IMAGES_DIR', './debug/webhook_images')) / pipeline_id
        filepath = save_dir / filename
        
        # Security: Ensure file is within the save directory (prevent path traversal)
        try:
            filepath.resolve().relative_to(save_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="Invalid file path")
        
        if not filepath.exists():
            raise HTTPException(status_code=404, detail="Image not found")
        
        return FileResponse(
            path=str(filepath),
            media_type="image/jpeg",
            filename=filename
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[WEBHOOK] Error getting webhook image: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting image: {str(e)}")

