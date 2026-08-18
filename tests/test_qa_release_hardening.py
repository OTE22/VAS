"""Release-readiness regression tests (QA hardening pass).

Each test pins a defect confirmed live during the pre-release audit:

  QA-001 BLOCKER  POST /api/cache/clear was unauthenticated — pattern=* wiped Redis
  QA-002 BLOCKER  POST /api/cleanup/manual was unauthenticated — triggered a
                  retention deletion sweep for anyone
  QA-003 HIGH     POST /api/cache/warm and POST /api/face-tracker/reset were
                  unauthenticated state mutations
  QA-004 HIGH     GET /api/detections 500'd (async lazy-load of Detection.faces
                  outside the request greenlet)
  QA-005 MEDIUM   GET /api/cache/stats and /api/cache/health 500'd when the
                  optional face_recognition_cache singleton was None
  QA-006 MEDIUM   GET /api/stats served unfiltered system-wide aggregates to
                  anonymous callers AND to cookie-authenticated non-admins
                  (the handler only read the Authorization header)

Run inside the api container:

    docker exec face_recognition_api python -m pytest tests/test_qa_release_hardening.py -v
"""
import json
import urllib.error
import urllib.request

import pytest

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"


def _http(method, path, body=None, token=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw or b"{}")
            except Exception:
                return resp.status, {"_raw": raw.decode(errors="replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"_raw": raw.decode(errors="replace")}


@pytest.fixture(scope="module")
def admin_token():
    status, body = _http("POST", "/api/auth/login",
                         {"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


@pytest.fixture(scope="module")
def user_token():
    """A live, non-admin ('user' role) account for authorization tests."""
    from sqlalchemy import text
    from db_connection import db_manager
    from backend.auth.password import hash_password

    username = "qa_hardening_probe_user"
    password = "QaHardeningProbe!123"

    async def seed():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.execute(text("""
                INSERT INTO users (username, email, full_name, password_hash, role,
                                   is_active, can_use_chatbot, created_at)
                VALUES (:u, :e, 'QA Hardening Probe', :h, 'user', true, false, now())
            """), {"u": username, "e": f"{username}@example.test",
                   "h": hash_password(password)})
            await db.commit()

    run_async(seed())
    status, body = _http("POST", "/api/auth/login",
                         {"username": username, "password": password})
    assert status == 200, body
    yield body["access_token"]

    async def cleanup():
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.commit()
    run_async(cleanup())


# The system-management mutations that must never be reachable without admin.
XSRF = {"X-Requested-With": "XMLHttpRequest"}
ADMIN_ONLY_MUTATIONS = [
    ("POST", "/api/cache/clear?pattern=stats:%2A", None),
    ("POST", "/api/cache/warm/00000000-0000-0000-0000-000000000000", None),
    ("POST", "/api/cleanup/manual", None),
    ("POST", "/api/face-tracker/reset/00000000-0000-0000-0000-000000000000", None),
]
ADMIN_ONLY_READS = [
    "/api/cache/stats",
    "/api/cache/health",
    "/api/cache/redis/stats",
    "/api/cache/redis/test",
    "/api/circuit-breaker/status",
    "/api/face-tracker/stats",
]


# -------------------------------------------------------------------------
# QA-001..003 — unauthenticated / non-admin cannot reach management surface
# -------------------------------------------------------------------------

def test_management_mutations_reject_anonymous():
    for method, path, body in ADMIN_ONLY_MUTATIONS:
        status, _ = _http(method, path, body=body, headers=XSRF)
        assert status in (401, 403), f"{method} {path} answered {status} anonymously"


def test_management_mutations_reject_non_admin(user_token):
    for method, path, body in ADMIN_ONLY_MUTATIONS:
        status, _ = _http(method, path, body=body, token=user_token, headers=XSRF)
        assert status == 403, f"{method} {path} answered {status} for a non-admin"


def test_management_reads_reject_anonymous():
    for path in ADMIN_ONLY_READS:
        status, _ = _http("GET", path)
        assert status in (401, 403), f"GET {path} answered {status} anonymously"


def test_management_reads_reject_non_admin(user_token):
    for path in ADMIN_ONLY_READS:
        status, _ = _http("GET", path, token=user_token)
        assert status == 403, f"GET {path} answered {status} for a non-admin"


def test_cache_clear_is_admin_gated_before_pattern_validation():
    """The dangerous default: pattern=* wipes everything. Anonymously it must
    be refused by AUTH, not merely by the pattern allowlist."""
    status, _ = _http("POST", "/api/cache/clear?pattern=%2A", headers=XSRF)
    assert status in (401, 403), (
        "cache clear with pattern=* must be blocked by authentication, "
        f"got {status}")


# -------------------------------------------------------------------------
# QA-004 — /api/detections no longer 500s on the lazy faces relationship
# -------------------------------------------------------------------------

def test_detections_returns_ok_for_admin(admin_token):
    status, body = _http("GET", "/api/detections?limit=5", token=admin_token)
    assert status == 200, f"/api/detections regressed to {status}: {body}"
    assert "detections" in body and "total" in body
    for d in body["detections"]:
        # faces_count is derived from the eager-loaded relationship
        assert "faces_count" in d


# -------------------------------------------------------------------------
# QA-005 — cache stats/health return 200 for admin (None-cache safe)
# -------------------------------------------------------------------------

def test_cache_stats_and_health_do_not_500_for_admin(admin_token):
    for path in ("/api/cache/stats", "/api/cache/health"):
        status, body = _http("GET", path, token=admin_token)
        assert status == 200, f"{path} returned {status}: {body}"
    # face_cache may legitimately be null when the singleton is unwired — the
    # point is it must not raise.
    status, stats = _http("GET", "/api/cache/stats", token=admin_token)
    assert "cache_manager" in stats


# -------------------------------------------------------------------------
# QA-006 — /api/stats requires auth and filters by the caller's role
# -------------------------------------------------------------------------

def test_stats_requires_authentication():
    status, _ = _http("GET", "/api/stats")
    assert status in (401, 403), (
        f"/api/stats served system aggregates anonymously (got {status})")


def test_stats_is_served_to_an_authenticated_admin(admin_token):
    status, body = _http("GET", "/api/stats", token=admin_token)
    assert status == 200, body
    assert "pipelines" in body and "faces" in body


def test_stats_filters_for_a_non_admin_with_no_pipelines(user_token):
    """A user with no pipeline grants sees zeros — never the global totals a
    cookie/anon caller used to receive."""
    status, body = _http("GET", "/api/stats", token=user_token)
    assert status == 200, body
    assert body["pipelines"]["active"] == 0
    assert body["pipelines"]["total_detections"] == 0
    assert body["faces"]["total"] == 0
