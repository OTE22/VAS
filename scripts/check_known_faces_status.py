#!/usr/bin/env python3
"""
Check Known Faces Status
========================
Check if known faces are loaded in the database with pgvector embeddings.
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
from sqlalchemy import select, func, and_

from db_connection import db_manager
from db_models import Identity, IdentityEmbedding, IdentityType, IdentityStatus

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def check_known_faces_status():
    """Check the status of known faces in the database."""
    logger.info("=" * 80)
    logger.info("🔍 Checking Known Faces Status")
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
        # Count known identities
        known_identities_query = select(func.count(Identity.id)).where(
            Identity.type == IdentityType.KNOWN,
            Identity.status == IdentityStatus.ACTIVE
        )
        known_identities_count = (await db.execute(known_identities_query)).scalar_one()
        
        # Count embeddings with pgvector (non-NULL embedding column)
        pgvector_embeddings_query = select(func.count(IdentityEmbedding.id)).where(
            IdentityEmbedding.embedding.isnot(None)
        )
        pgvector_embeddings_count = (await db.execute(pgvector_embeddings_query)).scalar_one()
        
        # Count embeddings for known identities with pgvector
        known_pgvector_query = select(func.count(IdentityEmbedding.id)).join(
            Identity, IdentityEmbedding.identity_id == Identity.id
        ).where(
            Identity.type == IdentityType.KNOWN,
            Identity.status == IdentityStatus.ACTIVE,
            IdentityEmbedding.embedding.isnot(None)
        )
        known_pgvector_count = (await db.execute(known_pgvector_query)).scalar_one()
        
        # Count identities without embeddings
        identities_without_embeddings_query = select(Identity).where(
            Identity.type == IdentityType.KNOWN,
            Identity.status == IdentityStatus.ACTIVE
        ).outerjoin(
            IdentityEmbedding, and_(
                IdentityEmbedding.identity_id == Identity.id,
                IdentityEmbedding.embedding.isnot(None)
            )
        ).where(IdentityEmbedding.id.is_(None))
        identities_without_embeddings = (await db.execute(identities_without_embeddings_query)).scalars().all()
        
        logger.info("")
        logger.info("📊 Database Status:")
        logger.info("=" * 80)
        logger.info(f"   Known Identities (ACTIVE):     {known_identities_count}")
        logger.info(f"   Total pgvector Embeddings:     {pgvector_embeddings_count}")
        logger.info(f"   Known pgvector Embeddings:     {known_pgvector_count}")
        logger.info(f"   Known Identities WITHOUT embeddings: {len(identities_without_embeddings)}")
        logger.info("=" * 80)
        
        # Check storage/faces directory
        from config import settings
        faces_dir = settings.FACES_DIR
        logger.info("")
        logger.info("📁 Storage Directory Status:")
        logger.info("=" * 80)
        if os.path.exists(faces_dir):
            image_files = [
                f for f in os.listdir(faces_dir)
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))
            ]
            logger.info(f"   Faces Directory: {faces_dir}")
            logger.info(f"   Image Files Found: {len(image_files)}")
            if image_files:
                logger.info(f"   Files: {', '.join(image_files[:10])}")
                if len(image_files) > 10:
                    logger.info(f"   ... and {len(image_files) - 10} more")
        else:
            logger.warning(f"   ⚠️  Faces directory not found: {faces_dir}")
        logger.info("=" * 80)
        
        # Show identities without embeddings
        if identities_without_embeddings:
            logger.info("")
            logger.warning("⚠️  Known Identities WITHOUT pgvector Embeddings:")
            logger.info("=" * 80)
            for identity in identities_without_embeddings:
                logger.warning(f"   - {identity.display_name} (ID: {identity.id})")
            logger.info("=" * 80)
            logger.info("")
            logger.info("💡 These identities need to be reloaded to create pgvector embeddings.")
            logger.info("   Run: POST /api/admin/identities/load-known-faces?force_reload=true")
        else:
            logger.info("")
            logger.info("✅ All known identities have pgvector embeddings!")
        
        # Show sample of known identities with embeddings
        if known_pgvector_count > 0:
            logger.info("")
            logger.info("✅ Sample of Known Identities WITH Embeddings:")
            logger.info("=" * 80)
            sample_query = select(Identity, IdentityEmbedding).join(
                IdentityEmbedding, Identity.id == IdentityEmbedding.identity_id
            ).where(
                Identity.type == IdentityType.KNOWN,
                Identity.status == IdentityStatus.ACTIVE,
                IdentityEmbedding.embedding.isnot(None)
            ).limit(5)
            sample_results = (await db.execute(sample_query)).all()
            for identity, embedding in sample_results:
                logger.info(f"   - {identity.display_name} (ID: {str(identity.id)[:8]}...) - Embedding ID: {embedding.id}")
            logger.info("=" * 80)
    
    await db_manager.close_db()
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("✅ Status Check Complete")
    logger.info("=" * 80)


if __name__ == "__main__":
    asyncio.run(check_known_faces_status())

