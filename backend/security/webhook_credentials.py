"""Issued ingest credentials — the database-backed half of webhook auth.

`webhook_auth` stays pure: it reads config through getattr and never imports a
database, so its tests can hand it a bare SimpleNamespace. Everything that needs
the `webhook_credentials` table lives here instead, and the two meet in
`webhook_auth.match_digest`, which is given both digest sets and checks them in
one constant-time loop.

WHY A CACHE. `require_webhook_key` runs on every ingest frame. At 50 cameras a
per-request SELECT would be ~50 queries/second against a table with tens of rows,
competing for the same connection pool the ingest writes use. Instead each worker
holds a snapshot and refreshes it at most once per TTL.

WHAT THE CACHE COSTS YOU. The TTL *is* the revocation latency, and it is
per-worker: gunicorn runs several processes, a DELETE lands in exactly one of
them, so `invalidate()` only helps that one. A revoked credential therefore keeps
working for up to WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS on the other workers. That
is stated in the DELETE response, in the admin UI and in the runbook rather than
being quietly rounded down to "revocation is instant". For immediate revocation
the honest answer is the break-glass path: rotate the env key and restart.

FAIL CLOSED. A refresh that raises keeps the PREVIOUS snapshot and advances the
clock so a broken database is not retried on every frame. There is deliberately
no path here that returns "accept everything" — a database outage degrades to
"issued credentials stop working, environment keys keep working", never to open
ingest.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from backend.security import webhook_auth

logger = logging.getLogger(__name__)

# tuple[(digest_bytes, name, token_hash_hex)] — replaced by a single attribute
# assignment, which is atomic under the GIL, so readers never observe a
# half-built cache and the read path needs no lock at all.
_snapshot: Tuple[Tuple[bytes, str, str], ...] = ()
_loaded_at: float = 0.0
_ever_loaded: bool = False
_refresh_lock: Optional[asyncio.Lock] = None
_refreshing: bool = False

# token_hash -> last seen (naive UTC). Written per frame, flushed per TTL.
_pending_last_used: Dict[str, datetime] = {}

# Names already logged in this process, so attribution costs one line per
# credential rather than one per frame. Same warn-once shape as
# _UNENFORCED_WARNED in backend/routes/webhook.py.
_first_use_logged: set = set()

def ttl_seconds() -> int:
    """The credential cache TTL, and therefore the revocation latency.

    Reads the declared field directly with no fallback: config.py owns the
    default, and a `getattr(settings, ..., 30)` here would be a second copy of
    it that drifts silently the day config.py changes. The single accessor also
    keeps the admin API from re-deriving the number it advertises to operators.
    """
    from config import settings
    return int(settings.WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS)


def _lock() -> asyncio.Lock:
    # Created lazily: importing this module must not require a running loop.
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


async def _refresh() -> None:
    """Flush pending last_used stamps, then reload the snapshot. One session."""
    global _snapshot, _loaded_at, _ever_loaded, _refreshing
    _refreshing = True
    try:
        from db_connection import db_manager
        from sqlalchemy import text as sa_text

        async with db_manager.get_session() as db:
            # Flush FIRST, in the same transaction as the reload, so the admin
            # list is never staler than the stamps it displays.
            pending = dict(_pending_last_used)
            if pending:
                _pending_last_used.clear()
                for token_hash, seen_at in pending.items():
                    # `last_used_at < :seen` keeps a slow worker's late flush
                    # from moving the timestamp backwards. A credential deleted
                    # between the frame and the flush matches zero rows, which
                    # is the same consumed-by-delete ergonomics as revocation.
                    await db.execute(sa_text(
                        "UPDATE webhook_credentials SET last_used_at = :seen "
                        "WHERE token_hash = :h "
                        "AND (last_used_at IS NULL OR last_used_at < :seen)"
                    ), {"seen": seen_at, "h": token_hash})
                await db.commit()

            result = await db.execute(sa_text(
                "SELECT token_hash, name FROM webhook_credentials"
            ))
            rows = result.fetchall()

        snapshot = []
        for token_hash, name in rows:
            try:
                snapshot.append((bytes.fromhex(token_hash), name, token_hash))
            except (ValueError, TypeError):
                # A malformed hash can never match anything anyway; skipping it
                # keeps one bad row from breaking every other credential.
                logger.warning("[WEBHOOK] ingest credential %r has an unusable "
                               "token_hash and was skipped", name)

        _snapshot = tuple(snapshot)
        live = {name for _d, name, _h in _snapshot}
        # Prune, so a re-issued name logs its first use again — which is exactly
        # the event an operator wants to see.
        _first_use_logged.intersection_update(live)
    except Exception as e:
        # Keep the previous snapshot. Never widen access on failure.
        logger.warning("[WEBHOOK] issued-credential refresh failed, serving the "
                       "previous snapshot (%d credentials): %s", len(_snapshot), e)
    finally:
        _loaded_at = time.monotonic()
        _ever_loaded = True
        _refreshing = False


async def ensure_fresh() -> None:
    """Make the snapshot usable, without ever blocking the hot path on I/O.

    Cold start awaits one query under a lock, so a burst of concurrent first
    requests produces exactly one SELECT. After that it is
    stale-while-revalidate: past the TTL a refresh is spawned and the CURRENT
    snapshot is served immediately, so no ingest frame ever waits on the
    database.
    """
    if not _ever_loaded:
        async with _lock():
            if not _ever_loaded:
                await _refresh()
        return

    if not _refreshing and (time.monotonic() - _loaded_at) > ttl_seconds():
        try:
            asyncio.create_task(_refresh())
        except RuntimeError:
            # No running loop (sync context). The snapshot stays valid.
            pass


def match(presented: Optional[str], cfg) -> Optional[str]:
    """Return the label of the matching credential, or None.

    Environment keys and issued credentials are concatenated into ONE
    non-short-circuiting loop, so which set matched is not observable through
    timing, and neither is the position within either set.
    """
    labelled: List[Tuple[bytes, str]] = [
        (digest, f"env#{i + 1}")
        for i, digest in enumerate(webhook_auth.key_digests(cfg))
    ]
    labelled.extend((digest, name) for digest, name, _h in _snapshot)
    return webhook_auth.match_digest(presented, labelled)


def note_use(label: str) -> None:
    """Record a successful authentication. A dict write, never a query."""
    for _digest, name, token_hash in _snapshot:
        if name == label:
            _pending_last_used[token_hash] = datetime.utcnow()
            break


def first_use(label: str) -> bool:
    """True the first time this process sees `label`. Used to log once."""
    if label in _first_use_logged:
        return False
    _first_use_logged.add(label)
    return True


def any_cached() -> bool:
    """Whether any issued credential exists.

    Load-bearing: `require_webhook_key` may only take its unauthenticated
    development branch when BOTH this and the environment key set are empty.
    """
    return bool(_snapshot)


def invalidate() -> None:
    """Force the next ensure_fresh() to reload BLOCKINGLY. Only this worker.

    Clearing _ever_loaded, not just the timestamp, is deliberate. The normal
    expiry path is stale-while-revalidate: it serves the current snapshot and
    refreshes behind it, which is right for a TTL tick but wrong here. After a
    mint, the stale snapshot is precisely the one that does NOT contain the new
    credential, so serving it once means the first frame an operator sends with
    a freshly issued token is rejected — they hand over a token that appears
    broken. Taking the cold path instead makes issuance and revocation take
    effect immediately on the worker that handled the admin request, at the cost
    of one awaited query on one request. The other workers still pick the change
    up on their own TTL, which is the latency the API response advertises.
    """
    global _loaded_at, _ever_loaded
    _loaded_at = 0.0
    _ever_loaded = False


def reset_for_tests() -> None:
    global _snapshot, _loaded_at, _ever_loaded, _refreshing
    _snapshot = ()
    _loaded_at = 0.0
    _ever_loaded = False
    _refreshing = False
    _pending_last_used.clear()
    _first_use_logged.clear()
