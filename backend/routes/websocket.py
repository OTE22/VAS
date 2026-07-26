"""
WebSocket Routes
================
WebSocket endpoint for real-time updates.
"""

import os
import sys
import base64
import logging
import asyncio
from datetime import datetime, timedelta
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from db_connection import db_manager
from db_models import Detection, Face, User, UserPipelineAccess
from sqlalchemy import select
from backend.config import FACE_TRACKING_ENABLED
from backend.core import processing_queue, face_tracker, retention_manager, ws_manager
from backend.auth.auth_service import AuthService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time dashboard updates"""
    logger.info(f"[WS] 🔌 New WebSocket connection attempt from {websocket.client.host if websocket.client else 'unknown'}")

    # Which page is this client? 'dashboard' (default) or 'unknown'.
    # Unknown-face payloads (full base64 images) are only sent to 'unknown' clients -
    # sending them to every dashboard doubled bandwidth for data the page discards.
    client_view = websocket.query_params.get("view", "dashboard")
    websocket.scope["client_view"] = client_view
    logger.info(f"[WS] Client view: {client_view}")
    # SECURITY: never log handshake headers or cookies — they contain the
    # auth token. Log only non-sensitive facts (view, origin host, username).

    # Origin validation: browsers always send Origin on WebSocket handshakes.
    # A cross-site page cannot forge it — reject anything not matching Host.
    origin = websocket.headers.get("origin")
    if origin:
        try:
            from backend.security.origins import origin_host as _origin_host
            from backend.security.origins import approved_origin_hosts

            origin_host = _origin_host(origin)
            allowed_hosts = approved_origin_hosts(
                request_host=websocket.headers.get("host") or ""
            )
            if origin_host not in allowed_hosts:
                logger.warning(f"[WS] ❌ Rejected cross-origin WebSocket from origin host: {origin_host}")
                await websocket.close(code=1008, reason="Origin not allowed")
                return
        except Exception:
            await websocket.close(code=1008, reason="Origin not allowed")
            return

    # Get token from cookies (HttpOnly cookie set during login)
    # WebSocket handshake includes cookies in the Cookie header
    token = None
    current_user = None
    user_pipelines = None

    # Parse cookies from WebSocket handshake headers
    # Cookies are sent in the "Cookie" header as "key=value; key2=value2"
    cookie_header = websocket.headers.get("cookie", "")
    if cookie_header:
        # Parse cookie string
        cookies = {}
        for cookie in cookie_header.split(";"):
            cookie = cookie.strip()
            if "=" in cookie:
                key, value = cookie.split("=", 1)
                cookies[key.strip()] = value.strip()
        
        token = cookies.get("__Host-access_token") or cookies.get("access_token")
        if token:
            logger.debug(f"[WS] Token found in Cookie header (length: {len(token)})")
    
    # Fallback: Try query params (for backward compatibility or if cookies don't work)
    if not token:
        token = websocket.query_params.get("token")
        if token:
            logger.debug(f"[WS] Token found in query params (length: {len(token)})")
    
    if token:
        try:
            # Verify token and get user
            payload = AuthService.decode_token(token)
            if payload:
                # JWT 'sub' claim is a string, convert to int for user lookup
                user_id_str = payload.get("sub")
                if user_id_str:
                    try:
                        user_id = int(user_id_str)
                        async with db_manager.get_session() as auth_db:
                            result = await auth_db.execute(
                                select(User).where(User.id == user_id)
                            )
                            current_user = result.scalar_one_or_none()
                            
                            if current_user:
                                # Check if user is active
                                if not current_user.is_active:
                                    logger.warning(f"[WS] User {current_user.username} is inactive, rejecting connection")
                                    try:
                                        await websocket.close(code=1008, reason="User account is inactive")
                                    except (OSError, RuntimeError, ConnectionError) as close_error:
                                        # Handle "Bad file descriptor" gracefully
                                        error_msg = str(close_error).lower()
                                        if "bad file descriptor" not in error_msg and "errno 9" not in error_msg:
                                            logger.debug(f"[WS] Error closing websocket for inactive user: {close_error}")
                                    except Exception:
                                        pass
                                    return
                                
                                # Get user's accessible pipelines (for ALL users, including admins)
                                # This respects pipeline permissions even for admin users
                                user_pipelines_list = await AuthService.get_user_pipelines(current_user.id, auth_db)
                                
                                # Determine pipeline access:
                                # - Admin with NO pipeline restrictions → user_pipelines = None (see all)
                                # - Admin WITH pipeline restrictions → user_pipelines = [list] (filtered)
                                # - Regular user with pipelines → user_pipelines = [list] (filtered)
                                # - Regular user without pipelines → user_pipelines = [] (no access)
                                if not user_pipelines_list and current_user.role == "admin":
                                    # Admin with no pipeline restrictions - see all pipelines
                                    user_pipelines = None
                                    logger.info(f"[WS] ✅ Admin user {current_user.username} (ID: {current_user.id}) connected with FULL access (no pipeline restrictions)")
                                else:
                                    # User (admin or regular) with specific pipeline access
                                    user_pipelines = user_pipelines_list
                                    if current_user.role == "admin":
                                        logger.info(f"[WS] ✅ Admin user {current_user.username} (ID: {current_user.id}) connected with RESTRICTED access to {len(user_pipelines)} pipelines: {user_pipelines}")
                                    else:
                                        logger.info(f"[WS] ✅ Regular user {current_user.username} (ID: {current_user.id}) connected with access to {len(user_pipelines)} pipelines: {user_pipelines}")
                            else:
                                logger.warning(f"[WS] User not found for ID: {user_id}")
                    except (ValueError, TypeError) as e:
                        logger.warning(f"[WS] Invalid user ID in token: {user_id_str}, error: {e}")
                        current_user = None
                else:
                    logger.warning(f"[WS] Token missing 'sub' claim")
                    current_user = None
            else:
                logger.warning(f"[WS] Token decode failed - invalid or expired token")
        except Exception as e:
            logger.warning(f"[WS] Token verification failed: {e}")
    else:
        logger.warning(f"[WS] No authentication token found (neither in cookies nor query params)")

    # SECURITY: the WebSocket is authenticated server-side. An anonymous
    # connection previously saw ALL pipelines — reject instead (close 1008
    # so the client can show "Authentication failed" and not retry forever).
    if not current_user:
        logger.warning("[WS] ❌ Rejecting unauthenticated WebSocket connection")
        try:
            await websocket.close(code=1008, reason="Authentication required")
        except Exception:
            pass
        return

    logger.info(f"[WS] 🔗 Attempting to accept WebSocket connection...")
    try:
        await ws_manager.connect(websocket, user_pipelines)
        logger.info(f"[WS] ✅ WebSocket connection accepted and registered - User: {current_user.username if current_user else 'anonymous'}, Pipelines: {user_pipelines if user_pipelines is not None else 'all (admin)'}")
        logger.info(f"[WS] 📊 Total active connections after this: {len(ws_manager.active_connections)}")
    except Exception as connect_error:
        logger.error(f"[WS] ❌ Failed to accept WebSocket connection: {connect_error}", exc_info=True)
        try:
            await websocket.close(code=1011, reason="Internal server error during connection")
        except (OSError, RuntimeError, ConnectionError) as close_error:
            # Handle "Bad file descriptor" and other socket errors gracefully
            error_msg = str(close_error).lower()
            if "bad file descriptor" in error_msg or "errno 9" in error_msg:
                logger.debug(f"[WS] Socket already closed (Bad file descriptor) - this is normal")
            else:
                logger.debug(f"[WS] Error closing websocket after connection failure: {close_error}")
        except Exception:
            # Ignore any other errors during cleanup
            pass
        return

    try:
        # Send initial data from database - get all detections from configured duration
        # CRITICAL: Always send initial_data, even if there's an error (send empty data)
        try:
            # REDIS CACHING: Check cache first for faster page loads
            from backend.core.redis_cache import redis_cache_service
            user_id = current_user.id if current_user else None
            display_hours = getattr(settings, 'DASHBOARD_FACE_DISPLAY_HOURS', 3)
            
            # Generate cache key
            cache_key_known = await redis_cache_service.get_dashboard_cache_key(
                user_id=user_id,
                pipeline_ids=user_pipelines,
                display_hours=display_hours
            )
            
            # Try to get from cache
            cached_data = await redis_cache_service.get(cache_key_known)
            if cached_data:
                logger.info(f"[WS] [CACHE] ✅ Cache HIT - returning cached initial data (key: {cache_key_known})")
                logger.info(f"[WS] [CACHE] Cached data: {len(cached_data.get('initial_data', []))} KNOWN pipelines, "
                          f"{len(cached_data.get('initial_unknown_data', []))} UNKNOWN pipelines")
                
                # Send cached data (unknown payload only to unknown-page clients)
                await websocket.send_json(cached_data.get('initial_data_message', {}))
                if client_view == "unknown" and cached_data.get('initial_unknown_data_message'):
                    await websocket.send_json(cached_data['initial_unknown_data_message'])
                
                # Continue to keep-alive loop
                while True:
                    try:
                        data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                        if data == "ping":
                            await websocket.send_json({"type": "pong", "timestamp": datetime.utcnow().isoformat()})
                    except asyncio.TimeoutError:
                        try:
                            await websocket.send_json({"type": "ping", "timestamp": datetime.utcnow().isoformat()})
                        except:
                            break
                    except WebSocketDisconnect:
                        break
                return
            
            logger.info(f"[WS] [CACHE] ❌ Cache MISS - querying database (key: {cache_key_known})")
            
            async with db_manager.get_session() as db:
                # Get all detections from configured hours (matching frontend cache duration)
                # For KNOWN faces, we should respect the retention period
                # For UNKNOWN faces, use the shorter display period
                cutoff_time = datetime.utcnow() - timedelta(hours=display_hours)
                
                logger.info(f"[WS] [STEP-BY-STEP] Loading initial dashboard data from database...")
                logger.info(f"[WS] [STEP-BY-STEP] Display hours: {display_hours}")
                logger.info(f"[WS] [STEP-BY-STEP] Cutoff time: {cutoff_time} (detections after this will be shown)")
                
                query = select(Detection).where(Detection.timestamp >= cutoff_time)
                
                # Filter by user's pipeline access if user is authenticated
                if current_user and user_pipelines is not None:
                    if user_pipelines:  # Only filter if user has assigned pipelines
                        query = query.where(Detection.pipeline_id.in_(user_pipelines))
                        logger.info(f"[WS] Filtering detections by user pipelines: {user_pipelines}")
                    else:
                        # User has no pipeline access - send empty data with filtered stats
                        logger.info(f"[WS] User {current_user.username} has no pipeline access")
                        queue_stats = await processing_queue.get_stats()
                        await websocket.send_json({
                            "type": "initial_data",
                            "data": [],
                            "stats": {
                                **queue_stats,
                                "pipelines": {"active": 0, "total_detections": 0},
                                "faces": {"total": 0}
                            },
                            "tracker_stats": {"enabled": FACE_TRACKING_ENABLED},
                            "storage_stats": {"total_size_mb": 0, "file_count": 0},
                            "timestamp": datetime.utcnow().isoformat()
                        })
                        # Continue to keep-alive loop
                        while True:
                            try:
                                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                                if data == "ping":
                                    await websocket.send_json({"type": "pong", "timestamp": datetime.utcnow().isoformat()})
                            except asyncio.TimeoutError:
                                try:
                                    await websocket.send_json({"type": "ping", "timestamp": datetime.utcnow().isoformat()})
                                except:
                                    break
                            except WebSocketDisconnect:
                                break
                        return
                
                # Bound the initial load: newest-first, hard cap. The dashboard only
                # shows the most recent face per (pipeline, person), so loading every
                # detection in the window (unbounded) just multiplied DB work and
                # base64 payload size as traffic grows.
                INITIAL_LOAD_MAX_DETECTIONS = 500
                result = await db.execute(
                    query.order_by(Detection.timestamp.desc()).limit(INITIAL_LOAD_MAX_DETECTIONS)
                )
                detections = result.scalars().all()

                logger.info(f"[WS] [STEP-BY-STEP] Found {len(detections)} detections from last {display_hours} hours (cap: {INITIAL_LOAD_MAX_DETECTIONS})")
                
                # OPTIMIZATION: Load KNOWN identities for query optimization
                # This helps optimize the database query to include KNOWN faces that may be slightly older
                # but still within retention period. UNKNOWN faces are loaded separately and routed to unknown page.
                from db_models import Identity, LabelState, IdentityType, IdentityStatus
                from sqlalchemy import and_
                
                logger.info(f"[WS] [STEP-BY-STEP] 🔄 Loading KNOWN identities for query optimization")
                
                # Get all KNOWN identities that have been seen within retention period
                identities_cutoff = datetime.utcnow() - timedelta(hours=display_hours)
                known_identities_query = select(Identity).where(
                    Identity.type == IdentityType.KNOWN,
                    Identity.status == IdentityStatus.ACTIVE,
                    Identity.last_seen_at >= identities_cutoff
                )
                known_identities_result = await db.execute(known_identities_query)
                known_identities = known_identities_result.scalars().all()
                
                logger.info(f"[WS] [STEP-BY-STEP] Found {len(known_identities)} KNOWN identities seen within retention period ({display_hours}h)")
                
                # Get all detections for KNOWN identities (even if older than display_hours)
                # This ensures KNOWN faces stay visible for the full retention period
                all_identities_to_load = list(known_identities)
                
                if all_identities_to_load:
                    identity_ids = [identity.id for identity in all_identities_to_load]
                    
                    # Get the most recent detection_id for each identity
                    # We want one detection per identity (the most recent one)
                    from sqlalchemy import func
                    
                    # Subquery: Get the most recent detection for each identity
                    subquery = select(
                        Face.identity_id,
                        func.max(Detection.timestamp).label('max_timestamp')
                    ).join(
                        Detection, Face.detection_id == Detection.id
                    ).where(
                        Face.identity_id.in_(identity_ids)
                    ).group_by(Face.identity_id).subquery()
                    
                    # Get the actual detection_id for each identity's most recent detection
                    faces_query = select(
                        Face.detection_id
                    ).join(
                        Detection, Face.detection_id == Detection.id
                    ).join(
                        subquery,
                        and_(
                            Face.identity_id == subquery.c.identity_id,
                            Detection.timestamp == subquery.c.max_timestamp
                        )
                    ).distinct()
                    
                    faces_result = await db.execute(faces_query)
                    identity_detection_ids = {row.detection_id for row in faces_result}
                    
                    # Load these detections if not already in the list
                    if identity_detection_ids:
                        # Get detection IDs we already have
                        existing_detection_ids = {d.id for d in detections}
                        missing_detection_ids = identity_detection_ids - existing_detection_ids
                        
                        if missing_detection_ids:
                            additional_detections_query = select(Detection).where(
                                Detection.id.in_(missing_detection_ids)
                            )
                            
                            # Apply user pipeline filter to additional detections too
                            if current_user and user_pipelines is not None and user_pipelines:
                                additional_detections_query = additional_detections_query.where(
                                    Detection.pipeline_id.in_(user_pipelines)
                                )
                            
                            additional_result = await db.execute(additional_detections_query)
                            additional_detections = list(additional_result.scalars().all())
                            
                            if additional_detections:
                                logger.info(f"[WS] [STEP-BY-STEP] Adding {len(additional_detections)} older detections for KNOWN faces within retention period (KNOWN: {len(known_identities)})")
                                detections = list(detections) + list(additional_detections)
                                # Re-sort by timestamp descending
                                detections.sort(key=lambda d: d.timestamp, reverse=True)
                                logger.info(f"[WS] [STEP-BY-STEP] Total detections after adding faces within retention period: {len(detections)}")

                logger.info(f"[WS] [STEP-BY-STEP] Total detections to process: {len(detections)} (including known faces within retention period)")

                # Build detection data with FULL face information (not just preview)
                # Optimize: Group by pipeline_id and name to send unique images only
                # Key: (pipeline_id, name) -> (most_recent_detection, face_data_with_image)
                # OPTIMIZED: Separate KNOWN and UNKNOWN faces - route them properly instead of filtering out
                unique_known_faces_map = {}  # (pipeline_id, name) -> {detection, face_data} for KNOWN faces
                unique_unknown_faces_map = {}  # (pipeline_id, identity_id) -> {detection, face_data} for UNKNOWN faces
                
                # Helper function to load face image — BLOCKING file I/O, so callers
                # run it via run_in_executor to keep the event loop free
                def load_face_image(face, detection, display_name):
                    """Load face image from face_image_path or storage directory"""
                    face_image_b64 = None
                    
                    # Try to load image from face_image_path first
                    if face.face_image_path and os.path.exists(face.face_image_path):
                        try:
                            with open(face.face_image_path, 'rb') as f:
                                face_image_bytes = f.read()
                                face_image_b64 = base64.b64encode(face_image_bytes).decode()
                        except Exception as e:
                            logger.warning(f"[WS] Failed to load face image {face.face_image_path}: {e}")
                    
                    # If no image loaded, try to find in storage/pipeline_id/person_name/
                    if not face_image_b64:
                        safe_name = "".join(c for c in display_name if c.isalnum() or c in ('-', '_')).lower()
                        if display_name.lower() == "unknown":
                            safe_name = "unknown"
                        
                        person_dir = os.path.join(settings.STORAGE_DIR, detection.pipeline_id, safe_name)
                        if os.path.exists(person_dir):
                            # Get most recent image from person's folder
                            image_files = [
                                f for f in os.listdir(person_dir)
                                if f.lower().endswith(('.jpg', '.jpeg', '.png'))
                            ]
                            if image_files:
                                image_files.sort(reverse=True)  # Most recent first
                                image_path = os.path.join(person_dir, image_files[0])
                                try:
                                    with open(image_path, 'rb') as f:
                                        face_image_bytes = f.read()
                                        face_image_b64 = base64.b64encode(face_image_bytes).decode()
                                    logger.debug(f"[WS] Loaded image from storage: {image_path}")
                                except Exception as e:
                                    logger.warning(f"[WS] Failed to load image from storage {image_path}: {e}")
                    
                    return face_image_b64
                
                # BATCH PREFETCH (kills the N+1): ONE query loads all faces for all
                # detections with their Identity LEFT JOINed, instead of a faces
                # query per detection plus an identity query per face.
                detection_ids = [d.id for d in detections]
                faces_by_detection = {}
                if detection_ids:
                    faces_result = await db.execute(
                        select(
                            Face.id,
                            Face.detection_id,
                            Face.name,
                            Face.similarity,
                            Face.identity_id,
                            Face.label_state,
                            Face.face_image_path,
                            Face.bbox_x1,
                            Face.bbox_y1,
                            Face.bbox_x2,
                            Face.bbox_y2,
                            Identity,
                        ).outerjoin(Identity, Face.identity_id == Identity.id)
                        .where(Face.detection_id.in_(detection_ids))
                    )
                    for row in faces_result.all():
                        face = Face()
                        face.id = row.id
                        face.detection_id = row.detection_id
                        face.name = row.name
                        face.similarity = row.similarity
                        face.identity_id = row.identity_id
                        face.label_state = row.label_state
                        face.face_image_path = row.face_image_path
                        face.bbox_x1 = row.bbox_x1
                        face.bbox_y1 = row.bbox_y1
                        face.bbox_x2 = row.bbox_x2
                        face.bbox_y2 = row.bbox_y2
                        faces_by_detection.setdefault(face.detection_id, []).append((face, row.Identity))

                _loop = asyncio.get_running_loop()

                for detection in detections:
                    all_faces = faces_by_detection.get(detection.id, [])
                    logger.debug(f"[WS] [STEP-BY-STEP] Routing faces for detection {detection.id}: total_faces={len(all_faces)}")

                    for face, identity in all_faces:
                        is_known = False

                        # Check 1: label_state (most reliable indicator)
                        if face.label_state == LabelState.AUTO_KNOWN:
                            is_known = True

                        # Check 2: identity.type (prefetched) - PRIMARY CHECK
                        if identity is not None and identity.type == IdentityType.KNOWN:
                            is_known = True

                        # Check 3: Explicit UNKNOWN checks
                        if face.label_state == LabelState.AUTO_UNKNOWN:
                            is_known = False

                        if face.name and face.name.lower() == "unknown":
                            is_known = False

                        identity_for_face = identity

                        # Route KNOWN faces to dashboard
                        if is_known:
                            display_name = identity_for_face.display_name if identity_for_face and identity_for_face.display_name else face.name
                            logger.info(f"[WS] [STEP-BY-STEP] ✅ ROUTING KNOWN face '{display_name}' to dashboard")
                            
                            # Process KNOWN faces for unique_known_faces_map
                            key = (detection.pipeline_id, display_name)
                            
                            # Only process if we haven't seen this person in this pipeline yet
                            # or if this detection is more recent
                            if key not in unique_known_faces_map or detection.timestamp > unique_known_faces_map[key]["detection"].timestamp:
                                face_image_b64 = await _loop.run_in_executor(None, load_face_image, face, detection, display_name)
                                
                                unique_known_faces_map[key] = {
                                    "detection": detection,
                                    "face_data": {
                                        "name": display_name,
                                        "similarity": float(face.similarity),
                                        "image": face_image_b64,
                                        "bbox": [float(face.bbox_x1), float(face.bbox_y1), float(face.bbox_x2), float(face.bbox_y2)],
                                        "last_seen_at": identity_for_face.last_seen_at.isoformat() if identity_for_face and identity_for_face.last_seen_at else None,
                                    }
                                }
                        else:
                            # Route UNKNOWN faces to unknown page
                            # Use identity_id as key for UNKNOWN faces (they may not have display_name)
                            identity_id_str = str(face.identity_id) if face.identity_id else f"temp_{face.id}"
                            display_name = identity_for_face.display_name if identity_for_face and identity_for_face.display_name else (face.name or "Unknown")
                            logger.info(f"[WS] [STEP-BY-STEP] 🔄 ROUTING UNKNOWN face '{display_name}' (ID: {identity_id_str}) to unknown page")
                            
                            # Process UNKNOWN faces for unique_unknown_faces_map
                            # Use (pipeline_id, identity_id) as key to group by identity
                            key = (detection.pipeline_id, identity_id_str)
                            
                            # Only process if we haven't seen this identity in this pipeline yet
                            # or if this detection is more recent
                            if key not in unique_unknown_faces_map or detection.timestamp > unique_unknown_faces_map[key]["detection"].timestamp:
                                face_image_b64 = await _loop.run_in_executor(None, load_face_image, face, detection, display_name)
                                
                                unique_unknown_faces_map[key] = {
                                    "detection": detection,
                                    "face_data": {
                                        "name": display_name,
                                        "similarity": float(face.similarity) if face.similarity else None,
                                        "image": face_image_b64,
                                        "bbox": [float(face.bbox_x1), float(face.bbox_y1), float(face.bbox_x2), float(face.bbox_y2)],
                                        "face_image_path": face.face_image_path,
                                        "identity_id": identity_id_str,
                                        "label_state": face.label_state.value if face.label_state else None,
                                    }
                                }

                # Group KNOWN faces by pipeline_id for dashboard
                initial_data = []
                known_pipeline_groups = {}  # pipeline_id -> list of face_data
                
                for (pipeline_id, name), data in unique_known_faces_map.items():
                    if pipeline_id not in known_pipeline_groups:
                        known_pipeline_groups[pipeline_id] = []
                    known_pipeline_groups[pipeline_id].append(data)
                
                # Create detection entries grouped by pipeline for KNOWN faces
                for pipeline_id, face_list in known_pipeline_groups.items():
                    # Use the most recent detection timestamp for this pipeline
                    most_recent = max(face_list, key=lambda x: x["detection"].timestamp)
                    
                    initial_data.append({
                        "pipeline_id": pipeline_id,
                        "timestamp": most_recent["detection"].timestamp.isoformat(),
                        "processing_time_ms": most_recent["detection"].processing_time_ms,
                        "faces": [f["face_data"] for f in face_list],  # Unique faces with unique images
                    })
                
                logger.info(f"[WS] Prepared {len(initial_data)} KNOWN pipeline entries with {sum(len(d['faces']) for d in initial_data)} KNOWN faces for dashboard")
                
                # Group UNKNOWN faces by pipeline_id for unknown page
                initial_unknown_data = []
                unknown_pipeline_groups = {}  # pipeline_id -> list of face_data
                
                for (pipeline_id, identity_id), data in unique_unknown_faces_map.items():
                    if pipeline_id not in unknown_pipeline_groups:
                        unknown_pipeline_groups[pipeline_id] = []
                    unknown_pipeline_groups[pipeline_id].append(data)
                
                # Create detection entries grouped by pipeline for UNKNOWN faces
                for pipeline_id, face_list in unknown_pipeline_groups.items():
                    # Use the most recent detection timestamp for this pipeline
                    most_recent = max(face_list, key=lambda x: x["detection"].timestamp)
                    
                    initial_unknown_data.append({
                        "pipeline_id": pipeline_id,
                        "timestamp": most_recent["detection"].timestamp.isoformat(),
                        "processing_time_ms": most_recent["detection"].processing_time_ms,
                        "faces": [f["face_data"] for f in face_list],  # Unique faces with unique images
                    })
                
                logger.info(f"[WS] Prepared {len(initial_unknown_data)} UNKNOWN pipeline entries with {sum(len(d['faces']) for d in initial_unknown_data)} UNKNOWN faces for unknown page")

                # Get system stats - filtered by user's pipeline access
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
                
                # Calculate stats for dashboard (KNOWN faces only)
                total_known_faces_on_dashboard = sum(len(d.get('faces', [])) for d in initial_data)
                
                filtered_stats = {
                    "pipelines": {
                        "active": len(initial_data),  # Count of unique pipelines with KNOWN faces
                        "total_detections": total_known_faces_on_dashboard,  # Count of KNOWN faces on dashboard
                    },
                    "faces": {
                        "total": total_known_faces_on_dashboard  # KNOWN faces routed to dashboard
                    }
                }
                
                logger.info(f"[WS] [STATS] Dashboard stats: {total_known_faces_on_dashboard} KNOWN faces routed to dashboard")

                # Lightweight per-pipeline unknown-face counts (numbers only, no images)
                # so the dashboard can show an "N unknown" badge linking to the unknown page.
                unknown_counts = {
                    pid: len(face_list)
                    for pid, face_list in unknown_pipeline_groups.items()
                }

                # Friendly display names (pipelines.location_name) so card titles are
                # correct immediately on load/refresh. Respects the user's pipeline
                # access filter (admins see all).
                pipeline_display_names = {}
                try:
                    from db_models import Pipeline as _Pipeline
                    name_query = select(_Pipeline.pipeline_id, _Pipeline.location_name).where(
                        _Pipeline.location_name.isnot(None)
                    )
                    if current_user and user_pipelines is not None:
                        name_query = name_query.where(_Pipeline.pipeline_id.in_(user_pipelines))
                    name_rows = await db.execute(name_query)
                    pipeline_display_names = {
                        row[0]: row[1] for row in name_rows.fetchall() if row[1] and row[1].strip()
                    }
                except Exception as name_err:
                    logger.debug(f"[WS] Display-name lookup failed (non-fatal): {name_err}")

                initial_data_message = {
                    "type": "initial_data",
                    "data": initial_data,  # Send as array directly (frontend expects array)
                    "unknown_counts": unknown_counts,
                    "pipeline_display_names": pipeline_display_names,
                    "stats": {
                        **queue_stats,
                        **filtered_stats  # Include filtered pipeline/face stats
                    },
                    "tracker_stats": tracker_stats,
                    "storage_stats": storage_stats,
                    "timestamp": datetime.utcnow().isoformat()
                }
                
                # REDIS CACHING: Prepare cache data (will be stored after sending)
                # Use different TTLs: dashboard (1 hour) and unknown (30 hours)
                dashboard_cache_ttl = getattr(settings, 'CACHE_TTL', 3600)  # Default 1 hour for dashboard
                unknown_cache_ttl = getattr(settings, 'CACHE_TTL_UNKNOWN', 108000)  # Default 30 hours for unknown faces
                cache_data = {
                    "initial_data": initial_data,
                    "initial_unknown_data": initial_unknown_data,
                    "initial_data_message": initial_data_message,
                    "cached_at": datetime.utcnow().isoformat()
                }
                # ===== STEP-BY-STEP LOGGING: WebSocket Data Sending =====
                total_faces = sum(len(d.get('faces', [])) for d in initial_data)
                logger.info(f"[WS] [STEP-BY-STEP] ========================================")
                logger.info(f"[WS] [STEP-BY-STEP] 📤 Preparing to send data to dashboard")
                logger.info(f"[WS] [STEP-BY-STEP] Pipeline entries: {len(initial_data)}")
                logger.info(f"[WS] [STEP-BY-STEP] Total faces: {total_faces}")
                
                # Log details for each pipeline
                for idx, entry in enumerate(initial_data):
                    pipeline_id = entry.get('pipeline_id', 'unknown')
                    faces_count = len(entry.get('faces', []))
                    logger.info(f"[WS] [STEP-BY-STEP]   Pipeline #{idx+1}: {pipeline_id} - {faces_count} faces")
                    for face_idx, face in enumerate(entry.get('faces', [])[:3]):  # Show first 3 faces
                        face_name = face.get('name', 'Unknown')
                        face_sim = face.get('similarity', 0)
                        logger.info(f"[WS] [STEP-BY-STEP]     Face #{face_idx+1}: {face_name} (similarity: {face_sim:.4f})")
                    if faces_count > 3:
                        logger.info(f"[WS] [STEP-BY-STEP]     ... and {faces_count - 3} more faces")
                
                logger.info(f"[WS] [STEP-BY-STEP] ========================================")
                logger.info(f"[WS] 📤 Sending initial_data: {len(initial_data)} pipeline entries, {total_faces} total faces")
                # Check connection state before sending
                try:
                    client_state = websocket.client_state.name if hasattr(websocket.client_state, 'name') else str(websocket.client_state)
                    if client_state != "CONNECTED":
                        logger.warning(f"[WS] ⚠️ Connection not in CONNECTED state: {client_state}, skipping initial_data")
                        raise WebSocketDisconnect(code=1006)
                except (AttributeError, Exception) as state_check_error:
                    # If we can't check state, try to send anyway (connection might still be valid)
                    logger.debug(f"[WS] Could not check connection state: {state_check_error}, attempting to send anyway")
                
                # Try to send initial_data (KNOWN faces for dashboard), handle disconnection gracefully
                try:
                    await websocket.send_json(initial_data_message)
                    logger.info(f"[WS] [STEP-BY-STEP] ✅ Successfully sent initial_data to dashboard")
                    logger.info(f"[WS] [STEP-BY-STEP] Dashboard will now display {total_faces} KNOWN faces")
                    logger.info(f"[WS] ✅ Successfully sent initial_data with {len(initial_data)} pipeline entries")
                    
                    # Send UNKNOWN faces to unknown page (if any)
                    initial_unknown_data_message = None
                    if initial_unknown_data:
                        total_unknown_faces = sum(len(d.get('faces', [])) for d in initial_unknown_data)
                        initial_unknown_data_message = {
                            "type": "initial_unknown_data",
                            "data": initial_unknown_data,  # Send as array directly
                            "stats": {
                                **queue_stats,
                                "pipelines": {
                                    "active": len(initial_unknown_data),
                                    "total_detections": total_unknown_faces,
                                },
                                "faces": {
                                    "total": total_unknown_faces
                                }
                            },
                            "timestamp": datetime.utcnow().isoformat()
                        }
                        
                        # Add to cache data
                        cache_data["initial_unknown_data_message"] = initial_unknown_data_message

                        logger.info(f"[WS] [STEP-BY-STEP] 📤 Preparing to send initial_unknown_data to unknown page")
                        logger.info(f"[WS] [STEP-BY-STEP] Pipeline entries: {len(initial_unknown_data)}")
                        logger.info(f"[WS] [STEP-BY-STEP] Total UNKNOWN faces: {total_unknown_faces}")

                        try:
                            # Only unknown-page clients need this heavy payload;
                            # dashboards discard it unhandled.
                            if client_view == "unknown":
                                await websocket.send_json(initial_unknown_data_message)
                            logger.info(f"[WS] [STEP-BY-STEP] ✅ Successfully sent initial_unknown_data to unknown page")
                            logger.info(f"[WS] ✅ Successfully sent initial_unknown_data with {len(initial_unknown_data)} pipeline entries")
                        except WebSocketDisconnect:
                            logger.info(f"[WS] ℹ️ Client disconnected while sending initial_unknown_data (normal if page closed/refreshed)")
                            raise
                        except (RuntimeError, ConnectionError) as send_error:
                            error_msg = str(send_error)
                            error_type = type(send_error).__name__
                            if any(keyword in error_msg.lower() for keyword in ["connection closed", "close message", "not connected", "clientdisconnected", "connectionclosed"]):
                                logger.info(f"[WS] ℹ️ Client disconnected while sending initial_unknown_data: {error_type}: {error_msg}")
                                raise WebSocketDisconnect(code=1006)
                            else:
                                logger.warning(f"[WS] ⚠️ Unexpected connection error sending initial_unknown_data: {error_type}: {error_msg}")
                                raise
                        except Exception as send_error:
                            error_msg = str(send_error)
                            error_type = type(send_error).__name__
                            is_connection_error = (
                                "ConnectionClosed" in error_type or 
                                "ClientDisconnected" in error_type or 
                                "Connection closed" in error_msg or
                                "close message" in error_msg.lower() or
                                "not connected" in error_msg.lower()
                            )
                            if is_connection_error:
                                logger.info(f"[WS] ℹ️ Client disconnected while sending initial_unknown_data: {error_type}: {error_msg}")
                                raise WebSocketDisconnect(code=1006)
                            else:
                                logger.error(f"[WS] ❌ Error sending initial_unknown_data: {error_type}: {error_msg}", exc_info=True)
                                # Don't raise - initial_data was sent successfully, this is just a bonus
                    else:
                        logger.info(f"[WS] [STEP-BY-STEP] ℹ️ No UNKNOWN faces to send (all faces are KNOWN)")
                    
                    # REDIS CACHING: Store in cache after successful send
                    # Use dashboard TTL for the main cache (contains both KNOWN and UNKNOWN data)
                    # The cache will be invalidated when new detections arrive anyway
                    await redis_cache_service.set(cache_key_known, cache_data, ttl=dashboard_cache_ttl)
                    logger.info(f"[WS] [CACHE] 💾 Cached initial data (Dashboard TTL: {dashboard_cache_ttl}s = {dashboard_cache_ttl/3600:.1f}h, Unknown TTL: {unknown_cache_ttl}s = {unknown_cache_ttl/3600:.1f}h, key: {cache_key_known})")
                    
                    # Also cache unknown data separately with longer TTL if it exists
                    if initial_unknown_data:
                        # Generate separate cache key for unknown data
                        cache_key_unknown = await redis_cache_service.get_unknown_cache_key(
                            user_id=user_id,
                            page=1,  # WebSocket sends all data, so use page 1
                            page_size=1000,  # Large page size to match WebSocket data
                            filters={}  # No filters for WebSocket data
                        )
                        unknown_cache_data = {
                            "initial_unknown_data": initial_unknown_data,
                            "initial_unknown_data_message": cache_data.get("initial_unknown_data_message"),
                            "cached_at": datetime.utcnow().isoformat()
                        }
                        await redis_cache_service.set(cache_key_unknown, unknown_cache_data, ttl=unknown_cache_ttl)
                        logger.info(f"[WS] [CACHE] 💾 Cached unknown data separately (TTL: {unknown_cache_ttl}s = {unknown_cache_ttl/3600:.1f} hours, key: {cache_key_unknown})")
                        
                except WebSocketDisconnect:
                    # Client disconnected - this is normal if page closed/refreshed
                    logger.info(f"[WS] ℹ️ Client disconnected while sending initial_data (normal if page closed/refreshed)")
                    raise  # Re-raise to exit gracefully
                except (RuntimeError, ConnectionError) as send_error:
                    # Connection-related errors - check if it's a disconnection
                    error_msg = str(send_error)
                    error_type = type(send_error).__name__
                    if any(keyword in error_msg.lower() for keyword in ["connection closed", "close message", "not connected", "clientdisconnected", "connectionclosed"]):
                        logger.info(f"[WS] ℹ️ Client disconnected while sending initial_data (normal if page closed/refreshed): {error_type}: {error_msg}")
                        raise WebSocketDisconnect(code=1006)  # Re-raise to exit gracefully
                    else:
                        logger.warning(f"[WS] ⚠️ Unexpected connection error sending initial_data: {error_type}: {error_msg}")
                        raise
                except Exception as send_error:
                    # Check if it's a connection-related error by examining the exception
                    error_msg = str(send_error)
                    error_type = type(send_error).__name__
                    
                    # Check exception message and type for connection-related errors
                    is_connection_error = (
                        "ConnectionClosed" in error_type or 
                        "ClientDisconnected" in error_type or 
                        "Connection closed" in error_msg or
                        "close message" in error_msg.lower() or
                        "not connected" in error_msg.lower()
                    )
                    
                    if is_connection_error:
                        logger.info(f"[WS] ℹ️ Client disconnected while sending initial_data (normal if page closed/refreshed): {error_type}: {error_msg}")
                        raise WebSocketDisconnect(code=1006)  # Re-raise to exit gracefully
                    else:
                        logger.error(f"[WS] ❌ Error sending initial_data: {error_type}: {error_msg}", exc_info=True)
                        raise
                
                # Verify connection is still active after sending initial_data
                async with ws_manager._lock:
                    is_still_connected = websocket in ws_manager.active_connections
                    total_connections = len(ws_manager.active_connections)
                    logger.info(f"[WS] 🔍 Connection verification after initial_data: still_connected={is_still_connected}, total_connections={total_connections}")
                    if not is_still_connected:
                        logger.error(f"[WS] ❌ CRITICAL: Connection was removed from active_connections after sending initial_data!")
                    else:
                        logger.info(f"[WS] ✅ Connection verified: still in active_connections (conn_id={id(websocket)})")
        except Exception as initial_data_error:
            # CRITICAL: If there's an error loading initial data, still send empty data
            # This ensures the frontend knows the WebSocket is working
            logger.error(f"[WS] ❌ Error loading initial_data: {initial_data_error}", exc_info=True)
            try:
                # Check connection state before sending
                try:
                    client_state = websocket.client_state.name if hasattr(websocket.client_state, 'name') else str(websocket.client_state)
                    is_connected = client_state == "CONNECTED"
                except (AttributeError, Exception):
                    # If we can't check state, assume connected and try to send
                    is_connected = True
                
                if is_connected:
                    # Get basic stats even if database query failed
                    try:
                        queue_stats = await processing_queue.get_stats()
                        await websocket.send_json({
                            "type": "initial_data",
                            "data": [],  # Empty data - frontend will handle gracefully
                            "stats": {
                                **queue_stats,
                                "pipelines": {"active": 0, "total_detections": 0},
                                "faces": {"total": 0}
                            },
                            "tracker_stats": {"enabled": FACE_TRACKING_ENABLED, "error": "Failed to load initial data"},
                            "storage_stats": {"total_size_mb": 0, "file_count": 0, "error": "Failed to load initial data"},
                            "timestamp": datetime.utcnow().isoformat(),
                            "error": str(initial_data_error)  # Include error for debugging
                        })
                        logger.warning(f"[WS] ⚠️ Sent empty initial_data due to error (frontend will use API fallback)")
                    except WebSocketDisconnect:
                        logger.info(f"[WS] ℹ️ Client disconnected while sending empty initial_data (normal if page closed/refreshed)")
                        raise  # Re-raise to exit gracefully
                    except (ConnectionError, RuntimeError) as send_error:
                        error_msg = str(send_error)
                        error_type = type(send_error).__name__
                        if any(keyword in error_msg.lower() for keyword in ["connection closed", "close message", "not connected", "clientdisconnected", "connectionclosed"]):
                            logger.info(f"[WS] ℹ️ Client disconnected while sending empty initial_data (normal if page closed/refreshed): {error_type}: {error_msg}")
                            raise WebSocketDisconnect(code=1006)
                        else:
                            logger.warning(f"[WS] ⚠️ Connection error while sending empty initial_data: {error_type}: {error_msg}")
                            raise WebSocketDisconnect(code=1006)
                    except Exception as send_error:
                        error_msg = str(send_error)
                        error_type = type(send_error).__name__
                        is_connection_error = (
                            "ConnectionClosed" in error_type or 
                            "ClientDisconnected" in error_type or 
                            "Connection closed" in error_msg or
                            "close message" in error_msg.lower()
                        )
                        if is_connection_error:
                            logger.info(f"[WS] ℹ️ Client disconnected while sending empty initial_data (normal if page closed/refreshed): {error_type}: {error_msg}")
                            raise WebSocketDisconnect(code=1006)
                        else:
                            logger.error(f"[WS] ❌ Failed to send even empty initial_data: {error_type}: {error_msg}", exc_info=True)
                            # Connection is likely broken, will be cleaned up in finally block
                else:
                    logger.warning(f"[WS] ⚠️ Connection not in CONNECTED state: {websocket.client_state.name}, cannot send empty initial_data")
            except WebSocketDisconnect:
                # Client disconnected - re-raise to exit gracefully
                raise
            except Exception as send_error:
                # Other errors - log but don't re-raise (connection might still be valid)
                logger.warning(f"[WS] ⚠️ Error in initial_data error handler: {send_error}")

        # Keep alive loop
        logger.info(f"[WS] 🔄 Starting keep-alive loop for WebSocket connection")
        keep_alive_count = 0
        while True:
            try:
                # Check connection state before receiving
                try:
                    client_state = websocket.client_state.name if hasattr(websocket.client_state, 'name') else str(websocket.client_state)
                    if client_state != "CONNECTED":
                        logger.warning(f"[WS] ⚠️ Connection not in CONNECTED state: {client_state}, breaking keep-alive loop")
                        break
                except (AttributeError, Exception):
                    # If we can't check state, assume connected and continue
                    pass
                
                # Wait for ping message or timeout
                logger.debug(f"[WS] ⏳ Waiting for message (timeout: 30s)...")
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                logger.debug(f"[WS] 📨 Received message from client: {data[:100]}")

                # Check connection state before sending response
                try:
                    client_state = websocket.client_state.name if hasattr(websocket.client_state, 'name') else str(websocket.client_state)
                    if client_state != "CONNECTED":
                        logger.warning(f"[WS] ⚠️ Connection not in CONNECTED state: {client_state}, breaking keep-alive loop")
                        break
                except (AttributeError, Exception):
                    # If we can't check state, assume connected and continue
                    pass
                
                # Handle different message types
                if data == "ping":
                    logger.debug(f"[WS] 📥 Received ping, sending pong")
                    try:
                        await websocket.send_json({"type": "pong", "timestamp": datetime.utcnow().isoformat()})
                    except (WebSocketDisconnect, ConnectionError, RuntimeError) as e:
                        logger.warning(f"[WS] ⚠️ Connection closed while sending pong: {e}")
                        break
                elif data.startswith("subscribe:"):
                    # Handle subscription requests
                    channel = data.split(":", 1)[1]
                    logger.info(f"[WS] Client subscribed to channel: {channel}")
                    try:
                        await websocket.send_json({
                            "type": "subscription_confirmed",
                            "channel": channel,
                            "timestamp": datetime.utcnow().isoformat()
                        })
                    except (WebSocketDisconnect, ConnectionError, RuntimeError) as e:
                        logger.warning(f"[WS] ⚠️ Connection closed while sending subscription confirmation: {e}")
                        break
                else:
                    logger.debug(f"[WS] 📨 Received unknown message type: {data[:50]}")

            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                keep_alive_count += 1
                logger.debug(f"[WS] ⏰ Timeout (30s) - sending ping to keep connection alive (count: {keep_alive_count})")
                
                # Check connection state before sending ping
                try:
                    client_state = websocket.client_state.name if hasattr(websocket.client_state, 'name') else str(websocket.client_state)
                    if client_state != "CONNECTED":
                        logger.warning(f"[WS] ⚠️ Connection not in CONNECTED state: {client_state}, breaking keep-alive loop")
                        break
                except (AttributeError, Exception):
                    # If we can't check state, assume connected and continue
                    pass
                
                try:
                    await websocket.send_json({"type": "ping", "timestamp": datetime.utcnow().isoformat()})
                    logger.debug(f"[WS] ✅ Ping sent successfully")
                except (WebSocketDisconnect, ConnectionError, RuntimeError) as ping_error:
                    logger.warning(f"[WS] ⚠️ Connection closed while sending ping: {ping_error}")
                    break  # Connection lost
                except Exception as ping_error:
                    logger.error(f"[WS] ❌ Failed to send ping: {ping_error}")
                    logger.error(f"[WS] 🔌 Connection lost - breaking keep-alive loop")
                    break  # Connection lost
            except WebSocketDisconnect:
                logger.info(f"[WS] 🔌 WebSocket disconnected by client")
                break
            except (ConnectionError, RuntimeError) as conn_error:
                # Handle connection errors gracefully
                error_msg = str(conn_error)
                if "not connected" in error_msg.lower() or "close" in error_msg.lower():
                    logger.info(f"[WS] 🔌 Connection closed: {error_msg}")
                else:
                    logger.warning(f"[WS] ⚠️ Connection error: {error_msg}")
                break
            except Exception as e:
                error_msg = str(e)
                # Check if it's a connection-related error
                if "not connected" in error_msg.lower() or "close" in error_msg.lower() or "accept" in error_msg.lower():
                    logger.info(f"[WS] 🔌 Connection state error: {error_msg}")
                    break
                else:
                    logger.error(f"[WS] ❌ Error in keep-alive loop: {e}", exc_info=True)
                    break

    except WebSocketDisconnect:
        logger.info(f"[WS] 🔌 WebSocket disconnected (outer exception handler)")
        pass
    except Exception as e:
        logger.error(f"[WS] ❌ Fatal error in WebSocket endpoint: {e}", exc_info=True)
    finally:
        logger.info(f"[WS] 🧹 Cleaning up WebSocket connection...")
        await ws_manager.disconnect(websocket)
        logger.info(f"[WS] ✅ WebSocket connection cleaned up. Remaining connections: {len(ws_manager.active_connections)}")

