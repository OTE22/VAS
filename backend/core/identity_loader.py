"""
Identity Loader Service
=======================
Loads known faces from storage/faces directory into the Identity system.
Supports both FAISS and pgvector backends.

Set VECTOR_BACKEND=pgvector in environment to use pgvector.
"""

import os
import cv2
import uuid
import logging
import numpy as np
from datetime import datetime
from typing import List, Tuple, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db_models import Identity, IdentityType, IdentityStatus, IdentityEmbedding
from config import settings

# Check vector backend
VECTOR_BACKEND = getattr(settings, 'VECTOR_BACKEND', 'faiss').lower()
USE_PGVECTOR = VECTOR_BACKEND == 'pgvector'

logger = logging.getLogger(__name__)
logger.info(f"[IDENTITY_LOADER] Vector backend: {VECTOR_BACKEND}")


class IdentityLoader:
    """Loads known faces from storage/faces into Identity system (unified storage)"""
    
    def __init__(self, identity_service, model_manager):
        self.identity_service = identity_service
        self.model_manager = model_manager
    
    async def load_known_faces_from_directory(
        self,
        faces_dir: str,
        db: AsyncSession,
        force_reload: bool = False
    ) -> Tuple[int, int, int]:
        """
        Load known faces from storage/faces directory into Identity system.
        Returns: (loaded_count, skipped_count, error_count)
        """
        logger.info("=" * 70)
        logger.info("[IDENTITY_LOADER] 🚀 Starting known faces loading process...")
        logger.info(f"[IDENTITY_LOADER] Faces directory: {faces_dir}")
        logger.info(f"[IDENTITY_LOADER] Force reload: {force_reload}")
        logger.info("=" * 70)
        
        if not os.path.exists(faces_dir):
            logger.warning(f"Faces directory not found: {faces_dir}")
            return 0, 0, 0
        
        # Support both flat structure (old) and folder structure (new)
        # New structure: faces/John_Doe/image1.jpg, faces/John_Doe/image2.jpg
        # Old structure: faces/john_doe.jpg (backward compatible)
        image_files = []
        person_folders = []
        
        # Check if we have folder structure (person folders)
        for item in os.listdir(faces_dir):
            item_path = os.path.join(faces_dir, item)
            if os.path.isdir(item_path):
                # This is a person folder - scan for images inside
                person_folders.append(item)
                folder_images = [
                    os.path.join(item, f) for f in os.listdir(item_path)
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))
                ]
                image_files.extend(folder_images)
            elif item.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                # Flat structure (old format) - backward compatible
                image_files.append(item)
        
        if person_folders:
            logger.info(f"Found {len(person_folders)} person folders with {len(image_files)} total images")
        else:
            logger.info(f"Using flat structure: found {len(image_files)} image files")
        
        if not image_files:
            logger.info(f"No image files found in {faces_dir}")
            return 0, 0, 0
        
        logger.info(f"Loading {len(image_files)} known faces from {faces_dir}...")
        
        # Check if index needs training (IVF/IVFPQ indexes)
        index = self.identity_service.identity_index.known_index
        needs_training = hasattr(index, 'is_trained') and not index.is_trained
        
        if needs_training:
            logger.warning("KNOWN index requires training! Collecting embeddings for training first...")
            # Collect embeddings for training (use 10-20% of data or min 10k samples)
            training_embeddings = []
            training_count = min(max(10000, len(image_files) // 10), len(image_files))
        
        loaded_count = 0
        skipped_count = 0
        error_count = 0
        embeddings_for_training = []
        
        for filename in image_files:
            try:
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ========================================")
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] 📸 Processing image: {filename}")
                
                # Extract person name from filename or folder
                # New structure: "John_Doe/image1.jpg" -> person_name = "John_Doe"
                # Old structure: "john_doe.jpg" -> person_name = "john_doe"
                if os.sep in filename or "/" in filename:
                    # Folder structure: extract folder name as person name
                    person_name = filename.split(os.sep)[0].split("/")[0]
                    image_path = os.path.join(faces_dir, filename)
                else:
                    # Flat structure: extract from filename (remove extension)
                    person_name = filename.rsplit(".", 1)[0]
                    image_path = os.path.join(faces_dir, filename)
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] Person name: {person_name}")
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] Image path: {image_path}")
                
                # Check if identity already exists
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] 🔍 Checking if identity '{person_name}' already exists...")
                if not force_reload:
                    result = await db.execute(
                        select(Identity).where(
                            Identity.type == IdentityType.KNOWN,
                            Identity.display_name == person_name
                        )
                    )
                    existing = result.scalar_one_or_none()
                    if existing:
                        logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ✅ Identity '{person_name}' already exists (ID: {existing.id})")
                        identity_id_str = str(existing.id)
                        
                        # Check if identity has embeddings (FAISS or pgvector)
                        has_embeddings = False
                        backend_name = "unknown"
                        
                        if USE_PGVECTOR and self.identity_service.use_pgvector:
                            # Check for pgvector embeddings in database
                            from db_models import IdentityEmbedding
                            # NOTE: do NOT filter on faiss_index_type here - historical
                            # rows have NULL there, and filtering made this check miss
                            # them, re-adding a duplicate embedding on EVERY restart.
                            emb_result = await db.execute(
                                select(IdentityEmbedding).where(
                                    IdentityEmbedding.identity_id == existing.id,
                                    IdentityEmbedding.embedding.isnot(None)
                                ).limit(1)
                            )
                            pgvector_emb = emb_result.scalar_one_or_none()
                            has_embeddings = pgvector_emb is not None
                            backend_name = "pgvector"
                            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Checking pgvector embeddings: {'✅ found' if has_embeddings else '❌ not found'}")
                        else:
                            # Check for FAISS embeddings
                            has_embeddings = (
                                identity_id_str in self.identity_service.identity_index.known_identity_to_faiss and
                                len(self.identity_service.identity_index.known_identity_to_faiss[identity_id_str]) > 0
                            )
                            backend_name = "FAISS"
                            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Checking FAISS embeddings: {'✅ found' if has_embeddings else '❌ not found'}")
                        
                        if has_embeddings:
                            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Identity has {backend_name} embeddings - skipping")
                            logger.debug(f"Skipping {person_name} - already exists (ID: {existing.id}) with {backend_name} embeddings")
                            # Check if best_snapshot_path is missing and try to find it
                            if not existing.best_snapshot_path:
                                logger.info(f"[IDENTITY_LOADER] Identity {person_name} exists but missing best_snapshot_path. Searching for images...")
                                best_snapshot_path = await self._find_best_image_from_storage(person_name, db, identity_id=existing.id)
                                if best_snapshot_path:
                                    existing.best_snapshot_path = best_snapshot_path
                                    await db.flush()
                                    logger.info(f"[IDENTITY_LOADER] ✅ Updated best_snapshot_path for {person_name}: {best_snapshot_path}")
                                else:
                                    logger.warning(f"[IDENTITY_LOADER] ⚠️ No image found in storage for {person_name}. Image will be set when person is first detected.")
                            skipped_count += 1
                            continue
                        else:
                            # Identity exists but no embeddings - add them
                            backend_name = "pgvector" if (USE_PGVECTOR and self.identity_service.use_pgvector) else "FAISS"
                            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ⚠️ Identity exists but missing {backend_name} embeddings. Adding embedding...")
                            logger.info(f"[IDENTITY_LOADER] Identity {person_name} exists but missing {backend_name} embeddings. Adding embedding...")
                            try:
                                # Extract embedding from image
                                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Extracting embedding from image...")
                                embedding = await self._extract_embedding(image_path)
                                if embedding is not None:
                                    embedding_norm = np.linalg.norm(embedding)
                                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding extracted: shape={embedding.shape}, norm={embedding_norm:.6f}")
                                    
                                    if USE_PGVECTOR and self.identity_service.use_pgvector:
                                        # Add to pgvector (skip if a near-identical
                                        # embedding is already stored - restart dedup)
                                        logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Saving to pgvector...")
                                        embedding_normalized = embedding / np.linalg.norm(embedding)
                                        existing_sim = await self.identity_service.pgvector_index.max_similarity_to_identity(
                                            str(existing.id), embedding_normalized, db
                                        )
                                        if existing_sim is not None and existing_sim >= 0.999:
                                            logger.info(f"[IDENTITY_LOADER]    ⏭️ Identical embedding already stored (sim={existing_sim:.4f}) - skipping duplicate")
                                            emb_id = None
                                        else:
                                            emb_id = await self.identity_service.pgvector_index.add_embedding(
                                                identity_id=str(existing.id),
                                                embedding=embedding_normalized,
                                                detection_id=None,
                                                pipeline_id="preloaded",
                                                quality_score=None,
                                                index_type='known',
                                                db=db
                                            )
                                        if emb_id:
                                            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding saved to pgvector: emb_id={emb_id}")
                                        else:
                                            logger.warning(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ❌ Failed to save embedding to pgvector")
                                    else:
                                        # Add to FAISS
                                        logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Adding to FAISS...")
                                        faiss_id = self.identity_service.identity_index.add_known(identity_id_str, embedding)
                                        logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding added to FAISS: faiss_id={faiss_id}")
                                        
                                        # Check if embedding record exists in database
                                        logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Checking for existing embedding record...")
                                        emb_result = await db.execute(
                                            select(IdentityEmbedding).where(
                                                IdentityEmbedding.identity_id == existing.id,
                                                IdentityEmbedding.faiss_index_type == 'known',
                                                IdentityEmbedding.faiss_id.is_(None)
                                            ).limit(1)
                                        )
                                        emb_record = emb_result.scalar_one_or_none()
                                        
                                        if emb_record:
                                            # Update existing record
                                            emb_record.faiss_id = faiss_id
                                            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Updated existing embedding record with faiss_id={faiss_id}")
                                        else:
                                            # Create new embedding record
                                            emb_record = IdentityEmbedding(
                                                identity_id=existing.id,
                                                detection_id=None,
                                                pipeline_id="preloaded",
                                                faiss_id=faiss_id,
                                                faiss_index_type='known',
                                                quality=None,
                                                created_at=datetime.utcnow()
                                            )
                                            db.add(emb_record)
                                            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Created new embedding record with faiss_id={faiss_id}")
                                        
                                        await db.flush()
                                    
                                    # Also check and update best_snapshot_path if missing
                                    if not existing.best_snapshot_path:
                                        logger.info(f"[IDENTITY_LOADER] Identity {person_name} missing best_snapshot_path. Searching for images...")
                                        best_snapshot_path = await self._find_best_image_from_storage(person_name, db, identity_id=existing.id)
                                        if best_snapshot_path:
                                            existing.best_snapshot_path = best_snapshot_path
                                            await db.flush()
                                            logger.info(f"[IDENTITY_LOADER] ✅ Updated best_snapshot_path for {person_name}: {best_snapshot_path}")
                                        else:
                                            logger.warning(f"[IDENTITY_LOADER] ⚠️ No image found in storage for {person_name}. Image will be set when person is first detected.")
                                    
                                    loaded_count += 1
                                    backend_name = "pgvector" if (USE_PGVECTOR and self.identity_service.use_pgvector) else "FAISS"
                                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Added {backend_name} embedding for existing identity: {person_name} (ID: {existing.id})")
                                    logger.info(f"[IDENTITY_LOADER] ✅ Added {backend_name} embedding for existing identity: {person_name} (ID: {existing.id})")
                                else:
                                    logger.warning(f"[IDENTITY_LOADER] ⚠️ Failed to extract embedding for {person_name}, skipping")
                                    skipped_count += 1
                            except Exception as e:
                                logger.error(f"[IDENTITY_LOADER] ❌ Error adding FAISS embedding for {person_name}: {e}", exc_info=True)
                                skipped_count += 1
                            continue
                
                # For training: collect embedding first
                if needs_training and len(embeddings_for_training) < training_count:
                    embedding = await self._extract_embedding(image_path)
                    if embedding is not None:
                        embeddings_for_training.append(embedding)
                
                # Load and process image
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] 🔄 Starting face processing for '{person_name}'...")
                success, identity = await self._load_single_face(
                    image_path=image_path,
                    person_name=person_name,
                    db=db
                )
                
                if success:
                    loaded_count += 1
                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ✅ Successfully loaded known face: {person_name} (ID: {identity.id})")
                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ========================================")
                else:
                    error_count += 1
                    logger.warning(f"[IDENTITY_LOADER] [STEP-BY-STEP] ❌ Failed to load {person_name}")
                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ========================================")
            except Exception as e:
                error_count += 1
                logger.error(f"❌ Error processing {filename}: {e}", exc_info=True)
        
        # Train index if needed (before adding vectors)
        if needs_training and len(embeddings_for_training) > 0:
            logger.info(f"Training KNOWN index with {len(embeddings_for_training)} samples...")
            training_array = np.array(embeddings_for_training)
            trained = self.identity_service.identity_index.train_known_index(training_array)
            if not trained:
                logger.error("❌ Index training failed! Cannot add vectors.")
                return loaded_count, skipped_count, error_count
            logger.info("✅ Index training complete")
        
        # Save indexes after loading
        if loaded_count > 0 and self.identity_service.identity_index:
            try:
                self.identity_service.identity_index.save()
                logger.info(f"💾 Saved identity indexes after loading {loaded_count} known faces")
            except Exception as e:
                logger.error(f"Failed to save identity indexes: {e}")
        
        logger.info(f"✅ Known faces loading complete: {loaded_count} loaded, {skipped_count} skipped, {error_count} errors")
        
        # Log final index state
        if USE_PGVECTOR and self.identity_service.use_pgvector:
            # pgvector backend - log database state
            try:
                from sqlalchemy import func, text
                # Use enum value instead of string - PostgreSQL enum requires proper casting
                count_result = await db.execute(
                    select(func.count(IdentityEmbedding.id))
                    .join(Identity, IdentityEmbedding.identity_id == Identity.id)
                    .where(
                        IdentityEmbedding.embedding.isnot(None),
                        Identity.type == IdentityType.KNOWN
                    )
                )
                known_count = count_result.scalar() or 0
                logger.info(f"[IDENTITY_LOADER] [PGVECTOR] Final KNOWN index state: {known_count} embeddings in PostgreSQL")
                if known_count == 0:
                    logger.error(f"[IDENTITY_LOADER] ❌❌❌ CRITICAL: KNOWN index is EMPTY after loading!")
                else:
                    logger.info(f"[IDENTITY_LOADER] ✅ KNOWN pgvector index ready with {known_count} embeddings")
            except Exception as e:
                logger.warning(f"[IDENTITY_LOADER] Could not verify pgvector index state: {e}")
        elif self.identity_service and self.identity_service.identity_index:
            # FAISS backend - log FAISS state
            known_index_size = self.identity_service.identity_index.known_index.ntotal if self.identity_service.identity_index.known_index else 0
            known_identities_count = len(self.identity_service.identity_index.known_identity_to_faiss)
            logger.info(f"[IDENTITY_LOADER] [FAISS] Final KNOWN index state: {known_index_size} vectors, {known_identities_count} identities")
            if known_index_size == 0:
                logger.error(f"[IDENTITY_LOADER] ❌❌❌ CRITICAL: KNOWN index is EMPTY after loading! No known faces are available for recognition!")
            else:
                logger.info(f"[IDENTITY_LOADER] ✅ KNOWN FAISS index ready with {known_index_size} vectors")
        
        return loaded_count, skipped_count, error_count
    
    async def _load_single_face(
        self,
        image_path: str,
        person_name: str,
        db: AsyncSession
    ) -> Tuple[bool, Optional[Identity]]:
        """
        Load a single known face from image file.
        
        Returns:
            Tuple of (success: bool, identity: Optional[Identity])
        """
        try:
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] 📖 Step 1: Reading image from {image_path}...")
            # Read image
            image = cv2.imread(image_path)
            if image is None:
                logger.warning(f"[IDENTITY_LOADER] [STEP-BY-STEP] ❌ Could not read image: {image_path}")
                return False, None
            
            image_shape = image.shape
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ✅ Image loaded: shape={image_shape} (HxWxC)")
            
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] 🔍 Step 2: Face Detection (SCRFD Model)...")
            # Detect face
            bboxes, kpss = self.model_manager.detector.detect(image, max_num=1)
            if kpss is None or len(kpss) == 0:
                # No face detected - check if this is a small face image (already cropped)
                h, w = image.shape[:2]
                # Common face crop sizes: 112x112, 128x128, 160x160, etc.
                # If image is small and roughly square, treat as face image
                is_small_image = (h <= 200 and w <= 200) and (abs(h - w) <= 20)
                
                if is_small_image:
                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ℹ️  No face detected, but image is small ({h}x{w}) - treating as face image")
                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Skipping face detection, using entire image for embedding")
                    
                    # Create approximate keypoints at image center (for face alignment)
                    center_x, center_y = w // 2, h // 2
                    kpss = np.array([[
                        [center_x - w * 0.15, center_y - h * 0.1],  # Left eye
                        [center_x + w * 0.15, center_y - h * 0.1],  # Right eye
                        [center_x, center_y],                         # Nose
                        [center_x - w * 0.1, center_y + h * 0.15],  # Left mouth corner
                        [center_x + w * 0.1, center_y + h * 0.15]   # Right mouth corner
                    ]], dtype=np.float32)
                    bboxes = np.array([[0, 0, w, h, 1.0]], dtype=np.float32)
                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ✅ Using entire image as face region for embedding generation")
                else:
                    logger.warning(f"[IDENTITY_LOADER] [STEP-BY-STEP] ❌ No face detected in {image_path} (image size: {h}x{w})")
                    return False, None
            else:
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ✅ SCRFD: Face detected successfully")
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Bounding boxes: {len(bboxes)}, Landmarks: {len(kpss)}")
                if len(bboxes) > 0:
                    confidence = bboxes[0][4] if len(bboxes[0]) > 4 else 'N/A'
                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Confidence: {confidence}")
            
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] 🔍 Step 3: Embedding Generation (ArcFace Model)...")
            # Generate embedding
            embedding = self.model_manager.recognizer.get_embedding(image, kpss[0])
            
            embedding_norm = np.linalg.norm(embedding)
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ✅ ArcFace: Embedding generated successfully")
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Embedding shape: {embedding.shape}, norm: {embedding_norm:.6f}")
            
            # Create Identity record
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] 🔍 Step 4: Creating Identity Record...")
            identity_id = uuid.uuid4()
            now = datetime.utcnow()
            
            # Try to find existing images in storage/pipeline_id/person_name/ structure
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Searching for existing images in storage for {person_name}...")
            best_snapshot_path = await self._find_best_image_from_storage(person_name, db)
            
            if best_snapshot_path:
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Found existing image: {best_snapshot_path}")
            else:
                logger.warning(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ⚠️ No existing image found. Will be set when person is first detected.")
            
            identity = Identity(
                id=identity_id,
                type=IdentityType.KNOWN,
                status=IdentityStatus.ACTIVE,
                display_name=person_name,
                first_seen_at=now,
                last_seen_at=now,
                appearances_count=0,
                best_snapshot_path=best_snapshot_path,
                created_at=now,
                updated_at=now
            )
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ✅ Identity record created: ID={identity_id}, name='{person_name}'")
            db.add(identity)
            await db.flush()
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Identity saved to database")
            
            # Add embedding to appropriate backend (FAISS or pgvector)
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] 🔍 Step 5: Saving Embedding...")
            if USE_PGVECTOR and self.identity_service.use_pgvector and self.identity_service.pgvector_index:
                # pgvector backend - store embedding directly in PostgreSQL
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Using pgvector backend")
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Normalizing embedding (L2 norm)...")
                
                # Normalize embedding
                embedding_normalized = embedding / np.linalg.norm(embedding)
                final_norm = np.linalg.norm(embedding_normalized)
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding normalized: norm={final_norm:.6f} (should be 1.0)")
                
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Saving to PostgreSQL (pgvector)...")
                emb_id = await self.identity_service.pgvector_index.add_embedding(
                    identity_id=str(identity_id),
                    embedding=embedding_normalized,
                    detection_id=None,
                    pipeline_id="preloaded",
                    quality_score=None,
                    index_type='known',
                    db=db
                )
                
                if emb_id:
                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding saved to pgvector: emb_id={emb_id}")
                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding stored in identity_embeddings table (vector type)")
                else:
                    logger.warning(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ❌ Failed to save embedding to pgvector")
            else:
                # FAISS backend - add to in-memory index
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Using FAISS backend")
                
                # Check if index needs training (IVF indexes)
                index = self.identity_service.identity_index.known_index
                if hasattr(index, 'is_trained') and not index.is_trained:
                    logger.warning(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ⚠️ KNOWN index is not trained! Training required for IVF indexes.")
                    # For now, we'll add anyway (will fail for IVF, but at least we log it)
                
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Adding to FAISS known_index (will normalize internally)...")
                faiss_id = self.identity_service.identity_index.add_known(
                    str(identity_id),
                    embedding
                )
                
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding added to FAISS: faiss_id={faiss_id}")
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Saving embedding record to database...")
                
                # Save embedding record
                embedding_record = IdentityEmbedding(
                    identity_id=identity_id,
                    detection_id=None,  # No detection for pre-loaded faces
                    pipeline_id="preloaded",  # Special pipeline ID for pre-loaded faces
                    faiss_id=faiss_id,
                    faiss_index_type='known',
                    quality=None,  # Quality not calculated for pre-loaded faces
                    created_at=now
                )
                db.add(embedding_record)
                await db.flush()
                
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding record saved to database (faiss_id={faiss_id})")
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding stored in FAISS index (in-memory)")
            
            return True, identity
            
        except Exception as e:
            logger.error(f"Error loading face from {image_path}: {e}", exc_info=True)
            return False, None
    
    async def _extract_embedding(self, image_path: str) -> Optional[np.ndarray]:
        """Extract embedding from image file (for training purposes)"""
        try:
            image = cv2.imread(image_path)
            if image is None:
                return None
            
            bboxes, kpss = self.model_manager.detector.detect(image, max_num=1)
            if kpss is None or len(kpss) == 0:
                # No face detected - check if this is a small face image (already cropped)
                h, w = image.shape[:2]
                # Common face crop sizes: 112x112, 128x128, 160x160, etc.
                # If image is small and roughly square, treat as face image
                is_small_image = (h <= 200 and w <= 200) and (abs(h - w) <= 20)
                
                if is_small_image:
                    logger.debug(f"[IDENTITY_LOADER] No face detected in {image_path}, but image is small ({h}x{w}) - treating as face image")
                    # Create approximate keypoints at image center
                    center_x, center_y = w // 2, h // 2
                    kpss = np.array([[
                        [center_x - w * 0.15, center_y - h * 0.1],  # Left eye
                        [center_x + w * 0.15, center_y - h * 0.1],  # Right eye
                        [center_x, center_y],                         # Nose
                        [center_x - w * 0.1, center_y + h * 0.15],  # Left mouth corner
                        [center_x + w * 0.1, center_y + h * 0.15]   # Right mouth corner
                    ]], dtype=np.float32)
                else:
                    return None
            
            embedding = self.model_manager.recognizer.get_embedding(image, kpss[0])
            return embedding
            
        except Exception as e:
            logger.warning(f"Failed to extract embedding from {image_path}: {e}")
            return None
    
    async def _find_best_image_from_storage(self, person_name: str, db: AsyncSession, identity_id: Optional[uuid.UUID] = None) -> Optional[str]:
        """
        Find the best quality image from storage/pipeline_id/person_name/ structure.
        Searches all pipelines for this person's name or identity_id and returns the best quality image.
        
        Args:
            person_name: Name of the person to search for
            db: Database session
            identity_id: Optional identity UUID to search by (more reliable than name)
        """
        try:
            from config import settings
            from db_models import Face, Detection, IdentityEmbedding
            
            storage_dir = getattr(settings, 'STORAGE_DIR', './storage')
            storage_dir_abs = os.path.abspath(storage_dir)
            safe_name = "".join(c for c in person_name if c.isalnum() or c in ('-', '_')).lower()
            
            logger.info(f"[IDENTITY_LOADER] Searching for images for '{person_name}' (safe_name: '{safe_name}', identity_id: {identity_id}) in storage: {storage_dir}")
            
            # Build query - prefer identity_id if available, fallback to name
            query = (
                select(
                    Face.face_image_path,
                    Detection.pipeline_id,
                    IdentityEmbedding.quality
                )
                .join(Detection, Face.detection_id == Detection.id)
                .outerjoin(IdentityEmbedding, 
                    (IdentityEmbedding.detection_id == Face.detection_id) &
                    (IdentityEmbedding.identity_id == Face.identity_id)
                )
                .where(Face.face_image_path.isnot(None))
            )
            
            if identity_id:
                # Search by identity_id (more reliable)
                query = query.where(Face.identity_id == identity_id)
                logger.debug(f"[IDENTITY_LOADER] Querying database for face records with identity_id='{identity_id}'...")
            else:
                # Fallback to name search
                query = query.where(Face.name == person_name)
                logger.debug(f"[IDENTITY_LOADER] Querying database for face records with name='{person_name}'...")
            
            query = query.order_by(
                IdentityEmbedding.quality.desc().nulls_last(),
                Detection.timestamp.desc()
            ).limit(10)  # Check top 10 by quality
            
            result = await db.execute(query)
            
            rows = result.all()
            logger.info(f"[IDENTITY_LOADER] Found {len(rows)} face records in database for '{person_name}'")
            
            best_path = None
            best_quality = None
            
            for idx, row in enumerate(rows):
                face_path, pipeline_id, quality = row
                logger.debug(f"[IDENTITY_LOADER] Checking face record {idx+1}: path={face_path}, pipeline={pipeline_id}, quality={quality}")
                
                if not face_path:
                    logger.debug(f"[IDENTITY_LOADER] Skipping record {idx+1} - no face_image_path")
                    continue
                
                # Check if file exists
                if os.path.exists(face_path):
                    logger.debug(f"[IDENTITY_LOADER] ✅ File exists: {face_path}")
                    # If we have quality scores, prefer highest quality
                    if quality is not None:
                        if best_quality is None or quality > best_quality:
                            best_path = face_path
                            best_quality = quality
                            logger.info(f"[IDENTITY_LOADER] Updated best path (quality={quality:.3f}): {best_path}")
                    elif best_path is None:
                        # If no quality scores, use first found
                        best_path = face_path
                        logger.info(f"[IDENTITY_LOADER] Using first found path (no quality score): {best_path}")
                else:
                    logger.warning(f"[IDENTITY_LOADER] ⚠️ File does not exist: {face_path}")
            
            # If no path found in database, try to find in storage structure directly
            if not best_path:
                logger.info(f"[IDENTITY_LOADER] No valid paths found in database. Searching storage directory structure...")
                if os.path.exists(storage_dir):
                    logger.debug(f"[IDENTITY_LOADER] Storage directory exists: {storage_dir}")
                    pipeline_dirs = [d for d in os.listdir(storage_dir) if os.path.isdir(os.path.join(storage_dir, d))]
                    logger.info(f"[IDENTITY_LOADER] Found {len(pipeline_dirs)} pipeline directories in storage")
                    
                    # Search all pipeline directories
                    for pipeline_dir_name in pipeline_dirs:
                        pipeline_path = os.path.join(storage_dir, pipeline_dir_name)
                        person_dir = os.path.join(pipeline_path, safe_name)
                        logger.debug(f"[IDENTITY_LOADER] Checking: {person_dir}")
                        
                        if os.path.exists(person_dir) and os.path.isdir(person_dir):
                            logger.info(f"[IDENTITY_LOADER] ✅ Found person directory: {person_dir}")
                            # Get all images in this directory
                            image_files = [
                                f for f in os.listdir(person_dir)
                                if f.lower().endswith(('.jpg', '.jpeg', '.png'))
                            ]
                            logger.info(f"[IDENTITY_LOADER] Found {len(image_files)} image files in {person_dir}")
                            
                            if image_files:
                                # Use most recent image (by filename timestamp)
                                image_files.sort(reverse=True)
                                best_path = os.path.join(person_dir, image_files[0])
                                logger.info(f"[IDENTITY_LOADER] ✅ Selected image from storage: {best_path}")
                                break
                        else:
                            logger.debug(f"[IDENTITY_LOADER] Person directory not found: {person_dir}")
                else:
                    logger.warning(f"[IDENTITY_LOADER] ⚠️ Storage directory does not exist: {storage_dir}")
            
            # Convert to relative path if found
            if best_path:
                logger.info(f"[IDENTITY_LOADER] Converting path to relative format: {best_path}")
                if os.path.isabs(best_path):
                    best_path_abs = os.path.abspath(best_path)
                    if best_path_abs.startswith(storage_dir_abs):
                        relative_path = os.path.relpath(best_path_abs, storage_dir_abs)
                        final_path = 'storage/' + relative_path.replace('\\', '/')
                        logger.info(f"[IDENTITY_LOADER] ✅ Converted absolute path to relative: {final_path}")
                        return final_path
                    else:
                        logger.warning(f"[IDENTITY_LOADER] ⚠️ Path outside storage directory: {best_path_abs}")
                else:
                    final_path = 'storage/' + best_path.lstrip('/') if not best_path.startswith('storage/') else best_path
                    logger.info(f"[IDENTITY_LOADER] ✅ Using relative path: {final_path}")
                    return final_path
            else:
                logger.warning(f"[IDENTITY_LOADER] ❌ No image found for '{person_name}' in storage")
            
            return None
            
        except Exception as e:
            logger.warning(f"[IDENTITY_LOADER] Failed to find best image from storage for {person_name}: {e}")
            return None
    
    async def verify_indexes(self, db: AsyncSession) -> dict:
        """
        Verify both KNOWN and UNKNOWN indexes are working correctly.
        
        Returns:
            Dictionary with verification results
        """
        results = {
            "known_index": {
                "faiss_count": 0,
                "pgvector_count": 0,
                "database_count": 0,
                "database_embedding_count": 0,
                "match": False,
                "issues": []
            },
            "unknown_index": {
                "faiss_count": 0,
                "pgvector_count": 0,
                "database_count": 0,
                "database_embedding_count": 0,
                "match": False,
                "issues": []
            },
            "assets_faces": {
                "directory_exists": False,
                "file_count": 0,
                "loaded_count": 0
            }
        }
        
        try:
            # Determine which backend is being used
            use_pgvector = (
                USE_PGVECTOR and 
                self.identity_service.use_pgvector and 
                self.identity_service.pgvector_index
            )
            
            if use_pgvector:
                logger.info("[IDENTITY_LOADER] [VERIFY] Using pgvector backend for verification")
                
                # Check KNOWN index (pgvector)
                from sqlalchemy import func
                result = await db.execute(
                    select(func.count(IdentityEmbedding.id))
                    .join(Identity, IdentityEmbedding.identity_id == Identity.id)
                    .where(
                        IdentityEmbedding.embedding.isnot(None),
                        Identity.type == IdentityType.KNOWN,
                        IdentityEmbedding.faiss_index_type == 'known'
                    )
                )
                results["known_index"]["pgvector_count"] = result.scalar() or 0
                
                # Count KNOWN identities in database
                result = await db.execute(
                    select(Identity).where(Identity.type == IdentityType.KNOWN)
                )
                known_identities = result.scalars().all()
                results["known_index"]["database_count"] = len(known_identities)
                
                # Count all embeddings (including those without vectors)
                result = await db.execute(
                    select(func.count(IdentityEmbedding.id))
                    .join(Identity, IdentityEmbedding.identity_id == Identity.id)
                    .where(
                        Identity.type == IdentityType.KNOWN,
                        IdentityEmbedding.faiss_index_type == 'known'
                    )
                )
                results["known_index"]["database_embedding_count"] = result.scalar() or 0
                
                # Check if pgvector embeddings match database identities
                if results["known_index"]["pgvector_count"] > 0:
                    results["known_index"]["match"] = True
                    logger.info(f"[IDENTITY_LOADER] [VERIFY] ✅ KNOWN: {results['known_index']['pgvector_count']} pgvector embeddings, {results['known_index']['database_count']} identities")
                else:
                    results["known_index"]["issues"].append(
                        f"No pgvector embeddings found for KNOWN identities"
                    )
                    logger.warning(f"[IDENTITY_LOADER] [VERIFY] ⚠️  KNOWN: No pgvector embeddings found")
                
                # Check UNKNOWN index (pgvector)
                result = await db.execute(
                    select(func.count(IdentityEmbedding.id))
                    .join(Identity, IdentityEmbedding.identity_id == Identity.id)
                    .where(
                        IdentityEmbedding.embedding.isnot(None),
                        Identity.type == IdentityType.UNKNOWN,
                        IdentityEmbedding.faiss_index_type == 'unknown'
                    )
                )
                results["unknown_index"]["pgvector_count"] = result.scalar() or 0
                
                result = await db.execute(
                    select(Identity).where(Identity.type == IdentityType.UNKNOWN)
                )
                unknown_identities = result.scalars().all()
                results["unknown_index"]["database_count"] = len(unknown_identities)
                
                result = await db.execute(
                    select(func.count(IdentityEmbedding.id))
                    .join(Identity, IdentityEmbedding.identity_id == Identity.id)
                    .where(
                        Identity.type == IdentityType.UNKNOWN,
                        IdentityEmbedding.faiss_index_type == 'unknown'
                    )
                )
                results["unknown_index"]["database_embedding_count"] = result.scalar() or 0
                
                if results["unknown_index"]["pgvector_count"] > 0:
                    results["unknown_index"]["match"] = True
                    logger.info(f"[IDENTITY_LOADER] [VERIFY] ✅ UNKNOWN: {results['unknown_index']['pgvector_count']} pgvector embeddings, {results['unknown_index']['database_count']} identities")
                else:
                    logger.info(f"[IDENTITY_LOADER] [VERIFY] ℹ️  UNKNOWN: {results['unknown_index']['pgvector_count']} pgvector embeddings (may be empty)")
                
            else:
                # FAISS backend verification
                logger.info("[IDENTITY_LOADER] [VERIFY] Using FAISS backend for verification")
                
                # Check KNOWN index
                if self.identity_service.identity_index:
                    results["known_index"]["faiss_count"] = self.identity_service.identity_index.known_index.ntotal if self.identity_service.identity_index.known_index else 0
                    
                    # Count KNOWN identities in database
                    result = await db.execute(
                        select(Identity).where(Identity.type == IdentityType.KNOWN)
                    )
                    known_identities = result.scalars().all()
                    results["known_index"]["database_count"] = len(known_identities)
                    
                    # Count embeddings in database
                    result = await db.execute(
                        select(IdentityEmbedding).where(IdentityEmbedding.faiss_index_type == 'known')
                    )
                    known_embeddings = result.scalars().all()
                    embedding_count = len(known_embeddings)
                    results["known_index"]["database_embedding_count"] = embedding_count
                    
                    # Check if counts match
                    if results["known_index"]["faiss_count"] == embedding_count:
                        results["known_index"]["match"] = True
                    else:
                        results["known_index"]["issues"].append(
                            f"FAISS count ({results['known_index']['faiss_count']}) != "
                            f"Database embedding count ({embedding_count})"
                        )
                    
                    # Check UNKNOWN index
                    results["unknown_index"]["faiss_count"] = self.identity_service.identity_index.unknown_index.ntotal if self.identity_service.identity_index.unknown_index else 0
                    
                    result = await db.execute(
                        select(Identity).where(Identity.type == IdentityType.UNKNOWN)
                    )
                    unknown_identities = result.scalars().all()
                    results["unknown_index"]["database_count"] = len(unknown_identities)
                    
                    result = await db.execute(
                        select(IdentityEmbedding).where(IdentityEmbedding.faiss_index_type == 'unknown')
                    )
                    unknown_embeddings = result.scalars().all()
                    unknown_embedding_count = len(unknown_embeddings)
                    results["unknown_index"]["database_embedding_count"] = unknown_embedding_count
                    
                    # Check if counts match
                    if results["unknown_index"]["faiss_count"] == unknown_embedding_count:
                        results["unknown_index"]["match"] = True
                    else:
                        results["unknown_index"]["issues"].append(
                            f"FAISS count ({results['unknown_index']['faiss_count']}) != "
                            f"Database embedding count ({unknown_embedding_count})"
                        )
            
            # Check storage/faces directory
            faces_dir = getattr(settings, 'FACES_DIR', './storage/faces')
            if os.path.exists(faces_dir):
                results["assets_faces"]["directory_exists"] = True
                image_files = [
                    f for f in os.listdir(faces_dir)
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))
                ]
                results["assets_faces"]["file_count"] = len(image_files)
                
                # Count how many are loaded in Identity system
                result = await db.execute(
                    select(Identity).where(Identity.type == IdentityType.KNOWN)
                )
                all_known = result.scalars().all()
                # Check which ones match files in assets/faces
                loaded_names = {id.display_name for id in all_known}
                file_names = {f.rsplit(".", 1)[0] for f in image_files}
                results["assets_faces"]["loaded_count"] = len(loaded_names & file_names)
            
        except Exception as e:
            logger.error(f"Error verifying indexes: {e}", exc_info=True)
            results["error"] = str(e)
        
        return results

