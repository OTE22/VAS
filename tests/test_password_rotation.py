"""
Forced password rotation
========================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_password_rotation.py -v

A password someone else chose is not the account owner's password. Two cases
produce one: the bootstrap admin seeded at deployment from
`secrets/bootstrap_admin_password`, and any account an administrator creates or
resets by typing a password into the admin UI. In both, the credential is known
to someone other than the owner, and until it is replaced the account is not
really theirs.

What is proven here:
  * an account flagged for rotation can still LOG IN (the credential was
    correct) and is told where to go — but every gated API answers 403
    PASSWORD_ROTATION_REQUIRED, so a client that ignores the redirect gains
    nothing
  * the exempt endpoints stay reachable: /api/auth/me, logout, the
    change-password endpoint itself, and the change-password page
  * changing the password requires the CURRENT one, refuses reuse, and enforces
    the same strength policy used to assess the deployment seed
  * a successful change clears the flag, stamps password_changed_at, and ends
    every OTHER session for that user
  * admin-created users are flagged; an admin resetting ANOTHER account flags
    it; an admin resetting their OWN does not (that would be a loop)

The regression stack deliberately runs with BOOTSTRAP_ADMIN_REQUIRE_ROTATION
false, so these tests seed their own users rather than relying on the bootstrap
admin's flag.
"""

import json
import urllib.error
import urllib.request

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"
BROWSER = {"X-Requested-With": "XMLHttpRequest"}

STRONG_PASSWORD = "Rotation!Probe#2026"
ANOTHER_STRONG_PASSWORD = "Second!Rotation#2026"
SEEDED_PASSWORD = "AdminAssigned!2026"

JS_PATH = "/app/frontend/js/change-password.js"
SIGNIN_JS_PATH = "/app/frontend/js/signin.js"
NAVBAR_JS_PATH = "/app/frontend/js/navbar-loader.js"
AUTH_SERVICE_PATH = "/app/backend/auth/auth_service.py"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _http(method, path, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                payload = {}
            return resp.status, payload, dict((k.lower(), v) for k, v in resp.getheaders())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}
        return e.code, payload, dict((k.lower(), v) for k, v in e.headers.items())


def _login(username, password, headers=None):
    return _http("POST", "/api/auth/login",
                 {"username": username, "password": password}, headers)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def _seed_user(username, password, *, must_change, role="user"):
    from sqlalchemy import text
    from db_connection import db_manager

    async def seed():
        from backend.auth.password import hash_password
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.execute(text("""
                INSERT INTO users (username, email, full_name, password_hash, role,
                                   is_active, can_use_chatbot, must_change_password,
                                   created_at)
                VALUES (:u, :e, 'Rotation Probe', :h, :r, true, false, :m, now())
            """), {"u": username, "e": f"{username}@example.test",
                   "h": hash_password(password), "r": role, "m": must_change})
            await db.commit()

    run_async(seed())


def _delete_user(username):
    from sqlalchemy import text
    from db_connection import db_manager

    async def cleanup():
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.commit()

    run_async(cleanup())


def _read_flags(username):
    """(must_change_password, password_changed_at) straight from the database."""
    from sqlalchemy import text
    from db_connection import db_manager

    async def read():
        async with db_manager.get_session() as db:
            row = (await db.execute(text(
                "SELECT must_change_password, password_changed_at "
                "FROM users WHERE username = :u"
            ), {"u": username})).first()
            return (row[0], row[1]) if row else (None, None)

    return run_async(read())


@pytest.fixture
def pending_user():
    """An account still carrying an admin-assigned password."""
    username = "rotation_pending_probe"
    _seed_user(username, SEEDED_PASSWORD, must_change=True)
    try:
        yield username
    finally:
        _delete_user(username)


@pytest.fixture
def settled_user():
    """An account whose password is its owner's already."""
    username = "rotation_settled_probe"
    _seed_user(username, STRONG_PASSWORD, must_change=False)
    try:
        yield username
    finally:
        _delete_user(username)


# ---------------------------------------------------------------------------
# Login still works, and says where to go
# ---------------------------------------------------------------------------

def test_pending_user_can_log_in_and_is_sent_to_change_password(pending_user):
    """The credential was correct, so the login succeeds. It must, or the user
    would have no session with which to change the password."""
    status, body, _ = _login(pending_user, SEEDED_PASSWORD, BROWSER)
    assert status == 200, body
    assert body["rotation_required"] is True
    assert body["redirect_url"] == "/change-password"


def test_settled_user_login_reports_no_rotation(settled_user):
    status, body, _ = _login(settled_user, STRONG_PASSWORD, BROWSER)
    assert status == 200, body
    assert body["rotation_required"] is False
    assert body["redirect_url"] == "/dashboard"


def test_pending_login_body_carries_no_credential_material(pending_user):
    """The rotation signal must not smuggle anything sensitive into the login
    body, which is why it is a bare boolean named `rotation_required`.

    Note what is NOT asserted: the literal substring "password". A pending
    login's redirect_url IS "/change-password", so the blanket substring rule
    that test_login_response_is_minimal applies to a settled account cannot
    hold here. A destination path is not credential material — the properties
    that matter are that no field is a password and no secret is echoed back.
    """
    status, body, _ = _login(pending_user, SEEDED_PASSWORD, BROWSER)
    assert status == 200, body

    dumped = json.dumps(body)
    assert SEEDED_PASSWORD not in dumped, "the submitted credential must never be echoed"
    for banned in ("hash", "session_id", "blocked_reason", "email"):
        assert banned not in dumped.lower(), f"login response leaks '{banned}'"

    # No field may be a password field, at any depth.
    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    offenders = [k for k in keys(body) if "password" in k.lower()]
    assert not offenders, f"login response carries password-named fields: {offenders}"
    assert body["user"].keys() == {"id", "username", "role"}


# ---------------------------------------------------------------------------
# The gate: a session is not authority
# ---------------------------------------------------------------------------

def test_pending_user_is_refused_by_gated_apis(pending_user):
    """The whole point. A client that ignores the redirect and calls the API
    with its perfectly valid token still gets nothing."""
    status, body, _ = _login(pending_user, SEEDED_PASSWORD)
    assert status == 200, body
    token = body["access_token"]

    status, body, _ = _http("GET", "/api/auth/me/privileges", headers=_bearer(token))
    assert status == 403, f"a pending account must not reach gated APIs: {status} {body}"
    detail = body.get("detail")
    assert isinstance(detail, dict), detail
    assert detail.get("code") == "PASSWORD_ROTATION_REQUIRED"


def test_pending_user_can_still_read_me_and_log_out(pending_user):
    """The exemptions exist so the user can be TOLD what is wrong and can
    leave. Refusing these would strand them on a blank page."""
    status, body, _ = _login(pending_user, SEEDED_PASSWORD)
    assert status == 200, body
    token = body["access_token"]

    status, me, _ = _http("GET", "/api/auth/me", headers=_bearer(token))
    assert status == 200, me
    assert me["rotation_required"] is True

    status, out, _ = _http("POST", "/api/auth/logout", headers=_bearer(token))
    assert status == 200, out


def test_change_password_page_is_reachable_while_pending(pending_user):
    status, body, _ = _login(pending_user, SEEDED_PASSWORD)
    assert status == 200, body
    token = body["access_token"]

    status, _, _ = _http("GET", "/change-password", headers=_bearer(token))
    assert status == 200, "the one page a pending user must be able to open"


# ---------------------------------------------------------------------------
# Changing the password
# ---------------------------------------------------------------------------

def test_change_requires_the_current_password(pending_user):
    status, body, _ = _login(pending_user, SEEDED_PASSWORD)
    token = body["access_token"]

    status, body, _ = _http("POST", "/api/auth/change-password", {
        "current_password": "NotTheRightOne!2026",
        "new_password": STRONG_PASSWORD,
    }, headers=_bearer(token))
    assert status == 403, body
    assert body["error"]["code"] == "INVALID_CURRENT_PASSWORD"

    # And the account is still locked down.
    must_change, _ = _read_flags(pending_user)
    assert must_change is True, "a failed attempt must not clear the flag"


def test_change_refuses_reusing_the_assigned_password(pending_user):
    """Re-submitting the seeded credential would satisfy the flag while leaving
    the shared secret in place — exactly what this feature exists to prevent."""
    status, body, _ = _login(pending_user, SEEDED_PASSWORD)
    token = body["access_token"]

    status, body, _ = _http("POST", "/api/auth/change-password", {
        "current_password": SEEDED_PASSWORD,
        "new_password": SEEDED_PASSWORD,
    }, headers=_bearer(token))
    assert status == 400, body
    assert body["error"]["code"] == "PASSWORD_REUSED"

    must_change, _ = _read_flags(pending_user)
    assert must_change is True


def test_change_enforces_the_strength_policy(pending_user):
    status, body, _ = _login(pending_user, SEEDED_PASSWORD)
    token = body["access_token"]

    status, body, _ = _http("POST", "/api/auth/change-password", {
        "current_password": SEEDED_PASSWORD,
        "new_password": "short",
    }, headers=_bearer(token))
    assert status == 400, body
    assert body["error"]["code"] == "WEAK_PASSWORD"

    must_change, _ = _read_flags(pending_user)
    assert must_change is True


def test_successful_change_clears_the_flag_and_grants_access(pending_user):
    status, body, _ = _login(pending_user, SEEDED_PASSWORD)
    token = body["access_token"]

    status, body, _ = _http("POST", "/api/auth/change-password", {
        "current_password": SEEDED_PASSWORD,
        "new_password": STRONG_PASSWORD,
    }, headers=_bearer(token))
    assert status == 200, body
    assert body["redirect_url"] == "/dashboard"
    new_token = body["access_token"]

    must_change, changed_at = _read_flags(pending_user)
    assert must_change is False, "the account is now the user's own"
    assert changed_at is not None, "password_changed_at is what ends other sessions"

    # The replacement session works on a gated endpoint the old one could not use.
    status, privileges, _ = _http("GET", "/api/auth/me/privileges", headers=_bearer(new_token))
    assert status == 200, privileges

    # And the new password is the real one now.
    status, body, _ = _login(pending_user, STRONG_PASSWORD, BROWSER)
    assert status == 200, body
    assert body["rotation_required"] is False
    assert body["redirect_url"] == "/dashboard"


def test_changing_the_password_ends_other_sessions(settled_user):
    """A password change is often a reaction to a compromise. Leaving the other
    sessions alive would defeat the point of changing it."""
    status, first, _ = _login(settled_user, STRONG_PASSWORD)
    assert status == 200, first
    other_session = first["access_token"]

    status, second, _ = _login(settled_user, STRONG_PASSWORD)
    assert status == 200, second
    changing_session = second["access_token"]

    # Both work right now.
    assert _http("GET", "/api/auth/me", headers=_bearer(other_session))[0] == 200

    status, body, _ = _http("POST", "/api/auth/change-password", {
        "current_password": STRONG_PASSWORD,
        "new_password": ANOTHER_STRONG_PASSWORD,
    }, headers=_bearer(changing_session))
    assert status == 200, body

    status, _, _ = _http("GET", "/api/auth/me", headers=_bearer(other_session))
    assert status == 401, "the session that did not change the password must die"

    status, _, _ = _http("GET", "/api/auth/me", headers=_bearer(body["access_token"]))
    assert status == 200, "the caller keeps working with the token issued to them"


def test_change_password_requires_the_csrf_header_for_cookie_clients(pending_user):
    """Bearer clients are exempt (a browser cannot attach one cross-site), so
    the header is proven with a cookie session."""
    status, body, headers = _login(pending_user, SEEDED_PASSWORD, BROWSER)
    assert status == 200, body
    cookie = headers.get("set-cookie", "").split(";")[0]
    assert cookie, "login must set the session cookie"

    status, body, _ = _http("POST", "/api/auth/change-password", {
        "current_password": SEEDED_PASSWORD,
        "new_password": STRONG_PASSWORD,
    }, headers={"Cookie": cookie})
    assert status == 403, body

    must_change, _ = _read_flags(pending_user)
    assert must_change is True, "a CSRF-rejected request must change nothing"


# ---------------------------------------------------------------------------
# Administrator-assigned credentials
# ---------------------------------------------------------------------------

def _admin_token():
    status, body, _ = _login("admin", "admin123")
    assert status == 200, f"admin login failed: {body}"
    return body["access_token"]


def test_admin_created_user_must_change_password():
    """The requirement, end to end: an admin types a password, and the account
    cannot be used until its owner replaces it."""
    username = "rotation_created_probe"
    _delete_user(username)
    token = _admin_token()
    try:
        status, body, _ = _http("POST", "/api/users", {
            "username": username,
            "email": f"{username}@example.test",
            "password": SEEDED_PASSWORD,
            "full_name": "Created Probe",
            "role": "user",
            "can_use_chatbot": False,
        }, headers=_bearer(token))
        assert status == 200, body
        assert body["must_change_password"] is True

        must_change, _ = _read_flags(username)
        assert must_change is True

        # The new user can sign in, and can do nothing else.
        status, login_body, _ = _login(username, SEEDED_PASSWORD, BROWSER)
        assert status == 200, login_body
        assert login_body["rotation_required"] is True
        assert login_body["redirect_url"] == "/change-password"
    finally:
        _delete_user(username)


def test_admin_reset_of_another_account_forces_rotation(settled_user):
    """Resetting someone else's password hands them a credential the admin
    knows, so it must be replaced."""
    from sqlalchemy import text
    from db_connection import db_manager

    async def user_id():
        async with db_manager.get_session() as db:
            row = (await db.execute(
                text("SELECT id FROM users WHERE username = :u"), {"u": settled_user}
            )).first()
            return row[0]

    target_id = run_async(user_id())
    token = _admin_token()

    status, body, _ = _http("POST", f"/api/users/{target_id}/reset-password",
                            {"new_password": SEEDED_PASSWORD}, headers=_bearer(token))
    assert status == 200, body

    must_change, changed_at = _read_flags(settled_user)
    assert must_change is True, "an admin-assigned password must be rotated"
    assert changed_at is not None, "a reset must also end the user's sessions"


def test_admin_reset_of_own_account_does_not_force_rotation():
    """An admin choosing their own password is not being handed anything.
    Forcing a change here would put them in a loop with no way out.

    Seeds its OWN administrator rather than using the shared `admin` account:
    a reset stamps password_changed_at, which ends that account's other
    sessions, and 60-odd other test modules sign in as `admin`.
    """
    username = "rotation_selfadmin_probe"
    _seed_user(username, STRONG_PASSWORD, must_change=False, role="admin")
    try:
        status, body, _ = _login(username, STRONG_PASSWORD)
        assert status == 200, body
        token = body["access_token"]

        status, me, _ = _http("GET", "/api/auth/me", headers=_bearer(token))
        assert status == 200, me

        status, body, _ = _http("POST", f"/api/users/{me['id']}/reset-password",
                                {"new_password": ANOTHER_STRONG_PASSWORD},
                                headers=_bearer(token))
        assert status == 200, body

        must_change, _ = _read_flags(username)
        assert must_change is False, "an admin must not be locked out of their own reset"
    finally:
        _delete_user(username)


# ---------------------------------------------------------------------------
# Source-level contract
# ---------------------------------------------------------------------------

def test_gate_is_enforced_in_the_shared_dependency():
    """Enforcement must sit in the dependency every route already builds on. A
    per-route check would be forgotten by the next route added."""
    src = _read(AUTH_SERVICE_PATH)
    assert "async def get_current_user_allow_pending_rotation" in src
    assert "PASSWORD_ROTATION_REQUIRED" in src
    assert "must_change_password" in src


def test_session_freshness_compares_utc_not_local_time():
    """password_changed_at is a naive datetime.utcnow(). `.timestamp()` would
    read it as LOCAL time while PyJWT encodes `iat` as UTC, so on a container
    with TZ set ahead of UTC every token issued within the offset would survive
    a password change — precisely the sessions the rule exists to end."""
    src = _read(AUTH_SERVICE_PATH)
    assert "calendar.timegm(changed_at.utctimetuple())" in src
    assert "changed_at.timestamp()" not in src


def test_frontend_allowlists_and_skips_the_change_password_page():
    assert "'/change-password'" in _read(SIGNIN_JS_PATH), \
        "signin must be allowed to send the user to /change-password"

    navbar = _read(NAVBAR_JS_PATH)
    assert "/change-password" in navbar, \
        "the navbar bootstrap must skip and redirect for a pending rotation"
    assert "rotation_required" in navbar


def test_change_password_page_never_renders_server_text():
    """Same rule as sign-in: messages are chosen by local code, so a hostile
    response body cannot control what the user is told."""
    src = _read(JS_PATH)
    assert "ERROR_MESSAGES" in src
    assert ".textContent" in src
    stripped = src.replace("submitButton.innerHTML = state.originalButtonHtml", "")
    assert ".innerHTML =" not in stripped, \
        "backend values must never be assigned through innerHTML"
    assert "X-Requested-With" in src, "the CSRF dependency requires this header"
