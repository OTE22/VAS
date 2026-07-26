#!/usr/bin/env python3
"""
Backfill pgvector Embeddings for Unknown Faces
===============================================
Processes all unknown face images from storage/{pipeline_id}/unknown/ directories
and generates embeddings for existing unknown identities in the database.

This script:
1. Finds all unknown face images in storage directories
2. Matches them to existing unknown Identity records
3. Generates embeddings from the images
4. Saves embeddings to pgvector in the database

Usage:
    python scripts/backfill_unknown_embeddings.py [--dry-run]
"""

import os
import sys
import asyncio
import logging
import cv2
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Tuple

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db_connection import db_manager
from db_models import Identity, IdentityEmbedding, IdentityType, IdentityStatus
from backend.core import model_manager as model_manager_module

# Initialize model manager
model_manager = None

def get_identity_service():
    """Get identity service instance"""
    try:
        from backend.core.identity_service import identity_service
        return identity_service
    except (ImportError, AttributeError):
        return None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def find_unknown_images() -> Dict[str, List[str]]:
    """
    Find all unknown face images in storage directories.
    
    Returns:
        Dict mapping pipeline_id to list of image paths
    """
    storage_dir = settings.STORAGE_DIR
    unknown_images = {}
    
    logger.info(f"🔍 Scanning for unknown face images in {storage_dir}...")
    
    if not os.path.exists(storage_dir):
        logger.error(f"❌ Storage directory not found: {storage_dir}")
        return unknown_images
    
    # Find all 'unknown' subdirectories
    for root, dirs, files in os.walk(storage_dir):
        if os.path.basename(root) == 'unknown':
            # Extract pipeline_id from parent directory
            pipeline_id = os.path.basename(os.path.dirname(root))
            
            # Find all image files
            image_files = []
            for file in files:
                if file.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.webp')):
                    image_path = os.path.join(root, file)
                    image_files.append(image_path)
            
            if image_files:
                unknown_images[pipeline_id] = image_files
                logger.info(f"   Found {len(image_files)} unknown images in pipeline: {pipeline_id}")
    
    total_images = sum(len(images) for images in unknown_images.values())
    logger.info(f"✅ Found {total_images} total unknown face images across {len(unknown_images)} pipelines")
    
    return unknown_images


async def match_image_to_identity(
    image_path: str,
    pipeline_id: str,
    db: AsyncSession
) -> Optional[Identity]:
    """
    Try to match an image to an existing unknown identity.
    
    Matching strategy:
    1. Find identities for this pipeline_id
    2. Check if identity has best_snapshot_path matching this image
    3. If no match, return None (will create new embedding for first available identity)
    """
    # Get all unknown identities for this pipeline
    query = select(Identity).where(
        Identity.type == IdentityType.UNKNOWN,
        Identity.status == IdentityStatus.ACTIVE
    )
    result = await db.execute(query)
    identities = result.scalars().all()
    
    # Normalize image path for comparison
    normalized_image_path = image_path.replace('\\', '/')
    
    # Try to match by best_snapshot_path
    for identity in identities:
        if identity.best_snapshot_path:
            normalized_best_path = identity.best_snapshot_path.replace('\\', '/')
            if normalized_image_path.endswith(normalized_best_path) or normalized_best_path in normalized_image_path:
                return identity
    
    # If no match, try to find identity without embedding for this pipeline
    # (prefer identities that don't have embeddings yet)
    for identity in identities:
        # Check if identity has any embeddings
        emb_query = select(func.count(IdentityEmbedding.id)).where(
            IdentityEmbedding.identity_id == identity.id,
            IdentityEmbedding.embedding.isnot(None)
        )
        emb_count = (await db.execute(emb_query)).scalar_one()
        
        if emb_count == 0:
            # This identity has no embeddings - use it
            return identity
    
    # If all identities have embeddings, return the first one (will add another embedding)
    if identities:
        return identities[0]
    
    return None


async def process_unknown_image(
    image_path: str,
    pipeline_id: str,
    db: AsyncSession,
    dry_run: bool = False
) -> dict:
    """
    Process a single unknown face image: detect face, generate embedding, save to database.
    
    Returns:
        dict with status: 'created', 'updated', 'skipped', or 'error'
    """
    result = {
        'status': 'error',
        'identity_id': None,
        'embedding_saved': False,
        'error': None,
        'image_path': image_path
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
            result['status'] = 'skipped'
            return result
        
        # Generate embedding
        embedding = model_manager.recognizer.get_embedding(image, kpss[0])
        if embedding is None or not np.isfinite(embedding).all() or np.linalg.norm(embedding) == 0:
            result['error'] = f"Invalid embedding generated"
            result['status'] = 'skipped'
            return result
        
        embedding_normalized = embedding / np.linalg.norm(embedding) if np.linalg.norm(embedding) > 0 else embedding
        
        # Match to existing identity or create new one
        identity = await match_image_to_identity(image_path, pipeline_id, db)
        
        if not identity:
            # No matching identity found - skip (we only update existing, don't create new)
            result['status'] = 'skipped'
            result['error'] = "No matching unknown identity found"
            logger.warning(f"⏭️  Skipping {os.path.basename(image_path)} - No matching identity")
            return result
        
        if dry_run:
            logger.info(f"[DRY-RUN] Would add embedding for identity: {identity.id}")
            result['status'] = 'skipped'
            result['identity_id'] = str(identity.id)
            return result
        
        # Update identity's best_snapshot_path if not set
        normalized_path = image_path.replace('\\', '/').replace('/app/storage/', 'storage/')
        if not identity.best_snapshot_path:
            identity.best_snapshot_path = normalized_path
            identity.updated_at = datetime.utcnow()
        
        # Add embedding using identity service
        identity_service = get_identity_service()
        if identity_service:
            embedding_record = await identity_service.save_embedding(
                identity=identity,
                embedding=embedding_normalized,
                detection_id=None,
                pipeline_id=pipeline_id,
                quality_score=None,
                db=db
            )
            
            if embedding_record:
                result['status'] = 'created'
                result['identity_id'] = str(identity.id)
                result['embedding_saved'] = True
                logger.info(f"✅ Added embedding (ID: {embedding_record.id}) for identity {identity.id[:8]}... from {os.path.basename(image_path)}")
            else:
                result['error'] = "save_embedding returned None"
                result['status'] = 'error'
                logger.warning(f"⚠️ Failed to save embedding for identity {identity.id}")
        else:
            result['error'] = "identity_service not available"
            result['status'] = 'error'
            logger.error(f"❌ identity_service is None - cannot save embedding")
        
        await db.commit()
        return result
        
    except Exception as e:
        logger.error(f"❌ Error processing {image_path}: {e}", exc_info=True)
        result['error'] = str(e)
        result['status'] = 'error'
        return result


async def backfill_unknown_embeddings(dry_run: bool = False):
    """Backfill embeddings for all unknown face images"""
    logger.info("=" * 80)
    logger.info("🔄 Starting Unknown Face Embeddings Backfill")
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
    
    # Initialize model manager
    global model_manager
    logger.info("🔄 Initializing model manager...")
    try:
        model_manager = model_manager_module.model_manager
        if model_manager is None:
            # Try to get it from the module
            if hasattr(model_manager_module, 'model_manager'):
                model_manager = model_manager_module.model_manager
            else:
                # Create new instance
                from backend.core.model_manager import ModelManager
                model_manager = ModelManager()
                model_manager.initialize()
        else:
            # Ensure it's initialized
            if not model_manager._initialized:
                model_manager.initialize()
        logger.info("✅ Model manager initialized")
    except Exception as e:
        logger.error(f"❌ Model manager initialization failed: {e}", exc_info=True)
        return
    
    # Find all unknown images
    unknown_images = await find_unknown_images()
    
    if not unknown_images:
        logger.info("ℹ️  No unknown face images found. Nothing to backfill.")
        return
    
    # Process each image
    total_processed = 0
    total_created = 0
    total_skipped = 0
    total_errors = 0
    
    async with db_manager.get_session() as db:
        for pipeline_id, image_paths in unknown_images.items():
            logger.info(f"\n📦 Processing pipeline: {pipeline_id} ({len(image_paths)} images)")
            
            for image_path in image_paths:
                result = await process_unknown_image(image_path, pipeline_id, db, dry_run)
                total_processed += 1
                
                if result['status'] == 'created':
                    total_created += 1
                elif result['status'] == 'skipped':
                    total_skipped += 1
                elif result['status'] == 'error':
                    total_errors += 1
                
                # Log progress every 10 images
                if total_processed % 10 == 0:
                    logger.info(f"   Progress: {total_processed} processed, {total_created} created, {total_skipped} skipped, {total_errors} errors")
    
    logger.info("\n" + "=" * 80)
    logger.info("📊 Backfill Statistics")
    logger.info("=" * 80)
    logger.info(f"✅ Total processed: {total_processed}")
    logger.info(f"✅ Embeddings created: {total_created}")
    logger.info(f"⏭️  Skipped: {total_skipped}")
    logger.info(f"❌ Errors: {total_errors}")
    logger.info("=" * 80)
    
    if not dry_run and total_created > 0:
        logger.info(f"\n✅ Successfully backfilled {total_created} embeddings for unknown faces!")
        logger.info("   Run the check script again to verify: python scripts/check_unknown_embeddings.py")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Backfill pgvector embeddings for unknown faces")
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode - don't make changes")
    args = parser.parse_args()
    
    try:
        asyncio.run(backfill_unknown_embeddings(dry_run=args.dry_run))
    finally:
        asyncio.run(db_manager.close_db())

