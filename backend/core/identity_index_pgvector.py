"""
Identity Index Service using pgvector
=====================================
Production-ready vector similarity search using PostgreSQL pgvector extension.
Replaces/complements FAISS with unified storage in PostgreSQL.

Features:
- HNSW and IVFFlat index support
- Automatic index management
- Comprehensive logging for debugging
- ACID compliant transactions
- No sync issues (everything in PostgreSQL)

Usage:
    Set VECTOR_BACKEND=pgvector in environment to enable.
"""

import os
import sys
import numpy as np
import logging
import asyncio
from datetime import datetime
from typing import Tuple, List, Optional, Dict, Any
from contextlib import asynccontextmanager

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from sqlalchemy import select, and_, text, func, update
from sqlalchemy.ext.asyncio import AsyncSession

# Import central config
try:
    from config import settings
except ImportError:
    settings = None

logger = logging.getLogger(__name__)


class IdentityIndexPgVector:
    """
    pgvector-based identity index service.
    
    Uses PostgreSQL for vector storage and similarity search.
    Supports both HNSW (fast) and IVFFlat (memory efficient) indexes.
    
    Key advantages over FAISS:
    - No sync issues between FAISS and PostgreSQL
    - ACID compliance - vectors are part of transactions
    - Automatic persistence - no manual save/load
    - Simplified architecture - single data store
    """
    
    def __init__(self, embedding_size: Optional[int] = None):
        """
        Initialize pgvector index service.
        
        Args:
            embedding_size: Dimension of embeddings (default: 512 for ArcFace)
        """
        self.embedding_size = embedding_size or getattr(settings, 'IDENTITY_EMBEDDING_SIZE', 512)
        self.index_type = getattr(settings, 'PGVECTOR_INDEX_TYPE', 'hnsw').lower()
        
        # HNSW parameters
        self.hnsw_m = getattr(settings, 'PGVECTOR_HNSW_M', 16)
        self.hnsw_ef_construction = getattr(settings, 'PGVECTOR_HNSW_EF_CONSTRUCTION', 64)
        self.hnsw_ef_search = getattr(settings, 'PGVECTOR_HNSW_EF_SEARCH', 40)  # Search-time parameter for accuracy
        
        # IVFFlat parameters
        self.ivfflat_lists = getattr(settings, 'PGVECTOR_IVFFLAT_LISTS', 100)
        self.ivfflat_probes = getattr(settings, 'PGVECTOR_IVFFLAT_PROBES', 10)
        
        # Statistics tracking
        self._stats = {
            'searches_known': 0,
            'searches_unknown': 0,
            'embeddings_added': 0,
            'embeddings_removed': 0,
            'last_search_time_ms': 0,
            'avg_search_time_ms': 0,
        }
        self._search_times = []
        
        logger.info(f"[PGVECTOR] ✅ Initialized IdentityIndexPgVector")
        logger.info(f"[PGVECTOR]   • Embedding size: {self.embedding_size}")
        logger.info(f"[PGVECTOR]   • Index type: {self.index_type}")
        if self.index_type == 'hnsw':
            logger.info(f"[PGVECTOR]   • HNSW M: {self.hnsw_m}, efConstruction: {self.hnsw_ef_construction}, efSearch: {self.hnsw_ef_search}")
            logger.info(f"[PGVECTOR]   • HNSW is APPROXIMATE - may return slightly different results than exact search")
            logger.info(f"[PGVECTOR]   • Higher efSearch = more accurate but slower (current: {self.hnsw_ef_search})")
        else:
            logger.info(f"[PGVECTOR]   • IVFFlat lists: {self.ivfflat_lists}, probes: {self.ivfflat_probes}")
    
    # =========================================================================
    # INDEX MANAGEMENT
    # =========================================================================
    
    async def ensure_indexes_exist(self, db: AsyncSession) -> Dict[str, bool]:
        """
        Ensure pgvector indexes exist on the embedding column.
        Creates HNSW or IVFFlat index if not present.
        
        Args:
            db: Database session
            
        Returns:
            Dict with index creation status
        """
        logger.info("[PGVECTOR] [INDEX] Checking/creating vector indexes...")
        results = {'known_index': False, 'unknown_index': False, 'general_index': False}
        
        try:
            # Check if vector extension is available
            check_ext = await db.execute(text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_extension WHERE extname = 'vector'
                )
            """))
            ext_exists = check_ext.scalar()
            
            if not ext_exists:
                logger.error("[PGVECTOR] [INDEX] ❌ pgvector extension not installed! Run: CREATE EXTENSION vector;")
                return results
            
            logger.info("[PGVECTOR] [INDEX] ✅ pgvector extension is installed")
            
            # Check existing indexes
            check_indexes = await db.execute(text("""
                SELECT indexname FROM pg_indexes 
                WHERE tablename = 'identity_embeddings' 
                AND indexname LIKE '%embedding%'
            """))
            existing_indexes = [row[0] for row in check_indexes.fetchall()]
            logger.info(f"[PGVECTOR] [INDEX] Existing embedding indexes: {existing_indexes}")
            
            # Create appropriate index based on configuration
            index_name = f"idx_embedding_vector_{self.index_type}"
            
            if index_name not in existing_indexes:
                logger.info(f"[PGVECTOR] [INDEX] Creating {self.index_type.upper()} index...")
                
                if self.index_type == 'hnsw':
                    # HNSW index - fast approximate nearest neighbor
                    create_sql = text(f"""
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name}
                        ON identity_embeddings 
                        USING hnsw (embedding vector_cosine_ops)
                        WITH (m = {self.hnsw_m}, ef_construction = {self.hnsw_ef_construction})
                    """)
                else:
                    # IVFFlat index - memory efficient
                    create_sql = text(f"""
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name}
                        ON identity_embeddings 
                        USING ivfflat (embedding vector_cosine_ops)
                        WITH (lists = {self.ivfflat_lists})
                    """)
                
                try:
                    # Need to use raw connection for CREATE INDEX CONCURRENTLY
                    await db.execute(text("COMMIT"))  # End current transaction
                    await db.execute(create_sql)
                    await db.execute(text("BEGIN"))  # Start new transaction
                    logger.info(f"[PGVECTOR] [INDEX] ✅ Created {self.index_type.upper()} index: {index_name}")
                    results['general_index'] = True
                except Exception as e:
                    logger.warning(f"[PGVECTOR] [INDEX] ⚠️ Could not create index concurrently: {e}")
                    logger.info("[PGVECTOR] [INDEX] Trying non-concurrent index creation...")
                    
                    # Fallback to non-concurrent (requires table lock)
                    if self.index_type == 'hnsw':
                        create_sql_simple = text(f"""
                            CREATE INDEX IF NOT EXISTS {index_name}
                            ON identity_embeddings 
                            USING hnsw (embedding vector_cosine_ops)
                            WITH (m = {self.hnsw_m}, ef_construction = {self.hnsw_ef_construction})
                        """)
                    else:
                        create_sql_simple = text(f"""
                            CREATE INDEX IF NOT EXISTS {index_name}
                            ON identity_embeddings 
                            USING ivfflat (embedding vector_cosine_ops)
                            WITH (lists = {self.ivfflat_lists})
                        """)
                    await db.execute(create_sql_simple)
                    await db.commit()
                    logger.info(f"[PGVECTOR] [INDEX] ✅ Created {self.index_type.upper()} index (non-concurrent)")
                    results['general_index'] = True
            else:
                logger.info(f"[PGVECTOR] [INDEX] Index {index_name} already exists")
                results['general_index'] = True
            
            # Set IVFFlat probes if using that index type
            if self.index_type == 'ivfflat':
                await db.execute(text(f"SET ivfflat.probes = {self.ivfflat_probes}"))
                logger.info(f"[PGVECTOR] [INDEX] Set IVFFlat probes to {self.ivfflat_probes}")
            
            return results
            
        except Exception as e:
            logger.error(f"[PGVECTOR] [INDEX] ❌ Error ensuring indexes: {e}", exc_info=True)
            return results
    
    # =========================================================================
    # SEARCH OPERATIONS
    # =========================================================================
    
    async def search_known(
        self, 
        embedding: np.ndarray, 
        db: AsyncSession,
        top_k: int = 1, 
        threshold: float = 0.4
    ) -> List[Tuple[str, float]]:
        """
        Search for similar embeddings in KNOWN identities.
        
        Args:
            embedding: Query embedding (will be L2-normalized)
            db: Database session
            top_k: Number of results to return
            threshold: Minimum similarity threshold (cosine similarity)
            
        Returns:
            List of (identity_id, similarity_score) tuples sorted by similarity descending
        """
        import time
        start_time = time.time()
        
        logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ========================================")
        logger.info(f"[PGVECTOR] [SEARCH_KNOWN] 🔍 Starting KNOWN Identity Search (pgvector)")
        logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Using: SCRFD (detection) → ArcFace (embedding) → pgvector (search)")
        logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Parameters: top_k={top_k}, threshold={threshold}")
        logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Embedding shape: {embedding.shape}, norm: {np.linalg.norm(embedding):.4f}")
        logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Searching PostgreSQL with pgvector extension...")
        logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ========================================")
        
        try:
            # L2 normalize embedding for cosine similarity (CRITICAL: must be norm=1.0)
            norm = np.linalg.norm(embedding)
            logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Input embedding norm: {norm:.6f}")
            
            if norm > 0:
                normalized = (embedding / norm).astype(np.float32)
                final_norm = np.linalg.norm(normalized)
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ✅ Query embedding normalized (norm: {final_norm:.6f}, should be 1.0)")
                
                if abs(final_norm - 1.0) > 0.01:
                    logger.error(f"[PGVECTOR] [SEARCH_KNOWN] ❌ CRITICAL: Normalized query embedding norm is NOT 1.0! ({final_norm:.6f})")
                    logger.error(f"[PGVECTOR] [SEARCH_KNOWN] This will cause INCORRECT similarity scores!")
            else:
                logger.warning("[PGVECTOR] [SEARCH_KNOWN] ⚠️ Zero-norm embedding, cannot normalize")
                return []
            
            logger.debug(f"[PGVECTOR] [SEARCH_KNOWN] Converting normalized embedding to list...")
            embedding_list = normalized.tolist()
            logger.debug(f"[PGVECTOR] [SEARCH_KNOWN] ✅ Embedding converted to list (length: {len(embedding_list)})")
            
            logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Normalized embedding (first 5 values): {embedding_list[:5]}")
            
            # Format embedding as PostgreSQL array literal for pgvector
            # pgvector expects: '[0.1, 0.2, ...]'::vector
            logger.debug(f"[PGVECTOR] [SEARCH_KNOWN] Formatting embedding array string for PostgreSQL...")
            embedding_array_str = '[' + ','.join(str(x) for x in embedding_list) + ']'
            logger.debug(f"[PGVECTOR] [SEARCH_KNOWN] ✅ Embedding array string formatted (length: {len(embedding_array_str)} chars)")
            
            # pgvector uses <=> for cosine distance
            # cosine_similarity = 1 - cosine_distance
            # Use f-string with explicit cast (safer than parameter binding for vector type)
            logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Executing PostgreSQL query with pgvector...")
            
            # Use CTE to avoid repeating the embedding array string multiple times
            # Use SQLAlchemy enum values instead of raw SQL strings for enum comparison
            from db_models import Identity, IdentityType, IdentityStatus
            
            # Set HNSW ef_search parameter for better accuracy (if using HNSW index)
            # Higher ef_search = more accurate but slower
            # This affects how many candidates HNSW explores during search
            if self.index_type == 'hnsw' and self.hnsw_ef_search:
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Using HNSW ef_search={self.hnsw_ef_search} for better accuracy")
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ⚠️ HNSW is APPROXIMATE - may return slightly different results than exact search")
                # Set ef_search for this transaction (affects HNSW search accuracy)
                logger.debug(f"[PGVECTOR] [SEARCH_KNOWN] Setting HNSW ef_search parameter...")
                await db.execute(text(f"SET LOCAL hnsw.ef_search = {self.hnsw_ef_search}"))
                logger.debug(f"[PGVECTOR] [SEARCH_KNOWN] ✅ HNSW ef_search parameter set")
            
            logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Preparing PostgreSQL query...")
            logger.debug(f"[PGVECTOR] [SEARCH_KNOWN] Query parameters: identity_type={IdentityType.KNOWN.value}, identity_status={IdentityStatus.ACTIVE.value}, threshold={threshold}, top_k={top_k}")
            
            logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Executing PostgreSQL query (this may take a moment)...")
            result = await db.execute(
                text(f"""
                    WITH query_vector AS (
                        SELECT '{embedding_array_str}'::vector AS vec
                    )
                    SELECT 
                        ie.identity_id::text as identity_id,
                        1 - (ie.embedding <=> qv.vec) as similarity,
                        ie.quality,
                        i.display_name
                    FROM identity_embeddings ie
                    JOIN identities i ON ie.identity_id = i.id
                    CROSS JOIN query_vector qv
                    WHERE
                        ie.embedding IS NOT NULL
                        AND i.type::text = UPPER(:identity_type)
                        AND i.status::text IN ('ACTIVE', 'PROMOTED')
                        AND 1 - (ie.embedding <=> qv.vec) >= :threshold
                    ORDER BY ie.embedding <=> qv.vec
                    LIMIT :top_k
                """),
                {
                    "identity_type": IdentityType.KNOWN.value,  # Use enum value: "known"
                    "threshold": threshold,
                    "top_k": top_k
                }
            )

            results = []
            rows = result.fetchall()

            logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Query executed successfully")
            logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Query returned {len(rows)} rows")

            if len(rows) == 0:
                # DIAGNOSTIC: find the best candidate even below the threshold so every
                # miss is explainable in the logs (the thresholded query hides scores).
                try:
                    diag = await db.execute(
                        text(f"""
                            SELECT i.display_name, 1 - (ie.embedding <=> '{embedding_array_str}'::vector) AS sim
                            FROM identity_embeddings ie
                            JOIN identities i ON ie.identity_id = i.id
                            WHERE ie.embedding IS NOT NULL
                              AND i.type::text = UPPER(:identity_type)
                              AND i.status::text IN ('ACTIVE', 'PROMOTED')
                            ORDER BY ie.embedding <=> '{embedding_array_str}'::vector
                            LIMIT 1
                        """),
                        {"identity_type": IdentityType.KNOWN.value}
                    )
                    best = diag.fetchone()
                    if best:
                        logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ❌ Below threshold: best candidate '{best[0]}' sim={float(best[1]):.3f} (threshold {threshold})")
                    else:
                        logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ❌ No KNOWN identities with embeddings exist in the database")
                except Exception as diag_err:
                    logger.debug(f"[PGVECTOR] [SEARCH_KNOWN] Diagnostic query failed: {diag_err}")
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Query embedding norm: {np.linalg.norm(normalized):.6f} (should be 1.0)")
            
            for idx, row in enumerate(rows):
                identity_id, similarity, quality, display_name = row
                similarity_float = float(similarity)
                results.append((identity_id, similarity_float))
                
                # Log detailed similarity analysis
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ========================================")
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ✅ Match #{idx+1}:")
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN]    Identity ID: {identity_id[:8]}...")
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN]    Display Name: {display_name}")
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN]    Similarity: {similarity_float:.6f} ({similarity_float*100:.2f}%)")
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN]    Quality: {quality}")
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN]    Threshold: {threshold} ({threshold*100:.2f}%)")
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN]    Query norm: {np.linalg.norm(normalized):.6f} (should be 1.0)")
                
                if similarity_float >= threshold:
                    logger.info(f"[PGVECTOR] [SEARCH_KNOWN]    ✅ PASSES threshold - will be recognized as KNOWN")
                else:
                    logger.warning(f"[PGVECTOR] [SEARCH_KNOWN]    ❌ BELOW threshold - will NOT be recognized")
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ========================================")
            
            # Update statistics
            elapsed_ms = (time.time() - start_time) * 1000
            self._stats['searches_known'] += 1
            self._stats['last_search_time_ms'] = elapsed_ms
            self._search_times.append(elapsed_ms)
            if len(self._search_times) > 100:
                self._search_times = self._search_times[-100:]
            self._stats['avg_search_time_ms'] = sum(self._search_times) / len(self._search_times)
            
            if results:
                best_similarity = results[0][1]
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ========================================")
                logger.info(
                    f"[PGVECTOR] [SEARCH_KNOWN] ✅ SUCCESS: Found {len(results)} KNOWN matches "
                    f"(best similarity: {best_similarity:.6f} = {best_similarity*100:.2f}%) in {elapsed_ms:.2f}ms"
                )
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] Threshold: {threshold:.6f} ({threshold*100:.2f}%)")
                if best_similarity >= threshold:
                    logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ✅ Best match PASSES threshold - face will be recognized as KNOWN")
                    logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ✅ This face will be shown on dashboard")
                else:
                    logger.warning(f"[PGVECTOR] [SEARCH_KNOWN] ⚠️ Best match BELOW threshold ({best_similarity:.6f} < {threshold:.6f})")
                    logger.warning(f"[PGVECTOR] [SEARCH_KNOWN] ⚠️ Face will NOT be recognized (similarity too low)")
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ========================================")
            else:
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ========================================")
                logger.info(
                    f"[PGVECTOR] [SEARCH_KNOWN] ❌ No matches found above threshold {threshold} "
                    f"in {elapsed_ms:.2f}ms"
                )
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] This means the face will be marked as UNKNOWN")
                logger.info(f"[PGVECTOR] [SEARCH_KNOWN] ========================================")
            
            return results
            
        except Exception as e:
            logger.error(f"[PGVECTOR] [SEARCH_KNOWN] ❌ Error during search: {e}", exc_info=True)
            # Rollback transaction on error to prevent "transaction aborted" state
            try:
                await db.rollback()
                logger.debug(f"[PGVECTOR] [SEARCH_KNOWN] Transaction rolled back after error")
            except Exception as rollback_error:
                logger.error(f"[PGVECTOR] [SEARCH_KNOWN] Failed to rollback transaction: {rollback_error}", exc_info=True)
            return []
    
    async def search_unknown(
        self, 
        embedding: np.ndarray, 
        db: AsyncSession,
        top_k: int = 1, 
        threshold: float = 0.35
    ) -> List[Tuple[str, float]]:
        """
        Search for similar embeddings in UNKNOWN identities.
        Uses slightly looser threshold than known search.
        
        Args:
            embedding: Query embedding (will be L2-normalized)
            db: Database session
            top_k: Number of results to return
            threshold: Minimum similarity threshold (default 0.35, looser than known)
            
        Returns:
            List of (identity_id, similarity_score) tuples sorted by similarity descending
        """
        import time
        start_time = time.time()
        
        logger.debug(f"[PGVECTOR] [SEARCH_UNKNOWN] Starting search: top_k={top_k}, threshold={threshold}")
        
        try:
            # L2 normalize embedding
            norm = np.linalg.norm(embedding)
            if norm > 0:
                normalized = (embedding / norm).astype(np.float32)
            else:
                logger.warning("[PGVECTOR] [SEARCH_UNKNOWN] ⚠️ Zero-norm embedding, cannot normalize")
                return []
            
            embedding_list = normalized.tolist()
            
            logger.debug(f"[PGVECTOR] [SEARCH_UNKNOWN] Normalized embedding (first 5 values): {embedding_list[:5]}")
            
            # Format embedding as PostgreSQL array literal for pgvector
            # pgvector expects: '[0.1, 0.2, ...]'::vector
            embedding_array_str = '[' + ','.join(str(x) for x in embedding_list) + ']'
            
            # pgvector uses <=> for cosine distance
            # cosine_similarity = 1 - cosine_distance
            logger.debug(f"[PGVECTOR] [SEARCH_UNKNOWN] Executing search query...")
            
            # Use CTE to avoid repeating the embedding array string multiple times
            # Use SQLAlchemy enum values instead of raw SQL strings for enum comparison
            from db_models import Identity, IdentityType, IdentityStatus
            
            # Set HNSW ef_search parameter for better accuracy (if using HNSW index)
            if self.index_type == 'hnsw' and self.hnsw_ef_search:
                await db.execute(text(f"SET LOCAL hnsw.ef_search = {self.hnsw_ef_search}"))
            
            result = await db.execute(
                text(f"""
                    WITH query_vector AS (
                        SELECT '{embedding_array_str}'::vector AS vec
                    )
                    SELECT 
                        ie.identity_id::text as identity_id,
                        1 - (ie.embedding <=> qv.vec) as similarity,
                        ie.quality
                    FROM identity_embeddings ie
                    JOIN identities i ON ie.identity_id = i.id
                    CROSS JOIN query_vector qv
                    WHERE 
                        ie.embedding IS NOT NULL
                        AND i.type::text = UPPER(:identity_type)
                        AND i.status::text = UPPER(:identity_status)
                        AND 1 - (ie.embedding <=> qv.vec) >= :threshold
                    ORDER BY ie.embedding <=> qv.vec
                    LIMIT :top_k
                """),
                {
                    "identity_type": IdentityType.UNKNOWN.value,  # Use enum value: "unknown"
                    "identity_status": IdentityStatus.ACTIVE.value,  # Use enum value: "active"
                    "threshold": threshold,
                    "top_k": top_k
                }
            )
            
            results = []
            rows = result.fetchall()
            
            for row in rows:
                identity_id, similarity, quality = row
                results.append((identity_id, float(similarity)))
                logger.debug(
                    f"[PGVECTOR] [SEARCH_UNKNOWN] Match: identity={identity_id[:8]}... "
                    f"sim={similarity:.4f} quality={quality}"
                )
            
            # Update statistics
            elapsed_ms = (time.time() - start_time) * 1000
            self._stats['searches_unknown'] += 1
            
            if results:
                logger.debug(
                    f"[PGVECTOR] [SEARCH_UNKNOWN] Found {len(results)} matches "
                    f"(best: {results[0][1]:.4f}) in {elapsed_ms:.2f}ms"
                )
            else:
                logger.debug(
                    f"[PGVECTOR] [SEARCH_UNKNOWN] No matches above threshold {threshold} "
                    f"in {elapsed_ms:.2f}ms"
                )
            
            return results
            
        except Exception as e:
            logger.error(f"[PGVECTOR] [SEARCH_UNKNOWN] ❌ Error during search: {e}", exc_info=True)
            # Rollback transaction on error to prevent "transaction aborted" state
            try:
                await db.rollback()
                logger.debug(f"[PGVECTOR] [SEARCH_UNKNOWN] Transaction rolled back after error")
            except Exception as rollback_error:
                logger.error(f"[PGVECTOR] [SEARCH_UNKNOWN] Failed to rollback transaction: {rollback_error}", exc_info=True)
            return []
    
    async def search_all(
        self, 
        embedding: np.ndarray, 
        db: AsyncSession,
        top_k: int = 5, 
        threshold: float = 0.35
    ) -> List[Tuple[str, float, str]]:
        """
        Search across ALL identities (both known and unknown).
        
        Args:
            embedding: Query embedding
            db: Database session
            top_k: Number of results
            threshold: Minimum similarity
            
        Returns:
            List of (identity_id, similarity, identity_type) tuples
        """
        logger.debug(f"[PGVECTOR] [SEARCH_ALL] Starting combined search: top_k={top_k}, threshold={threshold}")
        
        try:
            norm = np.linalg.norm(embedding)
            if norm > 0:
                normalized = (embedding / norm).astype(np.float32)
            else:
                return []
            
            embedding_list = normalized.tolist()
            
            logger.debug(f"[PGVECTOR] [SEARCH_ALL] Normalized embedding (first 5 values): {embedding_list[:5]}")
            
            # Format embedding as PostgreSQL array literal for pgvector
            # pgvector expects: '[0.1, 0.2, ...]'::vector
            embedding_array_str = '[' + ','.join(str(x) for x in embedding_list) + ']'
            
            # pgvector uses <=> for cosine distance
            # cosine_similarity = 1 - cosine_distance
            logger.debug(f"[PGVECTOR] [SEARCH_ALL] Executing search query...")
            
            # Use CTE to avoid repeating the embedding array string multiple times
            # Use SQLAlchemy enum values instead of raw SQL strings for enum comparison
            from db_models import IdentityStatus
            
            # Set HNSW ef_search parameter for better accuracy (if using HNSW index)
            if self.index_type == 'hnsw' and self.hnsw_ef_search:
                await db.execute(text(f"SET LOCAL hnsw.ef_search = {self.hnsw_ef_search}"))
            
            result = await db.execute(
                text(f"""
                    WITH query_vector AS (
                        SELECT '{embedding_array_str}'::vector AS vec
                    )
                    SELECT 
                        ie.identity_id::text as identity_id,
                        1 - (ie.embedding <=> qv.vec) as similarity,
                        i.type::text as identity_type,
                        i.display_name
                    FROM identity_embeddings ie
                    JOIN identities i ON ie.identity_id = i.id
                    CROSS JOIN query_vector qv
                    WHERE 
                        ie.embedding IS NOT NULL
                        AND i.status::text IN ('ACTIVE', 'PROMOTED')
                        AND 1 - (ie.embedding <=> qv.vec) >= :threshold
                    ORDER BY ie.embedding <=> qv.vec
                    LIMIT :top_k
                """),
                {
                    "threshold": threshold,
                    "top_k": top_k
                }
            )
            
            results = []
            for row in result.fetchall():
                identity_id, similarity, identity_type, display_name = row
                results.append((identity_id, float(similarity), identity_type))
                logger.debug(
                    f"[PGVECTOR] [SEARCH_ALL] Match: {identity_id[:8]}... "
                    f"type={identity_type} sim={similarity:.4f} name='{display_name}'"
                )
            
            logger.info(f"[PGVECTOR] [SEARCH_ALL] Found {len(results)} matches across all identities")
            return results
            
        except Exception as e:
            logger.error(f"[PGVECTOR] [SEARCH_ALL] ❌ Error: {e}", exc_info=True)
            # Rollback transaction on error to prevent "transaction aborted" state
            try:
                await db.rollback()
                logger.debug(f"[PGVECTOR] [SEARCH_ALL] Transaction rolled back after error")
            except Exception as rollback_error:
                logger.error(f"[PGVECTOR] [SEARCH_ALL] Failed to rollback transaction: {rollback_error}", exc_info=True)
            return []
    
    # =========================================================================
    # ADD/REMOVE OPERATIONS
    # =========================================================================
    
    async def max_similarity_to_identity(
        self,
        identity_id: str,
        embedding: np.ndarray,
        db: AsyncSession
    ) -> Optional[float]:
        """Max cosine similarity between `embedding` and an identity's stored embeddings.

        Returns None when the identity has no embeddings. Used to skip near-duplicate
        inserts (loader restarts, repeated identical frames) and to gate enrichment.
        """
        try:
            norm = np.linalg.norm(embedding)
            if norm == 0:
                return None
            normalized = (embedding / norm).astype(np.float32)
            embedding_array_str = '[' + ','.join(str(x) for x in normalized.tolist()) + ']'
            result = await db.execute(
                text(f"""
                    SELECT MAX(1 - (embedding <=> '{embedding_array_str}'::vector))
                    FROM identity_embeddings
                    WHERE identity_id = :identity_id AND embedding IS NOT NULL
                """),
                {"identity_id": identity_id}
            )
            value = result.scalar()
            return float(value) if value is not None else None
        except Exception as e:
            logger.warning(f"[PGVECTOR] max_similarity_to_identity failed for {identity_id[:8]}: {e}")
            return None

    async def count_identity_embeddings(self, identity_id: str, db: AsyncSession) -> int:
        """Number of stored embeddings for an identity."""
        try:
            result = await db.execute(
                text("SELECT COUNT(*) FROM identity_embeddings WHERE identity_id = :identity_id AND embedding IS NOT NULL"),
                {"identity_id": identity_id}
            )
            return int(result.scalar() or 0)
        except Exception:
            return 0

    async def add_embedding(
        self,
        identity_id: str,
        embedding: np.ndarray,
        detection_id: Optional[int],
        pipeline_id: str,
        quality_score: Optional[float],
        index_type: str,  # 'known' or 'unknown'
        db: AsyncSession
    ) -> Optional[int]:
        """
        Add embedding to database with pgvector.
        
        Args:
            identity_id: UUID of the identity
            embedding: Face embedding (will be L2-normalized)
            detection_id: Optional detection ID for traceability
            pipeline_id: Pipeline where face was detected
            quality_score: Quality score of the face
            index_type: 'known' or 'unknown'
            db: Database session
            
        Returns:
            ID of created embedding record, or None if failed
        """
        import uuid as uuid_module
        from db_models import IdentityEmbedding
        
        logger.debug(
            f"[PGVECTOR] [ADD] Adding embedding: identity={identity_id[:8]}... "
            f"type={index_type} pipeline={pipeline_id} quality={quality_score}"
        )
        
        try:
            # L2 normalize embedding (CRITICAL: must be norm=1.0 for cosine similarity)
            norm = np.linalg.norm(embedding)
            logger.info(f"[PGVECTOR] [ADD] Input embedding norm: {norm:.6f}")
            
            if norm > 0:
                normalized = (embedding / norm).astype(np.float32)
                final_norm = np.linalg.norm(normalized)
                logger.info(f"[PGVECTOR] [ADD] ✅ Normalized embedding norm: {final_norm:.6f} (should be 1.0)")
                
                if abs(final_norm - 1.0) > 0.01:
                    logger.error(f"[PGVECTOR] [ADD] ❌ CRITICAL: Normalized embedding norm is NOT 1.0! ({final_norm:.6f})")
                    logger.error(f"[PGVECTOR] [ADD] This will cause INCORRECT similarity scores!")
            else:
                logger.warning(f"[PGVECTOR] [ADD] ⚠️ Zero-norm embedding for {identity_id[:8]}...")
                normalized = embedding.astype(np.float32)
            
            embedding_list = normalized.tolist()
            
            logger.info(f"[PGVECTOR] [ADD] Embedding normalized, dim={len(embedding_list)}, final_norm={np.linalg.norm(normalized):.6f}")
            
            # Create embedding record
            embedding_record = IdentityEmbedding(
                identity_id=uuid_module.UUID(identity_id),
                detection_id=detection_id,
                pipeline_id=pipeline_id,
                embedding=embedding_list,  # pgvector accepts Python list
                quality=quality_score,
                faiss_index_type=index_type,  # Keep for compatibility
                faiss_id=None,  # Not used with pgvector
                created_at=datetime.utcnow()
            )
            
            db.add(embedding_record)
            await db.flush()
            
            self._stats['embeddings_added'] += 1
            
            logger.info(
                f"[PGVECTOR] [ADD] ✅ Added embedding: id={embedding_record.id} "
                f"identity={identity_id[:8]}... type={index_type} quality={quality_score}"
            )
            
            return embedding_record.id
            
        except Exception as e:
            logger.error(f"[PGVECTOR] [ADD] ❌ Failed to add embedding: {e}", exc_info=True)
            return None
    
    async def remove_embedding(
        self,
        identity_id: str,
        db: AsyncSession,
        index_type: Optional[str] = None
    ) -> int:
        """
        Remove all embeddings for an identity.
        
        Args:
            identity_id: UUID of the identity
            db: Database session
            index_type: Optional filter for 'known' or 'unknown'
            
        Returns:
            Number of embeddings removed
        """
        from db_models import IdentityEmbedding
        import uuid as uuid_module
        
        logger.info(f"[PGVECTOR] [REMOVE] Removing embeddings for identity={identity_id[:8]}... type={index_type}")
        
        try:
            query = select(IdentityEmbedding).where(
                IdentityEmbedding.identity_id == uuid_module.UUID(identity_id)
            )
            
            if index_type:
                query = query.where(IdentityEmbedding.faiss_index_type == index_type)
            
            result = await db.execute(query)
            embeddings = result.scalars().all()
            
            count = len(embeddings)
            for emb in embeddings:
                await db.delete(emb)
            
            await db.flush()
            
            self._stats['embeddings_removed'] += count
            
            logger.info(f"[PGVECTOR] [REMOVE] ✅ Removed {count} embeddings for identity={identity_id[:8]}...")
            return count
            
        except Exception as e:
            logger.error(f"[PGVECTOR] [REMOVE] ❌ Failed to remove embeddings: {e}", exc_info=True)
            return 0
    
    # =========================================================================
    # CLUSTERING SUPPORT
    # =========================================================================
    
    async def calculate_similarity(
        self,
        identity_id_1: str,
        identity_id_2: str,
        db: AsyncSession
    ) -> Tuple[bool, float]:
        """
        Calculate similarity between two identities using their best embeddings.
        Used by clustering service for merge suggestions.
        
        Args:
            identity_id_1: First identity UUID
            identity_id_2: Second identity UUID
            db: Database session
            
        Returns:
            Tuple of (is_similar, similarity_score)
        """
        logger.debug(
            f"[PGVECTOR] [SIMILARITY] Calculating similarity: "
            f"{identity_id_1[:8]}... vs {identity_id_2[:8]}..."
        )
        
        try:
            # Use best quality embedding from each identity
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
                        identity_id IN (:id1::uuid, :id2::uuid)
                        AND embedding IS NOT NULL
                )
                SELECT 
                    1 - (e1.embedding <=> e2.embedding) as similarity,
                    e1.quality as quality1,
                    e2.quality as quality2
                FROM best_embeddings e1
                CROSS JOIN best_embeddings e2
                WHERE 
                    e1.identity_id = :id1::uuid
                    AND e2.identity_id = :id2::uuid
                    AND e1.rn = 1 
                    AND e2.rn = 1
            """)
            
            result = await db.execute(
                query,
                {
                    "id1": identity_id_1,
                    "id2": identity_id_2
                }
            )
            
            row = result.fetchone()
            
            if not row or row[0] is None:
                logger.debug(
                    f"[PGVECTOR] [SIMILARITY] No embeddings found for comparison: "
                    f"{identity_id_1[:8]}... vs {identity_id_2[:8]}..."
                )
                return False, 0.0
            
            similarity, quality1, quality2 = row
            similarity = float(similarity)
            
            # Default threshold for clustering
            threshold = 0.35
            is_similar = similarity >= threshold
            
            logger.debug(
                f"[PGVECTOR] [SIMILARITY] Result: {identity_id_1[:8]}... vs {identity_id_2[:8]}... "
                f"similarity={similarity:.4f} (threshold={threshold}) "
                f"q1={quality1} q2={quality2} → {'✅ SIMILAR' if is_similar else '❌ NOT SIMILAR'}"
            )
            
            return is_similar, similarity
            
        except Exception as e:
            logger.error(f"[PGVECTOR] [SIMILARITY] ❌ Error calculating similarity: {e}", exc_info=True)
            return False, 0.0
    
    async def calculate_batch_similarities(
        self,
        identity_ids: List[str],
        db: AsyncSession,
        threshold: float = 0.35
    ) -> Dict[Tuple[str, str], float]:
        """
        Calculate similarities for all pairs of identities at once.
        Much more efficient for clustering than checking pairs individually.
        
        Args:
            identity_ids: List of identity UUIDs
            db: Database session
            threshold: Minimum similarity to include in results
            
        Returns:
            Dict mapping (id1, id2) tuples to similarity scores
        """
        logger.info(
            f"[PGVECTOR] [BATCH_SIMILARITY] Calculating similarities for {len(identity_ids)} identities "
            f"(threshold={threshold})"
        )
        
        if len(identity_ids) < 2:
            logger.debug("[PGVECTOR] [BATCH_SIMILARITY] Less than 2 identities, returning empty")
            return {}
        
        try:
            import time
            start_time = time.time()
            
            # Get best embedding per identity, then calculate all pair similarities
            query = text("""
                WITH best_embeddings AS (
                    SELECT DISTINCT ON (identity_id)
                        identity_id,
                        embedding,
                        quality
                    FROM identity_embeddings
                    WHERE 
                        identity_id = ANY(:identity_ids::uuid[])
                        AND embedding IS NOT NULL
                    ORDER BY identity_id, quality DESC NULLS LAST, created_at DESC
                )
                SELECT 
                    e1.identity_id::text as id1,
                    e2.identity_id::text as id2,
                    1 - (e1.embedding <=> e2.embedding) as similarity
                FROM best_embeddings e1
                CROSS JOIN best_embeddings e2
                WHERE 
                    e1.identity_id < e2.identity_id
                    AND 1 - (e1.embedding <=> e2.embedding) >= :threshold
                ORDER BY similarity DESC
            """)
            
            result = await db.execute(
                query,
                {
                    "identity_ids": identity_ids,
                    "threshold": threshold
                }
            )
            
            similarities = {}
            rows = result.fetchall()
            
            for row in rows:
                id1, id2, sim = row
                similarities[(id1, id2)] = float(sim)
            
            elapsed_ms = (time.time() - start_time) * 1000
            
            logger.info(
                f"[PGVECTOR] [BATCH_SIMILARITY] ✅ Found {len(similarities)} similar pairs "
                f"above threshold {threshold} in {elapsed_ms:.2f}ms"
            )
            
            # Log top 5 similarities for debugging
            if similarities:
                top_5 = sorted(similarities.items(), key=lambda x: x[1], reverse=True)[:5]
                for (id1, id2), sim in top_5:
                    logger.debug(f"[PGVECTOR] [BATCH_SIMILARITY]   Top: {id1[:8]}... ↔ {id2[:8]}... = {sim:.4f}")
            
            return similarities
            
        except Exception as e:
            logger.error(f"[PGVECTOR] [BATCH_SIMILARITY] ❌ Error: {e}", exc_info=True)
            return {}
    
    # =========================================================================
    # STATISTICS & MONITORING
    # =========================================================================
    
    async def get_stats(self, db: AsyncSession) -> Dict[str, Any]:
        """
        Get statistics about the pgvector indexes and data.
        
        Args:
            db: Database session
            
        Returns:
            Dictionary with statistics
        """
        logger.debug("[PGVECTOR] [STATS] Fetching statistics...")
        
        try:
            # Count embeddings by type
            known_query = text("""
                SELECT COUNT(*) FROM identity_embeddings ie
                JOIN identities i ON ie.identity_id = i.id
                WHERE ie.embedding IS NOT NULL AND i.type::text = 'KNOWN'
            """)
            unknown_query = text("""
                SELECT COUNT(*) FROM identity_embeddings ie
                JOIN identities i ON ie.identity_id = i.id
                WHERE ie.embedding IS NOT NULL AND i.type::text = 'UNKNOWN'
            """)
            total_query = text("""
                SELECT COUNT(*) FROM identity_embeddings 
                WHERE embedding IS NOT NULL
            """)
            null_query = text("""
                SELECT COUNT(*) FROM identity_embeddings 
                WHERE embedding IS NULL
            """)
            
            known_result = await db.execute(known_query)
            unknown_result = await db.execute(unknown_query)
            total_result = await db.execute(total_query)
            null_result = await db.execute(null_query)
            
            known_count = known_result.scalar() or 0
            unknown_count = unknown_result.scalar() or 0
            total_count = total_result.scalar() or 0
            null_count = null_result.scalar() or 0
            
            # Get index info
            index_query = text("""
                SELECT indexname, indexdef 
                FROM pg_indexes 
                WHERE tablename = 'identity_embeddings' 
                AND indexname LIKE '%embedding%'
            """)
            index_result = await db.execute(index_query)
            indexes = [{'name': row[0], 'definition': row[1]} for row in index_result.fetchall()]
            
            stats = {
                'backend': 'pgvector',
                'index_type': self.index_type,
                'embedding_size': self.embedding_size,
                'known_count': known_count,
                'unknown_count': unknown_count,
                'total_embeddings': total_count,
                'null_embeddings': null_count,
                'indexes': indexes,
                'searches_known': self._stats['searches_known'],
                'searches_unknown': self._stats['searches_unknown'],
                'embeddings_added': self._stats['embeddings_added'],
                'embeddings_removed': self._stats['embeddings_removed'],
                'last_search_time_ms': self._stats['last_search_time_ms'],
                'avg_search_time_ms': self._stats['avg_search_time_ms'],
            }
            
            logger.info(
                f"[PGVECTOR] [STATS] "
                f"known={known_count} unknown={unknown_count} total={total_count} "
                f"null={null_count} indexes={len(indexes)}"
            )
            
            return stats
            
        except Exception as e:
            logger.error(f"[PGVECTOR] [STATS] ❌ Error getting stats: {e}", exc_info=True)
            return {
                'backend': 'pgvector',
                'error': str(e)
            }
    
    async def health_check(self, db: AsyncSession) -> Dict[str, Any]:
        """
        Perform health check on pgvector.
        
        Args:
            db: Database session
            
        Returns:
            Health check results
        """
        logger.debug("[PGVECTOR] [HEALTH] Running health check...")
        
        results = {
            'healthy': False,
            'pgvector_extension': False,
            'vector_index_exists': False,
            'can_search': False,
            'embedding_count': 0,
        }
        
        try:
            # Check pgvector extension
            ext_check = await db.execute(text("""
                SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')
            """))
            results['pgvector_extension'] = ext_check.scalar()
            
            if not results['pgvector_extension']:
                logger.error("[PGVECTOR] [HEALTH] ❌ pgvector extension not installed")
                return results
            
            # Check index exists
            index_check = await db.execute(text("""
                SELECT COUNT(*) FROM pg_indexes 
                WHERE tablename = 'identity_embeddings' 
                AND indexname LIKE '%embedding%'
            """))
            results['vector_index_exists'] = (index_check.scalar() or 0) > 0
            
            # Check can search (with dummy embedding)
            try:
                dummy_embedding = [0.0] * self.embedding_size
                search_check = await db.execute(
                    text("""
                        SELECT 1 FROM identity_embeddings 
                        WHERE embedding IS NOT NULL 
                        ORDER BY embedding <=> :emb::vector 
                        LIMIT 1
                    """),
                    {"emb": str(dummy_embedding)}
                )
                results['can_search'] = True
            except Exception as e:
                logger.warning(f"[PGVECTOR] [HEALTH] Search test failed: {e}")
                results['can_search'] = False
            
            # Count embeddings
            count_check = await db.execute(text("""
                SELECT COUNT(*) FROM identity_embeddings WHERE embedding IS NOT NULL
            """))
            results['embedding_count'] = count_check.scalar() or 0
            
            # Overall health
            results['healthy'] = (
                results['pgvector_extension'] and 
                results['can_search']
            )
            
            status = "✅ HEALTHY" if results['healthy'] else "❌ UNHEALTHY"
            logger.info(f"[PGVECTOR] [HEALTH] {status}: {results}")
            
            return results
            
        except Exception as e:
            logger.error(f"[PGVECTOR] [HEALTH] ❌ Health check error: {e}", exc_info=True)
            results['error'] = str(e)
            return results


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

# Create global instance (will be initialized on import)
identity_index_pgvector: Optional[IdentityIndexPgVector] = None

def get_pgvector_index() -> Optional[IdentityIndexPgVector]:
    """Get or create the pgvector index instance."""
    global identity_index_pgvector
    
    if identity_index_pgvector is None:
        # Check if pgvector backend is enabled
        backend = getattr(settings, 'VECTOR_BACKEND', 'faiss').lower()
        if backend == 'pgvector':
            identity_index_pgvector = IdentityIndexPgVector()
            logger.info("[PGVECTOR] Created global IdentityIndexPgVector instance")
        else:
            logger.debug(f"[PGVECTOR] pgvector not enabled (VECTOR_BACKEND={backend})")
    
    return identity_index_pgvector


# Initialize on import if pgvector is the configured backend
_backend = getattr(settings, 'VECTOR_BACKEND', 'faiss').lower() if settings else 'faiss'
if _backend == 'pgvector':
    identity_index_pgvector = IdentityIndexPgVector()
    logger.info("[PGVECTOR] ✅ pgvector backend initialized on module load")

