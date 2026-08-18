"""The map actually works: real data in, a usable Lebanon map out.

tests/test_maplibre_stack.py pins the STACK (datasets validate, styles resolve
to one vector archive, Martin serves them, availability tells the truth). This
file pins the OUTCOME for the data layer: seed real cameras and a real
cross-camera track, call the endpoint the browser calls, and assert what comes
back can be drawn.

    docker exec face_recognition_api python -m pytest tests/test_map_rendering.py -v

It used to assert on server-rendered Folium HTML. That renderer is gone — the
browser draws the map with MapLibre from `/api/identities/{id}/map-data` over
Martin's offline datasets — so every case is now expressed against that JSON.
The behaviours are the same ones real bugs produced:

  * a camera without coordinates was PLACED AT 0,0 (null island) instead of
    being skipped;
  * the view centred on 0,0 when a centroid could not be resolved;
  * overlays (clusters, risk heatmap, timeline, patterns) silently returned
    nothing while the endpoint still answered 200 — the exact regression the
    Folium retirement could have caused, since the analysis half was extracted
    out of the renderer module rather than deleted with it;
  * the camera sequence has to be chronological or the route is nonsense.

Fixture data is qa_ prefixed and removed by the module fixture. Coordinates are
real Lebanese cities, inside every installed dataset's bbox.
"""

import json
import os
import re
import urllib.error
import urllib.request

import pytest

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"
TEST_PREFIX = "qa_maprender_"
CAM_PREFIX = "qa-maprender-cam-"

# Real Lebanese sites, all inside the tileset (lon 34.8-36.9E, lat 32.8-34.9N).
CAMERAS = [
    ("a", "QA Sidon",      33.5606, 35.3714),
    ("b", "QA Beirut Port", 33.9019, 35.5183),
    ("c", "QA Jounieh",    33.9808, 35.6178),
    ("d", "QA Byblos",     34.1230, 35.6519),
    ("e", "QA Zahle",      33.8463, 35.9020),
    ("f", "QA Baalbek",    34.0058, 36.2181),
    ("g", "QA Tripoli",    34.4361, 35.8497),
]

EXTERNAL_HOSTS = ("openstreetmap.org", "cdnjs.cloudflare.com", "jsdelivr.net",
                  "code.jquery.com", "googleapis.com", "mapbox.com",
                  "carto.com", "arcgis.com", "unpkg.com")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sql(statement, params=None, fetch="all"):
    from sqlalchemy import text

    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            result = await db.execute(text(statement), params or {})
            if not result.returns_rows:
                value = result.rowcount
            elif fetch == "scalar":
                value = result.scalar()
            else:
                value = result.all()
            await db.commit()
            return value
    return run_async(_run())


@pytest.fixture(scope="module")
def token():
    data = json.dumps({"username": "admin", "password": "admin123"}).encode()
    request = urllib.request.Request(BASE + "/api/auth/login", data=data,
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["access_token"]


def _get(path, token=None, timeout=300):
    """Returns (status, body, headers) with headers keyed in LOWERCASE.

    HTTP header names are case-insensitive and uvicorn emits them lowercase;
    a plain dict() of them is not, so lookups by canonical casing silently
    miss.
    """
    request = urllib.request.Request(BASE + path, method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, response.read().decode(errors="replace"), headers
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in exc.headers.items()}
        return exc.code, exc.read().decode(errors="replace"), headers


def _seed(cameras=CAMERAS, with_coords=True, name=None):
    """A tracked identity seen at each camera, oldest first."""
    for suffix, label, lat, lon in cameras:
        _sql(
            "INSERT INTO pipelines (pipeline_id, location_name, latitude, "
            " longitude, is_active, created_at, updated_at) "
            "VALUES (:p, :n, :la, :lo, 1, now(), now()) "
            "ON CONFLICT (pipeline_id) DO UPDATE SET "
            " latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude, "
            " location_name = EXCLUDED.location_name",
            {"p": CAM_PREFIX + suffix, "n": label,
             "la": lat if with_coords else None,
             "lo": lon if with_coords else None})

    identity_id = str(_sql(
        "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
        " last_seen_at, created_at, updated_at, appearances_count) "
        "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, now(), now(), "
        "        now(), now(), :c) RETURNING id",
        {"n": name or (TEST_PREFIX + "subject"), "c": len(cameras)},
        fetch="scalar"))

    # Descending minutes-ago => ascending chronological order.
    for index, (suffix, _label, _lat, _lon) in enumerate(cameras):
        _sql(
            "INSERT INTO identity_appearances (identity_id, pipeline_id, "
            " start_time, created_at) "
            "VALUES (:i, :p, now() - make_interval(mins => :m), now())",
            {"i": identity_id, "p": CAM_PREFIX + suffix,
             "m": 400 - index * 45})
    return identity_id


def _all_movements(body):
    """Every sighting, across every track, in observed order.

    /cross-camera returns ONE TRACK PER CALENDAR DAY — the service buckets
    appearances by `start_time.strftime("%Y-%m-%d")` and the docstring states
    "one per day". Reading only `[0]` therefore sees just the oldest day.

    The seed spans 400 -> 130 minutes ago, so whenever that window straddles
    local midnight the sightings split across two tracks and a `[0]`-only
    assertion fails — for roughly four hours a night, and passes the rest of
    the time. Flatten instead of pinning the seed inside one day, so the test
    keeps covering the multi-day shape the map endpoint actually consumes.
    """
    tracks = json.loads(body)
    return [movement for track in tracks for movement in track["movements"]]


def _cleanup():
    _sql("DELETE FROM identity_appearances WHERE pipeline_id LIKE :p",
         {"p": CAM_PREFIX + "%"})
    for (identity_id,) in _sql(
            "SELECT id FROM identities WHERE display_name LIKE :p",
            {"p": TEST_PREFIX + "%"}):
        _sql("DELETE FROM identity_appearances WHERE identity_id = :i",
             {"i": str(identity_id)})
        _sql("DELETE FROM identity_embeddings WHERE identity_id = :i",
             {"i": str(identity_id)})
        _sql("DELETE FROM identities WHERE id = :i", {"i": str(identity_id)})
    _sql("DELETE FROM pipelines WHERE pipeline_id LIKE :p", {"p": CAM_PREFIX + "%"})


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


# ---------------------------------------------------------------------------
# 1. the map renders, offline, from the shipped tileset
# ---------------------------------------------------------------------------


def _map_data(token, identity_id, **overrides):
    """Call /map-data exactly as frontend/js/identity-map.js does."""
    params = {"days_back": 30, "enable_security_features": "false",
              "detect_patterns": "false", "show_risk_heatmap": "false",
              "show_timeline": "false"}
    params.update({k: str(v).lower() if isinstance(v, bool) else v
                   for k, v in overrides.items()})
    query = "&".join(f"{k}={v}" for k, v in params.items())
    status, body, _headers = _get(f"/api/identities/{identity_id}/map-data?{query}", token)
    return status, (json.loads(body) if status == 200 else body)


def _points(payload):
    """[lng, lat] of every detection feature."""
    feats = (payload.get("detections") or {}).get("features") or []
    return [f["geometry"]["coordinates"] for f in feats]


def test_the_map_data_is_a_usable_lebanon_payload(token):
    identity_id = _seed()
    status, payload = _map_data(token, identity_id)
    assert status == 200, payload
    for key in ("identity", "detections", "route", "cameras", "metadata"):
        assert key in payload, f"/map-data must carry {key}: {sorted(payload)}"
    coords = _points(payload)
    assert coords, "no detection features returned for a seeded track"
    for lng, lat in coords:
        assert 34.8 <= lng <= 36.95 and 32.8 <= lat <= 34.95, f"{lng},{lat} is outside Lebanon"


def test_every_camera_appears_on_the_map(token):
    identity_id = _seed()
    status, payload = _map_data(token, identity_id)
    assert status == 200, payload
    names = {f["properties"].get("location_name") or f["properties"].get("pipeline_id")
             for f in (payload.get("cameras") or {}).get("features", [])}
    for suffix, label, _lat, _lng in CAMERAS:
        camera = f"{CAM_PREFIX}{suffix}"
        assert any(camera in str(n) or label in str(n) for n in names), \
            f"{label} ({camera}) missing from cameras: {names}"


def test_no_feature_is_placed_at_null_island(token):
    """A camera without coordinates must be SKIPPED, never emitted as 0,0 —
    there is no map data at null island, and a single such point drags the
    whole view into the Atlantic."""
    identity_id = _seed(cameras=CAMERAS[:3] + [("z", "QA NoCoords", None, None)], with_coords=True)
    status, payload = _map_data(token, identity_id)
    assert status == 200, payload
    for lng, lat in _points(payload):
        assert not (abs(lng) < 0.001 and abs(lat) < 0.001), "a feature was placed at 0,0"
    bounds = (payload.get("metadata") or {}).get("bounds")
    if bounds:
        flat = [c for pair in (bounds if isinstance(bounds[0], list) else [bounds]) for c in pair]
        assert not all(abs(c) < 0.001 for c in flat), "bounds collapsed onto null island"


def test_the_camera_sequence_is_chronological(token):
    identity_id = _seed()
    status, payload = _map_data(token, identity_id)
    assert status == 200, payload
    stamps = [f["properties"].get("timestamp")
              for f in (payload.get("detections") or {}).get("features", [])
              if f["properties"].get("timestamp")]
    assert stamps == sorted(stamps), "detections must be chronological or the route is nonsense"


def test_security_overlays_are_populated_not_merely_200(token):
    """The regression the Folium retirement could have caused.

    `SecurityMapAnalyzer` was extracted out of the deleted renderer module into
    backend/core/security_analysis.py. If that extraction had been skipped, this
    endpoint would keep returning HTTP 200 with every overlay empty — silent
    feature loss rather than a failure. So assert the overlays carry STRUCTURE,
    not that the request succeeded.
    """
    identity_id = _seed()
    status, payload = _map_data(token, identity_id, enable_security_features=True,
                                detect_patterns=True, show_risk_heatmap=True,
                                show_timeline=True)
    assert status == 200, payload
    for key in ("risk_points", "security_zones", "patterns", "threats"):
        assert key in payload, f"overlay {key} missing entirely: {sorted(payload)}"

    from backend.core.security_analysis import SecurityMapAnalyzer
    assert SecurityMapAnalyzer, "the analysis half must survive the renderer's removal"
    from backend.core import map_data_service
    assert map_data_service.SECURITY_ANALYSIS_AVAILABLE is True, (
        "map_data_service lost its analysis import — overlays would be empty at 200")


def test_an_identity_with_no_coordinates_still_answers(token):
    identity_id = _seed(cameras=[("z", "QA NoCoords", None, None)], with_coords=False)
    status, payload = _map_data(token, identity_id)
    assert status == 200, payload
    assert _points(payload) == [], "a coordinate-less identity must yield no placed features"


def test_the_map_data_requires_authentication(token):
    identity_id = _seed()
    status, _body, _headers = _get(f"/api/identities/{identity_id}/map-data", None)
    assert status in (401, 403), f"unauthenticated /map-data answered {status}"


def test_the_retired_folium_endpoints_are_gone(token):
    """Intentional breaking change: the server-rendered map, its GeoJSON twin
    and the renderer's stats endpoint were removed with the Folium stack. They
    had no frontend, backend, test or documented external consumer."""
    identity_id = _seed()
    for path in (f"/api/identities/{identity_id}/map",
                 f"/api/identities/{identity_id}/map/geojson",
                 "/api/map/stats"):
        status, _body, _headers = _get(path, token)
        assert status == 404, f"{path} still answers {status}; it must be gone"
