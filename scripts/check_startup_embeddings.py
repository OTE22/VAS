#!/usr/bin/env python3
"""
Check Startup Embeddings
========================
Check if embeddings were saved to the database during startup.
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


async def check_embeddings():
    """Check embeddings in database."""
    from db_connection import db_manager
    from db_models import Identity, IdentityEmbedding, IdentityType, IdentityStatus
    from sqlalchemy import select, func, and_
    
    # Initialize database connection
    logger.info("🔄 Initializing database connection...")
    if not db_manager._initialized:
        await db_manager.init_db()
    logger.info("✅ Database connection ready")
    
    logger.info("=" * 80)
    logger.info("🔍 Checking Startup Embeddings in Database")
    logger.info("=" * 80)
    
    async with db_manager.get_session() as db:
        try:
            # Count total identities
            result = await db.execute(
                select(func.count(Identity.id))
                .where(Identity.type == IdentityType.KNOWN)
            )
            total_known_identities = result.scalar() or 0
            logger.info(f"📊 Total KNOWN identities: {total_known_identities}")
            
            # Count identities with embeddings
            result = await db.execute(
                select(func.count(func.distinct(IdentityEmbedding.identity_id)))
                .join(Identity, IdentityEmbedding.identity_id == Identity.id)
                .where(
                    Identity.type == IdentityType.KNOWN,
                    IdentityEmbedding.embedding.isnot(None)
                )
            )
            identities_with_embeddings = result.scalar() or 0
            logger.info(f"📊 KNOWN identities with pgvector embeddings: {identities_with_embeddings}")
            
            # Count total embeddings
            result = await db.execute(
                select(func.count(IdentityEmbedding.id))
                .join(Identity, IdentityEmbedding.identity_id == Identity.id)
                .where(
                    Identity.type == IdentityType.KNOWN,
                    IdentityEmbedding.embedding.isnot(None),
                    IdentityEmbedding.faiss_index_type == 'known'
                )
            )
            total_embeddings = result.scalar() or 0
            logger.info(f"📊 Total KNOWN pgvector embeddings: {total_embeddings}")
            
            # Count embeddings from startup (pipeline_id="preloaded")
            result = await db.execute(
                select(func.count(IdentityEmbedding.id))
                .join(Identity, IdentityEmbedding.identity_id == Identity.id)
                .where(
                    Identity.type == IdentityType.KNOWN,
                    IdentityEmbedding.embedding.isnot(None),
                    IdentityEmbedding.faiss_index_type == 'known',
                    IdentityEmbedding.pipeline_id == 'preloaded'
                )
            )
            preloaded_embeddings = result.scalar() or 0
            logger.info(f"📊 Embeddings from startup (preloaded): {preloaded_embeddings}")
            
            # Count NULL embeddings
            result = await db.execute(
                select(func.count(IdentityEmbedding.id))
                .join(Identity, IdentityEmbedding.identity_id == Identity.id)
                .where(
                    Identity.type == IdentityType.KNOWN,
                    IdentityEmbedding.embedding.is_(None),
                    IdentityEmbedding.faiss_index_type == 'known'
                )
            )
            null_embeddings = result.scalar() or 0
            logger.info(f"📊 NULL embeddings (should be 0): {null_embeddings}")
            
            # Check identity status
            result = await db.execute(
                select(func.count(Identity.id))
                .where(
                    Identity.type == IdentityType.KNOWN,
                    Identity.status == IdentityStatus.ACTIVE
                )
            )
            active_known_identities = result.scalar() or 0
            logger.info(f"📊 ACTIVE KNOWN identities: {active_known_identities}")
            
            # Sample some identities
            logger.info("")
            logger.info("=" * 80)
            logger.info("📋 Sample Identities and Embeddings")
            logger.info("=" * 80)
            
            result = await db.execute(
                select(
                    Identity.id,
                    Identity.display_name,
                    Identity.type,
                    Identity.status,
                    func.count(IdentityEmbedding.id).label('embedding_count')
                )
                .outerjoin(IdentityEmbedding, Identity.id == IdentityEmbedding.identity_id)
                .where(Identity.type == IdentityType.KNOWN)
                .group_by(Identity.id, Identity.display_name, Identity.type, Identity.status)
                .limit(10)
            )
            
            for row in result:
                identity_id, display_name, identity_type, identity_status, emb_count = row
                logger.info(f"  • {display_name}")
                logger.info(f"    ID: {str(identity_id)[:8]}...")
                logger.info(f"    Type: {identity_type.value}, Status: {identity_status.value}")
                logger.info(f"    Embeddings: {emb_count}")
                
                # Check if embeddings are NULL
                emb_result = await db.execute(
                    select(IdentityEmbedding.embedding)
                    .where(IdentityEmbedding.identity_id == identity_id)
                    .limit(1)
                )
                emb_row = emb_result.first()
                if emb_row:
                    emb_value = emb_row[0]
                    if emb_value is None:
                        logger.warning(f"    ⚠️  Embedding is NULL!")
                    else:
                        logger.info(f"    ✅ Embedding exists (vector type)")
                else:
                    logger.warning(f"    ⚠️  No embedding record found!")
                logger.info("")
            
            # Summary
            logger.info("=" * 80)
            logger.info("📊 Summary")
            logger.info("=" * 80)
            
            if total_embeddings == 0:
                logger.error("❌ NO EMBEDDINGS FOUND!")
                logger.error("   This means embeddings were NOT saved during startup")
                logger.error("   Check startup logs for errors")
            elif null_embeddings > 0:
                logger.warning(f"⚠️  Found {null_embeddings} NULL embeddings")
                logger.warning("   Some embeddings were saved but are NULL")
            elif identities_with_embeddings < total_known_identities:
                logger.warning(f"⚠️  Only {identities_with_embeddings}/{total_known_identities} identities have embeddings")
            else:
                logger.info(f"✅ All {total_known_identities} KNOWN identities have embeddings")
                logger.info(f"✅ Total {total_embeddings} embeddings in database")
            
            if active_known_identities != total_known_identities:
                logger.warning(f"⚠️  Only {active_known_identities}/{total_known_identities} identities are ACTIVE")
                logger.warning("   Inactive identities won't be found in search!")
            
            logger.info("=" * 80)
            
        except Exception as e:
            logger.error(f"❌ Error checking embeddings: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(check_embeddings())

