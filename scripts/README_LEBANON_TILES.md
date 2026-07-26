# Download Map Tiles for Lebanon

This script downloads OpenStreetMap tiles for Lebanon and saves them in the directory structure required by the map service.

## Quick Start

### Step 1: Install Dependencies

```bash
pip install requests tqdm
```

Or if using the project's requirements:

```bash
pip install -r requirements-cpu.txt
```

### Step 2: Run the Script

```bash
python scripts/download_lebanon_tiles.py
```

The script will:
- Download tiles for all of Lebanon
- Save them to `./tiles` directory
- Show progress with a progress bar
- Display summary statistics

### Step 3: Configure Your Server

Add to your `.env` file:

```env
MAP_OFFLINE_TILES_ENABLED=true
MAP_OFFLINE_TILES_PATH=./tiles
```

### Step 4: Restart Server

Restart your FastAPI server. The tiles will be automatically mounted and served.

## Configuration

Edit the script to customize:

### Change Output Directory

```python
OUTPUT_DIR = "./tiles"  # Change to your preferred path
```

### Change Zoom Levels

```python
# Lower zoom = wider area, less detail, smaller files
# Higher zoom = more detail, larger files

ZOOM_LEVELS = list(range(10, 17))  # 10-16 (recommended)
# ZOOM_LEVELS = list(range(10, 15))  # 10-14 (smaller files)
# ZOOM_LEVELS = list(range(10, 19))  # 10-18 (very large files)
```

### Change Tile Source

```python
# OpenStreetMap (default, free)
TILE_URL_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# German OSM mirror (alternative)
# TILE_URL_TEMPLATE = "https://tile.openstreetmap.de/{z}/{x}/{y}.png"
```

### Adjust Download Speed

```python
DELAY_BETWEEN_REQUESTS = 0.1  # seconds (be respectful to tile servers)
```

## What Gets Downloaded

### Lebanon Coverage

The script downloads tiles for the entire country of Lebanon:
- **Bounding Box:** 33.0°N, 35.0°E to 34.7°N, 36.6°E
- **Covers:** All of Lebanon including Beirut, Tripoli, Sidon, Tyre, and all regions

### Zoom Levels

By default, downloads zoom levels 10-16:
- **Zoom 10-12:** Country/region overview
- **Zoom 13-14:** City level
- **Zoom 15-16:** Street level detail

### File Structure

Tiles are saved in the standard format:
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
└── ...
```

## Estimated Size

### Zoom Levels 10-16 (Default)
- **Estimated tiles:** ~50,000 - 100,000 tiles
- **Estimated size:** 500 MB - 2 GB
- **Download time:** 1-3 hours (depending on connection)

### Zoom Levels 10-14 (Smaller)
- **Estimated tiles:** ~10,000 - 20,000 tiles
- **Estimated size:** 100 MB - 500 MB
- **Download time:** 15-30 minutes

### Zoom Levels 10-18 (Very Detailed)
- **Estimated tiles:** ~500,000 - 1,000,000 tiles
- **Estimated size:** 5 GB - 20 GB
- **Download time:** 10+ hours

## Troubleshooting

### Issue: Download is Slow

**Solution:**
- This is normal - downloading thousands of tiles takes time
- The script includes delays to be respectful to tile servers
- Consider downloading only zoom levels 10-14 for faster download

### Issue: Some Tiles Fail to Download

**Solution:**
- Some tiles may not exist at high zoom levels (404 errors are normal)
- The script will retry failed tiles up to 3 times
- Check the summary at the end - a few failures are normal

### Issue: Out of Disk Space

**Solution:**
- Download fewer zoom levels (e.g., 10-14 instead of 10-16)
- Use a different output directory with more space
- Clean up old tiles if re-downloading

### Issue: Connection Errors

**Solution:**
- Check your internet connection
- The script will retry failed downloads automatically
- If many tiles fail, try again later (tile servers may be busy)

## Advanced Usage

### Download Specific Region Only

Edit the bounding box in the script:

```python
# Example: Beirut area only
LEBANON_BBOX = (33.8, 35.4, 33.9, 35.6)  # Smaller area
```

### Resume Interrupted Download

The script automatically skips tiles that already exist, so you can:
1. Stop the script (Ctrl+C)
2. Run it again
3. It will continue from where it left off

### Download in Background

```bash
# Linux/Mac
nohup python scripts/download_lebanon_tiles.py > download.log 2>&1 &

# Windows (PowerShell)
Start-Process python -ArgumentList "scripts/download_lebanon_tiles.py" -WindowStyle Hidden
```

## Legal Notice

**OpenStreetMap Attribution Required:**

When using OpenStreetMap tiles, you must include attribution:

```
© OpenStreetMap contributors
```

The map service automatically includes this attribution when using offline tiles.

## Next Steps

After downloading tiles:

1. **Verify Download:**
   ```bash
   ls -R tiles/ | head -20  # Check structure
   ```

2. **Configure Server:**
   - Add to `.env`: `MAP_OFFLINE_TILES_ENABLED=true`
   - Add to `.env`: `MAP_OFFLINE_TILES_PATH=./tiles`

3. **Restart Server:**
   - Restart FastAPI server
   - Check logs for: `✅ Offline map tiles mounted at /tiles`

4. **Test Map:**
   - Open intelligence page
   - Select an identity
   - Click "Map" button
   - Map should show Lebanon tiles instead of grid

## Support

For issues or questions:
- Check [Chapter 8.6: Offline Map Tiles Setup](../Docs/56_OFFLINE_MAP_TILES_SETUP.md)
- Check server logs for tile mount status
- Verify directory structure matches `{z}/{x}/{y}.png` format


