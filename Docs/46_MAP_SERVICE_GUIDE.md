# Chapter 8.1: Map Service Guide
## Backend Map Generation for Intelligence Tracking

**Version:** 5.0.0  
**Last Updated:** January 2025

---

## Overview

The Map Service provides server-side map generation using Python and Folium for the Intelligence Analysis tracking feature. This production-ready solution offers reliability, performance, and advanced security intelligence features.

---

## Architecture

### Components

1. **Map Service** (`backend/core/map_service.py`)
   - Generates interactive HTML maps using Folium
   - Generates GeoJSON data for advanced frontend rendering
   - Handles coordinate processing and route visualization
   - Integrates with security intelligence features

2. **Security Features** (`backend/core/security_map_features.py`)
   - Pattern detection (loitering, backtracking, rapid movement)
   - Risk scoring and heatmaps
   - Threat indicators from watchlists
   - Security zone visualization

3. **API Endpoints** (`backend/routes/intelligence.py`)
   - `/api/identities/{identity_id}/map` - Returns HTML map (Folium)
   - `/api/identities/{identity_id}/map/geojson` - Returns GeoJSON data
   - `/api/map/stats` - Returns map service statistics

4. **Frontend Integration** (`frontend/js/admin-intelligence.js`)
   - Automatically uses backend map when available
   - Embeds map in iframe for isolation

---

## Configuration

### Environment Variables

All map service configuration is managed through `config.py` and `.env`:

```bash
# Map Service Configuration
MAP_CACHE_TTL=3600                    # Cache TTL in seconds (default: 1 hour)
MAP_CACHE_ENABLED=true                 # Enable caching (default: true)
MAP_MAX_COORDINATES=10000              # Max coordinates per map (default: 10000)
MAP_GENERATION_TIMEOUT=30              # Timeout in seconds (default: 30)
MAP_MAX_TRACKS=100                     # Max tracks per request (default: 100)
MAP_DEFAULT_STYLE=dark                 # Default style: dark, light, satellite, terrain
MAP_ENABLE_SECURITY_FEATURES=true      # Enable security features by default
MAP_DETECT_PATTERNS=true               # Enable pattern detection by default
MAP_SHOW_RISK_HEATMAP=true             # Show risk heatmap by default
MAP_SHOW_TIMELINE=false                # Show timeline control by default
```

### Redis Configuration

The service uses the existing Redis cache manager:

```bash
REDIS_URL=redis://redis:6379/0
REDIS_MAX_CONNECTIONS=100
REDIS_POOL_SIZE=50
CACHE_TTL=3600
```

---

## Features

### ✅ Interactive Maps (Folium)
- **Interactive maps** with zoom, pan, and layer controls
- **Route visualization** showing movement paths
- **Marker clustering** for better performance with many locations
- **Popup information** with timestamp, duration, and location details
- **Multiple map styles**: dark, light, satellite, terrain
- **Offline support** - works without external map tile dependencies

### ✅ Security Intelligence Features
- **Pattern Detection**: Loitering, backtracking, rapid movement
- **Risk Scoring**: Multi-factor risk calculation
- **Threat Indicators**: Watchlist integration
- **Security Zones**: Monitored and restricted zones
- **Risk Heatmap**: Visual risk distribution
- **Timeline Playback**: Time-based movement visualization

### ✅ GeoJSON Endpoint
- **Standard format** for GIS tools and mapping libraries
- **Point features** for each location
- **LineString features** for routes
- **Rich metadata** in feature properties

### ✅ Production Features
- **Redis Caching**: 1-hour TTL with cache hit/miss tracking
- **Input Validation**: Coordinate, track, and style validation
- **Security**: XSS prevention, input sanitization
- **Error Handling**: Comprehensive error handling with graceful degradation
- **Performance**: Async operations, memory limits, timeout protection
- **Monitoring**: Statistics endpoint for metrics

---

## API Usage

### 1. Get Interactive Map (HTML)

```http
GET /api/identities/{identity_id}/map?date=2024-01-15&map_style=dark&enable_security_features=true
```

**Query Parameters:**
- `date` (optional): Specific date (YYYY-MM-DD)
- `days_back` (optional, default: 7): Days to analyze if no date
- `map_style` (optional, default: "dark"): Style - dark, light, satellite, terrain
- `include_popups` (optional, default: true): Include popup information
- `show_routes` (optional, default: true): Draw routes between locations
- `cluster_markers` (optional, default: true): Cluster nearby markers
- `enable_security_features` (optional, default: true): Enable security intelligence features
- `detect_patterns` (optional, default: true): Detect suspicious movement patterns
- `show_risk_heatmap` (optional, default: true): Show risk heatmap overlay
- `show_timeline` (optional, default: false): Show timeline playback control

**Response:** HTML page with embedded interactive map

**Caching:** Yes (configurable TTL, default: 1 hour)

**Example:**
```bash
curl -X GET "http://localhost:8000/api/identities/123e4567-e89b-12d3-a456-426614174000/map?days_back=7&map_style=dark" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### 2. Get GeoJSON Data

```http
GET /api/identities/{identity_id}/map/geojson?date=2024-01-15&days_back=7
```

**Query Parameters:**
- `date` (optional): Specific date (YYYY-MM-DD)
- `days_back` (optional, default: 7): Days to analyze

**Response:** GeoJSON FeatureCollection

**Example Response:**
```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {
        "type": "Point",
        "coordinates": [-122.4194, 37.7749]
      },
      "properties": {
        "date": "2024-01-15",
        "pipeline_name": "Main Entrance",
        "timestamp": "2024-01-15T10:30:00",
        "duration_at_location": 120,
        "sequence": 1,
        "is_start": true,
        "is_end": false
      }
    },
    {
      "type": "Feature",
      "geometry": {
        "type": "LineString",
        "coordinates": [[-122.4194, 37.7749], [-122.4094, 37.7849]]
      },
      "properties": {
        "type": "route",
        "date": "2024-01-15",
        "movement_count": 5
      }
    }
  ]
}
```

### 3. Get Map Service Statistics

```http
GET /api/map/stats
```

**Response:**
```json
{
  "maps_generated": 1250,
  "cache_hits": 850,
  "cache_misses": 400,
  "errors": 5,
  "timeouts": 2,
  "cache_enabled": true
}
```

---

## Data Flow

### Integration with Database

The map service integrates with the database through the Intelligence Service:

1. **Route Handler** (`backend/routes/intelligence.py`)
   - Receives database session
   - Calls `intelligence_service.get_cross_camera_track()`

2. **Intelligence Service** (`backend/core/intelligence_service.py`)
   - Queries `IdentityAppearance` table
   - Queries `Pipeline` table for coordinates (`latitude`, `longitude`)
   - Queries `Identity` table for identity information
   - Returns `CrossCameraTrack` objects with coordinates

3. **Map Service** (`backend/core/map_service.py`)
   - Receives pre-processed tracking data
   - Generates map with coordinates from database
   - Adds security features if enabled

### Coordinate Source

Coordinates are stored in the `pipelines` table:
- `latitude` (Float): Latitude coordinate
- `longitude` (Float): Longitude coordinate
- `location_name` (String): Human-readable location name

Coordinates are extracted by the Intelligence Service and passed to the Map Service.

---

## Security Intelligence Features

### Pattern Detection

1. **Loitering Detection**
   - Identifies when an identity stays in a small area for extended time
   - Configurable radius (default: 100m) and duration (default: 5 minutes)

2. **Backtracking Detection**
   - Detects when an identity returns to a previous location
   - Flags suspicious return patterns

3. **Rapid Movement Detection**
   - Identifies movements exceeding speed threshold (default: 100 km/h)
   - Flags suspiciously fast movements

### Risk Scoring

Multi-factor risk calculation:
- **Base Risk**: Default risk level
- **Watchlist Risk**: Based on watchlist matches
- **Pattern Risk**: Based on detected patterns
- **Zone Risk**: Based on security zone violations
- **Speed Risk**: Based on movement speed

Risk levels: Critical, High, Medium, Low

### Threat Indicators

- **Watchlist Integration**: Shows locations where watchlisted identities were detected
- **Alert Levels**: Critical, High, Medium, Low
- **Visual Markers**: Color-coded threat indicators on map

### Security Zones

- **Monitored Zones**: Areas under surveillance
- **Restricted Zones**: High-security areas
- **Risk Levels**: 1-10 scale
- **Polygon Overlays**: Visual zone boundaries

---

## Frontend Integration

The frontend automatically uses the backend map:

1. **Automatic Detection**: When "Map" button is clicked, loads backend map
2. **Iframe Embedding**: Map embedded in iframe to isolate Folium's CSS/JS
3. **Error Handling**: Graceful fallback if backend fails

### Manual Usage

You can also use the GeoJSON endpoint for custom frontend rendering:

```javascript
async function loadCustomMap() {
    const response = await fetch(`/api/identities/${identityId}/map/geojson?days_back=7`, {
        credentials: 'include',
        headers: {
            'Authorization': `Bearer ${token}`
        }
    });
    const geojson = await response.json();
    
    // Use with Leaflet, Mapbox, or any mapping library
    L.geoJSON(geojson).addTo(map);
}
```

---

## Installation

### Requirements

Add Folium to your requirements:

```bash
pip install folium>=0.15.0
```

Or add to `requirements-cpu.txt`:
```
folium>=0.15.0
```

### Configuration

1. **Update `.env` file** with map service settings (see Configuration section)
2. **Ensure Redis is running** for caching
3. **Verify Pipeline coordinates** are set in database

---

## Troubleshooting

### Folium Not Installed

If you see "Map Service Unavailable":
```bash
pip install folium>=0.15.0
```

### Map Not Loading

1. Check browser console for errors
2. Check backend logs for map generation errors
3. Verify coordinates are available in pipeline data:
   ```sql
   SELECT pipeline_id, latitude, longitude FROM pipelines WHERE latitude IS NOT NULL;
   ```
4. Check network tab for API response

### Empty Map

- Verify pipelines have latitude/longitude coordinates
- Check date range has tracking data
- Ensure identity has appearances in the time period
- Check backend logs for validation errors

### Cache Issues

- Verify Redis is running: `redis-cli ping`
- Check cache configuration in `.env`
- Review cache statistics: `GET /api/map/stats`

---

## Performance Optimization

### Caching

- Maps are cached for 1 hour (configurable via `MAP_CACHE_TTL`)
- Cache keys are based on request parameters
- Cache hit/miss rates tracked in statistics

### Memory Management

- Maximum 10,000 coordinates per map (configurable via `MAP_MAX_COORDINATES`)
- Maximum 100 tracks per request (configurable via `MAP_MAX_TRACKS`)
- Automatic limiting prevents memory issues

### Timeout Protection

- 30-second timeout (configurable via `MAP_GENERATION_TIMEOUT`)
- Prevents hanging requests
- Returns error if timeout exceeded

---

## Monitoring

### Statistics Endpoint

Monitor map service performance:

```bash
curl -X GET "http://localhost:8000/api/map/stats" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Metrics:**
- `maps_generated`: Total maps generated
- `cache_hits`: Cache hit count
- `cache_misses`: Cache miss count
- `errors`: Error count
- `timeouts`: Timeout count
- `cache_enabled`: Cache status

### Logging

All map operations are logged:
- `[MAP]` prefix for map service logs
- `[SECURITY]` prefix for security feature logs
- Error logs include stack traces
- Warning logs for non-critical issues

---

## Best Practices

1. **Set Pipeline Coordinates**: Ensure all pipelines have latitude/longitude
2. **Enable Caching**: Use Redis caching for better performance
3. **Monitor Statistics**: Regularly check `/api/map/stats`
4. **Configure Limits**: Adjust `MAP_MAX_COORDINATES` based on your data size
5. **Use Security Features**: Enable security intelligence for threat detection
6. **Error Handling**: Implement frontend error handling for map failures

---

## Related Documentation

- **Chapter 8.2**: Map Service Data Flow (`47_MAP_SERVICE_DATA_FLOW.md`)
- **Chapter 8.3**: Security Intelligence Map Features (`48_SECURITY_INTELLIGENCE_MAP_FEATURES.md`)
- **Chapter 8.4**: Map Service Production Guide (`49_MAP_SERVICE_PRODUCTION_GUIDE.md`)
- **Chapter 6**: Configuration Guide (`36_CONFIGURATION_GUIDE.md`)

---

## Conclusion

The Map Service provides a production-ready solution for intelligence tracking visualization with advanced security features, comprehensive error handling, and performance optimization. All configuration is managed through `config.py` and `.env` for easy deployment and maintenance.

