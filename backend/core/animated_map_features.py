"""
Animated Map Features for Multi-Identity Tracking
==================================================
Features for animated avatar movement and multi-identity tracking visualization.
Similar to central agency tracking systems.
"""

import logging
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
import json

logger = logging.getLogger(__name__)

# Try to import config
try:
    from config import settings
    ANIMATION_PERIOD_SECONDS = settings.MAP_ANIMATION_PERIOD_SECONDS
    ANIMATION_MAX_DURATION_SECONDS = settings.MAP_ANIMATION_MAX_DURATION_SECONDS
    ANIMATION_MIN_SPEED = settings.MAP_ANIMATION_MIN_SPEED
    ANIMATION_MAX_SPEED = settings.MAP_ANIMATION_MAX_SPEED
    ANIMATION_TRANSITION_TIME_MS = settings.MAP_ANIMATION_TRANSITION_TIME_MS
    CO_APPEARANCE_TIME_WINDOW_SECONDS = settings.MAP_CO_APPEARANCE_TIME_WINDOW_SECONDS
    CO_APPEARANCE_DISTANCE_METERS = settings.MAP_CO_APPEARANCE_DISTANCE_METERS
    CO_APPEARANCE_ENABLED = settings.MAP_CO_APPEARANCE_ENABLED
except ImportError:
    # Fallback defaults if config not available
    ANIMATION_PERIOD_SECONDS = 1
    ANIMATION_MAX_DURATION_SECONDS = 600
    ANIMATION_MIN_SPEED = 0.5
    ANIMATION_MAX_SPEED = 10.0
    ANIMATION_TRANSITION_TIME_MS = 300
    CO_APPEARANCE_TIME_WINDOW_SECONDS = 10
    CO_APPEARANCE_DISTANCE_METERS = 100.0
    CO_APPEARANCE_ENABLED = True
    logger.warning("[ANIMATED] Config not available, using default values")

# Try to import folium
try:
    import folium
    from folium import plugins
    FOLIUM_AVAILABLE = True
except ImportError:
    FOLIUM_AVAILABLE = False


class AnimatedMapRenderer:
    """Renderer for animated avatar movement and multi-identity tracking."""
    
    # Avatar colors for different identities
    AVATAR_COLORS = [
        '#00ff96',  # Green (primary)
        '#ff6b6b',  # Red
        '#4ecdc4',  # Cyan
        '#ffe66d',  # Yellow
        '#a8e6cf',  # Light Green
        '#ff8b94',  # Pink
        '#95e1d3',  # Teal
        '#f38181',  # Coral
        '#aa96da',  # Purple
        '#fcbad3'   # Light Pink
    ]
    
    @staticmethod
    def add_animated_avatar(
        map_obj,
        movements: List[Dict],
        identity_id: str,
        identity_name: Optional[str] = None,
        color_index: int = 0,
        show_path: bool = True
    ) -> bool:
        """
        Add animated avatar that moves along the route.
        
        Args:
            map_obj: Folium map object
            movements: List of movement data with coordinates and timestamps
            identity_id: Unique identifier for the identity
            identity_name: Display name for the identity
            color_index: Index for avatar color (0-9)
            show_path: Whether to show the path line
            
        Returns:
            True if successful, False otherwise
        """
        if not FOLIUM_AVAILABLE:
            logger.warning("[ANIMATED] Folium not available, cannot add animated avatar")
            return False
        
        try:
            logger.info(f"[ANIMATED] Adding animated avatar for {identity_name or identity_id} with {len(movements)} movements")
            
            # Filter valid movements with coordinates
            valid_movements = []
            for movement in movements:
                coords = movement.get('coordinates')
                if coords and coords.get('lat') and coords.get('lng'):
                    valid_movements.append(movement)
                else:
                    logger.debug(f"[ANIMATED] Skipping movement without valid coordinates: {movement.get('pipeline_name', 'Unknown')}")
            
            logger.info(f"[ANIMATED] Found {len(valid_movements)} valid movements with coordinates")
            
            if len(valid_movements) < 2:
                logger.warning(f"[ANIMATED] Need at least 2 movements for animation, found {len(valid_movements)}")
                return False
            
            # Get color for this identity
            color = AnimatedMapRenderer.AVATAR_COLORS[color_index % len(AnimatedMapRenderer.AVATAR_COLORS)]
            
            # Create feature group for this identity
            identity_name_display = identity_name or f"Identity {identity_id[:8]}"
            identity_group = folium.FeatureGroup(name=identity_name_display, show=True)
            
            # Draw path if enabled (make it thicker and more visible for testing)
            if show_path:
                path_coords = [
                    [m['coordinates']['lat'], m['coordinates']['lng']]
                    for m in valid_movements
                ]
                folium.PolyLine(
                    path_coords,
                    color=color,
                    weight=6,  # Thicker line for visibility
                    opacity=0.8,  # More opaque
                    popup=folium.Popup(f"<b>{identity_name_display}</b><br>Movement Path ({len(path_coords)} points)", max_width=200),
                    tooltip=f"{identity_name_display} Path"
                ).add_to(identity_group)
            
            # Add start marker (larger and more visible for testing)
            start_coords = [valid_movements[0]['coordinates']['lat'], valid_movements[0]['coordinates']['lng']]
            start_time = valid_movements[0].get('timestamp', 'Unknown')
            if isinstance(start_time, str):
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                    start_time = dt.strftime('%Y-%m-%d %H:%M:%S')
                except:
                    pass
            
            folium.Marker(
                start_coords,
                icon=folium.Icon(color='green', icon='play', prefix='fa'),
                popup=folium.Popup(f"<b>{identity_name_display}</b><br>📍 START<br>Time: {start_time}", max_width=300),
                tooltip=f"{identity_name_display} - START"
            ).add_to(identity_group)
            
            # Add end marker (larger and more visible for testing)
            end_coords = [valid_movements[-1]['coordinates']['lat'], valid_movements[-1]['coordinates']['lng']]
            end_time = valid_movements[-1].get('timestamp', 'Unknown')
            if isinstance(end_time, str):
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                    end_time = dt.strftime('%Y-%m-%d %H:%M:%S')
                except:
                    pass
            
            folium.Marker(
                end_coords,
                icon=folium.Icon(color='red', icon='flag', prefix='fa'),
                popup=folium.Popup(f"<b>{identity_name_display}</b><br>🏁 END<br>Time: {end_time}", max_width=300),
                tooltip=f"{identity_name_display} - END"
            ).add_to(identity_group)
            
            # Add numbered markers at key points for visibility (every 2nd point)
            for i in range(1, len(valid_movements) - 1, 2):
                mid_coords = [valid_movements[i]['coordinates']['lat'], valid_movements[i]['coordinates']['lng']]
                point_time = valid_movements[i].get('timestamp', 'Unknown')
                if isinstance(point_time, str):
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(point_time.replace('Z', '+00:00'))
                        point_time = dt.strftime('%H:%M:%S')
                    except:
                        pass
                
                folium.CircleMarker(
                    mid_coords,
                    radius=10,
                    popup=folium.Popup(f"<b>{identity_name_display}</b><br>📍 Point {i+1}/{len(valid_movements)}<br>Time: {point_time}", max_width=300),
                    color=color,
                    fill=True,
                    fillColor=color,
                    fillOpacity=0.7,
                    weight=3
                ).add_to(identity_group)
            
            # Prepare timestamped data for animation using REAL timestamps
            timestamped_data = []
            timestamps = []
            
            for i, movement in enumerate(valid_movements):
                coords = movement['coordinates']
                timestamp = movement.get('timestamp', '')
                
                try:
                    # Parse timestamp - use REAL detection time
                    if timestamp:
                        if isinstance(timestamp, str):
                            dt = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
                        elif isinstance(timestamp, datetime):
                            dt = timestamp
                        else:
                            # Try to parse as ISO string
                            dt = datetime.fromisoformat(str(timestamp))
                    else:
                        # Fallback: use index as time if no timestamp
                        logger.warning(f"[ANIMATED] No timestamp for movement {i}, using fallback")
                        dt = datetime.now() + timedelta(seconds=i * 10)
                    
                    timestamps.append(dt)
                    
                    # Format time for display
                    time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
                    duration = movement.get('duration_at_location')
                    duration_str = f"Duration: {duration}s" if duration else ""
                    
                    timestamped_data.append({
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [coords['lng'], coords['lat']]  # GeoJSON uses [lng, lat]
                        },
                        "properties": {
                            "time": dt.isoformat(),
                            "popup": f"<b>{identity_name_display}</b><br>"
                                    f"Location: {movement.get('pipeline_name', 'Unknown')}<br>"
                                    f"Detected: {time_str}<br>"
                                    f"{duration_str}",
                            "icon": "user",
                            "iconstyle": {
                                "iconUrl": f"data:image/svg+xml;base64,{AnimatedMapRenderer._create_avatar_icon(color)}",
                                "iconSize": [60, 60],  # Much larger icon for testing visibility
                                "iconAnchor": [30, 30],
                                "popupAnchor": [0, -30]
                            },
                            "style": {
                                "color": color,
                                "weight": 3
                            }
                        }
                    })
                except Exception as e:
                    logger.warning(f"[ANIMATED] Error processing movement {i}: {e}")
                    continue
            
            if timestamped_data and timestamps:
                logger.info(f"[ANIMATED] Processing {len(timestamped_data)} timestamped points for avatar animation")
                
                # Calculate REAL time span from first to last detection
                first_time = min(timestamps)
                last_time = max(timestamps)
                total_duration = (last_time - first_time).total_seconds()
                
                logger.info(f"[ANIMATED] Time span: {first_time} to {last_time} (duration: {total_duration:.1f} seconds)")
                
                # Use actual time differences for animation
                # Period: Use 1 second per frame for smooth animation
                # Duration: Based on actual time span, but cap at reasonable limits
                if total_duration > 0:
                    # Use configured period and max duration
                    period_seconds = ANIMATION_PERIOD_SECONDS
                    # Duration: total time span, but limit to configured max
                    # For testing: if duration is very long, use a reasonable cap but log it
                    duration_seconds = min(total_duration, ANIMATION_MAX_DURATION_SECONDS)
                    
                    if total_duration > ANIMATION_MAX_DURATION_SECONDS:
                        logger.warning(f"[ANIMATED] Animation compressed: {total_duration:.1f}s real time → {duration_seconds}s animation ({total_duration/duration_seconds:.1f}x speed)")
                        logger.info(f"[ANIMATED] Tip: Increase MAP_ANIMATION_MAX_DURATION_SECONDS in config.py for slower animation")
                    
                    period_str = f"PT{period_seconds}S"
                    duration_str = f"PT{int(duration_seconds)}S"
                else:
                    # Fallback if all timestamps are the same
                    logger.warning(f"[ANIMATED] All timestamps are the same, using fallback duration")
                    period_str = f"PT{ANIMATION_PERIOD_SECONDS}S"
                    duration_str = "PT10S"
                
                # Create TimestampedGeoJson for animation
                timestamped_geojson = {
                    "type": "FeatureCollection",
                    "features": timestamped_data
                }
                
                logger.info(f"[ANIMATED] Creating TimestampedGeoJson with period={period_str}, duration={duration_str}")
                
                # Add animated marker plugin with REAL timestamps
                # Note: TimestampedGeoJson must be added directly to the map, not to a FeatureGroup
                try:
                    timestamped_plugin = plugins.TimestampedGeoJson(
                        timestamped_geojson,
                        period=period_str,  # Real time period (from config)
                        duration=duration_str,  # Based on actual time span (capped by config)
                        auto_play=True,
                        loop=True,  # Loop animation so user can see it multiple times
                        max_speed=ANIMATION_MAX_SPEED,
                        min_speed=ANIMATION_MIN_SPEED,
                        transition_time=ANIMATION_TRANSITION_TIME_MS,
                        add_last_point=True,
                        time_slider_drag_update=True
                    )
                    # Add directly to map object (not feature group)
                    # Note: TimestampedGeoJson is a plugin, not a FeatureGroup, so LayerControl won't manage it
                    timestamped_plugin.add_to(map_obj)
                    logger.info(f"[ANIMATED] TimestampedGeoJson plugin added successfully to map")
                except Exception as e:
                    logger.error(f"[ANIMATED] Error creating TimestampedGeoJson plugin: {e}", exc_info=True)
                    raise
            
            # Add FeatureGroup to map (this will be managed by LayerControl)
            # IMPORTANT: Add FeatureGroup AFTER plugin to ensure proper initialization
            identity_group.add_to(map_obj)
            logger.info(f"[ANIMATED] FeatureGroup '{identity_name_display}' added to map")
            return True
            
        except Exception as e:
            logger.error(f"[ANIMATED] Error adding animated avatar: {e}", exc_info=True)
            return False
    
    @staticmethod
    def _create_avatar_icon(color: str) -> str:
        """Create a large, highly visible SVG avatar icon with pulsing animation for testing."""
        import base64
        # Make it much larger and more visible for testing
        svg = f'''<svg width="60" height="60" xmlns="http://www.w3.org/2000/svg">
            <defs>
                <style>
                    @keyframes pulse {{
                        0%, 100% {{ opacity: 1; transform: scale(1); }}
                        50% {{ opacity: 0.8; transform: scale(1.1); }}
                    }}
                    .pulse-circle {{
                        animation: pulse 1s ease-in-out infinite;
                    }}
                </style>
            </defs>
            <!-- Outer glow circle -->
            <circle cx="30" cy="30" r="28" fill="{color}" opacity="0.3" class="pulse-circle"/>
            <!-- Main circle -->
            <circle cx="30" cy="30" r="25" fill="{color}" stroke="white" stroke-width="4" opacity="0.95"/>
            <!-- Inner highlight -->
            <circle cx="30" cy="30" r="20" fill="none" stroke="white" stroke-width="2" opacity="0.8"/>
            <!-- Face icon -->
            <circle cx="30" cy="22" r="6" fill="white"/>
            <path d="M 15 38 Q 30 32 45 38" stroke="white" stroke-width="3" fill="none"/>
            <!-- Center dot for visibility -->
            <circle cx="30" cy="30" r="3" fill="white"/>
        </svg>'''
        return base64.b64encode(svg.encode()).decode()
    
    @staticmethod
    def add_multi_identity_tracking(
        map_obj,
        identities_data: List[Dict],
        show_paths: bool = True,
        show_avatars: bool = True
    ) -> bool:
        """
        Add multiple identities with animated avatars on the same map.
        Uses REAL timestamps from surveillance system to show synchronized movement.
        Identities appear at their actual detection times, allowing you to see:
        - When identities appear together (same time)
        - Time differences between appearances
        - Synchronized timeline playback
        
        Args:
            map_obj: Folium map object
            identities_data: List of dicts with keys:
                - identity_id: Unique identifier
                - identity_name: Display name
                - movements: List of movement data with 'timestamp' field (REAL detection times)
            show_paths: Whether to show path lines
            show_avatars: Whether to show animated avatars (uses real timestamps)
            
        Returns:
            True if successful, False otherwise
        """
        if not FOLIUM_AVAILABLE:
            return False
        
        try:
            success_count = 0
            for idx, identity_data in enumerate(identities_data):
                identity_id = identity_data.get('identity_id', f'identity_{idx}')
                identity_name = identity_data.get('identity_name')
                movements = identity_data.get('movements', [])
                
                if show_avatars:
                    if AnimatedMapRenderer.add_animated_avatar(
                        map_obj,
                        movements,
                        identity_id,
                        identity_name,
                        color_index=idx,
                        show_path=show_paths
                    ):
                        success_count += 1
                elif show_paths:
                    # Just show paths without animation
                    valid_movements = [
                        m for m in movements
                        if m.get('coordinates') and m['coordinates'].get('lat') and m['coordinates'].get('lng')
                    ]
                    
                    if len(valid_movements) >= 2:
                        color = AnimatedMapRenderer.AVATAR_COLORS[idx % len(AnimatedMapRenderer.AVATAR_COLORS)]
                        path_coords = [
                            [m['coordinates']['lat'], m['coordinates']['lng']]
                            for m in valid_movements
                        ]
                        
                        identity_name_display = identity_name or f"Identity {identity_id[:8]}"
                        identity_group = folium.FeatureGroup(name=identity_name_display, show=True)
                        
                        folium.PolyLine(
                            path_coords,
                            color=color,
                            weight=4,
                            opacity=0.6,
                            popup=folium.Popup(f"<b>{identity_name_display}</b><br>Movement Path", max_width=200),
                            tooltip=f"{identity_name_display} Path"
                        ).add_to(identity_group)
                        
                        # Add markers
                        start_coords = [valid_movements[0]['coordinates']['lat'], valid_movements[0]['coordinates']['lng']]
                        end_coords = [valid_movements[-1]['coordinates']['lat'], valid_movements[-1]['coordinates']['lng']]
                        
                        folium.Marker(
                            start_coords,
                            icon=folium.Icon(color='green', icon='play', prefix='fa'),
                            popup=folium.Popup(f"<b>{identity_name_display}</b><br>Start", max_width=200)
                        ).add_to(identity_group)
                        
                        folium.Marker(
                            end_coords,
                            icon=folium.Icon(color='red', icon='flag', prefix='fa'),
                            popup=folium.Popup(f"<b>{identity_name_display}</b><br>End", max_width=200)
                        ).add_to(identity_group)
                        
                        identity_group.add_to(map_obj)
                        success_count += 1
            
            # Add co-appearance indicators (when identities appear together)
            if show_avatars and CO_APPEARANCE_ENABLED and len(all_movements_by_time) > 1:
                AnimatedMapRenderer._add_co_appearance_indicators(map_obj, all_movements_by_time)
            
            return success_count > 0
            
        except Exception as e:
            logger.error(f"[ANIMATED] Error adding multi-identity tracking: {e}", exc_info=True)
            return False
    
    @staticmethod
    def _add_co_appearance_indicators(map_obj, movements_by_time: List[Dict]):
        """Add indicators when multiple identities appear together (same time/location)."""
        if not FOLIUM_AVAILABLE:
            return
        
        try:
            from collections import defaultdict
            
            # Group by time (configured window) and location (configured distance)
            co_appearance_group = folium.FeatureGroup(name="Co-Appearances", show=False)
            co_appearances_found = False
            
            # Group movements by rounded time (configured window)
            time_window = CO_APPEARANCE_TIME_WINDOW_SECONDS
            time_groups = defaultdict(list)
            for item in movements_by_time:
                time_key = int(item['timestamp'].timestamp() // time_window) * time_window
                time_groups[time_key].append(item)
            
            # Find co-appearances
            for time_key, items_at_time in time_groups.items():
                if len(items_at_time) > 1:
                    # Multiple identities at similar time - check locations
                    for i, item1 in enumerate(items_at_time):
                        for item2 in items_at_time[i+1:]:
                            coords1 = item1['movement'].get('coordinates', {})
                            coords2 = item2['movement'].get('coordinates', {})
                            
                            if not (coords1.get('lat') and coords1.get('lng') and 
                                   coords2.get('lat') and coords2.get('lng')):
                                continue
                            
                            # Calculate distance (simple approximation: ~111km per degree)
                            lat_diff = abs(coords1['lat'] - coords2['lat'])
                            lng_diff = abs(coords1['lng'] - coords2['lng'])
                            # Convert meters to degrees: 1 degree ≈ 111km, so distance_m/111000
                            distance_threshold = CO_APPEARANCE_DISTANCE_METERS / 111000.0
                            if lat_diff < distance_threshold and lng_diff < distance_threshold:
                                # Same location - add co-appearance marker
                                name1 = item1['identity_name'] or f"ID {item1['identity_id'][:8]}"
                                name2 = item2['identity_name'] or f"ID {item2['identity_id'][:8]}"
                                time_str = item1['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
                                
                                folium.Marker(
                                    [coords1['lat'], coords1['lng']],
                                    icon=folium.Icon(color='purple', icon='users', prefix='fa'),
                                    popup=folium.Popup(
                                        f"<b>👥 Co-Appearance Detected</b><br>"
                                        f"<b>{name1}</b> & <b>{name2}</b><br>"
                                        f"Time: {time_str}<br>"
                                        f"Location: {item1['movement'].get('pipeline_name', 'Unknown')}",
                                        max_width=300
                                    ),
                                    tooltip=f"{name1} & {name2} together at {time_str}"
                                ).add_to(co_appearance_group)
                                co_appearances_found = True
            
            if co_appearances_found:
                co_appearance_group.add_to(map_obj)
                logger.info(f"[ANIMATED] Added {len(co_appearance_group._children)} co-appearance indicators")
                
        except Exception as e:
            logger.warning(f"[ANIMATED] Error adding co-appearance indicators: {e}")

