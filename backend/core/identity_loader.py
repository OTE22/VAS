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
VECTOR_BACKEND = settings.VECTOR_BACKEND.lower()
USE_PGVECTOR = VECTOR_BACKEND == 'pgvector'

from backend.core.vector_index.access import index_stats, request_snapshot
from backend.core.face_extraction import (FaceExtractionError, embed_face,
                                          extract_single_face)

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
        
        # ONE supported layout: FACES_DIR/<identity_uuid>/image_NNN.ext
        #
        # This loader used to accept flat files and display-name folders and to
        # derive display_name from whatever the folder was called, which meant a
        # restart re-created identities from disk — undoing any cleanup, and (once
        # folders became UUIDs) creating people literally named "a75c5b6d-...".
        # It now resolves an identity by ID only and NEVER creates one.
        import uuid as _uuid

        image_files = []
        skipped_entries = []

        for item in sorted(os.listdir(faces_dir)):
            item_path = os.path.join(faces_dir, item)
            if not os.path.isdir(item_path):
                if item.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                    skipped_entries.append((item, "loose file — not a <uuid>/ folder"))
                continue
            if item.startswith("."):
                continue          # .incoming staging area
            try:
                _uuid.UUID(item)
            except (ValueError, AttributeError, TypeError):
                skipped_entries.append((item, "folder name is not an identity UUID"))
                continue
            folder_images = [
                os.path.join(item, f) for f in sorted(os.listdir(item_path))
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))
            ]
            image_files.extend(folder_images)

        if skipped_entries:
            logger.warning(
                "[IDENTITY_LOADER] Skipped %d entr%s not in the <identity_uuid>/ "
                "layout (nothing was enrolled from them):",
                len(skipped_entries), "y" if len(skipped_entries) == 1 else "ies")
            for name, reason in skipped_entries[:20]:
                logger.warning("[IDENTITY_LOADER]   - %s: %s", name, reason)

        logger.info("Found %d image(s) under identity-UUID folders", len(image_files))
        
        if not image_files:
            logger.info(f"No image files found in {faces_dir}")
            return 0, 0, 0
        
        logger.info(f"Loading {len(image_files)} known faces from {faces_dir}...")
        
        # No index training: the contract ships an exact flat index, and only
        # IVF/IVFPQ ever needed a training pass. The old check dereferenced the
        # legacy service unconditionally, so it raised on every startup once
        # that service stopped being constructed.
        needs_training = False
        training_count = 0
        
        loaded_count = 0
        skipped_count = 0
        error_count = 0
        embeddings_for_training = []
        
        for filename in image_files:
            try:
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ========================================")
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] 📸 Processing image: {filename}")
                
                # The owning identity is the FOLDER UUID — never the folder text.
                folder_uuid = filename.split(os.sep)[0].split("/")[0]
                image_path = os.path.join(faces_dir, filename)
                owner = (await db.execute(
                    select(Identity).where(Identity.id == uuid.UUID(folder_uuid))
                )).scalar_one_or_none()
                if owner is None:
                    logger.warning(
                        "[IDENTITY_LOADER] Skipping %s — no identity %s exists. "
                        "This loader never creates identities from disk.",
                        filename, folder_uuid)
                    skipped_count += 1
                    continue
                person_name = owner.display_name
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] Identity: {owner.id} ({person_name})")
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] Image path: {image_path}")

                # Check if identity already exists
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] 🔍 Checking embeddings for identity {owner.id}...")
                if not force_reload:
                    existing = owner
                    if existing:
                        logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ✅ Identity '{person_name}' already exists (ID: {existing.id})")
                        identity_id_str = str(existing.id)
                        
                        # Check if identity has embeddings (FAISS or pgvector)
                        has_embeddings = False
                        backend_name = "unknown"
                        
                        if USE_PGVECTOR and self.identity_service.use_pgvector:
                            # Check for pgvector embeddings in database.
                            # (IdentityEmbedding is imported at module level. A
                            # local `from db_models import ...` here made the
                            # name function-local for the WHOLE function, so the
                            # summary block at the end raised UnboundLocalError
                            # whenever no image reached this line.)
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
                            # Same question, same answer source: the index is
                            # derived, so a stored vector is what counts.
                            emb_result = await db.execute(
                                select(IdentityEmbedding).where(
                                    IdentityEmbedding.identity_id == existing.id,
                                    IdentityEmbedding.embedding.isnot(None)
                                ).limit(1)
                            )
                            has_embeddings = emb_result.scalar_one_or_none() is not None
                            backend_name = "index"
                            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Checking stored embeddings: {'✅ found' if has_embeddings else '❌ not found'}")
                        
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
                                                pipeline_id=None,  # preloaded gallery: not a camera sighting
                                                quality_score=None,
                                                index_type='known',
                                                db=db,
                                                # Without this the row is written
                                                # with a NULL model version and can
                                                # never prove it matches the index.
                                                model_version=self.identity_service.embedding_model_version,
                                            )
                                        if emb_id:
                                            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding saved to pgvector: emb_id={emb_id}")
                                        else:
                                            logger.warning(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ❌ Failed to save embedding to pgvector")
                                    else:
                                        # Persist the vector, then index it.
                                        # save_embedding owns that ordering and
                                        # records the sync outcome; the old code
                                        # wrote a row with a faiss_id and NO
                                        # vector, so the database could not
                                        # rebuild the index it pointed into.
                                        logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Saving embedding...")
                                        emb_record = await self.identity_service.save_embedding(
                                            identity=existing,
                                            embedding=embedding,
                                            detection_id=None,
                                            pipeline_id=None,  # preloaded gallery: not a camera sighting
                                            quality_score=None,
                                            db=db
                                        )
                                        if emb_record is None:
                                            raise RuntimeError(
                                                f"save_embedding returned no record for {person_name}")
                                        logger.info(
                                            f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding saved: "
                                            f"id={emb_record.id} state={emb_record.vector_index_sync_state}")
                                    
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
                                logger.error(f"[IDENTITY_LOADER] ❌ Error adding embedding for {person_name}: {e}", exc_info=True)
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
        
        # Snapshot request after a bulk load. The manager may decline if a save
        # is already running — the vectors are already durable in PostgreSQL, so
        # a declined snapshot costs nothing but a later rebuild.
        if loaded_count > 0:
            snap = await request_snapshot(trigger="load_known_faces")
            logger.info(f"💾 Snapshot after loading {loaded_count} known faces: {snap}")
        
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
        else:
            stats = index_stats()
            size = int((stats or {}).get("count", 0) or 0)
            logger.info(f"[IDENTITY_LOADER] Final index state: {stats}")
            if size == 0:
                logger.error("[IDENTITY_LOADER] ❌ Index is EMPTY after loading - "
                             "no known faces are available for recognition")
            else:
                logger.info(f"[IDENTITY_LOADER] ✅ Index ready with {size} vectors")
        
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
            # Detect through the shared extractor, which retries on a padded
            # canvas. What this replaces synthesized five keypoints from image
            # geometry for any small squarish image and fed them to ArcFace —
            # and unlike the search paths, the resulting vector was WRITTEN TO
            # THE GALLERY. A stored embedding built from invented geometry is
            # not merely a bad row: every future query is compared against it,
            # so it can neither be matched by its own person nor be recognised
            # as wrong. A face whose real landmarks cannot be found is now
            # skipped and logged instead.
            #
            # on_multiple="best" is required, not preferred: this used to be
            # max_num=1, so a folder containing a group photo silently loaded
            # the largest face. Rejecting here would empty that identity's
            # gallery slot at startup.
            try:
                face = extract_single_face(image, on_multiple="best",
                                           manager=self.model_manager)
            except FaceExtractionError as exc:
                h, w = image.shape[:2]
                logger.warning(
                    "[IDENTITY_LOADER] [STEP-BY-STEP] ❌ Skipping %s (%dx%d): %s (%s). "
                    "No embedding was stored for this image.",
                    image_path, w, h, exc.message, exc.code)
                return False, None

            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ✅ SCRFD: Face detected successfully")
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Confidence: {face.score:.4f}"
                        f"{' (padded retry)' if face.padded_retry else ''}")

            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] 🔍 Step 3: Embedding Generation (ArcFace Model)...")
            # Generate embedding
            embedding = embed_face(image, face, manager=self.model_manager)
            
            embedding_norm = np.linalg.norm(embedding)
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ✅ ArcFace: Embedding generated successfully")
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Embedding shape: {embedding.shape}, norm: {embedding_norm:.6f}")
            
            # Reuse the identity that owns this folder. The loader resolved it
            # from the folder UUID above and skipped anything unmatched, so it
            # never creates an identity from disk — that behaviour is what used
            # to resurrect purged people (and, under UUID folders, would have
            # created people named after a UUID) on every restart.
            identity = owner
            identity_id = identity.id
            now = datetime.utcnow()
            if not identity.best_snapshot_path:
                best_snapshot_path = await self._find_best_image_from_storage(
                    person_name, db, identity_id=identity.id)
                if best_snapshot_path:
                    identity.best_snapshot_path = best_snapshot_path
                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Set best_snapshot_path: {best_snapshot_path}")
            identity.updated_at = now
            logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP] ✅ Using existing identity {identity_id} ('{person_name}')")
            
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
                    pipeline_id=None,  # preloaded gallery: not a camera sighting
                    quality_score=None,
                    index_type='known',
                    db=db,
                    # Same reason as the enrichment path above: a preloaded
                    # known face is a searchable vector like any other.
                    model_version=self.identity_service.embedding_model_version,
                )
                
                if emb_id:
                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding saved to pgvector: emb_id={emb_id}")
                    logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding stored in identity_embeddings table (vector type)")
                else:
                    logger.warning(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ❌ Failed to save embedding to pgvector")
            else:
                # In-process index backend: persist first, index second.
                logger.info(f"[IDENTITY_LOADER] [STEP-BY-STEP]    Saving embedding via the vector index contract...")
                embedding_record = await self.identity_service.save_embedding(
                    identity=identity,
                    embedding=embedding,
                    detection_id=None,       # no detection for pre-loaded faces
                    pipeline_id=None,  # preloaded gallery: not a camera sighting
                    quality_score=None,      # not calculated for pre-loaded faces
                    db=db
                )
                if embedding_record is None:
                    logger.error(f"[IDENTITY_LOADER] [STEP-BY-STEP]    ❌ save_embedding returned no record")
                    return False, None
                logger.info(
                    f"[IDENTITY_LOADER] [STEP-BY-STEP]    ✅ Embedding stored in "
                    f"identity_embeddings (id={embedding_record.id}, "
                    f"state={embedding_record.vector_index_sync_state})")
            
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
            
            # Same shared extractor as everywhere else: a padded retry for tight
            # crops, and a refusal rather than invented keypoints when no real
            # face is there. Returning None is the existing "skip this image"
            # signal for both callers.
            try:
                face = extract_single_face(image, on_multiple="best",
                                           manager=self.model_manager)
            except FaceExtractionError as exc:
                logger.warning("[IDENTITY_LOADER] No usable face in %s: %s (%s)",
                               image_path, exc.message, exc.code)
                return None

            return embed_face(image, face, manager=self.model_manager)
            
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
            
            storage_dir = settings.STORAGE_DIR
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
                if index_stats() is not None:
                    results["known_index"]["faiss_count"] = int((index_stats() or {}).get("count", 0) or 0)
                    
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
                    # One index now; the KNOWN/UNKNOWN split lives in the
                    # database, so both report the same total.
                    results["unknown_index"]["faiss_count"] = int((index_stats() or {}).get("count", 0) or 0)
                    
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
            
            # The old "assets_faces" block that stood here scanned FACES_DIR
            # for LOOSE image files and matched their basenames against display
            # names — both halves of a representation this system no longer
            # uses (the gallery is <identity_uuid>/ folders). It counted 0 on
            # every deployment while looking like a real check, so it was
            # removed rather than kept as reassuring noise.

        except Exception as e:
            logger.error(f"Error verifying indexes: {e}", exc_info=True)
            results["error"] = str(e)
        
        return results

