"""
Rate limiting (Redis-backed fixed window)
=========================================
Per-user AND per-IP counters per scope; exceeding either returns
``429 Too Many Requests`` with a safe ``Retry-After`` header.

Backing store: the application's shared Redis (INCR + EXPIRE on a
fixed-window key ``fr:rl:{scope}:{principal}:{window}``). Fixed windows are
deliberately chosen over sliding sets: O(1) per request, deterministic in
tests, and worst-case burst is 2x the limit at a window boundary — an
acceptable bound for these endpoints.

Redis unavailable → FAIL-OPEN with a per-process in-memory fallback counter
and a warning metric/log. Documented behavior: availability wins over strict
limiting; the in-memory fallback is approximate (per worker, lost on
restart) and exists so a Redis outage still leaves SOME ceiling rather than
none. Do not rely on it for hard guarantees.

Limits come from runtime settings (RATE_LIMIT_* keys, live-applyable), so
operations can tighten them without a deploy.
"""

import logging
import time
from collections import defaultdict
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request
from config import settings

logger = logging.getLogger(__name__)

RL_PREFIX = "fr:rl:"

# scope -> (settings key for per-minute limit, default)
_HEAVY_DEFAULT = 60
_STANDARD_DEFAULT = 300

# In-memory fallback: {window_key: count} — pruned lazily.
_fallback_counts: Dict[str, int] = defaultdict(int)
_fallback_seen_windows: Dict[str, int] = {}



def _redis():
    try:
        from backend.core.cache_manager import cache_manager
        client = getattr(cache_manager, "redis_client", None)
        if client is not None and getattr(cache_manager, "_enabled", False):
            return client
    except Exception:
        pass
    return None


def _observe_rejection(scope: str) -> None:
    try:
        from backend.core import metrics as app_metrics
        if app_metrics.metrics_rate_limited is not None:
            app_metrics.metrics_rate_limited.labels(scope=scope).inc()
    except Exception:
        pass


def _client_ip(request: Request) -> str:
    """First X-Forwarded-For hop (nginx fronts the app) else the socket peer."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _principals(request: Request, current_user) -> Tuple[str, str]:
    user = None
    if isinstance(current_user, dict):
        user = current_user.get("id") or current_user.get("user_id") or current_user.get("username")
    elif current_user is not None:  # ORM User from get_current_user
        user = getattr(current_user, "id", None) or getattr(current_user, "username", None)
    return (f"u:{user}" if user else "u:anon", f"ip:{_client_ip(request)}")


async def _bump(key: str, window_seconds: int) -> Optional[int]:
    """Increment the fixed-window counter; returns the new count, or None
    when Redis is unavailable (caller falls back in-memory)."""
    client = _redis()
    if client is None:
        return None
    try:
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds + 2)
        results = await pipe.execute()
        return int(results[0])
    except Exception as e:
        logger.warning("[RATE_LIMIT] redis error (%s) — fail-open with in-memory fallback", e)
        return None


def _bump_fallback(key: str, window_index: int) -> int:
    # prune previous windows lazily to keep the dict bounded
    stale = [k for k, w in _fallback_seen_windows.items() if w != window_index]
    for k in stale[:1000]:
        _fallback_counts.pop(k, None)
        _fallback_seen_windows.pop(k, None)
    _fallback_seen_windows[key] = window_index
    _fallback_counts[key] += 1
    return _fallback_counts[key]


async def enforce_rate_limit(request: Request, current_user,
                             scope: str, heavy: bool = False,
                             limit_override: Optional[int] = None) -> None:
    """Raise HTTPException(429) with Retry-After when the caller exceeds the
    scope's per-minute limit on EITHER the user or the IP counter."""
    # API_RATE_LIMIT_ENABLED, not the legacy RATE_LIMIT_ENABLED: that older
    # env var belongs to a retired webhook throttle and live deployments
    # already set it false — reusing the name would silently disable THIS
    # limiter everywhere.
    if not bool(settings.API_RATE_LIMIT_ENABLED):
        return

    if limit_override is not None:
        limit = int(limit_override)
    elif heavy:
        limit = int(settings.RATE_LIMIT_HEAVY_PER_MINUTE)
    else:
        limit = int(settings.RATE_LIMIT_DEFAULT_PER_MINUTE)
    if limit <= 0:
        return  # 0 = disabled for this class

    window_seconds = 60
    now = time.time()
    window_index = int(now // window_seconds)
    retry_after = max(1, int((window_index + 1) * window_seconds - now))

    for principal in _principals(request, current_user):
        key = f"{RL_PREFIX}{scope}:{principal}:{window_index}"
        count = await _bump(key, window_seconds)
        if count is None:
            count = _bump_fallback(key, window_index)
        if count > limit:
            _observe_rejection(scope)
            logger.warning(
                "[RATE_LIMIT] scope=%s principal=%s count=%s limit=%s request_id=%s",
                scope, principal.split(":", 1)[0], count, limit,
                getattr(request.state, "request_id", None))
            raise HTTPException(
                status_code=429,
                detail={
                    "error_code": "RATE_LIMITED",
                    "message": f"Too many requests for {scope}. Retry later.",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )


def rate_limited(scope: str, heavy: bool = False):
    """FastAPI dependency factory: per-user + per-IP limits for `scope`.

    Depends on get_current_user — FastAPI caches that sub-dependency per
    request, so routes that also require auth (all limited routes do) resolve
    the user exactly once. Unauthenticated probes 401 here, identical to the
    route's own auth outcome.
    """
    from backend.auth.auth_service import get_current_user
    from fastapi import Depends

    async def _dependency(request: Request, current_user=Depends(get_current_user)):
        await enforce_rate_limit(request, current_user, scope, heavy=heavy)
    return _dependency
