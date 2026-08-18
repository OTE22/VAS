"""Pipeline ingest authentication, and the debug-image path traversal.

The webhook shipped with no credential check of any kind — no dependency, no
key, no allowlist — while accepting frames that become identities, embeddings
and stored face images. nginx rate-limits it; nothing authenticated it. The
debug-image endpoints were also unauthenticated AND traversable: they anchored
their containment check to a directory the caller could influence.

Two layers here, mirroring test_config_guard.py:

  * pure, in-process checks against a SimpleNamespace — no container state;
  * HTTP against the live app, reading the key from the same settings object
    gunicorn was started with, so no secret is written into this file.
"""

import itertools
import json
import os
import time
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"


def _key():
    from config import settings
    from backend.security import webhook_auth

    keys = webhook_auth.parse_keys(getattr(settings, "WEBHOOK_API_KEYS", ""))
    if not keys:
        pytest.skip("no WEBHOOK_API_KEYS configured in this container")
    return keys[0]


def _post(path, *, key=None, header=None, body=None, timeout=30):
    payload = json.dumps(body if body is not None else {}).encode()
    request = urllib.request.Request(BASE + path, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    if key:
        request.add_header(header or "X-Webhook-Key", key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _get(path, *, key=None, token=None, timeout=30):
    request = urllib.request.Request(BASE + path, method="GET")
    if key:
        request.add_header("X-Webhook-Key", key)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture(scope="module")
def admin_token():
    request = urllib.request.Request(
        BASE + "/api/auth/login",
        data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
        method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())["access_token"]
    except Exception as exc:                                   # pragma: no cover
        pytest.skip(f"cannot obtain an admin token: {exc}")


# ---------------------------------------------------------------------------
# Key handling (pure)
# ---------------------------------------------------------------------------

def test_parse_keys_handles_real_world_values():
    from backend.security.webhook_auth import parse_keys

    assert parse_keys("") == ()
    assert parse_keys(None) == ()
    assert parse_keys("a") == ("a",)
    assert parse_keys(" a , b ") == ("a", "b")
    assert parse_keys("a,,b,") == ("a", "b")
    # Deduplicated, order preserved — rotation appends, and appending the same
    # key twice must not create two entries.
    assert parse_keys("a,b,a") == ("a", "b")


def test_verify_key_accepts_every_key_in_the_set():
    """Multiple keys ARE the rotation story: both must work simultaneously."""
    from types import SimpleNamespace
    from backend.security.webhook_auth import verify_key

    cfg = SimpleNamespace(WEBHOOK_API_KEYS="old-key-value,new-key-value")
    assert verify_key("old-key-value", cfg) is True
    assert verify_key("new-key-value", cfg) is True


def test_verify_key_rejects_prefixes_supersets_and_empties():
    """The cases a truncated or startswith comparison would wave through."""
    from types import SimpleNamespace
    from backend.security.webhook_auth import verify_key

    cfg = SimpleNamespace(WEBHOOK_API_KEYS="correct-horse-battery-staple")
    for wrong in ("correct-horse", "correct-horse-battery-staple-extra",
                  "", None, " ", "CORRECT-HORSE-BATTERY-STAPLE"):
        assert verify_key(wrong, cfg) is False, wrong
    assert verify_key("correct-horse-battery-staple", cfg) is True


def test_verify_key_survives_hostile_input():
    """Arbitrary client bytes must not raise; hashing makes that safe."""
    from types import SimpleNamespace
    from backend.security.webhook_auth import verify_key

    cfg = SimpleNamespace(WEBHOOK_API_KEYS="a-real-key")
    assert verify_key("\udcff\udcfe" + "ünïcödé", cfg) is False
    assert verify_key("A" * 10_000_000, cfg) is False


def test_no_keys_configured_never_verifies():
    """An empty key set must not turn into 'everything matches'."""
    from types import SimpleNamespace
    from backend.security.webhook_auth import keys_configured, verify_key

    cfg = SimpleNamespace(WEBHOOK_API_KEYS="")
    assert keys_configured(cfg) is False
    assert verify_key("anything", cfg) is False
    assert verify_key("", cfg) is False


def test_auth_mode_normalizes_and_never_defaults_permissive():
    from types import SimpleNamespace
    from backend.security.webhook_auth import (MODE_ENFORCE, MODE_LOG_ONLY,
                                               MODE_OFF, auth_mode)

    assert auth_mode(SimpleNamespace(WEBHOOK_AUTH_MODE=" ENFORCE ")) == MODE_ENFORCE
    assert auth_mode(SimpleNamespace(WEBHOOK_AUTH_MODE="Log_Only")) == MODE_LOG_ONLY
    assert auth_mode(SimpleNamespace(WEBHOOK_AUTH_MODE="off")) == MODE_OFF
    # A typo resolves to the STRICTEST mode, never to off.
    assert auth_mode(SimpleNamespace(WEBHOOK_AUTH_MODE="enfroce")) == MODE_ENFORCE
    assert auth_mode(SimpleNamespace()) == MODE_ENFORCE
    assert auth_mode(SimpleNamespace(WEBHOOK_AUTH_MODE="")) == MODE_ENFORCE


def test_the_key_is_never_read_from_the_query_string():
    """nginx, gunicorn and the access-log middleware all log the request line."""
    from types import SimpleNamespace

    from backend.security.webhook_auth import present_key

    class FakeRequest:
        headers = {}

    cfg = SimpleNamespace(WEBHOOK_AUTH_HEADER="X-Webhook-Key")
    assert present_key(FakeRequest(), cfg) is None

    source = open("/app/backend/security/webhook_auth.py", encoding="utf-8").read()
    assert "query_params" not in source, (
        "a credential read from the query string lands in three access logs")


def test_present_key_accepts_header_and_bearer():
    from types import SimpleNamespace
    from backend.security.webhook_auth import present_key

    cfg = SimpleNamespace(WEBHOOK_AUTH_HEADER="X-Webhook-Key")

    class WithHeader:
        headers = {"X-Webhook-Key": "  abc  "}

    class WithBearer:
        headers = {"authorization": "Bearer xyz"}

    class WithNeither:
        headers = {"authorization": "Basic nope"}

    assert present_key(WithHeader(), cfg) == "abc"
    assert present_key(WithBearer(), cfg) == "xyz"
    assert present_key(WithNeither(), cfg) is None


def test_malformed_authorization_headers_never_yield_a_credential():
    """Every shape that is not exactly `Bearer <token>` must produce None.

    Separate from test_present_key_accepts_header_and_bearer on purpose: that one
    covers the happy paths, this one pins the rejection surface so a future
    "cleanup" of the [:7] slice cannot quietly start accepting `Bearerabc`.
    """
    from types import SimpleNamespace
    from backend.security.webhook_auth import present_key

    cfg = SimpleNamespace(WEBHOOK_AUTH_HEADER="X-Webhook-Key")

    def presented(authorization):
        return present_key(
            SimpleNamespace(headers={"authorization": authorization}), cfg)

    for value in ("Bearer",          # no separator, no token
                  "Bearer ",         # separator, empty token
                  "Bearer    ",      # whitespace-only token
                  "Bearerabc",       # no separator at all
                  "Bearer\tabc",     # tab is not SP
                  "Token abc",       # wrong scheme
                  "Basic abc",       # wrong scheme
                  ""):               # empty header
        assert presented(value) is None, f"{value!r} produced a credential"


def test_rfc7235_leniency_is_deliberate_and_must_not_be_tightened():
    """`bearer`/`BEARER` and multiple spaces are VALID per RFC 7235 §2.1.

    Pinned because they look like bugs: auth-scheme is case-insensitive and the
    grammar is `auth-scheme 1*SP`. Rejecting either would 401 a conforming
    sender, which is far worse than accepting it.
    """
    from types import SimpleNamespace
    from backend.security.webhook_auth import present_key

    cfg = SimpleNamespace(WEBHOOK_AUTH_HEADER="X-Webhook-Key")

    def presented(authorization):
        return present_key(
            SimpleNamespace(headers={"authorization": authorization}), cfg)

    assert presented("bearer abc") == "abc"
    assert presented("BEARER abc") == "abc"
    assert presented("Bearer  abc") == "abc"
    # Everything after the scheme is the credential, so this fails closed at
    # verify_key rather than being silently truncated to a valid prefix.
    assert presented("Bearer abc extra") == "abc extra"


def test_authorization_as_the_configured_header_still_parses_bearer():
    """WEBHOOK_AUTH_HEADER=Authorization must not 401 every request.

    Reading the custom header first would return the whole "Bearer <token>"
    string as the credential, so no request could ever authenticate — and the
    rejection would be logged as `invalid`, pointing at the camera rather than
    at the configuration.
    """
    from types import SimpleNamespace
    from backend.security.webhook_auth import present_key, verify_key

    cfg = SimpleNamespace(WEBHOOK_AUTH_HEADER="Authorization",
                          WEBHOOK_API_KEYS="tok-abcdefghijklmnopqrstuvwxyz012345")
    request = SimpleNamespace(
        headers={"authorization": "Bearer tok-abcdefghijklmnopqrstuvwxyz012345"})

    assert present_key(request, cfg) == "tok-abcdefghijklmnopqrstuvwxyz012345"
    assert verify_key(present_key(request, cfg), cfg) is True


def test_webhook_auth_token_folds_into_the_key_set_and_is_redacted():
    """The alias must authenticate AND be redacted — the second half is the point.

    utils/logging.py harvests literal secret values from WEBHOOK_API_KEYS, and
    its field-name patterns are anchored per-prefix, so nothing matched
    "WEBHOOK_AUTH_TOKEN". Had the token been kept in its own field instead of
    folded, it would have been written to app.log in full on the first request
    that logged it.
    """
    import logging

    from config import Settings
    from backend.security.webhook_auth import keys_configured, parse_keys, verify_key
    from utils.logging import SensitiveDataFilter

    token = "fold-me-abcdefghijklmnopqrstuvwxyz0123456789"
    cfg = Settings(WEBHOOK_API_KEYS="", WEBHOOK_AUTH_TOKEN=token)

    assert token in parse_keys(cfg.WEBHOOK_API_KEYS), "token was not folded"
    assert keys_configured(cfg) is True
    assert verify_key(token, cfg) is True
    assert verify_key("not-the-token", cfg) is False

    # Folding must ADD to the set, never replace it — rotation depends on both
    # credentials being live at once.
    both = Settings(WEBHOOK_API_KEYS="existing-key-abcdefghijklmnop012345",
                    WEBHOOK_AUTH_TOKEN=token)
    assert verify_key(token, both) is True
    assert verify_key("existing-key-abcdefghijklmnop012345", both) is True

    # Redaction by FIELD NAME. The literal-value harvest reads the process
    # singleton, not this locally-built Settings, so a record naming the field is
    # the layer that has to hold here.
    record = logging.LogRecord("t", logging.INFO, "x", 1,
                               f"config WEBHOOK_AUTH_TOKEN={token} loaded", (), None)
    SensitiveDataFilter().filter(record)
    assert token not in record.getMessage()


def test_secrets_are_redacted_and_locked_out_of_runtime_edits():
    from backend.security.config_guard import SECURITY_CRITICAL_KEYS
    from backend.security.redaction import SECRET_SETTINGS

    for name in ("WEBHOOK_API_KEYS", "WEBHOOK_API_KEYS_FILE", "WEBHOOK_AUTH_MODE",
                 "WEBHOOK_AUTH_HEADER", "WEBHOOK_AUTH_INSECURE_ACK",
                 "WEBHOOK_AUTH_TOKEN", "WEBHOOK_AUTH_TOKEN_FILE"):
        assert name in SECURITY_CRITICAL_KEYS, (
            f"{name} is editable at runtime — an admin token could reopen the "
            "unauthenticated webhook that config_guard refuses to start with")
    for name in ("WEBHOOK_API_KEYS", "WEBHOOK_API_KEYS_FILE",
                 "WEBHOOK_AUTH_TOKEN", "WEBHOOK_AUTH_TOKEN_FILE"):
        assert name in SECRET_SETTINGS, f"{name} would be rendered in a report"


def test_the_guard_report_never_prints_a_key():
    from types import SimpleNamespace
    from backend.security.config_guard import collect_violations, format_report

    secret = "hunter2hunter2"
    report = format_report(collect_violations(SimpleNamespace(
        ENVIRONMENT="production", WEBHOOK_API_KEYS=secret,
        WEBHOOK_AUTH_MODE="enforce")))
    assert secret not in report, "the report leaked the ingest key"


# ---------------------------------------------------------------------------
# Route inventory — the anti-bypass check, behavioral rather than a grep
# ---------------------------------------------------------------------------

def _flatten_routes(entries, prefix="", _seen=None):
    """Every concrete route, whatever shape the FastAPI version uses.

    Older FastAPI flattened `include_router` into `app.routes`. Newer versions
    store an `_IncludedRouter` wrapper instead and resolve through it at request
    time, so walking `app.routes` for `.path` finds NOTHING — which made this
    suite report "no webhook routes found" after an unpinned FastAPI upgrade,
    while the running server was in fact enforcing correctly on every route.

    Recursing handles both, so the assertions below test the app rather than the
    framework's current internal representation.
    """
    _seen = _seen if _seen is not None else set()
    out = []
    for entry in entries or ():
        if id(entry) in _seen:
            continue
        _seen.add(id(entry))

        sub = getattr(entry, "original_router", None)
        if sub is None:
            sub = getattr(entry, "included_router", None)
        if sub is not None:
            context = getattr(entry, "include_context", None)
            out.extend(_flatten_routes(getattr(sub, "routes", []),
                                       prefix + (getattr(context, "prefix", "") or ""),
                                       _seen))
            continue

        path = getattr(entry, "path", None)
        if path is None:
            # A bare APIRouter or Mount that still carries children.
            children = getattr(entry, "routes", None)
            if children:
                out.extend(_flatten_routes(children, prefix, _seen))
            continue
        out.append((prefix + path, entry))
    return out


def _webhook_routes():
    from backend.main import app

    out = []
    for path, route in _flatten_routes(app.routes):
        if not (path.startswith("/webhook") or path.startswith("/api/webhook")):
            continue
        dependant = getattr(route, "dependant", None)
        names = ([getattr(d.call, "__name__", "?") for d in dependant.dependencies]
                 if dependant is not None else [])
        out.append((path, tuple(sorted(getattr(route, "methods", []) or [])),
                    getattr(route, "name", ""), names))
    return out


def test_no_webhook_path_is_registered_twice():
    """A duplicate registration is how an unguarded route sneaks back in.

    `main.py` used to register POST /webhook/{pipeline_id} a second time,
    directly on the app and with no dependency. It was dead — the router's route
    matched first — but it would have become a live bypass the moment anyone
    moved the include_router call. This test failed before that block was
    deleted.
    """
    pairs = [(path, methods) for path, methods, _, _ in _webhook_routes()]
    duplicates = sorted({p for p in pairs if pairs.count(p) > 1})
    assert not duplicates, f"webhook route registered more than once: {duplicates}"


def test_every_webhook_route_is_guarded():
    routes = _webhook_routes()
    assert routes, "no webhook routes found — this test is looking in the wrong place"
    for path, methods, name, deps in routes:
        assert deps, f"{methods} {path} ({name}) has NO dependency at all"
        guarded = any(d in ("require_webhook_key", "role_checker", "get_current_user")
                      for d in deps)
        assert guarded, f"{methods} {path} ({name}) is unguarded: {deps}"


def test_debug_image_routes_require_a_user_not_a_camera_key():
    """A camera credential must not read back other cameras' face images."""
    for path, methods, name, deps in _webhook_routes():
        if "/images/" in path or path.endswith("/images/{pipeline_id}"):
            assert "require_webhook_key" not in deps, (
                f"{path} accepts the ingest key; any camera could read the gallery")
            assert any(d in ("role_checker", "get_current_user") for d in deps), (
                f"{path} is not behind user authentication: {deps}")


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------

def test_pipeline_id_cannot_escape_the_debug_image_base():
    """The traversal, at its root.

    The old check was `filepath.resolve().relative_to(save_dir.resolve())` where
    save_dir was built from the raw pipeline_id — i.e. containment was asserted
    against the ALREADY-TRAVERSED directory, so it always passed. Anchoring to a
    fixed base is what makes these unreachable.
    """
    from fastapi import HTTPException
    from backend.routes.webhook import _debug_images_base, _debug_images_dir

    base = _debug_images_base()
    assert _debug_images_dir("good-pipeline") == base / "good-pipeline"

    for hostile in ("../../storage/faces", "..", "../..", "a/../../b",
                    "/etc", "/app/storage/faces", "....//....//etc"):
        with pytest.raises(HTTPException) as excinfo:
            _debug_images_dir(hostile)
        assert excinfo.value.status_code in (400, 422), hostile


def test_debug_images_are_not_readable_anonymously():
    status, _ = _get("/api/webhook/images/any-pipeline")
    assert status in (401, 403), f"anonymous debug-image listing returned {status}"

    status, _ = _get("/api/webhook/images/any-pipeline/anything.jpg")
    assert status in (401, 403), f"anonymous debug-image read returned {status}"


def test_traversal_is_rejected_even_for_an_admin(admin_token):
    for hostile in ("..%2F..%2Fstorage", "..%2F..", "%2Fetc"):
        status, body = _get(f"/api/webhook/images/{hostile}", token=admin_token)
        assert status != 200, (
            f"traversal listing succeeded for {hostile!r}: {body[:200]!r}")
        status, body = _get(f"/api/webhook/images/{hostile}/x.jpg", token=admin_token)
        assert status != 200, (
            f"traversal read succeeded for {hostile!r}: {body[:200]!r}")


# ---------------------------------------------------------------------------
# Live enforcement
# ---------------------------------------------------------------------------

def test_ingest_requires_a_key_on_both_paths():
    key = _key()
    for path in ("/webhook/qa-auth-test", "/api/webhook/qa-auth-test"):
        assert _post(path)[0] == 401, f"{path} accepted a request with no key"
        assert _post(path, key="not-the-key")[0] == 401, (
            f"{path} accepted an invalid key")
        assert _post(path, key=key)[0] == 200, f"{path} rejected a valid key"


def test_a_rejection_is_a_401_not_a_redirect_to_signin():
    """A camera is not a browser.

    /webhook/... does not start with /api/, so the global exception handler
    classified it as an HTML page request and answered 302 -> /signin. A client
    following redirects then gets 200 and a login page, and reads a rejection as
    a successful ingest.
    """
    status, body = _post("/webhook/qa-auth-test")
    assert status == 401, f"expected 401, got {status}"
    payload = json.loads(body)
    detail = payload.get("detail")
    assert isinstance(detail, dict), f"unstructured rejection body: {payload}"
    assert detail["error"]["code"] == "WEBHOOK_AUTH_REQUIRED"


def test_the_bearer_form_is_accepted():
    key = _key()
    assert _post("/api/webhook/qa-auth-test", key=key,
                 header="Authorization") [0] != 200, (
        "a raw key in the Authorization header must not be accepted without the "
        "Bearer scheme")

    request = urllib.request.Request(
        BASE + "/api/webhook/qa-auth-test", data=b"{}", method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200


def _post_authorization(path, authorization, *, timeout=30):
    """POST with a VERBATIM Authorization header, including malformed values.

    _post() only ever sets a bare key, so it cannot express `Bearer` with no
    token or a wrong scheme — the shapes an external sender actually gets wrong.
    """
    request = urllib.request.Request(BASE + path, data=b"{}", method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", authorization)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def test_a_wrong_token_over_bearer_is_rejected():
    """Untested until now: the existing wrong-key case uses the custom header.

    Bearer is the transport external systems are told to use, so a wrong token
    over Bearer is the single most likely real-world rejection.
    """
    status, _, _ = _post_authorization("/api/webhook/qa-auth-test",
                                       "Bearer definitely-not-the-token")
    assert status == 401, f"a wrong bearer token was accepted with {status}"


def test_every_malformed_authorization_is_rejected_over_http():
    for value in ("Bearer", "Bearer ", "Bearer    ", "Bearer\tabc",
                  "Basic abc", "Token abc", "Bearerabc"):
        status, _, _ = _post_authorization("/api/webhook/qa-auth-test", value)
        assert status == 401, f"{value!r} was accepted with {status}"


def test_the_401_is_identical_for_missing_malformed_and_wrong():
    """No oracle: the response must not reveal WHY it failed.

    A body that distinguished "no credential" from "wrong credential" would let
    an attacker confirm a guessed token shape without ever guessing the value.
    """
    key = _key()
    missing = _post("/api/webhook/qa-auth-test")[1]
    malformed = _post_authorization("/api/webhook/qa-auth-test", "Bearer")[1]
    wrong = _post_authorization("/api/webhook/qa-auth-test", f"Bearer wrong-{key}")[1]

    assert json.loads(missing) == json.loads(malformed) == json.loads(wrong), (
        "the 401 body differs between failure modes — that is an oracle")


def test_the_401_never_echoes_a_credential():
    """Requirement: the token appears in no response byte, header or body."""
    key = _key()
    presented = f"wrong-{key}"
    status, body, headers = _post_authorization(
        "/api/webhook/qa-auth-test", f"Bearer {presented}")

    assert status == 401
    rendered = body.decode("utf-8", "replace") + repr(headers)
    assert presented not in rendered, "the response echoed the presented token"
    assert key not in rendered, "the response leaked the CONFIGURED key"


def test_the_challenge_names_a_scheme_a_sender_can_produce():
    """`WWW-Authenticate: WebhookKey` named nothing any HTTP client implements."""
    _, _, headers = _post_authorization("/api/webhook/qa-auth-test", "Bearer nope")
    challenge = {k.lower(): v for k, v in headers.items()}.get("www-authenticate", "")
    assert "bearer" in challenge.lower(), (
        f"challenge {challenge!r} does not offer Bearer")


def test_the_connectivity_probe_is_a_credential_self_check():
    key = _key()
    for path in ("/webhook/test", "/api/webhook/test"):
        assert _get(path)[0] == 401, f"{path} is reachable without a key"
        assert _get(path, key=key)[0] == 200, f"{path} rejected a valid key"


def test_an_unauthenticated_oversized_body_is_rejected_without_being_buffered():
    """This is the property that makes Depends, not an in-handler check, correct.

    WEBHOOK_MAX_BODY_MB is 25; this sends ~35 MB with no credential. The
    dependency runs before parse_webhook_request, so the answer arrives without
    the server ever reading the payload.
    """
    payload = json.dumps({"image": "A" * 35_000_000}).encode()
    request = urllib.request.Request(
        BASE + "/webhook/qa-auth-oversized", data=payload, method="POST")
    request.add_header("Content-Type", "application/json")

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, ConnectionError, BrokenPipeError):
        # The server closing the connection on an unauthenticated caller is an
        # acceptable rejection too.
        status = 401
    elapsed = time.perf_counter() - started

    assert status == 401, f"an unauthenticated 35 MB body got {status}, not 401"
    assert elapsed < 20.0, (
        f"the rejection took {elapsed:.1f}s — the body was buffered before the "
        "credential was checked")


def test_the_auth_metric_records_outcomes():
    key = _key()
    _post("/webhook/qa-auth-metric")                 # missing
    _post("/webhook/qa-auth-metric", key="nope")     # invalid
    _post("/webhook/qa-auth-metric", key=key)        # ok

    status, body = _get("/metrics")
    assert status == 200
    text = body.decode(errors="replace")
    assert "fr_webhook_auth_total" in text, "the ingest auth metric is not exposed"
    assert 'result="ok"' in text and 'result="missing"' in text, (
        "outcomes are not being labelled")
    # pipeline_id must never become a label: the caller chooses it, so it would
    # be an unbounded-cardinality lever aimed at the registry.
    for line in text.splitlines():
        if line.startswith("fr_webhook_auth_total"):
            assert "pipeline" not in line, f"unbounded label on {line}"


# ---------------------------------------------------------------------------
# The committed development key must never reach production
# ---------------------------------------------------------------------------

DEV_KEY = "dev-ingest-key-Zr6Xk1Nd8Bq3Ly7Vt5Wc0Mj2Hf9Ps4Ga"


def test_the_development_key_appears_only_in_non_production_compose():
    """A key committed for testing must not be inherited by a prod stack.

    docker-compose.gpu.yml sets ENVIRONMENT=production and briefly carried this
    key, which would have shipped a working ingest credential in the repository
    to a production deployment.
    """
    import pathlib

    root = pathlib.Path("/app")
    offenders = []
    for path in sorted((root / "docker").glob("docker-compose*.yml")):
        text = path.read_text(encoding="utf-8")
        if DEV_KEY not in text:
            continue
        if "ENVIRONMENT: production" in text:
            offenders.append(path.name)
    assert not offenders, (
        f"the development ingest key is present in production compose files: {offenders}")


def test_production_refuses_the_published_development_key():
    """Belt and braces: even a copy-pasted stanza cannot start."""
    from types import SimpleNamespace
    from backend.security.config_guard import codes_of, collect_violations

    codes = codes_of(collect_violations(SimpleNamespace(
        ENVIRONMENT="production", WEBHOOK_AUTH_MODE="enforce",
        WEBHOOK_API_KEYS=DEV_KEY)))
    assert "WEBHOOK_KEY_IS_PUBLISHED" in codes

    # ...and it is still caught when hidden among legitimate keys.
    codes = codes_of(collect_violations(SimpleNamespace(
        ENVIRONMENT="production", WEBHOOK_AUTH_MODE="enforce",
        WEBHOOK_API_KEYS=f"Kf9-Qz2LmXv7Bt4Rn8Wc3Ye6Ua1Sd5Gh0Jp,{DEV_KEY}")))
    assert "WEBHOOK_KEY_IS_PUBLISHED" in codes


def test_the_rejection_list_stores_digests_not_the_key_itself():
    """Rejecting a credential must not require re-publishing it."""
    source = open("/app/backend/security/webhook_auth.py", encoding="utf-8").read()
    assert DEV_KEY not in source, (
        "the development key is written into the source that exists to reject it")


def test_development_is_not_blocked_by_the_published_key_rule():
    """The dev stack legitimately uses it; only production refuses."""
    from types import SimpleNamespace
    from backend.security.config_guard import codes_of, collect_violations

    codes = codes_of(collect_violations(SimpleNamespace(
        ENVIRONMENT="development", WEBHOOK_AUTH_MODE="enforce",
        WEBHOOK_API_KEYS=DEV_KEY)))
    assert "WEBHOOK_KEY_IS_PUBLISHED" not in codes


# ---------------------------------------------------------------------------
# The production secret FILE
# ---------------------------------------------------------------------------

_secret_file_counter = itertools.count()


def _secret_codes(tmp_path, contents, mode=0o400):
    import os
    from types import SimpleNamespace
    from backend.security.config_guard import codes_of, collect_violations

    # A fresh filename per call: these files are written 0400 on purpose, so
    # reusing one path makes the SECOND write in a test fail with EACCES rather
    # than exercising the rule under test.
    path = tmp_path / f"webhook_api_keys_{next(_secret_file_counter)}"
    path.write_text(contents, encoding="utf-8")
    os.chmod(path, mode)
    return codes_of(collect_violations(SimpleNamespace(
        ENVIRONMENT="production", WEBHOOK_AUTH_MODE="enforce",
        WEBHOOK_API_KEYS_FILE=str(path))))


STRONG = "Zr6Xk1Nd8Bq3Ly7Vt5Wc0Mj2Hf9Ps4GaTx8Qw2"


def test_a_correct_secret_file_produces_no_violation(tmp_path):
    codes = _secret_codes(tmp_path, STRONG)
    assert not [c for c in codes if c.startswith("WEBHOOK_KEY_FILE")], codes


def test_a_missing_secret_file_blocks_startup():
    from types import SimpleNamespace
    from backend.security.config_guard import codes_of, collect_violations

    codes = codes_of(collect_violations(SimpleNamespace(
        ENVIRONMENT="production", WEBHOOK_AUTH_MODE="enforce",
        WEBHOOK_API_KEYS_FILE="/run/secrets/definitely-not-mounted")))
    assert "WEBHOOK_KEY_FILE_MISSING" in codes, (
        "an unmounted secret must fail the preflight, not resolve to empty")


def test_a_directory_instead_of_a_file_is_rejected():
    from types import SimpleNamespace
    from backend.security.config_guard import codes_of, collect_violations

    codes = codes_of(collect_violations(SimpleNamespace(
        ENVIRONMENT="production", WEBHOOK_AUTH_MODE="enforce",
        WEBHOOK_API_KEYS_FILE="/tmp")))
    assert "WEBHOOK_KEY_FILE_NOT_A_FILE" in codes


def test_an_empty_secret_file_is_rejected(tmp_path):
    """Empty reads exactly like unconfigured — which mid-rollout means open."""
    assert "WEBHOOK_KEY_FILE_EMPTY" in _secret_codes(tmp_path, "")
    assert "WEBHOOK_KEY_FILE_EMPTY" in _secret_codes(tmp_path, "   \n , ,\n")


def test_a_weak_key_in_the_secret_file_is_rejected(tmp_path):
    assert "WEBHOOK_KEY_FILE_WEAK" in _secret_codes(tmp_path, "short")
    assert "WEBHOOK_KEY_FILE_WEAK" in _secret_codes(tmp_path, "a" * 64)


def test_loose_permissions_are_rejected(tmp_path):
    """Anything writable, or readable by the whole host, is a tampering vector."""
    assert "WEBHOOK_KEY_FILE_PERMISSIONS" in _secret_codes(tmp_path, STRONG, mode=0o666)
    assert "WEBHOOK_KEY_FILE_PERMISSIONS" in _secret_codes(tmp_path, STRONG, mode=0o644)
    for allowed in (0o400, 0o440, 0o444):
        codes = _secret_codes(tmp_path, STRONG, mode=allowed)
        assert "WEBHOOK_KEY_FILE_PERMISSIONS" not in codes, (allowed, codes)


def test_multiple_keys_in_the_secret_file_are_all_validated(tmp_path):
    """Rotation puts two keys in the file; both must be strong."""
    codes = _secret_codes(tmp_path, f"{STRONG},{STRONG}Xy9")
    assert not [c for c in codes if c.startswith("WEBHOOK_KEY_FILE")], codes

    codes = _secret_codes(tmp_path, f"{STRONG},weak")
    assert "WEBHOOK_KEY_FILE_WEAK" in codes, (
        "a weak key added during rotation must still be caught")


def test_the_secret_file_report_never_prints_a_key(tmp_path):
    import os
    from types import SimpleNamespace
    from backend.security.config_guard import collect_violations, format_report

    path = tmp_path / "webhook_api_keys"
    path.write_text("weakling", encoding="utf-8")
    os.chmod(path, 0o400)
    report = format_report(collect_violations(SimpleNamespace(
        ENVIRONMENT="production", WEBHOOK_AUTH_MODE="enforce",
        WEBHOOK_API_KEYS_FILE=str(path))))
    assert "weakling" not in report, "the report leaked the key from the file"
    assert "#1" in report, "the report should identify the key by position"


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

def _redact(message):
    import logging
    from utils.logging import SensitiveDataFilter

    record = logging.LogRecord(name="t", level=logging.INFO, pathname=__file__,
                               lineno=1, msg=message, args=(), exc_info=None)
    SensitiveDataFilter().filter(record)
    return record.getMessage()


def test_the_ingest_key_never_survives_the_log_filter():
    """Field-name patterns AND the literal value.

    `WEBHOOK_API_KEYS` was not matched by the generic `api[_-]?key` alternative:
    that one is \\b-anchored, and inside "WEBHOOK_API_KEYS" the position before
    "API" sits between two word characters, so the value was logged in full.
    """
    key = _key()
    shapes = [
        f"X-Webhook-Key: {key}",
        f"x-webhook-key={key}",
        f"WEBHOOK_API_KEYS={key}",
        f"webhook_api_keys: {key}",
        f'{{"webhook_key": "{key}"}}',
        f"Authorization: Bearer {key}",
        # Shapes no field-name pattern can anticipate — covered by the literal
        # value redaction.
        f"camera posted with {key} and got 202",
        f"settings FILE=/run/secrets/x key={key}",
        key,
    ]
    leaks = [s for s in shapes if key in _redact(s)]
    assert not leaks, f"the ingest key survived redaction in: {leaks}"


def test_redaction_does_not_eat_safe_operational_fields():
    line = ("user=admin ip=10.0.0.5 request_id=abc123 pipeline_id=camera-1 "
            "status=202 duration=0.012s")
    assert _redact(line) == line


def test_the_key_is_not_exposed_through_the_admin_settings_api():
    """It must never reach the settings table at all."""
    source = open("/app/backend/routes/settings.py", encoding="utf-8").read()
    categories = source.split("categories = {", 1)[1].split("\n    }", 1)[0]
    assert "WEBHOOK_API_KEYS" not in categories, (
        "WEBHOOK_API_KEYS is in a settings category — it would be persisted to "
        "the settings table and served by the admin API")


def test_the_key_cannot_be_changed_at_runtime():
    from backend.core.runtime_settings import apply_to_runtime

    assert apply_to_runtime("WEBHOOK_API_KEYS", "attacker-supplied") is False
    assert apply_to_runtime("WEBHOOK_AUTH_MODE", "off") is False


def test_the_unenforced_branch_requires_both_credential_sets_to_be_empty():
    """The dev-convenience branch accepts everything when no key is configured.

    Once issued credentials exist, checking only WEBHOOK_API_KEYS would mean an
    operator who mints a credential in the admin UI without also setting an env
    key gets wide-open ingest while believing ingest is authenticated — the
    feature would make this posture worse than not having it. Pinned at the
    source level because the runtime state (a populated cache with an empty env
    key set) is awkward to stage against the live app.
    """
    import ast
    import inspect

    from backend.routes import webhook

    source = inspect.getsource(webhook.require_webhook_key)
    tree = ast.parse(source.lstrip())

    guards = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
                rendered = ast.dump(test)
                if "keys_configured" in rendered and "any_cached" in rendered:
                    guards.append(node)

    assert guards, (
        "the unenforced branch must require BOTH webhook_auth.keys_configured() "
        "and webhook_credentials.any_cached() to be false")
