"""
Pipeline-Aware ML Clustering Service
====================================
Advanced merge suggestion system that uses:
- Pipeline-specific embeddings
- User pipeline access filtering
- ML-based similarity scoring
- Image feature extraction
"""

import logging
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional, Set
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select, and_, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db_models import (
    Identity, IdentityEmbedding, MergeSuggestion,
    IdentityType, IdentityStatus, MergeSuggestionStatus,
    IdentityAppearance, UserPipelineAccess
)
from backend.core.vector_index.access import load_vector
from backend.core.similarity_model import similarity_model

logger = logging.getLogger(__name__)


@dataclass
class PipelineEmbeddingData:
    """Embedding data for a specific pipeline"""
    pipeline_id: str
    embeddings: List[np.ndarray]  # Multiple embeddings from this pipeline
    embedding_qualities: List[float]
    appearance_times: List[datetime]
    avg_quality: float
    representative_embedding: np.ndarray  # Best quality embedding


@dataclass
class IdentityPipelineFeatures:
    """Pipeline-aware features for an identity"""
    identity_id: str
    pipeline_embeddings: Dict[str, PipelineEmbeddingData]  # pipeline_id -> data
    all_pipelines: Set[str]
    total_appearances: int
    avg_embedding_quality: float
    global_representative_embedding: np.ndarray  # Best overall embedding


class PipelineAwareClusteringService:
    """
    Advanced ML-based clustering service that:
    1. Filters by user's accessible pipelines
    2. Uses pipeline-specific embeddings
    3. Applies weighted similarity based on pipeline context
    4. Leverages image features and embeddings together
    """
    
    # Properties, not __init__ assignments. This class is instantiated as a
    # module-level singleton, so capturing these in __init__ froze them at
    # first import and no admin edit could ever reach them. The four literals
    # they replaced also duplicated four declared settings exactly, which the
    # settings page offered for editing while nothing read them.
    @property
    def min_similarity_threshold(self) -> float:
        return float(settings.UNKNOWN_SIMILARITY_THRESHOLD)

    @property
    def cross_pipeline_similarity_threshold(self) -> float:
        return float(settings.CROSS_PIPELINE_SIMILARITY_THRESHOLD)

    @property
    def pipeline_weight(self) -> float:
        """Weight for pipeline overlap in the blended similarity."""
        return float(settings.PIPELINE_SIMILARITY_WEIGHT)

    @property
    def embedding_weight(self) -> float:
        """Weight for embedding similarity in the blended similarity."""
        return float(settings.EMBEDDING_SIMILARITY_WEIGHT)


    async def generate_pipeline_aware_suggestions(
        self,
        db: AsyncSession,
        user_id: Optional[int] = None,
        user_pipelines: Optional[List[str]] = None
    ) -> int:
        """
        Generate merge suggestions filtered by user's accessible pipelines.
        
        Args:
            db: Database session
            user_id: Optional user ID to filter by their pipelines
            user_pipelines: Optional list of pipeline IDs (if None, uses all pipelines)
        
        Returns:
            Number of suggestions created
        """
        logger.info("[PIPELINE_CLUSTERING] ========== Starting pipeline-aware merge suggestions ==========")
        start_time = datetime.utcnow()
        
        # Get user's accessible pipelines
        if user_id and not user_pipelines:
            from backend.auth.auth_service import AuthService
            user_pipelines = await AuthService.get_user_pipelines(user_id, db)
            logger.info(f"[PIPELINE_CLUSTERING] User {user_id} has access to {len(user_pipelines)} pipelines: {user_pipelines}")
        
        # If no user pipelines specified, use all pipelines (admin mode)
        if not user_pipelines:
            # Get all unique pipeline IDs from identities
            pipeline_result = await db.execute(
                select(IdentityAppearance.pipeline_id).distinct()
            )
            user_pipelines = [row[0] for row in pipeline_result if row[0]]
            logger.info(f"[PIPELINE_CLUSTERING] Admin mode: Using all {len(user_pipelines)} pipelines")
        
        if not user_pipelines:
            logger.warning("[PIPELINE_CLUSTERING] No pipelines available, skipping suggestions")
            return 0
        
        # Get identities visible in user's pipelines
        recent_cutoff = datetime.utcnow() - timedelta(days=90)
        
        # Get identities that appear in user's accessible pipelines
        identity_result = await db.execute(
            select(Identity).distinct()
            .join(IdentityAppearance, Identity.id == IdentityAppearance.identity_id)
            .where(
                and_(
                    Identity.type == IdentityType.UNKNOWN,
                    Identity.status == IdentityStatus.ACTIVE,
                    Identity.last_seen_at >= recent_cutoff,
                    IdentityAppearance.pipeline_id.in_(user_pipelines)
                )
            )
        )
        identities = identity_result.scalars().all()
        
        logger.info(f"[PIPELINE_CLUSTERING] Found {len(identities)} identities in accessible pipelines")
        
        if len(identities) < 2:
            logger.info("[PIPELINE_CLUSTERING] Not enough identities for clustering")
            return 0
        
        # Build pipeline-aware feature vectors for each identity
        logger.info("[PIPELINE_CLUSTERING] Building pipeline-aware feature vectors...")
        identity_features = await self._build_pipeline_features(identities, user_pipelines, db)
        
        if len(identity_features) < 2:
            logger.info("[PIPELINE_CLUSTERING] Not enough identities with embeddings")
            return 0
        
        # Generate suggestions using ML-based similarity
        suggestions_created = await self._generate_ml_suggestions(
            identity_features, user_pipelines, db
        )
        
        duration = (datetime.utcnow() - start_time).total_seconds()
        logger.info(
            f"[PIPELINE_CLUSTERING] ✅ Generated {suggestions_created} suggestions in {duration:.2f}s"
        )
        
        return suggestions_created
    
    async def _build_pipeline_features(
        self,
        identities: List[Identity],
        accessible_pipelines: List[str],
        db: AsyncSession
    ) -> Dict[str, IdentityPipelineFeatures]:
        """
        Build pipeline-aware feature vectors for each identity.
        Groups embeddings by pipeline and extracts pipeline-specific features.
        """
        logger.info(f"[PIPELINE_CLUSTERING] Building features for {len(identities)} identities...")
        
        identity_features = {}
        accessible_pipelines_set = set(accessible_pipelines)
        
        for identity in identities:
            # Get all embeddings for this identity, grouped by pipeline
            emb_result = await db.execute(
                select(IdentityEmbedding).where(
                    and_(
                        IdentityEmbedding.identity_id == identity.id,
                        IdentityEmbedding.faiss_index_type == 'unknown',
                        IdentityEmbedding.quality.isnot(None),
                        IdentityEmbedding.pipeline_id.in_(accessible_pipelines)  # Filter by accessible pipelines
                    )
                ).order_by(IdentityEmbedding.quality.desc())
            )
            embeddings = emb_result.scalars().all()
            
            if not embeddings:
                continue
            
            # Group embeddings by pipeline
            pipeline_embeddings: Dict[str, PipelineEmbeddingData] = {}
            all_qualities = []
            
            for emb in embeddings:
                pipeline_id = emb.pipeline_id
                if not pipeline_id or pipeline_id not in accessible_pipelines_set:
                    continue
                
                # Get embedding vector from FAISS or database
                embedding_vector = await self._get_embedding_vector(emb, db)
                if embedding_vector is None:
                    continue
                
                if pipeline_id not in pipeline_embeddings:
                    # Get appearance times for this pipeline
                    app_result = await db.execute(
                        select(IdentityAppearance.start_time).where(
                            and_(
                                IdentityAppearance.identity_id == identity.id,
                                IdentityAppearance.pipeline_id == pipeline_id
                            )
                        ).order_by(IdentityAppearance.start_time)
                    )
                    appearance_times = [row[0] for row in app_result]
                    
                    pipeline_embeddings[pipeline_id] = PipelineEmbeddingData(
                        pipeline_id=pipeline_id,
                        embeddings=[],
                        embedding_qualities=[],
                        appearance_times=appearance_times,
                        avg_quality=0.0,
                        representative_embedding=None
                    )
                
                pipeline_embeddings[pipeline_id].embeddings.append(embedding_vector)
                pipeline_embeddings[pipeline_id].embedding_qualities.append(emb.quality or 0.0)
                all_qualities.append(emb.quality or 0.0)
            
            if not pipeline_embeddings:
                continue
            
            # Calculate representative embeddings for each pipeline (best quality)
            for pipeline_id, pipeline_data in pipeline_embeddings.items():
                if pipeline_data.embeddings:
                    # Use best quality embedding as representative
                    best_idx = np.argmax(pipeline_data.embedding_qualities)
                    pipeline_data.representative_embedding = pipeline_data.embeddings[best_idx]
                    pipeline_data.avg_quality = np.mean(pipeline_data.embedding_qualities)
            
            # Get global best embedding (across all pipelines)
            best_global_emb = max(
                [(emb.quality or 0.0, emb) for emb in embeddings],
                key=lambda x: x[0]
            )[1]
            global_embedding = await self._get_embedding_vector(best_global_emb, db)
            
            if global_embedding is None:
                continue
            
            identity_features[str(identity.id)] = IdentityPipelineFeatures(
                identity_id=str(identity.id),
                pipeline_embeddings=pipeline_embeddings,
                all_pipelines=set(pipeline_embeddings.keys()),
                total_appearances=identity.appearances_count,
                avg_embedding_quality=np.mean(all_qualities) if all_qualities else 0.0,
                global_representative_embedding=global_embedding
            )
        
        logger.info(f"[PIPELINE_CLUSTERING] Built features for {len(identity_features)} identities")
        return identity_features
    
    async def _get_embedding_vector(
        self,
        embedding_record: IdentityEmbedding,
        db: AsyncSession
    ) -> Optional[np.ndarray]:
        """Read the stored vector for an embedding row.

        Reads `identity_embeddings.embedding`, the authoritative copy. The
        previous implementation reconstructed the vector out of the UNKNOWN
        FAISS index by positional `faiss_id`; those positions shift on every
        rebuild, so after one rebuild it returned a DIFFERENT PERSON'S vector
        and fed that into merge-suggestion scoring.
        """
        try:
            vector = await load_vector(db, embedding_record.id)
        except Exception as e:
            logger.debug(f"[PIPELINE_CLUSTERING] Error loading embedding: {e}")
            return None
        if vector is None or vector.size == 0:
            return None

        # Normalize for cosine similarity
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector = vector / norm
        return vector.astype(np.float32)
    
    async def _generate_ml_suggestions(
        self,
        identity_features: Dict[str, IdentityPipelineFeatures],
        accessible_pipelines: List[str],
        db: AsyncSession
    ) -> int:
        """
        Generate merge suggestions using ML-based similarity scoring.
        Uses pipeline-aware weighted similarity.
        """
        logger.info(f"[PIPELINE_CLUSTERING] Generating ML-based suggestions for {len(identity_features)} identities...")
        
        identity_list = list(identity_features.values())
        suggestions_created = 0
        pairs_checked = 0
        verified_pairs = 0
        
        # Check all pairs
        for i, features1 in enumerate(identity_list):
            for features2 in identity_list[i+1:]:
                pairs_checked += 1
                
                # Calculate pipeline-aware similarity
                similarity_score, is_similar = await self._calculate_pipeline_aware_similarity(
                    features1, features2, accessible_pipelines
                )
                
                if not is_similar:
                    continue
                
                verified_pairs += 1
                
                # Check if suggestion already exists
                existing_result = await db.execute(
                    select(MergeSuggestion).where(
                        and_(
                            MergeSuggestion.status == MergeSuggestionStatus.PENDING,
                            func.jsonb_array_length(MergeSuggestion.identity_ids) >= 2
                        )
                    )
                )
                existing_suggestions = existing_result.scalars().all()
                
                # Check if this pair already exists
                pair_exists = False
                for existing in existing_suggestions:
                    existing_ids = set(existing.identity_ids if isinstance(existing.identity_ids, list) else [])
                    if {features1.identity_id, features2.identity_id} <= existing_ids:
                        pair_exists = True
                        break
                
                if pair_exists:
                    continue
                
                # Create suggestion
                common_pipelines = list(features1.all_pipelines & features2.all_pipelines)
                all_pipelines = list(features1.all_pipelines | features2.all_pipelines)
                is_cross_pipeline = len(common_pipelines) == 0
                
                # Get representative snapshots
                snapshots = []
                identity1_obj = await db.get(Identity, features1.identity_id)
                identity2_obj = await db.get(Identity, features2.identity_id)
                
                if identity1_obj and identity1_obj.best_snapshot_path:
                    snapshots.append(identity1_obj.best_snapshot_path)
                if identity2_obj and identity2_obj.best_snapshot_path:
                    snapshots.append(identity2_obj.best_snapshot_path)
                
                # Create cluster ID
                cluster_type = "cross_pipeline_ml" if is_cross_pipeline else "pipeline_ml"
                cluster_id = f"{cluster_type}_{features1.identity_id[:8]}_{features2.identity_id[:8]}"
                
                # Create suggestion
                # Note: MergeSuggestion model may not have is_cross_camera field
                # We'll store pipeline info in cluster_id
                suggestion = MergeSuggestion(
                    cluster_id=cluster_id,
                    identity_ids=[features1.identity_id, features2.identity_id],
                    confidence=similarity_score,
                    status=MergeSuggestionStatus.PENDING,
                    representative_snapshots=snapshots if snapshots else None,
                    created_at=datetime.utcnow()
                )
                
                # Try to set is_cross_camera if the field exists
                if hasattr(suggestion, 'is_cross_camera'):
                    suggestion.is_cross_camera = is_cross_pipeline
                db.add(suggestion)
                suggestions_created += 1
                
                logger.debug(
                    f"[PIPELINE_CLUSTERING] Created suggestion: {features1.identity_id[:8]}... ↔ "
                    f"{features2.identity_id[:8]}... (similarity: {similarity_score:.3f}, "
                    f"cross_pipeline: {is_cross_pipeline}, pipelines: {all_pipelines})"
                )
        
        if suggestions_created > 0:
            await db.commit()
        
        logger.info(
            f"[PIPELINE_CLUSTERING] ✅ ML suggestions complete: "
            f"{pairs_checked} pairs checked → {verified_pairs} verified → {suggestions_created} created"
        )
        
        return suggestions_created
    
    async def _calculate_pipeline_aware_similarity(
        self,
        features1: IdentityPipelineFeatures,
        features2: IdentityPipelineFeatures,
        accessible_pipelines: List[str]
    ) -> Tuple[float, bool]:
        """
        Calculate pipeline-aware similarity score using ML approach.
        
        Combines:
        1. Pipeline overlap (how many common pipelines)
        2. Embedding similarity (FAISS cosine similarity)
        3. Quality-weighted similarity (better quality embeddings = more weight)
        
        Returns:
            (similarity_score, is_similar)
        """
        # 1. Pipeline overlap score
        common_pipelines = features1.all_pipelines & features2.all_pipelines
        all_pipelines = features1.all_pipelines | features2.all_pipelines
        
        if not all_pipelines:
            return 0.0, False
        
        pipeline_overlap_ratio = len(common_pipelines) / len(all_pipelines) if all_pipelines else 0.0
        is_cross_pipeline = len(common_pipelines) == 0
        
        # 2. Embedding similarity (use best embeddings from common pipelines if available)
        embedding_similarities = []
        
        if common_pipelines:
            # Use embeddings from common pipelines (more reliable)
            for pipeline_id in common_pipelines:
                if pipeline_id in features1.pipeline_embeddings and pipeline_id in features2.pipeline_embeddings:
                    emb1 = features1.pipeline_embeddings[pipeline_id].representative_embedding
                    emb2 = features2.pipeline_embeddings[pipeline_id].representative_embedding
                    
                    if emb1 is not None and emb2 is not None:
                        # Cosine similarity
                        similarity = np.dot(emb1, emb2)
                        # Weight by average quality
                        quality_weight = (
                            features1.pipeline_embeddings[pipeline_id].avg_quality +
                            features2.pipeline_embeddings[pipeline_id].avg_quality
                        ) / 2.0
                        embedding_similarities.append((similarity, quality_weight))
        else:
            # Cross-pipeline: use global representative embeddings
            emb1 = features1.global_representative_embedding
            emb2 = features2.global_representative_embedding
            
            if emb1 is not None and emb2 is not None:
                similarity = np.dot(emb1, emb2)
                quality_weight = (features1.avg_embedding_quality + features2.avg_embedding_quality) / 2.0
                embedding_similarities.append((similarity, quality_weight))
        
        if not embedding_similarities:
            return 0.0, False
        
        # Weighted average of embedding similarities
        total_weight = sum(w for _, w in embedding_similarities)
        if total_weight > 0:
            avg_embedding_similarity = sum(sim * w for sim, w in embedding_similarities) / total_weight
        else:
            avg_embedding_similarity = embedding_similarities[0][0]
        
        # 3. Combined similarity score using trained model (if available) or heuristic
        try:
            # Calculate quality scores
            avg_quality1 = features1.avg_embedding_quality
            avg_quality2 = features2.avg_embedding_quality
            
            # Calculate appearances difference
            appearances_diff = abs(features1.total_appearances - features2.total_appearances)
            # Normalize appearances difference (max difference = 100)
            appearances_diff_normalized = min(1.0, appearances_diff / 100.0)
            
            # Use trained model if available, otherwise use heuristic
            combined_score = similarity_model.predict(
                embedding_similarity=avg_embedding_similarity,
                pipeline_overlap=pipeline_overlap_ratio,
                quality_score_1=avg_quality1,
                quality_score_2=avg_quality2,
                appearances_diff=appearances_diff_normalized,
                is_cross_pipeline=is_cross_pipeline
            )
            
            # Set threshold based on whether model is trained
            base_threshold = (self.cross_pipeline_similarity_threshold
                              if is_cross_pipeline else self.min_similarity_threshold)
            if similarity_model.is_trained:
                # Trained model: the same configured bar, relaxed by a fixed
                # margin because the model is more accurate. Derived rather
                # than written out, so raising the configured threshold raises
                # this one too — the two used to be independent literals
                # (0.30/0.45) that ignored the settings entirely.
                min_threshold = max(0.0, base_threshold
                                    - float(settings.CLUSTER_TRAINED_MODEL_MARGIN))
            else:
                min_threshold = base_threshold

        except Exception as e:
            logger.warning(f"[PIPELINE_CLUSTERING] Error using similarity model: {e}, falling back to heuristic")
            # Fallback to heuristic. Same weights as the non-cross-pipeline
            # branch below: this used to blend 0.9/0.1 while its sibling used
            # the configured 0.7/0.3, so one code path silently ignored both
            # weight settings.
            if is_cross_pipeline:
                combined_score = (avg_embedding_similarity * self.embedding_weight) + (pipeline_overlap_ratio * self.pipeline_weight)
                min_threshold = self.cross_pipeline_similarity_threshold
            else:
                combined_score = (avg_embedding_similarity * self.embedding_weight) + (pipeline_overlap_ratio * self.pipeline_weight)
                min_threshold = self.min_similarity_threshold
            combined_score = max(0.0, min(1.0, combined_score))
        
        is_similar = combined_score >= min_threshold
        
        return combined_score, is_similar


# Global instance
pipeline_aware_clustering = PipelineAwareClusteringService()

