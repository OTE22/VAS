"""API documentation renders — offline, and through the proxy.

    docker exec face_recognition_api python -m pytest tests/test_api_docs.py -v

/docs and /redoc used to return HTTP 200 and a blank page, which is the worst
kind of broken: every automated check passed. Two independent causes, either
sufficient on its own:

  * FastAPI's stock pages load Swagger UI / ReDoc from cdn.jsdelivr.net (and
    ReDoc pulls Google Fonts). This system runs with no internet access.
  * The pages bootstrap themselves with an INLINE <script>, and the
    deployment sends `script-src 'self'` with no 'unsafe-inline'.

So a 200 proves nothing here. These tests assert the two properties that
actually make the page work: every asset is same-origin, and no script is
inline.
"""

import json
import re
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"
# The suite runs INSIDE the api container, where localhost is the app itself.
# nginx is a separate container reachable by its compose service name — that
# is the path a browser actually takes.
PROXY = "http://nginx"

CDN_HOSTS = ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com",
             "fonts.googleapis.com", "fastapi.tiangolo.com", "jsdelivr.net")


def _get(url, timeout=90):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def _external_refs(html):
    return re.findall(r'(?:src|href)=["\'](https?://[^"\']+)', html)


def _inline_scripts(html):
    """<script> tags with no src= — blocked by script-src 'self'."""
    return re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html)


# ---------------------------------------------------------------------------
# the three documented entry points
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_endpoints_answer_directly(path):
    status, _body = _get(BASE + path)
    assert status == 200, f"{path} returned {status} from the backend"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_endpoints_answer_through_nginx(path):
    """A browser reaches these through the proxy, not port 8000."""
    status, _body = _get(PROXY + path)
    assert status == 200, f"{path} returned {status} through nginx"


# ---------------------------------------------------------------------------
# the properties a 200 does NOT prove
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_docs_pages_load_no_internet_assets(path):
    _status, html = _get(BASE + path)
    external = _external_refs(html)
    assert external == [], (
        f"{path} loads assets from the internet: {external}. This deployment "
        f"is offline — the page renders blank.")
    for host in CDN_HOSTS:
        assert host not in html, f"{path} still references {host}"


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_docs_pages_have_no_inline_scripts(path):
    """`script-src 'self'` without 'unsafe-inline' blocks inline bootstraps —
    the page loads, the UI never initializes."""
    _status, html = _get(BASE + path)
    inline = _inline_scripts(html)
    assert inline == [], (
        f"{path} bootstraps with an inline <script> ({len(inline)} found), "
        f"which the Content-Security-Policy blocks")


def test_the_vendored_doc_assets_are_actually_served():
    """The pages reference these; if the frontend mount is wrong they 404 and
    the page is blank with no other symptom."""
    for asset in ("/frontend/vendor/swagger/swagger-ui.css",
                  "/frontend/vendor/swagger/swagger-ui-bundle.js",
                  "/frontend/vendor/swagger/swagger-ui-standalone-preset.js",
                  "/frontend/vendor/swagger/redoc.standalone.js",
                  "/frontend/js/docs-init.js"):
        status, body = _get(BASE + asset)
        assert status == 200, f"{asset} -> {status}"
        assert len(body) > 500, f"{asset} served but suspiciously small"


def test_every_asset_referenced_by_the_docs_pages_resolves():
    """Follow each page's own references and fetch them."""
    for path in ("/docs", "/redoc"):
        _status, html = _get(BASE + path)
        for ref in re.findall(r'(?:src|href)=["\'](/[^"\']+)', html):
            status, _body = _get(BASE + ref)
            assert status == 200, f"{path} references {ref} which returns {status}"


# ---------------------------------------------------------------------------
# the spec itself
# ---------------------------------------------------------------------------

def _spec():
    status, body = _get(BASE + "/openapi.json", timeout=120)
    assert status == 200
    return json.loads(body)


def test_openapi_is_valid_and_substantial():
    spec = _spec()
    assert spec.get("openapi", "").startswith("3."), "not an OpenAPI 3 document"
    assert spec["info"]["title"]
    assert len(spec["paths"]) > 50, (
        f"only {len(spec['paths'])} paths — routers are missing from the spec")


def test_the_core_routers_appear_in_the_spec():
    """If a router fails to import at startup, the app still serves and the
    only visible symptom is its endpoints missing from the spec."""
    paths = _spec()["paths"]
    required = {
        "authentication": "/api/auth/login",
        "health": "/health/detailed",
        "identities/unknown": "/api/admin/unknown",
        "merge": "/api/admin/identities/merge",
        "pipelines": "/api/pipelines",
        "webhook ingest": "/webhook/{pipeline_id}",
    }
    missing = {name: path for name, path in required.items() if path not in paths}
    assert not missing, f"routers missing from OpenAPI: {missing}"


def test_the_detailed_health_contract_is_documented():
    """Project rule: the detailed health endpoint is /health/detailed."""
    paths = _spec()["paths"]
    assert "/health/detailed" in paths
    assert "/health/live" in paths
    assert "/api/health" not in paths, (
        "/api/health would contradict the documented /health/detailed contract")


def test_no_duplicate_operation_ids():
    """Duplicates make generated clients collide and silently drop routes."""
    seen = {}
    duplicates = []
    for path, methods in _spec()["paths"].items():
        for method, operation in methods.items():
            if method not in ("get", "post", "put", "patch", "delete", "head", "options"):
                continue
            operation_id = operation.get("operationId")
            if not operation_id:
                continue
            if operation_id in seen:
                duplicates.append(f"{operation_id}: {seen[operation_id]} vs {method.upper()} {path}")
            seen[operation_id] = f"{method.upper()} {path}"
    assert not duplicates, f"duplicate operationIds: {duplicates}"


def test_the_spec_is_served_through_nginx_identically():
    """The proxy must not rewrite or truncate the spec."""
    _direct_status, direct = _get(BASE + "/openapi.json", timeout=120)
    _proxy_status, proxied = _get(PROXY + "/openapi.json", timeout=120)
    assert json.loads(direct)["paths"].keys() == json.loads(proxied)["paths"].keys(), (
        "the spec differs through nginx — proxy rewriting is corrupting it")


def test_docs_are_disabled_in_production_config():
    """Interactive docs publish every admin route; production must not."""
    source = open("/app/backend/main.py", encoding="utf-8").read()
    assert "not settings.is_production" in source, (
        "the docs gate no longer excludes production")
