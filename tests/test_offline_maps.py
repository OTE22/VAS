"""
Offline map guarantees that survive the Folium retirement.

What this file still owns: no frontend or backend map source may reference an
external map host, the pipelines coordinate picker runs on the MapLibre stack
and asks availability rather than hard-coding a style, zero is a valid
coordinate, and the intelligence pages render in-page through ONE controller
without refetching data on a style change.

What it no longer owns: the Folium/Leaflet renderer, its vendored JS/CSS and
the generated-HTML contract (renderer deleted — the browser draws the map from
/api/identities/{id}/map-data over Martin's offline datasets), and the raster
`tiles/` pyramid, which was 145,718 copies of OpenStreetMap's "Access blocked"
placeholder. The MapLibre + Martin contract lives in tests/test_maplibre_stack.py.
"""

import json
import os
import re
import urllib.request
import urllib.error

import pytest

BASE = "http://localhost:8000"

FRONTEND_MAP_SOURCES = [
    "/app/frontend/js/admin-pipelines.js",
    "/app/frontend/js/admin-intelligence.js",
    "/app/frontend/js/admin-security-intelligence.js",
    "/app/frontend/admin/pipelines.html",
    "/app/frontend/admin/intelligence.html",
    "/app/frontend/admin/security-intelligence.html",
]

EXTERNAL_MAP_HOSTS = [
    "openstreetmap.org", "google.com", "googleapis.com", "mapbox.com",
    "carto.com", "arcgis.com", "esri.com", "unpkg.com",
    "cdnjs.cloudflare.com", "jsdelivr.net", "code.jquery.com",
]


def _source(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# 1. the tileset itself
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 2. no external map dependencies in any map source
# ---------------------------------------------------------------------------

def test_no_frontend_map_source_references_an_external_map_host():
    offenders = []
    for path in FRONTEND_MAP_SOURCES:
        source = _source(path)
        for host in EXTERNAL_MAP_HOSTS:
            if host in source:
                offenders.append(f"{os.path.basename(path)}: {host}")
    assert not offenders, f"external map dependencies remain: {offenders}"


def test_the_pipelines_map_uses_the_offline_maplibre_basemap():
    """The coordinate picker was the last direct Leaflet user, hard-coding the
    /tiles/ URL, zooms, bounds and centre (drifting from config). It now uses
    the same MapLibre controller and the same Light style as the intelligence
    maps — ONE map stack — and is still constrained to the dataset footprint so
    panning cannot leave the coverage into blank space."""
    source = _source("/app/frontend/js/admin-pipelines.js")
    assert "window.IdentityMap" in source, "coordinate picker must use the MapLibre bridge"
    # The style comes from AVAILABILITY, never a literal. Hard-coding Light is
    # exactly how this picker ended up painting OSM "Access blocked" tiles with
    # no dropdown and no error path when the Light archive turned out to be
    # placeholder images; the style URL (and its cache version) has one owner.
    assert "firstUsableStyleUrl(" in source, "picker must ask which basemap is really available"
    assert "/frontend/maps/styles/" not in source, "picker must not hard-code a style file"
    assert "styles-" not in source, "picker must not duplicate the style cache version"
    assert "tile.openstreetmap.org" not in source
    assert "L.tileLayer(" not in source and "L.map(" not in source, "Leaflet must be gone"
    assert "maxBounds" in source, "map must be constrained to tile coverage"


def test_zero_is_a_valid_coordinate_in_the_pipelines_map():
    """The falsy-zero pattern the security-intelligence tests already ban."""
    source = _source("/app/frontend/js/admin-pipelines.js")
    assert "pipeline?.latitude || " not in source, (
        "a stored latitude of exactly 0 is erased by || fallback")
    assert "(lat && lng)" not in source, (
        "0 && anything is false — equator/meridian treated as unset")
    assert "Number.isFinite(lat)" in source


# ---------------------------------------------------------------------------
# 3. folium's own assets are vendored, and the iframe can actually execute
# ---------------------------------------------------------------------------

def test_the_maps_render_in_page_through_one_controller_with_no_refetch_on_style_change():
    """Both intelligence pages draw with the shared MapLibre controller, in the
    page, from /map-data. Two properties carried over from the iframe era and
    re-expressed for the new mechanics:

      * ONE data request per change of identity/server-params: the client
        keys the fetch on identityId + params and re-uses the payload for
        style switches and display-flag toggles (the old design refetched
        the whole map on every checkbox).
      * NO iframe, NO srcdoc, NO buildMapUrl.
    """
    for path in ("/app/frontend/js/admin-intelligence.js",
                 "/app/frontend/js/admin-security-intelligence.js"):
        source = _source(path)
        code = "\n".join(l for l in source.splitlines() if not l.strip().startswith(("//", "*", "/*")))
        assert "createElement('iframe')" not in code, f"{path} still uses an iframe"
        assert "iframe.srcdoc" not in code and "buildMapUrl(" not in code
        assert "window.IdentityMap.ready" in code, f"{path} must use the MapLibre controller"
        assert "state.mapDataKey !== dataKey" in code, (
            f"{path}: data must be refetched only when identity/params change, "
            f"never for a style switch")
        assert "ctl.setBasemap(" in code, f"{path}: style switching must go through setBasemap()"


# ---------------------------------------------------------------------------
# 4. deployment wiring
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 5. the generated map itself is offline
# ---------------------------------------------------------------------------

