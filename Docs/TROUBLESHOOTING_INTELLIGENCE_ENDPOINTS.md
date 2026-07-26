# Troubleshooting Intelligence Endpoints 404 Errors

## Problem

Getting 404 errors for all intelligence endpoints:
- `/api/identities/{id}/related` → 404
- `/api/identities/{id}/temporal-patterns` → 404
- `/api/identities/{id}/cross-camera` → 404
- `/api/identities/{id}/analyze` → 404

## Solutions

### 1. Restart the Server

The intelligence router needs to be loaded. **Restart your FastAPI server**:

```bash
# Stop the server (Ctrl+C)
# Then restart it
python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

Or if using Docker:
```bash
docker-compose restart
```

### 2. Check Server Logs

After restart, check the logs for:
```
✅ Intelligence router imported successfully
✅ Intelligence router registered
   📍 GET /api/identities/{identity_id}/related
   📍 POST /api/identities/{identity_id}/related/refresh
   📍 GET /api/identities/{identity_id}/temporal-patterns
   ...
```

If you see:
```
❌ Intelligence router import FAILED
```

Then there's an import error. Check the traceback in the logs.

### 3. Verify Router Registration

Check `backend/main.py` - the intelligence router should be included:

```python
if intelligence_router:
    app.include_router(intelligence_router)
    logger.info("✅ Intelligence router registered")
```

### 4. Check Import Errors

The router might fail to import due to:
- Missing dependencies
- Import errors in `backend/routes/intelligence.py`
- Import errors in dependencies (like `config.py`)

**Fixed**: Removed unused `curses` import from `config.py` that was causing import errors on Windows.

### 5. Verify Endpoints in Swagger

After restart, visit:
- `http://localhost:8000/docs`

You should see:
- **Intelligence** tag with all intelligence endpoints
- **Map Service** tag with map endpoints
- **Security Intelligence** tag with security endpoints

### 6. Test Endpoint Directly

```bash
curl -X GET "http://localhost:8000/api/identities/{identity_id}/related" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Leaflet File Issues

### Problem

Errors like:
```
GET http://localhost/frontend/vendor/leaflet/leaflet.markercluster.js net::ERR_ABORTED 404
Uncaught SyntaxError: Unexpected token 'export'
```

### Solution

**Fixed**: Updated `intelligence.html` to use CDN versions of Leaflet instead of local files:
- Leaflet CSS/JS from CDN
- MarkerCluster from CDN

The backend map (Folium) is the primary solution, so frontend Leaflet is only used as a fallback.

## Verification Checklist

- [ ] Server restarted after code changes
- [ ] Intelligence router shows in logs as registered
- [ ] Endpoints visible in Swagger UI (`/docs`)
- [ ] No import errors in server logs
- [ ] Leaflet files loading from CDN (or backend map working)

## Still Having Issues?

1. **Check server logs** for import errors
2. **Verify** `backend/routes/intelligence.py` has no syntax errors
3. **Test** a simple endpoint in Swagger UI
4. **Check** that you're using the correct base URL (should be `/api/...` not `/frontend/...`)

