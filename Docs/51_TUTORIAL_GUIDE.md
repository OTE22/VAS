# Chapter 10: Tutorial Guide
## Step-by-Step Tutorials

**Version:** 5.0.0  
**Last Updated:** January 2025

---

## Overview

This chapter provides step-by-step tutorials for common tasks, from basic setup to advanced features.

---

## Tutorial 1: Setting Up Map Service

### Prerequisites
- System installed and running
- Database configured
- Redis running (for caching)

### Step 1: Install Folium

```bash
pip install folium>=0.15.0
```

Or add to `requirements-cpu.txt`:
```
folium>=0.15.0
```

### Step 2: Configure Environment Variables

Add to `.env`:
```bash
# Map Service Configuration
MAP_CACHE_TTL=3600
MAP_CACHE_ENABLED=true
MAP_MAX_COORDINATES=10000
MAP_GENERATION_TIMEOUT=30
MAP_MAX_TRACKS=100
MAP_DEFAULT_STYLE=dark
MAP_ENABLE_SECURITY_FEATURES=true
MAP_DETECT_PATTERNS=true
MAP_SHOW_RISK_HEATMAP=true
MAP_SHOW_TIMELINE=false
```

### Step 3: Set Pipeline Coordinates

Update pipeline coordinates in the database:

**Via Web Interface**:
1. Go to Pipeline Management
2. Select a pipeline
3. Set latitude and longitude
4. Save

**Via API**:
```bash
curl -X PUT "http://localhost:8000/api/pipelines/camera_1/coordinates" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "latitude": 37.7749,
    "longitude": -122.4194,
    "location_name": "Main Entrance"
  }'
```

### Step 4: Verify Installation

```bash
curl -X GET "http://localhost:8000/api/map/stats" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Expected response:
```json
{
  "folium_available": true,
  "cache_available": true,
  "cache_enabled": true
}
```

---

## Tutorial 2: Generating Your First Map

### Step 1: Get Identity ID

```bash
curl -X GET "http://localhost:8000/api/identities" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Step 2: Generate Map

```bash
curl -X GET "http://localhost:8000/api/identities/{identity_id}/map?days_back=7" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -o map.html
```

### Step 3: View Map

Open `map.html` in a web browser.

### Step 4: Customize Map

```bash
curl -X GET "http://localhost:8000/api/identities/{identity_id}/map?days_back=7&map_style=satellite&show_routes=true" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -o map.html
```

---

## Tutorial 3: Using Security Intelligence Features

### Step 1: Enable Security Features

```bash
curl -X GET "http://localhost:8000/api/identities/{identity_id}/map?enable_security_features=true&detect_patterns=true&show_risk_heatmap=true" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -o map.html
```

### Step 2: Add Watchlist Entry

```bash
curl -X POST "http://localhost:8000/api/watchlists/{watchlist_id}/entries" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "identity_id": "{identity_id}",
    "alert_level": "critical"
  }'
```

### Step 3: View Threat Indicators

The map will automatically show threat indicators for watchlisted identities.

### Step 4: Analyze Patterns

The map will detect:
- Loitering patterns
- Backtracking
- Rapid movement

---

## Tutorial 4: Frontend Integration

### Step 1: Load Map in Frontend

```javascript
async function loadMap(identityId) {
    const url = `/api/identities/${identityId}/map?days_back=7&map_style=dark`;
    const response = await fetch(url, { 
        credentials: 'include',
        headers: {
            'Authorization': `Bearer ${token}`
        }
    });
    const mapHtml = await response.text();
    
    // Embed in iframe
    const iframe = document.createElement('iframe');
    iframe.srcdoc = mapHtml;
    iframe.style.width = '100%';
    iframe.style.height = '600px';
    document.getElementById('map-container').appendChild(iframe);
}
```

### Step 2: Use GeoJSON for Custom Rendering

```javascript
async function loadCustomMap(identityId) {
    const response = await fetch(`/api/identities/${identityId}/map/geojson?days_back=7`, {
        credentials: 'include'
    });
    const geojson = await response.json();
    
    // Use with Leaflet
    const map = L.map('map').setView([37.7749, -122.4194], 13);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);
    L.geoJSON(geojson).addTo(map);
}
```

---

## Tutorial 5: Monitoring and Optimization

### Step 1: Check Statistics

```bash
curl -X GET "http://localhost:8000/api/map/stats" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Step 2: Calculate Cache Hit Rate

```python
cache_hit_rate = cache_hits / (cache_hits + cache_misses)
print(f"Cache hit rate: {cache_hit_rate:.2%}")
```

### Step 3: Optimize Cache TTL

If cache hit rate is low, increase TTL:
```bash
MAP_CACHE_TTL=7200  # 2 hours
```

### Step 4: Monitor Errors

Check error rate:
```python
error_rate = errors / maps_generated
if error_rate > 0.05:  # 5%
    print("High error rate detected!")
```

---

## Tutorial 6: Troubleshooting

### Problem: Map Not Loading

**Solution**:
1. Check Folium installation:
   ```bash
   pip install folium>=0.15.0
   ```

2. Check logs:
   ```bash
   grep "[MAP]" logs/app.log
   ```

3. Verify coordinates:
   ```sql
   SELECT pipeline_id, latitude, longitude FROM pipelines WHERE latitude IS NOT NULL;
   ```

### Problem: Empty Map

**Solution**:
1. Check tracking data:
   ```sql
   SELECT * FROM identity_appearances WHERE identity_id = '{identity_id}';
   ```

2. Verify date range:
   - Use `days_back` parameter
   - Check date format (YYYY-MM-DD)

3. Check coordinates:
   - Ensure pipelines have latitude/longitude
   - Verify coordinate ranges

### Problem: Slow Map Generation

**Solution**:
1. Enable caching:
   ```bash
   MAP_CACHE_ENABLED=true
   ```

2. Reduce coordinate count:
   - Use date ranges
   - Filter tracks

3. Enable clustering:
   ```bash
   cluster_markers=true
   ```

---

## Tutorial 7: Production Deployment

### Step 1: Configure Environment

```bash
# Production .env
MAP_CACHE_TTL=3600
MAP_CACHE_ENABLED=true
MAP_MAX_COORDINATES=10000
MAP_GENERATION_TIMEOUT=30
MAP_MAX_TRACKS=100
```

### Step 2: Set Up Redis

```bash
# Docker
docker run -d -p 6379:6379 redis:alpine

# Or use existing Redis
REDIS_URL=redis://redis:6379/0
```

### Step 3: Monitor Performance

Set up monitoring:
```bash
# Check stats endpoint
curl http://localhost:8000/api/map/stats

# Monitor logs
tail -f logs/app.log | grep "[MAP]"
```

### Step 4: Load Testing

Test with production-like data:
```bash
# Generate multiple maps
for i in {1..100}; do
    curl -X GET "http://localhost:8000/api/identities/{identity_id}/map" \
      -H "Authorization: Bearer YOUR_TOKEN" &
done
```

---

## Tutorial 8: Advanced Configuration

### Custom Map Styles

```bash
# Use satellite imagery
map_style=satellite

# Use terrain
map_style=terrain

# Use light theme
map_style=light
```

### Security Features Configuration

```bash
# Disable security features
MAP_ENABLE_SECURITY_FEATURES=false

# Disable pattern detection
MAP_DETECT_PATTERNS=false

# Disable risk heatmap
MAP_SHOW_RISK_HEATMAP=false

# Enable timeline
MAP_SHOW_TIMELINE=true
```

### Performance Tuning

```bash
# Increase cache TTL for better hit rate
MAP_CACHE_TTL=7200  # 2 hours

# Reduce coordinate limit for faster generation
MAP_MAX_COORDINATES=5000

# Increase timeout for large datasets
MAP_GENERATION_TIMEOUT=60
```

---

## Quick Reference

### Common Commands

```bash
# Generate map
curl -X GET "http://localhost:8000/api/identities/{id}/map?days_back=7" \
  -H "Authorization: Bearer TOKEN"

# Get GeoJSON
curl -X GET "http://localhost:8000/api/identities/{id}/map/geojson?days_back=7" \
  -H "Authorization: Bearer TOKEN"

# Check stats
curl -X GET "http://localhost:8000/api/map/stats" \
  -H "Authorization: Bearer TOKEN"
```

### Configuration Checklist

- [ ] Folium installed
- [ ] Redis running
- [ ] Pipeline coordinates set
- [ ] Environment variables configured
- [ ] Authentication working
- [ ] Map generation tested

---

## Related Documentation

- **Chapter 8.1**: Map Service Guide (`46_MAP_SERVICE_GUIDE.md`)
- **Chapter 8.4**: Map Service Production Guide (`49_MAP_SERVICE_PRODUCTION_GUIDE.md`)
- **Chapter 9**: API Documentation (`50_API_DOCUMENTATION.md`)
- **Chapter 6**: Configuration Guide (`36_CONFIGURATION_GUIDE.md`)

---

## Conclusion

These tutorials provide step-by-step guidance for common tasks. For more detailed information, refer to the specific chapter documentation.

