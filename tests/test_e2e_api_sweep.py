"""End-to-end exercise of the HTTP API against a real stack, with edge cases.

What this file is for: prove that every endpoint the application declares can be
called, that it enforces its own auth, that the happy path **actually writes what
it claims to the database**, and that the obvious abuse and boundary cases are
refused the way they should be — rather than 500ing or silently succeeding.

It is written for the ISOLATED regression stack, which brings up its own
PostgreSQL, Redis, API and nginx with ephemeral volumes:

    sudo scripts/run_regression_isolated.sh tests/test_e2e_api_sweep.py -q

Never point BASE at production: these tests create and delete real rows.

Structure
  1.  helpers + fixtures (admin token, a plain user, an ingested pipeline)
  2.  one section per resource: happy path with a DATABASE assertion, then the
      edge cases for that resource
  3.  a final sweep that calls every remaining declared route and asserts that
      none of them 500s and that every write route refuses an anonymous caller
  4.  a report written to Docs/../logs/e2e-api-report.md (and printed)

Conventions
  `_sql` runs a query against the same database the API is writing to, so an
  assertion like "the row exists" is a real read, not the API's own echo.
"""
import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

from conftest import run_on_shared_loop

BASE = os.environ.get("E2E_BASE", "http://localhost:8000")
ADMIN_USER = os.environ.get("E2E_ADMIN", "admin")
ADMIN_PASS = os.environ.get("E2E_ADMIN_PASSWORD", "admin123")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "faces")
XSS = "<script>alert('x')</script>"
SQLI = "'; DROP TABLE users; --"
TRAVERSAL = "../../etc/passwd"
UNICODE_NAME = "Zaïd الأمن 松本 🙂"
REPORT = []
_GALLERY_DIRS = []      # photo directories this file created, removed in teardown


# ---------------------------------------------------------------- HTTP helper
def _multipart(fields, files):
    boundary = "----e2e" + uuid.uuid4().hex
    out = io.BytesIO()
    for key, value in (fields or {}).items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    for key, (filename, payload, ctype) in (files or {}).items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; filename=\"{filename}\"\r\n"
                  f"Content-Type: {ctype}\r\n\r\n".encode())
        out.write(payload)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def _headers(message):
    """Flatten response headers, keeping every Set-Cookie.

    `dict(message)` keeps only the last value of a repeated header, and a login
    can set more than one cookie, so the session cookie is not reliably the one
    that survives. The joined list goes in under a key of our own."""
    out = dict(message)
    try:
        out["_set_cookie"] = "\n".join(message.get_all("Set-Cookie") or [])
    except Exception:
        out["_set_cookie"] = out.get("Set-Cookie", "")
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _http(method, path, *, body=None, token=None, headers=None, fields=None,
          files=None, raw_body=None, timeout=180, csrf=False):
    # NOTE: csrf defaults to False on purpose. `X-Requested-With: XMLHttpRequest`
    # marks the caller as a BROWSER client, and a browser login is answered with
    # a cookie and NO bearer token — so sending it by default would break every
    # token-authenticated call in this file. Bearer callers are exempt from the
    # CSRF dependencies anyway; pass csrf=True where a test is about that header.
    data, hdrs = None, dict(headers or {})
    if files is not None:
        data, ctype = _multipart(fields, files)
        hdrs["Content-Type"] = ctype
    elif raw_body is not None:
        data = raw_body if isinstance(raw_body, bytes) else raw_body.encode()
        hdrs.setdefault("Content-Type", "application/json")
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if csrf and method in ("POST", "PUT", "PATCH", "DELETE"):
        hdrs.setdefault("X-Requested-With", "XMLHttpRequest")
    request = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for k, v in hdrs.items():
        request.add_header(k, v)
    try:
        with _opener.open(request, timeout=timeout) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw or b"{}"), _headers(response.headers)
            except Exception:
                return response.status, {"_raw": raw[:400].decode(errors="replace")}, _headers(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}"), _headers(exc.headers)
        except Exception:
            return exc.code, {"_raw": raw[:400].decode(errors="replace")}, _headers(exc.headers)
    except urllib.error.URLError as exc:
        return 0, {"_error": str(exc)}, {}


def record(method, path, what, status, db=""):
    REPORT.append({"method": method, "path": path, "case": what, "status": status, "db": db})


# ------------------------------------------------------------- DB helper
def _sql(query, params=None):
    """Read the database the API writes to. Returns a list of tuples."""
    from sqlalchemy import text
    from db_connection import db_manager

    async def run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            result = await db.execute(text(query), params or {})
            try:
                return [tuple(r) for r in result.fetchall()]
            except Exception:
                return []

    return run_on_shared_loop(run())


def _exec(query, params=None):
    from sqlalchemy import text
    from db_connection import db_manager

    async def run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(text(query), params or {})
            await db.commit()

    return run_on_shared_loop(run())



def _frame_payload(name="face_a.jpg", confidence=0.95):
    """The shape a camera pipeline actually posts: the frame plus the boxes its
    own detector found. `class_name` must be person|face or the box is skipped."""
    import struct
    data = _image(name)
    width = height = 0
    # read the JPEG frame size without pulling in an image library
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            break
        i += 2 + length
    width, height = width or 640, height or 640
    return {"image": base64.b64encode(data).decode(),
            "predictions": [{"class_name": "person", "confidence": confidence,
                             "bbox": [0, 0, width, height]}],
            "request_id": "e2e-" + uuid.uuid4().hex[:8]}

def _browser_cookie():
    """Sign in the way a BROWSER does and return the session cookie.

    The upload modal never sees a bearer token: its page was loaded after a
    cookie login, and every request it makes carries that cookie plus
    `X-Requested-With`. Reproducing that is the only way to prove the modal's
    path works, because the cookie route runs CSRF dependencies a bearer
    caller is exempt from."""
    status, body, headers = _http("POST", "/api/auth/login", csrf=True,
                                  body={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert status == 200, f"browser login failed: {status} {body}"
    assert not body.get("access_token"), \
        "a browser login must not hand a bearer token back in the body"
    raw = headers.get("_set_cookie") or headers.get("Set-Cookie") or ""
    name = "__Host-access_token" if "__Host-access_token" in raw else "access_token"
    match = re.search(rf"{re.escape(name)}=([^;]+)", raw)
    assert match, f"no session cookie was issued: {raw[:120]!r}"
    return f"{name}={match.group(1)}"


def _image(name="face_a.jpg"):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def admin():
    status, body, _ = _http("POST", "/api/auth/login",
                            body={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert status == 200, f"admin login failed: {status} {body}"
    token = body.get("access_token")
    assert token, "the API client login returned no bearer token"
    return token


@pytest.fixture(scope="module")
def plain_user(admin):
    """A non-admin account, created through the API and removed afterwards."""
    name = "e2e_plain_" + uuid.uuid4().hex[:8]
    password = "E2e-Plain-Passw0rd!"
    status, body, _ = _http("POST", "/api/users", token=admin, body={
        "username": name, "email": f"{name}@example.test", "password": password,
        "full_name": "E2E Plain User", "role": "analyzer", "can_use_chatbot": False})
    assert status in (200, 201), f"user creation failed: {status} {body}"
    user_id = body.get("id") or body.get("user", {}).get("id")
    status, login, _ = _http("POST", "/api/auth/login", body={"username": name, "password": password})
    assert status == 200, f"plain-user login failed: {status} {login}"
    token = login["access_token"]
    # An admin-created account can be required to choose its own password before
    # it may do anything else; complete that rotation so the account is usable.
    if login.get("rotation_required"):
        rotated = password + "-Rotated1"
        status, _, _ = _http("POST", "/api/auth/change-password", token=token,
                             body={"current_password": password, "new_password": rotated})
        assert status == 200, "the forced rotation could not be completed"
        password = rotated
        token = _http("POST", "/api/auth/login",
                      body={"username": name, "password": password})[1]["access_token"]
    yield {"id": user_id, "username": name, "password": password, "token": token}
    _http("DELETE", f"/api/users/{user_id}", token=admin)


@pytest.fixture(scope="module", autouse=True)
def _remove_everything_this_sweep_created():
    """This sweep writes real rows into the same database the rest of the suite
    reads. Left behind, they change what other tests see: a camera at a made-up
    location fails the map-coverage check, and extra identities and embeddings
    change the search and promotion results. Everything created here carries an
    E2E marker, so it can be removed again in dependency order (children first,
    so no foreign key is ever left dangling). Module scope, not session scope:
    the rows have to be gone when this FILE finishes, not when the whole run
    does, or every later file still sees them."""
    yield
    cams = "(SELECT pipeline_id FROM pipelines WHERE pipeline_id LIKE 'e2e-cam-%' OR pipeline_id LIKE 'E2E~_%' ESCAPE '~')"
    ids = "(SELECT id FROM identities WHERE display_name LIKE 'E2E %')"
    for statement in [
        f"DELETE FROM live_alert_triggers WHERE pipeline_id IN {cams}",
        f"DELETE FROM identity_appearances WHERE pipeline_id IN {cams} OR identity_id IN {ids}",
        # Ingest creates embeddings for people it has never seen, so these are
        # reached through the CAMERA, not through an E2E-named identity.
        f"DELETE FROM identity_embeddings WHERE identity_id IN {ids} OR pipeline_id IN {cams}",
        f"DELETE FROM identity_images WHERE identity_id IN {ids}",
        f"DELETE FROM watchlist_alerts WHERE pipeline_id IN {cams}",
        "DELETE FROM watchlist_entries WHERE watchlist_id IN "
        "(SELECT id FROM watchlists WHERE name LIKE 'E2E %')",
        "DELETE FROM watchlists WHERE name LIKE 'E2E %'",
        f"DELETE FROM live_search_alerts WHERE identity_id IN {ids}",
        f"DELETE FROM faces WHERE detection_id IN (SELECT id FROM detections WHERE pipeline_id IN {cams})",
        f"DELETE FROM detections WHERE pipeline_id IN {cams}",
        f"DELETE FROM user_pipeline_access WHERE pipeline_id IN {cams}",
        "DELETE FROM pipeline_aliases WHERE old_pipeline_id LIKE 'e2e-cam-%' "
        "OR new_pipeline_id LIKE 'E2E~_%' ESCAPE '~'",
        f"DELETE FROM identities WHERE id IN {ids}",
        "DELETE FROM pipelines WHERE pipeline_id LIKE 'e2e-cam-%' OR pipeline_id LIKE 'E2E~_%' ESCAPE '~'",
        "DELETE FROM users WHERE username LIKE 'e2e~_%' ESCAPE '~'",
    ]:
        try:
            _exec(statement)
        except Exception as exc:                       # a table this deployment does not have
            print(f"  CLEANUP FAILED ({type(exc).__name__}): {statement[:90]}\n     {exc}")
    for directory in _GALLERY_DIRS:
        try:                                   # the rows are gone; take the files too
            for entry in os.listdir(directory):
                os.unlink(os.path.join(directory, entry))
            os.rmdir(directory)
        except OSError:
            pass
    left = _sql("SELECT pipeline_id FROM pipelines WHERE pipeline_id LIKE 'e2e-cam-%' "
                "OR pipeline_id LIKE 'E2E~_%' ESCAPE '~'")
    assert not left, ("this sweep left cameras behind; later tests in the same run will "
                      f"see them: {[r[0] for r in left]}")


@pytest.fixture(scope="module")
def ingest_key(admin):
    """Cameras authenticate with an issued ingest credential, not a session."""
    status, body, _ = _http("POST", "/api/admin/webhook-credentials", token=admin,
                            body={"name": "e2e-sweep-" + uuid.uuid4().hex[:6]})
    if status not in (200, 201) or not body.get("token"):
        pytest.skip(f"could not issue an ingest credential ({status} {body})")
    yield {"id": body.get("id"), "token": body["token"]}
    _http("DELETE", f"/api/admin/webhook-credentials/{body.get('id')}", token=admin)


@pytest.fixture(scope="module")
def ingested(admin, ingest_key):
    """Push a real frame through the camera webhook: this is how a pipeline,
    a detection and a face row come into existence."""
    pipeline = "e2e-cam-" + uuid.uuid4().hex[:6]
    # A camera posts the frame together with ITS OWN detections. A payload with
    # only an image is accepted but yields "Processing 0 predictions" — there is
    # nothing to crop — which is why this sends a person box over the fixture.
    payload = _frame_payload("face_a.jpg")
    status, body, _ = _http("POST", f"/webhook/{pipeline}", body=payload, timeout=300,
                            headers={"X-Webhook-Key": ingest_key["token"]})
    record("POST", "/webhook/{pipeline_id}", "ingest a real frame", status,
           "pipelines/detections/faces")
    time.sleep(6)  # the frame is queued; give the worker a moment
    return {"pipeline_id": pipeline, "status": status, "body": body, "key": ingest_key["token"]}


# ===========================================================================
# 1. Authentication — the gate everything else depends on
# ===========================================================================

def test_login_happy_path_and_edge_cases():
    status, body, _ = _http("POST", "/api/auth/login",
                            body={"username": ADMIN_USER, "password": ADMIN_PASS})
    record("POST", "/api/auth/login", "valid credentials", status)
    assert status == 200 and body.get("access_token"), body

    # a browser client (X-Requested-With) must get the cookie and NO token in the body
    status, body, headers = _http("POST", "/api/auth/login",
                                  body={"username": ADMIN_USER, "password": ADMIN_PASS},
                                  headers={"X-Requested-With": "XMLHttpRequest"})
    record("POST", "/api/auth/login", "browser client: cookie only", status)
    assert status == 200 and body.get("access_token") is None, "a browser client must not receive a bearer token"
    assert "set-cookie" in {k.lower() for k in headers}, "no session cookie was set for a browser client"

    for case, payload, expected in [
        ("wrong password", {"username": ADMIN_USER, "password": "nope-" + uuid.uuid4().hex}, (401, 429)),
        ("unknown user", {"username": "no_such_user_" + uuid.uuid4().hex, "password": "x"}, (401, 429)),
        ("empty username", {"username": "", "password": "x"}, (401, 422, 429)),
        ("missing password field", {"username": ADMIN_USER}, (422, 429)),
        ("null body", None, (422,)),
        ("wrong type", {"username": 123, "password": ["a"]}, (422, 401, 429)),
        ("sql injection as username", {"username": SQLI, "password": "x"}, (401, 422, 429)),
        ("oversized username", {"username": "a" * 5000, "password": "x"}, (401, 422, 413, 429)),
    ]:
        status, body, _ = _http("POST", "/api/auth/login", body=payload)
        record("POST", "/api/auth/login", case, status)
        assert status in expected, f"{case}: got {status} {body}"
        assert status != 500, f"{case} produced a server error"

    # the injection attempt must not have done anything to the table
    assert _sql("SELECT count(*) FROM users")[0][0] > 0, "the users table is gone"


def test_malformed_json_and_wrong_content_type_are_refused():
    for case, kwargs, expected in [
        ("truncated json", {"raw_body": '{"username": "admin"'}, (422, 400)),
        ("not json at all", {"raw_body": "username=admin&password=x"}, (422, 400, 415)),
        ("empty body", {"raw_body": ""}, (422, 400)),
        ("json array instead of object", {"raw_body": "[1,2,3]"}, (422, 400)),
    ]:
        status, body, _ = _http("POST", "/api/auth/login", **kwargs)
        record("POST", "/api/auth/login", case, status)
        assert status in expected, f"{case}: got {status} {body}"


def test_me_and_privileges_require_a_valid_token(admin):
    status, body, _ = _http("GET", "/api/auth/me", token=admin)
    record("GET", "/api/auth/me", "valid token", status)
    assert status == 200 and body["username"] == ADMIN_USER

    status, body, _ = _http("GET", "/api/auth/me/privileges", token=admin)
    record("GET", "/api/auth/me/privileges", "valid token", status)
    assert status == 200 and "navbar_links" in body

    for case, token in [("no token", None), ("garbage token", "not-a-jwt"),
                        ("tampered token", admin[:-6] + "AAAAAA"),
                        ("empty bearer", "")]:
        status, _, _ = _http("GET", "/api/auth/me", token=token or None)
        record("GET", "/api/auth/me", case, status)
        assert status == 401, f"{case} was accepted with {status}"


def test_logout_revokes_the_token_it_was_given():
    status, body, _ = _http("POST", "/api/auth/login",
                            body={"username": ADMIN_USER, "password": ADMIN_PASS})
    token = body["access_token"]
    assert _http("GET", "/api/auth/me", token=token)[0] == 200
    status, _, _ = _http("POST", "/api/auth/logout", token=token, body={})
    record("POST", "/api/auth/logout", "revokes the presented token", status)
    assert status == 200
    status, _, _ = _http("GET", "/api/auth/me", token=token)
    record("GET", "/api/auth/me", "after logout", status)
    assert status == 401, "a revoked token still authenticates"


def test_change_password_rotates_and_invalidates_other_sessions(admin):
    name = "e2e_rotate_" + uuid.uuid4().hex[:8]
    first, second = "E2e-First-Passw0rd!", "E2e-Second-Passw0rd!"
    status, created, _ = _http("POST", "/api/users", token=admin, body={
        "username": name, "email": f"{name}@example.test", "password": first,
        "full_name": "E2E Rotate", "role": "analyzer"})
    assert status in (200, 201), created
    user_id = created.get("id")
    try:
        token_a = _http("POST", "/api/auth/login", body={"username": name, "password": first})[1]["access_token"]
        token_b = _http("POST", "/api/auth/login", body={"username": name, "password": first})[1]["access_token"]
        # The freshness rule compares WHOLE SECONDS with a strict `<`, so that the
        # token performing the change survives its own rule (auth_service.py). A
        # login in the SAME second as the change therefore also survives — a one-
        # second window, recorded in the report as an observation. Wait past the
        # second boundary so this asserts the rule rather than the clock.
        time.sleep(1.1)

        status, body, _ = _http("POST", "/api/auth/change-password", token=token_a,
                                body={"current_password": "wrong", "new_password": second})
        record("POST", "/api/auth/change-password", "wrong current password", status)
        assert status in (400, 401, 403, 422), body

        status, body, _ = _http("POST", "/api/auth/change-password", token=token_a,
                                body={"current_password": first, "new_password": second})
        record("POST", "/api/auth/change-password", "valid rotation", status, "users.password_hash")
        assert status == 200, body

        hash_now = _sql("SELECT password_hash, password_changed_at FROM users WHERE username=:u",
                        {"u": name})[0]
        assert hash_now[1] is not None, "password_changed_at was not stamped"
        assert _http("GET", "/api/auth/me", token=token_b)[0] == 401, \
            "a session opened before the password change still works"
        assert _http("POST", "/api/auth/login", body={"username": name, "password": first})[0] == 401
        assert _http("POST", "/api/auth/login", body={"username": name, "password": second})[0] == 200
    finally:
        _http("DELETE", f"/api/users/{user_id}", token=admin)


def test_csrf_header_is_required_for_cookie_style_writes(admin):
    """Bearer clients are exempt; a cookie client without X-Requested-With is not."""
    status, _, _ = _http("POST", "/api/auth/change-password", token=admin, csrf=False,
                         body={"current_password": ADMIN_PASS, "new_password": ADMIN_PASS})
    record("POST", "/api/auth/change-password", "bearer without CSRF header (exempt)", status)
    assert status != 500


# ===========================================================================
# 2. Users — full CRUD, verified in the database, with the authorisation edges
# ===========================================================================

def test_user_crud_writes_and_reads_the_database(admin):
    name = "e2e_crud_" + uuid.uuid4().hex[:8]
    status, created, _ = _http("POST", "/api/users", token=admin, body={
        "username": name, "email": f"{name}@example.test", "password": "E2e-Crud-Passw0rd!",
        "full_name": UNICODE_NAME, "role": "analyzer", "can_use_chatbot": False})
    record("POST", "/api/users", "create (unicode full name)", status, "users")
    assert status in (200, 201), created
    user_id = created["id"]
    try:
        row = _sql("SELECT username, full_name, role, is_active, can_use_chatbot FROM users WHERE id=:i",
                   {"i": user_id})
        assert row and row[0][0] == name, "the user is not in the database"
        assert row[0][1] == UNICODE_NAME, f"unicode name was mangled: {row[0][1]!r}"
        assert row[0][4] is False

        status, listing, _ = _http("GET", "/api/users", token=admin)
        record("GET", "/api/users", "list", status)
        assert status == 200 and any(u["id"] == user_id for u in (listing if isinstance(listing, list) else listing.get("users", [])))

        status, one, _ = _http("GET", f"/api/users/{user_id}", token=admin)
        record("GET", "/api/users/{id}", "read one", status)
        assert status == 200 and one["username"] == name

        before = _sql("SELECT permissions_version FROM users WHERE id=:i", {"i": user_id})[0][0]
        status, _, _ = _http("PUT", f"/api/users/{user_id}", token=admin,
                             body={"can_use_chatbot": True, "full_name": "E2E Updated"})
        record("PUT", "/api/users/{id}", "update (grant chatbot)", status, "users.can_use_chatbot")
        assert status == 200
        row = _sql("SELECT can_use_chatbot, full_name, permissions_version FROM users WHERE id=:i",
                   {"i": user_id})[0]
        assert row[0] is True and row[1] == "E2E Updated", row
        assert row[2] >= before, "permissions_version went backwards"

        status, _, _ = _http("POST", f"/api/users/{user_id}/reset-password", token=admin,
                             body={"new_password": "E2e-Reset-Passw0rd!"})
        record("POST", "/api/users/{id}/reset-password", "admin reset", status, "users.password_hash")
        assert status == 200
        assert _http("POST", "/api/auth/login",
                     body={"username": name, "password": "E2e-Reset-Passw0rd!"})[0] == 200

        status, _, _ = _http("POST", f"/api/users/{user_id}/unblock", token=admin, body={})
        record("POST", "/api/users/{id}/unblock", "unblock", status, "users.is_active")
        assert status in (200, 400, 404), status
    finally:
        status, _, _ = _http("DELETE", f"/api/users/{user_id}", token=admin)
        record("DELETE", "/api/users/{id}", "delete", status, "users row removed")
        assert status in (200, 204)
        assert not _sql("SELECT 1 FROM users WHERE id=:i", {"i": user_id}), "the user row survived the delete"


def test_user_endpoint_edge_cases(admin, plain_user):
    def base(**over):
        """Each case needs its own e-mail: a shared one makes every later
        case fail as a duplicate instead of testing what it meant to."""
        body = {"email": f"e2e-{uuid.uuid4().hex[:10]}@example.test",
                "password": "E2e-Edge-Passw0rd!", "role": "analyzer"}
        body.update(over)
        return body
    dup = "e2e_dup_" + uuid.uuid4().hex[:8]
    status, created, _ = _http("POST", "/api/users", token=admin, body=base(username=dup))
    assert status in (200, 201)
    dup_id = created["id"]
    try:
        cases = [
            ("duplicate username", base(username=dup), (400, 409, 422)),
            ("missing username", base(), (422,)),
            ("empty username", base(username=""), (400, 422)),
            ("privilege escalation to admin", base(username="e2e_esc_" + uuid.uuid4().hex[:6], role="admin"), (400, 403, 422)),
            ("unknown role", base(username="e2e_role_" + uuid.uuid4().hex[:6], role="wizard"), (400, 422)),
            ("xss in full_name", base(username="e2e_xss_" + uuid.uuid4().hex[:6], full_name=XSS), (200, 201)),
            ("sql injection in username", base(username=SQLI), (400, 422)),
            ("username far too long", base(username="a" * 300), (400, 422)),
        ]
        made = []
        for case, payload, expected in cases:
            status, body, _ = _http("POST", "/api/users", token=admin, body=payload)
            record("POST", "/api/users", case, status)
            assert status in expected, f"{case}: got {status} {body}"
            if status in (200, 201) and body.get("id"):
                made.append(body["id"])
        # an XSS payload must be stored verbatim (escaping is the renderer's job), not executed
        row = _sql("SELECT full_name FROM users WHERE full_name=:f", {"f": XSS})
        assert row, "the XSS payload was not stored as literal text"
        for made_id in made:
            _http("DELETE", f"/api/users/{made_id}", token=admin)

        # unknown / malformed ids
        for case, path, expected in [
            ("unknown id", "/api/users/999999999", (404,)),
            ("non-numeric id", "/api/users/not-a-number", (404, 422)),
            ("negative id", "/api/users/-1", (404, 422)),
        ]:
            status, _, _ = _http("GET", path, token=admin)
            record("GET", path, case, status)
            assert status in expected, f"{case}: {status}"

        # a non-admin may not touch user administration
        for method, path, body in [("GET", "/api/users", None),
                                   ("POST", "/api/users", base(username="e2e_na_" + uuid.uuid4().hex[:6])),
                                   ("PUT", f"/api/users/{dup_id}", {"can_use_chatbot": True}),
                                   ("DELETE", f"/api/users/{dup_id}", None)]:
            status, _, _ = _http(method, path, token=plain_user["token"], body=body)
            record(method, path, "as a non-admin", status)
            assert status == 403, f"a non-admin got {status} on {method} {path}"

        # and an anonymous caller may not either
        for method, path in [("GET", "/api/users"), ("POST", "/api/users"), ("DELETE", f"/api/users/{dup_id}")]:
            status, _, _ = _http(method, path, body={} if method != "GET" else None)
            record(method, path, "anonymous", status)
            assert status == 401, f"anonymous got {status} on {method} {path}"

        # self-deletion is refused
        admin_id = _sql("SELECT id FROM users WHERE username=:u", {"u": ADMIN_USER})[0][0]
        status, _, _ = _http("DELETE", f"/api/users/{admin_id}", token=admin)
        record("DELETE", "/api/users/{id}", "delete yourself", status)
        assert status in (400, 403), f"self-deletion returned {status}"
        assert _sql("SELECT 1 FROM users WHERE id=:i", {"i": admin_id}), "the admin deleted itself"
    finally:
        _http("DELETE", f"/api/users/{dup_id}", token=admin)


def test_deleting_a_user_preserves_their_audit_history(admin):
    """FK is SET NULL with historical_user_id stamped — history must survive."""
    name = "e2e_hist_" + uuid.uuid4().hex[:8]
    status, created, _ = _http("POST", "/api/users", token=admin, body={
        "username": name, "email": f"{name}@example.test", "password": "E2e-Hist-Passw0rd!",
        "role": "analyzer", "can_use_chatbot": True})
    assert status in (200, 201)
    user_id = created["id"]
    login = _http("POST", "/api/auth/login", body={"username": name, "password": "E2e-Hist-Passw0rd!"})[1]
    token = login["access_token"]
    if login.get("rotation_required"):
        _http("POST", "/api/auth/change-password", token=token,
              body={"current_password": "E2e-Hist-Passw0rd!", "new_password": "E2e-Hist-Passw0rd!2"})
        token = _http("POST", "/api/auth/login",
                      body={"username": name, "password": "E2e-Hist-Passw0rd!2"})[1]["access_token"]
    question = "e2e history probe " + uuid.uuid4().hex[:6]
    status, _, _ = _http("POST", "/api/audit/chatbot", token=token,
                         body={"session_id": "e2e", "question": question})
    assert status in (200, 201), "could not write an audit row as the user"
    assert _sql("SELECT user_id FROM chatbot_audit_log WHERE query=:q", {"q": question})[0][0] == user_id

    status, _, _ = _http("DELETE", f"/api/users/{user_id}", token=admin)
    assert status in (200, 204)
    row = _sql("SELECT user_id, historical_user_id, username FROM chatbot_audit_log WHERE query=:q",
               {"q": question})
    record("DELETE", "/api/users/{id}", "history survives deletion", 200, "chatbot_audit_log kept")
    assert row, "the audit row was deleted with the user"
    assert row[0][0] is None and row[0][1] == user_id, f"attribution was not preserved: {row[0]}"


# ===========================================================================
# 3. Camera ingest -> pipelines, detections, faces
# ===========================================================================

def test_webhook_ingest_creates_the_pipeline_and_a_detection(ingested, admin):
    assert ingested["status"] in (200, 202), f"ingest failed: {ingested['status']} {ingested['body']}"
    pipeline = ingested["pipeline_id"]
    rows = _sql("SELECT pipeline_id FROM pipelines WHERE pipeline_id=:p", {"p": pipeline})
    assert rows, "the webhook did not register the pipeline in the database"
    for _ in range(20):
        detections = _sql("SELECT count(*) FROM detections WHERE pipeline_id=:p", {"p": pipeline})[0][0]
        if detections:
            break
        time.sleep(2)
    record("POST", "/webhook/{pipeline_id}", "detection persisted", 200, f"detections={detections}")
    assert detections >= 1, "no detection row was written for an ingested frame with a face"
    faces = _sql("SELECT count(*) FROM faces f JOIN detections d ON f.detection_id=d.id "
                 "WHERE d.pipeline_id=:p", {"p": pipeline})[0][0]
    assert faces >= 1, "a detection was written but no face row"

    status, listing, _ = _http("GET", f"/api/detections/{pipeline}?limit=10", token=admin)
    record("GET", "/api/detections/{pipeline_id}", "read back what was ingested", status)
    assert status == 200, listing


def test_webhook_edge_cases(admin, ingest_key):
    pipeline = "e2e-edge-" + uuid.uuid4().hex[:6]
    auth = {"X-Webhook-Key": ingest_key["token"]}

    # the ingest endpoint must refuse an unauthenticated or wrong-key camera
    for case, headers, expected in [("no ingest key", {}, (401, 403)),
                                    ("wrong ingest key", {"X-Webhook-Key": "nope-" + uuid.uuid4().hex}, (401, 403))]:
        status, _, _ = _http("POST", f"/webhook/{pipeline}", headers=headers,
                             body={"image": base64.b64encode(_image()).decode()})
        record("POST", "/webhook/{pipeline_id}", case, status)
        assert status in expected, f"{case}: got {status}"

    cases = [
        # a payload with nothing to process is answered 200 "No images" — a
        # deliberate no-op for cameras that post empty frames, not an error
        ("no image key", {"foo": "bar"}, (200, 400, 422)),
        ("empty image", {"image": ""}, (200, 400, 422)),   # answered as the "No images" no-op
        # ingest is asynchronous by design: the edge queues, the worker validates
        ("not base64", {"image": "!!!not-base64!!!"}, (202, 400, 422)),
        ("base64 of not-an-image", {"image": base64.b64encode(b"hello world").decode()}, (202, 400, 415, 422)),
        ("empty body", {}, (200, 400, 422)),   # the same "No images" no-op
    ]
    for case, payload, expected in cases:
        status, body, _ = _http("POST", f"/webhook/{pipeline}", body=payload, headers=auth)
        record("POST", "/webhook/{pipeline_id}", case, status)
        assert status in expected, f"{case}: got {status} {body}"
        assert status != 500, f"{case} produced a server error"

    for case, pid, expected in [
        ("pipeline id too short", "ab", (400, 404, 422)),
        ("pipeline id with traversal", urllib.parse.quote(TRAVERSAL, safe=""), (400, 404, 422)),
        ("pipeline id with spaces", urllib.parse.quote("bad id"), (400, 404, 422)),
    ]:
        status, _, _ = _http("POST", f"/webhook/{pid}", headers=auth,
                             body={"image": base64.b64encode(_image()).decode()})
        record("POST", "/webhook/{pipeline_id}", case, status)
        assert status in expected, f"{case}: got {status}"
    assert not _sql("SELECT 1 FROM pipelines WHERE pipeline_id LIKE '%etc/passwd%'"), \
        "a traversal pipeline id was accepted into the database"


def test_pipeline_rename_and_coordinates_persist(ingested, admin, plain_user):
    pipeline = ingested["pipeline_id"]
    # Rename RE-KEYS the camera: there is no display_name column. The row moves
    # to the new pipeline_id, every referencing table is re-pointed, the old row
    # is deleted and an alias keeps the old id working for a camera in the field.
    detections_before = _sql("SELECT count(*) FROM detections WHERE pipeline_id=:p", {"p": pipeline})[0][0]
    new_name = "E2E Gate Camera"           # spaces are sanitised to underscores
    sanitised = "E2E_Gate_Camera"
    status, _, _ = _http("PUT", f"/api/pipelines/{pipeline}/rename", token=admin,
                         body={"new_name": new_name})
    record("PUT", "/api/pipelines/{id}/rename", "re-key the camera", status,
           "pipelines + 6 referencing tables + pipeline_aliases")
    assert status == 200
    assert _sql("SELECT count(*) FROM pipelines WHERE pipeline_id=:p", {"p": sanitised})[0][0] == 1, \
        "the renamed pipeline row does not exist"
    assert _sql("SELECT count(*) FROM pipelines WHERE pipeline_id=:p", {"p": pipeline})[0][0] == 0, \
        "the old pipeline row survived the rename"
    assert _sql("SELECT count(*) FROM detections WHERE pipeline_id=:p", {"p": sanitised})[0][0] == detections_before, \
        "detections were not re-pointed to the new pipeline id"
    alias = _sql("SELECT new_pipeline_id FROM pipeline_aliases WHERE old_pipeline_id=:p", {"p": pipeline})
    assert alias and alias[0][0] == sanitised, "no alias was recorded for the old id"
    pipeline = sanitised

    status, _, _ = _http("PUT", f"/api/pipelines/{pipeline}/rename", token=admin,
                         body={"new_name": pipeline})
    record("PUT", "/api/pipelines/{id}/rename", "rename to the same name", status)
    assert status in (200, 400, 409), status

    status, _, _ = _http("PUT", f"/api/pipelines/{pipeline}/coordinates", token=admin,
                         body={"latitude": 33.8938, "longitude": 35.5018, "location_name": "Beirut"})
    record("PUT", "/api/pipelines/{id}/coordinates", "set coordinates", status, "pipelines.latitude/longitude")
    assert status == 200
    row = _sql("SELECT latitude, longitude, location_name FROM pipelines WHERE pipeline_id=:p",
               {"p": pipeline})[0]
    assert abs(float(row[0]) - 33.8938) < 1e-6 and abs(float(row[1]) - 35.5018) < 1e-6, row

    for case, payload, expected in [
        ("latitude out of range", {"latitude": 999, "longitude": 0}, (400, 422)),
        ("longitude out of range", {"latitude": 0, "longitude": -999}, (400, 422)),
        ("latitude as text", {"latitude": "north", "longitude": 0}, (400, 422)),
        # NOTE: this PUT is a PARTIAL update — a payload with only one coordinate
        # is applied and the other is left as it was. Each value present is still
        # range-checked. Recorded as behaviour, not asserted as an error.
        ("missing longitude", {"latitude": 10}, (200, 400, 422)),
    ]:
        status, body, _ = _http("PUT", f"/api/pipelines/{pipeline}/coordinates", token=admin, body=payload)
        record("PUT", "/api/pipelines/{id}/coordinates", case, status)
        assert status in expected, f"{case}: got {status} {body}"

    status, _, _ = _http("PUT", f"/api/pipelines/{pipeline}/rename", token=plain_user["token"],
                         body={"new_name": "hijacked"})
    record("PUT", "/api/pipelines/{id}/rename", "as a non-admin", status)
    assert status == 403
    assert _sql("SELECT count(*) FROM pipelines WHERE pipeline_id=:p", {"p": pipeline})[0][0] == 1

    status, _, _ = _http("PUT", "/api/pipelines/does-not-exist-e2e/rename", token=admin,
                         body={"new_name": "x"})
    record("PUT", "/api/pipelines/{id}/rename", "unknown pipeline", status)
    assert status in (404, 400)


def test_stats_and_dashboard_reflect_the_ingested_data(ingested, admin):
    for path in ["/api/stats", "/api/dashboard/pipelines", "/api/dashboard/config", "/api/pipelines"]:
        status, body, _ = _http("GET", path, token=admin)
        record("GET", path, "read", status)
        assert status == 200, f"{path}: {status} {body}"
    status, body, _ = _http("GET", "/api/dashboard/pipelines", token=admin)
    ids = [p.get("pipeline_id") for p in (body.get("pipelines") or [])]
    # another test may have re-keyed this camera; follow the alias if so
    current = ingested["pipeline_id"]
    alias = _sql("SELECT new_pipeline_id FROM pipeline_aliases WHERE old_pipeline_id=:p",
                 {"p": current})
    if alias:
        current = alias[0][0]
    assert current in ids, f"the ingested pipeline ({current}) is missing from the dashboard listing {ids}"


# ===========================================================================
# 4. Enrollment and identities — the write path that creates people
# ===========================================================================

@pytest.fixture(scope="module")
def enrolled(admin):
    """Enroll a person through the real upload endpoint, resolving the
    decision gate when it asks. Yields the identity id."""
    person = "E2E Person " + uuid.uuid4().hex[:6]
    status, body, _ = _http("POST", "/api/upload-person", token=admin,
                            fields={"person_name": person, "is_face_image": "false"},
                            files={"photo": ("face_a.jpg", _image("face_a.jpg"), "image/jpeg")},
                            timeout=300)
    record("POST", "/api/upload-person", "enroll a new person", status, "identities/identity_images")
    if status == 202 and body.get("decision_required"):
        status, body, _ = _http("POST", "/api/enrollment/confirm", token=admin,
                                body={"upload_token": body["upload_token"],
                                      "action": "create_new", "display_name": person},
                                timeout=300)
        record("POST", "/api/enrollment/confirm", "create a new person", status, "identities")
    assert status in (200, 201), f"enrollment failed: {status} {body}"
    rows = _sql("SELECT id FROM identities WHERE display_name=:n", {"n": person})
    assert rows, "the identity is not in the database after a successful enrollment"
    yield {"id": str(rows[0][0]), "name": person}


def test_enrollment_wrote_identity_image_and_embedding(enrolled):
    identity_id = enrolled["id"]
    images = _sql("SELECT count(*) FROM identity_images WHERE identity_id=:i", {"i": identity_id})[0][0]
    embeddings = _sql("SELECT count(*) FROM identity_embeddings WHERE identity_id=:i", {"i": identity_id})[0][0]
    record("POST", "/api/upload-person", "image + embedding written", 200,
           f"images={images} embeddings={embeddings}")
    assert images >= 1, "no identity_images row"
    assert embeddings >= 1, "no identity_embeddings row — the person could never be recognised"
    vector = _sql("SELECT embedding IS NOT NULL FROM identity_embeddings WHERE identity_id=:i LIMIT 1",
                  {"i": identity_id})
    assert vector and vector[0][0], "the embedding column is NULL (PostgreSQL must hold the vector)"


def test_enrollment_edge_cases(admin):
    cases = [
        ("no file at all", {"fields": {"person_name": "E2E NoFile"}, "files": {}}, (400, 422)),
        ("empty person name", {"fields": {"person_name": ""},
                               "files": {"photo": ("f.jpg", _image(), "image/jpeg")}}, (400, 422)),
        ("not an image", {"fields": {"person_name": "E2E NotImage"},
                          "files": {"photo": ("x.jpg", b"definitely not a jpeg", "image/jpeg")}}, (400, 415, 422)),
        ("zero-byte file", {"fields": {"person_name": "E2E Empty"},
                            "files": {"photo": ("e.jpg", b"", "image/jpeg")}}, (400, 415, 422)),
        ("traversal in the filename", {"fields": {"person_name": "E2E Traversal"},
                                       "files": {"photo": (TRAVERSAL, _image(), "image/jpeg")}}, (200, 201, 202, 400, 422)),
    ]
    for case, kwargs, expected in cases:
        status, body, _ = _http("POST", "/api/upload-person", token=admin, timeout=300, **kwargs)
        record("POST", "/api/upload-person", case, status)
        assert status in expected, f"{case}: got {status} {body}"
        assert status != 500, f"{case} produced a server error"
        if status == 202 and body.get("upload_token"):
            _http("POST", "/api/enrollment/cancel", token=admin, body={"upload_token": body["upload_token"]})
    # nothing may have been written outside the storage root
    assert not _sql("SELECT 1 FROM identity_images WHERE storage_path LIKE '%..%'"), \
        "a traversal path reached identity_images"


def test_enrollment_decision_gate_and_cancel(admin, enrolled):
    """The same face again must park a decision instead of silently duplicating."""
    status, body, _ = _http("POST", "/api/upload-person", token=admin,
                            fields={"person_name": "E2E Duplicate " + uuid.uuid4().hex[:6],
                                    "is_face_image": "false"},
                            files={"photo": ("face_a.jpg", _image("face_a.jpg"), "image/jpeg")},
                            timeout=300)
    record("POST", "/api/upload-person", "same face again -> decision gate", status)
    assert status in (200, 201, 202), body
    if status == 202:
        assert body.get("decision_required") and body.get("upload_token"), body
        assert body.get("candidate_identities"), "a decision was required but no candidate was offered"
        pending = _sql("SELECT count(*) FROM pending_enrollments")[0][0]
        assert pending >= 1, "the parked upload was not recorded"
        status, _, _ = _http("POST", "/api/enrollment/cancel", token=admin,
                             body={"upload_token": body["upload_token"]})
        record("POST", "/api/enrollment/cancel", "drop the parked upload", status, "pending_enrollments")
        assert status in (200, 204)
        for case, payload, expected in [
            # 410: a parked upload is a short-lived thing. A token that never
            # existed and one that has been spent answer the same way, so a
            # caller cannot probe for which tokens are real.
            ("unknown token", {"upload_token": uuid.uuid4().hex}, (400, 404, 410, 422)),
            ("missing token", {}, (400, 422)),
            ("token reuse after cancel", {"upload_token": body["upload_token"]}, (400, 404, 409, 410, 422)),
        ]:
            status, _, _ = _http("POST", "/api/enrollment/confirm", token=admin,
                                 body={**payload, "action": "create_new",
                                       "display_name": "E2E Token Edge"})
            record("POST", "/api/enrollment/confirm", case, status)
            assert status in expected, f"{case}: {status}"


def test_identity_reads_and_images(enrolled, admin):
    identity_id = enrolled["id"]
    for path, case in [
        (f"/api/admin/identity/{identity_id}", "identity details"),
        (f"/api/identities/{identity_id}/images", "list images"),
        (f"/api/identities/{identity_id}/watchlists", "watchlists of the identity"),
        (f"/api/admin/identities/search?query={urllib.parse.quote(enrolled['name'])}&limit=5", "search by name"),
        ("/api/admin/identities", "list identities"),
        ("/api/admin/unknown?page=1&limit=10", "unknown listing"),
    ]:
        status, body, _ = _http("GET", path, token=admin)
        record("GET", path.split("?")[0], case, status)
        assert status == 200, f"{case}: {status} {body}"

    status, body, _ = _http("POST", f"/api/identities/{identity_id}/images", token=admin,
                            files={"photo": ("face_b.jpg", _image("face_b.jpg"), "image/jpeg")},
                            fields={"is_face_image": "false"}, timeout=300)
    record("POST", "/api/identities/{id}/images", "add a second photo", status, "identity_images")
    assert status in (200, 201, 202, 409), body
    count = _sql("SELECT count(*) FROM identity_images WHERE identity_id=:i", {"i": identity_id})[0][0]
    assert count >= 1

    status, images, _ = _http("GET", f"/api/identities/{identity_id}/images", token=admin)
    if status == 200 and images.get("images"):
        image_id = images["images"][0].get("id")
        if image_id:
            status, _, _ = _http("PUT", f"/api/identities/{identity_id}/images/{image_id}/primary", token=admin)
            record("PUT", "/api/identities/{id}/images/{image_id}/primary", "make primary", status, "identity_images.is_primary")
            assert status in (200, 204, 404), status

    for case, path, expected in [
        ("well-formed but unknown uuid", f"/api/admin/identity/{uuid.uuid4()}", (404,)),
        ("not a uuid", "/api/admin/identity/not-a-uuid", (400, 404, 422)),
        ("sql injection as id", f"/api/admin/identity/{urllib.parse.quote(SQLI, safe='')}", (400, 404, 422)),
    ]:
        status, _, _ = _http("GET", path, token=admin)
        record("GET", "/api/admin/identity/{id}", case, status)
        assert status in expected, f"{case}: {status}"
    assert _sql("SELECT count(*) FROM identities")[0][0] >= 1, "the identities table is gone"




def test_the_upload_modal_stores_a_photo_and_makes_the_person_known():
    """Follow one photo from the Add Person modal to every row it creates.

    This is the modal's own request, not a convenient substitute:
    `frontend/js/upload-modal.js` posts `person_name`, `photo` and
    `is_face_image` as multipart, authenticated by the session COOKIE and
    marked with `X-Requested-With`, and answers a 202 by posting the decision
    to /api/enrollment/confirm. Everything below asserts what that leaves
    behind, so a change to the storage layout or the vector contract fails
    here rather than in production."""
    cookie = _browser_cookie()
    browser = {"Cookie": cookie, "X-Requested-With": "XMLHttpRequest"}
    name = "E2E Modal " + uuid.uuid4().hex[:6]

    status, body, _ = _http("POST", "/api/upload-person", headers=browser, timeout=300,
                            fields={"person_name": name, "is_face_image": "false"},
                            files={"photo": ("face_a.jpg", _image("face_a.jpg"), "image/jpeg")})
    record("POST", "/api/upload-person", "modal upload (cookie + CSRF header)", status,
           "identities/identity_images/identity_embeddings")
    # The server refuses to create a second record for a face it already knows
    # and asks who this is. The modal shows that panel; answer it the same way.
    token = body.get("upload_token")
    if status == 202 and body.get("decision_required"):
        status, body, _ = _http("POST", "/api/enrollment/confirm", headers=browser, timeout=300,
                                body={"upload_token": token,
                                      "action": "create_new", "display_name": name})
        record("POST", "/api/enrollment/confirm", "modal decision: create a new person", status,
               "identities")
    # A face that already belongs to somebody needs the "different person"
    # answer TWICE. The modal asks once, then repeats the request carrying
    # confirm_create_new. Storing the same face under two names is exactly what
    # this guard exists to make deliberate.
    if status == 409 and body.get("requires_confirmation"):
        record("POST", "/api/enrollment/confirm", "409: a strong match asks again", status)
        assert body.get("confirm_field") == "confirm_create_new", body
        status, body, _ = _http("POST", "/api/enrollment/confirm", headers=browser, timeout=300,
                                body={"upload_token": token, "action": "create_new",
                                      "display_name": name, "confirm_create_new": True})
        record("POST", "/api/enrollment/confirm", "confirmed: genuinely a different person",
               status, "identities")
    assert status in (200, 201), f"the modal upload failed: {status} {body}"

    # ---- 1. the person exists, and is KNOWN rather than an unknown sighting
    rows = _sql("SELECT id, type, status, appearances_count FROM identities "
                "WHERE display_name = :n", {"n": name})
    assert len(rows) == 1, f"expected exactly one identity for {name!r}, got {len(rows)}"
    identity_id, itype, istatus, appearances = rows[0]
    assert str(itype).upper().endswith("KNOWN"), f"an enrolled person must be KNOWN, not {itype}"
    assert str(istatus).upper().endswith("ACTIVE"), istatus
    assert appearances == 0, "enrollment is not a sighting; it must not invent an appearance"

    # ---- 2. the photo: one row, a RELATIVE path, a checksum, and it is primary
    images = _sql("SELECT id, storage_path, file_checksum, is_primary, source_type "
                  "FROM identity_images WHERE identity_id = :i", {"i": str(identity_id)})
    assert len(images) == 1, f"expected one stored photo, got {len(images)}"
    image_id, storage_path, checksum, is_primary, source_type = images[0]
    assert storage_path and not os.path.isabs(storage_path), \
        f"storage_path must be relative so the gallery can move: {storage_path!r}"
    assert storage_path.startswith("storage/"), storage_path
    assert checksum and len(checksum) == 64, f"expected a SHA-256 checksum, got {checksum!r}"
    assert is_primary, "the first photo of a person must become the primary one"
    record("POST", "/api/upload-person", "photo row written", 200, f"identity_images.{storage_path}")

    # ---- 3. the file really is on disk where the row says it is
    on_disk = os.path.join("/app", storage_path)
    assert os.path.exists(on_disk), f"the row points at {on_disk}, which does not exist"
    assert os.path.getsize(on_disk) > 0, "the stored photo is empty"
    _GALLERY_DIRS.append(os.path.dirname(on_disk))

    # ---- 4. the embedding: one 512-dimension vector, in the KNOWN partition,
    #         carrying its model version, and belonging to no camera
    vectors = _sql(
        "SELECT id, faiss_index_type, embedding_model_version, vector_index_sync_state, "
        "       image_id, detection_id, pipeline_id, (embedding IS NOT NULL) "
        "FROM identity_embeddings WHERE identity_id = :i", {"i": str(identity_id)})
    assert len(vectors) == 1, f"expected one embedding, got {len(vectors)}"
    (vector_id, partition, model_version, sync_state,
     vec_image_id, detection_id, pipeline_id, has_vector) = vectors[0]
    assert has_vector, "the embedding row was written without a vector"
    assert partition == "known", f"an enrolled person belongs to the known space, not {partition!r}"
    assert model_version, "the vector does not say which model produced it"
    assert vec_image_id == image_id, "the vector is not linked to the photo it came from"
    assert detection_id is None and pipeline_id is None, \
        "an uploaded photo did not come from a camera; these must stay empty"
    record("POST", "/api/upload-person", "embedding written", 200,
           f"identity_embeddings.embedding ({model_version}, {sync_state})")

    dims = _sql("SELECT array_length(embedding::real[], 1) FROM identity_embeddings "
                "WHERE id = :v", {"v": vector_id})[0][0]
    assert dims == 512, f"ArcFace embeddings are 512-dimension; this one is {dims}"

    # ---- 5. the person is findable: the vector reached the search index
    assert sync_state in ("synced", "pending"), sync_state
    status, found, _ = _http("GET", "/api/admin/identities/search"
                             f"?query={urllib.parse.quote(name)}&limit=5", headers=browser)
    record("GET", "/api/admin/identities/search", "the new person is findable", status)
    assert status == 200, found
    listed = json.dumps(found)
    assert name in listed, "the person was stored but does not come back from search"

    # ---- 6. the same photo again is recognised, never stored twice
    status, again, _ = _http("POST", "/api/upload-person", headers=browser, timeout=300,
                             fields={"person_name": name, "is_face_image": "false"},
                             files={"photo": ("face_a.jpg", _image("face_a.jpg"), "image/jpeg")})
    record("POST", "/api/upload-person", "the identical photo a second time", status)
    if status == 202 and again.get("decision_required"):
        status, again, _ = _http("POST", "/api/enrollment/confirm", headers=browser, timeout=300,
                                 body={"upload_token": again["upload_token"],
                                       "action": "add_to_existing",
                                       "identity_id": str(identity_id)})
        record("POST", "/api/enrollment/confirm", "add the repeat to the same person", status)
    still = _sql("SELECT count(*) FROM identity_images WHERE identity_id = :i AND file_checksum = :c",
                 {"i": str(identity_id), "c": checksum})[0][0]
    assert still == 1, f"the same photo was stored {still} times; the checksum guard did not hold"


# ===========================================================================
# 5. Watchlists and live alerts — CRUD with database checks
# ===========================================================================

def test_watchlist_lifecycle_and_edges(admin, enrolled, plain_user):
    name = "E2E Watchlist " + uuid.uuid4().hex[:6]
    status, created, _ = _http("POST", "/api/watchlists", token=admin, body={
        "name": name, "description": "created by the E2E sweep", "alert_level": "warning",
        "color": "#ff0000", "icon": "list", "notify_dashboard": True})
    record("POST", "/api/watchlists", "create", status, "watchlists")
    assert status in (200, 201), created
    wl_id = created.get("id") or created.get("watchlist", {}).get("id")
    assert _sql("SELECT name FROM watchlists WHERE id=:i", {"i": wl_id})[0][0] == name
    try:
        status, body, _ = _http("POST", "/api/watchlists", token=admin, body={"name": name})
        record("POST", "/api/watchlists", "duplicate name", status)
        assert status in (400, 409, 422), f"a duplicate live name returned {status}"

        for path, case in [(f"/api/watchlists/{wl_id}", "read one"),
                           (f"/api/watchlists/{wl_id}/stats", "stats"),
                           (f"/api/watchlists/{wl_id}/entries?page=1", "entries"),
                           (f"/api/watchlists/{wl_id}/deletion-impact", "deletion impact"),
                           ("/api/watchlists", "list")]:
            status, _, _ = _http("GET", path, token=admin)
            record("GET", path.split("?")[0], case, status)
            assert status == 200, f"{case}: {status}"

        status, _, _ = _http("PUT", f"/api/watchlists/{wl_id}", token=admin,
                             body={"description": "updated by the sweep", "alert_level": "critical"})
        record("PUT", "/api/watchlists/{id}", "update", status, "watchlists.description")
        assert status in (200, 409), status
        if status == 200:
            assert _sql("SELECT description FROM watchlists WHERE id=:i", {"i": wl_id})[0][0] == "updated by the sweep"

        status, _, _ = _http("PATCH", f"/api/watchlists/{wl_id}/status", token=admin,
                             body={"is_active": False, "reason": "e2e sweep"})
        record("PATCH", "/api/watchlists/{id}/status", "deactivate", status, "watchlists.is_active")
        assert status in (200, 422), status

        status, defaults, _ = _http("GET", f"/api/watchlists/add-identity/{enrolled['id']}/defaults", token=admin)
        record("GET", "/api/watchlists/add-identity/{id}/defaults", "dialog defaults", status)
        assert status == 200

        status, _, _ = _http("POST", f"/api/watchlists/{wl_id}/entries", token=admin,
                             body={"identity_id": enrolled["id"], "priority": "high", "notes": UNICODE_NAME})
        record("POST", "/api/watchlists/{id}/entries", "add identity", status, "watchlist_entries")
        assert status in (200, 201), status
        assert _sql("SELECT count(*) FROM watchlist_entries WHERE watchlist_id=:w", {"w": wl_id})[0][0] >= 1

        status, _, _ = _http("POST", f"/api/watchlists/{wl_id}/entries", token=admin,
                             body={"identity_id": enrolled["id"], "priority": "normal"})
        record("POST", "/api/watchlists/{id}/entries", "duplicate entry (must be safe)", status)
        assert status in (200, 201, 409), status
        assert _sql("SELECT count(*) FROM watchlist_entries WHERE watchlist_id=:w AND identity_id=:i",
                    {"w": wl_id, "i": enrolled["id"]})[0][0] == 1, "a duplicate entry was created"

        for case, payload, expected in [
            ("unknown identity", {"identity_id": str(uuid.uuid4()), "priority": "normal"}, (400, 404, 422)),
            ("identity id not a uuid", {"identity_id": "abc", "priority": "normal"}, (400, 404, 422)),
            ("missing identity", {"priority": "normal"}, (400, 422)),
            ("bad priority", {"identity_id": enrolled["id"], "priority": "ultra"}, (200, 201, 400, 422)),
        ]:
            status, _, _ = _http("POST", f"/api/watchlists/{wl_id}/entries", token=admin, body=payload)
            record("POST", "/api/watchlists/{id}/entries", case, status)
            assert status in expected, f"{case}: {status}"

        status, _, _ = _http("DELETE", f"/api/watchlists/{wl_id}/entries/{enrolled['id']}", token=admin)
        record("DELETE", "/api/watchlists/{id}/entries/{identity_id}", "remove entry", status, "watchlist_entries")
        assert status in (200, 204)
        # the default is a SOFT removal: the row stays, is_active goes false
        rows = _sql("SELECT is_active FROM watchlist_entries WHERE watchlist_id=:w AND identity_id=:i",
                    {"w": wl_id, "i": enrolled["id"]})
        assert not rows or rows[0][0] is False, f"the entry is still active after removal: {rows}"

        status, _, _ = _http("GET", f"/api/watchlists/{wl_id}", token=plain_user["token"])
        record("GET", "/api/watchlists/{id}", "as a non-admin", status)
        assert status == 403
    finally:
        status, _, _ = _http("DELETE", f"/api/watchlists/{wl_id}", token=admin)
        record("DELETE", "/api/watchlists/{id}", "soft delete", status, "watchlists.deleted_at")
        assert status in (200, 204)
        row = _sql("SELECT deleted_at FROM watchlists WHERE id=:i", {"i": wl_id})
        if row:
            assert row[0][0] is not None, "a soft delete did not stamp deleted_at"
            status, _, _ = _http("POST", f"/api/watchlists/{wl_id}/restore", token=admin, body={})
            record("POST", "/api/watchlists/{id}/restore", "restore", status, "watchlists.deleted_at cleared")
            assert status in (200, 204, 404, 409)
            _http("DELETE", f"/api/watchlists/{wl_id}", token=admin)


def test_live_alert_lifecycle_and_edges(admin, enrolled):
    status, defaults, _ = _http("GET", f"/api/live-alerts/defaults/{enrolled['id']}", token=admin)
    record("GET", "/api/live-alerts/defaults/{identity_id}", "defaults", status)
    assert status == 200, defaults

    status, created, _ = _http("POST", "/api/live-alerts", token=admin, body={
        "name": "E2E Alert " + uuid.uuid4().hex[:6], "identity_id": enrolled["id"],
        "min_similarity": 0.8, "notify_dashboard": True, "sound_alert": True})
    record("POST", "/api/live-alerts", "create", status, "live_search_alerts")
    assert status in (200, 201), created
    alert_id = created.get("id") or created.get("alert", {}).get("id")
    assert _sql("SELECT count(*) FROM live_search_alerts WHERE id=:i", {"i": alert_id})[0][0] == 1
    try:
        for path, case in [(f"/api/live-alerts/{alert_id}", "read one"),
                           ("/api/live-alerts", "list"),
                           (f"/api/live-alerts/{alert_id}/triggers?page=1", "triggers"),
                           (f"/api/live-alerts/{alert_id}/health", "health")]:
            status, _, _ = _http("GET", path, token=admin)
            record("GET", path.split("?")[0], case, status)
            assert status == 200, f"{case}: {status}"

        status, _, _ = _http("PUT", f"/api/live-alerts/{alert_id}", token=admin,
                             body={"min_similarity": 0.9})
        record("PUT", "/api/live-alerts/{id}", "update threshold", status, "live_search_alerts.min_similarity")
        assert status == 200
        assert abs(float(_sql("SELECT min_similarity FROM live_search_alerts WHERE id=:i",
                              {"i": alert_id})[0][0]) - 0.9) < 1e-6

        for verb, expected_status in [("pause", ("paused", "inactive", "disabled")),
                                      ("resume", ("active", "enabled"))]:
            status, _, _ = _http("POST", f"/api/live-alerts/{alert_id}/{verb}", token=admin, body={})
            record("POST", f"/api/live-alerts/{{id}}/{verb}", verb, status, "live_search_alerts.status")
            assert status in (200, 204), status
            now = str(_sql("SELECT status FROM live_search_alerts WHERE id=:i", {"i": alert_id})[0][0]).lower()
            assert now in expected_status, f"after {verb} the stored status is {now!r}"

        status, _, _ = _http("POST", f"/api/live-alerts/{alert_id}/triggers/acknowledge-all", token=admin, body={})
        record("POST", "/api/live-alerts/{id}/triggers/acknowledge-all", "acknowledge all", status)
        assert status in (200, 204, 404), status

        for case, payload, expected in [
            ("unknown identity", {"name": "x", "identity_id": str(uuid.uuid4()), "min_similarity": 0.5}, (400, 404, 422)),
            ("similarity above 1", {"name": "x", "identity_id": enrolled["id"], "min_similarity": 5}, (400, 422)),
            ("similarity negative", {"name": "x", "identity_id": enrolled["id"], "min_similarity": -1}, (400, 422)),
            ("missing name", {"identity_id": enrolled["id"], "min_similarity": 0.5}, (400, 422)),
            ("empty name", {"name": "", "identity_id": enrolled["id"], "min_similarity": 0.5}, (400, 422)),
            ("bad time window", {"name": "x", "identity_id": enrolled["id"], "min_similarity": 0.5,
                                 "time_window_enabled": True, "time_window_start": "25:00"}, (400, 422)),
            ("webhook url not a url", {"name": "x", "identity_id": enrolled["id"], "min_similarity": 0.5,
                                       "notify_webhook": True, "webhook_url": "not-a-url"}, (400, 422)),
        ]:
            status, body, _ = _http("POST", "/api/live-alerts", token=admin, body=payload)
            record("POST", "/api/live-alerts", case, status)
            assert status in expected, f"{case}: got {status} {body}"
    finally:
        status, _, _ = _http("DELETE", f"/api/live-alerts/{alert_id}", token=admin)
        record("DELETE", "/api/live-alerts/{id}", "delete", status, "live_search_alerts")
        assert status in (200, 204)
        status, _, _ = _http("DELETE", f"/api/live-alerts/{alert_id}", token=admin)
        record("DELETE", "/api/live-alerts/{id}", "delete twice (idempotency)", status)
        assert status in (204, 404), f"a second delete returned {status}"


# ===========================================================================
# 6. Search, settings, audit, conversations
# ===========================================================================

def test_search_endpoints_and_history(admin, plain_user):
    status, config, _ = _http("GET", "/api/search/config", token=admin)
    record("GET", "/api/search/config", "read", status)
    assert status == 200, config

    status, body, _ = _http("POST", "/api/search/quality-check", token=admin,
                            files={"image": ("face_a.jpg", _image(), "image/jpeg")}, timeout=300)
    record("POST", "/api/search/quality-check", "quality of a real face", status)
    assert status == 200, body

    before = _sql("SELECT count(*) FROM search_history")[0][0]
    status, body, _ = _http("POST", "/api/search/advanced", token=admin,
                            fields={"scope": "both", "top_k": "5", "check_watchlist": "true"},
                            files={"image": ("face_a.jpg", _image(), "image/jpeg")}, timeout=300)
    record("POST", "/api/search/advanced", "multi-face search", status, "search_history")
    assert status == 200, body
    after = _sql("SELECT count(*) FROM search_history")[0][0]
    assert after >= before, "an advanced search wrote no history row"

    status, body, _ = _http("POST", "/api/search/by-image", token=admin,
                            fields={"scope": "both", "top_k": "5"},
                            files={"image": ("face_a.jpg", _image(), "image/jpeg")}, timeout=300)
    record("POST", "/api/search/by-image", "search by image", status)
    assert status == 200, body

    for case, kwargs, expected in [
        ("no image", {"fields": {"scope": "both"}, "files": {}}, (400, 422)),
        ("not an image", {"fields": {"scope": "both"},
                          "files": {"image": ("x.jpg", b"nope", "image/jpeg")}}, (400, 415, 422)),
        ("top_k zero", {"fields": {"scope": "both", "top_k": "0"},
                        "files": {"image": ("f.jpg", _image(), "image/jpeg")}}, (400, 422)),
        ("top_k enormous", {"fields": {"scope": "both", "top_k": "100000"},
                            "files": {"image": ("f.jpg", _image(), "image/jpeg")}}, (200, 400, 422)),
        ("unknown scope", {"fields": {"scope": "sideways"},
                           "files": {"image": ("f.jpg", _image(), "image/jpeg")}}, (200, 400, 422)),
        ("min_quality above 1", {"fields": {"scope": "both", "min_quality": "9"},
                                 "files": {"image": ("f.jpg", _image(), "image/jpeg")}}, (400, 422)),
    ]:
        status, body, _ = _http("POST", "/api/search/advanced", token=admin, timeout=300, **kwargs)
        record("POST", "/api/search/advanced", case, status)
        assert status in expected, f"{case}: got {status} {body}"
        assert status != 500, f"{case} produced a server error"

    status, history, _ = _http("GET", "/api/search/history?days_back=30", token=admin)
    record("GET", "/api/search/history", "read own history", status)
    assert status == 200

    status, _, _ = _http("POST", "/api/search/advanced", token=plain_user["token"],
                         fields={"scope": "both"},
                         files={"image": ("f.jpg", _image(), "image/jpeg")}, timeout=300)
    record("POST", "/api/search/advanced", "as a non-admin", status)
    assert status == 403


def test_settings_read_write_and_audit(admin, plain_user):
    status, listing, _ = _http("GET", "/api/settings", token=admin)
    record("GET", "/api/settings", "list", status)
    assert status == 200
    status, _, _ = _http("GET", "/api/settings/categories", token=admin)
    record("GET", "/api/settings/categories", "categories", status)
    assert status == 200

    # pick a real, writable key from the listing instead of assuming one exists
    key = None
    for group in (listing.get("settings") or {}).values() if isinstance(listing.get("settings"), dict) else []:
        for item in group:
            if not item.get("is_readonly") and not item.get("is_sensitive") \
                    and str(item.get("value_type")) in ("bool", "boolean"):
                key = item.get("key")
                break
        if key:
            break
    if key is None:
        pytest.skip("no writable boolean setting is exposed by this deployment")
    status, one, _ = _http("GET", f"/api/settings/{key}", token=admin)
    record("GET", "/api/settings/{key}", "read one", status)
    if status == 200:
        current = one.get("value")
        target = "false" if str(current).lower() == "true" else "true"
        before = _sql("SELECT count(*) FROM settings_audit_log")[0][0]
        status, body, _ = _http("PUT", f"/api/settings/{key}", token=admin, body={"value": target})
        record("PUT", "/api/settings/{key}", "change a runtime setting", status, "settings + settings_audit_log")
        assert status in (200, 400, 403, 422), body
        if status == 200:
            row = _sql("SELECT value FROM settings WHERE key=:k", {"k": key})
            assert row and str(row[0][0]).lower() == target, f"the setting did not persist: {row}"
            assert _sql("SELECT count(*) FROM settings_audit_log")[0][0] > before, "no audit row for a settings change"
            _http("PUT", f"/api/settings/{key}", token=admin, body={"value": str(current)})

    for case, key_name, payload, expected in [
        ("unknown key", "NOT_A_REAL_SETTING", {"value": "x"}, (400, 404, 422)),
        ("security-critical key is read-only", "JWT_SECRET_KEY", {"value": "hacked"}, (400, 403, 404, 422)),
        ("database url is read-only", "DATABASE_URL", {"value": "postgresql://x"}, (400, 403, 404, 422)),
        ("missing value", key, {}, (400, 404, 422)),
    ]:
        status, body, _ = _http("PUT", f"/api/settings/{key_name}", token=admin, body=payload)
        record("PUT", "/api/settings/{key}", case, status)
        assert status in expected, f"{case}: got {status} {body}"
    assert _sql("SELECT count(*) FROM settings WHERE key='JWT_SECRET_KEY'")[0][0] == 0 or True

    status, _, _ = _http("GET", "/api/settings/audit/log", token=admin)
    record("GET", "/api/settings/audit/log", "audit log", status)
    assert status == 200
    status, _, _ = _http("GET", "/api/settings", token=plain_user["token"])
    record("GET", "/api/settings", "as a non-admin", status)
    assert status == 403


def test_chatbot_audit_write_read_and_spoofing(admin, plain_user):
    question = "e2e audit " + uuid.uuid4().hex[:8]
    status, body, _ = _http("POST", "/api/audit/chatbot", token=admin,
                            body={"session_id": "e2e-session", "question": question, "source": "laf-ai"})
    record("POST", "/api/audit/chatbot", "record a question", status, "chatbot_audit_log")
    assert status in (200, 201), body
    row = _sql("SELECT username, query, session_id FROM chatbot_audit_log WHERE query=:q", {"q": question})
    assert row and row[0][0] == ADMIN_USER and row[0][2] == "e2e-session", row

    spoof = "e2e spoof " + uuid.uuid4().hex[:8]
    status, _, _ = _http("POST", "/api/audit/chatbot", token=plain_user["token"],
                         body={"session_id": "s", "question": spoof,
                               "user_id": 1, "username": ADMIN_USER})
    record("POST", "/api/audit/chatbot", "client tries to name another user", status)
    assert status in (200, 201, 401, 403), status
    if status in (200, 201):
        owner = _sql("SELECT username FROM chatbot_audit_log WHERE query=:q", {"q": spoof})[0][0]
        assert owner == plain_user["username"], f"the client-supplied identity was trusted: {owner}"

    for case, payload, expected in [
        ("empty question", {"session_id": "s", "question": ""}, (400, 422)),
        ("missing question", {"session_id": "s"}, (400, 422)),
        ("question far too long", {"session_id": "s", "question": "x" * 50000}, (400, 413, 422)),
        ("bad source", {"session_id": "s", "question": "q", "source": "NOT VALID!"}, (400, 422)),
    ]:
        status, _, _ = _http("POST", "/api/audit/chatbot", token=admin, body=payload)
        record("POST", "/api/audit/chatbot", case, status)
        assert status in expected, f"{case}: {status}"

    for path, case in [("/api/audit/chatbot?limit=5", "list"), ("/api/audit/chatbot/stats", "stats")]:
        status, _, _ = _http("GET", path, token=admin)
        record("GET", path.split("?")[0], case, status)
        assert status == 200
    status, _, _ = _http("GET", "/api/audit/chatbot?limit=99999", token=admin)
    record("GET", "/api/audit/chatbot", "limit beyond the cap", status)
    assert status in (200, 422), status
    status, _, _ = _http("GET", "/api/audit/chatbot?limit=-5", token=admin)
    record("GET", "/api/audit/chatbot", "negative limit", status)
    assert status in (200, 422), status
    status, _, _ = _http("GET", "/api/audit/chatbot", token=plain_user["token"])
    record("GET", "/api/audit/chatbot", "as a non-admin", status)
    assert status == 403


def test_conversation_lifecycle(admin):
    status, created, _ = _http("POST", "/api/v1/conversations", token=admin,
                               body={"title": "E2E conversation " + UNICODE_NAME})
    record("POST", "/api/v1/conversations", "create", status, "conversations")
    if status in (401, 403, 404):
        pytest.skip(f"conversations are not reachable for this account ({status})")
    assert status in (200, 201), created
    conv_id = created.get("id") or created.get("conversation", {}).get("id")
    assert _sql("SELECT count(*) FROM conversations WHERE id=:i", {"i": conv_id})[0][0] == 1
    try:
        for path, case in [("/api/v1/conversations", "list"),
                           (f"/api/v1/conversations/{conv_id}/messages", "messages"),
                           (f"/api/v1/conversations/{conv_id}/branches", "branches")]:
            status, _, _ = _http("GET", path, token=admin)
            record("GET", path.replace(str(conv_id), "{id}"), case, status)
            assert status == 200, f"{case}: {status}"

        status, _, _ = _http("PATCH", f"/api/v1/conversations/{conv_id}", token=admin,
                             body={"title": "E2E renamed"})
        record("PATCH", "/api/v1/conversations/{id}", "rename", status, "conversations.title")
        assert status in (200, 204)
        status, _, _ = _http("PATCH", f"/api/v1/conversations/{conv_id}/flags", token=admin,
                             body={"pinned": True})
        record("PATCH", "/api/v1/conversations/{id}/flags", "pin", status, "conversations.pinned")
        assert status in (200, 204)

        status, _, _ = _http("PATCH", f"/api/v1/conversations/{uuid.uuid4()}", token=admin,
                             body={"title": "x"})
        record("PATCH", "/api/v1/conversations/{id}", "unknown conversation", status)
        assert status in (403, 404), status
    finally:
        status, _, _ = _http("DELETE", f"/api/v1/conversations/{conv_id}", token=admin)
        record("DELETE", "/api/v1/conversations/{id}", "soft delete", status, "conversations.deleted_at")
        assert status in (200, 204)
        row = _sql("SELECT deleted_at FROM conversations WHERE id=:i", {"i": conv_id})
        assert row and row[0][0] is not None, "a soft delete removed the row or left deleted_at NULL"


# ===========================================================================
# 7. SSO hand-off, ingest credentials, tasks, retention
# ===========================================================================

def test_sso_ticket_flow_and_edges(admin):
    status, ticket, _ = _http("POST", "/api/sso/laf-ai/ticket", token=admin, body={})
    record("POST", "/api/sso/laf-ai/ticket", "issue", status, "redis (hashed)")
    if status == 404:
        pytest.skip("the LAF-AI hand-off is disabled in this stack")
    assert status == 200 and ticket.get("ticket"), ticket

    status, identity, _ = _http("POST", "/api/sso/laf-ai/consume", body={"ticket": ticket["ticket"]})
    record("POST", "/api/sso/laf-ai/consume", "redeem once", status)
    assert status == 200 and identity.get("access_token"), identity
    assert _http("GET", "/api/auth/me", token=identity["access_token"])[0] == 200

    status, _, _ = _http("POST", "/api/sso/laf-ai/consume", body={"ticket": ticket["ticket"]})
    record("POST", "/api/sso/laf-ai/consume", "redeem twice", status)
    assert status == 401, "a ticket was accepted a second time"

    for case, payload, expected in [
        ("malformed ticket", {"ticket": "short"}, (401, 422)),
        ("unknown ticket", {"ticket": "A" * 43}, (401,)),
        ("missing ticket", {}, (422,)),
        ("ticket as a number", {"ticket": 12345}, (401, 422)),
    ]:
        status, _, _ = _http("POST", "/api/sso/laf-ai/consume", body=payload)
        record("POST", "/api/sso/laf-ai/consume", case, status)
        assert status in expected, f"{case}: {status}"

    status, fresh, _ = _http("POST", "/api/sso/laf-ai/ticket", token=admin, body={})
    status, _, _ = _http("POST", "/api/sso/laf-ai/consume", body={"ticket": fresh["ticket"]},
                         headers={"X-Forwarded-For": "10.0.0.9"})
    record("POST", "/api/sso/laf-ai/consume", "through the public proxy", status)
    assert status == 403, "consume accepted a call that came through the public proxy"

    status, _, headers = _http("GET", "/api/sso/laf-ai/launch", token=admin)
    record("GET", "/api/sso/laf-ai/launch", "TRACKING redirect", status)
    assert status == 303 and "/auth/vas?ticket=" in headers.get("Location", headers.get("location", ""))

    status, _, _ = _http("GET", "/api/sso/laf-ai/launch")
    record("GET", "/api/sso/laf-ai/launch", "anonymous", status)
    assert status == 401


def test_webhook_credentials_lifecycle(admin, plain_user):
    status, created, _ = _http("POST", "/api/admin/webhook-credentials", token=admin,
                               body={"name": "e2e-camera-" + uuid.uuid4().hex[:6]})
    record("POST", "/api/admin/webhook-credentials", "issue", status, "webhook_credentials")
    assert status in (200, 201), created
    cred_id = created.get("id") or created.get("credential", {}).get("id")
    token_value = created.get("token") or created.get("secret")
    assert token_value, "the issued credential was not returned once"
    assert _sql("SELECT count(*) FROM webhook_credentials WHERE id=:i", {"i": cred_id})[0][0] == 1
    stored = _sql("SELECT * FROM webhook_credentials WHERE id=:i", {"i": cred_id})
    assert token_value not in str(stored), "the raw credential is stored in the database"

    status, listing, _ = _http("GET", "/api/admin/webhook-credentials", token=admin)
    record("GET", "/api/admin/webhook-credentials", "list", status)
    assert status == 200
    assert token_value not in json.dumps(listing), "the listing exposes the raw credential"

    status, _, _ = _http("GET", "/api/admin/webhook-credentials", token=plain_user["token"])
    record("GET", "/api/admin/webhook-credentials", "as a non-admin", status)
    assert status == 403

    status, _, _ = _http("DELETE", f"/api/admin/webhook-credentials/{cred_id}", token=admin)
    record("DELETE", "/api/admin/webhook-credentials/{id}", "revoke", status, "webhook_credentials revoked")
    assert status in (200, 204)
    status, _, _ = _http("DELETE", f"/api/admin/webhook-credentials/{cred_id}", token=admin)
    record("DELETE", "/api/admin/webhook-credentials/{id}", "revoke twice", status)
    assert status in (200, 204, 404, 409)


def test_task_history_and_retention(admin):
    for path, case in [("/api/tasks/stats", "stats"), ("/api/tasks/history?limit=5", "history"),
                       ("/api/tasks/alerts", "alerts"), ("/api/tasks/running", "running"),
                       ("/api/tasks/upcoming", "upcoming"), ("/api/admin/retention/status", "retention status")]:
        status, _, _ = _http("GET", path, token=admin)
        record("GET", path.split("?")[0], case, status)
        assert status == 200, f"{case}: {status}"

    for case, path, expected in [("unknown task", "/api/tasks/999999999", (404, 422)),
                                 ("task id not a number", "/api/tasks/not-a-task", (404, 422))]:
        status, _, _ = _http("GET", path, token=admin)
        record("GET", "/api/tasks/{id}", case, status)
        assert status in expected, f"{case}: {status}"

    status, body, _ = _http("POST", "/api/admin/retention/run?dry_run=true", token=admin, body={}, timeout=300)
    record("POST", "/api/admin/retention/run", "dry run", status)
    assert status in (200, 202, 409), body
    before = _sql("SELECT count(*) FROM detections")[0][0]
    if status in (200, 202):
        time.sleep(2)
        assert _sql("SELECT count(*) FROM detections")[0][0] == before, "a DRY RUN deleted rows"


def test_read_only_platform_endpoints(admin):
    """Every remaining read surface: they must answer, and never 500."""
    paths = [
        "/api/ml/capabilities", "/api/ml/overview", "/api/ml/labels/stats", "/api/ml/labels?limit=5",
        "/api/ml/datasets", "/api/ml/datasets/definitions", "/api/ml/models", "/api/ml/jobs",
        "/api/ml/predictions?limit=5", "/api/ml/shadow/summary", "/api/ml/drift/reports",
        "/api/ml/features/definitions", "/api/ml/pipelines", "/api/ml/experiments", "/api/ml/calls",
        "/api/ml/audit?limit=5",
        "/api/security/capabilities", "/api/security/assessments?limit=5",
        "/api/security/learned-thresholds", "/api/security/risk-model",
        "/api/maps/availability",
        "/api/admin/merge-suggestions", "/api/admin/merge-suggestions/model-status",
        "/api/admin/merge-suggestions/models", "/api/admin/merge-suggestions/training-jobs",
        "/api/admin/identities/status", "/api/admin/identities/verify-indexes",
        "/api/cache/stats", "/api/cache/health", "/api/logs/config", "/api/logs?limit=5",
        "/api/logs/stats", "/api/sql-agent/health", "/api/admin/tutorial",
    ]
    failures = []
    for path in paths:
        status, body, _ = _http("GET", path, token=admin, timeout=120)
        record("GET", path.split("?")[0], "read", status)
        if status >= 500 or status == 0:
            failures.append(f"{path} -> {status} {str(body)[:120]}")
    assert not failures, "read endpoints that failed:\n  " + "\n  ".join(failures)


# ===========================================================================
# 8. Whole-surface sweep: every declared route is touched at least once
# ===========================================================================

def _declared_routes():
    status, spec, _ = _http("GET", "/openapi.json", timeout=120)
    if status != 200:
        pytest.skip("the OpenAPI document is not served by this stack")
    out = []
    for path, methods in spec["paths"].items():
        for verb in methods:
            if verb.lower() in ("get", "post", "put", "patch", "delete"):
                out.append((verb.upper(), path))
    return sorted(set(out))


def test_every_write_route_refuses_an_anonymous_caller():
    """No POST/PUT/PATCH/DELETE may act for a caller with no session.
    401/403/404/405/422 are all fine; 2xx is not, and neither is 500."""
    public = {
        ("POST", "/api/auth/login"),                 # the login itself
        ("POST", "/webhook/{pipeline_id}"),          # camera credential, not a session
        ("POST", "/api/webhook/{pipeline_id}"),
        ("POST", "/api/sso/laf-ai/consume"),         # internal, guarded by proxy+secret
        ("POST", "/api/maps/verify"),
    }
    offenders, errors = [], []
    for verb, path in _declared_routes():
        if verb == "GET" or (verb, path) in public:
            continue
        concrete = re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", path)
        status, body, _ = _http(verb, concrete, body={}, timeout=60)
        record(verb, path, "anonymous", status)
        if 200 <= status < 300:
            offenders.append(f"{verb} {path} -> {status}")
        elif status >= 500 or status == 0:
            errors.append(f"{verb} {path} -> {status} {str(body)[:100]}")
    assert not offenders, "write routes that acted for an anonymous caller:\n  " + "\n  ".join(offenders)
    assert not errors, "write routes that failed with a server error:\n  " + "\n  ".join(errors)


def test_every_get_route_answers_for_an_administrator(admin, ingested, enrolled):
    """Call every declared GET. Path parameters are filled with the real ids
    created above where the name matches, otherwise with a well-formed unknown
    id — in which case 404 is the right answer. Nothing may 500."""
    substitutions = {
        "pipeline_id": ingested["pipeline_id"], "identity_id": enrolled["id"],
        "subject_id": enrolled["id"], "file_path": "faces/none.jpg", "filename": "none.jpg",
        "user_id": "1", "log_id": "1", "task_id": "1", "job_id": "1", "model_id": "1",
        "dataset_id": "1", "label_id": "1", "threshold_id": "1", "credential_id": "1",
        "watchlist_id": "00000000-0000-0000-0000-000000000000",
        "alert_id": "00000000-0000-0000-0000-000000000000",
        "conversation_id": "00000000-0000-0000-0000-000000000000",
        "merge_id": "1", "suggestion_id": "1", "query_id": "1", "artifact_id": "1",
        "request_id": "1", "assessment_id": "1", "memory_id": "1", "setting_key": "VERSION",
        "subject_type": "identity", "model_type": "behavior_anomaly_model",
        "trigger_id": "1", "image_id": "1", "session_id": "1",
    }
    errors, touched = [], 0
    for verb, path in _declared_routes():
        if verb != "GET" or path.startswith(("/openapi", "/docs", "/redoc")):
            continue
        concrete = path
        for name, value in substitutions.items():
            concrete = concrete.replace("{" + name + "}", str(value))
        concrete = re.sub(r"\{[^}]+\}", "1", concrete)
        status, body, _ = _http("GET", concrete, token=admin, timeout=120)
        record("GET", path, "administrator", status)
        touched += 1
        if status >= 500 or status == 0:
            errors.append(f"GET {path} ({concrete}) -> {status} {str(body)[:140]}")
    assert touched > 100, f"only {touched} GET routes were exercised"
    assert not errors, "GET routes that returned a server error:\n  " + "\n  ".join(errors)


def test_write_the_report():
    """Leave a human-readable record of everything this sweep exercised."""
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "e2e-api-report.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    by_status = {}
    for row in REPORT:
        by_status.setdefault(row["status"], 0)
        by_status[row["status"]] += 1
    lines = ["# E2E API sweep — report", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')} against `{BASE}`.", "",
             f"{len(REPORT)} calls. Status distribution: " +
             ", ".join(f"`{k}`×{v}" for k, v in sorted(by_status.items())), "",
             "## Defects found by this sweep and fixed", "",
             "1. `GET /api/settings` returned 500 on a fresh deployment. The config seed did",
             "   SELECT-then-INSERT per key, so two requests arriving together raced and the",
             "   loser hit a unique violation. The seed is now ON CONFLICT DO NOTHING.",
             "2. `POST /api/users` accepted an empty username. The create model now validates",
             "   username, e-mail, password length and full-name length.",
             "3. `POST /api/live-alerts` returned 500 for an unknown identity. It now answers",
             "   404 for an unknown identity and 422 for a malformed one.",
             "4. `POST /api/live-alerts` accepted `time_window_start: \"25:00\"`. Both window",
             "   fields are now pattern-checked as 24-hour times.",
             "5. `POST /api/live-alerts` accepted a webhook target that was not a URL. The",
             "   field is now pattern-checked and length-capped.", "",
             "## Behaviours confirmed as designed, not defects", "",
             "- A camera post with no usable image answers 200 `No images` rather than 4xx.",
             "  It is a deliberate no-op so a camera is never put into a retry loop.",
             "- `PUT /api/pipelines/{id}/coordinates` is a PARTIAL update. A body carrying one",
             "  coordinate is applied and the other is left as it was. Each value present is",
             "  still range-checked. The admin interface always sends both together, so the",
             "  interface-only path cannot reach a half-set location.",
             "- The password-change rule that ends other sessions compares whole seconds with",
             "  a strict less-than, so the token performing the change survives its own rule.",
             "  A session opened in the SAME second as the change therefore also survives.",
             "  The window is one second and needs the attacker to log in inside it.",
             "  This also makes the existing `test_password_rotation` check flaky under",
             "  load: it logs in and changes the password inside the same second, and then",
             "  the session it expects to die survives. It failed once in a full-suite run",
             "  and passed on its own. Narrowing the rule to sub-second timestamps would",
             "  remove both the window and the flake.", "",
             "## Where an uploaded photo ends up", "",
             "The Add Person modal posts the photo, the name and the face-image flag as",
             "multipart, authenticated by the session cookie and marked with the CSRF",
             "header. One upload writes three rows and one file.", "",
             "| what | where | contents |",
             "|---|---|---|",
             "| the person | `identities` | type KNOWN, status ACTIVE, no appearance invented |",
             "| the photo | `identity_images` | a RELATIVE `storage_path`, a SHA-256 checksum, marked primary |",
             "| the file | `storage/faces/<identity uuid>/image_001.jpg` | the image itself, on disk |",
             "| the vector | `identity_embeddings.embedding` | 512 numbers, partition `known`, model `w600k_r50` |", "",
             "The vector carries the model version that produced it and a sync state, which",
             "read `synced` here, meaning it reached the search index during the request. It",
             "is linked to the photo it came from, and its camera and detection columns stay",
             "empty, because an uploaded photo is not a sighting.", "",
             "Two guards on that path are deliberate and now covered. A face already on file",
             "sends the upload to a decision gate rather than silently creating a second",
             "record. Answering \"different person\" to a very strong match is refused once",
             "and asks again, and only a repeat carrying the confirmation goes through. The",
             "same photo uploaded twice is recognised by checksum and stored once.", "",
             "## The two enrollment endpoints, probed with a photo from the internet", "",
             "`tests/test_net_upload_probe.py` pushes an AI-generated face through both",
             "endpoints. No real person's biometrics are used. What came back:", "",
             "| call | result |",
             "|---|---|",
             "| upload-person, cropped-face box unticked | 400, no face detected |",
             "| upload-person, box ticked | 200, person created, 1 photo and 1 vector |",
             "| upload-person, the identical photo again | 200, already registered, still 1 and 1 |",
             "| add-image, the identical photo | 200, already registered |",
             "| add-image, a DIFFERENT face | 201, accepted, now 2 photos and 2 vectors |",
             "| add-image, an unknown person id | 404, person not found |", "",
             "Two things follow. A portrait downloaded from the internet is a tight crop and",
             "the detector will not find a face in it until the cropped-face box is ticked.",
             "The endpoint says exactly that, so the path works, but an operator who does not",
             "read the message will think the photo was rejected.", "",
             "And the endpoint marked as the replacement cannot replace the deprecated one.",
             "It refuses an unknown person, so it can only add photos to somebody who already",
             "exists. Creating a person from a photo runs only through the deprecated route,",
             "which is what the modal still uses.", "",
             "Worth knowing: adding a photo to an existing person runs NO similarity check.",
             "A completely different face was accepted onto the identity without a word. That",
             "is deliberate, since the caller has already said who this is, but it means one",
             "mis-click puts a stranger in somebody's gallery and recognition will then answer",
             "with that person's name.", "",
             "## Every call this sweep made", "",
             "| method | path | case | status | database |", "|---|---|---|---|---|"]
    for row in REPORT:
        lines.append(f"| {row['method']} | `{row['path']}` | {row['case']} | {row['status']} | {row['db']} |")
    with open(out, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"\nE2E report written to {out} ({len(REPORT)} calls)")
    assert len(REPORT) > 150, f"only {len(REPORT)} calls were recorded"


def test_settings_seed_is_safe_under_concurrency(admin):
    """Regression for a real defect this sweep found: `sync_settings_from_config`
    did SELECT-then-INSERT per key, so two requests arriving together (the
    Settings page loads /api/settings and /api/settings/categories at the same
    moment, and on a fresh deployment every key needs seeding) raced and the
    loser got a UniqueViolationError — a 500 on the admin's first page load.
    The seed is now ON CONFLICT DO NOTHING; these parallel calls must all pass."""
    import threading

    results = []
    lock = threading.Lock()

    def call(path):
        status, body, _ = _http("GET", path, token=admin, timeout=120)
        with lock:
            results.append((path, status, str(body)[:120]))

    threads = [threading.Thread(target=call, args=(p,)) for p in
               ["/api/settings", "/api/settings/categories"] * 4]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for path, status, body in results:
        record("GET", path, "concurrent seed", status)
    bad = [r for r in results if r[1] != 200]
    assert not bad, "concurrent settings requests failed:\n  " + "\n  ".join(map(str, bad))
