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
cleanup, and the live operator-console hierarchy.
"""

import json
from html.parser import HTMLParser
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
    "Live ML activation depends on service readiness; HYBRID is unavailable.",
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
    page_script = re.search(r"<script\b[^>]*\bsrc=[\"']/frontend/js/admin-ml-ops\.js\?v=mlops-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*[\"']", html)
    assert page_script, "the page script must have an explicit cache version"
    page_at = page_script.start()
    assert -1 not in (actions_at, nav_at, page_at), "a pinned script tag is missing"
    assert actions_at < nav_at < page_at, "script order contract broken"
    for tag in re.findall(r"<script[^>]*actions\.js[^>]*>", html):
        assert "defer" not in tag
    assert "footer-loader.js" not in html, "footer-loader.js does not exist in this app"
    assert re.search(r"<link\b[^>]*\bhref=[\"']/frontend/css/admin-ml-ops\.css\?v=mlops-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*[\"']", html), "the page stylesheet is not version-pinned"
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
                       "model-evaluation", "model-evaluation-result",
                       "stop-shadow-btn", "registry-action-panel",
                       "model-detail-drawer", "shadow-summary-body",
                       "predictions-body", "predictions-fallback-only",
                       "drift-reports-body", "run-drift-btn",
                       "start-training-btn", "cancel-training-btn",
                       "build-dataset-btn", "labels-body", "create-label-btn",
                       "policy-body", "audit-body"):
        assert f'id="{element_id}"' in html, f"missing section anchor #{element_id}"


def test_operator_console_leads_with_live_backend_execution_state():
    html = read(HTML)
    js = read(JS)
    queue_at = html.find('id="operations-queue"')
    governance_at = html.find('id="system-governance"')
    assert -1 not in (queue_at, governance_at) and queue_at < governance_at
    for element_id in (
        "console-connection-state", "summary-mode", "summary-worker",
        "summary-active-jobs", "summary-last-update", "jobs-refresh-btn",
    ):
        assert f'id="{element_id}"' in html
    assert "function setConsoleConnection" in js
    assert "function setSummaryValue" in js
    assert "JOB_PRESENTATION" in js
    assert "mlops-job-row" in js
    assert "aria-valuenow" in js


def test_page_groups_the_lifecycle_into_guided_workspaces():
    html = read(HTML)
    js = read(JS)
    expected = {"overview": 3, "prepare": 6, "review": 2, "monitor": 3, "audit": 2}
    assert 'id="mlops-service-select"' in html
    assert 'id="service-workflow-milestone"' in html
    assert 'id="service-workflow-checklist"' not in html
    assert 'id="mlops-next-step-title"' not in html
    for stage in ('prepare', 'test', 'activate', 'monitor'):
        assert html.count(f'data-journey-stage="{stage}"') == 1
    assert 'id="mlops-workspace-panels"' in html
    for workspace, count in expected.items():
        assert f'data-mlops-view="{workspace}"' in html
        assert len(re.findall(f'data-mlops-panel="{workspace}"', html)) == count
    assert "function activateWorkspace" in js
    assert "function installWorkspaceNavigation" in js
    assert "function updateNextStep" in js
    assert "window.history.replaceState" in js


def test_one_journey_keeps_optional_tools_out_of_the_main_path():
    html, js = read(HTML), read(JS)
    for removed in ('mlops-runbook-title', 'mlops-next-step-title', 'service-workflow-next'):
        assert f'id="{removed}"' not in html
    for present in ('service-data-options', 'service-debug-details', 'mlops-advanced-tools'):
        assert f'id="{present}"' in html
    assert 'function updateRunbook' not in js
    assert 'function latestServiceJob' in js
    assert 'aria-current' in js
    assert 'What the status labels mean' in html


def test_lifecycle_confirmation_is_accessible_and_never_filtered_out():
    html = read(HTML)
    js = read(JS)
    class DialogAncestry(HTMLParser):
        void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                     "link", "meta", "param", "source", "track", "wbr"}

        def __init__(self):
            super().__init__()
            self.stack = []
            self.dialog_ancestors = []

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if attributes.get("id") == "registry-action-panel":
                self.dialog_ancestors.append(list(self.stack))
            if tag not in self.void_tags:
                self.stack.append((tag, attributes))

        def handle_endtag(self, tag):
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    break

    structure = DialogAncestry()
    structure.feed(html)
    assert len(structure.dialog_ancestors) == 1, "exactly one registry confirmation is required"
    ancestors = structure.dialog_ancestors[0]
    assert all(tag not in ("main", "details") for tag, _ in ancestors), \
        "confirmation must live outside the scrolling main and collapsible advanced details"
    assert all(attrs.get("id") != "mlops-workspace-panels" and
               "data-mlops-panel" not in attrs for _, attrs in ancestors), \
        "confirmation must live outside filtered workspaces"
    assert 'role="alertdialog"' in html
    assert 'aria-describedby="registry-action-description"' in html
    assert 'id="registry-action-note"' in html
    assert 'autofocus' in html
    assert "window.ModalStack.open(panel" in js
    assert "window.ModalStack.close(panel)" in js
    assert "setNote('registry-action-note'" in js


def test_plain_language_labels_and_local_action_feedback_are_present():
    html = read(HTML)
    js = read(JS)
    assert "Behavior anomaly (person)" in html
    assert "Median/MAD baseline" in html
    assert "Probability output?" in html
    assert "friendlyModelType" in js and "friendlyAlgorithm" in js
    assert 'id="registry-note"' in html
    assert 'id="drift-action-note"' in html
    assert "setNote('registry-note'" in js
    assert "setNote('drift-action-note'" in js


def test_errors_include_recovery_and_request_correlation():
    js = read(JS)
    assert "function recoveryForError" in js
    assert "function formatActionError" in js
    assert "Request ID: " in js
    assert "response.headers.get('X-Request-ID')" in js
    assert "Next step:" in js
    assert "function renderCardError" in js and "mlops-error-state" in js


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


def test_expired_session_redirects_to_the_real_signin_page():
    code = code_only(read(JS))
    assert "window.location.href = '/signin'" in code
    assert "window.location.href = '/login'" not in code


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


def test_page_script_polling_has_bounded_backoff():
    code = code_only(read(JS))
    match = re.search(r"const JOB_POLL_MAX_BACKOFF_MS = (\d+)", code)
    assert match, "no job polling backoff bound"
    assert int(match.group(1)) <= 60000
    assert "Math.min(JOB_POLL_MAX_BACKOFF_MS" in code
    assert "window.setTimeout(refreshJobs, delay)" in code


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
    assert "mlops-sr-only" in js, "tooltips need screen-reader descriptions"
    assert "aria-describedby" in js
    assert "fa-circle-question" in js and "Guide" in js
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
