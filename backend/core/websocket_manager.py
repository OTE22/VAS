"""
WebSocket Manager
==================
"""

import os
import sys
import asyncio
import logging
import time
from typing import Dict, Optional, Any, Set, List, Tuple
from collections import defaultdict, deque
from enum import Enum

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from backend.config import *
from backend.core.metrics import *
from fastapi import WebSocket
import json
from datetime import datetime, date
from uuid import UUID

logger = logging.getLogger(__name__)

# Get process ID to identify which worker this is
PROCESS_ID = os.getpid()

# Message types that only unknown-page clients (ws connected with ?view=unknown)
# should receive - they carry full base64 face images the dashboard discards.
UNKNOWN_VIEW_MESSAGE_TYPES = {"new_unknown_detection", "initial_unknown_data"}

# Try to import Redis
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None


def _serialize_for_redis(obj, visited=None, max_depth=10, current_depth=0):
    """Recursively serialize objects for Redis pub/sub, handling non-serializable types
    
    Args:
        obj: Object to serialize
        visited: Set of object IDs already visited (to prevent circular references)
        max_depth: Maximum recursion depth
        current_depth: Current recursion depth
    """
    # Prevent infinite recursion
    if current_depth > max_depth:
        return f"<max_depth_reached:{type(obj).__name__}>"
    
    if visited is None:
        visited = set()
    
    # Handle primitive types
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    
    # Handle hashable types that might cause circular references
    obj_id = id(obj)
    if obj_id in visited:
        return f"<circular_reference:{type(obj).__name__}>"
    
    # Add to visited set for mutable types
    if isinstance(obj, (dict, list, tuple, set)) or hasattr(obj, '__dict__'):
        visited.add(obj_id)
    
    try:
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        
        # Handle mappingproxy (immutable dict-like objects)
        if type(obj).__name__ == 'mappingproxy':
            try:
                return {k: _serialize_for_redis(v, visited, max_depth, current_depth + 1) for k, v in obj.items()}
            except (TypeError, AttributeError):
                return str(obj)
        
        # Handle dict-like objects
        if isinstance(obj, dict):
            return {k: _serialize_for_redis(v, visited, max_depth, current_depth + 1) for k, v in obj.items()}
        
        # Handle iterable objects (lists, tuples, sets)
        if isinstance(obj, (list, tuple, set)):
            return [_serialize_for_redis(item, visited, max_depth, current_depth + 1) for item in obj]
        
        # Handle objects with __dict__ attribute (but be careful with circular refs)
        if hasattr(obj, '__dict__'):
            try:
                # Only serialize simple attributes, skip complex objects
                result = {}
                for k, v in obj.__dict__.items():
                    # Skip private/internal attributes that might cause issues
                    if k.startswith('_') and k not in ['_id', '_name']:
                        continue
                    try:
                        result[k] = _serialize_for_redis(v, visited, max_depth, current_depth + 1)
                    except (RecursionError, TypeError):
                        result[k] = f"<serialization_error:{type(v).__name__}>"
                return result
            except (TypeError, AttributeError, RecursionError):
                return f"<object:{type(obj).__name__}>"
        
        # Fallback: convert to string
        return str(obj)
    finally:
        # Remove from visited set when done (for nested structures)
        if obj_id in visited and current_depth == 0:
            visited.discard(obj_id)


class WebSocketJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder for WebSocket messages that handles UUIDs, dates, and other non-serializable types"""
    def default(self, obj):
        serialized = _serialize_for_redis(obj)
        # If serialization returned the same object, it means it's still not serializable
        if serialized is obj and not isinstance(obj, (str, int, float, bool, type(None))):
            return str(obj)
        return serialized


class WebSocketManager:
    """Manage WebSocket connections for real-time dashboard updates"""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.connection_pipelines: Dict[WebSocket, Optional[List[str]]] = {}  # Store user's accessible pipelines per connection
        self._lock = asyncio.Lock()
        
        # Redis pub/sub for multi-worker support
        self.redis_client: Optional[redis.Redis] = None
        self.redis_pubsub: Optional[Any] = None
        self._redis_enabled = False
        self._redis_listener_task: Optional[asyncio.Task] = None
        self._redis_initialized = False

    async def initialize_redis(self):
        """Initialize Redis connection for pub/sub across workers"""
        if not REDIS_AVAILABLE:
            logger.warning(f"[WS-MANAGER] ⚠️ Redis not available - WebSocket broadcasts will only work within single worker")
            return False
        
        if self._redis_initialized:
            return self._redis_enabled
        
        try:
            redis_url = getattr(settings, 'REDIS_URL', "redis://localhost:6379/0")
            logger.info(f"[WS-MANAGER] 🔗 Initializing Redis pub/sub for WebSocket broadcasts (URL: {redis_url})")
            
            self.redis_client = await redis.from_url(
                redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_keepalive=True,
                socket_connect_timeout=5,
                retry_on_timeout=True,
            )
            await self.redis_client.ping()
            
            # Create pubsub instance
            self.redis_pubsub = self.redis_client.pubsub()
            await self.redis_pubsub.subscribe("websocket_broadcast")
            
            # Start listener task
            self._redis_listener_task = asyncio.create_task(self._redis_listener())
            
            self._redis_enabled = True
            self._redis_initialized = True
            logger.info(f"[WS-MANAGER] ✅ Redis pub/sub initialized (Worker PID: {PROCESS_ID})")
            return True
        except Exception as e:
            logger.error(f"[WS-MANAGER] ❌ Failed to initialize Redis pub/sub: {e}", exc_info=True)
            self._redis_enabled = False
            self._redis_initialized = True
            return False
    
    async def _redis_listener(self):
        """Listen for messages from other workers via Redis pub/sub"""
        logger.info(f"[WS-MANAGER] 👂 Starting Redis listener (Worker PID: {PROCESS_ID})")
        try:
            while True:
                try:
                    message = await self.redis_pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message and message['type'] == 'message':
                        try:
                            data = json.loads(message['data'])
                            # Don't broadcast to local connections if this message came from this worker
                            if data.get('worker_pid') != PROCESS_ID:
                                logger.info(f"[WS-MANAGER] 📥 Received broadcast from worker {data.get('worker_pid')} via Redis")
                                # Broadcast to local connections
                                await self._broadcast_local(
                                    data['message'],
                                    data.get('pipeline_id'),
                                    data.get('admin_only', False)
                                )
                            else:
                                logger.debug(f"[WS-MANAGER] 🔄 Ignoring own broadcast (Worker PID: {PROCESS_ID})")
                        except Exception as e:
                            logger.error(f"[WS-MANAGER] ❌ Error processing Redis message: {e}", exc_info=True)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"[WS-MANAGER] ❌ Error in Redis listener: {e}", exc_info=True)
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info(f"[WS-MANAGER] 🛑 Redis listener stopped (Worker PID: {PROCESS_ID})")
        except Exception as e:
            logger.error(f"[WS-MANAGER] ❌ Redis listener fatal error: {e}", exc_info=True)
    
    async def _broadcast_local(self, message: dict, pipeline_id: Optional[str] = None, admin_only: bool = False):
        """Broadcast message to local connections only (called from Redis listener)"""
        message_type = message.get('type', 'unknown')
        
        async with self._lock:
            connections = list(self.active_connections)
            connection_pipelines = {conn: self.connection_pipelines.get(conn) for conn in connections}
        
        if not connections:
            return
        
        disconnected_connections = []
        sent_count = 0
        filtered_count = 0
        
        async def safe_send(conn: WebSocket):
            nonlocal sent_count, filtered_count
            try:
                # Unknown-face payloads (heavy base64) only go to unknown-page clients
                if message_type in UNKNOWN_VIEW_MESSAGE_TYPES:
                    if conn.scope.get("client_view") != "unknown":
                        filtered_count += 1
                        return None

                if admin_only:
                    user_pipelines = connection_pipelines.get(conn)
                    if user_pipelines is not None:
                        filtered_count += 1
                        return None
                
                if pipeline_id:
                    user_pipelines = connection_pipelines.get(conn)
                    if user_pipelines is not None and (not user_pipelines or pipeline_id not in user_pipelines):
                        filtered_count += 1
                        return None
                
                # Serialize message to handle UUIDs and other non-serializable types
                serialized_message = _serialize_for_redis(message)
                await conn.send_json(serialized_message)
                sent_count += 1
                return None
            except Exception:
                return conn
        
        tasks = [safe_send(conn) for conn in connections]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        async with self._lock:
            for i, result in enumerate(results):
                if isinstance(result, WebSocket):
                    self.active_connections.discard(result)
                    disconnected_connections.append(result)
                elif isinstance(result, Exception):
                    if i < len(connections):
                        self.active_connections.discard(connections[i])
                        disconnected_connections.append(connections[i])
        
        if sent_count > 0:
            logger.info(f"[WS-MANAGER] ✅ Local broadcast '{message_type}' sent to {sent_count} connection(s) (from Redis)")
    
    async def connect(self, websocket: WebSocket, user_pipelines: Optional[List[str]] = None):
        logger.info(f"[WS-MANAGER] 🔗 Accepting WebSocket connection...")
        try:
            await websocket.accept()
            logger.info(f"[WS-MANAGER] ✅ WebSocket handshake accepted")
        except Exception as accept_error:
            logger.error(f"[WS-MANAGER] ❌ Failed to accept WebSocket: {accept_error}", exc_info=True)
            raise
        
        async with self._lock:
            self.active_connections.add(websocket)
            self.connection_pipelines[websocket] = user_pipelines  # None means admin (all pipelines)
            metrics_websocket_connections.set(len(self.active_connections))
            logger.info(f"[WS-MANAGER] ✅ Client registered. Total active connections: {len(self.active_connections)} (Worker PID: {PROCESS_ID})")
            logger.info(f"[WS-MANAGER] 📋 Connection pipelines: {user_pipelines if user_pipelines is not None else 'all (admin)'}")

    async def disconnect(self, websocket: WebSocket):
        """Safely disconnect a WebSocket connection, handling already-closed sockets gracefully"""
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.discard(websocket)
                user_pipelines = self.connection_pipelines.pop(websocket, None)
                metrics_websocket_connections.set(len(self.active_connections))
                logger.info(f"[WS-MANAGER] 🔌 Client disconnected. Remaining connections: {len(self.active_connections)}")
                logger.info(f"[WS-MANAGER] 📋 Disconnected client had pipelines: {user_pipelines if user_pipelines is not None else 'all (admin)'}")
            else:
                logger.debug(f"[WS-MANAGER] ⚠️ Attempted to disconnect WebSocket that was not in active connections (may have been already cleaned up)")
        
        # Try to close the websocket gracefully, but ignore errors if already closed
        try:
            # Check if websocket is still in a valid state before closing
            try:
                client_state = websocket.client_state.name if hasattr(websocket.client_state, 'name') else str(websocket.client_state)
                if client_state not in ["CONNECTED", "CONNECTING"]:
                    # Already disconnected, no need to close
                    return
            except (AttributeError, Exception):
                # Can't check state, try to close anyway
                pass
            
            # Try to close the websocket
            try:
                await websocket.close(code=1000, reason="Normal closure")
            except (OSError, RuntimeError, ConnectionError) as close_error:
                # Handle "Bad file descriptor" and other socket errors gracefully
                error_msg = str(close_error).lower()
                if "bad file descriptor" in error_msg or "errno 9" in error_msg:
                    logger.debug(f"[WS-MANAGER] Socket already closed (Bad file descriptor) - this is normal during shutdown")
                elif "connection closed" in error_msg or "not connected" in error_msg:
                    logger.debug(f"[WS-MANAGER] Socket already closed - this is normal")
                else:
                    logger.warning(f"[WS-MANAGER] Error closing websocket: {close_error}")
            except Exception as close_error:
                # Other errors - log but don't raise
                error_msg = str(close_error).lower()
                if "bad file descriptor" not in error_msg and "errno 9" not in error_msg:
                    logger.debug(f"[WS-MANAGER] Socket close error (non-critical): {close_error}")
        except Exception as e:
            # Ignore any errors during cleanup - socket may already be closed
            logger.debug(f"[WS-MANAGER] Error during websocket cleanup (non-critical): {e}")

    async def broadcast(self, message: dict, pipeline_id: Optional[str] = None, admin_only: bool = False):
        """
        Broadcast message to all active connections safely, filtered by pipeline access.
        Uses Redis pub/sub for multi-worker support.
        
        Args:
            message: Message dict to send
            pipeline_id: Filter by specific pipeline (None = all pipelines)
            admin_only: If True, send only to admin users (default: False)
        """
        message_type = message.get('type', 'unknown')
        # Surface the display name (if the message carries one) for traceability
        _loc = None
        if isinstance(message, dict):
            _loc = message.get('location_name')
            if _loc is None and isinstance(message.get('data'), dict):
                _loc = message['data'].get('location_name')
        logger.info(f"[WS] type={message_type} pipeline_id={pipeline_id} location_name={_loc!r}")
        logger.info(f"[WS-MANAGER] 📡 Starting broadcast: type='{message_type}', pipeline_id={pipeline_id}, admin_only={admin_only} (Worker PID: {PROCESS_ID})")
        
        # Initialize Redis if not already done
        if not self._redis_initialized:
            await self.initialize_redis()
        
        # Publish to Redis for other workers (if Redis is enabled)
        if self._redis_enabled and self.redis_client:
            try:
                # Pre-serialize the message to handle nested non-serializable objects
                serialized_message = _serialize_for_redis(message)
                redis_payload = {
                    'message': serialized_message,
                    'pipeline_id': pipeline_id,
                    'admin_only': admin_only,
                    'worker_pid': PROCESS_ID
                }
                redis_message = json.dumps(redis_payload, cls=WebSocketJSONEncoder)
                await self.redis_client.publish("websocket_broadcast", redis_message)
                logger.info(f"[WS-MANAGER] 📤 Published broadcast to Redis (Worker PID: {PROCESS_ID})")
            except Exception as e:
                logger.error(f"[WS-MANAGER] ❌ Failed to publish to Redis: {e}", exc_info=True)
        
        # Get copy of connections and their pipeline access under lock
        async with self._lock:
            connections = list(self.active_connections)
            connection_pipelines = {conn: self.connection_pipelines.get(conn) for conn in connections}
            logger.info(f"[WS-MANAGER] 📊 Current state: {len(connections)} active connection(s) in memory (Worker PID: {PROCESS_ID})")
            if len(connections) > 0:
                for i, conn in enumerate(connections):
                    pipelines = connection_pipelines.get(conn)
                    logger.debug(f"[WS-MANAGER]   Connection {i+1}: pipelines={pipelines if pipelines is not None else 'all (admin)'}, conn_id={id(conn)}")
            else:
                if self._redis_enabled:
                    logger.info(f"[WS-MANAGER] ℹ️ No local connections in this worker (PID: {PROCESS_ID}), but Redis will distribute to other workers")

        if not connections:
            # Even if no local connections, Redis will handle distribution to other workers
            if not self._redis_enabled:
                logger.warning(f"[WS-MANAGER] ⚠️ No active connections to broadcast to (message type: {message_type})")
            return

        logger.info(f"[WS-MANAGER] 📊 Active connections: {len(connections)}, message type: '{message_type}', pipeline_id: {pipeline_id}")

        disconnected_connections = []
        sent_count = 0
        filtered_count = 0
        error_count = 0

        async def safe_send(conn: WebSocket):
            nonlocal sent_count, filtered_count, error_count
            try:
                # Unknown-face payloads (heavy base64) only go to unknown-page clients
                if message_type in UNKNOWN_VIEW_MESSAGE_TYPES:
                    if conn.scope.get("client_view") != "unknown":
                        filtered_count += 1
                        return None

                # Filter by admin-only if requested
                if admin_only:
                    user_pipelines = connection_pipelines.get(conn)
                    # None means admin (all pipelines), list means regular user
                    if user_pipelines is not None:
                        filtered_count += 1
                        logger.debug(f"[WS-MANAGER] 🔒 Filtered out connection (admin_only=True, user is not admin)")
                        return None  # Skip this connection - not an admin
                
                # Filter by pipeline access if pipeline_id is specified
                if pipeline_id:
                    user_pipelines = connection_pipelines.get(conn)
                    # None means admin (all pipelines), empty list means no access
                    if user_pipelines is not None and (not user_pipelines or pipeline_id not in user_pipelines):
                        filtered_count += 1
                        logger.debug(f"[WS-MANAGER] 🔒 Filtered out connection - user doesn't have access to pipeline '{pipeline_id}' (user pipelines: {user_pipelines})")
                        return None  # Skip this connection - user doesn't have access
                
                # Send message
                logger.info(f"[WS-MANAGER] 📤 Sending '{message_type}' to connection (pipeline_id={pipeline_id})")
                try:
                    # Serialize message to handle UUIDs and other non-serializable types
                    serialized_message = _serialize_for_redis(message)
                    await conn.send_json(serialized_message)
                    sent_count += 1
                    logger.info(f"[WS-MANAGER] ✅ Successfully sent '{message_type}' to connection (pipeline_id={pipeline_id})")
                except Exception as send_error:
                    logger.error(f"[WS-MANAGER] ❌ Failed to send '{message_type}' to connection: {send_error}", exc_info=True)
                    raise  # Re-raise to mark connection for removal
                return None
            except Exception as e:
                error_count += 1
                logger.error(f"[WS-MANAGER] ❌ Send error for '{message_type}': {e}", exc_info=True)
                return conn  # Mark for removal

        # Send to all connections
        logger.debug(f"[WS-MANAGER] 🚀 Creating {len(connections)} send tasks for '{message_type}'")
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

        if sent_count > 0:
            logger.info(f"[WS-MANAGER] ✅ Broadcast '{message_type}' SUCCESS: sent to {sent_count} connection(s), filtered: {filtered_count}, errors: {error_count}, pipeline_id={pipeline_id}")
        elif filtered_count > 0:
            logger.warning(f"[WS-MANAGER] ⚠️ Broadcast '{message_type}' FILTERED: all {filtered_count} connection(s) filtered out (no matching pipeline access), pipeline_id={pipeline_id}")
        elif error_count > 0:
            logger.error(f"[WS-MANAGER] ❌ Broadcast '{message_type}' FAILED: {error_count} error(s) occurred, pipeline_id={pipeline_id}")
        else:
            logger.warning(f"[WS-MANAGER] ⚠️ Broadcast '{message_type}' NO RECIPIENTS: no connections matched filters, pipeline_id={pipeline_id}")

        if disconnected_connections:
            metrics_websocket_connections.set(len(self.active_connections))
            logger.warning(f"[WS-MANAGER] ⚠️ Removed {len(disconnected_connections)} disconnected clients during broadcast. Remaining: {len(self.active_connections)}")
            for conn in disconnected_connections:
                logger.debug(f"[WS-MANAGER]   Removed connection: {conn}")

    async def get_stats(self) -> dict:
        """Get WebSocket statistics"""
        async with self._lock:
            connection_count = len(self.active_connections)
            logger.info(f"[WS-MANAGER] 📊 Current active connections: {connection_count}")
            if connection_count > 0:
                # Log pipeline access for each connection
                for i, conn in enumerate(self.active_connections):
                    pipelines = self.connection_pipelines.get(conn)
                    logger.debug(f"[WS-MANAGER]   Connection {i+1}: pipelines={pipelines if pipelines is not None else 'all (admin)'}")
            return {
                "active_connections": connection_count,
                "connections": list(self.active_connections),
            }


ws_manager = WebSocketManager()
