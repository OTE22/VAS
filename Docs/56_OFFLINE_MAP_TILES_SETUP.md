# Chapter 8.6: Offline Map Tiles Setup Guide

## Overview

This guide explains how to set up offline map tiles (MBTiles or directory structure) to replace the grid background with actual map imagery. This provides a real map appearance while maintaining 100% offline functionality.

## Table of Contents

1. [Quick Start](#quick-start)
2. [Tile Formats](#tile-formats)
3. [Downloading Tiles](#downloading-tiles)
4. [Configuration](#configuration)
5. [Directory Structure](#directory-structure)
6. [MBTiles Support](#mbtiles-support)
7. [Verification](#verification)
8. [Troubleshooting](#troubleshooting)

---

## Quick Start

### Step 1: Download Map Tiles

Choose one of these methods:

**Option A: Using Mobile Atlas Creator (MOBAC)**
1. Download [Mobile Atlas Creator](https://mobac.sourceforge.io/)
2. Select your area of interest
3. Choose zoom levels (e.g., 10-16)
4. Export as "Osmdroid ZIP" or "Directory structure"
5. Extract to a directory

**Option B: Using Python Script**
```bash
# Install tile downloader
pip install tiletanic

# Download tiles for a specific area
python -c "
from tiletanic import tilecover
import requests
import os

# Define bounding box (lat, lng)
bbox = (min_lat, min_lng, max_lat, max_lng)
zoom_levels = range(10, 17)  # Zoom levels 10-16

# Create tiles directory
os.makedirs('tiles', exist_ok=True)

# Download tiles
for zoom in zoom_levels:
    tiles = tilecover.bbox(bbox, zoom)
    for tile in tiles:
        z, x, y = tile
        url = f'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
        tile_dir = f'tiles/{z}/{x}'
        os.makedirs(tile_dir, exist_ok=True)
        response = requests.get(url)
        if response.status_code == 200:
            with open(f'{tile_dir}/{y}.png', 'wb') as f:
                f.write(response.content)
        print(f'Downloaded: {z}/{x}/{y}.png')
"
```

### Step 2: Configure in `.env`

```env
# Enable offline tiles
MAP_OFFLINE_TILES_ENABLED=true

# Set path to tiles directory (absolute or relative to project root)
MAP_OFFLINE_TILES_PATH=/path/to/tiles
# OR
MAP_OFFLINE_TILES_PATH=./tiles
```

### Step 3: Restart Server

```bash
# Restart your FastAPI server
# The tiles will be automatically mounted at /tiles/{z}/{x}/{y}.png
```

---

## Tile Formats

### Directory Structure (Recommended)

**Format:** `{z}/{x}/{y}.png`

```
tiles/
├── 10/
│   ├── 512/
│   │   ├── 512.png
│   │   ├── 513.png
│   │   └── ...
│   └── 513/
│       └── ...
├── 11/
│   └── ...
└── 12/
    └── ...
```

**Advantages:**
- Simple to understand
- Easy to add/remove tiles
- Fast access
- Works directly with StaticFiles

### MBTiles Format

**Format:** Single `.mbtiles` file (SQLite database)

**Advantages:**
- Compact (compressed)
- Single file
- Standard format

**Disadvantages:**
- Requires extraction or special server
- Not directly supported yet (coming soon)

---

## Downloading Tiles

### Method 1: Mobile Atlas Creator (MOBAC)

1. **Download:** [https://mobac.sourceforge.io/](https://mobac.sourceforge.io/)
2. **Select Area:**
   - Use map to select region
   - Or enter coordinates
3. **Choose Source:**
   - OpenStreetMap (free)
   - Or other providers
4. **Select Zoom Levels:**
   - Recommended: 10-16 for city/region
   - 17-18 for detailed street view (larger files)
5. **Export Format:**
   - Choose "Osmdroid ZIP" or "Directory structure"
6. **Extract:**
   - Extract ZIP to your tiles directory
   - Ensure structure is: `{z}/{x}/{y}.png`

### Method 2: Python Script (Tiletanic)

```python
from tiletanic import tilecover
import requests
import os
from tqdm import tqdm

def download_tiles(bbox, zoom_levels, output_dir='tiles', tile_url_template='https://tile.openstreetmap.org/{z}/{x}/{y}.png'):
    """
    Download map tiles for a bounding box.
    
    Args:
        bbox: (min_lat, min_lng, max_lat, max_lng)
        zoom_levels: List of zoom levels (e.g., [10, 11, 12, 13, 14, 15, 16])
        output_dir: Directory to save tiles
        tile_url_template: URL template for tiles
    """
    os.makedirs(output_dir, exist_ok=True)
    
    total_tiles = 0
    for zoom in zoom_levels:
        tiles = list(tilecover.bbox(bbox, zoom))
        total_tiles += len(tiles)
    
    print(f"Total tiles to download: {total_tiles}")
    
    downloaded = 0
    with tqdm(total=total_tiles, desc="Downloading tiles") as pbar:
        for zoom in zoom_levels:
            tiles = tilecover.bbox(bbox, zoom)
            for tile in tiles:
                z, x, y = tile
                tile_dir = os.path.join(output_dir, str(z), str(x))
                os.makedirs(tile_dir, exist_ok=True)
                tile_path = os.path.join(tile_dir, f"{y}.png")
                
                # Skip if already downloaded
                if os.path.exists(tile_path):
                    pbar.update(1)
                    continue
                
                url = tile_url_template.format(z=z, x=x, y=y)
                try:
                    response = requests.get(url, timeout=5)
                    if response.status_code == 200:
                        with open(tile_path, 'wb') as f:
                            f.write(response.content)
                        downloaded += 1
                except Exception as e:
                    print(f"Error downloading {z}/{x}/{y}.png: {e}")
                
                pbar.update(1)
    
    print(f"Downloaded {downloaded} tiles to {output_dir}")

# Example usage
if __name__ == "__main__":
    # Example: Download tiles for a city (adjust coordinates)
    bbox = (40.7, -74.0, 40.8, -73.9)  # New York City area
    zoom_levels = [10, 11, 12, 13, 14, 15, 16]
    
    download_tiles(bbox, zoom_levels, output_dir='tiles')
```

**Install dependencies:**
```bash
pip install tiletanic requests tqdm
```

### Method 3: Using SAS Planet

1. **Download:** [https://www.sasgis.org/sasplanet/](https://www.sasgis.org/sasplanet/)
2. **Select Area:** Draw polygon on map
3. **Choose Provider:** OpenStreetMap or others
4. **Select Zoom Levels:** 10-16 recommended
5. **Export:** Choose "Osmdroid ZIP" format
6. **Extract:** Extract to tiles directory

---

## Configuration

### Environment Variables

Add to your `.env` file:

```env
# Enable offline map tiles
MAP_OFFLINE_TILES_ENABLED=true

# Path to tiles directory (absolute or relative)
# Absolute path example:
MAP_OFFLINE_TILES_PATH=/app/data/tiles

# Relative path example (relative to project root):
MAP_OFFLINE_TILES_PATH=./tiles

# Or use environment variable expansion:
MAP_OFFLINE_TILES_PATH=${PROJECT_ROOT}/tiles
```

### Config.py

The configuration is automatically loaded from `config.py`:

```python
MAP_OFFLINE_TILES_PATH: Optional[str] = Field(
    default=None,
    env="MAP_OFFLINE_TILES_PATH",
    description="Path to offline map tiles directory (format: {z}/{x}/{y}.png) or MBTiles file"
)

MAP_OFFLINE_TILES_ENABLED: bool = Field(
    default=False,
    env="MAP_OFFLINE_TILES_ENABLED",
    description="Enable offline map tiles (requires MAP_OFFLINE_TILES_PATH to be set)"
)
```

---

## Directory Structure

### Required Structure

```
tiles/
├── {z}/          # Zoom level (e.g., 10, 11, 12, ...)
│   ├── {x}/      # X coordinate (tile column)
│   │   ├── {y}.png   # Y coordinate (tile row) - PNG or JPG
│   │   ├── {y+1}.png
│   │   └── ...
│   ├── {x+1}/
│   │   └── ...
│   └── ...
├── 11/
│   └── ...
└── 12/
    └── ...
```

### Example

```
tiles/
├── 10/
│   ├── 512/
│   │   ├── 512.png
│   │   ├── 513.png
│   │   └── 514.png
│   └── 513/
│       ├── 512.png
│       └── 513.png
├── 11/
│   ├── 1024/
│   │   └── 1024.png
│   └── 1025/
│       └── 1025.png
└── 12/
    └── ...
```

### File Naming

- **PNG format:** `{y}.png` (recommended)
- **JPG format:** `{y}.jpg` (also supported)

---

## MBTiles Support

**Current Status:** MBTiles files are detected but require extraction to directory structure.

### Extracting MBTiles

**Option 1: Using mbutil**

```bash
# Install mbutil
pip install mbutil

# Extract MBTiles to directory structure
mb-util your_map.mbtiles tiles/
```

**Option 2: Using Python**

```python
import sqlite3
from PIL import Image
import os

def extract_mbtiles(mbtiles_path, output_dir='tiles'):
    """Extract MBTiles to directory structure."""
    conn = sqlite3.connect(mbtiles_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles")
    
    os.makedirs(output_dir, exist_ok=True)
    
    for zoom, x, y, tile_data in cursor:
        # MBTiles uses TMS Y coordinate (flipped)
        # Convert to standard Y
        max_y = 2 ** zoom - 1
        y = max_y - y
        
        tile_dir = os.path.join(output_dir, str(zoom), str(x))
        os.makedirs(tile_dir, exist_ok=True)
        tile_path = os.path.join(tile_dir, f"{y}.png")
        
        with open(tile_path, 'wb') as f:
            f.write(tile_data)
        
        print(f"Extracted: {zoom}/{x}/{y}.png")
    
    conn.close()

# Usage
extract_mbtiles('your_map.mbtiles', 'tiles')
```

---

## Verification

### Step 1: Check Directory Structure

```bash
# Verify tiles directory structure
ls -R tiles/ | head -20

# Should show:
# tiles/10/512/512.png
# tiles/10/512/513.png
# etc.
```

### Step 2: Check Server Logs

When server starts, you should see:

```
✅ Offline map tiles mounted at /tiles from /path/to/tiles
   Tiles will be served from: /tiles/{z}/{x}/{y}.png
```

### Step 3: Test Tile Access

```bash
# Test if tiles are accessible (replace with actual coordinates)
curl http://localhost/tiles/10/512/512.png

# Should return PNG image (not 404)
```

### Step 4: Test Map

1. Open intelligence page
2. Select an identity
3. Click "Map" button
4. Map should show actual tiles instead of grid background

---

## Troubleshooting

### Issue: Tiles Not Showing

**Symptoms:** Map still shows grid background

**Solutions:**
1. **Check Configuration:**
   ```bash
   # Verify .env settings
   grep MAP_OFFLINE_TILES .env
   ```

2. **Check Directory Structure:**
   ```bash
   # Verify structure
   ls -R tiles/ | grep -E "\.png$|\.jpg$" | head -5
   ```

3. **Check Server Logs:**
   - Look for: `✅ Offline map tiles mounted at /tiles`
   - Or: `⚠️ Offline tiles enabled but path not found`

4. **Check Tile Path:**
   - Ensure path is absolute or relative to project root
   - Check file permissions

### Issue: 404 Errors for Tiles

**Symptoms:** Browser console shows 404 for `/tiles/{z}/{x}/{y}.png`

**Solutions:**
1. **Verify Mount:**
   - Check server logs for tile mount confirmation
   - Restart server if needed

2. **Check Path:**
   - Ensure `MAP_OFFLINE_TILES_PATH` points to correct directory
   - Verify directory exists and is readable

3. **Check Structure:**
   - Ensure tiles follow `{z}/{x}/{y}.png` format
   - Check file names (should be `{y}.png`, not `{y}.PNG`)

### Issue: Tiles Load Slowly

**Solutions:**
1. **Reduce Zoom Levels:**
   - Only download necessary zoom levels
   - Higher zoom = more tiles = slower loading

2. **Optimize Images:**
   ```bash
   # Compress PNG files (optional)
   find tiles/ -name "*.png" -exec optipng -o2 {} \;
   ```

3. **Use CDN/Cache:**
   - Consider caching tiles in Redis
   - Or use nginx to serve tiles directly

### Issue: Wrong Area Showing

**Symptoms:** Tiles show different location than expected

**Solutions:**
1. **Check Coordinates:**
   - Verify bounding box when downloading
   - Ensure tiles cover your area of interest

2. **Check Zoom Levels:**
   - Lower zoom = wider area
   - Higher zoom = more detail but smaller area

---

## Best Practices

1. **Zoom Levels:**
   - **City/Region:** 10-14
   - **Detailed Street View:** 15-16
   - **Building Level:** 17-18 (very large files)

2. **Storage:**
   - Estimate: ~100KB per tile
   - 1000 tiles ≈ 100MB
   - City area (zoom 10-16) ≈ 500MB - 2GB

3. **Updates:**
   - Tiles don't auto-update
   - Re-download periodically for fresh data
   - Or use online tiles when internet available

4. **Legal:**
   - Respect tile provider terms of service
   - OpenStreetMap: Attribution required
   - Commercial providers: Check licensing

---

## Next Steps

- See [Chapter 8.5: Offline Map Setup](./55_OFFLINE_MAP_SETUP.md) for JS/CSS offline setup
- See [Chapter 8.1: Map Service Guide](./46_MAP_SERVICE_GUIDE.md) for map features
- See [Chapter 8.4: Security Intelligence Features](./48_SECURITY_INTELLIGENCE_MAP_FEATURES.md) for security features

---

**Last Updated:** 2026-01-11


