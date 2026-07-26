# Unified Storage Migration Guide

## Overview

The system now uses a **unified storage structure** to simplify path handling and eliminate confusion between `/app/assets/faces` and `/app/storage`.

## New Storage Structure

```
/app/storage/
├── faces/              # Known faces (uploaded persons)
│   ├── person1_20260110_063513.jpg
│   └── person2_20260110_063514.jpg
└── {pipeline_id}/     # Detected faces from pipelines
    ├── person_name/
    │   └── image.jpg
    └── unknown/
        └── image.jpg
```

## Changes Made

### 1. Configuration (`config.py`)
- **Before:** `FACES_DIR = "/app/assets/faces"`
- **After:** `FACES_DIR = "/app/storage/faces"`

### 2. Docker Compose
- **Removed:** Separate volume mount for `../assets/faces:/app/assets/faces`
- **Unified:** All storage now in `../storage:/app/storage`
- Known faces are stored in `storage/faces/` subdirectory

### 3. Docker Entrypoint (`docker-entrypoint.sh`)
- Creates `storage/faces/` directory on startup
- Ensures proper permissions for unified storage

### 4. Path Utilities (`backend/utils/path_utils.py`)
- Updated to recognize `storage/faces/` as valid storage path
- Handles both `storage/faces/` and `storage/{pipeline_id}/` paths

## Migration Steps

### Option 1: Automatic (Recommended)
The system will automatically create the new structure. Existing files in `assets/faces/` will need to be moved manually.

### Option 2: Manual Migration
If you have existing known faces in `assets/faces/`, move them:

```bash
# On host (outside Docker)
mkdir -p storage/faces
mv assets/faces/* storage/faces/ 2>/dev/null || true
```

### Option 3: Docker Volume Migration
If using Docker volumes:

```bash
# Copy files from old location to new location
docker exec face_recognition_backend sh -c "mkdir -p /app/storage/faces && cp -r /app/assets/faces/* /app/storage/faces/ 2>/dev/null || true"
```

## Benefits

1. **Single Persistent Volume:** All data in one place (`storage/`)
2. **Simplified Paths:** No confusion between `/app/assets` and `/app/storage`
3. **Easier Backups:** One directory to backup
4. **Consistent Structure:** All files follow same pattern
5. **Better Organization:** Clear separation: `faces/` vs `{pipeline_id}/`

## Verification

After migration, verify:

1. **Known faces location:**
   ```bash
   ls -la storage/faces/
   ```

2. **Upload test:**
   - Upload a new person via "Add Person" button
   - Check that file appears in `storage/faces/`

3. **Path normalization:**
   - Check logs for path normalization warnings
   - Should see `storage/faces/` paths, not `/app/assets/faces`

## Rollback (if needed)

If you need to rollback:

1. Update `config.py`: `FACES_DIR = "/app/assets/faces"`
2. Restore docker-compose volume: `../assets/faces:/app/assets/faces`
3. Restart containers

## Notes

- Old `assets/faces/` directory can be kept for reference
- No data loss - files are just moved to new location
- Database records will automatically use new paths
- Static file serving handles both old and new paths during transition

