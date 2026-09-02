"""
SQL Agent API Routes
====================
FastAPI router for SQL Intelligence Agent endpoints.
"""

import asyncio
import json
import logging
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Optional, Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Depends, Request, status
from fastapi.responses import JSONResponse, StreamingResponse, Response, FileResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator
from io import BytesIO
import re

# Add parent directory to path for auth imports
import os
import sys
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

try:
    from backend.auth.auth_service import get_current_user, require_chatbot_access
    from db_models import User
    # db_manager is used directly (not via the get_db dependency) wherever a
    # handler opens its OWN short-lived session: `async with
    # db_manager.get_session()` is a real context manager, so the connection is
    # returned deterministically. The previous `async for db in get_db()` idiom
    # left the async generator suspended on `break`/`return`, deferring cleanup
    # to garbage collection and holding pooled connections longer than the work.
    from db_connection import get_db, db_manager
    from sql_agent.services.user_query_history_service import user_query_history_service
    AUTH_AVAILABLE = True
    _AUTH_IMPORT_ERROR = None
except ImportError as _auth_import_error:
    # FAIL CLOSED.
    #
    # These stubs previously returned None, which turned every
    # `Depends(require_chatbot_access())` into a no-op and served the entire
    # SQL agent — including query execution — with no authentication at all.
    # The blast radius was wider than it looks: this block also imports the
    # query-history service, so an unrelated failure there disabled auth.
    #
    # An import failure is a deployment fault, not a reason to drop
    # authorization. Refuse every request instead.
    AUTH_AVAILABLE = False
    _AUTH_IMPORT_ERROR = _auth_import_error
    logger_name = __name__
    logging.getLogger(logger_name).critical(
        "[SQL_AGENT] Authentication could not be imported (%s). Every SQL agent "
        "endpoint will refuse requests until this is fixed.",
        _auth_import_error,
    )

    def _auth_unavailable():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "AUTH_UNAVAILABLE",
                "message": (
                    "The assistant is unavailable because authentication could "
                    "not be initialised."
                ),
            },
        )

    async def get_current_user():  # type: ignore[misc]
        _auth_unavailable()

    def require_chatbot_access():  # type: ignore[misc]
        async def _denied():
            _auth_unavailable()
        return _denied

logger = logging.getLogger(__name__)

# Router for SQL Agent endpoints
router = APIRouter(prefix="/api/sql-agent", tags=["SQL Agent"])

# Global state - will be set by backend.lifespan during startup
_sql_agent_instance = None
_sql_agent_available = False

# ---------------------------------------------------------------------------
# SQL-agent isolation: concurrency cap, total timeout, per-user serialization,
# bounded LRU of user agents, and a thread->asyncio.Queue streaming bridge.
# The LangGraph pipeline is fully synchronous (sync ChatOllama + psycopg2), so
# every query MUST run in its own thread — never on the event loop — and the
# number of simultaneous queries must be small (each one monopolizes Ollama).
# ---------------------------------------------------------------------------
# Read the declared fields directly. The old shape wrapped them in
# getattr(..., 2) / getattr(..., 300) inside a try/except that supplied the
# same two numbers again — three declarations of each setting, any of which
# could drift from config.py without anything noticing.
from config import settings as _app_settings
from sql_agent.agent import TurnCancelled

SQL_AGENT_MAX_CONCURRENT = int(_app_settings.SQL_AGENT_MAX_CONCURRENT)
SQL_AGENT_TOTAL_TIMEOUT = float(_app_settings.SQL_AGENT_TOTAL_TIMEOUT)
SQL_AGENT_MAX_QUERY_CHARS = int(_app_settings.SQL_AGENT_MAX_QUERY_CHARS)


class SQLAgentQueryRequest(BaseModel):
    """Bounded request shared by the REST and SSE query endpoints."""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=SQL_AGENT_MAX_QUERY_CHARS)
    request_id: Optional[str] = None
    conversation_id: Optional[str] = None

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


def _request_value(request, name: str, default=None):
    """Read a field from a validated model or a direct-call test dictionary."""
    if isinstance(request, dict):
        return request.get(name, default)
    return getattr(request, name, default)


def _bounded_query(raw) -> tuple:
    """Return ``(query, error)`` using one contract for all transports."""
    if not isinstance(raw, str):
        return "", "Query must be a string"
    query = raw.strip()
    if not query:
        return "", "Query is required"
    if len(query) > SQL_AGENT_MAX_QUERY_CHARS:
        return "", (
            f"Query is too long. Maximum length is "
            f"{SQL_AGENT_MAX_QUERY_CHARS} characters.")
    return query, None

# The semaphore is sized once, at import — the concurrency cap is registered
# api_restart for exactly that reason, and boot hydration now applies stored
# values before this module loads.
_sql_agent_semaphore = asyncio.Semaphore(SQL_AGENT_MAX_CONCURRENT)
_SEMAPHORE_WAIT_SECONDS = 5.0
_BUSY_MESSAGE = (
    "The SQL assistant is currently handling other requests. "
    "Please try again in a few moments."
)

# User-specific agent instances (one per user for persistent memory).
# Bounded LRU: at most _USER_AGENTS_MAX users keep a live agent; the least
# recently used is evicted (previously this dict grew forever).
_USER_AGENTS_MAX = 10
_user_agents: "OrderedDict[int, object]" = OrderedDict()

# Per-user locks so two concurrent queries from the SAME user (double-submit,
# two tabs) serialize instead of corrupting shared conversation memory.
_user_query_locks: Dict[int, asyncio.Lock] = {}


def _get_user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_query_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_query_locks[user_id] = lock
    return lock


def _maybe_release_user_lock(user_id: int) -> bool:
    """Reclaim a user's lock ONLY if nothing holds or awaits it.

    Agent lifetime and lock lifetime are different concerns. The LRU used to
    pop the lock together with the agent — but the agent lookup happens
    BEFORE the lock is acquired on every transport, so with 11+ active users
    a turn could be in flight holding lock L when its user was evicted; L was
    dropped, the user's second tab got a fresh lock from _get_user_lock, and
    two turns ran concurrently for one user. That concurrent read-merge-write
    on the same session file is precisely what the lock exists to prevent.

    A lock that is held, or that has waiters queued on it, therefore stays in
    the dict no matter what happens to the agent; the next eviction of an
    idle user reclaims it. `_waiters` is asyncio.Lock internals, so it is read
    defensively — when in doubt, keeping a small lock object is always safer
    than dropping a live one.
    """
    lock = _user_query_locks.get(user_id)
    if lock is None:
        return True
    try:
        waiters = getattr(lock, "_waiters", None)
        busy = lock.locked() or bool(waiters)
    except Exception:
        busy = True
    if busy:
        logger.info(
            "[SQL_AGENT_API] Keeping lock for evicted user_id=%s (held or "
            "awaited); it will be reclaimed once idle", user_id)
        observability.observe_eviction("lock_kept_busy")
        return False
    _user_query_locks.pop(user_id, None)
    observability.observe_eviction("lock_reclaimed")
    return True


# user_id -> permissions_version the cached agent was built for. A cached agent
# carries the user's scope (pipelines, features, conversation memory), so it must
# not outlive the authorization it was built under.
_user_agent_versions: Dict[int, int] = {}


def invalidate_user_sql_agent(user_id: int, reason: str = "authorization_changed") -> bool:
    """Drop a user's cached agent so the next query rebuilds it from current config.

    Called when an administrator changes a role, chatbot access, active state or
    pipeline assignment. Returns True if an agent was actually evicted.

    Deliberately synchronous and side-effect-free beyond the two dicts: it is
    invoked from request handlers and from the users service, and must never be
    able to fail the write it accompanies.
    """
    existed = _user_agents.pop(user_id, None) is not None
    _user_agent_versions.pop(user_id, None)
    if existed:
        logger.info(
            "[SQL_AGENT_API] Invalidated cached agent for user_id=%s reason=%s",
            user_id, reason,
        )
    return existed


def _get_or_create_user_agent(user_id: int, permissions_version: Optional[int] = None):
    """Get the user's agent (LRU-refresh) or create it, evicting the oldest.

    When ``permissions_version`` is supplied and differs from the version the
    cached agent was built under, the cached agent is discarded and rebuilt. That
    is what stops a user whose chatbot access or pipeline scope was just revoked
    from continuing to be served by an agent holding the old scope.
    """
    agent = _user_agents.get(user_id)
    if agent is not None:
        cached_version = _user_agent_versions.get(user_id)
        if (permissions_version is not None
                and cached_version is not None
                and cached_version != permissions_version):
            logger.info(
                "[SQL_AGENT_API] Rebuilding agent for user_id=%s: permissions_version %s -> %s",
                user_id, cached_version, permissions_version,
            )
            _user_agents.pop(user_id, None)
            _user_agent_versions.pop(user_id, None)
            agent = None
        else:
            _user_agents.move_to_end(user_id)
            if permissions_version is not None:
                _user_agent_versions[user_id] = permissions_version
            return agent

    from sql_agent.agent import SQLIntelligenceAgent
    from sql_agent.conversation_memory import ConversationMemory

    logger.info(f"[SQL_AGENT_API] Creating user-specific agent for user {user_id}")
    user_memory = ConversationMemory(user_id=user_id)
    user_session_id = user_memory.start_session()
    agent = SQLIntelligenceAgent(conversation_memory=user_memory)
    _user_agents[user_id] = agent
    if permissions_version is not None:
        _user_agent_versions[user_id] = permissions_version
    logger.info(f"[SQL_AGENT_API] User {user_id} agent created (session: {user_session_id})")

    while len(_user_agents) > _USER_AGENTS_MAX:
        evicted_id, _evicted = _user_agents.popitem(last=False)
        # NOT _user_query_locks.pop(): a held or awaited lock must survive
        # its agent's eviction (see _maybe_release_user_lock for the bug).
        _maybe_release_user_lock(evicted_id)
        _user_agent_versions.pop(evicted_id, None)
        observability.observe_eviction("agent")
        logger.info(f"[SQL_AGENT_API] Evicted LRU agent for user {evicted_id} (cap {_USER_AGENTS_MAX})")

    return agent


def _uid(current_user) -> str:
    """User id for logs. Never logs the username."""
    return str(getattr(current_user, "id", "unknown"))


def _scoped_agent(current_user):
    """The caller's own agent.

    Session endpoints used to read and mutate the shared global instance, so
    one user could enumerate, load or reset another's conversation without
    authenticating. Routing through the per-user agent makes ownership
    structural: a session belonging to someone else is not in this store, so
    there is no ownership check left to forget.
    """
    user_id = getattr(current_user, "id", None)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": "AUTH_REQUIRED",
                "message": "Authentication is required.",
            },
        )
    return _get_or_create_user_agent(user_id)


_STREAM_SENTINEL = object()

# ---------------------------------------------------------------------------
# Request registry (correlation, cancellation, idempotency)
# ---------------------------------------------------------------------------
# request_id -> {"cancel_event": threading.Event, "user_id": int, "status": str,
#                "started_at": float}
# Bounded: terminal entries are pruned; hard cap evicts oldest.
_ACTIVE_REQUESTS: "OrderedDict[str, dict]" = OrderedDict()
_MAX_TRACKED_REQUESTS = 300
_REQUEST_ID_RE = re.compile(r'^[A-Za-z0-9_-]{8,64}$')


def _normalize_request_id(raw) -> str:
    """Accept a client-supplied request id (uuid-ish) or mint one."""
    import uuid as _uuid
    if isinstance(raw, str) and _REQUEST_ID_RE.match(raw):
        return raw
    return _uuid.uuid4().hex


def _register_request(request_id: str, user_id, cancel_event: threading.Event) -> bool:
    """Register an accepted request. Returns False if this request_id was
    already accepted (idempotency: a fallback transport must NOT re-execute)."""
    existing = _ACTIVE_REQUESTS.get(request_id)
    if existing is not None:
        return False
    while len(_ACTIVE_REQUESTS) >= _MAX_TRACKED_REQUESTS:
        # Evict TERMINAL entries first. Unconditionally popping the oldest
        # could drop a STILL-RUNNING request past 300 tracked — its cancel
        # endpoint then 404s and its request_id becomes replayable while the
        # query is still executing. Only if every entry is running (300
        # simultaneous queries — far beyond the semaphore) does the oldest
        # running one go, with a log line.
        evicted = None
        for key, entry in _ACTIVE_REQUESTS.items():
            if entry.get("status") != "running":
                evicted = key
                break
        if evicted is not None:
            _ACTIVE_REQUESTS.pop(evicted, None)
        else:
            dropped_id, _ = _ACTIVE_REQUESTS.popitem(last=False)
            logger.warning(
                "[SQL_AGENT_API] request registry full of RUNNING entries; "
                "dropped %s — its cancel handle is gone", dropped_id)
    _ACTIVE_REQUESTS[request_id] = {
        "cancel_event": cancel_event,
        "user_id": user_id,
        "status": "running",
        "started_at": asyncio.get_event_loop().time(),
    }
    return True


def _finish_request(request_id: str, status: str):
    entry = _ACTIVE_REQUESTS.get(request_id)
    if entry is not None:
        entry["status"] = status  # kept (bounded) so duplicate submits still 409


def _sse_event(payload: dict, request_id: str, seq: int) -> str:
    """Every server event repeats request_id + sequence (client correlation)."""
    payload = {**payload, "request_id": request_id, "sequence": seq}
    return f"data: {json.dumps(payload)}\n\n"


def _error_body(code: str, message: str, reference_id: str = None,
                retryable: bool = False, status: str = None) -> dict:
    """Structured error contract — the frontend reacts to `error.code`,
    NEVER to substring matching. No internal details are exposed."""
    body = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
            "reference_id": reference_id,
            "retryable": retryable,
        },
        "response": None,
    }
    if status:
        body["status"] = status
    return body


def require_sql_agent_csrf(request: Request):
    """CSRF defense-in-depth for cookie-authenticated mutating requests
    (same policy as the live-alerts routes): SameSite=lax cookie + custom
    header. Bearer-token clients are exempt (token can't be sent cross-site)."""
    if request.headers.get("authorization"):
        return
    if request.headers.get("x-requested-with", "").lower() != "xmlhttprequest":
        raise HTTPException(
            status_code=403,
            detail="CSRF check failed: X-Requested-With header required",
        )


# ---------------------------------------------------------------------------
# Security policy.
#
# The decision now lives in sql_agent/security_policy.py — ONE function that
# every transport calls, so REST, SSE and WebSocket cannot drift apart again.
#
# What used to be here: a process-local counter (which made the effective
# threshold 3 x WORKERS) and a handler that returned ACCOUNT_BLOCKED whether or
# not the database write succeeded. Worse, the streaming transports only reached
# this code when the agent's PROSE started with "Security:" — which it never did
# — so on SSE and WebSocket no violation was ever recorded and no account was
# ever blocked, while the user was told on every attempt that theirs had been.
# ---------------------------------------------------------------------------
from sql_agent.services import artifact_registry              # noqa: E402
from sql_agent.tools.agent_tools import translate_document_text  # noqa: E402
from sql_agent import observability                            # noqa: E402
from sql_agent.security_policy import (                        # noqa: E402
    OUTCOME_BLOCKED,
    OUTCOME_ENFORCEMENT_FAILED,
    REASON_FORBIDDEN_SQL,
    TRANSPORT_REST,
    TRANSPORT_SSE,
    TRANSPORT_WEBSOCKET,
    SecurityDecision,
    apply_security_policy,
)

_SECURITY_VIOLATION_THRESHOLD = 3          # kept for existing imports/tests


async def _handle_security_denial(current_user, query: str, reason: str,
                                  execution_time_ms: float, session_id=None,
                                  transport: str = TRANSPORT_REST,
                                  reason_code: str = REASON_FORBIDDEN_SQL,
                                  actor: str = "user") -> dict:
    """Apply the policy and return the transport-agnostic error body.

    Thin by design: the decision belongs to security_policy.apply_security_policy
    so that all three transports produce the same outcome for the same violation.
    """
    if not (AUTH_AVAILABLE and current_user):
        current_user = None

    outcome = await apply_security_policy(
        user=current_user,
        decision=SecurityDecision(violation=True, action="DENY",
                                  reason_code=reason_code, reason=reason),
        transport=transport,
        query=query,
        execution_time_ms=execution_time_ms,
        session_id=session_id,
        # Only what the USER wrote can count against the user. Everything the
        # SQL layers refuse was written by the model.
        attributable=(actor == "user"),
    )

    body = _error_body(outcome.error_code, outcome.message, outcome.reference_id)
    # Transports need to know whether to hang up; the CLIENT is told only
    # error_code/message. ENFORCEMENT_FAILED never surfaces as ACCOUNT_BLOCKED —
    # PolicyOutcome.error_code downgrades it to QUERY_DENIED.
    body["_policy"] = {
        "outcome": outcome.outcome,
        "blocked": outcome.blocked,
        "close_connection": outcome.close_connection,
    }
    return body


def _policy_says_close(body: dict) -> bool:
    """Whether the transport should terminate the connection after this body."""
    return bool((body or {}).get("_policy", {}).get("close_connection"))


def _client_body(body: dict) -> dict:
    """Strip the internal policy annotation before serializing to a client."""
    if not isinstance(body, dict):
        return body
    return {k: v for k, v in body.items() if k != "_policy"}


class _StageTimer:
    """Per-stage timing for one agent run.

    The agent already tags every update with a ``step`` (init, schema, rag,
    generate_sql, validate, execute, response, ...), so stage names come from the
    agent itself rather than being guessed at this layer.

    This exists because the reported incident timed out after 300s with
    ``response_chars=0`` and no indication of which stage consumed the time. A
    stage breakdown turns "the request hung" into "it hung in `execute`".

    Records only stage names and durations — never query text, prompts, generated
    SQL or response bodies.

    Attribution note (this is easy to get wrong)
    --------------------------------------------
    The agent streams LangGraph node outputs, so an update announcing step X
    arrives when node X has *finished*. The interval between two updates is
    therefore the runtime of the LATER node, not the earlier one. Time is
    credited accordingly.

    When the stream ends with work still in flight — which is precisely the
    timeout case — the outstanding interval belongs to a node that never
    reported, so it is recorded as ``after:<last completed step>``. A stall
    reported as ``after:rag`` means the graph was inside the node that follows
    retrieve_examples. Naming that node here would couple this timer to the
    graph's shape; saying "after rag" is what the events actually prove.
    """

    __slots__ = ("_now", "start", "stages", "_last_step", "_last_at",
                 "first_chunk_at", "first_update_at", "_finished")

    def __init__(self, now):
        self._now = now
        self.start = now()
        self.stages = {}            # step -> cumulative ms
        self._last_step = None
        self._last_at = self.start
        self.first_chunk_at = None  # first user-visible content
        self.first_update_at = None # first update of any kind
        self._finished = False

    def observe(self, update: dict) -> None:
        if not isinstance(update, dict) or self._finished:
            return
        t = self._now()
        if self.first_update_at is None:
            self.first_update_at = t
        if update.get("type") == "content" and self.first_chunk_at is None:
            self.first_chunk_at = t
        step = update.get("step")
        if not step:
            return
        # The elapsed interval is the work that PRODUCED this step.
        self.stages[step] = self.stages.get(step, 0.0) + (t - self._last_at) * 1000.0
        self._last_step = step
        self._last_at = t

    def finish(self) -> None:
        """Close out any work still in flight (the timeout case)."""
        if self._finished:
            return
        self._finished = True
        outstanding = (self._now() - self._last_at) * 1000.0
        if outstanding <= 0:
            return
        label = f"after:{self._last_step}" if self._last_step else "before_first_step"
        self.stages[label] = self.stages.get(label, 0.0) + outstanding

    @property
    def slowest(self):
        """(step, ms) of the stage that consumed the most time, or (None, 0)."""
        if not self.stages:
            return (None, 0.0)
        step = max(self.stages, key=self.stages.get)
        return (step, self.stages[step])

    def summary(self) -> str:
        """Compact `stage=ms` breakdown, longest first."""
        if not self.stages:
            return "none"
        return " ".join(
            f"{k}={v:.0f}" for k, v in
            sorted(self.stages.items(), key=lambda kv: kv[1], reverse=True)
        )


def _start_stream_thread(agent_instance, query: str,
                         cancel_event: threading.Event, loop):
    """TRUE streaming bridge: a dedicated thread pumps the agent's blocking
    query_stream() generator into an asyncio.Queue item by item, so SSE/WS
    consumers can forward each update the moment it is produced (the previous
    implementation either collected the full list before the first byte, or —
    worse — iterated the blocking generator directly on the event loop)."""
    q: asyncio.Queue = asyncio.Queue()
    worker_done = asyncio.Event()

    def _pump():
        try:
            for update in agent_instance.query_stream(query, cancel_event=cancel_event):
                loop.call_soon_threadsafe(q.put_nowait, update)
                if cancel_event.is_set():
                    break
        except Exception as e:
            logger.error(f"[SQL_AGENT_API] Stream thread error: {e}", exc_info=True)
            try:
                loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "message": str(e)})
            except RuntimeError:
                pass  # loop closed
        finally:
            try:
                loop.call_soon_threadsafe(q.put_nowait, _STREAM_SENTINEL)
                loop.call_soon_threadsafe(worker_done.set)
            except RuntimeError:
                pass

    threading.Thread(target=_pump, name="sql-agent-stream", daemon=True).start()
    return q, worker_done

# Holds strong references to fire-and-forget background tasks. The event loop only
# keeps weak references, so without this a task can be garbage-collected mid-run.
_background_tasks: set = set()


def _spawn_background(coro):
    """Schedule a best-effort background task, keeping a strong reference to it."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _drain_cancelled_turn(worker_task, user_lock, request_id: str) -> None:
    """Keep isolation resources until a cancelled worker actually exits."""
    try:
        await worker_task
    except (TurnCancelled, asyncio.CancelledError):
        pass
    except Exception as exc:
        logger.warning(
            "[SQL_AGENT_API] request_id=%s cancelled worker ended with %s",
            request_id, type(exc).__name__)
    finally:
        if user_lock is not None and user_lock.locked():
            user_lock.release()
        _sql_agent_semaphore.release()
        logger.info("[SQL_AGENT_API] request_id=%s cancelled worker drained",
                    request_id)


async def _release_stream_resources_when_done(worker_done: asyncio.Event,
                                              user_lock, release_semaphore: bool,
                                              request_id: str) -> None:
    """Release stream isolation only after its worker thread has stopped."""
    await worker_done.wait()
    if user_lock is not None and user_lock.locked():
        user_lock.release()
    if release_semaphore:
        _sql_agent_semaphore.release()
    logger.info("[SQL_AGENT_API] request_id=%s stream worker drained",
                request_id)


async def await_persistence_despite_disconnect(coro, request_id: str):
    """Run a persistence coroutine so a client disconnect cannot destroy it.

    Why this exists
    ---------------
    The stream persists history and audit AFTER its terminal event — and the
    browser aborts the connection the moment it receives that event (a closed
    tab does the same at any point). The abort propagates through Starlette's
    BaseHTTPMiddleware cancel scope and cancels the request task, which was
    still awaiting the history commit. Observed live: "persisting history"
    logged with no "[HISTORY_SAVE] saved" after it, the row missing from the
    sidebar, and SQLAlchemy's pool logging "Exception terminating connection /
    CancelledError" as it tore down the connection mid-commit.

    So: run the coroutine as a BACKGROUND task — outside the request's cancel
    scope — and await it through asyncio.shield. The normal path still waits
    for the commit before the request tears down; on cancellation the await is
    interrupted but the task runs to completion and commits, and the
    CancelledError is re-raised so teardown proceeds normally.

    This is deliberately the narrow shield the incident brief allows ("shield
    only a very small, essential cleanup operation") — never the agent
    workflow, whose cancellation on disconnect is correct and wanted.
    """
    task = _spawn_background(coro)
    try:
        # shield() yields the task's OWN result, which this used to discard.
        # Returning it lets a caller keep, for example, the history row id it
        # just wrote; callers that ignore it are unaffected.
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            logger.info(
                "[SQL_AGENT_API] request_id=%s client disconnected during persistence; "
                "history/audit save continuing in background",
                request_id,
            )
        raise


async def check_authorization_fresh(user_id: int):
    """Re-read the live authorization for a long-lived connection.

    Returns ``(ok, permissions_version, reason)``. ``ok`` is False when the
    account has been deactivated or chatbot access revoked since the connection
    was authorized.

    A WebSocket is authorized once at handshake, and because the session maker
    sets ``expire_on_commit=False`` the ``current_user`` snapshot never refreshes
    — so without this an administrator's revocation would not reach an open
    socket at all, and `block_user_for_forbidden_sql` would leave the very socket
    that triggered the block still working.

    Deliberately a narrow column read on the primary key, opened and closed
    inside this call, so it holds no connection between messages.
    """
    try:
        from sqlalchemy import select as _select
        from db_models import User as _User
        from db_connection import db_manager

        # db_manager.get_session() (a real async context manager), NOT
        # `async for _db in get_db()`. Returning out of an `async for` over an
        # async generator leaves the generator suspended, so its cleanup waits on
        # garbage collection — which leaked a pooled connection per call. This
        # runs on every WebSocket message, so that leak exhausted the pool under
        # ordinary chat traffic (caught by
        # test_check_authorization_fresh_holds_no_connection).
        async with db_manager.get_session() as _db:
            row = (await _db.execute(
                _select(_User.permissions_version, _User.is_active, _User.can_use_chatbot)
                .where(_User.id == user_id)
            )).first()
            if row is None:
                return False, None, "ACCOUNT_NOT_FOUND"
            version, is_active, can_use_chatbot = row
            if not is_active:
                return False, int(version or 1), "ACCOUNT_DEACTIVATED"
            if not can_use_chatbot:
                return False, int(version or 1), "CHATBOT_ACCESS_REVOKED"
            return True, int(version or 1), None
    except Exception as exc:
        # Fail OPEN on infrastructure error, not on an authorization decision:
        # a transient database blip must not disconnect every active user. The
        # handshake check already established access; this is a re-check.
        logger.warning(
            "[SQL_AGENT_API] Authorization re-check failed for user_id=%s: %s",
            user_id, type(exc).__name__,
        )
        return True, None, None
    return True, None, None


async def release_request_session(db, *detach) -> None:
    """Return the pooled connection to the pool BEFORE starting a long stream.

    Why this exists
    ---------------
    A ``StreamingResponse`` keeps FastAPI's dependency scope open until the last
    byte is sent. ``get_current_user`` depends on ``get_db``, and its
    ``SELECT ... FROM users`` autobegins a transaction — so without this call the
    auth session sits *idle in transaction* for the whole stream.

    PostgreSQL is configured with ``idle_in_transaction_session_timeout=300000``
    (300s, ``db_connection.py``), which is the SAME number as
    ``SQL_AGENT_TOTAL_TIMEOUT``. The server therefore terminates that connection
    at almost exactly the moment the stream deadline fires, and the
    ``session.commit()`` in ``get_session``'s cleanup then raises
    ``InterfaceError`` ("connection closed during commit"). The database is
    healthy throughout — only this one abandoned connection dies, which is why
    ``/health`` and every other endpoint keep returning 200.

    Detaching first is safe because the session maker sets
    ``expire_on_commit=False``, so already-loaded attributes on ``current_user``
    remain readable after expunge. Detaching is also the point: it makes any
    accidental lazy load raise loudly instead of silently reopening a connection
    mid-stream.
    """
    if db is None:
        return
    for obj in detach:
        if obj is None:
            continue
        try:
            db.expunge(obj)
        except Exception:
            # Already detached or never attached — nothing to do.
            pass
    try:
        await db.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[SQL_AGENT_API] Failed to release request session: %s", type(exc).__name__)


async def persist_query_history(
    user_id: int,
    query: str,
    response: Optional[str],
    session_id: Optional[str],
    success: bool = True,
    processing_time_ms: Optional[float] = None,
    metadata: Optional[dict] = None,
    conversation_id=None,
):
    """Reliably persist a query so it appears in the user's sidebar history.

    The core history row (session + query) is committed synchronously so it always
    lands. Semantic-search extras (memories + embeddings) run afterward as a
    referenced background task and can never block or fail the core save.

    Callers should ``await`` this. It is meant to run after any streamed response is
    already sent, so awaiting adds no user-facing latency while guaranteeing the row
    is committed before the request tears down (the previous fire-and-forget approach
    raced teardown and dropped queries intermittently).
    """
    if not (AUTH_AVAILABLE and user_id):
        return None

    query_history_id = None
    try:
        async with db_manager.get_session() as db:
            db_session = await user_query_history_service.get_or_create_session(
                db=db, user_id=user_id, session_id=session_id
            )
            query_history = await user_query_history_service.save_query_history(
                db=db,
                user_id=user_id,
                query_text=query,
                response_text=response,
                session_id=session_id,
                success=success,
                processing_time_ms=processing_time_ms,
                metadata=metadata or {},
            )
            await db.commit()
            query_history_id = query_history.id
            logger.info(f"[HISTORY_SAVE] ✅ Query {query_history_id} saved for user {user_id} (session {session_id})")
    except Exception as e:
        logger.error(f"[HISTORY_SAVE] ❌ Failed to persist query history for user {user_id}: {e}", exc_info=True)
        return None

    # Dual-write into the conversation domain (organizations -> workspaces ->
    # conversations -> branches -> messages). Own session, own try/except:
    # while both models coexist, the flat history above remains the read path
    # for the sidebar, so a failure here must never lose the primary save.
    try:
        from backend.services.conversation_service import record_exchange_for_session

        assistant_blocks = []
        if response:
            assistant_blocks.append({"type": "text", "text": response})
        sql_text = (metadata or {}).get("sql")
        if sql_text:
            assistant_blocks.append({"type": "sql", "sql": str(sql_text)})
        artifact_block = (metadata or {}).get("artifact")
        if artifact_block:
            # Carries an id, a title and a URL — never the document body and
            # never its source_content. Reading it still requires the
            # ownership-checked download route.
            assistant_blocks.append(dict(artifact_block, type="artifact"))
        if not assistant_blocks:
            assistant_blocks.append({"type": "text", "text": ""})

        async with db_manager.get_session() as conv_db:
            await record_exchange_for_session(
                conv_db, user_id=user_id, session_id=session_id,
                user_text=query, assistant_blocks=assistant_blocks,
                success=success, processing_time_ms=processing_time_ms,
                conversation_id=conversation_id,
            )
            await conv_db.commit()
    except Exception as conv_error:
        logger.warning(
            "[HISTORY_SAVE] conversation dual-write failed for user %s: %s",
            user_id, type(conv_error).__name__,
        )

    # Best-effort enrichment (memories + embeddings) — never blocks the sidebar row
    if query_history_id is not None:
        _spawn_background(_enrich_query_history(
            user_id=user_id,
            query_history_id=query_history_id,
            query=query,
            response=response,
            session_id=session_id,
        ))
    return query_history_id


async def _enrich_query_history(user_id, query_history_id, query, response, session_id):
    """Best-effort semantic-search enrichment: memory extraction + query embedding.

    Runs after the core history row is already committed, so any failure here leaves
    the sidebar entry intact.
    """
    try:
        async with db_manager.get_session() as db:
            try:
                await user_query_history_service.extract_and_save_memories(
                    db=db, user_id=user_id, query_id=query_history_id,
                    query_text=query, response_text=response, session_id=session_id,
                )
            except Exception as mem_err:
                logger.warning(f"[HISTORY_ENRICH] memory extraction failed for query {query_history_id}: {mem_err}")
            try:
                embedding = await user_query_history_service.generate_query_embedding(query)
                if embedding:
                    await user_query_history_service.save_query_embedding(
                        db=db, query_history_id=query_history_id,
                        user_id=user_id, embedding=embedding,
                    )
                else:
                    # `generate_query_embedding` catches its own failures and
                    # returns None, so without this the outcome is invisible:
                    # no row is written, no warning is logged, and semantic
                    # recall just never matches anything. That is how a model
                    # cache the process could not write (PermissionError on
                    # HF_HOME) disabled the feature indefinitely with nothing
                    # but a single ERROR at first use to show for it.
                    logger.warning(
                        "[HISTORY_ENRICH] no embedding produced for query %s — "
                        "semantic query-history search will not match it. "
                        "Check the [EMBEDDING] records for the cause "
                        "(model download, HF_HOME writability, disk space).",
                        query_history_id)
            except Exception as emb_err:
                logger.warning(f"[HISTORY_ENRICH] embedding failed for query {query_history_id}: {emb_err}")
            await db.commit()
    except Exception as e:
        logger.warning(f"[HISTORY_ENRICH] enrichment task failed for query {query_history_id}: {e}")


def set_sql_agent_instance(instance: Optional[object], available: bool = False):
    """Set the SQL agent instance and availability flag."""
    global _sql_agent_instance, _sql_agent_available
    _sql_agent_instance = instance
    _sql_agent_available = available


def get_sql_agent_instance():
    """Get the SQL agent instance."""
    return _sql_agent_instance


def is_sql_agent_available():
    """Check if SQL agent is available."""
    return _sql_agent_available


async def log_chatbot_query(
    user_id: int,
    username: str,
    query: str,
    response: Optional[str],
    success: bool,
    error_message: Optional[str] = None,
    processing_time_ms: Optional[float] = None,
    session_id: Optional[str] = None
):
    """
    Log chatbot query and response to audit log.
    This runs asynchronously to avoid blocking the main request.
    """
    try:
        from db_models import ChatbotAuditLog
        from db_connection import get_db
        
        async with db_manager.get_session() as db:
            audit_log = ChatbotAuditLog(
                user_id=user_id,
                username=username,
                query=query,
                response=response,
                success=success,
                error_message=error_message,
                processing_time_ms=processing_time_ms,
                session_id=session_id
            )
            db.add(audit_log)
            await db.commit()
            logger.debug(f"[AUDIT] Logged chatbot query for user {username} (success: {success})")
    except Exception as e:
        # Don't fail the main request if logging fails
        logger.warning(f"[AUDIT] Failed to log chatbot query: {e}")


async def block_user_for_forbidden_sql(user_id: int, username: str,
                                       sql_query: str, reason: str) -> bool:
    """Deprecated shim. Blocking lives in sql_agent/security_policy.py.

    Kept so nothing that still imports this name silently does nothing, and
    because the name reads like it enforces something. It does not decide policy
    and it does not swallow failure: it returns whether the account is actually
    blocked, which is the property the caller must not guess at.

    The version this replaces caught every exception and returned None, while
    the caller returned ACCOUNT_BLOCKED to the user regardless — so a failed
    UPDATE still told someone they had been blocked.
    """
    from sql_agent.security_policy import _persist_block

    logger.warning(
        "[SECURITY] block_user_for_forbidden_sql is deprecated; "
        "use security_policy.apply_security_policy (user_id=%s)", user_id)
    succeeded, _already = await _persist_block(
        user_id=user_id, username=username,
        reference_id="SEC-legacy", transport="legacy")
    return succeeded


@router.post("/query")
async def sql_agent_query(
    request: SQLAgentQueryRequest,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None
):
    """
    Query the SQL Intelligence Agent with natural language.
    Uses user-specific persistent conversation memory (like ChatGPT).
    
    Request body:
    {
        "query": "Track Joey"
    }
    
    Returns:
    {
        "response": "SURVEILLANCE INTELLIGENCE REPORT...",
        "session_id": "session_id",
        "success": true
    }
    """
    logger.info("[SQL_AGENT_API] Query endpoint called request_type=%s "
                "auth_available=%s user_id=%s", type(request).__name__,
                AUTH_AVAILABLE, _uid(current_user))
    
    if not _sql_agent_available:
        logger.warning("[SQL_AGENT_API] Query request received but SQL_AGENT_AVAILABLE is False")
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "error": "SQL Intelligence Agent module is not available. Please check server logs.",
                "response": None
            }
        )
    
    if _sql_agent_instance is None:
        logger.error("[SQL_AGENT_API] Query request received but sql_agent_instance is None")
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "error": "SQL Intelligence Agent is not initialized. Please check server logs.",
                "response": None
            }
        )
    
    # Verify agent is functional
    try:
        if not hasattr(_sql_agent_instance, 'query'):
            logger.error("[SQL_AGENT_API] sql_agent_instance missing 'query' method")
            return JSONResponse(
                status_code=503,
                content={
                    "success": False,
                    "error": "SQL Intelligence Agent is not properly configured.",
                    "response": None
                }
            )
    except Exception as check_error:
        logger.error(f"[SQL_AGENT_API] Error checking agent: {str(check_error)}", exc_info=True)
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "error": f"SQL Intelligence Agent health check failed: {str(check_error)}",
                "response": None
            }
        )

    # Set only after idempotency registration. The outer finally turns any
    # otherwise-unhandled early return or exception into a terminal registry
    # entry, so a request can never remain "running" after this coroutine has
    # returned. Explicit completed/cancelled/failed outcomes below win because
    # the finally only changes entries that are still running.
    rest_request_id = None
    try:
        # Validate request format
        if not isinstance(request, (dict, SQLAgentQueryRequest)):
            logger.warning("[SQL_AGENT_API] Invalid request format - not a dict")
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "Invalid request format. Expected JSON object with 'query' field.",
                    "response": None
                }
            )
        
        query, query_error = _bounded_query(_request_value(request, "query"))
        if query_error:
            logger.warning("[SQL_AGENT_API] Invalid query rejected: %s", query_error)
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": query_error,
                    "response": None
                }
            )
        
        logger.info("[SQL_AGENT_API] Processing query (chars=%d)", len(query))
        start_time = asyncio.get_event_loop().time()

        # Idempotency — the same contract SSE and WS have always had, which
        # REST lacked: a client retry or double-click re-ran the whole
        # pipeline, and if that turn produced a document it rendered and
        # registered a SECOND artifact. A client that sends request_id gets
        # exactly-once acceptance; one that doesn't gets a minted id and
        # keeps today's behaviour.
        rest_request_id = _normalize_request_id(
            _request_value(request, "request_id"))
        rest_cancel_event = threading.Event()
        if not _register_request(rest_request_id, getattr(current_user, "id", None),
                                 rest_cancel_event):
            return JSONResponse(
                status_code=409,
                content=_error_body(
                    "DUPLICATE_REQUEST",
                    "This request was already received and is being processed.",
                ),
            )

        # Get or create user-specific agent instance for persistent memory
        agent_instance = _sql_agent_instance
        user_id = None
        username = None
        if AUTH_AVAILABLE and current_user:
            user_id = current_user.id
            username = current_user.username
            # Check if user is already blocked — structured code, safe message,
            # no internal reason leaked to the browser
            if not current_user.is_active or not current_user.can_use_chatbot:
                return JSONResponse(
                    status_code=403,
                    content=_error_body(
                        "ACCOUNT_BLOCKED",
                        "Your access to the SQL assistant is temporarily restricted. "
                        "Please contact an administrator.",
                    ),
                )

            agent_instance = _get_or_create_user_agent(
                user_id,
                permissions_version=int(getattr(current_user, "permissions_version", 1) or 1),
            )

        # Track if security violation was detected (to prevent duplicate audit logs)
        security_violation_detected = False

        # Run query in thread pool with timeout to avoid blocking
        try:
            QUERY_TIMEOUT = SQL_AGENT_TOTAL_TIMEOUT
            try:
                # Concurrency cap: bounded number of simultaneous agent queries;
                # reject with "busy" instead of piling threads onto Ollama.
                try:
                    await asyncio.wait_for(_sql_agent_semaphore.acquire(), timeout=_SEMAPHORE_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    # Busy is retryable BY DESIGN, so the idempotency entry is
                    # removed outright — keeping it would 409 the very retry
                    # the message invites.
                    _ACTIVE_REQUESTS.pop(rest_request_id, None)
                    return JSONResponse(
                        status_code=503,
                        content={"success": False, "error": _BUSY_MESSAGE, "response": None},
                    )

                user_lock = _get_user_lock(user_id) if user_id else None
                lock_acquired = False
                resources_deferred = False
                try:
                    if user_lock:
                        await user_lock.acquire()
                        lock_acquired = True
                    logger.info("[SQL_AGENT_API] Starting query timeout=%ss chars=%d",
                                QUERY_TIMEOUT, len(query))
                    # The shared lifecycle, inside the user's lock — the same
                    # call SSE and WS make, so the transports cannot drift.
                    await prepare_turn(agent_instance, current_user)
                    worker_task = asyncio.create_task(run_in_threadpool(
                        agent_instance.query, query, True, rest_cancel_event))
                    try:
                        # Shield the worker from wait_for cancellation. On a
                        # timeout we signal it and keep the user's lock plus
                        # the global slot until it reaches a graph boundary.
                        agent_result = await asyncio.wait_for(
                            asyncio.shield(worker_task), timeout=QUERY_TIMEOUT)
                    except asyncio.TimeoutError:
                        rest_cancel_event.set()
                        resources_deferred = True
                        _spawn_background(_drain_cancelled_turn(
                            worker_task, user_lock if lock_acquired else None,
                            rest_request_id))
                        raise
                finally:
                    if lock_acquired and not resources_deferred:
                        user_lock.release()
                    if not resources_deferred:
                        _sql_agent_semaphore.release()
            except asyncio.TimeoutError:
                logger.error("[SQL_AGENT_API] Query timeout after %s seconds "
                             "request_id=%s", QUERY_TIMEOUT, rest_request_id)
                _finish_request(rest_request_id, "failed")
                return JSONResponse(
                    status_code=504,
                    content={
                        "success": False,
                        "error": f"Query took too long to process (over {QUERY_TIMEOUT} seconds). Please try a simpler question or check if the system is busy.",
                        "response": None
                    }
                )
            
            # Handle agent response (may be string or tuple with security flags).
            # PRIVACY: never log the response body — lengths and status only.
            if isinstance(agent_result, tuple):
                response, result_dict = agent_result
                logger.info(f"[SQL_AGENT_API] Agent returned tuple response ({len(response)} chars)")
            else:
                response = agent_result
                result_dict = {}
                logger.info(f"[SQL_AGENT_API] Agent returned string response ({len(response)} chars)")
            
            # SECURITY: only an EXPLICIT agent flag counts as a violation.
            # The old "Layer 2" that substring-scanned the model's PROSE for
            # words like "blocked"/"read-only" and then blocked the account is
            # deliberately removed: a denial explanation is not an attack.
            # Policy: Denied -> Audited -> Explained safely; ACCOUNT_BLOCKED
            # only after the explicit threshold in _handle_security_denial.
            if result_dict.get("security_block_user"):
                block_reason = result_dict.get("security_block_reason", "Attempted forbidden SQL operation")
                execution_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
                denial = await _handle_security_denial(
                    current_user, query, block_reason, execution_time_ms,
                    session_id=agent_instance.conversation_memory.current_session_id
                    if hasattr(agent_instance, "conversation_memory") else None,
                    transport=TRANSPORT_REST,
                    reason_code=result_dict.get("security_reason_code",
                                                REASON_FORBIDDEN_SQL),
                    # Defaults to "user" when absent, keeping the stricter
                    # behaviour for any site that has not declared itself.
                    actor=result_dict.get("security_block_actor", "user"),
                )
                security_violation_detected = True
                # _client_body strips the internal _policy annotation; the client
                # sees only error.code / message / reference_id.
                _finish_request(rest_request_id, "failed")
                return JSONResponse(status_code=403, content=_client_body(denial))
        except TurnCancelled:
            _finish_request(rest_request_id, "cancelled")
            return JSONResponse(
                status_code=409,
                content=_error_body(
                    "REQUEST_CANCELLED",
                    "The request was cancelled before it completed.",
                ),
            )
        except Exception as agent_error:
            logger.error(f"[SQL_AGENT_API] Agent execution error: {str(agent_error)}", exc_info=True)
            error_message = str(agent_error)

            # NOTE: exceptions never auto-block. Make messages user-safe.
            if "timeout" in error_message.lower():
                error_message = "Query timed out. Please try a simpler question."
            elif "connection" in error_message.lower() or "database" in error_message.lower():
                error_message = "Database connection error. Please try again later."
            else:
                error_message = "The assistant could not process this query. Please try again."

            _finish_request(rest_request_id, "failed")
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": f"Error processing query: {error_message}",
                    "response": None
                }
            )
        
        execution_time = asyncio.get_event_loop().time() - start_time
        execution_time_ms = execution_time * 1000  # Convert to milliseconds
        
        # Validate response
        if not response or not isinstance(response, str):
            logger.error(f"[SQL_AGENT_API] Invalid response from agent: {type(response)}")
            
            # Log failed query to audit log
            if AUTH_AVAILABLE and current_user:
                await log_chatbot_query(
                    user_id=current_user.id,
                    username=current_user.username,
                    query=query,
                    response=None,
                    success=False,
                    error_message="Agent returned invalid response format",
                    processing_time_ms=execution_time_ms,
                    session_id=agent_instance.conversation_memory.current_session_id
                )
            
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "Agent returned invalid response format",
                    "response": None
                }
            )
        
        # Get current session ID from the agent instance used
        session_id = agent_instance.conversation_memory.current_session_id
        
        # PRIVACY: log status + length only — never the response body
        logger.info(f"[SQL_AGENT_API] ✅ Query completed in {execution_time:.2f}s ({len(response)} chars)")
        
        # Exists even when authentication is disabled. Previously it was
        # initialized only inside the authenticated persistence branch and the
        # successful unauthenticated path raised UnboundLocalError here.
        artifact_block = None

        # Log successful query to audit log (only if no security violation was detected)
        # Security violations are logged in the security check blocks above
        if AUTH_AVAILABLE and current_user and not security_violation_detected:
            await log_chatbot_query(
                user_id=current_user.id,
                username=current_user.username,
                query=query,
                response=response,
                success=True,
                error_message=None,
                processing_time_ms=execution_time_ms,
                session_id=session_id
            )
            
            # Save to user query history so it appears in the sidebar.
            # Awaited (not fire-and-forget) so it reliably commits before this
            # request returns; the core insert is fast (embeddings run in the
            # background) so the added latency is negligible.
            if AUTH_AVAILABLE and current_user:
                metadata = {}
                if result_dict:
                    if "generated_sql" in result_dict:
                        metadata["sql"] = result_dict.get("generated_sql")
                    if "intent" in result_dict:
                        metadata["intent"] = result_dict.get("intent")
                    if "query_result" in result_dict:
                        result_data = result_dict.get("query_result", {})
                        if isinstance(result_data, dict):
                            metadata["row_count"] = result_data.get("row_count", 0)
                    response, artifact_block = await complete_turn_document(
                        agent_instance, current_user, response)
                    if artifact_block:
                        # Into the metadata so the conversation message gets an
                        # artifact block, and onto the response body so the UI
                        # can offer the download immediately.
                        metadata["artifact"] = artifact_block

                await finalize_turn(
                    agent_instance,
                    user_id=current_user.id,
                    query=query,
                    response=response,
                    session_id=session_id,
                    success=True,
                    processing_time_ms=execution_time_ms,
                    metadata=metadata,
                    request_label="query-sync",
                )

        body = {
            "success": True,
            "response": response,
            "session_id": session_id,
            "timestamp": datetime.utcnow().isoformat()
        }
        if artifact_block:
            # The id and a download URL — the document itself is fetched from
            # the ownership-checked route, never inlined here.
            body["artifact"] = artifact_block
        _finish_request(rest_request_id, "completed")
        return body
    
    except Exception as e:
        logger.error(f"[SQL_AGENT_API] Unexpected error: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": f"Unexpected error: {str(e)}",
                "response": None
            }
        )
    finally:
        if rest_request_id is not None:
            entry = _ACTIVE_REQUESTS.get(rest_request_id)
            if entry is not None and entry.get("status") == "running":
                _finish_request(rest_request_id, "failed")


@router.post("/query/stream")
async def sql_agent_query_stream(
    request: SQLAgentQueryRequest,
    http_request: Request,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None,
    db=Depends(get_db) if AUTH_AVAILABLE else None,
):
    """
    Query the SQL Intelligence Agent with streaming SSE response.
    
    Request body:
    {
        "query": "Track Joey",
        "session_id": "optional_session_id"
    }
    
    Returns:
    Server-Sent Events stream with progress updates and final response
    """
    if not _sql_agent_available or _sql_agent_instance is None:
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'SQL Agent not available'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")
    
    try:
        query, query_error = _bounded_query(_request_value(request, "query"))
        request_id = _normalize_request_id(
            _request_value(request, "request_id"))

        def _single_event_stream(payload: dict):
            async def _stream():
                yield _sse_event(payload, request_id, 0)
                yield _sse_event({"type": "complete", "success": False}, request_id, 1)
            return StreamingResponse(_stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache, no-transform",
                                              "X-Accel-Buffering": "no"})

        if query_error:
            return _single_event_stream({"type": "error",
                                         "error_code": "INVALID_REQUEST",
                                         "message": query_error})

        logger.info(f"[SQL_AGENT_API] request_id={request_id} streaming query ({len(query)} chars)")

        # Check if user is already blocked BEFORE processing — structured code,
        # safe message, no internal blocked_reason leaked to the browser
        if AUTH_AVAILABLE and current_user:
            if not current_user.is_active or not current_user.can_use_chatbot:
                return _single_event_stream({
                    "type": "error",
                    "error_code": "ACCOUNT_BLOCKED",
                    "message": "Your access to the SQL assistant is temporarily restricted. "
                               "Please contact an administrator.",
                })

        # Idempotency: the same request_id is executed at most once — a client
        # falling back to another transport cannot run the query twice.
        cancel_event = threading.Event()
        if not _register_request(request_id,
                                 current_user.id if (AUTH_AVAILABLE and current_user) else None,
                                 cancel_event):
            return _single_event_stream({
                "type": "error",
                "error_code": "DUPLICATE_REQUEST",
                "message": "This request was already accepted. Not executing it again.",
            })

        # Optional explicit conversation target. Validated BEFORE the stream
        # starts (and before the request session is released): a foreign or
        # deleted id is rejected up front instead of streaming a full answer
        # into a conversation the caller cannot read back.
        stream_conversation_id = None
        raw_conversation_id = _request_value(request, "conversation_id")
        if raw_conversation_id and AUTH_AVAILABLE and current_user and db is not None:
            import uuid as _uuid_mod
            try:
                candidate = _uuid_mod.UUID(str(raw_conversation_id))
            except (ValueError, TypeError):
                return _single_event_stream({
                    "type": "error", "error_code": "CONVERSATION_NOT_FOUND",
                    "message": "Conversation not found.",
                })
            from backend.services.conversation_service import verify_conversation_owner
            if not await verify_conversation_owner(db, candidate, current_user.id):
                return _single_event_stream({
                    "type": "error", "error_code": "CONVERSATION_NOT_FOUND",
                    "message": "Conversation not found.",
                })
            stream_conversation_id = candidate

        # Get or create user-specific agent instance for persistent memory.
        # Keyed on permissions_version so an administrator's role / chatbot /
        # pipeline change evicts the cached agent instead of letting it keep
        # serving the old scope (see _get_or_create_user_agent).
        agent_instance = _sql_agent_instance
        stream_user_id = None
        stream_username = None
        stream_permissions_version = None
        if AUTH_AVAILABLE and current_user:
            stream_user_id = current_user.id
            stream_username = current_user.username
            stream_permissions_version = int(getattr(current_user, "permissions_version", 1) or 1)
            agent_instance = _get_or_create_user_agent(
                stream_user_id, permissions_version=stream_permissions_version
            )

        # Hand the pooled connection back BEFORE the stream begins. Everything the
        # stream needs is now a plain value (ids and strings), so nothing below
        # touches the request session. Without this the auth session would sit
        # idle-in-transaction for the full stream and PostgreSQL would terminate
        # it at 300s — see release_request_session for the full chain.
        await release_request_session(db, current_user)

        async def stream_query():
            final_response = None
            # An artifact produced mid-stream, recorded on the conversation
            # message alongside the narrative.
            stream_artifact_block = None
            stream_start_time = asyncio.get_event_loop().time()
            stream_success = False
            completion_sent = False
            last_heartbeat = stream_start_time
            heartbeat_interval = 20.0  # heartbeats keep proxies + client alive
            seq = 0

            def evt(payload: dict) -> str:
                nonlocal seq
                seq += 1
                return _sse_event(payload, request_id, seq)

            # Cancellation + isolation state (cancel_event is registered in the
            # request registry — POST /requests/{id}/cancel sets it server-side)
            sem_acquired = False
            lock_acquired = False
            worker_done = None
            user_lock = _get_user_lock(stream_user_id) if stream_user_id is not None else None
            deadline = stream_start_time + SQL_AGENT_TOTAL_TIMEOUT
            was_cancelled = False
            # Which limit ended the request, so the log names the culprit instead
            # of leaving "300s" to be traced back through the config by hand.
            timeout_source = None

            try:
                # Concurrency cap: bounded simultaneous agent queries
                try:
                    await asyncio.wait_for(_sql_agent_semaphore.acquire(), timeout=_SEMAPHORE_WAIT_SECONDS)
                    sem_acquired = True
                except asyncio.TimeoutError:
                    yield evt({"type": "error", "error_code": "AGENT_BUSY", "message": _BUSY_MESSAGE})
                    yield evt({"type": "complete", "success": False})
                    completion_sent = True
                    return

                # Per-user serialization (double-submit / second tab)
                if user_lock is not None:
                    await user_lock.acquire()
                    lock_acquired = True

                # The shared lifecycle. This transport NOT calling it is how
                # "same report but camera 3" silently bound to recency on the
                # very transport the browser uses.
                await prepare_turn(agent_instance, current_user)

                # TRUE streaming: dedicated thread pumps updates into an asyncio
                # queue; each update is forwarded to the client the moment the
                # agent produces it (first byte during generation, not after).
                update_queue, worker_done = _start_stream_thread(
                    agent_instance, query, cancel_event, asyncio.get_running_loop()
                )
                stages = _StageTimer(lambda: asyncio.get_event_loop().time())

                while True:
                    now = asyncio.get_event_loop().time()
                    if now >= deadline:
                        cancel_event.set()
                        timeout_source = "SQL_AGENT_TOTAL_TIMEOUT"
                        try:
                            stages.finish()
                            _slow_step, _slow_ms = stages.slowest
                        except Exception:  # pragma: no cover
                            _slow_step, _slow_ms = None, 0.0
                        logger.warning(
                            "[SQL_AGENT_API] request_id=%s stream timeout after %.0fs "
                            "(source=SQL_AGENT_TOTAL_TIMEOUT) stalled_in=%s stage_ms=%.0f stages[%s]",
                            request_id, SQL_AGENT_TOTAL_TIMEOUT, _slow_step, _slow_ms,
                            stages.summary(),
                        )
                        yield evt({"type": "error", "error_code": "QUERY_TIMEOUT",
                                   "message": "Query took too long and was cancelled.", "retryable": True})
                        yield evt({"type": "complete", "success": False})
                        completion_sent = True
                        stream_success = False
                        break

                    # Server-side cancellation (POST /requests/{id}/cancel or WS
                    # cancel): terminal 'cancelled' event, LLM/DB thread stops
                    # at its next boundary via the shared cancel_event.
                    if cancel_event.is_set():
                        was_cancelled = True
                        yield evt({"type": "cancelled"})
                        completion_sent = True
                        stream_success = False
                        break

                    try:
                        update = await asyncio.wait_for(update_queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        # No update yet (LLM thinking): check disconnect + heartbeat
                        try:
                            if await http_request.is_disconnected():
                                cancel_event.set()
                                logger.info(f"[SQL_AGENT_API] request_id={request_id} client disconnected - cancelling")
                                return
                        except Exception:
                            pass
                        current_time = asyncio.get_event_loop().time()
                        if current_time - last_heartbeat >= heartbeat_interval:
                            # Authorization checkpoint, on the heartbeat cadence
                            # rather than every 2s poll: a long stream must not
                            # outlive the access that started it, but it also must
                            # not issue a database round-trip per poll.
                            if stream_user_id is not None:
                                ok, _live_version, deny_reason = await check_authorization_fresh(
                                    stream_user_id
                                )
                                if not ok:
                                    cancel_event.set()
                                    logger.info(
                                        "[SQL_AGENT_API] request_id=%s terminating stream for "
                                        "user_id=%s reason=%s",
                                        request_id, stream_user_id, deny_reason,
                                    )
                                    invalidate_user_sql_agent(
                                        stream_user_id, deny_reason or "authorization_changed"
                                    )
                                    yield evt({
                                        "type": "error",
                                        "error_code": "AUTHORIZATION_CHANGED",
                                        "message": "Your access to the SQL assistant has changed. "
                                                   "Please sign in again.",
                                        "retryable": False,
                                    })
                                    yield evt({"type": "complete", "success": False})
                                    completion_sent = True
                                    stream_success = False
                                    break
                            yield evt({"type": "heartbeat",
                                       "timestamp": datetime.utcnow().isoformat() + "Z"})
                            last_heartbeat = current_time
                        continue

                    if update is _STREAM_SENTINEL:
                        break

                    stages.observe(update)

                    try:
                        # A detected violation is NOT forwarded verbatim: the
                        # agent describes what it refused, the policy layer
                        # decides what happens to the account. Intercept BEFORE
                        # yielding, or the agent's own wording reaches the
                        # browser — which is how users were told they were
                        # blocked while nothing had happened.
                        if update.get("type") == "error" and update.get("security_violation"):
                            decision = await _handle_security_denial(
                                current_user, query,
                                update.get("security_reason") or "",
                                (asyncio.get_event_loop().time() - stream_start_time) * 1000,
                                session_id=getattr(
                                    getattr(agent_instance, "conversation_memory", None),
                                    "current_session_id", None),
                                transport=TRANSPORT_SSE,
                                actor=update.get("security_block_actor",
                                                 "user"),
                                reason_code=update.get("security_reason_code",
                                                       REASON_FORBIDDEN_SQL),
                            )
                            err = decision["error"]
                            yield evt({"type": "error",
                                       "error_code": err["code"],
                                       "message": err["message"],
                                       "reference_id": err["reference_id"],
                                       "retryable": False})
                            yield evt({"type": "complete", "success": False})
                            completion_sent = True
                            stream_success = False
                            break

                        # A document the graph rendered but could not persist:
                        # finish it BEFORE the completion event is serialised,
                        # so the client learns about it in the same event and
                        # the conversation message records it. The payload
                        # itself (raw bytes) never travels here — the agent
                        # holds it and complete_turn_document takes it.
                        if update.get("type") == "complete" and update.get("has_document"):
                            update.pop("has_document", None)
                            streamed_response, streamed_block = \
                                await complete_turn_document(
                                    agent_instance, current_user,
                                    update.get("response") or final_response or "")
                            if streamed_block:
                                update["artifact"] = streamed_block
                                stream_artifact_block = streamed_block
                            if streamed_response:
                                update["response"] = streamed_response
                        update.pop("has_document", None)

                        # Format as SSE (request_id + sequence on every event)
                        yield evt(update)

                        # Capture final response
                        if update.get("type") == "complete":
                            # Check if response is in completion message (some agents include it)
                            if "response" in update:
                                final_response = update.get("response")
                            # If we don't have final_response yet but have accumulated content, use that
                            elif final_response is None or len(final_response) == 0:
                                # Check if we have accumulated content from previous chunks
                                # This handles cases where content was streamed but not captured
                                logger.debug(f"[SQL_AGENT_API] Completion received but no response field, checking accumulated content")
                            # Check if the completion message indicates success
                            if update.get("success") is not False:  # True or undefined means success
                                stream_success = True
                            else:
                                stream_success = False
                            completion_sent = True
                            logger.debug(f"[SQL_AGENT_API] Completion received: success={stream_success}, final_response_length={len(final_response) if final_response else 0}, response_length={update.get('response_length', 'N/A')}")
                        elif update.get("type") == "content":
                            # Accumulate content chunks - this is how we build the final response
                            if final_response is None:
                                final_response = ""
                            content_chunk = update.get("content", "")
                            final_response += content_chunk
                            logger.debug(f"[SQL_AGENT_API] Content chunk received: {len(content_chunk)} chars, total accumulated: {len(final_response)} chars")
                        elif update.get("type") == "error":
                            # Security violations were handled above, keyed on a
                            # structured flag. Nothing here inspects wording:
                            # prose-based authorization is what broke this path.
                            stream_success = False
                            final_response = update.get("message", "An error occurred")
                            # Send completion after error to properly close stream
                            yield evt({"type": "complete", "success": False})
                            completion_sent = True
                            break
                    except Exception as yield_error:
                        logger.error(f"[SQL_AGENT_API] Error yielding update: {str(yield_error)}", exc_info=True)
                        try:
                            yield evt({"type": "error", "error_code": "STREAM_ERROR",
                                       "message": "Streaming failed. Please retry.", "retryable": True})
                            yield evt({"type": "complete", "success": False})
                            completion_sent = True
                        except Exception:
                            pass
                        break

                # An explicit terminal event is REQUIRED by the contract —
                # clients never infer completion from content length.
                if not completion_sent:
                    try:
                        yield evt({"type": "complete", "success": stream_success,
                                   "response": final_response[:500] if final_response else None})
                        completion_sent = True
                    except Exception as completion_error:
                        logger.error(f"[SQL_AGENT_API] Error sending completion: {str(completion_error)}")

            except Exception as e:
                logger.error(f"[SQL_AGENT_API] Streaming error (outer): {str(e)}", exc_info=True)
                if not completion_sent:
                    try:
                        yield evt({"type": "error", "error_code": "STREAM_ERROR",
                                   "message": "Streaming failed. Please retry.", "retryable": True})
                        yield evt({"type": "complete", "success": False})
                        completion_sent = True
                    except Exception as close_error:
                        logger.error(f"[SQL_AGENT_API] Error closing stream: {str(close_error)}")
                stream_success = False
                final_response = None
            finally:
                # Stop the agent thread at its next node boundary and free the slot
                # (also runs on client disconnect / GeneratorExit).
                cancel_event.set()
                _finish_request(request_id,
                                "cancelled" if was_cancelled
                                else ("completed" if stream_success else "failed"))
                if worker_done is not None and not worker_done.is_set():
                    _spawn_background(_release_stream_resources_when_done(
                        worker_done,
                        user_lock if lock_acquired else None,
                        sem_acquired,
                        request_id,
                    ))
                    lock_acquired = False
                    sem_acquired = False
                if lock_acquired and user_lock is not None:
                    try:
                        user_lock.release()
                    except RuntimeError:
                        pass
                if sem_acquired:
                    _sql_agent_semaphore.release()

                if not completion_sent:
                    try:
                        yield evt({"type": "complete", "success": stream_success})
                        completion_sent = True
                    except Exception:
                        pass

                # PRIVACY: structured status and stage NAMES only — never the
                # query, the generated SQL, or any part of the response body.
                try:
                    stages.finish()
                    slowest_step, slowest_ms = stages.slowest
                    ttfc = ("%.0f" % ((stages.first_chunk_at - stream_start_time) * 1000.0)
                            if stages.first_chunk_at is not None else "none")
                    ttfu = ("%.0f" % ((stages.first_update_at - stream_start_time) * 1000.0)
                            if stages.first_update_at is not None else "none")
                    stage_summary = stages.summary()
                except Exception:  # pragma: no cover - instrumentation must never break the stream
                    slowest_step, slowest_ms, ttfc, ttfu, stage_summary = None, 0.0, "?", "?", "?"

                logger.info(
                    "[SQL_AGENT_API] request_id=%s user_id=%s stream finished status=%s "
                    "duration=%.1fs response_chars=%d timeout_source=%s "
                    "time_to_first_update_ms=%s time_to_first_chunk_ms=%s "
                    "slowest_stage=%s slowest_stage_ms=%.0f stages[%s]",
                    request_id,
                    stream_user_id,
                    "cancelled" if was_cancelled else ("completed" if stream_success else "failed"),
                    asyncio.get_event_loop().time() - stream_start_time,
                    len(final_response) if final_response else 0,
                    timeout_source or "none",
                    ttfu, ttfc,
                    slowest_step, slowest_ms, stage_summary,
                )

                # Conversation-memory fallback for history persistence only
                if not final_response:
                    try:
                        if hasattr(agent_instance, 'conversation_memory') and agent_instance.conversation_memory:
                            recent_messages = agent_instance.conversation_memory.get_recent_messages(limit=5)
                            for msg in reversed(recent_messages or []):
                                if msg.get('role') == 'assistant' and msg.get('content'):
                                    final_response = msg.get('content')
                                    break
                    except Exception:
                        pass
            
            # Log to audit and save history after streaming completes.
            # Note: do NOT require final_response here - even queries that produced an
            # empty/failed response must still appear in the user's sidebar history.
            if stream_user_id is not None:
                try:
                    stream_time_ms = (asyncio.get_event_loop().time() - stream_start_time) * 1000
                    session_id = agent_instance.conversation_memory.current_session_id

                    # Plain values only — the request session was closed before the
                    # stream started, so these must not be ORM attribute reads.
                    logger.info(
                        "[SQL_AGENT_API] request_id=%s persisting history user_id=%s success=%s "
                        "duration_ms=%.0f response_chars=%d",
                        request_id, stream_user_id, stream_success, stream_time_ms,
                        len(final_response) if final_response else 0,
                    )

                    async def _persist_outcome():
                        # Audit first, then sidebar history. Each opens its OWN
                        # short-lived session; a failure in one must not lose
                        # the other.
                        try:
                            await log_chatbot_query(
                                user_id=stream_user_id,
                                username=stream_username,
                                query=query,
                                response=final_response,
                                success=stream_success,
                                error_message=None if stream_success else "Streaming error",
                                processing_time_ms=stream_time_ms,
                                session_id=session_id
                            )
                        except Exception as audit_error:
                            logger.error(f"[SQL_AGENT_API] Error logging audit: {audit_error}", exc_info=True)
                        try:
                            # The shared lifecycle funnel. Already inside the
                            # shielded _persist_outcome, so finalize_turn's own
                            # shield is redundant here but harmless — one
                            # funnel beats a bespoke persist per transport.
                            await finalize_turn(
                                agent_instance,
                                user_id=stream_user_id,
                                query=query,
                                response=final_response,
                                session_id=session_id,
                                success=stream_success,
                                processing_time_ms=stream_time_ms,
                                metadata=({"artifact": stream_artifact_block}
                                          if stream_artifact_block else {}),
                                conversation_id=stream_conversation_id,
                                request_label=request_id,
                            )
                        except Exception as history_error:
                            logger.error(f"[SQL_AGENT_API] Error saving history: {history_error}", exc_info=True)

                    # Awaited so the commit normally lands within the request
                    # lifecycle (the response is already fully streamed, so this
                    # adds no user-facing latency) — but through the shield
                    # helper, because the browser aborts the connection the
                    # instant it sees the terminal event, and that abort used to
                    # cancel this very save mid-commit. See
                    # await_persistence_despite_disconnect.
                    await await_persistence_despite_disconnect(_persist_outcome(), request_id)

                except asyncio.CancelledError:
                    raise
                except Exception as audit_error:
                    logger.error(f"[SQL_AGENT_API] Error persisting outcome: {str(audit_error)}", exc_info=True)
        
        return StreamingResponse(
            stream_query(), 
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "Transfer-Encoding": "chunked"
            }
        )
    
    except Exception as e:
        logger.error(f"[SQL_AGENT_API] Stream endpoint error: {str(e)}", exc_info=True)
        # Exception targets are cleared when the except block exits. Capture
        # the safe text now so the async generator does not later raise
        # NameError while trying to report the original endpoint failure.
        error_message = str(e)
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': error_message})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")


async def sql_agent_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for SQL Agent queries with real-time streaming.
    This is registered separately in backend.main at /ws/sql-agent
    Uses user-specific persistent conversation memory.
    """
    # Origin validation BEFORE accept: cross-site pages cannot forge Origin
    origin = websocket.headers.get("origin")
    if origin:
        try:
            from backend.security.origins import approved_origin_hosts
            from backend.security.origins import origin_host as _origin_host

            origin_host = _origin_host(origin)
            if origin_host not in approved_origin_hosts(
                request_host=websocket.headers.get("host") or ""
            ):
                logger.warning(f"[SQL_AGENT_WS] Rejected cross-origin connection from {origin_host}")
                await websocket.close(code=1008, reason="Origin not allowed")
                return
        except Exception:
            await websocket.close(code=1008, reason="Origin not allowed")
            return

    await websocket.accept()
    logger.info("[SQL_AGENT_WS] WebSocket connection established")

    # Check authentication and get current user. The browser sends the
    # HttpOnly session cookie on the handshake — parse it FIRST (the old
    # token-only path meant cookie-authenticated browsers could never
    # use this transport at all).
    current_user = None
    if AUTH_AVAILABLE:
        try:
            from backend.auth.auth_service import AuthService
            token = None
            cookie_header = websocket.headers.get("cookie", "")
            if cookie_header:
                for cookie in cookie_header.split(";"):
                    cookie = cookie.strip()
                    if cookie.startswith("access_token="):
                        token = cookie.split("=", 1)[1].strip()
                        break
            if not token:
                token = websocket.query_params.get("token") or websocket.headers.get("Authorization", "").replace("Bearer ", "")
            if token:
                payload = AuthService.decode_token(token)
                if payload:
                    # JWT 'sub' claim is a string, convert to int for user lookup
                    user_id_str = payload.get("sub")
                    if user_id_str:
                        try:
                            user_id = int(user_id_str)
                            from db_connection import get_db
                            async with db_manager.get_session() as db:
                                current_user = await AuthService.get_user_by_id(user_id, db)
                        except (ValueError, TypeError) as e:
                            logger.error(f"[SQL_AGENT_WS] Invalid user ID in token: {user_id_str}, error: {e}")
                            await websocket.close(code=1008, reason="Invalid token")
                            return
                        
                        # Check if user is valid after getting from database
                        if not current_user or not current_user.is_active or not current_user.can_use_chatbot:
                            await websocket.close(code=1008, reason="Unauthorized or chatbot access denied")
                            return
                    else:
                        await websocket.close(code=1008, reason="Invalid token")
                        return
                else:
                    await websocket.close(code=1008, reason="Invalid token")
                    return
            else:
                await websocket.close(code=1008, reason="Authentication required")
                return
        except Exception as e:
            logger.error(f"WebSocket auth error: {e}")
            await websocket.close(code=1008, reason="Authentication failed")
            return
    
    if not _sql_agent_available or _sql_agent_instance is None:
        await websocket.send_json({
            "type": "error",
            "message": "SQL Agent not available"
        })
        await websocket.close()
        return
    
    # Get or create user-specific agent instance for persistent memory
    agent_instance = _sql_agent_instance
    ws_user_id = None
    ws_permissions_version = None
    if AUTH_AVAILABLE and current_user:
        ws_user_id = current_user.id
        ws_permissions_version = int(getattr(current_user, "permissions_version", 1) or 1)
        agent_instance = _get_or_create_user_agent(
            ws_user_id, permissions_version=ws_permissions_version
        )

    try:
        while True:
            # Receive message from client — typed protocol:
            #   {"type":"query","request_id":..., "query":...}
            #   {"type":"cancel","request_id":...}
            # (legacy bare {"query": ...} still accepted)
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                continue

            msg_type = data.get("type") or ("query" if data.get("query") else None)

            # Re-authorize EVERY message. The handshake check is a point-in-time
            # decision; without this an administrator's revocation would never
            # reach an already-open socket.
            if ws_user_id is not None:
                ok, live_version, deny_reason = await check_authorization_fresh(ws_user_id)
                if not ok:
                    logger.info(
                        "[SQL_AGENT_WS] Closing socket for user_id=%s reason=%s",
                        ws_user_id, deny_reason,
                    )
                    try:
                        await websocket.send_json({
                            "type": "error",
                            "error_code": "AUTHORIZATION_CHANGED",
                            "message": "Your access to the SQL assistant has changed. "
                                       "Please sign in again.",
                            "retryable": False,
                        })
                    except Exception:
                        pass
                    invalidate_user_sql_agent(ws_user_id, deny_reason or "authorization_changed")
                    await websocket.close(code=1008, reason="AUTHORIZATION_CHANGED")
                    return
                if live_version is not None and live_version != ws_permissions_version:
                    # Still authorized, but the scope changed (role, pipelines).
                    # Rebuild the agent so it cannot serve the old scope.
                    logger.info(
                        "[SQL_AGENT_WS] user_id=%s permissions_version %s -> %s; rebuilding agent",
                        ws_user_id, ws_permissions_version, live_version,
                    )
                    ws_permissions_version = live_version
                    agent_instance = _get_or_create_user_agent(
                        ws_user_id, permissions_version=live_version
                    )

            if msg_type == "cancel":
                cancel_id = data.get("request_id")
                entry = _ACTIVE_REQUESTS.get(cancel_id) if isinstance(cancel_id, str) else None
                if entry is not None and (not (AUTH_AVAILABLE and current_user)
                                          or entry.get("user_id") in (None, current_user.id)
                                          or current_user.role == "admin"):
                    entry["cancel_event"].set()
                    entry["status"] = "cancelling"
                    logger.info(f"[SQL_AGENT_WS] request_id={cancel_id} cancel via WebSocket")
                continue

            if msg_type != "query":
                continue

            query, query_error = _bounded_query(data.get("query"))
            # Optional explicit conversation target. UUID-validated here;
            # OWNERSHIP is enforced inside the conversation service, which
            # falls back to session placement on any access failure — so a
            # forged id can never write into another user's conversation.
            ws_conversation_id = None
            _raw_conv = data.get("conversation_id")
            if _raw_conv:
                import uuid as _uuid_mod
                try:
                    ws_conversation_id = _uuid_mod.UUID(str(_raw_conv))
                except (ValueError, TypeError):
                    ws_conversation_id = None
            request_id = _normalize_request_id(data.get("request_id"))
            seq = 0

            def ws_evt(payload: dict) -> dict:
                nonlocal seq
                seq += 1
                return {**payload, "request_id": request_id, "sequence": seq}

            if query_error:
                await websocket.send_json(ws_evt({"type": "error",
                                                  "error_code": "INVALID_REQUEST",
                                                  "message": query_error}))
                continue

            if query.lower() in ["close", "exit", "quit"]:
                await websocket.send_json(ws_evt({"type": "close", "message": "Closing connection"}))
                break

            logger.info(f"[SQL_AGENT_WS] request_id={request_id} processing query ({len(query)} chars)")
            ws_start_time = asyncio.get_event_loop().time()

            cancel_event = threading.Event()
            # Idempotency: never execute the same request_id twice
            if not _register_request(request_id,
                                     current_user.id if (AUTH_AVAILABLE and current_user) else None,
                                     cancel_event):
                await websocket.send_json(ws_evt({
                    "type": "error", "error_code": "DUPLICATE_REQUEST",
                    "message": "This request was already accepted."}))
                continue

            # Send initial status
            await websocket.send_json(ws_evt({
                "type": "status", "message": "Processing query...", "step": "start"}))

            sem_acquired = False
            lock_acquired = False
            was_cancelled = False
            query_success = False
            accumulated_response = ""
            worker_done = None
            user_lock = _get_user_lock(current_user.id) if (AUTH_AVAILABLE and current_user) else None
            try:
                # Concurrency cap shared with the HTTP endpoints
                try:
                    await asyncio.wait_for(_sql_agent_semaphore.acquire(), timeout=_SEMAPHORE_WAIT_SECONDS)
                    sem_acquired = True
                except asyncio.TimeoutError:
                    await websocket.send_json(ws_evt({"type": "error", "error_code": "AGENT_BUSY",
                                                      "message": _BUSY_MESSAGE}))
                    await websocket.send_json(ws_evt({"type": "complete", "success": False}))
                    _finish_request(request_id, "failed")
                    continue

                if user_lock is not None:
                    await user_lock.acquire()
                    lock_acquired = True

                # The shared lifecycle — same call as REST and SSE, so this
                # transport can no longer drift (it used to skip the artifact
                # index AND discard rendered documents).
                await prepare_turn(agent_instance, current_user)

                # TRUE streaming bridge: agent runs in its own thread, updates are
                # forwarded as they arrive — the blocking generator is never
                # iterated on the event loop.
                update_queue, worker_done = _start_stream_thread(
                    agent_instance, query, cancel_event, asyncio.get_running_loop()
                )
                deadline = asyncio.get_event_loop().time() + SQL_AGENT_TOTAL_TIMEOUT

                accumulated_response = ""
                # A document completed during this turn, recorded on the
                # conversation message alongside the narrative.
                ws_artifact_block = None
                query_success = False
                last_heartbeat = asyncio.get_event_loop().time()
                while True:
                    if asyncio.get_event_loop().time() >= deadline:
                        cancel_event.set()
                        await websocket.send_json(ws_evt({"type": "error", "error_code": "QUERY_TIMEOUT",
                                                          "message": "Query took too long and was cancelled.",
                                                          "retryable": True}))
                        await websocket.send_json(ws_evt({"type": "complete", "success": False}))
                        break

                    # Server-side cancellation (cancel message or REST endpoint)
                    if cancel_event.is_set():
                        was_cancelled = True
                        await websocket.send_json(ws_evt({"type": "cancelled"}))
                        break

                    try:
                        update = await asyncio.wait_for(update_queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        now = asyncio.get_event_loop().time()
                        if now - last_heartbeat >= 20.0:
                            await websocket.send_json(ws_evt({
                                "type": "heartbeat",
                                "timestamp": datetime.utcnow().isoformat() + "Z"}))
                            last_heartbeat = now
                        continue  # still generating; loop re-checks deadline/cancel

                    if update is _STREAM_SENTINEL:
                        break

                    # Intercept a detected violation BEFORE relaying it: the
                    # agent states what it refused, the policy layer states what
                    # happened to the account. Keyed on a structured flag, never
                    # on wording.
                    if update.get("type") == "error" and update.get("security_violation"):
                        query_success = False
                        decision = await _handle_security_denial(
                            current_user, query,
                            update.get("security_reason") or "",
                            (asyncio.get_event_loop().time() - ws_start_time) * 1000,
                            session_id=getattr(
                                getattr(agent_instance, "conversation_memory", None),
                                "current_session_id", None),
                            transport=TRANSPORT_WEBSOCKET,
                            actor=update.get("security_block_actor", "user"),
                            reason_code=update.get("security_reason_code",
                                                   REASON_FORBIDDEN_SQL),
                        )
                        err = decision["error"]
                        await websocket.send_json(ws_evt({
                            "type": "error",
                            "error_code": err["code"],
                            "message": err["message"],
                            "reference_id": err["reference_id"],
                            "retryable": False,
                        }))
                        await websocket.send_json(ws_evt({"type": "complete", "success": False}))
                        # The socket that earned the block hangs up immediately.
                        # Other sockets for this account are not reachable from
                        # here; they die at their next message, when
                        # check_authorization_fresh() re-reads is_active.
                        if _policy_says_close(decision):
                            invalidate_user_sql_agent(
                                getattr(current_user, "id", 0), "user_blocked")
                            await websocket.close(code=1008, reason="ACCOUNT_BLOCKED")
                            return
                        break

                    # Finish pending document work BEFORE the completion event
                    # is sent, so the client learns about the artifact in the
                    # same frame. This transport used to skip this entirely —
                    # a document rendered over WS sat in _pending_document and
                    # was silently overwritten by the next turn.
                    if update.get("type") == "complete" and update.get("has_document"):
                        update.pop("has_document", None)
                        ws_doc_response, ws_artifact_block = await complete_turn_document(
                            agent_instance, current_user,
                            update.get("response") or accumulated_response or "")
                        if ws_artifact_block:
                            update["artifact"] = ws_artifact_block
                        if ws_doc_response:
                            update["response"] = ws_doc_response
                    update.pop("has_document", None)

                    await websocket.send_json(ws_evt(update))

                    # Capture final response
                    if update.get("type") == "complete":
                        if update.get("response"):
                            accumulated_response = update.get("response")
                        query_success = update.get("success") is not False
                    elif update.get("type") == "content":
                        accumulated_response += update.get("content", "")
                    elif update.get("type") == "error":
                        query_success = False

                    # Break if complete or error
                    if update.get("type") in ["complete", "error"]:
                        break

                # Persist to sidebar history (awaited so it reliably commits).
                # The WebSocket path previously never saved history at all - this was
                # a second reason some queries were missing from the sidebar.
                if AUTH_AVAILABLE and current_user:
                    ws_time_ms = (asyncio.get_event_loop().time() - ws_start_time) * 1000
                    ws_session_id = None
                    try:
                        ws_session_id = agent_instance.conversation_memory.current_session_id
                    except Exception:
                        pass
                    # The shared lifecycle funnel: shielded persist (the client
                    # has its answer and often closes the socket right now) plus
                    # the working-memory row pointer — same as REST and SSE.
                    await finalize_turn(
                        agent_instance,
                        user_id=current_user.id,
                        query=query,
                        response=accumulated_response or None,
                        session_id=ws_session_id,
                        success=query_success,
                        processing_time_ms=ws_time_ms,
                        metadata=({"artifact": ws_artifact_block}
                                  if ws_artifact_block else {}),
                        conversation_id=ws_conversation_id,
                        request_label=request_id,
                    )

            except WebSocketDisconnect:
                # Client vanished mid-query: stop the agent thread, then bail out
                cancel_event.set()
                raise
            except Exception as e:
                logger.error(f"[SQL_AGENT_WS] Query processing error: {str(e)}", exc_info=True)
                await websocket.send_json(ws_evt({
                    "type": "error", "error_code": "STREAM_ERROR",
                    "message": "The assistant could not process this query.", "retryable": True}))
            finally:
                cancel_event.set()
                _finish_request(request_id,
                                "cancelled" if was_cancelled
                                else ("completed" if query_success else "failed"))
                if worker_done is not None and not worker_done.is_set():
                    _spawn_background(_release_stream_resources_when_done(
                        worker_done,
                        user_lock if lock_acquired else None,
                        sem_acquired,
                        request_id,
                    ))
                    lock_acquired = False
                    sem_acquired = False
                if lock_acquired and user_lock is not None:
                    try:
                        user_lock.release()
                    except RuntimeError:
                        pass
                if sem_acquired:
                    _sql_agent_semaphore.release()

    except WebSocketDisconnect:
        logger.info("[SQL_AGENT_WS] WebSocket disconnected")
    except Exception as e:
        logger.error(f"[SQL_AGENT_WS] WebSocket error: {str(e)}", exc_info=True)
        try:
            await websocket.send_json({
                "type": "error",
                "message": f"WebSocket error: {str(e)}"
            })
        except:
            pass
    finally:
        try:
            await websocket.close()
        except:
            pass


@router.post("/requests/{request_id}/cancel")
async def cancel_sql_agent_request(
    request_id: str,
    http_request: Request,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None,
    _csrf: None = Depends(require_sql_agent_csrf),
):
    """Server-side cancellation: stops the LLM/SQL worker thread at its next
    boundary via the request's registered cancel event (the stream then emits
    a terminal 'cancelled' event). Owner or admin only."""
    entry = _ACTIVE_REQUESTS.get(request_id)
    if entry is None:
        return JSONResponse(status_code=404, content=_error_body(
            "REQUEST_NOT_FOUND", "No such request.", retryable=False))

    if AUTH_AVAILABLE and current_user:
        if entry.get("user_id") not in (None, current_user.id) and current_user.role != "admin":
            return JSONResponse(status_code=403, content=_error_body(
                "ACCESS_DENIED", "You cannot cancel another user's request."))

    if entry["status"] != "running":
        return {"success": True, "request_id": request_id, "status": entry["status"]}

    entry["cancel_event"].set()
    entry["status"] = "cancelling"
    logger.info("[SQL_AGENT_API] request_id=%s cancel requested by user_id=%s",
                request_id, current_user.id if (AUTH_AVAILABLE and current_user) else None)
    return {"success": True, "request_id": request_id, "status": "cancelling"}


# Health-result cache: the endpoint must stay LIGHTWEIGHT (no LLM calls, no
# model init, no expensive SQL) — one SELECT 1, cached briefly.
_health_cache = {"at": 0.0, "body": None}
_HEALTH_CACHE_SECONDS = 15.0


@router.get("/health")
async def sql_agent_health(response: Response):
    """Lightweight component health: never invokes the LLM or heavy SQL."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    now = asyncio.get_event_loop().time()
    if _health_cache["body"] is not None and now - _health_cache["at"] < _HEALTH_CACHE_SECONDS:
        return _health_cache["body"]

    components = {
        "model": "ready" if (_sql_agent_available and _sql_agent_instance is not None) else "unavailable",
        "database": "unknown",
        "history": "ready" if AUTH_AVAILABLE else "unavailable",
    }

    if _sql_agent_instance is not None:
        try:
            # SELECT 1 on a threadpool with a hard 3s cap — the only I/O here
            db_ok = await asyncio.wait_for(
                run_in_threadpool(_sql_agent_instance.test_connection), timeout=3.0)
            components["database"] = "ready" if db_ok else "error"
        except Exception:
            components["database"] = "error"

    status = "operational" if all(v == "ready" for k, v in components.items()
                                  if k in ("model", "database")) else "degraded"
    body = {
        "available": _sql_agent_available and _sql_agent_instance is not None,
        "status": status,
        "components": components,
        "checked_at": datetime.utcnow().isoformat() + "Z",
    }
    _health_cache["at"] = now
    _health_cache["body"] = body
    return body


@router.get("/schema")
async def sql_agent_schema(
    current_user: "User" = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None,
):
    """Get database schema description.

    Authenticated: the schema names every table and column the assistant can
    reach, which is reconnaissance for anyone probing the deployment.
    """
    logger.debug("[SQL_AGENT_API] Schema request received")

    if not _sql_agent_available or _sql_agent_instance is None:
        logger.warning("[SQL_AGENT_API] Schema request - agent not available")
        raise HTTPException(status_code=503, detail="SQL Agent not available")

    try:
        schema = _sql_agent_instance.get_schema()
        logger.info(f"[SQL_AGENT_API] Schema retrieved successfully ({len(schema)} chars)")
        return {
            "success": True,
            "schema": schema
        }
    except Exception as e:
        logger.error(f"[SQL_AGENT_API] Schema retrieval error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/session/new")
async def sql_agent_new_session(
    current_user: "User" = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None,
):
    """Start a new conversation session for the calling user.

    Operates on the caller's own agent. It previously ran unauthenticated
    against the shared global instance, so any caller could reset, enumerate
    or load another user's conversation.
    """
    logger.info("[SQL_AGENT_API] New session request received")

    if not _sql_agent_available:
        logger.warning("[SQL_AGENT_API] New session - agent not available")
        raise HTTPException(status_code=503, detail="SQL Agent not available")

    try:
        agent_instance = _scoped_agent(current_user)
        memory = agent_instance.conversation_memory
        session_id = memory.start_session()
        # start_session RELOADS the user's persistent session; on its own this
        # endpoint therefore returned the same accumulated conversation and
        # called it new. Clear it, so "new session" means what it says and a
        # caller has some way to start over.
        memory.reset_session()
        logger.info(f"[SQL_AGENT_API] New session created for user {_uid(current_user)}")
        return {
            "success": True,
            "session_id": session_id,
            "message": "New session created"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SQL_AGENT_API] New session error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not create a session.")


@router.get("/sessions")
async def sql_agent_list_sessions(
    current_user: "User" = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None,
):
    """List the calling user's conversation sessions.

    Scoped to the caller's own agent. Unauthenticated enumeration of the shared
    global instance previously exposed other users' session identifiers.
    """
    logger.debug("[SQL_AGENT_API] List sessions request received")

    if not _sql_agent_available:
        logger.warning("[SQL_AGENT_API] List sessions - agent not available")
        raise HTTPException(status_code=503, detail="SQL Agent not available")

    try:
        agent_instance = _scoped_agent(current_user)
        memory = agent_instance.conversation_memory
        sessions = memory.list_sessions()
        logger.info(
            "[SQL_AGENT_API] Listed %d sessions for user %s",
            len(sessions), _uid(current_user),
        )
        return {
            "success": True,
            "sessions": sessions,
            "current_session": memory.current_session_id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SQL_AGENT_API] List sessions error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not list sessions.")


@router.post("/session/load")
async def sql_agent_load_session(
    request: dict,
    current_user: "User" = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None,
):
    """Load one of the calling user's own sessions.

    The session is resolved against the caller's agent, so a session_id
    belonging to another user simply does not exist here — ownership is a
    property of which store is consulted, not a check that can be forgotten.
    """
    logger.info("[SQL_AGENT_API] Load session request received")

    if not _sql_agent_available:
        logger.warning("[SQL_AGENT_API] Load session - agent not available")
        raise HTTPException(status_code=503, detail="SQL Agent not available")

    session_id = request.get("session_id")
    if not session_id or not isinstance(session_id, str):
        logger.warning("[SQL_AGENT_API] Load session - session_id missing")
        raise HTTPException(status_code=400, detail="session_id is required")

    try:
        agent_instance = _scoped_agent(current_user)
        loaded = agent_instance.conversation_memory.load_session(session_id)
        if loaded:
            logger.info(
                "[SQL_AGENT_API] Session loaded for user %s", _uid(current_user)
            )
            return {
                "success": True,
                "session_id": session_id,
                "message": "Session loaded successfully"
            }
        logger.warning("[SQL_AGENT_API] Session not found for user %s", _uid(current_user))
        raise HTTPException(status_code=404, detail="Session not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SQL_AGENT_API] Load session error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load the session.")




# Export models and document rendering live in sql_agent/services/
# export_builders.py: the agent's graph nodes render documents too, and
# importing this module from a node would be a cycle (routes imports the
# agent). The endpoints below are the HTTP boundary over that code — they
# wrap the returned bytes in the same Response they always did.
from sql_agent.services.export_builders import (   # noqa: E402
    ExportRequest,
    _ARABIC_CHAR,
    _build_pdf_export,
    _build_word_export,
    _export_semaphore,
    _sanitize_export,
    detect_language,
    render_and_register,
)

@router.post("/export/pdf")
async def export_to_pdf(
    request: ExportRequest,
    http_request: Request,
    current_user: User = Depends(require_chatbot_access()),
    _csrf: None = Depends(require_sql_agent_csrf),
):
    """Export chatbot response to PDF format (auth + CSRF + size limits +
    markup-injection prevention + bounded concurrency + audit log line)."""
    safe_title, safe_content, safe_date = _sanitize_export(request)
    logger.info("[EXPORT] format=pdf user_id=%s content_chars=%d",
                current_user.id if current_user else None, len(request.content))
    async with _export_semaphore:
        pdf_bytes = await asyncio.wait_for(
            run_in_threadpool(_build_pdf_export, safe_title, safe_content, safe_date,
                              current_user.username if current_user else 'System'),
            timeout=60.0,
        )
    return await _respond_with_export(
        pdf_bytes, "pdf", safe_title, safe_date, request, current_user)






@router.post("/export/word")
async def export_to_word(
    request: ExportRequest,
    http_request: Request,
    current_user: User = Depends(require_chatbot_access()),
    _csrf: None = Depends(require_sql_agent_csrf),
):
    """Export chatbot response to Word (auth + CSRF + size limits + bounded
    concurrency; docx runs are plain text — no markup injection possible)."""
    safe_title, _safe_content, safe_date = _sanitize_export(request)
    logger.info("[EXPORT] format=word user_id=%s content_chars=%d",
                current_user.id if current_user else None, len(request.content))
    async with _export_semaphore:
        word_bytes = await asyncio.wait_for(
            run_in_threadpool(_build_word_export, safe_title, request.content, safe_date,
                              current_user.username if current_user else 'System'),
            timeout=60.0,
        )
    return await _respond_with_export(
        word_bytes, "word", safe_title, safe_date, request, current_user)


_DURABLE_MEMORY_MAX_CHARS = 500


# ---------------------------------------------------------------------------
# THE TURN LIFECYCLE. One contract, three transports.
#
# REST, SSE and WebSocket each used to hand-roll their own pre/post-turn
# sequence, and the sequences drifted: the artifact index and durable memory
# were loaded only on REST — so on SSE (the transport the browser uses)
# "same report but camera 3" silently bound to recency instead of lineage,
# and WebSocket discarded rendered documents outright. The drift IS the bug
# class; these functions are its fix. A transport may add framing around
# them; it may not reimplement them.
#
#   prepare_turn()          before the graph runs, inside the user's lock
#   complete_turn_document() at the transport's completion boundary
#   finalize_turn()          after the terminal event has been sent
# ---------------------------------------------------------------------------

async def prepare_turn(agent_instance, current_user) -> None:
    """Load everything a turn needs before the graph runs — every transport.

    Owner-scoped artifact index + source-SQL map (what "the last report" may
    refer to, and which query it came from), and the user's durable memories.
    Authorization freshness stays transport-specific: REST checks via
    dependency, the streams via check_authorization_fresh on their heartbeat.

    Never fatal: a failed load costs reference resolution, not the turn.
    """
    await _refresh_artifact_index(agent_instance, current_user)
    await _refresh_identity_index(agent_instance, current_user)
    try:
        session_id = agent_instance.conversation_memory.current_session_id
    except Exception:
        session_id = None
    agent_instance.set_durable_memory(
        await _durable_memory_section(current_user, session_id))


async def complete_turn_document(agent_instance, current_user, response_text,
                                 conversation_id=None):
    """Finish document work the graph left pending. Returns (text, block).

    Runs at the transport's completion boundary — BEFORE the terminal event
    is serialized on a stream, so the client learns about the document in the
    same event — and must be called on every transport: a transport that
    skips it discards rendered documents (WebSocket did exactly that).
    """
    return await _finish_pending_document(
        agent_instance, current_user, response_text,
        conversation_id=conversation_id)


async def finalize_turn(agent_instance, *, user_id, query, response, session_id,
                        success, processing_time_ms, metadata,
                        conversation_id=None, request_label="turn") -> None:
    """Persist the turn and let working memory point at it — every transport.

    History is written through the disconnect shield (the client often hangs
    up the moment it has its answer; the commit must survive that), and the
    resulting row id is recorded into working_context.last_result so a later
    "show me all of those" has a durable reference.
    """
    saved_history_id = await await_persistence_despite_disconnect(
        persist_query_history(
            user_id=user_id, query=query, response=response,
            session_id=session_id, success=success,
            processing_time_ms=processing_time_ms, metadata=metadata or {},
            conversation_id=conversation_id,
        ),
        request_label,
    )
    # OFF THE EVENT LOOP. _remember_result_row_id writes the session file:
    # read + fsync + os.replace, guarded by a threading.Lock that the agent's
    # own stream THREAD also takes for its message saves. Calling it directly
    # from async code makes the event loop wait on a lock held by a worker
    # thread — which stalls every request in the process, including
    # /health/live. Observed live 2026-08-30: six minutes of total silence,
    # no logs, not even the loop-lag watchdog (it could not run either).
    await run_in_threadpool(_remember_result_row_id, agent_instance,
                            saved_history_id)


async def _durable_memory_section(current_user, session_id) -> str:
    """The user's stored memories, as a short block for the prompts.

    `get_context_for_query` has existed and been maintained for a long time
    with NO caller on the query path: memories were written on every turn and
    read by nothing, so the agent forgot preferences it had explicitly
    recorded. All five prompts already consume `conversation_context`, so
    appending here needs no prompt changes at all.

    Bounded hard. This text goes into every prompt, and an unbounded memory
    list would crowd out the actual question on a small local model.
    """
    user_id = getattr(current_user, "id", None)
    if user_id is None:
        return ""
    try:
        async with db_manager.get_session() as db:
            context = await user_query_history_service.get_context_for_query(
                db, user_id=user_id, session_id=session_id,
                recent_limit=0, memory_limit=6)
    except Exception as e:
        logger.warning("[MEMORY] durable memory unavailable: %s", e)
        observability.observe_memory_failure("durable_memory_load")
        return ""

    memories = context.get("memories") or []
    if not memories:
        return ""
    lines = []
    for memory in memories:
        entry = f"- {memory.get('key')}: {memory.get('value')}"
        if len("\n".join(lines)) + len(entry) > _DURABLE_MEMORY_MAX_CHARS:
            break
        lines.append(entry)
    if not lines:
        return ""
    return ("\n[durable memory - things this user told you earlier. Use them "
            "only if relevant; never quote this label]\n" + "\n".join(lines)
            + "\n[end of durable memory]\n")


def _remember_result_row_id(agent_instance, history_id) -> None:
    """Attach the history row id to the working context's last_result.

    The preview in working memory is deliberately tiny; this is the pointer
    back to the full result, which user_query_history already stores under the
    conversation-data policy. Written after the fact because the row does not
    exist until the turn has been persisted.

    Never fatal: losing the pointer costs a future "show me all of those",
    not this turn.
    """
    if agent_instance is None or history_id is None:
        return
    try:
        memory = getattr(agent_instance, "conversation_memory", None)
        if memory is None:
            return
        context = memory.get_working_context()
        last_result = context.get("last_result")
        if isinstance(last_result, dict) and last_result.get("history_id") is None:
            memory.update_working_context(
                last_result=dict(last_result, history_id=int(history_id)))
    except Exception as e:
        logger.warning("[MEMORY] could not record the result row id: %s", e)


async def _refresh_identity_index(agent_instance, current_user) -> None:
    """Hand the agent the enrolled people it may resolve a name against.

    Root cause of "track ali" answering that Ali did not exist: the resolver's
    only pool was `SELECT DISTINCT name FROM faces` — the DETECTION rows — so
    a person who was enrolled but not yet detected was invisible.

    Read through the ORM rather than the agent's SQL path because `identities`
    is not in that path's table allowlist, and keeping it out is what stops
    the model reading the table directly.

    Never fatal: without the index the agent falls back to detected names,
    which is the behaviour that shipped before this — a worse answer, not an
    unsafe one.
    """
    if agent_instance is None:
        return
    try:
        if getattr(current_user, "id", None) is None:
            agent_instance.set_identity_index([])
            return
        from sqlalchemy import select

        from db_models import Identity, IdentityStatus, IdentityType

        async with db_manager.get_session() as db:
            rows = await db.execute(
                select(Identity.id, Identity.display_name)
                .where(Identity.type == IdentityType.KNOWN,
                       Identity.status == IdentityStatus.ACTIVE,
                       Identity.display_name.isnot(None))
                .order_by(Identity.display_name)
                .limit(500))
            agent_instance.set_identity_index(
                [{"identity_id": str(identity_id), "display_name": display}
                 for identity_id, display in rows.all() if display])
    except Exception as e:
        logger.warning("[IDENTITY] could not load the identity index: %s", e)
        try:
            agent_instance.set_identity_index([])
        except Exception:
            pass


async def _refresh_artifact_index(agent_instance, current_user) -> None:
    """Hand the agent the documents this caller may refer to.

    Owner-scoped by the query itself, so the candidate set the planner sees
    physically cannot contain another user's document. Never fatal: without
    an index the agent simply cannot resolve "the last report", which is a
    worse answer, not an unsafe one.
    """
    if agent_instance is None:
        return
    try:
        user_id = getattr(current_user, "id", None)
        if user_id is None:
            agent_instance.set_artifact_index([])
            agent_instance.set_artifact_sql_index({})
            return
        # THE conversation boundary. Every artifact row has conversation_id
        # NULL, so the column-based filter matches nothing; the session's
        # created_at is rewritten by reset_session and works on all three
        # transports. None means "cannot tell", which keeps today's
        # behaviour rather than hiding everything.
        try:
            since = agent_instance.conversation_memory.session_started_at()
        except Exception:
            since = None

        async with db_manager.get_session() as db:
            agent_instance.set_artifact_index(
                await artifact_registry.list_recent_artifacts(
                    db, user_id, limit=3, since=since))
            # Kept SEPARATE from the list above, which feeds a prompt and must
            # stay free of SQL. This map only reaches the modification node.
            agent_instance.set_artifact_sql_index(
                await artifact_registry.get_artifact_source_sql(
                    db, user_id, limit=3, since=since))
    except Exception as e:
        logger.warning("[ARTIFACT] could not load the artifact index: %s", e)
        observability.observe_memory_failure("artifact_index_refresh")
        try:
            agent_instance.set_artifact_index([])
            agent_instance.set_artifact_sql_index({})
        except Exception:
            pass


async def _finish_pending_document(agent_instance, current_user, response_text,
                                   conversation_id=None):
    """Finish document work the graph left behind. Returns (text, block).

    BOTH transports call this. The REST route and the streaming route used to
    be a natural place for two divergent implementations — and the streaming
    one simply did not exist, so on the transport the browser actually uses,
    "make that a PDF" answered with a confirmation and no document.
    """
    pending = {}
    try:
        pending = agent_instance.take_pending_document() if agent_instance else {}
    except Exception as e:
        logger.warning("[ARTIFACT] could not read pending document work: %s", e)
    if not pending:
        return response_text, None

    block = None
    had_artifact_work = bool(pending.get("artifact_payload")
                             or pending.get("translation_request"))
    if pending.get("translation_request"):
        translated, block = await _complete_translation(
            pending["translation_request"], current_user,
            agent_instance=agent_instance, conversation_id=conversation_id)
        if translated:
            response_text = translated
    if pending.get("artifact_payload"):
        block = await _persist_agent_artifact(
            pending["artifact_payload"], current_user,
            agent_instance=agent_instance, conversation_id=conversation_id)
        if block is None:
            # The node already wrote "you can download it below" — it had no
            # way to know this would fail. Leaving that in place promises a
            # link that is not there, which is worse than saying so.
            response_text = (
                "I built the document but couldn't save it, so there's no "
                "download link. The report is above — ask me again and I'll "
                "retry.")
    if had_artifact_work:
        observability.observe_document_completion(
            "completed" if block else "failed")
    return response_text, block


async def _complete_translation(request: dict, current_user, agent_instance=None,
                                conversation_id=None):
    """Carry out a translation the graph decided on. Returns (text, block).

    Ownership is re-checked HERE, against the database, even though the
    planner could only choose from a candidate set this user owns. That set is
    built from the same user_id, so this is defence in depth — but it is the
    check that actually decides, and it is the reason a planner that somehow
    named a foreign id gets a refusal rather than someone else's report.
    """
    artifact_id = (request or {}).get("artifact_id")
    language = (request or {}).get("language") or "en"
    user_id = getattr(current_user, "id", None)
    if not artifact_id:
        return None, None

    try:
        async with db_manager.get_session() as db:
            artifact = await artifact_registry.get_owned_artifact(db, artifact_id, user_id)
            if artifact is None:
                logger.warning("[TRANSLATE] refused: artifact %s is not this user's",
                               artifact_id)
                return ("I couldn't find that report. It may have been removed.", None)
            source = artifact.source_content
            source_sql = artifact.source_sql
            source_result_id = artifact.source_result_id
            artifact_type = request.get("format") or artifact.type or "pdf"
            title = artifact.title
    except Exception as e:
        logger.error("[TRANSLATE] could not load the artifact: %s", e)
        return None, None

    if not source:
        # Older rows predate source_content. Regenerating is honest; parsing
        # the PDF back would be unreliable and would destroy Arabic shaping.
        return ("I have that report but not the text it was written from, so I "
                "can't translate it directly. Ask me the question again and I'll "
                "produce it in the language you want.", None)

    translated = await run_in_threadpool(translate_document_text, source, language)

    try:
        from ..services import export_builders

        class _Request:
            pass

        rendered_request = _Request()
        rendered_request.content = translated
        rendered_request.title = title
        rendered_request.timestamp = ""
        safe_title, safe_content, safe_date = export_builders.sanitize_export(
            rendered_request)
        if artifact_type == "word":
            payload = await run_in_threadpool(
                export_builders.build_word_bytes, safe_title, translated,
                safe_date, "Agent")
        else:
            artifact_type = "pdf"
            payload = await run_in_threadpool(
                export_builders.build_pdf_bytes, safe_title, safe_content,
                safe_date, "Agent")
    except Exception as e:
        logger.error("[TRANSLATE] re-render failed: %s", e, exc_info=True)
        # The translation itself succeeded — hand it over as text rather than
        # losing the work because a renderer failed.
        return translated, None

    block = await _persist_agent_artifact(
        {"bytes": payload, "type": artifact_type, "title": title,
         "language": language, "source_content": translated,
         "source_sql": source_sql, "source_result_id": source_result_id,
         # The translation IS derived from that document. This is the lineage
         # that makes "same report but camera 3" resolvable afterwards.
         "parent_artifact_id": artifact_id},
        current_user, agent_instance=agent_instance, conversation_id=conversation_id)

    confirmation = ("لقد أعددت **{t}** بالعربية. يمكنك تنزيله أدناه."
                    if language == "ar"
                    else "I've prepared **{t}** in English. You can download it below."
                    ).format(t=title)
    return (confirmation if block else translated), block


async def _persist_agent_artifact(payload: dict, current_user, agent_instance=None,
                                  conversation_id=None) -> Optional[dict]:
    """Commit a document the agent rendered, and describe it for the client.

    Graph nodes are synchronous, so the node produced bytes and this — the
    async layer — writes them, through the SAME render_and_register the HTTP
    export uses. Returns the block the UI needs, or None.

    Failing here costs the user the download link, never the answer: the
    narrative is already in the response.
    """
    if not payload or not payload.get("bytes"):
        return None
    user_id = getattr(current_user, "id", None)
    try:
        async with db_manager.get_session() as db:
            artifact_id = await render_and_register(
                db, payload=payload["bytes"], artifact_type=payload.get("type", "pdf"),
                title=payload.get("title") or "Intelligence Report",
                language=payload.get("language") or "en",
                user_id=user_id,
                created_by_username=getattr(current_user, "username", None) or "System",
                conversation_id=conversation_id,
                source_content=payload.get("source_content"),
                source_sql=payload.get("source_sql"),
                source_result_id=payload.get("source_result_id"),
                parent_artifact_id=payload.get("parent_artifact_id"),
            )
        if not artifact_id:
            return None
    except Exception as e:
        logger.warning("[ARTIFACT] agent document not persisted: %s", e)
        return None

    # Remember it, so the NEXT turn can say "translate the last report". This
    # writes through to the session file: the file, not this process, is what
    # answers after a restart.
    def _record_artifact_state():
        """Blocking session-file writes — runs in a THREAD, never on the loop.

        Every call here does read + fsync + os.replace under a threading.Lock
        that the agent's own stream thread also takes. On the event loop that
        makes the whole process wait on a worker thread's lock; see the note
        in finalize_turn for the live incident.
        """
        memory = agent_instance.conversation_memory
        memory.update_working_context(
            last_artifact_id=artifact_id, last_action="generate_document")
        # Dialogue state: a registered document is a VALIDATED tool result —
        # commit the reference through the delta door and snapshot the task,
        # so "go back to the previous report" has a real branch point to
        # restore. The application commits; the model only ever proposed.
        try:
            from sql_agent import dialogue_state as ds
            current = ds.migrate_state(
                (memory.get_working_context() or {}).get("dialogue_state"))
            turn_id = f"artifact-{artifact_id[:8]}"
            current = ds.apply_delta(current, {
                "operation": "REFERENCE", "field": "referenced_artifact",
                "proposed_value": artifact_id, "source": "tool_result",
            }, turn_id=turn_id)
            current = ds.snapshot_task(
                current, turn_id=turn_id,
                label=(payload.get("title") or "report")[:120])
            memory.update_working_context(dialogue_state=current)
        except Exception as state_error:
            logger.info("[DIALOGUE_STATE] artifact commit skipped: %s",
                        state_error)

    try:
        if agent_instance is not None and getattr(agent_instance,
                                                  "conversation_memory", None):
            await run_in_threadpool(_record_artifact_state)
    except Exception as e:
        logger.warning("[ARTIFACT] could not record last_artifact_id: %s", e)

    return {
        "type": "artifact",
        "artifact_id": artifact_id,
        "artifact_type": payload.get("type", "pdf"),
        "title": payload.get("title") or "Intelligence Report",
        "language": payload.get("language") or "en",
        "url": f"/api/sql-agent/artifacts/{artifact_id}",
    }


_EXPORT_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_EXPORT_FILE_EXTENSIONS = {"pdf": "pdf", "word": "docx"}


async def _respond_with_export(payload: bytes, artifact_type: str, safe_title: str,
                               safe_date: str, request: "ExportRequest",
                               current_user) -> Response:
    """Serve the rendered document, and remember it if we can.

    The Response is byte-for-byte what this endpoint has always returned —
    same bytes, same media type, same server-built filename. The only
    addition is an X-Artifact-Id header when the document was also persisted,
    which is what later lets the agent resolve "make the last report Arabic".

    Persistence is best-effort ON PURPOSE. The user asked for a document and
    the document exists; failing the request because a row could not be
    written would trade a working export for a bookkeeping error. When it
    fails the header is simply absent — an export that is not an artifact is
    honest, whereas a header naming a row that does not exist would not be.
    """
    headers = {
        "Content-Disposition": (
            f'attachment; filename="Intelligence_Report_{safe_date}'
            f'.{_EXPORT_FILE_EXTENSIONS[artifact_type]}"'),
    }
    try:
        async with db_manager.get_session() as db:
            artifact_id = await render_and_register(
                db, payload=payload, artifact_type=artifact_type, title=safe_title,
                language=detect_language(request.content),
                # NOT _uid(): that returns a string for logging (and the
                # literal "unknown" when absent). user_id is an integer FK.
                user_id=getattr(current_user, "id", None),
                created_by_username=getattr(current_user, "username", None) or "System",
                source_query=None,
                # The narrative the document was rendered FROM. It is lineage
                # for a later translation, never something we serialize back.
                source_content=request.content,
            )
        if artifact_id:
            headers["X-Artifact-Id"] = artifact_id
            # The browser cannot read a custom header on a download without
            # being told which ones are exposed.
            headers["Access-Control-Expose-Headers"] = "X-Artifact-Id"
    except Exception as e:
        logger.warning("[EXPORT] could not persist %s artifact: %s", artifact_type, e)

    return Response(content=payload,
                    media_type=_EXPORT_MEDIA_TYPES[artifact_type],
                    headers=headers)


# =====================================================
# QUERY HISTORY ENDPOINTS
# =====================================================

@router.get("/artifacts/{artifact_id}")
async def download_artifact(
    artifact_id: str,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None,
):
    """Download a document the agent generated, by id.

    Deliberately NOT served through /storage/{path}: that route authenticates
    but performs no ownership check, so any signed-in user could read another
    user's query output. Here the id resolves to a row that carries the owner,
    and the path comes from that row — never from the caller.

    Missing, soft-deleted and foreign artifacts all return the SAME 404 with
    the same body. A distinguishable response would let one user enumerate
    another user's report ids.
    """
    if not AUTH_AVAILABLE or not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    not_found = HTTPException(status_code=404, detail="Artifact not found")

    async with db_manager.get_session() as db:
        artifact = await artifact_registry.get_owned_artifact(
            db, artifact_id, getattr(current_user, "id", None))
        if artifact is None:
            raise not_found
        stored_type = artifact.type
        stored_path = artifact.storage_path
        stored_title = artifact.title

    try:
        full_path = artifact_registry._assert_inside_artifacts(
            os.path.join(artifact_registry.artifacts_root(), stored_path))
    except artifact_registry.ArtifactError:
        logger.error("[ARTIFACT] stored path escapes the artifact root: id=%s", artifact_id)
        raise not_found
    if not os.path.isfile(full_path):
        # The row outlived its file (manual deletion, restore gone wrong).
        logger.warning("[ARTIFACT] row without a file: id=%s", artifact_id)
        raise not_found

    media_type = {"pdf": "application/pdf",
                  "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  "report": "text/plain; charset=utf-8"}.get(stored_type, "application/octet-stream")
    extension = {"pdf": ".pdf", "word": ".docx", "report": ".txt"}.get(stored_type, "")
    # Filename is rebuilt from the stored title, stripped to a safe set — the
    # title reached us from a model/user and must not steer the header.
    safe_name = re.sub(r'[^A-Za-z0-9 ._-]', '', stored_title or "report").strip() or "report"
    return FileResponse(path=full_path, media_type=media_type,
                        filename=f"{safe_name[:80]}{extension}")


@router.get("/history")
async def get_query_history(
    response: Response,
    page: int = 1,
    page_size: int = 25,
    limit: Optional[int] = None,   # legacy compat: maps onto page_size
    offset: Optional[int] = None,  # legacy compat: maps onto page
    session_id: Optional[str] = None,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None
):
    """User's query history — server-side pagination, owner-scoped.

    Returns {history, page, page_size, count, has_more}. Timestamps are
    timezone-aware ISO-8601 (UTC, 'Z' suffix).
    """
    if not AUTH_AVAILABLE or not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"

    if limit is not None:
        page_size = limit
    page_size = max(1, min(int(page_size), 100))
    page = max(1, int(page))
    computed_offset = offset if offset is not None else (page - 1) * page_size

    try:
        async with db_manager.get_session() as db:
            # Fetch one extra row to compute has_more without a COUNT(*)
            history = await user_query_history_service.get_user_query_history(
                db=db,
                user_id=current_user.id,
                limit=page_size + 1,
                offset=computed_offset,
                session_id=session_id
            )
            has_more = len(history) > page_size
            history = history[:page_size]

            return {
                "success": True,
                "history": [
                    {
                        "id": q.id,
                        "query": q.query_text,
                        "response": q.response_text[:500] if q.response_text else None,
                        "timestamp": q.query_timestamp.isoformat() + "Z",
                        "success": q.success,
                        "processing_time_ms": q.processing_time_ms,
                        "session_id": q.session_id,
                        "metadata": q.query_metadata or {}
                    }
                    for q in history
                ],
                "page": page,
                "page_size": page_size,
                "count": len(history),
                "has_more": has_more,
            }
    except Exception as e:
        logger.error(f"[HISTORY] Error getting query history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load history")


@router.get("/history/{query_id}")
async def get_query_by_id(
    query_id: int,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None
):
    """Get a specific query by ID."""
    if not AUTH_AVAILABLE or not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    try:
        async with db_manager.get_session() as db:
            # The explicitly scoped accessor: ownership is applied by the
            # service, not asserted by this caller.
            query = await user_query_history_service.get_query_by_id_for_user(
                db=db,
                query_id=query_id,
                user_id=current_user.id
            )
            
            if not query:
                raise HTTPException(status_code=404, detail="Query not found")
            
            return {
                "success": True,
                "query": {
                    "id": query.id,
                    "query": query.query_text,
                    "response": query.response_text,
                    "timestamp": query.query_timestamp.isoformat(),
                    "response_timestamp": query.response_timestamp.isoformat() if query.response_timestamp else None,
                    "success": query.success,
                    "error_message": query.error_message,
                    "processing_time_ms": query.processing_time_ms,
                    "session_id": query.session_id,
                    "metadata": query.query_metadata or {}  # Fixed: use query_metadata instead of metadata_
                }
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[HISTORY] Error getting query: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/history/{query_id}")
async def delete_query_from_history(
    query_id: int,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None,
    _csrf: None = Depends(require_sql_agent_csrf),
):
    """Permanently delete one of the CALLER'S OWN history entries (the service
    filters by user_id — object-level authorization, no ID guessing). The
    chatbot audit log entry is preserved as the security record."""
    if not AUTH_AVAILABLE or not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        async with db_manager.get_session() as db:
            deleted = await user_query_history_service.delete_query(
                db=db,
                query_id=query_id,
                user_id=current_user.id
            )

            if not deleted:
                raise HTTPException(status_code=404, detail="Query not found")

            return {"success": True, "deleted_id": query_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[HISTORY] Error deleting query: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================
# SESSION MANAGEMENT ENDPOINTS
# =====================================================

@router.get("/sessions/list")
async def list_user_sessions(
    active_only: bool = False,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None
):
    """List user's conversation sessions."""
    if not AUTH_AVAILABLE or not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    try:
        async with db_manager.get_session() as db:
            sessions = await user_query_history_service.get_user_sessions(
                db=db,
                user_id=current_user.id,
                active_only=active_only
            )
            
            return {
                "success": True,
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "session_name": s.session_name,
                        "started_at": s.started_at.isoformat(),
                        "last_activity_at": s.last_activity_at.isoformat(),
                        "is_active": s.is_active,
                        "query_count": s.query_count,
                        "context_summary": s.context_summary
                    }
                    for s in sessions
                ]
            }
    except Exception as e:
        logger.error(f"[SESSIONS] Error listing sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/create")
async def create_session(
    request: dict,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None
):
    """Create a new conversation session."""
    if not AUTH_AVAILABLE or not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    try:
        session_name = request.get("session_name")
        session_id = request.get("session_id")
        
        async with db_manager.get_session() as db:
            session = await user_query_history_service.get_or_create_session(
                db=db,
                user_id=current_user.id,
                session_id=session_id,
                session_name=session_name
            )
            await db.commit()
            
            return {
                "success": True,
                "session": {
                    "session_id": session.session_id,
                    "session_name": session.session_name,
                    "started_at": session.started_at.isoformat(),
                    "last_activity_at": session.last_activity_at.isoformat()
                }
            }
    except Exception as e:
        logger.error(f"[SESSIONS] Error creating session: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/sessions/{session_id}")
async def update_session(
    session_id: str,
    request: dict,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None
):
    """Update a session (e.g., context summary, active status)."""
    if not AUTH_AVAILABLE or not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    try:
        context_summary = request.get("context_summary")
        is_active = request.get("is_active")
        
        async with db_manager.get_session() as db:
            session = await user_query_history_service.update_session(
                db=db,
                user_id=current_user.id,
                session_id=session_id,
                context_summary=context_summary,
                is_active=is_active
            )
            await db.commit()
            
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            
            return {
                "success": True,
                "session": {
                    "session_id": session.session_id,
                    "session_name": session.session_name,
                    "context_summary": session.context_summary,
                    "is_active": session.is_active,
                    "last_activity_at": session.last_activity_at.isoformat()
                }
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SESSIONS] Error updating session: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================
# MEMORY MANAGEMENT ENDPOINTS
# =====================================================

@router.get("/memory")
async def get_user_memories(
    memory_type: Optional[str] = None,
    min_importance: int = 0,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None
):
    """Get user's memories."""
    if not AUTH_AVAILABLE or not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    try:
        from db_models import MemoryType
        
        mem_type = None
        if memory_type:
            try:
                mem_type = MemoryType(memory_type)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid memory type: {memory_type}")
        
        async with db_manager.get_session() as db:
            memories = await user_query_history_service.get_user_memories(
                db=db,
                user_id=current_user.id,
                memory_type=mem_type,
                min_importance=min_importance
            )
            
            return {
                "success": True,
                "memories": [
                    {
                        "id": m.id,
                        "type": m.memory_type.value,
                        "key": m.memory_key,
                        "value": m.memory_value,
                        "importance": m.importance_score,
                        "created_at": m.created_at.isoformat(),
                        "last_accessed_at": m.last_accessed_at.isoformat() if m.last_accessed_at else None,
                        "access_count": m.access_count,
                        "expires_at": m.expires_at.isoformat() if m.expires_at else None
                    }
                    for m in memories
                ],
                "count": len(memories)
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MEMORY] Error getting memories: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/memory")
async def create_memory(
    request: dict,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None
):
    """Create a new memory."""
    if not AUTH_AVAILABLE or not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    try:
        from db_models import MemoryType
        
        memory_type = MemoryType(request.get("memory_type", "fact"))
        memory_key = request.get("memory_key")
        memory_value = request.get("memory_value", {})
        importance_score = request.get("importance_score", 50)
        expires_at = request.get("expires_at")
        
        if not memory_key:
            raise HTTPException(status_code=400, detail="memory_key is required")
        
        if expires_at:
            expires_at = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
        
        async with db_manager.get_session() as db:
            memory = await user_query_history_service.save_memory(
                db=db,
                user_id=current_user.id,
                memory_type=memory_type,
                memory_key=memory_key,
                memory_value=memory_value,
                importance_score=importance_score,
                expires_at=expires_at
            )
            await db.commit()
            
            return {
                "success": True,
                "memory": {
                    "id": memory.id,
                    "type": memory.memory_type.value,
                    "key": memory.memory_key,
                    "value": memory.memory_value,
                    "importance": memory.importance_score
                }
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MEMORY] Error creating memory: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/memory/{memory_id}")
async def delete_memory(
    memory_id: int,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None
):
    """Delete a memory."""
    if not AUTH_AVAILABLE or not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    try:
        async with db_manager.get_session() as db:
            deleted = await user_query_history_service.delete_memory(
                db=db,
                user_id=current_user.id,
                memory_id=memory_id
            )
            await db.commit()
            
            if not deleted:
                raise HTTPException(status_code=404, detail="Memory not found")
            
            return {"success": True, "message": "Memory deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MEMORY] Error deleting memory: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/context")
async def get_query_context(
    session_id: Optional[str] = None,
    current_user: User = Depends(require_chatbot_access()) if AUTH_AVAILABLE else None
):
    """Get context for AI agent (recent queries + memories)."""
    if not AUTH_AVAILABLE or not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    try:
        async with db_manager.get_session() as db:
            context = await user_query_history_service.get_context_for_query(
                db=db,
                user_id=current_user.id,
                session_id=session_id
            )
            
            return {
                "success": True,
                "context": context
            }
    except Exception as e:
        logger.error(f"[CONTEXT] Error getting context: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

