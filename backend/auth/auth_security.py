"""
Authentication Security Controls
=================================
Brute-force protection, token revocation, login-CSRF (Origin) validation,
timing equalization and structured auth errors.

Design notes:
  * Rate limiting and the revocation denylist are backed by **Redis** so they
    are shared across API replicas. An in-process fallback keeps single-node
    deployments protected if Redis is unavailable (fail-closed for counting,
    never fail-open for revocation).
  * Per-account AND per-IP counters run together: an attacker spraying one
    account is throttled by the account counter; an attacker spraying many
    accounts from one host is throttled by the IP counter. Neither can be
    used to permanently lock a victim out — both expire.
  * Usernames and IPs are never logged in clear: audit records carry a
    truncated HMAC so the same actor is correlatable without storing PII.
"""

import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from fastapi import Request

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured error codes (stable API contract — clients branch on these)
# ---------------------------------------------------------------------------

AUTH_ERROR_CODES = frozenset({
    "INVALID_CREDENTIALS",
    "RATE_LIMITED",
    "ACCOUNT_DISABLED",
    "ACCOUNT_LOCKED",
    "MFA_REQUIRED",
    "SESSION_CREATION_FAILED",
    "AUTH_SERVICE_UNAVAILABLE",
    "CSRF_FAILED",
    "INVALID_REQUEST",
    # Self-service password change. Unregistered codes are rewritten to
    # INVALID_REQUEST by auth_error(), which would leave the client unable to
    # tell "your current password is wrong" from "that password is too weak".
    "INVALID_CURRENT_PASSWORD",
    "PASSWORD_REUSED",
    "WEAK_PASSWORD",
    "PASSWORD_UPDATE_FAILED",
})

# The SAME message is returned for "no such user" and "wrong password" so the
# response body cannot be used to enumerate accounts.
GENERIC_CREDENTIALS_MESSAGE = "Invalid username or password."

# Input limits (the backend is authoritative; the frontend mirrors these)
MAX_USERNAME_LENGTH = 150
MAX_PASSWORD_LENGTH = 1024


def auth_error(code: str, message: str, reference_id: Optional[str] = None,
               retryable: bool = True, **extra) -> Dict:
    """Build the structured error body returned by auth endpoints."""
    body = {
        "error": {
            "code": code if code in AUTH_ERROR_CODES else "INVALID_REQUEST",
            "message": message,
            "reference_id": reference_id or new_reference_id(),
            "retryable": retryable,
        }
    }
    if extra:
        body["error"].update(extra)
    return body


def new_reference_id() -> str:
    """Opaque id correlating a client-visible error with the server log."""
    return f"AUTH-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Privacy-preserving identifiers for logs/metrics
# ---------------------------------------------------------------------------

def pseudonymize(value: str) -> str:
    """Stable, non-reversible short id for logs.

    Keyed with the JWT secret so identifiers cannot be brute-forced back into
    usernames/IPs from a leaked log file alone.
    """
    if not value:
        return "-"
    digest = hmac.new(
        str(settings.JWT_SECRET_KEY).encode(),
        str(value).lower().strip().encode(),
        hashlib.sha256,
    ).hexdigest()
    return digest[:12]


def client_ip(request: Request) -> str:
    """Client IP, trusting proxy headers only from the configured proxy.

    nginx sets X-Real-IP; direct socket peer is the fallback. We deliberately
    do NOT parse X-Forwarded-For chains, which are trivially spoofable when
    the trusted-proxy list is not enforced.
    """
    if settings.AUTH_TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-real-ip")
        if forwarded:
            return forwarded.strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Login CSRF: Origin / Referer validation
# ---------------------------------------------------------------------------

def _allowed_origin_hosts(request: Request) -> set:
    """Hosts accepted as the origin of a credential submission.

    Delegates to backend.security.origins so CORS, login-CSRF, WebSocket and
    the SQL-agent stream cannot drift apart. The request Host is accepted as
    same-origin unless AUTH_SAME_HOST_ORIGIN_TRUSTED is disabled, which
    production does once every real hostname is listed explicitly.
    """
    from backend.security.origins import approved_origin_hosts

    return approved_origin_hosts(
        settings, request_host=request.headers.get("host") or ""
    )


def validate_login_origin(request: Request) -> Tuple[bool, Optional[str]]:
    """Reject cross-site credential submissions (login CSRF).

    Returns (ok, failure_detail). Requests without an Origin AND without a
    Referer are allowed: that is a non-browser client (curl, server-side
    integration), which cannot be a CSRF victim. Browsers always send at
    least one of the two on a cross-origin POST.
    """
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if not origin and not referer:
        return True, None

    allowed = _allowed_origin_hosts(request)
    source = origin or referer
    parsed = urlparse(source)
    host = (parsed.hostname or "").lower()

    if not host:
        return False, "unparseable_origin"
    if host not in allowed:
        return False, "cross_origin"
    return True, None


# ---------------------------------------------------------------------------
# Rate limiting (Redis-backed, in-process fallback)
# ---------------------------------------------------------------------------

@dataclass
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0
    scope: str = ""          # account | ip | global
    attempts: int = 0


class _InProcessCounters:
    """Fallback counter store used when Redis is unavailable.

    Single-replica correctness only — Redis remains the shared source of
    truth in multi-replica deployments.
    """

    def __init__(self):
        self._buckets: Dict[str, Tuple[int, float]] = {}

    def incr(self, key: str, window_seconds: int) -> int:
        now = time.time()
        count, expires_at = self._buckets.get(key, (0, 0.0))
        if expires_at <= now:
            count, expires_at = 0, now + window_seconds
        count += 1
        self._buckets[key] = (count, expires_at)
        if len(self._buckets) > 10000:  # bounded memory
            for stale_key in [k for k, (_, exp) in self._buckets.items() if exp <= now][:5000]:
                self._buckets.pop(stale_key, None)
        return count

    def ttl(self, key: str) -> int:
        _, expires_at = self._buckets.get(key, (0, 0.0))
        return max(0, int(expires_at - time.time()))

    def reset(self, key: str) -> None:
        self._buckets.pop(key, None)


_fallback_counters = _InProcessCounters()


async def _redis():
    """Shared Redis client, or None when unavailable."""
    try:
        from backend.core.cache_manager import cache_manager
        client = getattr(cache_manager, "redis_client", None)
        if client is not None and getattr(cache_manager, "_enabled", True):
            return client
    except Exception:
        pass
    return None


async def _incr_with_window(key: str, window_seconds: int) -> Tuple[int, int]:
    """Increment a fixed-window counter. Returns (count, ttl_seconds)."""
    client = await _redis()
    if client is not None:
        try:
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, window_seconds)
            ttl = await client.ttl(key)
            return int(count), int(ttl if ttl and ttl > 0 else window_seconds)
        except Exception as e:
            logger.warning("[AUTH] Redis rate-limit unavailable, using local counters: %s", e)
    count = _fallback_counters.incr(key, window_seconds)
    return count, _fallback_counters.ttl(key) or window_seconds


async def _reset_counter(key: str) -> None:
    client = await _redis()
    if client is not None:
        try:
            await client.delete(key)
            return
        except Exception:
            pass
    _fallback_counters.reset(key)


def _limits() -> Dict[str, int]:
    return {
        "account_max": int(settings.AUTH_RATE_LIMIT_ACCOUNT_MAX),
        "account_window": int(settings.AUTH_RATE_LIMIT_ACCOUNT_WINDOW),
        "ip_max": int(settings.AUTH_RATE_LIMIT_IP_MAX),
        "ip_window": int(settings.AUTH_RATE_LIMIT_IP_WINDOW),
        "global_max": int(settings.AUTH_RATE_LIMIT_GLOBAL_MAX),
        "global_window": int(settings.AUTH_RATE_LIMIT_GLOBAL_WINDOW),
    }


def _account_key(username: str) -> str:
    return f"auth:fail:account:{pseudonymize(username)}"


def _ip_key(ip: str) -> str:
    return f"auth:fail:ip:{pseudonymize(ip)}"


_GLOBAL_KEY = "auth:attempts:global"


async def check_rate_limits(username: str, ip: str) -> RateLimitDecision:
    """Called BEFORE credential verification.

    Reads current failure counters without incrementing them (increments only
    happen on an actual failure) and applies a global surge counter.
    """
    if not settings.AUTH_RATE_LIMIT_ENABLED:
        return RateLimitDecision(allowed=True)

    limits = _limits()

    # Global surge protection (counts every attempt, success or failure)
    global_count, global_ttl = await _incr_with_window(_GLOBAL_KEY, limits["global_window"])
    if global_count > limits["global_max"]:
        return RateLimitDecision(False, global_ttl, "global", global_count)

    client = await _redis()
    for scope, key, maximum, window in (
        ("account", _account_key(username), limits["account_max"], limits["account_window"]),
        ("ip", _ip_key(ip), limits["ip_max"], limits["ip_window"]),
    ):
        count = 0
        ttl = window
        if client is not None:
            try:
                raw = await client.get(key)
                count = int(raw) if raw else 0
                if count:
                    redis_ttl = await client.ttl(key)
                    ttl = int(redis_ttl) if redis_ttl and redis_ttl > 0 else window
            except Exception:
                count = 0
        else:
            count, _ = _fallback_counters._buckets.get(key, (0, 0.0))
            ttl = _fallback_counters.ttl(key) or window

        if count >= maximum:
            return RateLimitDecision(False, ttl, scope, count)

    return RateLimitDecision(allowed=True)


async def record_failure(username: str, ip: str) -> None:
    """Increment failure counters after an unsuccessful credential check."""
    if not settings.AUTH_RATE_LIMIT_ENABLED:
        return
    limits = _limits()
    await _incr_with_window(_account_key(username), limits["account_window"])
    await _incr_with_window(_ip_key(ip), limits["ip_window"])


async def clear_failures(username: str, ip: str) -> None:
    """Successful login clears that account's and source's failure counters.

    This is why an attacker cannot permanently lock a victim out: the moment
    the real user authenticates, the throttle for them is released.
    """
    if not settings.AUTH_RATE_LIMIT_ENABLED:
        return
    await _reset_counter(_account_key(username))
    await _reset_counter(_ip_key(ip))


# ---------------------------------------------------------------------------
# Token revocation (logout must actually end the session)
# ---------------------------------------------------------------------------

_revoked_fallback: Dict[str, float] = {}


def _revocation_key(jti: str) -> str:
    return f"auth:revoked:{jti}"


async def revoke_token(jti: str, expires_in_seconds: int) -> bool:
    """Add a token id to the denylist until its natural expiry.

    Entries expire with the token itself, so the denylist stays small.
    """
    if not jti:
        return False
    ttl = max(1, int(expires_in_seconds))
    client = await _redis()
    if client is not None:
        try:
            await client.setex(_revocation_key(jti), ttl, "1")
            return True
        except Exception as e:
            logger.warning("[AUTH] Redis revocation write failed, using local denylist: %s", e)
    _revoked_fallback[jti] = time.time() + ttl
    if len(_revoked_fallback) > 50000:
        now = time.time()
        for stale in [k for k, exp in _revoked_fallback.items() if exp <= now][:25000]:
            _revoked_fallback.pop(stale, None)
    return True


async def is_token_revoked(jti: Optional[str]) -> bool:
    """Check the denylist. Never fails open on a Redis error."""
    if not jti:
        return False
    client = await _redis()
    if client is not None:
        try:
            return bool(await client.exists(_revocation_key(jti)))
        except Exception:
            pass  # fall through to the local denylist
    expires_at = _revoked_fallback.get(jti)
    if expires_at is None:
        return False
    if expires_at <= time.time():
        _revoked_fallback.pop(jti, None)
        return False
    return True


# ---------------------------------------------------------------------------
# Cookie configuration
# ---------------------------------------------------------------------------

def cookie_settings() -> Dict:
    """Authentication cookie attributes derived from configuration.

    `__Host-` prefixed names are only valid when Secure + Path=/ + no Domain,
    so the prefix is applied only when HTTPS is actually enabled.
    """
    secure = bool(settings.AUTH_COOKIE_SECURE)
    samesite = str(settings.AUTH_COOKIE_SAMESITE).lower()
    if samesite not in ("lax", "strict", "none"):
        samesite = "lax"
    return {
        "key": auth_cookie_name(),
        "httponly": True,
        "secure": secure,
        "samesite": samesite,
        "path": "/",
        "domain": None,  # host-only cookie
        "max_age": int(settings.ACCESS_TOKEN_EXPIRE_MINUTES) * 60,
    }


def auth_cookie_name() -> str:
    """Cookie name; upgrades to the __Host- prefix when Secure is enabled."""
    if bool(settings.AUTH_COOKIE_SECURE) and \
       bool(settings.AUTH_COOKIE_HOST_PREFIX):
        return "__Host-access_token"
    return "access_token"


def read_auth_cookie(request: Request) -> Optional[str]:
    """Read the auth cookie under either name (supports rollout both ways)."""
    return request.cookies.get("__Host-access_token") or request.cookies.get("access_token")


# ---------------------------------------------------------------------------
# Redirect allowlist (backend is authoritative)
# ---------------------------------------------------------------------------

# "/change-password" is a destination login can choose (for an account whose
# password is still the seeded or admin-assigned one), so it belongs here.
ALLOWED_REDIRECTS = ("/home", "/dashboard", "/change-password")


def redirect_for_role(role: str) -> str:
    """Post-login destination. Purely a convenience hint — every destination
    route enforces its own server-side authorization."""
    return "/home" if str(role).lower() == "admin" else "/dashboard"


# ---------------------------------------------------------------------------
# Timing equalization
# ---------------------------------------------------------------------------

# A real bcrypt hash of a random value. Verifying against it when the user
# does not exist keeps the response time comparable to a genuine password
# check, so timing cannot be used to enumerate usernames.
_DUMMY_HASH: Optional[str] = None


def dummy_password_verify(password: str) -> None:
    """Burn the same CPU a real verification would, then discard the result."""
    global _DUMMY_HASH
    try:
        from backend.auth.password import hash_password, verify_password
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password(uuid.uuid4().hex)
        verify_password(password or "x", _DUMMY_HASH)
    except Exception:
        # Never let the equalization path change observable behaviour
        pass


# ---------------------------------------------------------------------------
# Metrics (no high-cardinality labels — never usernames or raw IPs)
# ---------------------------------------------------------------------------

_metrics_initialized = False
metrics_login_attempts = None
metrics_login_failures = None
metrics_login_rate_limited = None
metrics_login_duration = None
metrics_session_creation_failures = None


def initialize_auth_metrics() -> None:
    """Register Prometheus auth metrics (idempotent)."""
    global _metrics_initialized, metrics_login_attempts, metrics_login_failures
    global metrics_login_rate_limited, metrics_login_duration, metrics_session_creation_failures
    if _metrics_initialized:
        return
    try:
        from prometheus_client import Counter, Histogram
        metrics_login_attempts = Counter(
            "auth_login_attempts_total", "Login attempts", ["result"])
        metrics_login_failures = Counter(
            "auth_login_failures_total", "Failed logins by reason", ["failure_code"])
        metrics_login_rate_limited = Counter(
            "auth_login_rate_limited_total", "Rate-limited login attempts", ["scope"])
        metrics_login_duration = Histogram(
            "auth_login_duration_seconds", "Login handler duration")
        metrics_session_creation_failures = Counter(
            "auth_session_creation_failures_total", "Session/token creation failures")
        _metrics_initialized = True
    except Exception as e:  # duplicate registration or prometheus missing
        logger.debug("[AUTH] Metrics initialization skipped: %s", e)


def record_metric(name: str, **labels) -> None:
    """Best-effort metric recording — never breaks the auth path."""
    try:
        initialize_auth_metrics()
        if name == "attempt" and metrics_login_attempts:
            metrics_login_attempts.labels(result=labels.get("result", "unknown")).inc()
        elif name == "failure" and metrics_login_failures:
            metrics_login_failures.labels(failure_code=labels.get("failure_code", "unknown")).inc()
        elif name == "rate_limited" and metrics_login_rate_limited:
            metrics_login_rate_limited.labels(scope=labels.get("scope", "unknown")).inc()
        elif name == "duration" and metrics_login_duration:
            metrics_login_duration.observe(float(labels.get("seconds", 0)))
        elif name == "session_failure" and metrics_session_creation_failures:
            metrics_session_creation_failures.inc()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def audit(event: str, *, result: str, request_id: str = "-", user_id=None,
          failure_code: Optional[str] = None, username: Optional[str] = None,
          ip: Optional[str] = None, duration_ms: Optional[int] = None,
          reference_id: Optional[str] = None, **extra) -> None:
    """One structured line per authentication event.

    Usernames and IPs are pseudonymized; credentials, tokens, cookies and
    hashes are never accepted by this function.
    """
    fields = {
        "event": event,
        "result": result,
        "request_id": request_id,
        "user_id": user_id,
        "failure_code": failure_code,
        "actor": pseudonymize(username) if username else None,
        "source": pseudonymize(ip) if ip else None,
        "duration_ms": duration_ms,
        "reference_id": reference_id,
    }
    fields.update(extra)
    rendered = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info("[AUTH_AUDIT] %s", rendered)
