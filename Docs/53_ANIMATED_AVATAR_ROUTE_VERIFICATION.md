# Animated Avatar Route Verification Guide

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

## Route Registration Status

### ✅ Route is Registered

The animated avatar feature is accessible via the following endpoint:

```
GET /api/identities/{identity_id}/map?show_animated_avatar=true
```

**Route Definition**: `backend/routes/intelligence.py` (line 305-339)

**Router Registration**: `backend/main.py` (line 347-356)

---

## Verification Steps

### 1. Check Route Registration

The intelligence router is registered in `backend/main.py`:

```python
if intelligence_router:
    app.include_router(intelligence_router)
    logger.info("✅ Intelligence router registered")
```

**To verify**:
1. Check server logs on startup - should see: `✅ Intelligence router registered`
2. Check for route listing - should see: `📍 GET /api/identities/{identity_id}/map`

### 2. Check Route Definition

The route is defined in `backend/routes/intelligence.py`:

```python
@router.get(
    "/api/identities/{identity_id}/map",
    response_class=HTMLResponse,
    tags=["Map Service"],
    summary="Get Interactive Map (Folium)",
    ...
)
async def get_tracking_map(
    identity_id: str,
    ...
    show_animated_avatar: bool = Query(default=False, description="Show animated avatar moving along route"),
    ...
):
```

**Parameter**: `show_animated_avatar` (bool, default: False)

### 3. Check Frontend Integration

The frontend sends the parameter in `frontend/js/admin-intelligence.js`:

```javascript
const showAnimatedAvatar = document.getElementById('map-show-animated-avatar')?.checked || false;
url += `&show_animated_avatar=${showAnimatedAvatar}`;
```

**Checkbox ID**: `map-show-animated-avatar`

### 4. Check Backend Processing

The backend processes the parameter in `backend/core/map_service.py`:

```python
if show_animated_avatar:
    if not ANIMATED_FEATURES_AVAILABLE:
        logger.warning("[MAP] Animated avatar requested but features not available")
    else:
        # Add animated avatar
        AnimatedMapRenderer.add_animated_avatar(...)
```

---

## How to Test

### Method 1: Via Frontend

1. Navigate to **Intelligence Analysis** → **Cross-Camera Tracking**
2. Select an identity
3. Click **"Map"** button
4. Check **"Animated Avatar"** checkbox in Map Settings
5. Click **"Refresh Map"**

### Method 2: Via API (Direct)

```bash
curl -X GET \
  "http://localhost:8000/api/identities/{identity_id}/map?show_animated_avatar=true" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Accept: text/html"
```

### Method 3: Via Swagger UI

1. Navigate to `http://localhost:8000/docs`
2. Find endpoint: `GET /api/identities/{identity_id}/map`
3. Click **"Try it out"**
4. Set `show_animated_avatar` to `true`
5. Click **"Execute"**

---

## Troubleshooting

### Issue: Avatar Not Appearing

**Check 1: Route Registration**
- Check server logs for: `✅ Intelligence router registered`
- If not present, check for import errors in `backend/routes/intelligence.py`

**Check 2: Parameter Passing**
- Check browser Network tab - URL should include `show_animated_avatar=true`
- Check server logs for: `[MAP] Adding animated avatar with X movements`

**Check 3: Module Availability**
- Check server logs for: `[MAP] Animated avatar requested but features not available`
- Verify `backend/core/animated_map_features.py` exists and imports correctly

**Check 4: Movement Data**
- Check server logs for: `[ANIMATED] Found X valid movements with coordinates`
- Need at least 2 movements with valid coordinates and timestamps

**Check 5: Folium Plugin**
- Check server logs for: `[ANIMATED] TimestampedGeoJson plugin added successfully`
- Verify `folium` and `folium.plugins` are installed

### Issue: Route Returns 404

**Possible Causes**:
1. Router not registered - check `backend/main.py` line 347
2. Import error - check `backend/main.py` line 265-271 for import errors
3. Syntax error in route file - check `backend/routes/intelligence.py` for syntax errors

**Solution**:
1. Check server startup logs for errors
2. Verify `intelligence_router` is not `None`
3. Check for Python syntax errors in route file

### Issue: Parameter Not Recognized

**Check**:
1. Frontend checkbox ID: `map-show-animated-avatar`
2. URL parameter name: `show_animated_avatar` (not `animated_avatar` or `avatar`)
3. Parameter type: boolean (true/false, not string)

---

## Log Messages to Look For

### Success Messages

```
✅ Intelligence router registered
📍 GET /api/identities/{identity_id}/map
[MAP] Adding animated avatar with X movements
[ANIMATED] Adding animated avatar for Identity Name with X movements
[ANIMATED] Found X valid movements with coordinates
[ANIMATED] Processing X timestamped points for avatar animation
[ANIMATED] TimestampedGeoJson plugin added successfully to map
[MAP] Animated avatar added successfully for identity {id}
```

### Warning Messages

```
[MAP] Animated avatar requested but features not available
[ANIMATED] Need at least 2 movements for animation, found X
[ANIMATED] All timestamps are the same, using fallback duration
[MAP] No movements found for animated avatar
```

### Error Messages

```
❌ Intelligence router NOT registered
[MAP] Error adding animated avatar: {error}
[ANIMATED] Error creating TimestampedGeoJson plugin: {error}
```

---

## Route Details

### Endpoint

```
GET /api/identities/{identity_id}/map
```

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `show_animated_avatar` | bool | `false` | Show animated avatar moving along route |
| `map_style` | str | `"light"` | Map style: dark, light, satellite, terrain |
| `show_routes` | bool | `true` | Draw routes between locations |
| `enable_security_features` | bool | `true` | Enable security intelligence features |
| `detect_patterns` | bool | `true` | Detect suspicious movement patterns |
| `show_risk_heatmap` | bool | `true` | Show risk heatmap overlay |
| `show_timeline` | bool | `false` | Show timeline playback control |
| `date` | str | `None` | Specific date (YYYY-MM-DD) |
| `days_back` | int | `7` | Days to analyze if no date |

### Authentication

**Required**: Yes (Admin role)

**Header**: `Authorization: Bearer {token}`

### Response

**Type**: `text/html`

**Content**: Complete HTML page with embedded interactive Folium map

---

## Code Flow

1. **Frontend** (`admin-intelligence.js`):
   - User checks "Animated Avatar" checkbox
   - `loadBackendMap()` builds URL with `show_animated_avatar=true`
   - Fetches map HTML from backend

2. **Backend Route** (`intelligence.py`):
   - Receives request with `show_animated_avatar` parameter
   - Calls `map_service.generate_folium_map()` with parameter

3. **Map Service** (`map_service.py`):
   - Checks `ANIMATED_FEATURES_AVAILABLE`
   - Collects movements from tracks
   - Calls `AnimatedMapRenderer.add_animated_avatar()`

4. **Animated Renderer** (`animated_map_features.py`):
   - Validates movements (coordinates + timestamps)
   - Creates `TimestampedGeoJson` plugin
   - Adds plugin to map object

5. **Response**:
   - Returns HTML with embedded map
   - Map includes animated avatar visualization

---

## Verification Checklist

- [ ] Intelligence router registered in server logs
- [ ] Route appears in Swagger UI (`/docs`)
- [ ] Frontend checkbox exists (`map-show-animated-avatar`)
- [ ] URL includes `show_animated_avatar=true` when checked
- [ ] Server logs show `[MAP] Adding animated avatar`
- [ ] Server logs show `[ANIMATED] Found X valid movements`
- [ ] Server logs show `[ANIMATED] TimestampedGeoJson plugin added`
- [ ] Map displays animated avatar icon
- [ ] Avatar moves along route
- [ ] Timeline controls appear

---

## Quick Test Command

```bash
# Test route registration
curl -X GET "http://localhost:8000/docs" | grep -i "identities.*map"

# Test endpoint (replace {identity_id} and {token})
curl -X GET \
  "http://localhost:8000/api/identities/{identity_id}/map?show_animated_avatar=true" \
  -H "Authorization: Bearer {token}" \
  -H "Accept: text/html" \
  -o test_map.html

# Check if animated avatar code is in HTML
grep -i "timestampedgeojson\|animated" test_map.html
```

---

## Summary

✅ **Route is registered** - `/api/identities/{identity_id}/map`  
✅ **Parameter is defined** - `show_animated_avatar` (bool)  
✅ **Frontend integration** - Checkbox sends parameter  
✅ **Backend processing** - Map service handles parameter  
✅ **Module available** - `AnimatedMapRenderer` is imported  

If the avatar is not appearing, check:
1. Server logs for error messages
2. Movement data has valid coordinates and timestamps
3. At least 2 movements exist
4. Folium and plugins are installed

---

**See Also**:
- [Animated Avatar Guide](52_ANIMATED_AVATAR_GUIDE.md) - Complete usage guide
- [Map Service Guide](46_MAP_SERVICE_GUIDE.md) - General map service documentation
- [API Documentation](50_API_DOCUMENTATION.md) - Complete API reference


