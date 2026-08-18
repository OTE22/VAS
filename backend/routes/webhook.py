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
from fastapi import (APIRouter, BackgroundTasks, Depends, HTTPException,
                     Request, Body)
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

# ---------------------------------------------------------------------------
# Cardinality guard for the pipeline_id metric label.
#
# pipeline_id is a caller-chosen URL segment (any ^[A-Za-z0-9_-]{3,100}$ —
# validate_pipeline_id checks shape, not existence), and Prometheus never
# reclaims a label value once seen. Without a cap, an authenticated sender
# cycling names would grow the registry without bound and every scrape would
# ship the whole graveyard. Real deployments have a handful of cameras; the
# first N distinct ids keep their own series, everything after collapses into
# one "other" series. The DATA path is unaffected — only the metric label.
# ---------------------------------------------------------------------------
_METRIC_PIPELINE_LABEL_CAP = 50
_metric_pipeline_labels_seen: set = set()


def _metric_pipeline_label(pipeline_id: str) -> str:
    if pipeline_id in _metric_pipeline_labels_seen:
        return pipeline_id
    if len(_metric_pipeline_labels_seen) < _METRIC_PIPELINE_LABEL_CAP:
        _metric_pipeline_labels_seen.add(pipeline_id)
        return pipeline_id
    return "other"
from backend.auth.auth_service import require_role
from backend.routes.utils import validate_pipeline_id, validate_image_content
from utils.helpers import extract_images_from_payload
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Ingest (Webhook)"])

_UNENFORCED_WARNED = False


async def require_webhook_key(request: Request) -> None:
    """Reject an ingest request that does not carry a valid key.

    Attached with Depends rather than implemented inside the handler or as
    middleware, for two concrete reasons:

      * a dependency runs BEFORE the route body, so an unauthenticated caller
        never gets 25 MB of body buffered by parse_webhook_request on its behalf;
      * route-level attachment is visible in `app.routes`, which is what lets a
        test assert that no webhook route is unguarded without grepping source.
    """
    global _UNENFORCED_WARNED
    from backend.core import metrics
    from backend.security import webhook_auth, webhook_credentials

    def _count(result: str) -> None:
        counter = getattr(metrics, "metrics_webhook_auth", None)
        if counter is not None:
            try:
                counter.labels(result=result).inc()
            except Exception:                                  # noqa: BLE001
                pass

    def _count_source(source: str) -> None:
        counter = getattr(metrics, "metrics_webhook_auth_source", None)
        if counter is not None:
            try:
                counter.labels(source=source).inc()
            except Exception:                                  # noqa: BLE001
                pass

    mode = webhook_auth.auth_mode(settings)
    if mode == webhook_auth.MODE_OFF:
        _count("unenforced")
        return

    # Load the issued-credential snapshot. After the first request of a worker's
    # life this awaits nothing that touches I/O: past the TTL it spawns a
    # refresh and serves the current snapshot. It never reads the request body,
    # so an unauthenticated oversized payload is still rejected unbuffered.
    await webhook_credentials.ensure_fresh()

    # BOTH sets must be empty. Checking only the environment keys would mean an
    # operator who mints a credential in the admin UI without also setting
    # WEBHOOK_API_KEYS gets wide-open ingest while believing it is authenticated
    # — the feature would make this posture worse than having no feature at all.
    if not webhook_auth.keys_configured(settings) and not webhook_credentials.any_cached():
        # Development convenience only: production cannot reach this state,
        # because config_guard refuses to start without a key. Warn once per
        # process rather than per request so it is visible but not a flood.
        if not _UNENFORCED_WARNED:
            _UNENFORCED_WARNED = True
            logger.warning(
                "[WEBHOOK] No WEBHOOK_API_KEYS and no issued credentials - ingest "
                "is accepting unauthenticated frames. Anyone who can reach this "
                "port can create identities and fill face storage.")
        _count("unenforced")
        return

    presented = webhook_auth.present_key(request, settings)
    label = webhook_credentials.match(presented, settings)
    if label is not None:
        _count("ok")
        _count_source("env" if label.startswith("env#") else "db")
        webhook_credentials.note_use(label)
        # One INFO line the first time a credential is seen, then DEBUG. At 50
        # cameras a per-frame INFO would be pure noise, but "which senders are
        # actually live" is exactly what tells an operator whether a rotation
        # can finish. The label is a name or a position, never the token.
        if webhook_credentials.first_use(label):
            logger.info("[WEBHOOK] ingest credential in use: %s client=%s",
                        label, _client_fingerprint(request))
        else:
            logger.debug("[WEBHOOK] ingest ok credential=%s", label)
        return

    result = "missing" if not presented else "invalid"
    if mode == webhook_auth.MODE_LOG_ONLY:
        # Migration posture: report what WOULD be rejected so an operator can
        # watch the count fall to zero before switching to enforce.
        _count("would_reject")
        logger.warning(
            "[WEBHOOK] ingest credential %s (log_only: accepted) path=%s client=%s",
            result, request.url.path, _client_fingerprint(request))
        return

    _count(result)
    logger.warning("[WEBHOOK] ingest credential %s - rejected path=%s client=%s",
                   result, request.url.path, _client_fingerprint(request))
    raise HTTPException(
        status_code=401,
        detail={"error": {"code": "WEBHOOK_AUTH_REQUIRED",
                          "message": "A valid ingest key is required."}},
        # Both accepted transports, standard scheme first: an external sender
        # reading the challenge should find one it can actually produce.
        # "WebhookKey" alone named nothing any HTTP client implements.
        headers={"WWW-Authenticate": "Bearer, WebhookKey"})


def _client_fingerprint(request: Request) -> str:
    """A correlatable, non-identifying stand-in for the caller's address.

    Matches the [AUTH_AUDIT] discipline: a rejected camera has to be findable in
    the logs without writing raw client IPs into them.
    """
    try:
        from backend.auth.auth_security import pseudonymize
        host = getattr(getattr(request, "client", None), "host", "") or "unknown"
        return pseudonymize(host)
    except Exception:                                          # noqa: BLE001
        return "unknown"


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
        save_dir = Path(settings.WEBHOOK_IMAGES_DIR)
        save_dir = save_dir / pipeline_id
        save_dir.mkdir(parents=True, exist_ok=True)
        
        # Create filename with timestamp and request ID
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')[:-3]  # UTC; filename only
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
    ttl = int(settings.WEBHOOK_DEDUP_TTL_SECONDS)
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
    max_bytes = int(settings.WEBHOOK_MAX_BODY_MB) * 1024 * 1024
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

        # Idempotency FIRST - before the no-images early return, so image-free
        # events carrying a sender id (event_id/request_id/frame_id) are
        # deduplicated too. VMS retries an image-free detection event with the
        # same event_id; without this ordering those retries were re-processed.
        # This is idempotent deduplication within WEBHOOK_DEDUP_TTL_SECONDS,
        # not exactly-once processing (and _dedup_seen is per-process; prod
        # runs WORKERS=1 so per-process == global there). A keyless image-free
        # payload still gets job_key=None -> no dedup, exactly as before.
        job_key = _job_key(payload, pipeline_id, images_b64)
        if job_key and _dedup_is_duplicate(job_key):
            metrics_requests_total.labels(pipeline_id=_metric_pipeline_label(pipeline_id), status="duplicate").inc()
            logger.info(f"[WEBHOOK] 🔁 Duplicate request {request_id} (key={job_key[:64]}) - acknowledged, not re-queued")
            return JSONResponse(status_code=200, content={
                "status": "duplicate",
                "job_id": request_id,
                "pipeline_id": pipeline_id,
                "location_name": location_name,
                "queued": 0,
                "dropped": 0,
            })

        if not images_b64:
            metrics_requests_total.labels(pipeline_id=_metric_pipeline_label(pipeline_id), status="no_images").inc()
            return {"status": "ok", "message": "No images", "request_id": request_id}

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
            metrics_requests_total.labels(pipeline_id=_metric_pipeline_label(pipeline_id), status="queue_full").inc()
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
        metrics_requests_total.labels(pipeline_id=_metric_pipeline_label(pipeline_id), status=status).inc()

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
        metrics_requests_total.labels(pipeline_id=_metric_pipeline_label(error_pipeline_id), status="error").inc()
        raise HTTPException(status_code=500, detail=f"Webhook processing error: {str(e)}")


# Register webhook endpoint at both /webhook/{pipeline_id} and /api/webhook/{pipeline_id}
@router.post("/webhook/{pipeline_id}",
             dependencies=[Depends(require_webhook_key)])
async def webhook(pipeline_id: str, request: Request, background_tasks: BackgroundTasks):
    """Webhook endpoint - /webhook/{pipeline_id}"""
    payload = await parse_webhook_request(request)
    return await webhook_handler(pipeline_id, payload, background_tasks)


@router.post("/api/webhook/{pipeline_id}",
             dependencies=[Depends(require_webhook_key)])
async def webhook_api(pipeline_id: str, request: Request, background_tasks: BackgroundTasks):
    """Webhook endpoint - /api/webhook/{pipeline_id} (alias for compatibility)"""
    payload = await parse_webhook_request(request)
    return await webhook_handler(pipeline_id, payload, background_tasks)


# Connectivity probe. Key-gated on purpose: as a credential self-check for a
# camera installer ("200 means your key works, 401 means fix it") this is
# strictly more useful than an anonymous liveness ping, and it removes an
# unauthenticated endpoint from the surface.
@router.get("/webhook/test", dependencies=[Depends(require_webhook_key)])
@router.get("/api/webhook/test", dependencies=[Depends(require_webhook_key)])
async def webhook_test():
    """Verify that ingest routes are reachable AND that your key is accepted."""
    logger.info("[WEBHOOK] Test endpoint accessed")
    return {
        "status": "ok",
        "message": "Webhook endpoint is accessible and the ingest key was accepted",
        "endpoints": [
            "/webhook/{pipeline_id}",
            "/api/webhook/{pipeline_id}"
        ]
    }




def _debug_images_base() -> Path:
    """The one directory debug snapshots may ever be read from."""
    return Path(settings.WEBHOOK_IMAGES_DIR).resolve()


def _debug_images_dir(pipeline_id: str) -> Path:
    """Resolve a pipeline's snapshot directory, anchored to a FIXED base.

    The previous version built `save_dir = WEBHOOK_IMAGES_DIR / pipeline_id`
    from the raw path parameter and then checked containment with
    `filepath.resolve().relative_to(save_dir.resolve())` — against the
    ALREADY-TRAVERSED directory. So `pipeline_id="../../storage/faces"` resolved
    save_dir to the face gallery and every file inside it passed the check:
    unauthenticated arbitrary .jpg read over the whole tree.

    Two changes make that impossible: pipeline_id is validated against the same
    strict pattern the ingest path uses BEFORE it becomes a path component, and
    containment is asserted against the fixed base rather than against a
    directory the caller influenced.
    """
    validate_pipeline_id(pipeline_id)
    base = _debug_images_base()
    candidate = (base / pipeline_id).resolve()
    if candidate != base and base not in candidate.parents:
        raise HTTPException(status_code=400, detail="Invalid pipeline id")
    return candidate


@router.get("/api/webhook/images/{pipeline_id}")
async def list_webhook_images(pipeline_id: str,
                              current_user=Depends(require_role(["admin"]))):
    """
    List all saved webhook images for a pipeline.
    Returns list of image files with metadata.
    """
    if not settings.SAVE_WEBHOOK_IMAGES:
        # 404, not an explanatory 200: a disabled debug facility should not
        # advertise its own existence or its configuration flag.
        raise HTTPException(status_code=404, detail="Not found")

    save_dir = _debug_images_dir(pipeline_id)
    try:
        if not save_dir.is_dir():
            return {"pipeline_id": pipeline_id, "images": [], "count": 0}

        base = _debug_images_base()
        image_files = []
        for img_file in sorted(save_dir.glob("*.jpg"),
                               key=lambda x: x.stat().st_mtime, reverse=True):
            stat = img_file.stat()
            image_files.append({
                "filename": img_file.name,
                # Relative to the debug base, not to CWD: relative_to(Path.cwd())
                # raised ValueError -> 500 whenever WEBHOOK_IMAGES_DIR sat
                # outside the working directory, which is the default layout.
                "path": str(img_file.resolve().relative_to(base)),
                "size_bytes": stat.st_size,
                "size_kb": round(stat.st_size / 1024, 2),
                "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "url": f"/api/webhook/images/{pipeline_id}/{img_file.name}"
            })

        return {
            "pipeline_id": pipeline_id,
            "images": image_files,
            "count": len(image_files),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[WEBHOOK] Error listing webhook images: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error listing images")


@router.get("/api/webhook/images/{pipeline_id}/{filename}")
async def get_webhook_image(pipeline_id: str, filename: str,
                            current_user=Depends(require_role(["admin"]))):
    """Serve one debug snapshot, anchored inside the configured base."""
    from fastapi.responses import FileResponse

    if not settings.SAVE_WEBHOOK_IMAGES:
        raise HTTPException(status_code=404, detail="Not found")

    save_dir = _debug_images_dir(pipeline_id)

    # The filename is a NAME, never a path. Rejecting separators outright is
    # clearer than normalizing them away, and `Path(filename).name` alone would
    # silently accept "../x.jpg" by quietly reinterpreting it.
    if filename != os.path.basename(filename) or filename in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="Invalid file name")
    if not filename.lower().endswith(".jpg"):
        raise HTTPException(status_code=404, detail="Image not found")

    base = _debug_images_base()
    filepath = (save_dir / filename).resolve()
    if base not in filepath.parents:
        # Anchored to the FIXED base, not to save_dir.
        raise HTTPException(status_code=400, detail="Invalid file path")

    if not filepath.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(path=str(filepath), media_type="image/jpeg",
                        filename=filename)

