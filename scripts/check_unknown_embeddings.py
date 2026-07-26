#!/usr/bin/env python3
"""
Check Unknown Face Embeddings in Database
==========================================
Script to verify if unknown faces have embeddings stored in the database.
"""

import asyncio
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_connection import db_manager
from sqlalchemy import select, func
from db_models import Identity, IdentityEmbedding, IdentityType

async def check_unknown_embeddings():
    # Initialize database connection
    await db_manager.init_db()
    """Check unknown face embeddings in database"""
    print("=" * 80)
    print("Checking Unknown Face Embeddings")
    print("=" * 80)
    
    async with db_manager.get_session() as db:
        # Count total unknown identities
        total_unknown_query = select(func.count(Identity.id)).where(
            Identity.type == IdentityType.UNKNOWN
        )
        total_unknown = (await db.execute(total_unknown_query)).scalar_one()
        
        print(f"\n📊 Statistics:")
        print(f"   Total Unknown Identities: {total_unknown}")
        
        if total_unknown == 0:
            print("\n✅ No unknown identities found in database.")
            return
        
        # Count unknown identities with pgvector embeddings (embedding column is not NULL)
        unknown_with_emb_query = select(func.count(IdentityEmbedding.id)).join(Identity).where(
            Identity.type == IdentityType.UNKNOWN,
            IdentityEmbedding.embedding.isnot(None)
        )
        unknown_with_emb = (await db.execute(unknown_with_emb_query)).scalar_one()
        
        # Count total unknown embedding records (including NULL embeddings)
        unknown_total_emb_query = select(func.count(IdentityEmbedding.id)).join(Identity).where(
            Identity.type == IdentityType.UNKNOWN
        )
        unknown_total_emb = (await db.execute(unknown_total_emb_query)).scalar_one()
        
        # Count unknown identities with NULL embeddings
        unknown_null_emb_query = select(func.count(IdentityEmbedding.id)).join(Identity).where(
            Identity.type == IdentityType.UNKNOWN,
            IdentityEmbedding.embedding.is_(None)
        )
        unknown_null_emb = (await db.execute(unknown_null_emb_query)).scalar_one()
        
        print(f"   Unknown Identities with pgvector embeddings (non-NULL): {unknown_with_emb}")
        print(f"   Total Unknown Embedding Records: {unknown_total_emb}")
        print(f"   Unknown Embedding Records with NULL embedding: {unknown_null_emb}")
        
        # Get sample unknown identities
        sample_query = select(Identity).where(
            Identity.type == IdentityType.UNKNOWN
        ).limit(10)
        sample_result = await db.execute(sample_query)
        sample_identities = sample_result.scalars().all()
        
        print(f"\n🔍 Sample Unknown Identities (first 10):")
        print("-" * 80)
        
        for identity in sample_identities:
            # Check embeddings for this identity
            emb_query = select(IdentityEmbedding).where(
                IdentityEmbedding.identity_id == identity.id
            )
            emb_result = await db.execute(emb_query)
            embeddings = emb_result.scalars().all()
            
            has_pgvector = any(emb.embedding is not None for emb in embeddings)
            has_faiss = any(emb.faiss_id is not None for emb in embeddings)
            
            print(f"   Identity ID: {identity.id}")
            print(f"      Created: {identity.created_at}")
            print(f"      Last Seen: {identity.last_seen_at}")
            print(f"      Appearances: {identity.appearances_count}")
            print(f"      Embedding Records: {len(embeddings)}")
            print(f"      Has pgvector embedding: {'✅ YES' if has_pgvector else '❌ NO'}")
            print(f"      Has FAISS ID: {'✅ YES' if has_faiss else '❌ NO'}")
            
            if embeddings:
                for emb in embeddings:
                    print(f"         - Embedding ID: {emb.id}, Pipeline: {emb.pipeline_id}, "
                          f"pgvector: {'✅' if emb.embedding is not None else '❌ NULL'}, "
                          f"FAISS ID: {emb.faiss_id}, Type: {emb.faiss_index_type}")
            else:
                print(f"         ⚠️  NO EMBEDDING RECORDS FOUND!")
            print()
        
        print("=" * 80)
        
        if unknown_with_emb == 0 and total_unknown > 0:
            print("\n⚠️  WARNING: Unknown identities exist but have NO pgvector embeddings!")
            print("   This indicates a problem with the embedding save workflow.")
        elif unknown_with_emb < total_unknown:
            print(f"\n⚠️  WARNING: Only {unknown_with_emb}/{total_unknown} unknown identities have embeddings!")
        else:
            print("\n✅ All unknown identities have embeddings stored correctly.")

if __name__ == "__main__":
    try:
        asyncio.run(check_unknown_embeddings())
    finally:
        asyncio.run(db_manager.close_db())

