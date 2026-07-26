# Chapter 8.2: Map Service Data Flow & Integration

**Version:** 5.0.0  
**Last Updated:** January 2025

---

## Overview

This chapter describes how the map service integrates with the existing application services and database, including the complete data flow from database to map visualization.

---

## Data Flow Architecture

```
Frontend Request
    ↓
API Route (intelligence.py)
    ↓
Intelligence Service (intelligence_service.py)
    ↓
Database (PostgreSQL)
    ↓
Pipeline Table (coordinates)
    ↓
Map Service (map_service.py)
    ↓
Folium Map Generation
    ↓
HTML Response
```

---

## Complete Integration Flow

### 1. API Request
**Endpoint**: `GET /api/identities/{identity_id}/map`

**Route Handler**: `backend/routes/intelligence.py::get_tracking_map()`

### 2. Database Query (via Intelligence Service)

The route handler calls:
```python
tracks = await intelligence_service.get_cross_camera_track(
    db=db,  # Database session from FastAPI dependency
    identity_id=identity_id,
    date=date,
    days_back=days_back
)
```

### 3. Intelligence Service Database Operations

**File**: `backend/core/intelligence_service.py::get_cross_camera_track()`

**Database Queries**:

1. **Query IdentityAppearance table**:
   ```python
   query = select(IdentityAppearance).where(
       and_(
           IdentityAppearance.identity_id == target_uuid,
           IdentityAppearance.start_time >= start_date,
           IdentityAppearance.start_time <= end_date
       )
   ).order_by(IdentityAppearance.start_time)
   ```

2. **Query Identity table**:
   ```python
   identity_query = select(Identity).where(Identity.id == target_uuid)
   ```

3. **Query Pipeline table for coordinates**:
   ```python
   pipeline_ids = list(set(a.pipeline_id for a in appearances))
   pipeline_query = select(Pipeline).where(Pipeline.pipeline_id.in_(pipeline_ids))
   result = await db.execute(pipeline_query)
   pipelines = {p.pipeline_id: p for p in result.scalars().all()}
   ```

4. **Extract coordinates from Pipeline**:
   ```python
   pipeline = pipelines.get(app.pipeline_id)
   coordinates = None
   if pipeline and pipeline.latitude is not None and pipeline.longitude is not None:
       coordinates = {"lat": float(pipeline.latitude), "lng": float(pipeline.longitude)}
   ```

### 4. Data Transformation

The route handler converts database objects to dictionaries:
```python
tracks_dict = [
    {
        "identity_id": t.identity_id,
        "display_name": t.display_name,
        "date": t.date,
        "movements": [
            {
                "pipeline_id": m.pipeline_id,
                "pipeline_name": m.pipeline_name,
                "timestamp": m.timestamp.isoformat(),
                "snapshot_path": m.snapshot_path,
                "snapshot_url": path_to_url(m.snapshot_path),
                "duration_at_location": m.duration_at_location,
                "coordinates": m.coordinates  # From Pipeline table
            }
            for m in t.movements
        ],
        ...
    }
    for t in tracks
]
```

### 5. Watchlist Integration

**File**: `backend/routes/intelligence.py::get_tracking_map()`

```python
if enable_security_features:
    from backend.core.watchlist_service import watchlist_service
    watchlist_matches = await watchlist_service.get_identity_watchlists(db, identity_id)
```

**Database Query**: The watchlist service queries:
- `WatchlistEntry` table
- `Watchlist` table
- `Identity` table

### 6. Map Service Processing

**File**: `backend/core/map_service.py::generate_folium_map()`

The map service receives:
- `tracks_dict`: Pre-processed tracking data with coordinates
- `watchlist_matches`: Watchlist information for security features
- `security_zones`: Optional security zones

**Processing**:
1. Validates input data
2. Checks cache (Redis) using `cache_manager`
3. Generates Folium map
4. Adds security features (patterns, threats, heatmaps)
5. Returns HTML

---

## Database Schema Integration

### Pipeline Table
```sql
CREATE TABLE pipelines (
    id INTEGER PRIMARY KEY,
    pipeline_id VARCHAR(255) UNIQUE,
    latitude FLOAT,           -- Used for map coordinates
    longitude FLOAT,          -- Used for map coordinates
    location_name VARCHAR(255), -- Used for map labels
    ...
);
```

### IdentityAppearance Table
```sql
CREATE TABLE identity_appearances (
    id INTEGER PRIMARY KEY,
    identity_id UUID,         -- Links to Identity
    pipeline_id VARCHAR(255), -- Links to Pipeline (for coordinates)
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    ...
);
```

### Identity Table
```sql
CREATE TABLE identities (
    id UUID PRIMARY KEY,
    display_name VARCHAR(255), -- Used in map legend
    ...
);
```

---

## Service Dependencies

### Map Service Dependencies
- ✅ **Intelligence Service**: Provides tracking data
- ✅ **Watchlist Service**: Provides security information
- ✅ **Cache Manager**: Provides Redis caching (from `config.py`)
- ✅ **Database**: Accessed via Intelligence Service (not directly)

### Why Map Service Doesn't Access Database Directly

**Architecture Decision**: Separation of Concerns

1. **Intelligence Service** handles:
   - Database queries
   - Data aggregation
   - Business logic
   - Coordinate extraction from Pipeline table

2. **Map Service** handles:
   - Map visualization
   - Pattern detection
   - Security features
   - HTML generation

3. **Benefits**:
   - Single responsibility
   - Easier testing
   - Reusable services
   - Clear data flow

---

## Data Sources

### Coordinates Source
- **Table**: `pipelines`
- **Columns**: `latitude`, `longitude`
- **Accessed via**: `intelligence_service.get_cross_camera_track()`
- **Flow**: Pipeline → Intelligence Service → Map Service

### Identity Information
- **Table**: `identities`
- **Columns**: `display_name`, `id`
- **Accessed via**: `intelligence_service.get_cross_camera_track()`

### Tracking Data
- **Table**: `identity_appearances`
- **Columns**: `pipeline_id`, `start_time`, `end_time`, `identity_id`
- **Accessed via**: `intelligence_service.get_cross_camera_track()`

### Watchlist Data
- **Tables**: `watchlists`, `watchlist_entries`
- **Accessed via**: `watchlist_service.get_identity_watchlists()`

---

## Configuration Integration

All configuration is managed through `config.py`:

```python
# Map Service Configuration
MAP_CACHE_TTL = settings.MAP_CACHE_TTL
MAP_CACHE_ENABLED = settings.MAP_CACHE_ENABLED
MAP_MAX_COORDINATES = settings.MAP_MAX_COORDINATES
MAP_GENERATION_TIMEOUT = settings.MAP_GENERATION_TIMEOUT
MAP_MAX_TRACKS = settings.MAP_MAX_TRACKS
```

**Environment Variables** (`.env`):
```bash
MAP_CACHE_TTL=3600
MAP_CACHE_ENABLED=true
MAP_MAX_COORDINATES=10000
MAP_GENERATION_TIMEOUT=30
MAP_MAX_TRACKS=100
```

---

## Example Data Flow

### Request
```http
GET /api/identities/123e4567-e89b-12d3-a456-426614174000/map?days_back=7
```

### Step 1: Route Handler
```python
# backend/routes/intelligence.py
tracks = await intelligence_service.get_cross_camera_track(
    db=db,  # Database session
    identity_id=identity_id,
    days_back=7
)
```

### Step 2: Intelligence Service
```python
# backend/core/intelligence_service.py
# Query IdentityAppearance
appearances = await db.execute(select(IdentityAppearance)...)

# Query Pipeline for coordinates
pipelines = await db.execute(select(Pipeline).where(...))

# Extract coordinates
for app in appearances:
    pipeline = pipelines.get(app.pipeline_id)
    if pipeline and pipeline.latitude:
        coordinates = {"lat": pipeline.latitude, "lng": pipeline.longitude}
```

### Step 3: Route Handler Transformation
```python
# Convert to dict format
tracks_dict = [{
    "movements": [{
        "coordinates": m.coordinates  # From Pipeline table
    }]
}]
```

### Step 4: Map Service
```python
# backend/core/map_service.py
map_html = await map_service.generate_folium_map(
    tracks=tracks_dict,  # Contains coordinates from database
    watchlist_matches=watchlist_matches  # From watchlist service
)
```

---

## Integration Verification

### ✅ Verified Integrations

1. **Database Access**: ✅
   - Intelligence service queries database
   - Pipeline coordinates extracted
   - Identity information retrieved

2. **Service Integration**: ✅
   - Intelligence service used for data
   - Watchlist service used for security
   - Cache manager used for performance

3. **Data Flow**: ✅
   - Database → Intelligence Service → Route Handler → Map Service
   - Coordinates from Pipeline table
   - Tracking from IdentityAppearance table

4. **Configuration**: ✅
   - All settings in `config.py`
   - Environment variables in `.env`
   - No hardcoded values

---

## Troubleshooting

### No Coordinates on Map

**Check**:
1. Pipeline table has latitude/longitude:
   ```sql
   SELECT pipeline_id, latitude, longitude FROM pipelines WHERE pipeline_id = 'your_pipeline';
   ```

2. Intelligence service extracts coordinates:
   - Check logs for coordinate extraction
   - Verify pipeline lookup in intelligence_service.py:564

3. Coordinates passed to map service:
   - Check tracks_dict in route handler
   - Verify coordinates in movements

### Missing Data

**Check**:
1. IdentityAppearance records exist:
   ```sql
   SELECT * FROM identity_appearances WHERE identity_id = 'your_id';
   ```

2. Pipeline records exist:
   ```sql
   SELECT * FROM pipelines WHERE pipeline_id IN (...);
   ```

3. Date range correct:
   - Verify start_date and end_date in intelligence_service

---

## Related Documentation

- **Chapter 8.1**: Map Service Guide (`46_MAP_SERVICE_GUIDE.md`)
- **Chapter 8.3**: Security Intelligence Map Features (`48_SECURITY_INTELLIGENCE_MAP_FEATURES.md`)
- **Chapter 8.4**: Map Service Production Guide (`49_MAP_SERVICE_PRODUCTION_GUIDE.md`)

---

## Conclusion

The map service is **fully integrated** with:
- ✅ Database via Intelligence Service
- ✅ Pipeline table for coordinates
- ✅ Identity table for names
- ✅ Watchlist service for security
- ✅ Cache manager for performance
- ✅ Configuration via `config.py` and `.env`

The architecture follows best practices with proper separation of concerns and clear data flow.

