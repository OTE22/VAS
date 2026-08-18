"""The `system` audit principal: restoring it, and keeping it unusable.

    docker exec face_recognition_api python -m pytest tests/test_system_principal.py -v

`identity_audit_log.user_id` is NOT NULL and a foreign key to `users.id`, so
machine-initiated work — vector index rebuild, reconciliation, corruption
recovery — needs a real row to attribute to. Migration a3b4c5d6e7f8 seeds exactly
one such row. Attributing those events to the human `admin` was the rejected
alternative: it writes a false audit record, after which nobody can tell a 3am
automated rebuild from something an administrator actually did.

This database had lost that row, and because the seeding migration is already
applied it could never come back by migrating. Hence a repair endpoint.

The properties worth pinning are the ones that fail silently:

  * restore is idempotent — two calls never produce two rows;
  * restore repairs a TAMPERED row completely, not just the two flags the
    migration forces. Half a repair leaves an account named `system` holding a
    real bcrypt hash and an ordinary role, armed for whenever someone flips
    is_active;
  * the endpoint never returns password_hash;
  * the principal cannot be edited, re-passworded, activated or deleted through
    the ordinary user-administration routes, and `system` cannot be claimed as a
    name by a new account.

HTTP against the live app, like test_webhook_credentials.py. Tests run inside
the container, so BASE is localhost:8000.
"""

import json
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"
RESTORE = "/api/users/system/restore"

SYSTEM_USERNAME = "system"
SYSTEM_EMAIL = "system@localhost.invalid"
UNUSABLE_PASSWORD_HASH = "!"


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


@pytest.fixture(scope="module")
def admin_token():
    # csrf=False is load-bearing: the login handler treats X-Requested-With as
    # "this is a browser" and returns the JWT in an httpOnly cookie with
    # access_token nulled in the body. Sending it yields a None token and every
    # assertion below fails as a meaningless 401.
    status, raw = _http("POST", "/api/auth/login",
                        {"username": "admin", "password": "admin123"},
                        csrf=False)
    assert status == 200, f"admin login failed: {raw[:200]}"
    token = _json(raw).get("access_token")
    assert token, "login returned no bearer token"
    return token


def _run(coro):
    from conftest import run_on_shared_loop
    return run_on_shared_loop(coro)


async def _session():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()
    return db_manager.get_session()


async def _fetch_system_rows():
    from sqlalchemy import text as sa_text
    async with await _session() as db:
        return (await db.execute(sa_text(
            "SELECT id, username, email, role, is_active, can_use_chatbot, "
            "password_hash FROM users WHERE username = :u"),
            {"u": SYSTEM_USERNAME})).fetchall()


def _system_rows():
    return _run(_fetch_system_rows())


def _system_row():
    rows = _system_rows()
    assert len(rows) == 1, f"expected exactly one '{SYSTEM_USERNAME}' row, found {len(rows)}"
    return rows[0]


@pytest.fixture
def restored(admin_token):
    """The principal, present and canonical, whatever state the DB was in."""
    status, raw = _http("POST", RESTORE, token=admin_token)
    assert status == 200, f"restore failed: {raw[:300]}"
    return _system_row()


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

def test_restore_creates_the_principal_when_it_is_missing(admin_token):
    """The row is absent in this database, which is why machine audit writes
    were being skipped. Removing it first proves creation, not just presence."""
    from sqlalchemy import text as sa_text

    async def _drop():
        async with await _session() as db:
            # Only safe because nothing references it yet in this fixture path;
            # if audit rows exist the FK will refuse and the test skips.
            try:
                await db.execute(sa_text("DELETE FROM users WHERE username = :u"),
                                 {"u": SYSTEM_USERNAME})
                await db.commit()
                return True
            except Exception:                                  # noqa: BLE001
                await db.rollback()
                return False

    if not _run(_drop()):
        pytest.skip("audit rows already reference the principal; cannot test creation")

    assert _system_rows() == [], "precondition: the principal should be gone"

    status, raw = _http("POST", RESTORE, token=admin_token)
    assert status == 200, f"restore failed: {raw[:300]}"
    assert _json(raw)["created"] is True, "restore did not report creating the row"

    row = _system_row()
    assert row.role == SYSTEM_USERNAME
    assert row.is_active is False
    assert row.can_use_chatbot is False
    assert row.password_hash == UNUSABLE_PASSWORD_HASH
    assert row.email == SYSTEM_EMAIL


def test_restore_is_idempotent(admin_token, restored):
    """Called twice, it must not produce a second row. ON CONFLICT DO NOTHING
    is what makes this safe to expose as a button an admin can double-click."""
    before_id = restored.id

    status, raw = _http("POST", RESTORE, token=admin_token)
    assert status == 200
    assert _json(raw)["created"] is False, "a second restore claimed to create a row"

    row = _system_row()
    assert row.id == before_id, "the principal was recreated with a new id"


def test_restore_repairs_every_security_property_not_just_the_flags(admin_token, restored):
    """The migration forces only is_active and can_use_chatbot. That is not
    enough for a repair action: an admin could have set role='user' and given
    the account a real bcrypt hash. Restoring the two flags alone would leave a
    row named `system` holding a valid password and an ordinary role — dormant,
    but live the moment anyone flips is_active again.
    """
    from sqlalchemy import text as sa_text
    from backend.auth.password import hash_password

    real_hash = hash_password("NotSupposedToWork!2026")

    async def _tamper():
        async with await _session() as db:
            await db.execute(sa_text(
                "UPDATE users SET role = 'user', is_active = true, "
                "can_use_chatbot = true, password_hash = :h, email = :e "
                "WHERE username = :u"),
                {"h": real_hash, "e": "tampered@example.test", "u": SYSTEM_USERNAME})
            await db.commit()

    _run(_tamper())
    tampered = _system_row()
    assert tampered.is_active is True, "precondition: tampering did not apply"

    status, raw = _http("POST", RESTORE, token=admin_token)
    assert status == 200, f"restore failed: {raw[:300]}"

    row = _system_row()
    assert row.role == SYSTEM_USERNAME, "role was not restored"
    assert row.is_active is False, "is_active was not restored"
    assert row.can_use_chatbot is False, "can_use_chatbot was not restored"
    assert row.password_hash == UNUSABLE_PASSWORD_HASH, (
        "the real bcrypt hash survived the restore — the account still has a "
        "working password")
    assert row.email == SYSTEM_EMAIL, "email was not restored"


def test_the_restore_response_never_contains_a_password_hash(admin_token, restored):
    """Even the unusable '!' sentinel should not be echoed to a client;
    serialising password hashes out of this API must not become normal."""
    status, raw = _http("POST", RESTORE, token=admin_token)
    assert status == 200
    payload = _json(raw)
    assert "password_hash" not in json.dumps(payload), (
        f"the restore response leaked a password hash: {payload}")
    assert payload["user"]["username"] == SYSTEM_USERNAME
    assert payload["user"]["is_active"] is False


def test_restore_requires_admin_and_csrf(admin_token, restored):
    """Same protection as every other mutating user-administration route."""
    status, _ = _http("POST", RESTORE)                       # no credentials
    assert status in (401, 403), f"unauthenticated restore returned {status}"


# ---------------------------------------------------------------------------
# the principal cannot be turned into an account
# ---------------------------------------------------------------------------

def test_the_principal_cannot_log_in(restored):
    """Three independent reasons it must fail: is_active=false, a password hash
    that is not bcrypt, and a role that grants nothing."""
    for password in ("!", "", "system", "admin123"):
        status, _ = _http("POST", "/api/auth/login",
                          {"username": SYSTEM_USERNAME, "password": password},
                          csrf=False)
        assert status != 200, f"logged in as '{SYSTEM_USERNAME}' with {password!r}"


def test_the_principal_cannot_be_deleted(admin_token, restored):
    status, raw = _http("DELETE", f"/api/users/{restored.id}", token=admin_token)
    assert status == 403, f"expected 403, got {status}: {raw[:300]}"
    assert _system_rows(), "the principal was deleted"


def test_the_principal_cannot_be_activated(admin_token, restored):
    status, raw = _http("PUT", f"/api/users/{restored.id}",
                        {"is_active": True}, token=admin_token)
    assert status == 403, f"expected 403, got {status}: {raw[:300]}"
    assert _system_row().is_active is False, "the principal was activated"


def test_the_principal_role_cannot_be_changed(admin_token, restored):
    status, raw = _http("PUT", f"/api/users/{restored.id}",
                        {"role": "user"}, token=admin_token)
    assert status == 403, f"expected 403, got {status}: {raw[:300]}"
    assert _system_row().role == SYSTEM_USERNAME, "the principal's role was changed"


def test_the_principal_cannot_be_given_a_password(admin_token, restored):
    status, raw = _http("POST", f"/api/users/{restored.id}/reset-password",
                        {"new_password": "RealPassword!2026"}, token=admin_token)
    assert status == 403, f"expected 403, got {status}: {raw[:300]}"
    assert _system_row().password_hash == UNUSABLE_PASSWORD_HASH, (
        "the principal was given a usable password")


def test_the_principal_cannot_be_activated_through_unblock(admin_token, restored):
    """`unblock_user()` sets is_active = True unconditionally, so the unblock
    route was a way around the update guard rather than a separate feature.
    Found by checking every route that writes is_active, not just the obvious
    one — the account need not be blocked for the route to accept it."""
    status, raw = _http("POST", f"/api/users/{restored.id}/unblock", token=admin_token)
    assert status == 403, f"expected 403, got {status}: {raw[:300]}"
    assert _system_row().is_active is False, (
        "the principal was activated through the unblock route")


def test_the_guard_message_is_a_string_not_an_object(admin_token, restored):
    """The admin page renders errors with `error.detail || ...`, so a dict
    detail displays as "[object Object]" — which is what _guard_last_administrator
    does. This guard must not repeat it."""
    status, raw = _http("DELETE", f"/api/users/{restored.id}", token=admin_token)
    assert status == 403
    detail = _json(raw).get("detail")
    assert isinstance(detail, str), f"detail should be a plain string, got {type(detail)}"
    assert SYSTEM_USERNAME in detail


# ---------------------------------------------------------------------------
# the name and role are reserved
# ---------------------------------------------------------------------------

def test_ordinary_user_creation_cannot_claim_the_system_username(admin_token, restored):
    """While the principal is missing, an ordinary account called `system`
    would be picked up as the actor for machine-generated audit rows."""
    status, raw = _http("POST", "/api/users", {
        "username": SYSTEM_USERNAME,
        "email": "qa_system_clash@example.test",
        "password": "QaSystem!2026",
        "role": "user",
    }, token=admin_token)
    assert status == 400, f"expected 400, got {status}: {raw[:300]}"
    assert len(_system_rows()) == 1, "a second 'system' row was created"


def test_ordinary_user_creation_cannot_assign_the_system_role(admin_token):
    """`system` is not in the Role enum, so this is already rejected — pinned
    because canonical_role() falls back to the least-privileged role, and a
    future change that dropped is_known_role() would let it through silently."""
    username = "qa_system_role_probe"
    status, raw = _http("POST", "/api/users", {
        "username": username,
        "email": f"{username}@example.test",
        "password": "QaSystem!2026",
        "role": SYSTEM_USERNAME,
    }, token=admin_token)
    assert status == 400, f"expected 400, got {status}: {raw[:300]}"

    async def _cleanup():
        from sqlalchemy import text as sa_text
        async with await _session() as db:
            await db.execute(sa_text("DELETE FROM users WHERE username = :u"),
                             {"u": username})
            await db.commit()

    _run(_cleanup())


# ---------------------------------------------------------------------------
# blast radius
# ---------------------------------------------------------------------------

def test_restore_does_not_touch_any_other_account(admin_token, restored):
    """Every statement in ensure_system_principal is keyed on
    username='system'. This is what makes the endpoint safe in production."""
    from sqlalchemy import text as sa_text

    async def _snapshot():
        async with await _session() as db:
            return (await db.execute(sa_text(
                "SELECT id, username, email, role, is_active, can_use_chatbot, "
                "password_hash, permissions_version FROM users "
                "WHERE username <> :u ORDER BY id"),
                {"u": SYSTEM_USERNAME})).fetchall()

    before = _run(_snapshot())
    status, _ = _http("POST", RESTORE, token=admin_token)
    assert status == 200
    after = _run(_snapshot())

    assert before == after, "restoring the principal modified other user accounts"


# ---------------------------------------------------------------------------
# the admin page's side of the contract
#
# Source assertions, like tests/test_ml_ops_page.py — there is no browser in the
# container. These are not the security boundary (the routes above are); they
# stop the UI from offering actions the backend will refuse, which reads to an
# administrator as a broken page.
# ---------------------------------------------------------------------------

def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_the_admin_page_renders_the_principal_as_protected():
    source = _read("/app/frontend/js/admin-users.js")
    assert "function isSystemAccount(" in source
    assert "Protected" in source, "no protected marker in the row renderer"
    assert "badge-nonlogin" in source, "the principal's status is not rendered as Non-login"


def test_both_table_views_use_one_row_renderer():
    """renderUsersTable and filterBlockedUsers each carried their own copy of
    the row markup. With two copies, a protected row applied to one still
    offers Edit/Delete on `system` in the other."""
    source = _read("/app/frontend/js/admin-users.js")
    assert source.count("function renderUserRow(") == 1
    assert source.count("data-action=\"deleteUser\"") == 1, (
        "the row markup is duplicated again; both views must share renderUserRow")
    assert source.count("map(renderUserRow)") == 2, (
        "both the full table and the blocked filter must render through renderUserRow")


def test_the_edit_modal_refuses_the_principal():
    """The protected row shows no Edit button, but actions are delegated via
    data-action and remain reachable from the console."""
    source = _read("/app/frontend/js/admin-users.js")
    edit = source.split("function editUser(", 1)[1].split("function ", 1)[0]
    assert "isSystemAccount(user)" in edit, "editUser does not refuse the principal"


def test_the_restore_control_exists_and_is_hidden_by_default():
    html = _read("/app/frontend/admin/users.html")
    assert 'id="system-principal-notice"' in html
    assert 'data-action="restoreSystemPrincipal"' in html
    assert "display: none" in html.split('id="system-principal-notice"', 1)[1][:200], (
        "the restore notice must start hidden; it is shown only when the "
        "principal is missing")
    assert "admin-users.js?v=" in html, (
        "admin-users.js has no cache-buster, so browsers keep the old file")


def test_a_normal_user_is_not_treated_as_the_principal(admin_token):
    """is_system_principal matches username OR role; an ordinary account must
    match neither, or the guards would lock admins out of real users."""
    from backend.services.system_principal import is_system_principal

    class _Probe:
        username = "alice"
        role = "user"

    assert is_system_principal(_Probe()) is False
    assert is_system_principal(None) is False
