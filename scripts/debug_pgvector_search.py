#!/usr/bin/env python3
"""
Debug pgvector Search
=====================
Test the exact query used by pgvector search to see why it returns 0 rows.
"""

import os
import sys
import asyncio
import logging
import numpy as np
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


async def debug_search():
    """Debug the pgvector search query."""
    from db_connection import db_manager
    from db_models import Identity, IdentityEmbedding, IdentityType, IdentityStatus
    from sqlalchemy import select, func, text
    
    # Initialize database
    logger.info("🔄 Initializing database...")
    if not db_manager._initialized:
        await db_manager.init_db()
    logger.info("✅ Database initialized")
    
    async with db_manager.get_session() as db:
        # Create a dummy normalized embedding for testing
        dummy_embedding = np.random.rand(512).astype(np.float32)
        dummy_embedding = dummy_embedding / np.linalg.norm(dummy_embedding)
        embedding_list = dummy_embedding.tolist()
        embedding_array_str = '[' + ','.join(str(x) for x in embedding_list) + ']'
        
        logger.info("=" * 80)
        logger.info("🔍 Testing pgvector Search Query")
        logger.info("=" * 80)
        
        # Test 1: Count total embeddings
        result = await db.execute(text("""
            SELECT COUNT(*) 
            FROM identity_embeddings 
            WHERE embedding IS NOT NULL
        """))
        total_embeddings = result.scalar()
        logger.info(f"📊 Total embeddings (non-NULL): {total_embeddings}")
        
        # Test 2: Count embeddings for KNOWN identities
        result = await db.execute(text("""
            SELECT COUNT(*) 
            FROM identity_embeddings ie
            JOIN identities i ON ie.identity_id = i.id
            WHERE ie.embedding IS NOT NULL
            AND i.type::text = 'known'
        """))
        known_embeddings = result.scalar()
        logger.info(f"📊 KNOWN embeddings: {known_embeddings}")
        
        # Test 3: Count ACTIVE KNOWN identities
        result = await db.execute(text("""
            SELECT COUNT(*) 
            FROM identity_embeddings ie
            JOIN identities i ON ie.identity_id = i.id
            WHERE ie.embedding IS NOT NULL
            AND i.type::text = 'known'
            AND i.status::text = 'active'
        """))
        active_known_embeddings = result.scalar()
        logger.info(f"📊 ACTIVE KNOWN embeddings: {active_known_embeddings}")
        
        # Test 4: Test the actual search query (with threshold=0.0 to see all matches)
        logger.info("")
        logger.info("Testing search query with threshold=0.0 (should return all matches)...")
        result = await db.execute(text(f"""
            WITH query_vector AS (
                SELECT '{embedding_array_str}'::vector AS vec
            )
            SELECT 
                ie.identity_id::text as identity_id,
                1 - (ie.embedding <=> qv.vec) as similarity,
                ie.quality,
                i.display_name
            FROM identity_embeddings ie
            JOIN identities i ON ie.identity_id = i.id
            CROSS JOIN query_vector qv
            WHERE 
                ie.embedding IS NOT NULL
                AND i.type::text = 'known'
                AND i.status::text = 'active'
                AND 1 - (ie.embedding <=> qv.vec) >= 0.0
            ORDER BY ie.embedding <=> qv.vec
            LIMIT 5
        """))
        rows = result.fetchall()
        logger.info(f"📊 Search query returned {len(rows)} rows (threshold=0.0)")
        for idx, row in enumerate(rows):
            identity_id, similarity, quality, display_name = row
            logger.info(f"  {idx+1}. {display_name}: similarity={float(similarity):.6f}")
        
        # Test 5: Test with threshold=0.4 (actual threshold)
        logger.info("")
        logger.info("Testing search query with threshold=0.4 (actual threshold)...")
        result = await db.execute(text(f"""
            WITH query_vector AS (
                SELECT '{embedding_array_str}'::vector AS vec
            )
            SELECT 
                ie.identity_id::text as identity_id,
                1 - (ie.embedding <=> qv.vec) as similarity,
                ie.quality,
                i.display_name
            FROM identity_embeddings ie
            JOIN identities i ON ie.identity_id = i.id
            CROSS JOIN query_vector qv
            WHERE 
                ie.embedding IS NOT NULL
                AND i.type::text = 'known'
                AND i.status::text = 'active'
                AND 1 - (ie.embedding <=> qv.vec) >= 0.4
            ORDER BY ie.embedding <=> qv.vec
            LIMIT 5
        """))
        rows = result.fetchall()
        logger.info(f"📊 Search query returned {len(rows)} rows (threshold=0.4)")
        if len(rows) == 0:
            logger.warning("⚠️  No matches found with threshold=0.4")
            logger.warning("   This is expected if the dummy embedding doesn't match any real faces")
        else:
            for idx, row in enumerate(rows):
                identity_id, similarity, quality, display_name = row
                logger.info(f"  {idx+1}. {display_name}: similarity={float(similarity):.6f}")
        
        # Test 6: Check if there are any embeddings at all
        logger.info("")
        logger.info("Checking embedding column types...")
        result = await db.execute(text("""
            SELECT 
                COUNT(*) as total,
                COUNT(embedding) as non_null,
                COUNT(*) FILTER (WHERE embedding IS NULL) as null_count
            FROM identity_embeddings
            WHERE faiss_index_type = 'known'
        """))
        row = result.first()
        total, non_null, null_count = row
        logger.info(f"📊 identity_embeddings (known): total={total}, non_null={non_null}, null={null_count}")
        
        logger.info("=" * 80)


if __name__ == "__main__":
    asyncio.run(debug_search())

