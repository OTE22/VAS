"""Chatbot permission must reach the NAVBAR, not just the database.

THE reported bug: granting chatbot access updated users.can_use_chatbot,
/api/auth/me and the capability list — but the regular-user branch of
/api/auth/me/privileges built navbar_links with DASHBOARD (+ UNKNOWN FACES /
LIVE ALERTS) and NO chatbot entry at all. The navbar renders from
navbar_links, so the user held the permission with no way to reach the
assistant, and the grant looked like it had not propagated.

Also covers the block-state contract: blocked_at/blocked_reason are
backend-controlled RESPONSE fields (absent from UpdateUserRequest), so
editing permissions can neither set nor clear them — while an explicit
reactivation must clear stale markers, or the users table shows an active
account with a permanent BLOCKED badge.
"""

import json
import urllib.error
import urllib.request

import pytest

from conftest import run_on_shared_loop

BASE = "http://localhost:8000"
PROBE_PASSWORD = "ProbePassword!123"
PROBE_USERNAME = "chatbot_probe"


def _http(method, path, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


_TOKENS = {}


def _bearer(username, password=PROBE_PASSWORD):
    if username not in _TOKENS:
        status, body = _http("POST", "/api/auth/login",
                             {"username": username, "password": password})
        assert status == 200, f"login failed for {username}: {body}"
        _TOKENS[username] = body["access_token"]
    return {"Authorization": f"Bearer {_TOKENS[username]}"}


def _admin_headers():
    headers = _bearer("admin", "admin123")
    return {**headers, "X-Requested-With": "XMLHttpRequest"}


def _set(user_id, **fields):
    status, body = _http("PUT", f"/api/users/{user_id}", fields, headers=_admin_headers())
    assert status == 200, f"admin update failed: {status} {body}"
    return body


def _navbar(auth):
    status, body = _http("GET", "/api/auth/me/privileges", headers=auth)
    assert status == 200, body
    return body, {link["page"] for link in body["navbar_links"] if link.get("visible")}


@pytest.fixture
def probe():
    """A disposable analyzer with chatbot access initially OFF."""
    from sqlalchemy import text
    from db_connection import db_manager
    from backend.auth.password import hash_password

    async def create():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"),
                             {"u": PROBE_USERNAME})
            await db.execute(text(
                "INSERT INTO users (username, email, full_name, password_hash, role, "
                "is_active, can_use_chatbot, permissions_version, created_at) "
                "VALUES (:u, :e, 'Chatbot Probe', :h, 'analyzer', true, false, 1, now())"
            ), {"u": PROBE_USERNAME, "e": PROBE_USERNAME + "@example.test",
                "h": hash_password(PROBE_PASSWORD)})
            await db.commit()
            return (await db.execute(text(
                "SELECT id FROM users WHERE username = :u"), {"u": PROBE_USERNAME})).scalar()

    async def destroy():
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"),
                             {"u": PROBE_USERNAME})
            await db.commit()

    user_id = run_on_shared_loop(create())
    _TOKENS.pop(PROBE_USERNAME, None)
    try:
        yield user_id
    finally:
        run_on_shared_loop(destroy())
        _TOKENS.pop(PROBE_USERNAME, None)


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------

def test_granting_chatbot_access_adds_the_navbar_link(probe):
    auth = _bearer(PROBE_USERNAME)

    body, pages = _navbar(auth)
    assert "tracking" not in pages, "chatbot link shown without access"
    assert body["can_access_chatbot"] is False

    _set(probe, can_use_chatbot=True)

    body, pages = _navbar(auth)
    assert "tracking" in pages, (
        "granting chatbot access did not add the navbar link — the user holds "
        "the permission but has no way to reach the assistant (the reported bug)"
    )
    assert body["can_access_chatbot"] is True


def test_revoking_chatbot_access_removes_the_navbar_link(probe):
    auth = _bearer(PROBE_USERNAME)
    _set(probe, can_use_chatbot=True)
    _body, pages = _navbar(auth)
    assert "tracking" in pages

    _set(probe, can_use_chatbot=False)
    body, pages = _navbar(auth)
    assert "tracking" not in pages, "revoked access still shows the chatbot link"
    assert body["can_access_chatbot"] is False


def test_admin_without_chatbot_access_gets_no_chatbot_link():
    """The page enforces require_chatbot_access(), so a link that 403s is
    worse than no link — admins are gated on the same flag."""
    from sqlalchemy import text
    from db_connection import db_manager

    auth = _bearer("admin", "admin123")

    async def read():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT can_use_chatbot FROM users WHERE username = 'admin'"))).scalar()

    async def write(value):
        async with db_manager.get_session() as db:
            await db.execute(text(
                "UPDATE users SET can_use_chatbot = :v WHERE username = 'admin'"),
                {"v": value})
            await db.commit()

    original = run_on_shared_loop(read())
    try:
        run_on_shared_loop(write(False))
        _body, pages = _navbar(auth)
        assert "tracking" not in pages, "admin without chatbot access still sees the link"

        run_on_shared_loop(write(True))
        _body, pages = _navbar(auth)
        assert "tracking" in pages
    finally:
        run_on_shared_loop(write(original))


def test_privileges_payload_keeps_the_pipelines_contract(probe):
    """The navbar dereferences privileges.pipelines — the key must exist."""
    _s, body = _http("GET", "/api/auth/me/privileges", headers=_bearer(PROBE_USERNAME))
    assert isinstance(body.get("pipelines"), list)
    assert isinstance(body.get("navbar_links"), list)


# ---------------------------------------------------------------------------
# Block-state contract
# ---------------------------------------------------------------------------

def test_block_fields_are_response_only():
    """blocked_at/blocked_reason appear in UserResponse (which is where they
    were observed in the network tab) and are ABSENT from UpdateUserRequest —
    the edit form cannot send them even by accident."""
    from backend.routes.users import UpdateUserRequest, UserResponse

    assert "blocked_at" not in UpdateUserRequest.model_fields
    assert "blocked_reason" not in UpdateUserRequest.model_fields
    assert "blocked_at" in UserResponse.model_fields
    assert "blocked_reason" in UserResponse.model_fields


def test_user_form_payload_is_an_explicit_allowlist():
    """The form must build its payload field by field, never by echoing the
    fetched user object back (which is how stray fields get sent)."""
    with open("/app/frontend/js/admin-users.js", encoding="utf-8") as f:
        src = f.read()
    payload = src.split("const formData = {", 1)[1].split("};", 1)[0]
    for stray in ("blocked_at", "blocked_reason", "created_at", "last_login", "id:"):
        assert stray not in payload, f"form payload includes {stray}"
    for expected in ("username", "email", "full_name", "can_use_chatbot",
                     "is_active", "pipeline_ids"):
        assert expected in payload, f"form payload lost {expected}"


def test_editing_permissions_preserves_block_information(probe):
    """Editing chatbot access on a blocked user must not disturb the block."""
    from sqlalchemy import text
    from db_connection import db_manager

    async def block():
        async with db_manager.get_session() as db:
            await db.execute(text(
                "UPDATE users SET is_active = false, blocked_reason = 'test block', "
                "blocked_at = now() WHERE id = :i"), {"i": probe})
            await db.commit()

    async def state():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT blocked_reason, blocked_at IS NOT NULL, is_active "
                "FROM users WHERE id = :i"), {"i": probe})).first()

    run_on_shared_loop(block())

    # Only chatbot access is sent; is_active deliberately absent.
    status, _b = _http("PUT", f"/api/users/{probe}",
                       {"can_use_chatbot": True}, headers=_admin_headers())
    assert status == 200

    reason, has_blocked_at, is_active = run_on_shared_loop(state())
    assert reason == "test block", "editing permissions cleared blocked_reason"
    assert has_blocked_at is True, "editing permissions cleared blocked_at"
    assert is_active is False, "editing permissions silently reactivated a blocked user"


def test_reactivation_clears_stale_block_markers(probe):
    """The users table derives its BLOCKED badge from blocked_reason/blocked_at,
    not is_active — reactivating must clear them or the row shows an active
    account permanently badged BLOCKED."""
    from sqlalchemy import text
    from db_connection import db_manager

    async def block():
        async with db_manager.get_session() as db:
            await db.execute(text(
                "UPDATE users SET is_active = false, blocked_reason = 'stale', "
                "blocked_at = now() WHERE id = :i"), {"i": probe})
            await db.commit()

    async def state():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT blocked_reason, blocked_at, is_active FROM users WHERE id = :i"
            ), {"i": probe})).first()

    run_on_shared_loop(block())
    status, _b = _http("PUT", f"/api/users/{probe}",
                       {"is_active": True}, headers=_admin_headers())
    assert status == 200

    reason, blocked_at, is_active = run_on_shared_loop(state())
    assert is_active is True
    assert reason is None and blocked_at is None, (
        "reactivated account kept its block markers"
    )


def test_deactivating_via_the_form_does_not_fabricate_a_block(probe):
    """Un-ticking 'active' is a deactivation, not a block: blocked_at stays
    backend-controlled and is set only by an explicit block action."""
    from sqlalchemy import text
    from db_connection import db_manager

    status, _b = _http("PUT", f"/api/users/{probe}",
                       {"is_active": False}, headers=_admin_headers())
    assert status == 200

    async def state():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT blocked_reason, blocked_at, is_active FROM users WHERE id = :i"
            ), {"i": probe})).first()

    reason, blocked_at, is_active = run_on_shared_loop(state())
    assert is_active is False
    assert reason is None and blocked_at is None, (
        "a plain deactivation fabricated block markers"
    )


# ---------------------------------------------------------------------------
# Frontend privilege caching
# ---------------------------------------------------------------------------

def _navbar_js():
    with open("/app/frontend/js/navbar-loader.js", encoding="utf-8") as f:
        return f.read()


def test_privilege_promise_is_in_flight_only():
    """A permanently memoized promise made the first answer the only answer:
    a privilege change could not be observed without a full reload."""
    src = _navbar_js()
    assert "_authFetch" in src
    assert ".finally(" in src, "the promise is never released after settling"
    assert "window[cacheKey] = null" in src, "settled promise is not cleared"
    assert "refreshAuthState" in src, "no forced-refresh entry point"


def test_logout_and_pageshow_drop_the_auth_promises():
    src = _navbar_js()
    clear = src.split("function clearCachedSessionView", 1)[1][:700]
    assert "__authPrivilegesPromise = null" in clear, (
        "logout leaves the previous user's privileges resolvable in-memory"
    )
    assert "__authMePromise = null" in clear
    assert "pageshow" in src, "bfcache restores never re-resolve privileges"


def test_navbar_tolerates_a_missing_pipelines_property():
    src = _navbar_js()
    assert "Array.isArray(privileges.pipelines) ? privileges.pipelines : []" in src, (
        "navbar dereferences privileges.pipelines unguarded — a partial "
        "payload would abort customization mid-render"
    )
