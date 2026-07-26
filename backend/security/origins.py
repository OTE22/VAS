"""The single approved-origin policy.

CORS, login-CSRF validation, WebSocket handshakes and the SQL-agent stream all
resolve through here so they can never drift apart. `config` is imported lazily
inside functions, never at module scope.
"""

from __future__ import annotations

import json
from typing import Iterable, List, Optional, Set
from urllib.parse import urlparse

# Hosts that are always same-origin for a local request.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def parse_origins(raw) -> List[str]:
    """Parse an origin setting into a de-duplicated, order-preserving list.

    Accepts every shape this deployment has produced: a JSON array
    ('["*"]', which Docker Compose passes), a comma-separated string, a bare
    origin, an empty string, or an actual list.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items: Iterable = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                decoded = json.loads(text)
                items = decoded if isinstance(decoded, list) else [decoded]
            except (ValueError, TypeError):
                items = text[1:-1].split(",")
        else:
            items = text.split(",")

    origins: List[str] = []
    for item in items:
        value = str(item).strip().strip("'\"").rstrip("/")
        if value and value not in origins:
            origins.append(value)
    return origins


def origin_host(origin: str) -> str:
    """Hostname of an origin, tolerating bare 'host' and 'host:port' forms."""
    if not origin:
        return ""
    candidate = origin if "//" in origin else f"//{origin}"
    try:
        return (urlparse(candidate).hostname or origin).lower()
    except ValueError:
        return origin.lower()


def origin_hosts(origins: Iterable[str]) -> Set[str]:
    return {host for host in (origin_host(o) for o in origins) if host}


def _cfg(cfg=None):
    if cfg is not None:
        return cfg
    from config import settings

    return settings


def approved_origins(cfg=None) -> List[str]:
    """Origins permitted to carry credentials.

    AUTH_ALLOWED_ORIGINS wins when set; otherwise the non-wildcard CORS entries
    are used so a deployment that configured only CORS still gets a coherent
    policy.
    """
    cfg = _cfg(cfg)
    explicit = parse_origins(getattr(cfg, "AUTH_ALLOWED_ORIGINS", ""))
    if explicit:
        return explicit
    return [o for o in parse_origins(getattr(cfg, "CORS_ORIGINS", "")) if o != "*"]


def approved_origin_hosts(
    cfg=None,
    *,
    request_host: Optional[str] = None,
) -> Set[str]:
    """Hostnames accepted as the origin of a credentialed request.

    The host the request was actually sent to is same-origin by definition, so
    it is accepted unless AUTH_SAME_HOST_ORIGIN_TRUSTED is disabled. Production
    disables it once AUTH_ALLOWED_ORIGINS lists every real hostname, which
    closes the gap where an attacker-controlled Host header would self-approve.
    """
    cfg = _cfg(cfg)
    hosts = origin_hosts(approved_origins(cfg))

    if request_host and bool(getattr(cfg, "AUTH_SAME_HOST_ORIGIN_TRUSTED", True)):
        bare = request_host.split(":")[0].strip().lower()
        if bare:
            hosts.add(bare)

    return hosts


def is_approved_origin(
    origin: Optional[str],
    *,
    request_host: Optional[str] = None,
    cfg=None,
) -> bool:
    """True when `origin` may submit credentials.

    An absent origin is allowed — non-browser clients omit it, and browsers
    always send it on cross-origin requests. Callers that need to distinguish
    must check for None themselves.
    """
    if not origin:
        return True
    host = origin_host(origin)
    if not host:
        return False
    return host in approved_origin_hosts(cfg, request_host=request_host)
