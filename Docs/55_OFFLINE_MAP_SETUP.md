# Completely Offline Map Setup Guide

**Version:** 5.0.0  
**Last Updated:** January 2025

---

## Overview

This guide explains how to set up **completely offline maps** using Folium that require **zero internet connection**. The maps will work 100% offline with no external requests.

---

## Current Implementation

### ✅ What's Already Done

1. **Tiles Disabled**: Maps use `tiles=None` - no tile requests to external servers
2. **Offline Background**: Custom colored background with grid pattern (no internet needed)
3. **Offline_folium Support**: Code detects and uses `offline_folium` if installed

### ⚠️ What Still Needs Internet (Without offline_folium)

Without `offline_folium` package:
- **JavaScript**: Leaflet.js loads from CDN (unpkg.com)
- **CSS**: Leaflet.css loads from CDN (unpkg.com)
- **Plugins**: MarkerCluster, TimestampedGeoJson plugins load from CDN

**Result**: Maps work but may load JS/CSS from internet on first load.

---

## Complete Offline Solution

### Step 1: Install offline_folium

```bash
pip install offline-folium
```

Or add to `requirements-cpu.txt`:
```
offline-folium>=0.1.0
```

### Step 2: Download Offline Resources

**While connected to internet**, run:

```bash
python -m offline_folium
```

This downloads all necessary JavaScript and CSS files locally.

### Step 3: Verify Installation

The code automatically detects `offline_folium` and uses it. Check server logs:

```
[MAP] ✅ Offline mode available - maps will work without internet
[MAP] ✅ Generated HTML using offline_folium (100% offline - no internet required)
```

---

## How It Works

### Without offline_folium

```python
# Tiles are offline (tiles=None)
# But JS/CSS load from CDN:
# - https://unpkg.com/leaflet@1.9.4/dist/leaflet.js
# - https://unpkg.com/leaflet@1.9.4/dist/leaflet.css
```

**Result**: Maps work, but require internet for JS/CSS on first load.

### With offline_folium

```python
from offline_folium import offline  # Must be imported before folium
import folium

# All resources served locally - no internet needed
```

**Result**: 100% offline - no internet connection required.

---

## Configuration

### Current Settings

- **Tiles**: `None` (no tile requests)
- **Background**: Custom colored background (CSS-based)
- **JavaScript/CSS**: Uses `offline_folium` if available, otherwise CDN

### Map Styles (Offline Background Colors)

- **Light**: `#f5f5f5` (light gray)
- **Dark**: `#1a1a2e` (dark blue-gray)
- **Satellite**: `#2d5016` (green)
- **Terrain**: `#8b7355` (brown)

---

## Verification

### Check Server Logs

When generating a map, you should see:

**With offline_folium**:
```
[MAP] ✅ Offline mode available - maps will work without internet
[MAP] ✅ Generated HTML using offline_folium (100% offline - no internet required)
```

**Without offline_folium**:
```
[MAP] ℹ️ offline_folium not installed - install with: pip install offline-folium for 100% offline maps
[MAP] Generated HTML (tiles offline, JS/CSS may use CDN - install offline-folium for 100% offline)
```

### Check Browser Network Tab

1. Open browser DevTools (F12)
2. Go to Network tab
3. Refresh the map
4. Check for external requests:
   - **With offline_folium**: No external requests (100% offline)
   - **Without offline_folium**: Requests to `unpkg.com` for JS/CSS

---

## Troubleshooting

### Issue: Maps Still Load from Internet

**Check**:
1. Is `offline_folium` installed? `pip list | grep offline`
2. Did you run `python -m offline_folium` to download resources?
3. Is `from offline_folium import offline` imported before `import folium`?

**Solution**:
```bash
pip install offline-folium
python -m offline_folium
# Restart server
```

### Issue: Map Shows Blank Background

**This is normal** - tiles are disabled for offline mode. You should see:
- Colored background (based on map style)
- Grid pattern
- All markers and routes still visible

### Issue: JavaScript Errors

**Check**:
1. Browser console for errors
2. Network tab for failed requests
3. Server logs for offline_folium status

**Solution**: Install and configure `offline_folium` as described above.

---

## Complete Setup Checklist

- [ ] Install `offline-folium`: `pip install offline-folium`
- [ ] Download resources: `python -m offline_folium`
- [ ] Verify in server logs: `✅ Offline mode available`
- [ ] Test map generation
- [ ] Check browser Network tab: No external requests
- [ ] Test with internet disabled: Map should still work

---

## Summary

### Current Status

✅ **Tiles are offline** - `tiles=None` means no tile requests  
✅ **Background is offline** - Custom CSS background  
⚠️ **JS/CSS may use CDN** - Unless `offline_folium` is installed  

### To Make 100% Offline

1. Install: `pip install offline-folium`
2. Download: `python -m offline_folium`
3. Done! Maps will be 100% offline

---

**See Also**:
- [Map Service Guide](46_MAP_SERVICE_GUIDE.md) - General map documentation
- [Animated Avatar Guide](52_ANIMATED_AVATAR_GUIDE.md) - Avatar features
- [API Documentation](50_API_DOCUMENTATION.md) - API reference


