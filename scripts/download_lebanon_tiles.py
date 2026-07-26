#!/usr/bin/env python3
"""
Download OpenStreetMap Tiles for Lebanon
=========================================

This script downloads map tiles for Lebanon and saves them in the directory
structure required by the map service: {z}/{x}/{y}.png

Usage:
    python scripts/download_lebanon_tiles.py

Configuration:
    Edit the variables below to customize:
    - output_dir: Where to save tiles (default: ./tiles)
    - zoom_levels: Which zoom levels to download (default: 10-16)
    - tile_source: URL template for tile source (default: OpenStreetMap)
"""

import os
import sys
import requests
import time
from pathlib import Path
from typing import Tuple, List
from tqdm import tqdm

# ============================================================================
# CONFIGURATION
# ============================================================================

# Lebanon bounding box (min_lat, min_lng, max_lat, max_lng)
# Coordinates cover all of Lebanon
LEBANON_BBOX = (33.0, 35.0, 34.7, 36.6)

# Output directory for tiles
OUTPUT_DIR = "./tiles"

# Zoom levels to download
# 10-14: Good for city/region overview (smaller file size)
# 15-16: Detailed street view (larger file size)
# 17-18: Building level detail (very large, not recommended for full country)
ZOOM_LEVELS = list(range(10, 17))  # 10, 11, 12, 13, 14, 15, 16

# Tile source URL template
# OpenStreetMap (free, requires attribution)
TILE_URL_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# Alternative tile sources (uncomment to use):
# TILE_URL_TEMPLATE = "https://tile.openstreetmap.de/{z}/{x}/{y}.png"  # German OSM mirror
# TILE_URL_TEMPLATE = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"  # With subdomains

# Download settings
MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds
REQUEST_TIMEOUT = 10  # seconds
DELAY_BETWEEN_REQUESTS = 0.1  # seconds (to be respectful to tile servers)

# ============================================================================
# TILE CALCULATION FUNCTIONS
# ============================================================================

def deg2num(lat_deg: float, lon_deg: float, zoom: int) -> Tuple[int, int]:
    """
    Convert latitude/longitude to tile coordinates.
    
    Args:
        lat_deg: Latitude in degrees
        lon_deg: Longitude in degrees
        zoom: Zoom level
        
    Returns:
        (x, y) tile coordinates
    """
    import math
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return (x, y)


def get_tile_range(bbox: Tuple[float, float, float, float], zoom: int) -> Tuple[int, int, int, int]:
    """
    Get the range of tiles needed for a bounding box at a given zoom level.
    
    Args:
        bbox: (min_lat, min_lng, max_lat, max_lng)
        zoom: Zoom level
        
    Returns:
        (min_x, min_y, max_x, max_y) tile coordinates
    """
    min_lat, min_lng, max_lat, max_lng = bbox
    
    # Top-left corner: max_lat (north), min_lng (west) -> gives min_x, min_y
    min_x, min_y = deg2num(max_lat, min_lng, zoom)
    
    # Bottom-right corner: min_lat (south), max_lng (east) -> gives max_x, max_y
    max_x, max_y = deg2num(min_lat, max_lng, zoom)
    
    return (min_x, min_y, max_x, max_y)


def download_tile(z: int, x: int, y: int, output_dir: str, url_template: str) -> bool:
    """
    Download a single tile.
    
    Args:
        z: Zoom level
        x: X coordinate
        y: Y coordinate
        output_dir: Output directory
        url_template: URL template for tiles
        
    Returns:
        True if successful, False otherwise
    """
    tile_dir = os.path.join(output_dir, str(z), str(x))
    os.makedirs(tile_dir, exist_ok=True)
    tile_path = os.path.join(tile_dir, f"{y}.png")
    
    # Skip if already downloaded
    if os.path.exists(tile_path):
        return True
    
    url = url_template.format(z=z, x=x, y=y)
    
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                with open(tile_path, 'wb') as f:
                    f.write(response.content)
                return True
            elif response.status_code == 404:
                # Tile doesn't exist (common at high zoom levels)
                return False
            else:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (attempt + 1))
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                print(f"  Error downloading {z}/{x}/{y}.png: {e}")
    
    return False


def estimate_tile_count(bbox: Tuple[float, float, float, float], zoom_levels: List[int]) -> int:
    """
    Estimate the total number of tiles to download.
    
    Args:
        bbox: Bounding box
        zoom_levels: List of zoom levels
        
    Returns:
        Estimated tile count
    """
    total = 0
    for zoom in zoom_levels:
        min_x, min_y, max_x, max_y = get_tile_range(bbox, zoom)
        count = (max_x - min_x + 1) * (max_y - min_y + 1)
        total += count
    return total


# ============================================================================
# MAIN DOWNLOAD FUNCTION
# ============================================================================

def download_tiles_for_lebanon():
    """
    Download all tiles for Lebanon.
    """
    print("=" * 70)
    print("Lebanon Map Tiles Downloader")
    print("=" * 70)
    print(f"Bounding Box: {LEBANON_BBOX}")
    print(f"Zoom Levels: {ZOOM_LEVELS}")
    print(f"Output Directory: {OUTPUT_DIR}")
    print(f"Tile Source: {TILE_URL_TEMPLATE}")
    print("=" * 70)
    
    # Create output directory
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Estimate total tiles
    total_tiles = estimate_tile_count(LEBANON_BBOX, ZOOM_LEVELS)
    print(f"\nEstimated tiles to download: {total_tiles:,}")
    print("This may take a while...\n")
    
    # Download tiles
    downloaded = 0
    skipped = 0
    failed = 0
    
    with tqdm(total=total_tiles, desc="Downloading tiles", unit="tile") as pbar:
        for zoom in ZOOM_LEVELS:
            min_x, min_y, max_x, max_y = get_tile_range(LEBANON_BBOX, zoom)
            
            print(f"\nZoom level {zoom}: {min_x}-{max_x} x {min_y}-{max_y} "
                  f"({(max_x - min_x + 1) * (max_y - min_y + 1)} tiles)")
            
            for x in range(min_x, max_x + 1):
                for y in range(min_y, max_y + 1):
                    success = download_tile(zoom, x, y, OUTPUT_DIR, TILE_URL_TEMPLATE)
                    
                    if success:
                        downloaded += 1
                    elif os.path.exists(os.path.join(OUTPUT_DIR, str(zoom), str(x), f"{y}.png")):
                        skipped += 1
                    else:
                        failed += 1
                    
                    pbar.update(1)
                    
                    # Be respectful to tile servers
                    time.sleep(DELAY_BETWEEN_REQUESTS)
    
    # Summary
    print("\n" + "=" * 70)
    print("Download Complete!")
    print("=" * 70)
    print(f"Downloaded: {downloaded:,} tiles")
    print(f"Skipped (already exist): {skipped:,} tiles")
    print(f"Failed: {failed:,} tiles")
    print(f"Total: {downloaded + skipped + failed:,} tiles")
    
    # Calculate size
    total_size = 0
    for root, dirs, files in os.walk(OUTPUT_DIR):
        for file in files:
            filepath = os.path.join(root, file)
            total_size += os.path.getsize(filepath)
    
    size_mb = total_size / (1024 * 1024)
    size_gb = total_size / (1024 * 1024 * 1024)
    
    if size_gb >= 1:
        print(f"Total size: {size_gb:.2f} GB")
    else:
        print(f"Total size: {size_mb:.2f} MB")
    
    print(f"\nTiles saved to: {os.path.abspath(OUTPUT_DIR)}")
    print("\nNext steps:")
    print("1. Set in .env: MAP_OFFLINE_TILES_ENABLED=true")
    print(f"2. Set in .env: MAP_OFFLINE_TILES_PATH={os.path.abspath(OUTPUT_DIR)}")
    print("3. Restart your server")
    print("=" * 70)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    try:
        download_tiles_for_lebanon()
    except KeyboardInterrupt:
        print("\n\nDownload interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

