"""Conversation domain: tenancy isolation, branching, persistence, dual-write.

The acceptance criteria these tests exist for:
* Conversations survive restart (they are database rows, verified directly).
* Users can retrieve their authorized history and NOBODY else's.
* Workspace data is isolated; ids cannot be probed (owner mismatch == 404).
* Editing a message forks a branch; the original timeline is untouched.
* fr_readonly — the role that executes LLM-generated SQL — cannot read any
  chat table, so prompt injection cannot exfiltrate conversations.
* The streaming path's dual-write lands typed message blocks.
"""

import json
import urllib.error
import urllib.request

import pytest

from conftest import run_on_shared_loop

BASE = "http://localhost:8000"
PROBE_PASSWORD = "ProbePassword!123"


def _http(method, path, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {}


_TOKEN_CACHE = {}


def _bearer(username, password=PROBE_PASSWORD):
    if username not in _TOKEN_CACHE:
        status, body = _http("POST", "/api/auth/login",
                             {"username": username, "password": password})
        assert status == 200, f"login failed for {username}: {body}"
        _TOKEN_CACHE[username] = body["access_token"]
    return {"Authorization": f"Bearer {_TOKEN_CACHE[username]}",
            "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture(scope="module")
def probe_users():
    """Two users with chatbot access, torn down afterwards."""
    from sqlalchemy import text
    from db_connection import db_manager
    from backend.auth.password import hash_password

    names = ("conv_probe_a", "conv_probe_b")

    async def create():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            for name in names:
                await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": name})
                await db.execute(text("""
                    INSERT INTO users (username, email, full_name, password_hash, role,
                                       is_active, can_use_chatbot, permissions_version, created_at)
                    VALUES (:u, :e, :u, :h, 'analyzer', true, true, 1, now())
                """), {"u": name, "e": f"{name}@example.test",
                       "h": hash_password(PROBE_PASSWORD)})
            await db.commit()

    async def destroy():
        async with db_manager.get_session() as db:
            for name in names:
                await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": name})
            await db.commit()

    run_on_shared_loop(create())
    _TOKEN_CACHE.clear()
    try:
        yield names
    finally:
        run_on_shared_loop(destroy())
        _TOKEN_CACHE.clear()


# ---------------------------------------------------------------------------
# Lifecycle + persistence
# ---------------------------------------------------------------------------

def test_create_list_rename_pin_archive_delete(probe_users):
    user_a, _ = probe_users
    auth = _bearer(user_a)

    status, created = _http("POST", "/api/v1/conversations",
                            {"title": "Quarterly detections"}, headers=auth)
    assert status == 200, created
    conv_id = created["id"]
    assert created["primary_branch_id"]

    status, listing = _http("GET", "/api/v1/conversations", headers=auth)
    assert status == 200
    assert any(c["id"] == conv_id for c in listing["conversations"])

    status, renamed = _http("PATCH", f"/api/v1/conversations/{conv_id}",
                            {"title": "Renamed thread"}, headers=auth)
    assert status == 200 and renamed["title"] == "Renamed thread"

    status, flagged = _http("PATCH", f"/api/v1/conversations/{conv_id}/flags",
                            {"pinned": True}, headers=auth)
    assert status == 200 and flagged["pinned"] is True

    status, archived = _http("PATCH", f"/api/v1/conversations/{conv_id}/flags",
                             {"archived": True}, headers=auth)
    assert status == 200 and archived["archived"] is True

    # Archived conversations leave the default listing but not the archive view.
    _s, default_list = _http("GET", "/api/v1/conversations", headers=auth)
    assert not any(c["id"] == conv_id for c in default_list["conversations"])
    _s, archive_list = _http("GET", "/api/v1/conversations?include_archived=true",
                             headers=auth)
    assert any(c["id"] == conv_id for c in archive_list["conversations"])

    status, _b = _http("DELETE", f"/api/v1/conversations/{conv_id}", headers=auth)
    assert status == 200
    _s, after_delete = _http("GET", "/api/v1/conversations?include_archived=true",
                             headers=auth)
    assert not any(c["id"] == conv_id for c in after_delete["conversations"])

    # Soft delete: the ROW survives for retention/recovery; only the UI hides it.
    from sqlalchemy import text
    from db_connection import db_manager

    async def row_state():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT deleted_at IS NOT NULL FROM conversations WHERE id = :i"
            ), {"i": conv_id})).scalar()

    assert run_on_shared_loop(row_state()) is True, "hard-deleted instead of soft"


def test_conversation_survives_reload(probe_users):
    """Persistence criterion: what the API returns is database state, so a new
    request (fresh session, fresh process would behave identically) sees it."""
    user_a, _ = probe_users
    auth = _bearer(user_a)
    _s, created = _http("POST", "/api/v1/conversations",
                        {"title": "Persistence check"}, headers=auth)
    conv_id = created["id"]

    from sqlalchemy import text
    from db_connection import db_manager

    async def exists():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT count(*) FROM conversations WHERE id = :i"), {"i": conv_id}
            )).scalar()

    assert run_on_shared_loop(exists()) == 1


# ---------------------------------------------------------------------------
# Tenancy isolation
# ---------------------------------------------------------------------------

def test_users_cannot_see_each_others_conversations(probe_users):
    user_a, user_b = probe_users
    auth_a, auth_b = _bearer(user_a), _bearer(user_b)

    _s, created = _http("POST", "/api/v1/conversations",
                        {"title": "Private to A"}, headers=auth_a)
    conv_id = created["id"]

    # B's listing must not contain it.
    _s, listing_b = _http("GET", "/api/v1/conversations", headers=auth_b)
    assert not any(c["id"] == conv_id for c in listing_b["conversations"])

    # Direct id access must 404 (indistinguishable from nonexistent).
    status, _b = _http("GET", f"/api/v1/conversations/{conv_id}/messages", headers=auth_b)
    assert status == 404, f"cross-user read returned {status}"
    status, _b = _http("PATCH", f"/api/v1/conversations/{conv_id}",
                       {"title": "hijacked"}, headers=auth_b)
    assert status == 404, f"cross-user rename returned {status}"
    status, _b = _http("DELETE", f"/api/v1/conversations/{conv_id}", headers=auth_b)
    assert status == 404, f"cross-user delete returned {status}"

    # And A still owns an intact conversation.
    _s, listing_a = _http("GET", "/api/v1/conversations", headers=auth_a)
    mine = [c for c in listing_a["conversations"] if c["id"] == conv_id]
    assert mine and mine[0]["title"] == "Private to A"


def test_malformed_ids_are_not_found_not_errors(probe_users):
    user_a, _ = probe_users
    status, _b = _http("GET", "/api/v1/conversations/not-a-uuid/messages",
                       headers=_bearer(user_a))
    assert status == 404, f"malformed id produced {status}, should read as not-found"


# ---------------------------------------------------------------------------
# Branching
# ---------------------------------------------------------------------------

def test_editing_a_message_forks_a_branch_and_preserves_the_original(probe_users):
    user_a, _ = probe_users
    auth = _bearer(user_a)
    from sqlalchemy import text
    from db_connection import db_manager
    from backend.services.conversation_service import append_exchange

    _s, created = _http("POST", "/api/v1/conversations",
                        {"title": "Branching"}, headers=auth)
    conv_id = created["id"]

    async def uid():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT id FROM users WHERE username = :u"), {"u": user_a})).scalar()
    user_id = run_on_shared_loop(uid())

    async def seed():
        async with db_manager.get_session() as db:
            await append_exchange(db, user_id, conv_id, "first question",
                                  [{"type": "text", "text": "first answer"}])
            await append_exchange(db, user_id, conv_id, "second question",
                                  [{"type": "text", "text": "second answer"}])
            await db.commit()
    run_on_shared_loop(seed())

    _s, original = _http("GET", f"/api/v1/conversations/{conv_id}/messages", headers=auth)
    assert len(original["messages"]) == 4
    second_user_msg = [m for m in original["messages"]
                       if m["role"] == "user" and m["sequence"] == 3][0]

    status, fork = _http("POST", f"/api/v1/conversations/{conv_id}/branches",
                         {"message_id": second_user_msg["id"],
                          "new_text": "second question, rephrased"},
                         headers=auth)
    assert status == 200, fork

    # The new branch: prefix (2 messages) + the edited message.
    _s, branched = _http(
        "GET",
        f"/api/v1/conversations/{conv_id}/messages?branch_id={fork['branch_id']}",
        headers=auth)
    roles = [(m["role"], m["sequence"]) for m in branched["messages"]]
    assert roles == [("user", 1), ("assistant", 2), ("user", 3)], roles
    assert branched["messages"][-1]["content_blocks"][0]["text"] == "second question, rephrased"

    # The ORIGINAL branch is untouched — that is what branching means.
    _s, original_after = _http(f"GET",
                               f"/api/v1/conversations/{conv_id}/messages", headers=auth)
    assert len(original_after["messages"]) == 4

    _s, branches = _http("GET", f"/api/v1/conversations/{conv_id}/branches", headers=auth)
    assert len(branches["branches"]) == 2
    assert sum(1 for b in branches["branches"] if b["is_primary"]) == 1


def test_only_user_messages_can_fork(probe_users):
    user_a, _ = probe_users
    auth = _bearer(user_a)
    _s, listing = _http("GET", "/api/v1/conversations", headers=auth)
    target = None
    for c in listing["conversations"]:
        _s2, msgs = _http(f"GET", f"/api/v1/conversations/{c['id']}/messages", headers=auth)
        assistants = [m for m in msgs["messages"] if m["role"] == "assistant"]
        if assistants:
            target = (c["id"], assistants[0]["id"])
            break
    if target is None:
        pytest.skip("no assistant message available")
    conv_id, message_id = target
    status, body = _http("POST", f"/api/v1/conversations/{conv_id}/branches",
                         {"message_id": message_id, "new_text": "x"}, headers=auth)
    assert status == 400, f"editing an assistant message should 400, got {status}"


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

def test_feedback_upserts_per_user_per_message(probe_users):
    user_a, _ = probe_users
    auth = _bearer(user_a)
    from sqlalchemy import text
    from db_connection import db_manager
    from backend.services.conversation_service import append_exchange

    _s, created = _http("POST", "/api/v1/conversations",
                        {"title": "Feedback"}, headers=auth)
    conv_id = created["id"]

    async def uid():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT id FROM users WHERE username = :u"), {"u": user_a})).scalar()
    user_id = run_on_shared_loop(uid())

    async def seed():
        async with db_manager.get_session() as db:
            await append_exchange(db, user_id, conv_id, "q",
                                  [{"type": "text", "text": "a"}])
            await db.commit()
    run_on_shared_loop(seed())

    _s, msgs = _http("GET", f"/api/v1/conversations/{conv_id}/messages", headers=auth)
    assistant_id = [m for m in msgs["messages"] if m["role"] == "assistant"][0]["id"]

    for rating in (1, -1):  # second call must UPDATE, not violate the unique index
        status, _b = _http("POST", f"/api/v1/conversations/{conv_id}/feedback",
                           {"message_id": assistant_id, "rating": rating}, headers=auth)
        assert status == 200

    async def feedback_state():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT count(*), min(rating) FROM message_feedback WHERE message_id = :m"
            ), {"m": assistant_id})).first()
    count, rating = run_on_shared_loop(feedback_state())
    assert count == 1 and rating == -1


# ---------------------------------------------------------------------------
# Security: the SQL-execution role must not see chat tables
# ---------------------------------------------------------------------------

def test_fr_readonly_cannot_read_any_chat_table():
    """fr_readonly executes LLM-generated SQL. If it can SELECT these tables,
    a prompt injection can exfiltrate private conversations."""
    from sqlalchemy import text
    from db_connection import db_manager

    tables = ("organizations", "workspaces", "workspace_members",
              "conversations", "conversation_branches", "messages",
              "message_feedback")

    async def check():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        denied = {}
        async with db_manager.get_session() as db:
            role_exists = (await db.execute(text(
                "SELECT 1 FROM pg_roles WHERE rolname = 'fr_readonly'"))).scalar()
            if not role_exists:
                return None
            for table in tables:
                denied[table] = (await db.execute(text(
                    "SELECT has_table_privilege('fr_readonly', :t, 'SELECT')"
                ), {"t": table})).scalar()
        return denied

    result = run_on_shared_loop(check())
    if result is None:
        pytest.skip("fr_readonly role not present in this database")
    readable = [t for t, can_read in result.items() if can_read]
    assert not readable, (
        f"fr_readonly can SELECT {readable} — LLM-generated SQL could read "
        f"private conversations"
    )


# ---------------------------------------------------------------------------
# Backfill + dual-write
# ---------------------------------------------------------------------------

def test_every_history_session_has_a_conversation():
    """Backfill criterion: no pre-existing history was left behind."""
    from sqlalchemy import text
    from db_connection import db_manager

    async def orphans():
        async with db_manager.get_session() as db:
            return (await db.execute(text("""
                SELECT count(DISTINCT (h.user_id, COALESCE(h.session_id, '')))
                FROM user_query_history h
                WHERE NOT EXISTS (
                    SELECT 1 FROM conversations c
                    WHERE c.user_id = h.user_id
                      AND COALESCE(c.legacy_session_id, '') = COALESCE(h.session_id, '')
                )
            """))).scalar()

    assert run_on_shared_loop(orphans()) == 0, "history sessions missing conversations"


def test_stream_persistence_dual_writes_into_conversations(probe_users):
    """persist_query_history must land typed blocks in the conversation model."""
    user_a, _ = probe_users
    from sqlalchemy import text
    from db_connection import db_manager
    from sql_agent.api.routes import persist_query_history

    marker_session = "dualwrite_probe_session"

    async def uid():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT id FROM users WHERE username = :u"), {"u": user_a})).scalar()
    user_id = run_on_shared_loop(uid())

    async def scenario():
        await persist_query_history(
            user_id=user_id, query="how many cameras?",
            response="There are 6 cameras.", session_id=marker_session,
            success=True, processing_time_ms=42.0,
            metadata={"sql": "SELECT count(*) FROM pipelines"},
        )
        async with db_manager.get_session() as db:
            return (await db.execute(text("""
                SELECT m.role, m.content_blocks
                FROM conversations c
                JOIN conversation_branches b ON b.conversation_id = c.id
                JOIN messages m ON m.branch_id = b.id
                WHERE c.user_id = :uid AND c.legacy_session_id = :sess
                ORDER BY m.sequence
            """), {"uid": user_id, "sess": marker_session})).fetchall()

    rows = run_on_shared_loop(scenario())
    assert len(rows) == 2, f"expected user+assistant messages, got {len(rows)}"
    assert rows[0][0] == "user"
    assistant_blocks = rows[1][1]
    types = [b["type"] for b in assistant_blocks]
    assert "text" in types and "sql" in types, (
        f"assistant message lost its typed blocks: {types}"
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_search_matches_titles_and_message_content(probe_users):
    user_a, _ = probe_users
    auth = _bearer(user_a)
    from sqlalchemy import text
    from db_connection import db_manager
    from backend.services.conversation_service import append_exchange

    _s, created = _http("POST", "/api/v1/conversations",
                        {"title": "Uniquely Named Falcon Thread"}, headers=auth)
    conv_id = created["id"]

    async def uid():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT id FROM users WHERE username = :u"), {"u": user_a})).scalar()
    user_id = run_on_shared_loop(uid())

    async def seed():
        async with db_manager.get_session() as db:
            await append_exchange(db, user_id, conv_id, "where was the osprey detected",
                                  [{"type": "text", "text": "The osprey appeared on camera 3."}])
            await db.commit()
    run_on_shared_loop(seed())

    # Title match
    _s, by_title = _http("GET", "/api/v1/conversations?q=Falcon", headers=auth)
    assert any(c["id"] == conv_id for c in by_title["conversations"]), "title search missed"

    # Message-content match (the term appears only inside message blocks)
    _s, by_content = _http("GET", "/api/v1/conversations?q=osprey", headers=auth)
    assert any(c["id"] == conv_id for c in by_content["conversations"]), "content search missed"

    # No match
    _s, none = _http("GET", "/api/v1/conversations?q=zz_absent_zz", headers=auth)
    assert not any(c["id"] == conv_id for c in none["conversations"])


def test_search_cannot_widen_visibility_across_users(probe_users):
    """Searching for another user's content must return nothing.

    The search predicate is ORed onto the listing query; if it were ORed onto
    the WHOLE where-clause instead of just the title/content pair, a match in
    someone else's conversation would leak it. This pins the scoping.
    """
    user_a, user_b = probe_users
    # user_a owns the Falcon/osprey conversation from the previous test; B searches for it.
    _s, results = _http("GET", "/api/v1/conversations?q=osprey", headers=_bearer(user_b))
    assert results["conversations"] == [], (
        "search returned another user's conversation"
    )


# ---------------------------------------------------------------------------
# Send-into-conversation (B2)
# ---------------------------------------------------------------------------

def test_stream_rejects_a_foreign_conversation_id_before_any_work(probe_users):
    """A conversation the caller does not own must be refused up front.

    The rejection happens BEFORE the agent starts, so the terminal error is
    the FIRST event — this also keeps the test fast (no LLM involved).
    """
    user_a, user_b = probe_users
    _s, created = _http("POST", "/api/v1/conversations",
                        {"title": "A's target"}, headers=_bearer(user_a))
    conv_id = created["id"]

    # Raw fetch: the response is SSE text, not JSON, so the shared JSON helper
    # cannot be used here.
    import urllib.request as _ur
    req = _ur.Request(BASE + "/api/sql-agent/query/stream",
                      data=json.dumps({"query": "hello", "conversation_id": conv_id}).encode(),
                      method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", _bearer(user_b)["Authorization"])
    with _ur.urlopen(req, timeout=30) as resp:
        first = resp.read(500).decode()
    assert "CONVERSATION_NOT_FOUND" in first, f"foreign id accepted: {first[:200]}"


def test_persist_targets_the_explicit_conversation(probe_users):
    """With a conversation_id, the exchange lands in THAT conversation."""
    user_a, _ = probe_users
    auth = _bearer(user_a)
    from sqlalchemy import text
    from db_connection import db_manager
    from sql_agent.api.routes import persist_query_history

    _s, created = _http("POST", "/api/v1/conversations",
                        {"title": "Explicit target"}, headers=auth)
    conv_id = created["id"]

    async def uid():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT id FROM users WHERE username = :u"), {"u": user_a})).scalar()
    user_id = run_on_shared_loop(uid())

    async def scenario():
        await persist_query_history(
            user_id=user_id, query="targeted question",
            response="targeted answer", session_id="some_other_session",
            success=True, processing_time_ms=5.0, metadata={},
            conversation_id=conv_id,
        )
        async with db_manager.get_session() as db:
            return (await db.execute(text("""
                SELECT count(*) FROM messages m
                JOIN conversation_branches b ON m.branch_id = b.id
                WHERE b.conversation_id = :c
            """), {"c": conv_id})).scalar()

    assert run_on_shared_loop(scenario()) == 2, "exchange did not land in the target"


def test_forged_conversation_id_cannot_write_into_another_users_thread(probe_users):
    """Defense in depth: even if the route check were bypassed, the service
    re-checks ownership and files the exchange in the CALLER's space instead.
    """
    user_a, user_b = probe_users
    from sqlalchemy import text
    from db_connection import db_manager
    from sql_agent.api.routes import persist_query_history

    _s, created = _http("POST", "/api/v1/conversations",
                        {"title": "A's private thread"}, headers=_bearer(user_a))
    a_conv_id = created["id"]

    async def uid(name):
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT id FROM users WHERE username = :u"), {"u": name})).scalar()
    b_id = run_on_shared_loop(uid(user_b))

    async def scenario():
        # B calls persist with A's conversation id.
        await persist_query_history(
            user_id=b_id, query="intrusion attempt",
            response="should not land in A's thread", session_id="b_fallback_session",
            success=True, processing_time_ms=1.0, metadata={},
            conversation_id=a_conv_id,
        )
        async with db_manager.get_session() as db:
            in_a = (await db.execute(text("""
                SELECT count(*) FROM messages m
                JOIN conversation_branches b ON m.branch_id = b.id
                WHERE b.conversation_id = :c
            """), {"c": a_conv_id})).scalar()
            in_b_fallback = (await db.execute(text("""
                SELECT count(*) FROM conversations
                WHERE user_id = :u AND legacy_session_id = 'b_fallback_session'
            """), {"u": b_id})).scalar()
            return in_a, in_b_fallback

    in_a, in_b_fallback = run_on_shared_loop(scenario())
    assert in_a == 0, "B's message landed in A's conversation"
    assert in_b_fallback == 1, "fallback placement missing — the message was lost"
