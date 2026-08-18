"""The MapLibre GL JS + Martin map stack — offline, same-origin, no fallback.

    docker exec face_recognition_api python -m pytest tests/test_maplibre_stack.py -v

What this pins, and why each line exists:

  * MapLibre is VENDORED (frontend/vendor/maplibre) as the complete runtime
    set — main + shared chunk + worker + css — and the worker is a same-origin
    file. Missing the shared chunk fails only at runtime in the browser.
  * The 6.x ESM build has NO default export; `import maplibregl from` fails in
    the browser with "does not provide an export named 'default'". Found by
    headless Chrome during this migration; guarded here so it cannot recur.
  * nginx serves .mjs as JavaScript. The stock mime.types has no mjs entry,
    and a `types` block inside server{} REPLACES the inherited map (a
    server-level block turned every .css into octet-stream, observed live) —
    so it must be at http level. Both configs.
  * The map document is NOT an iframe and NOT backend HTML any more.
  * Style JSONs reference only same-origin resources.
  * Availability is derived from Martin's catalog + a probe tile, cached, and
    a missing dataset is OFFLINE_MAP_DATASET_UNAVAILABLE — never a substitute.
  * The security analysis import must succeed: a failed import quietly turned
    every overlay off while the endpoint still returned 200 (observed).
  * The transitional raster archive is byte-identical to the pyramid it was
    converted from (TMS y-flip is the highest-risk detail).
"""

import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request

import pytest

REPO = "/app"
BASE = "http://localhost:8000"
VENDOR = f"{REPO}/frontend/vendor/maplibre"
STYLES = f"{REPO}/frontend/maps/styles"

EXTERNAL_MAP_HOSTS = (
    "openstreetmap.org", "tile.openstreetmap", "maptiler.com", "mapbox.com",
    "google.com", "googleapis.com", "gstatic.com", "bing.com", "arcgisonline.com",
    "cartocdn.com", "unpkg.com", "jsdelivr.net", "cdnjs.cloudflare.com",
    "copernicus.eu", "dataspace.copernicus", "registry.npmjs.org", "github.com",
    "thunderforest", "stadiamaps",
)


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _http(method, path, body=None, *, token=None, csrf=True, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    url = path if path.startswith("http://") or path.startswith("https://") else BASE + path
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if csrf and method != "GET":
        request.add_header("X-Requested-With", "XMLHttpRequest")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def PRODUCTION():
    """The dataset directory THIS deployment serves.

    Not a constant: the isolated regression stack points MAP_DATA_DIR at its
    own immutable fixtures while still bind-mounting the whole repo at /app,
    so a hard-coded map-data/production path would quietly grade the
    developer's archives instead of the ones under test.
    """
    from config import settings
    return settings.MAP_PRODUCTION_DIR


def SHIPPED_PRODUCTION():
    """The REAL dataset tree, regardless of what this stack is configured with.

    A few tests check that the shipped styles agree with the shipped archives:
    the OpenMapTiles source-layers they name, the zoom ranges they declare,
    real Lebanese elevations. Those are claims about the DATA THAT SHIPS, so
    they must not follow MAP_DATA_DIR into the regression fixture tree, which
    is deliberately synthetic (z0-8 vector, z6-10 DEM, invented layer names)
    and would fail them for the wrong reason. They skip when the real archives
    are not present.
    """
    return f"{REPO}/map-data/production"


def _json(raw):
    try:
        return json.loads(raw.decode())
    except Exception:                                          # noqa: BLE001
        return {}


@pytest.fixture(scope="module")
def admin_token():
    # csrf=False is load-bearing: the login handler treats X-Requested-With as
    # "this is a browser" and puts the JWT in an httpOnly cookie with
    # access_token nulled in the body — a None token and meaningless 401s.
    status, raw = _http("POST", "/api/auth/login",
                        {"username": "admin", "password": "admin123"}, csrf=False)
    assert status == 200, f"admin login failed: {raw[:200]}"
    token = _json(raw).get("access_token")
    assert token, "login returned no bearer token"
    return token


# ---------------------------------------------------------------------------
# vendored runtime
# ---------------------------------------------------------------------------

def test_maplibre_runtime_set_is_complete():
    """main + the shared chunk it imports + the worker + css. The main module
    statically imports ./maplibre-gl-shared.mjs — copying only maplibre-gl.mjs
    fails at first import."""
    for name in ("maplibre-gl.mjs", "maplibre-gl-shared.mjs",
                 "maplibre-gl-worker.mjs", "maplibre-gl.css", "LICENSE.txt"):
        assert os.path.isfile(f"{VENDOR}/{name}"), f"missing vendored file {name}"
    main = _read(f"{VENDOR}/maplibre-gl.mjs")
    for imported in re.findall(r"from\s*['\"](\./[^'\"]+)['\"]", main):
        assert os.path.isfile(f"{VENDOR}/{imported[2:]}"), (
            f"maplibre-gl.mjs imports {imported}, which is not vendored")


def test_maplibre_worker_is_a_same_origin_file_not_a_blob():
    """6.3.0 resolves its worker as new URL('./maplibre-gl-worker.mjs',
    import.meta.url) launched {type:'module'} — a same-origin file. Blob
    wrappers exist only for cross-origin worker URLs. That is why the CSP
    delta is `worker-src 'self'` and NOT `blob:` (headless Chrome: zero CSP
    violations, worker URL reported as the vendored file)."""
    main = _read(f"{VENDOR}/maplibre-gl.mjs")
    assert "maplibre-gl-worker.mjs" in main
    controller = _read(f"{REPO}/frontend/js/identity-map.js")
    assert "setWorkerUrl('/frontend/vendor/maplibre/maplibre-gl-worker.mjs')" in controller, (
        "the controller must name the self-hosted worker explicitly")


def test_controller_uses_a_namespace_import_not_default():
    """The ESM build exports NAMED symbols only. `import maplibregl from`
    throws in the browser; the controller must use `import * as`."""
    controller = _read(f"{REPO}/frontend/js/identity-map.js")
    # Only real code lines — the comment explaining this very bug quotes the
    # bad form, and a raw substring search matched it.
    code = "\n".join(l for l in controller.splitlines() if not l.strip().startswith(("//", "*", "/*")))
    assert re.search(r"import \* as maplibregl from '/frontend/vendor/maplibre/maplibre-gl\.mjs'", code), (
        "identity-map.js must namespace-import maplibre-gl.mjs")
    assert not re.search(r"^\s*import maplibregl from", code, re.M), (
        "default import of maplibre-gl.mjs fails: the ESM build has no default export")
    main = _read(f"{VENDOR}/maplibre-gl.mjs")
    assert "export default" not in main and "as default" not in main, (
        "the vendored build now HAS a default export — revisit this test and the import")


def test_nginx_serves_mjs_as_javascript_from_the_http_context():
    """The mjs types entry MUST be at http level: a `types` block inside
    server{} replaces the inherited MIME map instead of extending it, and
    turned every .css into application/octet-stream when tried (observed)."""
    for conf in (f"{REPO}/nginx.conf", f"{REPO}/nginx.prod.conf"):
        src = _read(conf)
        http_block = src[: src.index("  server {")]
        assert re.search(r"types\s*\{\s*application/javascript\s+mjs;\s*\}", http_block), (
            f"{os.path.basename(conf)}: mjs MIME type must be declared at http level")
        server_part = src[src.index("  server {"):]
        assert "application/javascript  js mjs;" not in server_part, (
            f"{os.path.basename(conf)}: a server-level types block replaces the whole "
            f"MIME map — move it to http level")


def test_nginx_proxies_maps_to_martin_same_origin_in_both_configs():
    for conf in (f"{REPO}/nginx.conf", f"{REPO}/nginx.prod.conf"):
        src = _read(conf)
        assert "location ^~ /maps/ {" in src, f"{conf} has no /maps/ location"
        block = src[src.index("location ^~ /maps/ {"):]
        block = block[: block.index("\n    }\n")]
        assert "set $martin_upstream http://martin:3000;" in block, (
            "use a VARIABLE upstream: a literal one makes nginx refuse to start "
            "whenever Martin is down")
        assert "Access-Control-Allow-Origin" not in block, "no CORS on /maps/ — same origin"


def test_csp_grants_worker_src_self_only():
    for conf in (f"{REPO}/nginx.conf", f"{REPO}/nginx.prod.conf"):
        csp = re.search(r'add_header Content-Security-Policy "([^"]+)"', _read(conf)).group(1)
        assert "worker-src 'self'" in csp, f"{conf}: worker-src 'self' missing"
        worker = re.search(r"worker-src ([^;]+)", csp).group(1)
        assert "blob:" not in worker, (
            f"{conf}: worker-src grants blob: — not required for the vendored 6.3.0 "
            f"worker (proven in-browser); do not widen CSP without evidence")
        assert "unsafe-eval" not in csp and "script-src 'self'" in csp


# ---------------------------------------------------------------------------
# no iframe, no backend HTML, no external hosts
# ---------------------------------------------------------------------------

def test_no_map_page_uses_an_iframe_or_leaflet():
    for js in ("admin-intelligence.js", "admin-security-intelligence.js", "admin-pipelines.js"):
        src = _read(f"{REPO}/frontend/js/{js}")
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith(("//", "*", "/*")))
        assert "createElement('iframe')" not in code and 'createElement("iframe")' not in code, (
            f"{js} still renders a map iframe")
        assert "buildMapUrl(" not in code, f"{js} still builds the legacy /map URL"
        assert not re.search(r"\bL\.(map|tileLayer|marker)\(", code), f"{js} still calls Leaflet"
    for html in ("intelligence.html", "security-intelligence.html", "pipelines.html"):
        src = _read(f"{REPO}/frontend/admin/{html}")
        assert "vendor/leaflet" not in src, f"{html} still loads Leaflet"
        assert 'type="module" src="/frontend/js/identity-map.js' in src, (
            f"{html} does not load the MapLibre controller module")
        assert "vendor/maplibre/maplibre-gl.css" in src, f"{html} lacks the MapLibre stylesheet"


def test_style_jsons_reference_only_same_origin_resources():
    for name in ("light", "dark", "satellite", "terrain"):
        path = f"{STYLES}/{name}.json"
        assert os.path.isfile(path), f"missing style {name}.json"
        style = json.loads(_read(path))
        assert style.get("version") == 8
        blob = json.dumps(style)
        assert "http://" not in blob and "https://" not in blob, (
            f"{name}.json references an absolute URL — must be same-origin relative")
        for host in EXTERNAL_MAP_HOSTS:
            assert host not in blob, f"{name}.json references external host {host}"
        for sid, src in style["sources"].items():
            for url in src.get("tiles", []):
                assert url.startswith("/maps/"), f"{name}.json source {sid} tile URL is not /maps/: {url}"
        if "glyphs" in style:
            assert style["glyphs"].startswith("/maps/"), f"{name}.json glyphs not same-origin"


def test_no_frontend_or_backend_map_code_references_an_external_map_host():
    files = [f"{REPO}/frontend/js/identity-map.js", f"{REPO}/frontend/js/admin-intelligence.js",
             f"{REPO}/frontend/js/admin-security-intelligence.js", f"{REPO}/frontend/js/admin-pipelines.js",
             f"{REPO}/backend/core/map_data_service.py", f"{REPO}/backend/core/map_availability.py",
             f"{REPO}/config/martin.yaml", f"{REPO}/nginx.conf", f"{REPO}/nginx.prod.conf"]
    for path in files:
        src = _read(path).lower()
        for host in EXTERNAL_MAP_HOSTS:
            assert host not in src, f"{os.path.basename(path)} references external host {host}"


# ---------------------------------------------------------------------------
# Martin + transitional dataset
# ---------------------------------------------------------------------------

def test_martin_config_is_explicit_and_cors_disabled():
    cfg = _read(f"{REPO}/config/martin.yaml")
    assert "mbtiles:" in cfg and "/map-data/production" in cfg
    assert re.search(r"^cors:\s*false", cfg, re.M), (
        "Martin defaults to CORS origin '*'; it must be disabled — nginx same-origin only")
    for compose in ("docker/docker-compose.cpu.yml", "docker/docker-compose.prod.yml"):
        src = _read(f"{REPO}/{compose}")
        assert "ghcr.io/maplibre/martin:1.13.0" in src, f"{compose}: Martin not pinned to 1.13.0"
        assert "ghcr.io/maplibre/martin:latest" not in src
        assert "../config/martin.yaml:/config/martin.yaml:ro" in src
        assert "../map-data:/map-data:ro" in src
        martin_block = src[src.index("  martin:"):src.index("  nginx:")]
        assert "ports:" not in martin_block, f"{compose}: Martin must not be published to the host"
        assert "/health" in martin_block, f"{compose}: Martin healthcheck must hit /health"


def test_map_data_is_ignored_by_git_and_docker():
    """The datasets are hundreds of MB to GB; mounted, never committed or baked."""
    assert re.search(r"^map-data/production/", _read(f"{REPO}/.gitignore"), re.M)
    assert re.search(r"^map-data/", _read(f"{REPO}/.dockerignore"), re.M)


# `test_transitional_raster_archive_matches_the_pyramid_byte_for_byte` stood
# here. It sampled the raster archive against the tiles/ pyramid and asserted
# they were byte-identical — which they were: both were 145,718 copies of
# OpenStreetMap's "Access blocked" image. It asserted poison matched poison,
# passed for the entire life of the defect, and has skipped silently since both
# artifacts were deleted. The TMS row flip it was meant to protect is now
# covered where it can still be got wrong: tile_probe.pick() derives its probe
# address through the same conversion, and install_dataset.sh fails if the
# served tile does not match the stored one.


# ---------------------------------------------------------------------------
# availability + data contract (live)
# ---------------------------------------------------------------------------

def test_availability_is_derived_from_martin_and_names_the_backing_source(admin_token):
    status, raw = _http("GET", "/api/maps/availability", token=admin_token)
    assert status == 200, raw[:200]
    body = _json(raw)
    assert set(body["styles"]) == {"light", "dark", "satellite", "terrain"}
    assert body["unavailable_state"] == "OFFLINE_MAP_DATASET_UNAVAILABLE"
    for name, detail in body["detail"].items():
        if detail["available"]:
            assert detail["source"], f"{name} available but no backing source named"
            assert detail["state"] == "AVAILABLE"
        else:
            assert detail["source"] is None
            assert detail["state"] == "OFFLINE_MAP_DATASET_UNAVAILABLE", (
                f"{name}: an unavailable style must report the deterministic state, "
                f"never a substitute")


def test_light_availability_follows_the_data_not_the_file(admin_token):
    """Light is available IFF a street source is installed AND serves map data.

    This used to assert "archive installed => light available", which is the
    assumption the placeholder incident disproved: the archive was present,
    complete and checksum-verified while every tile was OpenStreetMap's "Access
    blocked" image. Availability is a statement about DATA, so the assertion is
    now conditional on what the backing source actually serves — it holds both
    while the poison archive is installed and after the vector build replaces it.
    """
    archive = os.path.join(PRODUCTION(), "lebanon-streets-raster.mbtiles")
    vector = os.path.join(PRODUCTION(), "lebanon-streets-vector.mbtiles")
    if not (os.path.isfile(archive) or os.path.isfile(vector)):
        pytest.skip("no street dataset installed")
    body = _json(_http("GET", "/api/maps/availability", token=admin_token)[1])
    assert body["martin_reachable"] is True, "Martin unreachable from the API container"

    light = body["detail"]["light"]
    if light["available"]:
        assert light["source"] in ("lebanon-streets-vector", "lebanon-streets-raster")
        assert light["state"] == "AVAILABLE"
    else:
        # refused: every candidate must say WHY, and a placeholder must be named
        assert light["state"] == body["unavailable_state"]
        reasons = [c["error"] for c in light["candidates"].values()]
        assert all(reasons), f"an unavailable style must explain every candidate: {light['candidates']}"
        if os.path.isfile(archive):
            raster = light["candidates"].get("lebanon-streets-raster", {})
            assert raster.get("usable") is False and raster.get("error"), raster


def test_security_analysis_import_succeeds():
    """A failed import here quietly disabled patterns/risk/zones while the
    endpoint kept returning 200 with empty collections (observed: a symbol
    imported from the wrong module). Silent feature loss is the failure mode
    this whole migration exists to remove."""
    import sys
    sys.path.insert(0, REPO)
    from backend.core import map_data_service
    assert map_data_service.SECURITY_ANALYSIS_AVAILABLE is True


def test_map_data_contract_and_ordering(admin_token):
    """Every key present; positions [lng, lat]; detections chronological; a
    camera without coordinates skipped, never placed at (0,0)."""
    from backend.core.map_data_service import build_map_data
    tracks = [{"identity_id": "x", "display_name": "Probe", "movements": [
        {"pipeline_id": "b", "pipeline_name": "Cam B", "timestamp": "2026-08-13T05:00:00Z",
         "coordinates": {"lat": 33.90, "lng": 35.50}},
        {"pipeline_id": "a", "pipeline_name": "Cam A", "timestamp": "2026-08-13T04:00:00Z",
         "coordinates": {"lat": 33.89, "lng": 35.48}},
        {"pipeline_id": "c", "pipeline_name": "No Coords", "timestamp": "2026-08-13T04:30:00Z",
         "coordinates": None},
    ]}]
    d = build_map_data(identity_id="x", tracks=tracks, include_routes=True)
    assert set(d) == {"identity", "detections", "route", "cameras", "risk_points",
                      "security_zones", "patterns", "threats", "metadata"}
    feats = d["detections"]["features"]
    assert [f["properties"]["pipeline_name"] for f in feats] == ["Cam A", "Cam B"], "not chronological"
    assert feats[0]["geometry"]["coordinates"] == [35.48, 33.89], "GeoJSON must be [lng, lat]"
    assert not any(f["geometry"]["coordinates"] == [0, 0] for f in feats), "null island"
    assert d["metadata"]["counts"]["detections"] == 2
    assert "skipped" in " ".join(d["metadata"]["warnings"])
    assert d["route"]["features"][0]["geometry"]["coordinates"] == [[35.48, 33.89], [35.50, 33.90]]
    assert d["metadata"]["bounds"] == [[35.48, 33.89], [35.50, 33.90]]


def test_map_data_endpoint_requires_auth_and_defaults_security_off(admin_token):
    status, _ = _http("GET", "/api/identities/00000000-0000-0000-0000-000000000000/map-data")
    assert status in (401, 403)
    status, raw = _http("GET", "/api/identities/00000000-0000-0000-0000-000000000000/map-data",
                        token=admin_token)
    assert status == 404, "unknown identity should 404, not leak"


# ---------------------------------------------------------------------------
# vector streets: schema contract (Light + Dark over ONE source)
# ---------------------------------------------------------------------------

def _vector_layers_in_archive(path):
    """Layer names Planetiler recorded in the MBTiles `json` metadata."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM metadata WHERE name='json'").fetchone()
    finally:
        con.close()
    if not row:
        return set()
    return {v["id"] for v in json.loads(row[0]).get("vector_layers", [])}


def test_light_and_dark_are_the_same_layers_over_the_same_vector_source():
    """'Dark is a proper style, not a filter' means: identical layer set and
    order, identical source, only paint differs. Generated from one definition
    (scripts/map_data/build_vector_styles.py) precisely so this holds."""
    light = json.loads(_read(f"{STYLES}/light.json"))
    dark = json.loads(_read(f"{STYLES}/dark.json"))
    if light["metadata"].get("armyeye:dataset") != "lebanon-streets-vector":
        pytest.skip("light.json is still the transitional raster style")
    assert [l["id"] for l in light["layers"]] == [l["id"] for l in dark["layers"]]
    assert light["sources"] == dark["sources"]
    assert light["sources"]["streets"]["tiles"] == ["/maps/lebanon-streets-vector/{z}/{x}/{y}"]
    assert light["glyphs"] == "/maps/font/{fontstack}/{range}"
    for style in (light, dark):
        for layer in style["layers"]:
            if layer["type"] == "symbol":
                assert layer["layout"]["text-font"] == ["Noto Sans Regular", "Noto Sans Arabic Regular"], (
                    f"{layer['id']}: labels must use the vendored Noto stack incl. Arabic")


def test_every_source_layer_the_vector_styles_reference_exists_in_the_archive():
    """The style is proven against the data, not assumed to match it: every
    `source-layer` in light/dark must be a layer Planetiler actually emitted."""
    archive = os.path.join(SHIPPED_PRODUCTION(), "lebanon-streets-vector.mbtiles")
    if not os.path.isfile(archive):
        pytest.skip("lebanon-streets-vector.mbtiles not built yet")
    present = _vector_layers_in_archive(archive)
    assert present, "archive has no vector_layers metadata — not an OpenMapTiles build?"
    for name in ("light", "dark"):
        style = json.loads(_read(f"{STYLES}/{name}.json"))
        referenced = {l["source-layer"] for l in style["layers"] if "source-layer" in l}
        missing = referenced - present
        assert not missing, (
            f"{name}.json references source-layers absent from the archive: {sorted(missing)}; "
            f"archive has {sorted(present)}")


# ---------------------------------------------------------------- terrain / DEM


def test_restore_path_is_not_gated_on_isStyleLoaded():
    """`map.isStyleLoaded()` stays false until every source has fetched its
    tiles, and on `style.load` for a real basemap swap that is never yet true —
    while `style.load` does not fire again. Gating the overlay restore on it
    emptied every overlay after switching Light → Terrain (found in headless
    Chrome). addSource/addLayer are legal as soon as `style.load` fires."""
    controller = _read(f"{REPO}/frontend/js/identity-map.js")
    body = controller[controller.index("_restoreOverlays() {"):]
    body = body[:body.index("\n    }\n")]
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith(("//", "*", "/*")))
    assert "isStyleLoaded" not in code, "_restoreOverlays must not early-return on isStyleLoaded()"


def test_terrain_style_is_a_raster_dem_with_terrain_and_hillshade_from_one_source():
    """DEM ≠ terrain ≠ hillshade: one raster-dem source (the DEM) powers both
    the 3-D `terrain` and a client-side `hillshade` layer. Encoding must be one
    MapLibre decodes natively; no separate pre-rendered hillshade dataset."""
    style = json.loads(_read(f"{REPO}/frontend/maps/styles/terrain.json"))
    dem = [s for s in style["sources"].values() if s.get("type") == "raster-dem"]
    assert len(dem) == 1, "terrain.json must declare exactly one raster-dem source"
    assert dem[0]["encoding"] in ("terrarium", "mapbox")
    assert all(u.startswith("/maps/lebanon-dem/") for u in dem[0]["tiles"])
    dem_id = next(k for k, v in style["sources"].items() if v is dem[0])
    assert style.get("terrain", {}).get("source") == dem_id, "terrain must be driven by the DEM source"
    hill = [l for l in style["layers"] if l["type"] == "hillshade"]
    assert hill and all(l["source"] == dem_id for l in hill), "hillshade must come from the same DEM source"


def test_dem_archive_decodes_to_real_lebanon_elevations():
    """Not 'a PNG exists': decode representative terrarium tiles and check the
    numbers are Lebanon's — sea ≈ 0, Qurnat as Sawda ≈ 3,088 m, Bekaa above
    1,000 m, nothing constant on land, no NaN."""
    path = os.path.join(SHIPPED_PRODUCTION(), "lebanon-dem.mbtiles")
    if not os.path.isfile(path):
        pytest.skip("lebanon-dem not installed")
    import io
    import math
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        pytest.skip("Pillow not available")

    def tile_xy(lon, lat, z):
        n = 2 ** z
        x = int((lon + 180) / 360 * n)
        y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
        return x, y

    def decode(blob):
        img = Image.open(io.BytesIO(blob)).convert("RGB")
        px = img.load()
        vals = [px[i, j][0] * 256 + px[i, j][1] + px[i, j][2] / 256 - 32768
                for i in range(0, img.width, 8) for j in range(0, img.height, 8)]
        return vals

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert dict(con.execute("select name, value from metadata"))["encoding"] == "terrarium"
    z = 12
    checks = {  # name: (lon, lat, min_expected_max, max_expected_max)
        "Beirut coast": (35.50, 33.89, 20, 400),
        "Qurnat as Sawda": (36.12, 34.30, 2900, 3150),
        "Bekaa / Baalbek": (36.22, 34.00, 1100, 2400),
        "Mount Hermon": (35.85, 33.42, 2600, 2900),
    }
    for name, (lon, lat, lo, hi) in checks.items():
        x, y = tile_xy(lon, lat, z)
        row = con.execute("select tile_data from tiles where zoom_level=? and tile_column=? and tile_row=?",
                          (z, x, 2 ** z - 1 - y)).fetchone()
        assert row, f"{name}: no DEM tile at z{z}/{x}/{y}"
        vals = decode(row[0])
        assert not any(math.isnan(v) for v in vals)
        assert lo <= max(vals) <= hi, f"{name}: peak {max(vals):.0f} m outside [{lo}, {hi}]"
        assert max(vals) - min(vals) > 10, f"{name}: constant tile"
    x, y = tile_xy(34.90, 33.90, z)  # open Mediterranean
    row = con.execute("select tile_data from tiles where zoom_level=? and tile_column=? and tile_row=?",
                      (z, x, 2 ** z - 1 - y)).fetchone()
    assert row and max(decode(row[0])) == 0 == min(decode(row[0])), "open sea must decode to exactly 0 m"


def test_every_style_source_declares_the_zoom_range_its_dataset_actually_serves():
    """A style source with an explicit `tiles` array and no minzoom/maxzoom makes
    MapLibre assume z0-22, so it requests zooms the archive does not contain and
    Martin 404s every one:

        ERROR martin::srv::tiles::content: error="Zoom 15 is outside the
        supported range: lebanon-dem supports zoom 6-12"

    Observed live for terrain (z3, z4, z13-z16 — terrain + hillshade requests
    against a z6-12 DEM) and latent for satellite. Two rules:

      1. every `tiles` source declares BOTH minzoom and maxzoom (a source that
         resolves through `url` inherits them from the TileJSON instead);
      2. when the dataset IS installed in Martin, the declared range equals the
         served range exactly — a style must advertise neither more (404 storm,
         and for raster-dem a terrain mesh that silently stops updating) nor
         less (blank at zooms the data covers) than exists; and the style bbox
         must not reach outside the archive bbox (the same failure, sideways).

    Measured on the live stack (headless Chrome, terrain style, jumping to
    z13-16 and z3-5): before the fix Martin logged
    `Zoom 15 is outside the supported range` and the map never fired `load`
    at all; after it, 0 requests outside z6-12, 0 non-200 tile responses,
    0 console errors.

    Declared ranges track the builders: lebanon-dem z6-12
    (scripts/map_data/build_dem_terrarium.py), lebanon-satellite z8-14
    (build_satellite.py MIN_Z/MAX_Z), lebanon-streets-raster z10-16, the
    openmaptiles vector archive z0-14.
    """
    import glob

    # Rule 1 is a property of the style FILES and holds anywhere. Rule 2
    # compares them against what Martin is actually serving, which is a claim
    # about the SHIPPED pairing — so it cannot be evaluated on a stack serving
    # the regression fixtures (deliberately z0-8 vector, z6-10 DEM). Comparing
    # shipped styles to fixture data would fail for the wrong reason.
    serving_fixtures = (os.path.normpath(PRODUCTION())
                        != os.path.normpath(SHIPPED_PRODUCTION()))

    missing, mismatched, checked = [], [], []
    for path in sorted(glob.glob(f"{STYLES}/*.json")):
        style = json.loads(_read(path))
        name = os.path.basename(path)
        for source_name, source in (style.get("sources") or {}).items():
            if "tiles" not in source:
                continue                      # resolved from the TileJSON at runtime
            if source.get("minzoom") is None or source.get("maxzoom") is None:
                missing.append(f"{name}:{source_name}")
                continue
            if serving_fixtures:
                continue                      # rule 1 applied; rule 2 needs the real data
            dataset = source["tiles"][0].split("/maps/", 1)[1].split("/", 1)[0]
            status, raw = _http("GET", f"http://nginx/maps/{dataset}")
            if status != 200:
                continue                      # dataset not installed yet; rule 1 still applied
            served_meta = _json(raw)
            declared = (int(source["minzoom"]), int(source["maxzoom"]))
            served = (int(served_meta["minzoom"]), int(served_meta["maxzoom"]))
            checked.append(f"{name}:{source_name}={declared}")
            if declared != served:
                mismatched.append(
                    f"{name}:{source_name} declares {declared} but {dataset} serves {served}")
            # same failure mode geographically: a style bbox wider than the archive
            # makes MapLibre request tiles the dataset has no data for.
            style_bounds, served_bounds = source.get("bounds"), served_meta.get("bounds")
            if style_bounds and served_bounds:
                w, s_, e, n = [float(v) for v in style_bounds]
                sw, ss, se, sn = [float(v) for v in served_bounds]
                if w < sw or s_ < ss or e > se or n > sn:
                    mismatched.append(
                        f"{name}:{source_name} bounds {style_bounds} reach outside {dataset} {served_bounds}")
    assert not missing, (
        "style sources with an explicit tiles array and no zoom bounds — MapLibre would "
        f"request z0-22 and Martin would 404 everything outside the archive: {missing}")
    assert not mismatched, mismatched
    if serving_fixtures:
        pytest.skip("this stack serves the regression fixtures, not the shipped "
                    "archives; rule 1 (every tiles source declares its zoom bounds) "
                    "was still applied to every style")
    assert checked, "no installed dataset was verified; is Martin reachable through nginx?"


def test_a_dataset_of_placeholder_tiles_is_never_reported_available():
    """Structure is not data.

    The transitional raster street archive was 145,718 tiles that were all the
    SAME 7,412-byte PNG: OpenStreetMap's "Access blocked - App is not following
    the tile usage policy" image, saved by whatever scraped the original z/x/y
    pyramid. Every structural check passed — tile present, byte-identical to the
    pyramid, count matches, coverage complete — because each measured structure,
    never content. Light was advertised as AVAILABLE and the operator got a wall
    of "Access blocked".

    Two rules, both unit-checked here (no Martin needed):
      1. a known upstream placeholder image is recognised by content hash;
      2. a source serving one is NOT usable, so its style is UNAVAILABLE.
    """
    from backend.core.map_availability import (PLACEHOLDER_TILES, SourceState,
                                               placeholder_reason, probe_candidates)

    assert PLACEHOLDER_TILES, "the placeholder registry must not be empty"
    osm_blocked = "6eabebf6e8f2ff16f9109c808d7e7a0228fed0235a05c074b4a1ef99f964edfd"
    assert osm_blocked in PLACEHOLDER_TILES, "the OSM blocked tile must stay registered"

    # 1. content recognition — the real bytes, not a name match
    blocked_png = None
    archive = os.path.join(PRODUCTION(), "lebanon-streets-raster.mbtiles")
    if os.path.exists(archive):
        con = sqlite3.connect(f"file:{archive}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
            blocked_png = row[0] if row else None
        finally:
            con.close()
    if blocked_png is not None:
        import hashlib
        if hashlib.sha256(blocked_png).hexdigest() == osm_blocked:
            assert placeholder_reason(blocked_png), "the installed archive's tile must be recognised"
    assert placeholder_reason(b"") is None
    assert placeholder_reason(b"\x89PNG\r\n\x1a\nnot-a-placeholder") is None

    # 2. a placeholder-backed source is not usable
    poisoned = SourceState(id="x", in_catalog=True, tile_ok=True,
                           placeholder=PLACEHOLDER_TILES[osm_blocked])
    assert not poisoned.usable, "a source serving a placeholder image must not be usable"

    # 3. FAIL CLOSED. This assertion used to read
    #        assert SourceState(id="x", in_catalog=True, tile_ok=True).usable
    #    i.e. "in the catalog and answering = usable", which is the permissive
    #    rule that let an archive of error images be served. It is now the
    #    fail-closed rule: content must have been MEASURED and passed. An
    #    intentional semantic change, not a weakened test — the case that used
    #    to pass is the exact case that must now fail.
    unmeasured = SourceState(id="x", in_catalog=True, tile_ok=True)
    assert unmeasured.content_ok is None, "content_ok must default to 'never measured'"
    assert not unmeasured.usable, (
        "a source whose content has never been measured must NOT be usable; "
        "'nobody has looked at it' is not evidence that it is fine")

    measured_bad = SourceState(id="x", in_catalog=True, tile_ok=True,
                               content_ok=False, resources_ok=True)
    assert not measured_bad.usable, "content that failed verification must not be usable"

    measured_good = SourceState(id="x", in_catalog=True, tile_ok=True,
                                content_ok=True, resources_ok=True)
    assert measured_good.usable, "a fully measured, passing source must be usable"

    no_glyphs = SourceState(id="x", in_catalog=True, tile_ok=True,
                            content_ok=True, resources_ok=False)
    assert not no_glyphs.usable, (
        "a style whose glyphs are not served must not be offered: MapLibre drops "
        "every label that uses a missing font stack, silently")

    # 4. the probe must be able to land on a tile that exists, or the content
    #    check never runs (the fixed z11 tile 204'd for the raster archive)
    candidates = probe_candidates({"minzoom": 10, "maxzoom": 16, "bounds": None})
    assert len(candidates) >= 2 and candidates[0][0] == 10, candidates


def test_installing_an_archive_of_one_repeated_image_is_refused():
    """The install-time content gate: an archive that is the same image over and
    over, or a known placeholder, must be refused BEFORE it is installed."""
    import importlib.util
    import tempfile

    spec = importlib.util.spec_from_file_location(
        "coverage_check", f"{REPO}/scripts/map_data/coverage_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    import io
    import random

    from PIL import Image

    def _png(seed, *, solid=False):
        """A 256px PNG: either one flat colour, or deterministic noise that
        decodes with real entropy — the gate judges DECODED content now, so a
        fixture of `b"tile-0"` would be rejected for not being an image at all,
        which would test the wrong thing."""
        image = Image.new("RGB", (256, 256))
        rng = random.Random(seed)
        if solid:
            # colour derived from the seed: distinct flat graphics, each one
            # individually uniform — the shape that beats a top-share rule
            image.paste((40 + (seed * 37) % 200, 60 + (seed * 53) % 180, 80 + (seed * 71) % 160),
                        (0, 0, 256, 256))
        else:
            image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                           for _ in range(256 * 256)])
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    def _archive(path, tiles, fmt="png"):
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                    "tile_row INTEGER, tile_data BLOB)")
        if fmt:
            con.executemany("INSERT INTO metadata VALUES (?,?)",
                            [("format", fmt), ("minzoom", "10"), ("maxzoom", "10"), ("name", "fixture")])
        con.executemany("INSERT INTO tiles VALUES (?,?,?,?)", tiles)
        con.commit()
        con.close()

    with tempfile.TemporaryDirectory() as tmp:
        # one image repeated — the shape of the poisoned archive
        degenerate = os.path.join(tmp, "degenerate.mbtiles")
        same = _png(0, solid=True)
        _archive(degenerate, [(10, x, 1, same) for x in range(50)])
        verdict = mod.content_check(degenerate)
        assert verdict["pass"] is False and "SAME image" in verdict["reason"], verdict

        # genuinely varied imagery is accepted
        varied = os.path.join(tmp, "varied.mbtiles")
        _archive(varied, [(10, x, 1, _png(x)) for x in range(50)])
        accepted = mod.content_check(varied)
        assert accepted["pass"] is True, accepted
        assert accepted["kind"] == "imagery", accepted

        # a handful of alternating graphics beats the 98% rule but not the floor
        few = os.path.join(tmp, "few.mbtiles")
        palette = [_png(1, solid=True), _png(2, solid=True), _png(3, solid=True)]
        _archive(few, [(10, x, 1, palette[x % 3]) for x in range(60)])
        thin = mod.content_check(few)
        assert thin["pass"] is False and "distinct" in thin["reason"], thin

        # blank/uniform imagery is refused even when every tile differs
        blank = os.path.join(tmp, "blank.mbtiles")
        _archive(blank, [(10, x, 1, _png(x, solid=True)) for x in range(50)])
        assert mod.content_check(blank)["pass"] is False

        # a truncated tile must be caught by decoding, not trusted
        corrupt = os.path.join(tmp, "corrupt.mbtiles")
        _archive(corrupt, [(10, x, 1, _png(x)[:120]) for x in range(50)])
        assert mod.content_check(corrupt)["pass"] is False

        empty = os.path.join(tmp, "empty.mbtiles")
        _archive(empty, [])
        assert mod.content_check(empty)["pass"] is False

        # DETERMINISM: an install refusal must reproduce for the operator
        assert mod.content_check(few)["reason"] == thin["reason"]

    # The gate is wired into the installer BEFORE the swap. This used to be a
    # substring-order check over the shell script; the transaction now lives in
    # install_dataset.py, so the ordering is asserted on the code that actually
    # executes it, and the fault-injection tests below prove the ordering holds
    # at runtime rather than only in the source.
    transaction = _read(f"{REPO}/scripts/map_data/install_dataset.py")
    # From the function body, not the file: the module docstring documents the
    # step order and would satisfy a naive index() search over the whole text.
    body = transaction.split("def install(", 1)[1]
    assert body.index("validate_archive(staged)") < body.index("os.replace(staged, dest)"), \
        "the content gate must run BEFORE the staged archive is moved into place"
    assert body.index("os.replace(staged, dest)") < body.index("promote_pending"), \
        "the verdict must be activated only after the bytes are in place"
    # The gate runs where the runtime lives: the api container has the decoders
    # (Pillow) and the repo mount; this dev host has no working python3, no
    # sqlite3 CLI and no Pillow, so a host-side gate silently could not run.
    install = _read(f"{REPO}/scripts/map_data/install_dataset.sh")
    assert 'docker exec' in install, "validation must run inside the api container"
    assert "install_dataset.py" in install, (
        "the shell wrapper must delegate to the transaction, not re-implement it")


def test_no_installed_dataset_serves_placeholder_tiles():
    """Every installed archive must contain map data — not one image repeated,
    and never an upstream error/placeholder image.

    This is the check that was missing when `lebanon-streets-raster` shipped
    145,718 identical copies of OpenStreetMap's "Access blocked" PNG: coverage
    was complete, checksums matched the source pyramid, and the basemap was a
    wall of "Access blocked". If this test fails, the named archive carries no
    map: rebuild it (Planetiler vector streets) or remove it — the runtime
    already refuses to advertise a style backed by it.
    """
    import glob
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "coverage_check", f"{REPO}/scripts/map_data/coverage_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    archives = sorted(glob.glob(os.path.join(PRODUCTION(), "*.mbtiles")))
    if not archives:
        pytest.skip("no production archives installed")
    bad = []
    for path in archives:
        verdict = mod.content_check(path)
        if not verdict["pass"]:
            bad.append(f"{os.path.basename(path)}: {verdict['reason']}")
    assert not bad, "installed archives that carry no map data:\n  " + "\n  ".join(bad)


def test_install_script_verifies_freshness_not_presence():
    """Martin 1.13.0 has no hot reload (proven): after an atomic swap it keeps
    serving the old archive while the catalog still lists the id. The install
    script must therefore compare the SERVED probe tile with the tile inside
    the archive it just installed, and only ever restart Martin — nothing else."""
    src = _read(f"{REPO}/scripts/map_data/install_dataset.sh")
    # The probe address is DERIVED from the archive, not hard-coded: a fixed z11
    # Beirut tile does not exist in every dataset (a z13-18 satellite build would
    # fail freshness verification for the sole reason that the address is absent).
    assert "tile_probe.py" in src and "pick" in src, "probe address must come from the archive"
    assert "PROBE_X=1225" not in src, "the hard-coded probe address must be gone"
    assert "restart martin" in src

    probe = _read(f"{REPO}/scripts/map_data/tile_probe.py")
    assert "STALE" in probe and "FRESH" in probe and "sha256" in probe
    # Vector tiles are stored gzipped and served content-negotiated, so both
    # sides must be normalised before hashing or every MVT install reports STALE.
    assert "def normalise" in probe and "gzip.decompress" in probe, \
        "freshness comparison must normalise compression on both sides"
    for forbidden in ("restart api", "restart nginx", "restart db", "restart redis", "down -v", "restart worker"):
        assert forbidden not in src, f"install script must never {forbidden}"


def test_every_installed_dataset_covers_lebanon_sites_and_every_camera():
    """D4 coverage acceptance (Docs/86 §4): every installed archive must have a
    tile at eight Lebanon locations AND at every camera coordinate in the
    database, at min/mid/max zoom. Runs the same script operators run."""
    import subprocess
    # The tree this stack actually serves: coverage is a property of whatever
    # is installed here, unlike the style/archive pairing above.
    prod = PRODUCTION()
    if not glob_mbtiles(prod):
        pytest.skip("no datasets installed")
    proc = subprocess.run(
        # --coverage-only: this test owns COVERAGE (is a tile present at every
        # site/zoom). Whether those tiles are real map data is a different
        # question with its own test below, so one failure never masks the other.
        ["python3", f"{REPO}/scripts/map_data/coverage_check.py", "--production", prod,
         "--coverage-only"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout and "MISSING" not in proc.stdout, proc.stdout


# ---------------------------------------------------------------------------
# Fail-closed content ledger
#
# The defect these cover: a dataset was judged usable from STRUCTURE alone —
# in the catalog, answering with bytes, not one known bad hash. An archive of
# 145,718 copies of OpenStreetMap's "Access blocked" image satisfied all three.
# The rule now is that content must have been decoded and MEASURED, and that
# "never measured" is not usable.
# ---------------------------------------------------------------------------

def _load_script(name):
    """Load a scripts/map_data module by path (they are not a package)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        f"_probe_{name}", f"{REPO}/scripts/map_data/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tile_png(seed, *, solid=False, size=256):
    """A decodable PNG: deterministic noise, or one flat colour derived from
    the seed. The gate judges DECODED content, so a fixture of b"tile-0" would
    be rejected for not being an image and would test the wrong thing."""
    import io
    import random as _random
    from PIL import Image
    image = Image.new("RGB", (size, size))
    rng = _random.Random(seed)
    if solid:
        image.paste((40 + (seed * 37) % 200, 60 + (seed * 53) % 180, 80 + (seed * 71) % 160),
                    (0, 0, size, size))
    else:
        image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                       for _ in range(size * size)])
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _make_archive(path, tiles, *, meta=None):
    """Write a minimal MBTiles. `meta=None` means NO metadata rows at all —
    the signature of a build that died before writing them."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                "tile_row INTEGER, tile_data BLOB)")
    if meta:
        con.executemany("INSERT INTO metadata VALUES (?,?)", list(meta.items()))
    con.executemany("INSERT INTO tiles VALUES (?,?,?,?)", tiles)
    con.commit()
    con.close()
    return path


IMAGERY_META = {"format": "png", "minzoom": "10", "maxzoom": "10",
                "bounds": "34.80,32.84,36.92,34.89", "name": "fixture"}


def test_the_reason_code_registries_agree_across_the_stack():
    """One vocabulary, three processes.

    coverage_check and build_helpers run inside preparation containers that
    cannot import the backend, so each carries its own copy of the codes it
    emits. A code that exists in only one of them reaches a client as a string
    nothing can route on.
    """
    from backend.core import map_content_ledger as ledger

    for name in ("coverage_check", "build_helpers"):
        module = _load_script(name)
        emitted = {value for key, value in vars(module).items()
                   if key.isupper() and isinstance(value, str)
                   and value.isupper() and value == key}
        unknown = emitted - set(ledger.REASON_CODES)
        assert not unknown, (
            f"scripts/map_data/{name}.py emits {sorted(unknown)}, which "
            f"backend/core/map_content_ledger.REASON_CODES does not define")

    # And the placeholder registries stay byte-identical.
    from backend.core.map_availability import PLACEHOLDER_TILES
    assert _load_script("coverage_check").PLACEHOLDER_TILES == PLACEHOLDER_TILES


def test_a_verdict_is_bound_to_the_bytes_it_was_taken_from(tmp_path):
    """A stale verdict must never authorize a replaced archive.

    SHA-256 is the authoritative identity, but hashing a multi-GB archive on
    every availability refresh is its own outage — so the cheap check is the
    full stat tuple, and ANY part of it changing means "unverified" until
    something re-measures it.
    """
    from backend.core import map_content_ledger as ledger

    prod = tmp_path / "production"
    prod.mkdir()
    archive = str(prod / "probe.mbtiles")
    _make_archive(archive, [(10, x, 1, _tile_png(x)) for x in range(40)], meta=IMAGERY_META)

    entry = ledger.build_entry("probe", archive)
    entries = {"probe": entry}
    assert ledger.verdict_for("probe", entries=entries, production_dir=str(prod))[0] is True

    # every field of the cheap identity, one at a time
    for field, mutate in (
        ("mtime_ns", lambda: os.utime(archive, (1, 1))),
        ("size_bytes", lambda: open(archive, "ab").write(b"x")),
    ):
        fresh = ledger.build_entry("probe", archive)
        mutate()
        ok, code, _msg = ledger.verdict_for("probe", entries={"probe": fresh},
                                            production_dir=str(prod))
        assert ok is None and code == "CONTENT_NOT_VERIFIED", (
            f"a change in {field} left the old verdict authorizing the new bytes")

    # a wholly different archive at the same path
    other = ledger.build_entry("probe", archive)
    _make_archive(archive + ".tmp", [(10, x, 1, _tile_png(x + 500)) for x in range(40)],
                  meta=IMAGERY_META)
    os.replace(archive + ".tmp", archive)
    ok, code, _ = ledger.verdict_for("probe", entries={"probe": other}, production_dir=str(prod))
    assert ok is None and code == "CONTENT_NOT_VERIFIED"

    # gone entirely
    os.unlink(archive)
    ok, code, _ = ledger.verdict_for("probe", entries={"probe": other}, production_dir=str(prod))
    assert ok is None and code == "CONTENT_MISSING"


def test_a_pending_verdict_authorizes_nothing(tmp_path):
    """Pending entries describe a STAGED archive that is not in place yet.

    They are also kept in their own map rather than overwriting the active
    entry for the same id: measured, a shared key took the archive being
    replaced offline the moment an install started, and left it offline if the
    install then failed.
    """
    from backend.core import map_content_ledger as ledger

    prod = tmp_path / "production"
    prod.mkdir()
    archive = str(prod / "probe.mbtiles")
    _make_archive(archive, [(10, x, 1, _tile_png(x)) for x in range(40)], meta=IMAGERY_META)

    pending = ledger.build_entry("probe", archive, state=ledger.STATE_PENDING)
    ok, code, _ = ledger.verdict_for("probe", entries={"probe": pending},
                                     production_dir=str(prod))
    assert ok is None and code == "CONTENT_NOT_VERIFIED", (
        "a pending verdict authorized a dataset")

    ledger_file = str(tmp_path / "content_verdicts.json")
    active = ledger.build_entry("probe", archive)
    ledger.save({"probe": active}, {"probe": pending}, path=ledger_file)
    assert ledger.load(ledger_file)["probe"]["state"] == "active"
    assert ledger.load_pending(ledger_file)["probe"]["state"] == "pending"


def test_the_ledger_survives_a_corrupt_file_by_failing_closed(tmp_path):
    """An unparseable ledger must leave every dataset unverified, not crash
    availability and not silently authorize anything."""
    from backend.core import map_content_ledger as ledger

    broken = tmp_path / "content_verdicts.json"
    broken.write_text("{not json at all", encoding="utf-8")
    assert ledger.load(str(broken)) == {}

    broken.write_text('{"version": 1}', encoding="utf-8")
    assert ledger.load(str(broken)) == {}


def test_the_ledger_is_written_atomically(tmp_path):
    """A reader must never see a half-written ledger, and a crash must leave
    the previous one intact — the discipline used for ML artifacts."""
    from backend.core import map_content_ledger as ledger

    source = _read(f"{REPO}/backend/core/map_content_ledger.py")
    assert "os.replace(tmp, target)" in source, "the ledger is not renamed into place"
    assert "os.fsync" in source, "the ledger is not fsynced before the rename"

    path = str(tmp_path / "content_verdicts.json")
    ledger.save({"alpha": {"source_id": "alpha", "state": "active", "pass": True}}, {}, path=path)
    assert set(ledger.load(path)) == {"alpha"}
    ledger.save({"beta": {"source_id": "beta", "state": "active", "pass": True}}, {}, path=path)
    assert set(ledger.load(path)) == {"beta"}, "the rewrite did not replace the previous entries"
    leftovers = [f for f in os.listdir(str(tmp_path)) if f.endswith(".tmp")]
    assert not leftovers, f"atomic write left temporary files behind: {leftovers}"


# ---------------------------------------------------------------------------
# Content refusals, by machine-readable code
# ---------------------------------------------------------------------------

def test_an_error_page_stored_as_a_tile_is_refused_as_a_blocked_source(tmp_path):
    """The next incarnation of the original defect.

    If the scraper had saved the deny PAGE rather than OSM's placeholder PNG,
    the archive would hold HTML under a .png tile. That must be refused as
    SOURCE_BLOCKED — a licence/credentials problem — and not as a corrupt
    archive, which would send the operator to rebuild instead.
    """
    mod = _load_script("coverage_check")

    # DISTINCT deny pages, so this cannot pass by accident through the
    # "98% identical" rule — the archive must be refused for what the tiles
    # ARE, not for them happening to be uniform.
    archive = str(tmp_path / "blocked.mbtiles")
    _make_archive(archive, [
        (10, 610 + i, 419 + (i % 6),
         f"<!DOCTYPE html><html><head><title>403 Forbidden</title></head>"
         f"<body>Access denied for request {i}</body></html>".encode())
        for i in range(40)], meta=IMAGERY_META)

    ok, verdict = mod.validate_archive(archive)
    assert ok is False
    assert verdict["code"] == "SOURCE_BLOCKED", verdict
    assert "refused" in verdict["reason"] or "deny" in verdict["reason"].lower(), verdict["reason"]

    # And a uniform one is still SOURCE_BLOCKED, not CONTENT_DEGENERATE: an
    # operator told "98% of tiles are identical" goes and rebuilds, when the
    # actual problem is that the source refused the download.
    uniform = str(tmp_path / "blocked_uniform.mbtiles")
    page = b"<!DOCTYPE html><html><body>Access denied</body></html>"
    _make_archive(uniform, [(10, 610 + i, 419 + (i % 6), page) for i in range(40)],
                  meta=IMAGERY_META)
    ok, verdict = mod.validate_archive(uniform)
    assert ok is False and verdict["code"] == "SOURCE_BLOCKED", verdict


def test_an_archive_with_no_metadata_is_refused_as_an_unfinished_build(tmp_path):
    """Both builders write metadata LAST historically, so tiles with an empty
    metadata table is exactly what an interrupted build leaves — and it is what
    the failed satellite run DID leave: 29 tiles, no metadata. It must be named
    as an incomplete build, and it must not raise."""
    mod = _load_script("coverage_check")
    archive = str(tmp_path / "headless.mbtiles")
    _make_archive(archive, [(10, x, 1, _tile_png(x)) for x in range(29)], meta=None)

    ok, verdict = mod.validate_archive(archive)
    assert ok is False and verdict["code"] == "BUILD_INCOMPLETE", verdict

    # and the coverage path reports it rather than raising KeyError
    result = mod.check_dataset(archive, [("Beirut", 35.4955, 33.8938)])
    assert result["pass"] is False and result["metadata_valid"] is False


def test_a_build_that_never_reached_its_deepest_zoom_is_refused(tmp_path):
    """The exact shape of the interrupted satellite build.

    It left 29 tiles at z8-z9 while its metadata declared z8-z14. Every
    count-based rule finds that plausible — 29 tiles is not obviously wrong —
    but the declared zoom range being unpopulated is unambiguous: the build
    stopped partway.
    """
    mod = _load_script("coverage_check")
    meta = dict(IMAGERY_META, minzoom="8", maxzoom="14")
    archive = str(tmp_path / "fragment.mbtiles")
    _make_archive(archive, [(8, 145 + x, 100, _tile_png(x)) for x in range(9)]
                  + [(9, 290 + x, 200, _tile_png(x + 50)) for x in range(20)], meta=meta)

    ok, verdict = mod.validate_archive(archive)
    assert ok is False and verdict["code"] == "BUILD_INCOMPLETE", verdict
    assert "no tiles at" in verdict["reason"], verdict["reason"]


def test_a_tile_count_that_cannot_fill_its_own_bbox_is_refused(tmp_path):
    """Populated at every declared zoom, but far too sparse to be that map.

    Measured ratios of tiles held to footprint capacity: DEM 1.00, vector 0.81.
    One tile per zoom over Lebanon at z6-14 is 0.0008.
    """
    mod = _load_script("coverage_check")
    meta = dict(IMAGERY_META, minzoom="6", maxzoom="14")
    archive = str(tmp_path / "sparse.mbtiles")
    _make_archive(archive, [(z, 40 + z, 30 + z, _tile_png(z)) for z in range(6, 15)],
                  meta=meta)

    ok, verdict = mod.validate_archive(archive)
    assert ok is False and verdict["code"] == "TILE_COUNT_INVALID", verdict
    assert "declared bbox" in verdict["reason"]


def test_an_archive_of_osm_blocked_images_can_never_be_installed_or_reported_available(tmp_path):
    """The named regression for the original failure, end to end.

    Reconstructs the exact defect — an archive whose every tile is the real
    OpenStreetMap "Access blocked" image — and asserts all three doors are
    shut: the content gate refuses it, the install transaction refuses it, and
    a source serving it is not usable at runtime.
    """
    import subprocess

    from backend.core.map_availability import (PLACEHOLDER_TILES, SourceState,
                                               placeholder_reason)
    mod = _load_script("coverage_check")

    # The genuine bytes, recovered from the registry's own hash: build a PNG
    # and confirm it is the registered placeholder before using it as one.
    blocked = None
    for source in sorted(glob.glob(os.path.join(PRODUCTION(), "*.mbtiles"))):
        con = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
        finally:
            con.close()
        if row and placeholder_reason(bytes(row[0])):
            blocked = bytes(row[0])
            break
    if blocked is None:
        # The poisoned archive is deleted, as it should be. Synthesise the
        # SHAPE of the defect instead: one image repeated across the archive.
        blocked = _tile_png(1, solid=True)

    archive = str(tmp_path / "poison.mbtiles")
    _make_archive(archive, [(10, x, 1, blocked) for x in range(60)], meta=IMAGERY_META)

    # 1. the content gate
    ok, verdict = mod.validate_archive(archive)
    assert ok is False, "an archive of one repeated error image passed validation"
    assert verdict["code"] in ("PLACEHOLDER_CONTENT", "CONTENT_DEGENERATE"), verdict

    # 2. the install transaction, run exactly as the operator runs it
    work = tmp_path / "install"
    (work / "production").mkdir(parents=True)
    (work / "metadata").mkdir(parents=True)
    proc = subprocess.run(
        ["python3", f"{REPO}/scripts/map_data/install_dataset.py", archive, "lebanon-streets-vector"],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "MAP_DATA_DIR": str(work)})
    assert proc.returncode != 0, "the installer accepted an archive of error images"
    assert "PLACEHOLDER_CONTENT" in proc.stderr or "CONTENT_DEGENERATE" in proc.stderr, proc.stderr
    installed = os.listdir(str(work / "production"))
    assert installed == [], f"a refused archive still left files behind: {installed}"

    # 3. the runtime
    poisoned = SourceState(id="x", in_catalog=True, tile_ok=True,
                           placeholder=next(iter(PLACEHOLDER_TILES.values())),
                           content_ok=True, resources_ok=True)
    assert not poisoned.usable


# ---------------------------------------------------------------------------
# The availability reason invariant
# ---------------------------------------------------------------------------

def test_an_unavailable_style_always_names_a_registered_reason_code(admin_token):
    """No client should ever have to render {"available": false, "reason": null}.

    The frontend was already written for detail[style].reason — and the backend
    emitted no such key at all, so admin-pipelines.js showed the bare code for
    every failure regardless of cause.
    """
    from backend.core import map_content_ledger as ledger

    status, raw = _http("GET", "/api/maps/availability", token=admin_token)
    assert status == 200, raw[:200]
    body = _json(raw)

    assert all(isinstance(v, bool) for v in body["styles"].values()), (
        "styles must stay a flat {name: bool} map; identity-map.js does "
        "opt.disabled = !ok over it")

    for name, detail in body["detail"].items():
        if detail["available"]:
            assert detail["reason"] is None, f"{name} is available but names a reason"
            assert detail["source"] and detail["source_type"], (
                f"{name} is available but does not say what backs it")
        else:
            assert detail["reason"], f"{name} is unavailable with reason={detail['reason']!r}"
            assert detail["reason"] in ledger.REASON_CODES, (
                f"{name} reports {detail['reason']!r}, which is not a registered code")
            assert detail["reason_text"], f"{name} has a code but no human sentence"


def test_the_reason_invariant_is_executable_logic_not_an_assert():
    """`python -O` strips asserts. A guarantee that evaporates under an
    optimisation flag is not a guarantee, so the invariant is a branch that
    degrades to a registered internal code, logs at ERROR and counts a metric.
    """
    from backend.core import map_availability as ma

    source = _read(f"{REPO}/backend/core/map_availability.py")
    body = source.split("def style_entry(", 1)[1].split("\ndef ", 1)[0]
    # Statements only: the docstring explains why this is not an assert, and a
    # naive substring search matches that prose.
    statements = [line.strip() for line in body.splitlines()
                  if line.strip().startswith("assert ")]
    assert not statements, (
        f"style_entry guards the reason invariant with an assert; -O removes it: {statements}")

    # A reason composer that returns nonsense must not produce reason=null.
    original = ma.compose_reason
    try:
        ma.compose_reason = lambda *a, **k: (None, None)
        entry = ma.style_entry("satellite", None, [ma.SourceState(id="s")])
    finally:
        ma.compose_reason = original
    assert entry["available"] is False
    assert entry["reason"] == "AVAILABILITY_STATE_INVALID", entry
    assert entry["reason_text"], "the internal code must still carry a sentence"

    # And it survives the flag that removes asserts.
    import subprocess
    code = (
        "import sys; sys.path.insert(0, '/app');"
        "from backend.core import map_availability as m;"
        "m.compose_reason = lambda *a, **k: (None, None);"
        "e = m.style_entry('satellite', None, [m.SourceState(id='s')]);"
        "print(e['reason'])"
    )
    proc = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True,
                          text=True, cwd=REPO, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "AVAILABILITY_STATE_INVALID", (
        f"under -O the invariant produced {proc.stdout.strip()!r}")


def test_terrain_stays_available_once_its_content_is_measured(admin_token):
    """The sequencing risk of failing closed: the installed DEM had no verdict,
    so turning the rule on without populating the ledger would have taken
    Terrain offline."""
    if not os.path.isfile(os.path.join(PRODUCTION(), "lebanon-dem.mbtiles")):
        pytest.skip("no DEM installed")
    from backend.core import map_content_ledger as ledger

    entry = ledger.load().get("lebanon-dem")
    assert entry and entry["state"] == "active" and entry["pass"] is True, (
        "the installed DEM has no active passing verdict; Terrain would be "
        "reported unavailable")
    status, raw = _http("GET", "/api/maps/availability", token=admin_token)
    body = _json(raw)
    assert body["styles"]["terrain"] is True
    assert body["detail"]["terrain"]["candidates"]["lebanon-dem"]["checks"]["content_ok"] is True


# ---------------------------------------------------------------------------
# The install transaction: every crash point
# ---------------------------------------------------------------------------

FAIL_POINTS = ["after_stage", "after_validate", "after_ledger_pending", "before_rename",
               "after_rename", "before_ledger_active", "after_ledger_active", "before_refresh"]
COMMITTED_AFTER = {"after_ledger_active", "before_refresh"}


@pytest.mark.parametrize("point", FAIL_POINTS)
def test_a_crash_never_leaves_new_bytes_wearing_the_old_verdict(tmp_path, point):
    """Replacing a dataset must be all-or-nothing from the runtime's view.

    Every abort point must leave either the OLD archive serving and available,
    or the NEW bytes in place and reported UNAVAILABLE. Never new bytes with
    the previous archive's authorization — that is how an archive nobody
    inspected gets served.
    """
    import subprocess

    from backend.core import map_content_ledger as ledger

    work = tmp_path / "map-data"
    (work / "production").mkdir(parents=True)
    (work / "metadata").mkdir(parents=True)
    env = {**os.environ, "MAP_DATA_DIR": str(work)}

    old = _make_archive(str(tmp_path / "old.mbtiles.new"),
                        [(10, x, 1, _tile_png(x)) for x in range(40)], meta=IMAGERY_META)
    new = _make_archive(str(tmp_path / "new.mbtiles.new"),
                        [(10, x, 1, _tile_png(x + 900)) for x in range(60)], meta=IMAGERY_META)

    def install(source, fail_at=None):
        extra = {"MAP_INSTALL_FAIL_AT": fail_at} if fail_at else {}
        return subprocess.run(
            ["python3", f"{REPO}/scripts/map_data/install_dataset.py", source, "probe"],
            capture_output=True, text=True, timeout=600, env={**env, **extra})

    assert install(old).returncode == 0

    def verdict():
        proc = subprocess.run(
            ["python3", "-c",
             "import json,sys; sys.path.insert(0,'/app');"
             "from backend.core import map_content_ledger as l;"
             "print(json.dumps(l.verdict_for('probe')[:2]))"],
            capture_output=True, text=True, timeout=120, env=env, cwd=REPO)
        return json.loads(proc.stdout)

    assert verdict()[0] is True, "the baseline install is not usable"
    old_size = os.path.getsize(str(work / "production" / "probe.mbtiles"))

    install(new, fail_at=point)
    content_ok, code = verdict()
    size = os.path.getsize(str(work / "production" / "probe.mbtiles"))
    new_bytes = size != old_size

    if point in COMMITTED_AFTER:
        assert new_bytes and content_ok is True, (
            f"{point}: the transaction had committed, so the new archive must be usable")
    elif new_bytes:
        assert content_ok is not True, (
            f"{point}: NEW bytes are on disk and still reported usable — they "
            f"inherited the previous archive's verdict")
        assert code == "CONTENT_NOT_VERIFIED", code
    else:
        assert content_ok is True, (
            f"{point}: the old archive is untouched but was reported unusable; "
            f"an install that changed nothing took a working dataset offline")


def test_rollback_restores_the_previously_verified_archive(tmp_path):
    """The operator's undo for a half-finished install."""
    import subprocess

    work = tmp_path / "map-data"
    (work / "production").mkdir(parents=True)
    (work / "metadata").mkdir(parents=True)
    env = {**os.environ, "MAP_DATA_DIR": str(work)}
    old = _make_archive(str(tmp_path / "old.mbtiles.new"),
                        [(10, x, 1, _tile_png(x)) for x in range(40)], meta=IMAGERY_META)
    new = _make_archive(str(tmp_path / "new.mbtiles.new"),
                        [(10, x, 1, _tile_png(x + 900)) for x in range(60)], meta=IMAGERY_META)
    script = f"{REPO}/scripts/map_data/install_dataset.py"

    subprocess.run(["python3", script, old, "probe"], env=env, capture_output=True, timeout=600)
    old_size = os.path.getsize(str(work / "production" / "probe.mbtiles"))
    subprocess.run(["python3", script, new, "probe"], env={**env, "MAP_INSTALL_FAIL_AT": "after_rename"},
                   capture_output=True, timeout=600)
    assert os.path.getsize(str(work / "production" / "probe.mbtiles")) != old_size

    proc = subprocess.run(["python3", script, "--rollback", "probe"], env=env,
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr
    assert os.path.getsize(str(work / "production" / "probe.mbtiles")) == old_size
    assert "content_ok=True" in proc.stdout, proc.stdout


def test_a_verified_archive_is_never_replaced_by_one_that_failed(tmp_path):
    """A rejected candidate must leave the working dataset exactly as it was."""
    import subprocess

    work = tmp_path / "map-data"
    (work / "production").mkdir(parents=True)
    (work / "metadata").mkdir(parents=True)
    env = {**os.environ, "MAP_DATA_DIR": str(work)}
    good = _make_archive(str(tmp_path / "good.mbtiles.new"),
                         [(10, x, 1, _tile_png(x)) for x in range(40)], meta=IMAGERY_META)
    poison = _make_archive(str(tmp_path / "poison.mbtiles.new"),
                           [(10, x, 1, _tile_png(3, solid=True)) for x in range(40)],
                           meta=IMAGERY_META)
    script = f"{REPO}/scripts/map_data/install_dataset.py"

    subprocess.run(["python3", script, good, "probe"], env=env, capture_output=True, timeout=600)
    before = _read_bytes(str(work / "production" / "probe.mbtiles"))

    proc = subprocess.run(["python3", script, poison, "probe"], env=env,
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode != 0
    after = _read_bytes(str(work / "production" / "probe.mbtiles"))
    assert before == after, "a failed candidate modified the installed archive"
    orphans = [f for f in os.listdir(str(work / "production")) if not f.endswith(".mbtiles")]
    assert not orphans, f"a refused install left {orphans} behind"


def _read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# Download hardening
# ---------------------------------------------------------------------------

def test_download_failures_are_classified_and_bounded():
    """Retrying a 403 forever is how a licence error becomes an overnight hang;
    giving up on a 503 is how a bucket blip fails a whole build."""
    bh = _load_script("build_helpers")

    assert bh.is_transient(status=503) and bh.is_transient(status=429)
    assert not bh.is_transient(status=403) and not bh.is_transient(status=404)
    assert bh.status_code(403) == "SOURCE_BLOCKED"
    assert bh.status_code(404) == "DOWNLOAD_FAILED"

    # HTTPError is a URLError subclass, so it MUST be classified by status
    # before the generic network-error rule, or a 403 reads as a flaky link.
    import urllib.error
    forbidden = urllib.error.HTTPError("https://x/y", 403, "Forbidden", {}, None)
    assert not bh.is_transient(exc=forbidden), (
        "a 403 was treated as a transient network error and would be retried")
    assert bh.is_transient(exc=urllib.error.HTTPError("https://x/y", 503, "s", {}, None))
    assert bh.is_transient(exc=TimeoutError("stalled"))

    # backoff is exponential with FULL jitter, and bounded
    delays = [bh.backoff_delay(i) for i in range(8)]
    assert all(0 <= d <= 30.0 for d in delays), delays
    assert max(delays[4:]) > max(delays[:2]) or len(set(delays)) > 4, (
        "backoff does not grow; a constant delay synchronises parallel retries")


def test_a_download_never_leaves_a_partial_file_under_the_final_name(tmp_path):
    """`dest` appears only after status, length, block-page sniff, checksum and
    the caller's own validation have all passed. Anything less and the next run
    finds a truncated file that looks complete — which is exactly how the
    satellite build died on a COG cut short at 1,820,566 of 2,067,792 bytes."""
    bh = _load_script("build_helpers")

    payload = bytes(range(256)) * 400
    server, base, cert = _tls_server({
        "/good.tif": (200, payload, "application/octet-stream"),
        "/deny.tif": (200, b"<!DOCTYPE html><html>Access denied</html>", "application/octet-stream"),
        "/html.tif": (200, b"nope", "text/html"),
        "/gone.tif": (404, b"no", "text/plain"),
    })
    try:
        out = str(tmp_path / "out")
        os.makedirs(out)

        dest = os.path.join(out, "good.bin")
        bh.fetch(f"{base}/good.tif", dest, allowed_hosts={"localhost"},
                 reserve_gb=0.001, log=lambda *a: None)
        assert _read_bytes(dest) == payload

        for name, path, expected in (
            ("deny", "/deny.tif", "SOURCE_BLOCKED"),
            ("html", "/html.tif", "SOURCE_BLOCKED"),
            ("gone", "/gone.tif", "DOWNLOAD_FAILED"),
        ):
            target = os.path.join(out, name + ".bin")
            with pytest.raises(bh.BuildError) as exc:
                bh.fetch(f"{base}{path}", target, allowed_hosts={"localhost"},
                         reserve_gb=0.001, max_attempts=2, log=lambda *a: None)
            assert exc.value.code == expected, (name, exc.value.code)
            assert not os.path.exists(target), f"{name}: a refused download left {target}"
            assert not os.path.exists(target + ".part"), f"{name}: a .part survived"

        # a checksum that does not match is a permanent refusal
        target = os.path.join(out, "sum.bin")
        with pytest.raises(bh.BuildError) as exc:
            bh.fetch(f"{base}/good.tif", target, allowed_hosts={"localhost"},
                     expected_sha256="0" * 64, reserve_gb=0.001, log=lambda *a: None)
        assert exc.value.code == "CHECKSUM_MISMATCH"
        assert not os.path.exists(target)
    finally:
        server.shutdown()
        os.unlink(cert)


def test_a_download_refuses_hosts_and_schemes_outside_the_allow_list(tmp_path):
    """An open-ended manifest is a way to smuggle in imagery whose licence
    forbids offline storage."""
    bh = _load_script("build_helpers")

    for url in ("https://tile.openstreetmap.org/10/1/1.png",
                "https://mt1.google.com/vt/x=1",
                "http://localhost/plain.tif"):
        with pytest.raises(bh.BuildError) as exc:
            bh.fetch(url, str(tmp_path / "x.bin"),
                     allowed_hosts={"sentinel-cogs.s3.us-west-2.amazonaws.com"},
                     log=lambda *a: None)
        assert exc.value.code == "SOURCE_BLOCKED", url


def test_the_satellite_manifest_is_validated_before_anything_is_fetched(tmp_path):
    """A bare manifest["scenes"] index was a KeyError waiting to happen, and
    nothing checked where the imagery came from."""
    sat = _load_script("build_satellite") if _gdal_available() else None
    if sat is None:
        # The builder imports GDAL, which is absent outside the prep container.
        # Its manifest rules are still worth pinning, so read them as source.
        source = _read(f"{REPO}/scripts/map_data/build_satellite.py")
        assert "ALLOWED_HOSTS" in source and "sentinel-cogs" in source
        assert "def load_manifest" in source
        assert "acquisition_date" in source
        assert "/vsicurl/" not in source, (
            "the builder still streams scenes through /vsicurl, which cannot "
            "time out and cannot verify what it read")
        return
    for bad, code in (
        ({}, "BUILD_INCOMPLETE"),
        ({"scenes": []}, "BUILD_INCOMPLETE"),
        ({"scenes": [{}], "acquisition_date": "2026-08-12"}, "BUILD_INCOMPLETE"),
        ({"scenes": [{"visual_href": "https://mt1.google.com/a.tif"}],
          "acquisition_date": "2026-08-12"}, "SOURCE_BLOCKED"),
    ):
        path = tmp_path / "m.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(Exception) as exc:
            sat.load_manifest(str(path))
        assert getattr(exc.value, "code", None) == code, (bad, exc.value)


def _gdal_available():
    import importlib.util
    return importlib.util.find_spec("osgeo") is not None


def _tls_server(routes):
    """A loopback HTTPS server. fetch() insists on https, so the transport
    rules are exercised exactly as they run in production."""
    import http.server
    import ssl
    import subprocess
    import tempfile
    import threading
    import urllib.request

    work = tempfile.mkdtemp(prefix="tls-")
    key, crt = os.path.join(work, "k.pem"), os.path.join(work, "c.pem")
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", crt, "-days", "1", "-subj", "/CN=localhost"],
                       check=True, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"openssl unavailable for the TLS fixture: {exc}")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _serve(self):
            code, body, ctype = routes.get(self.path, (404, b"?", "text/plain"))
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = _serve
        do_HEAD = _serve

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    urllib.request.install_opener(urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=crt))))
    return srv, f"https://localhost:{srv.server_address[1]}", crt


# ---------------------------------------------------------------------------
# Disk safety
# ---------------------------------------------------------------------------

def test_a_build_refuses_before_downloading_when_the_volume_is_too_small(tmp_path):
    """Planetiler's own disk guard was explicitly disabled with force=true on
    the one run that produced the current vector archive. This one refuses
    before the first byte and names the numbers."""
    bh = _load_script("build_helpers")

    free = bh.disk_free(str(tmp_path))
    with pytest.raises(bh.BuildError) as exc:
        bh.disk_preflight(str(tmp_path), free * 2, reserve_gb=0.0, label="oversized build")
    assert exc.value.code == "DISK_SPACE_INSUFFICIENT"
    assert "free" in str(exc.value) and "required" in str(exc.value)
    assert "Nothing has been downloaded" in exc.value.message

    # the reserve is what makes this more than a "will it just barely fit" test
    with pytest.raises(bh.BuildError):
        bh.disk_preflight(str(tmp_path), 1024, reserve_gb=free / 1024 ** 3 + 1)
    bh.disk_preflight(str(tmp_path), 1024, reserve_gb=0.001)     # comfortably fits


def test_no_builder_hard_codes_a_dataset_size():
    """Required space must be computed from what is actually being fetched and
    produced, or the guard is wrong the moment the data changes."""
    for name in ("build_satellite.py", "build_dem_terrarium.py", "install_dataset.py"):
        source = _read(f"{REPO}/scripts/map_data/{name}")
        assert "disk_preflight" in source or "preflight_disk" in source, (
            f"{name} does no disk preflight")
        assert not re.search(r"(?<![\w.])\d{9,}\s*(?:#|$)", source, re.M), (
            f"{name} appears to hard-code a byte size")


def test_out_of_space_is_reported_as_such_not_as_a_stray_oserror():
    """ENOSPC mid-write must map to the registered code, and the partial file
    must be removed rather than left looking like an artifact."""
    for name in ("build_helpers.py",):
        source = _read(f"{REPO}/scripts/map_data/{name}")
        assert "errno.ENOSPC" in source, f"{name} does not detect ENOSPC"
        assert "DISK_SPACE_INSUFFICIENT" in source
    installer = _read(f"{REPO}/scripts/map_data/install_dataset.py")
    assert "errno.ENOSPC" in installer and "DISK_SPACE_INSUFFICIENT" in installer


# ---------------------------------------------------------------------------
# Builder independence
# ---------------------------------------------------------------------------

def test_each_dataset_has_its_own_builder_and_none_waits_for_another():
    """The one real chained run reads:

        2026-08-15T19:53:30Z waiting for satellite build to finish (link serialized)
        2026-08-16T12:00:48Z satellite build finished (exit 1); starting downloads

    16 h 07 m of the streets build waiting on a satellite build that failed.
    A satellite failure must cost you satellite and nothing else.
    """
    builders = {"build_streets_vector.sh": "lebanon-streets-vector",
                "build_satellite.sh": "lebanon-satellite",
                "build_dem.sh": "lebanon-dem"}
    for script, dataset in builders.items():
        path = f"{REPO}/scripts/map_data/{script}"
        assert os.path.isfile(path), f"{script} is missing; that dataset has no committed builder"
        source = _read(path)
        assert dataset in source, f"{script} does not build {dataset}"
        for other in set(builders) - {script}:
            assert other not in source, (
                f"{script} invokes {other}; the builders must be independent")


def test_the_satellite_disk_estimate_is_derived_not_guessed():
    """A guard that blocks correct work is as bad as one that never fires.

    WORKING_SPACE_MULTIPLIER was a guess of 6x the SOURCE bytes. Against the
    real manifest (2.80 GB of COGs) that demanded 16.8 GB, plus a 10 GB
    reserve, and would have REFUSED a build whose actual footprint is about
    0.7 GB — the archive is 15,177 tiles of roughly 24 KB, and streaming means
    the sources are never stored locally at all.

    The estimate must therefore come from the declared bbox and zoom range, so
    it stays right when either changes.
    """
    source = _read(f"{REPO}/scripts/map_data/build_satellite.py")
    assert "WORKING_SPACE_MULTIPLIER" not in source, (
        "the guessed source-size multiplier is back")
    assert "def estimate_output_bytes" in source

    sat = _load_satellite_module()
    estimate = sat.estimate_output_bytes()

    # Derived from the real footprint, not a constant: recomputing the tile
    # capacity of the declared bbox must reproduce it exactly.
    west, south, east, north = sat.BBOX_WGS84
    bh = _load_script("build_helpers")
    tiles = 0
    for z in range(sat.MIN_Z, sat.MAX_Z + 1):
        x0, y0 = bh.lonlat_to_tile(west, north, z)
        x1, y1 = bh.lonlat_to_tile(east, south, z)
        tiles += (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
    assert estimate == tiles * sat.AVG_TILE_BYTES
    assert 100 * 1024 ** 2 < estimate < 1024 ** 3, (
        f"{estimate} bytes is not a plausible archive for {tiles} tiles")

    # The build must not be refused on a volume that can clearly hold it.
    required = int(estimate * sat.WORKING_MULTIPLIER)
    assert required < 2 * 1024 ** 3, "the working multiplier is inflating again"
    bh.disk_preflight(tempfile.gettempdir(), required, reserve_gb=1.0,
                      label="satellite estimate")


def test_the_satellite_stream_is_bounded_in_process_and_in_children():
    """The original hung ~16 hours because nothing bounded it.

    gdal.SetConfigOption is IN-PROCESS only, so its twelve gdalwarp children
    ran with no timeout at all. The mirror mistake is just as fatal: the tiling
    loop reads the mosaic over /vsicurl in-process, so settings applied only to
    children would leave THAT unbounded. One dict, applied both ways.
    """
    source = _read(f"{REPO}/scripts/map_data/build_satellite.py")
    for setting in ("GDAL_HTTP_TIMEOUT", "GDAL_HTTP_CONNECTTIMEOUT",
                    "GDAL_HTTP_LOW_SPEED_TIME", "GDAL_HTTP_LOW_SPEED_LIMIT"):
        assert setting in source, f"{setting} is not configured"
    assert "for _key, _value in GDAL_CHILD_ENV.items():" in source, (
        "the HTTP timeouts are not applied to THIS process; the in-process "
        "/vsicurl reads in the tiling loop would be unbounded")
    assert "env=GDAL_CHILD_ENV" in source, (
        "the HTTP timeouts are not passed to the gdalwarp children")
    assert "timeout=1800" in source, "child processes have no wall-clock bound"

    # And the trap that made a failed build unre-runnable must stay gone.
    # Statements only: the module docstring explains the historical bug and
    # would satisfy a naive substring search for the phrase.
    code = [line for line in source.splitlines()
            if "refusing to overwrite" in line and line.strip().startswith("sys.exit")]
    assert not code, f"the refusing-to-overwrite trap is back: {code}"


def _load_satellite_module():
    """Import build_satellite outside the GDAL container by stubbing osgeo."""
    import importlib.util
    import types
    if "osgeo" not in sys.modules:
        pkg, gdal = types.ModuleType("osgeo"), types.ModuleType("osgeo.gdal")
        gdal.UseExceptions = lambda: None
        gdal.SetConfigOption = lambda *a, **k: None
        pkg.gdal = gdal
        sys.modules["osgeo"], sys.modules["osgeo.gdal"] = pkg, gdal
    spec = importlib.util.spec_from_file_location(
        "_probe_build_satellite", f"{REPO}/scripts/map_data/build_satellite.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_regression_stack_needs_no_development_container():
    """The acceptance run must stand on its own.

    It used to start only redis + api on the dev network as an `external`, so
    every map assertion — availability, served-tile freshness, style/zoom
    coverage — was really measuring the DEVELOPMENT Martin serving the
    DEVELOPMENT map-data, and the run could not start at all when the dev stack
    was down. A regression that validates another stack's data proves nothing
    about the code under test.
    """
    compose = _read(f"{REPO}/docker/docker-compose.regression.yml")

    for service in ("postgres_regression", "redis_regression", "martin_regression",
                    "face_recognition_regression", "nginx_regression"):
        assert f"{service}:" in compose, (
            f"the regression stack does not start its own {service}; it would "
            f"borrow the developer's")

    networks = compose.split("networks:")[-1]
    assert "external: true" not in networks, (
        "the regression stack joins an external network; dev containers are in scope")

    # The production nginx config resolves upstreams by service name, so the
    # aliases are what let it run unmodified inside the private network.
    for alias in ("[postgres]", "[redis]", "[martin]", "[face_recognition]", "[nginx]"):
        assert f"aliases: {alias}" in compose, f"missing network alias {alias}"

    # Fixtures, not the developer's archives.
    assert "../map-data-test:/map-data:ro" in compose
    assert "MAP_DATA_DIR: /app/map-data-test" in compose
    assert "../map-data:" not in compose, (
        "the regression stack mounts the developer's map-data")

    runner = _read(f"{REPO}/scripts/run_regression_isolated.sh")
    assert "make_test_fixtures.py" in runner, "the runner does not build the fixtures"
    assert "no development container or network is involved" in runner, (
        "the runner does not prove that no dev container is part of the run")

    check = _read(f"{REPO}/scripts/regression_isolation_check.py")
    assert "DEV_CONTAINERS" in check and "are reachable from the regression" in check, (
        "the isolation check no longer asserts the dev services are unreachable")


def test_build_all_runs_every_builder_then_fails_only_at_the_end(tmp_path):
    """Requested builders all run, artifacts of the ones that passed are kept,
    and the exit code is 0 only if every requested dataset succeeded."""
    import subprocess

    work = tmp_path / "repo" / "scripts" / "map_data"
    work.mkdir(parents=True)
    shutil.copy(f"{REPO}/scripts/map_data/build_all.sh", str(work))
    for name in ("build_streets_vector", "build_satellite", "build_dem"):
        stub = work / f"{name}.sh"
        stub.write_text(f"#!/usr/bin/env bash\necho building {name}\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)

    def run_all():
        return subprocess.run(["bash", str(work / "build_all.sh")], capture_output=True,
                              text=True, timeout=300)

    ok = run_all()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert ok.stdout.count("PASS") >= 3

    broken = work / "build_satellite.sh"
    broken.write_text("#!/usr/bin/env bash\necho satellite is broken\nexit 1\n", encoding="utf-8")
    broken.chmod(0o755)

    failed = run_all()
    assert failed.returncode != 0, "build_all reported success with a failed dataset"
    assert "=== dem: PASS" in failed.stdout, (
        "the dem builder did not run after the satellite builder failed")
    assert "satellite        FAIL" in failed.stdout
    assert "streets-vector   PASS" in failed.stdout


def glob_mbtiles(prod):
    import glob as _glob
    return _glob.glob(f"{prod}/*.mbtiles")
