"""
Security Intelligence system tests
==================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_security_intelligence_system.py -v

Proves the Security Intelligence overhaul contract:
  * /api/security/network is ALWAYS bounded (scope/truncated/totals envelope,
    server-enforced max_nodes) — never the unbounded full graph
  * /api/security/capabilities reports real backend readiness (no-store)
  * threshold learning runs as a CSRF-protected background job (202+job_id,
    409 single-flight, job-status endpoint)
  * correlation is described as association (never causation) and flags
    low-sample results; trajectory reports model version + insufficient
    evidence; both guard identity existence with 404
  * frontend (admin-security-intelligence.js / security-intelligence.html):
    zero innerHTML, zero inline handlers, sandboxed map iframe, server-side
    paginated identity search, deterministic init without fixed delays,
    component-API preselection, explicit checkbox defaults, vis lifecycle
    (destroy + physics-off), safe coordinates, no alert() dialogs
"""

import json
import uuid
import urllib.request
import urllib.error

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"

JS_PATH = "/app/frontend/js/admin-security-intelligence.js"
HTML_PATH = "/app/frontend/admin/security-intelligence.html"
ROUTES_PATH = "/app/backend/routes/intelligence.py"


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


# ---------------------------------------------------------------------------
# Bounded network graph
# ---------------------------------------------------------------------------

def test_network_is_bounded_envelope(token):
    status, body, _ = _http("GET", "/api/security/network?days_back=7", token=token, timeout=120)
    assert status == 200, body
    for key in ("nodes", "edges", "scope", "truncated", "total_nodes", "returned_nodes", "max_nodes"):
        assert key in body, f"bounded-network envelope missing {key}"
    assert body["scope"] == "top_risk", "no explicit ids must mean the top-risk slice, not the full graph"
    assert body["returned_nodes"] <= body["max_nodes"] <= 300


def test_network_max_nodes_capped(token):
    status, _, _ = _http("GET", "/api/security/network?max_nodes=5000", token=token)
    assert status == 422, "max_nodes beyond the server ceiling must be rejected"


def test_network_requires_auth():
    status, _, _ = _http("GET", "/api/security/network")
    assert status == 401


def test_network_bounds_in_source():
    src = _read(ROUTES_PATH)
    assert "NETWORK_MAX_NODES = 300" in src
    assert "NETWORK_MAX_EDGES" in src
    assert '"truncated"' in src and '"scope"' in src


# ---------------------------------------------------------------------------
# Capabilities: honest readiness, never hard-coded
# ---------------------------------------------------------------------------

def test_capabilities_report_real_status(token):
    status, body, headers = _http("GET", "/api/security/capabilities", token=token)
    assert status == 200, body
    caps = body.get("capabilities")
    assert caps, "capabilities payload required"
    for feature in ("network_analysis", "threshold_learning", "trajectory_prediction",
                    "activity_correlation", "map_generation", "offline_maps"):
        assert feature in caps, f"missing capability {feature}"
        assert "enabled" in caps[feature] and "status" in caps[feature]
    assert body["checked_at"].endswith("Z")
    assert "no-store" in (headers.get("Cache-Control") or "").lower()


def test_capabilities_requires_auth():
    status, _, _ = _http("GET", "/api/security/capabilities")
    assert status == 401


# ---------------------------------------------------------------------------
# Threshold learning: background job, CSRF, single-flight
# ---------------------------------------------------------------------------

def test_threshold_job_requires_csrf(cookie):
    status, body, _ = _http("POST", "/api/intelligence/thresholds/jobs", body={}, cookie=cookie)
    assert status == 403, f"cookie POST without CSRF header must be blocked: {status} {body}"


def test_legacy_threshold_learn_requires_csrf(cookie):
    status, _, _ = _http("POST", "/api/intelligence/thresholds/learn", body={}, cookie=cookie)
    assert status == 403


def test_threshold_job_scheduling(cookie):
    status, body, _ = _http("POST", "/api/intelligence/thresholds/jobs", body={}, cookie=cookie,
                            headers={"X-Requested-With": "XMLHttpRequest"})
    # 202 = scheduled; 409 = single-flight guard — both prove the work is
    # not executed inside the HTTP request
    assert status in (202, 409), body
    if status == 202:
        assert body["accepted"] is True
        assert body["status"] == "scheduled"
        assert body["job_id"].startswith("threshold-")
    else:
        assert body["detail"]["error_code"] == "JOB_ALREADY_RUNNING"
        assert body["detail"]["job_id"]


def test_threshold_job_status_endpoint(token):
    status, _, _ = _http("GET", "/api/intelligence/thresholds/jobs/threshold-doesnotexist", token=token)
    assert status == 404


def test_threshold_single_flight_guard_logic():
    import importlib
    intel = importlib.import_module("backend.routes.intelligence")
    intel._THRESHOLD_JOB["job_id"] = None
    intel._THRESHOLD_JOB["started_at"] = None
    assert intel._try_acquire_threshold_job("threshold-t1") is None
    assert intel._try_acquire_threshold_job("threshold-t2") == "threshold-t1"
    intel._release_threshold_job("threshold-t1")
    assert intel._threshold_job_running() is None


# ---------------------------------------------------------------------------
# Correlation + trajectory semantics
# ---------------------------------------------------------------------------

def test_correlation_guards_identity_existence(token):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    status, body, _ = _http(
        "GET", f"/api/intelligence/correlation/calculate?identity_a={a}&identity_b={b}", token=token)
    assert status == 404, body


def test_trajectory_guards_identity_existence(token):
    ghost = str(uuid.uuid4())
    status, body, _ = _http(
        "GET", f"/api/intelligence/trajectory/predict?identity_id={ghost}&current_camera=cam1", token=token)
    assert status == 404, body


def test_no_causality_claims_in_backend():
    src = _read(ROUTES_PATH)
    assert "causal" not in src.lower(), "correlation must never be described as causation"
    assert "does not prove causation" in src
    assert "insufficient_evidence" in src
    assert "CORRELATION_MIN_SEQUENCES" in src
    # The ban extends to the core analyzer — its docstring used to open with
    # "Detects causal relationships".
    core = _read("/app/backend/core/activity_correlation.py")
    assert "causal" not in core.lower(), "core correlation module claims causation"
    assert "does not prove causation" in core


def test_trajectory_reports_model_metadata():
    src = _read(ROUTES_PATH)
    assert "TRAJECTORY_MODEL_VERSION" in src
    assert "model_version=TRAJECTORY_MODEL_VERSION" in src
    assert "insufficient_evidence=len(predictions) == 0" in src


def test_security_endpoints_audited():
    src = _read(ROUTES_PATH)
    for action in ("social_network", "suspicious_patterns", "anomaly_detection",
                   "threat_assessment", "trajectory_prediction", "activity_correlation",
                   "threshold_job_scheduled"):
        assert f'_audit("{action}"' in src, f"missing audit for {action}"


# ---------------------------------------------------------------------------
# Frontend source contracts (admin-security-intelligence.js)
# ---------------------------------------------------------------------------

def test_js_zero_innerhtml():
    src = _read(JS_PATH)
    assert ".innerHTML" not in src
    assert "insertAdjacentHTML" not in src
    assert "document.write" not in src


def test_js_map_renders_in_page_with_maplibre_not_an_iframe():
    """See tests/test_intelligence_system.py — same migration, same rule."""
    src = _read(JS_PATH)
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith(("//", "*", "/*")))
    assert "createElement('iframe')" not in code, "map must not be an iframe any more"
    assert "window.IdentityMap.ready" in code, "map must go through the MapLibre controller"
    assert "buildMapUrl(" not in code, "legacy /map HTML URL builder must be gone"


def test_js_no_thousand_identity_load():
    src = _read(JS_PATH)
    assert "limit=1000" not in src, "the 1000-identity browser load must be gone"
    assert "page_size" in src and "'/api/admin/identities'" in src
    assert "Load more" in src


def test_js_deterministic_init_no_delays():
    src = _read(JS_PATH)
    assert "initializeSecurityIntelligence" in src
    assert "await Promise.all([loadPipelines(), loadCapabilities()])" in src
    assert "applyPreselectedUrlState" in src
    assert "Wait for dropdowns to populate" not in src, "the 500ms preselection guess must be gone"
    assert ", 500)" not in src


def test_js_component_api_preselection():
    src = _read(JS_PATH)
    assert "component.setValue = async function" in src, "preselection must go through the component API"
    assert "dispatchEvent(new Event('change'" not in src, "synthetic change events caused double loads"


def test_js_valid_tabs_guarded():
    src = _read(JS_PATH)
    assert "VALID_SECURITY_TABS" in src
    assert "VALID_SECURITY_TABS.has(rawTab) ? rawTab : 'network'" in src


def test_js_safe_coordinates():
    src = _read(JS_PATH)
    assert "normalizeCoordinate" in src
    assert "pipeline.latitude && pipeline.longitude" not in src, "zero coordinates must be valid"
    assert "latitude.toFixed" not in src, "numeric strings must not crash coordinate rendering"


def test_js_explicit_checkbox_defaults():
    src = _read(JS_PATH)
    assert "checked !== false" not in src, "missing checkbox must never mean enabled"
    assert "checked === true" in src


def test_js_request_correlation_and_aborts():
    src = _read(JS_PATH)
    assert "AbortController" in src
    assert "beginRequest(" in src and "isCurrent()" in src
    assert "encodeURIComponent" in src
    assert "searchParams.set" in src


def test_js_vis_network_lifecycle():
    src = _read(JS_PATH)
    assert "destroyNetworkInstance" in src
    assert "stabilizationIterationsDone" in src
    assert "physics: { enabled: false }" in src
    assert "window.vis" in src, "vis availability must be checked before construction"


def test_js_network_values_validated():
    src = _read(JS_PATH)
    assert "normalizeNetworkNode" in src and "normalizeNetworkEdge" in src
    assert "clamp(raw.risk_score, 0, 100)" in src
    assert "risk_score.toFixed" not in src, "unvalidated .toFixed() calls must be gone"
    assert "truncated" in src, "graph truncation must be surfaced to the analyst"


def test_js_association_not_causation():
    src = _read(JS_PATH)
    assert "causal" not in src.lower()
    assert "does not prove causation" in src
    assert "insufficient_evidence" in src


def test_js_threshold_background_job_flow():
    src = _read(JS_PATH)
    assert "'/api/intelligence/thresholds/jobs'" in src, "threshold learning must use the job endpoint"
    assert "pollThresholdJob" in src
    assert "JOB_POLL_MAX" in src, "job polling must be bounded"


def test_js_capabilities_driven_status():
    src = _read(JS_PATH)
    assert "'/api/security/capabilities'" in src
    assert "'Enabled'" not in src, "feature status must never be hard-coded"


def test_js_no_browser_alerts():
    src = _read(JS_PATH)
    assert "alert(" not in src, "alert/confirm must be replaced with accessible modals"
    assert "showModal" in src
    assert "role: 'dialog'" in src or "'aria-modal'" in src


def test_js_strict_timestamps_and_numbers():
    src = _read(JS_PATH)
    assert "parseServerTimestamp" in src
    assert "'Unknown time'" in src
    assert "toFiniteNumber" in src and "formatPercent01" in src


def test_js_cleanup_on_unload():
    src = _read(JS_PATH)
    assert "pagehide" in src
    assert "abortAllRequests" in src
    assert "destroySelector" in src
    assert "const DEBUG = false" in src


def test_js_single_outside_click_listener():
    src = _read(JS_PATH)
    assert "openSelectorPanels" in src, "one shared outside-click manager, not one listener per dropdown"
    assert src.count("document.addEventListener('click'") == 1


def test_js_accessible_selector():
    src = _read(JS_PATH)
    for needle in ("role: 'combobox'", "role: 'listbox'", "aria-activedescendant",
                   "'aria-expanded'", "ArrowDown", "Escape", "aria-multiselectable"):
        assert needle in src, f"selector accessibility missing: {needle}"


# ---------------------------------------------------------------------------
# HTML contract
# ---------------------------------------------------------------------------

def test_html_no_inline_handlers():
    src = _read(HTML_PATH)
    for banned in ("onclick=", "onerror=", "onmouseover=", "onmouseout=", "onload="):
        assert banned not in src, f"inline handler {banned} must be removed"
    assert "admin-security-intelligence.js?v=sec-7" in src


def test_html_security_features_opt_in():
    src = _read(HTML_PATH)
    assert 'id="map-enable-security" checked' not in src.replace("  ", " ")
    assert 'id="map-detect-patterns" checked' not in src.replace("  ", " ")
    assert 'id="map-show-heatmap" checked' not in src.replace("  ", " ")


def test_html_no_causality_claims():
    src = _read(HTML_PATH)
    assert "causal" not in src.lower()
    assert "does not prove causation" in src
