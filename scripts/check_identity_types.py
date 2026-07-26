#!/usr/bin/env python3
"""
Check Identity Types
====================
Check what types identities actually have in the database.
"""

import os
import sys
import asyncio
import logging
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def check_types():
    """Check identity types in database."""
    from db_connection import db_manager
    from db_models import Identity, IdentityEmbedding, IdentityType, IdentityStatus
    from sqlalchemy import select, func, text
    
    # Initialize database
    logger.info("🔄 Initializing database...")
    if not db_manager._initialized:
        await db_manager.init_db()
    logger.info("✅ Database initialized")
    
    async with db_manager.get_session() as db:
        logger.info("=" * 80)
        logger.info("🔍 Checking Identity Types")
        logger.info("=" * 80)
        
        # Check identity types using raw SQL
        result = await db.execute(text("""
            SELECT 
                type::text,
                status::text,
                COUNT(*) as count
            FROM identities
            GROUP BY type, status
            ORDER BY type, status
        """))
        logger.info("📊 Identity types in database:")
        for row in result:
            identity_type, identity_status, count = row
            logger.info(f"  • type={identity_type}, status={identity_status}: {count} identities")
        
        # Check embeddings with identity types
        logger.info("")
        logger.info("📊 Embeddings by identity type:")
        result = await db.execute(text("""
            SELECT 
                i.type::text as identity_type,
                i.status::text as identity_status,
                COUNT(ie.id) as embedding_count,
                COUNT(ie.embedding) as non_null_embeddings
            FROM identity_embeddings ie
            JOIN identities i ON ie.identity_id = i.id
            WHERE ie.faiss_index_type = 'known'
            GROUP BY i.type, i.status
            ORDER BY i.type, i.status
        """))
        for row in result:
            identity_type, identity_status, total, non_null = row
            logger.info(f"  • type={identity_type}, status={identity_status}: {total} total, {non_null} non-NULL")
        
        # Check if the enum comparison works
        logger.info("")
        logger.info("📊 Testing enum comparison:")
        result = await db.execute(text("""
            SELECT COUNT(*) 
            FROM identities
            WHERE type::text = 'known'
        """))
        known_count = result.scalar()
        logger.info(f"  • Identities with type::text = 'known': {known_count}")
        
        result = await db.execute(text("""
            SELECT COUNT(*) 
            FROM identities
            WHERE type = 'known'::identitytype
        """))
        known_count2 = result.scalar()
        logger.info(f"  • Identities with type = 'known'::identitytype: {known_count2}")
        
        # Check actual enum values
        logger.info("")
        logger.info("📊 Sample identities:")
        result = await db.execute(text("""
            SELECT 
                id,
                display_name,
                type::text,
                status::text
            FROM identities
            LIMIT 5
        """))
        for row in result:
            identity_id, display_name, identity_type, identity_status = row
            logger.info(f"  • {display_name}: type={identity_type}, status={identity_status}")
        
        logger.info("=" * 80)


if __name__ == "__main__":
    asyncio.run(check_types())

