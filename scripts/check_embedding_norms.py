#!/usr/bin/env python3
"""
Check Embedding Norms
=====================
Verify that all embeddings in the database have norm=1.0 (L2 normalized).
This is critical for cosine similarity to work correctly with pgvector.
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

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text

from db_connection import db_manager
from db_models import IdentityEmbedding, Identity, IdentityType

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def check_embedding_norms():
    """Check if all embeddings have norm=1.0 (L2 normalized)."""
    logger.info("=" * 80)
    logger.info("🔍 Checking Embedding Norms (pgvector)")
    logger.info("=" * 80)
    logger.info("")
    logger.info("⚠️  CRITICAL: For cosine similarity to work correctly,")
    logger.info("   ALL embeddings must be L2-normalized (norm = 1.0)")
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
        # Get all embeddings with their norms using pgvector
        logger.info("")
        logger.info("📊 Checking embedding norms using pgvector...")
        logger.info("=" * 80)
        
        # Fetch embeddings and calculate norms in Python
        # pgvector stores embeddings as vector type, we fetch them and calculate L2 norm
        try:
            # Fetch all embeddings with their identity info
            query = select(
                IdentityEmbedding.id,
                IdentityEmbedding.identity_id,
                IdentityEmbedding.embedding,
                Identity.display_name,
                Identity.type
            ).join(
                Identity, IdentityEmbedding.identity_id == Identity.id
            ).where(
                IdentityEmbedding.embedding.isnot(None)
            )
            
            result = await db.execute(query)
            rows = result.all()
            
            if not rows:
                logger.warning("⚠️  No embeddings found in database")
                return
            
            logger.info(f"📊 Found {len(rows)} embeddings to check")
            logger.info("")
            
            # Calculate norms for each embedding
            norms = []
            embedding_data = []
            for row in rows:
                emb_id, identity_id, embedding_vec, display_name, id_type = row
                if embedding_vec is not None and len(embedding_vec) > 0:
                    # Convert to numpy array and calculate norm
                    emb_array = np.array(embedding_vec)
                    norm = np.linalg.norm(emb_array)
                    norms.append(norm)
                    embedding_data.append({
                        'id': emb_id,
                        'identity_id': identity_id,
                        'display_name': display_name,
                        'type': id_type,
                        'norm': norm
                    })
            
            if not norms:
                logger.warning("⚠️  No valid embeddings found")
                return
            
            min_norm = min(norms)
            max_norm = max(norms)
            avg_norm = sum(norms) / len(norms)
            
            # Count embeddings with norm != 1.0
            incorrect_norms = [n for n in norms if abs(n - 1.0) > 0.01]  # Allow 0.01 tolerance
            correct_norms = [n for n in norms if abs(n - 1.0) <= 0.01]
            
            logger.info("📊 Norm Statistics:")
            logger.info("=" * 80)
            logger.info(f"   Total embeddings:        {len(rows)}")
            logger.info(f"   ✅ Correct norm (≈1.0):   {len(correct_norms)} ({len(correct_norms)/len(rows)*100:.1f}%)")
            logger.info(f"   ❌ Incorrect norm (≠1.0): {len(incorrect_norms)} ({len(incorrect_norms)/len(rows)*100:.1f}%)")
            logger.info(f"   Min norm:                {min_norm:.6f}")
            logger.info(f"   Max norm:                {max_norm:.6f}")
            logger.info(f"   Avg norm:                {avg_norm:.6f}")
            logger.info("=" * 80)
            
            if incorrect_norms:
                logger.warning("")
                logger.warning("⚠️  PROBLEM DETECTED: Some embeddings are NOT normalized!")
                logger.warning("=" * 80)
                logger.warning("   This will cause INCORRECT similarity scores!")
                logger.warning("   Cosine similarity requires ALL vectors to have norm=1.0")
                logger.warning("")
                logger.warning("   Sample of embeddings with incorrect norms:")
                logger.warning("=" * 80)
                
                # Show first 10 incorrect norms
                incorrect_count = 0
                for emb_data in embedding_data:
                    if abs(emb_data['norm'] - 1.0) > 0.01:
                        logger.warning(
                            f"   Embedding ID: {emb_data['id']}, Identity: {emb_data['display_name'] or 'N/A'} "
                            f"({emb_data['type']}), Norm: {emb_data['norm']:.6f}"
                        )
                        incorrect_count += 1
                        if incorrect_count >= 10:
                            break
                
                logger.warning("=" * 80)
                logger.warning("")
                logger.warning("💡 SOLUTION:")
                logger.warning("   1. Re-normalize all embeddings in database")
                logger.warning("   2. Ensure embeddings are normalized when saving")
                logger.warning("   3. Ensure embeddings are normalized when searching")
            else:
                logger.info("")
                logger.info("✅ SUCCESS: All embeddings are properly normalized (norm ≈ 1.0)")
                logger.info("   Cosine similarity will work correctly!")
            
            # Check by identity type
            logger.info("")
            logger.info("📊 Norm Statistics by Identity Type:")
            logger.info("=" * 80)
            
            known_norms = [emb['norm'] for emb in embedding_data if emb['type'] == IdentityType.KNOWN]
            unknown_norms = [emb['norm'] for emb in embedding_data if emb['type'] == IdentityType.UNKNOWN]
            
            if known_norms:
                known_avg = sum(known_norms) / len(known_norms)
                known_incorrect = len([n for n in known_norms if abs(n - 1.0) > 0.01])
                logger.info(f"   KNOWN identities:")
                logger.info(f"      Total: {len(known_norms)}")
                logger.info(f"      Avg norm: {known_avg:.6f}")
                logger.info(f"      Incorrect: {known_incorrect}")
            
            if unknown_norms:
                unknown_avg = sum(unknown_norms) / len(unknown_norms)
                unknown_incorrect = len([n for n in unknown_norms if abs(n - 1.0) > 0.01])
                logger.info(f"   UNKNOWN identities:")
                logger.info(f"      Total: {len(unknown_norms)}")
                logger.info(f"      Avg norm: {unknown_avg:.6f}")
                logger.info(f"      Incorrect: {unknown_incorrect}")
            
            logger.info("=" * 80)
            
        except Exception as e:
            logger.error(f"❌ Error checking norms: {e}", exc_info=True)
            logger.info("")
            logger.info("💡 Trying alternative method (direct SQL)...")
            
            # Alternative: Check a sample using Python
            try:
                sample_query = select(IdentityEmbedding).where(
                    IdentityEmbedding.embedding.isnot(None)
                ).limit(10)
                sample_result = await db.execute(sample_query)
                sample_embeddings = sample_result.scalars().all()
                
                logger.info(f"   Checking sample of {len(sample_embeddings)} embeddings...")
                for emb in sample_embeddings:
                    if emb.embedding is not None and len(emb.embedding) > 0:
                        emb_array = np.array(emb.embedding)
                        norm = np.linalg.norm(emb_array)
                        logger.info(f"   Embedding ID {emb.id}: norm = {norm:.6f}")
            except Exception as e2:
                logger.error(f"❌ Alternative method also failed: {e2}")
    
    await db_manager.close_db()
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("✅ Embedding Norm Check Complete")
    logger.info("=" * 80)


if __name__ == "__main__":
    asyncio.run(check_embedding_norms())

