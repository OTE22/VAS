# Chapter 8.4: Map Service Production Guide

**Version:** 5.0.0  
**Last Updated:** January 2025

---

## Overview

This guide covers production deployment, monitoring, and optimization of the map generation service. The service has been enhanced with enterprise-grade features including caching, validation, security, and monitoring.

---

## Production Features

### ✅ 1. Redis Caching
- **Automatic caching** of generated maps (configurable TTL, default: 1 hour)
- **Cache key generation** based on request parameters (SHA256 hash)
- **Cache hit/miss tracking** for monitoring
- **Graceful degradation** if Redis is unavailable

### ✅ 2. Input Validation
- **Coordinate validation** (lat: -90 to 90, lng: -180 to 180)
- **Track limit** (max 100 tracks per request, from `MAP_MAX_TRACKS`)
- **Coordinate limit** (max 10,000 coordinates per request, from `MAP_MAX_COORDINATES`)
- **Map style validation** (dark, light, satellite, terrain)
- **Automatic sanitization** of user input

### ✅ 3. Security
- **XSS prevention** via HTML escaping
- **Input sanitization** for all user-provided data
- **Safe coordinate handling** with validation
- **Error message sanitization**

### ✅ 4. Error Handling
- **Comprehensive error handling** with proper HTTP status codes
- **Detailed logging** for debugging
- **Graceful fallbacks** when services are unavailable
- **Timeout protection** (30 seconds default, from `MAP_GENERATION_TIMEOUT`)

### ✅ 5. Performance
- **Async/await** for non-blocking operations
- **Efficient coordinate processing**
- **Memory management** with limits
- **Cache-first strategy** for fast responses

### ✅ 6. Monitoring
- **Statistics endpoint** (`/api/map/stats`)
- **Cache hit/miss tracking**
- **Error counting**
- **Generation time tracking**

---

## Configuration

### Environment Variables

All configuration is managed through `config.py` and `.env`:

```bash
# Map Service Configuration
MAP_CACHE_TTL=3600                    # Cache TTL in seconds (default: 1 hour)
MAP_CACHE_ENABLED=true                 # Enable caching (default: true)
MAP_MAX_COORDINATES=10000              # Max coordinates per map (default: 10000)
MAP_GENERATION_TIMEOUT=30              # Timeout in seconds (default: 30)
MAP_MAX_TRACKS=100                     # Max tracks per request (default: 100)
MAP_DEFAULT_STYLE=dark                 # Default style: dark, light, satellite, terrain
MAP_ENABLE_SECURITY_FEATURES=true      # Enable security features by default
MAP_DETECT_PATTERNS=true                # Enable pattern detection by default
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

### Code Configuration

All settings are loaded from `config.py`:

```python
# backend/core/map_service.py
from config import settings

MAP_CACHE_TTL = settings.MAP_CACHE_TTL
MAP_CACHE_ENABLED = settings.MAP_CACHE_ENABLED
MAP_MAX_COORDINATES = settings.MAP_MAX_COORDINATES
MAP_GENERATION_TIMEOUT = settings.MAP_GENERATION_TIMEOUT
MAP_MAX_TRACKS = settings.MAP_MAX_TRACKS
```

---

## API Endpoints

### 1. Generate Interactive Map

```http
GET /api/identities/{identity_id}/map
```

**Query Parameters**:
- `date` (optional): Specific date (YYYY-MM-DD)
- `days_back` (optional, default: 7): Days to analyze
- `map_style` (optional, default: "dark"): Style - dark, light, satellite, terrain
- `include_popups` (optional, default: true): Include popup information
- `show_routes` (optional, default: true): Draw routes between locations
- `cluster_markers` (optional, default: true): Cluster nearby markers
- `enable_security_features` (optional, default: true): Enable security features
- `detect_patterns` (optional, default: true): Detect patterns
- `show_risk_heatmap` (optional, default: true): Show risk heatmap
- `show_timeline` (optional, default: false): Show timeline

**Response**: HTML page with embedded interactive map

**Caching**: Yes (configurable TTL, default: 1 hour)

### 2. Get GeoJSON Data

```http
GET /api/identities/{identity_id}/map/geojson
```

**Query Parameters**:
- `date` (optional): Specific date (YYYY-MM-DD)
- `days_back` (optional, default: 7): Days to analyze

**Response**: GeoJSON FeatureCollection

**Caching**: Yes (configurable TTL, default: 1 hour)

### 3. Get Service Statistics

```http
GET /api/map/stats
```

**Response**:
```json
{
  "maps_generated": 1234,
  "cache_hits": 890,
  "cache_misses": 344,
  "errors": 5,
  "timeouts": 0,
  "folium_available": true,
  "cache_available": true,
  "cache_enabled": true
}
```

---

## Usage Examples

### Python Client

```python
import httpx

async def get_map(identity_id: str, days_back: int = 7):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"/api/identities/{identity_id}/map",
            params={
                "days_back": days_back,
                "map_style": "dark",
                "include_popups": True,
                "show_routes": True,
                "cluster_markers": True,
                "enable_security_features": True
            },
            cookies={"session": "your_session_cookie"}
        )
        return response.text  # HTML map
```

### JavaScript/Frontend

```javascript
async function loadMap(identityId, daysBack = 7) {
    const url = `/api/identities/${identityId}/map?days_back=${daysBack}&map_style=dark`;
    const response = await fetch(url, { credentials: 'include' });
    const mapHtml = await response.text();
    
    // Embed in iframe
    const iframe = document.createElement('iframe');
    iframe.srcdoc = mapHtml;
    document.getElementById('map-container').appendChild(iframe);
}
```

---

## Performance Optimization

### Cache Strategy

1. **Cache Key Generation**: Based on all request parameters (SHA256 hash)
2. **TTL**: Configurable via `MAP_CACHE_TTL` (default: 1 hour)
3. **Cache Invalidation**: Automatic after TTL expires
4. **Cache Warming**: Can be implemented for frequently accessed identities

### Memory Management

- **Coordinate Limits**: Max 10,000 coordinates per request (from `MAP_MAX_COORDINATES`)
- **Track Limits**: Max 100 tracks per request (from `MAP_MAX_TRACKS`)
- **Validation**: Coordinates validated before processing
- **Efficient Processing**: Only processes valid coordinates

### Timeout Protection

- **Timeout**: Configurable via `MAP_GENERATION_TIMEOUT` (default: 30 seconds)
- **Prevents**: Hanging requests
- **Returns**: Error if timeout exceeded

---

## Monitoring

### Key Metrics

1. **Cache Hit Rate**: `cache_hits / (cache_hits + cache_misses)`
2. **Error Rate**: `errors / maps_generated`
3. **Average Generation Time**: Tracked in logs
4. **Service Availability**: Check `/api/map/stats`

### Logging

The service logs:
- Map generation start/end with timing
- Cache hits/misses
- Validation errors
- Generation errors (with stack traces)

**Log Prefixes**:
- `[MAP]`: Map service operations
- `[SECURITY]`: Security feature operations

**Example log**:
```
[MAP] Cache hit for key: map:abc123...
[MAP] Map generated in 0.45s (5 tracks)
[MAP] GeoJSON generated in 0.23s (5 tracks)
[SECURITY] Pattern detected: loitering (severity: 5)
```

### Statistics Endpoint

Monitor service health:
```bash
curl -X GET "http://localhost:8000/api/map/stats" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## Error Handling

### Common Errors

1. **Folium Not Installed**
   - Status: 503 Service Unavailable
   - Solution: `pip install folium>=0.15.0`

2. **Invalid Coordinates**
   - Status: 400 Bad Request
   - Message: "Invalid coordinates: lat=X, lng=Y"

3. **Too Many Coordinates**
   - Status: 400 Bad Request
   - Message: "Too many coordinates (X). Maximum is {MAP_MAX_COORDINATES}"

4. **Timeout**
   - Status: 500 Internal Server Error
   - Message: "Map generation timed out after {MAP_GENERATION_TIMEOUT} seconds"

5. **Cache Errors**
   - Status: 200 (map still generated)
   - Logged as warnings, service continues

---

## Security Considerations

### XSS Prevention

All user-provided data is sanitized:
- Identity names: HTML escaped
- Pipeline names: HTML escaped
- Dates: HTML escaped
- Timestamps: HTML escaped

**Implementation**: `sanitize_html()` function in `map_service.py`

### Input Validation

- Coordinates validated for valid ranges
- Map styles validated against allowed list
- Track counts limited to prevent DoS
- Coordinate counts limited to prevent memory issues

**Implementation**: `validate_coordinates()` and `_validate_inputs()` functions

---

## Troubleshooting

### Map Not Generating

1. **Check Folium Installation**
   ```bash
   pip install folium>=0.15.0
   ```

2. **Check Redis Connection**
   ```bash
   redis-cli ping
   ```

3. **Check Logs**
   ```bash
   grep "[MAP]" logs/app.log
   ```

### Slow Map Generation

1. **Check Cache Hit Rate**
   ```bash
   curl /api/map/stats
   ```

2. **Reduce Coordinate Count**
   - Filter tracks before requesting map
   - Use date ranges to limit data

3. **Enable Clustering**
   - Set `cluster_markers=true` for many markers

### Cache Not Working

1. **Check Redis Connection**
   ```python
   from backend.core.cache_manager import cache_manager
   await cache_manager.health_check()
   ```

2. **Check Cache Configuration**
   ```bash
   # In .env
   MAP_CACHE_ENABLED=true
   REDIS_URL=redis://redis:6379/0
   ```

3. **Check Cache Key Generation**
   - Ensure consistent parameters for cache hits

---

## Best Practices

### 1. Use Caching
Always use caching for production (default: enabled via `MAP_CACHE_ENABLED`)

### 2. Limit Data
- Use date ranges to limit tracks
- Filter coordinates before requesting map
- Adjust `MAP_MAX_COORDINATES` based on your data size

### 3. Monitor Performance
- Check `/api/map/stats` regularly
- Monitor cache hit rates
- Track error rates

### 4. Handle Errors Gracefully
- Implement retry logic for transient errors
- Show user-friendly error messages
- Log errors for debugging

### 5. Security
- Never trust user input
- Always validate coordinates
- Sanitize all HTML output

### 6. Configuration
- Use environment variables for all settings
- Document all configuration options
- Test configuration changes

---

## Deployment Checklist

### Pre-Deployment
- [ ] Install Folium: `pip install folium>=0.15.0`
- [ ] Verify Redis connection
- [ ] Check environment variables in `.env`
- [ ] Review log levels
- [ ] Test with production-like data

### Post-Deployment
- [ ] Monitor `/api/map/stats` endpoint
- [ ] Check error logs
- [ ] Monitor cache hit rates
- [ ] Verify security features working
- [ ] Check memory usage

---

## Related Documentation

- **Chapter 8.1**: Map Service Guide (`46_MAP_SERVICE_GUIDE.md`)
- **Chapter 8.2**: Map Service Data Flow (`47_MAP_SERVICE_DATA_FLOW.md`)
- **Chapter 8.3**: Security Intelligence Map Features (`48_SECURITY_INTELLIGENCE_MAP_FEATURES.md`)
- **Chapter 6**: Configuration Guide (`36_CONFIGURATION_GUIDE.md`)

---

## Conclusion

The production-ready map service provides:
- ✅ High performance with caching
- ✅ Security with input validation
- ✅ Reliability with error handling
- ✅ Monitoring with statistics
- ✅ Scalability with limits
- ✅ Configuration via `config.py` and `.env`

All settings are configurable through environment variables, making deployment and maintenance straightforward.

