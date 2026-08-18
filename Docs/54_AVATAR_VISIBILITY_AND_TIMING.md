# Avatar Visibility and Real-Time Detection Guide

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

## How Avatar Appears Based on Real Detection Times

### ✅ Yes, Avatar Uses Real Detection Times

The animated avatar **does appear at the real time of detection** from your surveillance system. Here's how it works:

1. **Data Source**: Uses `app.start_time` from `IdentityAppearance` table (actual detection timestamp)
2. **Timestamp Format**: Converts datetime to ISO format for animation
3. **Animation Timing**: Avatar appears at exact detection times, respecting real time differences
4. **Movement**: Avatar moves along route based on actual time gaps between detections

---

## Why You Might Not See the Avatar

### Common Issues

#### 1. **No Coordinates Available**

**Problem**: Pipelines don't have `latitude` and `longitude` set.

**Check**:
```sql
SELECT pipeline_id, location_name, latitude, longitude 
FROM pipelines 
WHERE latitude IS NULL OR longitude IS NULL;
```

**Solution**: Add coordinates to pipelines in the database.

**Log Message**:
```
[ANIMATED] Found 0 valid movements with coordinates
```

#### 2. **Less Than 2 Movements**

**Problem**: Need at least 2 detections with coordinates for animation.

**Check**: Server logs for:
```
[ANIMATED] Need at least 2 movements for animation, found X
```

**Solution**: Ensure identity has multiple detections at different locations.

#### 3. **Avatar is There But Not Visible**

**Problem**: Avatar might be added but not visible due to:
- Map zoom level too far out
- Avatar color blends with map
- Layer control hiding the feature group

**Check**: 
- Look for green "play" icon (start) and red "flag" icon (end)
- Check layer control panel on map
- Zoom in/out to see if avatar appears

#### 4. **TimestampedGeoJson Not Rendering**

**Problem**: Folium plugin might not be loading correctly.

**Check**: Browser console for JavaScript errors.

**Solution**: Ensure `folium` and `folium.plugins` are installed:
```bash
# (removed: folium is no longer a dependency of this project)
```

---

## Data Flow: From Detection to Avatar

### Step 1: Detection in Database

```python
# IdentityAppearance table
app.start_time = datetime(2025, 1, 11, 10, 30, 45)  # REAL detection time
app.pipeline_id = "camera-001"
```

### Step 2: Intelligence Service Collects Data

```python
# backend/core/intelligence_service.py
movements.append(CrossCameraMovement(
    pipeline_id=app.pipeline_id,
    timestamp=app.start_time,  # REAL detection time
    coordinates={"lat": pipeline.latitude, "lng": pipeline.longitude}
))
```

### Step 3: Route Converts to Dict

```python
# backend/routes/intelligence.py
"timestamp": m.timestamp.isoformat(),  # Converts to ISO string
"coordinates": m.coordinates  # {"lat": float, "lng": float}
```

### Step 4: Map Service Collects Movements

```python
# backend/core/map_service.py
for track in tracks:
    all_movements.extend(track.get('movements', []))
```

### Step 5: Animated Avatar Processes

```python
# backend/core/animated_map_features.py
# Parses timestamp back to datetime
dt = datetime.fromisoformat(timestamp)

# Creates TimestampedGeoJson with REAL times
timestamped_geojson = {
    "features": [{
        "properties": {
            "time": dt.isoformat()  # REAL detection time
        }
    }]
}
```

### Step 6: Avatar Appears on Map

- Avatar appears at first detection time
- Moves to next location at next detection time
- Respects actual time gaps between detections

---

## Verification Steps

### Step 1: Check Server Logs

When you enable animated avatar, you should see:

```
[MAP] Adding animated avatar with X movements
[ANIMATED] Adding animated avatar for Identity Name with X movements
[ANIMATED] Found X valid movements with coordinates
[ANIMATED] Processing X timestamped points for avatar animation
[ANIMATED] Time span: 2025-01-11 10:00:00 to 2025-01-11 10:30:00 (duration: 1800.0 seconds)
[ANIMATED] Creating TimestampedGeoJson with period=PT1S, duration=PT1800S
[ANIMATED] TimestampedGeoJson plugin added successfully to map
[MAP] Animated avatar added successfully for identity {id}
```

### Step 2: Check Data Has Coordinates

```python
# In Python console or debug endpoint
tracks = await intelligence_service.get_cross_camera_track(db, identity_id)
for track in tracks:
    for movement in track.movements:
        print(f"Movement: {movement.pipeline_name}")
        print(f"  Timestamp: {movement.timestamp}")
        print(f"  Coordinates: {movement.coordinates}")
```

### Step 3: Check Browser Console

Open browser DevTools (F12) and check:
- No JavaScript errors
- Map loads successfully
- TimestampedGeoJson plugin is present in HTML

### Step 4: Check Map HTML

View page source and search for:
- `TimestampedGeoJson` - should be present
- `"time":` - should have ISO timestamps
- `iconUrl` - should have base64 avatar icon

---

## Debugging: Add Diagnostic Endpoint

Add this to `backend/routes/intelligence.py` for debugging:

```python
@router.get("/api/identities/{identity_id}/map/debug")
async def debug_map_data(
    identity_id: str,
    days_back: int = Query(default=7),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Debug endpoint to check map data availability."""
    tracks = await intelligence_service.get_cross_camera_track(
        db=db,
        identity_id=identity_id,
        days_back=days_back
    )
    
    debug_info = {
        "total_tracks": len(tracks),
        "tracks_detail": []
    }
    
    for track in tracks:
        track_info = {
            "date": track.date,
            "total_movements": len(track.movements),
            "movements_with_coords": 0,
            "movements_without_coords": 0,
            "movements_detail": []
        }
        
        for movement in track.movements:
            has_coords = movement.coordinates is not None and \
                        movement.coordinates.get('lat') and \
                        movement.coordinates.get('lng')
            
            if has_coords:
                track_info["movements_with_coords"] += 1
            else:
                track_info["movements_without_coords"] += 1
            
            track_info["movements_detail"].append({
                "pipeline_id": movement.pipeline_id,
                "pipeline_name": movement.pipeline_name,
                "timestamp": movement.timestamp.isoformat(),
                "has_coordinates": has_coords,
                "coordinates": movement.coordinates
            })
        
        debug_info["tracks_detail"].append(track_info)
    
    return JSONResponse(content=debug_info)
```

---

## Expected Behavior

### Avatar Appearance

1. **Start Marker**: Green "play" icon at first detection location
2. **End Marker**: Red "flag" icon at last detection location
3. **Animated Avatar**: Colored circle icon that moves along route
4. **Route Line**: Colored line connecting all detection points
5. **Timeline Controls**: Play/pause, speed, time slider

### Animation Timing

- **First Detection**: Avatar appears at first detection time
- **Movement**: Avatar moves to next location at next detection time
- **Time Gaps**: Respects actual time differences (e.g., 5 minutes between detections = avatar takes 5 minutes to move)
- **Duration**: Total animation duration = time from first to last detection

### Example Timeline

**Detections**:
- 10:00:00 - Camera 1 (Start)
- 10:05:30 - Camera 2 (5 minutes 30 seconds later)
- 10:12:15 - Camera 3 (6 minutes 45 seconds later)

**Avatar Behavior**:
- Appears at Camera 1 at 10:00:00
- Moves to Camera 2, arriving at 10:05:30
- Moves to Camera 3, arriving at 10:12:15
- Total animation: 12 minutes 15 seconds

---

## Troubleshooting Checklist

- [ ] Check server logs for avatar-related messages
- [ ] Verify pipelines have coordinates (lat/lng)
- [ ] Ensure at least 2 detections with coordinates
- [ ] Check browser console for JavaScript errors
- [ ] Verify `show_animated_avatar=true` in URL
- [ ] Check map HTML contains `TimestampedGeoJson`
- [ ] Look for start/end markers (green play, red flag)
- [ ] Check layer control panel for feature groups
- [ ] Try different map styles (light/dark)
- [ ] (removed — folium is not a dependency)

---

## Quick Test

### Test 1: Check Coordinates

```bash
# Check if pipelines have coordinates
curl -X GET "http://localhost:8000/api/pipelines" \
  -H "Authorization: Bearer YOUR_TOKEN" | \
  jq '.[] | {id: .pipeline_id, name: .location_name, lat: .latitude, lng: .longitude}'
```

### Test 2: Check Tracking Data

```bash
# Get tracking data for identity
curl -X GET "http://localhost:8000/api/identities/{identity_id}/cross-camera?days_back=7" \
  -H "Authorization: Bearer YOUR_TOKEN" | \
  jq '.[0].movements[] | {pipeline: .pipeline_name, time: .timestamp, coords: .coordinates}'
```

### Test 3: Test Map with Avatar

```bash
# Generate map with animated avatar
curl -X GET "http://localhost:8000/api/identities/{identity_id}/map?show_animated_avatar=true" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Accept: text/html" \
  -o test_map.html

# Check if avatar code is present
grep -i "timestampedgeojson\|animated\|avatar" test_map.html
```

---

## Summary

✅ **Avatar uses real detection times** - Based on `app.start_time` from database  
✅ **Respects time gaps** - Animation duration matches actual time differences  
✅ **Requires coordinates** - Pipelines must have `latitude` and `longitude`  
✅ **Needs 2+ movements** - At least 2 detections with coordinates required  

**If avatar doesn't appear**:
1. Check pipelines have coordinates
2. Check at least 2 movements exist
3. Check server logs for error messages
4. Verify checkbox is checked and parameter is in URL

---

**See Also**:
- [Animated Avatar Guide](52_ANIMATED_AVATAR_GUIDE.md) - Complete usage guide
- [Route Verification](53_ANIMATED_AVATAR_ROUTE_VERIFICATION.md) - Route troubleshooting
- [Map Service Guide](46_MAP_SERVICE_GUIDE.md) - General map documentation


