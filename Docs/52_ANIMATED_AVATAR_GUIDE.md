# Chapter 9.1: Animated Avatar Movement Guide

> **⚠ Superseded in part (2026-08-17).** The sections below that describe a
> server-side Folium/Leaflet map renderer document code that has been REMOVED.
> `GET /api/identities/{id}/map`, `/map/geojson` and `/api/map/stats` return 404;
> `folium` is not a dependency of this project and must not be installed. Maps
> are drawn in the browser by MapLibre GL JS over a local Martin tile server —
> see [`46_MAP_SERVICE_GUIDE.md`](46_MAP_SERVICE_GUIDE.md). Everything else in
> this document still applies.


**Version:** 5.0.0  
**Last Updated:** January 2025

---

## Overview

The animated avatar feature provides real-time visualization of tracked person movement on maps, similar to central agency tracking systems. Avatars move along routes based on **actual detection timestamps** from your surveillance system, allowing you to see:

- **Real-time movement**: Avatars appear at exact detection times
- **Synchronized tracking**: Multiple identities tracked simultaneously with synchronized timeline
- **Co-appearance detection**: Automatic detection when identities appear together
- **Time-based animation**: Movement respects actual time differences between detections

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [How It Works](#how-it-works)
3. [Initialization](#initialization)
4. [Configuration](#configuration)
5. [Single Identity Tracking](#single-identity-tracking)
6. [Multi-Identity Tracking](#multi-identity-tracking)
7. [Real-Time Timestamps](#real-time-timestamps)
8. [Co-Appearance Detection](#co-appearance-detection)
9. [Troubleshooting](#troubleshooting)

---

## Quick Start

### Enable Animated Avatar via Frontend

1. Navigate to **Intelligence Analysis** → **Cross-Camera Tracking**
2. Select an identity from the dropdown
3. Click **"Track Movement"** button
4. Click **"Map"** button to switch to map view
5. In the **Map Settings** panel, check **"Animated Avatar"**
6. Click **"Refresh Map"** or the map will auto-refresh

### Enable via API

```bash
GET /api/identities/{identity_id}/map?show_animated_avatar=true
```

### Enable via Configuration

Add to your `.env` file:
```env
MAP_SHOW_ANIMATED_AVATAR=true
```

---

## How It Works

### Movement Animation

The animated avatar system:

1. **Collects Movement Data**: Retrieves all detections with coordinates and timestamps
2. **Parses Real Timestamps**: Uses actual detection times from surveillance system
3. **Calculates Time Span**: Determines total duration from first to last detection
4. **Creates Timeline**: Generates animation timeline based on real time differences
5. **Renders Avatar**: Displays animated avatar icon that moves along the route
6. **Synchronizes Playback**: All identities use the same timeline for synchronized viewing

### Avatar Movement

- **Start Position**: Green "play" icon marker at first detection
- **End Position**: Red "flag" icon marker at last detection
- **Movement Path**: Colored line showing the route
- **Animated Icon**: Colored avatar icon that moves along the path
- **Timeline Controls**: Play/pause, speed control, time slider

---

## Initialization

### Frontend Initialization

The animated avatar is initialized when:

1. **Map View is Activated**: Clicking the "Map" button in Cross-Camera Tracking tab
2. **Checkbox is Checked**: "Animated Avatar" checkbox in Map Settings panel
3. **Map Refreshes**: Automatically refreshes when settings change

**JavaScript Flow:**
```javascript
// User checks "Animated Avatar" checkbox
document.getElementById('map-show-animated-avatar').checked = true;

// Event listener triggers map refresh
loadBackendMap();

// Builds API URL with parameter
url += `&show_animated_avatar=true`;

// Backend generates map with animated avatar
```

### Backend Initialization

**File**: `backend/core/animated_map_features.py`

```python
from backend.core.animated_map_features import AnimatedMapRenderer

# Initialize animated avatar for single identity
AnimatedMapRenderer.add_animated_avatar(
    map_obj=folium_map,
    movements=movement_data,  # List of movements with coordinates and timestamps
    identity_id="identity-uuid",
    identity_name="John Doe",
    color_index=0,  # Color for this identity
    show_path=True  # Show route line
)
```

### API Endpoint

**Endpoint**: `GET /api/identities/{identity_id}/map`

**Parameters**:
- `show_animated_avatar` (bool): Enable animated avatar (default: false)
- `map_style` (str): Map style - light, dark, satellite, terrain
- `show_routes` (bool): Show route lines (default: true)
- `enable_security_features` (bool): Enable security features (default: true)

**Example**:
```bash
curl -X GET "http://localhost:8000/api/identities/abc123/map?show_animated_avatar=true&map_style=light" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## Configuration

### Configuration Variables

All settings are in `config.py` and can be overridden via `.env`:

```python
# Animated Avatar Default
MAP_SHOW_ANIMATED_AVATAR: bool = False  # Enable by default

# Animation Timing
MAP_ANIMATION_PERIOD_SECONDS: int = 1  # Seconds of real time per frame
MAP_ANIMATION_MAX_DURATION_SECONDS: int = 600  # Max 10 minutes

# Playback Speed
MAP_ANIMATION_MIN_SPEED: float = 0.5  # Minimum speed (0.5x)
MAP_ANIMATION_MAX_SPEED: float = 10.0  # Maximum speed (10x)

# Animation Smoothness
MAP_ANIMATION_TRANSITION_TIME_MS: int = 300  # Transition between frames

# Co-Appearance Detection
MAP_CO_APPEARANCE_TIME_WINDOW_SECONDS: int = 10  # Time window (10 seconds)
MAP_CO_APPEARANCE_DISTANCE_METERS: float = 100.0  # Distance threshold (100m)
MAP_CO_APPEARANCE_ENABLED: bool = True  # Enable co-appearance detection
```

### Environment Variables

Add to `.env`:
```env
# Enable animated avatar by default
MAP_SHOW_ANIMATED_AVATAR=true

# Adjust animation speed (1 second real time = 1 frame)
MAP_ANIMATION_PERIOD_SECONDS=1

# Maximum animation duration (10 minutes)
MAP_ANIMATION_MAX_DURATION_SECONDS=600

# Playback speed range
MAP_ANIMATION_MIN_SPEED=0.5
MAP_ANIMATION_MAX_SPEED=10.0

# Co-appearance settings
MAP_CO_APPEARANCE_TIME_WINDOW_SECONDS=10
MAP_CO_APPEARANCE_DISTANCE_METERS=100.0
MAP_CO_APPEARANCE_ENABLED=true
```

---

## Single Identity Tracking

### Basic Usage

**Frontend**:
1. Select identity
2. Click "Map" button
3. Check "Animated Avatar"
4. Map refreshes with animated avatar

**API**:
```python
GET /api/identities/{identity_id}/map?show_animated_avatar=true
```

### What You'll See

- **Animated Avatar**: Colored circle icon moving along the route
- **Start Marker**: Green play icon at first detection
- **End Marker**: Red flag icon at last detection
- **Route Line**: Colored line showing the path
- **Timeline Controls**: Play/pause, speed, time slider
- **Time Display**: Actual detection times in popups

### Avatar Colors

Each identity gets a unique color from the palette:
- Identity 1: Green (#00ff96)
- Identity 2: Red (#ff6b6b)
- Identity 3: Cyan (#4ecdc4)
- Identity 4: Yellow (#ffe66d)
- ... and more

---

## Multi-Identity Tracking

### Overview

Track multiple identities simultaneously on the same map with synchronized timeline.

### How It Works

1. **Unified Timeline**: All identities use the same time-based animation
2. **Synchronized Playback**: Avatars appear at their actual detection times
3. **Color Coding**: Each identity has a unique color
4. **Co-Appearance Detection**: Automatically detects when identities appear together

### API Usage

**Multi-Identity Endpoint** (Future):
```python
POST /api/map/multi-identity
{
    "identities": [
        {
            "identity_id": "uuid-1",
            "identity_name": "Person A",
            "movements": [...]
        },
        {
            "identity_id": "uuid-2",
            "identity_name": "Person B",
            "movements": [...]
        }
    ],
    "show_animated_avatar": true,
    "show_paths": true
}
```

### Current Implementation

Currently, multi-identity tracking is available through the map service:

```python
from backend.core.animated_map_features import AnimatedMapRenderer

identities_data = [
    {
        'identity_id': 'uuid-1',
        'identity_name': 'Person A',
        'movements': [
            {
                'coordinates': {'lat': 40.7128, 'lng': -74.0060},
                'timestamp': '2025-01-11T10:00:00',
                'pipeline_name': 'Camera 1'
            },
            # ... more movements
        ]
    },
    {
        'identity_id': 'uuid-2',
        'identity_name': 'Person B',
        'movements': [
            # ... movements
        ]
    }
]

AnimatedMapRenderer.add_multi_identity_tracking(
    map_obj=folium_map,
    identities_data=identities_data,
    show_paths=True,
    show_avatars=True
)
```

---

## Real-Time Timestamps

### How Timestamps Work

The animated avatar system uses **real detection timestamps** from your surveillance system:

1. **Timestamp Source**: `movement.timestamp` from database (from `app.start_time`)
2. **Time Parsing**: Handles ISO format strings and datetime objects
3. **Time Calculation**: Calculates actual time differences between detections
4. **Animation Timing**: Animation period based on real time differences

### Example Timeline

**Person A Detections**:
- 10:00:00 - Camera 1 (Start)
- 10:05:30 - Camera 2 (5 minutes 30 seconds later)
- 10:12:15 - Camera 3 (6 minutes 45 seconds later)

**Person B Detections**:
- 10:00:05 - Camera 1 (5 seconds after Person A - **together!**)
- 10:08:00 - Camera 3 (7 minutes 55 seconds later)

**Animation Behavior**:
- Avatar A appears at 10:00:00, moves to Camera 2 at 10:05:30, then Camera 3 at 10:12:15
- Avatar B appears at 10:00:05 (almost together with A), then Camera 3 at 10:08:00
- Timeline shows actual 12+ minute span
- Co-appearance marker at Camera 1 showing they appeared together

### Timestamp Format

The system accepts timestamps in multiple formats:

```python
# ISO format string
timestamp = "2025-01-11T10:00:00"
timestamp = "2025-01-11T10:00:00Z"  # UTC
timestamp = "2025-01-11T10:00:00+00:00"  # With timezone

# Datetime object
from datetime import datetime
timestamp = datetime(2025, 1, 11, 10, 0, 0)
```

### Time-Based Animation

- **Period**: 1 second of real time per animation frame (configurable)
- **Duration**: Based on actual time span (capped at 10 minutes by default)
- **Speed Control**: Adjustable from 0.5x to 10x speed
- **Timeline Slider**: Shows actual time range and allows scrubbing

---

## Co-Appearance Detection

### Overview

Automatically detects when multiple identities appear at the same time and location.

### Detection Criteria

1. **Time Window**: Identities detected within configured time window (default: 10 seconds)
2. **Distance**: Identities at same location (default: within 100 meters)
3. **Visual Indicator**: Purple "users" icon marker at co-appearance location

### Configuration

```python
# Time window for co-appearance (seconds)
MAP_CO_APPEARANCE_TIME_WINDOW_SECONDS = 10

# Distance threshold (meters)
MAP_CO_APPEARANCE_DISTANCE_METERS = 100.0

# Enable/disable co-appearance detection
MAP_CO_APPEARANCE_ENABLED = True
```

### Example

**Scenario**:
- Person A detected at Camera 1 at 10:00:00
- Person B detected at Camera 1 at 10:00:05 (5 seconds later, same location)

**Result**:
- Co-appearance marker appears at Camera 1
- Popup shows: "👥 Co-Appearance Detected - Person A & Person B - Time: 10:00:00"
- Marker visible in "Co-Appearances" layer (toggleable)

### Co-Appearance Layer

The co-appearance markers are added to a separate layer:
- **Layer Name**: "Co-Appearances"
- **Default Visibility**: Hidden (can be toggled in layer control)
- **Icon**: Purple "users" icon
- **Popup**: Shows both identities and detection time

---

## Avatar Movement Details

### Movement Path

The avatar follows the route defined by detection coordinates:

1. **Start Point**: First detection location (green marker)
2. **Intermediate Points**: All detection locations in chronological order
3. **End Point**: Last detection location (red marker)
4. **Route Line**: Colored line connecting all points
5. **Avatar Movement**: Icon moves along the route based on timestamps

### Animation Behavior

- **Smooth Movement**: Avatar transitions between points
- **Time-Based**: Movement speed based on actual time differences
- **Pause at Locations**: Avatar pauses at each detection point
- **Duration Display**: Shows how long person stayed at each location

### Visual Elements

**Avatar Icon**:
- Colored circle with face icon
- Size: 30x30 pixels
- Unique color per identity
- Moves along route

**Markers**:
- **Start**: Green play icon (▶)
- **End**: Red flag icon (🏁)
- **Co-Appearance**: Purple users icon (👥)

**Route Line**:
- Colored line matching avatar color
- Weight: 4 pixels
- Opacity: 0.6
- Connects all detection points

---

## Timeline Controls

### Available Controls

When animated avatar is enabled, the map includes:

1. **Play/Pause Button**: Start or pause animation
2. **Speed Control**: Adjust playback speed (0.5x to 10x)
3. **Time Slider**: Scrub through timeline
4. **Time Display**: Shows current time in animation
5. **Loop Toggle**: Repeat animation (optional)

### Timeline Features

- **Auto-Play**: Animation starts automatically (configurable)
- **Time Range**: Shows actual time span of detections
- **Frame-by-Frame**: Can step through individual detections
- **Speed Adjustment**: Slow down to see details or speed up for overview

---

## Code Examples

### Python: Single Identity

```python
from backend.core.map_service import map_service

# Generate map with animated avatar
map_html = await map_service.generate_folium_map(
    tracks=tracking_data,
    identity_name="John Doe",
    map_style="light",
    show_animated_avatar=True,  # Enable animated avatar
    show_routes=True,
    enable_security_features=True
)
```

### Python: Multi-Identity

```python
from backend.core.animated_map_features import AnimatedMapRenderer
import folium

# Create map
m = folium.Map(location=[40.7128, -74.0060], zoom_start=13)

# Prepare multi-identity data
identities_data = [
    {
        'identity_id': 'person-a-uuid',
        'identity_name': 'Person A',
        'movements': [
            {
                'coordinates': {'lat': 40.7128, 'lng': -74.0060},
                'timestamp': '2025-01-11T10:00:00',
                'pipeline_name': 'Camera 1'
            },
            # ... more movements
        ]
    },
    {
        'identity_id': 'person-b-uuid',
        'identity_name': 'Person B',
        'movements': [
            # ... movements
        ]
    }
]

# Add multi-identity tracking
AnimatedMapRenderer.add_multi_identity_tracking(
    map_obj=m,
    identities_data=identities_data,
    show_paths=True,
    show_avatars=True
)

# Get HTML
map_html = m._repr_html_()
```

### JavaScript: Frontend Integration

```javascript
// Load map with animated avatar
async function loadMapWithAvatar(identityId) {
    const url = `/api/identities/${identityId}/map?` +
        `show_animated_avatar=true&` +
        `map_style=light&` +
        `show_routes=true`;
    
    const response = await fetch(url, {
        credentials: 'include',
        headers: { 'Accept': 'text/html' }
    });
    
    if (response.ok) {
        const mapHtml = await response.text();
        document.getElementById('map-container').innerHTML = mapHtml;
    }
}
```

---

## Troubleshooting

### Avatar Not Moving

**Problem**: Avatar appears but doesn't move

**Solutions**:
1. Check that movements have valid `timestamp` fields
2. Verify timestamps are in correct format (ISO string or datetime)
3. Ensure at least 2 movements with coordinates exist
4. Check browser console for JavaScript errors

### Avatar Not Appearing

**Problem**: No avatar visible on map

**Solutions**:
1. Verify `show_animated_avatar=true` is in API request
2. Check that checkbox is checked in frontend
3. Ensure movements have valid coordinates (`lat` and `lng`)
4. Check backend logs for errors

### Timeline Not Synchronized

**Problem**: Multiple identities not synchronized

**Solutions**:
1. Ensure all movements have real timestamps (not fallback times)
2. Check that timestamps are from same timezone
3. Verify `MAP_ANIMATION_PERIOD_SECONDS` is consistent
4. Use unified timeline for multi-identity (automatic)

### Co-Appearances Not Detected

**Problem**: Co-appearance markers not showing

**Solutions**:
1. Check `MAP_CO_APPEARANCE_ENABLED=true` in config
2. Verify time window is appropriate (default: 10 seconds)
3. Check distance threshold (default: 100 meters)
4. Ensure "Co-Appearances" layer is enabled in layer control
5. Verify identities actually appeared at same time/location

### Performance Issues

**Problem**: Map generation is slow

**Solutions**:
1. Reduce `MAP_ANIMATION_MAX_DURATION_SECONDS` (default: 600)
2. Limit number of movements per identity
3. Use caching (`MAP_CACHE_ENABLED=true`)
4. Reduce `MAP_MAX_COORDINATES` if too many points

---

## Best Practices

### 1. Timestamp Quality

- **Always use real timestamps**: Don't use fallback/index-based times
- **Consistent timezone**: Ensure all timestamps use same timezone
- **Precise times**: Use detection times, not rounded/approximate times

### 2. Movement Data

- **Valid coordinates**: Ensure all movements have `lat` and `lng`
- **Chronological order**: Movements should be sorted by timestamp
- **Reasonable density**: Too many points can slow animation

### 3. Multi-Identity Tracking

- **Synchronized timeline**: All identities use same time reference
- **Unique colors**: Each identity gets distinct color automatically
- **Co-appearance analysis**: Enable to detect relationships

### 4. Performance

- **Cache maps**: Enable caching for frequently viewed identities
- **Limit duration**: Cap animation duration for very long tracks
- **Optimize coordinates**: Reduce coordinate count if needed

---

## Advanced Features

### Custom Avatar Icons

The system uses SVG-based avatar icons. To customize:

```python
# In animated_map_features.py
@staticmethod
def _create_avatar_icon(color: str) -> str:
    """Create custom SVG avatar icon."""
    svg = f'''<svg width="30" height="30" xmlns="http://www.w3.org/2000/svg">
        <circle cx="15" cy="15" r="12" fill="{color}" stroke="white" stroke-width="2"/>
        <!-- Customize icon here -->
    </svg>'''
    return base64.b64encode(svg.encode()).decode()
```

### Animation Speed Control

Users can adjust playback speed:
- **Minimum**: 0.5x (slow, detailed viewing)
- **Maximum**: 10x (fast, overview)
- **Default**: 1x (real-time)

### Timeline Scrubbing

The timeline slider allows:
- **Jump to time**: Click anywhere on timeline
- **Frame-by-frame**: Use arrow keys (if implemented)
- **Time display**: Shows current time in animation

---

## Integration with Security Features

The animated avatar works seamlessly with security intelligence features:

- **Security Zones**: Avatars move through security zones
- **Threat Indicators**: Co-appearances can trigger alerts
- **Pattern Detection**: Movement patterns visible in animation
- **Risk Heatmap**: Avatar movement overlaid on risk visualization

---

## API Reference

### Endpoint: Get Map with Animated Avatar

```
GET /api/identities/{identity_id}/map
```

**Query Parameters**:
- `show_animated_avatar` (bool): Enable animated avatar
- `map_style` (str): Map style (light, dark, satellite, terrain)
- `show_routes` (bool): Show route lines
- `enable_security_features` (bool): Enable security features
- `date` (str, optional): Specific date (YYYY-MM-DD)
- `days_back` (int): Days to analyze (default: 7)

**Response**: HTML page with embedded interactive map

**Example**:
```bash
curl -X GET \
  "http://localhost:8000/api/identities/abc123/map?show_animated_avatar=true&map_style=light" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## Summary

The animated avatar feature provides:

✅ **Real-time movement visualization** based on actual detection timestamps  
✅ **Synchronized multi-identity tracking** with unified timeline  
✅ **Co-appearance detection** for relationship analysis  
✅ **Configurable animation** with speed and duration controls  
✅ **Professional visualization** similar to central agency systems  

The avatar moves along routes respecting real detection times, allowing you to see exactly when and where people were detected, whether they appeared together, and the time differences between appearances.

---

**See Also**:
- [Map Service Guide](46_MAP_SERVICE_GUIDE.md) - General map service documentation
- [Security Intelligence Features](48_SECURITY_INTELLIGENCE_MAP_FEATURES.md) - Security features
- [API Documentation](50_API_DOCUMENTATION.md) - Complete API reference


