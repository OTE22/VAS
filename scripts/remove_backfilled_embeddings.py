#!/usr/bin/env python3
"""
Remove Backfilled Embeddings
==============================
Removes embeddings that were added by the backfill script (pipeline_id="backfilled").

Usage:
    python scripts/remove_backfilled_embeddings.py
    python scripts/remove_backfilled_embeddings.py --dry-run
"""

import os
import sys
import asyncio
import logging
import argparse
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from config import settings
from db_connection import db_manager
from db_models import IdentityEmbedding

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def remove_backfilled_embeddings(dry_run: bool = False):
    """
    Remove all embeddings with pipeline_id="backfilled"
    """
    logger.info("=" * 80)
    logger.info("🗑️  Removing Backfilled Embeddings")
    logger.info("=" * 80)
    
    if dry_run:
        logger.info("🔍 DRY-RUN MODE - No changes will be made")
    
    # Initialize database
    logger.info("🔄 Initializing database...")
    try:
        await db_manager.init_db()
        logger.info("✅ Database initialized")
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        return
    
    async with db_manager.get_session() as db:
        # Count embeddings to be deleted
        count_query = await db.execute(
            select(IdentityEmbedding).where(
                IdentityEmbedding.pipeline_id == "backfilled"
            )
        )
        embeddings_to_delete = count_query.scalars().all()
        count = len(embeddings_to_delete)
        
        if count == 0:
            logger.info("✅ No backfilled embeddings found to delete")
            return
        
        logger.info(f"📊 Found {count} backfilled embeddings to delete")
        
        if dry_run:
            logger.info("🔍 DRY-RUN: Would delete the following embeddings:")
            for emb in embeddings_to_delete[:10]:  # Show first 10
                logger.info(f"   - ID: {emb.id}, Identity: {emb.identity_id}, Created: {emb.created_at}")
            if count > 10:
                logger.info(f"   ... and {count - 10} more")
            return
        
        # Delete embeddings
        logger.info("🔄 Deleting backfilled embeddings...")
        try:
            delete_stmt = delete(IdentityEmbedding).where(
                IdentityEmbedding.pipeline_id == "backfilled"
            )
            result = await db.execute(delete_stmt)
            await db.commit()
            
            deleted_count = result.rowcount if hasattr(result, 'rowcount') else count
            logger.info(f"✅ Successfully deleted {deleted_count} backfilled embeddings")
            
        except Exception as e:
            logger.error(f"❌ Error deleting embeddings: {e}", exc_info=True)
            await db.rollback()
            return
    
    logger.info("=" * 80)
    logger.info("✅ Removal complete!")
    logger.info("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="Remove backfilled embeddings")
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Dry run mode - show what would be deleted without making changes'
    )
    args = parser.parse_args()
    
    try:
        asyncio.run(remove_backfilled_embeddings(dry_run=args.dry_run))
    except KeyboardInterrupt:
        logger.info("\n⚠️ Interrupted by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

