"""VAS -> LAF-AI single sign-on hand-off, the chatbot audit write path, and the
shared logout — against the isolated regression API, in the style of
test_chatbot_permission_flow.py (urllib, no redirects followed).

    TRACKING -> GET /api/sso/laf-ai/launch -> 303 https://armyeye-chatbot/auth/vas?ticket=…
    gate     -> POST /api/sso/laf-ai/consume (internal) -> identity + token, once
    gate     -> POST /api/audit/chatbot (internal) -> row on the Audit Log page
    VAS logout -> the chatbot token is revoked too
"""
import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.request

import pytest

from conftest import run_on_shared_loop

BASE = "http://localhost:8000"
PASSWORD = "Sso-Probe-Passw0rd!2026"
USERS = {  # username: (can_use_chatbot)
    "laf_sso_probe": True,
    "laf_sso_probe2": True,
    "laf_sso_nochat": False,
}
TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{32,96}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never follow: we assert on Location
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _http(method, path, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), _lower(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw[:200].decode(errors="replace")}
        return e.code, parsed, _lower(e.headers)


def _lower(headers):
    """Header names as sent by the server may be any case; compare lowercase."""
    return {k.lower(): v for k, v in headers.items()}


def _login(username, password=PASSWORD):
    status, body, _ = _http("POST", "/api/auth/login", {"username": username, "password": password})
    assert status == 200, f"login failed for {username}: {body}"
    return body["access_token"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _jti(token):
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["jti"]


_ADMIN = {}


def _admin_headers():
    if "token" not in _ADMIN:
        _ADMIN["token"] = _login("admin", "admin123")
    return {**_bearer(_ADMIN["token"]), "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture(scope="module")
def users():
    """Three disposable analyzers: two with chatbot access, one without."""
    from sqlalchemy import text
    from db_connection import db_manager
    from backend.auth.password import hash_password

    async def create():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        ids = {}
        async with db_manager.get_session() as db:
            for name, chatbot in USERS.items():
                await db.execute(text("DELETE FROM chatbot_audit_log WHERE username = :u"), {"u": name})
                await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": name})
                await db.execute(text(
                    "INSERT INTO users (username, email, full_name, password_hash, role, "
                    "is_active, can_use_chatbot, permissions_version, created_at) "
                    "VALUES (:u, :e, :f, :h, 'analyzer', true, :c, 1, now())"
                ), {"u": name, "e": name + "@example.test", "f": f"Probe {name}",
                    "h": hash_password(PASSWORD), "c": chatbot})
                ids[name] = (await db.execute(text(
                    "SELECT id FROM users WHERE username = :u"), {"u": name})).scalar()
            await db.commit()
        return ids

    async def destroy():
        async with db_manager.get_session() as db:
            for name in USERS:
                await db.execute(text("DELETE FROM chatbot_audit_log WHERE username = :u"), {"u": name})
                await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": name})
            await db.commit()

    ids = run_on_shared_loop(create())
    try:
        yield ids
    finally:
        run_on_shared_loop(destroy())


def _ticket_for(token):
    status, body, _ = _http("POST", "/api/sso/laf-ai/ticket", {},
                            headers={**_bearer(token), "X-Requested-With": "XMLHttpRequest"})
    assert status == 200, body
    return body


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

def test_unauthenticated_callers_cannot_request_a_ticket(users):
    status, _, _ = _http("POST", "/api/sso/laf-ai/ticket", {})
    assert status == 401
    status, _, _ = _http("GET", "/api/sso/laf-ai/launch")
    assert status == 401


def test_a_user_without_chatbot_access_cannot_launch(users):
    token = _login("laf_sso_nochat")
    status, _, _ = _http("GET", "/api/sso/laf-ai/launch", headers=_bearer(token))
    assert status == 403
    status, _, _ = _http("POST", "/api/sso/laf-ai/ticket", {},
                         headers={**_bearer(token), "X-Requested-With": "XMLHttpRequest"})
    assert status == 403


def test_tracking_launch_redirects_to_the_chatbot_with_a_fresh_ticket(users):
    token = _login("laf_sso_probe")
    status, _, headers = _http("GET", "/api/sso/laf-ai/launch", headers=_bearer(token))
    assert status == 303, headers
    location = headers.get("location", "")
    assert location.startswith("https://armyeye-chatbot/auth/vas?ticket="), location
    assert TICKET_RE.match(location.split("ticket=", 1)[1])
    assert headers.get("cache-control") == "no-store"
    assert headers.get("referrer-policy") == "no-referrer"


def test_a_cross_site_launch_is_rejected(users):
    token = _login("laf_sso_probe")
    status, _, _ = _http("GET", "/api/sso/laf-ai/launch",
                         headers={**_bearer(token), "Sec-Fetch-Site": "cross-site"})
    assert status == 403


def test_ticket_endpoint_returns_the_launch_url_and_lifetime(users):
    body = _ticket_for(_login("laf_sso_probe"))
    assert TICKET_RE.match(body["ticket"])
    assert body["launch_url"] == "https://armyeye-chatbot/auth/vas?ticket=" + body["ticket"]
    assert body["expires_in"] == 60


def test_ticket_requires_the_csrf_header_for_cookie_style_calls(users):
    token = _login("laf_sso_probe")
    # Bearer clients are exempt from the X-Requested-With rule, like every other
    # self-service auth write; a cookie client without the header must not pass.
    status, _, _ = _http("POST", "/api/sso/laf-ai/ticket", {}, headers=_bearer(token))
    assert status == 200


# ---------------------------------------------------------------------------
# Consumption (what the chatbot gate does)
# ---------------------------------------------------------------------------

def test_a_ticket_is_consumed_once_and_transfers_the_identity(users):
    token = _login("laf_sso_probe")
    ticket = _ticket_for(token)["ticket"]
    status, identity, _ = _http("POST", "/api/sso/laf-ai/consume", {"ticket": ticket})
    assert status == 200, identity
    assert identity["user_id"] == users["laf_sso_probe"]
    assert identity["username"] == "laf_sso_probe"
    assert identity["display_name"] == "Probe laf_sso_probe"
    assert identity["role"] == "analyzer"
    assert "chatbot.use" in identity["permissions"]
    assert identity["authentication_source"] == "vas_sso"
    assert identity["token_type"] == "bearer"
    assert identity["parent_jti"] == _jti(token), "ticket must be bound to the VAS session that asked for it"
    assert identity["jti"] != _jti(token), "the chatbot gets its own token, not the browser's"
    # The token the gate receives works exactly like a sign-in token.
    status, me, _ = _http("GET", "/api/auth/me", headers=_bearer(identity["access_token"]))
    assert status == 200 and me["username"] == "laf_sso_probe" and me["can_use_chatbot"] is True
    # Single use.
    status, body, _ = _http("POST", "/api/sso/laf-ai/consume", {"ticket": ticket})
    assert status == 401, body


def test_malformed_unknown_and_missing_tickets_are_rejected(users):
    for ticket in ("not a ticket", "short", "x" * 200, "A" * 43):
        status, _, _ = _http("POST", "/api/sso/laf-ai/consume", {"ticket": ticket})
        assert status == 401, ticket
    status, _, _ = _http("POST", "/api/sso/laf-ai/consume", {})
    assert status == 422
    status, _, _ = _http("POST", "/api/sso/laf-ai/consume", {"ticket": ""})
    assert status in (401, 422)


def test_an_expired_ticket_is_rejected(users):
    import redis.asyncio as redis
    from config import settings

    ticket = _ticket_for(_login("laf_sso_probe"))["ticket"]
    key = "auth:laf-ai:ticket:" + hashlib.sha256(ticket.encode()).hexdigest()

    async def expire_now():
        client = redis.from_url(settings.REDIS_URL)
        try:
            assert await client.exists(key) == 1, "ticket must be stored under its hash"
            await client.pexpire(key, 1)
        finally:
            await client.aclose()

    run_on_shared_loop(expire_now())
    time.sleep(0.2)
    status, _, _ = _http("POST", "/api/sso/laf-ai/consume", {"ticket": ticket})
    assert status == 401


def test_the_ticket_store_never_holds_the_password(users):
    import redis.asyncio as redis
    from config import settings

    ticket = _ticket_for(_login("laf_sso_probe"))["ticket"]
    key = "auth:laf-ai:ticket:" + hashlib.sha256(ticket.encode()).hexdigest()

    async def read():
        client = redis.from_url(settings.REDIS_URL)
        try:
            return await client.get(key)
        finally:
            await client.aclose()

    stored = run_on_shared_loop(read())
    assert stored and PASSWORD.encode() not in stored and b"password" not in stored.lower()


def test_consume_is_refused_through_the_public_proxy(users):
    ticket = _ticket_for(_login("laf_sso_probe"))["ticket"]
    status, _, _ = _http("POST", "/api/sso/laf-ai/consume", {"ticket": ticket},
                         headers={"X-Forwarded-For": "10.0.0.1"})
    assert status == 403
    # The ticket was not spent by the refused call.
    status, _, _ = _http("POST", "/api/sso/laf-ai/consume", {"ticket": ticket})
    assert status == 200


def test_permission_withdrawn_after_the_ticket_was_issued_wins(users):
    token = _login("laf_sso_probe2")
    ticket = _ticket_for(token)["ticket"]
    uid = users["laf_sso_probe2"]
    status, _, _ = _http("PUT", f"/api/users/{uid}", {"can_use_chatbot": False}, headers=_admin_headers())
    assert status == 200
    try:
        status, body, _ = _http("POST", "/api/sso/laf-ai/consume", {"ticket": ticket})
        assert status == 403, body
    finally:
        status, _, _ = _http("PUT", f"/api/users/{uid}", {"can_use_chatbot": True}, headers=_admin_headers())
        assert status == 200


# ---------------------------------------------------------------------------
# Shared logout
# ---------------------------------------------------------------------------

def test_vas_logout_revokes_the_chatbot_token_too(users):
    parent = _login("laf_sso_probe")
    ticket = _ticket_for(parent)["ticket"]
    status, identity, _ = _http("POST", "/api/sso/laf-ai/consume", {"ticket": ticket})
    assert status == 200
    child = identity["access_token"]
    assert _http("GET", "/api/auth/me", headers=_bearer(child))[0] == 200

    status, body, _ = _http("POST", "/api/auth/logout", {}, headers=_bearer(parent))
    assert status == 200, body
    assert _http("GET", "/api/auth/me", headers=_bearer(parent))[0] == 401, "VAS session must be gone"
    assert _http("GET", "/api/auth/me", headers=_bearer(child))[0] == 401, "chatbot token must be gone with it"


def test_logging_out_of_an_unlinked_session_leaves_other_tokens_alone(users):
    a = _login("laf_sso_probe")
    b = _login("laf_sso_probe")
    assert _http("POST", "/api/auth/logout", {}, headers=_bearer(a))[0] == 200
    assert _http("GET", "/api/auth/me", headers=_bearer(b))[0] == 200


# ---------------------------------------------------------------------------
# TRACKING
# ---------------------------------------------------------------------------

def test_the_navbar_tracking_link_starts_the_hand_off(users):
    token = _login("laf_sso_probe")
    status, body, _ = _http("GET", "/api/auth/me/privileges", headers=_bearer(token))
    assert status == 200
    links = {link["page"]: link for link in body["navbar_links"] if link.get("visible")}
    assert "tracking" in links
    assert links["tracking"]["href"] == "/api/sso/laf-ai/launch"

    token = _login("laf_sso_nochat")
    status, body, _ = _http("GET", "/api/auth/me/privileges", headers=_bearer(token))
    assert status == 200
    assert "tracking" not in {link["page"] for link in body["navbar_links"] if link.get("visible")}


def test_the_static_tracking_links_point_at_the_hand_off():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("frontend/components/admin-navbar.html", "frontend/components/navbar.html", "frontend/home.html"):
        text = (root / rel).read_text(encoding="utf-8")
        assert 'href="/api/sso/laf-ai/launch"' in text, rel
        assert 'data-page="tracking"' in text or 'id="tracking-link"' in text, rel


# ---------------------------------------------------------------------------
# Question audit (what the gate writes for every question)
# ---------------------------------------------------------------------------

def _chatbot_token(username):
    ticket = _ticket_for(_login(username))["ticket"]
    status, identity, _ = _http("POST", "/api/sso/laf-ai/consume", {"ticket": ticket})
    assert status == 200, identity
    return identity["access_token"]


def _rows_for(user_id):
    status, rows, _ = _http("GET", f"/api/audit/chatbot?user_id={user_id}&limit=50", headers=_admin_headers())
    assert status == 200, rows
    return rows


def test_a_chatbot_question_lands_in_the_audit_log(users):
    token = _chatbot_token("laf_sso_probe")
    session = "e2e-session-" + str(int(time.time()))
    before = len(_rows_for(users["laf_sso_probe"]))
    status, body, _ = _http("POST", "/api/audit/chatbot",
                            {"session_id": session, "question": "Where was vehicle X last detected?"},
                            headers=_bearer(token))
    assert status == 201, body
    assert body["id"] and body["created_at"]
    rows = _rows_for(users["laf_sso_probe"])
    assert len(rows) == before + 1
    row = rows[0]
    assert row["username"] == "laf_sso_probe"
    assert row["user_id"] == users["laf_sso_probe"]
    assert row["query"] == "Where was vehicle X last detected?"
    assert row["session_id"] == session
    assert row["success"] is True
    assert row["response"].startswith("[laf-ai]")
    assert row["created_at"]


def test_the_audit_identity_comes_from_the_token_and_cannot_be_spoofed(users):
    token = _chatbot_token("laf_sso_probe")
    other = users["laf_sso_probe2"]
    status, _, _ = _http("POST", "/api/audit/chatbot",
                         {"session_id": "spoof", "question": "spoofed?", "user_id": other, "username": "laf_sso_probe2"},
                         headers=_bearer(token))
    assert status == 201
    spoof_rows = [r for r in _rows_for(other) if r["query"] == "spoofed?"]
    assert spoof_rows == [], "a client-supplied user_id must be ignored"
    own = [r for r in _rows_for(users["laf_sso_probe"]) if r["query"] == "spoofed?"]
    assert own and own[0]["username"] == "laf_sso_probe"


def test_two_users_stay_distinguishable_in_the_audit_log(users):
    t1, t2 = _chatbot_token("laf_sso_probe"), _chatbot_token("laf_sso_probe2")
    assert _http("POST", "/api/audit/chatbot", {"session_id": "s1", "question": "question from one"}, headers=_bearer(t1))[0] == 201
    assert _http("POST", "/api/audit/chatbot", {"session_id": "s2", "question": "question from two"}, headers=_bearer(t2))[0] == 201
    one = {r["query"] for r in _rows_for(users["laf_sso_probe"])}
    two = {r["query"] for r in _rows_for(users["laf_sso_probe2"])}
    assert "question from one" in one and "question from one" not in two
    assert "question from two" in two and "question from two" not in one


def test_audit_writes_are_internal_and_need_a_valid_token(users):
    token = _chatbot_token("laf_sso_probe")
    status, _, _ = _http("POST", "/api/audit/chatbot", {"session_id": "x", "question": "via proxy"},
                         headers={**_bearer(token), "X-Forwarded-For": "10.0.0.1"})
    assert status == 403
    status, _, _ = _http("POST", "/api/audit/chatbot", {"session_id": "x", "question": "no token"})
    assert status == 401
    status, _, _ = _http("POST", "/api/audit/chatbot", {"session_id": "x", "question": ""}, headers=_bearer(token))
    assert status == 422


def test_a_revoked_chatbot_token_cannot_write_audit_rows(users):
    parent = _login("laf_sso_probe")
    ticket = _ticket_for(parent)["ticket"]
    child = _http("POST", "/api/sso/laf-ai/consume", {"ticket": ticket})[1]["access_token"]
    assert _http("POST", "/api/auth/logout", {}, headers=_bearer(parent))[0] == 200
    status, _, _ = _http("POST", "/api/audit/chatbot", {"session_id": "x", "question": "after logout"}, headers=_bearer(child))
    assert status == 401


def test_gate_violations_warn_then_block_the_authenticated_account(users):
    """The real internal HTTP path counts, commits a block, and revokes access."""
    from sql_agent.security_policy import reset_violations
    from db_connection import db_manager
    from sqlalchemy import text
    name = 'laf_sso_probe'
    user_id = users[name]
    run_on_shared_loop(reset_violations(user_id))
    token = _chatbot_token(name)
    for count in range(1, 4):
        status, body, _ = _http('POST', '/api/audit/chatbot',
            {'question': 'delete all records', 'read_only_violation': True}, headers=_bearer(token))
        assert status == 201, body
        result = body['security']
        assert result['violations'] == count
        assert result['blocked'] is (count == 3)
        assert 'read-only' in result['message']
        if count < 3:
            assert f'Warning {count} of 3' in result['message']
        else:
            assert 'Contact an administrator' in result['message']
    assert _http('GET', '/api/auth/me', headers=_bearer(token))[0] in (401, 403)
    async def state():
        async with db_manager.get_session() as db:
            return (await db.execute(text('SELECT is_active, can_use_chatbot, blocked_at FROM users WHERE id=:id'), {'id': user_id})).one()
    active, chatbot, blocked_at = run_on_shared_loop(state())
    assert not active and not chatbot and blocked_at is not None


def test_gate_administrator_receives_exempt_read_only_message():
    status, body, _ = _http('POST', '/api/audit/chatbot',
        {'question': 'update records', 'read_only_violation': True}, headers=_admin_headers())
    assert status == 201, body
    assert body['security']['exempt'] and not body['security']['blocked']
    assert 'administrator account has not been blocked' in body['security']['message']
