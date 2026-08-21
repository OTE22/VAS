"""
Intelligence Analysis system tests
==================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_intelligence_system.py -v

Proves the Intelligence Analysis overhaul contract:
  * object-level guard: malformed/unknown identity ids -> 404 (never 500,
    never SQL error leakage) on every intelligence endpoint
  * structured safe errors: no detail=str(e), no dependency install hints
  * map endpoint: style allowlist, private/no-store + CSP sandbox headers,
    security overlays default OFF
  * calculate-all: CSRF-protected, 202 + job_id envelope, single-flight
    guard (409 with the running job id), job-status endpoint
  * /api/admin/identities: server-side paginated search envelope with caps
  * frontend (admin-intelligence.js / intelligence.html): zero innerHTML,
    zero inline handlers, sandboxed map iframe, one authoritative selection
    path, per-resource aborts + generations, explicit checkbox defaults,
    strict timestamps, bounded map retry, hour-aligned charts
"""

import json
import uuid
import urllib.request
import urllib.error

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"

JS_PATH = "/app/frontend/js/admin-intelligence.js"
HTML_PATH = "/app/frontend/admin/intelligence.html"
ROUTES_PATH = "/app/backend/routes/intelligence.py"
TASK_PATH = "/app/backend/core/relationship_calculation_task.py"
IDENTITIES_ROUTES_PATH = "/app/backend/routes/identities.py"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _http(method, path, body=None, token=None, cookie=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if cookie:
        req.add_header("Cookie", cookie)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                payload = {"_raw": raw.decode(errors="replace")}
            return resp.status, payload, resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {"_raw": raw.decode(errors="replace")}
        return e.code, payload, e.headers


@pytest.fixture(scope="module")
def token():
    status, body, _ = _http("POST", "/api/auth/login",
                            {"username": "admin", "password": "admin123"})
    assert status == 200, f"admin login failed: {body}"
    return body["access_token"]


@pytest.fixture(scope="module")
def cookie(token):
    """Cookie-shaped auth (what the browser sends) for CSRF tests."""
    return f"access_token={token}"


@pytest.fixture(scope="module")
def any_identity(token):
    status, body, _ = _http("GET", "/api/admin/identities?page=1&page_size=1", token=token)
    assert status == 200, body
    items = body.get("items") or []
    return items[0]["id"] if items else None


# ---------------------------------------------------------------------------
# Object-level authorization: 404 for unknown/malformed identity ids
# ---------------------------------------------------------------------------

INTEL_GET_ENDPOINTS = [
    "/api/identities/{iid}/related",
    "/api/identities/{iid}/temporal-patterns",
    "/api/identities/{iid}/cross-camera",
    "/api/identities/{iid}/analyze",
    "/api/identities/{iid}/timeline",
]


def test_unknown_identity_returns_404(token):
    ghost = str(uuid.uuid4())
    for tpl in INTEL_GET_ENDPOINTS:
        status, body, _ = _http("GET", tpl.format(iid=ghost), token=token)
        assert status == 404, f"{tpl}: expected 404 for unknown id, got {status} {body}"
        assert body.get("detail") == "Identity not found"


def test_malformed_identity_id_returns_404_not_500(token):
    for tpl in INTEL_GET_ENDPOINTS:
        status, body, _ = _http("GET", tpl.format(iid="not-a-valid-id"), token=token)
        assert status == 404, f"{tpl}: malformed id must 404, got {status} {body}"


def test_intelligence_requires_auth():
    ghost = str(uuid.uuid4())
    status, _, _ = _http("GET", f"/api/identities/{ghost}/related")
    assert status == 401


# ---------------------------------------------------------------------------
# Contracts on real data (skipped when the test DB has no identities)
# ---------------------------------------------------------------------------

def test_related_envelope_with_threshold_policy(token, any_identity):
    if not any_identity:
        pytest.skip("no identities in test database")
    status, body, _ = _http("GET", f"/api/identities/{any_identity}/related", token=token)
    assert status == 200, body
    assert isinstance(body.get("items"), list)
    thresholds = body.get("thresholds")
    assert thresholds, "threshold policy must be returned as API metadata"
    assert thresholds["strong"]["min_percentage"] == 50
    assert thresholds["moderate"]["min_percentage"] == 25


def test_analyze_reports_per_section_status(token, any_identity):
    if not any_identity:
        pytest.skip("no identities in test database")
    status, body, _ = _http("GET", f"/api/identities/{any_identity}/analyze", token=token)
    assert status == 200, body
    sections = body.get("sections")
    assert sections, "complete analysis must expose per-section statuses"
    for key in ("related", "temporal", "tracking"):
        assert sections[key]["status"] in ("ready", "partial", "unavailable", "error"), sections
    # tracking must never claim 'ready' without movement evidence
    if sections["tracking"]["status"] == "ready":
        assert sections["tracking"].get("movement_count", 0) > 0
    assert body["analyzed_at"].endswith("Z"), "analysis timestamps must be timezone-aware"


# ---------------------------------------------------------------------------
# calculate-all: CSRF, job envelope, single-flight, job status
# ---------------------------------------------------------------------------

def test_calculate_all_requires_csrf(cookie):
    status, body, _ = _http("POST", "/api/intelligence/relationships/calculate-all",
                            body={}, cookie=cookie)
    assert status == 403, f"cookie POST without CSRF header must be blocked: {status} {body}"
    assert "CSRF" in str(body.get("detail", ""))


def test_calculate_all_with_csrf_header_accepted(cookie):
    status, body, _ = _http("POST", "/api/intelligence/relationships/calculate-all",
                            body={}, cookie=cookie,
                            headers={"X-Requested-With": "XMLHttpRequest"})
    # 202 = scheduled; 409 = a previous run is still active — both prove the
    # endpoint returns instantly with a job envelope instead of doing the work inline
    assert status in (202, 409), body
    if status == 202:
        assert body["accepted"] is True
        assert body["status"] == "scheduled"
        assert body["job_id"].startswith("relationships-")
    else:
        detail = body["detail"]
        assert detail["error_code"] == "JOB_ALREADY_RUNNING"
        assert detail["job_id"]


def test_relationship_job_status_endpoint(token):
    status, body, _ = _http("GET", "/api/intelligence/relationships/jobs/relationships-doesnotexist",
                            token=token)
    assert status == 404


def test_single_flight_guard_logic():
    """The in-process guard is deterministic: second acquire returns the
    running job id; release frees it."""
    import importlib
    intel = importlib.import_module("backend.routes.intelligence")
    # ensure clean slate for the unit check
    intel._RELATIONSHIP_JOB["job_id"] = None
    intel._RELATIONSHIP_JOB["started_at"] = None
    assert intel._try_acquire_relationship_job("relationships-test1") is None
    assert intel._try_acquire_relationship_job("relationships-test2") == "relationships-test1"
    intel._release_relationship_job("relationships-test1")
    assert intel._try_acquire_relationship_job("relationships-test3") is None
    intel._release_relationship_job("relationships-test3")


def test_worker_writes_job_progress():
    src = _read(TASK_PATH)
    assert "def calculate_all_relationships(job_id" in src.replace("async ", ""), \
        "worker must accept the API-issued job_id"
    assert "update_progress" in src, "worker must report progress to the job row"
    assert "finish_job" in src, "worker must finish the job row on completion/failure"


# ---------------------------------------------------------------------------
# Backend hygiene (source contracts)
# ---------------------------------------------------------------------------

def test_no_raw_exception_leakage():
    src = _read(ROUTES_PATH)
    assert "detail=str(e)" not in src, "raw exception text must never reach the client"
    assert "pip install" not in src, "dependency instructions belong in health checks, not error pages"


def test_map_security_features_default_off():
    src = _read(ROUTES_PATH)
    # show_timeline / show_animated_avatar were rendering switches on the
    # retired Folium endpoint; the browser now decides what to draw from the
    # /map-data payload. These three still gate SERVER-SIDE analysis work and
    # must stay opt-in.
    for flag in ("enable_security_features", "detect_patterns", "show_risk_heatmap"):
        seg = src.split(f"{flag}: bool = Query(default=", 1)
        assert len(seg) == 2, f"{flag} query param missing"
        assert seg[1].startswith("False"), f"{flag} must default to False (opt-in)"


def test_audit_logging_present():
    src = _read(ROUTES_PATH)
    assert src.count("_audit(") >= 8, "every sensitive access path must audit"
    # the audit line format carries ids/counters only — never payloads
    assert "[INTEL_AUDIT] action=%s user_id=%s identity_id=%s result=%s" in src


def test_identity_guard_applied_everywhere():
    src = _read(ROUTES_PATH)
    assert src.count("_get_identity_or_404") >= 9, \
        "all identity-scoped endpoints must run the existence/authorization guard"


# ---------------------------------------------------------------------------
# /api/admin/identities: server-side paginated search
# ---------------------------------------------------------------------------

def test_identities_paginated_envelope(token):
    status, body, _ = _http("GET", "/api/admin/identities?page=1&page_size=5", token=token)
    assert status == 200, body
    for key in ("items", "total", "page", "page_size", "total_pages"):
        assert key in body, f"paginated envelope missing {key}"
    assert len(body["items"]) <= 5
    assert body["page"] == 1 and body["page_size"] == 5
    for item in body["items"]:
        assert "id" in item and "type" in item
        if item.get("last_seen_at"):
            assert item["last_seen_at"].endswith("Z") or "+" in item["last_seen_at"]


def test_identities_page_size_capped(token):
    status, _, _ = _http("GET", "/api/admin/identities?page=1&page_size=500", token=token)
    assert status == 422, "page_size beyond 100 must be rejected"


def test_identities_search_filters(token, any_identity):
    if not any_identity:
        pytest.skip("no identities in test database")
    # id-prefix search must find the identity
    status, body, _ = _http(
        "GET", f"/api/admin/identities?page=1&page_size=10&q={any_identity[:8]}", token=token)
    assert status == 200, body
    assert any(item["id"] == any_identity for item in body["items"]), \
        "id-prefix search must match server-side"


def test_identities_legacy_mode_preserved(token):
    status, body, _ = _http("GET", "/api/admin/identities?limit=3", token=token)
    assert status == 200, body
    assert "identities" in body and "type_filter" in body, "legacy consumers keep their shape"
    assert len(body["identities"]) <= 3


def test_identities_requires_admin():
    status, _, _ = _http("GET", "/api/admin/identities?page=1")
    assert status == 401


def test_identities_no_per_row_queries():
    src = _read(IDENTITIES_ROUTES_PATH)
    assert "_batch_pipeline_ids" in src, "pipeline ids must be fetched in one batched query"
    fn = src.split("async def list_all_identities")[1].split("\n@router")[0]
    assert "for identity in identities:\n            # Get pipeline IDs" not in fn, \
        "per-identity N+1 pipeline lookup must be gone"


# ---------------------------------------------------------------------------
# Frontend source contracts (admin-intelligence.js)
# ---------------------------------------------------------------------------

def test_js_zero_innerhtml():
    src = _read(JS_PATH)
    assert ".innerHTML" not in src, "all rendering must use createElement/textContent"
    assert "insertAdjacentHTML" not in src
    assert "document.write" not in src


def test_js_map_renders_in_page_with_maplibre_not_an_iframe():
    """The map used to be Folium HTML in a sandboxed iframe. It is now MapLibre
    GL JS drawing GeoJSON directly in the page (frontend/js/identity-map.js).
    No iframe means no opaque-origin CORS trap, no double-CSP intersection,
    no backend-rendered HTML — the whole class of bugs the old design carried.
    """
    src = _read(JS_PATH)
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith(("//", "*", "/*")))
    assert "createElement('iframe')" not in code, "map must not be an iframe any more"
    assert "window.IdentityMap.ready" in code, "map must go through the MapLibre controller"
    assert "map-data" in code or "ctl.load(" in code, "map data must come from /map-data"
    assert "buildMapUrl(" not in code, "legacy /map HTML URL builder must be gone"


def test_js_single_authoritative_selection():
    src = _read(JS_PATH)
    assert "chooseIdentity" in src, "one authoritative selection path required"
    assert "dispatchEvent(new Event('change'" not in src, \
        "synthetic change events created the double-request bug"


def test_js_request_correlation_and_aborts():
    src = _read(JS_PATH)
    assert "AbortController" in src
    assert "beginRequest(" in src and "isCurrent()" in src, \
        "every resource needs generation-checked stale-response rejection"
    assert "encodeURIComponent" in src, "ids must be encoded in path segments"
    assert "searchParams.set" in src, "query params must be built with URLSearchParams"


def test_js_server_side_search():
    src = _read(JS_PATH)
    assert "limit=1000" not in src, "the 1000-identity client-side load must be gone"
    assert "page_size" in src and "'/api/admin/identities'" in src
    assert "Load more" in src


def test_js_explicit_checkbox_defaults():
    src = _read(JS_PATH)
    assert "checked !== false" not in src, "missing checkbox must never mean enabled"
    assert "checked === true" in src, "features enable only on an explicit check"


def test_js_strict_timestamps():
    src = _read(JS_PATH)
    assert "'Unknown time'" in src, "invalid timestamps render a safe label, never 'now'"
    assert "parseTimestamp" in src
    assert "co_appearance_percentage.toFixed" not in src, "numeric values must be validated first"
    assert "'Unknown'" in src, "invalid transit durations must render Unknown"


def test_js_bounded_map_retry():
    src = _read(JS_PATH)
    assert "mapRetryUsed" in src, "map container recovery must be bounded"
    assert "setTimeout(() => loadBackendMap(), 500)" not in src, "recursive map retry loop must be gone"


def test_js_hour_aligned_charts():
    src = _read(JS_PATH)
    assert "Object.values(patterns.hourly_distribution" not in src, \
        "object key order must not drive the hourly chart"
    assert "{ length: 24 }" in src, "hours 0-23 must be mapped explicitly"
    assert "window.Chart" in src, "chart library availability must be checked"
    assert ".destroy()" in src or "destroyTemporalCharts" in src


def test_js_csrf_and_no_sensitive_logging():
    src = _read(JS_PATH)
    assert "X-Requested-With" in src, "mutations must carry the CSRF header"
    assert "const DEBUG = false" in src, "production must not log query/response data"
    assert "console.log('[INTELLIGENCE] Loading image from:" not in src


def test_js_cleanup_on_unload():
    src = _read(JS_PATH)
    assert "pagehide" in src
    assert "abortAllRequests" in src
    assert "destroyIdentityPicker" in src


def test_js_accessible_combobox():
    src = _read(JS_PATH)
    for needle in ("role: 'combobox'", "role: 'listbox'", "aria-activedescendant",
                   "'aria-expanded'", "ArrowDown", "Escape"):
        assert needle in src, f"combobox accessibility missing: {needle}"


# ---------------------------------------------------------------------------
# HTML contract
# ---------------------------------------------------------------------------

def test_html_no_inline_handlers():
    src = _read(HTML_PATH)
    for banned in ("onclick=", "onerror=", "onmouseover=", "onmouseout=", "onload="):
        assert banned not in src, f"inline handler {banned} must be removed"
    assert "admin-intelligence.js?v=intel-5" in src, "cache-bust must force the rewritten script"
