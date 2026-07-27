"""
Authorization on the SQL agent's endpoints.

Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_sql_agent_authz.py -v

Two regressions are pinned here.

1. Five endpoints had no auth dependency at all. Three of them —
   /session/new, /sessions, /session/load — operated on the *shared global*
   agent instance, so any unauthenticated caller could enumerate, load or
   reset another user's conversation. /schema published every table and
   column the assistant can reach.

2. The auth imports sat in a try/except that, on ImportError, replaced
   get_current_user and require_chatbot_access with stubs returning None.
   Every `Depends(require_chatbot_access())` then became a no-op and the whole
   agent — including query execution — served unauthenticated. The blast
   radius was wider than it looks: the same try block imports the
   query-history service, so an unrelated failure there disabled auth.

No LLM is invoked.
"""

import json
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"
ROUTES_PATH = "/app/sql_agent/api/routes.py"


def _http(method, path, body=None, token=None, timeout=30):
    """Bearer-authenticated HTTP.

    Deliberately does NOT send X-Requested-With: that header marks the caller
    as a browser client, and login then withholds the access token from the
    response body by design (the cookie is the credential there). Bearer
    clients are also exempt from the CSRF header requirement.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw or b"{}")
            except Exception:
                return resp.status, {"_raw": raw[:200].decode(errors="replace")}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         {"username": "admin", "password": "admin123"})
    assert status == 200, f"admin login failed: {body}"
    return body["access_token"]


# Endpoints that must never answer an anonymous caller.
PROTECTED = [
    ("GET", "/api/sql-agent/schema", None),
    ("GET", "/api/sql-agent/sessions", None),
    ("POST", "/api/sql-agent/session/new", {}),
    ("POST", "/api/sql-agent/session/load", {"session_id": "someone-elses-session"}),
]


# ------------------------------------------------- anonymous is refused

@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_protected_endpoints_reject_anonymous_callers(method, path, body):
    status, _ = _http(method, path, body)
    assert status in (401, 403), f"{method} {path} answered {status} without credentials"


def test_schema_is_not_public():
    """The schema names every reachable table and column."""
    status, body = _http("GET", "/api/sql-agent/schema")
    assert status in (401, 403)
    assert "faces" not in json.dumps(body)


# ------------------------------------------------- authenticated works

def test_schema_available_to_authorized_user(token):
    status, body = _http("GET", "/api/sql-agent/schema", token=token)
    assert status == 200
    assert body.get("success") is True


def test_sessions_scoped_to_the_calling_user(token):
    status, body = _http("GET", "/api/sql-agent/sessions", token=token)
    assert status == 200
    assert body.get("success") is True
    assert isinstance(body.get("sessions"), list)


def test_new_session_works_for_authorized_user(token):
    status, body = _http("POST", "/api/sql-agent/session/new", {}, token=token)
    assert status == 200
    assert body.get("session_id")


def test_loading_another_users_session_is_not_found(token):
    """Sessions resolve against the caller's own store, so another user's id
    is simply absent — ownership is structural, not a check to forget."""
    status, _ = _http("POST", "/api/sql-agent/session/load",
                      {"session_id": "user_999999_main"}, token=token)
    assert status == 404


def test_load_session_validates_input(token):
    status, _ = _http("POST", "/api/sql-agent/session/load", {}, token=token)
    assert status == 400


# ------------------------------------------------- fail-closed auth stub

def _routes_source():
    with open(ROUTES_PATH, encoding="utf-8") as handle:
        return handle.read()


def test_auth_import_failure_denies_instead_of_allowing():
    """The fallback stubs must refuse requests, not return None."""
    source = _routes_source()
    block = source[source.index("except ImportError"):source.index("logger = logging.getLogger")]
    assert "AUTH_UNAVAILABLE" in block
    assert "SERVICE_UNAVAILABLE" in block or "503" in block
    assert "return None" not in block, "stub still grants anonymous access"


def test_session_endpoints_no_longer_touch_the_global_instance():
    source = _routes_source()
    for handler in ("sql_agent_new_session", "sql_agent_list_sessions",
                    "sql_agent_load_session"):
        start = source.index(f"async def {handler}")
        body = source[start:start + 2400]
        assert "_scoped_agent(current_user)" in body, f"{handler} is not user-scoped"
        assert "_sql_agent_instance.conversation_memory" not in body, (
            f"{handler} still reads the shared global conversation memory"
        )


@pytest.mark.parametrize("handler", [
    "sql_agent_schema", "sql_agent_new_session",
    "sql_agent_list_sessions", "sql_agent_load_session",
])
def test_previously_open_endpoints_now_declare_a_dependency(handler):
    source = _routes_source()
    start = source.index(f"async def {handler}")
    signature = source[start:source.index("):", start)]
    assert "require_chatbot_access()" in signature, (
        f"{handler} has no authorization dependency"
    )
