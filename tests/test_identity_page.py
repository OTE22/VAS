"""The dedicated identity profile page (/admin/identity/{id}).

The app's first path-parameter HTML route. It exists because "Open full
profile" had nowhere honest to land: identity detail lived only in a modal on
the Unknown Faces Center, so a KNOWN person's profile carried an "unknown" URL
and framing — flagged twice by the user before this page was built.
"""

import json
import re
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"
FRONTEND = "/app/frontend"
HTML = f"{FRONTEND}/admin/identity.html"
JS = f"{FRONTEND}/js/admin-identity.js"
ROUTES = "/app/backend/routes/dashboard.py"


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def code_only(source):
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


def get_json(path, token):
    request = urllib.request.Request(BASE + path)
    request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def get_page(path, token=None):
    request = urllib.request.Request(BASE + path)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.geturl(), response.read().decode(errors="replace")


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

def test_identity_page_serves_for_an_admin(token):
    identities = get_json("/api/admin/identities?limit=1&type=known", token).get("identities", [])
    if not identities:
        pytest.skip("no identities present")
    status, landed, body = get_page("/admin/identity/" + identities[0]["id"], token)
    assert status == 200
    assert "signin" not in landed.lower()
    assert 'id="identity-profile"' in body, "the identity page markup is not served"
    assert "admin-identity.js" in body


def test_identity_page_requires_authentication():
    status, landed, _ = get_page("/admin/identity/00000000-0000-0000-0000-000000000000")
    assert "signin" in landed.lower(), (
        f"unauthenticated access served the page instead of redirecting: {landed}"
    )


def test_identity_page_serves_the_same_file_for_any_id(token):
    """The server injects nothing — a malformed id still gets the page, and
    the client renders its own error state. (Server-side data injection is the
    CSP-violating pattern test_ml_model_system pins as removed.)"""
    status, landed, body = get_page("/admin/identity/not-a-uuid", token)
    assert status == 200
    assert "signin" not in landed.lower()
    assert 'id="identity-error"' in body

    source = read(ROUTES)
    route = source.split('@router.get("/admin/identity/{identity_id}")', 1)
    assert len(route) == 2, "the identity page route is gone"
    body_src = route[1].split("@router.get", 1)[0]
    assert "FileResponse" in body_src
    assert "json.dumps" not in body_src, "the route injects data server-side"


def test_identity_page_redirects_pipeline_less_users(token):
    """Same audience rule as /admin/unknown: a non-admin with no pipelines is
    sent to /dashboard, fail-closed."""
    import sys
    sys.path.insert(0, "/app/tests")
    from conftest import run_on_shared_loop

    from sqlalchemy import text as sa_text
    from db_connection import db_manager
    from backend.auth.password import hash_password

    username = "identity_page_probe"
    password = "Probe-Page-Passw0rd!"

    async def create():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(sa_text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.execute(sa_text("""
                INSERT INTO users (username, email, full_name, password_hash, role,
                                   is_active, can_use_chatbot, permissions_version, created_at)
                VALUES (:u, :e, 'Identity Page Probe', :h, 'user', true, false, 1, now())
            """), {"u": username, "e": f"{username}@example.test",
                   "h": hash_password(password)})
            await db.commit()

    async def destroy():
        async with db_manager.get_session() as db:
            await db.execute(sa_text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.commit()

    run_on_shared_loop(create())
    try:
        login = urllib.request.Request(
            BASE + "/api/auth/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            method="POST")
        login.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(login, timeout=30) as response:
            probe_token = json.loads(response.read())["access_token"]

        _, landed, _ = get_page(
            "/admin/identity/00000000-0000-0000-0000-000000000000", probe_token)
        assert "/dashboard" in landed, (
            f"pipeline-less non-admin was not redirected to /dashboard: {landed}"
        )
    finally:
        run_on_shared_loop(destroy())


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------

def test_page_reads_the_id_from_the_path_and_validates_it():
    source = code_only(read(JS))
    assert "location.pathname" in source, "the id must come from the URL path"
    assert "UUID_RE" in source
    init = source.split("async function init(", 1)[1]
    assert init.index("UUID_RE.test") < init.index("loadPipelineNames"), (
        "network calls happen before the id is validated"
    )


def test_ported_modals_lost_their_injection_sinks():
    """The originals had two: the watchlist <select> interpolated the
    operator-supplied watchlist NAME into innerHTML unescaped, and live-alert
    warnings were string-built. Both are DOM-built here."""
    source = code_only(read(JS))

    watchlist = source.split("async function openWatchlistModal", 1)[1].split("\n    async function ", 1)[0]
    assert "replaceChildren" in watchlist and "new Option(" in watchlist
    assert "innerHTML" not in watchlist

    alert = source.split("async function openLiveAlertModal", 1)[1].split("\n    async function ", 1)[0]
    assert "innerHTML" not in alert, "live-alert warnings are string-built again"
    assert "replaceChildren" in alert


def test_live_alert_creation_no_longer_hard_redirects():
    """The Unknown-page original bounced to /admin/live-alerts 800 ms after
    success. A profile page stays put and reports."""
    source = code_only(read(JS))
    create = source.split("async function createLiveAlert", 1)[1].split("\n    function ", 1)[0]
    assert "window.location" not in create
    assert "showNotification" in create


def test_promote_and_merge_stay_in_the_unknown_center():
    """They are unknown-triage workflows; the page links there for unknown
    identities instead of duplicating the flows."""
    source = code_only(read(JS))
    actions = source.split("function renderActions", 1)[1].split("\n    function ", 1)[0]
    assert "/admin/unknown?view=" in actions
    assert "isUnknown" in actions, "the manage link is not gated on type"

    markup = read(HTML)
    assert 'id="profile-manage-link"' in markup


def test_snapshot_urls_are_guarded_in_the_timeline():
    """The ported timeline renderer is the page's one string renderer; every
    interpolation is escaped and image URLs go through the same-origin guard."""
    source = read(JS)
    timeline = source.split("function renderAdvancedTimeline", 1)[1].split("\n    function applyTimelineScale", 1)[0]
    assert "safeImageUrl(" in timeline
    assert "escapeHtml(" in timeline
    # The raw fields never reach the markup unescaped.
    for raw in ("${app.pipeline_id}", "${app.track_id}", "${app.snapshot_url}"):
        assert raw not in timeline, f"unescaped interpolation: {raw}"


def test_timeline_scrolls_inside_its_container_not_the_page():
    """The rendered track is a fixed 2880px (24h x 120px/h). On the Unknown
    page it sits inside .advanced-timeline-container (overflow-x: auto); the
    first port here injected it bare, so the track widened the whole page.
    The wrapper is the fix — it must be the injection target."""
    markup = read(HTML)
    assert re.search(
        r'id="identity-timeline"[^>]*class="[^"]*advanced-timeline-container',
        markup) or re.search(
        r'class="[^"]*advanced-timeline-container[^"]*"[^>]*id="identity-timeline"',
        markup), "the timeline is injected outside the scroll wrapper"

    # The wrapper class actually scrolls (defined in admin.css, which the
    # page loads).
    admin_css = read(f"{FRONTEND}/css/admin.css")
    block = re.search(r"^\.advanced-timeline-container\s*\{([^}]*)\}", admin_css, re.M | re.S)
    assert block and "overflow-x: auto" in block.group(1), (
        ".advanced-timeline-container no longer scrolls horizontally"
    )

    # Page CSS carries the fit guards and is versioned so they actually ship.
    page_css = read(f"{FRONTEND}/css/admin-identity.css")
    assert "max-width: 100%" in page_css
    assert "admin-identity.css?v=" in markup, "unversioned CSS hides fixes behind cache"


def test_page_is_its_own_scroll_region():
    """The decisive fit bug: this app is an app-shell — admin.css sets
    `body { height: 100vh; overflow: hidden }`, so the DOCUMENT never scrolls.
    Every page must be its own scroll region (other pages use
    .intelligence-content). Without overflow-y on .identity-page, everything
    below the first viewport was clipped and unreachable."""
    # Strip /* ... */ first: the rule's own comment QUOTES the body CSS —
    # including a `}` — which truncates a naive [^}]* match.
    page_css = re.sub(r"/\*.*?\*/", "", read(f"{FRONTEND}/css/admin-identity.css"), flags=re.S)
    block = re.search(r"^\.identity-page\s*\{([^}]*)\}", page_css, re.M | re.S)
    assert block, ".identity-page rule is gone"
    body = block.group(1)
    assert "overflow-y: auto" in body, (
        "the page has no scroll region — content below the fold is clipped "
        "(body is overflow: hidden in this app)"
    )
    assert "flex: 1" in body and "min-height: 0" in body, (
        "without flex sizing the scroll region does not fill the shell"
    )

    # The premise this fix rests on: if the app-shell model ever changes,
    # this test should be revisited rather than silently passing.
    admin_css = read(f"{FRONTEND}/css/admin.css")
    body_block = re.search(r"^body\s*\{([^}]*)\}", admin_css, re.M | re.S)
    assert body_block and "overflow: hidden" in body_block.group(1)


def test_page_chrome_matches_the_house_rules():
    markup = read(HTML)
    assert 'id="navbar-placeholder"' in markup
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', markup)
    assert scripts and "actions.js" in scripts[0], "actions.js must load first"
    assert not re.search(r"\son(?:click|change|submit|input|load|error)\s*=", markup), (
        "inline handlers violate CSP"
    )
    assert "admin.css" in markup and "admin-identity.css" in markup


def test_deep_links_now_target_the_identity_page():
    """All three call sites that used to send people to the Unknown Faces
    Center now use the dedicated page."""
    for path, marker in (
        (f"{FRONTEND}/js/admin-search.js", "/admin/identity/"),
        (f"{FRONTEND}/js/admin-intelligence.js", "/admin/identity/"),
        (f"{FRONTEND}/js/dashboard.js", "/admin/identity/"),
    ):
        assert marker in code_only(read(path)), f"{path} still bypasses the identity page"

    # The navbar keeps UNKNOWN FACES highlighted on the new page.
    assert "/admin/identity/" in read(f"{FRONTEND}/js/navbar-loader.js")
