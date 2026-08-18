"""
Security intelligence ANALYSIS — geometry and behaviour, no rendering.

Zones, threat indicators, movement patterns (loitering, backtracking, rapid
movement), risk scoring and activity heatmap aggregation. Everything here is
pure computation over coordinates and timestamps; it produces numbers and
GeoJSON-ready structures, never a map.

Extracted from the retired `security_map_features` module when the Folium /
Leaflet renderer was removed. The rendering half went; this half is what the
live MapLibre map actually runs on — `backend/core/map_data_service.py` builds
every overlay from it, so deleting it alongside the renderer would have left
`/api/identities/{id}/map-data` returning HTTP 200 with empty overlays: the
silent feature loss this migration exists to remove.

Consumers: backend/core/map_data_service.py · tests/test_intel_algorithms.py ·
tests/test_risk_platform.py.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SecurityZone:
    """Security zone definition"""
    name: str
    coordinates: List[List[float]]  # [[lat, lng], ...]
    zone_type: str  # 'restricted', 'high_security', 'monitored', 'safe'
    risk_level: int  # 1-10
    description: Optional[str] = None


@dataclass
class ThreatIndicator:
    """Threat indicator for a location"""
    lat: float
    lng: float
    threat_level: int  # 1-10
    threat_type: str  # 'suspicious', 'alert', 'watchlist', 'pattern'
    description: str
    timestamp: Optional[datetime] = None


@dataclass
class MovementPattern:
    """Detected movement pattern"""
    pattern_type: str  # 'loitering', 'backtracking', 'rapid_movement', 'unusual_route'
    locations: List[Tuple[float, float]]
    severity: int  # 1-10
    description: str
    start_time: datetime
    end_time: datetime


class SecurityMapAnalyzer:
    """Analyzer for security intelligence patterns"""
    
    @staticmethod
    def calculate_speed(lat1: float, lng1: float, time1: datetime,
                       lat2: float, lng2: float, time2: datetime) -> float:
        """
        Calculate speed in km/h between two points.
        
        Args:
            lat1, lng1: First point coordinates
            time1: First point timestamp
            lat2, lng2: Second point coordinates
            time2: Second point timestamp
            
        Returns:
            Speed in km/h, or 0.0 if invalid
        """
        try:
            # Validate inputs
            if not all(isinstance(x, (int, float)) and math.isfinite(x) 
                      for x in [lat1, lng1, lat2, lng2]):
                return 0.0
            
            if not isinstance(time1, datetime) or not isinstance(time2, datetime):
                return 0.0
            
            if time1 >= time2:
                return 0.0
            
            # Validate coordinate ranges
            if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90 and
                   -180 <= lng1 <= 180 and -180 <= lng2 <= 180):
                return 0.0
            
            # Haversine formula for distance
            R = 6371  # Earth radius in km
            dlat = math.radians(lat2 - lat1)
            dlng = math.radians(lng2 - lng1)
            a = (math.sin(dlat / 2) ** 2 +
                 math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
                 math.sin(dlng / 2) ** 2)
            
            # Prevent domain error in asin
            a = min(1.0, max(0.0, a))
            c = 2 * math.asin(math.sqrt(a))
            distance_km = R * c
            
            time_diff_hours = (time2 - time1).total_seconds() / 3600
            if time_diff_hours <= 0:
                return 0.0
            
            speed = distance_km / time_diff_hours
            
            # Sanity check: reasonable speed limit (1000 km/h)
            if speed > 1000:
                logger.warning(f"[SECURITY] Unrealistic speed calculated: {speed} km/h")
                return 0.0
            
            return speed
            
        except Exception as e:
            logger.warning(f"[SECURITY] Error calculating speed: {e}")
            return 0.0
    
    @staticmethod
    def detect_loitering(movements: List[Dict],
                        radius_meters: float = 50,
                        min_duration_minutes: int = 5,
                        max_gap_minutes: int = 30) -> List[MovementPattern]:
        """
        Detect loitering behavior (staying in small area for extended time).

        Walks the time-ordered points: each anchor point opens a cluster that
        absorbs consecutive points within `radius_meters` AND within
        `max_gap_minutes` of the previous cluster point; a cluster whose
        span reaches `min_duration_minutes` is emitted, and the walk resumes
        at the first point OUTSIDE the cluster — so every dwell spot in the
        track is reported, not just one.

        The gap guard matters because movement coordinates are the CAMERA's
        fixed lat/lng and tracks span days: without it, 09:00 today and 08:55
        tomorrow at the same lobby camera read as one continuous 1435-minute
        max-severity dwell.

        (The previous implementation's while-loop never advanced its index
        past a valid point — it busy-spun to an iteration cap — and its
        cluster/emit logic sat dedented OUTSIDE the loop, so it ran once on
        loop leftovers and could NameError when no point parsed.)

        Args:
            movements: List of movement dictionaries
            radius_meters: Radius in meters to consider loitering
            min_duration_minutes: Minimum duration in minutes

        Returns:
            List of detected loitering patterns
        """
        patterns = []

        # Validate inputs
        if not isinstance(movements, list) or len(movements) < 2:
            return patterns

        # Validate parameters
        radius_meters = max(10, min(1000, float(radius_meters)))  # Clamp between 10-1000m
        min_duration_minutes = max(1, min(60, int(min_duration_minutes)))  # Clamp between 1-60min
        max_gap_minutes = max(1, min(720, int(max_gap_minutes)))

        # Parse and validate once, up front — invalid rows are dropped here
        # instead of being interleaved with the cluster walk.
        points = []
        for idx, movement in enumerate(movements):
            if not isinstance(movement, dict):
                continue
            coords = movement.get('coordinates')
            if not isinstance(coords, dict):
                continue
            try:
                lat = float(coords.get('lat', 0))
                lng = float(coords.get('lng', 0))
                if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                    continue
                timestamp_str = movement.get('timestamp', '')
                if not timestamp_str:
                    continue
                point_time = datetime.fromisoformat(str(timestamp_str).replace('Z', '+00:00'))
            except (ValueError, TypeError, KeyError) as e:
                logger.debug(f"[SECURITY] Error parsing movement {idx}: {e}")
                continue
            points.append((lat, lng, point_time))

        if len(points) < 2:
            return patterns

        i = 0
        while i < len(points):
            anchor_lat, anchor_lng, anchor_time = points[i]
            cluster = [(anchor_lat, anchor_lng)]
            prev_time = anchor_time
            j = i + 1
            while j < len(points):
                lat, lng, point_time = points[j]
                gap_minutes = (point_time - prev_time).total_seconds() / 60
                if gap_minutes > max_gap_minutes:
                    break  # presence was interrupted — this is a NEW visit
                distance_km = SecurityMapAnalyzer._haversine_distance(
                    anchor_lat, anchor_lng, lat, lng
                )
                if distance_km <= radius_meters / 1000:  # Convert to km
                    cluster.append((lat, lng))
                    prev_time = point_time
                    j += 1
                else:
                    break

            if len(cluster) > 1:
                end_time = points[j - 1][2]
                duration_minutes = (end_time - anchor_time).total_seconds() / 60

                if duration_minutes >= min_duration_minutes:
                    severity = min(10, max(1, int(duration_minutes / 5)))  # 1 point per 5 minutes
                    patterns.append(MovementPattern(
                        pattern_type='loitering',
                        locations=cluster,
                        severity=severity,
                        description=f"Loitering detected for {int(duration_minutes)} minutes",
                        start_time=anchor_time,
                        end_time=end_time
                    ))

            # Resume at the first point outside the cluster (always advances).
            i = j if j > i + 1 else i + 1

        return patterns
    
    @staticmethod
    def detect_backtracking(movements: List[Dict], 
                           threshold_ratio: float = 0.5) -> List[MovementPattern]:
        """
        Detect backtracking (returning to previous locations).
        
        Args:
            movements: List of movement dictionaries
            threshold_ratio: Unused parameter (kept for API compatibility)
            
        Returns:
            List of detected backtracking patterns
        """
        patterns = []
        
        try:
            if not isinstance(movements, list):
                return patterns
            
            if len(movements) < 3:
                return patterns
            
            visited_locations = []
            max_locations = 1000  # Prevent memory issues
            
            for i, movement in enumerate(movements):
                if not isinstance(movement, dict):
                    continue
                
                if not movement.get('coordinates'):
                    continue
                
                coords = movement.get('coordinates')
                if not isinstance(coords, dict):
                    continue
                
                try:
                    lat = float(coords.get('lat', 0))
                    lng = float(coords.get('lng', 0))
                    
                    # Validate coordinates
                    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                        continue
                    
                    # Emit at most ONE pattern per revisit, scanning prior
                    # visits newest-first: an immediately-preceding sighting
                    # at the same spot means we never left (no backtracking);
                    # a 2-stop loop is too recent to call a return, so older
                    # visits are consulted; the first QUALIFYING match (gap
                    # > 2 with parseable timestamps) emits and stops. (The
                    # original loop emitted once per prior match — N-1 rows
                    # on the Nth visit; a naive dedup that breaks on ANY
                    # nearby match silences ping-pong "casing" tracks whose
                    # most recent same-spot visit is only 2 stops back.)
                    for prev_lat, prev_lng, prev_idx in reversed(visited_locations):
                        distance = SecurityMapAnalyzer._haversine_distance(lat, lng, prev_lat, prev_lng)
                        if distance >= 0.1:  # different spot — keep scanning
                            continue
                        gap = i - prev_idx
                        if gap <= 1:
                            break  # consecutive sighting — still at the spot
                        if gap <= 2:
                            continue  # short loop; judge against older visits
                        try:
                            prev_timestamp = movements[prev_idx].get('timestamp', '')
                            curr_timestamp = movement.get('timestamp', '')
                            if prev_timestamp and curr_timestamp:
                                start_time = datetime.fromisoformat(prev_timestamp.replace('Z', '+00:00'))
                                end_time = datetime.fromisoformat(curr_timestamp.replace('Z', '+00:00'))

                                severity = min(10, gap // 2)
                                patterns.append(MovementPattern(
                                    pattern_type='backtracking',
                                    locations=[(prev_lat, prev_lng), (lat, lng)],
                                    severity=severity,
                                    description=f"Backtracking detected: returned to location visited {gap} stops earlier",
                                    start_time=start_time,
                                    end_time=end_time
                                ))
                                break  # one emission per revisited spot
                        except (ValueError, KeyError, TypeError) as e:
                            logger.debug(f"[SECURITY] Error parsing timestamps for backtracking: {e}")
                        # unparseable/missing timestamps: consult older visits
                    
                    # Limit visited locations to prevent memory issues
                    if len(visited_locations) < max_locations:
                        visited_locations.append((lat, lng, i))
                    else:
                        # Remove oldest entries
                        visited_locations = visited_locations[-max_locations//2:]
                        visited_locations.append((lat, lng, i))
                        
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug(f"[SECURITY] Error processing movement {i}: {e}")
                    continue
            
            return patterns
            
        except Exception as e:
            logger.error(f"[SECURITY] Error detecting backtracking: {e}", exc_info=True)
            return patterns
    
    @staticmethod
    def detect_rapid_movement(movements: List[Dict],
                            speed_threshold_kmh: float = 100) -> List[MovementPattern]:
        """
        Detect rapid movement (suspicious speed).
        
        Args:
            movements: List of movement dictionaries
            speed_threshold_kmh: Speed threshold in km/h (default: 100)
            
        Returns:
            List of detected rapid movement patterns
        """
        patterns = []
        
        try:
            if not isinstance(movements, list):
                return patterns
            
            if len(movements) < 2:
                return patterns
            
            # Validate and clamp threshold
            speed_threshold_kmh = max(10, min(500, float(speed_threshold_kmh)))
            
            for i in range(len(movements) - 1):
                try:
                    curr = movements[i]
                    next_mov = movements[i + 1]
                    
                    if not isinstance(curr, dict) or not isinstance(next_mov, dict):
                        continue
                    
                    if not curr.get('coordinates') or not next_mov.get('coordinates'):
                        continue
                    
                    curr_coords = curr.get('coordinates')
                    next_coords = next_mov.get('coordinates')
                    
                    if not isinstance(curr_coords, dict) or not isinstance(next_coords, dict):
                        continue
                    
                    try:
                        curr_lat = float(curr_coords.get('lat', 0))
                        curr_lng = float(curr_coords.get('lng', 0))
                        next_lat = float(next_coords.get('lat', 0))
                        next_lng = float(next_coords.get('lng', 0))
                        
                        # Validate coordinates
                        if not all(-90 <= x <= 90 for x in [curr_lat, next_lat]):
                            continue
                        if not all(-180 <= x <= 180 for x in [curr_lng, next_lng]):
                            continue
                        
                        curr_timestamp = curr.get('timestamp', '')
                        next_timestamp = next_mov.get('timestamp', '')
                        
                        if not curr_timestamp or not next_timestamp:
                            continue
                        
                        curr_time = datetime.fromisoformat(curr_timestamp.replace('Z', '+00:00'))
                        next_time = datetime.fromisoformat(next_timestamp.replace('Z', '+00:00'))
                        
                        speed = SecurityMapAnalyzer.calculate_speed(
                            curr_lat, curr_lng, curr_time,
                            next_lat, next_lng, next_time
                        )
                        
                        if speed > speed_threshold_kmh:
                            severity = min(10, max(1, int((speed - speed_threshold_kmh) / 20)))
                            patterns.append(MovementPattern(
                                pattern_type='rapid_movement',
                                locations=[
                                    (curr_lat, curr_lng),
                                    (next_lat, next_lng)
                                ],
                                severity=severity,
                                description=f"Rapid movement detected: {speed:.1f} km/h",
                                start_time=curr_time,
                                end_time=next_time
                            ))
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug(f"[SECURITY] Error processing rapid movement at index {i}: {e}")
                        continue
                        
                except (IndexError, KeyError) as e:
                    logger.debug(f"[SECURITY] Error accessing movement at index {i}: {e}")
                    continue
        
        except Exception as e:
            logger.error(f"[SECURITY] Error detecting rapid movement: {e}", exc_info=True)
            return patterns
        
        return patterns
    
    @staticmethod
    def _haversine_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """
        Calculate distance in km using Haversine formula.
        
        Args:
            lat1, lng1: First point coordinates
            lat2, lng2: Second point coordinates
            
        Returns:
            Distance in kilometers, or 0.0 if invalid
        """
        try:
            # Validate inputs
            if not all(isinstance(x, (int, float)) and math.isfinite(x) 
                      for x in [lat1, lng1, lat2, lng2]):
                return 0.0
            
            # Validate coordinate ranges
            if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90 and
                   -180 <= lng1 <= 180 and -180 <= lng2 <= 180):
                return 0.0
            
            R = 6371  # Earth radius in km
            dlat = math.radians(lat2 - lat1)
            dlng = math.radians(lng2 - lng1)
            a = (math.sin(dlat / 2) ** 2 +
                 math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
                 math.sin(dlng / 2) ** 2)
            
            # Prevent domain error
            a = min(1.0, max(0.0, a))
            c = 2 * math.asin(math.sqrt(a))
            return R * c
            
        except Exception as e:
            logger.warning(f"[SECURITY] Error calculating distance: {e}")
            return 0.0
    
    @staticmethod
    def calculate_risk_score(movements: List[Dict],
                           watchlist_matches: Optional[List[Dict]] = None,
                           patterns: Optional[List[MovementPattern]] = None,
                           zones: Optional[List[SecurityZone]] = None) -> Dict[str, Any]:
        """
        Calculate overall risk score for tracking data.
        
        Args:
            movements: List of movement dictionaries
            watchlist_matches: Optional list of watchlist matches
            patterns: Optional list of detected patterns
            zones: Optional list of security zones
            
        Returns:
            Dictionary with risk score and breakdown
        """
        try:
            risk_factors = {
                'base_risk': 1,
                'watchlist_risk': 0,
                'pattern_risk': 0,
                'zone_risk': 0,
                'speed_risk': 0
            }
            
            # Validate inputs
            if not isinstance(movements, list):
                movements = []
            
            if watchlist_matches and not isinstance(watchlist_matches, list):
                watchlist_matches = None
            
            if patterns and not isinstance(patterns, list):
                patterns = None
            
            if zones and not isinstance(zones, list):
                zones = None
        
            # Watchlist risk
            if watchlist_matches:
                for match in watchlist_matches:
                    if not isinstance(match, dict):
                        continue
                    
                    try:
                        alert_level = str(match.get('alert_level', 'low')).lower()
                        if alert_level == 'critical':
                            risk_factors['watchlist_risk'] += 5
                        elif alert_level == 'high':
                            risk_factors['watchlist_risk'] += 3
                        elif alert_level == 'medium':
                            risk_factors['watchlist_risk'] += 2
                        else:
                            risk_factors['watchlist_risk'] += 1
                        
                        # Cap watchlist risk to prevent overflow
                        risk_factors['watchlist_risk'] = min(50, risk_factors['watchlist_risk'])
                    except Exception as e:
                        logger.debug(f"[SECURITY] Error processing watchlist match: {e}")
                        continue
        
            # Pattern risk
            if patterns:
                for pattern in patterns:
                    if isinstance(pattern, MovementPattern):
                        risk_factors['pattern_risk'] += min(10, max(0, pattern.severity))
                        # Cap pattern risk
                        risk_factors['pattern_risk'] = min(50, risk_factors['pattern_risk'])
            
            # Zone risk
            if zones and movements:
                for movement in movements:
                    if not isinstance(movement, dict):
                        continue
                    
                    if not movement.get('coordinates'):
                        continue
                    
                    coords = movement.get('coordinates')
                    if not isinstance(coords, dict):
                        continue
                    
                    try:
                        lat = float(coords.get('lat', 0))
                        lng = float(coords.get('lng', 0))
                        
                        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                            continue
                        
                        for zone in zones:
                            if isinstance(zone, SecurityZone):
                                if SecurityMapAnalyzer._point_in_polygon(lat, lng, zone.coordinates):
                                    risk_factors['zone_risk'] += min(10, max(0, zone.risk_level))
                                    # Cap zone risk
                                    risk_factors['zone_risk'] = min(50, risk_factors['zone_risk'])
                    except (ValueError, TypeError, KeyError):
                        continue
            
            # Speed risk
            if len(movements) > 1:
                speeds = []
                for i in range(min(len(movements) - 1, 100)):  # Limit iterations
                    try:
                        curr = movements[i]
                        next_mov = movements[i + 1]
                        
                        if not isinstance(curr, dict) or not isinstance(next_mov, dict):
                            continue
                        
                        if not curr.get('coordinates') or not next_mov.get('coordinates'):
                            continue
                        
                        curr_coords = curr.get('coordinates')
                        next_coords = next_mov.get('coordinates')
                        
                        if not isinstance(curr_coords, dict) or not isinstance(next_coords, dict):
                            continue
                        
                        curr_timestamp = curr.get('timestamp', '')
                        next_timestamp = next_mov.get('timestamp', '')
                        
                        if not curr_timestamp or not next_timestamp:
                            continue
                        
                        curr_time = datetime.fromisoformat(curr_timestamp.replace('Z', '+00:00'))
                        next_time = datetime.fromisoformat(next_timestamp.replace('Z', '+00:00'))
                        
                        speed = SecurityMapAnalyzer.calculate_speed(
                            float(curr_coords.get('lat', 0)),
                            float(curr_coords.get('lng', 0)),
                            curr_time,
                            float(next_coords.get('lat', 0)),
                            float(next_coords.get('lng', 0)),
                            next_time
                        )
                        
                        if speed > 80:  # Suspicious speed
                            speeds.append(speed)
                    except (ValueError, TypeError, KeyError, IndexError):
                        continue
                
                if speeds:
                    avg_speed = sum(speeds) / len(speeds)
                    if avg_speed > 100:
                        risk_factors['speed_risk'] = min(5, max(0, int((avg_speed - 100) / 20)))
            
            # Unified risk engine (profile movement_map): same 0-100 range
            # and severity bands as every other risk number in the product.
            # The point buckets above are unchanged evidence; the engine owns
            # scaling, banding and the honesty labeling.
            from backend.core.risk_engine import risk_engine
            config, version = risk_engine.peek_model("movement_map")
            risk = risk_engine.score_movement_sync(
                config, version,
                watchlist_points=risk_factors['watchlist_risk'],
                pattern_points=risk_factors['pattern_risk'],
                zone_points=risk_factors['zone_risk'],
                speed_points=risk_factors['speed_risk'],
            )
            result = {
                'total_risk': risk.total_score,
                # legacy key/value contract ('medium', not 'moderate')
                'risk_level': risk.legacy_severity,
                'risk_factors': risk_factors,
                'severity': risk.severity,
            }
            result.update(risk.labeling())
            return result
            
        except Exception as e:
            logger.error(f"[SECURITY] Error calculating risk score: {e}", exc_info=True)
            # Return safe default
            return {
                'total_risk': 0,
                'risk_level': 'low',
                'risk_factors': {
                    'base_risk': 1,
                    'watchlist_risk': 0,
                    'pattern_risk': 0,
                    'zone_risk': 0,
                    'speed_risk': 0
                }
            }
    
    @staticmethod
    def _point_in_polygon(lat: float, lng: float, polygon: List[List[float]]) -> bool:
        """
        Check if point is inside polygon using ray casting algorithm.
        
        Args:
            lat, lng: Point coordinates
            polygon: List of [lat, lng] pairs defining polygon
            
        Returns:
            True if point is inside polygon, False otherwise
        """
        try:
            # Validate inputs
            if not isinstance(polygon, list) or len(polygon) < 3:
                return False

            if not (isinstance(lat, (int, float)) and isinstance(lng, (int, float))):
                return False

            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                return False

            # Clean the vertex list FIRST, then ray-cast over consecutive
            # edges. The old loop skipped invalid vertices with `continue`
            # WITHOUT updating `j`, so the edge pairing after any skipped
            # vertex tested phantom edges and could invert the result.
            vertices = []
            for entry in polygon:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                try:
                    vlat, vlng = float(entry[0]), float(entry[1])
                except (ValueError, TypeError):
                    continue
                if not (-90 <= vlat <= 90 and -180 <= vlng <= 180):
                    continue
                vertices.append((vlat, vlng))

            if len(vertices) < 3:
                return False

            inside = False
            j = len(vertices) - 1
            for i in range(len(vertices)):
                xi, yi = vertices[i]
                xj, yj = vertices[j]
                if ((yi > lng) != (yj > lng)) and (lat < (xj - xi) * (lng - yi) / (yj - yi) + xi):
                    inside = not inside
                j = i

            return inside

        except Exception as e:
            logger.warning(f"[SECURITY] Error checking point in polygon: {e}")
            return False
