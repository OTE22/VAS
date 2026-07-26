"""
Identity Service
================
Service for managing identities (creation, promotion, merging).

Supports dual vector backends:
- FAISS: Fast in-memory vector search (default)
- pgvector: PostgreSQL-based vector search (simpler, ACID compliant)

Set VECTOR_BACKEND=pgvector in environment to use pgvector.
"""

import os
import sys
import shutil
import logging
import uuid
import cv2
import numpy as np
from datetime import datetime
from typing import Optional, Tuple, List, Union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_models import (
    Identity, IdentityAppearance, IdentityEmbedding, Face, Detection,
    IdentityType, IdentityStatus, LabelState
)
from backend.core.identity_index import IdentityIndexService

# Import pgvector support
try:
    from backend.core.identity_index_pgvector import IdentityIndexPgVector, identity_index_pgvector
    PGVECTOR_AVAILABLE = True
except ImportError:
    IdentityIndexPgVector = None
    identity_index_pgvector = None
    PGVECTOR_AVAILABLE = False

# Import settings
try:
    from config import settings
    VECTOR_BACKEND = getattr(settings, 'VECTOR_BACKEND', 'faiss').lower()
except ImportError:
    settings = None
    VECTOR_BACKEND = 'faiss'

USE_PGVECTOR = VECTOR_BACKEND == 'pgvector' and PGVECTOR_AVAILABLE

logger = logging.getLogger(__name__)
logger.info(f"[IDENTITY_SERVICE] Vector backend: {VECTOR_BACKEND} (pgvector_available={PGVECTOR_AVAILABLE}, use_pgvector={USE_PGVECTOR})")


class IdentityService:
    """
    Service for managing identities.
    
    Supports dual vector backends:
    - FAISS (default): Fast in-memory search via IdentityIndexService
    - pgvector: PostgreSQL-based search via IdentityIndexPgVector
    """
    
    def __init__(
        self, 
        identity_index: IdentityIndexService,
        pgvector_index: Optional[IdentityIndexPgVector] = None
    ):
        self.identity_index = identity_index  # FAISS backend (always initialized for compatibility)
        self.pgvector_index = pgvector_index or identity_index_pgvector  # pgvector backend
        self.use_pgvector = USE_PGVECTOR
        
        # Verify pgvector_index is available if using pgvector
        if self.use_pgvector and not self.pgvector_index:
            logger.error(f"[IDENTITY_SERVICE] ❌ CRITICAL: pgvector backend enabled but pgvector_index is None!")
            logger.error(f"[IDENTITY_SERVICE] This will cause the system to fall back to FAISS!")
            logger.error(f"[IDENTITY_SERVICE] Check that IdentityIndexPgVector is properly initialized.")
            # Don't fail, but log warning - will fall back to FAISS
        
        # Thresholds: known_threshold / unknown_threshold are PROPERTIES that
        # read the live settings object per call (see below) — previously they
        # were hardcoded 0.4/0.35 here, which made SIMILARITY_THRESHOLD a dead
        # knob: neither .env nor the admin settings page affected recognition.
        # Quality thresholds - separate for known vs unknown
        # Unknown uses lower threshold to save more embeddings for clustering/merge suggestions
        self.quality_threshold_known = getattr(settings, 'IDENTITY_QUALITY_THRESHOLD_KNOWN', 0.5)
        self.quality_threshold_unknown = getattr(settings, 'IDENTITY_QUALITY_THRESHOLD_UNKNOWN', 0.1)
        # Legacy threshold (for backward compatibility)
        self.quality_threshold = self.quality_threshold_known
        
        backend_status = 'pgvector' if (self.use_pgvector and self.pgvector_index) else 'faiss'
        logger.info(f"[IDENTITY_SERVICE] Initialized with backend: {backend_status}")
        logger.info(f"[IDENTITY_SERVICE] Thresholds (live): known={self.known_threshold} unknown={self.unknown_threshold}")
        if self.use_pgvector and self.pgvector_index:
            logger.info(f"[IDENTITY_SERVICE] ✅ pgvector_index is available and will be used")
        elif self.use_pgvector and not self.pgvector_index:
            logger.warning(f"[IDENTITY_SERVICE] ⚠️  pgvector enabled but pgvector_index is None - will use FAISS!")
        else:
            logger.info(f"[IDENTITY_SERVICE] Using FAISS backend (VECTOR_BACKEND=faiss)")

    @property
    def known_threshold(self) -> float:
        """Live read: SIMILARITY_THRESHOLD governs KNOWN matching (dynamic)."""
        return float(getattr(settings, 'SIMILARITY_THRESHOLD', 0.4))

    @property
    def unknown_threshold(self) -> float:
        """Live read: UNKNOWN_SIMILARITY_THRESHOLD governs UNKNOWN matching (dynamic)."""
        return float(getattr(settings, 'UNKNOWN_SIMILARITY_THRESHOLD', 0.35))

    async def find_or_create_identity(
        self,
        embedding: np.ndarray,
        pipeline_id: str,
        detection_id: Optional[int],
        db: AsyncSession,
        quality_score: Optional[float] = None
    ) -> Tuple[Identity, bool, float]:
        """
        Find existing identity or create new one based on embedding search.
        
        Supports dual backends:
        - FAISS: In-memory search (default)
        - pgvector: PostgreSQL search (if VECTOR_BACKEND=pgvector)
        
        Returns:
            Tuple of (Identity, is_new_identity, similarity_score)
        """
        backend_name = 'pgvector' if self.use_pgvector else 'faiss'
        logger.info(f"[IDENTITY_SEARCH] ===== Starting identity search (backend={backend_name}) for pipeline={pipeline_id}, detection_id={detection_id} =====")
        logger.debug(f"[IDENTITY_SEARCH] use_pgvector: {self.use_pgvector}, pgvector_index available: {self.pgvector_index is not None}")
        
        # Use pgvector backend if configured
        if self.use_pgvector and self.pgvector_index:
            logger.info(f"[IDENTITY_SEARCH] ✅ Using pgvector backend - calling _find_or_create_identity_pgvector()...")
            return await self._find_or_create_identity_pgvector(
                embedding, pipeline_id, detection_id, db, quality_score
            )
        else:
            logger.info(f"[IDENTITY_SEARCH] Using FAISS backend (use_pgvector={self.use_pgvector}, pgvector_index={self.pgvector_index is not None})")
        
        # FAISS backend (default)
        # Log index states
        known_index_size = self.identity_index.known_index.ntotal if self.identity_index.known_index else 0
        unknown_index_size = self.identity_index.unknown_index.ntotal if self.identity_index.unknown_index else 0
        logger.info(f"[IDENTITY_SEARCH] Index sizes - KNOWN: {known_index_size} vectors, UNKNOWN: {unknown_index_size} vectors")
        logger.info(f"[IDENTITY_SEARCH] Search thresholds - KNOWN: {self.known_threshold}, UNKNOWN: {self.unknown_threshold}")
        
        # Step 1: Search KNOWN index first
        logger.info(f"[IDENTITY_SEARCH] Step 1: Searching KNOWN index (threshold={self.known_threshold})...")
        
        # DEBUG: Log all known identities in FAISS before search
        known_identities_in_faiss = list(self.identity_index.known_identity_to_faiss.keys())
        logger.debug(f"[IDENTITY_SEARCH] DEBUG: KNOWN identities in FAISS: {len(known_identities_in_faiss)} identities")
        if len(known_identities_in_faiss) <= 10:
            for identity_id_str in known_identities_in_faiss:
                faiss_ids = self.identity_index.known_identity_to_faiss[identity_id_str]
                logger.debug(f"[IDENTITY_SEARCH] DEBUG: Identity {identity_id_str}: {len(faiss_ids)} embeddings in FAISS")
        
        known_matches = self.identity_index.search_known(
            embedding,
            top_k=5,  # Get top 5 for debugging
            threshold=self.known_threshold
        )
        
        logger.info(f"[IDENTITY_SEARCH] KNOWN index search returned {len(known_matches)} matches above threshold {self.known_threshold}")
        if known_matches:
            # Log all matches for debugging
            for idx, (match_id, match_sim) in enumerate(known_matches):
                logger.debug(f"[IDENTITY_SEARCH] Match {idx}: identity_id={match_id}, similarity={match_sim:.4f}")
        
        if known_matches:
            identity_id_str, similarity = known_matches[0]
            identity_id = uuid.UUID(identity_id_str)
            logger.info(f"[IDENTITY_SEARCH] ✅ KNOWN index match found! identity_id={identity_id_str}, similarity={similarity:.4f}")
            
            # Get identity from database
            result = await db.execute(
                select(Identity).where(Identity.id == identity_id)
            )
            identity = result.scalar_one_or_none()
            
            if identity:
                logger.info(f"[IDENTITY_SEARCH] Database lookup: identity_id={identity.id}, type={identity.type.value}, display_name={identity.display_name}, status={identity.status.value}")
                
                # CRITICAL: Double-check identity type in database
                if identity.type != IdentityType.KNOWN:
                    logger.error(f"[IDENTITY_SEARCH] ❌ [SECURITY] FAISS found identity {identity_id} in KNOWN index but database says type={identity.type.value}. Skipping.")
                    logger.error(f"[IDENTITY_SEARCH] This is a data inconsistency! Identity should be KNOWN but DB says {identity.type.value}")
                    logger.error(f"[IDENTITY_SEARCH] Identity details: id={identity.id}, display_name={identity.display_name}, status={identity.status.value}")
                elif identity.status != IdentityStatus.ACTIVE:
                    logger.warning(f"[IDENTITY_SEARCH] ⚠️ Identity {identity_id} found but status={identity.status.value} (not ACTIVE). Skipping.")
                else:
                    logger.info(f"[IDENTITY_SEARCH] ✅ SUCCESS: Found KNOWN identity: {identity.display_name} (sim={similarity:.4f})")
                    await self._update_identity_seen(identity, db)
                    logger.info(f"[IDENTITY_SEARCH] ===== Search complete: KNOWN identity found =====")
                    return identity, False, similarity
            else:
                logger.error(f"[IDENTITY_SEARCH] ❌ FAISS found identity_id={identity_id_str} in KNOWN index but NOT FOUND in database! This is a data corruption issue.")
                logger.error(f"[IDENTITY_SEARCH] DEBUG: Checking if identity exists with different UUID...")
                # Check if identity exists with same display_name
                name_result = await db.execute(
                    select(Identity).where(Identity.display_name == identity_id_str).limit(1)
                )
                name_match = name_result.scalar_one_or_none()
                if name_match:
                    logger.warning(f"[IDENTITY_SEARCH] Found identity with same name but different ID: {name_match.id} (FAISS has {identity_id_str})")
                
                # Auto-repair: Remove orphaned entry from FAISS metadata
                logger.warning(f"[IDENTITY_SEARCH] 🔧 Auto-repairing: Removing orphaned FAISS entry for identity_id={identity_id_str}")
                self.identity_index.remove_from_known(identity_id_str)
        else:
            logger.info(f"[IDENTITY_SEARCH] ❌ No match in KNOWN index (threshold={self.known_threshold})")
            logger.debug(f"[IDENTITY_SEARCH] KNOWN index search returned {len(known_matches)} matches")
            
            # DEBUG: Search with lower threshold to see what's there
            debug_matches = self.identity_index.search_known(embedding, top_k=5, threshold=0.2)
            if debug_matches:
                logger.debug(f"[IDENTITY_SEARCH] DEBUG: Found {len(debug_matches)} matches with lower threshold (0.2):")
                for match_id, match_sim in debug_matches:
                    logger.debug(f"[IDENTITY_SEARCH] DEBUG:   - identity_id={match_id}, similarity={match_sim:.4f} (below threshold {self.known_threshold})")
        
        # Step 2: Search UNKNOWN index
        logger.info(f"[IDENTITY_SEARCH] Step 2: Searching UNKNOWN index (threshold={self.unknown_threshold})...")
        unknown_matches = self.identity_index.search_unknown(
            embedding,
            top_k=1,
            threshold=self.unknown_threshold
        )
        
        if unknown_matches:
            identity_id_str, similarity = unknown_matches[0]
            identity_id = uuid.UUID(identity_id_str)
            logger.info(f"[IDENTITY_SEARCH] ✅ UNKNOWN index match found! identity_id={identity_id_str}, similarity={similarity:.4f}")
            
            # Get identity from database
            result = await db.execute(
                select(Identity).where(Identity.id == identity_id)
            )
            identity = result.scalar_one_or_none()
            
            if identity:
                logger.info(f"[IDENTITY_SEARCH] Database lookup: identity_id={identity.id}, type={identity.type.value}, display_name={identity.display_name}")
                
                # CRITICAL FIX: Before returning UNKNOWN, check if there's a KNOWN identity with similar embedding
                # This prevents known faces from appearing as both KNOWN and UNKNOWN
                logger.info(f"[IDENTITY_SEARCH] 🔍 Checking if this UNKNOWN identity should actually be KNOWN...")
                
                # Search KNOWN with lower threshold to catch cases where similarity is just below threshold
                known_matches_lower_threshold = self.identity_index.search_known(
                    embedding=embedding,
                    top_k=1,
                    threshold=max(0.2, self.known_threshold - 0.1)  # Lower threshold: 0.3 if known_threshold is 0.4
                )
                
                if known_matches_lower_threshold:
                    known_id_str, known_similarity = known_matches_lower_threshold[0]
                    known_id = uuid.UUID(known_id_str)
                    logger.warning(f"[IDENTITY_SEARCH] ⚠️ Found KNOWN identity {known_id_str} with similarity {known_similarity:.4f} (below threshold {self.known_threshold})")
                    logger.warning(f"[IDENTITY_SEARCH] ⚠️ This face should be KNOWN but similarity ({known_similarity:.4f}) is below threshold ({self.known_threshold})")
                    logger.warning(f"[IDENTITY_SEARCH] ⚠️ Returning KNOWN identity instead of UNKNOWN to prevent duplicates")
                    
                    # Get the KNOWN identity
                    known_result = await db.execute(
                        select(Identity).where(Identity.id == known_id)
                    )
                    known_identity = known_result.scalar_one_or_none()
                    
                    if known_identity and known_identity.type == IdentityType.KNOWN:
                        logger.info(f"[IDENTITY_SEARCH] ✅ Returning KNOWN identity: {known_identity.display_name} (sim={known_similarity:.4f})")
                        await self._update_identity_seen(known_identity, db)
                        return known_identity, False, known_similarity
                
                # CRITICAL: Double-check identity type - if it's KNOWN, this is a bug!
                if identity.type == IdentityType.KNOWN:
                    logger.error(f"[IDENTITY_SEARCH] ❌❌❌ [CRITICAL BUG] FAISS found identity {identity_id} in UNKNOWN index but database says type=KNOWN!")
                    logger.error(f"[IDENTITY_SEARCH] This means embeddings weren't moved during promotion!")
                    logger.error(f"[IDENTITY_SEARCH] Identity: {identity.display_name}, similarity={similarity:.4f}")
                    logger.error(f"[IDENTITY_SEARCH] This person should be recognized as KNOWN but is appearing as UNKNOWN!")
                    logger.warning(f"[IDENTITY] Attempting to repair: moving embeddings from UNKNOWN to KNOWN index...")
                    
                    # Repair: Move embeddings from UNKNOWN to KNOWN index
                    try:
                        # Get faiss_ids from UNKNOWN index
                        identity_id_str = str(identity_id)
                        faiss_ids_to_move = list(self.identity_index.unknown_identity_to_faiss.get(identity_id_str, []))
                        
                        # Reconstruct and move embeddings
                        for faiss_id in faiss_ids_to_move:
                            try:
                                if faiss_id < self.identity_index.unknown_index.ntotal:
                                    embedding_vector = self.identity_index.unknown_index.reconstruct(int(faiss_id))
                                    new_faiss_id = self.identity_index.add_known(identity_id_str, embedding_vector)
                                    logger.info(f"[IDENTITY] Repaired: moved embedding (old_faiss_id={faiss_id}, new_faiss_id={new_faiss_id})")
                            except Exception as e:
                                logger.error(f"[IDENTITY] Failed to repair embedding {faiss_id}: {e}")
                        
                        # Remove from UNKNOWN index
                        self.identity_index.remove_from_unknown(identity_id_str)
                        
                        # Update database records
                        result = await db.execute(
                            select(IdentityEmbedding).where(
                                IdentityEmbedding.identity_id == identity_id,
                                IdentityEmbedding.faiss_index_type == 'unknown'
                            )
                        )
                        for emb in result.scalars().all():
                            emb.faiss_index_type = 'known'
                            # faiss_id will be updated on next save_embedding call
                        
                        logger.info(f"[IDENTITY] Successfully repaired identity {identity_id} - moved {len(faiss_ids_to_move)} embeddings to KNOWN index")
                    except Exception as e:
                        logger.error(f"[IDENTITY] Failed to repair identity {identity_id}: {e}")
                    
                    # Still return the identity so detection works
                    await self._update_identity_seen(identity, db)
                    return identity, False, similarity
                
                logger.info(f"[IDENTITY] Found UNKNOWN identity: {identity.id} (sim={similarity:.3f})")
                await self._update_identity_seen(identity, db)
                return identity, False, similarity
        else:
            logger.debug(f"[IDENTITY] No match in UNKNOWN index (threshold={self.unknown_threshold})")
        
        # Step 3: Create new UNKNOWN identity
        logger.warning(f"[IDENTITY_SEARCH] ⚠️ No match found in either KNOWN or UNKNOWN indexes!")
        logger.warning(f"[IDENTITY_SEARCH] Creating new UNKNOWN identity (no match in KNOWN or UNKNOWN indexes)")
        logger.info(f"[IDENTITY_SEARCH] This could mean:")
        logger.info(f"[IDENTITY_SEARCH]   1. This is truly a new person")
        logger.info(f"[IDENTITY_SEARCH]   2. Known person's embeddings are missing from KNOWN index")
        logger.info(f"[IDENTITY_SEARCH]   3. Similarity threshold too high (KNOWN={self.known_threshold}, UNKNOWN={self.unknown_threshold})")
        identity = await self._create_unknown_identity(embedding, pipeline_id, detection_id, db, quality_score)
        logger.info(f"[IDENTITY_SEARCH] ===== Search complete: NEW UNKNOWN identity created: {identity.id} =====")
        return identity, True, 0.0
    
    async def _create_unknown_identity(
        self,
        embedding: np.ndarray,
        pipeline_id: str,
        detection_id: Optional[int],
        db: AsyncSession,
        quality_score: Optional[float] = None
    ) -> Identity:
        """Create a new unknown identity"""
        identity_id = uuid.uuid4()
        now = datetime.utcnow()
        
        # Create identity record
        identity = Identity(
            id=identity_id,
            type=IdentityType.UNKNOWN,
            status=IdentityStatus.ACTIVE,
            display_name=None,
            first_seen_at=now,
            last_seen_at=now,
            appearances_count=0,
            created_at=now,
            updated_at=now
        )
        db.add(identity)
        await db.flush()
        
        # Add embedding to appropriate backend
        # Use lower threshold for unknown identities to enable merge suggestions/clustering
        # Even low-quality embeddings can be useful for finding connections
        quality_threshold = self.quality_threshold_unknown
        if quality_score is None or quality_score >= quality_threshold:
            if self.use_pgvector and self.pgvector_index:
                # pgvector backend - store embedding directly in PostgreSQL
                emb_id = await self.pgvector_index.add_embedding(
                    identity_id=str(identity_id),
                    embedding=embedding,
                    detection_id=detection_id,
                    pipeline_id=pipeline_id,
                    quality_score=quality_score,
                    index_type='unknown',
                    db=db
                )
                logger.debug(f"[IDENTITY] Added embedding to pgvector (emb_id={emb_id})")
            else:
                # FAISS backend - add to in-memory index
                faiss_id = self.identity_index.add_unknown(str(identity_id), embedding)
                
                # Save embedding record
                embedding_record = IdentityEmbedding(
                    identity_id=identity_id,
                    detection_id=detection_id,
                    pipeline_id=pipeline_id,
                    faiss_id=faiss_id,
                    faiss_index_type='unknown',
                    quality=quality_score,
                    created_at=now
                )
                db.add(embedding_record)
                logger.debug(f"[IDENTITY] Added embedding to FAISS (faiss_id={faiss_id})")
        else:
            logger.warning(f"[IDENTITY] ⚠️ Skipping embedding save for UNKNOWN identity (quality {quality_score:.2f} < threshold {quality_threshold:.2f})")
            logger.warning(f"[IDENTITY] This identity will NOT appear in merge suggestions/clustering!")
            logger.warning(f"[IDENTITY] Consider lowering IDENTITY_QUALITY_THRESHOLD_UNKNOWN if you need merge suggestions for low-quality faces")
        
        await db.flush()
        logger.info(f"[IDENTITY] Created UNKNOWN identity: {identity_id}")
        return identity
    
    async def _find_or_create_identity_pgvector(
        self,
        embedding: np.ndarray,
        pipeline_id: str,
        detection_id: Optional[int],
        db: AsyncSession,
        quality_score: Optional[float] = None
    ) -> Tuple[Identity, bool, float]:
        """
        Find or create identity using pgvector backend.
        All searches use PostgreSQL's vector similarity operators.
        
        Returns:
            Tuple of (Identity, is_new_identity, similarity_score)
        """
        logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] Starting search: pipeline={pipeline_id}, detection_id={detection_id}")
        logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] Thresholds - KNOWN: {self.known_threshold}, UNKNOWN: {self.unknown_threshold}")
        
        # Step 1: Search KNOWN identities in PostgreSQL
        logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] Step 1: Searching KNOWN identities...")
        known_matches = await self.pgvector_index.search_known(
            embedding=embedding,
            db=db,
            top_k=5,
            threshold=self.known_threshold
        )
        
        logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] KNOWN search returned {len(known_matches)} matches")
        
        if known_matches:
            identity_id_str, similarity = known_matches[0]
            identity_id = uuid.UUID(identity_id_str)
            logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] ✅ KNOWN match found! identity_id={identity_id_str[:8]}..., similarity={similarity:.4f}")
            
            # Get identity from database
            result = await db.execute(
                select(Identity).where(Identity.id == identity_id)
            )
            identity = result.scalar_one_or_none()
            
            if identity:
                logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] Database lookup: identity_id={identity.id}, type={identity.type.value}, display_name={identity.display_name}")
                
                if identity.type != IdentityType.KNOWN:
                    logger.error(f"[IDENTITY_SEARCH] [PGVECTOR] ❌ Data inconsistency: Found in KNOWN search but type={identity.type.value}")
                elif identity.status not in (IdentityStatus.ACTIVE, IdentityStatus.PROMOTED):
                    # PROMOTED identities (unknown -> known via the UI) are valid known
                    # people; rejecting them silently broke the promote workflow.
                    logger.warning(f"[IDENTITY_SEARCH] [PGVECTOR] ⚠️ Identity found but status={identity.status.value}")
                else:
                    logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] ✅ SUCCESS: Found KNOWN identity: {identity.display_name} (sim={similarity:.4f})")
                    await self._update_identity_seen(identity, db)
                    # Auto-enrichment: let the identity learn this new view so future
                    # matches from this angle/lighting score higher.
                    await self._maybe_enrich_identity(
                        identity, embedding, similarity, quality_score,
                        detection_id, pipeline_id, db
                    )
                    return identity, False, similarity
            else:
                logger.error(f"[IDENTITY_SEARCH] [PGVECTOR] ❌ Identity {identity_id_str[:8]}... found in search but NOT in database!")
        else:
            logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] No match in KNOWN identities (threshold={self.known_threshold})")
        
        # Step 2: Search UNKNOWN identities
        logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] Step 2: Searching UNKNOWN identities...")
        unknown_matches = await self.pgvector_index.search_unknown(
            embedding=embedding,
            db=db,
            top_k=1,
            threshold=self.unknown_threshold
        )
        
        if unknown_matches:
            identity_id_str, similarity = unknown_matches[0]
            identity_id = uuid.UUID(identity_id_str)
            logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] ✅ UNKNOWN match found! identity_id={identity_id_str[:8]}..., similarity={similarity:.4f}")
            
            result = await db.execute(
                select(Identity).where(Identity.id == identity_id)
            )
            identity = result.scalar_one_or_none()
            
            if identity:
                logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] Found UNKNOWN identity: {identity.id}")
                
                # CRITICAL FIX: Before returning UNKNOWN, check if there's a KNOWN identity with similar embedding
                # This prevents known faces from appearing as both KNOWN and UNKNOWN
                logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] 🔍 Checking if this UNKNOWN identity should actually be KNOWN...")
                
                # Search KNOWN with lower threshold to catch cases where similarity is just below threshold
                known_matches_lower_threshold = await self.pgvector_index.search_known(
                    embedding=embedding,
                    db=db,
                    top_k=1,
                    threshold=max(0.2, self.known_threshold - 0.1)  # Lower threshold: 0.3 if known_threshold is 0.4
                )
                
                if known_matches_lower_threshold:
                    known_id_str, known_similarity = known_matches_lower_threshold[0]
                    known_id = uuid.UUID(known_id_str)
                    logger.warning(f"[IDENTITY_SEARCH] [PGVECTOR] ⚠️ Found KNOWN identity {known_id_str[:8]}... with similarity {known_similarity:.4f} (below threshold {self.known_threshold})")
                    logger.warning(f"[IDENTITY_SEARCH] [PGVECTOR] ⚠️ This face should be KNOWN but similarity ({known_similarity:.4f}) is below threshold ({self.known_threshold})")
                    logger.warning(f"[IDENTITY_SEARCH] [PGVECTOR] ⚠️ Returning KNOWN identity instead of UNKNOWN to prevent duplicates")
                    
                    # Get the KNOWN identity
                    known_result = await db.execute(
                        select(Identity).where(Identity.id == known_id)
                    )
                    known_identity = known_result.scalar_one_or_none()
                    
                    if known_identity and known_identity.type == IdentityType.KNOWN:
                        logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] ✅ Returning KNOWN identity: {known_identity.display_name} (sim={known_similarity:.4f})")
                        await self._update_identity_seen(known_identity, db)
                        return known_identity, False, known_similarity
                
                # Check for type mismatch (shouldn't happen with pgvector since query filters by type)
                if identity.type == IdentityType.KNOWN:
                    logger.warning(f"[IDENTITY_SEARCH] [PGVECTOR] ⚠️ Identity {identity_id} is KNOWN but found in UNKNOWN search - returning anyway")
                
                await self._update_identity_seen(identity, db)
                return identity, False, similarity
        else:
            logger.debug(f"[IDENTITY_SEARCH] [PGVECTOR] No match in UNKNOWN identities (threshold={self.unknown_threshold})")
        
        # Step 3: Create new UNKNOWN identity
        logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] No matches found - creating new UNKNOWN identity")
        identity = await self._create_unknown_identity(embedding, pipeline_id, detection_id, db, quality_score)
        logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] ===== Search complete: NEW UNKNOWN identity created: {identity.id} =====")
        return identity, True, 0.0
    
    async def _update_identity_seen(self, identity: Identity, db: AsyncSession):
        """Update identity's last_seen_at timestamp"""
        identity.last_seen_at = datetime.utcnow()
        await db.flush()

    async def _maybe_enrich_identity(
        self,
        identity: Identity,
        embedding: np.ndarray,
        similarity: float,
        quality_score: Optional[float],
        detection_id: Optional[int],
        pipeline_id: str,
        db: AsyncSession
    ):
        """Auto-enrichment: add a confidently-matched runtime embedding to the identity.

        Enrolled identities historically held a SINGLE view, so any pose/lighting
        deviation dropped below the match threshold. By adding high-confidence,
        sufficiently-different embeddings over time, each identity learns the
        person's appearance range. Guards: min similarity, min quality, near-duplicate
        skip, and a hard cap per identity. Never raises - enrichment is best-effort.
        """
        try:
            min_sim = float(getattr(settings, 'IDENTITY_ENRICH_MIN_SIMILARITY', 0.55))
            min_quality = float(getattr(settings, 'IDENTITY_ENRICH_MIN_QUALITY', 0.5))
            max_embeddings = int(getattr(settings, 'IDENTITY_MAX_EMBEDDINGS', 20))

            if similarity < min_sim:
                return
            if quality_score is not None and quality_score < min_quality:
                logger.debug(f"[ENRICH] Skip {identity.display_name}: quality {quality_score:.2f} < {min_quality}")
                return

            identity_id_str = str(identity.id)

            count = await self.pgvector_index.count_identity_embeddings(identity_id_str, db)
            if count >= max_embeddings:
                logger.debug(f"[ENRICH] Skip {identity.display_name}: already has {count} embeddings (cap {max_embeddings})")
                return

            max_sim = await self.pgvector_index.max_similarity_to_identity(identity_id_str, embedding, db)
            if max_sim is not None and max_sim >= 0.95:
                logger.debug(f"[ENRICH] Skip {identity.display_name}: near-duplicate of existing view (sim {max_sim:.3f})")
                return

            emb_id = await self.pgvector_index.add_embedding(
                identity_id=identity_id_str,
                embedding=embedding,
                detection_id=detection_id,
                pipeline_id=pipeline_id,
                quality_score=quality_score,
                index_type='known',
                db=db
            )
            if emb_id:
                logger.info(
                    f"[ENRICH] ✅ Identity '{identity.display_name}' learned a new view "
                    f"(sim={similarity:.3f}, novelty={1 - (max_sim or 0):.3f}, embeddings={count + 1})"
                )
        except Exception as e:
            logger.warning(f"[ENRICH] Enrichment failed for {identity.display_name}: {e}")
    
    async def create_appearance(
        self,
        identity: Identity,
        pipeline_id: str,
        track_id: Optional[str],
        start_time: datetime,
        best_snapshot_path: Optional[str],
        db: AsyncSession,
        quality_score: Optional[float] = None,
        similarity: float = 0.0
    ) -> IdentityAppearance:
        """
        Create or update an identity appearance.
        Updates best_snapshot_path if the new image has better quality or similarity.
        """
        # Check if there's an active appearance for this track
        if track_id:
            result = await db.execute(
                select(IdentityAppearance).where(
                    IdentityAppearance.identity_id == identity.id,
                    IdentityAppearance.pipeline_id == pipeline_id,
                    IdentityAppearance.track_id == track_id,
                    IdentityAppearance.end_time.is_(None)
                )
            )
            existing = result.scalar_one_or_none()
            
            if existing:
                # Update existing appearance
                existing.end_time = datetime.utcnow()
                # Update best snapshot if new one is better
                if best_snapshot_path:
                    should_update = False
                    if not existing.best_snapshot_path:
                        should_update = True
                    elif quality_score is not None:
                        # Get best quality embedding for this identity (simpler approach)
                        best_quality_result = await db.execute(
                            select(func.max(IdentityEmbedding.quality)).where(
                                IdentityEmbedding.identity_id == identity.id,
                                IdentityEmbedding.quality.isnot(None)
                            )
                        )
                        best_quality = best_quality_result.scalar_one_or_none()
                        
                        # Compare quality scores - update if new quality is better
                        if best_quality is None or quality_score > best_quality:
                            should_update = True
                            logger.debug(f"[IDENTITY] Updating appearance best snapshot: new quality {quality_score:.3f} > best {best_quality:.3f if best_quality else 'N/A'}")
                    elif similarity > 0.0:
                        # If no quality scores, prefer higher similarity
                        should_update = True
                        logger.debug(f"[IDENTITY] Updating appearance best snapshot: similarity {similarity:.3f}")
                    
                    if should_update:
                        existing.best_snapshot_path = best_snapshot_path
                await db.flush()
                return existing
        
        # Create new appearance
        appearance = IdentityAppearance(
            identity_id=identity.id,
            pipeline_id=pipeline_id,
            track_id=track_id,
            start_time=start_time,
            end_time=None,
            best_snapshot_path=best_snapshot_path,
            created_at=datetime.utcnow()
        )
        db.add(appearance)
        
        # Update identity cache
        identity.appearances_count += 1
        
        # Update identity's best_snapshot_path if new one is better
        if best_snapshot_path:
            logger.info(f"[IDENTITY] 📸 Checking if best_snapshot_path should be updated for {identity.display_name} (ID: {identity.id})")
            logger.info(f"[IDENTITY]   Current best_snapshot_path: {identity.best_snapshot_path}")
            logger.info(f"[IDENTITY]   New best_snapshot_path: {best_snapshot_path}")
            
            should_update_identity = False
            if not identity.best_snapshot_path:
                should_update_identity = True
                logger.info(f"[IDENTITY]   ✅ No existing snapshot - will set new one")
            elif quality_score is not None:
                # Get best quality embedding for this identity (simpler approach)
                best_quality_result = await db.execute(
                    select(func.max(IdentityEmbedding.quality)).where(
                        IdentityEmbedding.identity_id == identity.id,
                        IdentityEmbedding.quality.isnot(None)
                    )
                )
                best_quality = best_quality_result.scalar_one_or_none()
                
                # Compare quality scores - update if new quality is better
                if best_quality is None or quality_score > best_quality:
                    should_update_identity = True
                    logger.info(f"[IDENTITY]   ✅ Updating identity best snapshot: new quality {quality_score:.3f} > best {best_quality:.3f if best_quality else 'N/A'}")
                else:
                    logger.info(f"[IDENTITY]   ⏭️ Keeping existing snapshot: new quality {quality_score:.3f} <= best {best_quality:.3f}")
            elif similarity > 0.0:
                # If no quality scores, prefer higher similarity
                should_update_identity = True
                logger.info(f"[IDENTITY]   ✅ Updating identity best snapshot: similarity {similarity:.3f}")
            
            if should_update_identity:
                identity.best_snapshot_path = best_snapshot_path
                logger.info(f"[IDENTITY]   💾 Updated identity.best_snapshot_path to: {best_snapshot_path}")
                logger.info(f"[IDENTITY]   🌐 This path will be used for display in identity cards")
            else:
                logger.info(f"[IDENTITY]   ⏭️ Keeping existing best_snapshot_path (not updating)")
        else:
            # best_snapshot_path is None - images will be loaded directly from storage when needed
            logger.debug(f"[IDENTITY]   ℹ️ best_snapshot_path is None for {identity.display_name} - images will be loaded from storage/pipeline_id/person_name/ when displayed")
        
        await db.flush()
        return appearance
    
    async def save_embedding(
        self,
        identity: Identity,
        embedding: np.ndarray,
        detection_id: Optional[int],
        pipeline_id: str,
        quality_score: Optional[float],
        db: AsyncSession
    ) -> Optional[IdentityEmbedding]:
        """
        Save embedding to database and vector index (FAISS or pgvector).
        
        Args:
            identity: Identity to associate embedding with
            embedding: Face embedding vector (512-dim)
            detection_id: Optional detection ID for traceability
            pipeline_id: Pipeline where face was detected
            quality_score: Quality score of the face
            db: Database session
            
        Returns:
            IdentityEmbedding record if saved, None if skipped due to quality
        """
        # Use appropriate quality threshold based on identity type
        quality_threshold = self.quality_threshold_known if identity.type == IdentityType.KNOWN else self.quality_threshold_unknown
        
        if quality_score is not None and quality_score < quality_threshold:
            logger.debug(f"[IDENTITY] Skipping embedding save (quality {quality_score:.2f} < threshold {quality_threshold:.2f} for {identity.type.value})")
            return None
        
        # Determine which index to use
        index_type = 'known' if identity.type == IdentityType.KNOWN else 'unknown'
        
        if self.use_pgvector and self.pgvector_index:
            # pgvector backend - store embedding directly in PostgreSQL
            logger.debug(f"[IDENTITY] [PGVECTOR] Saving embedding: identity={str(identity.id)[:8]}..., type={index_type}, quality={quality_score}")
            
            emb_id = await self.pgvector_index.add_embedding(
                identity_id=str(identity.id),
                embedding=embedding,
                detection_id=detection_id,
                pipeline_id=pipeline_id,
                quality_score=quality_score,
                index_type=index_type,
                db=db
            )
            
            if emb_id:
                # Get the created embedding record
                result = await db.execute(
                    select(IdentityEmbedding).where(IdentityEmbedding.id == emb_id)
                )
                embedding_record = result.scalar_one_or_none()
                logger.debug(f"[IDENTITY] [PGVECTOR] Saved embedding: id={emb_id}")
                return embedding_record
            else:
                logger.warning(f"[IDENTITY] [PGVECTOR] Failed to save embedding for identity {identity.id}")
                return None
        else:
            # FAISS backend - add to in-memory index
            logger.debug(f"[IDENTITY] [FAISS] Saving embedding: identity={str(identity.id)[:8]}..., type={index_type}, quality={quality_score}")
            
            if index_type == 'known':
                faiss_id = self.identity_index.add_known(str(identity.id), embedding)
            else:
                faiss_id = self.identity_index.add_unknown(str(identity.id), embedding)
            
            # Save embedding record
            embedding_record = IdentityEmbedding(
                identity_id=identity.id,
                detection_id=detection_id,
                pipeline_id=pipeline_id,
                faiss_id=faiss_id,
                faiss_index_type=index_type,
                quality=quality_score,
                created_at=datetime.utcnow()
            )
            db.add(embedding_record)
            await db.flush()
            
            logger.debug(f"[IDENTITY] [FAISS] Saved embedding: faiss_id={faiss_id}")
            return embedding_record
    
    async def promote_unknown_to_known(
        self,
        identity_id: uuid.UUID,
        display_name: str,
        user_id: int,
        db: AsyncSession
    ) -> Identity:
        """
        Promote an unknown identity to known.
        
        With FAISS: Moves embeddings from UNKNOWN to KNOWN index (complex).
        With pgvector: Just updates the database record (simple!).
        """
        # Get identity
        result = await db.execute(
            select(Identity).where(Identity.id == identity_id)
        )
        identity = result.scalar_one_or_none()
        
        if not identity:
            raise ValueError(f"Identity {identity_id} not found")
        
        if identity.type != IdentityType.UNKNOWN:
            raise ValueError(f"Identity {identity_id} is not unknown (type: {identity.type})")
        
        backend_name = 'pgvector' if self.use_pgvector else 'faiss'
        logger.info(f"[IDENTITY_PROMOTE] ===== Starting promotion (backend={backend_name}): identity_id={identity_id}, display_name={display_name} =====")
        logger.info(f"[IDENTITY_PROMOTE] Current state: type={identity.type.value}, status={identity.status.value}")
        
        # =====================================================
        # PGVECTOR PATH - Simple database update!
        # =====================================================
        if self.use_pgvector:
            return await self._promote_with_pgvector(identity, identity_id, display_name, user_id, db)
        
        # =====================================================
        # FAISS PATH - Complex index manipulation
        # =====================================================
        # Log current index states
        known_index_size = self.identity_index.known_index.ntotal if self.identity_index.known_index else 0
        unknown_index_size = self.identity_index.unknown_index.ntotal if self.identity_index.unknown_index else 0
        logger.info(f"[IDENTITY_PROMOTE] [FAISS] Index sizes BEFORE promotion - KNOWN: {known_index_size}, UNKNOWN: {unknown_index_size}")
        
        # Check how many embeddings this identity has
        identity_id_str = str(identity_id)
        unknown_faiss_ids = list(self.identity_index.unknown_identity_to_faiss.get(identity_id_str, []))
        logger.info(f"[IDENTITY_PROMOTE] [FAISS] Identity has {len(unknown_faiss_ids)} embeddings in UNKNOWN index: {unknown_faiss_ids}")
        
        # Update identity
        identity.type = IdentityType.KNOWN
        identity.status = IdentityStatus.PROMOTED
        identity.display_name = display_name
        identity.updated_at = datetime.utcnow()
        
        # Get all embeddings for this identity
        result = await db.execute(
            select(IdentityEmbedding).where(
                IdentityEmbedding.identity_id == identity_id,
                IdentityEmbedding.faiss_index_type == 'unknown'
            )
        )
        embeddings = result.scalars().all()
        
        # CRITICAL FIX: Actually move embeddings to KNOWN index immediately
        # Don't wait for next detection - this causes known faces to be recreated as unknown!
        # Step 1: Get faiss_ids BEFORE removing from UNKNOWN index
        identity_id_str = str(identity_id)
        faiss_ids_to_move = list(self.identity_index.unknown_identity_to_faiss.get(identity_id_str, []))
        
        logger.info(f"[IDENTITY] Found {len(faiss_ids_to_move)} embeddings in UNKNOWN index to move")
        
        # Step 2: Reconstruct embeddings from UNKNOWN FAISS index BEFORE removing
        embeddings_to_add = []
        for faiss_id in faiss_ids_to_move:
            try:
                # Reconstruct embedding from UNKNOWN index
                if faiss_id < self.identity_index.unknown_index.ntotal:
                    embedding_vector = self.identity_index.unknown_index.reconstruct(int(faiss_id))
                    # Get pipeline_id from existing embedding record
                    emb_pipeline_id = None
                    for emb in embeddings:
                        if emb.faiss_id == faiss_id and emb.pipeline_id:
                            emb_pipeline_id = emb.pipeline_id
                            break
                    # Format: (old_faiss_id, embedding_vector, pipeline_id, image_path)
                    # For FAISS embeddings, image_path is None (will be set from best_snapshot_path)
                    embeddings_to_add.append((faiss_id, embedding_vector, emb_pipeline_id, None))
                    logger.debug(f"[IDENTITY] Reconstructed embedding from UNKNOWN index (faiss_id={faiss_id}, pipeline: {emb_pipeline_id})")
                else:
                    logger.warning(f"[IDENTITY] FAISS ID {faiss_id} out of range (total: {self.identity_index.unknown_index.ntotal})")
            except Exception as e:
                logger.warning(f"[IDENTITY] Failed to reconstruct embedding {faiss_id}: {e}")
        
        # Step 2.5: If no embeddings found in FAISS, try to extract from stored images
        # Also track pipeline_id and best image for moving to storage/faces
        source_pipeline_id = None
        best_image_path = None
        image_paths_with_pipeline = []  # List of (image_path, pipeline_id) tuples
        
        if len(embeddings_to_add) == 0:
            logger.warning(f"[IDENTITY_PROMOTE] ⚠️ No embeddings found in UNKNOWN FAISS index. Attempting to extract from stored images...")
            
            # Try to get ModelManager for extracting embeddings
            try:
                from backend.core.model_manager import model_manager
                
                # 1. Check best_snapshot_path
                if identity.best_snapshot_path and os.path.exists(identity.best_snapshot_path):
                    # Try to extract pipeline_id from path: storage/pipeline_id/person_name/image.jpg
                    path_parts = identity.best_snapshot_path.replace("\\", "/").split("/")
                    if "storage" in path_parts:
                        storage_idx = path_parts.index("storage")
                        if len(path_parts) > storage_idx + 1:
                            pipeline_from_path = path_parts[storage_idx + 1]
                            image_paths_with_pipeline.append((identity.best_snapshot_path, pipeline_from_path))
                            logger.info(f"[IDENTITY_PROMOTE] Found best_snapshot_path: {identity.best_snapshot_path} (pipeline: {pipeline_from_path})")
                
                # 2. Check IdentityAppearance records for images
                appearances_result = await db.execute(
                    select(IdentityAppearance).where(
                        IdentityAppearance.identity_id == identity_id
                    ).order_by(IdentityAppearance.start_time.desc()).limit(5)
                )
                appearances = appearances_result.scalars().all()
                for appearance in appearances:
                    if appearance.best_snapshot_path and os.path.exists(appearance.best_snapshot_path):
                        # Use pipeline_id from appearance
                        pipeline_id = appearance.pipeline_id or "unknown"
                        # Check if not already added
                        if not any(path == appearance.best_snapshot_path for path, _ in image_paths_with_pipeline):
                            image_paths_with_pipeline.append((appearance.best_snapshot_path, pipeline_id))
                            logger.info(f"[IDENTITY_PROMOTE] Found appearance image: {appearance.best_snapshot_path} (pipeline: {pipeline_id})")
                
                # 3. Try to find images in storage/pipeline_id/person_name/ structure
                if not image_paths_with_pipeline:
                    # Search for images in storage directories
                    from config import STORAGE_DIR
                    import glob
                    # Look for any images associated with this identity's appearances
                    for appearance in appearances:
                        if appearance.pipeline_id:
                            search_pattern = os.path.join(STORAGE_DIR, appearance.pipeline_id, "*", "*.jpg")
                            found_images = glob.glob(search_pattern)
                            for img_path in found_images[:3]:  # Limit to 3 images
                                if not any(path == img_path for path, _ in image_paths_with_pipeline) and os.path.exists(img_path):
                                    image_paths_with_pipeline.append((img_path, appearance.pipeline_id))
                                    logger.info(f"[IDENTITY_PROMOTE] Found storage image: {img_path} (pipeline: {appearance.pipeline_id})")
                
                # Extract embeddings from found images and track best image
                for image_path, pipeline_id in image_paths_with_pipeline:
                    try:
                        image = cv2.imread(image_path)
                        if image is None:
                            logger.warning(f"[IDENTITY_PROMOTE] Could not read image: {image_path}")
                            continue
                        
                        # Detect face
                        bboxes, kpss = model_manager.detector.detect(image, max_num=1)
                        if kpss is None or len(kpss) == 0:
                            logger.warning(f"[IDENTITY_PROMOTE] No face detected in: {image_path}")
                            continue
                        
                        # Generate embedding
                        embedding = model_manager.recognizer.get_embedding(image, kpss[0])
                        if embedding is not None:
                            # Normalize embedding
                            embedding = embedding / np.linalg.norm(embedding)
                            embeddings_to_add.append((None, embedding, pipeline_id, image_path))  # Track pipeline and path
                            logger.info(f"[IDENTITY_PROMOTE] ✅ Extracted embedding from image: {image_path} (pipeline: {pipeline_id})")
                            
                            # Track best image (first successful extraction, or best quality)
                            if best_image_path is None:
                                best_image_path = image_path
                                source_pipeline_id = pipeline_id
                    except Exception as e:
                        logger.warning(f"[IDENTITY_PROMOTE] Failed to extract embedding from {image_path}: {e}")
                        continue
                
                if len(embeddings_to_add) > 0:
                    logger.info(f"[IDENTITY_PROMOTE] ✅ Successfully extracted {len(embeddings_to_add)} embeddings from stored images")
                else:
                    logger.error(f"[IDENTITY_PROMOTE] ❌ Could not extract embeddings from any stored images")
            except ImportError:
                logger.error(f"[IDENTITY_PROMOTE] ModelManager not available - cannot extract embeddings from images")
            except Exception as e:
                logger.error(f"[IDENTITY_PROMOTE] Error extracting embeddings from images: {e}", exc_info=True)
        else:
            # If we have FAISS embeddings, try to get pipeline_id from existing embeddings
            if embeddings:
                # Get pipeline_id from first embedding record
                first_emb = embeddings[0]
                if first_emb.pipeline_id and first_emb.pipeline_id != "promoted":
                    source_pipeline_id = first_emb.pipeline_id
                    logger.info(f"[IDENTITY_PROMOTE] Using pipeline_id from existing embedding: {source_pipeline_id}")
            
            # Also try to get from appearances
            if not source_pipeline_id:
                appearances_result = await db.execute(
                    select(IdentityAppearance).where(
                        IdentityAppearance.identity_id == identity_id
                    ).order_by(IdentityAppearance.start_time.desc()).limit(1)
                )
                appearance = appearances_result.scalar_one_or_none()
                if appearance and appearance.pipeline_id:
                    source_pipeline_id = appearance.pipeline_id
                    logger.info(f"[IDENTITY_PROMOTE] Using pipeline_id from appearance: {source_pipeline_id}")
            
            # Try to get best image path
            if identity.best_snapshot_path and os.path.exists(identity.best_snapshot_path):
                best_image_path = identity.best_snapshot_path
        
        # Step 3: Remove from UNKNOWN index metadata (only if we had FAISS embeddings)
        if len(faiss_ids_to_move) > 0:
            removed_count = self.identity_index.remove_from_unknown(identity_id_str)
            logger.info(f"[IDENTITY] Removed {removed_count} embeddings from UNKNOWN index metadata")
        else:
            logger.info(f"[IDENTITY] No FAISS embeddings to remove from UNKNOWN index (extracted from images instead)")
        
        # Step 4: Check if KNOWN index needs training (IVF/IVFPQ indexes)
        # If index is not trained, we need to train it first with existing known embeddings
        index_needs_training = (
            hasattr(self.identity_index.known_index, 'is_trained') and 
            not self.identity_index.known_index.is_trained
        )
        
        if index_needs_training:
            logger.warning(f"[IDENTITY] KNOWN index requires training! Attempting to train with existing embeddings...")
            # Try to get some existing known embeddings for training
            # If no known embeddings exist, use the embeddings we're about to add
            training_embeddings = []
            
            # Get existing known embeddings from database for training
            existing_embeddings_result = await db.execute(
                select(IdentityEmbedding).where(
                    IdentityEmbedding.faiss_index_type == 'known',
                    IdentityEmbedding.faiss_id.isnot(None)
                ).limit(10000)  # Get up to 10k for training
            )
            existing_embeddings = existing_embeddings_result.scalars().all()
            
            # Reconstruct embeddings from KNOWN index if they exist
            for emb_record in existing_embeddings:
                if emb_record.faiss_id is not None:
                    try:
                        if emb_record.faiss_id < self.identity_index.known_index.ntotal:
                            emb_vector = self.identity_index.known_index.reconstruct(int(emb_record.faiss_id))
                            training_embeddings.append(emb_vector)
                    except Exception as e:
                        logger.debug(f"Could not reconstruct embedding {emb_record.faiss_id} for training: {e}")
            
            # If no existing embeddings, use the ones we're promoting (need at least some for training)
            if len(training_embeddings) == 0 and len(embeddings_to_add) > 0:
                logger.info(f"[IDENTITY] No existing known embeddings found, using promoted embeddings for training")
                # Use the embeddings we're about to add (need at least 100 for IVF training)
                if len(embeddings_to_add) >= 100:
                    training_embeddings = [emb for _, emb in embeddings_to_add[:1000]]  # Use first 1000 for training
                else:
                    logger.warning(f"[IDENTITY] Not enough embeddings for training ({len(embeddings_to_add)} < 100). "
                                 f"Index will need training before adding vectors.")
            
            # Train the index if we have enough training data
            if len(training_embeddings) >= 100:
                training_array = np.array(training_embeddings)
                trained = self.identity_index.train_known_index(training_array)
                if trained:
                    logger.info(f"[IDENTITY] Successfully trained KNOWN index with {len(training_embeddings)} samples")
                else:
                    logger.error(f"[IDENTITY] Failed to train KNOWN index! Cannot add vectors.")
                    raise ValueError("KNOWN index training failed. Cannot promote identity.")
            else:
                logger.error(f"[IDENTITY] Not enough training data ({len(training_embeddings)} < 100). "
                            f"Cannot train IVF index. Please load known faces first or use Flat index.")
                raise ValueError(
                    f"KNOWN index requires training but insufficient data ({len(training_embeddings)} < 100). "
                    f"Load known faces from storage/faces first, or set KNOWN_INDEX_TYPE=flat"
                )
        
        # Step 5: Add to KNOWN index immediately
        # Format: (old_faiss_id, embedding_vector, pipeline_id, image_path)
        for emb_data in embeddings_to_add:
            try:
                # Unpack embedding data (always 4 elements now)
                if len(emb_data) == 4:
                    old_faiss_id, embedding_vector, emb_pipeline_id, emb_image_path = emb_data
                else:
                    logger.error(f"[IDENTITY] Invalid embedding data format (expected 4 elements, got {len(emb_data)}): {emb_data}")
                    continue
                
                # Get pipeline_id: from embedding data, or from existing embedding record, or from source
                pipeline_id_to_use = emb_pipeline_id
                if not pipeline_id_to_use and old_faiss_id is not None:
                    # Try to get from existing embedding record
                    for emb in embeddings:
                        if emb.faiss_id == old_faiss_id and emb.pipeline_id:
                            pipeline_id_to_use = emb.pipeline_id
                            break
                
                # Fallback to source_pipeline_id or "promoted"
                if not pipeline_id_to_use:
                    pipeline_id_to_use = source_pipeline_id or "promoted"
                
                new_faiss_id = self.identity_index.add_known(identity_id_str, embedding_vector)
                
                # Find corresponding IdentityEmbedding record by old faiss_id (if it exists)
                matching_emb = None
                if old_faiss_id is not None:
                    for emb in embeddings:
                        if emb.faiss_id == old_faiss_id:
                            matching_emb = emb
                            break
                
                if matching_emb:
                    # Update existing embedding record
                    matching_emb.faiss_index_type = 'known'
                    matching_emb.faiss_id = new_faiss_id
                    # Update pipeline_id if it was "promoted" or None
                    if not matching_emb.pipeline_id or matching_emb.pipeline_id == "promoted":
                        matching_emb.pipeline_id = pipeline_id_to_use
                    logger.debug(f"[IDENTITY] Moved embedding to KNOWN index (old_faiss_id={old_faiss_id}, new_faiss_id={new_faiss_id}, pipeline: {pipeline_id_to_use})")
                else:
                    # Create new embedding record (for embeddings extracted from images)
                    new_emb = IdentityEmbedding(
                        identity_id=identity_id,
                        detection_id=None,
                        pipeline_id=pipeline_id_to_use,  # Use actual pipeline_id from source
                        faiss_id=new_faiss_id,
                        faiss_index_type='known',
                        quality=None,  # Quality not calculated for promoted embeddings
                        created_at=datetime.utcnow()
                    )
                    db.add(new_emb)
                    logger.info(f"[IDENTITY] Created new embedding record for promoted identity (new_faiss_id={new_faiss_id}, pipeline: {pipeline_id_to_use})")
            except ValueError as e:
                # Handle training error specifically
                if "not trained" in str(e):
                    logger.error(f"[IDENTITY] KNOWN index not trained: {e}")
                    raise ValueError(f"Cannot promote identity: {e}")
                raise
            except Exception as e:
                logger.error(f"[IDENTITY] Failed to add embedding to KNOWN index: {e}")
                raise
        
        # Step 6: For any embeddings that couldn't be reconstructed, mark for re-indexing
        # Only process embeddings that have faiss_id set (not None)
        faiss_ids_processed = [old_id for old_id, *_ in embeddings_to_add if old_id is not None]
        for emb in embeddings:
            if emb.faiss_id is not None and emb.faiss_id not in faiss_ids_processed:
                emb.faiss_index_type = 'known'
                emb.faiss_id = None  # Will be re-indexed on next detection
                logger.debug(f"[IDENTITY] Marked embedding for re-indexing (old_faiss_id={emb.faiss_id} not found in UNKNOWN index)")
            elif emb.faiss_id is None and emb.faiss_index_type == 'unknown':
                # Update type for embeddings that were never indexed
                emb.faiss_index_type = 'known'
                logger.debug(f"[IDENTITY] Updated embedding type from unknown to known (will be indexed on next detection)")
        
                # Step 6.5: Move best image to storage/faces directory
        if best_image_path and os.path.exists(best_image_path):
            try:
                from config import settings
                faces_dir = settings.FACES_DIR
                
                # Ensure storage/faces directory exists
                os.makedirs(faces_dir, exist_ok=True)
                
                # Get file extension from source image
                _, ext = os.path.splitext(best_image_path)
                if not ext:
                    ext = ".jpg"  # Default to .jpg if no extension
                
                # Create filename: sanitize display_name for filesystem
                safe_name = "".join(c for c in display_name if c.isalnum() or c in (' ', '-', '_')).strip()
                safe_name = safe_name.replace(' ', '_')  # Replace spaces with underscores
                if not safe_name:
                    safe_name = f"promoted_{str(identity_id)[:8]}"  # Fallback if name is invalid
                
                # CLEVER: Check if a KNOWN Identity with same name already exists - reuse their folder!
                logger.info(f"[IDENTITY_PROMOTE]   🔍 Checking if KNOWN Identity with name '{display_name}' already exists...")
                existing_known_result = await db.execute(
                    select(Identity).where(
                        Identity.type == IdentityType.KNOWN,
                        Identity.display_name == display_name,
                        Identity.id != identity_id  # Exclude the identity we're promoting
                    ).limit(1)
                )
                existing_known = existing_known_result.scalar_one_or_none()
                
                person_folder = None
                if existing_known:
                    logger.info(f"[IDENTITY_PROMOTE]   ✅ Found existing KNOWN Identity (ID: {existing_known.id}) with same name!")
                    logger.info(f"[IDENTITY_PROMOTE]   📁 Will reuse existing person folder to keep all images together")
                    
                    # Try to extract folder from existing identity's best_snapshot_path
                    if existing_known.best_snapshot_path:
                        existing_path = existing_known.best_snapshot_path.replace("\\", "/")
                        if "/" in existing_path or os.sep in existing_path:
                            if faces_dir.replace("\\", "/") in existing_path.replace("\\", "/"):
                                parts = existing_path.replace("\\", "/").split("/")
                                if safe_name in parts:
                                    safe_name_idx = parts.index(safe_name)
                                    folder_parts = parts[:safe_name_idx + 1]
                                    person_folder = "/".join(folder_parts)
                                    if os.path.exists(person_folder):
                                        logger.info(f"[IDENTITY_PROMOTE]   ✅ Extracted existing folder: {person_folder}")
                    
                    # If couldn't extract, check filesystem
                    if not person_folder or not os.path.exists(person_folder):
                        potential_folder = os.path.join(faces_dir, safe_name)
                        if os.path.exists(potential_folder) and os.path.isdir(potential_folder):
                            person_folder = potential_folder
                            logger.info(f"[IDENTITY_PROMOTE]   ✅ Found existing folder: {person_folder}")
                    
                    # Fallback: create new folder
                    if not person_folder or not os.path.exists(person_folder):
                        person_folder = os.path.join(faces_dir, safe_name)
                        os.makedirs(person_folder, exist_ok=True)
                        logger.info(f"[IDENTITY_PROMOTE]   📁 Created new folder: {person_folder}")
                else:
                    # No existing Identity - create new folder
                    person_folder = os.path.join(faces_dir, safe_name)
                    os.makedirs(person_folder, exist_ok=True)
                    logger.info(f"[IDENTITY_PROMOTE]   📁 No existing Identity - created new folder: {person_folder}")
                
                # Save as image1.jpg, image2.jpg, etc. inside person's folder
                dest_path = os.path.join(person_folder, f"image1{ext}")
                
                # Handle filename conflicts (increment counter if exists)
                counter = 1
                while os.path.exists(dest_path):
                    counter += 1
                    dest_path = os.path.join(person_folder, f"image{counter}{ext}")
                    if counter > 1000:  # Safety limit
                        break
                
                # Copy (not move) the image to preserve original
                shutil.copy2(best_image_path, dest_path)
                logger.info(f"[IDENTITY_PROMOTE] ✅ Copied image to storage/faces: {dest_path}")
                
                # Update best_snapshot_path to point to new location
                identity.best_snapshot_path = dest_path
                logger.info(f"[IDENTITY_PROMOTE] Updated best_snapshot_path to: {dest_path}")
                
                # Also update IdentityAppearance records that reference the old path
                if best_image_path != dest_path:
                    await db.execute(
                        update(IdentityAppearance).where(
                            IdentityAppearance.identity_id == identity_id,
                            IdentityAppearance.best_snapshot_path == best_image_path
                        ).values(
                            best_snapshot_path=dest_path
                        )
                    )
                    logger.debug(f"[IDENTITY_PROMOTE] Updated IdentityAppearance records to use new path")
            except Exception as e:
                logger.warning(f"[IDENTITY_PROMOTE] Failed to copy image to storage/faces: {e}", exc_info=True)
                # Don't fail promotion if image copy fails
        else:
            logger.warning(f"[IDENTITY_PROMOTE] No best image path available to copy to storage/faces")
        
        # Update all Face records with this identity to use the display_name
        await db.execute(
            update(Face).where(
                Face.identity_id == identity_id
            ).values(
                name=display_name,
                label_state=LabelState.MANUAL_LABELED
            )
        )
        
        await db.flush()
        # Log final index states
        known_index_size_after = self.identity_index.known_index.ntotal if self.identity_index.known_index else 0
        unknown_index_size_after = self.identity_index.unknown_index.ntotal if self.identity_index.unknown_index else 0
        logger.info(f"[IDENTITY_PROMOTE] Index sizes AFTER promotion - KNOWN: {known_index_size_after} (+{known_index_size_after - known_index_size}), UNKNOWN: {unknown_index_size_after} ({unknown_index_size - unknown_index_size_after} removed)")
        
        # Verify embeddings were moved
        known_faiss_ids = list(self.identity_index.known_identity_to_faiss.get(identity_id_str, []))
        logger.info(f"[IDENTITY_PROMOTE] Identity now has {len(known_faiss_ids)} embeddings in KNOWN index: {known_faiss_ids}")
        
        if len(known_faiss_ids) == 0:
            logger.error(f"[IDENTITY_PROMOTE] ❌❌❌ CRITICAL: After promotion, identity has NO embeddings in KNOWN index!")
            logger.error(f"[IDENTITY_PROMOTE] This means the promotion failed to move embeddings!")
        else:
            logger.info(f"[IDENTITY_PROMOTE] ✅ Successfully promoted identity {identity_id} to KNOWN with {len(known_faiss_ids)} embeddings")
        
        logger.info(f"[IDENTITY_PROMOTE] ===== Promotion complete =====")
        
        return identity
    
    async def _promote_with_pgvector(
        self,
        identity: Identity,
        identity_id: uuid.UUID,
        display_name: str,
        user_id: int,
        db: AsyncSession
    ) -> Identity:
        """
        Promote unknown identity to known using pgvector backend.
        
        This is MUCH simpler than FAISS because:
        - No index reconstruction needed
        - No vector movement between indexes
        - Just update database records!
        """
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR] ========================================")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR] 🚀 Starting pgvector promotion")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR] Identity ID: {identity_id}")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR] Display name: '{display_name}'")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR] User ID: {user_id}")
        
        # Count embeddings before
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR] Step 1: Counting embeddings before promotion...")
        emb_count_result = await db.execute(
            select(func.count(IdentityEmbedding.id)).where(
                IdentityEmbedding.identity_id == identity_id,
                IdentityEmbedding.embedding.isnot(None)
            )
        )
        embedding_count = emb_count_result.scalar() or 0
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   ✅ Found {embedding_count} embeddings with non-NULL vectors")
        
        # Count by type
        emb_type_result = await db.execute(
            select(
                IdentityEmbedding.faiss_index_type,
                func.count(IdentityEmbedding.id)
            ).where(
                IdentityEmbedding.identity_id == identity_id
            ).group_by(IdentityEmbedding.faiss_index_type)
        )
        emb_by_type = {row[0]: row[1] for row in emb_type_result.all()}
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   Embeddings by type: {emb_by_type}")
        
        # Step 1: Update identity type (UNKNOWN → KNOWN)
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR] Step 2: Updating identity record...")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   BEFORE:")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]     type: {identity.type.value}")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]     status: {identity.status.value}")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]     display_name: {identity.display_name}")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]     updated_at: {identity.updated_at}")
        
        identity.type = IdentityType.KNOWN
        identity.status = IdentityStatus.PROMOTED
        old_display_name = identity.display_name
        identity.display_name = display_name
        identity.updated_at = datetime.utcnow()
        
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   AFTER:")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]     ✅ type: {identity.type.value} (was {IdentityType.UNKNOWN.value})")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]     ✅ status: {identity.status.value} (was {identity.status.value})")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]     ✅ display_name: '{identity.display_name}' (was '{old_display_name}')")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]     ✅ updated_at: {identity.updated_at}")
        
        # Step 2: Update embedding records (just change faiss_index_type field)
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR] Step 3: Updating embedding records...")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   Updating faiss_index_type: 'unknown' → 'known'")
        
        update_result = await db.execute(
            update(IdentityEmbedding).where(
                IdentityEmbedding.identity_id == identity_id,
                IdentityEmbedding.faiss_index_type == 'unknown'
            ).values(
                faiss_index_type='known'
            )
        )
        rows_updated = update_result.rowcount
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   ✅ Updated {rows_updated} embedding records")
        
        # Verify the update
        emb_after_result = await db.execute(
            select(
                IdentityEmbedding.faiss_index_type,
                func.count(IdentityEmbedding.id)
            ).where(
                IdentityEmbedding.identity_id == identity_id
            ).group_by(IdentityEmbedding.faiss_index_type)
        )
        emb_after_by_type = {row[0]: row[1] for row in emb_after_result.all()}
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   Embeddings after update: {emb_after_by_type}")
        
        if 'known' in emb_after_by_type and emb_after_by_type['known'] > 0:
            logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   ✅ {emb_after_by_type['known']} embeddings now marked as KNOWN")
        else:
            logger.warning(f"[IDENTITY_PROMOTE] [PGVECTOR]   ⚠️ No embeddings marked as KNOWN after update!")
        
        # Step 3: Copy best image to storage/faces using folder structure
        # Structure: faces/John_Doe/image1.jpg
        # CLEVER: Check if a KNOWN Identity with same name already exists - reuse their folder!
        best_image_path = identity.best_snapshot_path
        if best_image_path and os.path.exists(best_image_path):
            try:
                from config import settings
                faces_dir = settings.FACES_DIR
                
                os.makedirs(faces_dir, exist_ok=True)
                
                _, ext = os.path.splitext(best_image_path)
                if not ext:
                    ext = ".jpg"
                
                safe_name = "".join(c for c in display_name if c.isalnum() or c in (' ', '-', '_')).strip()
                safe_name = safe_name.replace(' ', '_')
                if not safe_name:
                    safe_name = f"promoted_{str(identity_id)[:8]}"
                
                # CLEVER: Check if a KNOWN Identity with this display_name already exists
                logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   🔍 Checking if KNOWN Identity with name '{display_name}' already exists...")
                existing_known_result = await db.execute(
                    select(Identity).where(
                        Identity.type == IdentityType.KNOWN,
                        Identity.display_name == display_name,
                        Identity.id != identity_id  # Exclude the identity we're promoting
                    ).limit(1)
                )
                existing_known = existing_known_result.scalar_one_or_none()
                
                person_folder = None
                if existing_known:
                    logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   ✅ Found existing KNOWN Identity (ID: {existing_known.id}) with same name!")
                    logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   📁 Will reuse existing person folder to keep all images together")
                    
                    # Try to extract folder from existing identity's best_snapshot_path
                    if existing_known.best_snapshot_path:
                        # Extract folder: faces/John_Doe/image1.jpg -> faces/John_Doe/
                        existing_path = existing_known.best_snapshot_path.replace("\\", "/")
                        if "/" in existing_path or os.sep in existing_path:
                            # Check if path contains faces_dir
                            if faces_dir.replace("\\", "/") in existing_path.replace("\\", "/"):
                                # Extract folder path
                                parts = existing_path.replace("\\", "/").split("/")
                                if safe_name in parts:
                                    # Found the person folder in the path
                                    safe_name_idx = parts.index(safe_name)
                                    folder_parts = parts[:safe_name_idx + 1]
                                    person_folder = "/".join(folder_parts)
                                    if os.path.exists(person_folder):
                                        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   ✅ Extracted existing folder from best_snapshot_path: {person_folder}")
                    
                    # If couldn't extract from path, check if folder exists in filesystem
                    if not person_folder or not os.path.exists(person_folder):
                        potential_folder = os.path.join(faces_dir, safe_name)
                        if os.path.exists(potential_folder) and os.path.isdir(potential_folder):
                            person_folder = potential_folder
                            logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   ✅ Found existing folder in filesystem: {person_folder}")
                    
                    # If still no folder found, create new one (shouldn't happen, but safe fallback)
                    if not person_folder or not os.path.exists(person_folder):
                        person_folder = os.path.join(faces_dir, safe_name)
                        os.makedirs(person_folder, exist_ok=True)
                        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   📁 Created new folder (existing identity had no folder): {person_folder}")
                else:
                    # No existing Identity with this name - create new folder
                    person_folder = os.path.join(faces_dir, safe_name)
                    os.makedirs(person_folder, exist_ok=True)
                    logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   📁 No existing Identity found - created new person folder: {person_folder}")
                
                # Save as image1.jpg, image2.jpg, etc. inside person's folder
                dest_path = os.path.join(person_folder, f"image1{ext}")
                
                counter = 1
                while os.path.exists(dest_path):
                    counter += 1
                    dest_path = os.path.join(person_folder, f"image{counter}{ext}")
                    if counter > 1000:  # Safety limit
                        logger.error(f"[IDENTITY_PROMOTE] [PGVECTOR]   ❌ Too many images for {safe_name} (limit: 1000)")
                        break
                
                shutil.copy2(best_image_path, dest_path)
                logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   ✅ Copied image to: {dest_path}")
                
                identity.best_snapshot_path = dest_path
                
                # Update appearances
                await db.execute(
                    update(IdentityAppearance).where(
                        IdentityAppearance.identity_id == identity_id,
                        IdentityAppearance.best_snapshot_path == best_image_path
                    ).values(
                        best_snapshot_path=dest_path
                    )
                )
            except Exception as e:
                logger.warning(f"[IDENTITY_PROMOTE] [PGVECTOR]   ⚠️ Failed to copy image: {e}", exc_info=True)
        else:
            logger.warning(f"[IDENTITY_PROMOTE] [PGVECTOR]   ⚠️ No best image path available")
        
        # Step 4: Update Face records
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR] Step 5: Updating Face records...")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   Setting name: 'Unknown' → '{display_name}'")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   Setting label_state: AUTO_UNKNOWN → MANUAL_LABELED")
        
        face_update_result = await db.execute(
            update(Face).where(
                Face.identity_id == identity_id
            ).values(
                name=display_name,
                label_state=LabelState.MANUAL_LABELED
            )
        )
        faces_updated = face_update_result.rowcount
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   ✅ Updated {faces_updated} Face records")
        
        # Verify Face records were updated
        face_verify_result = await db.execute(
            select(func.count(Face.id), func.count(Face.id).filter(Face.name == display_name))
            .where(Face.identity_id == identity_id)
        )
        face_verify_row = face_verify_result.first()
        total_faces = face_verify_row[0] or 0
        faces_with_name = face_verify_row[1] or 0
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   Verification: {faces_with_name}/{total_faces} Face records have name '{display_name}'")
        
        await db.flush()
        
        # Verify
        verify_result = await db.execute(
            select(func.count(IdentityEmbedding.id)).where(
                IdentityEmbedding.identity_id == identity_id,
                IdentityEmbedding.faiss_index_type == 'known',
                IdentityEmbedding.embedding.isnot(None)
            )
        )
        known_count = verify_result.scalar() or 0
        
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR] ✅ Successfully promoted identity {identity_id}")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   • Display name: {display_name}")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   • Embeddings as KNOWN: {known_count}")
        logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR] ===== Promotion complete =====")
        
        return identity
    
    async def merge_identities(
        self,
        from_identity_id: uuid.UUID,
        to_identity_id: uuid.UUID,
        user_id: int,
        notes: Optional[str],
        db: AsyncSession
    ) -> Identity:
        """
        Merge two identities into one.
        All data from from_identity is moved to to_identity.
        """
        # Get identities
        result = await db.execute(
            select(Identity).where(Identity.id.in_([from_identity_id, to_identity_id]))
        )
        identities = {id.id: id for id in result.scalars().all()}
        
        from_identity = identities.get(from_identity_id)
        to_identity = identities.get(to_identity_id)
        
        if not from_identity or not to_identity:
            raise ValueError("One or both identities not found")
        
        if from_identity.status == IdentityStatus.MERGED:
            raise ValueError(f"Identity {from_identity_id} is already merged")
        
        logger.info(f"[IDENTITY] Merging identity {from_identity_id} into {to_identity_id}")
        
        # Update from_identity
        from_identity.status = IdentityStatus.MERGED
        from_identity.merged_into_id = to_identity_id
        from_identity.updated_at = datetime.utcnow()
        
        # Move appearances
        await db.execute(
            update(IdentityAppearance).where(
                IdentityAppearance.identity_id == from_identity_id
            ).values(identity_id=to_identity_id)
        )
        
        # Move embeddings
        await db.execute(
            update(IdentityEmbedding).where(
                IdentityEmbedding.identity_id == from_identity_id
            ).values(identity_id=to_identity_id)
        )
        
        # Move faces
        await db.execute(
            update(Face).where(
                Face.identity_id == from_identity_id
            ).values(identity_id=to_identity_id)
        )
        
        # Update to_identity cache
        to_identity.appearances_count = (
            await db.execute(
                select(func.count(IdentityAppearance.id)).where(
                    IdentityAppearance.identity_id == to_identity_id
                )
            )
        ).scalar() or 0
        
        # Create merge audit record
        from db_models import IdentityMerge
        merge_record = IdentityMerge(
            from_identity_id=from_identity_id,
            to_identity_id=to_identity_id,
            merged_by=user_id,
            merged_at=datetime.utcnow(),
            notes=notes
        )
        db.add(merge_record)
        
        # Handle vector index based on backend
        if self.use_pgvector:
            # pgvector: No index removal needed!
            # The embeddings stay in PostgreSQL, just with updated identity_id
            # Search queries filter by identity.status='active', so merged identities are excluded
            logger.info(f"[IDENTITY] [PGVECTOR] Merge complete - no index removal needed (DB handles it)")
        else:
            # FAISS: Remove from indexes (mark as removed in metadata)
            if from_identity.type == IdentityType.KNOWN:
                self.identity_index.remove_from_known(str(from_identity_id))
            else:
                self.identity_index.remove_from_unknown(str(from_identity_id))
            logger.info(f"[IDENTITY] [FAISS] Removed from index")
        
        await db.flush()
        logger.info(f"[IDENTITY] Successfully merged identity {from_identity_id} into {to_identity_id}")
        
        return to_identity
    
    async def find_best_identity(
        self,
        identity_ids: List[uuid.UUID],
        db: AsyncSession
    ) -> tuple:
        """
        Find the best identity from a list to use as merge target.
        
        Production-grade selection criteria (weighted scoring):
        1. KNOWN type priority (weight: 5000) - preserve known identities
        2. Appearances count (weight: 1000) - most important metric
        3. Pipeline diversity (weight: 200 per pipeline) - cross-pipeline visibility
        4. Best quality snapshot (weight: 100) - quality indicator
        5. Snapshot quality score (weight: 50) - actual quality metric
        6. Age bonus (weight: 1 per day) - older = more established
        
        Time Complexity: O(n) - single pass through identities + pipeline queries
        
        Args:
            identity_ids: List of identity UUIDs to evaluate
            db: Database session
            
        Returns:
            Tuple of (best_identity_uuid, selection_details_dict)
        """
        if not identity_ids:
            raise ValueError("Cannot find best identity from empty list")
        
        if len(identity_ids) == 1:
            return identity_ids[0], {"auto_selected": False, "reason": "single_identity"}
        
        # Fetch all identities in one query (O(n))
        result = await db.execute(
            select(Identity).where(Identity.id.in_(identity_ids))
        )
        identities = list(result.scalars().all())
        
        if len(identities) != len(identity_ids):
            raise ValueError(f"Some identities not found. Expected {len(identity_ids)}, found {len(identities)}")
        
        # Filter out already merged identities
        active_identities = [id for id in identities if id.status != IdentityStatus.MERGED]
        if not active_identities:
            raise ValueError("All identities are already merged")
        
        # Get pipeline diversity for each identity
        pipeline_counts = {}
        for identity in active_identities:
            # Query pipeline_ids from IdentityAppearance
            pipeline_result = await db.execute(
                select(IdentityAppearance.pipeline_id)
                .where(IdentityAppearance.identity_id == identity.id)
                .distinct()
            )
            pipelines = [row[0] for row in pipeline_result if row[0]]
            
            # Fallback to IdentityEmbedding if no appearances
            if not pipelines:
                embedding_result = await db.execute(
                    select(IdentityEmbedding.pipeline_id)
                    .where(IdentityEmbedding.identity_id == identity.id)
                    .distinct()
                )
                pipelines = [row[0] for row in embedding_result if row[0]]
            
            pipeline_counts[identity.id] = {
                "count": len(pipelines),
                "pipelines": pipelines
            }
        
        # Find best identity using production-grade scoring system
        best_identity = None
        best_score = -1
        selection_details = []
        
        for identity in active_identities:
            # Score calculation with production weights:
            score = 0
            score_breakdown = {}
            
            # 1. KNOWN type priority (weight: 5000)
            if identity.type == IdentityType.KNOWN:
                score += 5000
                score_breakdown["known_type"] = 5000
            
            # 2. Appearances count (weight: 1000)
            appearances_score = identity.appearances_count * 1000
            score += appearances_score
            score_breakdown["appearances"] = appearances_score
            
            # 3. Pipeline diversity (weight: 200 per pipeline)
            pipeline_info = pipeline_counts.get(identity.id, {"count": 0})
            diversity_score = pipeline_info["count"] * 200
            score += diversity_score
            score_breakdown["pipeline_diversity"] = diversity_score
            
            # 4. Has best snapshot (weight: 100)
            if identity.best_snapshot_path:
                score += 100
                score_breakdown["has_snapshot"] = 100
            
            # 5. Age bonus (weight: 1 per day)
            if identity.created_at:
                age_days = (datetime.utcnow() - identity.created_at).days
                score += age_days
                score_breakdown["age_days"] = age_days
            
            selection_details.append({
                "id": str(identity.id),
                "type": identity.type.value,
                "display_name": identity.display_name,
                "score": score,
                "score_breakdown": score_breakdown,
                "appearances": identity.appearances_count,
                "pipelines": pipeline_info.get("pipelines", []),
                "pipeline_count": pipeline_info.get("count", 0)
            })
            
            if score > best_score:
                best_score = score
                best_identity = identity
        
        if not best_identity:
            raise ValueError("No valid identity found")
        
        # Sort selection details by score descending
        selection_details.sort(key=lambda x: x["score"], reverse=True)
        
        logger.info(f"[IDENTITY] Selected best identity {best_identity.id} "
                   f"(score: {best_score}, type: {best_identity.type.value}, "
                   f"appearances: {best_identity.appearances_count}, "
                   f"pipelines: {pipeline_counts.get(best_identity.id, {}).get('count', 0)})")
        
        return best_identity.id, {
            "auto_selected": True,
            "selected_id": str(best_identity.id),
            "selected_score": best_score,
            "candidates": selection_details,
            "reason": "highest_score"
        }
    
    async def merge_multiple_identities(
        self,
        identity_ids: List[uuid.UUID],
        target_identity_id: Optional[uuid.UUID] = None,
        user_id: int = None,
        notes: Optional[str] = None,
        db: AsyncSession = None
    ) -> dict:
        """
        Production-grade merge of multiple identities into one efficiently.
        
        Smart approach with production features:
        - If target_identity_id is provided, merge all others into it
        - Otherwise, automatically find the best identity (appearances, quality, diversity)
        - Type promotion: UNKNOWN + KNOWN → KNOWN (preserves known status)
        - Best snapshot selection: Compares quality across all identities
        - Enhanced FAISS handling: Proper embedding migration
        - Updated timestamps: first_seen_at and last_seen_at from all merged
        
        Time Complexity: O(n) where n = number of identities
        - O(n) to find best identity (if not provided)
        - O(n) to merge all others (each merge is O(1) database operations)
        
        Args:
            identity_ids: List of identity UUIDs to merge
            target_identity_id: Optional target identity. If None, best identity is auto-selected
            user_id: User performing the merge
            notes: Optional notes for audit
            db: Database session
            
        Returns:
            Dict with merged identity and detailed merge statistics
        """
        if not identity_ids or len(identity_ids) < 2:
            raise ValueError("At least 2 identities required for merge")
        
        # Remove duplicates
        unique_ids = list(set(identity_ids))
        if len(unique_ids) < 2:
            raise ValueError("At least 2 unique identities required")
        
        # Determine target identity
        selection_details = None
        if target_identity_id:
            if target_identity_id not in unique_ids:
                raise ValueError(f"Target identity {target_identity_id} not in merge list")
            target_id = target_identity_id
        else:
            # Auto-select best identity with production scoring (O(n) operation)
            target_id, selection_details = await self.find_best_identity(unique_ids, db)
        
        # Get all identities to merge (excluding target)
        source_ids = [id for id in unique_ids if id != target_id]
        
        if not source_ids:
            raise ValueError("No source identities to merge (all are the same as target)")
        
        logger.info(f"[IDENTITY] [MERGE] Starting production merge: {len(source_ids)} identities into {target_id}")
        
        # Get target identity
        result = await db.execute(select(Identity).where(Identity.id == target_id))
        target_identity = result.scalar_one_or_none()
        
        if not target_identity:
            raise ValueError(f"Target identity {target_id} not found")
        
        if target_identity.status == IdentityStatus.MERGED:
            raise ValueError(f"Target identity {target_id} is already merged")
        
        # Verify all source identities exist and are not merged
        source_result = await db.execute(
            select(Identity).where(Identity.id.in_(source_ids))
        )
        source_identities = {id.id: id for id in source_result.scalars().all()}
        
        if len(source_identities) != len(source_ids):
            missing = set(source_ids) - set(source_identities.keys())
            raise ValueError(f"Some source identities not found: {missing}")
        
        # Check for already merged identities
        already_merged = [id for id, identity in source_identities.items() if identity.status == IdentityStatus.MERGED]
        if already_merged:
            raise ValueError(f"Some identities are already merged: {already_merged}")
        
        # =====================================================
        # PRODUCTION FEATURE 1: Type Promotion Logic
        # If any source is KNOWN, target becomes KNOWN
        # =====================================================
        type_changed = False
        original_type = target_identity.type
        known_source_name = None
        
        if target_identity.type == IdentityType.UNKNOWN:
            for source_identity in source_identities.values():
                if source_identity.type == IdentityType.KNOWN:
                    target_identity.type = IdentityType.KNOWN
                    # Inherit display name from known source if target doesn't have one
                    if not target_identity.display_name and source_identity.display_name:
                        target_identity.display_name = source_identity.display_name
                        known_source_name = source_identity.display_name
                    type_changed = True
                    logger.info(f"[IDENTITY] [MERGE] Type promotion: UNKNOWN → KNOWN "
                               f"(inherited from {source_identity.id})")
                    break
        
        # =====================================================
        # PRODUCTION FEATURE 2: Best Snapshot Selection
        # Compare quality across all identities
        # =====================================================
        best_snapshot_path = target_identity.best_snapshot_path
        best_snapshot_quality = 0.0
        snapshot_source = "target"
        
        # Get target's embeddings to check quality
        target_embeddings = await db.execute(
            select(IdentityEmbedding)
            .where(IdentityEmbedding.identity_id == target_id)
            .order_by(IdentityEmbedding.quality.desc().nullslast())
            .limit(1)
        )
        target_best_emb = target_embeddings.scalar_one_or_none()
        if target_best_emb and target_best_emb.quality:
            best_snapshot_quality = target_best_emb.quality
        
        # Check source identities for better snapshots
        for source_id, source_identity in source_identities.items():
            if source_identity.best_snapshot_path:
                # Get best quality embedding for this identity
                source_embeddings = await db.execute(
                    select(IdentityEmbedding)
                    .where(IdentityEmbedding.identity_id == source_id)
                    .order_by(IdentityEmbedding.quality.desc().nullslast())
                    .limit(1)
                )
                source_best_emb = source_embeddings.scalar_one_or_none()
                source_quality = source_best_emb.quality if source_best_emb and source_best_emb.quality else 0.0
                
                if source_quality > best_snapshot_quality:
                    best_snapshot_path = source_identity.best_snapshot_path
                    best_snapshot_quality = source_quality
                    snapshot_source = str(source_id)
                    logger.info(f"[IDENTITY] [MERGE] Better snapshot found: {source_id} "
                               f"(quality: {source_quality:.3f} > {best_snapshot_quality:.3f})")
        
        # Update target's best snapshot if a better one was found
        if snapshot_source != "target" and best_snapshot_path:
            target_identity.best_snapshot_path = best_snapshot_path
            logger.info(f"[IDENTITY] [MERGE] Updated best snapshot from source {snapshot_source}")
        
        # =====================================================
        # PRODUCTION FEATURE 3: Collect Pipeline Stats
        # =====================================================
        all_pipelines = set()
        pipeline_stats = {}
        
        # Get target's pipelines
        target_pipelines_result = await db.execute(
            select(IdentityAppearance.pipeline_id)
            .where(IdentityAppearance.identity_id == target_id)
            .distinct()
        )
        target_pipelines = [row[0] for row in target_pipelines_result if row[0]]
        all_pipelines.update(target_pipelines)
        pipeline_stats[str(target_id)] = target_pipelines
        
        # Collect timestamps for updating first_seen_at and last_seen_at
        all_first_seen = [target_identity.first_seen_at] if target_identity.first_seen_at else []
        all_last_seen = [target_identity.last_seen_at] if target_identity.last_seen_at else []
        
        # =====================================================
        # MERGE EXECUTION
        # =====================================================
        merge_count = 0
        embeddings_moved = 0
        faces_moved = 0
        appearances_moved = 0
        
        for source_id in source_ids:
            source_identity = source_identities[source_id]
            
            # Collect timestamps
            if source_identity.first_seen_at:
                all_first_seen.append(source_identity.first_seen_at)
            if source_identity.last_seen_at:
                all_last_seen.append(source_identity.last_seen_at)
            
            # Get source's pipelines
            source_pipelines_result = await db.execute(
                select(IdentityAppearance.pipeline_id)
                .where(IdentityAppearance.identity_id == source_id)
                .distinct()
            )
            source_pipelines = [row[0] for row in source_pipelines_result if row[0]]
            all_pipelines.update(source_pipelines)
            pipeline_stats[str(source_id)] = source_pipelines
            
            # Update source identity status
            source_identity.status = IdentityStatus.MERGED
            source_identity.merged_into_id = target_id
            source_identity.updated_at = datetime.utcnow()
            
            # Count records before move
            appearance_count = (await db.execute(
                select(func.count(IdentityAppearance.id))
                .where(IdentityAppearance.identity_id == source_id)
            )).scalar() or 0
            
            embedding_count = (await db.execute(
                select(func.count(IdentityEmbedding.id))
                .where(IdentityEmbedding.identity_id == source_id)
            )).scalar() or 0
            
            face_count = (await db.execute(
                select(func.count(Face.id))
                .where(Face.identity_id == source_id)
            )).scalar() or 0
            
            # Move appearances (batch update - pipeline_id preserved!)
            await db.execute(
                update(IdentityAppearance).where(
                    IdentityAppearance.identity_id == source_id
                ).values(identity_id=target_id)
            )
            appearances_moved += appearance_count
            
            # Move embeddings (batch update - pipeline_id preserved!)
            await db.execute(
                update(IdentityEmbedding).where(
                    IdentityEmbedding.identity_id == source_id
                ).values(identity_id=target_id)
            )
            embeddings_moved += embedding_count
            
            # Move faces (batch update)
            await db.execute(
                update(Face).where(
                    Face.identity_id == source_id
                ).values(identity_id=target_id)
            )
            faces_moved += face_count
            
            # Create merge audit record with enhanced details
            from db_models import IdentityMerge
            merge_record = IdentityMerge(
                from_identity_id=source_id,
                to_identity_id=target_id,
                merged_by=user_id,
                merged_at=datetime.utcnow(),
                notes=f"{notes or ''} | pipelines: {source_pipelines} | embeddings: {embedding_count}"
            )
            db.add(merge_record)
            
            # =====================================================
            # PRODUCTION FEATURE 4: Vector Index Management
            # =====================================================
            if self.use_pgvector:
                # pgvector: No index removal needed - embeddings stay in PostgreSQL
                # Query filters by status='active', so merged identities are excluded automatically
                logger.debug(f"[IDENTITY] [MERGE] [PGVECTOR] Source {source_id} marked as merged (no index removal needed)")
            else:
                # FAISS: Mark source embeddings as removed in FAISS
                if source_identity.type == IdentityType.KNOWN:
                    removed = self.identity_index.remove_from_known(str(source_id))
                    logger.info(f"[IDENTITY] [MERGE] [FAISS] Removed {removed} vectors from KNOWN index for {source_id}")
                else:
                    removed = self.identity_index.remove_from_unknown(str(source_id))
                    logger.info(f"[IDENTITY] [MERGE] [FAISS] Removed {removed} vectors from UNKNOWN index for {source_id}")
            
            merge_count += 1
            logger.debug(f"[IDENTITY] [MERGE] Processed source {source_id}: "
                        f"appearances={appearance_count}, embeddings={embedding_count}, faces={face_count}")
        
        # =====================================================
        # PRODUCTION FEATURE 5: Update Target Timestamps
        # =====================================================
        if all_first_seen:
            target_identity.first_seen_at = min(all_first_seen)
        if all_last_seen:
            target_identity.last_seen_at = max(all_last_seen)
        
        # Update target identity cache
        target_identity.appearances_count = (
            await db.execute(
                select(func.count(IdentityAppearance.id)).where(
                    IdentityAppearance.identity_id == target_id
                )
            )
        ).scalar() or 0
        
        target_identity.updated_at = datetime.utcnow()
        
        # =====================================================
        # PRODUCTION FEATURE 6: Handle Type Change
        # If target was promoted from UNKNOWN to KNOWN, update embeddings
        # =====================================================
        if type_changed and original_type == IdentityType.UNKNOWN:
            if self.use_pgvector:
                # pgvector: Just update the faiss_index_type field in database
                logger.info(f"[IDENTITY] [MERGE] [PGVECTOR] Updating embeddings type: unknown → known")
                await db.execute(
                    update(IdentityEmbedding).where(
                        IdentityEmbedding.identity_id == target_id,
                        IdentityEmbedding.faiss_index_type == 'unknown'
                    ).values(faiss_index_type='known')
                )
                logger.info(f"[IDENTITY] [MERGE] [PGVECTOR] ✅ Embeddings updated to KNOWN type")
            else:
                # FAISS: Move embeddings from UNKNOWN to KNOWN index
                logger.info(f"[IDENTITY] [MERGE] [FAISS] Moving embeddings from UNKNOWN to KNOWN index for promoted target")
                
                # Get all embeddings now belonging to target
                target_embeddings_result = await db.execute(
                    select(IdentityEmbedding)
                    .where(IdentityEmbedding.identity_id == target_id)
                )
                target_all_embeddings = target_embeddings_result.scalars().all()
                
                # Move each embedding from UNKNOWN to KNOWN index
                for emb in target_all_embeddings:
                    if emb.faiss_id is not None and emb.faiss_index_type == "unknown":
                        # Reconstruct embedding from FAISS
                        try:
                            embedding_vector = self.identity_index.unknown_index.reconstruct(emb.faiss_id)
                            if embedding_vector is not None:
                                # Add to KNOWN index
                                new_faiss_id = self.identity_index.add_to_known(
                                    str(target_id),
                                    embedding_vector,
                                    emb.pipeline_id,
                                    emb.quality or 0.5
                                )
                                # Update database record
                                emb.faiss_id = new_faiss_id
                                emb.faiss_index_type = "known"
                                logger.debug(f"[IDENTITY] [MERGE] [FAISS] Moved embedding {emb.id} to KNOWN index: {new_faiss_id}")
                        except Exception as e:
                            logger.warning(f"[IDENTITY] [MERGE] [FAISS] Could not move embedding {emb.id}: {e}")
                
                # Remove old entries from UNKNOWN index
                self.identity_index.remove_from_unknown(str(target_id))
        
        await db.flush()
        
        # Build comprehensive result
        result = {
            "identity": target_identity,
            "merge_count": merge_count,
            "statistics": {
                "appearances_moved": appearances_moved,
                "embeddings_moved": embeddings_moved,
                "faces_moved": faces_moved,
                "total_appearances": target_identity.appearances_count,
                "pipeline_count": len(all_pipelines),
                "pipelines": list(all_pipelines)
            },
            "type_promotion": {
                "changed": type_changed,
                "from": original_type.value if type_changed else None,
                "to": target_identity.type.value if type_changed else None,
                "inherited_name": known_source_name
            },
            "snapshot_selection": {
                "source": snapshot_source,
                "quality": best_snapshot_quality,
                "path": best_snapshot_path
            },
            "pipeline_distribution": pipeline_stats,
            "selection_details": selection_details,
            "timestamps": {
                "first_seen_at": target_identity.first_seen_at.isoformat() if target_identity.first_seen_at else None,
                "last_seen_at": target_identity.last_seen_at.isoformat() if target_identity.last_seen_at else None
            }
        }
        
        logger.info(f"[IDENTITY] [MERGE] ✅ Successfully merged {merge_count} identities into {target_id}: "
                   f"appearances={appearances_moved}, embeddings={embeddings_moved}, pipelines={len(all_pipelines)}, "
                   f"type_changed={type_changed}")
        
        return result
    
    def compute_quality_score(
        self,
        face_size: int,
        blur_score: Optional[float] = None,
        confidence: Optional[float] = None,
        pose_score: Optional[float] = None
    ) -> float:
        """
        Compute quality score for a face embedding.
        Returns a score between 0.0 and 1.0.
        """
        score = 0.0
        
        # Face size (larger is better, normalized to 0-1)
        if face_size > 0:
            # Assume good face size is 100x100 or larger
            size_score = min(1.0, face_size / 10000.0)  # 100x100 = 10000
            score += size_score * 0.3
        
        # Blur score (lower blur is better)
        if blur_score is not None:
            blur_score_norm = max(0.0, 1.0 - blur_score / 100.0)  # Normalize blur
            score += blur_score_norm * 0.3
        
        # Confidence (higher is better)
        if confidence is not None:
            score += confidence * 0.2
        
        # Pose score (if available)
        if pose_score is not None:
            score += pose_score * 0.2
        
        return min(1.0, score)


# Global instance - will be set during startup in lifespan.py
identity_service: Optional[IdentityService] = None

