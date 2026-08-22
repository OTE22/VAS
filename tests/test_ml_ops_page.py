"""
ML Milestone 5: the /admin/ml-ops page contracts.
=================================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_ml_ops_page.py -v

Pins: admin-only route serving a static file (no server-side injection);
three-sided navbar registration; version-pinned assets in the house script
order; the seven warnings verbatim in markup; scroll-region layout; and the
page script's safety contracts — DOM building only, typed normalizers,
latest-wins requests, bounded job polling, no browser dialogs, pagehide
cleanup.
"""

import json
import re
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"
FRONTEND = "/app/frontend"
HTML = f"{FRONTEND}/admin/ml-ops.html"
JS = f"{FRONTEND}/js/admin-ml-ops.js"
CSS = f"{FRONTEND}/css/admin-ml-ops.css"
NAVBAR = f"{FRONTEND}/components/admin-navbar.html"
NAV_LOADER = f"{FRONTEND}/js/navbar-loader.js"
ROUTES = "/app/backend/routes/dashboard.py"
AUTH_ROUTES = "/app/backend/routes/auth.py"

WARNINGS = [
    "Anomaly does not mean threat.",
    "Heuristic scores are not probabilities.",
    "Uncalibrated model outputs are not probabilities.",
    "Shadow mode does not affect live decisions.",
    "ML and HYBRID modes are currently gated.",
    "Drift does not automatically prove model failure.",
    "Human review remains required.",
]


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def code_only(source):
    """Strip JS comments so contract scans read code, not prose."""
    lines = []
    in_block = False
    for line in source.splitlines():
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block = True
            continue
        if stripped.startswith(("//", "*")):
            continue
        lines.append(line)
    return "\n".join(lines)


@pytest.fixture(scope="module")
def token():
    request = urllib.request.Request(
        BASE + "/api/auth/login",
        data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
        method="POST")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["access_token"]


def get_page(path, token=None):
    request = urllib.request.Request(BASE + path)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.geturl(), response.read().decode(errors="replace")


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

def test_ml_ops_page_serves_for_an_admin(token):
    status, landed, body = get_page("/admin/ml-ops", token)
    assert status == 200
    assert "signin" not in landed.lower()
    assert 'id="mode-cards"' in body, "the ml-ops markup is not being served"
    assert "admin-ml-ops.js" in body


def test_ml_ops_page_requires_authentication():
    status, landed, _ = get_page("/admin/ml-ops")
    assert "signin" in landed.lower(), (
        f"unauthenticated access served the page instead of redirecting: {landed}")


def test_route_serves_a_static_file_without_injection():
    source = read(ROUTES)
    parts = source.split('@router.get("/admin/ml-ops")', 1)
    assert len(parts) == 2, "the ml-ops page route is gone"
    body = parts[1].split("@router.get", 1)[0]
    assert "FileResponse" in body
    assert 'allowed_roles=["admin"]' in body, "the page must stay admin-only"
    assert "json.dumps" not in body, "the route injects data server-side"


# ---------------------------------------------------------------------------
# Three-sided navbar registration
# ---------------------------------------------------------------------------

def test_navbar_registration_is_three_sided():
    assert 'href="/admin/ml-ops"' in read(NAVBAR), "navbar markup misses ml-ops"
    assert "'/admin/ml-ops'" in read(NAV_LOADER), "navbar-loader cannot highlight ml-ops"
    auth_src = read(AUTH_ROUTES)
    assert 'page="ml-ops"' in auth_src, "auth NavbarLink for ml-ops is missing"
    assert 'parent_page="system"' in auth_src


# ---------------------------------------------------------------------------
# HTML contracts
# ---------------------------------------------------------------------------

def test_page_chrome_follows_the_house_rules():
    html = read(HTML)
    assert 'id="navbar-placeholder"' in html
    assert 'class="intelligence-main-container"' in html, "scroll-region wrapper missing"
    assert 'class="intelligence-content"' in html, "the page must scroll inside its content region"
    # Version-pinned assets in the required order; actions.js is never deferred.
    actions_at = html.find("js/actions.js?v=actions-1")
    nav_at = html.find("js/navbar-loader.js?v=nav-7")
    page_at = html.find("js/admin-ml-ops.js?v=mlops-7")
    assert -1 not in (actions_at, nav_at, page_at), "a pinned script tag is missing"
    assert actions_at < nav_at < page_at, "script order contract broken"
    for tag in re.findall(r"<script[^>]*actions\.js[^>]*>", html):
        assert "defer" not in tag
    assert "footer-loader.js" not in html, "footer-loader.js does not exist in this app"
    assert "admin-ml-ops.css?v=mlops-7" in html, "the page stylesheet is not version-pinned"
    assert "onclick=" not in html, "no inline handlers"


def test_the_seven_warnings_render_verbatim_in_markup():
    html = read(HTML)
    for warning in WARNINGS:
        assert warning in html, f"warning missing from the page: {warning!r}"


def test_page_surfaces_every_operations_section():
    html = read(HTML)
    for element_id in ("current-mode-badge", "mode-cards", "pause-ml-btn",
                       "label-readiness-body", "data-readiness-body",
                       "optional-capabilities-body", "models-table-body",
                       "stop-shadow-btn", "registry-action-panel",
                       "model-detail-drawer", "shadow-summary-body",
                       "predictions-body", "predictions-fallback-only",
                       "drift-reports-body", "run-drift-btn",
                       "start-training-btn", "cancel-training-btn",
                       "build-dataset-btn", "labels-body", "create-label-btn",
                       "policy-body", "audit-body"):
        assert f'id="{element_id}"' in html, f"missing section anchor #{element_id}"


# ---------------------------------------------------------------------------
# JS contracts
# ---------------------------------------------------------------------------

def test_page_script_builds_dom_without_markup_injection():
    code = code_only(read(JS))
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in code, f"markup-injection sink {sink} in admin-ml-ops.js"
    assert "createElement" in code
    assert "textContent" in code


def test_page_script_ships_with_debug_off_and_no_dialogs():
    code = code_only(read(JS))
    assert re.search(r"const DEBUG = false", code), "DEBUG must ship false"
    for dialog in ("alert(", "confirm(", "prompt("):
        assert dialog not in code, f"browser dialog {dialog} used"


def test_page_script_uses_typed_normalizers_not_silent_coercion():
    code = code_only(read(JS))
    for helper in ("function toBoolean", "function toFiniteNumber", "function formatMetric"):
        assert helper in code, f"{helper} missing"
    assert "'N/A'" in code, "absent metrics must render N/A, not zero"
    assert not re.search(r"\|\|\s*0\b", code), "silent ||0 coercion"
    assert not re.search(r"\|\|\s*false\b", code), "silent ||false coercion"


def test_page_script_requests_are_latest_wins_and_cleaned_up():
    code = code_only(read(JS))
    assert "AbortController" in code
    assert "isCurrent" in code, "stale responses could overwrite newer state"
    assert "'pagehide'" in code, "no pagehide cleanup"
    assert "abortAllRequests" in code
    assert "X-Requested-With" in code, "mutations would fail the CSRF check"
    assert "no-store" in code


def test_page_script_polling_is_bounded():
    code = code_only(read(JS))
    match = re.search(r"const JOB_POLL_MAX = (\d+)", code)
    assert match, "no JOB_POLL_MAX bound"
    assert int(match.group(1)) <= 1000
    assert "attempt > JOB_POLL_MAX" in code, "the poll loop never checks its bound"


def test_page_script_exposes_no_filesystem_paths():
    code = code_only(read(JS))
    for needle in ("/app/", "models/ml", "artifact_path", "storage_path"):
        assert needle not in code, f"page script references server path token {needle!r}"


# ---------------------------------------------------------------------------
# Section help + call log (mlops-4)
# ---------------------------------------------------------------------------

def test_every_card_has_section_help_and_the_modal_exists():
    import re
    with open("/app/frontend/admin/ml-ops.html", encoding="utf-8") as f:
        html = f.read()
    with open("/app/frontend/js/admin-ml-ops.js", encoding="utf-8") as f:
        js = f.read()
    cards = re.findall(r'<div class="mlops-card(?: mlops-card-wide)?"([^>]*)>', html)
    keys = [re.search(r'data-help="([a-z_]+)"', attrs).group(1) for attrs in cards
            if re.search(r'data-help="([a-z_]+)"', attrs)]
    assert len(keys) == len(cards) >= 13, "every card carries a data-help key"
    help_keys = set(re.findall(r"^\s{8}([a-z_]+): \{\s*$", js, re.M))
    assert set(keys) <= help_keys, set(keys) - help_keys
    for key in keys:
        block = js.split(f"        {key}: {{", 1)[1].split("\n        }", 1)[0]
        assert "title:" in block and "what:" in block and "read:" in block, key
    assert 'id="mlops-help-modal"' in html and 'id="mlops-help-body"' in html
    assert 'id="calls-body"' in html and 'id="calls-errors-only"' in html
    assert "function installHelpButtons" in js and "function applyTooltips" in js
    assert "function stageStrip" in js and "progress_percent" in js
    assert "onclick=" not in html


def test_tooltip_targets_exist_in_markup():
    import re
    with open("/app/frontend/admin/ml-ops.html", encoding="utf-8") as f:
        html = f.read()
    with open("/app/frontend/js/admin-ml-ops.js", encoding="utf-8") as f:
        js = f.read()
    block = js.split("const TOOLTIPS = {", 1)[1].split("\n    };", 1)[0]
    ids = re.findall(r"^\s+'([a-z\-]+)':", block, re.M)
    assert len(ids) >= 25
    missing = [i for i in ids if f'id="{i}"' not in html]
    assert missing == [], f"tooltips name elements that do not exist: {missing}"


def test_system_state_card_reflects_backend_facts():
    with open("/app/frontend/admin/ml-ops.html", encoding="utf-8") as f:
        html = f.read()
    with open("/app/frontend/js/admin-ml-ops.js", encoding="utf-8") as f:
        js = f.read()
    assert 'id="system-state-body"' in html and 'id="system-notes-btn"' in html
    assert 'data-help="system"' in html
    assert "function renderSystemState" in js and "renderSystemState(data && data.system)" in js
    # every fact shown comes from the payload, never a literal in the page
    for literal in ("secintel-features-v1", "secintel-features-v2", "explicit-cap-v1", "b7d2f4a9c6e1"):
        assert literal not in js, f"{literal} must come from /api/ml/overview, not the script"
