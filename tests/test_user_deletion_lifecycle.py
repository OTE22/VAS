"""Permanent user deletion: account state dies, history survives.

    docker exec face_recognition_api python -m pytest tests/test_user_deletion_lifecycle.py -v

Deleting a user used to be impossible twice over. The ORM de-associated
CASCADE-owned children before PostgreSQL could act — `UPDATE workspace_members
SET user_id = NULL` against NOT NULL — and 14 FKs had no delete rule at all, so
each surviving row raised in turn ("violates foreign key constraint
chatbot_audit_log_user_id_fkey", observed live). The one path that DIDN'T error
was worse: conversations, query history and embeddings were ON DELETE CASCADE,
so a "successful" deletion would have destroyed the user's chat history.

The properties pinned here, each cheap to break silently:

  * the ORM never nullifies CASCADE-owned children — asserted with a statement
    listener, run both with collections cold AND pre-loaded, because
    passive_deletes="all" is what protects the loaded case and a downgrade to
    True would pass the cold case only;
  * history survives with user_id NULL and full attribution (author_username,
    historical_* ids, the deleted_users tombstone);
  * account-bound state is really gone; workspace and other members survive;
  * a replayed pre-deletion JWT is inert; the SQL-agent cache entry is evicted
    after commit;
  * the last workspace admin cannot be deleted without a valid successor,
    promoted in the same transaction;
  * a recreated username is a NEW account and inherits nothing.

HTTP against the live app plus raw SQL, like test_webhook_credentials.py.
Requires migration b0c1d2e3f4a5 to be applied.
"""

import json
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"

PROBE = "qa_del_probe"
PROBE_PASSWORD = "QaDelete!2026"


def _http(method, path, body=None, *, token=None, csrf=True, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if csrf:
        request.add_header("X-Requested-With", "XMLHttpRequest")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _json(raw):
    try:
        return json.loads(raw.decode())
    except Exception:                                          # noqa: BLE001
        return {}


def _run(coro):
    from conftest import run_on_shared_loop
    return run_on_shared_loop(coro)


async def _session():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()
    return db_manager.get_session()


@pytest.fixture(scope="module")
def admin_token():
    # csrf=False: the login handler treats X-Requested-With as "browser" and
    # moves the JWT into an httpOnly cookie, nulling access_token in the body.
    status, raw = _http("POST", "/api/auth/login",
                        {"username": "admin", "password": "admin123"},
                        csrf=False)
    assert status == 200, f"admin login failed: {raw[:200]}"
    token = _json(raw).get("access_token")
    assert token, "login returned no bearer token"
    return token


# ---------------------------------------------------------------------------
# probe user construction / teardown
# ---------------------------------------------------------------------------

async def _purge(username):
    """Remove a probe user and its residue, idempotently.

    Includes the PRESERVED rows this suite creates: deletion is designed to
    keep conversations and history, which for a test fixture means every run
    would otherwise leak an orphaned conversation titled 'qa deletion probe' —
    and the admin-read test then finds last week's orphans instead of its own
    (that exact failure happened). Test residue is not history; it goes.
    """
    from sqlalchemy import text as sa_text
    async with await _session() as db:
        uid = (await db.execute(sa_text(
            "SELECT id FROM users WHERE username = :u"), {"u": username})).scalar()
        if uid is not None:
            # Post-migration the DB owns the cleanup; a plain DELETE is enough.
            await db.execute(sa_text("DELETE FROM users WHERE id = :i"), {"i": uid})
        # Orphaned residue from THIS suite only, matched by its fixture markers
        # — never a blanket orphan sweep. search_history resolves through the
        # tombstone, so it must be purged BEFORE the tombstone row goes.
        await db.execute(sa_text("""
            DELETE FROM search_history WHERE user_id IS NULL
              AND historical_user_id IN (SELECT user_id FROM deleted_users WHERE username = :u)
        """), {"u": username})
        await db.execute(sa_text("""
            DELETE FROM conversations
            WHERE title = 'qa deletion probe' AND user_id IS NULL
              AND (author_username = :u OR author_username IS NULL)
        """), {"u": username})
        await db.execute(sa_text("""
            DELETE FROM user_query_history
            WHERE query_text = 'qa deletion probe query' AND user_id IS NULL
        """))
        await db.execute(sa_text("""
            DELETE FROM chatbot_audit_log
            WHERE username = :u AND user_id IS NULL AND query = 'qa probe audit'
        """), {"u": username})
        await db.execute(sa_text("""
            DELETE FROM identity_audit_log
            WHERE username = :u AND user_id IS NULL AND action_type = 'qa_probe_action'
        """), {"u": username})
        await db.execute(sa_text(
            "DELETE FROM deleted_users WHERE username = :u"), {"u": username})
        await db.commit()


async def _mint(username, *, role="user", active=True):
    """Create a probe user; returns its id."""
    from sqlalchemy import text as sa_text
    from backend.auth.password import hash_password
    async with await _session() as db:
        await db.execute(sa_text("DELETE FROM users WHERE username = :u"),
                         {"u": username})
        uid = (await db.execute(sa_text("""
            INSERT INTO users (username, email, password_hash, role, is_active,
                               can_use_chatbot, permissions_version, created_at)
            VALUES (:u, :e, :h, :r, :a, true, 1, now()) RETURNING id
        """), {"u": username, "e": f"{username}@example.test",
               "h": hash_password(PROBE_PASSWORD), "r": role, "a": active})).scalar()
        await db.commit()
        return uid


async def _scalar(sql, **params):
    from sqlalchemy import text as sa_text
    async with await _session() as db:
        return (await db.execute(sa_text(sql), params)).scalar()


@pytest.fixture
def probe():
    """A disposable user with a full spread of dependent rows."""
    from sqlalchemy import text as sa_text

    async def build():
        # Delete-before-insert, including preserved orphans a prior run left.
        await _purge(PROBE)
        uid = await _mint(PROBE)
        async with await _session() as db:
            ws = (await db.execute(sa_text(
                "SELECT id FROM workspaces ORDER BY is_default DESC LIMIT 1"))).scalar()
            assert ws is not None, "no workspace; migration d3e4f5a6b7c8 missing"
            await db.execute(sa_text("""
                INSERT INTO workspace_members (id, workspace_id, user_id, role, created_at)
                VALUES (gen_random_uuid(), :w, :u, 'member', now())
                ON CONFLICT DO NOTHING
            """), {"w": ws, "u": uid})
            conv = (await db.execute(sa_text("""
                INSERT INTO conversations (id, workspace_id, user_id, title, pinned,
                                           archived, created_at, updated_at)
                VALUES (gen_random_uuid(), :w, :u, 'qa deletion probe', false, false,
                        now(), now()) RETURNING id
            """), {"w": ws, "u": uid})).scalar()
            branch = (await db.execute(sa_text("""
                INSERT INTO conversation_branches (id, conversation_id, is_primary, created_at)
                VALUES (gen_random_uuid(), :c, true, now()) RETURNING id
            """), {"c": conv})).scalar()
            await db.execute(sa_text("""
                INSERT INTO messages (id, branch_id, role, sequence, content_blocks,
                                      status, created_at)
                VALUES (gen_random_uuid(), :b, 'user', 1,
                        '[{"type":"text","text":"where was X?"}]'::jsonb,
                        'complete', now())
            """), {"b": branch})
            qh = (await db.execute(sa_text("""
                INSERT INTO user_query_history (user_id, query_text, query_timestamp, success)
                VALUES (:u, 'qa deletion probe query', now(), true) RETURNING id
            """), {"u": uid})).scalar()
            await db.execute(sa_text("""
                INSERT INTO user_conversation_sessions (user_id, session_id, started_at,
                                                        last_activity_at, is_active, query_count)
                VALUES (:u, :s, now(), now(), true, 1)
            """), {"u": uid, "s": f"qa-del-{uid}"})
            await db.execute(sa_text("""
                INSERT INTO chatbot_audit_log (user_id, username, query, success, created_at)
                VALUES (:u, :n, 'qa probe audit', true, now())
            """), {"u": uid, "n": PROBE})
            await db.execute(sa_text("""
                INSERT INTO identity_audit_log (user_id, username, action_type, success, created_at)
                VALUES (:u, :n, 'qa_probe_action', true, now())
            """), {"u": uid, "n": PROBE})
            await db.execute(sa_text("""
                INSERT INTO search_history (id, user_id, search_type,
                                            watchlist_alerts_count, created_at)
                VALUES (gen_random_uuid(), :u, 'SINGLE', 0, now())
            """), {"u": uid})
            await db.commit()
            return {"id": uid, "workspace_id": str(ws), "conversation_id": str(conv),
                    "query_history_id": qh}

    info = _run(build())
    try:
        yield info
    finally:
        _run(_purge(PROBE))


def _delete_via_api(admin_token, user_id, query=""):
    return _http("DELETE", f"/api/users/{user_id}{query}", token=admin_token)


# ---------------------------------------------------------------------------
# the core lifecycle
# ---------------------------------------------------------------------------

def test_deletion_succeeds_and_preserves_history(admin_token, probe):
    uid = probe["id"]
    status, raw = _delete_via_api(admin_token, uid)
    assert status == 200, f"deletion failed: {raw[:400]}"

    # account + account-bound state: gone
    assert _run(_scalar("SELECT count(*) FROM users WHERE id=:u", u=uid)) == 0
    for table in ("workspace_members", "user_conversation_sessions",
                  "user_conversation_memory", "user_pipeline_access",
                  "message_feedback", "pending_enrollments"):
        assert _run(_scalar(
            f"SELECT count(*) FROM {table} WHERE user_id=:u", u=uid)) == 0, (
            f"{table} rows survived deletion")

    # history: preserved, detached, attributed
    conv = _run(_scalar("""
        SELECT count(*) FROM conversations
        WHERE historical_user_id=:u AND user_id IS NULL
          AND author_username=:n""", u=uid, n=PROBE))
    assert conv == 1, "conversation lost or missing attribution"
    msgs = _run(_scalar("""
        SELECT count(*) FROM messages m
        JOIN conversation_branches b ON m.branch_id=b.id
        WHERE b.conversation_id = CAST(:c AS uuid)""", c=probe["conversation_id"]))
    assert msgs == 1, "messages did not survive"
    for table in ("user_query_history", "search_history",
                  "chatbot_audit_log", "identity_audit_log"):
        kept = _run(_scalar(
            f"SELECT count(*) FROM {table} "
            f"WHERE historical_user_id=:u AND user_id IS NULL", u=uid))
        assert kept == 1, f"{table}: history lost or historical id not stamped"

    # tombstone
    row = _run(_scalar(
        "SELECT username FROM deleted_users WHERE user_id=:u", u=uid))
    assert row == PROBE, "tombstone missing or wrong"

    # workspace itself survives
    assert _run(_scalar(
        "SELECT count(*) FROM workspaces WHERE id = CAST(:w AS uuid)",
        w=probe["workspace_id"])) == 1


def test_the_orm_never_nullifies_cascade_owned_children(admin_token, probe):
    """The original bug, asserted at the statement level, in BOTH load states.

    passive_deletes="all" keeps SQLAlchemy's hands off the children whether or
    not they are in the session. A regression to True would pass the cold run
    and fail the pre-loaded one — which is exactly the case that produced
    `UPDATE workspace_members SET user_id = NULL` in production. The preserve
    tables are allowed to end up NULL — but by the DATABASE's ON DELETE SET
    NULL, never by an ORM UPDATE.
    """
    import re

    from sqlalchemy import event
    from db_connection import db_manager

    cascade_tables = ("workspace_members", "user_conversation_sessions",
                      "user_conversation_memory", "user_pipeline_access",
                      "message_feedback", "pending_enrollments")
    forbidden = re.compile(
        r"UPDATE\s+(" + "|".join(cascade_tables) + r")\s+SET\s+\w*user_id\w*\s*=",
        re.I)
    offending = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if forbidden.search(statement):
            offending.append(statement[:200])

    async def run_deletion(preload: bool):
        from sqlalchemy import select
        from backend.services.user_service import UserService
        from db_models import User

        sync_engine = db_manager.engine.sync_engine
        event.listen(sync_engine, "before_cursor_execute", listener)
        try:
            async with db_manager.get_session() as db:
                if preload:
                    user = (await db.execute(
                        select(User).where(User.id == probe["id"]))).scalar_one()
                    # Force the collections into the session — the load state
                    # that reproduced the original failure.
                    await db.refresh(user, ["workspace_memberships",
                                            "conversation_sessions",
                                            "pipeline_access"])
                return await UserService.delete_user(probe["id"], db)
        finally:
            event.remove(sync_engine, "before_cursor_execute", listener)

    # Round 1: collections pre-loaded (the historical failure mode).
    assert _run(run_deletion(preload=True)) is True
    assert not offending, (
        f"ORM nullified CASCADE-owned children (pre-loaded): {offending}")

    # Round 2: cold, on a fresh probe.
    _run(_purge(PROBE))
    uid2 = _run(_mint(PROBE))
    offending.clear()
    probe["id"] = uid2
    assert _run(run_deletion(preload=False)) is True
    assert not offending, (
        f"ORM nullified CASCADE-owned children (cold): {offending}")


def test_replayed_jwt_is_rejected_after_deletion(admin_token, probe):
    status, raw = _http("POST", "/api/auth/login",
                        {"username": PROBE, "password": PROBE_PASSWORD},
                        csrf=False)
    assert status == 200, f"probe login failed: {raw[:200]}"
    token = _json(raw)["access_token"]

    status, _ = _http("GET", "/api/auth/me", token=token)
    assert status == 200, "token should work before deletion"

    status, raw = _delete_via_api(admin_token, probe["id"])
    assert status == 200, raw[:300]

    status, _ = _http("GET", "/api/auth/me", token=token)
    assert status in (401, 403), (
        f"a deleted user's JWT still authenticates (got {status})")

    # Chat surfaces with the same replayed token: REST and SSE entrypoints.
    status, _ = _http("POST", "/api/sql-agent/query",
                      {"query": "how many detections today"}, token=token)
    assert status in (401, 403), f"SQL-agent REST accepted a deleted user ({status})"
    # SSE is POST on this API (same handler family as REST).
    status, _ = _http("POST", "/api/sql-agent/query/stream",
                      {"query": "how many detections today"}, token=token)
    assert status in (401, 403), f"SQL-agent SSE accepted a deleted user ({status})"

    # New login must also fail (row is gone).
    status, _ = _http("POST", "/api/auth/login",
                      {"username": PROBE, "password": PROBE_PASSWORD}, csrf=False)
    assert status != 200, "a deleted user logged in again"


def test_websocket_authorization_check_reports_account_gone(admin_token, probe):
    """The long-lived-socket recheck is what closes an open WS mid-session."""
    uid = probe["id"]
    status, raw = _delete_via_api(admin_token, uid)
    assert status == 200, raw[:300]

    async def check():
        from sql_agent.api.routes import check_authorization_fresh
        return await check_authorization_fresh(uid)

    ok, _version, reason = _run(check())
    assert ok is False and reason == "ACCOUNT_NOT_FOUND", (
        f"open-socket recheck did not flag the deleted account: {ok}, {reason}")


def test_sql_agent_cache_is_evicted_after_commit(probe):
    """In-process deliberately: the runtime cache is a module-level dict, so an
    HTTP delete would evict the SERVER process's dict while this test can only
    observe its own. Calling UserService.delete_user here exercises the same
    code path the route uses, in a process whose cache we can inspect."""
    from sql_agent.api import routes as r

    uid = probe["id"]
    r._user_agents[uid] = object()
    r._user_agent_versions[uid] = 1
    try:
        async def run():
            from backend.services.user_service import UserService
            async with await _session() as db:
                return await UserService.delete_user(uid, db)

        assert _run(run()) is True
        assert uid not in r._user_agents, "agent cache entry survived deletion"
        assert uid not in r._user_agent_versions, "agent version entry survived deletion"
    finally:
        r._user_agents.pop(uid, None)
        r._user_agent_versions.pop(uid, None)


def test_failed_deletion_does_not_evict_the_cache(probe):
    """Eviction must happen AFTER commit: a rolled-back deletion must not
    evict a live user's agent (comment ordering in block_user is the same
    contract)."""
    from sqlalchemy import text as sa_text
    from sql_agent.api import routes as r

    uid = probe["id"]
    r._user_agents[uid] = object()
    r._user_agent_versions[uid] = 1
    try:
        async def run():
            from backend.services.user_service import UserService
            async with await _session() as db:
                await db.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS qa_delete_blocker2 (
                        id serial PRIMARY KEY,
                        user_id integer NOT NULL REFERENCES users(id) ON DELETE RESTRICT)
                """))
                await db.execute(sa_text(
                    "INSERT INTO qa_delete_blocker2 (user_id) VALUES (:u)"), {"u": uid})
                await db.commit()
            try:
                async with await _session() as db:
                    try:
                        await UserService.delete_user(uid, db)
                        return None
                    except ValueError as e:
                        return str(e)
            finally:
                async with await _session() as db:
                    await db.execute(sa_text("DROP TABLE IF EXISTS qa_delete_blocker2"))
                    await db.commit()

        error = _run(run())
        assert error is not None, "deletion should have failed"
        assert uid in r._user_agents, (
            "a FAILED deletion evicted the live user's cached agent")
    finally:
        r._user_agents.pop(uid, None)
        r._user_agent_versions.pop(uid, None)


def test_failed_deletion_rolls_back_everything(admin_token, probe):
    """Force the final DELETE to fail and prove nothing else stuck."""
    from sqlalchemy import text as sa_text

    uid = probe["id"]

    async def attempt_with_block():
        # An advisory-locked trigger is overkill; simplest reliable failure:
        # a temp table with a RESTRICT FK to users blocks the DELETE.
        async with await _session() as db:
            await db.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS qa_delete_blocker (
                    id serial PRIMARY KEY,
                    user_id integer NOT NULL REFERENCES users(id) ON DELETE RESTRICT)
            """))
            await db.execute(sa_text(
                "INSERT INTO qa_delete_blocker (user_id) VALUES (:u)"), {"u": uid})
            await db.commit()
        try:
            status, raw = _delete_via_api(admin_token, uid)
            return status, raw
        finally:
            async with await _session() as db:
                await db.execute(sa_text("DROP TABLE IF EXISTS qa_delete_blocker"))
                await db.commit()

    status, raw = _run(attempt_with_block())
    assert status == 500, f"expected failure, got {status}: {raw[:200]}"

    # Everything must still be present: user, membership, tombstone absent.
    assert _run(_scalar("SELECT count(*) FROM users WHERE id=:u", u=uid)) == 1, (
        "user gone despite failed deletion")
    assert _run(_scalar(
        "SELECT count(*) FROM workspace_members WHERE user_id=:u", u=uid)) == 1, (
        "membership gone despite failed deletion")
    assert _run(_scalar(
        "SELECT count(*) FROM deleted_users WHERE user_id=:u", u=uid)) == 0, (
        "tombstone committed despite failed deletion")
    # attribution stamps must have rolled back too (author_username was set
    # inside the transaction)
    assert _run(_scalar("""
        SELECT count(*) FROM conversations
        WHERE user_id=:u AND author_username IS NOT NULL""", u=uid)) == 0, (
        "historical attribution committed despite failed deletion")


# ---------------------------------------------------------------------------
# workspace-admin succession
# ---------------------------------------------------------------------------

SOLO = "qa_del_solo_admin"
HEIR = "qa_del_heir"


@pytest.fixture
def solo_admin_workspace(admin_token):
    """A workspace whose ONLY admin is the probe, plus a member heir."""
    from sqlalchemy import text as sa_text

    async def build():
        solo_id = await _mint(SOLO)
        heir_id = await _mint(HEIR)
        async with await _session() as db:
            org = (await db.execute(sa_text(
                "SELECT id FROM organizations LIMIT 1"))).scalar()
            ws = (await db.execute(sa_text("""
                INSERT INTO workspaces (id, organization_id, name, is_default,
                                        created_at, updated_at)
                VALUES (gen_random_uuid(), :o, 'qa-del-solo-ws', false, now(), now())
                RETURNING id
            """), {"o": org})).scalar()
            for uid, role in ((solo_id, "admin"), (heir_id, "member")):
                await db.execute(sa_text("""
                    INSERT INTO workspace_members (id, workspace_id, user_id, role, created_at)
                    VALUES (gen_random_uuid(), :w, :u, :r, now())
                """), {"w": ws, "u": uid, "r": role})
            await db.commit()
            return {"solo": solo_id, "heir": heir_id, "workspace": str(ws)}

    info = _run(build())
    try:
        yield info
    finally:
        async def cleanup():
            from sqlalchemy import text as sa_text
            async with await _session() as db:
                await db.execute(sa_text(
                    "DELETE FROM workspaces WHERE name = 'qa-del-solo-ws'"))
                await db.commit()
        _run(cleanup())
        _run(_purge(SOLO))
        _run(_purge(HEIR))


def test_last_workspace_admin_requires_a_successor(admin_token, solo_admin_workspace):
    info = solo_admin_workspace

    # no successor -> 409, nothing changes
    status, raw = _delete_via_api(admin_token, info["solo"])
    assert status == 409, f"expected 409, got {status}: {raw[:300]}"
    assert _run(_scalar("SELECT count(*) FROM users WHERE id=:u",
                        u=info["solo"])) == 1

    # successor not a member of the workspace -> 409
    outsider = _run(_mint("qa_del_outsider"))
    try:
        status, raw = _delete_via_api(
            admin_token, info["solo"], f"?reassign_admin_to={outsider}")
        assert status == 409, f"non-member successor accepted: {raw[:300]}"
    finally:
        _run(_purge("qa_del_outsider"))

    # valid successor -> deletion succeeds and the heir is admin
    status, raw = _delete_via_api(
        admin_token, info["solo"], f"?reassign_admin_to={info['heir']}")
    assert status == 200, f"deletion with successor failed: {raw[:300]}"
    role = _run(_scalar("""
        SELECT role FROM workspace_members
        WHERE workspace_id = CAST(:w AS uuid) AND user_id = :u
    """, w=info["workspace"], u=info["heir"]))
    assert role == "admin", "successor was not promoted"
    # the workspace itself survives
    assert _run(_scalar(
        "SELECT count(*) FROM workspaces WHERE id = CAST(:w AS uuid)",
        w=info["workspace"])) == 1


# ---------------------------------------------------------------------------
# orphaned chat authorization
# ---------------------------------------------------------------------------

def test_orphaned_chat_is_admin_readable_and_immutable(admin_token, probe):
    conv_id = probe["conversation_id"]
    ws_id = probe["workspace_id"]
    status, raw = _delete_via_api(admin_token, probe["id"])
    assert status == 200, raw[:300]

    # A workspace ADMIN (the default workspace's admin is `admin`) can read it.
    # The endpoint always scopes to the default workspace and wraps the list.
    # Search by the fixture title: the probe conversation has last_message_at
    # NULL, which sorts LAST — in a full-suite run the admin's accumulated
    # conversations would page it out of a plain first-page listing, making
    # this test order-dependent (it failed exactly that way).
    status, raw = _http("GET", "/api/v1/conversations?q=qa%20deletion%20probe",
                        token=admin_token)
    assert status == 200, raw[:300]
    listed = {c["id"]: c for c in _json(raw).get("conversations", [])}
    assert conv_id in listed, "orphaned conversation invisible to workspace admin"
    entry = listed[conv_id]
    assert entry.get("orphaned") is True
    assert entry.get("author_username") == PROBE, (
        "orphaned conversation lost its author attribution")

    status, raw = _http("GET", f"/api/v1/conversations/{conv_id}/messages",
                        token=admin_token)
    assert status == 200, f"admin cannot read orphaned messages: {raw[:200]}"

    # Mutations must all be refused — visibility is not ownership.
    for method, path, body in (
        ("PATCH", f"/api/v1/conversations/{conv_id}", {"title": "hijacked"}),
        ("DELETE", f"/api/v1/conversations/{conv_id}", None),
    ):
        status, raw = _http(method, path, body, token=admin_token)
        assert status >= 400, (
            f"{method} on an orphaned conversation succeeded ({status}): "
            f"read-only means read-only")


def test_orphaned_chat_is_invisible_to_ordinary_members(admin_token, probe):
    from sqlalchemy import text as sa_text

    conv_id = probe["conversation_id"]
    ws_id = probe["workspace_id"]
    status, raw = _delete_via_api(admin_token, probe["id"])
    assert status == 200, raw[:300]

    member_id = _run(_mint("qa_del_member"))

    async def enroll():
        async with await _session() as db:
            await db.execute(sa_text("""
                INSERT INTO workspace_members (id, workspace_id, user_id, role, created_at)
                VALUES (gen_random_uuid(), CAST(:w AS uuid), :u, 'member', now())
                ON CONFLICT DO NOTHING
            """), {"w": ws_id, "u": member_id})
            await db.commit()

    _run(enroll())
    try:
        status, raw = _http("POST", "/api/auth/login",
                            {"username": "qa_del_member",
                             "password": PROBE_PASSWORD}, csrf=False)
        assert status == 200, raw[:200]
        member_token = _json(raw)["access_token"]

        # Search by the exact fixture title: a plain listing could omit the
        # orphan merely by pagination, making this assertion pass vacuously.
        # Searching proves the member cannot see it even when asking for it.
        status, raw = _http("GET", "/api/v1/conversations?q=qa%20deletion%20probe",
                            token=member_token)
        assert status == 200, raw[:200]
        assert conv_id not in {c["id"] for c in _json(raw).get("conversations", [])}, (
            "an ordinary member can see another (deleted) user's history")

        status, _ = _http("GET", f"/api/v1/conversations/{conv_id}/messages",
                          token=member_token)
        assert status >= 400, "an ordinary member can read orphaned messages"
    finally:
        _run(_purge("qa_del_member"))


# ---------------------------------------------------------------------------
# identity is never inherited
# ---------------------------------------------------------------------------

def test_recreated_username_is_a_new_account_and_inherits_nothing(admin_token, probe):
    old_id = probe["id"]
    status, raw = _delete_via_api(admin_token, old_id)
    assert status == 200, raw[:300]

    new_id = _run(_mint(PROBE))
    try:
        assert new_id != old_id, "PostgreSQL reused a user id"
        # No conversation, history or membership points at the NEW id.
        for table in ("conversations", "user_query_history", "search_history",
                      "chatbot_audit_log", "identity_audit_log",
                      "workspace_members"):
            assert _run(_scalar(
                f"SELECT count(*) FROM {table} WHERE user_id=:u", u=new_id)) == 0, (
                f"recreated account inherited rows in {table}")
        # The old history still resolves to the tombstone, not the new account.
        assert _run(_scalar(
            "SELECT count(*) FROM deleted_users WHERE user_id=:u AND username=:n",
            u=old_id, n=PROBE)) == 1
    finally:
        _run(_purge(PROBE))


def test_no_dangling_user_references_after_deletion(admin_token, probe):
    """Every preserved user_id is NULL or resolves to a live user."""
    status, raw = _delete_via_api(admin_token, probe["id"])
    assert status == 200, raw[:300]

    for table, column in (
        ("conversations", "user_id"), ("user_query_history", "user_id"),
        ("user_query_embeddings", "user_id"), ("search_history", "user_id"),
        ("chatbot_audit_log", "user_id"), ("identity_audit_log", "user_id"),
        ("identity_merges", "merged_by"), ("live_search_alerts", "created_by"),
        ("workspace_members", "user_id"),
    ):
        dangling = _run(_scalar(f"""
            SELECT count(*) FROM {table} t
            WHERE t.{column} IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = t.{column})
        """))
        assert dangling == 0, f"{table}.{column}: {dangling} dangling reference(s)"
