"""
Security Intelligence Map Features
==================================
Advanced features for security intelligence visualization:
- Threat level visualization
- Watchlist integration
- Alert overlays
- Zone/area marking
- Timeline playback
- Speed analysis
- Pattern detection (loitering, backtracking)
- Co-appearance visualization
- Risk scoring
- Activity heatmaps
"""

import logging
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime, timedelta
from dataclasses import dataclass
import math

logger = logging.getLogger(__name__)

# Try to import folium
try:
    import folium
    from folium import plugins
    FOLIUM_AVAILABLE = True
except ImportError:
    FOLIUM_AVAILABLE = False


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
                        min_duration_minutes: int = 5) -> List[MovementPattern]:
        """
        Detect loitering behavior (staying in small area for extended time).
        
        Args:
            movements: List of movement dictionaries
            radius_meters: Radius in meters to consider loitering
            min_duration_minutes: Minimum duration in minutes
            
        Returns:
            List of detected loitering patterns
        """
        patterns = []
        
        try:
            # Validate inputs
            if not isinstance(movements, list):
                return patterns
            
            if len(movements) < 2:
                return patterns
            
            # Validate parameters
            radius_meters = max(10, min(1000, float(radius_meters)))  # Clamp between 10-1000m
            min_duration_minutes = max(1, min(60, int(min_duration_minutes)))  # Clamp between 1-60min
        
            i = 0
            max_iterations = len(movements) * 10  # Prevent infinite loops
            iteration_count = 0
            
            while i < len(movements) and iteration_count < max_iterations:
                iteration_count += 1
                
                if not isinstance(movements[i], dict):
                    i += 1
                    continue
                
                if not movements[i].get('coordinates'):
                    i += 1
                    continue
                
                coords = movements[i].get('coordinates')
                if not isinstance(coords, dict):
                    i += 1
                    continue
                
                try:
                    start_lat = float(coords.get('lat', 0))
                    start_lng = float(coords.get('lng', 0))
                    
                    # Validate coordinates
                    if not (-90 <= start_lat <= 90 and -180 <= start_lng <= 180):
                        i += 1
                        continue
                    
                    timestamp_str = movements[i].get('timestamp', '')
                    if not timestamp_str:
                        i += 1
                        continue
                    
                    start_time = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug(f"[SECURITY] Error parsing movement {i}: {e}")
                    i += 1
                    continue
            
            # Find all movements within radius
            loitering_points = [(start_lat, start_lng)]
            j = i + 1
            
            while j < len(movements):
                if not movements[j].get('coordinates'):
                    j += 1
                    continue
                
                curr_coords = movements[j]['coordinates']
                curr_lat, curr_lng = curr_coords['lat'], curr_coords['lng']
                
                # Calculate distance
                distance = SecurityMapAnalyzer._haversine_distance(
                    start_lat, start_lng, curr_lat, curr_lng
                )
                
                if distance <= radius_meters / 1000:  # Convert to km
                    loitering_points.append((curr_lat, curr_lng))
                    j += 1
                else:
                    break
            
            # Check if loitering duration is significant
            if len(loitering_points) > 1:
                end_time = datetime.fromisoformat(movements[j-1]['timestamp'].replace('Z', '+00:00'))
                duration_minutes = (end_time - start_time).total_seconds() / 60
                
                if duration_minutes >= min_duration_minutes:
                    severity = min(10, int(duration_minutes / 5))  # 1 point per 5 minutes
                    patterns.append(MovementPattern(
                        pattern_type='loitering',
                        locations=loitering_points,
                        severity=severity,
                        description=f"Loitering detected for {int(duration_minutes)} minutes",
                        start_time=start_time,
                        end_time=end_time
                    ))
            
                i = j
            
            return patterns
            
        except Exception as e:
            logger.error(f"[SECURITY] Error detecting loitering: {e}", exc_info=True)
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
                    
                    # Check if we've been near this location before
                    for prev_lat, prev_lng, prev_idx in visited_locations:
                        distance = SecurityMapAnalyzer._haversine_distance(lat, lng, prev_lat, prev_lng)
                        
                        if distance < 0.1:  # Within 100m
                            # Check if significant backtracking
                            if i - prev_idx > 2:  # At least 2 movements between
                                try:
                                    prev_timestamp = movements[prev_idx].get('timestamp', '')
                                    curr_timestamp = movement.get('timestamp', '')
                                    
                                    if prev_timestamp and curr_timestamp:
                                        start_time = datetime.fromisoformat(prev_timestamp.replace('Z', '+00:00'))
                                        end_time = datetime.fromisoformat(curr_timestamp.replace('Z', '+00:00'))
                                        
                                        severity = min(10, (i - prev_idx) // 2)
                                        patterns.append(MovementPattern(
                                            pattern_type='backtracking',
                                            locations=[(prev_lat, prev_lng), (lat, lng)],
                                            severity=severity,
                                            description=f"Backtracking detected: returned to location visited {i - prev_idx} stops earlier",
                                            start_time=start_time,
                                            end_time=end_time
                                        ))
                                except (ValueError, KeyError, TypeError) as e:
                                    logger.debug(f"[SECURITY] Error parsing timestamps for backtracking: {e}")
                                    continue
                    
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
            
            # Calculate total risk
            total_risk = sum(risk_factors.values())
            
            # Determine risk level
            risk_level = 'low'
            if total_risk >= 20:
                risk_level = 'critical'
            elif total_risk >= 15:
                risk_level = 'high'
            elif total_risk >= 10:
                risk_level = 'medium'
            
            return {
                'total_risk': min(100, max(0, total_risk * 5)),  # Scale to 0-100
                'risk_level': risk_level,
                'risk_factors': risk_factors
            }
            
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
            
            inside = False
            j = len(polygon) - 1
            
            for i in range(len(polygon)):
                if not isinstance(polygon[i], list) or len(polygon[i]) < 2:
                    continue
                
                try:
                    xi, yi = float(polygon[i][0]), float(polygon[i][1])
                    xj, yj = float(polygon[j][0]), float(polygon[j][1])
                    
                    # Validate polygon coordinates
                    if not (-90 <= xi <= 90 and -90 <= xj <= 90 and
                           -180 <= yi <= 180 and -180 <= yj <= 180):
                        continue
                    
                    if ((yi > lng) != (yj > lng)) and (lat < (xj - xi) * (lng - yi) / (yj - yi) + xi):
                        inside = not inside
                    
                    j = i
                except (ValueError, TypeError, IndexError):
                    continue
            
            return inside
            
        except Exception as e:
            logger.warning(f"[SECURITY] Error checking point in polygon: {e}")
            return False


class SecurityMapRenderer:
    """Renderer for security intelligence features on maps"""
    
    @staticmethod
    def add_security_zones(map_obj, zones: List[SecurityZone]):
        """Add security zones to map."""
        if not FOLIUM_AVAILABLE:
            return
        
        zone_colors = {
            'restricted': 'red',
            'high_security': 'orange',
            'monitored': 'yellow',
            'safe': 'green'
        }
        
        for zone in zones:
            color = zone_colors.get(zone.zone_type, 'gray')
            opacity = 0.3 if zone.risk_level < 5 else 0.5
            
            folium.Polygon(
                zone.coordinates,
                color=color,
                fill=True,
                fillColor=color,
                fillOpacity=opacity,
                popup=folium.Popup(
                    f"<b>{zone.name}</b><br>"
                    f"Type: {zone.zone_type}<br>"
                    f"Risk Level: {zone.risk_level}/10<br>"
                    f"{zone.description or ''}",
                    max_width=300
                ),
                tooltip=f"{zone.name} (Risk: {zone.risk_level}/10)"
            ).add_to(map_obj)
    
    @staticmethod
    def add_threat_indicators(map_obj, threats: List[ThreatIndicator]):
        """Add threat indicators to map."""
        if not FOLIUM_AVAILABLE:
            return
        
        for threat in threats:
            # Color based on threat level
            if threat.threat_level >= 8:
                color = 'red'
                icon = 'exclamation-triangle'
            elif threat.threat_level >= 5:
                color = 'orange'
                icon = 'exclamation-circle'
            else:
                color = 'yellow'
                icon = 'info-sign'
            
            folium.Marker(
                [threat.lat, threat.lng],
                icon=folium.Icon(color=color, icon=icon, prefix='fa'),
                popup=folium.Popup(
                    f"<b>Threat Alert</b><br>"
                    f"Level: {threat.threat_level}/10<br>"
                    f"Type: {threat.threat_type}<br>"
                    f"{threat.description}",
                    max_width=300
                ),
                tooltip=f"Threat: {threat.threat_type} (Level {threat.threat_level})"
            ).add_to(map_obj)
    
    @staticmethod
    def add_pattern_indicators(map_obj, patterns: List[MovementPattern]):
        """Add movement pattern indicators to map."""
        if not FOLIUM_AVAILABLE:
            return
        
        pattern_colors = {
            'loitering': 'purple',
            'backtracking': 'red',
            'rapid_movement': 'orange',
            'unusual_route': 'darkred'
        }
        
        for pattern in patterns:
            color = pattern_colors.get(pattern.pattern_type, 'gray')
            
            # Draw pattern area or line
            if len(pattern.locations) > 1:
                if pattern.pattern_type == 'loitering':
                    # Draw circle for loitering area
                    center_lat = sum(loc[0] for loc in pattern.locations) / len(pattern.locations)
                    center_lng = sum(loc[1] for loc in pattern.locations) / len(pattern.locations)
                    
                    folium.Circle(
                        location=[center_lat, center_lng],
                        radius=50,  # 50 meters
                        color=color,
                        fill=True,
                        fillColor=color,
                        fillOpacity=0.3,
                        popup=folium.Popup(
                            f"<b>{pattern.pattern_type.replace('_', ' ').title()}</b><br>"
                            f"Severity: {pattern.severity}/10<br>"
                            f"{pattern.description}",
                            max_width=300
                        )
                    ).add_to(map_obj)
                else:
                    # Draw line for movement patterns
                    folium.PolyLine(
                        pattern.locations,
                        color=color,
                        weight=5,
                        opacity=0.7,
                        dashArray='10, 5',
                        popup=folium.Popup(
                            f"<b>{pattern.pattern_type.replace('_', ' ').title()}</b><br>"
                            f"Severity: {pattern.severity}/10<br>"
                            f"{pattern.description}",
                            max_width=300
                        )
                    ).add_to(map_obj)
    
    @staticmethod
    def add_risk_heatmap(map_obj, movements: List[Dict], risk_scores: Dict[str, Any]):
        """Add risk heatmap overlay."""
        if not FOLIUM_AVAILABLE:
            return
        
        # Prepare heatmap data
        heat_data = []
        for movement in movements:
            if not movement.get('coordinates'):
                continue
            coords = movement['coordinates']
            # Weight based on risk
            weight = risk_scores.get('total_risk', 1) / 10
            heat_data.append([coords['lat'], coords['lng'], weight])
        
        if heat_data:
            plugins.HeatMap(
                heat_data,
                min_opacity=0.2,
                max_zoom=18,
                radius=25,
                blur=15,
                gradient={0.2: 'blue', 0.4: 'lime', 0.6: 'orange', 1: 'red'}
            ).add_to(map_obj)
    
    @staticmethod
    def add_timeline_control(map_obj, movements: List[Dict]):
        """Add timeline playback control."""
        if not FOLIUM_AVAILABLE or len(movements) < 2:
            return
        
        # Prepare timeline data
        timeline_data = []
        for i, movement in enumerate(movements):
            if not movement.get('coordinates'):
                continue
            coords = movement['coordinates']
            timestamp = movement.get('timestamp', '')
            
            try:
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                timeline_data.append({
                    'time': dt.isoformat(),
                    'popup': f"Location {i+1}: {movement.get('pipeline_name', 'Unknown')}",
                    'coordinates': [coords['lat'], coords['lng']]
                })
            except:
                continue
        
        if timeline_data:
            # Create timeline feature group
            timeline_group = folium.FeatureGroup(name='Timeline')
            
            for i, point in enumerate(timeline_data):
                folium.Marker(
                    point['coordinates'],
                    popup=folium.Popup(point['popup'], max_width=200),
                    icon=folium.Icon(color='blue', icon='circle', prefix='fa')
                ).add_to(timeline_group)
            
            timeline_group.add_to(map_obj)
            
            # Note: Full timeline plugin would require additional JavaScript
            # This is a basic implementation

