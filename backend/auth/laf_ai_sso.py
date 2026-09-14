"""One-time tickets that hand a signed-in VAS user to the LAF-AI chatbot.

The chatbot (https://armyeye-chatbot) is a different host name, so the VAS
session cookie never reaches it. TRACKING therefore asks this module for a
ticket, sends the browser to the chatbot's gate with it, and the gate consumes
the ticket here — server to server — receiving the identity plus a fresh VAS
access token for that user, which is what its own session then validates
against ``/api/auth/me`` on every request. No second identity store, no
password ever leaves VAS.

Ticket properties: 256 bits of randomness, opaque to the browser, stored in
Redis only as its SHA-256 hash, ``LAF_AI_SSO_TICKET_TTL_SECONDS`` (60 s) of
life, and consumed with an atomic get-and-delete so it can be redeemed exactly
once. The token minted at consumption is linked to the browser's VAS session
(``parent_jti``) so that VAS logout can revoke both — see ``routes/auth.logout``.

Redis is the only store: with several gunicorn workers an in-process dict could
not be consumed by a worker other than the one that issued it. Without Redis
the hand-off reports itself unavailable (fail closed) and TRACKING falls back
to the chatbot's own sign-in page.
"""
import hashlib
import json
import logging
import re
import secrets
import time
from typing import Any, Dict, Optional

from fastapi import Request

from config import settings
from backend.auth.auth_security import _redis

logger = logging.getLogger(__name__)

TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{32,96}$")
SSO_SECRET_HEADER = "x-laf-ai-sso-secret"
GATE_REVOKE_TIMEOUT_SECONDS = 2.0

# Atomic GET + DEL: the ticket can only ever be redeemed once, whichever worker
# or process gets there first. Works on every Redis version (no GETDEL needed).
_GETDEL_LUA = "local v = redis.call('GET', KEYS[1]); if v then redis.call('DEL', KEYS[1]) end; return v"


class SSOUnavailable(RuntimeError):
    """Redis is not reachable: tickets cannot be issued or consumed safely."""


def _ticket_key(ticket: str) -> str:
    return "auth:laf-ai:ticket:" + hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def _child_key(parent_jti: str) -> str:
    return f"auth:laf-ai:child:{parent_jti}"


def chatbot_url(path: str = "") -> str:
    return settings.LAF_AI_CHATBOT_URL.rstrip("/") + path


async def _client():
    client = await _redis()
    if client is None:
        raise SSOUnavailable("redis unavailable")
    return client


async def issue_ticket(*, user_id: int, username: str, display_name: Optional[str],
                       role: str, parent_jti: Optional[str]) -> str:
    """Create a single-use ticket for an already-authenticated user."""
    client = await _client()
    ttl = max(5, int(settings.LAF_AI_SSO_TICKET_TTL_SECONDS))
    ticket = secrets.token_urlsafe(32)
    now = int(time.time())
    payload = {
        "user_id": int(user_id),
        "username": username,
        "display_name": display_name,
        "role": role,
        "parent_jti": parent_jti,
        "issued_at": now,
        "expires_at": now + ttl,
        "nonce": secrets.token_hex(8),
    }
    try:
        await client.setex(_ticket_key(ticket), ttl, json.dumps(payload))
    except Exception as exc:  # redis hiccup: fail closed, never issue an unstorable ticket
        logger.warning("[SSO] ticket store failed: %s", type(exc).__name__)
        raise SSOUnavailable(str(exc)) from exc
    return ticket


async def consume_ticket(ticket: Any) -> Optional[Dict[str, Any]]:
    """Redeem a ticket exactly once. ``None`` for malformed, unknown, used or expired."""
    if not isinstance(ticket, str) or not TICKET_RE.match(ticket):
        return None
    client = await _client()
    try:
        raw = await client.eval(_GETDEL_LUA, 1, _ticket_key(ticket))
    except Exception as exc:
        logger.warning("[SSO] ticket consume failed: %s", type(exc).__name__)
        raise SSOUnavailable(str(exc)) from exc
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if int(payload.get("expires_at", 0)) < int(time.time()):
        return None  # belt and braces: Redis TTL already dropped it
    return payload


async def link_child_session(parent_jti: Optional[str], child_jti: str, ttl_seconds: int) -> None:
    """Remember which chatbot token belongs to which VAS browser session."""
    if not parent_jti or not child_jti:
        return
    try:
        client = await _client()
        await client.setex(_child_key(parent_jti), max(1, int(ttl_seconds)), child_jti)
    except Exception as exc:  # logout will still end the chatbot side through the gate's re-check
        logger.warning("[SSO] child-session link not stored: %s", type(exc).__name__)


async def pop_child_session(parent_jti: Optional[str]) -> Optional[str]:
    if not parent_jti:
        return None
    try:
        client = await _client()
        value = await client.eval(_GETDEL_LUA, 1, _child_key(parent_jti))
    except Exception as exc:
        logger.warning("[SSO] child-session lookup failed: %s", type(exc).__name__)
        return None
    if value is None:
        return None
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)


async def notify_gate_revoked(*, child_jti: Optional[str], parent_jti: Optional[str]) -> bool:
    """Tell the chatbot gate to drop the session now (its own re-check would
    catch the revoked token within seconds anyway). Best effort, never raises."""
    if not (child_jti or parent_jti):
        return False
    url = settings.LAF_AI_GATE_URL.rstrip("/") + "/_gate/revoke"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=GATE_REVOKE_TIMEOUT_SECONDS) as http:
            resp = await http.post(url, json={"jti": child_jti, "parent_jti": parent_jti})
        return resp.status_code in (200, 204)
    except Exception as exc:
        logger.info("[SSO] gate revoke notification skipped: %s", type(exc).__name__)
        return False


def came_through_public_proxy(request: Request) -> bool:
    """The VAS nginx stamps X-Forwarded-For on everything it proxies; a direct
    call on the docker network (the gate) carries none. Used to keep the
    gate-only endpoints off the public surface."""
    return bool(request.headers.get("x-forwarded-for"))


def presented_secret_ok(request: Request) -> bool:
    """When LAF_AI_SSO_SECRET is configured the gate must present it."""
    expected = settings.LAF_AI_SSO_SECRET or ""
    if not expected:
        return True
    presented = request.headers.get(SSO_SECRET_HEADER, "")
    return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
