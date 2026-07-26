#!/usr/bin/env python3
"""
Fix NULL Embeddings
===================
Finds identities with NULL embeddings and updates them by matching identity names to filenames.

Usage:
    python scripts/fix_null_embeddings.py
"""

import os
import sys
import asyncio
import logging
import cv2
import numpy as np
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config import settings
from db_connection import db_manager
from db_models import Identity, IdentityEmbedding, IdentityType, IdentityStatus
from backend.core import model_manager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def fix_null_embeddings():
    """
    Find identities with NULL embeddings and update them
    """
    logger.info("=" * 80)
    logger.info("🔧 Fixing NULL Embeddings")
    logger.info("=" * 80)
    
    # Initialize database
    logger.info("🔄 Initializing database...")
    try:
        await db_manager.init_db()
        logger.info("✅ Database initialized")
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        return
    
    # Initialize model manager if needed
    if not model_manager._initialized:
        logger.info("🔄 Initializing model manager...")
        model_manager.initialize()
    
    # Get faces directory
    faces_dir = settings.FACES_DIR
    logger.info(f"📁 Faces directory: {faces_dir}")
    
    if not os.path.exists(faces_dir):
        logger.error(f"❌ Faces directory not found: {faces_dir}")
        return
    
    # Get all image files
    image_files = [
        f for f in os.listdir(faces_dir)
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".webp"))
    ]
    
    logger.info(f"📊 Found {len(image_files)} image files")
    logger.info("")
    
    async with db_manager.get_session() as db:
        # Find all identities with NULL embeddings
        null_embeddings_query = await db.execute(
            select(IdentityEmbedding, Identity).join(
                Identity, IdentityEmbedding.identity_id == Identity.id
            ).where(
                IdentityEmbedding.embedding.is_(None),
                Identity.type == IdentityType.KNOWN,
                Identity.status == IdentityStatus.ACTIVE
            )
        )
        
        null_embeddings = null_embeddings_query.all()
        
        if not null_embeddings:
            logger.info("✅ No NULL embeddings found!")
            return
        
        logger.info(f"🔍 Found {len(null_embeddings)} identities with NULL embeddings")
        logger.info("")
        
        updated_count = 0
        skipped_count = 0
        
        for emb, identity in null_embeddings:
            identity_name = identity.display_name
            logger.info(f"Processing: {identity_name} (ID: {identity.id})")
            
            # Try to find matching image file
            matching_file = None
            for filename in image_files:
                filename_without_ext = os.path.splitext(filename)[0]
                # Check if identity name matches filename (with or without extension)
                if identity_name == filename_without_ext or identity_name == filename:
                    matching_file = filename
                    break
                # Also check if identity name is in filename
                if identity_name in filename or filename_without_ext in identity_name:
                    matching_file = filename
                    break
            
            if not matching_file:
                logger.warning(f"   ⏭️  No matching image file found for '{identity_name}'")
                skipped_count += 1
                continue
            
            image_path = os.path.join(faces_dir, matching_file)
            logger.info(f"   📷 Found matching image: {matching_file}")
            
            try:
                # Read image
                image = cv2.imread(image_path)
                if image is None:
                    logger.error(f"   ❌ Could not read image: {image_path}")
                    skipped_count += 1
                    continue
                
                # Detect face
                bboxes, kpss = model_manager.detector.detect(image, max_num=1)
                if kpss is None or len(kpss) == 0:
                    logger.error(f"   ❌ No face detected in image")
                    skipped_count += 1
                    continue
                
                # Generate embedding
                embedding = model_manager.recognizer.get_embedding(image, kpss[0])
                embedding_normalized = embedding / np.linalg.norm(embedding) if np.linalg.norm(embedding) > 0 else embedding
                
                # Update the embedding
                emb.embedding = embedding_normalized.tolist() if hasattr(embedding_normalized, 'tolist') else embedding_normalized
                emb.quality = None
                
                await db.flush()
                await db.commit()
                
                logger.info(f"   ✅ Updated embedding (ID: {emb.id})")
                updated_count += 1
                
            except Exception as e:
                logger.error(f"   ❌ Error processing {identity_name}: {e}", exc_info=True)
                skipped_count += 1
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("📊 Fix Statistics")
    logger.info("=" * 80)
    logger.info(f"✅ Updated: {updated_count}")
    logger.info(f"⏭️  Skipped: {skipped_count}")
    logger.info("=" * 80)


def main():
    try:
        asyncio.run(fix_null_embeddings())
    except KeyboardInterrupt:
        logger.info("\n⚠️ Interrupted by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

