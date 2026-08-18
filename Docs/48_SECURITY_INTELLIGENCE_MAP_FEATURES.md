# Chapter 8.3: Security Intelligence Map Features

**Version:** 5.0.0  
**Last Updated:** January 2025

---

## Overview

The map service includes advanced security intelligence features designed for security operations, threat analysis, and surveillance systems. These features provide comprehensive threat visualization and pattern analysis capabilities.

---

## Security Features

### 🔒 1. Threat Level Visualization

**Purpose**: Visualize threat levels at each location

**Features**:
- Color-coded markers based on threat level (Red: Critical, Orange: High, Yellow: Medium)
- Threat indicators show watchlist matches, alerts, and suspicious patterns
- Risk score calculation (0-100) with risk level classification

**Usage**:
```python
# Automatically enabled when watchlist_matches are provided
watchlist_matches = [
    {
        'list_name': 'High Risk',
        'alert_level': 'critical',
        'priority': 'high'
    }
]
```

### 🚨 2. Watchlist Integration

**Purpose**: Highlight identities on watchlists

**Features**:
- Automatic detection of watchlist matches
- Visual indicators for different alert levels
- Integration with existing watchlist service

**Visual Indicators**:
- **Critical**: Red exclamation triangle
- **High**: Orange exclamation circle
- **Medium/Low**: Yellow info sign

**API Integration**:
```python
# Automatically fetched in route handler
if enable_security_features:
    watchlist_matches = await watchlist_service.get_identity_watchlists(db, identity_id)
```

### 📍 3. Security Zones

**Purpose**: Define and visualize security zones

**Zone Types**:
- **Restricted**: High-security areas (Red)
- **High Security**: Monitored zones (Orange)
- **Monitored**: Areas under surveillance (Yellow)
- **Safe**: Low-risk zones (Green)

**Usage**:
```python
security_zones = [
    {
        'name': 'Main Entrance',
        'coordinates': [[lat1, lng1], [lat2, lng2], ...],
        'zone_type': 'restricted',
        'risk_level': 8,
        'description': 'Restricted access area'
    }
]
```

### 🔍 4. Pattern Detection

**Purpose**: Automatically detect suspicious movement patterns

**Detected Patterns**:

#### Loitering
- **Definition**: Staying in a small area (50m radius) for extended time (5+ minutes)
- **Severity**: Based on duration (1 point per 5 minutes, max 10)
- **Visual**: Purple circle overlay

#### Backtracking
- **Definition**: Returning to previously visited locations
- **Severity**: Based on number of movements between visits
- **Visual**: Red dashed line

#### Rapid Movement
- **Definition**: Moving at suspicious speeds (>100 km/h)
- **Severity**: Based on speed over threshold
- **Visual**: Orange dashed line

**Configuration**:
```python
detect_patterns=True  # Enable pattern detection (default from config.py)
```

**Implementation**: `backend/core/security_map_features.py::SecurityMapAnalyzer`

### 🗺️ 5. Risk Heatmap

**Purpose**: Visualize risk distribution across the map

**Features**:
- Color gradient: Blue (low) → Green → Orange → Red (high)
- Weighted by risk score
- Overlay on base map

**Usage**:
```python
show_risk_heatmap=True  # Enable risk heatmap (default from config.py)
```

### ⏱️ 6. Timeline Playback

**Purpose**: Animate movement over time

**Features**:
- Sequential marker display
- Time-based visualization
- Playback controls (future enhancement)

**Usage**:
```python
show_timeline=True  # Enable timeline control (default: false)
```

### 📊 7. Risk Scoring

**Purpose**: Calculate overall risk score for tracking data

**Risk Factors**:
- **Base Risk**: 1 point
- **Watchlist Risk**: 1-5 points based on alert level
- **Pattern Risk**: Based on detected patterns (severity)
- **Zone Risk**: Based on zones visited (risk level)
- **Speed Risk**: Based on rapid movement

**Risk Levels**:
- **Critical**: 20+ points (Red)
- **High**: 15-19 points (Orange)
- **Medium**: 10-14 points (Yellow)
- **Low**: <10 points (Green)

**Display**: Shown in map legend with color coding

**Implementation**: `backend/core/security_map_features.py::SecurityMapAnalyzer.calculate_risk_score()`

---

## API Usage

### Enhanced Map Endpoint

```http
GET /api/identities/{identity_id}/map?enable_security_features=true&detect_patterns=true&show_risk_heatmap=true
```

**Query Parameters**:
- `enable_security_features` (default: true, from `MAP_ENABLE_SECURITY_FEATURES`): Enable all security features
- `detect_patterns` (default: true, from `MAP_DETECT_PATTERNS`): Detect movement patterns
- `show_risk_heatmap` (default: true, from `MAP_SHOW_RISK_HEATMAP`): Show risk heatmap
- `show_timeline` (default: false, from `MAP_SHOW_TIMELINE`): Show timeline playback

**Configuration**: All defaults can be set in `config.py` and `.env`:
```bash
# MAP_ENABLE_SECURITY_FEATURES=true   # NOT A SETTING - query parameter, see note below
# MAP_DETECT_PATTERNS=true   # NOT A SETTING - query parameter, see note below
# MAP_SHOW_RISK_HEATMAP=true   # NOT A SETTING - query parameter, see note below
# MAP_SHOW_TIMELINE=false   # NOT A SETTING - query parameter, see note below
```


> **These four are query parameters, not settings.** `enable_security_features`,
> `detect_patterns`, `show_risk_heatmap` and `show_timeline` are passed per
> request to `GET /api/identities/{identity_id}/map`, each defaulting to
> `false`. There is no `MAP_ENABLE_SECURITY_FEATURES` / `MAP_DETECT_PATTERNS` /
> `MAP_SHOW_RISK_HEATMAP` / `MAP_SHOW_TIMELINE` environment variable — setting
> one in `.env` does nothing at all. The real `MAP_*` settings are listed in
> [`36_CONFIGURATION_GUIDE.md`](36_CONFIGURATION_GUIDE.md).
>
> ```bash
> curl -G "http://localhost/api/identities/$ID/map" \
>   -H "Authorization: Bearer $TOKEN" \
>   -d days_back=7 -d map_style=light \
>   -d enable_security_features=true -d detect_patterns=true \
>   -d show_risk_heatmap=true -d show_timeline=false
> ```

### Example Request

```javascript
const url = `/api/identities/${identityId}/map?` +
    `days_back=7&` +
    `map_style=dark&` +
    `enable_security_features=true&` +
    `detect_patterns=true&` +
    `show_risk_heatmap=true&` +
    `show_timeline=false`;

const response = await fetch(url, { credentials: 'include' });
const mapHtml = await response.text();
```

---

## Visual Elements

### Map Legend

The map includes a security intelligence legend showing:
- **Identity Name**: With security icon
- **Risk Level**: Color-coded (Critical/High/Medium/Low)
- **Risk Score**: 0-100 scale
- **Total Locations**: Number of tracked points

### Color Coding

- **Red**: Critical threats, restricted zones, backtracking
- **Orange**: High threats, rapid movement, high-security zones
- **Yellow**: Medium threats, monitored zones
- **Green**: Low risk, safe zones, start points
- **Blue**: Normal movement, timeline markers
- **Purple**: Loitering patterns

---

## Security Intelligence Dashboard

### Risk Assessment

The map automatically calculates and displays:
1. **Overall Risk Score**: 0-100
2. **Risk Level**: Critical/High/Medium/Low
3. **Risk Breakdown**: By factor (watchlist, patterns, zones, speed)

### Pattern Analysis

Detected patterns are displayed with:
- Pattern type and description
- Severity score (1-10)
- Time range
- Visual overlay

### Threat Indicators

Watchlist matches and alerts shown as:
- Threat level (1-10)
- Threat type (watchlist, alert, pattern)
- Description
- Timestamp

---

## Implementation Details

### Pattern Detection Algorithm

**File**: `backend/core/security_map_features.py`

**Classes**:
- `SecurityMapAnalyzer`: Pattern detection and risk calculation
- `SecurityMapRenderer`: Map visualization and rendering

**Methods**:
- `detect_loitering()`: Haversine distance calculation for loitering
- `detect_backtracking()`: Location history tracking
- `detect_rapid_movement()`: Speed calculation using timestamps
- `calculate_risk_score()`: Multi-factor risk calculation

### Data Flow

1. **Route Handler** receives request with security feature flags
2. **Intelligence Service** fetches tracking data from database
3. **Watchlist Service** fetches watchlist matches
4. **Map Service** calls security features:
   - `SecurityMapAnalyzer` detects patterns
   - `SecurityMapRenderer` adds visualizations
5. **MapLibre** draws the overlays in the browser from `/api/identities/{id}/map-data`

---

## Configuration

### Pattern Detection Settings

Pattern detection uses configurable thresholds (currently in code, can be moved to config.py):

```python
# In security_map_features.py
LOITERING_RADIUS_METERS = 50
LOITERING_MIN_DURATION_MINUTES = 5
RAPID_MOVEMENT_THRESHOLD_KMH = 100
```

### Risk Calculation

Risk weights can be adjusted in `calculate_risk_score()`:
```python
risk_factors = {
    'base_risk': 1,
    'watchlist_risk': 0,  # 1-5 based on alert level
    'pattern_risk': 0,    # Based on pattern severity
    'zone_risk': 0,       # Based on zone risk level
    'speed_risk': 0       # Based on speed over threshold
}
```

---

## Performance

### Optimization

- Pattern detection runs only when `detect_patterns=True`
- Heatmap generation optimized for large datasets
- Caching includes security feature parameters
- Memory limits enforced (from `MAP_MAX_COORDINATES`)

### Limits

- Maximum 10,000 coordinates for pattern detection (from `MAP_MAX_COORDINATES`)
- Pattern detection timeout: 30 seconds (from `MAP_GENERATION_TIMEOUT`)
- Heatmap data points limited to prevent memory issues

---

## Use Cases

### 1. Threat Assessment
- Identify high-risk individuals
- Visualize threat distribution
- Assess security zones

### 2. Pattern Analysis
- Detect suspicious behavior
- Identify loitering
- Track backtracking patterns

### 3. Incident Investigation
- Reconstruct movement paths
- Identify key locations
- Analyze timeline

### 4. Security Planning
- Define security zones
- Assess coverage areas
- Plan response routes

---

## Troubleshooting

### Patterns Not Detected

1. Check coordinate data quality
2. Verify timestamp accuracy
3. Adjust detection thresholds
4. Check logs: `[SECURITY]` prefix

### Risk Score Too High/Low

1. Review risk factor weights
2. Check watchlist alert levels
3. Verify zone risk levels
4. Check risk calculation logs

### Heatmap Not Showing

1. Ensure `show_risk_heatmap=true`
2. Check coordinate data
3. Verify risk score calculation
4. Check browser console for errors

---

## Related Documentation

- **Chapter 8.1**: Map Service Guide (`46_MAP_SERVICE_GUIDE.md`)
- **Chapter 7.4**: Security Intelligence Guide (`45_SECURITY_INTELLIGENCE_GUIDE.md`)

---

## Conclusion

The security intelligence map features provide comprehensive threat visualization and pattern analysis capabilities, making the map service suitable for security operations and surveillance systems. All features are production-ready with proper error handling, validation, and performance optimization.

