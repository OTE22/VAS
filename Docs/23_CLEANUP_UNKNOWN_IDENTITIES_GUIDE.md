# Cleanup Unknown Identities - Complete Guide

> **Vector backend note.** Where this document says *FAISS*, the live
> system uses **PostgreSQL + pgvector**. PostgreSQL is authoritative and
> the index is a disposable acceleration layer — see
> [`70_VECTOR_INDEX_CONTRACT.md`](70_VECTOR_INDEX_CONTRACT.md). The
> surrounding explanation of *what* the index does is still accurate.

## Overview

This guide explains how to remove all unknown identities and their related data from the database to start fresh.

## ⚠️ WARNING

**This operation is IRREVERSIBLE!** Make sure you have a database backup before running the cleanup script.

## What Gets Deleted

The cleanup script removes:

1. **Identity Records**: All identities with `type='unknown'`
2. **IdentityAppearance Records**: All appearance records for unknown identities
3. **IdentityEmbedding Records**: All embedding records for unknown identities
4. **Face Links**: Sets `Face.identity_id` to `NULL` (preserves Face records for historical data)
5. **FAISS Index**: Removes embeddings from the unknown FAISS index
6. **MergeSuggestion Records**: Removes merge suggestions referencing unknown identities
7. **IdentityMerge Records**: Removes merge history for unknown identities
8. **IdentityAuditLog Records**: Removes audit logs for unknown identities
9. **Image Files** (optional): Deletes image files from disk if `--delete-images` flag is used

## Prerequisites

1. **Database Backup**: Create a backup before running
2. **Access**: You need access to the database and file system
3. **Python Environment**: Ensure you're in the correct Python environment

## Usage

### Step 1: Dry Run (Recommended First)

Always run a dry run first to see what will be deleted:

```bash
# Inside Docker container
docker exec -it face_recognition_api python scripts/maintenance/cleanup_unknown_identities.py --dry-run

# Or locally (if running outside Docker)
python scripts/maintenance/cleanup_unknown_identities.py --dry-run
```

This will show you:
- How many unknown identities exist
- How many related records will be affected
- How many image files will be deleted (if using `--delete-images`)

### Step 2: Run Actual Cleanup

#### Option A: Database Only (Keep Images)

```bash
docker exec -it face_recognition_api python scripts/maintenance/cleanup_unknown_identities.py
```

This will:
- ✅ Remove all unknown identities from database
- ✅ Clean up all related database records
- ✅ Remove from FAISS index
- ❌ Keep image files on disk

#### Option B: Database + Images (Complete Cleanup)

```bash
docker exec -it face_recognition_api python scripts/maintenance/cleanup_unknown_identities.py --delete-images
```

This will:
- ✅ Remove all unknown identities from database
- ✅ Clean up all related database records
- ✅ Remove from FAISS index
- ✅ Delete all image files from disk

### Step 3: Verification

After cleanup, verify the results:

```bash
# Check unknown identities count (should be 0)
docker exec -it face_recognition_api python -c "
import asyncio
from db_connection import db_manager
from db_models import Identity, IdentityType
from sqlalchemy import select, func

async def check():
    async with db_manager.get_session() as db:
        result = await db.execute(
            select(func.count(Identity.id)).where(Identity.type == IdentityType.UNKNOWN)
        )
        count = result.scalar() or 0
        print(f'Unknown identities remaining: {count}')

asyncio.run(check())
"
```

## Example Output

### Dry Run Output

```
============================================================
UNKNOWN IDENTITIES CLEANUP SCRIPT
============================================================
DRY RUN MODE - No changes will be made

[Step 1] Finding unknown identities...
Found 15 unknown identities

[Step 2] Analyzing related data...
  - Faces linked to unknown identities: 45
  - IdentityAppearance records: 120
  - IdentityEmbedding records: 150
  - MergeSuggestion records: 3
  - IdentityMerge records: 2
  - IdentityAuditLog records: 25

[Step 3] Finding image files...
Found 180 unique image file paths
  - 175 files exist on disk
  - 5 files not found on disk

[Step 4] Starting cleanup...
  [5a] Setting Face.identity_id to NULL...
    [DRY RUN] Would set 45 face records to identity_id=NULL
  [5b] Deleting IdentityEmbedding records...
    [DRY RUN] Would delete 150 embedding records
  [5c] Deleting IdentityAppearance records...
    [DRY RUN] Would delete 120 appearance records
  [5d] Deleting IdentityMerge records...
    [DRY RUN] Would delete 2 merge records
  [5e] Deleting IdentityAuditLog records...
    [DRY RUN] Would delete 25 audit log records
  [5f] Cleaning up MergeSuggestion records...
    [DRY RUN] Would delete 3 merge suggestion records
  [5g] Removing from FAISS index...
    [DRY RUN] Would remove identity ... from FAISS unknown index
  [5h] Deleting Identity records...
    [DRY RUN] Would delete 15 identity records
  [5i] Deleting image files from disk...
    [DRY RUN] Would delete 175 image files

✅ Dry run completed - no changes made
```

### Live Run Output

```
============================================================
UNKNOWN IDENTITIES CLEANUP SCRIPT
============================================================
LIVE MODE - Changes will be PERMANENT

[Step 1] Finding unknown identities...
Found 15 unknown identities

[Step 2] Analyzing related data...
  - Faces linked to unknown identities: 45
  - IdentityAppearance records: 120
  - IdentityEmbedding records: 150
  - MergeSuggestion records: 3
  - IdentityMerge records: 2
  - IdentityAuditLog records: 25

[Step 3] Finding image files...
Found 180 unique image file paths
  - 175 files exist on disk
  - 5 files not found on disk

============================================================
WARNING: This will PERMANENTLY DELETE:
  - 15 unknown identities
  - 120 appearance records
  - 150 embedding records
  - 45 face links (will be set to NULL)
  - 180 image files from disk
============================================================

Type 'DELETE' to confirm: DELETE

[Step 4] Starting cleanup...
  [5a] Setting Face.identity_id to NULL...
    ✅ Set 45 face records to identity_id=NULL
  [5b] Deleting IdentityEmbedding records...
    ✅ Deleted 150 embedding records
  [5c] Deleting IdentityAppearance records...
    ✅ Deleted 120 appearance records
  [5d] Deleting IdentityMerge records...
    ✅ Deleted 2 merge records
  [5e] Deleting IdentityAuditLog records...
    ✅ Deleted 25 audit log records
  [5f] Cleaning up MergeSuggestion records...
    ✅ Deleted 3 merge suggestion records
  [5g] Removing from FAISS index...
    ✅ Removed 150 embeddings from FAISS unknown index
  [5h] Deleting Identity records...
    ✅ Deleted 15 identity records
  [5i] Deleting image files from disk...
    ✅ Deleted 175 image files

✅ Cleanup completed successfully!

[Step 5] Verifying cleanup...
✅ All unknown identities have been removed
```

## Manual SQL Alternative

If you prefer to use SQL directly, here's the SQL equivalent:

```sql
-- Step 1: Set Face.identity_id to NULL for unknown identities
UPDATE faces 
SET identity_id = NULL 
WHERE identity_id IN (
    SELECT id FROM identities WHERE type = 'unknown'
);

-- ⚠️ DESTRUCTIVE AND IRREVERSIBLE. Take a database backup first
-- (Docs/60_BACKUP_AND_RESTORE.md) and prefer the Python script, which also
-- removes the stored image files these rows point at.

-- Step 2: Delete related records
DELETE FROM identity_embeddings 
WHERE identity_id IN (SELECT id FROM identities WHERE type = 'unknown');

DELETE FROM identity_appearances 
WHERE identity_id IN (SELECT id FROM identities WHERE type = 'unknown');

DELETE FROM identity_merges 
WHERE from_identity_id IN (SELECT id FROM identities WHERE type = 'unknown')
   OR to_identity_id IN (SELECT id FROM identities WHERE type = 'unknown');

DELETE FROM identity_audit_log 
WHERE identity_id IN (SELECT id FROM identities WHERE type = 'unknown')
   OR related_identity_id IN (SELECT id FROM identities WHERE type = 'unknown');

-- Step 3: Delete unknown identities
DELETE FROM identities WHERE type = 'unknown';
```

**Note**: SQL approach doesn't handle FAISS index cleanup. Use the Python script for complete cleanup.

## Troubleshooting

### Error: "Database connection failed"
**Solution**: 
- Ensure the database is running
- Check `DATABASE_URL` in your config
- Verify you're running from the correct directory

### Error: "FAISS index not available"
**Solution**: 
- This is not critical - the script will continue
- FAISS cleanup is optional
- Database cleanup will still work

### Error: "Permission denied" when deleting images
**Solution**: 
- Check file permissions
- Ensure the script has write access to storage directory
- Run with appropriate permissions

### Images not deleted
**Solution**: 
- Check if `--delete-images` flag was used
- Verify image paths in database match actual file locations
- Some images may have been manually moved/deleted

## After Cleanup

1. **Verify Database**: Check that unknown identities are gone
2. **Check FAISS Index**: Verify unknown index is cleaned (if available)
3. **Verify Images**: If using `--delete-images`, check storage directory
4. **Restart Services**: Restart the application to ensure clean state

## Starting Fresh

After cleanup, the system will:
- ✅ Create new unknown identities for new detections
- ✅ Start with a clean unknown identities database
- ✅ Maintain all known identities (not affected)
- ✅ Preserve Face records (with `identity_id=NULL` for historical data)

## Safety Tips

1. **Always backup first**: `pg_dump` your database
2. **Test in staging**: Run cleanup on a test environment first
3. **Use dry-run**: Always run `--dry-run` first
4. **Monitor logs**: Watch for any errors during cleanup
5. **Verify results**: Check counts after cleanup

## Related Files

- `cleanup_unknown_identities.py` - The cleanup script
- `db_models.py` - Database model definitions
- `backend/core/vector_index/` - FAISS index management

## Support

If you encounter issues:
1. Check the logs for detailed error messages
2. Verify database connectivity
3. Ensure all dependencies are installed
4. Check file system permissions

