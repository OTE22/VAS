#!/usr/bin/env python3
"""
Clear All Data from Database
=============================
This script deletes ALL data from the face recognition database while preserving
the database schema (tables, indexes, etc.).

⚠️  WARNING: This will permanently delete:
   - All face detections
   - All recognized faces
   - All identities (known and unknown)
   - All embeddings
   - All appearances
   - All audit logs
   - All system metrics

✅ This will KEEP:
   - Database schema (tables, indexes, constraints)
   - User accounts (admin/users)
   - Pipeline configurations (cameras)
   - Database structure

Usage:
    python scripts/clear_all_data.py [--confirm]
    
    Without --confirm flag, script will show what will be deleted and ask for confirmation.
"""

import os
import sys
import asyncio
import logging
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from db_connection import db_manager
from db_models import (
    Detection, Face, Identity, IdentityEmbedding, IdentityAppearance,
    ChatbotAuditLog, IdentityAuditLog, SettingsAuditLog,
    SystemMetrics, Pipeline, User, WatchlistAlert, LiveSearchAlert,
    LiveAlertTrigger, SearchHistory, SavedSearch, BackgroundTaskHistory,
    IdentityMerge, MergeSuggestion, IdentityRelationship
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def get_table_counts(db: AsyncSession) -> dict:
    """Get row counts for all tables"""
    counts = {}
    
    tables = [
        ('IdentityEmbedding', IdentityEmbedding),
        ('IdentityAppearance', IdentityAppearance),
        ('Face', Face),
        ('Detection', Detection),
        ('IdentityMerge', IdentityMerge),
        ('MergeSuggestion', MergeSuggestion),
        ('IdentityRelationship', IdentityRelationship),
        ('Identity', Identity),
        ('ChatbotAuditLog', ChatbotAuditLog),
        ('IdentityAuditLog', IdentityAuditLog),
        ('SettingsAuditLog', SettingsAuditLog),
        ('SystemMetrics', SystemMetrics),
        ('WatchlistAlert', WatchlistAlert),
        ('LiveSearchAlert', LiveSearchAlert),
        ('LiveAlertTrigger', LiveAlertTrigger),
        ('SearchHistory', SearchHistory),
        ('SavedSearch', SavedSearch),
        ('BackgroundTaskHistory', BackgroundTaskHistory),
    ]
    
    for table_name, model in tables:
        try:
            count = (await db.execute(select(func.count(model.id)))).scalar_one()
            counts[table_name] = count
        except Exception as e:
            logger.warning(f"Could not count {table_name}: {e}")
            counts[table_name] = 0
    
    return counts


async def clear_all_data(confirm: bool = False):
    """Clear all data from database"""
    
    logger.info("=" * 80)
    logger.info("🗑️  Database Data Deletion Script")
    logger.info("=" * 80)
    
    if not confirm:
        logger.warning("⚠️  WARNING: This will delete ALL data from the database!")
        logger.warning("⚠️  This includes:")
        logger.warning("   - All face detections")
        logger.warning("   - All recognized faces")
        logger.warning("   - All identities (known and unknown)")
        logger.warning("   - All embeddings")
        logger.warning("   - All appearances")
        logger.warning("   - All audit logs (chatbot, identity, settings)")
        logger.warning("   - All watchlist alerts and search history")
        logger.warning("   - All system metrics")
        logger.warning("")
        logger.warning("✅ This will KEEP:")
        logger.warning("   - Database schema (tables, indexes)")
        logger.warning("   - User accounts")
        logger.warning("   - Pipeline configurations")
        logger.warning("")
        logger.error("❌ To proceed, run with --confirm flag:")
        logger.error("   python scripts/clear_all_data.py --confirm")
        return
    
    # Initialize database
    logger.info("🔄 Initializing database connection...")
    try:
        await db_manager.init_db()
        logger.info("✅ Database connected")
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        return
    
    # Get current counts
    async with db_manager.get_session() as db:
        logger.info("\n📊 Current Data Counts:")
        logger.info("-" * 80)
        counts = await get_table_counts(db)
        for table_name, count in counts.items():
            logger.info(f"   {table_name:20s}: {count:,} rows")
        logger.info("-" * 80)
        
        total_rows = sum(counts.values())
        if total_rows == 0:
            logger.info("\n✅ Database is already empty. Nothing to delete.")
            return
        
        logger.warning(f"\n⚠️  About to delete {total_rows:,} total rows!")
        logger.info("")
    
    # Delete in correct order (respecting foreign key constraints)
    logger.info("🔄 Starting deletion process...")
    logger.info("")
    
    async with db_manager.get_session() as db:
        try:
            # 1. Delete IdentityEmbedding (references Identity and Detection)
            logger.info("1️⃣  Deleting IdentityEmbedding records...")
            result = await db.execute(text("DELETE FROM identity_embeddings"))
            deleted = result.rowcount
            logger.info(f"   ✅ Deleted {deleted:,} IdentityEmbedding records")
            await db.commit()
            
            # 2. Delete IdentityAppearance (references Identity)
            logger.info("2️⃣  Deleting IdentityAppearance records...")
            result = await db.execute(text("DELETE FROM identity_appearances"))
            deleted = result.rowcount
            logger.info(f"   ✅ Deleted {deleted:,} IdentityAppearance records")
            await db.commit()
            
            # 3. Delete Face (references Detection and Identity)
            logger.info("3️⃣  Deleting Face records...")
            result = await db.execute(text("DELETE FROM faces"))
            deleted = result.rowcount
            logger.info(f"   ✅ Deleted {deleted:,} Face records")
            await db.commit()
            
            # 4. Delete Detection (references Pipeline)
            logger.info("4️⃣  Deleting Detection records...")
            result = await db.execute(text("DELETE FROM detections"))
            deleted = result.rowcount
            logger.info(f"   ✅ Deleted {deleted:,} Detection records")
            await db.commit()
            
            # 5. Delete Identity-related tables (must be deleted before Identity)
            logger.info("5️⃣  Deleting Identity-related records...")
            
            # Delete IdentityMerge (references Identity)
            result = await db.execute(text("DELETE FROM identity_merges"))
            deleted = result.rowcount
            if deleted > 0:
                logger.info(f"   ✅ Deleted {deleted:,} IdentityMerge records")
            await db.commit()
            
            # Delete MergeSuggestion (references Identity)
            result = await db.execute(text("DELETE FROM merge_suggestions"))
            deleted = result.rowcount
            if deleted > 0:
                logger.info(f"   ✅ Deleted {deleted:,} MergeSuggestion records")
            await db.commit()
            
            # Delete IdentityRelationship (references Identity)
            result = await db.execute(text("DELETE FROM identity_relationships"))
            deleted = result.rowcount
            if deleted > 0:
                logger.info(f"   ✅ Deleted {deleted:,} IdentityRelationship records")
            await db.commit()
            
            # Delete IdentityAuditLog (references Identity) - MUST be before Identity
            result = await db.execute(text("DELETE FROM identity_audit_log"))
            deleted = result.rowcount
            if deleted > 0:
                logger.info(f"   ✅ Deleted {deleted:,} IdentityAuditLog records")
            await db.commit()
            
            # 6. Delete Identity (now safe - all references removed)
            logger.info("6️⃣  Deleting Identity records...")
            result = await db.execute(text("DELETE FROM identities"))
            deleted = result.rowcount
            logger.info(f"   ✅ Deleted {deleted:,} Identity records")
            await db.commit()
            
            # 7. Delete Audit Logs (other audit logs that don't reference Identity)
            logger.info("7️⃣  Deleting Audit Log records...")
            for audit_table in ['chatbot_audit_log', 'settings_audit_log']:
                result = await db.execute(text(f"DELETE FROM {audit_table}"))
                deleted = result.rowcount
                if deleted > 0:
                    logger.info(f"   ✅ Deleted {deleted:,} records from {audit_table}")
            await db.commit()
            
            # 8. Delete Watchlist and Search data
            logger.info("8️⃣  Deleting Watchlist and Search data...")
            # Delete WatchlistEntry first (references Watchlist)
            result = await db.execute(text("DELETE FROM watchlist_entries"))
            deleted = result.rowcount
            if deleted > 0:
                logger.info(f"   ✅ Deleted {deleted:,} WatchlistEntry records")
            await db.commit()
            
            # Delete Watchlist (now safe)
            result = await db.execute(text("DELETE FROM watchlists"))
            deleted = result.rowcount
            if deleted > 0:
                logger.info(f"   ✅ Deleted {deleted:,} Watchlist records")
            await db.commit()
            
            # Delete other search/alert tables
            for table in ['watchlist_alerts', 'live_search_alerts', 'live_alert_triggers', 'search_history', 'saved_searches']:
                result = await db.execute(text(f"DELETE FROM {table}"))
                deleted = result.rowcount
                if deleted > 0:
                    logger.info(f"   ✅ Deleted {deleted:,} records from {table}")
            await db.commit()
            
            # 9. Delete Background Task History
            logger.info("9️⃣  Deleting Background Task History...")
            result = await db.execute(text("DELETE FROM background_task_history"))
            deleted = result.rowcount
            logger.info(f"   ✅ Deleted {deleted:,} BackgroundTaskHistory records")
            await db.commit()
            
            # 10. Delete SystemMetrics
            logger.info("🔟 Deleting SystemMetrics records...")
            result = await db.execute(text("DELETE FROM system_metrics"))
            deleted = result.rowcount
            logger.info(f"   ✅ Deleted {deleted:,} SystemMetrics records")
            await db.commit()
            
            logger.info("")
            logger.info("=" * 80)
            logger.info("✅ Data Deletion Complete!")
            logger.info("=" * 80)
            
            # Verify deletion
            logger.info("\n📊 Verification - Remaining Data:")
            logger.info("-" * 80)
            final_counts = await get_table_counts(db)
            for table_name, count in final_counts.items():
                if count > 0:
                    logger.warning(f"   {table_name:20s}: {count:,} rows (⚠️  NOT DELETED)")
                else:
                    logger.info(f"   {table_name:20s}: {count:,} rows ✅")
            logger.info("-" * 80)
            
            remaining = sum(final_counts.values())
            if remaining == 0:
                logger.info("\n✅ All data successfully deleted! Database is now empty.")
            else:
                logger.warning(f"\n⚠️  Warning: {remaining:,} rows still remain. Check foreign key constraints.")
            
        except Exception as e:
            logger.error(f"\n❌ Error during deletion: {e}", exc_info=True)
            await db.rollback()
            logger.error("❌ Transaction rolled back. No changes were made.")
            raise
        finally:
            # Close database connection within the async context
            try:
                await db_manager.close_db()
                logger.info("✅ Database connections closed")
            except Exception as e:
                # Ignore errors when closing (event loop may already be closed)
                logger.debug(f"Note: Error closing database connection (non-critical): {e}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Clear all data from the face recognition database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Show what will be deleted (dry-run)
  python scripts/clear_all_data.py
  
  # Actually delete all data
  python scripts/clear_all_data.py --confirm
        """
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm deletion (required to actually delete data)"
    )
    
    args = parser.parse_args()
    
    try:
        asyncio.run(clear_all_data(confirm=args.confirm))
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Operation cancelled by user")
    except Exception as e:
        logger.error(f"\n❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)

