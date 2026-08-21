"""Advanced Search page (/admin/search) — contracts the audit fixes depend on.

Each test here pins a defect that was live on the page and would come back
silently. Node is not installed in the container, so the frontend assertions
read source (the convention the other frontend tests follow); the backend
assertions call the running app.

Comment lines are stripped before any "must not contain X" check: several of
these fixes are DOCUMENTED by naming the thing they avoid (innerHTML,
include_quality, watchlist.type), and a naive substring match reads the
explanation as the violation.
"""

import glob
import json
import os
import re
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"
FRONTEND = "/app/frontend"
JS = f"{FRONTEND}/js/admin-search.js"
HTML = f"{FRONTEND}/admin/search.html"
CSS = f"{FRONTEND}/css/admin-search.css"
ACTIONS = f"{FRONTEND}/js/actions.js"


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def code_only(source):
    """Drop comment lines and block-comment bodies."""
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


def get(path, token):
    request = urllib.request.Request(BASE + path)
    request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


# ---------------------------------------------------------------------------
# The batch-search crash
# ---------------------------------------------------------------------------

def test_watchlist_alerts_type_disagreement_is_normalized():
    """/api/search/advanced returns watchlist_alerts as an ARRAY;
    /api/search/batch returns it as an INT. renderAlertsTab assumed an array
    unconditionally, so `alerts.map` threw, the throw escaped to the search
    catch, switchTab never ran, and every tab panel stayed display:none —
    an empty results panel plus a red "Search failed" toast after a search
    that had actually succeeded."""
    source = read(JS)
    assert "function normalizeAlerts(" in source
    assert "function alertCount(" in source

    body = source.split("function renderAlertsTab", 1)[1].split("\n    function ", 1)[0]
    assert "normalizeAlerts(results.watchlist_alerts)" in body, (
        "renderAlertsTab reads watchlist_alerts without normalizing its type"
    )
    # The raw field must never be indexed/iterated directly again.
    raw_uses = re.findall(r"results\.watchlist_alerts(\.\w+)", code_only(source))
    assert not raw_uses, f"raw watchlist_alerts member access: {raw_uses}"


def test_batch_alert_count_is_not_read_as_length():
    """The Alerts summary stat read `.length` off the int, so batch searches
    always showed 0 alerts."""
    source = code_only(read(JS))
    assert "results.watchlist_alerts.length" not in source
    assert "alertCount(results.watchlist_alerts)" in source


# ---------------------------------------------------------------------------
# Limits must come from the server, not from literals
# ---------------------------------------------------------------------------

def test_batch_cap_is_fetched_not_hardcoded():
    """The page staged 100 images against an enforced limit of 20, so the
    whole batch 400'd after everything had been uploaded."""
    source = code_only(read(JS))
    assert "/api/search/config" in source, "the page never asks for its limits"
    assert "state.limits.batchMaxImages" in source
    assert not re.search(r"\bmax 100\b", source), "a literal 100-file cap survives"


def test_search_config_exposes_the_upload_limits(token):
    """The cap, the size limit and the extension list all have to be
    discoverable, or the UI reinvents them and they drift again."""
    config = get("/api/search/config", token)

    assert config["batch"]["max_images"] > 0
    upload = config["upload"]
    assert upload["max_file_size_bytes"] > 0
    assert isinstance(upload["allowed_extensions"], list) and upload["allowed_extensions"]
    for extension in upload["allowed_extensions"]:
        assert extension.startswith("."), f"extension without a dot: {extension}"


def test_file_validation_runs_on_every_entry_path():
    """Validation existed only on drag-drop, and nothing anywhere checked
    size — so readAsDataURL on a multi-GB file killed the tab."""
    source = code_only(read(JS))
    assert "function validateImageFile(" in source
    assert "function filterValidImages(" in source
    # single-file path and both multi-file paths
    assert source.count("validateImageFile(") >= 2
    assert "filterValidImages(files)" in source
    assert "maxFileSizeBytes" in source, "no size limit is enforced"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def test_export_never_sends_a_parameter_no_route_accepts():
    """`include_quality` exists on neither export route and quality is always
    written, and the value was computed by `x?.checked || true` — always true.
    The checkbox lied in both directions."""
    source = code_only(read(JS))
    assert "include_quality" not in source
    assert "exportIncludeQuality" not in source
    assert "|| true" not in source, "the always-true expression is back"

    markup = read(HTML)
    assert 'id="export-include-quality"' not in markup, "the inert checkbox is back"


def test_batch_export_cannot_request_pdf():
    """export_service supports csv/json for batch; offering PDF produced a 400
    only after the user confirmed."""
    source = code_only(read(JS))
    assert "BATCH_EXPORT_FORMATS" in source
    assert "'csv', 'json'" in source
    assert "syncExportOptions" in source, "the format list is never gated on result shape"


def test_export_include_images_is_not_sent_for_batch():
    """/api/search/batch/export has no include_images parameter."""
    body = read(JS).split("async function handleExport", 1)[1].split("\n    // Store", 1)[0]
    assert "!isBatch && includeImages" in body


def test_http_errors_surface_the_servers_own_reason():
    """`error.detail || 'Export failed'` dropped the 422 list shape to
    "[object Object]", discarded the 500's request_id entirely, and threw a
    SyntaxError of its own on an nginx HTML error page."""
    source = read(JS)
    assert "async function describeHttpError(" in source
    helper = source.split("async function describeHttpError", 1)[1].split("\n    /** Throw", 1)[0]
    assert "Array.isArray(detail)" in helper, "the 422 list shape is unhandled"
    assert "request_id" in helper, "the 500 request_id is discarded"
    assert "catch" in helper, "a non-JSON body still rejects"

    # No call site reconstructs the old pattern.
    assert "error.detail ||" not in code_only(source)


# ---------------------------------------------------------------------------
# Watchlist rendering
# ---------------------------------------------------------------------------

def test_csv_export_survives_a_match_with_no_watchlist(token):
    """Observed live: "'NoneType' object has no attribute 'get'" on every CSV
    export. watchlist_match is Optional, so the key is PRESENT with the value
    None for any match not on a watchlist — the common case — and
    `match.get('watchlist_match', {})` returns the default only for an ABSENT
    key, so it handed back None."""
    payload = {
        "search_id": "test",
        "summary": {"total_faces_detected": 1},
        "faces": [{
            "face_index": 0,
            "quality_score": 0.9,
            "matches": [{
                "identity_id": "abc",
                "display_name": "Someone",
                "type": "known",
                "similarity": 0.91,
                "confidence_band": "HIGH",
                "watchlist_match": None,     # <-- the crash
            }],
        }],
        "watchlist_alerts": [],
    }
    request = urllib.request.Request(
        BASE + "/api/search/export?format=csv",
        data=json.dumps(payload).encode(), method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode()
    assert response.status == 200
    assert "Someone" in body, "the match is missing from the CSV"


def test_export_survives_null_containers(token):
    """The results body is client-supplied, so faces/matches/summary can all
    arrive as an explicit null. None of them may 500."""
    for payload in ({"faces": None, "summary": None, "watchlist_alerts": None},
                    {"faces": [{"matches": None}]},
                    {}):
        for fmt in ("csv", "json"):
            request = urllib.request.Request(
                BASE + f"/api/search/export?format={fmt}",
                data=json.dumps(payload).encode(), method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("Authorization", "Bearer " + token)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    assert response.status == 200
            except urllib.error.HTTPError as exc:
                assert exc.code < 500, (
                    f"{fmt} export 500s on {payload}: {exc.read()[:200]}"
                )


def test_watchlists_render_from_alert_level_not_type():
    """The serializer emits alert_level and has no `type` field, so every
    branch on watchlist.type was dead: generic icon, label "custom"."""
    source = code_only(read(JS))
    assert "watchlist.type" not in source
    assert "ALERT_LEVEL_ICONS" in source
    assert "alert_level" in source


def test_watchlist_serializer_still_emits_alert_level(token):
    """If the API renamed this field the icons would silently go generic
    again — which is exactly how the `type` branches died."""
    watchlists = get("/api/watchlists", token)
    if not watchlists:
        pytest.skip("no watchlists configured")
    entry = watchlists[0]
    assert "alert_level" in entry, "the field the UI reads is gone"
    assert "type" not in entry, "a `type` field reappeared — recheck the mapping"
    assert entry["alert_level"] in {"info", "warning", "critical"}


# ---------------------------------------------------------------------------
# Injection surface
# ---------------------------------------------------------------------------

def test_no_javascript_string_escape_is_used_as_html_escaping():
    r"""`.replace(/'/g, "\\'")` is a JavaScript string escape emitted into an
    HTML attribute: it leaves `"` and `<` untouched and mangles O'Brien."""
    source = code_only(read(JS))
    assert r"""replace(/'/g, "\\'")""" not in source

    # A single-character replace outside escapeHtml is the anti-pattern; the
    # chained replaces INSIDE escapeHtml are the fix, so exclude that function.
    outside = source.replace(
        source.split("function escapeHtml", 1)[1].split("\n    }", 1)[0], "")
    assert "replace(/\"/g, '&quot;')" not in outside, (
        "single-character attribute escaping is not HTML escaping"
    )


def test_escape_helper_covers_all_five_characters():
    source = read(JS)
    body = source.split("function escapeHtml", 1)[1].split("\n    /**", 1)[0]
    for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert entity in body, f"escapeHtml does not emit {entity}"
    assert body.index("&amp;") < body.index("&lt;"), (
        "the ampersand must be replaced first or the other entities are double-escaped"
    )


def test_untrusted_values_never_reach_innerHTML_unescaped():
    """Every remaining template-literal renderer interpolates through
    escapeHtml; the high-value ones (alerts, file names, pipeline options)
    are DOM-built instead."""
    # Only lines that build markup. `setAttribute('aria-label', `Remove
    # ${file.name}`)` interpolates the same value into a DOM property, which
    # is never parsed as HTML — flagging it would be a false positive.
    markup_lines = "\n".join(
        line for line in code_only(read(JS)).splitlines()
        if "<" in line or "innerHTML" in line
    )

    for sink in ("${identity.display_name}", "${watchlist.name}",
                 "${alert.notes}", "${alert.action_instructions}",
                 "${alert.identity_name}", "${alert.list_name}",
                 "${file.name}", "${imgResult.image_name}",
                 "${imgResult.error_message}", "${face.skip_reason}",
                 "${face.quality_warning}", "${match.display_name}",
                 "${p.pipeline_id}", "${pipeline.pipeline_id}",
                 "${snapshotUrl}"):
        assert sink not in markup_lines, f"unescaped interpolation: {sink}"


def test_snapshot_url_is_constrained_to_same_origin():
    """snapshot_url went from the API straight into an img src with no parse:
    a protocol-relative //host/x would have loaded cross-origin."""
    source = read(JS)
    assert "function safeImageUrl(" in source
    guard = source.split("function safeImageUrl", 1)[1].split("\n    //", 1)[0]
    assert "'//'" in guard, "protocol-relative URLs are accepted"
    assert "'..'" in guard, "path traversal is accepted"
    assert "safeImageUrl(snapshotUrl)" in code_only(source), (
        "the guard exists but the snapshot path does not use it"
    )


def test_placeholder_avatar_is_escaped_at_its_attribute_site():
    """The inline SVG data URI contains raw double quotes (xmlns="..."), so an
    unescaped interpolation ended the attribute at the first one: the rest was
    re-tokenized as stray <img> attributes and the fallback rendered a broken
    image every time a snapshot 404'd — exactly what it exists to prevent."""
    source = code_only(read(JS))
    assert "PLACEHOLDER_AVATAR" in source
    assert "fallbackSvg" not in source, "the duplicated constant is back"
    assert 'data-fallback-src="${escapeHtml(PLACEHOLDER_AVATAR)}"' in source, (
        "the fallback data URI reaches an HTML attribute unescaped"
    )
    # And it is defined exactly once.
    assert source.count("const PLACEHOLDER_AVATAR") == 1


def test_dead_data_arg2_plumbing_is_gone():
    """toggleIdentity/toggleWatchlist never read the name, so escaping it was
    inert plumbing that only added an injection sink."""
    assert "data-arg2" not in code_only(read(JS))
    assert "dataset.arg2" not in code_only(read(JS))
    assert "dataset.arg2" not in code_only(read(HTML))


# ---------------------------------------------------------------------------
# Request lifecycle
# ---------------------------------------------------------------------------

def test_every_mutating_fetch_sends_the_csrf_header():
    source = read(JS)
    assert "CSRF_HEADERS" in source
    for block in re.finditer(r"fetch\((.*?)\n        \}\)", source, re.S):
        text = block.group(0)
        if "method: 'POST'" not in text:
            continue
        assert "CSRF_HEADERS" in text, f"POST without the CSRF header:\n{text[:200]}"


def test_long_running_operations_are_cancellable():
    """A quality check started during a search used to land whenever it
    landed, and displayQualityResult hides the results tabs — blanking search
    results the user was reading."""
    source = code_only(read(JS))
    assert "new AbortController()" in source
    assert "function beginRequest(" in source and "function endRequest(" in source
    for key in ("'search'", "'quality'", "'export'"):
        assert f"beginRequest({key})" in source, f"{key} has no in-flight controller"
        assert f"endRequest({key}" in source, f"{key} never releases its controller"
    assert "isAbort(error)" in source, "an aborted request is reported as a failure"


def test_stale_quality_response_cannot_blank_the_results_panel():
    body = read(JS).split("async function performQualityCheck", 1)[1].split("\n    function ", 1)[0]
    assert "inflight.quality !== controller" in body, (
        "a superseded quality response still renders"
    )


def test_search_routes_require_the_csrf_header():
    """Frontend and backend must land together or the page breaks."""
    for path, count in (("backend/routes/advanced_search.py", 2),
                        ("backend/routes/batch_export.py", 4)):
        source = read(f"/app/{path}")
        found = source.count("Depends(require_search_csrf)")
        assert found == count, f"{path}: {found} routes guarded, expected {count}"


def test_csrf_is_enforced_for_cookie_clients():
    """Cookie-authenticated POST without the header must be refused. Bearer
    clients stay exempt — a token cannot be sent cross-site by the browser."""
    request = urllib.request.Request(
        BASE + "/api/search/export?format=csv",
        data=b"{}", method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Cookie", "access_token=not-a-real-token")
    try:
        urllib.request.urlopen(request, timeout=15)
        pytest.fail("cookie POST without X-Requested-With was accepted")
    except urllib.error.HTTPError as exc:
        # 403 from the CSRF dependency, or 401 if auth rejects first —
        # never a 2xx, and never a 422 (which would mean the body was parsed).
        assert exc.code in (401, 403), f"unexpected status {exc.code}"


# ---------------------------------------------------------------------------
# Batch mode must not offer what the endpoint cannot do
# ---------------------------------------------------------------------------

def test_batch_mode_disables_the_filters_it_cannot_send():
    """POST /api/search/batch accepts only images/scope/top_k/check_watchlist
    and batch_search_service never forwards exclusions, yet the Filters and
    Exclude panels stayed fully interactive."""
    source = code_only(read(JS))
    assert "function applyBatchModeCapabilities(" in source
    assert "applyBatchModeCapabilities()" in source, "the gating is never invoked"

    markup = read(HTML)
    assert markup.count("data-batch-unsupported") == 2, (
        "both the Filters and Exclude sections must carry the marker"
    )
    assert markup.count("batch-unsupported-note") >= 2, "no visible reason is shown"


def test_batch_gating_disables_without_discarding_user_input():
    """Disabling is the honest signal; clearing would silently throw away a
    date range or exclusion list on a toggle the user may undo a second later.
    The section is dimmed and carries a note, so nothing is implied."""
    body = read(JS).split("function applyBatchModeCapabilities(", 1)[1].split("\n    }", 1)[0]
    assert "el.disabled = batch" in body
    assert "value = ''" not in body, "toggling batch mode destroys filter values"
    assert "selectedIdentities.clear()" not in body, "exclusions are silently dropped"


def test_batch_mode_state_is_adopted_from_the_control_at_init():
    """Browsers restore checkbox state across a soft reload, so the toggle can
    come back checked while state.isBatchMode is still its initial false —
    the upload area would then stage single files into a batch search."""
    body = read(JS).split("function init(", 1)[1].split("\n    // Start when DOM", 1)[0]
    assert "state.isBatchMode = elements.batchModeToggle.checked" in body
    assert "applyBatchModeCapabilities()" in body, (
        "the gating never runs on a page that loads already in batch mode"
    )


def test_batch_search_still_rejects_the_unsupported_params():
    """If the backend ever starts accepting them, this gating should be
    removed — this test is the reminder."""
    source = read("/app/backend/routes/batch_export.py")
    signature = source.split("async def batch_search(", 1)[1].split("):", 1)[0]
    for absent in ("exclude_identity_ids", "exclude_watchlist_ids", "pipeline_id", "date_from"):
        assert absent not in signature, (
            f"batch_search now accepts {absent} — drop applyBatchModeCapabilities"
        )


# ---------------------------------------------------------------------------
# File state
# ---------------------------------------------------------------------------

def test_mode_toggle_does_not_leave_an_invisible_file_selected():
    """Pick an image, turn batch on, turn it off: the preview was gone and
    #remove-image unreachable, but Search stayed enabled and searched the
    image the user believed they had discarded."""
    body = read(JS).split("elements.batchModeToggle.addEventListener", 1)[1].split("\n    }", 1)[0]
    assert "clearSelectedFile()" in body, "the hidden single file is never cleared"
    assert "clearBatchFiles()" in body, "batch files are never cleared"


def test_batch_input_value_is_reset():
    """Nothing reset batchInput.value, so re-picking the same files fired no
    change event and the click appeared to do nothing."""
    source = code_only(read(JS))
    assert "elements.batchInput.value = ''" in source


# ---------------------------------------------------------------------------
# Exclude dropdown
# ---------------------------------------------------------------------------

def test_selecting_an_exclusion_does_not_destroy_the_clicked_node():
    """Full re-render on every selection removed the clicked element, so the
    outside-click handler saw a detached target and closed the dropdown."""
    source = code_only(read(JS))
    assert "function updateExcludeItemState(" in source
    for toggle in ("toggleIdentity", "toggleWatchlist"):
        body = source.split(f"window.adminSearch.{toggle} = function", 1)[1].split("};", 1)[0]
        assert "updateExcludeItemState(" in body
        assert "Dropdown(" not in body, f"{toggle} still re-renders the whole list"

    # Defence in depth: the outside-click listener must run in the CAPTURE
    # phase, before actions.js dispatches on bubble and any handler can detach
    # the node. Anchored on code, not on a comment (code_only strips those).
    setup = source.split("function setupExcludeUI(", 1)[1]
    listener = setup.split("document.addEventListener('click'", 1)
    assert len(listener) == 2, "the outside-click listener is gone"
    assert re.search(r"\}, true\);", listener[1][:1200]), (
        "the outside-click listener is not registered in the capture phase"
    )


# ---------------------------------------------------------------------------
# Modal styling ownership
# ---------------------------------------------------------------------------

def test_page_owns_its_modal_styles():
    """admin-search.js opens modals with classList.add('active'), but the only
    `.modal.active` rule reaching this page came from upload-modal.css, which
    upload-modal-loader.js injects at runtime for an unrelated component. If
    that fetch failed, Export was a silent no-op."""
    markup = read(HTML)
    sheets = re.findall(r'<link[^>]+href="([^"]+\.css)[^"]*"', markup)
    assert any("admin-search.css" in href for href in sheets)

    css = read(CSS)
    assert re.search(r"\.modal\.active\s*\{", css), (
        ".modal.active is not defined in a stylesheet this page loads"
    )
    assert re.search(r"^\.modal\s*\{", css, re.M)

    # admin-search.css must be last so it wins over admin.css's `.modal`.
    own = next(i for i, href in enumerate(sheets) if "admin-search.css" in href)
    admin = next(i for i, href in enumerate(sheets) if href.endswith("admin.css"))
    assert own > admin, "admin.css loads after admin-search.css"


def test_camera_filter_shows_location_names_not_raw_ids():
    """Most cameras carry a UUID for a pipeline_id, so the filter rendered a
    column of opaque GUIDs. It must read the endpoint that carries a
    human-readable name."""
    source = read(JS)
    loader = source.split("async function loadPipelines", 1)[1].split("\n    // ", 1)[0]

    assert "/api/dashboard/pipelines" in loader, (
        "the filter reads an endpoint with no display name — options are raw ids"
    )
    assert "display_name" in loader
    # The submitted value must remain the id: that is what the search filters on.
    assert "option.value = id" in loader
    # Labels are set as text, never markup: location_name is operator-supplied.
    assert "innerHTML" not in code_only(loader)

    markup = read(HTML)
    assert re.search(r'<label[^>]*for="pipeline-filter"', markup), (
        "the camera filter has no associated label"
    )


def test_camera_filter_never_renders_a_blank_option(token):
    """A camera with no location_name must still get a label; display_name is
    the admin-set name with a pipeline_id fallback, and at least one live
    pipeline currently has location_name = null."""
    payload = get("/api/dashboard/pipelines", token)
    pipelines = payload["pipelines"]
    assert isinstance(pipelines, list)
    for pipeline in pipelines:
        assert pipeline.get("display_name"), (
            f"blank label for {pipeline.get('pipeline_id')}"
        )
        assert "location_name" in pipeline
        assert pipeline.get("pipeline_id"), "an option would have no value"

    # The frontend falls back the same way, so the two cannot drift apart.
    loader = read(JS).split("async function loadPipelines", 1)[1].split("\n    // ", 1)[0]
    assert "|| id" in loader, "no client-side fallback for a blank display_name"


# ---------------------------------------------------------------------------
# Identity detail opens in place
# ---------------------------------------------------------------------------

def test_clicking_a_match_does_not_navigate_away():
    """It used to do `window.location.href = '/admin/unknown?view=' + id`.
    That page is "Unknown Faces Center" — its grid is filtered to
    type == UNKNOWN, so a KNOWN match opened a modal over a list that cannot
    contain them. Worse, the search lives only in memory here, so navigating
    away and pressing Back meant re-uploading the image and re-running it."""
    code = code_only(read(JS))
    assert "window.location.href" not in code, (
        "a result click navigates away again — the search state is lost"
    )
    body = read(JS).split("async function viewIdentity(", 1)[1].split("\n    function ", 1)[0]
    assert "openIdentityModal()" in body, "the panel is never opened"
    assert "/api/admin/identity/" in body


def test_identity_panel_reads_both_sources():
    """Profile comes from one endpoint; watchlist membership from another that
    no page called before this."""
    code = code_only(read(JS))
    assert "/api/admin/identity/" in code
    assert "/watchlists" in code and "identities/" in code

    markup = read(HTML)
    for element in ("identity-modal", "identity-modal-body",
                    "close-identity-modal", "identity-full-profile"):
        assert f'id="{element}"' in markup, f"missing {element}"


def test_identity_panel_is_dom_built():
    """Same discipline as the rest of this file: display_name, watchlist names
    and camera labels are operator-supplied and must never be parsed as markup."""
    source = read(JS)
    panel = source.split("function renderIdentityPanel", 1)[1].split("\n    function openIdentityModal", 1)[0]
    assert "innerHTML" not in code_only(panel)
    builders = source.split("function buildIdentityHeader", 1)[1].split("function renderIdentityPanel", 1)[0]
    assert "innerHTML" not in code_only(builders)
    assert "buildEl(" in builders


def test_identity_panel_survives_a_watchlist_failure():
    """A failure fetching watchlist membership must not blank the profile that
    already loaded."""
    body = read(JS).split("async function viewIdentity(", 1)[1].split("\n    function ", 1)[0]
    watchlist_block = body.split("/watchlists", 1)[1][:600]
    assert "catch" in watchlist_block, "a watchlist failure takes down the whole panel"


def test_identity_panel_caps_the_sighting_list():
    """The appearances endpoint has no LIMIT — it returns every row. Rendering
    them all would be thousands of nodes for a long-lived identity."""
    source = read(JS)
    assert "MAX_SIGHTINGS_SHOWN" in source
    fn = source.split("function buildSightings", 1)[1].split("\n    function ", 1)[0]
    assert "slice(0, MAX_SIGHTINGS_SHOWN)" in fn
    # And it must say what it withheld rather than look complete.
    assert "sightings.length" in fn and "showing" in fn.lower()


def test_identity_panel_request_is_cancellable():
    """Clicking quickly down a result list must not let an earlier response
    overwrite a later one."""
    source = code_only(read(JS))
    assert "beginRequest('identity')" in source
    assert "endRequest('identity'" in source
    assert "inflight.identity !== controller" in source, "no stale-response guard"
    assert "identity: null" in source, "the identity slot is not registered"


def test_identity_panel_can_be_closed_and_restores_focus():
    """The panel must close by Escape and hand focus back to whatever opened it.

    Both are now ModalStack's job, not this page's. It used to keep a private
    `identityModalOpener` and its own Escape handler; the shared component
    records the trigger at open, restores it in settle(), contains Tab inside
    the dialog and owns the Escape key document-wide. The old assertions named
    those private details, so they would fail on a page that had adopted the
    shared behaviour correctly — the requirement is the behaviour, not the
    variable.

    Runtime proof that focus really lands inside and comes back:
    scripts/dev/modal_sweep_probe.js exercises this dialog through three full
    open/close cycles.
    """
    source = code_only(read(JS))
    assert "function closeIdentityModal(" in source
    assert "window.ModalStack.open(modal" in source, (
        "the identity panel no longer opens through the shared modal stack, "
        "which is what supplies Escape and focus restoration")
    assert "window.ModalStack.close(modal)" in source, (
        "closing must unwind the stack, or focus and the scroll lock are never "
        "restored")


def test_identity_detail_contract_holds(token):
    """The panel renders these fields by name; a backend rename would leave it
    showing blanks with nothing failing loudly."""
    identities = get("/api/admin/identities?limit=5&type=known", token).get("identities", [])
    if not identities:
        pytest.skip("no known identities")
    detail = get("/api/admin/identity/" + identities[0]["id"], token)

    for field in ("id", "type", "display_name", "status", "first_seen_at",
                  "last_seen_at", "appearances_count", "snapshot_url",
                  "pipeline_ids", "appearances"):
        assert field in detail, f"/api/admin/identity/{{id}} no longer returns {field}"
    assert isinstance(detail["appearances"], list)
    assert isinstance(detail["pipeline_ids"], list)
    for appearance in detail["appearances"]:
        assert "pipeline_id" in appearance and "start_time" in appearance


def test_identity_watchlists_contract_holds(token):
    """This endpoint existed and no page called it; the panel is its first
    consumer, so pin its shape."""
    identities = get("/api/admin/identities?limit=5&type=known", token).get("identities", [])
    if not identities:
        pytest.skip("no known identities")
    payload = get("/api/identities/%s/watchlists" % identities[0]["id"], token)
    assert "watchlists" in payload
    assert isinstance(payload["watchlists"], list)


def test_match_context_is_rendered_from_the_clicked_result():
    """The identity endpoint does not return THIS search's similarity or the
    match's watchlist_match body (notes / action_instructions) — those live on
    the clicked result and must be threaded into the panel."""
    source = read(JS)
    assert "function findMatchContext(" in source
    assert "function buildMatchContext(" in source

    context = source.split("function buildMatchContext", 1)[1].split("\n    /** One label", 1)[0]
    assert "similarity" in context
    assert "action_instructions" in context and "notes" in context, (
        "the operator-critical watchlist fields are not shown"
    )
    # Free text from the watchlist editor: DOM-built only.
    assert "innerHTML" not in code_only(context)
    # Both response shapes are searched (faces[] single, results[] batch).
    lookup = source.split("function findMatchContext", 1)[1].split("\n    /**", 1)[0]
    assert "results.faces" in lookup and "results.results" in lookup


def test_identity_type_is_allowlisted_not_coerced():
    """An unexpected type value must render neutrally and be logged, never be
    silently relabelled 'unknown' on a security tool."""
    source = code_only(read(JS))
    assert "IDENTITY_TYPES" in source
    assert "function normalizeIdentityType(" in source
    fn = source.split("function normalizeIdentityType", 1)[1].split("\n    }", 1)[0]
    assert "console.warn" in fn, "an unexpected type passes silently"
    assert "return null" in fn, "unexpected types are coerced instead of flagged"
    # The badge consumes the allowlist, not a raw ternary.
    header = source.split("function buildIdentityHeader", 1)[1].split("\n    function ", 1)[0]
    assert "normalizeIdentityType(" in header


def test_result_card_carries_id_and_type():
    """The brief requires the action to receive both; type is escaped like
    every other interpolated value."""
    source = code_only(read(JS))
    card = source.split('class="match-card"', 1)[1][:600]
    assert 'data-arg="${escapeHtml(match.identity_id)}"' in card
    assert 'data-identity-type="${escapeHtml(match.type)}"' in card


def test_result_cards_are_keyboard_operable():
    """The card is a div: without role/tabindex it cannot be reached by
    keyboard at all, and without a keydown action Enter/Space do nothing."""
    source = code_only(read(JS))
    card = source.split('class="match-card"', 1)[1][:600]
    assert 'role="button"' in card
    assert 'tabindex="0"' in card
    assert 'data-action-keydown="viewIdentityKey"' in card
    assert "aria-label" in card

    # The handler filters keys and stops Space from scrolling the page.
    handler = source.split("viewIdentityKey:", 1)[1].split("},", 1)[0]
    assert "'Enter'" in handler and "' '" in handler
    assert "preventDefault" in handler

    # actions.js actually delegates keydown (the binding this relies on).
    assert "'data-action-keydown'" in read(ACTIONS)

    css = read(CSS)
    assert ".match-card:focus-visible" in css, "no visible focus ring"


def test_view_identity_is_not_a_window_global():
    """Dispatch goes through the Actions registry alone; a window global is one
    more thing an injected script could replace."""
    source = code_only(read(JS))
    assert "window.viewIdentity" not in source
    # Registered from inside the IIFE, where the scoped function is visible.
    inside = source.split("async function viewIdentity(", 1)[1]
    assert "viewIdentity: (el) => viewIdentity(el.dataset.arg)" in inside


def test_malformed_identity_id_never_reaches_the_network():
    """Ids come from data attributes on rendered markup; the column is a uuid.
    Anything else renders the error state without firing a request."""
    source = read(JS)
    assert "UUID_RE" in source
    body = source.split("async function viewIdentity(", 1)[1].split("\n    // Registered here", 1)[0]
    guard_at = body.index("UUID_RE.test")
    fetch_at = body.index("fetch(")
    assert guard_at < fetch_at, "the fetch happens before the id is validated"
    guard_block = body[guard_at:guard_at + 700]
    assert "return" in guard_block.split("fetch(")[0], "a malformed id still falls through"


def test_invalid_identity_id_is_a_client_error_not_a_500(token):
    """Belt and braces for the same case server-side."""
    request = urllib.request.Request(BASE + "/api/admin/identity/not-a-uuid")
    request.add_header("Authorization", "Bearer " + token)
    try:
        urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as exc:
        assert exc.code < 500, f"malformed id causes a server error: {exc.code}"


def test_identity_detail_serves_unknown_identities_too(token):
    """Same endpoint, same shape, for the unknown type — the panel must not be
    a known-only feature."""
    identities = get("/api/admin/unknown?limit=5", token).get("identities", [])
    if not identities:
        pytest.skip("no unknown identities present")
    detail = get("/api/admin/identity/" + identities[0]["id"], token)
    assert detail["type"] == "unknown"
    for field in ("appearances", "pipeline_ids", "snapshot_url"):
        assert field in detail


def test_footer_links_point_at_routes_that_exist():
    """Full profile now targets the dedicated identity page — built because
    this link used to have nowhere honest to land. The analyze link still uses
    the security-intelligence deep link that page actually parses."""
    source = code_only(read(JS))
    assert "/admin/identity/" in source, (
        "the full-profile link no longer targets the identity page"
    )
    assert "/admin/unknown?view=" not in source, (
        "the search page links a known match to the Unknown Faces Center again"
    )
    assert "/admin/security-intelligence" in source and "identity_id=" in source
    assert "/admin/identities?view=" not in source, "links to a route that does not exist"

    markup = read(HTML)
    assert 'id="identity-analyze"' in markup
    assert 'id="identity-full-profile"' in markup

    # The deep-link parameter matches what admin-security-intelligence.js reads.
    consumer = read(f"{FRONTEND}/js/admin-security-intelligence.js")
    assert "identity_id" in consumer


def test_view_parameter_no_longer_injects_inline_scripts(token):
    """?view= used to be answered by string-injecting an inline <script> into
    the served HTML — a script-src 'self' CSP violation of exactly the class
    actions.js was written to eliminate. The deep link is client-parsed now."""
    identities = get("/api/admin/identities?limit=1&type=known", token).get("identities", [])
    if not identities:
        pytest.skip("no identities to probe against")

    request = urllib.request.Request(
        BASE + "/admin/unknown?view=" + identities[0]["id"])
    request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode(errors="replace")

    assert 'id="backend-identity-data"' not in body, "the inline injection is back"
    assert 'id="backend-referrer-page"' not in body
    assert "identityModalReferrer" not in body, (
        "an inline script assigns the referrer — that lives in admin-unknown.js now"
    )

    # And the backend source no longer builds it at all.
    source = read("/app/backend/routes/dashboard.py")
    assert "backend-identity-data" not in source
    assert "script_tag" not in source


def test_deep_link_is_parsed_client_side():
    """admin-unknown.js owns ?view=/&from= now: UUID-validated, referrer
    sanitized to a same-origin path, dispatched to the existing modal."""
    source = read(f"{FRONTEND}/js/admin-unknown.js")
    assert "function openDeepLinkedIdentity(" in source or \
           "async function openDeepLinkedIdentity(" in source
    assert "URLSearchParams" in source
    assert "DEEP_LINK_UUID_RE" in source

    bootstrap = source.split("async function openDeepLinkedIdentity", 1)[1].split("\n}", 1)[0]
    assert "params.get('view')" in bootstrap
    assert "sanitizeReferrerPath" in bootstrap
    assert "viewIdentityDetails(" in bootstrap

    # The referrer must reject protocol-relative and absolute URLs.
    sanitizer = source.split("function sanitizeReferrerPath", 1)[1].split("\n}", 1)[0]
    assert r"^\/(?!\/)" in sanitizer, "protocol-relative referrers are accepted"

    # And the bootstrap actually runs at load.
    init = source.split("document.addEventListener('DOMContentLoaded'", 1)[1][:900]
    assert "openDeepLinkedIdentity()" in init


def test_missing_deep_linked_identity_is_no_longer_silent():
    """A deleted or not-permitted identity used to render the plain list with
    no modal and no message. The fetch path must say which failure occurred."""
    source = read(f"{FRONTEND}/js/admin-unknown.js")
    body = source.split("async function viewIdentityDetails", 1)[1].split("\n}", 1)[0]
    assert "404" in body and "403" in body, "failures collapse to one generic message"
    assert "showNotification(error.message" in body
    assert "return null" in body, "the deep-link caller cannot tell success from failure"


def test_known_identity_reframes_the_unknown_page():
    """Option (a): when the deep-linked identity is KNOWN, the page must stop
    claiming to be the Unknown Faces Center."""
    source = read(f"{FRONTEND}/js/admin-unknown.js")
    assert "function reframeForKnownIdentity(" in source
    fn = source.split("function reframeForKnownIdentity", 1)[1].split("\n}", 1)[0]
    assert "'known'" in fn, "the reframe is not gated on type"
    assert "Identity Profile" in fn
    assert "textContent" in fn
    assert "innerHTML" not in fn, "operator-supplied display_name parsed as markup"
    assert "identity-back-link" in fn

    # The back link's style lives in a stylesheet unknown.html actually loads.
    assert ".identity-back-link" in read(f"{FRONTEND}/css/admin.css")


def test_identity_detail_denies_a_pipeline_less_user(token):
    """The live 403: a non-admin with NO pipeline access must be refused by
    GET /api/admin/identity/{id} (check_identity_access, identities.py) — the
    permission check the panel's error path surfaces.

    Uses the same disposable-user pattern as test_permission_propagation.py:
    direct INSERT (the shared event loop owns the pooled connections), login
    through the real API, cleanup in finally regardless of outcome."""
    import sys
    sys.path.insert(0, "/app/tests")
    from conftest import run_on_shared_loop  # NOT tests.conftest — separate module object

    from sqlalchemy import text as sa_text
    from db_connection import db_manager
    from backend.auth.password import hash_password

    username = "adv_search_403_probe"
    password = "Probe-403-Passw0rd!"

    identities = get("/api/admin/identities?limit=1&type=known", token).get("identities", [])
    if not identities:
        pytest.skip("no identities to probe against")
    identity_id = identities[0]["id"]

    async def create():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(sa_text("DELETE FROM users WHERE username = :u"), {"u": username})
            await db.execute(sa_text("""
                INSERT INTO users (username, email, full_name, password_hash, role,
                                   is_active, can_use_chatbot, permissions_version, created_at)
                VALUES (:u, :e, 'Search 403 Probe', :h, 'user', true, false, 1, now())
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

        request = urllib.request.Request(BASE + "/api/admin/identity/" + identity_id)
        request.add_header("Authorization", "Bearer " + probe_token)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                pytest.fail(
                    f"pipeline-less non-admin read identity detail: {response.status}"
                )
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, f"expected 403, got {exc.code}"
    finally:
        run_on_shared_loop(destroy())


def test_full_profile_opens_in_a_new_tab():
    """The results behind the panel live only in this tab's memory, so
    navigating away destroys them. target=_blank keeps them; noopener must
    accompany it or the new page gets a window.opener handle back into the
    search tab."""
    markup = read(HTML)
    anchor = markup.split('id="identity-full-profile"', 1)
    assert len(anchor) == 2, "the full-profile link is gone"
    tag = anchor[0].rsplit("<a", 1)[1] + anchor[1].split(">", 1)[0]
    assert 'target="_blank"' in tag, "full profile navigates the search tab away"
    assert 'rel="noopener"' in tag, "_blank without noopener leaks window.opener"


def test_full_profile_href_has_no_back_param():
    """In a NEW tab, a `from` back link would navigate the fresh tab to an
    EMPTY search page — the results only exist in the original tab. Same-tab
    callers (intelligence, dashboard) keep passing `from`, where the back link
    is truthful."""
    source = code_only(read(JS))
    builder = source.split("identity-full-profile", 1)[1][:400]
    assert "from=" not in builder, "the new-tab link carries a misleading back param"
    # The same-tab caller still passes it (via URL.searchParams, not a literal).
    assert "searchParams.set('from'" in code_only(read(f"{FRONTEND}/js/admin-intelligence.js"))


def test_search_page_assets_carry_cache_busters():
    """The recurring 'it still redirects to Unknown Faces' report was a stale
    cached admin-search.js: unlike actions.js?v= and navbar-loader.js?v=, this
    page's own assets shipped unversioned, so fixes never reached browsers
    without a manual hard refresh."""
    markup = read(HTML)
    assert re.search(r'src="/frontend/js/admin-search\.js\?v=[\w.-]+"', markup), (
        "admin-search.js is unversioned — the next fix will sit behind browser cache"
    )
    assert re.search(r'href="/frontend/css/admin-search\.css\?v=[\w.-]+"', markup), (
        "admin-search.css is unversioned"
    )


def test_malformed_view_parameter_does_not_bounce_to_signin(token):
    """`except (ValueError, uuid.InvalidUUIDError)` — that attribute does not
    exist in stdlib uuid, so evaluating the except clause raised AttributeError,
    which a sibling `except Exception` does not catch. It escaped to the outer
    handler and redirected an authenticated admin to /signin."""
    request = urllib.request.Request(BASE + "/admin/unknown?view=not-a-uuid")
    request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=30) as response:
        landed = response.geturl()
        body = response.read().decode(errors="replace")
    assert "signin" not in landed.lower(), (
        f"a malformed view parameter bounced an authenticated admin to {landed}"
    )
    assert "Unknown Faces Center" in body


def test_native_control_chrome_is_dark():
    """`.search-panel .form-control` darkens the CLOSED select, but the option
    list that drops open is painted by the browser and defaulted to white with
    black text — on every dropdown on the page. Same for the date fields,
    whose calendar indicator ships as a dark glyph on a dark field."""
    css = read(CSS)

    assert "color-scheme: dark" in css, (
        "native widgets (option lists, date pickers, scrollbars) still render light"
    )
    # Chromium paints the option rows from CSS and ignores color-scheme there.
    assert re.search(r"select option[^{]*\{[^}]*background-color:", css, re.S), (
        "option rows have no explicit background — they open white"
    )
    assert "::-webkit-calendar-picker-indicator" in css, (
        "the date picker glyph is invisible on a dark field"
    )
    assert "-webkit-autofill" in css, "autofill repaints the field white"

    # The custom exclusion list must be opaque, not a translucent black.
    block = re.findall(r"^\.exclude-dropdown\s*\{(.*?)\}", css, re.S | re.M)
    assert block, ".exclude-dropdown rule is gone"
    assert any("rgba" not in b and "background" in b for b in block), (
        "the exclusion dropdown is still translucent"
    )


def test_no_page_control_declares_a_white_background():
    """Guards the specific regression: a light-theme rule copied in from
    admin.css would make a control unreadable against this page's white text."""
    # Strip /* ... */ first: the fix is documented by quoting the rule it
    # replaces ("admin.css sets .modal-content { background: white }"), and a
    # naive scan reads that explanation as the violation.
    css = re.sub(r"/\*.*?\*/", "", read(CSS), flags=re.S)
    offenders = []
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        selector, body = match.group(1).strip(), match.group(2)
        if re.search(r"background(-color)?\s*:\s*(white|#fff\b|#ffffff\b)", body, re.I):
            offenders.append(selector.splitlines()[-1].strip())
    assert not offenders, f"white backgrounds on a dark page: {offenders}"


def test_modal_panel_is_not_white_on_this_dark_page():
    """admin.css sets `.modal-content { background: white }` for the light
    admin forms, and upload-modal.css only ever styled `.upload-modal-content`
    — so the export modal rendered as a white panel carrying this page's white
    text and light-grey hints, i.e. invisible content."""
    css = read(CSS)
    block = re.search(r"^\.modal-content\s*\{(.*?)\}", css, re.S | re.M)
    assert block, "admin-search.css does not own .modal-content"
    body = block.group(1)
    assert "background:" in body, "no background declared, so white wins"
    assert "background: white" not in body
    assert "color: #fff" in body or "color: white" in body, (
        "the panel sets a background without setting a readable text colour"
    )
    # A <select>'s option list is OS-painted and does not inherit the panel.
    assert ".form-control option" in css, (
        "dropdown options would render white-on-white"
    )


# ---------------------------------------------------------------------------
# Accessibility (the two Critical items)
# ---------------------------------------------------------------------------

def test_toggle_inputs_are_reachable_by_keyboard():
    """`display: none` removed #batch-mode-toggle and #check-watchlist from
    the tab order AND the accessibility tree: two features were mouse-only."""
    css = read(CSS)
    block = re.search(r"\.toggle-switch input\s*\{(.*?)\}", css, re.S)
    assert block, ".toggle-switch input rule is gone"
    assert "display: none" not in block.group(1), (
        "the toggle input is hidden from the accessibility tree again"
    )
    assert "opacity: 0" in block.group(1), "expected the visually-hidden pattern"
    assert "input:focus-visible" in css, "a focus ring is required once it is focusable"


def test_upload_dropzone_is_a_real_control():
    """Both file inputs are display:none, so with a plain <div> dropzone the
    page's only entry point could not be reached by keyboard at all."""
    markup = read(HTML)
    assert re.search(r'<button[^>]*class="upload-area"[^>]*id="upload-area"', markup) or \
           re.search(r'<button[^>]*id="upload-area"', markup), (
        "the dropzone is not a button"
    )
    assert '<div class="upload-area"' not in markup


def test_remove_controls_are_buttons_not_clickable_spans():
    source = code_only(read(JS))
    assert "buildEl('button', 'remove-file')" in source
    assert "buildEl('button', 'exclude-chip-remove')" in source


# ---------------------------------------------------------------------------
# actions.js guards
# ---------------------------------------------------------------------------

def test_actions_ignores_disabled_elements():
    """This page generates <span data-action> elements, where the browser
    offers no protection at all."""
    source = code_only(read(ACTIONS))
    assert "function isDisabled(" in source
    assert "aria-disabled" in source
    dispatch = source.split("function dispatch(", 1)[1].split("\n    Object.keys", 1)[0]
    assert "isDisabled(element)" in dispatch


def test_actions_labels_async_rejections():
    """try/catch catches synchronous throws only; a rejected promise from an
    async handler escaped unlabelled."""
    dispatch = code_only(read(ACTIONS)).split("function dispatch(", 1)[1]
    assert "typeof result.catch === 'function'" in dispatch
    assert "rejected" in dispatch


def test_actions_is_idempotent_on_a_second_load():
    """A second load attached seven more document listeners AND replaced
    window.Actions with an empty registry, dropping every prior handler."""
    source = code_only(read(ACTIONS))
    assert "__initialized" in source
    guard = source.split("'use strict';", 1)[1][:600]
    assert "window.Actions" in guard and "return;" in guard, (
        "no re-entry guard before the listeners are attached"
    )


def test_actions_warns_on_duplicate_registration():
    register = code_only(read(ACTIONS)).split("function register(", 1)[1].split("\n    /**", 1)[0]
    assert "handlers.has(name)" in register
    assert "Duplicate handler" in register


# ---------------------------------------------------------------------------
# Per-page action parity
# ---------------------------------------------------------------------------

def test_every_data_action_is_registered_by_a_script_that_page_loads():
    """The existing global test pools `used` and `registered` across the whole
    frontend, so a button on page A counts as handled if page B happens to
    register that name. This closes the check per page."""
    # Brace-counted, not indentation-anchored: registration is legal at top
    # level AND inside a page IIFE (admin-search.js, admin-identity.js do the
    # latter, because handlers scoped inside the IIFE must register from
    # inside it).
    def registered_names(source):
        names = set()
        for m in re.finditer(r"Actions\.register\(\{", source):
            depth, i = 1, m.end()
            while i < len(source) and depth:
                if source[i] == "{":
                    depth += 1
                elif source[i] == "}":
                    depth -= 1
                i += 1
            names |= set(re.findall(
                r"^\s*([A-Za-z_$][\w$]*)\s*[,:]", source[m.end():i], re.M))
        return names

    registry = {}
    for path in glob.glob(os.path.join(FRONTEND, "js", "*.js")):
        registry[os.path.basename(path)] = registered_names(read(path))

    # upload-modal-loader.js dynamically injects upload-modal.js (which
    # registers openUploadModal etc.) — a page loading the loader gets those
    # registrations at runtime, so credit them statically too.
    registry["upload-modal-loader.js"] = (
        registry.get("upload-modal-loader.js", set())
        | registry.get("upload-modal.js", set()))

    pages = (glob.glob(os.path.join(FRONTEND, "*.html"))
             + glob.glob(os.path.join(FRONTEND, "admin", "*.html")))
    assert pages

    # admin-background-tasks.js owns this one with its own delegated listener.
    exempt = {"details"}
    problems = []
    for page in pages:
        markup = read(page)
        loaded = set(re.findall(r'src="[^"]*js/([\w.-]+\.js)', markup))
        # A page's own inline-rendered actions also come from the JS it loads.
        available = set()
        for name in loaded:
            available |= registry.get(name, set())
            js_path = os.path.join(FRONTEND, "js", name)
            if os.path.exists(js_path):
                available |= set(re.findall(
                    r'data-action(?:-\w+)?="([A-Za-z_$][\w$]*)"', read(js_path)))
        used = set(re.findall(r'data-action(?:-\w+)?="([A-Za-z_$][\w$]*)"', markup))
        for name in loaded:
            js_path = os.path.join(FRONTEND, "js", name)
            if os.path.exists(js_path):
                used |= set(re.findall(
                    r'data-action(?:-\w+)?="([A-Za-z_$][\w$]*)"', read(js_path)))
        unhandled = used - available - exempt
        # Only report names this page can actually render.
        unhandled = {n for n in unhandled if n not in registry.get("actions.js", set())}
        if unhandled:
            problems.append(f"{os.path.basename(page)}: {sorted(unhandled)}")
    assert not problems, "data-action names with no handler on their own page:\n" + "\n".join(problems)


def test_search_page_actions_are_all_registered():
    """The narrow version of the above, for the page under audit."""
    markup = read(HTML)
    source = read(JS)
    used = set(re.findall(r'data-action(?:-\w+)?="([A-Za-z_$][\w$]*)"', markup))
    used |= set(re.findall(r"dataset\.action = '([A-Za-z_$][\w$]*)'", source))

    registered = set()
    for block in re.findall(r"Actions\.register\(\{(.*?)\n\}\);", source, re.S):
        registered |= set(re.findall(r"^\s{4}([A-Za-z_$][\w$]*)\s*[,:]", block, re.M))

    assert used, "no data-action names found — did the pattern change?"
    assert not used - registered, f"unregistered on /admin/search: {sorted(used - registered)}"
