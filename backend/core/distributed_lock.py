"""
Distributed locks (Redis-backed)
================================
Cross-worker mutual exclusion for single-flight operations. Built on the
standard Redis pattern:

    acquire: SET key token NX PX ttl_ms
    release: Lua compare-and-delete (only the owner's token releases)
    renew:   Lua compare-and-expire (only the owner extends)

Lock keys identify the operation and subject (e.g. ``fr:lock:threshold-job``,
``fr:lock:assessment:identity:<uuid>``). TTLs are bounded — a crashed worker's
lock self-expires, so there are no permanent deadlocks.

Degraded mode: when Redis is unavailable the helpers report
``redis_available() == False`` and acquisition FAILS OPEN (returns a token
without cross-process protection). Callers that layer this under an
in-process guard (routes/intelligence.py single-flight dicts) keep their
single-worker guarantees; multi-worker deployments without Redis get a
startup warning (see backend/lifespan.py) because cross-process
single-flight cannot be guaranteed there.
"""

import logging
import secrets
import time
from typing import Optional

logger = logging.getLogger(__name__)

LOCK_PREFIX = "fr:lock:"

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""


def _observe_contention(lock_name: str) -> None:
    try:
        from backend.core import metrics as app_metrics
        if app_metrics.metrics_lock_contention is not None:
            app_metrics.metrics_lock_contention.labels(lock=lock_name).inc()
    except Exception:
        pass


def _redis():
    """The application's shared Redis client, or None when unavailable."""
    try:
        from backend.core.cache_manager import cache_manager
        client = getattr(cache_manager, "redis_client", None)
        if client is not None and getattr(cache_manager, "_enabled", False):
            return client
    except Exception:
        pass
    return None


def redis_available() -> bool:
    return _redis() is not None


class DistributedLock:
    """One named lock. Not reentrant; one instance per acquisition attempt.

    Usage::

        lock = DistributedLock("threshold-job", ttl_seconds=3600)
        if not await lock.acquire():
            ...someone else holds it (lock.holder_hint may say who)...
        try:
            ...work...
            await lock.renew()          # optional, for long work
        finally:
            await lock.release()        # only releases OUR token
    """

    def __init__(self, name: str, ttl_seconds: float = 300.0):
        self.name = name
        self.key = LOCK_PREFIX + name
        self.ttl_ms = max(1000, int(ttl_seconds * 1000))
        self.token = secrets.token_hex(16)
        self.acquired = False
        self.distributed = False   # False in degraded (no-Redis) mode
        self.holder_hint: Optional[str] = None

    async def acquire(self, holder_label: Optional[str] = None) -> bool:
        """True on success. In degraded mode succeeds WITHOUT cross-process
        protection (callers keep their in-process guard as the first layer).
        """
        client = _redis()
        if client is None:
            self.acquired = True
            self.distributed = False
            return True
        value = f"{self.token}|{holder_label or ''}"
        try:
            ok = await client.set(self.key, value, nx=True, px=self.ttl_ms)
            if ok:
                self.acquired = True
                self.distributed = True
                self._value = value
                return True
            current = await client.get(self.key)
            if current:
                raw = current.decode() if isinstance(current, (bytes, bytearray)) else str(current)
                self.holder_hint = raw.split("|", 1)[1] if "|" in raw else None
            _observe_contention(self.name)
            logger.info("[LOCK] contention lock=%s holder=%s", self.name, self.holder_hint)
            return False
        except Exception as e:
            # Redis errored mid-flight: degrade exactly like redis-unavailable.
            logger.warning("[LOCK] redis error on acquire lock=%s error=%s — degraded mode", self.name, e)
            self.acquired = True
            self.distributed = False
            return True

    async def renew(self) -> bool:
        """Extend our TTL (long-running work). True if still owned."""
        if not (self.acquired and self.distributed):
            return self.acquired
        client = _redis()
        if client is None:
            return True
        try:
            renewed = await client.eval(_RENEW_LUA, 1, self.key, self._value, str(self.ttl_ms))
            return bool(renewed)
        except Exception as e:
            logger.warning("[LOCK] renew failed lock=%s error=%s", self.name, e)
            return True  # keep working; TTL expiry is the backstop

    async def release(self) -> None:
        """Compare-and-delete: never releases another worker's lock."""
        if not self.acquired:
            return
        self.acquired = False
        if not self.distributed:
            return
        client = _redis()
        if client is None:
            return
        try:
            await client.eval(_RELEASE_LUA, 1, self.key, self._value)
        except Exception as e:
            logger.warning("[LOCK] release failed lock=%s error=%s (TTL will expire it)", self.name, e)


async def peek_holder(name: str) -> Optional[str]:
    """The holder label of a lock, or None if free / Redis unavailable."""
    client = _redis()
    if client is None:
        return None
    try:
        current = await client.get(LOCK_PREFIX + name)
        if not current:
            return None
        raw = current.decode() if isinstance(current, (bytes, bytearray)) else str(current)
        return raw.split("|", 1)[1] if "|" in raw else raw
    except Exception:
        return None


async def with_retry(name: str, ttl_seconds: float, attempts: int = 3,
                     backoff_seconds: float = 0.2,
                     holder_label: Optional[str] = None) -> Optional[DistributedLock]:
    """Bounded-backoff acquisition helper. Returns the held lock or None."""
    import asyncio
    delay = backoff_seconds
    for attempt in range(max(1, attempts)):
        lock = DistributedLock(name, ttl_seconds)
        if await lock.acquire(holder_label=holder_label):
            return lock
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2.0)
    return None
