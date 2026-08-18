"""Issued ingest credentials: minting, revocation, and what must never leak.

Before this feature the only way an external system could get an ingest token
was a human running `openssl rand` on the Docker host and sending the string out
of band. The application could not generate one, could not display one, and
recorded nothing about the handover; every sender shared one flat key set.

The properties worth pinning here are the ones that are cheap to break silently:

  * the raw token is returned EXACTLY once and only its SHA-256 is stored;
  * revocation is DELETE, and a revoked credential's 401 is byte-identical to a
    wrong one — there is no revoked state for the response to leak;
  * `ondelete='SET NULL'` on the issuer, so removing an employee's account does
    not black out the cameras they provisioned;
  * the environment key keeps working alongside, as break-glass;
  * verification does not query the database per frame.

HTTP against the live app, like test_webhook_auth.py. Tests run inside the
container, so BASE is localhost:8000 and paths are /app/...
"""

import hashlib
import json
import time
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"
API = "/api/admin/webhook-credentials"


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
            raw = response.read()
            return response.status, raw, dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _json(raw):
    try:
        return json.loads(raw.decode())
    except Exception:                                          # noqa: BLE001
        return {}


@pytest.fixture(scope="module")
def admin_token():
    # csrf=False is load-bearing here, not tidiness: the login handler treats
    # X-Requested-With as "this is a browser", and browser clients get the JWT
    # in an httpOnly cookie with `access_token` nulled in the body
    # (backend/routes/auth.py:135-142). Sending it would yield a None token and
    # every assertion below would fail as a 401 that means nothing.
    status, raw, _ = _http("POST", "/api/auth/login",
                           {"username": "admin", "password": "admin123"},
                           csrf=False)
    assert status == 200, f"admin login failed: {raw[:200]}"
    token = _json(raw).get("access_token")
    assert token, "login returned no bearer token"
    return token


def _mint(admin_token, name):
    status, raw, headers = _http("POST", API, {"name": name}, token=admin_token)
    assert status == 201, f"mint failed ({status}): {raw[:300]}"
    return _json(raw), headers


def _revoke(admin_token, credential_id):
    return _http("DELETE", f"{API}/{credential_id}", token=admin_token)


def _list(admin_token):
    status, raw, headers = _http("GET", API, token=admin_token)
    assert status == 200, raw[:200]
    return _json(raw), headers


def _ingest(token_value, pipeline="qa-cred-pipeline"):
    """POST a frame with the given bearer credential."""
    request = urllib.request.Request(
        f"{BASE}/api/webhook/{pipeline}", data=b"{}", method="POST")
    request.add_header("Content-Type", "application/json")
    if token_value:
        request.add_header("Authorization", f"Bearer {token_value}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _ttl(admin_token):
    payload, _ = _list(admin_token)
    return int(payload.get("cache_ttl_seconds") or 30)


def _cleanup(admin_token, name_prefix):
    payload, _ = _list(admin_token)
    for credential in payload.get("credentials", []):
        if credential["name"].startswith(name_prefix):
            _revoke(admin_token, credential["id"])


# --------------------------------------------------------------------------
# minting
# --------------------------------------------------------------------------

def test_a_minted_credential_authenticates_over_bearer(admin_token):
    _cleanup(admin_token, "QA Mint")
    created, _ = _mint(admin_token, "QA Mint Bearer")
    try:
        status, _body, _h = _ingest(created["token"])
        assert status == 200, "a freshly minted token must work on the FIRST request"
    finally:
        _revoke(admin_token, created["id"])


def test_the_first_request_after_minting_is_not_rejected(admin_token):
    """Regression: invalidate() used to only reset the timestamp, and the
    stale-while-revalidate path then served the PREVIOUS snapshot once — the one
    without the new credential. An operator would hand over a token that failed
    on first use and worked seconds later."""
    _cleanup(admin_token, "QA FirstUse")
    created, _ = _mint(admin_token, "QA FirstUse")
    try:
        first, _b, _h = _ingest(created["token"])
        assert first == 200, "the very first frame after issuance must authenticate"
    finally:
        _revoke(admin_token, created["id"])


def test_only_the_hash_is_persisted_never_the_token(admin_token):
    _cleanup(admin_token, "QA Hash")
    created, _ = _mint(admin_token, "QA Hash Only")
    raw_token = created["token"]
    expected = hashlib.sha256(raw_token.encode()).hexdigest()

    async def _read():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            row = (await db.execute(sa_text(
                "SELECT token_hash, CAST(webhook_credentials.* AS text) "
                "FROM webhook_credentials WHERE id = :i"), {"i": created["id"]})).first()
            return row

    try:
        from conftest import run_on_shared_loop as run_async
        row = run_async(_read())
        assert row is not None
        assert row[0] == expected, "stored hash is not SHA-256 of the issued token"
        assert raw_token not in row[1], "the raw token appears in a column"
    finally:
        _revoke(admin_token, created["id"])


def test_the_create_response_is_no_store(admin_token):
    """The one response that carries a token must not be cached anywhere."""
    _cleanup(admin_token, "QA NoStore")
    created, headers = _mint(admin_token, "QA NoStore")
    try:
        cache_control = (headers.get("Cache-Control") or headers.get("cache-control") or "")
        assert "no-store" in cache_control.lower(), cache_control
    finally:
        _revoke(admin_token, created["id"])


def test_the_token_is_never_returned_again(admin_token):
    _cleanup(admin_token, "QA Once")
    created, _ = _mint(admin_token, "QA Once")
    raw_token = created["token"]
    try:
        payload, _ = _list(admin_token)
        blob = json.dumps(payload)
        assert raw_token not in blob, "the list response echoed the raw token"
        for credential in payload["credentials"]:
            assert "token" not in credential
            assert "token_hash" not in credential
    finally:
        _revoke(admin_token, created["id"])


def test_the_list_model_has_no_token_field():
    """Structural, not behavioural: FastAPI serializes the declared model, so a
    careless handler edit cannot start leaking the token from the list route."""
    from backend.routes.webhook_credentials import WebhookCredentialOut
    fields = set(WebhookCredentialOut.model_fields)
    assert "token" not in fields
    assert "token_hash" not in fields
    assert "fingerprint" in fields


def test_duplicate_names_are_refused_and_create_nothing(admin_token):
    _cleanup(admin_token, "QA Dup")
    created, _ = _mint(admin_token, "QA Dup Name")
    try:
        before, _ = _list(admin_token)
        for variant in ("QA Dup Name", "qa dup name", "  QA   Dup   Name  "):
            status, raw, _h = _http("POST", API, {"name": variant}, token=admin_token)
            assert status == 409, f"{variant!r} should collide, got {status}: {raw[:200]}"
        after, _ = _list(admin_token)
        assert after["count"] == before["count"], "a refused mint still created a row"
    finally:
        _revoke(admin_token, created["id"])


# --------------------------------------------------------------------------
# revocation
# --------------------------------------------------------------------------

def test_revoking_stops_the_credential_working(admin_token):
    _cleanup(admin_token, "QA Revoke")
    created, _ = _mint(admin_token, "QA Revoke")
    raw_token = created["token"]

    status, _b, _h = _ingest(raw_token)
    assert status == 200, "precondition: the credential should work before revocation"

    revoke_status, revoke_raw, _h = _revoke(admin_token, created["id"])
    assert revoke_status == 200, revoke_raw[:200]

    # Polled, not asserted once: the cache TTL is real and per-worker, and a
    # single-shot assert here would misrepresent the guarantee the API states.
    deadline = time.time() + _ttl(admin_token) + 15
    while time.time() < deadline:
        status, _b, _h = _ingest(raw_token)
        if status == 401:
            return
        time.sleep(2)
    pytest.fail("revoked credential still authenticated past the cache TTL")


def test_a_revoked_credential_is_indistinguishable_from_a_wrong_one(admin_token):
    """No oracle. If these differed, a holder of a revoked token could tell
    'this was valid once' from 'this was never valid'."""
    _cleanup(admin_token, "QA Oracle")
    created, _ = _mint(admin_token, "QA Oracle")
    raw_token = created["token"]
    _revoke(admin_token, created["id"])

    deadline = time.time() + _ttl(admin_token) + 15
    revoked_status = None
    while time.time() < deadline:
        revoked_status, revoked_body, revoked_headers = _ingest(raw_token)
        if revoked_status == 401:
            break
        time.sleep(2)
    assert revoked_status == 401, "credential never became invalid"

    wrong_status, wrong_body, wrong_headers = _ingest("definitely-not-a-valid-token")
    assert revoked_status == wrong_status
    assert revoked_body == wrong_body, "the 401 body distinguishes revoked from wrong"

    def _challenge(headers):
        return (headers.get("WWW-Authenticate") or headers.get("www-authenticate") or "")
    assert _challenge(revoked_headers) == _challenge(wrong_headers)


def test_deleting_an_unknown_id_is_a_404(admin_token):
    status, _raw, _h = _revoke(admin_token, 99999999)
    assert status == 404


def test_deleting_the_issuing_admin_does_not_delete_the_credential(admin_token):
    """ondelete='SET NULL', not CASCADE. Cascading would mean that removing a
    departing employee's account silently blacks out every camera they
    provisioned — an outage discovered rather than a decision taken."""
    _cleanup(admin_token, "QA Orphan")

    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        from backend.auth.password import hash_password
        async with db_manager.get_session() as db:
            await db.execute(sa_text("DELETE FROM users WHERE username = :u"),
                             {"u": "qa_cred_issuer"})
            user_id = (await db.execute(sa_text(
                "INSERT INTO users (username, email, password_hash, role, is_active, "
                "can_use_chatbot, created_at) VALUES (:u, :e, :h, 'admin', true, false, now()) "
                "RETURNING id"),
                {"u": "qa_cred_issuer", "e": "qa_cred_issuer@example.com",
                 "h": hash_password("QaIssuer!2026")})).scalar()
            await db.execute(sa_text(
                "INSERT INTO webhook_credentials "
                "(token_hash, name, name_key, created_by_user_id, created_by_username, created_at) "
                "VALUES (:th, :n, :nk, :uid, :un, now())"),
                {"th": hashlib.sha256(b"qa-orphan-probe").hexdigest(),
                 "n": "QA Orphan Probe", "nk": "qa orphan probe",
                 "uid": user_id, "un": "qa_cred_issuer"})
            await db.commit()

            await db.execute(sa_text("DELETE FROM users WHERE id = :i"), {"i": user_id})
            await db.commit()

            row = (await db.execute(sa_text(
                "SELECT created_by_user_id, created_by_username FROM webhook_credentials "
                "WHERE name_key = 'qa orphan probe'"))).first()
            await db.execute(sa_text(
                "DELETE FROM webhook_credentials WHERE name_key = 'qa orphan probe'"))
            await db.commit()
            return row

    from conftest import run_on_shared_loop as run_async
    row = run_async(_run())
    assert row is not None, "the credential was deleted along with its issuer"
    assert row[0] is None, "created_by_user_id should be NULL, not dangling"
    assert row[1] == "qa_cred_issuer", "attribution should survive via the denormalized name"


# --------------------------------------------------------------------------
# access control
# --------------------------------------------------------------------------

def test_unauthenticated_callers_are_refused():
    for method, path, body in (("GET", API, None),
                               ("POST", API, {"name": "QA Anon"}),
                               ("DELETE", f"{API}/1", None)):
        status, _raw, _h = _http(method, path, body)
        assert status == 401, f"{method} {path} returned {status}, expected 401"


def test_non_admins_cannot_mint_list_or_revoke():
    async def _make_user():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        from backend.auth.password import hash_password
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(sa_text("DELETE FROM users WHERE username = :u"),
                             {"u": "qa_cred_viewer"})
            await db.execute(sa_text(
                "INSERT INTO users (username, email, password_hash, role, is_active, "
                "can_use_chatbot, created_at) "
                "VALUES (:u, :e, :h, 'analyzer', true, false, now())"),
                {"u": "qa_cred_viewer", "e": "qa_cred_viewer@example.com",
                 "h": hash_password("QaViewer!2026")})
            await db.commit()

    from conftest import run_on_shared_loop as run_async
    run_async(_make_user())

    status, raw, _h = _http("POST", "/api/auth/login",
                            {"username": "qa_cred_viewer", "password": "QaViewer!2026"},
                            csrf=False)
    assert status == 200, raw[:200]
    viewer = _json(raw)["access_token"]
    assert viewer, "non-admin login returned no bearer token"

    for method, path, body in (("GET", API, None),
                               ("POST", API, {"name": "QA Sneaky"}),
                               ("DELETE", f"{API}/1", None)):
        code, response, _h = _http(method, path, body, token=viewer)
        assert code == 403, f"{method} {path} returned {code} for a non-admin"


def test_cookie_clients_need_the_csrf_header(admin_token):
    """Bearer clients are exempt; this pins the cookie policy, matching the
    other admin surfaces via require_upload_csrf."""
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login = urllib.request.Request(
        BASE + "/api/auth/login",
        data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
        method="POST")
    login.add_header("Content-Type", "application/json")
    with opener.open(login, timeout=30) as response:
        assert response.status == 200

    request = urllib.request.Request(
        BASE + API, data=json.dumps({"name": "QA NoCsrf"}).encode(), method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with opener.open(request, timeout=30) as response:
            pytest.fail(f"cookie mint without CSRF header succeeded: {response.status}")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403, exc.code


# --------------------------------------------------------------------------
# coexistence with the environment key, and cost
# --------------------------------------------------------------------------

def test_the_environment_key_still_works_alongside_issued_credentials(admin_token):
    """Break-glass. A database outage must not lock every camera out, which is
    also why config_guard still requires an env key in production."""
    from config import settings
    from backend.security import webhook_auth

    keys = webhook_auth.parse_keys(getattr(settings, "WEBHOOK_API_KEYS", ""))
    if not keys:
        pytest.skip("no WEBHOOK_API_KEYS configured in this container")

    _cleanup(admin_token, "QA Coexist")
    created, _ = _mint(admin_token, "QA Coexist")
    try:
        assert _ingest(keys[0])[0] == 200, "env key stopped working"
        assert _ingest(created["token"])[0] == 200, "issued credential stopped working"
    finally:
        _revoke(admin_token, created["id"])


def test_verification_does_not_query_the_database_per_frame(admin_token):
    """The property the whole cache exists for. Without a test it regresses
    silently: everything still works, just with a query per ingested frame."""
    from backend.security import webhook_credentials

    calls = {"n": 0}

    class _FakeResult:
        def fetchall(self):
            return []

    class _FakeSession:
        async def execute(self, *a, **k):
            calls["n"] += 1
            return _FakeResult()

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import db_connection
    original = db_connection.db_manager.get_session
    webhook_credentials.reset_for_tests()
    db_connection.db_manager.get_session = lambda *a, **k: _FakeSession()
    try:
        from conftest import run_on_shared_loop as run_async

        async def _many():
            for _ in range(20):
                await webhook_credentials.ensure_fresh()

        run_async(_many())
    finally:
        db_connection.db_manager.get_session = original
        webhook_credentials.reset_for_tests()

    assert calls["n"] <= 2, (
        f"{calls['n']} queries for 20 checks — the snapshot cache is not holding")


def test_a_failed_refresh_never_widens_access():
    """Fail closed. A database error must keep the previous snapshot, never
    degrade to accepting everything."""
    from backend.security import webhook_credentials

    webhook_credentials.reset_for_tests()

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("database is down")

        async def __aexit__(self, *a):
            return False

    import db_connection
    original = db_connection.db_manager.get_session
    db_connection.db_manager.get_session = lambda *a, **k: _Boom()
    try:
        from conftest import run_on_shared_loop as run_async
        run_async(webhook_credentials.ensure_fresh())
        assert webhook_credentials.any_cached() is False
        # No credential set means no match — never a blanket accept.
        assert webhook_credentials.match("anything-at-all", object()) is None
    finally:
        db_connection.db_manager.get_session = original
        webhook_credentials.reset_for_tests()


def test_the_cache_ttl_is_not_runtime_editable():
    """An admin token that could set this to 86400 could make revocation take a
    day — the same class of hole as flipping WEBHOOK_AUTH_MODE to off."""
    from backend.core import runtime_settings
    assert runtime_settings.apply_to_runtime(
        "WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS", "86400") is False


def test_credential_names_never_become_metric_labels(admin_token):
    """prometheus_client never reclaims a series within a process, so a name
    label would grow monotonically as credentials are issued and revoked."""
    _cleanup(admin_token, "QA MetricProbe")
    created, _ = _mint(admin_token, "QA MetricProbe Unique")
    try:
        _ingest(created["token"])
        request = urllib.request.Request(BASE + "/metrics", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                pytest.skip("/metrics is not exposed unauthenticated here")
            raise
        assert "QA MetricProbe Unique" not in body
        for line in body.splitlines():
            if line.startswith("fr_webhook_auth_source_total"):
                assert 'source="' in line
    finally:
        _revoke(admin_token, created["id"])
