"""
Cleanup Script: Remove All Unknown Identities
==============================================
This script removes all unknown identities and their related data from the database.

WARNING: This operation is IRREVERSIBLE. Make sure you have a database backup before running.

What gets deleted:
1. All Identity records with type='unknown'
2. All IdentityAppearance records for unknown identities (cascade)
3. All IdentityEmbedding records for unknown identities (cascade)
4. Face.identity_id set to NULL for unknown identities (preserves Face records)
5. Related MergeSuggestion records
6. Related IdentityMerge records
7. Related IdentityAuditLog records
8. Optionally: Image files from disk

Usage:
    python cleanup_unknown_identities.py [--delete-images] [--dry-run]
    
Options:
    --delete-images: Also delete image files from storage directory
    --dry-run: Show what would be deleted without actually deleting
"""

import os
import sys
import asyncio
import argparse
from pathlib import Path

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_connection import db_manager
from db_models import (
    Identity, IdentityType, IdentityAppearance, IdentityEmbedding,
    Face, MergeSuggestion, IdentityMerge, IdentityAuditLog
)
from sqlalchemy import select, update, delete, func
from config import settings
import logging

# Index access goes through the contract. Running standalone there is usually
# no in-process index at all — which is fine: the index is derived state, so a
# script that deletes rows can leave re-derivation to reconciliation.
try:
    from backend.core.vector_index.access import (get_vector_index,
                                                  remove_identity_vectors)
    IDENTITY_INDEX_AVAILABLE = True
except ImportError:
    IDENTITY_INDEX_AVAILABLE = False
    get_vector_index = None
    remove_identity_vectors = None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def get_unknown_identities_count(db):
    """Get count of unknown identities"""
    result = await db.execute(
        select(func.count(Identity.id)).where(Identity.type == IdentityType.UNKNOWN)
    )
    return result.scalar() or 0


async def get_related_data_counts(db, identity_ids):
    """Get counts of related data for unknown identities"""
    if not identity_ids:
        return {
            'faces': 0,
            'appearances': 0,
            'embeddings': 0,
            'merge_suggestions': 0,
            'identity_merges': 0,
            'audit_logs': 0
        }
    
    # Count faces
    faces_result = await db.execute(
        select(func.count(Face.id)).where(Face.identity_id.in_(identity_ids))
    )
    faces_count = faces_result.scalar() or 0
    
    # Count appearances
    appearances_result = await db.execute(
        select(func.count(IdentityAppearance.id)).where(
            IdentityAppearance.identity_id.in_(identity_ids)
        )
    )
    appearances_count = appearances_result.scalar() or 0
    
    # Count embeddings
    embeddings_result = await db.execute(
        select(func.count(IdentityEmbedding.id)).where(
            IdentityEmbedding.identity_id.in_(identity_ids)
        )
    )
    embeddings_count = embeddings_result.scalar() or 0
    
    # Count merge suggestions (that reference unknown identities)
    merge_suggestions_result = await db.execute(
        select(func.count(MergeSuggestion.id))
    )
    merge_suggestions_count = merge_suggestions_result.scalar() or 0
    
    # Count identity merges
    identity_merges_result = await db.execute(
        select(func.count(IdentityMerge.id)).where(
            (IdentityMerge.from_identity_id.in_(identity_ids)) |
            (IdentityMerge.to_identity_id.in_(identity_ids))
        )
    )
    identity_merges_count = identity_merges_result.scalar() or 0
    
    # Count audit logs
    audit_logs_result = await db.execute(
        select(func.count(IdentityAuditLog.id)).where(
            (IdentityAuditLog.identity_id.in_(identity_ids)) |
            (IdentityAuditLog.related_identity_id.in_(identity_ids))
        )
    )
    audit_logs_count = audit_logs_result.scalar() or 0
    
    return {
        'faces': faces_count,
        'appearances': appearances_count,
        'embeddings': embeddings_count,
        'merge_suggestions': merge_suggestions_count,
        'identity_merges': identity_merges_count,
        'audit_logs': audit_logs_count
    }


async def get_image_paths(db, identity_ids):
    """Get all image paths for unknown identities"""
    if not identity_ids:
        return []
    
    image_paths = []
    
    # Get paths from Identity.best_snapshot_path
    identities_result = await db.execute(
        select(Identity.best_snapshot_path).where(
            Identity.id.in_(identity_ids),
            Identity.best_snapshot_path.isnot(None)
        )
    )
    for row in identities_result:
        if row[0]:
            image_paths.append(row[0])
    
    # Get paths from IdentityAppearance.best_snapshot_path
    appearances_result = await db.execute(
        select(IdentityAppearance.best_snapshot_path).where(
            IdentityAppearance.identity_id.in_(identity_ids),
            IdentityAppearance.best_snapshot_path.isnot(None)
        )
    )
    for row in appearances_result:
        if row[0]:
            image_paths.append(row[0])
    
    # Get paths from Face.face_image_path where name='Unknown'
    faces_result = await db.execute(
        select(Face.face_image_path).where(
            Face.identity_id.in_(identity_ids),
            Face.face_image_path.isnot(None)
        )
    )
    for row in faces_result:
        if row[0]:
            image_paths.append(row[0])
    
    # Also get paths from faces with name='Unknown' even if identity_id is NULL
    # (in case some faces weren't linked to identities)
    unknown_faces_result = await db.execute(
        select(Face.face_image_path).where(
            Face.name == "Unknown",
            Face.face_image_path.isnot(None)
        )
    )
    for row in unknown_faces_result:
        if row[0]:
            image_paths.append(row[0])
    
    # Remove duplicates and None values
    image_paths = list(set([p for p in image_paths if p]))
    return image_paths


async def cleanup_unknown_identities(db, delete_images=False, dry_run=False, skip_confirmation=False):
    """Main cleanup function"""
    logger.info("=" * 60)
    logger.info("UNKNOWN IDENTITIES CLEANUP SCRIPT")
    logger.info("=" * 60)
    
    if dry_run:
        logger.warning("DRY RUN MODE - No changes will be made")
    else:
        logger.warning("LIVE MODE - Changes will be PERMANENT")
    
    # Step 1: Get all unknown identity IDs
    logger.info("\n[Step 1] Finding unknown identities...")
    result = await db.execute(
        select(Identity.id).where(Identity.type == IdentityType.UNKNOWN)
    )
    identity_ids = [row[0] for row in result]
    unknown_count = len(identity_ids)
    
    if unknown_count == 0:
        logger.info("✅ No unknown identities found. Nothing to clean up.")
        return
    
    logger.info(f"Found {unknown_count} unknown identities")
    
    # Step 2: Get related data counts
    logger.info("\n[Step 2] Analyzing related data...")
    counts = await get_related_data_counts(db, identity_ids)
    logger.info(f"  - Faces linked to unknown identities: {counts['faces']}")
    logger.info(f"  - IdentityAppearance records: {counts['appearances']}")
    logger.info(f"  - IdentityEmbedding records: {counts['embeddings']}")
    logger.info(f"  - MergeSuggestion records: {counts['merge_suggestions']}")
    logger.info(f"  - IdentityMerge records: {counts['identity_merges']}")
    logger.info(f"  - IdentityAuditLog records: {counts['audit_logs']}")
    
    # Step 3: Get image paths
    logger.info("\n[Step 3] Finding image files...")
    image_paths = await get_image_paths(db, identity_ids)
    logger.info(f"Found {len(image_paths)} unique image file paths")
    
    if delete_images:
        # Count files that actually exist
        existing_files = [p for p in image_paths if os.path.exists(p)]
        logger.info(f"  - {len(existing_files)} files exist on disk")
        if len(existing_files) < len(image_paths):
            logger.warning(f"  - {len(image_paths) - len(existing_files)} files not found on disk")
    
    # Step 4: Confirm deletion
    if not dry_run:
        logger.warning("\n" + "=" * 60)
        logger.warning("WARNING: This will PERMANENTLY DELETE:")
        logger.warning(f"  - {unknown_count} unknown identities")
        logger.warning(f"  - {counts['appearances']} appearance records")
        logger.warning(f"  - {counts['embeddings']} embedding records")
        logger.warning(f"  - {counts['faces']} face links (will be set to NULL)")
        if delete_images:
            logger.warning(f"  - {len(image_paths)} image files from disk")
        logger.warning("=" * 60)
        
        if not skip_confirmation:
            response = input("\nType 'DELETE' to confirm: ")
            if response != "DELETE":
                logger.info("❌ Cleanup cancelled by user")
                return
        else:
            logger.warning("⚠️  --yes flag detected, skipping confirmation prompt")
    
    # Step 5: Perform cleanup
    logger.info("\n[Step 4] Starting cleanup...")
    
    try:
        # 5a. Set Face.identity_id to NULL (preserve Face records)
        logger.info("  [5a] Setting Face.identity_id to NULL...")
        if not dry_run:
            await db.execute(
                update(Face).where(
                    Face.identity_id.in_(identity_ids)
                ).values(identity_id=None)
            )
            logger.info(f"    ✅ Set {counts['faces']} face records to identity_id=NULL")
        else:
            logger.info(f"    [DRY RUN] Would set {counts['faces']} face records to identity_id=NULL")
        
        # 5b. Delete IdentityEmbedding records (cascade will handle, but explicit is cleaner)
        logger.info("  [5b] Deleting IdentityEmbedding records...")
        if not dry_run:
            await db.execute(
                delete(IdentityEmbedding).where(
                    IdentityEmbedding.identity_id.in_(identity_ids)
                )
            )
            logger.info(f"    ✅ Deleted {counts['embeddings']} embedding records")
        else:
            logger.info(f"    [DRY RUN] Would delete {counts['embeddings']} embedding records")
        
        # 5c. Delete IdentityAppearance records (cascade will handle, but explicit is cleaner)
        logger.info("  [5c] Deleting IdentityAppearance records...")
        if not dry_run:
            await db.execute(
                delete(IdentityAppearance).where(
                    IdentityAppearance.identity_id.in_(identity_ids)
                )
            )
            logger.info(f"    ✅ Deleted {counts['appearances']} appearance records")
        else:
            logger.info(f"    [DRY RUN] Would delete {counts['appearances']} appearance records")
        
        # 5d. Delete IdentityMerge records
        logger.info("  [5d] Deleting IdentityMerge records...")
        if not dry_run:
            await db.execute(
                delete(IdentityMerge).where(
                    (IdentityMerge.from_identity_id.in_(identity_ids)) |
                    (IdentityMerge.to_identity_id.in_(identity_ids))
                )
            )
            logger.info(f"    ✅ Deleted {counts['identity_merges']} merge records")
        else:
            logger.info(f"    [DRY RUN] Would delete {counts['identity_merges']} merge records")
        
        # 5e. Delete IdentityAuditLog records
        logger.info("  [5e] Deleting IdentityAuditLog records...")
        if not dry_run:
            await db.execute(
                delete(IdentityAuditLog).where(
                    (IdentityAuditLog.identity_id.in_(identity_ids)) |
                    (IdentityAuditLog.related_identity_id.in_(identity_ids))
                )
            )
            logger.info(f"    ✅ Deleted {counts['audit_logs']} audit log records")
        else:
            logger.info(f"    [DRY RUN] Would delete {counts['audit_logs']} audit log records")
        
        # 5f. Delete MergeSuggestion records that reference unknown identities
        logger.info("  [5f] Cleaning up MergeSuggestion records...")
        # Get merge suggestions that contain unknown identity IDs
        merge_suggestions_result = await db.execute(
            select(MergeSuggestion)
        )
        merge_suggestions = merge_suggestions_result.scalars().all()
        
        suggestions_to_delete = []
        for suggestion in merge_suggestions:
            if suggestion.identity_ids:
                # Check if any identity_id in the suggestion is in our unknown list
                suggestion_ids = suggestion.identity_ids if isinstance(suggestion.identity_ids, list) else []
                if any(str(id) in [str(uid) for uid in identity_ids] for id in suggestion_ids):
                    suggestions_to_delete.append(suggestion.id)
        
        if suggestions_to_delete:
            if not dry_run:
                await db.execute(
                    delete(MergeSuggestion).where(
                        MergeSuggestion.id.in_(suggestions_to_delete)
                    )
                )
                logger.info(f"    ✅ Deleted {len(suggestions_to_delete)} merge suggestion records")
            else:
                logger.info(f"    [DRY RUN] Would delete {len(suggestions_to_delete)} merge suggestion records")
        else:
            logger.info(f"    ℹ️  No merge suggestions to delete")
        
        # 5g. Remove from the vector index
        logger.info("  [5g] Removing from the vector index...")
        if IDENTITY_INDEX_AVAILABLE and identity_ids and get_vector_index() is not None:
            removed_from_index = 0
            for identity_id in identity_ids:
                if not dry_run:
                    try:
                        removed_from_index += await remove_identity_vectors(db, identity_id)
                    except Exception as e:
                        logger.warning(f"    Failed to remove {identity_id} from the index: {e}")
                else:
                    logger.info(f"    [DRY RUN] Would remove identity {identity_id} from the index")

            if not dry_run:
                logger.info(f"    ✅ Removed {removed_from_index} vector(s) from the index")
                # No snapshot forced here: the index is derived from PostgreSQL,
                # and the rows are about to be deleted, so the next autosave or
                # reconciliation converges to the same state either way.
        else:
            logger.info(f"    ℹ️  No in-process index to update (rows are authoritative)")
        
        # 5h. Delete Identity records
        logger.info("  [5h] Deleting Identity records...")
        if not dry_run:
            await db.execute(
                delete(Identity).where(Identity.id.in_(identity_ids))
            )
            logger.info(f"    ✅ Deleted {unknown_count} identity records")
        else:
            logger.info(f"    [DRY RUN] Would delete {unknown_count} identity records")
        
        # 5i. Delete image files (if requested)
        if delete_images and image_paths:
            logger.info("  [5i] Deleting image files from disk...")
            deleted_count = 0
            failed_count = 0
            
            for image_path in image_paths:
                if not dry_run:
                    try:
                        if os.path.exists(image_path):
                            os.remove(image_path)
                            deleted_count += 1
                        else:
                            logger.debug(f"    File not found: {image_path}")
                    except Exception as e:
                        logger.error(f"    Failed to delete {image_path}: {e}")
                        failed_count += 1
                else:
                    if os.path.exists(image_path):
                        logger.info(f"    [DRY RUN] Would delete: {image_path}")
            
            if not dry_run:
                logger.info(f"    ✅ Deleted {deleted_count} image files")
                if failed_count > 0:
                    logger.warning(f"    ⚠️  Failed to delete {failed_count} files")
            else:
                logger.info(f"    [DRY RUN] Would delete {len([p for p in image_paths if os.path.exists(p)])} image files")
        
        # Commit transaction
        if not dry_run:
            await db.commit()
            logger.info("\n✅ Cleanup completed successfully!")
        else:
            logger.info("\n✅ Dry run completed - no changes made")
        
        # Verify cleanup
        logger.info("\n[Step 5] Verifying cleanup...")
        remaining_count = await get_unknown_identities_count(db)
        if remaining_count == 0:
            logger.info("✅ All unknown identities have been removed")
        else:
            logger.warning(f"⚠️  {remaining_count} unknown identities still remain")
        
    except Exception as e:
        logger.error(f"❌ Error during cleanup: {e}", exc_info=True)
        await db.rollback()
        raise


async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Cleanup all unknown identities from the database"
    )
    parser.add_argument(
        '--delete-images',
        action='store_true',
        help='Also delete image files from storage directory'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be deleted without actually deleting'
    )
    parser.add_argument(
        '--yes',
        action='store_true',
        help='Skip confirmation prompt (use with caution!)'
    )
    
    args = parser.parse_args()
    
    try:
        # Initialize database connection
        logger.info("Initializing database connection...")
        await db_manager.init_db()
        logger.info("✅ Database connection initialized")
        
        async with db_manager.get_session() as db:
            await cleanup_unknown_identities(
                db,
                delete_images=args.delete_images,
                dry_run=args.dry_run,
                skip_confirmation=args.yes
            )
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

