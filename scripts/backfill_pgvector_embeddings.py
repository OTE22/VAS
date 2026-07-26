#!/usr/bin/env python3
"""
Backfill pgvector Embeddings for Known Faces
=============================================
Processes all images in storage/faces/ and adds embeddings to EXISTING Identity records.

IMPORTANT: This script ONLY updates existing Identity rows - it does NOT create new ones.

This script:
1. Scans storage/faces/ directory
2. For each image:
   - Detects face and generates embedding
   - Finds EXISTING Identity record (by name)
   - Updates the Identity row (last_seen_at, updated_at)
   - Adds embedding to database using pgvector
   - SKIPS if Identity doesn't exist (won't create new Identity)
3. Reports statistics

Usage:
    python scripts/backfill_pgvector_embeddings.py
    python scripts/backfill_pgvector_embeddings.py --dry-run
"""

import os
import sys
import asyncio
import logging
import argparse
from pathlib import Path
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import cv2
import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config import settings
from db_connection import db_manager
from db_models import Identity, IdentityType, IdentityStatus, IdentityEmbedding
from backend.core import model_manager
from backend.utils.path_utils import normalize_storage_path

# Get identity_service (will be initialized during import if services are running)
def get_identity_service():
    """Get identity_service instance"""
    try:
        import backend.core
        if hasattr(backend.core, 'identity_service') and backend.core.identity_service is not None:
            return backend.core.identity_service
    except (ImportError, AttributeError):
        pass
    
    try:
        from backend.core.identity_service import identity_service
        return identity_service if identity_service is not None else None
    except (ImportError, AttributeError):
        return None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def process_image(image_path: str, person_name: str, db: AsyncSession, dry_run: bool = False) -> dict:
    """
    Process a single image: detect face, generate embedding, save to database.
    
    Returns:
        dict with status: 'created', 'updated', 'skipped', or 'error'
    """
    result = {
        'status': 'error',
        'identity_id': None,
        'embedding_saved': False,
        'error': None
    }
    
    try:
        # Read image
        image = cv2.imread(image_path)
        if image is None:
            result['error'] = f"Could not read image: {image_path}"
            return result
        
        # Detect face
        bboxes, kpss = model_manager.detector.detect(image, max_num=1)
        if kpss is None or len(kpss) == 0:
            result['error'] = f"No face detected in image"
            return result
        
        # Generate embedding
        embedding = model_manager.recognizer.get_embedding(image, kpss[0])
        embedding_normalized = embedding / np.linalg.norm(embedding) if np.linalg.norm(embedding) > 0 else embedding
        
        # Check if Identity exists FIRST - try exact match, then try with filename (for timestamped names)
        result_query = await db.execute(
            select(Identity).where(
                Identity.display_name == person_name,
                Identity.type == IdentityType.KNOWN,
                Identity.status == IdentityStatus.ACTIVE
            )
        )
        existing_identity = result_query.scalar_one_or_none()
        
        # If not found, try matching with filename (without extension) - handles timestamped names
        if not existing_identity:
            filename_without_ext = os.path.splitext(os.path.basename(image_path))[0]
            result_query = await db.execute(
                select(Identity).where(
                    Identity.display_name == filename_without_ext,
                    Identity.type == IdentityType.KNOWN,
                    Identity.status == IdentityStatus.ACTIVE
                )
            )
            existing_identity = result_query.scalar_one_or_none()
            if existing_identity:
                logger.info(f"   Found identity by filename: {filename_without_ext}")
        
        if not existing_identity:
            # Skip if Identity doesn't exist - only update existing ones, never create new
            result['status'] = 'skipped'
            result['error'] = f"Identity '{person_name}' does not exist - skipping (only updating existing identities, no new rows created)"
            logger.warning(f"⏭️  Skipping {person_name} - Identity does not exist (only updating existing identities, no new rows created)")
            return result
        
        # Identity exists - proceed
        identity = existing_identity
        
        if dry_run:
            logger.info(f"[DRY-RUN] Would update Identity: {person_name} (ID: {identity.id}) and add embedding")
            result['status'] = 'skipped'
            return result
        
        # Update existing Identity row (no new rows created)
        identity.last_seen_at = datetime.utcnow()
        identity.updated_at = datetime.utcnow()
        
        # Update best_snapshot_path if not set
        if not identity.best_snapshot_path:
            normalized_path = normalize_storage_path(image_path)
            identity.best_snapshot_path = normalized_path
        
        result['status'] = 'updated'
        result['identity_id'] = str(identity.id)
        logger.info(f"🔄 Updating existing Identity: {person_name} (ID: {identity.id})")
        
        # Find ALL existing embedding records to UPDATE (not create new)
        embedding_query = await db.execute(
            select(IdentityEmbedding).where(
                IdentityEmbedding.identity_id == identity.id
            ).order_by(IdentityEmbedding.created_at.desc())
        )
        existing_embeddings = embedding_query.scalars().all()
        
        if existing_embeddings:
            # UPDATE all existing embeddings
            try:
                updated_count = 0
                for existing_embedding in existing_embeddings:
                    # Update the embedding vector
                    existing_embedding.embedding = embedding_normalized.tolist() if hasattr(embedding_normalized, 'tolist') else embedding_normalized
                    existing_embedding.quality = None  # Update quality if needed
                    updated_count += 1
                
                await db.flush()
                
                result['embedding_saved'] = True
                logger.info(f"✅ Updated {updated_count} existing embedding(s) for {person_name}")
            except Exception as e:
                result['error'] = f"Error updating embeddings: {str(e)}"
                logger.error(f"❌ Error updating embeddings for {person_name}: {e}", exc_info=True)
        else:
            # No existing embedding found - skip (only update, don't create)
            result['error'] = f"No existing embedding found for {person_name} - skipping (only updating existing embeddings, no new rows created)"
            logger.warning(f"⏭️  No existing embedding found for {person_name} - skipping (only updating existing embeddings)")
        
        return result
        
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"❌ Error processing {image_path}: {e}", exc_info=True)
        return result


async def backfill_embeddings(dry_run: bool = False):
    """
    Main function to backfill embeddings for all images in storage/faces/
    """
    logger.info("=" * 80)
    logger.info("🚀 Starting pgvector Embedding Backfill")
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
    
    # Get faces directory
    faces_dir = settings.FACES_DIR
    logger.info(f"📁 Faces directory: {faces_dir}")
    
    if not os.path.exists(faces_dir):
        logger.error(f"❌ Faces directory not found: {faces_dir}")
        return
    
    # Initialize model manager if needed
    if not model_manager._initialized:
        logger.info("🔄 Initializing model manager...")
        model_manager.initialize()
    
    # Initialize identity_index and identity_service
    logger.info("🔄 Initializing identity services...")
    try:
        from backend.core.identity_index import IdentityIndexService
        from backend.core.identity_service import IdentityService
        
        # Initialize identity_index with proper parameters
        embedding_size = getattr(settings, 'IDENTITY_EMBEDDING_SIZE', 512)
        db_path = getattr(settings, 'IDENTITY_INDEX_DB_PATH', './database/identity_indexes')
        identity_index = IdentityIndexService(
            embedding_size=embedding_size,
            db_path=db_path
        )
        
        # Load existing indexes from disk
        try:
            loaded = identity_index.load()
            if loaded:
                known_index_size = identity_index.known_index.ntotal if identity_index.known_index else 0
                unknown_index_size = identity_index.unknown_index.ntotal if identity_index.unknown_index else 0
                logger.info(f"✅ Identity indexes loaded: KNOWN={known_index_size}, UNKNOWN={unknown_index_size}")
            else:
                logger.info("✅ Identity indexes initialized (empty - no existing indexes found)")
        except Exception as e:
            logger.warning(f"⚠️ Could not load existing indexes (will start fresh): {e}")
        
        # Initialize identity_service
        identity_service = IdentityService(identity_index)
        logger.info("✅ Identity service initialized")
        
        # Make it available globally (for compatibility)
        try:
            import backend.core
            backend.core.identity_service = identity_service
            backend.core.identity_index = identity_index
        except Exception as e:
            logger.warning(f"Could not set global identity_service: {e}")
        
    except Exception as e:
        logger.error(f"❌ Failed to initialize identity services: {e}", exc_info=True)
        return
    
    if not identity_service:
        logger.error("❌ identity_service is not available!")
        return
    
    logger.info(f"✅ Using backend: {'pgvector' if identity_service.use_pgvector else 'faiss'}")
    
    if not identity_service.use_pgvector:
        logger.warning("⚠️ WARNING: Not using pgvector backend!")
        logger.warning("   Set VECTOR_BACKEND=pgvector in environment to use pgvector.")
        logger.warning("   Continuing anyway, but embeddings will be saved to FAISS instead.")
    
    # Get all image files
    image_files = [
        f for f in os.listdir(faces_dir)
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".webp"))
    ]
    
    if not image_files:
        logger.warning(f"⚠️ No image files found in {faces_dir}")
        return
    
    logger.info(f"📊 Found {len(image_files)} image files to process")
    logger.info("")
    
    # Statistics
    stats = {
        'total': len(image_files),
        'created': 0,
        'updated': 0,
        'skipped': 0,
        'errors': 0,
        'embeddings_saved': 0
    }
    
    # Process each image
    async with db_manager.get_session() as db:
        for idx, filename in enumerate(image_files, 1):
            # Extract person name from filename (remove extension and timestamp)
            # Format: person_name_YYYYMMDD_HHMMSS.jpg
            name_with_timestamp = filename.rsplit(".", 1)[0]
            
            # Try to extract just the name (before last underscore + timestamp pattern)
            # Simple approach: take everything before the last underscore
            parts = name_with_timestamp.rsplit("_", 2)
            if len(parts) >= 3 and len(parts[-1]) == 6 and len(parts[-2]) == 8:
                # Likely timestamp format: YYYYMMDD_HHMMSS
                person_name = "_".join(parts[:-2])
            else:
                # No timestamp pattern, use whole name
                person_name = name_with_timestamp
            
            # Fallback: if name is empty, use filename
            if not person_name:
                person_name = name_with_timestamp
            
            image_path = os.path.join(faces_dir, filename)
            
            logger.info(f"[{idx}/{len(image_files)}] Processing: {filename} -> {person_name}")
            
            result = await process_image(image_path, person_name, db, dry_run=dry_run)
            
            # Update statistics
            if result['status'] == 'created':
                stats['created'] += 1
            elif result['status'] == 'updated':
                stats['updated'] += 1
            elif result['status'] == 'skipped':
                stats['skipped'] += 1
            else:
                stats['errors'] += 1
            
            if result['embedding_saved']:
                stats['embeddings_saved'] += 1
            
            if result['error']:
                logger.error(f"   ❌ Error: {result['error']}")
            
            # Commit after each image (or batch)
            if not dry_run:
                await db.commit()
    
    # Final statistics
    logger.info("")
    logger.info("=" * 80)
    logger.info("📊 Backfill Statistics")
    logger.info("=" * 80)
    logger.info(f"Total images: {stats['total']}")
    logger.info(f"🔄 Identities updated: {stats['updated']}")
    logger.info(f"⏭️  Skipped (not found): {stats['skipped']}")
    logger.info(f"✅ Embeddings saved: {stats['embeddings_saved']}")
    logger.info(f"❌ Errors: {stats['errors']}")
    logger.info("=" * 80)
    
    if not dry_run and stats['embeddings_saved'] > 0:
        logger.info("")
        logger.info("✅ Backfill complete! All embeddings are now in the database.")
        logger.info("💡 You can verify by running:")
        logger.info("   SELECT COUNT(*) FROM identity_embeddings WHERE embedding IS NOT NULL;")


def main():
    parser = argparse.ArgumentParser(description="Backfill pgvector embeddings for known faces")
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Dry run mode - show what would be done without making changes'
    )
    args = parser.parse_args()
    
    try:
        asyncio.run(backfill_embeddings(dry_run=args.dry_run))
    except KeyboardInterrupt:
        logger.info("\n⚠️ Interrupted by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

