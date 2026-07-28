"""Home dashboard: data contract, safety discipline, and the pipeline dropdown.

The redesign renders every /api/stats field. These tests pin the parts that
break silently: field paths that must exist in the live response, formatter
edge cases the brief called out by name, the CSP/textContent discipline, and
the pipeline dropdown's source endpoint (the obvious one returns bare UUID
strings, which is unusable as a human-facing list).
"""

import json
import re
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"
HOME_HTML = "/app/frontend/home.html"
HOME_JS = "/app/frontend/js/home.js"
HOME_CSS = "/app/frontend/css/home.css"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _code_only(src):
    """Strip comment lines. Several fixes here are DOCUMENTED by naming the
    thing they avoid (innerHTML, data-action), and a naive substring check
    reads the explanation as the violation."""
    return "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith(("//", "*", "/*"))
    )


@pytest.fixture(scope="module")
def token():
    req = urllib.request.Request(
        BASE + "/api/auth/login",
        data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
        method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["access_token"]


def _get(path, token):
    req = urllib.request.Request(BASE + path)
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Backend contract the dashboard reads
# ---------------------------------------------------------------------------

def test_stats_contract_supplies_every_field_the_dashboard_renders(token):
    """Each path below is read by home.js. A rename on the backend would make
    the card render an em-dash forever with nothing failing loudly."""
    stats = _get("/api/stats", token)

    required = [
        ("service",), ("version",), ("timestamp",),
        ("pipelines", "active"), ("pipelines", "total_detections"),
        ("faces", "total"),
        ("queue", "queue_size"), ("queue", "processing"), ("queue", "max_size"),
        ("queue", "total_received"), ("queue", "total_processed"), ("queue", "total_skipped"),
        ("storage", "total_size_mb"), ("storage", "total_size_gb"),
        ("storage", "file_count"), ("storage", "max_storage_gb"), ("storage", "usage_percent"),
        ("cache", "enabled"), ("cache", "healthy"),
        ("tracker", "enabled"), ("tracker", "active_pipelines"),
        ("tracker", "total_tracked_faces"), ("tracker", "window_seconds"),
        ("tracker", "frontend_notification_window_seconds"),
        ("tracker", "memory_usage_mb"), ("tracker", "memory_limit_mb"),
        ("tracker", "total_faces_added"), ("tracker", "total_faces_skipped"),
        ("tracker", "total_duplicates_prevented"),
        ("retention_days",),
    ]
    missing = []
    for path in required:
        node = stats
        for key in path:
            if not isinstance(node, dict) or key not in node:
                missing.append(".".join(path))
                break
            node = node[key]
    assert not missing, f"/api/stats no longer supplies: {missing}"


def test_database_stats_are_populated(token):
    """This was ALWAYS {} because both call sites invoked a method that does
    not exist (get_connection_stats) and a bare except swallowed the
    AttributeError — so the dashboard had no database status to show."""
    stats = _get("/api/stats", token)
    database = stats.get("database")
    assert isinstance(database, dict) and database, (
        "database stats are empty again — check db_manager.get_stats()"
    )
    assert "pool_size" in database


def test_dashboard_pipelines_carry_human_readable_names(token):
    """The dropdown needs display_name; /api/users/me/pipelines returns bare
    pipeline_id strings (UUIDs for most cameras), which is why the dropdown
    reads from /api/dashboard/pipelines instead."""
    payload = _get("/api/dashboard/pipelines", token)
    pipelines = payload["pipelines"]
    assert isinstance(pipelines, list)
    for p in pipelines:
        assert "pipeline_id" in p and "display_name" in p
        assert p["display_name"], "display_name must never be blank"
        assert "is_active" in p and "location_name" in p


# ---------------------------------------------------------------------------
# Formatter behavior the brief named explicitly
# ---------------------------------------------------------------------------

def test_percent_formatter_avoids_the_unreadable_value():
    """The brief's exact complaint: 0.000013946834951639175% must render as
    '< 0.01%', while a true zero must still render '0%' (not '< 0.01%')."""
    js = _read(HOME_JS)
    fn = re.search(r"function formatPercent\(n\) \{.*?\n\}", js, re.S)
    assert fn, "formatPercent is gone"
    body = fn.group(0)
    assert "'< 0.01%'" in body
    assert "n <= 0" in body and "'0%'" in body, (
        "a true zero must be distinguished from a tiny non-zero value"
    )
    # usage_percent arrives ALREADY as a percentage.
    assert "* 100" not in body, "formatPercent must not re-scale usage_percent"


def test_safe_number_distinguishes_zero_from_absent():
    """Number(null) === 0, so a naive helper turns a field the service never
    reported into a confident '0'."""
    js = _read(HOME_JS)
    fn = re.search(r"function safeNumber\([^)]*\) \{.*?\n\}", js, re.S)
    assert fn, "safeNumber is gone"
    assert "=== null" in fn.group(0) and "=== undefined" in fn.group(0), (
        "safeNumber must reject null/undefined before Number() coerces them to 0"
    )


# ---------------------------------------------------------------------------
# Safety discipline
# ---------------------------------------------------------------------------

def test_home_js_is_csp_safe_and_uses_textcontent():
    code = _code_only(_read(HOME_JS))
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                   "eval(", "new Function", "document.write", "javascript:"):
        assert banned not in code, f"home.js uses {banned}"
    assert "textContent" in code


def test_home_html_has_no_inline_handlers():
    html = _read(HOME_HTML)
    handlers = re.findall(r'\son(?:click|change|submit|input|load|error)\s*=', html)
    assert not handlers, f"inline handlers violate CSP: {handlers}"
    assert "<script>" not in html.replace('<script src=', ''), "inline script block"


def test_production_console_is_quiet():
    """No unconditional console.log, and the stats payload is never dumped."""
    code = _code_only(_read(HOME_JS))
    assert "console.log(" not in code, "unconditional console.log in home.js"
    assert "DEBUG_HOME" in code and "function homeDebug" in code
    assert re.search(r"const DEBUG_HOME = false", code), "debug logging left enabled"


# ---------------------------------------------------------------------------
# Pipeline dropdown wiring
# ---------------------------------------------------------------------------

def test_pipeline_dropdown_is_wired_end_to_end():
    html = _read(HOME_HTML)
    js = _read(HOME_JS)

    assert 'id="pipeline-select"' in html, "no pipeline dropdown in the markup"
    assert 'data-action-change="selectPipeline"' in html, "dropdown has no change action"
    assert "selectPipeline:" in js, "selectPipeline is not registered"

    # It must read the endpoint that carries display_name.
    loader = js.split("async function loadPipelines", 1)[1].split("\nfunction ", 1)[0]
    assert "/api/dashboard/pipelines" in loader, (
        "dropdown reads an endpoint without display_name — options would be raw UUIDs"
    )
    assert "display_name" in loader

    # Options are built with textContent, never markup.
    assert "option.textContent" in loader
    assert "innerHTML" not in _code_only(loader)


def test_pipeline_dropdown_has_an_accessible_label():
    html = _read(HOME_HTML)
    assert re.search(r'<label[^>]*for="pipeline-select"', html), (
        "the dropdown has no associated <label>"
    )


def test_dead_chip_styles_were_removed():
    """The chip list the dropdown replaced must not linger in any layer."""
    for path in (HOME_HTML, HOME_JS, HOME_CSS):
        src = _read(path)
        assert "hp-chip-list" not in src, f"dead chip markup/style in {path}"
        assert "hp-pipe" not in src, f"dead pipe markup/style in {path}"


# ---------------------------------------------------------------------------
# Structure / accessibility
# ---------------------------------------------------------------------------

def test_single_h1_and_required_sections():
    html = _read(HOME_HTML)
    assert len(re.findall(r"<h1\b", html)) == 1, "exactly one h1 expected"
    for section in ("pipelines-section", "queue-panel", "storage-panel",
                    "tracker-panel", "health-summary", "hp-error"):
        assert f'id="{section}"' in html, f"missing section: {section}"


def test_navbar_and_script_order_preserved():
    html = _read(HOME_HTML)
    assert 'id="navbar-placeholder"' in html
    actions = html.index("js/actions.js")
    navbar = html.index("js/navbar-loader.js")
    home = html.index("js/home.js")
    assert actions < navbar < home, "script order broken"
    assert "defer" not in html[actions - 60:actions + 60], "actions.js must not be deferred"


def test_auth_gating_preserved():
    """Admin styling from the enforced capability list, both branches present."""
    js = _read(HOME_JS)
    assert "admin.users.manage" in js
    assert "classList.toggle('admin-verified'" in js
    assert "classList.toggle('admin-user'" in js
    assert "chatbot.use" in js
    assert re.search(r"canUseChatbot \? '[a-z]+' : 'none'", js), (
        "#tracking-link must be both shown AND hidden"
    )
