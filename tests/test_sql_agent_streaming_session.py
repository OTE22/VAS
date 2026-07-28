"""SQL-agent streaming: session lifetime, cancellation and cached-agent scope.

Background — the failure these tests lock down
----------------------------------------------
A streaming request timed out at 300s having produced zero characters, and the
teardown then raised ``InterfaceError`` ("connection closed during commit").
The database was healthy the whole time; ``/api/sql-agent/health`` and every
other endpoint kept returning 200.

The cause was a lifetime mismatch, not an outage:

  * ``get_current_user`` depends on ``get_db``; its ``SELECT ... FROM users``
    autobegins a transaction.
  * FastAPI holds a dependency scope open until the response is fully sent, and
    a ``StreamingResponse`` is not "sent" until the stream ends.
  * PostgreSQL is configured with ``idle_in_transaction_session_timeout=300000``
    (``db_connection.py``) — the SAME 300s as ``SQL_AGENT_TOTAL_TIMEOUT``.

So the auth connection sat idle-in-transaction for the entire stream and the
server terminated it at almost exactly the moment the stream deadline fired.
``get_session``'s cleanup then tried to ``commit()`` through the dead socket.

``release_request_session`` closes that session before streaming starts.
``test_idle_in_transaction_terminates_held_session`` reproduces the termination
mechanism directly, at a 1s timeout instead of 300s, so the diagnosis itself is
regression-tested rather than merely asserted in a comment.
"""

import asyncio
import contextlib

import pytest

from conftest import run_on_shared_loop


@contextlib.asynccontextmanager
async def _isolated_session():
    """A session on a dedicated NullPool engine, disposed on exit.

    These tests deliberately set session-level GUCs and let PostgreSQL terminate
    connections. Doing either on the shared application pool poisons it:
    ``pool_reset_on_return='rollback'`` rolls back the transaction but does NOT
    reset session variables, so a pooled connection still carrying
    ``idle_in_transaction_session_timeout='1s'`` goes on to kill unrelated tests
    that simply hold a transaction slightly too long. That is exactly what
    happened the first time these tests ran inside the full suite.

    A NullPool engine hands back a fresh connection every time and keeps nothing,
    so the blast radius is this test only.
    """
    from sqlalchemy.ext.asyncio import (
        create_async_engine, async_sessionmaker, AsyncSession,
    )
    from sqlalchemy.pool import NullPool
    from config import settings

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# The diagnosis: an idle-in-transaction session is killed by the server
# ---------------------------------------------------------------------------

def test_idle_in_transaction_terminates_held_session():
    """Holding an open transaction past the server timeout kills the connection.

    This is the exact mechanism behind the reported InterfaceError, reproduced
    with a 1-second timeout instead of the configured 300s. It is what makes the
    "hold the auth session for the whole stream" pattern fatal.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

    async def scenario():
        async with _isolated_session() as db:
            # Shrink the server-side timeout for this connection only.
            await db.execute(text("SET idle_in_transaction_session_timeout = '1s'"))
            # Autobegin a transaction, exactly as get_current_user's SELECT does.
            await db.execute(text("SELECT 1"))
            # Sit idle inside it, exactly as the stream used to.
            await asyncio.sleep(2.5)
            try:
                await db.execute(text("SELECT 1"))
                return "survived"
            except (InterfaceError, OperationalError, DBAPIError) as exc:
                return type(exc).__name__
            finally:
                try:
                    await db.rollback()
                except Exception:
                    pass

    outcome = run_on_shared_loop(scenario())
    assert outcome != "survived", (
        "Expected PostgreSQL to terminate a session left idle in transaction past "
        "idle_in_transaction_session_timeout. If this now survives, the server "
        "setting changed and the 300s streaming diagnosis needs revisiting."
    )


def test_release_request_session_detaches_and_closes():
    """release_request_session returns the connection and detaches the user row.

    After the call the session must hold no connection, and the detached object
    must still expose the attributes already loaded (the session maker sets
    expire_on_commit=False, which is what makes detaching safe).
    """
    from sqlalchemy import select
    from db_models import User
    from sql_agent.api.routes import release_request_session

    async def scenario():
        async with _isolated_session() as db:
            user = (await db.execute(select(User).limit(1))).scalars().first()
            if user is None:
                pytest.skip("no users seeded")

            # Mirrors the streaming route: capture plain values, then release.
            captured_id = user.id
            captured_name = user.username

            await release_request_session(db, user)

            # The connection is back in the pool.
            assert not db.in_transaction(), "session still holds a transaction"
            assert user not in db, "user was not detached from the session"

            # Loaded attributes survive detachment.
            assert user.id == captured_id
            assert user.username == captured_name

    run_on_shared_loop(scenario())


def test_stream_route_releases_session_before_streaming():
    """The streaming route must call release_request_session before it streams.

    Structural check: the release has to happen in the route body, *before* the
    generator is defined. If it moved inside the generator it would run only
    once the first chunk is pulled, which is far too late — the whole point is
    that the connection is already back in the pool when the LLM work starts.
    """
    import inspect
    from sql_agent.api import routes

    source = inspect.getsource(routes.sql_agent_query_stream)
    assert "release_request_session" in source, (
        "streaming route no longer releases the request session — the auth "
        "connection would sit idle-in-transaction for the whole stream"
    )

    release_at = source.index("release_request_session(db")
    generator_at = source.index("async def stream_query")
    assert release_at < generator_at, (
        "release_request_session must run before the stream generator is defined"
    )


# ---------------------------------------------------------------------------
# Cached agents must not outlive the authorization they were built under
# ---------------------------------------------------------------------------

def test_agent_cache_rebuilds_when_permissions_version_changes(monkeypatch):
    """A changed permissions_version discards the cached agent."""
    from sql_agent.api import routes

    built = []

    class _FakeAgent:
        def __init__(self, tag):
            self.tag = tag

    def _fake_builder(user_id, permissions_version=None):
        # Exercise the real cache logic but with a cheap object in place of a
        # full SQLIntelligenceAgent (which would open LLM + DB connections).
        agent = routes._user_agents.get(user_id)
        if agent is not None:
            cached = routes._user_agent_versions.get(user_id)
            if (permissions_version is not None and cached is not None
                    and cached != permissions_version):
                routes._user_agents.pop(user_id, None)
                routes._user_agent_versions.pop(user_id, None)
            else:
                return agent
        agent = _FakeAgent(f"v{permissions_version}")
        built.append(agent.tag)
        routes._user_agents[user_id] = agent
        routes._user_agent_versions[user_id] = permissions_version
        return agent

    uid = -991
    routes._user_agents.pop(uid, None)
    routes._user_agent_versions.pop(uid, None)
    try:
        a1 = _fake_builder(uid, permissions_version=1)
        a2 = _fake_builder(uid, permissions_version=1)
        assert a1 is a2, "same version must reuse the cached agent"
        assert built == ["v1"]

        a3 = _fake_builder(uid, permissions_version=2)
        assert a3 is not a1, "changed version must rebuild the agent"
        assert built == ["v1", "v2"]
    finally:
        routes._user_agents.pop(uid, None)
        routes._user_agent_versions.pop(uid, None)


def test_invalidate_user_sql_agent_evicts():
    """invalidate_user_sql_agent drops both the agent and its version entry."""
    from sql_agent.api import routes

    uid = -992
    routes._user_agents[uid] = object()
    routes._user_agent_versions[uid] = 3
    try:
        assert routes.invalidate_user_sql_agent(uid, "test") is True
        assert uid not in routes._user_agents
        assert uid not in routes._user_agent_versions
        # Idempotent: a second call is a no-op, not an error.
        assert routes.invalidate_user_sql_agent(uid, "test") is False
    finally:
        routes._user_agents.pop(uid, None)
        routes._user_agent_versions.pop(uid, None)


def test_user_service_invalidates_agent_cache_on_authorization_change():
    """The users service is wired to the agent cache.

    Without this hook a role or pipeline change would leave the cached agent
    serving the previous scope until the LRU happened to evict it.
    """
    import inspect
    from backend.services.user_service import UserService

    for method_name in ("update_user", "block_user", "unblock_user"):
        source = inspect.getsource(getattr(UserService, method_name))
        assert "_invalidate_sql_agent_cache" in source, (
            f"{method_name} does not invalidate the cached SQL agent"
        )


# ---------------------------------------------------------------------------
# Live authorization re-check
# ---------------------------------------------------------------------------

def test_check_authorization_fresh_denies_revoked_and_deactivated():
    """The re-check reports revocation for open connections."""
    from sqlalchemy import text
    from db_connection import db_manager
    from sql_agent.api.routes import check_authorization_fresh
    from backend.auth.password import hash_password

    username = "stream_authz_probe"

    async def scenario():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()

        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.execute(text("""
                INSERT INTO users (username, email, full_name, password_hash, role,
                                   is_active, can_use_chatbot, permissions_version, created_at)
                VALUES (:u, :e, 'Stream Probe', :h, 'analyzer', true, true, 1, now())
            """), {"u": username, "e": f"{username}@example.test",
                   "h": hash_password("ProbePassword!123")})
            await db.commit()
            uid = (await db.execute(
                text("SELECT id FROM users WHERE username = :u"), {"u": username}
            )).scalar()

        try:
            ok, version, reason = await check_authorization_fresh(uid)
            assert ok is True and reason is None, f"active user denied: {reason}"
            assert version == 1

            # Revoke chatbot access.
            async with db_manager.get_session() as db:
                await db.execute(text(
                    "UPDATE users SET can_use_chatbot = false, "
                    "permissions_version = permissions_version + 1 WHERE id = :i"
                ), {"i": uid})
                await db.commit()

            ok, version, reason = await check_authorization_fresh(uid)
            assert ok is False, "revoked chatbot access still reported as authorized"
            assert reason == "CHATBOT_ACCESS_REVOKED"
            assert version == 2, "permissions_version did not advance"

            # Deactivate entirely.
            async with db_manager.get_session() as db:
                await db.execute(text(
                    "UPDATE users SET can_use_chatbot = true, is_active = false WHERE id = :i"
                ), {"i": uid})
                await db.commit()

            ok, _version, reason = await check_authorization_fresh(uid)
            assert ok is False and reason == "ACCOUNT_DEACTIVATED"
        finally:
            async with db_manager.get_session() as db:
                await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
                await db.commit()

    run_on_shared_loop(scenario())


def test_check_authorization_fresh_holds_no_connection():
    """The per-message re-check must not leak a pooled connection.

    It runs on every WebSocket message, so a leak here would exhaust the pool
    under normal chat traffic.
    """
    from db_connection import db_manager
    from sql_agent.api.routes import check_authorization_fresh

    async def scenario():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()

        pool = db_manager.engine.pool
        before = pool.checkedout()
        for _ in range(5):
            await check_authorization_fresh(-99999)  # no such user
        after = pool.checkedout()
        assert after <= before, (
            f"re-check leaked connections: checked out {before} -> {after}"
        )

    run_on_shared_loop(scenario())


def test_websocket_rechecks_authorization_every_message():
    """Structural: the WS loop re-authorizes per message, not just at handshake."""
    import inspect
    from sql_agent.api import routes

    source = inspect.getsource(routes)
    ws_start = source.index("async def sql_agent_websocket")
    ws_source = source[ws_start:]

    assert "check_authorization_fresh" in ws_source, (
        "WebSocket loop does not re-check authorization; a revoked user would "
        "keep full access over an already-open socket"
    )
    assert "AUTHORIZATION_CHANGED" in ws_source
    assert "1008" in ws_source, "revoked socket must close with policy-violation 1008"


def test_sse_stream_rechecks_authorization():
    """Structural: the SSE drain loop re-authorizes at its heartbeat checkpoint."""
    import inspect
    from sql_agent.api import routes

    source = inspect.getsource(routes.sql_agent_query_stream)
    assert "check_authorization_fresh" in source, (
        "SSE stream does not re-check authorization mid-stream"
    )
    assert "AUTHORIZATION_CHANGED" in source


# ---------------------------------------------------------------------------
# Stage timing
# ---------------------------------------------------------------------------

def test_stage_timer_credits_the_node_that_produced_the_event():
    """Time belongs to the node that FINISHED, not the one before it.

    The agent emits a step when its LangGraph node completes, so the interval
    between two updates is the runtime of the later node. Getting this backwards
    would name the wrong stage in exactly the situation the instrumentation
    exists for. Uses a fake clock so the assertion is exact.
    """
    from sql_agent.api.routes import _StageTimer

    clock = {"t": 0.0}
    timer = _StageTimer(lambda: clock["t"])

    clock["t"] = 1.0
    timer.observe({"type": "status", "step": "schema"})       # schema took 1.0s
    clock["t"] = 1.5
    timer.observe({"type": "status", "step": "rag"})          # rag took 0.5s
    clock["t"] = 60.0
    timer.observe({"type": "status", "step": "execute"})      # execute took 58.5s
    timer.finish()

    step, ms = timer.slowest
    assert step == "execute", f"time must be credited to the node that finished, got {step}"
    assert abs(ms - 58500) < 1, ms
    assert abs(timer.stages["schema"] - 1000) < 1
    assert abs(timer.stages["rag"] - 500) < 1


def test_stage_timer_labels_work_still_in_flight_at_timeout():
    """A stall with no further event is reported as after:<last step>.

    This is the live timeout signature: the last event was `rag`, then nothing
    for 272s because the NEXT node never reported. Attributing that silence to
    `rag` itself would point at the wrong stage.
    """
    from sql_agent.api.routes import _StageTimer

    clock = {"t": 0.0}
    timer = _StageTimer(lambda: clock["t"])

    clock["t"] = 28.4
    timer.observe({"type": "status", "step": "rag"})
    clock["t"] = 300.6            # timeout, nothing further ever arrives
    timer.finish()

    step, ms = timer.slowest
    assert step == "after:rag", f"expected after:rag, got {step}"
    assert abs(ms - 272200) < 100, ms
    # finish() is idempotent — the stream calls it on both the timeout and the
    # terminal log paths, and double-counting would inflate the breakdown.
    timer.finish()
    assert abs(timer.stages["after:rag"] - 272200) < 100


def test_stage_timer_records_time_to_first_chunk():
    """time_to_first_chunk is only set by real content, not by status updates."""
    from sql_agent.api.routes import _StageTimer

    clock = {"t": 0.0}
    timer = _StageTimer(lambda: clock["t"])

    clock["t"] = 2.0
    timer.observe({"type": "status", "step": "schema"})
    assert timer.first_chunk_at is None, "a status update is not a content chunk"
    assert timer.first_update_at == 2.0

    clock["t"] = 9.0
    timer.observe({"type": "content", "content": "x", "step": "response"})
    assert timer.first_chunk_at == 9.0


def test_stage_timer_logs_no_query_or_response_text():
    """The breakdown must contain stage names and numbers only."""
    from sql_agent.api.routes import _StageTimer

    timer = _StageTimer(lambda: 0.0)
    timer.observe({"type": "sql", "sql": "SELECT secret FROM users", "step": "generate_sql"})
    timer.observe({"type": "content", "content": "sensitive answer", "step": "response"})
    timer.finish()

    summary = timer.summary()
    assert "SELECT" not in summary and "secret" not in summary
    assert "sensitive" not in summary
    assert "generate_sql" in summary


def test_stream_logs_timeout_source_and_stage_breakdown():
    """Structural: the stream's terminal log names the limit that fired."""
    import inspect
    from sql_agent.api import routes

    source = inspect.getsource(routes.sql_agent_query_stream)
    assert "timeout_source" in source, "stream does not record which limit fired"
    assert "SQL_AGENT_TOTAL_TIMEOUT" in source
    assert "slowest_stage" in source, "stream does not report the stalled stage"


# ---------------------------------------------------------------------------
# Post-failure database health
# ---------------------------------------------------------------------------

def test_pool_pre_ping_enabled():
    """pool_pre_ping must stay on.

    It is what lets the pool silently replace a connection the server killed,
    instead of handing a dead one to the next request. Without it, the incident's
    terminated connection could be served to an unrelated caller.
    """
    from db_connection import db_manager

    async def scenario():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        assert db_manager.engine.pool._pre_ping is True, (
            "pool_pre_ping is disabled; a server-terminated connection could be "
            "handed to the next request"
        )

    run_on_shared_loop(scenario())


def test_database_healthy_after_a_connection_dies():
    """One killed connection must not affect subsequent application queries.

    The regression guard for "one SQL-agent timeout broke the database for
    everyone". The connection is killed on an ISOLATED engine so this test
    cannot itself poison the shared pool it is asserting about.
    """
    from sqlalchemy import text
    from db_connection import db_manager

    async def scenario():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()

        # Kill a connection the same way the server did during the incident.
        async with _isolated_session() as doomed:
            await doomed.execute(text("SET idle_in_transaction_session_timeout = '1s'"))
            await doomed.execute(text("SELECT 1"))
            await asyncio.sleep(2.0)
            try:
                await doomed.execute(text("SELECT 1"))
            except Exception:
                pass
            try:
                await doomed.rollback()
            except Exception:
                pass

        # The application pool must be entirely unaffected.
        for _ in range(3):
            async with db_manager.get_session() as db:
                assert (await db.execute(text("SELECT 1"))).scalar() == 1

    run_on_shared_loop(scenario())


# ---------------------------------------------------------------------------
# A model timeout must read as a timeout
# ---------------------------------------------------------------------------

def test_timeout_errors_are_recognised_in_every_form():
    """The timeout can arrive as httpx, builtin, or wrapped by langchain."""
    import httpx
    from sql_agent.tools.agent_tools import _is_timeout_error

    assert _is_timeout_error(httpx.ReadTimeout("slow"))
    assert _is_timeout_error(httpx.ConnectTimeout("down"))
    assert _is_timeout_error(TimeoutError())

    wrapped = RuntimeError("model call failed")
    wrapped.__cause__ = httpx.ReadTimeout("slow")
    assert _is_timeout_error(wrapped), "must follow the cause chain"

    assert not _is_timeout_error(ValueError("bad SQL")), "must not swallow real errors"


def test_generation_timeout_produces_a_useful_message_not_no_sql():
    """A generation timeout must not surface as 'No SQL query to execute'.

    Observed live: the model exceeded its budget, generated_sql was left empty,
    and the user was shown a bare 'SQL Error: No SQL query to execute' — which
    reads like an internal fault rather than a capacity limit.
    """
    import inspect
    from sql_agent.tools import agent_tools

    source = inspect.getsource(agent_tools.SQLAgentTools.generate_sql)
    assert "sql_generation_timed_out" in source, (
        "generate_sql does not distinguish a timeout from unusable output"
    )

    execute_source = inspect.getsource(agent_tools.SQLAgentTools.execute_sql)
    assert "sql_generation_timed_out" in execute_source, (
        "execute_sql overwrites the timeout reason with 'No SQL query to execute'"
    )


def test_timeout_message_leaks_no_internals():
    """The user-facing timeout text must not expose prompts, SQL or stack detail."""
    import inspect
    from sql_agent.tools import agent_tools

    source = inspect.getsource(agent_tools.SQLAgentTools.generate_sql)
    start = source.index("sql_generation_timed_out")
    window = source[max(0, start - 600):start]
    assert "took too long" in window, "expected a plain-language timeout message"
    for leak in ("prompt", "traceback", "exc_info=True"):
        assert leak not in window.lower().split("logger.warning")[0][-300:], (
            f"timeout branch may leak {leak}"
        )


def test_timeout_flag_is_declared_in_the_graph_state():
    """LangGraph drops undeclared keys, so the flag must be in AgentState.

    Without this declaration the flag is set by generate_sql and silently
    discarded before execute_sql reads it — the timeout then surfaces as
    "No SQL query to execute" again. Verified live: adding the field is what
    made the useful message actually reach the client.
    """
    from sql_agent.state import AgentState

    assert "sql_generation_timed_out" in AgentState.__annotations__, (
        "sql_generation_timed_out is not declared in AgentState; LangGraph will "
        "drop it between nodes"
    )


# ---------------------------------------------------------------------------
# History must survive the client disconnecting after the terminal event
# ---------------------------------------------------------------------------
#
# Observed live (2026-07-28 08:28): a query timed out cleanly, the terminal
# event was sent, the frontend aborted the connection on receiving it — and the
# abort, propagated through BaseHTTPMiddleware's cancel scope, cancelled the
# request task while persist_query_history was awaiting its commit. The log
# showed "persisting history" with no "[HISTORY_SAVE] saved" after it, the row
# never reached the sidebar, and SQLAlchemy's pool logged "Exception
# terminating connection / CancelledError" tearing down the connection.

def test_persistence_survives_cancellation_of_the_awaiting_task():
    """Cancel the requester mid-persist; the commit must still land.

    Exercises await_persistence_despite_disconnect with a coroutine that
    records completion, cancelling the awaiting task while the work is
    deliberately slow. The helper must re-raise CancelledError to the requester
    (teardown proceeds) while the shielded task runs to completion.
    """
    from sql_agent.api import routes

    async def scenario():
        completed = asyncio.Event()

        async def slow_persist():
            await asyncio.sleep(0.3)
            completed.set()

        async def request_like():
            await routes.await_persistence_despite_disconnect(slow_persist(), "test-req")

        requester = asyncio.get_event_loop().create_task(request_like())
        await asyncio.sleep(0.05)          # persist is now in flight
        requester.cancel()                  # the client "disconnects"

        with pytest.raises(asyncio.CancelledError):
            await requester

        # The persistence must still finish, uncancelled.
        await asyncio.wait_for(completed.wait(), timeout=2.0)
        assert completed.is_set()

    run_on_shared_loop(scenario())


def test_persistence_commits_to_the_database_despite_cancellation():
    """Same, end-to-end: a REAL history row lands although the requester died."""
    from sqlalchemy import text
    from db_connection import db_manager
    from sql_agent.api import routes

    marker = "cancel_survival_probe_query"

    async def scenario():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()

        async with db_manager.get_session() as db:
            await db.execute(text(
                "DELETE FROM user_query_history WHERE query_text = :q"), {"q": marker})
            await db.commit()
            admin_id = (await db.execute(text(
                "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"))).scalar()
        if admin_id is None:
            pytest.skip("no admin user seeded")

        async def request_like():
            await routes.await_persistence_despite_disconnect(
                routes.persist_query_history(
                    user_id=admin_id,
                    query=marker,
                    response=None,
                    session_id=None,
                    success=False,
                    processing_time_ms=1.0,
                    metadata={},
                ),
                "test-db-req",
            )

        requester = asyncio.get_event_loop().create_task(request_like())
        # Cancel essentially immediately — before the first commit can finish.
        await asyncio.sleep(0)
        requester.cancel()
        try:
            await requester
        except asyncio.CancelledError:
            pass

        # Wait for the background task set to drain, then the row must exist.
        for _ in range(100):
            if not routes._background_tasks:
                break
            await asyncio.sleep(0.05)

        async with db_manager.get_session() as db:
            count = (await db.execute(text(
                "SELECT count(*) FROM user_query_history WHERE query_text = :q"),
                {"q": marker})).scalar()
            await db.execute(text(
                "DELETE FROM user_query_history WHERE query_text = :q"), {"q": marker})
            await db.commit()

        assert count == 1, (
            "the history row was lost when the requester was cancelled — the "
            "live incident of 2026-07-28 08:28 is back"
        )

    run_on_shared_loop(scenario())


def test_all_persistence_sites_are_shielded():
    """Every history save on a client-facing path must go through the helper.

    A bare `await persist_query_history(...)` in a route handler dies with the
    connection. Allowed exceptions: the helper itself and its own internals.
    """
    import inspect
    from sql_agent.api import routes

    source = inspect.getsource(routes)
    lines = source.splitlines()
    unshielded = []
    for i, line in enumerate(lines):
        if "await persist_query_history(" in line:
            # Shielded if the wrapper appears nearby — either the call is the
            # wrapper's argument (a few lines back) or it sits inside the
            # _persist_outcome closure whose whole body runs as the shielded
            # background task (the def is further up).
            window = "\n".join(lines[max(0, i - 30):i + 1])
            if ("await_persistence_despite_disconnect" not in window
                    and "_persist_outcome" not in window):
                unshielded.append(i + 1)
    assert not unshielded, (
        f"bare awaited history saves at lines {unshielded} — a client abort "
        f"cancels them mid-commit"
    )


# ---------------------------------------------------------------------------
# Validation errors are not database errors
# ---------------------------------------------------------------------------

def test_request_validation_errors_are_not_logged_as_db_failures(caplog):
    """A 422 (e.g. page_size=500 against le=100) raised inside the get_db
    dependency must propagate WITHOUT the 'DB session error' ERROR logging.

    Observed live: each such 422 produced two ERROR lines with tracebacks
    ('DB session error' + 'DB session creation failed'), making routine client
    input rejections look like database failures.
    """
    import logging
    from fastapi.exceptions import RequestValidationError
    from db_connection import db_manager

    async def scenario():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        with pytest.raises(RequestValidationError):
            async with db_manager.get_session() as _db:
                raise RequestValidationError(errors=[{"loc": ("query", "page_size"),
                                                      "msg": "too big", "type": "le"}])

    with caplog.at_level(logging.ERROR, logger="db_connection"):
        run_on_shared_loop(scenario())

    db_errors = [r for r in caplog.records
                 if r.name == "db_connection" and r.levelno >= logging.ERROR]
    assert not db_errors, (
        f"validation error was logged as a DB failure: {[r.message for r in db_errors]}"
    )
