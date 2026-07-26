"""
Identity Clustering Service
===========================
Offline clustering job for generating merge suggestions.

Supports dual backends:
- FAISS: Fast in-memory vector search (default)
- pgvector: PostgreSQL-based vector search (simpler, ACID compliant)

Set VECTOR_BACKEND=pgvector in environment to use pgvector.
"""

import os
import sys
import asyncio
import logging
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from collections import defaultdict

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from db_connection import db_manager
from db_models import (
    Identity, IdentityEmbedding, MergeSuggestion, IdentityAppearance,
    IdentityType, IdentityStatus, MergeSuggestionStatus
)
from sqlalchemy import select, func, and_, text

# Import FAISS index (original backend)
from backend.core.identity_index import identity_index

# Import pgvector index (new backend)
try:
    from backend.core.identity_index_pgvector import identity_index_pgvector, get_pgvector_index
    PGVECTOR_AVAILABLE = True
except ImportError:
    identity_index_pgvector = None
    get_pgvector_index = None
    PGVECTOR_AVAILABLE = False

# Determine which backend to use
VECTOR_BACKEND = getattr(settings, 'VECTOR_BACKEND', 'faiss').lower()
USE_PGVECTOR = VECTOR_BACKEND == 'pgvector' and PGVECTOR_AVAILABLE

logger = logging.getLogger(__name__)
logger.info(f"[CLUSTERING] Module loaded: {__name__}")
logger.info(f"[CLUSTERING] Vector backend: {VECTOR_BACKEND} (pgvector_available={PGVECTOR_AVAILABLE}, use_pgvector={USE_PGVECTOR})")

# Try to import clustering libraries
try:
    from sklearn.cluster import DBSCAN
    SKLEARN_AVAILABLE = True
except ImportError:
    try:
        # Try HDBSCAN as alternative
        import hdbscan
        HDBSCAN_AVAILABLE = True
        SKLEARN_AVAILABLE = False
    except ImportError:
        SKLEARN_AVAILABLE = False
        HDBSCAN_AVAILABLE = False
        logger.warning("⚠️  Clustering libraries not available. Install scikit-learn or hdbscan for merge suggestions.")


class IdentityClusteringService:
    """
    Clustering service for generating merge suggestions.
    Runs periodically to find similar unknown identities.
    """
    
    def __init__(
        self,
        cluster_interval_hours: Optional[int] = None,
        cluster_startup_delay_hours: Optional[float] = None,
        min_cluster_size: Optional[int] = None,
        eps: Optional[float] = None,
        min_samples: Optional[int] = None
    ):
        logger.info("[CLUSTERING] Initializing IdentityClusteringService...")
        # Load from central config if not provided
        self.cluster_interval_hours = cluster_interval_hours or settings.CLUSTER_INTERVAL_HOURS
        self.cluster_startup_delay_hours = cluster_startup_delay_hours or getattr(settings, 'CLUSTER_STARTUP_DELAY_HOURS', 1.0)
        self.min_cluster_size = min_cluster_size or settings.CLUSTER_MIN_SIZE
        self.eps = eps or settings.CLUSTER_EPS
        self.min_samples = min_samples or settings.CLUSTER_MIN_SAMPLES
        self._clustering_task = None
        # Always enabled - hybrid approach works with or without scikit-learn
        self._enabled = True
        logger.info(f"[CLUSTERING] Configuration: interval={self.cluster_interval_hours}h, startup_delay={self.cluster_startup_delay_hours}h, min_size={self.min_cluster_size}, eps={self.eps}, min_samples={self.min_samples}")
    
    async def start(self):
        """Start periodic clustering job"""
        logger.info("[CLUSTERING] Starting IdentityClusteringService...")
        self._clustering_task = asyncio.create_task(self._periodic_clustering())
        if identity_index:
            logger.info(f"[CLUSTERING] ✅ Service started successfully (startup delay: {self.cluster_startup_delay_hours}h, interval: {self.cluster_interval_hours}h, hybrid mode: pattern-based + FAISS)")
        else:
            logger.warning("[CLUSTERING] ⚠️  Service started but FAISS index not available - using pattern-based only")
    
    async def stop(self):
        """Stop clustering task"""
        if self._clustering_task:
            self._clustering_task.cancel()
            try:
                await self._clustering_task
            except asyncio.CancelledError:
                pass
        logger.info("[CLUSTERING] ✅ Service stopped successfully")
    
    async def _periodic_clustering(self):
        """Run clustering periodically"""
        # Wait before first run (configurable delay after startup)
        startup_delay_seconds = int(self.cluster_startup_delay_hours * 3600)
        logger.info(f"[CLUSTERING] Waiting {self.cluster_startup_delay_hours} hour(s) ({startup_delay_seconds}s) after startup before first clustering run...")
        await asyncio.sleep(startup_delay_seconds)
        
        while True:
            try:
                # Send notification 1 minute before clustering starts
                try:
                    from backend.core.background_task_notifier import background_task_notifier, TaskType
                    from datetime import datetime, timedelta
                    next_run_time = datetime.utcnow() + timedelta(seconds=60)
                    await background_task_notifier.notify_task_starting(
                        task_type=TaskType.IDENTITY_CLUSTERING,
                        task_name="Identity Clustering",
                        description="Analyzing unknown identities to generate merge suggestions using ML-based clustering",
                        estimated_duration="2-10 minutes",
                        scheduled_time=next_run_time
                    )
                    await asyncio.sleep(60)  # Wait 1 minute before starting
                except Exception as e:
                    logger.warning(f"[CLUSTERING] Failed to send notification: {e}")
                
                import time
                start_time = time.time()
                await self.generate_merge_suggestions()
                duration = time.time() - start_time
                
                # Send completion notification
                try:
                    from backend.core.background_task_notifier import background_task_notifier, TaskType
                    await background_task_notifier.notify_task_completed(
                        task_type=TaskType.IDENTITY_CLUSTERING,
                        task_name="Identity Clustering",
                        success=True,
                        duration_seconds=duration
                    )
                except Exception as e:
                    logger.debug(f"[CLUSTERING] Failed to send completion notification: {e}")
                
                # Wait for next clustering cycle (subtract 60 seconds for notification lead time)
                await asyncio.sleep((self.cluster_interval_hours * 3600) - 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CLUSTERING] ❌ Error during clustering cycle: {e}", exc_info=True)
                logger.info("[CLUSTERING] Will retry in 1 hour...")
                await asyncio.sleep(3600)  # Retry in 1 hour on error
    
    async def generate_merge_suggestions(self):
        """
        Generate merge suggestions by clustering unknown identities.
        """
        logger.info("[CLUSTERING] ========== Starting merge suggestions generation ==========")
        start_time = datetime.utcnow()
        
        # Check if we have at least one backend available
        has_faiss = identity_index is not None
        has_pgvector = USE_PGVECTOR and PGVECTOR_AVAILABLE
        
        if not has_faiss and not has_pgvector:
            logger.warning("[CLUSTERING] ⚠️  No vector backend available (FAISS or pgvector), skipping clustering")
            return
        
        logger.info(f"[CLUSTERING] 🔄 Starting identity clustering for merge suggestions... (FAISS: {has_faiss}, pgvector: {has_pgvector})")
        
        try:
            async with db_manager.get_session() as db:
                # Get active unknown identities with recent embeddings
                # Only consider identities seen in the last 90 days
                recent_cutoff = datetime.utcnow() - timedelta(days=90)
                logger.debug(f"[CLUSTERING] Querying for unknown identities seen after {recent_cutoff}")
                
                result = await db.execute(
                    select(Identity).where(
                        and_(
                            Identity.type == IdentityType.UNKNOWN,
                            Identity.status == IdentityStatus.ACTIVE,
                            Identity.last_seen_at >= recent_cutoff
                        )
                    )
                )
                identities = result.scalars().all()
                
                logger.info(f"[CLUSTERING] Found {len(identities)} active unknown identities (last 90 days)")
                
                if len(identities) < self.min_cluster_size:
                    logger.info(f"[CLUSTERING] ⏭️  Not enough identities for clustering ({len(identities)} < {self.min_cluster_size} minimum)")
                    return
                
                logger.info(f"[CLUSTERING] Processing {len(identities)} unknown identities for clustering...")
                
                # Verify that identities have embeddings before proceeding
                identities_with_embeddings = []
                for identity in identities:
                    # Check if identity has at least one embedding
                    emb_check = await db.execute(
                        select(func.count(IdentityEmbedding.id)).where(
                            and_(
                                IdentityEmbedding.identity_id == identity.id,
                                IdentityEmbedding.faiss_index_type == 'unknown'
                            )
                        )
                    )
                    emb_count = emb_check.scalar() or 0
                    
                    if emb_count > 0:
                        identities_with_embeddings.append(identity)
                    else:
                        logger.debug(f"[CLUSTERING] Identity {identity.id} has no embeddings, skipping")
                
                logger.info(f"[CLUSTERING] Found {len(identities_with_embeddings)} identities with embeddings (out of {len(identities)} total)")
                
                if len(identities_with_embeddings) < 2:
                    logger.info(f"[CLUSTERING] ⏭️  Not enough identities with embeddings for clustering ({len(identities_with_embeddings)} < 2 minimum)")
                    return
                
                # Use similarity-based clustering (works with both FAISS and pgvector)
                logger.info("[CLUSTERING] Starting similarity-based clustering...")
                await self._cluster_by_similarity(identities_with_embeddings, db)
                
                duration = (datetime.utcnow() - start_time).total_seconds()
                logger.info(f"[CLUSTERING] ✅ Clustering completed successfully in {duration:.2f}s")
                logger.info("[CLUSTERING] ========== Merge suggestions generation complete ==========")
        
        except Exception as e:
            duration = (datetime.utcnow() - start_time).total_seconds()
            logger.error(f"[CLUSTERING] ❌ Error generating merge suggestions after {duration:.2f}s: {e}", exc_info=True)
    
    async def _cluster_by_similarity(
        self,
        identities: List[Identity],
        db
    ):
        """
        Multi-approach clustering with fallback:
        1. Graph-based clustering (advanced) - finds clusters of 3+ identities
        2. Hybrid approach (fallback) - pattern-based + FAISS for pairs
        
        Strategy:
        - Try graph-based first (finds larger clusters)
        - Fallback to hybrid if graph-based fails or finds nothing
        """
        logger.info(f"[CLUSTERING] Starting multi-approach clustering for {len(identities)} identities")
        
        # Check if we have at least one backend available
        has_faiss = identity_index is not None
        has_pgvector = USE_PGVECTOR and PGVECTOR_AVAILABLE
        
        if not has_faiss and not has_pgvector:
            logger.warning("[CLUSTERING] ⚠️  No vector backend available, using pattern-based only")
            await self._create_pattern_based_suggestions(identities, db)
            return
        
        # Try graph-based clustering first (more advanced, finds clusters)
        logger.info("[CLUSTERING] Step 1: Attempting graph-based clustering (finds clusters of 3+)...")
        try:
            graph_suggestions = await self._create_graph_based_suggestions(identities, db)
            if graph_suggestions > 0:
                logger.info(f"[CLUSTERING] ✅ Graph-based clustering created {graph_suggestions} cluster suggestions")
                # Also run hybrid for any pairs not in clusters
                logger.info("[CLUSTERING] Step 2: Running hybrid approach for remaining pairs...")
                await self._create_hybrid_suggestions(identities, db)
                return
            else:
                logger.info("[CLUSTERING] Graph-based clustering found no clusters, falling back to hybrid approach")
        except Exception as e:
            logger.warning(f"[CLUSTERING] ⚠️  Graph-based clustering failed: {e}, falling back to hybrid approach")
        
        # Fallback to hybrid approach (reliable, works for pairs)
        logger.info("[CLUSTERING] Using hybrid clustering: pattern-based + FAISS similarity verification")
        await self._create_hybrid_suggestions(identities, db)
    
    
    async def _create_hybrid_suggestions(
        self,
        identities: List[Identity],
        db
    ):
        """
        Hybrid approach: Pattern-based filtering + FAISS similarity verification.
        This combines speed of pattern-based with accuracy of embedding similarity.
        
        Enhanced with CROSS-CAMERA support:
        - Mode 1: Same-camera matches (high confidence, fast)
        - Mode 2: Cross-camera matches (FAISS-first, lower confidence)
        """
        from db_models import IdentityAppearance
        
        suggestions_created = 0
        pattern_candidates = 0
        verified_candidates = 0
        cross_camera_candidates = 0
        
        logger.info(f"[CLUSTERING] [HYBRID] Step 1: Pattern-based + FAISS filtering for {len(identities)} identities (cross-camera enabled)...")
        
        # OPTIMIZATION: Pre-load all appearances in batch
        identity_ids_list = [id.id for id in identities]
        all_appearances_result = await db.execute(
            select(IdentityAppearance).where(
                IdentityAppearance.identity_id.in_(identity_ids_list)
            )
        )
        all_appearances = all_appearances_result.scalars().all()
        
        # Build lookup maps for fast access
        appearances_by_identity = defaultdict(list)
        for app in all_appearances:
            appearances_by_identity[app.identity_id].append(app)
        
        # Step 1: Find candidate pairs
        candidate_pairs = []
        pairs_checked = 0
        
        for i, identity1 in enumerate(identities):
            if i >= len(identities) - 1:
                break
            
            appearances1 = appearances_by_identity.get(identity1.id, [])
            if not appearances1:
                continue
            
            pipelines1 = {app.pipeline_id for app in appearances1}
            times1 = [app.start_time for app in appearances1]
            
            for identity2 in identities[i+1:]:
                if identity2.id == identity1.id:
                    continue
                
                pairs_checked += 1
                
                appearances2 = appearances_by_identity.get(identity2.id, [])
                if not appearances2:
                    continue
                
                pipelines2 = {app.pipeline_id for app in appearances2}
                times2 = [app.start_time for app in appearances2]
                
                # Calculate overlap metrics
                common_pipelines = pipelines1 & pipelines2
                all_pipelines = pipelines1 | pipelines2
                overlap_ratio = len(common_pipelines) / max(len(pipelines1), len(pipelines2)) if max(len(pipelines1), len(pipelines2)) > 0 else 0
                
                # Check time overlap
                time_overlap = False
                for t1 in times1:
                    for t2 in times2:
                        if abs((t1 - t2).total_seconds()) < 3600:
                            time_overlap = True
                            break
                    if time_overlap:
                        break
                
                # MODE 1: Same-camera patterns (original logic, fast pre-filter)
                # If cameras overlap >= 50% AND similar appearance count → add to candidates
                if overlap_ratio >= 0.5 and abs(identity1.appearances_count - identity2.appearances_count) <= 5:
                    if time_overlap or overlap_ratio >= 0.7:
                        pattern_candidates += 1
                        candidate_pairs.append({
                            'identity1': identity1,
                            'identity2': identity2,
                            'overlap_ratio': overlap_ratio,
                            'time_overlap': time_overlap,
                            'pipelines1': pipelines1,
                            'pipelines2': pipelines2,
                            'is_cross_camera': False
                        })
                
                # MODE 2: Cross-camera detection (NEW!)
                # If no camera overlap but vector backend will verify face similarity
                # This is slower but catches cross-camera duplicates
                elif overlap_ratio < 0.5 and (identity_index or USE_PGVECTOR):
                    # Cross-camera: No pattern pre-filter, rely entirely on FAISS
                    # Only check if both have at least 2 appearances (reduces noise)
                    if identity1.appearances_count >= 2 and identity2.appearances_count >= 2:
                        cross_camera_candidates += 1
                        candidate_pairs.append({
                            'identity1': identity1,
                            'identity2': identity2,
                            'overlap_ratio': overlap_ratio,
                            'time_overlap': time_overlap,
                            'pipelines1': pipelines1,
                            'pipelines2': pipelines2,
                            'is_cross_camera': True  # Flag for cross-camera
                        })
        
        logger.info(
            f"[CLUSTERING] [HYBRID] Step 1 complete: Checked {pairs_checked} pairs, "
            f"found {pattern_candidates} same-camera + {cross_camera_candidates} cross-camera candidates"
        )
        
        if not candidate_pairs:
            logger.info("[CLUSTERING] [HYBRID] No candidate pairs found for merge suggestions")
            return
        
        # Step 2: FAISS similarity verification (accurate)
        logger.info(f"[CLUSTERING] [HYBRID] Step 2: Verifying {len(candidate_pairs)} candidates with FAISS similarity...")
        
        verified_count = 0
        skipped_count = 0
        from sqlalchemy import text
        
        for idx, candidate in enumerate(candidate_pairs):
            identity1 = candidate['identity1']
            identity2 = candidate['identity2']
            overlap_ratio = candidate['overlap_ratio']
            time_overlap = candidate['time_overlap']
            is_cross_camera = candidate.get('is_cross_camera', False)
            pipelines1 = candidate['pipelines1']
            pipelines2 = candidate['pipelines2']
            
            # Verify face similarity using configured backend (FAISS or pgvector)
            logger.debug(f"[CLUSTERING] [HYBRID] Verifying candidate {idx + 1}/{len(candidate_pairs)}: {str(identity1.id)[:8]}... vs {str(identity2.id)[:8]}... (cross-camera: {is_cross_camera}, backend={VECTOR_BACKEND})")
            is_similar, face_similarity = await self._verify_face_similarity(
                identity1, identity2, db
            )
            
            # Cross-camera requires higher similarity threshold (0.5 vs 0.35)
            min_similarity = 0.50 if is_cross_camera else 0.35
            
            if not is_similar or face_similarity < min_similarity:
                skipped_count += 1
                logger.debug(f"[CLUSTERING] [HYBRID] Candidate {idx + 1} rejected: similarity {face_similarity:.3f} < threshold {min_similarity}")
                continue
            
            verified_candidates += 1
            verified_count += 1
            logger.debug(f"[CLUSTERING] [HYBRID] Candidate {idx + 1} verified: similarity={face_similarity:.3f}, overlap={overlap_ratio:.2f}, cross_camera={is_cross_camera}")
            
            # Check if suggestion already exists
            existing_result = await db.execute(
                select(MergeSuggestion).where(
                    and_(
                        MergeSuggestion.status == MergeSuggestionStatus.PENDING,
                        text(f"identity_ids::jsonb @> '[\"{identity1.id}\"]'::jsonb"),
                        text(f"identity_ids::jsonb @> '[\"{identity2.id}\"]'::jsonb")
                    )
                )
            )
            existing = existing_result.scalar_one_or_none()
            
            if existing:
                logger.debug(f"[CLUSTERING] [HYBRID] Suggestion already exists for {str(identity1.id)[:8]}... and {str(identity2.id)[:8]}..., skipping")
                continue
            
            # Calculate combined confidence score using ML model if available, otherwise heuristic
            try:
                from backend.core.similarity_model import similarity_model
                
                # Extract ML features for prediction
                # Get average quality scores from embeddings (calculate from database)
                quality_result1 = await db.execute(
                    select(func.avg(IdentityEmbedding.quality)).where(
                        IdentityEmbedding.identity_id == identity1.id,
                        IdentityEmbedding.quality.isnot(None)
                    )
                )
                quality1 = float(quality_result1.scalar() or 0.5)
                
                quality_result2 = await db.execute(
                    select(func.avg(IdentityEmbedding.quality)).where(
                        IdentityEmbedding.identity_id == identity2.id,
                        IdentityEmbedding.quality.isnot(None)
                    )
                )
                quality2 = float(quality_result2.scalar() or 0.5)
                
                # Calculate appearances difference (normalized)
                appearances1 = identity1.appearances_count or 0
                appearances2 = identity2.appearances_count or 0
                max_appearances = max(appearances1, appearances2, 1)
                appearances_diff = abs(appearances1 - appearances2) / max_appearances
                
                # Use ML model if trained, otherwise fallback to heuristic
                if similarity_model.is_trained:
                    ml_confidence = similarity_model.predict(
                        embedding_similarity=face_similarity,
                        pipeline_overlap=overlap_ratio,
                        quality_score_1=quality1,
                        quality_score_2=quality2,
                        appearances_diff=appearances_diff,
                        is_cross_pipeline=is_cross_camera
                    )
                    combined_confidence = ml_confidence
                    logger.debug(f"[CLUSTERING] [HYBRID] Using ML prediction: confidence={combined_confidence:.3f} (face={face_similarity:.3f}, cross_camera={is_cross_camera})")
                else:
                    # Fallback to heuristic if model not trained
                    if is_cross_camera:
                        combined_confidence = (face_similarity * 0.8) + 0.06
                    else:
                        pattern_confidence = min(0.75, overlap_ratio + (0.1 if time_overlap else 0))
                        combined_confidence = (face_similarity * 0.6) + (pattern_confidence * 0.4)
                        combined_confidence = min(0.95, combined_confidence)
                    logger.debug(f"[CLUSTERING] [HYBRID] Using heuristic (ML not trained): confidence={combined_confidence:.3f}")
            except Exception as ml_error:
                # Fallback to heuristic on any error
                logger.warning(f"[CLUSTERING] [HYBRID] ML prediction failed: {ml_error}, using heuristic")
                if is_cross_camera:
                    combined_confidence = (face_similarity * 0.8) + 0.06
                else:
                    pattern_confidence = min(0.75, overlap_ratio + (0.1 if time_overlap else 0))
                    combined_confidence = (face_similarity * 0.6) + (pattern_confidence * 0.4)
                    combined_confidence = min(0.95, combined_confidence)
            
            logger.debug(f"[CLUSTERING] [HYBRID] Creating suggestion: confidence={combined_confidence:.3f} (face={face_similarity:.3f}, cross_camera={is_cross_camera})")
            
            # Get representative snapshots
            snapshots = []
            if identity1.best_snapshot_path:
                snapshots.append(identity1.best_snapshot_path)
            if identity2.best_snapshot_path:
                snapshots.append(identity2.best_snapshot_path)
            
            # Create cluster_id that indicates cross-camera
            cluster_type = "cross_camera" if is_cross_camera else "hybrid"
            all_pipelines = list(pipelines1 | pipelines2)
            
            # Create suggestion with combined confidence
            suggestion = MergeSuggestion(
                cluster_id=f"{cluster_type}_{identity1.id}_{identity2.id}",
                identity_ids=[str(identity1.id), str(identity2.id)],
                confidence=combined_confidence,
                status=MergeSuggestionStatus.PENDING,
                representative_snapshots=snapshots if snapshots else None,
                created_at=datetime.utcnow()
            )
            db.add(suggestion)
            suggestions_created += 1
        
        if suggestions_created > 0:
            await db.commit()
            logger.info(
                f"[CLUSTERING] [HYBRID] ✅ Hybrid clustering complete: "
                f"{pattern_candidates} same-camera + {cross_camera_candidates} cross-camera candidates → "
                f"{verified_candidates} verified → "
                f"{suggestions_created} suggestions created"
            )
        else:
            logger.info(f"[CLUSTERING] [HYBRID] No new merge suggestions created (verified: {verified_candidates}, skipped: {skipped_count})")
    
    async def _verify_face_similarity(
        self,
        identity1: Identity,
        identity2: Identity,
        db
    ) -> Tuple[bool, float]:
        """
        Verify face similarity using configured backend (FAISS or pgvector).
        Dispatcher method that routes to the appropriate implementation.
        
        Args:
            identity1: First identity to compare
            identity2: Second identity to compare
            db: Database session
            
        Returns:
            Tuple of (is_similar, similarity_score)
        """
        if USE_PGVECTOR:
            return await self._verify_face_similarity_pgvector(identity1, identity2, db)
        else:
            return await self._verify_face_similarity_faiss(identity1, identity2, db)
    
    async def _verify_face_similarity_pgvector(
        self,
        identity1: Identity,
        identity2: Identity,
        db
    ) -> Tuple[bool, float]:
        """
        Verify face similarity using pgvector (PostgreSQL).
        MUCH SIMPLER than FAISS - direct SQL similarity calculation!
        No need to reconstruct embeddings or manage FAISS IDs.
        
        Args:
            identity1: First identity to compare
            identity2: Second identity to compare
            db: Database session
            
        Returns:
            Tuple of (is_similar, similarity_score)
        """
        logger.debug(
            f"[CLUSTERING] [PGVECTOR_VERIFY] Verifying similarity: "
            f"{str(identity1.id)[:8]}... vs {str(identity2.id)[:8]}..."
        )
        
        try:
            # Direct similarity calculation using pgvector
            # Uses the <=> operator for cosine distance
            # Similarity = 1 - distance
            query = text("""
                WITH best_embeddings AS (
                    SELECT 
                        identity_id,
                        embedding,
                        quality,
                        ROW_NUMBER() OVER (
                            PARTITION BY identity_id 
                            ORDER BY quality DESC NULLS LAST, created_at DESC
                        ) as rn
                    FROM identity_embeddings
                    WHERE 
                        identity_id IN (:id1, :id2)
                        AND embedding IS NOT NULL
                        AND faiss_index_type = 'unknown'
                )
                SELECT 
                    1 - (e1.embedding <=> e2.embedding) as similarity,
                    e1.quality as quality1,
                    e2.quality as quality2
                FROM best_embeddings e1
                CROSS JOIN best_embeddings e2
                WHERE 
                    e1.identity_id = :id1
                    AND e2.identity_id = :id2
                    AND e1.rn = 1 
                    AND e2.rn = 1
            """)
            
            logger.debug(
                f"[CLUSTERING] [PGVECTOR_VERIFY] Executing similarity query for "
                f"{str(identity1.id)[:8]}... vs {str(identity2.id)[:8]}..."
            )
            
            result = await db.execute(
                query,
                {
                    "id1": str(identity1.id),
                    "id2": str(identity2.id)
                }
            )
            
            row = result.fetchone()
            
            if not row or row[0] is None:
                logger.debug(
                    f"[CLUSTERING] [PGVECTOR_VERIFY] No embeddings found for comparison: "
                    f"{str(identity1.id)[:8]}... vs {str(identity2.id)[:8]}..."
                )
                return False, 0.0
            
            similarity_score, quality1, quality2 = row
            similarity_score = float(similarity_score)
            
            # Use same threshold as FAISS (0.35 for unknown)
            threshold = 0.35
            is_similar = similarity_score >= threshold
            
            if is_similar:
                logger.debug(
                    f"[CLUSTERING] [PGVECTOR_VERIFY] ✅ Face similarity verified: "
                    f"{str(identity1.id)[:8]}... ↔ {str(identity2.id)[:8]}... "
                    f"(similarity: {similarity_score:.3f}, q1={quality1}, q2={quality2})"
                )
            else:
                logger.debug(
                    f"[CLUSTERING] [PGVECTOR_VERIFY] ❌ Similarity too low: "
                    f"{str(identity1.id)[:8]}... vs {str(identity2.id)[:8]}... "
                    f"similarity={similarity_score:.3f} < threshold={threshold}"
                )
            
            return is_similar, similarity_score
            
        except Exception as e:
            logger.error(
                f"[CLUSTERING] [PGVECTOR_VERIFY] ❌ Error calculating similarity: {e}",
                exc_info=True
            )
            return False, 0.0
    
    async def _verify_face_similarity_faiss(
        self,
        identity1: Identity,
        identity2: Identity,
        db
    ) -> Tuple[bool, float]:
        """
        Verify face similarity using FAISS search.
        Searches for identity2 when querying with identity1's embedding.
        Returns (is_similar, similarity_score)
        """
        logger.debug(f"[CLUSTERING] [FAISS_VERIFY] Verifying similarity: {str(identity1.id)[:8]}... vs {str(identity2.id)[:8]}...")
        
        if not identity_index:
            logger.debug("[CLUSTERING] [FAISS_VERIFY] Identity index not available")
            return False, 0.0
        
        try:
            # Get best embedding records for both identities
            logger.debug(f"[CLUSTERING] [FAISS_VERIFY] Fetching embeddings for identity1: {str(identity1.id)[:8]}...")
            emb_result1 = await db.execute(
                select(IdentityEmbedding).where(
                    and_(
                        IdentityEmbedding.identity_id == identity1.id,
                        IdentityEmbedding.faiss_index_type == 'unknown',
                        IdentityEmbedding.quality.isnot(None)
                    )
                ).order_by(IdentityEmbedding.quality.desc()).limit(1)
            )
            emb_record1 = emb_result1.scalar_one_or_none()
            
            emb_result2 = await db.execute(
                select(IdentityEmbedding).where(
                    and_(
                        IdentityEmbedding.identity_id == identity2.id,
                        IdentityEmbedding.faiss_index_type == 'unknown',
                        IdentityEmbedding.quality.isnot(None)
                    )
                ).order_by(IdentityEmbedding.quality.desc()).limit(1)
            )
            emb_record2 = emb_result2.scalar_one_or_none()
            
            if not emb_record1 or not emb_record2:
                logger.debug(f"[CLUSTERING] [FAISS_VERIFY] Missing embeddings: emb1={emb_record1 is not None}, emb2={emb_record2 is not None}")
                return False, 0.0
            
            if emb_record1.faiss_id is None or emb_record2.faiss_id is None:
                logger.debug(f"[CLUSTERING] [FAISS_VERIFY] Missing FAISS IDs: faiss_id1={emb_record1.faiss_id}, faiss_id2={emb_record2.faiss_id}")
                return False, 0.0
            
            logger.debug(f"[CLUSTERING] [FAISS_VERIFY] Found embeddings: faiss_id1={emb_record1.faiss_id}, faiss_id2={emb_record2.faiss_id}, quality1={emb_record1.quality}, quality2={emb_record2.quality}")
            
            # Get embeddings from FAISS index using reconstruct
            # FAISS IndexFlatIP supports reconstruct
            try:
                with identity_index.lock:
                    # Check if index has enough vectors
                    if identity_index.unknown_index.ntotal == 0:
                        return False, 0.0
                    
                    if emb_record1.faiss_id >= identity_index.unknown_index.ntotal:
                        logger.warning(f"[CLUSTERING] [FAISS_VERIFY] FAISS ID {emb_record1.faiss_id} out of range (total: {identity_index.unknown_index.ntotal})")
                        return False, 0.0
                    
                    # Reconstruct embedding for identity1
                    logger.debug(f"[CLUSTERING] [FAISS_VERIFY] Reconstructing embedding from FAISS index (faiss_id={emb_record1.faiss_id})")
                    emb1_vector = identity_index.unknown_index.reconstruct(int(emb_record1.faiss_id))
                    emb1_vector = emb1_vector.astype(np.float32).reshape(1, -1)
                    
                    # Normalize for cosine similarity (IndexFlatIP uses inner product)
                    emb1_norm = np.linalg.norm(emb1_vector)
                    if emb1_norm > 0:
                        emb1_vector = emb1_vector / emb1_norm
                    
                    # Search for similar identities using identity1's embedding
                    # We'll search top 20 to see if identity2 appears
                    search_k = min(20, identity_index.unknown_index.ntotal)
                    similarities, indices = identity_index.unknown_index.search(emb1_vector, search_k)
                    
                    # Check if identity2 appears in results
                    identity2_str = str(identity2.id)
                    for sim, idx in zip(similarities[0], indices[0]):
                        if idx < 0:
                            continue
                        found_identity_id = identity_index.unknown_metadata.get(int(idx))
                        if found_identity_id == identity2_str:
                            # Found identity2 in search results
                            similarity_score = float(sim)
                            # Use threshold of 0.35 (same as unknown search)
                            if similarity_score >= 0.35:
                                logger.debug(
                                    f"[CLUSTERING] [FAISS_VERIFY] ✅ Face similarity verified: {str(identity1.id)[:8]}... ↔ {str(identity2.id)[:8]}... "
                                    f"(similarity: {similarity_score:.3f})"
                                )
                                return True, similarity_score
                    
                    logger.debug(
                        f"[CLUSTERING] [FAISS_VERIFY] ❌ Identity2 not found in top {search_k} results"
                    )
                    return False, 0.0
                    
            except AttributeError:
                # Index type doesn't support reconstruct
                logger.warning("[CLUSTERING] [FAISS_VERIFY] ⚠️  FAISS index doesn't support reconstruct, using pattern-based only")
                return True, 0.5  # Fallback to pattern-based
            except Exception as e:
                logger.debug(f"FAISS reconstruct/search error: {e}")
                # Fallback: if both have embeddings, assume pattern-based is good enough
                return True, 0.5
            
        except Exception as e:
            logger.debug(f"Error in FAISS similarity verification: {e}")
            return False, 0.0
    
    async def _create_graph_based_suggestions(
        self,
        identities: List[Identity],
        db
    ) -> int:
        """
        Graph-based clustering for finding clusters of 3+ identities.
        
        How it works:
        1. Build a similarity graph where:
           - Nodes = identities
           - Edges = similarity relationships (if two identities are similar enough)
        2. Find connected components (groups of identities all connected to each other)
        3. Each connected component = a cluster of identities that should be merged
        4. Create merge suggestions for each cluster
        
        Benefits:
        - Finds larger clusters (3, 4, 5+ identities) automatically
        - More efficient than checking all pairs
        - Handles transitive relationships (A=B, B=C → finds A=B=C)
        
        Example:
        Identity A ↔ Identity B (similar)
        Identity B ↔ Identity C (similar)
        Identity C ↔ Identity D (similar)
        → Graph finds cluster: [A, B, C, D] (all should be merged)
        
        Returns:
            Number of cluster suggestions created
        """
        from db_models import IdentityAppearance
        
        logger.info(f"[CLUSTERING] [GRAPH] 🔗 Starting graph-based clustering for {len(identities)} identities...")
        
        if len(identities) < 2:
            logger.info("[CLUSTERING] [GRAPH] Not enough identities for graph clustering (need at least 2)")
            return 0
        
        # OPTIMIZATION: Pre-load all appearances in batch (much faster than per-identity queries)
        logger.info("[CLUSTERING] [GRAPH] Step 0: Pre-loading identity appearances (optimization)...")
        identity_ids_list = [id.id for id in identities]
        
        all_appearances_result = await db.execute(
            select(IdentityAppearance).where(
                IdentityAppearance.identity_id.in_(identity_ids_list)
            )
        )
        all_appearances = all_appearances_result.scalars().all()
        
        # Build lookup maps for fast access
        appearances_by_identity = defaultdict(list)
        for app in all_appearances:
            appearances_by_identity[app.identity_id].append(app)
        
        # Pre-compute pipeline sets and time ranges for each identity
        identity_data = {}
        for identity in identities:
            appearances = appearances_by_identity.get(identity.id, [])
            if appearances:
                identity_data[identity.id] = {
                    'pipelines': {app.pipeline_id for app in appearances if app.pipeline_id},
                    'times': sorted([app.start_time for app in appearances]),
                    'appearances': appearances
                }
        
        logger.info(f"[CLUSTERING] [GRAPH] Step 0 complete: Loaded appearances for {len(identity_data)} identities")
        
        # Step 1: Build similarity graph (OPTIMIZED)
        # Graph structure: {identity_id: set of similar identity_ids}
        similarity_graph = defaultdict(set)
        edges_checked = 0
        edges_added = 0
        similarity_scores = {}  # Store similarity scores for later use
        
        logger.info(f"[CLUSTERING] [GRAPH] Step 1: Building similarity graph (checking {len(identities) * (len(identities) - 1) // 2} possible pairs)...")
        
        # Check all pairs to build edges (optimized with pre-loaded data)
        for i, identity1 in enumerate(identities):
            if i >= len(identities) - 1:
                break
            
            data1 = identity_data.get(identity1.id)
            if not data1:
                continue
            
            pipelines1 = data1['pipelines']
            times1 = data1['times']
            
            for identity2 in identities[i+1:]:
                if identity2.id == identity1.id:
                    continue
                
                data2 = identity_data.get(identity2.id)
                if not data2:
                    continue
                
                edges_checked += 1
                
                pipelines2 = data2['pipelines']
                times2 = data2['times']
                
                # OPTIMIZED: Pattern-based filtering (fast check before expensive FAISS)
                common_pipelines = pipelines1 & pipelines2
                overlap_ratio = len(common_pipelines) / max(len(pipelines1), len(pipelines2)) if max(len(pipelines1), len(pipelines2)) > 0 else 0
                
                # OPTIMIZED: Temporal overlap check (use sorted times for efficiency)
                time_overlap = False
                if times1 and times2:
                    # Check if time ranges overlap (optimized)
                    min_time1, max_time1 = times1[0], times1[-1]
                    min_time2, max_time2 = times2[0], times2[-1]
                    
                    # Quick range check first
                    if (max_time1 >= min_time2 - timedelta(hours=1)) and (min_time1 <= max_time2 + timedelta(hours=1)):
                        # Detailed check only if ranges overlap
                        for t1 in times1:
                            for t2 in times2:
                                if abs((t1 - t2).total_seconds()) < 3600:
                                    time_overlap = True
                                    break
                            if time_overlap:
                                break
                
                # Check if they pass pattern-based criteria
                pattern_match = False
                is_cross_camera = False
                appearance_diff = abs(identity1.appearances_count - identity2.appearances_count)
                
                # MODE 1: Same-camera pattern matching (original)
                if overlap_ratio >= 0.5 and appearance_diff <= 5:
                    if time_overlap or overlap_ratio >= 0.7:
                        pattern_match = True
                
                # MODE 2: Cross-camera detection (NEW!)
                # No camera overlap but both have enough appearances → check FAISS directly
                elif overlap_ratio < 0.5 and identity1.appearances_count >= 2 and identity2.appearances_count >= 2:
                    is_cross_camera = True
                    pattern_match = True  # Will rely on FAISS verification
                
                # If pattern matches, verify with vector backend (FAISS or pgvector)
                if pattern_match:
                    is_similar, face_similarity = await self._verify_face_similarity(
                        identity1, identity2, db
                    )
                    
                    # Cross-camera requires higher similarity threshold
                    min_similarity = 0.50 if is_cross_camera else 0.35
                    
                    if is_similar and face_similarity >= min_similarity:
                        # Add edge to graph (bidirectional)
                        edge_key = tuple(sorted([str(identity1.id), str(identity2.id)]))
                        similarity_graph[str(identity1.id)].add(str(identity2.id))
                        similarity_graph[str(identity2.id)].add(str(identity1.id))
                        similarity_scores[edge_key] = face_similarity
                        edges_added += 1
                        
                        if is_cross_camera:
                            logger.debug(f"[CLUSTERING] [GRAPH] Cross-camera edge: {str(identity1.id)[:8]}... ↔ {str(identity2.id)[:8]}... (similarity: {face_similarity:.3f})")
        
        logger.info(f"[CLUSTERING] [GRAPH] Step 1 complete: Graph built with {edges_added} edges from {edges_checked} pairs checked")
        
        if edges_added == 0:
            logger.info("[CLUSTERING] [GRAPH] No similarity edges found, no clusters to create")
            return 0
        
        # Step 2: Find connected components (clusters)
        # A connected component is a group of identities all connected to each other
        logger.info("[CLUSTERING] [GRAPH] Step 2: Finding connected components (clusters)...")
        
        visited = set()
        clusters = []
        
        def dfs(identity_id: str, cluster: set):
            """Depth-first search to find all connected identities"""
            if identity_id in visited:
                return
            visited.add(identity_id)
            cluster.add(identity_id)
            
            # Visit all neighbors
            for neighbor_id in similarity_graph.get(identity_id, set()):
                if neighbor_id not in visited:
                    dfs(neighbor_id, cluster)
        
        # Find all connected components
        for identity_id in similarity_graph.keys():
            if identity_id not in visited:
                cluster = set()
                dfs(identity_id, cluster)
                if len(cluster) >= 2:  # Only clusters with 2+ identities
                    clusters.append(cluster)
        
        logger.info(f"[CLUSTERING] [GRAPH] Step 2 complete: Found {len(clusters)} clusters")
        
        if not clusters:
            logger.info("[CLUSTERING] [GRAPH] No clusters found (all components are single identities)")
            return 0
        
        # Step 3: Create merge suggestions for each cluster
        logger.info(f"[CLUSTERING] [GRAPH] Step 3: Creating merge suggestions for {len(clusters)} clusters...")
        
        suggestions_created = 0
        from sqlalchemy import text
        
        for cluster_idx, cluster in enumerate(clusters):
            if len(cluster) < 2:
                continue
            
            # Convert string IDs back to UUIDs and get Identity objects
            cluster_ids = list(cluster)
            identity_objects = []
            for id_str in cluster_ids:
                # Find identity in our list
                for identity in identities:
                    if str(identity.id) == id_str:
                        identity_objects.append(identity)
                        break
            
            if len(identity_objects) < 2:
                continue
            
            # OPTIMIZED: Calculate cluster confidence using pre-computed similarity scores
            # Use cached similarity scores from graph building (faster than re-checking)
            total_similarity = 0.0
            similarity_count = 0
            
            # Check all pairs in cluster using cached scores
            for i, id1 in enumerate(identity_objects):
                for id2 in identity_objects[i+1:]:
                    edge_key = tuple(sorted([str(id1.id), str(id2.id)]))
                    if edge_key in similarity_scores:
                        # Use cached similarity score
                        total_similarity += similarity_scores[edge_key]
                        similarity_count += 1
                    else:
                        # Fallback: verify if not in cache (shouldn't happen, but handle edge case)
                        is_similar, face_sim = await self._verify_face_similarity(id1, id2, db)
                        if is_similar:
                            total_similarity += face_sim
                            similarity_count += 1
                            similarity_scores[edge_key] = face_sim
            
            # Average similarity in cluster
            avg_similarity = total_similarity / similarity_count if similarity_count > 0 else 0.5
            
            # Cluster confidence: average similarity with bonus for cluster size
            # Larger clusters get slight confidence boost (they're more likely to be correct)
            cluster_size_bonus = min(0.1, (len(cluster) - 2) * 0.02)  # Max 0.1 bonus for large clusters
            cluster_confidence = min(0.95, avg_similarity + cluster_size_bonus)
            
            # OPTIMIZED: Check if suggestion already exists (more efficient check)
            # Check if any pending suggestion contains all these identity IDs
            cluster_exists = False
            try:
                existing_result = await db.execute(
                    select(MergeSuggestion).where(
                        MergeSuggestion.status == MergeSuggestionStatus.PENDING
                    )
                )
                existing_suggestions = existing_result.scalars().all()
                
                cluster_ids_set = set(cluster_ids)
                for existing in existing_suggestions:
                    existing_ids = set(existing.identity_ids if isinstance(existing.identity_ids, list) else [])
                    # Check if this cluster is a subset or superset of existing suggestion
                    if existing_ids == cluster_ids_set:
                        cluster_exists = True
                        break
                    # Also check if any identity in this cluster is already in a pending suggestion
                    # (prevents duplicate suggestions for overlapping clusters)
                    if cluster_ids_set & existing_ids:
                        # If significant overlap (>50%), skip to avoid duplicates
                        overlap_ratio = len(cluster_ids_set & existing_ids) / max(len(cluster_ids_set), len(existing_ids))
                        if overlap_ratio > 0.5:
                            cluster_exists = True
                            logger.debug(f"Cluster {cluster_idx + 1} overlaps significantly with existing suggestion, skipping")
                            break
            except Exception as e:
                logger.debug(f"Error checking existing suggestions: {e}, continuing anyway")
            
            if cluster_exists:
                continue
            
            # Get representative snapshots (best from each identity in cluster)
            # OPTIMIZED: Also format paths properly for frontend
            snapshots = []
            import os
            storage_dir = settings.STORAGE_DIR
            storage_dir_abs = os.path.abspath(storage_dir)
            
            for identity in identity_objects:
                if identity.best_snapshot_path:
                    snapshot_path = identity.best_snapshot_path
                    # Format path for frontend (ensure relative path)
                    if os.path.isabs(snapshot_path):
                        snapshot_abs = os.path.abspath(snapshot_path)
                        if snapshot_abs.startswith(storage_dir_abs):
                            relative_path = os.path.relpath(snapshot_abs, storage_dir_abs)
                            snapshot_path = 'storage/' + relative_path.replace('\\', '/')
                    elif not snapshot_path.startswith('storage/'):
                        snapshot_path = 'storage/' + snapshot_path.lstrip('/')
                    
                    snapshots.append(snapshot_path)
                if len(snapshots) >= 6:  # Increased to 6 for better visualization
                    break
            
            # Create cluster suggestion
            cluster_id_str = f"graph_cluster_{cluster_idx + 1}_{len(cluster)}ids"
            suggestion = MergeSuggestion(
                cluster_id=cluster_id_str,
                identity_ids=cluster_ids,
                confidence=cluster_confidence,
                status=MergeSuggestionStatus.PENDING,
                representative_snapshots=snapshots if snapshots else None,
                created_at=datetime.utcnow()
            )
            db.add(suggestion)
            suggestions_created += 1
            
            logger.info(
                f"[CLUSTERING] [GRAPH] Created cluster suggestion {cluster_idx + 1}/{len(clusters)}: "
                f"{len(cluster)} identities, confidence: {cluster_confidence:.3f}"
            )
        
        if suggestions_created > 0:
            await db.commit()
            logger.info(
                f"[CLUSTERING] [GRAPH] ✅ Graph-based clustering complete: "
                f"{len(clusters)} clusters found → "
                f"{suggestions_created} suggestions created"
            )
        else:
            logger.info(f"[CLUSTERING] [GRAPH] No new cluster suggestions created from {len(clusters)} clusters")
        
        return suggestions_created
    
    async def _create_pattern_based_suggestions(
        self,
        identities: List[Identity],
        db
    ):
        """
        Create merge suggestions based on appearance patterns only.
        This is a fallback when FAISS is not available.
        """
        from db_models import IdentityAppearance
        
        logger.info(f"[CLUSTERING] [PATTERN] Starting pattern-based suggestions for {len(identities)} identities (FAISS not available)")
        
        # Group identities by similar appearance patterns
        # (same cameras, overlapping time windows)
        suggestions_created = 0
        pairs_checked = 0
        
        # For each identity, find others with similar patterns
        for i, identity1 in enumerate(identities):
            if i >= len(identities) - 1:
                break
            
            # Get appearances for identity1
            app_result1 = await db.execute(
                select(IdentityAppearance).where(
                    IdentityAppearance.identity_id == identity1.id
                )
            )
            appearances1 = app_result1.scalars().all()
            
            if not appearances1:
                continue
            
            # Get unique pipelines for identity1
            pipelines1 = {app.pipeline_id for app in appearances1}
            
            # Find identities with overlapping patterns
            for identity2 in identities[i+1:]:
                if identity2.id == identity1.id:
                    continue
                
                pairs_checked += 1
                
                # Get appearances for identity2
                app_result2 = await db.execute(
                    select(IdentityAppearance).where(
                        IdentityAppearance.identity_id == identity2.id
                    )
                )
                appearances2 = app_result2.scalars().all()
                
                if not appearances2:
                    continue
                
                pipelines2 = {app.pipeline_id for app in appearances2}
                
                # Check for overlap
                common_pipelines = pipelines1 & pipelines2
                overlap_ratio = len(common_pipelines) / max(len(pipelines1), len(pipelines2)) if max(len(pipelines1), len(pipelines2)) > 0 else 0
                
                # If they appear in same cameras and have similar appearance counts
                if overlap_ratio >= 0.5 and abs(identity1.appearances_count - identity2.appearances_count) <= 5:
                    # Also check time overlap
                    times1 = [app.start_time for app in appearances1]
                    times2 = [app.start_time for app in appearances2]
                    
                    # Check if there's temporal overlap (within 1 hour)
                    time_overlap = False
                    for t1 in times1:
                        for t2 in times2:
                            if abs((t1 - t2).total_seconds()) < 3600:  # 1 hour
                                time_overlap = True
                                break
                        if time_overlap:
                            break
                    
                    if time_overlap or overlap_ratio >= 0.7:
                        # Check if suggestion already exists
                        # JSONB contains check - use PostgreSQL JSONB operators
                        from sqlalchemy import text
                        existing_result = await db.execute(
                            select(MergeSuggestion).where(
                                and_(
                                    MergeSuggestion.status == MergeSuggestionStatus.PENDING,
                                    text(f"identity_ids::jsonb @> '[\"{identity1.id}\"]'::jsonb"),
                                    text(f"identity_ids::jsonb @> '[\"{identity2.id}\"]'::jsonb")
                                )
                            )
                        )
                        existing = existing_result.scalar_one_or_none()
                        
                        if not existing:
                            # Get representative snapshots
                            snapshots = []
                            if identity1.best_snapshot_path:
                                snapshots.append(identity1.best_snapshot_path)
                            if identity2.best_snapshot_path:
                                snapshots.append(identity2.best_snapshot_path)
                            
                            # Create suggestion
                            suggestion = MergeSuggestion(
                                cluster_id=f"pattern_{identity1.id}_{identity2.id}",
                                identity_ids=[str(identity1.id), str(identity2.id)],
                                confidence=min(0.75, overlap_ratio + (0.1 if time_overlap else 0)),
                                status=MergeSuggestionStatus.PENDING,
                                representative_snapshots=snapshots if snapshots else None,
                                created_at=datetime.utcnow()
                            )
                            db.add(suggestion)
                            suggestions_created += 1
        
        if suggestions_created > 0:
            await db.commit()
            logger.info(f"[CLUSTERING] [PATTERN] ✅ Created {suggestions_created} pattern-based merge suggestions (checked {pairs_checked} pairs)")
        else:
            logger.info(f"[CLUSTERING] [PATTERN] No new merge suggestions created (checked {pairs_checked} pairs)")


# Global instance - uses config values
clustering_service = IdentityClusteringService()

