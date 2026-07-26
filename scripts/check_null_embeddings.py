#!/usr/bin/env python3
"""
Check NULL Embeddings
=====================
Checks which embeddings are NULL and why.

Usage:
    python scripts/check_null_embeddings.py
"""

import os
import sys
import asyncio
import logging
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from config import settings
from db_connection import db_manager
from db_models import Identity, IdentityEmbedding, IdentityType, IdentityStatus

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def check_null_embeddings():
    """
    Check which embeddings are NULL and provide details
    """
    logger.info("=" * 80)
    logger.info("🔍 Checking NULL Embeddings")
    logger.info("=" * 80)
    
    # Initialize database
    logger.info("🔄 Initializing database...")
    try:
        await db_manager.init_db()
        logger.info("✅ Database initialized")
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        return
    
    async with db_manager.get_session() as db:
        # Count total embeddings
        total_query = await db.execute(
            select(func.count(IdentityEmbedding.id))
        )
        total_count = total_query.scalar() or 0
        
        # Count NULL embeddings
        null_query = await db.execute(
            select(func.count(IdentityEmbedding.id)).where(
                IdentityEmbedding.embedding.is_(None)
            )
        )
        null_count = null_query.scalar() or 0
        
        # Count non-NULL embeddings
        not_null_count = total_count - null_count
        
        logger.info("")
        logger.info("📊 Embedding Statistics:")
        logger.info(f"   Total embeddings: {total_count}")
        logger.info(f"   ✅ Non-NULL embeddings: {not_null_count}")
        logger.info(f"   ❌ NULL embeddings: {null_count}")
        logger.info("")
        
        if null_count > 0:
            # Get details about NULL embeddings
            logger.info("🔍 Details of NULL embeddings:")
            logger.info("-" * 80)
            
            null_embeddings_query = await db.execute(
                select(IdentityEmbedding, Identity).join(
                    Identity, IdentityEmbedding.identity_id == Identity.id
                ).where(
                    IdentityEmbedding.embedding.is_(None)
                ).order_by(IdentityEmbedding.created_at.desc())
            )
            
            null_embeddings = null_embeddings_query.all()
            
            for emb, identity in null_embeddings:
                logger.info(f"   Embedding ID: {emb.id}")
                logger.info(f"   Identity: {identity.display_name or 'Unknown'} (ID: {identity.id})")
                logger.info(f"   Pipeline ID: {emb.pipeline_id}")
                logger.info(f"   Created: {emb.created_at}")
                logger.info(f"   FAISS ID: {emb.faiss_id}")
                logger.info(f"   FAISS Index Type: {emb.faiss_index_type}")
                logger.info(f"   Quality: {emb.quality}")
                logger.info("")
            
            logger.info("-" * 80)
            logger.info("")
            logger.info("💡 Possible reasons for NULL embeddings:")
            logger.info("   1. Using FAISS backend (embeddings stored in FAISS index, not database)")
            logger.info("   2. Embedding was not properly saved during update")
            logger.info("   3. Image had no face detected")
            logger.info("   4. Error during embedding generation")
            logger.info("")
            logger.info("🔧 To fix:")
            logger.info("   - If using pgvector: Run backfill script to update embeddings")
            logger.info("   - If using FAISS: NULL is normal (embeddings are in FAISS index)")
        else:
            logger.info("✅ All embeddings have values! No NULL embeddings found.")
    
    logger.info("=" * 80)


def main():
    try:
        asyncio.run(check_null_embeddings())
    except KeyboardInterrupt:
        logger.info("\n⚠️ Interrupted by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

