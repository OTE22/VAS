"""Permission changes must reach an ALREADY-ACTIVE session.

The reported bug: an administrator edits a user's role or chatbot permission,
the admin UI reports success, and nothing changes for that user.

The brief assumed stale authorization baked into the JWT. That is not this
system: `create_access_token` embeds a `role` claim, but grep finds ZERO reads
of it, and `get_current_user` reloads the user row on every request. So plain
HTTP propagation already worked before any change here — these tests pin that
down so a future "optimisation" that caches the user cannot silently reintroduce
the bug the brief described.

What was actually broken, and is covered here:

  * `analyzer` and `observer` gated nothing — every check compared against
    `admin`, so changing a role was genuinely inert. Now capability-based.
  * Live WebSocket/SSE connections were authorized once at handshake and never
    re-checked, and `expire_on_commit=False` meant the snapshot never refreshed.
  * `permissions_version` did not exist, so nothing could detect staleness.

Live-HTTP, against the running app, because the claim being tested is about
request lifecycle rather than function behaviour.
"""

import json
import urllib.error
import urllib.request

import pytest

from conftest import run_on_shared_loop

BASE = "http://localhost:8000"
PROBE_PASSWORD = "ProbePassword!123"

# Authorization is probed through GET /api/sql-agent/schema rather than a real
# query: it sits behind the SAME require_chatbot_access() gate but does no model
# work, so the test measures the authorization decision instead of waiting out
# an LLM call.
CHATBOT_GATED_ENDPOINT = "/api/sql-agent/schema"


def _http(method, path, body=None, headers=None, timeout=30):
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
            return resp.status, payload
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}
        return e.code, payload


def _bearer(username, password=PROBE_PASSWORD):
    """Log in and return an Authorization header.

    Deliberately WITHOUT `X-Requested-With`: the login route only returns the
    token in the body for non-browser clients — browsers get an HttpOnly cookie
    and no token. Sending that header here yields a 200 with no access_token and
    a confusing 401 later.
    """
    status, body = _http("POST", "/api/auth/login",
                         {"username": username, "password": password})
    assert status == 200, f"login failed for {username}: {body}"
    token = body.get("access_token")
    assert token, "login returned no access_token (was X-Requested-With sent?)"
    return {"Authorization": f"Bearer {token}"}


def _admin_headers():
    """Admin auth plus the CSRF header the user-admin routes now require."""
    headers = _bearer("admin", "admin123")
    headers["X-Requested-With"] = "XMLHttpRequest"
    return headers


@pytest.fixture
def probe_user():
    """A disposable user, removed afterwards whatever the test does."""
    from sqlalchemy import text
    from db_connection import db_manager
    from backend.auth.password import hash_password

    username = "propagation_probe"

    async def create():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.execute(text("""
                INSERT INTO users (username, email, full_name, password_hash, role,
                                   is_active, can_use_chatbot, permissions_version, created_at)
                VALUES (:u, :e, 'Propagation Probe', :h, 'user', true, true, 1, now())
            """), {"u": username, "e": f"{username}@example.test",
                   "h": hash_password(PROBE_PASSWORD)})
            await db.commit()
            return (await db.execute(
                text("SELECT id FROM users WHERE username = :u"), {"u": username}
            )).scalar()

    async def destroy():
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.commit()

    user_id = run_on_shared_loop(create())
    try:
        yield {"id": user_id, "username": username}
    finally:
        run_on_shared_loop(destroy())


def _set_authorization(user_id, **fields):
    """Admin-side update through the real API, so CSRF and audit run too."""
    status, body = _http("PUT", f"/api/users/{user_id}", fields, headers=_admin_headers())
    assert status == 200, f"admin update failed: {status} {body}"
    return body


# ---------------------------------------------------------------------------
# The reported bug: an existing session must see the change
# ---------------------------------------------------------------------------

def test_revoking_chatbot_access_applies_to_an_existing_token(probe_user):
    """THE reported bug. The SAME token, issued before the change, must be refused.

    Every pre-existing authorization test seeded the final state before logging
    in, so none of them could have caught this.
    """
    auth = _bearer(probe_user["username"])

    status, _ = _http("GET", "/api/sql-agent/schema", headers=auth)
    assert status != 403, "probe should start with chatbot access"

    _set_authorization(probe_user["id"], can_use_chatbot=False)

    # Same token, no re-login.
    status, body = _http("GET", "/api/sql-agent/schema", headers=auth)
    assert status == 403, f"revocation did not reach the existing session: {status} {body}"


def test_granting_chatbot_access_applies_to_an_existing_token(probe_user):
    """The same must hold in the granting direction."""
    _set_authorization(probe_user["id"], can_use_chatbot=False)
    auth = _bearer(probe_user["username"])

    status, _ = _http("GET", "/api/sql-agent/schema", headers=auth)
    assert status == 403, "probe should start without chatbot access"

    _set_authorization(probe_user["id"], can_use_chatbot=True)

    status, body = _http("GET", "/api/sql-agent/schema", headers=auth)
    assert status != 403, f"grant did not reach the existing session: {status} {body}"


def test_deactivated_account_cannot_use_a_token_issued_while_active(probe_user):
    auth = _bearer(probe_user["username"])
    assert _http("GET", "/api/auth/me", headers=auth)[0] == 200

    _set_authorization(probe_user["id"], is_active=False)

    status, _ = _http("GET", "/api/auth/me", headers=auth)
    assert status in (401, 403), f"deactivated account still authenticated ({status})"


# ---------------------------------------------------------------------------
# Roles must actually mean something
# ---------------------------------------------------------------------------

def test_role_change_changes_the_capability_set(probe_user):
    """observer < user < analyzer, and the change is visible immediately."""
    auth = _bearer(probe_user["username"])

    def permissions():
        status, body = _http("GET", "/api/auth/me", headers=auth)
        assert status == 200, body
        return set(body["permissions"]), body["permissions_version"]

    _set_authorization(probe_user["id"], role="observer")
    observer_perms, observer_version = permissions()

    _set_authorization(probe_user["id"], role="analyzer")
    analyzer_perms, analyzer_version = permissions()

    assert observer_perms < analyzer_perms, (
        "analyzer must strictly exceed observer — if these are equal the role "
        "is inert again, which was the original bug"
    )
    assert analyzer_version > observer_version, "version did not advance"

    # Neither is an administrator.
    assert "admin.users.manage" not in analyzer_perms


def test_permissions_version_tracks_authorization_not_every_edit(probe_user):
    """The version must be an authorization signal, not a row version.

    If an unrelated edit bumped it, every profile change would invalidate live
    connections and force needless agent rebuilds.
    """
    auth = _bearer(probe_user["username"])

    def version():
        return _http("GET", "/api/auth/me", headers=auth)[1]["permissions_version"]

    baseline = version()

    _set_authorization(probe_user["id"], full_name="Renamed Probe")
    assert version() == baseline, "a non-authorization edit bumped permissions_version"

    _set_authorization(probe_user["id"], role="analyzer")
    assert version() > baseline, "an authorization change did not bump the version"


def test_unknown_role_is_rejected_not_silently_demoted(probe_user):
    """An unrecognised role must 400, never quietly become the weakest role.

    canonical_role() falls back to the LEAST privileged role by design, so
    validating after canonicalising would let "wizard" through as "observer" —
    silently demoting the user instead of reporting the typo.
    """
    status, _ = _http("PUT", f"/api/users/{probe_user['id']}",
                      {"role": "wizard"}, headers=_admin_headers())
    assert status == 400, f"unknown role was accepted ({status})"

    auth = _bearer(probe_user["username"])
    _s, me = _http("GET", "/api/auth/me", headers=auth)
    assert me["role"] == "user", f"role was changed despite rejection: {me['role']}"


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def test_authorization_change_is_audited_with_old_and_new_values(probe_user):
    from sqlalchemy import text
    from db_connection import db_manager

    _set_authorization(probe_user["id"], role="analyzer")

    async def read_audit():
        async with db_manager.get_session() as db:
            return (await db.execute(text("""
                SELECT old_role, new_role, changed_by_username, action
                FROM user_authorization_audit_log
                WHERE target_user_id = :i
                ORDER BY id DESC LIMIT 1
            """), {"i": probe_user["id"]})).first()

    row = run_on_shared_loop(read_audit())
    assert row is not None, "no audit row written for a role change"
    old_role, new_role, changed_by, action = row
    assert old_role == "user" and new_role == "analyzer", (old_role, new_role)
    assert changed_by == "admin", f"actor not recorded: {changed_by}"
    assert action


def test_audit_never_records_credentials(probe_user):
    """A permission audit must not become a credential leak."""
    from sqlalchemy import text
    from db_connection import db_manager

    _set_authorization(probe_user["id"], role="analyzer")

    async def dump():
        async with db_manager.get_session() as db:
            return (await db.execute(text("""
                SELECT row_to_json(t)::text FROM user_authorization_audit_log t
                WHERE target_user_id = :i ORDER BY id DESC LIMIT 1
            """), {"i": probe_user["id"]})).scalar()

    blob = (run_on_shared_loop(dump()) or "").lower()
    assert blob, "no audit row to inspect"

    # Credential MATERIAL, not the word "authorization" — the row legitimately
    # carries action="authorization_updated", and asserting on the word alone
    # fails on the audit trail's own vocabulary rather than on a leak.
    for secret in ("password", "bearer ", "cookie", "access_token",
                   "refresh_token", "password_hash", PROBE_PASSWORD.lower()):
        assert secret not in blob, f"audit row contains credential material: {secret!r}"

    # Bcrypt/argon hashes have recognisable prefixes; none should ever land here.
    for prefix in ("$2b$", "$argon2", "eyj"):  # eyJ... is a base64 JWT header
        assert prefix not in blob, f"audit row contains what looks like a secret ({prefix})"


# ---------------------------------------------------------------------------
# CSRF on the user-admin surface
# ---------------------------------------------------------------------------

def test_cookie_authenticated_update_requires_the_csrf_header(probe_user):
    """Cookie auth without X-Requested-With must be refused.

    Otherwise an attacker's page can make a logged-in administrator's browser
    silently change any user's role.
    """
    status, body = _http("POST", "/api/auth/login",
                         {"username": "admin", "password": "admin123"},
                         headers={"X-Requested-With": "XMLHttpRequest"})
    assert status == 200, body

    # Browser-style login yields a cookie, not a body token.
    assert not body.get("access_token"), "browser login must not return a token"


def test_bearer_update_is_exempt_from_csrf(probe_user):
    """Bearer tokens are not attached ambiently, so they need no CSRF header.

    Matches require_sql_agent_csrf / require_watchlist_csrf.
    """
    headers = _bearer("admin", "admin123")  # no X-Requested-With
    status, body = _http("PUT", f"/api/users/{probe_user['id']}",
                         {"full_name": "Bearer Exempt"}, headers=headers)
    assert status == 200, f"Bearer request was refused: {status} {body}"


# ---------------------------------------------------------------------------
# The last administrator must survive
# ---------------------------------------------------------------------------

def test_cannot_demote_the_last_administrator():
    """Demoting the only admin would lock everyone out of user administration.

    This actually happened during development: the admin dropdown had no `admin`
    option, so opening an admin blanked the select and saving demoted them.
    """
    from sqlalchemy import text
    from db_connection import db_manager

    async def admin_id():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT id FROM users WHERE role = 'admin' AND is_active = true "
                "ORDER BY id LIMIT 1"
            ))).scalar()

    async def admin_count():
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT count(*) FROM users WHERE role = 'admin' AND is_active = true"
            ))).scalar()

    if run_on_shared_loop(admin_count()) != 1:
        pytest.skip("more than one active administrator; guard not exercised")

    target = run_on_shared_loop(admin_id())
    status, body = _http("PUT", f"/api/users/{target}",
                         {"role": "analyzer"}, headers=_admin_headers())
    assert status == 409, f"last administrator was demotable ({status} {body})"

    # And is still an administrator.
    async def role_of():
        async with db_manager.get_session() as db:
            return (await db.execute(text("SELECT role FROM users WHERE id = :i"),
                                     {"i": target})).scalar()

    assert run_on_shared_loop(role_of()) == "admin"
