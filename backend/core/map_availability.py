"""Which basemap styles are actually usable right now — derived from the
backing datasets, cached, refreshed under supervision.

The dropdown must never offer a style whose data is not installed, and a
requested style whose data is missing must produce a deterministic
OFFLINE_MAP_DATASET_UNAVAILABLE state — never a silent downgrade to Light,
never a CSS-filtered street map pretending to be satellite.

"Available" is proven, not inferred. The gate ladder, in order, first failure
wins:

    style JSON exists                 -> means nothing
    Martin /catalog lists it          -> installed
    TileJSON parses, declares zooms   -> readable, metadata valid
    a representative tile 200s        -> serving
    ...and is not a known placeholder -> cheap content sniff, every refresh
    ...and its CONTENT was measured   -> the ledger; fail closed if it was not
    ...and its fonts/sprite are served-> the style can actually be drawn

The two content rules exist because a 200 proves only that bytes came back. The
transitional raster street archive shipped 145,718 tiles that were all the same
7,412-byte PNG: OpenStreetMap's "Access blocked - App is not following the tile
usage policy" image. Every structural check passed — tile present, byte-identical
to the pyramid, count matches, coverage complete, checksum recorded — because
every one of them measured structure, never content. So the dropdown offered
Light and the operator got a wall of "Access blocked".

The placeholder hash catches that exact image and nothing else, so it is not a
defence against the next one. The defence is the ledger: an archive is usable
only if its content has been decoded and measured, and "never measured" is NOT
usable. See backend/core/map_content_ledger.py.

Style -> dataset dependencies. Light and Dark are the same data with different
paint; they MUST resolve to one archive:

    light      lebanon-streets-vector
    dark       lebanon-streets-vector
    satellite  lebanon-satellite
    terrain    lebanon-dem

The deep check costs a few HTTP round-trips to Martin per dataset, so it is NOT
run on every dropdown request. It runs at start-up, then every
MAP_AVAILABILITY_REFRESH_SECONDS under the service supervisor, and on demand.
`GET /api/maps/availability` returns the cached verdict. The ledger lookup is
one os.stat per dataset — the archive is never re-hashed on a refresh.

Martin is reached at its INTERNAL address only (MAP_MARTIN_INTERNAL_URL);
browsers use /maps/ through nginx.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

from config import settings
from backend.core import map_content_ledger as ledger

logger = logging.getLogger(__name__)

STATE_UNAVAILABLE = "OFFLINE_MAP_DATASET_UNAVAILABLE"

# Style -> ordered list of source IDs that can back it. The FIRST one that is
# usable wins and is reported as `source`. One candidate each today: the raster
# fallback was removed with the archive that motivated it.
STYLE_SOURCES: Dict[str, List[str]] = {
    "light": ["lebanon-streets-vector"],
    "dark": ["lebanon-streets-vector"],
    "satellite": ["lebanon-satellite"],
    "terrain": ["lebanon-dem"],
}

# Fallback probe when a source advertises no bounds: Beirut at z11
# (lon 35.50, lat 33.89 -> x=1226, y=830).
_PROBE_TILE = (11, 1226, 830)

_STYLE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "frontend", "maps", "styles")


def _tile_xy(lon: float, lat: float, zoom: int) -> tuple:
    """WGS84 -> XYZ tile indices (the scheme Martin serves)."""
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def probe_candidates(tilejson: Optional[dict]) -> List[tuple]:
    """Ordered (z, x, y) candidates that should EXIST in this source.

    A single fixed address is not enough: the fixed z11 Beirut tile returned 204
    ("live source, no tile at that address") for the raster street archive, so
    the content check never saw a byte — which is how an archive of 145,718 OSM
    "Access blocked" images kept reporting AVAILABLE. Candidates, in order:

      1. the centre of the source's own advertised bounds at its own minzoom;
      2. the centre of the deployment's operational bbox (MAP_BOUNDS_*) at the
         source's minzoom — for archives that advertise no bounds;
      3. the fixed Beirut z11 tile.

    The first candidate that returns bytes is the one whose content is judged.
    """
    out: List[tuple] = []
    zoom = None
    if tilejson:
        try:
            zoom = int(tilejson.get("minzoom"))
        except (TypeError, ValueError):
            zoom = None
        bounds = tilejson.get("bounds")
        if bounds and len(bounds) == 4 and zoom is not None:
            west, south, east, north = [float(v) for v in bounds]
            out.append((zoom,) + _tile_xy((west + east) / 2.0, (south + north) / 2.0, zoom))
    if zoom is not None:
        lon = (float(settings.MAP_BOUNDS_WEST) + float(settings.MAP_BOUNDS_EAST)) / 2.0
        lat = (float(settings.MAP_BOUNDS_SOUTH) + float(settings.MAP_BOUNDS_NORTH)) / 2.0
        out.append((zoom,) + _tile_xy(lon, lat, zoom))
    out.append(_PROBE_TILE)
    seen, unique = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


# Known upstream placeholder / error images, by sha256 of the exact bytes. A
# dataset serving one of these is serving someone else's error message.
PLACEHOLDER_TILES: Dict[str, str] = {
    "6eabebf6e8f2ff16f9109c808d7e7a0228fed0235a05c074b4a1ef99f964edfd":
        "OpenStreetMap 'Access blocked' tile-usage-policy placeholder (osm.wiki/Blocked)",
}


def placeholder_reason(tile_bytes: bytes) -> Optional[str]:
    """The placeholder this tile IS, or None. Pure — the unit under test."""
    if not tile_bytes:
        return None
    return PLACEHOLDER_TILES.get(hashlib.sha256(tile_bytes).hexdigest())


def source_type_of(tilejson: Optional[dict], content_type: Optional[str]) -> Optional[str]:
    """vector | raster | dem, from what the source says about itself.

    `encoding` is checked before `format` because the DEM declares format=png
    and is emphatically not an image layer — reading format first would label
    the terrain source "raster" and a client could reasonably draw it as one.
    """
    meta = tilejson or {}
    encoding = str(meta.get("encoding") or "").lower()
    if encoding in ("terrarium", "mapbox"):
        return "dem"
    fmt = str(meta.get("format") or "").lower()
    if fmt in ("pbf", "mvt"):
        return "vector"
    if fmt in ("png", "jpg", "jpeg", "webp"):
        return "raster"
    ctype = str(content_type or "").lower()
    if "protobuf" in ctype or "mvt" in ctype:
        return "vector"
    if ctype.startswith("image/"):
        return "raster"
    return None


def style_requirements(style: str, style_dir: Optional[str] = None) -> dict:
    """What drawing this style needs from Martin beyond tiles.

    {"font_stacks": [[name, ...], ...], "sprite": str|None}. A missing glyph
    stack is not cosmetic: MapLibre drops every label that uses it, so an
    Arabic-only stack going missing silently removes Arabic place names while
    the map still looks fine to an English reader.
    """
    path = os.path.join(style_dir or _STYLE_DIR, f"{style}.json")
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return {"font_stacks": [], "sprite": None}
    stacks = []
    for layer in doc.get("layers") or []:
        fonts = (layer.get("layout") or {}).get("text-font")
        if isinstance(fonts, list) and fonts and all(isinstance(f, str) for f in fonts):
            if fonts not in stacks:
                stacks.append(list(fonts))
    return {"font_stacks": stacks, "sprite": doc.get("sprite")}


@dataclass
class SourceState:
    id: str
    in_catalog: bool = False
    tile_ok: bool = False
    content_type: Optional[str] = None
    error: Optional[str] = None
    placeholder: Optional[str] = None      # the upstream error image it serves, if any
    probe_tile: Optional[str] = None       # the z/x/y actually fetched
    source_type: Optional[str] = None      # vector | raster | dem
    metadata_valid: Optional[bool] = None  # TileJSON parsed and declared its zooms
    # Tri-state on purpose. True = content measured and passed. False = measured
    # and rejected. None = NEVER MEASURED, which is not the same as fine.
    content_ok: Optional[bool] = None
    # Whether the fonts/sprite the styles backed by this source need are served.
    resources_ok: Optional[bool] = None
    code: Optional[str] = None             # machine-readable reason for refusal

    @property
    def usable(self) -> bool:
        """Fail closed: every gate must have been evaluated AND passed.

        `content_ok is True` rather than a truthiness test is the whole point —
        None means the archive has never been decoded by anything, and an
        unmeasured archive is exactly what shipped 145,718 error images.
        """
        return (self.in_catalog and self.tile_ok and self.placeholder is None
                and self.content_ok is True and self.resources_ok is True)


def _first_failure(state: SourceState) -> Tuple[str, str]:
    """(code, sentence) for the earliest gate this source fails.

    Ordered like the ladder so the operator is told the FIRST thing to fix, not
    the last thing checked: a dataset that is not installed should say so rather
    than complaining that its content is unverified.
    """
    if not state.in_catalog:
        return ledger.CONTENT_MISSING, state.error or f"{state.id} is not installed"
    if state.metadata_valid is False:
        return ledger.METADATA_INVALID, state.error or f"{state.id} declares no usable zoom range"
    if not state.tile_ok:
        return ledger.PROBE_FAILED, state.error or f"{state.id} did not answer a tile request"
    if state.placeholder is not None:
        return ledger.PLACEHOLDER_CONTENT, state.error or (
            f"{state.id} serves a placeholder image, not map data")
    if state.content_ok is not True:
        return (state.code or ledger.CONTENT_NOT_VERIFIED,
                state.error or ledger.CODE_TEXT[ledger.CONTENT_NOT_VERIFIED])
    if state.resources_ok is not True:
        return ledger.RESOURCES_MISSING, state.error or ledger.CODE_TEXT[ledger.RESOURCES_MISSING]
    return ledger.AVAILABILITY_STATE_INVALID, "no gate failed, yet the source is not usable"


def compose_reason(style: str, states: List[SourceState],
                   martin_reachable: bool = True) -> Tuple[str, str]:
    """Why `style` is unavailable. Pure; one code, one sentence.

    With several candidates the first one's failure is reported, because that
    is the dataset the deployment is expected to have.
    """
    if not martin_reachable:
        return ledger.MARTIN_UNREACHABLE, ledger.CODE_TEXT[ledger.MARTIN_UNREACHABLE]
    if not states:
        return ledger.CONTENT_MISSING, f"no dataset is configured for the {style} style"
    return _first_failure(states[0])


def _invalid_state_metric(style: str) -> None:
    """Count the impossible case. Best-effort: a metrics problem must not
    become a map problem."""
    try:
        from backend.core import metrics as app_metrics
        counter = getattr(app_metrics, "metrics_map_availability_invalid", None)
        if counter is not None:
            counter.labels(style=style).inc()
    except Exception:                                              # noqa: BLE001
        pass


def style_entry(style: str, backing: Optional[str], states: List[SourceState],
                martin_reachable: bool = True) -> dict:
    """The ONLY place a per-style availability entry is built.

    Deriving `reason` here, from `available`, is what makes
    {"available": false, "reason": null} unreachable: there is no code path
    that sets one without the other. This is deliberately NOT an assert —
    `python -O` strips asserts, and a guarantee that evaporates under an
    optimisation flag is not a guarantee. A reason that cannot be composed is
    itself a bug, so it is reported as one and the style stays closed.
    """
    available = backing is not None
    code = text = None
    if not available:
        code, text = compose_reason(style, states, martin_reachable)
        if code not in ledger.REASON_CODES:
            logger.error("[MAP] %s is unavailable but reason composition produced %r, which is "
                         "not a registered code; reporting %s", style, code,
                         ledger.AVAILABILITY_STATE_INVALID)
            _invalid_state_metric(style)
            code = ledger.AVAILABILITY_STATE_INVALID
            text = ledger.CODE_TEXT[ledger.AVAILABILITY_STATE_INVALID]
    return {
        "available": available,
        "source": backing,
        "source_type": next((s.source_type for s in states if s.id == backing), None),
        "state": "AVAILABLE" if available else STATE_UNAVAILABLE,
        "reason": code,
        "reason_text": text,
        "candidates": {
            s.id: {
                "usable": s.usable,
                "error": s.error,                      # unchanged; health.py reads this
                "code": None if s.usable else _first_failure(s)[0],
                "source_type": s.source_type,
                # null = the gate was never reached because an earlier one failed
                "checks": {
                    "installed": s.in_catalog,
                    "readable": s.metadata_valid,
                    "metadata_valid": s.metadata_valid,
                    "content_ok": s.content_ok,
                    "resources_ok": s.resources_ok,
                },
            }
            for s in states
        },
    }


@dataclass
class AvailabilitySnapshot:
    checked_at: float
    martin_reachable: bool
    sources: Dict[str, SourceState] = field(default_factory=dict)
    styles: Dict[str, dict] = field(default_factory=dict)

    def public(self) -> dict:
        """The wire shape: booleans per style, plus what backs each one.

        `styles` stays a flat {name: bool}. identity-map.js does
        `opt.disabled = !ok` over it and the capabilities endpoint filters on
        truthiness; widening it to an object would silently enable every
        disabled option in the picker.
        """
        return {
            "styles": {name: bool(v["available"]) for name, v in self.styles.items()},
            "detail": self.styles,
            "martin_reachable": self.martin_reachable,
            "checked_at": self.checked_at,
            "unavailable_state": STATE_UNAVAILABLE,
        }


_snapshot: Optional[AvailabilitySnapshot] = None
_lock = asyncio.Lock()


def _martin_base() -> str:
    return str(settings.MAP_MARTIN_INTERNAL_URL).rstrip("/")


async def _probe_source(client: httpx.AsyncClient, source_id: str, in_catalog: bool,
                        entries: Optional[Dict[str, dict]] = None) -> SourceState:
    state = SourceState(id=source_id, in_catalog=in_catalog)
    if not in_catalog:
        state.error = "not in Martin catalog"
        state.code = ledger.CONTENT_MISSING
        return state

    tilejson = None
    try:
        meta = await client.get(f"{_martin_base()}/{source_id}")
        if meta.status_code == 200:
            tilejson = meta.json()
    except Exception:                                              # noqa: BLE001
        tilejson = None                                            # fall back to the fixed probe

    # Readable + metadata valid: a source that cannot say which zooms it serves
    # produces a style whose declared range is a guess, and every tile outside
    # it 404s into a blank map that looks like a data problem.
    if tilejson is None:
        state.metadata_valid = False
        state.error = "TileJSON did not parse"
        state.code = ledger.METADATA_INVALID
        return state
    try:
        int(tilejson["minzoom"]), int(tilejson["maxzoom"])
        state.metadata_valid = True
    except (KeyError, TypeError, ValueError):
        state.metadata_valid = False
        state.error = "TileJSON declares no minzoom/maxzoom"
        state.code = ledger.METADATA_INVALID
        return state

    try:
        resp = None
        for z, x, y in probe_candidates(tilejson):
            state.probe_tile = f"{z}/{x}/{y}"
            resp = await client.get(f"{_martin_base()}/{source_id}/{z}/{x}/{y}")
            if resp.status_code == 200 and resp.content:
                break          # bytes to judge; stop at the first real tile
        state.content_type = resp.headers.get("content-type")
        state.source_type = source_type_of(tilejson, state.content_type)
        # 200 = tile bytes; 204 = source has no tile at that exact address,
        # which is still a live, serving source (coverage gaps are legal).
        state.tile_ok = resp.status_code in (200, 204)
        if not state.tile_ok:
            state.error = f"probe tile HTTP {resp.status_code}"
            state.code = ledger.PROBE_FAILED
            return state
        if resp.status_code == 200:
            state.placeholder = placeholder_reason(resp.content)
            if state.placeholder:
                state.error = f"tiles are a placeholder image, not map data: {state.placeholder}"
                state.code = ledger.PLACEHOLDER_CONTENT
                logger.error("[MAP] dataset %s serves a placeholder image (%s) — reporting it "
                             "UNAVAILABLE; rebuild or remove the archive", source_id, state.placeholder)
                return state
    except Exception as exc:                                       # noqa: BLE001
        state.error = f"probe failed: {type(exc).__name__}"
        state.code = ledger.PROBE_FAILED
        return state

    # Content: the recorded verdict for exactly these bytes. One os.stat.
    content_ok, code, message = ledger.verdict_for(source_id, entries=entries)
    state.content_ok = content_ok
    if content_ok is not True:
        state.code = code
        state.error = message
        if content_ok is None:
            logger.warning("[MAP] %s has no usable content verdict (%s) — reporting it "
                           "UNAVAILABLE until it is verified", source_id, code)
        else:
            logger.error("[MAP] %s FAILED content verification (%s): %s",
                         source_id, code, message)
    return state


def _apply_resource_gate(sources: Dict[str, SourceState], catalog: dict,
                         style_dir: Optional[str] = None) -> None:
    """Can the styles backed by each source actually be drawn?

    Evaluated per source rather than per style because `usable` is a property
    of SourceState. Light and Dark declare identical requirements over one
    archive, so the union below is unambiguous; if that ever stops being true
    the parity test in tests/test_maplibre_stack.py fails first.
    """
    served_fonts = set((catalog.get("fonts") or {}).keys())
    served_sprites = set((catalog.get("sprites") or {}).keys())
    for source_id, state in sources.items():
        if state.content_ok is not True:
            continue                       # an earlier gate already decided this
        missing = []
        for style, ids in STYLE_SOURCES.items():
            if source_id not in ids:
                continue
            needs = style_requirements(style, style_dir)
            for stack in needs["font_stacks"]:
                missing.extend(f"font '{f}'" for f in stack if f not in served_fonts)
            sprite = needs["sprite"]
            if sprite and os.path.basename(str(sprite)) not in served_sprites:
                missing.append(f"sprite '{sprite}'")
        state.resources_ok = not missing
        if missing:
            state.code = ledger.RESOURCES_MISSING
            state.error = (f"the tile server does not serve {', '.join(sorted(set(missing)))} — "
                           f"labels using it would silently disappear")
            logger.error("[MAP] %s is installed and valid but %s", source_id, state.error)


async def deep_check() -> AvailabilitySnapshot:
    """The expensive check. Called at start-up, on the refresh loop, and on
    explicit refresh — never per ordinary request."""
    catalog: dict = {}
    catalog_ids: set = set()
    reachable = False
    sources: Dict[str, SourceState] = {}
    entries = ledger.load()

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{_martin_base()}/catalog")
            if resp.status_code == 200:
                reachable = True
                catalog = resp.json()
                catalog_ids = set((catalog.get("tiles") or {}).keys())
        except Exception as exc:                                   # noqa: BLE001
            logger.warning("[MAP_AVAILABILITY] Martin catalog unreachable: %s",
                           type(exc).__name__)

        wanted = {sid for ids in STYLE_SOURCES.values() for sid in ids}
        for sid in sorted(wanted):
            if reachable:
                sources[sid] = await _probe_source(client, sid, sid in catalog_ids, entries)
            else:
                sources[sid] = SourceState(id=sid, error="Martin unreachable",
                                           code=ledger.MARTIN_UNREACHABLE)

    if reachable:
        _apply_resource_gate(sources, catalog)

    styles: Dict[str, dict] = {}
    for style, candidates in STYLE_SOURCES.items():
        states = [sources[sid] for sid in candidates]
        backing = next((s.id for s in states if s.usable), None)
        styles[style] = style_entry(style, backing, states, reachable)

    snap = AvailabilitySnapshot(checked_at=time.time(), martin_reachable=reachable,
                                sources=sources, styles=styles)
    logger.info("[MAP_AVAILABILITY] martin=%s %s", reachable,
                {k: (v["state"] if v["available"] else v["reason"]) for k, v in styles.items()})
    return snap


async def refresh() -> AvailabilitySnapshot:
    global _snapshot
    async with _lock:
        _snapshot = await deep_check()
        return _snapshot


def cached() -> Optional[AvailabilitySnapshot]:
    """The last verdict, or None before the first check completes."""
    return _snapshot


# A snapshot taken while Martin was down is re-checked far sooner than the
# normal refresh cadence: an "unreachable" verdict is exactly the state that
# changes when someone restarts the tile server, and waiting out the full
# interval would show every style as unavailable for minutes afterwards.
NEGATIVE_CACHE_SECONDS = 20.0


async def get_or_refresh() -> AvailabilitySnapshot:
    snap = _snapshot
    if snap is None:
        return await refresh()
    if not snap.martin_reachable and (time.time() - snap.checked_at) > NEGATIVE_CACHE_SECONDS:
        return await refresh()
    return snap


def is_style_available(style: str) -> bool:
    snap = _snapshot
    return bool(snap and snap.styles.get(style, {}).get("available"))


async def verify_and_refresh(*, only_unverified: bool = False, verifier: str = "verify") -> dict:
    """Re-measure installed archives, rewrite the ledger, refresh availability.

    The verification itself decodes tiles and hashes whole files, so it runs in
    a worker thread; holding the event loop for that would stall every request
    on the process.
    """
    results = await asyncio.to_thread(
        ledger.verify_installed, only_unverified=only_unverified, verifier=verifier)
    snap = await refresh()
    return {"verified": results, "availability": snap.public()}


_task: Optional[asyncio.Task] = None
_verify_task: Optional[asyncio.Task] = None


async def start_refresh_loop() -> None:
    """Supervised periodic refresh — same mechanism as every other loop."""
    global _task
    if _task and not _task.done():
        return
    from backend.core.service_supervisor import supervised_loop
    interval = float(settings.MAP_AVAILABILITY_REFRESH_SECONDS)
    _task = asyncio.create_task(
        supervised_loop("map_availability", interval, refresh,
                        error_backoff_base=30.0),
        name="map_availability")
    logger.info("[MAP_AVAILABILITY] refresh loop started (every %ss)", interval)


async def start_boot_verification() -> None:
    """Measure any installed archive that has no usable verdict, in background.

    Fail-closed means an archive nobody has measured is UNAVAILABLE — which
    would take Terrain offline on any deployment whose map-data was copied into
    place rather than installed through install_dataset.py. Rather than trust
    those bytes, measure them: the dataset stays unavailable until the check
    PASSES, and boot is never blocked waiting for it (the DEM takes 0.30 s and
    the vector archive 0.16 s, but a large satellite archive would not).
    """
    global _verify_task
    if _verify_task and not _verify_task.done():
        return

    async def _run():
        try:
            outcome = await verify_and_refresh(only_unverified=True, verifier="boot")
            fresh = {k: v for k, v in outcome["verified"].items() if not v.get("skipped")}
            if fresh:
                logger.info("[MAP_AVAILABILITY] boot verification measured %s", fresh)
        except Exception as exc:                                   # noqa: BLE001
            logger.error("[MAP_AVAILABILITY] boot verification failed (%s: %s); every "
                         "unverified dataset stays UNAVAILABLE",
                         type(exc).__name__, exc)

    _verify_task = asyncio.create_task(_run(), name="map_content_verification")


async def stop_refresh_loop() -> None:
    global _task, _verify_task
    for name in ("_task", "_verify_task"):
        task = globals().get(name)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):            # noqa: BLE001
                pass
            globals()[name] = None
