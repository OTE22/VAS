"""Structured map data for the MapLibre intelligence map.

Everything the map shows, as GeoJSON — no HTML, no renderer. The browser
(frontend/js/identity-map.js) draws these with MapLibre GL JS over the
offline basemap that Martin serves; this module never touches a map library.

Authorization is the caller's: routes gate on `require_admin()` and load the
tracks/watchlists/zones for the requested identity, then hand the already
authorized inputs here. This module derives, it does not fetch — so nothing
it returns can exceed what the route was allowed to load.

The analysis (patterns, risk score) is `SecurityMapAnalyzer` — pure data,
shared with the previous renderer — so the numbers a user sees are the same
ones the Folium map showed. Only the drawing moved to the client.

Contract (all keys always present; empty collections when a feature is off):

    identity          {id, name, risk_badge?}
    detections        FeatureCollection<Point>  one per movement, ordered by time
    route             FeatureCollection<LineString> chronological, per-segment props
    cameras           FeatureCollection<Point>  one per distinct camera
    risk_points       [[lng, lat, weight], ...] for the heatmap layer
    security_zones    FeatureCollection<Polygon>
    patterns          FeatureCollection<Point|LineString> per detected pattern
    threats           FeatureCollection<Point>
    metadata          {bounds, first_seen, last_seen, counts, features_enabled, warnings}

GeoJSON positions are [lng, lat] (RFC 7946). The tracks pipeline stores
coordinates as {"lat", "lng"}; the flip happens exactly once, here.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# The same ceilings the renderer used, so the two never disagreed while both
# existed. MAP_MAX_COORDINATES bounds memory on pathological tracks.
from config import settings  # noqa: E402

MAX_POINTS = int(settings.MAP_MAX_COORDINATES)

def validate_coordinates(lat: float, lng: float) -> bool:
    """WGS-84 range check. Defined here, not imported from map_service — that
    module is the Folium renderer and is being retired; this one must stand
    without it."""
    return -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0


try:
    from backend.core.security_analysis import SecurityMapAnalyzer, SecurityZone
    SECURITY_ANALYSIS_AVAILABLE = True
except Exception as _exc:                                      # noqa: BLE001
    SECURITY_ANALYSIS_AVAILABLE = False
    logger.warning("[MAP_DATA] security analysis unavailable: %s", _exc)


def _feature(geometry: Dict[str, Any], properties: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def _fc(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def _coords(movement: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """(lat, lng) from a movement, or None if absent/invalid.

    A camera without coordinates is SKIPPED, never placed at (0, 0):
    null island is a real place in the Gulf of Guinea, and a pin there reads
    as a person tracked to the middle of the ocean.
    """
    coords = movement.get("coordinates")
    if not isinstance(coords, dict):
        return None
    try:
        lat, lng = float(coords.get("lat")), float(coords.get("lng"))
    except (TypeError, ValueError):
        return None
    if not validate_coordinates(lat, lng):
        return None
    return lat, lng


def _parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(value: Any) -> Optional[str]:
    dt = _parse_ts(value)
    return dt.isoformat() if dt else None


def build_map_data(
    *,
    identity_id: str,
    tracks: List[Dict[str, Any]],
    watchlist_matches: Optional[List[Dict[str, Any]]] = None,
    security_zones: Optional[List[Dict[str, Any]]] = None,
    include_routes: bool = True,
    detect_patterns: bool = False,
    include_risk: bool = False,
    include_security: bool = False,
) -> Dict[str, Any]:
    """Assemble the map-data payload from already-authorized inputs."""
    warnings: List[str] = []

    # ---- flatten movements, chronological, bounded ------------------------
    movements: List[Dict[str, Any]] = []
    for track in tracks or []:
        if isinstance(track, dict):
            for m in track.get("movements", []) or []:
                if isinstance(m, dict):
                    movements.append(m)
    movements.sort(key=lambda m: _parse_ts(m.get("timestamp")) or datetime.min)
    if len(movements) > MAX_POINTS:
        warnings.append(f"movements truncated from {len(movements)} to {MAX_POINTS}")
        movements = movements[:MAX_POINTS]

    identity_name = None
    for track in tracks or []:
        if isinstance(track, dict) and track.get("display_name"):
            identity_name = str(track["display_name"])
            break

    # ---- detections + cameras + route ------------------------------------
    detections: List[Dict[str, Any]] = []
    cameras: Dict[str, Dict[str, Any]] = {}
    positions: List[List[float]] = []          # [lng, lat] for the route
    seg_props: List[Dict[str, Any]] = []
    lats: List[float] = []
    lngs: List[float] = []
    skipped = 0
    prev_pname: Optional[str] = None

    for seq, m in enumerate(movements):
        ll = _coords(m)
        if ll is None:
            skipped += 1
            continue
        lat, lng = ll
        lats.append(lat); lngs.append(lng)
        pid = str(m.get("pipeline_id") or "")
        pname = str(m.get("pipeline_name") or pid or "camera")
        detections.append(_feature(
            {"type": "Point", "coordinates": [lng, lat]},
            {
                "seq": seq,
                "pipeline_id": pid,
                "pipeline_name": pname,
                "timestamp": _iso(m.get("timestamp")),
                "duration_at_location": m.get("duration_at_location"),
                "snapshot_url": m.get("snapshot_url"),
            },
        ))
        if pid and pid not in cameras:
            cameras[pid] = _feature(
                {"type": "Point", "coordinates": [lng, lat]},
                {"pipeline_id": pid, "pipeline_name": pname, "visits": 0},
            )
        if pid:
            cameras[pid]["properties"]["visits"] += 1
        if include_routes:
            if positions:
                seg_props.append({
                    "from_seq": seq - 1, "to_seq": seq,
                    "from_pipeline": prev_pname,
                    "to_pipeline": pname,
                    "timestamp": _iso(m.get("timestamp")),
                })
            positions.append([lng, lat])
            prev_pname = pname
    if skipped:
        warnings.append(f"{skipped} detection(s) had no coordinates and were skipped")

    route_features: List[Dict[str, Any]] = []
    if include_routes and len(positions) > 1:
        route_features.append(_feature(
            {"type": "LineString", "coordinates": positions},
            {"identity_id": identity_id, "points": len(positions),
             "segments": seg_props,
             "first_seen": detections[0]["properties"]["timestamp"] if detections else None,
             "last_seen": detections[-1]["properties"]["timestamp"] if detections else None},
        ))

    # ---- security analysis (pure data) -----------------------------------
    zones_fc: List[Dict[str, Any]] = []
    zone_objs: List[Any] = []
    patterns_fc: List[Dict[str, Any]] = []
    threats_fc: List[Dict[str, Any]] = []
    risk_points: List[List[float]] = []
    risk_summary: Optional[Dict[str, Any]] = None

    if include_security and security_zones:
        for z in security_zones:
            if not isinstance(z, dict):
                continue
            ring = []
            for c in z.get("coordinates") or []:
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    try:
                        zlat, zlng = float(c[0]), float(c[1])
                    except (TypeError, ValueError):
                        continue
                    if validate_coordinates(zlat, zlng):
                        ring.append([zlng, zlat])
            if len(ring) < 3:
                continue
            if ring[0] != ring[-1]:
                ring.append(ring[0])              # GeoJSON polygons are closed
            props = {
                "name": str(z.get("name", "Zone"))[:100],
                "zone_type": str(z.get("zone_type", "monitored"))[:50],
                "risk_level": min(10, max(1, int(z.get("risk_level", 5) or 5))),
                "description": (str(z.get("description"))[:500] if z.get("description") else None),
            }
            zones_fc.append(_feature({"type": "Polygon", "coordinates": [ring]}, props))
            if SECURITY_ANALYSIS_AVAILABLE:
                try:
                    zone_objs.append(SecurityZone(
                        name=props["name"],
                        coordinates=[[p[1], p[0]] for p in ring[:-1]],
                        zone_type=props["zone_type"], risk_level=props["risk_level"],
                        description=props["description"]))
                except Exception:                                  # noqa: BLE001
                    pass

    detected_patterns: List[Any] = []
    if detect_patterns and movements and SECURITY_ANALYSIS_AVAILABLE:
        for name, fn in (("loitering", SecurityMapAnalyzer.detect_loitering),
                         ("backtracking", SecurityMapAnalyzer.detect_backtracking),
                         ("rapid_movement", SecurityMapAnalyzer.detect_rapid_movement)):
            try:
                found = fn(movements) or []
                detected_patterns.extend(found)
            except Exception as exc:                               # noqa: BLE001
                warnings.append(f"{name} detection failed: {type(exc).__name__}")
        for p in detected_patterns:
            locs = [(float(a), float(b)) for a, b in (getattr(p, "locations", None) or [])
                    if validate_coordinates(float(a), float(b))]
            if not locs:
                continue
            props = {
                "pattern_type": getattr(p, "pattern_type", "unknown"),
                "severity": int(getattr(p, "severity", 5) or 5),
                "description": str(getattr(p, "description", ""))[:300],
                "start_time": _iso(getattr(p, "start_time", None)),
                "end_time": _iso(getattr(p, "end_time", None)),
            }
            if len(locs) == 1 or props["pattern_type"] == "loitering":
                lat, lng = locs[0]
                patterns_fc.append(_feature({"type": "Point", "coordinates": [lng, lat]}, props))
            else:
                patterns_fc.append(_feature(
                    {"type": "LineString", "coordinates": [[lng, lat] for lat, lng in locs]}, props))

    if include_security and watchlist_matches:
        level_map = {"critical": 9, "high": 7, "medium": 5, "low": 3}
        for match in watchlist_matches:
            if not isinstance(match, dict):
                continue
            level = level_map.get(str(match.get("alert_level", "low")).lower(), 3)
            list_name = str(match.get("list_name", "Unknown"))[:100]
            for m in movements[:100]:
                ll = _coords(m)
                if ll is None:
                    continue
                lat, lng = ll
                threats_fc.append(_feature(
                    {"type": "Point", "coordinates": [lng, lat]},
                    {"threat_level": level, "threat_type": "watchlist",
                     "list_name": list_name, "timestamp": _iso(m.get("timestamp"))}))

    if include_risk and movements and SECURITY_ANALYSIS_AVAILABLE:
        try:
            score = SecurityMapAnalyzer.calculate_risk_score(
                movements, watchlist_matches, detected_patterns, zone_objs or None)
        except Exception as exc:                                   # noqa: BLE001
            score = None
            warnings.append(f"risk scoring failed: {type(exc).__name__}")
        if score is not None:
            # total_risk is on the unified risk engine's 0-100 scale (see
            # SecurityMapAnalyzer.calculate_risk_score). The heatmap weight is
            # that as a 0-1 fraction with a floor, so a low-risk track still
            # renders a faint heat rather than nothing at all - the layer
            # exists to show WHERE, the number says HOW MUCH.
            total = float(score.get("total_risk", 0) or 0)
            weight = max(0.15, min(1.0, total / 100.0))
            for m in movements:
                ll = _coords(m)
                if ll is not None:
                    risk_points.append([ll[1], ll[0], round(weight, 3)])
            risk_summary = {
                "total_risk": total,
                "risk_level": score.get("risk_level"),
                "severity": score.get("severity"),
                "factors": score.get("risk_factors"),
            }

    # ---- metadata ---------------------------------------------------------
    bounds = None
    if lats:
        bounds = [[min(lngs), min(lats)], [max(lngs), max(lats)]]  # [[w,s],[e,n]]

    return {
        "identity": {"id": identity_id, "name": identity_name,
                     "risk": risk_summary},
        "detections": _fc(detections),
        "route": _fc(route_features),
        "cameras": _fc(list(cameras.values())),
        "risk_points": risk_points,
        "security_zones": _fc(zones_fc),
        "patterns": _fc(patterns_fc),
        "threats": _fc(threats_fc),
        "metadata": {
            "bounds": bounds,
            "first_seen": detections[0]["properties"]["timestamp"] if detections else None,
            "last_seen": detections[-1]["properties"]["timestamp"] if detections else None,
            "counts": {"detections": len(detections), "cameras": len(cameras),
                       "route_points": len(positions), "zones": len(zones_fc),
                       "patterns": len(patterns_fc), "threats": len(threats_fc),
                       "risk_points": len(risk_points)},
            "features_enabled": {"routes": include_routes, "patterns": detect_patterns,
                                 "risk": include_risk, "security": include_security},
            "warnings": warnings,
        },
    }
