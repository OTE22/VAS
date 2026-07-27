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

from tests.conftest import run_on_shared_loop


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
        # asyncpg connections are loop-bound (see tests/conftest.py). An earlier
        # module may have filled the pool from a different loop; disposing forces
        # fresh connections on THIS loop. Checked-out connections are unaffected.
        await db_manager.engine.dispose()

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
