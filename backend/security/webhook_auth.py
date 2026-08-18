"""Credential checking for the pipeline ingest webhook.

The webhook shipped with **no authentication of any kind**: no dependency, no
API key, no allowlist. `backend/main.py` installs only logging and CORS
middleware, and nginx rate-limits `^/(api/)?webhook/` without authenticating it.
Anyone able to reach the port could enqueue frames, create identities, poison an
existing person's embeddings and fill face storage.

**A shared key set, not per-pipeline keys.** There is no provisioning moment to
mint a per-pipeline secret at: `batch_writer` inserts the `pipelines` row on the
*first detection*, so a per-pipeline key would mean either trust-on-first-use
(no security at all) or a mandatory admin pre-registration step that changes
deployment from "point a camera at the URL" into a two-party workflow. A key
*set* solves the operational problem a single secret has — rotation — without a
schema change. The wire format here does not preclude binding a key to a
pipeline later.

Reading config exclusively through `getattr(cfg, ...)` mirrors `config_guard`,
so tests can pass a plain SimpleNamespace with no environment or container.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MODE_ENFORCE = "enforce"
MODE_LOG_ONLY = "log_only"
MODE_OFF = "off"
VALID_MODES = (MODE_ENFORCE, MODE_LOG_ONLY, MODE_OFF)

DEFAULT_HEADER = "X-Webhook-Key"

# Below this a key is guessable at the request rates nginx already permits
# (burst=120). config_guard rejects anything shorter in production.
MIN_KEY_LENGTH = 32

# Keys that are published in this repository and must never reach production.
#
# Stored as digests rather than literals: the point is to REJECT these values,
# and writing them out again would put a working development credential into a
# second file that greps and secret scanners then have to be taught to ignore.
# The development compose stack commits a key on purpose (so the enforcement
# tests exercise a real 401/202 split); this is what stops that key being
# inherited by a production deployment that copied the stanza.
PUBLISHED_KEY_DIGESTS = frozenset({
    # docker/docker-compose.cpu.yml — the development ingest key
    "dbf734483bc6a3e26d520e0e07be12a3444e11327ea7afb0c6885ab6d9a2283d",
})


def published_keys(cfg) -> Sequence[int]:
    """Positions of configured keys that are published in this repository."""
    return tuple(
        index for index, key in enumerate(parse_keys(getattr(cfg, "WEBHOOK_API_KEYS", "")))
        if hashlib.sha256(key.encode("utf-8")).hexdigest() in PUBLISHED_KEY_DIGESTS)


def parse_keys(raw: Optional[str]) -> Tuple[str, ...]:
    """Comma-separated keys -> a deduplicated tuple, order preserved.

    Multiple keys are the rotation story: append the new one, roll it out to the
    cameras, then drop the old one — with no window where either is rejected.
    """
    if not raw:
        return ()
    seen = []
    for part in str(raw).split(","):
        candidate = part.strip()
        if candidate and candidate not in seen:
            seen.append(candidate)
    return tuple(seen)


def key_digests(cfg) -> Tuple[bytes, ...]:
    """SHA-256 of each configured key."""
    raw = getattr(cfg, "WEBHOOK_API_KEYS", "") or ""
    return tuple(hashlib.sha256(k.encode("utf-8")).digest() for k in parse_keys(raw))


def auth_mode(cfg) -> str:
    """The configured mode, normalized.

    An unrecognized value resolves to `enforce`, never to a permissive mode — a
    typo must fail closed. config_guard rejects the typo outright, in *every*
    environment, so this default is a backstop rather than the real handling.
    """
    raw = str(getattr(cfg, "WEBHOOK_AUTH_MODE", MODE_ENFORCE) or MODE_ENFORCE)
    mode = raw.strip().lower()
    return mode if mode in VALID_MODES else MODE_ENFORCE


def header_name(cfg) -> str:
    return str(getattr(cfg, "WEBHOOK_AUTH_HEADER", DEFAULT_HEADER)
               or DEFAULT_HEADER).strip()


def present_key(request, cfg) -> Optional[str]:
    """Extract the presented credential from the request.

    Accepted: the configured header, or `Authorization: Bearer <key>`.

    **Never a query parameter.** nginx logs the request line, gunicorn logs the
    request line, and `backend/main.py`'s access-log middleware logs the path —
    a credential in the query string lands in three logs on the first request.
    """
    try:
        headers = request.headers
    except AttributeError:                                   # pragma: no cover
        return None

    # `WEBHOOK_AUTH_HEADER=Authorization` is a configuration an operator can
    # reasonably reach for after reading "senders use Authorization: Bearer".
    # Without this branch, headers.get("Authorization") returns the whole string
    # "Bearer <token>", that becomes the presented credential, and EVERY request
    # 401s — reported as `invalid` rather than anything that points at the cause.
    # Fall through to the Bearer parse below instead.
    name = header_name(cfg)
    if name.strip().lower() != "authorization":
        value = headers.get(name)
        if value and value.strip():
            return value.strip()

    authorization = headers.get("authorization") or ""
    if authorization[:7].lower() == "bearer ":
        candidate = authorization[7:].strip()
        if candidate:
            return candidate
    return None


def verify_key(presented: Optional[str], cfg) -> bool:
    """Constant-time membership test against the configured key set.

    Hashing before comparison is deliberate: every comparison then operates on
    32 fixed-length bytes, so neither the length of a configured key nor an
    early length mismatch inside compare_digest is observable through timing.
    It also makes arbitrary client bytes safe to handle.
    """
    if not presented:
        return False
    digests = key_digests(cfg)
    if not digests:
        return False
    candidate = hashlib.sha256(presented.encode("utf-8", "replace")).digest()
    matched = False
    for digest in digests:
        if hmac.compare_digest(candidate, digest):
            matched = True
    return matched


def match_digest(presented: Optional[str], labelled_digests) -> Optional[str]:
    """Constant-time membership test that reports WHICH entry matched.

    Same discipline as verify_key above — hash first so every comparison is over
    32 fixed-length bytes, and never short-circuit, so the POSITION of a match is
    not observable through timing either. Returns the matching label, or None.

    This exists so issued database credentials and environment keys can be
    checked in ONE loop: an env key and a DB credential then cost the same, and
    the loop length depends only on how many credentials exist, never on the
    presented bytes. Kept here, next to verify_key, because it is pure — the
    caller supplies the digests, so this module stays constructible from a bare
    SimpleNamespace with no database and no container.
    """
    if not presented:
        return None
    candidate = hashlib.sha256(presented.encode("utf-8", "replace")).digest()
    matched = None
    for digest, label in labelled_digests:
        if hmac.compare_digest(candidate, digest):
            matched = label
    return matched


def keys_configured(cfg) -> bool:
    return bool(key_digests(cfg))


def weak_keys(cfg) -> Sequence[Tuple[int, str]]:
    """(index, reason) for each configured key that is too weak to ship.

    Returns positions, never values — this feeds a report that must not leak a
    secret. Reuses config_guard's strength assessment rather than inventing a
    second, divergent notion of "strong enough".
    """
    from backend.security.config_guard import assess_secret_strength

    problems = []
    for index, key in enumerate(parse_keys(getattr(cfg, "WEBHOOK_API_KEYS", ""))):
        if len(key) < MIN_KEY_LENGTH:
            problems.append((index, f"only {len(key)} characters "
                                    f"(need {MIN_KEY_LENGTH}+)"))
            continue
        reasons = assess_secret_strength(key)
        if reasons:
            problems.append((index, "; ".join(reasons)))
    return tuple(problems)


def reused_keys(cfg) -> Sequence[int]:
    """Positions of keys that duplicate another secret in the configuration.

    A camera credential is distributed to every camera and is therefore the
    least-protected secret in the deployment; it must not also be the value that
    signs sessions or opens the database.
    """
    others = set()
    for name in ("JWT_SECRET_KEY", "POSTGRES_PASSWORD"):
        value = getattr(cfg, name, "") or ""
        if value:
            others.add(str(value))
    return tuple(i for i, key in enumerate(parse_keys(
        getattr(cfg, "WEBHOOK_API_KEYS", ""))) if key in others)
