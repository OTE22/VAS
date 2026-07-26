#!/usr/bin/env python3
"""
FAISS to pgvector Migration Script
===================================
Migrates embeddings from FAISS index to PostgreSQL pgvector.

This script:
1. Reads existing embeddings from FAISS index files
2. Matches them with IdentityEmbedding records in PostgreSQL
3. Updates the embedding column with actual vector data
4. Creates HNSW index for fast similarity search

Usage:
    python scripts/migrate_faiss_to_pgvector.py [--dry-run] [--batch-size=1000]

Options:
    --dry-run       Preview changes without applying them
    --batch-size    Number of embeddings to process per batch (default: 1000)

Requirements:
    - PostgreSQL with pgvector extension installed
    - FAISS index files in configured location
    - Database connection configured in config.py
"""

import os
import sys
import argparse
import asyncio
import logging
import numpy as np
from datetime import datetime
from typing import Optional, Tuple, Dict, List

# Add parent directory to path
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
sys.path.insert(0, parent_dir)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Import dependencies
try:
    import faiss
except ImportError:
    logger.error("FAISS not installed. Run: pip install faiss-cpu")
    sys.exit(1)

from sqlalchemy import select, update, text, and_
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from config import settings
from db_models import IdentityEmbedding, Identity, IdentityType


class FaissToPgvectorMigrator:
    """
    Migrates embeddings from FAISS index to PostgreSQL pgvector.
    """
    
    def __init__(self, dry_run: bool = False, batch_size: int = 1000):
        self.dry_run = dry_run
        self.batch_size = batch_size
        self.stats = {
            'known_migrated': 0,
            'unknown_migrated': 0,
            'skipped': 0,
            'errors': 0,
            'start_time': None,
            'end_time': None
        }
        
        # Index paths
        self.index_base_path = getattr(settings, 'IDENTITY_INDEX_DB_PATH', '/app/database/identity_indexes')
        self.known_index_path = os.path.join(self.index_base_path, 'known_index.faiss')
        self.unknown_index_path = os.path.join(self.index_base_path, 'unknown_index.faiss')
        self.known_mapping_path = os.path.join(self.index_base_path, 'known_faiss_mapping.npy')
        self.unknown_mapping_path = os.path.join(self.index_base_path, 'unknown_faiss_mapping.npy')
        
        logger.info(f"[MIGRATION] Initialized migrator (dry_run={dry_run}, batch_size={batch_size})")
        logger.info(f"[MIGRATION] FAISS index path: {self.index_base_path}")
    
    async def check_prerequisites(self, db: AsyncSession) -> bool:
        """Check that all prerequisites are met for migration."""
        logger.info("[MIGRATION] Checking prerequisites...")
        
        # Check pgvector extension
        try:
            result = await db.execute(text("""
                SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')
            """))
            ext_exists = result.scalar()
            if not ext_exists:
                logger.error("[MIGRATION] ❌ pgvector extension not installed!")
                logger.error("[MIGRATION] Run: CREATE EXTENSION IF NOT EXISTS vector;")
                return False
            logger.info("[MIGRATION] ✅ pgvector extension installed")
        except Exception as e:
            logger.error(f"[MIGRATION] ❌ Error checking pgvector extension: {e}")
            return False
        
        # Check embedding column exists
        try:
            result = await db.execute(text("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'identity_embeddings' 
                AND column_name = 'embedding'
            """))
            col_exists = result.scalar() is not None
            if not col_exists:
                logger.error("[MIGRATION] ❌ 'embedding' column not found in identity_embeddings table!")
                logger.error("[MIGRATION] Run database migrations first: alembic upgrade head")
                return False
            logger.info("[MIGRATION] ✅ 'embedding' column exists")
        except Exception as e:
            logger.error(f"[MIGRATION] ❌ Error checking embedding column: {e}")
            return False
        
        # Check FAISS index files exist
        if not os.path.exists(self.known_index_path):
            logger.warning(f"[MIGRATION] ⚠️ KNOWN index file not found: {self.known_index_path}")
        else:
            logger.info(f"[MIGRATION] ✅ KNOWN index file exists: {self.known_index_path}")
        
        if not os.path.exists(self.unknown_index_path):
            logger.warning(f"[MIGRATION] ⚠️ UNKNOWN index file not found: {self.unknown_index_path}")
        else:
            logger.info(f"[MIGRATION] ✅ UNKNOWN index file exists: {self.unknown_index_path}")
        
        return True
    
    def load_faiss_index(self, index_path: str, mapping_path: str) -> Tuple[Optional[faiss.Index], Optional[Dict]]:
        """Load FAISS index and mapping file."""
        if not os.path.exists(index_path):
            logger.warning(f"[MIGRATION] Index file not found: {index_path}")
            return None, None
        
        try:
            index = faiss.read_index(index_path)
            logger.info(f"[MIGRATION] Loaded FAISS index: {index_path} ({index.ntotal} vectors)")
            
            # Load mapping
            mapping = {}
            if os.path.exists(mapping_path):
                try:
                    mapping_data = np.load(mapping_path, allow_pickle=True).item()
                    if isinstance(mapping_data, dict):
                        # Mapping is identity_to_faiss: {identity_id: [faiss_id1, faiss_id2, ...]}
                        # Reverse it to faiss_to_identity: {faiss_id: identity_id}
                        for identity_id, faiss_ids in mapping_data.items():
                            for faiss_id in faiss_ids:
                                mapping[int(faiss_id)] = identity_id
                    logger.info(f"[MIGRATION] Loaded mapping: {len(mapping)} entries")
                except Exception as e:
                    logger.warning(f"[MIGRATION] Could not load mapping file: {e}")
            
            return index, mapping
            
        except Exception as e:
            logger.error(f"[MIGRATION] Failed to load FAISS index: {e}")
            return None, None
    
    async def migrate_embeddings(
        self,
        index: faiss.Index,
        mapping: Dict[int, str],
        index_type: str,
        db: AsyncSession
    ) -> int:
        """
        Migrate embeddings from FAISS index to PostgreSQL.
        
        Args:
            index: FAISS index containing vectors
            mapping: Dict mapping faiss_id to identity_id
            index_type: 'known' or 'unknown'
            db: Database session
            
        Returns:
            Number of embeddings migrated
        """
        if index is None:
            logger.warning(f"[MIGRATION] No {index_type} index to migrate")
            return 0
        
        migrated = 0
        total = index.ntotal
        
        logger.info(f"[MIGRATION] Starting migration of {total} {index_type} embeddings...")
        
        # Process in batches
        for batch_start in range(0, total, self.batch_size):
            batch_end = min(batch_start + self.batch_size, total)
            batch_count = 0
            
            for faiss_id in range(batch_start, batch_end):
                try:
                    # Reconstruct embedding from FAISS
                    embedding = index.reconstruct(faiss_id)
                    
                    # Normalize embedding
                    norm = np.linalg.norm(embedding)
                    if norm > 0:
                        embedding = embedding / norm
                    
                    embedding_list = embedding.tolist()
                    
                    # Get identity_id from mapping or from database
                    identity_id = mapping.get(faiss_id)
                    
                    if identity_id:
                        # Update by identity_id and faiss_id
                        if not self.dry_run:
                            await db.execute(
                                update(IdentityEmbedding)
                                .where(and_(
                                    IdentityEmbedding.faiss_id == faiss_id,
                                    IdentityEmbedding.faiss_index_type == index_type
                                ))
                                .values(embedding=embedding_list)
                            )
                        batch_count += 1
                    else:
                        # Try to find by faiss_id alone
                        if not self.dry_run:
                            result = await db.execute(
                                select(IdentityEmbedding).where(
                                    IdentityEmbedding.faiss_id == faiss_id,
                                    IdentityEmbedding.faiss_index_type == index_type
                                )
                            )
                            emb_record = result.scalar_one_or_none()
                            
                            if emb_record:
                                emb_record.embedding = embedding_list
                                batch_count += 1
                            else:
                                self.stats['skipped'] += 1
                                logger.debug(f"[MIGRATION] No DB record for faiss_id={faiss_id}, skipping")
                        else:
                            batch_count += 1  # Count for dry run
                    
                except Exception as e:
                    self.stats['errors'] += 1
                    logger.warning(f"[MIGRATION] Error migrating faiss_id={faiss_id}: {e}")
            
            if not self.dry_run:
                await db.flush()
            
            migrated += batch_count
            
            progress = (batch_end / total) * 100
            logger.info(f"[MIGRATION] [{index_type.upper()}] Progress: {batch_end}/{total} ({progress:.1f}%) - {migrated} migrated")
        
        return migrated
    
    async def create_vector_index(self, db: AsyncSession) -> bool:
        """Create HNSW index on embedding column."""
        if self.dry_run:
            logger.info("[MIGRATION] [DRY RUN] Would create HNSW index on embedding column")
            return True
        
        logger.info("[MIGRATION] Creating HNSW index on embedding column...")
        
        try:
            # Check if index already exists
            result = await db.execute(text("""
                SELECT indexname FROM pg_indexes 
                WHERE tablename = 'identity_embeddings' 
                AND indexname LIKE '%embedding%hnsw%'
            """))
            existing = result.scalar()
            
            if existing:
                logger.info(f"[MIGRATION] ✅ HNSW index already exists: {existing}")
                return True
            
            # Create HNSW index
            hnsw_m = getattr(settings, 'PGVECTOR_HNSW_M', 16)
            hnsw_ef = getattr(settings, 'PGVECTOR_HNSW_EF_CONSTRUCTION', 64)
            
            # Commit current transaction for CONCURRENTLY
            await db.execute(text("COMMIT"))
            
            await db.execute(text(f"""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_embedding_vector_hnsw
                ON identity_embeddings 
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = {hnsw_m}, ef_construction = {hnsw_ef})
            """))
            
            await db.execute(text("BEGIN"))
            
            logger.info(f"[MIGRATION] ✅ Created HNSW index (m={hnsw_m}, ef_construction={hnsw_ef})")
            return True
            
        except Exception as e:
            logger.error(f"[MIGRATION] ❌ Failed to create HNSW index: {e}")
            return False
    
    async def verify_migration(self, db: AsyncSession) -> Dict:
        """Verify migration was successful."""
        logger.info("[MIGRATION] Verifying migration...")
        
        stats = {}
        
        try:
            # Count total embeddings with vector data
            result = await db.execute(text("""
                SELECT COUNT(*) FROM identity_embeddings WHERE embedding IS NOT NULL
            """))
            stats['embeddings_with_vectors'] = result.scalar() or 0
            
            # Count by type
            result = await db.execute(text("""
                SELECT faiss_index_type, COUNT(*) 
                FROM identity_embeddings 
                WHERE embedding IS NOT NULL 
                GROUP BY faiss_index_type
            """))
            for row in result.fetchall():
                stats[f'{row[0]}_count'] = row[1]
            
            # Count null embeddings
            result = await db.execute(text("""
                SELECT COUNT(*) FROM identity_embeddings WHERE embedding IS NULL
            """))
            stats['embeddings_without_vectors'] = result.scalar() or 0
            
            # Test similarity search
            result = await db.execute(text("""
                SELECT COUNT(*) FROM identity_embeddings 
                WHERE embedding IS NOT NULL 
                LIMIT 1
            """))
            if result.scalar() > 0:
                # Get a sample embedding
                sample = await db.execute(text("""
                    SELECT embedding FROM identity_embeddings 
                    WHERE embedding IS NOT NULL 
                    LIMIT 1
                """))
                sample_embedding = sample.scalar()
                
                if sample_embedding:
                    # Test similarity search
                    test_result = await db.execute(
                        text("""
                            SELECT COUNT(*) FROM identity_embeddings 
                            WHERE embedding IS NOT NULL 
                            AND 1 - (embedding <=> :query) >= 0.3
                        """),
                        {"query": str(sample_embedding)}
                    )
                    stats['similarity_search_works'] = test_result.scalar() > 0
            
            logger.info(f"[MIGRATION] Verification results: {stats}")
            return stats
            
        except Exception as e:
            logger.error(f"[MIGRATION] ❌ Verification error: {e}")
            return {'error': str(e)}
    
    async def run(self):
        """Run the full migration process."""
        self.stats['start_time'] = datetime.utcnow()
        
        logger.info("=" * 70)
        logger.info("[MIGRATION] 🚀 Starting FAISS to pgvector migration")
        logger.info(f"[MIGRATION] Mode: {'DRY RUN' if self.dry_run else 'LIVE MIGRATION'}")
        logger.info("=" * 70)
        
        # Create database connection
        database_url = settings.DATABASE_URL.replace('postgresql://', 'postgresql+asyncpg://').replace('postgresql+psycopg2://', 'postgresql+asyncpg://')
        engine = create_async_engine(database_url, echo=False)
        async_session = async_sessionmaker(engine, expire_on_commit=False)
        
        async with async_session() as db:
            # Check prerequisites
            if not await self.check_prerequisites(db):
                logger.error("[MIGRATION] ❌ Prerequisites not met. Aborting.")
                return False
            
            # Load FAISS indexes
            logger.info("\n[MIGRATION] Loading FAISS indexes...")
            known_index, known_mapping = self.load_faiss_index(
                self.known_index_path, 
                self.known_mapping_path
            )
            unknown_index, unknown_mapping = self.load_faiss_index(
                self.unknown_index_path, 
                self.unknown_mapping_path
            )
            
            if known_index is None and unknown_index is None:
                logger.warning("[MIGRATION] ⚠️ No FAISS indexes found. Nothing to migrate.")
                return True
            
            # Migrate KNOWN embeddings
            if known_index:
                logger.info("\n" + "=" * 50)
                logger.info("[MIGRATION] Migrating KNOWN embeddings...")
                logger.info("=" * 50)
                self.stats['known_migrated'] = await self.migrate_embeddings(
                    known_index, known_mapping, 'known', db
                )
            
            # Migrate UNKNOWN embeddings
            if unknown_index:
                logger.info("\n" + "=" * 50)
                logger.info("[MIGRATION] Migrating UNKNOWN embeddings...")
                logger.info("=" * 50)
                self.stats['unknown_migrated'] = await self.migrate_embeddings(
                    unknown_index, unknown_mapping, 'unknown', db
                )
            
            # Create vector index
            logger.info("\n[MIGRATION] Creating vector index...")
            await self.create_vector_index(db)
            
            # Commit changes
            if not self.dry_run:
                await db.commit()
                logger.info("[MIGRATION] ✅ Changes committed")
            else:
                await db.rollback()
                logger.info("[MIGRATION] [DRY RUN] Changes rolled back")
            
            # Verify migration
            if not self.dry_run:
                verification = await self.verify_migration(db)
                self.stats['verification'] = verification
        
        await engine.dispose()
        
        self.stats['end_time'] = datetime.utcnow()
        duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds()
        
        # Print summary
        logger.info("\n" + "=" * 70)
        logger.info("[MIGRATION] 📊 Migration Summary")
        logger.info("=" * 70)
        logger.info(f"  Mode:             {'DRY RUN' if self.dry_run else 'LIVE'}")
        logger.info(f"  Duration:         {duration:.2f} seconds")
        logger.info(f"  KNOWN migrated:   {self.stats['known_migrated']}")
        logger.info(f"  UNKNOWN migrated: {self.stats['unknown_migrated']}")
        logger.info(f"  Skipped:          {self.stats['skipped']}")
        logger.info(f"  Errors:           {self.stats['errors']}")
        logger.info("=" * 70)
        
        if self.dry_run:
            logger.info("\n[MIGRATION] 💡 To perform actual migration, run without --dry-run")
        else:
            logger.info("\n[MIGRATION] ✅ Migration complete!")
            logger.info("[MIGRATION] 💡 Set VECTOR_BACKEND=pgvector in your environment to use pgvector")
        
        return True


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Migrate FAISS embeddings to pgvector')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without applying them')
    parser.add_argument('--batch-size', type=int, default=1000, help='Batch size for processing')
    args = parser.parse_args()
    
    migrator = FaissToPgvectorMigrator(dry_run=args.dry_run, batch_size=args.batch_size)
    success = await migrator.run()
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    asyncio.run(main())

