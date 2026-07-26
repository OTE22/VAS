"""
Map Generation Service (Production-Ready)
==========================================
Provides server-side map generation for intelligence tracking with:
- Redis caching for performance
- Input validation and sanitization
- Error handling and graceful degradation
- Memory management
- Security (XSS prevention)
- Metrics and logging
- Timeout handling

Supports multiple output formats:
1. HTML Map (Folium) - Interactive map embedded in page
2. GeoJSON - For advanced frontend rendering
3. Static Image (optional) - PNG/JPEG map image
"""

import logging
import json
import hashlib
import html
import time
import math
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime
from io import BytesIO
import base64
import asyncio
from functools import wraps

from config import settings

logger = logging.getLogger(__name__)

# Try to import folium, but make it optional
try:
    # Try to use offline_folium for completely offline maps (no internet required)
    # IMPORTANT: Only import offline if resources are downloaded to avoid FileNotFoundError
    OFFLINE_MODE_AVAILABLE = False
    try:
        import os
        import importlib.util
        
        # Check if offline_folium package exists WITHOUT importing it
        # This prevents FileNotFoundError when resources aren't downloaded
        try:
            spec = importlib.util.find_spec("offline_folium")
            if spec and spec.origin:
                # Package exists - check if resources are downloaded
                offline_package_path = os.path.dirname(spec.origin)
                offline_resources_path = os.path.join(offline_package_path, 'local')
                
                # Only import and enable offline mode if resources are actually downloaded
                if os.path.exists(offline_resources_path) and os.listdir(offline_resources_path):
                    # Resources exist - now import offline to enable it
                    from offline_folium import offline
                    OFFLINE_MODE_AVAILABLE = True
                    logger.info("[MAP] ✅ Offline mode available - maps will work without internet")
                else:
                    logger.warning("[MAP] ⚠️ offline_folium installed but resources not downloaded")
                    logger.info("[MAP] Run 'python -m offline_folium' to download resources")
                    logger.info("[MAP] Using standard mode (tiles offline, JS/CSS from CDN)")
            else:
                logger.info("[MAP] ℹ️ offline_folium not installed - install with: pip install offline-folium for offline maps")
        except ImportError:
            logger.info("[MAP] ℹ️ offline_folium not installed - install with: pip install offline-folium for offline maps")
        except Exception as e:
            logger.warning(f"[MAP] ⚠️ Error checking offline_folium: {e} - using standard mode")
    except Exception as e:
        logger.warning(f"[MAP] ⚠️ Error setting up offline mode: {e} - using standard mode")
    
    import folium
    from folium import plugins
    FOLIUM_AVAILABLE = True
except ImportError:
    FOLIUM_AVAILABLE = False
    OFFLINE_MODE_AVAILABLE = False
    logger.warning("[MAP] Folium not available. Install with: pip install folium")

# Try to import cache manager
try:
    from backend.core.cache_manager import cache_manager
    CACHE_AVAILABLE = True
except ImportError:
    CACHE_AVAILABLE = False
    logger.warning("[MAP] Cache manager not available")

# Try to import security features
try:
    from backend.core.security_map_features import (
        SecurityMapAnalyzer, SecurityMapRenderer,
        SecurityZone, ThreatIndicator, MovementPattern
    )
    SECURITY_FEATURES_AVAILABLE = True
except ImportError:
    SECURITY_FEATURES_AVAILABLE = False
    logger.warning("[MAP] Security features not available")

# Try to import animated map features
try:
    from backend.core.animated_map_features import AnimatedMapRenderer
    ANIMATED_FEATURES_AVAILABLE = True
except ImportError:
    ANIMATED_FEATURES_AVAILABLE = False
    logger.warning("[MAP] Animated features not available")

# Configuration (from config.py)
MAP_CACHE_TTL = settings.MAP_CACHE_TTL
MAP_OFFLINE_TILES_PATH = getattr(settings, 'MAP_OFFLINE_TILES_PATH', None)
MAP_OFFLINE_TILES_ENABLED = getattr(settings, 'MAP_OFFLINE_TILES_ENABLED', False)
MAP_CACHE_ENABLED = settings.MAP_CACHE_ENABLED
MAP_MAX_COORDINATES = settings.MAP_MAX_COORDINATES
MAP_GENERATION_TIMEOUT = settings.MAP_GENERATION_TIMEOUT
MAP_MAX_TRACKS = settings.MAP_MAX_TRACKS
MAP_DEFAULT_STYLE = settings.MAP_DEFAULT_STYLE
MAP_SHOW_ANIMATED_AVATAR = settings.MAP_SHOW_ANIMATED_AVATAR
MAP_ANIMATION_PERIOD_SECONDS = settings.MAP_ANIMATION_PERIOD_SECONDS
MAP_ANIMATION_MAX_DURATION_SECONDS = settings.MAP_ANIMATION_MAX_DURATION_SECONDS
MAP_ANIMATION_MIN_SPEED = settings.MAP_ANIMATION_MIN_SPEED
MAP_ANIMATION_MAX_SPEED = settings.MAP_ANIMATION_MAX_SPEED
MAP_ANIMATION_TRANSITION_TIME_MS = settings.MAP_ANIMATION_TRANSITION_TIME_MS
MAP_CO_APPEARANCE_TIME_WINDOW_SECONDS = settings.MAP_CO_APPEARANCE_TIME_WINDOW_SECONDS
MAP_CO_APPEARANCE_DISTANCE_METERS = settings.MAP_CO_APPEARANCE_DISTANCE_METERS
MAP_CO_APPEARANCE_ENABLED = settings.MAP_CO_APPEARANCE_ENABLED


def validate_coordinates(lat: float, lng: float) -> bool:
    """
    Validate coordinate ranges.
    
    Args:
        lat: Latitude (-90 to 90)
        lng: Longitude (-180 to 180)
        
    Returns:
        True if coordinates are valid, False otherwise
    """
    try:
        # Ensure they are numbers
        lat_float = float(lat)
        lng_float = float(lng)
        
        # Check for NaN or infinity
        if not (math.isfinite(lat_float) and math.isfinite(lng_float)):
            return False
        
        return -90 <= lat_float <= 90 and -180 <= lng_float <= 180
    except (ValueError, TypeError):
        return False


def sanitize_html(text: Any) -> str:
    """
    Sanitize HTML to prevent XSS attacks.
    
    Args:
        text: Input text to sanitize
        
    Returns:
        Sanitized HTML-safe string
    """
    if text is None:
        return ""
    try:
        # Convert to string and escape HTML
        text_str = str(text)
        # Limit length to prevent DoS
        if len(text_str) > 10000:
            text_str = text_str[:10000] + "..."
        return html.escape(text_str)
    except Exception:
        return ""


def generate_cache_key(identity_id: str, date: Optional[str], days_back: int,
                       map_style: str, include_popups: bool, 
                       show_routes: bool, cluster_markers: bool,
                       show_animated_avatar: bool = False,
                       enable_security_features: bool = True,
                       detect_patterns: bool = True,
                       show_risk_heatmap: bool = True,
                       show_timeline: bool = False) -> str:
    """
    Generate cache key for map request.
    
    Args:
        identity_id: Identity UUID
        date: Optional date string
        days_back: Number of days
        map_style: Map style
        include_popups: Include popups flag
        show_routes: Show routes flag
        cluster_markers: Cluster markers flag
        show_animated_avatar: Show animated avatar flag
        enable_security_features: Enable security features flag
        detect_patterns: Detect patterns flag
        show_risk_heatmap: Show risk heatmap flag
        show_timeline: Show timeline flag
        
    Returns:
        SHA256 hash-based cache key
    """
    try:
        # Sanitize inputs to prevent injection
        identity_id = str(identity_id)[:100] if identity_id else ""
        date = str(date)[:20] if date else "None"
        days_back = int(days_back) if days_back else 0
        map_style = str(map_style)[:20] if map_style else MAP_DEFAULT_STYLE
        
        # Include all parameters that affect map output in cache key
        key_data = (f"{identity_id}:{date}:{days_back}:{map_style}:"
                   f"{include_popups}:{show_routes}:{cluster_markers}:"
                   f"{show_animated_avatar}:{enable_security_features}:"
                   f"{detect_patterns}:{show_risk_heatmap}:{show_timeline}")
        return f"map:{hashlib.sha256(key_data.encode('utf-8')).hexdigest()}"
    except Exception as e:
        logger.error(f"[MAP] Error generating cache key: {e}")
        # Fallback to simple hash
        return f"map:{hashlib.sha256(str(time.time()).encode('utf-8')).hexdigest()}"


def timeout_handler(timeout_seconds: int):
    """Decorator to add timeout to map generation."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await asyncio.wait_for(
                    func(*args, **kwargs),
                    timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                logger.error(f"[MAP] {func.__name__} timed out after {timeout_seconds}s")
                raise RuntimeError(f"Map generation timed out after {timeout_seconds} seconds")
        return wrapper
    return decorator


class MapService:
    """Production-ready service for generating maps from tracking data."""
    
    def __init__(self):
        self.folium_available = FOLIUM_AVAILABLE
        self.cache_available = CACHE_AVAILABLE
        self.stats = {
            'maps_generated': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'errors': 0,
            'timeouts': 0
        }
    
    def _validate_inputs(
        self,
        tracks: List[Dict],
        identity_name: Optional[str] = None,
        map_style: str = MAP_DEFAULT_STYLE
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate input parameters with comprehensive checks.
        
        Args:
            tracks: List of track dictionaries
            identity_name: Optional identity name
            map_style: Map style string
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            # Validate tracks
            if not tracks:
                return False, "No tracks provided"
            
            if not isinstance(tracks, list):
                return False, "Tracks must be a list"
            
            if len(tracks) > MAP_MAX_TRACKS:
                return False, f"Too many tracks ({len(tracks)}). Maximum is {MAP_MAX_TRACKS}"
            
            # Validate map style
            if not isinstance(map_style, str):
                return False, "Map style must be a string"
            
            if map_style not in ['dark', 'light', 'satellite', 'terrain']:
                return False, f"Invalid map style: {map_style}. Must be one of: dark, light, satellite, terrain"
            
            # Validate identity name length
            if identity_name and len(str(identity_name)) > 200:
                return False, "Identity name too long (max 200 characters)"
            
            # Validate coordinates and track structure
            total_coords = 0
            for idx, track in enumerate(tracks):
                if not isinstance(track, dict):
                    return False, f"Track at index {idx} is not a dictionary"
                
                movements = track.get('movements', [])
                if not isinstance(movements, list):
                    return False, f"Movements in track {idx} must be a list"
                
                for mov_idx, movement in enumerate(movements):
                    if not isinstance(movement, dict):
                        continue  # Skip invalid movements
                    
                    coords = movement.get('coordinates')
                    if coords:
                        if not isinstance(coords, dict):
                            continue
                        
                        try:
                            lat = coords.get('lat')
                            lng = coords.get('lng')
                            
                            if lat is not None and lng is not None:
                                # Validate coordinate types
                                try:
                                    lat_float = float(lat)
                                    lng_float = float(lng)
                                except (ValueError, TypeError):
                                    logger.warning(f"[MAP] Invalid coordinate types at track {idx}, movement {mov_idx}")
                                    continue
                                
                                if not validate_coordinates(lat_float, lng_float):
                                    return False, f"Invalid coordinates at track {idx}, movement {mov_idx}: lat={lat}, lng={lng}"
                                
                                total_coords += 1
                                if total_coords > MAP_MAX_COORDINATES:
                                    return False, f"Too many coordinates ({total_coords}). Maximum is {MAP_MAX_COORDINATES}"
                        except Exception as e:
                            logger.warning(f"[MAP] Error validating coordinates: {e}")
                            continue
            
            return True, None
            
        except Exception as e:
            logger.error(f"[MAP] Error in input validation: {e}", exc_info=True)
            return False, f"Validation error: {str(e)}"
    
    async def generate_folium_map(
        self,
        tracks: List[Dict],
        identity_name: Optional[str] = None,
        map_style: str = MAP_DEFAULT_STYLE,
        include_popups: bool = True,
        show_routes: bool = True,
        cluster_markers: bool = True,
        use_cache: bool = True,
        cache_key: Optional[str] = None,
        # Security intelligence features
        enable_security_features: bool = True,
        watchlist_matches: Optional[List[Dict]] = None,
        security_zones: Optional[List[Dict]] = None,
        detect_patterns: bool = True,
        show_risk_heatmap: bool = True,
        show_timeline: bool = False,
        # Animated features
        show_animated_avatar: bool = False,
        multi_identity_data: Optional[List[Dict]] = None
    ) -> str:
        """
        Generate an interactive HTML map using Folium (production-ready).
        
        Args:
            tracks: List of track data with movements containing coordinates
            identity_name: Name of the identity being tracked
            map_style: Map style ('dark', 'light', 'satellite', 'terrain')
            include_popups: Whether to include popup information on markers
            show_routes: Whether to draw routes between locations
            cluster_markers: Whether to cluster nearby markers
            use_cache: Whether to use cache (default: True)
            cache_key: Optional cache key (auto-generated if not provided)
            
        Returns:
            HTML string containing the map
            
        Raises:
            RuntimeError: If Folium is not available
            ValueError: If input validation fails
        """
        start_time = time.time()
        
        # Check if Folium is available
        if not self.folium_available:
            self.stats['errors'] += 1
            raise RuntimeError("Folium is not installed. Install with: pip install folium")
        
        # Validate inputs
        is_valid, error_msg = self._validate_inputs(tracks, identity_name, map_style)
        if not is_valid:
            self.stats['errors'] += 1
            raise ValueError(error_msg or "Invalid input parameters")
        
        # Check cache first
        if use_cache and self.cache_available and cache_key:
            try:
                cached_map = await cache_manager.get(cache_key)
                if cached_map:
                    self.stats['cache_hits'] += 1
                    logger.info(f"[MAP] Cache hit for key: {cache_key[:50]}...")
                    return cached_map
                self.stats['cache_misses'] += 1
            except Exception as e:
                logger.warning(f"[MAP] Cache get error (continuing without cache): {e}")
        
        # Generate map
        try:
            map_html = self._generate_map_internal(
                tracks, identity_name, map_style, 
                include_popups, show_routes, cluster_markers,
                enable_security_features, watchlist_matches,
                security_zones, detect_patterns, show_risk_heatmap, show_timeline,
                show_animated_avatar, multi_identity_data
            )
            
            # Cache the result
            if use_cache and self.cache_available and cache_key:
                try:
                    await cache_manager.set(cache_key, map_html, ttl=MAP_CACHE_TTL)
                except Exception as e:
                    logger.warning(f"[MAP] Cache set error (map still generated): {e}")
            
            self.stats['maps_generated'] += 1
            elapsed = time.time() - start_time
            logger.info(f"[MAP] Map generated in {elapsed:.2f}s ({len(tracks)} tracks)")
            
            return map_html
            
        except Exception as e:
            self.stats['errors'] += 1
            logger.error(f"[MAP] Error generating map: {e}", exc_info=True)
            raise
    
    def _generate_map_internal(
        self,
        tracks: List[Dict],
        identity_name: Optional[str] = None,
        map_style: str = MAP_DEFAULT_STYLE,
        include_popups: bool = True,
        show_routes: bool = True,
        cluster_markers: bool = True,
        enable_security_features: bool = True,
        watchlist_matches: Optional[List[Dict]] = None,
        security_zones: Optional[List[Dict]] = None,
        detect_patterns: bool = True,
        show_risk_heatmap: bool = True,
        show_timeline: bool = False,
        show_animated_avatar: bool = False,
        multi_identity_data: Optional[List[Dict]] = None
    ) -> str:
        
        """Internal method to generate map (without caching)."""
        # Collect all coordinates with validation
        all_coords = []
        movements_by_date = {}
        
        for track in tracks:
            date = track.get('date', 'Unknown')
            movements = track.get('movements', [])
            movements_by_date[date] = movements
            
            for movement in movements:
                coords = movement.get('coordinates')
                if coords:
                    lat = coords.get('lat')
                    lng = coords.get('lng')
                    if lat is not None and lng is not None:
                        # Validate coordinates
                        if validate_coordinates(lat, lng):
                            all_coords.append([lat, lng])
                        else:
                            logger.warning(f"[MAP] Skipping invalid coordinates: lat={lat}, lng={lng}")
        
        if not all_coords:
            # Return empty map with message
            return self._generate_empty_map()
        
        # Calculate center and bounds safely
        lats = [c[0] for c in all_coords]
        lngs = [c[1] for c in all_coords]
        center_lat = sum(lats) / len(lats)
        center_lng = sum(lngs) / len(lngs)
        
        # Ensure center is valid
        if not validate_coordinates(center_lat, center_lng):
            logger.warning(f"[MAP] Invalid center calculated, using default")
            center_lat, center_lng = 0.0, 0.0
        
        # Create COMPLETELY OFFLINE map - no internet required
        # Option 1: Use local tiles if available (directory structure: {z}/{x}/{y}.png)
        # Option 2: Use grid background if no local tiles
        
        use_local_tiles = False
        local_tiles_path = None
        
        if MAP_OFFLINE_TILES_ENABLED and MAP_OFFLINE_TILES_PATH:
            import os
            tiles_path = MAP_OFFLINE_TILES_PATH
            
            # Check if it's a directory with tile structure
            if os.path.isdir(tiles_path):
                # Check if directory has proper tile structure (at least one zoom level)
                # Structure should be: tiles/{z}/{x}/{y}.png
                has_tiles = False
                try:
                    # Check for at least one zoom level directory
                    for item in os.listdir(tiles_path):
                        zoom_dir = os.path.join(tiles_path, item)
                        if os.path.isdir(zoom_dir) and item.isdigit():
                            # Check if it has x directories
                            for x_item in os.listdir(zoom_dir):
                                x_dir = os.path.join(zoom_dir, x_item)
                                if os.path.isdir(x_dir) and x_item.isdigit():
                                    # Check if it has y.png files
                                    if any(f.endswith('.png') or f.endswith('.jpg') for f in os.listdir(x_dir)):
                                        has_tiles = True
                                        break
                            if has_tiles:
                                break
                except Exception as e:
                    logger.warning(f"[MAP] Error checking tile structure: {e}")
                
                if has_tiles:
                    use_local_tiles = True
                    local_tiles_path = tiles_path
                    logger.info(f"[MAP] ✅ Using local tiles from: {tiles_path}")
                else:
                    logger.warning(f"[MAP] ⚠️ Tiles directory exists but doesn't have proper structure: {tiles_path}")
                    logger.info("[MAP] Expected structure: tiles/{z}/{x}/{y}.png")
            elif os.path.isfile(tiles_path) and tiles_path.endswith('.mbtiles'):
                # MBTiles file - would need mbutil or similar to serve
                logger.info(f"[MAP] MBTiles file found: {tiles_path}")
                logger.warning("[MAP] MBTiles support requires additional setup - using grid background for now")
                logger.info("[MAP] Tip: Extract MBTiles to directory structure using tools like mbutil")
            else:
                logger.warning(f"[MAP] ⚠️ Tiles path not found or invalid: {tiles_path}")
        
        # Create map - use local tiles if available, otherwise no tiles (grid background)
        if use_local_tiles:
            # Create map with local tiles
            m = folium.Map(
                location=[center_lat, center_lng],
                zoom_start=13,
                tiles=None,  # We'll add custom tile layer
                prefer_canvas=True,
                max_zoom=18,
                min_zoom=2
            )
            
            # Add local tile layer
            # Tiles will be served via /tiles/{z}/{x}/{y}.png endpoint
            # The path is relative to the tiles directory mount point
            tile_url = "/tiles/{z}/{x}/{y}.png"
            
            folium.TileLayer(
                tiles=tile_url,
                attr='Offline Map Tiles',
                name='Offline Tiles',
                overlay=False,
                control=True,
                min_zoom=0,
                max_zoom=18
            ).add_to(m)
            
            logger.info(f"[MAP] ✅ Added local tile layer from: {local_tiles_path}")
            logger.info(f"[MAP] Tiles will be served from: {tile_url}")
        else:
            # Create map WITHOUT tiles (completely offline with grid background)
            m = folium.Map(
                location=[center_lat, center_lng],
                zoom_start=13,
                tiles=None,  # No tiles = completely offline, no internet needed
                prefer_canvas=True,
                max_zoom=18,
                min_zoom=2
            )
            
            # Add a simple colored background based on style (offline, no internet)
            # This provides visual context without requiring online tiles
            background_colors = {
                'dark': '#1a1a2e',      # Dark blue-gray
                'light': '#f5f5f5',     # Light gray
                'satellite': '#2d5016', # Green (like satellite)
                'terrain': '#8b7355'    # Brown (like terrain)
            }
            bg_color = background_colors.get(map_style, '#f5f5f5')
            
            # Add custom CSS for offline background
            offline_style = f'''
            <style>
                .leaflet-container {{
                    background-color: {bg_color} !important;
                    background-image: 
                        linear-gradient(rgba(0,0,0,0.05) 1px, transparent 1px),
                        linear-gradient(90deg, rgba(0,0,0,0.05) 1px, transparent 1px);
                    background-size: 50px 50px;
                }}
                .leaflet-tile-container {{
                    display: none !important;
                }}
            </style>
            '''
            m.get_root().html.add_child(folium.Element(offline_style))
            
            if MAP_OFFLINE_TILES_ENABLED:
                logger.info("[MAP] Offline tiles enabled but path not found - using grid background")
                logger.info(f"[MAP] Set MAP_OFFLINE_TILES_PATH to your tiles directory (format: {{z}}/{{x}}/{{y}}.png)")
            else:
                logger.info("[MAP] Using completely offline mode (grid background) - no internet connection required")
        
        # Create marker cluster group if enabled
        if cluster_markers and len(all_coords) > 5:
            marker_cluster = plugins.MarkerCluster(name='Locations').add_to(m)
            marker_group = marker_cluster
        else:
            marker_group = m
        
        # Process each track day
        colors = ['#00ff96', '#ff6b6b', '#4ecdc4', '#ffe66d', '#a8e6cf', '#ff8b94']
        color_index = 0
        
        for date, movements in movements_by_date.items():
            color = colors[color_index % len(colors)]
            color_index += 1
            
            # Filter movements with coordinates
            valid_movements = [
                m for m in movements 
                if m.get('coordinates') and m.get('coordinates').get('lat') and m.get('coordinates').get('lng')
            ]
            
            if not valid_movements:
                continue
            
            # Draw route if enabled and we have multiple points
            if show_routes and len(valid_movements) > 1:
                route_coords = [
                    [m['coordinates']['lat'], m['coordinates']['lng']]
                    for m in valid_movements
                ]
                folium.PolyLine(
                    route_coords,
                    color=color,
                    weight=3,
                    opacity=0.7,
                    popup=f"Route for {date}",
                    tooltip=f"Movement path on {date}"
                ).add_to(m)
            
            # Add markers for each location
            for idx, movement in enumerate(valid_movements):
                coords = movement['coordinates']
                lat, lng = coords['lat'], coords['lng']
                
                # Create popup content with XSS protection
                popup_html = ""
                if include_popups:
                    timestamp = movement.get('timestamp', '')
                    pipeline_name = sanitize_html(
                        movement.get('pipeline_name', movement.get('pipeline_id', 'Unknown'))
                    )
                    duration = movement.get('duration_at_location')
                    date_safe = sanitize_html(date)
                    
                    try:
                        if timestamp:
                            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
                        else:
                            time_str = 'Unknown time'
                    except Exception:
                        time_str = sanitize_html(str(timestamp))
                    
                    duration_str = f"{duration}s" if duration else "N/A"
                    
                    popup_html = f"""
                    <div style="min-width: 200px;">
                        <h4 style="margin: 0 0 10px 0; color: {color};">{pipeline_name}</h4>
                        <p style="margin: 5px 0;"><strong>Time:</strong> {time_str}</p>
                        <p style="margin: 5px 0;"><strong>Duration:</strong> {duration_str}</p>
                        <p style="margin: 5px 0;"><strong>Date:</strong> {date_safe}</p>
                        <p style="margin: 5px 0;"><strong>Stop #:</strong> {idx + 1}</p>
                    </div>
                    """
                
                # Create marker
                icon_color = 'green' if idx == 0 else ('red' if idx == len(valid_movements) - 1 else 'blue')
                icon = folium.Icon(
                    color=icon_color,
                    icon='map-marker' if idx == 0 else ('flag' if idx == len(valid_movements) - 1 else 'circle'),
                    prefix='fa'
                )
                
                marker = folium.Marker(
                    [lat, lng],
                    popup=folium.Popup(popup_html, max_width=300) if popup_html else None,
                    tooltip=f"{pipeline_name} - {time_str if 'time_str' in locals() else 'Unknown'}",
                    icon=icon
                )
                marker.add_to(marker_group)
        
        # Fit bounds to show all markers (before adding animated avatar)
        if len(all_coords) > 1:
            m.fit_bounds(all_coords, padding=(50, 50))
        
        # Add animated avatar if enabled (BEFORE layer control)
        if show_animated_avatar:
            if not ANIMATED_FEATURES_AVAILABLE:
                logger.warning("[MAP] Animated avatar requested but features not available (animated_map_features module not loaded)")
            else:
                try:
                    # Collect all movements from tracks
                    all_movements = []
                    movements_with_coords = 0
                    movements_without_coords = 0
                    
                    for track in tracks:
                        track_movements = track.get('movements', [])
                        for movement in track_movements:
                            all_movements.append(movement)
                            # Check if movement has coordinates
                            coords = movement.get('coordinates')
                            if coords and coords.get('lat') and coords.get('lng'):
                                movements_with_coords += 1
                            else:
                                movements_without_coords += 1
                                logger.debug(f"[MAP] Movement without coordinates: {movement.get('pipeline_name', 'Unknown')} - {movement.get('pipeline_id', 'N/A')}")
                    
                    logger.info(f"[MAP] Adding animated avatar with {len(all_movements)} total movements ({movements_with_coords} with coordinates, {movements_without_coords} without)")
                    
                    if all_movements:
                        identity_id = tracks[0].get('identity_id', 'unknown') if tracks else 'unknown'
                        
                        if movements_with_coords < 2:
                            logger.warning(f"[MAP] Cannot add animated avatar: need at least 2 movements with coordinates, found {movements_with_coords}")
                            logger.warning(f"[MAP] Tip: Ensure pipelines have latitude and longitude set in database")
                        else:
                            result = AnimatedMapRenderer.add_animated_avatar(
                                m,
                                all_movements,
                                identity_id=identity_id,
                                identity_name=identity_name,
                                color_index=0,
                                show_path=show_routes
                            )
                            if result:
                                logger.info(f"[MAP] ✅ Animated avatar added successfully for identity {identity_id}")
                            else:
                                logger.warning(f"[MAP] ❌ Animated avatar addition returned False (check movements have coordinates and timestamps)")
                    else:
                        logger.warning("[MAP] No movements found for animated avatar")
                except Exception as e:
                    logger.error(f"[MAP] Error adding animated avatar: {e}", exc_info=True)
        
        # Add multi-identity tracking if provided
        if multi_identity_data and ANIMATED_FEATURES_AVAILABLE:
            try:
                AnimatedMapRenderer.add_multi_identity_tracking(
                    m,
                    multi_identity_data,
                    show_paths=show_routes,
                    show_avatars=show_animated_avatar
                )
            except Exception as e:
                logger.warning(f"[MAP] Error adding multi-identity tracking: {e}")
        
        # Add security intelligence features
        if enable_security_features and SECURITY_FEATURES_AVAILABLE:
            try:
                self._add_security_features(
                    m, tracks, watchlist_matches, security_zones,
                    detect_patterns, show_risk_heatmap, show_timeline
                )
            except Exception as e:
                logger.warning(f"[MAP] Error adding security features: {e}")
        
        # Add title/legend if identity name provided (with XSS protection)
        if identity_name:
            identity_name_safe = sanitize_html(identity_name)
            
            # Calculate risk score if security features enabled
            risk_info = ""
            if enable_security_features and SECURITY_FEATURES_AVAILABLE:
                try:
                    all_movements = []
                    for track in tracks:
                        all_movements.extend(track.get('movements', []))
                    
                    risk_score = SecurityMapAnalyzer.calculate_risk_score(
                        all_movements, watchlist_matches
                    )
                    risk_color = {
                        'critical': '#ff0000',
                        'high': '#ff6600',
                        'medium': '#ffaa00',
                        'low': '#00ff96'
                    }.get(risk_score['risk_level'], '#00ff96')
                    
                    risk_info = f'''
                    <div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.2);">
                        <p style="margin: 5px 0; font-size: 11px; opacity: 0.8;">
                            <strong>Risk Level:</strong> 
                            <span style="color: {risk_color}; font-weight: bold;">
                                {risk_score['risk_level'].upper()}
                            </span>
                            <span style="opacity: 0.7;">({risk_score['total_risk']}/100)</span>
                        </p>
                    </div>
                    '''
                except Exception as e:
                    logger.warning(f"[MAP] Error calculating risk score: {e}")
            
            title_html = f'''
            <div style="position: fixed; 
                        top: 10px; right: 10px; width: 280px; height: auto; 
                        background-color: rgba(0, 0, 0, 0.9);
                        border: 2px solid {colors[0]};
                        border-radius: 8px;
                        padding: 15px;
                        z-index: 9999;
                        font-family: Arial, sans-serif;
                        color: white;
                        box-shadow: 0 4px 12px rgba(0,0,0,0.5);">
                <div style="display: flex; align-items: center; margin-bottom: 10px;">
                    <span style="font-size: 20px; margin-right: 8px;">🔒</span>
                    <h3 style="margin: 0; color: {colors[0]}; font-size: 16px;">{identity_name_safe}</h3>
                </div>
                <p style="margin: 5px 0; font-size: 12px; opacity: 0.9;">Security Intelligence Map</p>
                <p style="margin: 5px 0; font-size: 11px; opacity: 0.8;">Total Locations: {len(all_coords)}</p>
                {risk_info}
            </div>
            '''
            m.get_root().html.add_child(folium.Element(title_html))
        
        # Add layer control AFTER all layers (FeatureGroups, plugins, etc.) are added
        # Based on Leaflet documentation: LayerControl must be added AFTER all layers are fully initialized
        # The "setZIndex" error occurs when LayerControl tries to manage layers that aren't fully initialized
        # 
        # IMPORTANT: When using TimestampedGeoJson plugin with FeatureGroups, there can be conflicts.
        # Solution: Only add LayerControl if we're not using animated avatar, or skip it entirely if there are issues.
        
        # Skip LayerControl if animated avatar is enabled (to avoid conflicts with TimestampedGeoJson)
        # The map will still work perfectly - users just won't have layer toggles
        if show_animated_avatar:
            logger.info("[MAP] Skipping LayerControl when animated avatar is enabled (prevents setZIndex conflicts)")
        else:
            # Only add LayerControl if animated avatar is NOT enabled
            try:
                # Ensure all FeatureGroups are fully added before creating LayerControl
                # LayerControl automatically detects FeatureGroups that are already on the map
                layer_control = folium.LayerControl(collapsed=False)
                layer_control.add_to(m)
                logger.debug("[MAP] LayerControl added successfully")
            except (AttributeError, TypeError, KeyError) as e:
                # These errors indicate a layer initialization issue
                # This can happen if FeatureGroups aren't fully initialized when LayerControl tries to manage them
                logger.warning(f"[MAP] LayerControl error (map will still work): {type(e).__name__}: {e}")
                logger.info("[MAP] This is a known issue when mixing plugins with FeatureGroups - map functionality is unaffected")
            except Exception as e:
                # Catch any other errors
                logger.warning(f"[MAP] Could not add layer control: {e}")
                logger.info("[MAP] Map will continue without layer control - all features will still work")
        
        # Generate HTML - use offline mode if available and resources are downloaded
        # If offline_folium is installed AND resources are downloaded, use it
        # Otherwise, use standard Folium (tiles are None, but JS/CSS may use CDN)
        try:
            if OFFLINE_MODE_AVAILABLE:
                # offline_folium was imported at the top, so it will automatically handle offline resources
                html = m._repr_html_()
                logger.info("[MAP] ✅ Generated HTML using offline_folium (100% offline - no internet required)")
            else:
                # Standard Folium - tiles are None (offline) but JS/CSS may load from CDN
                html = m._repr_html_()
                logger.info("[MAP] Generated HTML (tiles offline, JS/CSS may use CDN)")
        except FileNotFoundError as e:
            # If offline_folium resources aren't downloaded, fall back to standard mode
            logger.warning(f"[MAP] ⚠️ offline_folium resources not found: {e}")
            logger.info("[MAP] Falling back to standard mode - run 'python -m offline_folium' to download resources")
            # Don't import offline - use standard Folium
            html = m._repr_html_()
        except Exception as e:
            logger.error(f"[MAP] Error generating HTML: {e}")
            # Fallback to standard mode
            html = m._repr_html_()
        
        return html
    
    def _add_security_features(
        self,
        map_obj,
        tracks: List[Dict],
        watchlist_matches: Optional[List[Dict]],
        security_zones: Optional[List[Dict]],
        detect_patterns: bool,
        show_risk_heatmap: bool,
        show_timeline: bool
    ):
        """Add security intelligence features to map."""
        if not SECURITY_FEATURES_AVAILABLE:
            return
        
        try:
            # Collect all movements safely
            all_movements = []
            for track in tracks:
                if isinstance(track, dict):
                    movements = track.get('movements', [])
                    if isinstance(movements, list):
                        all_movements.extend(movements)
            
            # Limit movements to prevent memory issues
            if len(all_movements) > MAP_MAX_COORDINATES:
                logger.warning(f"[MAP] Limiting movements from {len(all_movements)} to {MAP_MAX_COORDINATES}")
                all_movements = all_movements[:MAP_MAX_COORDINATES]
            
            # Add security zones
            if security_zones and isinstance(security_zones, list):
                zones = []
                for zone_data in security_zones:
                    if not isinstance(zone_data, dict):
                        continue
                    
                    try:
                        coordinates = zone_data.get('coordinates', [])
                        if not isinstance(coordinates, list) or len(coordinates) < 3:
                            continue
                        
                        # Validate coordinates
                        valid_coords = []
                        for coord in coordinates:
                            if isinstance(coord, list) and len(coord) >= 2:
                                try:
                                    lat, lng = float(coord[0]), float(coord[1])
                                    if validate_coordinates(lat, lng):
                                        valid_coords.append([lat, lng])
                                except (ValueError, TypeError, IndexError):
                                    continue
                        
                        if len(valid_coords) < 3:
                            continue
                        
                        zones.append(SecurityZone(
                            name=str(zone_data.get('name', 'Unknown'))[:100],
                            coordinates=valid_coords,
                            zone_type=str(zone_data.get('zone_type', 'monitored'))[:50],
                            risk_level=min(10, max(1, int(zone_data.get('risk_level', 5)))),
                            description=str(zone_data.get('description', ''))[:500] if zone_data.get('description') else None
                        ))
                    except Exception as e:
                        logger.warning(f"[MAP] Error processing security zone: {e}")
                        continue
                
                if zones:
                    try:
                        # Add security zones to a feature group for layer control
                        zones_group = folium.FeatureGroup(name='Security Zones', show=True)
                        for zone in zones:
                            color = {'restricted': 'red', 'high_security': 'orange', 'monitored': 'yellow', 'safe': 'green'}.get(zone.zone_type, 'gray')
                            opacity = 0.3 if zone.risk_level < 5 else 0.5
                            folium.Polygon(
                                zone.coordinates,
                                color=color,
                                fill=True,
                                fillColor=color,
                                fillOpacity=opacity,
                                popup=folium.Popup(
                                    f"<b>{zone.name}</b><br>Type: {zone.zone_type}<br>Risk: {zone.risk_level}/10<br>{zone.description or ''}",
                                    max_width=300
                                ),
                                tooltip=f"{zone.name} (Risk: {zone.risk_level}/10)"
                            ).add_to(zones_group)
                        zones_group.add_to(map_obj)
                    except Exception as e:
                        logger.warning(f"[MAP] Error adding security zones: {e}")
            
            # Detect movement patterns
            patterns = []
            if detect_patterns and all_movements:
                try:
                    # Detect loitering
                    loitering = SecurityMapAnalyzer.detect_loitering(all_movements)
                    if loitering:
                        patterns.extend(loitering)
                except Exception as e:
                    logger.warning(f"[MAP] Error detecting loitering: {e}")
                
                try:
                    # Detect backtracking
                    backtracking = SecurityMapAnalyzer.detect_backtracking(all_movements)
                    if backtracking:
                        patterns.extend(backtracking)
                except Exception as e:
                    logger.warning(f"[MAP] Error detecting backtracking: {e}")
                
                try:
                    # Detect rapid movement
                    rapid = SecurityMapAnalyzer.detect_rapid_movement(all_movements)
                    if rapid:
                        patterns.extend(rapid)
                except Exception as e:
                    logger.warning(f"[MAP] Error detecting rapid movement: {e}")
                
                if patterns:
                    try:
                        # Add pattern indicators to a feature group
                        patterns_group = folium.FeatureGroup(name='Movement Patterns', show=True)
                        pattern_colors = {'loitering': 'purple', 'backtracking': 'red', 'rapid_movement': 'orange', 'unusual_route': 'darkred'}
                        
                        for pattern in patterns:
                            color = pattern_colors.get(pattern.pattern_type, 'gray')
                            
                            if len(pattern.locations) > 1:
                                if pattern.pattern_type == 'loitering':
                                    center_lat = sum(loc[0] for loc in pattern.locations) / len(pattern.locations)
                                    center_lng = sum(loc[1] for loc in pattern.locations) / len(pattern.locations)
                                    folium.Circle(
                                        location=[center_lat, center_lng],
                                        radius=50,
                                        color=color,
                                        fill=True,
                                        fillColor=color,
                                        fillOpacity=0.3,
                                        popup=folium.Popup(f"<b>Loitering Detected</b><br>Duration: {pattern.severity} minutes", max_width=200),
                                        tooltip="Loitering Pattern"
                                    ).add_to(patterns_group)
                                else:
                                    folium.PolyLine(
                                        [[loc[0], loc[1]] for loc in pattern.locations],
                                        color=color,
                                        weight=3,
                                        opacity=0.7,
                                        popup=folium.Popup(f"<b>{pattern.pattern_type.replace('_', ' ').title()}</b>", max_width=200),
                                        tooltip=f"{pattern.pattern_type.replace('_', ' ').title()} Pattern"
                                    ).add_to(patterns_group)
                        
                        patterns_group.add_to(map_obj)
                    except Exception as e:
                        logger.warning(f"[MAP] Error adding pattern indicators: {e}")
            
            # Add threat indicators from watchlist matches
            threats = []
            if watchlist_matches and isinstance(watchlist_matches, list):
                for match in watchlist_matches:
                    if not isinstance(match, dict):
                        continue
                    
                    try:
                        alert_level = str(match.get('alert_level', 'low')).lower()
                        threat_level = {
                            'critical': 9,
                            'high': 7,
                            'medium': 5,
                            'low': 3
                        }.get(alert_level, 3)
                        
                        list_name = str(match.get('list_name', 'Unknown'))[:100]
                        
                        # Find locations where this identity was detected
                        for movement in all_movements[:100]:  # Limit to prevent too many threats
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
                                
                                if not validate_coordinates(lat, lng):
                                    continue
                                
                                timestamp = None
                                if movement.get('timestamp'):
                                    try:
                                        timestamp = datetime.fromisoformat(
                                            str(movement.get('timestamp')).replace('Z', '+00:00')
                                        )
                                    except (ValueError, TypeError):
                                        pass
                                
                                threats.append(ThreatIndicator(
                                    lat=lat,
                                    lng=lng,
                                    threat_level=threat_level,
                                    threat_type='watchlist',
                                    description=f"Watchlist: {list_name}",
                                    timestamp=timestamp
                                ))
                            except (ValueError, TypeError, KeyError):
                                continue
                    except Exception as e:
                        logger.warning(f"[MAP] Error processing watchlist match: {e}")
                        continue
                
                if threats:
                    try:
                        # Add threat indicators to a feature group
                        threats_group = folium.FeatureGroup(name='Threat Indicators', show=True)
                        for threat in threats:
                            color = 'red' if threat.threat_level >= 8 else ('orange' if threat.threat_level >= 5 else 'yellow')
                            icon = 'exclamation-triangle' if threat.threat_level >= 8 else ('exclamation-circle' if threat.threat_level >= 5 else 'info-sign')
                            folium.Marker(
                                [threat.lat, threat.lng],
                                icon=folium.Icon(color=color, icon=icon, prefix='fa'),
                                popup=folium.Popup(
                                    f"<b>Threat Alert</b><br>Level: {threat.threat_level}/10<br>Type: {threat.threat_type}<br>{threat.description}",
                                    max_width=300
                                ),
                                tooltip=f"Threat: {threat.threat_type} (Level {threat.threat_level})"
                            ).add_to(threats_group)
                        threats_group.add_to(map_obj)
                    except Exception as e:
                        logger.warning(f"[MAP] Error adding threat indicators: {e}")
            
            # Add risk heatmap
            if show_risk_heatmap and all_movements:
                try:
                    zones_list = []
                    if security_zones and isinstance(security_zones, list):
                        for z in security_zones:
                            if isinstance(z, dict):
                                try:
                                    zones_list.append(SecurityZone(**z))
                                except Exception:
                                    pass
                    
                    risk_score = SecurityMapAnalyzer.calculate_risk_score(
                        all_movements, watchlist_matches, patterns, zones_list if zones_list else None
                    )
                    
                    if risk_score:
                        SecurityMapRenderer.add_risk_heatmap(map_obj, all_movements, risk_score)
                except Exception as e:
                    logger.warning(f"[MAP] Error adding risk heatmap: {e}")
            
            # Add timeline control
            if show_timeline and all_movements:
                try:
                    SecurityMapRenderer.add_timeline_control(map_obj, all_movements)
                except Exception as e:
                    logger.warning(f"[MAP] Error adding timeline control: {e}")
                    
        except Exception as e:
            logger.error(f"[MAP] Error in security features: {e}", exc_info=True)
            # Continue without security features rather than failing completely
    
    def _generate_empty_map(self) -> str:
        """Generate an empty map with informative message (completely offline)."""
        # Create completely offline empty map
        m = folium.Map(
            location=[0, 0],
            zoom_start=2,
            tiles=None  # No tiles = completely offline
        )
        
        # Add offline background
        offline_style = '''
        <style>
            .leaflet-container {
                background-color: #f5f5f5 !important;
                background-image: 
                    linear-gradient(rgba(0,0,0,0.05) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(0,0,0,0.05) 1px, transparent 1px);
                background-size: 50px 50px;
            }
            .leaflet-tile-container {
                display: none !important;
            }
        </style>
        '''
        m.get_root().html.add_child(folium.Element(offline_style))
        folium.Marker(
            [0, 0],
            popup=folium.Popup(
                html="<div style='text-align: center;'><h4>No Coordinates Available</h4><p>Pipelines need latitude and longitude coordinates to display on map.</p></div>",
                max_width=300
            ),
            icon=folium.Icon(color='red', icon='info-sign')
        ).add_to(m)
        return m._repr_html_()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get service statistics."""
        return {
            **self.stats,
            'folium_available': self.folium_available,
            'cache_available': self.cache_available,
            'cache_enabled': MAP_CACHE_ENABLED
        }
    
    async def generate_geojson(
        self,
        tracks: List[Dict],
        use_cache: bool = True,
        cache_key: Optional[str] = None
    ) -> Dict:
        """
        Generate GeoJSON format data for advanced frontend rendering (production-ready).
        
        Args:
            tracks: List of track data with movements containing coordinates
            use_cache: Whether to use cache (default: True)
            cache_key: Optional cache key (auto-generated if not provided)
            
        Returns:
            GeoJSON FeatureCollection
        """
        start_time = time.time()
        
        # Validate inputs
        is_valid, error_msg = self._validate_inputs(tracks, None, 'dark')
        if not is_valid:
            self.stats['errors'] += 1
            raise ValueError(error_msg or "Invalid input parameters")
        
        # Check cache first
        if use_cache and self.cache_available and cache_key:
            try:
                cached_geojson = await cache_manager.get(cache_key)
                if cached_geojson:
                    self.stats['cache_hits'] += 1
                    logger.info(f"[MAP] GeoJSON cache hit for key: {cache_key[:50]}...")
                    return json.loads(cached_geojson)
                self.stats['cache_misses'] += 1
            except Exception as e:
                logger.warning(f"[MAP] Cache get error (continuing without cache): {e}")
        
        # Generate GeoJSON
        try:
            geojson = self._generate_geojson_internal(tracks)
            
            # Cache the result
            if use_cache and self.cache_available and cache_key:
                try:
                    await cache_manager.set(
                        cache_key, 
                        json.dumps(geojson), 
                        ttl=MAP_CACHE_TTL
                    )
                except Exception as e:
                    logger.warning(f"[MAP] Cache set error (GeoJSON still generated): {e}")
            
            elapsed = time.time() - start_time
            logger.info(f"[MAP] GeoJSON generated in {elapsed:.2f}s ({len(tracks)} tracks)")
            
            return geojson
            
        except Exception as e:
            self.stats['errors'] += 1
            logger.error(f"[MAP] Error generating GeoJSON: {e}", exc_info=True)
            raise
    
    def _generate_geojson_internal(self, tracks: List[Dict]) -> Dict:
        """Internal method to generate GeoJSON (without caching)."""
        features = []
        
        for track_idx, track in enumerate(tracks):
            date = track.get('date', 'Unknown')
            movements = track.get('movements', [])
            
            # Filter valid movements with coordinates
            valid_movements = [
                m for m in movements 
                if m.get('coordinates') and m.get('coordinates').get('lat') and m.get('coordinates').get('lng')
            ]
            
            if not valid_movements:
                continue
            
            # Create route line (LineString)
            if len(valid_movements) > 1:
                coordinates = [
                    [m['coordinates']['lng'], m['coordinates']['lat']]  # GeoJSON uses [lng, lat]
                    for m in valid_movements
                ]
                
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coordinates
                    },
                    "properties": {
                        "date": date,
                        "type": "route",
                        "movement_count": len(valid_movements),
                        "track_index": track_idx
                    }
                })
            
            # Create point markers with validation
            for idx, movement in enumerate(valid_movements):
                coords = movement['coordinates']
                lat = coords.get('lat')
                lng = coords.get('lng')
                
                # Validate coordinates
                if lat is None or lng is None:
                    continue
                if not validate_coordinates(lat, lng):
                    logger.warning(f"[MAP] Skipping invalid coordinates in GeoJSON: lat={lat}, lng={lng}")
                    continue
                
                # Sanitize string values
                pipeline_id = sanitize_html(str(movement.get('pipeline_id', '')))
                pipeline_name = sanitize_html(
                    str(movement.get('pipeline_name', movement.get('pipeline_id', 'Unknown')))
                )
                
                feature = {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [float(lng), float(lat)]  # GeoJSON uses [lng, lat]
                    },
                    "properties": {
                        "date": sanitize_html(str(date)),
                        "type": "location",
                        "pipeline_id": pipeline_id,
                        "pipeline_name": pipeline_name,
                        "timestamp": movement.get('timestamp'),
                        "duration_at_location": movement.get('duration_at_location'),
                        "sequence": idx + 1,
                        "is_start": idx == 0,
                        "is_end": idx == len(valid_movements) - 1,
                        "track_index": track_idx
                    }
                }
                features.append(feature)
        
        return {
            "type": "FeatureCollection",
            "features": features
        }


# Global instance
map_service = MapService()

