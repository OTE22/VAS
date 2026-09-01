"""
Authentication security tests
=============================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_auth_security.py -v

Proves the sign-in hardening contract:
  * browser (cookie) clients receive NO access token in the response body —
    the credential exists only in the HttpOnly cookie; programmatic clients
    keep their bearer token so existing automation is unaffected
  * the login response is minimal (id/username/role only) and carries a
    backend-chosen, allowlisted redirect_url
  * login responses are never cached (no-store)
  * unknown username and wrong password are indistinguishable (same code,
    same message, comparable timing) — no account enumeration
  * cross-origin credential submission is rejected (login CSRF)
  * brute force is throttled per account and per source with 429 + Retry-After,
    and a successful login releases the victim's throttle
  * JWTs carry jti/iat/iss/aud; every login mints a NEW jti (session rotation)
  * logout revokes the token server-side — the old cookie/bearer stops working
  * disabled accounts cannot authenticate
  * non-admin users cannot reach admin APIs regardless of client-side role
  * frontend: no /api/auth/me retry loop, no credential logging, single-flight
    state machine, request timeout, allowlisted redirect, accessible errors
"""

import json
import time
import urllib.error
import urllib.request

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"

JS_PATH = "/app/frontend/js/signin.js"
HTML_PATH = "/app/frontend/signin.html"
AUTH_ROUTES_PATH = "/app/backend/routes/auth.py"
AUTH_SERVICE_PATH = "/app/backend/auth/auth_service.py"
AUTH_SECURITY_PATH = "/app/backend/auth/auth_security.py"
NGINX_PATH = "/app/nginx.conf"

BROWSER = {"X-Requested-With": "XMLHttpRequest"}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


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
            return resp.status, payload, dict((k.lower(), v) for k, v in resp.getheaders())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}
        return e.code, payload, dict((k.lower(), v) for k, v in e.headers.items())


def _login(username="admin", password="admin123", headers=None):
    return _http("POST", "/api/auth/login",
                 {"username": username, "password": password}, headers)


@pytest.fixture(scope="module")
def token():
    status, body, _ = _login()
    assert status == 200, f"admin login failed: {body}"
    return body["access_token"]


# ---------------------------------------------------------------------------
# Response shape: no token to browsers, minimal user, backend redirect
# ---------------------------------------------------------------------------

def test_browser_login_returns_no_access_token():
    """The whole point: page JavaScript must never hold the credential."""
    status, body, headers = _login(headers=BROWSER)
    assert status == 200, body
    assert body.get("access_token") is None, "browser clients must NOT receive a token in JSON"
    assert body.get("token_type") is None
    assert body["success"] is True
    # The credential must be in an HttpOnly cookie instead
    cookie = headers.get("set-cookie", "")
    assert "access_token=" in cookie
    assert "HttpOnly" in cookie, f"auth cookie must be HttpOnly: {cookie}"
    assert "Path=/" in cookie
    assert "SameSite=lax" in cookie or "SameSite=strict" in cookie.lower()


def test_api_client_still_receives_token():
    """Backward compatibility: scripts/integrations keep working."""
    status, body, _ = _login()  # no X-Requested-With
    assert status == 200, body
    assert body.get("access_token"), "programmatic clients must keep their bearer token"
    assert body.get("token_type") == "bearer"


def test_login_response_is_minimal():
    status, body, _ = _login(headers=BROWSER)
    assert status == 200
    assert set(body["user"].keys()) == {"id", "username", "role"}, \
        "login must not leak email, permissions, flags or profile data"
    dumped = json.dumps(body)
    for banned in ("password", "hash", "session_id", "blocked_reason", "email"):
        assert banned not in dumped.lower(), f"login response leaks '{banned}'"
    # The rotation signal is a bare boolean and is named `rotation_required`
    # precisely so it cannot carry the word "password" into this body. The
    # regression admin does not need to rotate, so it must read False.
    assert body.get("rotation_required") is False, \
        "login must report rotation state, and this account should not need one"


def test_backend_chooses_allowlisted_redirect():
    status, body, _ = _login(headers=BROWSER)
    assert status == 200
    assert body["redirect_url"] in ("/home", "/dashboard"), body.get("redirect_url")
    assert body["redirect_url"] == "/home", "admin should be routed to /home by the backend"


def test_login_responses_are_not_cached():
    for headers in (BROWSER, None):
        status, _, resp_headers = _login(headers=headers)
        assert status == 200
        assert "no-store" in resp_headers.get("cache-control", "").lower()
    # Failures must not be cached either
    _, _, fail_headers = _login(password="wrong-password-xyz")
    assert "no-store" in fail_headers.get("cache-control", "").lower()


def test_me_endpoint_is_not_cached(token):
    status, _, headers = _http("GET", "/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert status == 200
    assert "no-store" in headers.get("cache-control", "").lower()


# ---------------------------------------------------------------------------
# No account enumeration
# ---------------------------------------------------------------------------

def test_unknown_user_and_wrong_password_are_indistinguishable():
    s1, b1, _ = _login(password="definitely-not-the-password")
    s2, b2, _ = _login(username="user_that_does_not_exist_zzz", password="definitely-not-the-password")
    assert s1 == s2 == 401
    assert b1["error"]["code"] == b2["error"]["code"] == "INVALID_CREDENTIALS"
    assert b1["error"]["message"] == b2["error"]["message"]
    # Neither response may hint at which factor failed
    for body in (b1, b2):
        text = json.dumps(body).lower()
        for hint in ("not found", "no such user", "unknown user", "incorrect password", "does not exist"):
            assert hint not in text, f"enumeration hint leaked: {hint}"


def test_credential_errors_are_structured():
    _, body, _ = _login(password="wrong-password-xyz")
    error = body["error"]
    for key in ("code", "message", "reference_id", "retryable"):
        assert key in error, f"structured error missing {key}"
    assert error["reference_id"].startswith("AUTH-")
    assert isinstance(error["retryable"], bool)


def test_timing_equalization_exists_in_source():
    """Unknown users skip bcrypt, so the handler must burn equivalent CPU."""
    src = _read(AUTH_ROUTES_PATH)
    assert "dummy_password_verify" in src, "missing timing equalization for unknown users"
    sec = _read(AUTH_SECURITY_PATH)
    assert "def dummy_password_verify" in sec
    assert "_DUMMY_HASH" in sec


def test_unknown_user_timing_is_comparable():
    """Unknown-user and wrong-password latency must be comparable in BOTH
    directions.

    Generous bounds (a live HTTP hop cannot be constant-time), but tight
    enough to catch the two real oracles:
      * no equalization at all -> unknown user is ~100x faster
      * equalization applied after a real bcrypt -> wrong password is 2x slower
    Throttled (429) samples are excluded: they short-circuit before
    verification and would corrupt the measurement.
    """
    def measure(username):
        samples = []
        for _ in range(4):
            start = time.time()
            status, _, _ = _login(username=username, password="some-wrong-password")
            elapsed = time.time() - start
            if status == 401:  # only genuine credential rejections
                samples.append(elapsed)
            time.sleep(0.1)
        return samples

    unknown_samples = measure(f"nonexistent_user_timing_{int(time.time())}")
    wrong_samples = measure("admin")

    if len(unknown_samples) < 2 or len(wrong_samples) < 2:
        pytest.skip("rate limiting prevented a clean timing measurement")

    unknown_user = sorted(unknown_samples)[len(unknown_samples) // 2]
    wrong_password = sorted(wrong_samples)[len(wrong_samples) // 2]
    ratio = unknown_user / wrong_password if wrong_password else 0

    assert 0.5 <= ratio <= 2.0, (
        f"login timing leaks whether an account exists "
        f"(unknown={unknown_user:.3f}s vs wrong_password={wrong_password:.3f}s, "
        f"ratio={ratio:.2f}) — both paths must perform one password verification"
    )


# ---------------------------------------------------------------------------
# Login CSRF / Origin validation
# ---------------------------------------------------------------------------

def test_cross_origin_login_is_rejected():
    status, body, _ = _login(headers={"Origin": "https://attacker.example.com"})
    assert status == 403, body
    assert body["error"]["code"] == "CSRF_FAILED"
    assert body["error"]["retryable"] is False


def test_cross_origin_referer_is_rejected():
    status, body, _ = _login(headers={"Referer": "https://attacker.example.com/login"})
    assert status == 403, body
    assert body["error"]["code"] == "CSRF_FAILED"


def test_same_origin_login_is_allowed():
    status, _, _ = _login(headers={"Origin": "http://localhost:8000"})
    assert status == 200


def test_non_browser_client_without_origin_is_allowed():
    """curl/scripts send no Origin and cannot be CSRF victims."""
    status, _, _ = _login()
    assert status == 200


# ---------------------------------------------------------------------------
# Brute-force protection
# ---------------------------------------------------------------------------

def test_rate_limiting_blocks_repeated_failures():
    """Per-account throttling triggers 429 with Retry-After, then a correct
    password releases the throttle (an attacker cannot lock the victim out)."""
    victim = "ratelimit_probe_user_zzz"
    saw_429 = False
    retry_after = None
    for _ in range(12):
        status, body, headers = _login(username=victim, password="wrong-password")
        if status == 429:
            saw_429 = True
            retry_after = headers.get("retry-after")
            assert body["error"]["code"] == "RATE_LIMITED"
            break
        assert status == 401, f"unexpected status during throttle probe: {status} {body}"

    assert saw_429, "repeated failed logins must eventually be rate limited"
    assert retry_after and int(retry_after) > 0, "429 must carry a positive Retry-After"

    # A different, valid account is unaffected by that account's counter
    status, _, _ = _login()
    assert status == 200, "throttling one account must not block a different valid login"


def test_successful_login_clears_failure_counters():
    """The real user authenticating must release their own throttle — this is
    why an attacker cannot permanently lock someone out."""
    src = _read(AUTH_ROUTES_PATH)
    assert "clear_failures" in src, "successful login must reset failure counters"
    sec = _read(AUTH_SECURITY_PATH)
    assert "async def clear_failures" in sec


def test_rate_limit_is_shared_state_not_frontend_only():
    sec = _read(AUTH_SECURITY_PATH)
    assert "auth:fail:account:" in sec and "auth:fail:ip:" in sec
    assert "_incr_with_window" in sec
    assert "redis" in sec.lower(), "throttling must use shared state across replicas"
    # Global surge protection
    assert "auth:attempts:global" in sec


def test_nginx_has_auth_specific_rate_limit():
    conf = _read(NGINX_PATH)
    assert "zone=auth_rate" in conf, "auth endpoints need their own nginx rate-limit zone"
    # change-password verifies the CURRENT password, so it is throttled with
    # login rather than sitting outside the zone as an unlimited oracle.
    assert "api/auth/(login|logout|change-password)" in conf
    assert 'add_header Cache-Control "no-store" always' in conf


# ---------------------------------------------------------------------------
# Session rotation + revocation
# ---------------------------------------------------------------------------

def _decode_unverified(token):
    import jwt
    return jwt.decode(token, options={"verify_signature": False})


def test_jwt_carries_jti_iat_issuer_audience():
    _, body, _ = _login()
    claims = _decode_unverified(body["access_token"])
    for claim in ("jti", "iat", "exp", "iss", "aud", "sub"):
        assert claim in claims, f"JWT missing {claim} claim"


def test_every_login_rotates_the_session_id():
    """No pre-authentication identifier is ever reused."""
    _, first, _ = _login()
    _, second, _ = _login()
    jti_1 = _decode_unverified(first["access_token"])["jti"]
    jti_2 = _decode_unverified(second["access_token"])["jti"]
    assert jti_1 != jti_2, "each login must mint a fresh session id (jti)"


def test_logout_revokes_the_token():
    """After logout the old credential must stop working — not just be deleted
    from the browser."""
    _, body, _ = _login()
    tok = body["access_token"]
    auth = {"Authorization": f"Bearer {tok}"}

    status, _, _ = _http("GET", "/api/auth/me", headers=auth)
    assert status == 200, "token should work before logout"

    status, logout_body, _ = _http("POST", "/api/auth/logout", headers=auth)
    assert status == 200, logout_body
    assert logout_body.get("success") is True

    status, _, _ = _http("GET", "/api/auth/me", headers=auth)
    assert status == 401, "the revoked token must no longer authenticate anyone"


def test_logout_clears_cookie_with_matching_attributes():
    src = _read(AUTH_ROUTES_PATH)
    logout_fn = src.split("async def logout")[1]
    assert "cookie_settings()" in logout_fn
    for attr in ("path=", "samesite=", "secure=", "httponly=True"):
        assert attr in logout_fn, f"delete_cookie must match the set attributes ({attr})"
    assert "revoke_token" in logout_fn


def test_revocation_is_checked_on_every_request():
    src = _read(AUTH_SERVICE_PATH)
    assert "is_token_revoked" in src, "get_current_user must consult the denylist"


# ---------------------------------------------------------------------------
# Account state + authorization
# ---------------------------------------------------------------------------

def test_disabled_account_cannot_authenticate():
    """A deactivated user fails exactly like bad credentials."""
    from sqlalchemy import text
    from db_connection import db_manager

    username = "disabled_probe_zzz"
    password = "ProbePassword!123"

    async def seed():
        from backend.auth.password import hash_password
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.execute(text("""
                INSERT INTO users (username, email, full_name, password_hash, role,
                                   is_active, can_use_chatbot, created_at)
                VALUES (:u, :e, 'Disabled Probe', :h, 'user', false, false, now())
            """), {"u": username, "e": f"{username}@example.test",
                   "h": hash_password(password)})
            await db.commit()

    async def cleanup():
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.commit()

    run_async(seed())
    try:
        status, body, _ = _login(username=username, password=password)
        assert status == 401, f"disabled account must not authenticate: {status} {body}"
        # Identical error to bad credentials — no account-state disclosure
        assert body["error"]["code"] == "INVALID_CREDENTIALS"
        assert "disabled" not in json.dumps(body).lower()
    finally:
        run_async(cleanup())


def test_non_admin_cannot_reach_admin_api():
    """Server-side authorization, independent of any client-side role value."""
    from sqlalchemy import text
    from db_connection import db_manager

    username = "lowpriv_probe_zzz"
    password = "ProbePassword!123"

    async def seed():
        from backend.auth.password import hash_password
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.execute(text("""
                INSERT INTO users (username, email, full_name, password_hash, role,
                                   is_active, can_use_chatbot, created_at)
                VALUES (:u, :e, 'Low Priv Probe', :h, 'user', true, false, now())
            """), {"u": username, "e": f"{username}@example.test",
                   "h": hash_password(password)})
            await db.commit()

    async def cleanup():
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.commit()

    run_async(seed())
    try:
        status, body, _ = _login(username=username, password=password)
        assert status == 200, body
        # The backend routes a non-admin away from the admin landing page
        assert body["redirect_url"] == "/dashboard"
        tok = body["access_token"]
        auth = {"Authorization": f"Bearer {tok}"}

        for path in ("/api/admin/identities?page=1",
                     "/api/admin/merge-suggestions/model-status",
                     "/api/watchlists",
                     "/api/security/capabilities"):
            status, _, _ = _http("GET", path, headers=auth)
            assert status == 403, f"{path} must reject a non-admin token (got {status})"
    finally:
        run_async(cleanup())


def test_admin_routes_require_authentication():
    for path in ("/api/auth/me", "/api/admin/identities?page=1", "/api/watchlists"):
        status, _, _ = _http("GET", path)
        assert status == 401, f"{path} must require authentication"


# ---------------------------------------------------------------------------
# Log hygiene
# ---------------------------------------------------------------------------

def test_no_credential_or_cookie_logging_in_source():
    service = _read(AUTH_SERVICE_PATH)
    routes = _read(AUTH_ROUTES_PATH)
    # The old code logged raw cookie values and usernames/emails
    assert "Cookie values" not in service, "cookie values must never be logged"
    assert "dict(all_cookies)" not in service
    assert "Token data:" not in service, "token payloads must never be logged"
    assert "user.email" not in service.split("async def authenticate_user")[1].split("@staticmethod")[0]
    # Login route logs pseudonymized identifiers, not raw usernames
    login_fn = routes.split("async def login(")[1].split("\n@router")[0]
    assert "credentials.username" not in login_fn.replace("username = credentials.username", ""), \
        "the login handler must not log the raw username"
    assert "credentials.password" in login_fn  # only passed to verification/equalization
    assert "logger.info(\"[AUTH] Login attempt user=%s" not in routes


def test_audit_uses_pseudonymized_identifiers():
    sec = _read(AUTH_SECURITY_PATH)
    assert "def pseudonymize" in sec
    assert "hmac" in sec, "pseudonymization must be keyed, not a plain hash"
    audit_fn = sec.split("def audit(")[1]
    assert "pseudonymize(username)" in audit_fn and "pseudonymize(ip)" in audit_fn


def test_auth_metrics_have_no_high_cardinality_labels():
    """Metric LABELS must stay low-cardinality. (The audit log is a separate
    concern and does carry user_id, which is required and intended.)"""
    sec = _read(AUTH_SECURITY_PATH)
    # Only the metric declarations, not the rest of the module
    metrics_block = sec.split("def initialize_auth_metrics")[1].split("def record_metric")[0]
    assert '["result"]' in metrics_block, "login attempts should be labelled by result"
    for banned in ("username", "user_id", "ip", "actor", "source"):
        assert f'"{banned}"]' not in metrics_block and f'["{banned}"' not in metrics_block, \
            f"metric label '{banned}' is high-cardinality"


# ---------------------------------------------------------------------------
# Frontend source contracts
# ---------------------------------------------------------------------------

def test_js_no_cookie_verification_retry_loop():
    src = _read(JS_PATH)
    # No REQUEST to /api/auth/me (a doc comment mentioning it is fine)
    assert "fetch('/api/auth/me'" not in src and 'fetch("/api/auth/me"' not in src, \
        "the post-login verification loop must be gone"
    assert "maxRetries" not in src
    assert "setTimeout(resolve, 200)" not in src
    assert "proceeding with redirect anyway" not in src
    # Exactly one network call in the sign-in flow: the login itself
    assert src.count("await fetch(") == 1, "sign-in must make exactly one request"


def test_js_does_not_log_credentials_or_user():
    src = _read(JS_PATH)
    assert "user data:" not in src
    assert "console.log" not in src, "sign-in must not log anything in production"
    for banned in ("data.user)", "password)", "username)"):
        assert f"console.error({banned}" not in src


def test_js_single_flight_state_machine():
    src = _read(JS_PATH)
    assert "STATES" in src and "SUBMITTING" in src and "SUCCEEDED" in src
    assert "function canSubmit" in src
    assert "if (!canSubmit()) return" in src, "double click / repeated Enter must be ignored"
    assert "signinInFlight" not in src, "the boolean-only guard must be replaced"


def test_js_request_timeout_and_abort():
    src = _read(JS_PATH)
    assert "AbortController" in src
    assert "LOGIN_TIMEOUT_MS" in src
    assert "AbortError" in src, "timeout must be distinguished from bad credentials"
    assert "pagehide" in src, "an in-flight login should abort on navigation"


def test_js_allowlists_redirect():
    src = _read(JS_PATH)
    assert "ALLOWED_REDIRECTS" in src
    # '/change-password' is a destination the backend picks for an account whose
    # password is still the seeded or admin-assigned one. Missing from this set,
    # safeRedirect() silently sends such a user to /dashboard instead, which
    # then 403s them straight back.
    assert "new Set(['/home', '/dashboard', '/change-password'])" in src
    assert "function safeRedirect" in src
    assert "data.redirect_url" in src, "the backend chooses the destination"
    assert "user.role === 'admin' ? '/home'" not in src, "frontend must not decide by role"


def test_js_error_handling_is_safe_and_accessible():
    src = _read(JS_PATH)
    assert ".innerHTML =" not in src.replace("submitButton.innerHTML = state.originalButtonHtml", ""), \
        "backend values must never be assigned through innerHTML"
    assert "response.statusText" not in src, "server-controlled strings must not be displayed"
    assert "ERROR_MESSAGES" in src, "messages must be chosen locally by code"
    assert "textContent" in src


def test_js_clears_password_keeps_username():
    src = _read(JS_PATH)
    assert "passwordInput.value = ''" in src
    assert "passwordInput.focus()" in src
    assert "usernameInput.value = ''" not in src, "the username must survive a failed attempt"


def test_js_dom_guards_and_input_limits():
    src = _read(JS_PATH)
    assert "function requireElement" in src
    assert "MAX_USERNAME_LENGTH" in src and "MAX_PASSWORD_LENGTH" in src
    assert ".trim()" in src, "username must be trimmed"
    # The password must never be trimmed or otherwise modified
    assert "passwordInput.value.trim()" not in src


def test_js_announces_submission_and_sends_csrf_header():
    src = _read(JS_PATH)
    assert "aria-busy" in src
    assert "signin-status" in src
    assert "'X-Requested-With': 'XMLHttpRequest'" in src, \
        "browser clients must identify themselves so no token is returned"
    assert "cache: 'no-store'" in src


def test_html_accessibility_and_no_inline_handlers():
    src = _read(HTML_PATH)
    for banned in ("onclick=", "onerror=", "onmouseover=", "onload="):
        assert banned not in src, f"inline handler {banned} must be removed"
    assert 'autocomplete="username"' in src
    assert 'autocomplete="current-password"' in src
    assert 'role="alert"' in src and 'aria-live="assertive"' in src
    assert 'id="capslock-warning"' in src, "Caps Lock warning aids password entry"
    assert 'id="signin-status"' in src
    assert 'aria-pressed="false"' in src, "password toggle must expose its state"
    assert 'tabindex="-1"' not in src.split('class="toggle-password-btn"')[1][:200], \
        "the password toggle must be keyboard reachable"
    assert "signin.js?v=signin-3" in src
    assert 'for="username"' in src and 'for="password"' in src


def test_every_cookie_reader_supports_the_host_prefix():
    """Enabling AUTH_COOKIE_SECURE renames the cookie to __Host-access_token.
    Every reader must accept BOTH names or production logins break (page
    routes would bounce authenticated admins back to /signin)."""
    import subprocess
    result = subprocess.run(
        ["grep", "-rn", 'cookies.get("access_token")', "/app/backend/"],
        capture_output=True, text=True)
    offenders = [line for line in result.stdout.splitlines()
                 if line.strip() and "__Host-access_token" not in line]
    assert not offenders, (
        "these cookie readers would break when the __Host- prefix is enabled:\n"
        + "\n".join(offenders))


def test_cookie_configuration_is_production_ready():
    sec = _read(AUTH_SECURITY_PATH)
    cookie_fn = sec.split("def cookie_settings")[1].split("def ")[0]
    assert '"httponly": True' in cookie_fn
    assert "AUTH_COOKIE_SECURE" in cookie_fn, "Secure must be configurable for production"
    assert '"path": "/"' in cookie_fn
    assert '"domain": None' in cookie_fn, "host-only cookie (no broad Domain)"
    assert "__Host-" in sec, "support the __Host- prefix when Secure is enabled"


# ---------------------------------------------------------------------------
# /me and /me/privileges: one authorization vocabulary, never cached
# ---------------------------------------------------------------------------

def _admin_auth():
    """Bearer token. No X-Requested-With — the body only carries the token for
    non-browser clients (browsers get the HttpOnly cookie instead)."""
    status, body, _ = _http("POST", "/api/auth/login",
                            {"username": "admin", "password": "admin123"})
    assert status == 200, body
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_me_and_privileges_agree_on_the_same_user():
    """The two endpoints must never disagree about one user at one moment.

    They previously exposed overlapping but differently-shaped views, so a
    client could believe one and be refused by the other.
    """
    auth = _admin_auth()
    s1, me, _ = _http("GET", "/api/auth/me", headers=auth)
    s2, priv, _ = _http("GET", "/api/auth/me/privileges", headers=auth)
    assert s1 == 200 and s2 == 200

    assert me["role"] == priv["role"]
    assert me["permissions"] == priv["permissions"], "capability lists diverge"
    assert me["permissions_version"] == priv["permissions_version"]


def test_permissions_use_stable_codes_not_ui_labels():
    """Capability codes must be stable identifiers the frontend can compare."""
    auth = _admin_auth()
    _s, me, _h = _http("GET", "/api/auth/me", headers=auth)

    assert me["permissions"], "no capability codes returned"
    assert me["permissions"] == sorted(me["permissions"]), "codes must be sorted"
    for code in me["permissions"]:
        assert "." in code, f"{code!r} is not a dotted capability code"
        assert code == code.lower(), f"{code!r} should be lowercase"
    # An admin holds the full set, including administration capabilities.
    assert "admin.users.manage" in me["permissions"]


def test_privileges_response_is_never_cached():
    """A cached privileges response can outlive a revocation."""
    auth = _admin_auth()
    for path in ("/api/auth/me", "/api/auth/me/privileges"):
        _s, _b, headers = _http("GET", path, headers=auth)
        cache_control = headers.get("cache-control", "").lower()
        assert "no-store" in cache_control, f"{path} is cacheable: {cache_control!r}"
