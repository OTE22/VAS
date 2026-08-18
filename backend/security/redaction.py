"""Redaction helpers for anything that may reach a log, a report, or a response.

Stdlib + SQLAlchemy only; no import of `config`.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit, urlunsplit

# Settings whose values must never be rendered, anywhere, for any reason.
SECRET_SETTINGS = frozenset({
    "JWT_SECRET_KEY",
    "JWT_SECRET_KEY_FILE",
    "POSTGRES_PASSWORD",
    "POSTGRES_PASSWORD_FILE",
    "DATABASE_URL",
    "DATABASE_URL_FILE",
    "REDIS_URL",
    "REDIS_URL_FILE",
    "REDIS_PASSWORD",
    "BOOTSTRAP_ADMIN_PASSWORD",
    "BOOTSTRAP_ADMIN_PASSWORD_FILE",
    "OLLAMA_API_KEY",
    # Distributed to every camera, so the least-protected secret in the
    # deployment — and the one most likely to end up pasted into a support
    # ticket if a report ever renders it.
    "WEBHOOK_API_KEYS",
    "WEBHOOK_API_KEYS_FILE",
    # The bearer alias. Its value is folded into WEBHOOK_API_KEYS at startup,
    # but the FIELD still exists and would otherwise be rendered verbatim by
    # /api/settings and the config-guard report.
    "WEBHOOK_AUTH_TOKEN",
    "WEBHOOK_AUTH_TOKEN_FILE",
})


def redact_database_url(database_url: str) -> str:
    """Render a database URL without exposing the password."""
    if not database_url:
        return "<unset>"
    try:
        from sqlalchemy.engine.url import make_url

        return make_url(database_url).render_as_string(hide_password=True)
    except Exception:
        if "@" in database_url:
            return f"***@{database_url.rsplit('@', 1)[1]}"
        return "<database-url-hidden>"


def redact_url(url: str) -> str:
    """Strip credentials from any URL (Redis, Ollama, webhooks).

    Unlike redact_database_url this does not depend on a SQLAlchemy dialect
    string, so it works for redis:// and http:// URLs.
    """
    if not url:
        return "<unset>"
    try:
        parts = urlsplit(url)
        if not parts.netloc:
            return "<url-hidden>"
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        if parts.username or parts.password:
            host = f"***@{host}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:
        return "<url-hidden>"


def url_password(url: str) -> Optional[str]:
    """Extract the password from a URL, for policy checks only.

    Callers must compare the result — never log or render it.
    """
    if not url:
        return None
    try:
        return urlsplit(url).password
    except Exception:
        return None


def url_username(url: str) -> Optional[str]:
    """Extract the username from a URL, for policy checks only."""
    if not url:
        return None
    try:
        return urlsplit(url).username
    except Exception:
        return None
