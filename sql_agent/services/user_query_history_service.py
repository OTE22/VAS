"""
User Query History and Memory Service
======================================
Production-ready service for managing user query history, sessions, and memory.
Integrates with the database tables for persistent storage and retrieval.
"""

import logging
import asyncio
import threading
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy import select, func, and_, or_, desc, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# Import database models
from db_models import (
    UserQueryHistory,
    UserConversationSession,
    UserConversationMemory,
    UserQueryEmbedding,
    MemoryType
)

logger = logging.getLogger(__name__)


class UserQueryHistoryService:
    """
    Service for managing user query history, sessions, and memory.
    Production-ready with error handling, async operations, and optimization.
    """

    # Process-wide guard for the sentence-transformer load. Class-level on
    # purpose: the first burst of embedding calls (history save + similar-query
    # search) races the initial load, and per-instance state would still let
    # each racer build its own ~90 MB model.
    _embedding_load_lock = threading.Lock()
    _shared_embedding_model = None

    def __init__(self):
        """Initialize the service."""
        self.logger = logger
    
    # =====================================================
    # QUERY HISTORY MANAGEMENT
    # =====================================================
    
    async def save_query_history(
        self,
        db: AsyncSession,
        user_id: int,
        query_text: str,
        response_text: Optional[str],
        session_id: Optional[str] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        processing_time_ms: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> UserQueryHistory:
        """
        Save a user query and response to history.
        
        Args:
            db: Database session
            user_id: User ID
            query_text: The user's query
            response_text: The AI agent's response
            session_id: Optional session ID
            success: Whether the query was successful
            error_message: Error message if failed
            processing_time_ms: Processing time in milliseconds
            metadata: Additional metadata (SQL, tables queried, etc.)
            
        Returns:
            UserQueryHistory instance
        """
        self.logger.info(f"[QUERY_HISTORY] 💾 Starting to save query to database (user_id={user_id}, session_id={session_id})")
        self.logger.debug("[QUERY_HISTORY] Query received (chars=%d)",
                          len(query_text or ""))
        self.logger.debug(f"[QUERY_HISTORY] Response length: {len(response_text) if response_text else 0} chars")
        self.logger.debug(f"[QUERY_HISTORY] Success: {success}, Processing time: {processing_time_ms}ms")
        
        try:
            # Step 1: Prepare timestamps
            self.logger.debug("[QUERY_HISTORY] Step 1: Preparing timestamps...")
            query_timestamp = datetime.utcnow()
            response_timestamp = query_timestamp if response_text else None
            self.logger.debug(f"[QUERY_HISTORY] ✅ Step 1: Timestamps prepared (query: {query_timestamp}, response: {response_timestamp})")
            
            # Step 2: Create query history record
            self.logger.info("[QUERY_HISTORY] Step 2: Creating UserQueryHistory record...")
            query_history = UserQueryHistory(
                user_id=user_id,
                session_id=session_id,
                query_text=query_text,
                response_text=response_text,
                query_timestamp=query_timestamp,
                response_timestamp=response_timestamp,
                query_metadata=metadata or {},
                success=success,
                error_message=error_message,
                processing_time_ms=processing_time_ms
            )
            self.logger.debug(f"[QUERY_HISTORY] ✅ Step 2: Record object created")
            self.logger.debug(f"[QUERY_HISTORY] Record details: user_id={user_id}, session_id={session_id}, success={success}")
            if metadata:
                self.logger.debug(f"[QUERY_HISTORY] Metadata keys: {list(metadata.keys())}")
            
            # Step 3: Add to database session
            self.logger.info("[QUERY_HISTORY] Step 3: Adding record to database session...")
            db.add(query_history)
            self.logger.debug("[QUERY_HISTORY] ✅ Step 3: Record added to session")
            
            # Step 4: Flush to get the ID
            self.logger.info("[QUERY_HISTORY] Step 4: Flushing to database to get record ID...")
            await db.flush()  # Flush to get the ID
            record_id = query_history.id
            self.logger.info(f"[QUERY_HISTORY] ✅ Step 4: Record flushed successfully (ID: {record_id})")
            
            # Step 5: Update session query count if session_id provided
            if session_id:
                self.logger.debug(f"[QUERY_HISTORY] Step 5: Updating session query count for session_id={session_id}...")
                await self._update_session_query_count(db, user_id, session_id)
                self.logger.debug("[QUERY_HISTORY] ✅ Step 5: Session query count updated")
            else:
                self.logger.debug("[QUERY_HISTORY] Step 5: Skipped (no session_id provided)")
            
            self.logger.info(f"[QUERY_HISTORY] ✅ Query history saved successfully to database!")
            self.logger.info(f"[QUERY_HISTORY] 📊 Summary: ID={record_id}, user_id={user_id}, session_id={session_id}, success={success}, response_length={len(response_text) if response_text else 0}")
            return query_history
            
        except Exception as e:
            self.logger.error(f"[QUERY_HISTORY] ❌ Error saving query history to database: {e}", exc_info=True)
            self.logger.error(f"[QUERY_HISTORY] Failed data: user_id={user_id}, session_id={session_id}, query_length={len(query_text)}, response_length={len(response_text) if response_text else 0}")
            raise
    
    async def get_user_query_history(
        self,
        db: AsyncSession,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
        session_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> List[UserQueryHistory]:
        """
        Get user's query history with filters.
        
        Args:
            db: Database session
            user_id: User ID
            limit: Maximum number of results
            offset: Offset for pagination
            session_id: Optional session filter
            start_date: Optional start date filter
            end_date: Optional end date filter
            
        Returns:
            List of UserQueryHistory records
        """
        try:
            query = select(UserQueryHistory).where(
                UserQueryHistory.user_id == user_id
            )
            
            if session_id:
                query = query.where(UserQueryHistory.session_id == session_id)
            
            if start_date:
                query = query.where(UserQueryHistory.query_timestamp >= start_date)
            
            if end_date:
                query = query.where(UserQueryHistory.query_timestamp <= end_date)
            
            query = query.order_by(desc(UserQueryHistory.query_timestamp))
            query = query.limit(limit).offset(offset)
            
            result = await db.execute(query)
            return result.scalars().all()
            
        except Exception as e:
            self.logger.error(f"[QUERY_HISTORY] Error getting query history: {e}", exc_info=True)
            return []
    
    async def get_query_by_id_for_user(
        self,
        db: AsyncSession,
        query_id: int,
        user_id: int
    ) -> Optional[UserQueryHistory]:
        """One history row, ALWAYS scoped to its owner.

        The ownership filter is not conditional and `user_id` has no default,
        so it cannot be forgotten. The previous accessor gated the filter on
        `if user_id:`, which meant `None` — and `0` — returned any user's
        row: the safe behaviour was the caller's responsibility and the
        dangerous one was the default.

        A falsy id returns None, the SAME answer as a row that does not
        exist. A caller must not be able to distinguish "not yours" from "not
        there": that difference is itself a disclosure about other users'
        queries, which here are surveillance questions about named people.

        This mirrors `delete_query` below, which has always had this contract.
        """
        if not user_id:
            self.logger.warning(
                "[QUERY_HISTORY] refused an unscoped lookup of query %s", query_id)
            return None
        try:
            query = select(UserQueryHistory).where(
                and_(
                    UserQueryHistory.id == query_id,
                    UserQueryHistory.user_id == user_id
                )
            )
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            self.logger.error(f"[QUERY_HISTORY] Error getting query by ID: {e}", exc_info=True)
            return None

    async def get_query_by_id(
        self,
        db: AsyncSession,
        query_id: int,
        user_id: int
    ) -> Optional[UserQueryHistory]:
        """Deprecated name for `get_query_by_id_for_user`.

        Kept so existing callers keep working, but `user_id` is now REQUIRED:
        omitting it used to silently drop the ownership filter, and a caller
        that forgets is now a TypeError at the call site instead of a quiet
        cross-user read.
        """
        return await self.get_query_by_id_for_user(
            db=db, query_id=query_id, user_id=user_id)

    async def delete_query(
        self,
        db: AsyncSession,
        query_id: int,
        user_id: int
    ) -> bool:
        """Delete a query history entry. Only deletes entries owned by user_id."""
        try:
            query = select(UserQueryHistory).where(
                and_(
                    UserQueryHistory.id == query_id,
                    UserQueryHistory.user_id == user_id
                )
            )
            result = await db.execute(query)
            entry = result.scalar_one_or_none()
            if not entry:
                return False

            await db.delete(entry)
            await db.commit()
            self.logger.info(f"[QUERY_HISTORY] Deleted query {query_id} for user {user_id}")
            return True
        except Exception as e:
            await db.rollback()
            self.logger.error(f"[QUERY_HISTORY] Error deleting query: {e}", exc_info=True)
            return False

    # =====================================================
    # SESSION MANAGEMENT
    # =====================================================
    
    async def get_or_create_session(
        self,
        db: AsyncSession,
        user_id: int,
        session_id: Optional[str] = None,
        session_name: Optional[str] = None
    ) -> UserConversationSession:
        """
        Get existing session or create a new one.
        
        Args:
            db: Database session
            user_id: User ID
            session_id: Optional session ID (if None, generates new)
            session_name: Optional session name
            
        Returns:
            UserConversationSession instance
        """
        try:
            if session_id:
                # Try to get existing session
                result = await db.execute(
                    select(UserConversationSession).where(
                        and_(
                            UserConversationSession.user_id == user_id,
                            UserConversationSession.session_id == session_id
                        )
                    )
                )
                session = result.scalar_one_or_none()
                
                if session:
                    # Update last activity
                    session.last_activity_at = datetime.utcnow()
                    session.is_active = True
                    await db.flush()
                    return session
            
            # Create new session
            if not session_id:
                session_id = f"user_{user_id}_session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            
            session = UserConversationSession(
                user_id=user_id,
                session_id=session_id,
                session_name=session_name or f"Session {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
                started_at=datetime.utcnow(),
                last_activity_at=datetime.utcnow(),
                is_active=True,
                query_count=0
            )
            
            db.add(session)
            await db.flush()
            
            self.logger.debug(f"[SESSION] Created session {session_id} for user {user_id}")
            return session
            
        except Exception as e:
            self.logger.error(f"[SESSION] Error getting/creating session: {e}", exc_info=True)
            raise
    
    async def update_session(
        self,
        db: AsyncSession,
        user_id: int,
        session_id: str,
        context_summary: Optional[str] = None,
        is_active: Optional[bool] = None
    ) -> Optional[UserConversationSession]:
        """Update session properties."""
        try:
            result = await db.execute(
                select(UserConversationSession).where(
                    and_(
                        UserConversationSession.user_id == user_id,
                        UserConversationSession.session_id == session_id
                    )
                )
            )
            session = result.scalar_one_or_none()
            
            if session:
                if context_summary is not None:
                    session.context_summary = context_summary
                if is_active is not None:
                    session.is_active = is_active
                session.last_activity_at = datetime.utcnow()
                await db.flush()
            
            return session
            
        except Exception as e:
            self.logger.error(f"[SESSION] Error updating session: {e}", exc_info=True)
            return None
    
    async def get_user_sessions(
        self,
        db: AsyncSession,
        user_id: int,
        active_only: bool = False,
        limit: int = 20
    ) -> List[UserConversationSession]:
        """Get user's sessions."""
        try:
            query = select(UserConversationSession).where(
                UserConversationSession.user_id == user_id
            )
            
            if active_only:
                query = query.where(UserConversationSession.is_active == True)
            
            query = query.order_by(desc(UserConversationSession.last_activity_at))
            query = query.limit(limit)
            
            result = await db.execute(query)
            return result.scalars().all()
            
        except Exception as e:
            self.logger.error(f"[SESSION] Error getting user sessions: {e}", exc_info=True)
            return []
    
    async def _update_session_query_count(
        self,
        db: AsyncSession,
        user_id: int,
        session_id: str
    ):
        """Update session query count."""
        try:
            self.logger.debug(f"[SESSION] Updating query count for session (user_id={user_id}, session_id={session_id})...")
            result = await db.execute(
                select(UserConversationSession).where(
                    and_(
                        UserConversationSession.user_id == user_id,
                        UserConversationSession.session_id == session_id
                    )
                )
            )
            session = result.scalar_one_or_none()
            
            if session:
                # Count queries in this session
                count_result = await db.execute(
                    select(func.count(UserQueryHistory.id)).where(
                        and_(
                            UserQueryHistory.user_id == user_id,
                            UserQueryHistory.session_id == session_id
                        )
                    )
                )
                new_count = count_result.scalar() or 0
                session.query_count = new_count
                session.last_activity_at = datetime.utcnow()
                await db.flush()
                self.logger.debug(f"[SESSION] ✅ Session query count updated: {new_count} queries")
            else:
                self.logger.warning(f"[SESSION] ⚠️ Session not found for query count update (user_id={user_id}, session_id={session_id})")
                
        except Exception as e:
            self.logger.warning(f"[SESSION] ❌ Error updating query count: {e}", exc_info=True)
    
    # =====================================================
    # MEMORY MANAGEMENT
    # =====================================================
    
    async def save_memory(
        self,
        db: AsyncSession,
        user_id: int,
        memory_type: MemoryType,
        memory_key: str,
        memory_value: Dict[str, Any],
        importance_score: int = 50,
        source_session_id: Optional[str] = None,
        source_query_id: Optional[int] = None,
        expires_at: Optional[datetime] = None
    ) -> UserConversationMemory:
        """
        Save a memory for a user.
        
        Args:
            db: Database session
            user_id: User ID
            memory_type: Type of memory (fact, preference, context, pattern)
            memory_key: Key identifier for the memory
            memory_value: The memory content (JSON)
            importance_score: Importance score 0-100
            source_session_id: Session where memory was created
            source_query_id: Query that created this memory
            expires_at: Optional expiration date
            
        Returns:
            UserConversationMemory instance
        """
        try:
            # Check if memory with same key exists
            existing = await db.execute(
                select(UserConversationMemory).where(
                    and_(
                        UserConversationMemory.user_id == user_id,
                        UserConversationMemory.memory_key == memory_key
                    )
                )
            )
            existing_memory = existing.scalar_one_or_none()
            
            if existing_memory:
                # Update existing memory
                existing_memory.memory_value = memory_value
                existing_memory.importance_score = importance_score
                existing_memory.last_accessed_at = datetime.utcnow()
                existing_memory.access_count += 1
                if expires_at:
                    existing_memory.expires_at = expires_at
                await db.flush()
                return existing_memory
            else:
                # Create new memory
                memory = UserConversationMemory(
                    user_id=user_id,
                    memory_type=memory_type,
                    memory_key=memory_key,
                    memory_value=memory_value,
                    importance_score=importance_score,
                    source_session_id=source_session_id,
                    source_query_id=source_query_id,
                    expires_at=expires_at
                )
                db.add(memory)
                await db.flush()
                return memory
                
        except Exception as e:
            self.logger.error(f"[MEMORY] Error saving memory: {e}", exc_info=True)
            raise
    
    async def get_user_memories(
        self,
        db: AsyncSession,
        user_id: int,
        memory_type: Optional[MemoryType] = None,
        min_importance: int = 0,
        include_expired: bool = False
    ) -> List[UserConversationMemory]:
        """
        Get user's memories with filters.
        
        Args:
            db: Database session
            user_id: User ID
            memory_type: Optional memory type filter
            min_importance: Minimum importance score
            include_expired: Whether to include expired memories
            
        Returns:
            List of UserConversationMemory records
        """
        try:
            query = select(UserConversationMemory).where(
                UserConversationMemory.user_id == user_id
            )
            
            if memory_type:
                query = query.where(UserConversationMemory.memory_type == memory_type)
            
            query = query.where(UserConversationMemory.importance_score >= min_importance)
            
            if not include_expired:
                query = query.where(
                    or_(
                        UserConversationMemory.expires_at.is_(None),
                        UserConversationMemory.expires_at > datetime.utcnow()
                    )
                )
            
            query = query.order_by(
                desc(UserConversationMemory.importance_score),
                desc(UserConversationMemory.last_accessed_at)
            )
            
            result = await db.execute(query)
            return result.scalars().all()
            
        except Exception as e:
            self.logger.error(f"[MEMORY] Error getting memories: {e}", exc_info=True)
            return []
    
    async def update_memory_access(
        self,
        db: AsyncSession,
        memory_id: int
    ) -> Optional[UserConversationMemory]:
        """Update memory access timestamp and count."""
        try:
            result = await db.execute(
                select(UserConversationMemory).where(UserConversationMemory.id == memory_id)
            )
            memory = result.scalar_one_or_none()
            
            if memory:
                memory.last_accessed_at = datetime.utcnow()
                memory.access_count += 1
                await db.flush()
            
            return memory
            
        except Exception as e:
            self.logger.error(f"[MEMORY] Error updating memory access: {e}", exc_info=True)
            return None
    
    async def delete_memory(
        self,
        db: AsyncSession,
        user_id: int,
        memory_id: int
    ) -> bool:
        """Delete a memory."""
        try:
            result = await db.execute(
                select(UserConversationMemory).where(
                    and_(
                        UserConversationMemory.id == memory_id,
                        UserConversationMemory.user_id == user_id
                    )
                )
            )
            memory = result.scalar_one_or_none()
            
            if memory:
                await db.delete(memory)
                await db.flush()
                return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"[MEMORY] Error deleting memory: {e}", exc_info=True)
            return False
    
    # =====================================================
    # EMBEDDING MANAGEMENT
    # =====================================================
    
    async def generate_query_embedding(
        self,
        query_text: str,
        embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    ) -> Optional[List[float]]:
        """
        Generate embedding for a query text.
        
        Args:
            query_text: The query text to embed
            embedding_model: Model to use for embedding
            
        Returns:
            Embedding vector or None if generation fails
        """
        self.logger.info("[EMBEDDING] Starting automatic embedding generation "
                         "(query_chars=%d)", len(query_text or ""))
        
        try:
            # Step 1: import sentence-transformers — IN AN EXECUTOR.
            #
            # `import sentence_transformers` pulls in torch and transformers:
            # seconds of synchronous CPU work the first time. Doing it here on
            # the loop froze the whole process for a measured 14.02s (the
            # loop-lag watchdog caught it), stalling every other request
            # including /health/live. The model load and the encode below were
            # already off-loop; this import was the one that was missed.
            #
            # Python caches modules, so on every later turn this executor hop
            # returns immediately.
            self.logger.debug("[EMBEDDING] Step 1: Checking if sentence-transformers is available...")
            loop = asyncio.get_running_loop()

            def _import_sentence_transformers():
                from sentence_transformers import SentenceTransformer
                return SentenceTransformer

            try:
                SentenceTransformer = await loop.run_in_executor(
                    None, _import_sentence_transformers)
                self.logger.info("[EMBEDDING] ✅ Step 1: sentence-transformers library imported successfully")
            except ImportError:
                self.logger.warning("[EMBEDDING] ❌ Step 1: sentence-transformers not available, embeddings will not be generated")
                self.logger.warning("[EMBEDDING] 💡 Install with: pip install sentence-transformers")
                return None
            
            # Step 2: Load model (cache it for performance)
            #
            # LOCAL FIRST. A plain SentenceTransformer(name) call asks the HF
            # Hub to revalidate every file even when the model is fully cached
            # — a dozen HEAD/GET requests per process start, and a hard
            # dependency on huggingface.co being reachable. With the cache
            # populated (HF_HOME is a named volume), local_files_only loads
            # with ZERO network traffic; the online path below runs only the
            # one time the cache is actually empty.
            #
            # Both loading and encoding are CPU-bound and used to run directly
            # on the event loop, freezing every other request for ~10s on
            # first use. Off-loop, like bcrypt and the ONNX sessions.
            self.logger.debug(f"[EMBEDDING] Step 2: Checking if model '{embedding_model}' is loaded...")
            if not hasattr(self, '_embedding_model') or self._embedding_model is None:
                def _load():
                    # Double-checked under a process-wide lock: several callers
                    # (history save, similar-query search) race the FIRST use,
                    # and without this each ran its own load — the same model
                    # constructed three or four times back to back, visible as
                    # repeated tqdm "Loading weights" bars in the logs.
                    with UserQueryHistoryService._embedding_load_lock:
                        cached = UserQueryHistoryService._shared_embedding_model
                        if cached is not None:
                            return cached
                        model = _load_locked()
                        UserQueryHistoryService._shared_embedding_model = model
                        return model

                def _load_locked():
                    try:
                        model = SentenceTransformer(embedding_model, local_files_only=True)
                        self.logger.info(
                            f"[EMBEDDING] ✅ Step 2: '{embedding_model}' loaded from the "
                            f"LOCAL cache (no network)")
                        return model
                    except Exception:
                        self.logger.info(
                            f"[EMBEDDING] Step 2: '{embedding_model}' not in the local "
                            f"cache — downloading once (persists in the HF_HOME volume)")
                        return SentenceTransformer(embedding_model)
                try:
                    self._embedding_model = await loop.run_in_executor(None, _load)
                    self.logger.info(f"[EMBEDDING] 📦 Model info: {self._embedding_model.get_sentence_embedding_dimension()} dimensions")
                except Exception as e:
                    self.logger.error(f"[EMBEDDING] ❌ Step 2: Failed to load embedding model: {e}", exc_info=True)
                    self.logger.error(f"[EMBEDDING] 💡 Check: Model download, disk space, internet connection")
                    return None
            else:
                self.logger.debug("[EMBEDDING] ✅ Step 2: Using cached model (already loaded)")

            # Step 3: Generate embedding (CPU-bound -> executor, see above)
            self.logger.debug(f"[EMBEDDING] Step 3: Encoding query text (length: {len(query_text)} chars)...")
            try:
                embedding = await loop.run_in_executor(
                    None,
                    lambda: self._embedding_model.encode(
                        query_text, convert_to_numpy=True, show_progress_bar=False),
                )
                embedding_list = embedding.tolist()
                
                self.logger.info(f"[EMBEDDING] ✅ Step 3: Embedding generated successfully")
                self.logger.info(f"[EMBEDDING] 📊 Embedding stats: dimension={len(embedding_list)}, dtype={type(embedding_list[0]).__name__}")
                self.logger.debug(f"[EMBEDDING] 🎯 Embedding sample (first 5 values): {embedding_list[:5]}")
                
                return embedding_list
            except Exception as e:
                self.logger.error(f"[EMBEDDING] ❌ Step 3: Error during encoding: {e}", exc_info=True)
                return None
            
        except Exception as e:
            self.logger.error(f"[EMBEDDING] ❌ Unexpected error in embedding generation: {e}", exc_info=True)
            return None
    
    async def save_query_embedding(
        self,
        db: AsyncSession,
        query_history_id: int,
        user_id: int,
        embedding: List[float],
        embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
        embedding_dimensions: int = 384
    ) -> Optional[UserQueryEmbedding]:
        """
        Save query embedding for semantic search.
        
        Args:
            db: Database session
            query_history_id: Query history ID
            user_id: User ID
            embedding: Embedding vector
            embedding_model: Model used to generate embedding
            embedding_dimensions: Number of dimensions
            
        Returns:
            UserQueryEmbedding instance or None
        """
        self.logger.info(f"[EMBEDDING] 💾 Saving embedding to database (query_id={query_history_id}, user_id={user_id})")
        
        try:
            # Check if embedding already exists
            self.logger.debug(f"[EMBEDDING] Checking for existing embedding for query_id={query_history_id}...")
            existing = await db.execute(
                select(UserQueryEmbedding).where(
                    UserQueryEmbedding.query_history_id == query_history_id
                )
            )
            existing_embedding = existing.scalar_one_or_none()
            
            if existing_embedding:
                # Update existing
                self.logger.info(f"[EMBEDDING] 🔄 Updating existing embedding (id={existing_embedding.id})")
                existing_embedding.embedding = embedding
                existing_embedding.embedding_model = embedding_model
                existing_embedding.embedding_dimensions = embedding_dimensions
                await db.flush()
                self.logger.info(f"[EMBEDDING] ✅ Embedding updated successfully")
                return existing_embedding
            else:
                # Create new
                self.logger.info(f"[EMBEDDING] ➕ Creating new embedding record")
                query_embedding = UserQueryEmbedding(
                    query_history_id=query_history_id,
                    user_id=user_id,
                    embedding=embedding,
                    embedding_model=embedding_model,
                    embedding_dimensions=embedding_dimensions
                )
                db.add(query_embedding)
                await db.flush()
                self.logger.info(f"[EMBEDDING] ✅ Embedding saved successfully (id={query_embedding.id})")
                return query_embedding
                
        except Exception as e:
            self.logger.error(f"[EMBEDDING] ❌ Error saving embedding: {e}", exc_info=True)
            return None
    
    async def find_similar_queries(
        self,
        db: AsyncSession,
        user_id: int,
        query_embedding: List[float],
        limit: int = 5,
        similarity_threshold: float = 0.7
    ) -> List[UserQueryHistory]:
        """
        Find similar queries using HNSW index with pgvector for fast approximate nearest neighbor search.
        
        Uses pgvector's HNSW index with the cosine distance operator (<=>).

        `<->` is L2, not cosine. It was used here and reported as cosine, so the
        score was a distance presented as a similarity, and the HNSW index —
        built with vector_cosine_ops — could not serve it, making every search a
        sequential scan.
        Falls back to manual cosine similarity calculation if pgvector is not available.
        
        Args:
            db: Database session
            user_id: User ID
            query_embedding: Query embedding vector
            limit: Maximum number of results
            similarity_threshold: Minimum similarity threshold (0-1, cosine similarity)
            
        Returns:
            List of similar UserQueryHistory records
        """
        self.logger.info(f"[SEMANTIC_SEARCH] 🔍 Starting semantic search (user_id={user_id}, limit={limit}, threshold={similarity_threshold})")
        self.logger.debug(f"[SEMANTIC_SEARCH] Query embedding dimension: {len(query_embedding)}")
        
        try:
            from sqlalchemy import text, func
            from db_models import PGVECTOR_AVAILABLE
            
            # Step 1: Check if pgvector is available
            self.logger.debug("[SEMANTIC_SEARCH] Step 1: Checking pgvector availability...")
            use_hnsw = PGVECTOR_AVAILABLE
            
            if use_hnsw:
                self.logger.info("[SEMANTIC_SEARCH] ✅ Step 1: pgvector is available - will use HNSW index")
                
                # Step 2: Convert embedding to pgvector format
                self.logger.debug("[SEMANTIC_SEARCH] Step 2: Converting embedding to pgvector string format...")
                embedding_str = '[' + ','.join(map(str, query_embedding)) + ']'
                self.logger.debug(f"[SEMANTIC_SEARCH] ✅ Step 2: Embedding converted (length: {len(embedding_str)} chars)")
                
                # Step 3: Prepare HNSW query with cosine distance
                self.logger.info("[SEMANTIC_SEARCH] Step 3: Preparing HNSW query with cosine distance operator (<=>)...")
                self.logger.debug("[SEMANTIC_SEARCH] Step 3a: HNSW index will be used automatically if available")
                self.logger.debug("[SEMANTIC_SEARCH] Step 3b: Using cosine distance: similarity = 1 - (embedding <=> query_embedding)")
                
                query_sql = text("""
                    SELECT 
                        qe.query_history_id,
                        1 - (qe.embedding <=> :query_embedding::vector) as similarity
                    FROM user_query_embeddings qe
                    WHERE qe.user_id = :user_id
                      AND qe.embedding IS NOT NULL
                      AND (1 - (qe.embedding <=> :query_embedding::vector))
                          BETWEEN :similarity_threshold AND 1.0
                    ORDER BY qe.embedding <=> :query_embedding::vector
                    LIMIT :limit
                """)
                
                # Step 4: Execute HNSW query
                self.logger.info("[SEMANTIC_SEARCH] Step 4: Executing HNSW query...")
                try:
                    result = await db.execute(
                        query_sql,
                        {
                            "query_embedding": embedding_str,
                            "user_id": user_id,
                            "similarity_threshold": similarity_threshold,
                            "limit": limit
                        }
                    )
                    self.logger.info("[SEMANTIC_SEARCH] ✅ Step 4: HNSW query executed successfully")
                except Exception as query_error:
                    self.logger.error(f"[SEMANTIC_SEARCH] ❌ Step 4: HNSW query failed: {query_error}", exc_info=True)
                    self.logger.warning("[SEMANTIC_SEARCH] ⚠️ Falling back to cosine similarity calculation...")
                    # Fall through to fallback
                    use_hnsw = False
                
                if use_hnsw:
                    rows = result.fetchall()
                    self.logger.info(f"[SEMANTIC_SEARCH] Step 5: HNSW query returned {len(rows)} results")
                    
                    if not rows:
                        self.logger.info("[SEMANTIC_SEARCH] ⚠️ No similar queries found (threshold too high or no embeddings)")
                        return []
                    
                    # Step 6: Extract query history IDs
                    self.logger.debug("[SEMANTIC_SEARCH] Step 6: Extracting query history IDs from results...")
                    query_history_ids = [row[0] for row in rows]
                    similarities = [row[1] for row in rows]
                    self.logger.info(f"[SEMANTIC_SEARCH] ✅ Step 6: Extracted {len(query_history_ids)} query IDs")
                    self.logger.debug(f"[SEMANTIC_SEARCH] Similarity scores: {[f'{s:.3f}' for s in similarities]}")
                    
                    # Step 7: Fetch query history records
                    self.logger.debug("[SEMANTIC_SEARCH] Step 7: Fetching query history records...")
                    result = await db.execute(
                        select(UserQueryHistory).where(
                            UserQueryHistory.id.in_(query_history_ids)
                        )
                    )
                    queries = result.scalars().all()
                    self.logger.info(f"[SEMANTIC_SEARCH] ✅ Step 7: Fetched {len(queries)} query history records")
                    
                    # Step 8: Sort by similarity (maintain order from HNSW search)
                    self.logger.debug("[SEMANTIC_SEARCH] Step 8: Sorting results by similarity...")
                    query_dict = {q.id: q for q in queries}
                    sorted_queries = [query_dict[qid] for qid in query_history_ids if qid in query_dict]
                    
                    self.logger.info(f"[SEMANTIC_SEARCH] ✅ Semantic search completed: Found {len(sorted_queries)} similar queries using HNSW index")
                    return sorted_queries
            
            # Fallback: Use cosine similarity calculation (slower, but works without pgvector)
            self.logger.warning("[SEMANTIC_SEARCH] ⚠️ pgvector not available or HNSW query failed - using fallback cosine similarity")
            self.logger.info("[SEMANTIC_SEARCH] Step 1 (Fallback): Fetching user embeddings from database...")
            
            # Get all embeddings for this user
            result = await db.execute(
                select(UserQueryEmbedding).where(
                    UserQueryEmbedding.user_id == user_id
                ).limit(1000)  # Limit for performance
            )
            embeddings = result.scalars().all()
            self.logger.info(f"[SEMANTIC_SEARCH] ✅ Step 1 (Fallback): Fetched {len(embeddings)} embeddings from database")
            
            if not embeddings:
                self.logger.warning("[SEMANTIC_SEARCH] ⚠️ No embeddings found for user - cannot perform similarity search")
                return []
            
            # Step 2 (Fallback): Calculate cosine similarity
            self.logger.info("[SEMANTIC_SEARCH] Step 2 (Fallback): Calculating cosine similarity for each embedding...")
            similarities = []
            processed_count = 0
            skipped_count = 0
            
            for emb in embeddings:
                if not emb.embedding:
                    skipped_count += 1
                    continue
                
                # Handle both Vector type (pgvector) and JSONB (fallback)
                emb_vector = emb.embedding
                if isinstance(emb_vector, str):
                    # If it's a string representation, parse it
                    try:
                        import json
                        emb_vector = json.loads(emb_vector) if emb_vector.startswith('[') else emb_vector
                    except Exception as parse_error:
                        self.logger.debug(f"[SEMANTIC_SEARCH] Skipping embedding {emb.id}: parse error - {parse_error}")
                        skipped_count += 1
                        continue
                
                try:
                    similarity = self._cosine_similarity(query_embedding, emb_vector)
                    if similarity >= similarity_threshold:
                        similarities.append((similarity, emb.query_history_id))
                        processed_count += 1
                except Exception as e:
                    self.logger.warning(f"[SEMANTIC_SEARCH] Error calculating similarity for embedding {emb.id}: {e}")
                    skipped_count += 1
                    continue
            
            self.logger.info(f"[SEMANTIC_SEARCH] ✅ Step 2 (Fallback): Processed {processed_count} embeddings, skipped {skipped_count}")
            self.logger.info(f"[SEMANTIC_SEARCH] Found {len(similarities)} embeddings above threshold ({similarity_threshold})")
            
            if not similarities:
                self.logger.warning("[SEMANTIC_SEARCH] ⚠️ No similar queries found (threshold too high)")
                return []
            
            # Step 3 (Fallback): Sort by similarity and get top results
            self.logger.debug("[SEMANTIC_SEARCH] Step 3 (Fallback): Sorting by similarity and selecting top results...")
            similarities.sort(key=lambda x: x[0], reverse=True)
            top_similarities = similarities[:limit]
            top_query_ids = [qid for _, qid in top_similarities]
            self.logger.info(f"[SEMANTIC_SEARCH] ✅ Step 3 (Fallback): Selected top {len(top_query_ids)} results")
            self.logger.debug(f"[SEMANTIC_SEARCH] Top similarity scores: {[f'{s[0]:.3f}' for s in top_similarities]}")
            
            # Step 4 (Fallback): Get query history records
            self.logger.debug("[SEMANTIC_SEARCH] Step 4 (Fallback): Fetching query history records...")
            result = await db.execute(
                select(UserQueryHistory).where(
                    UserQueryHistory.id.in_(top_query_ids)
                )
            )
            queries = result.scalars().all()
            self.logger.info(f"[SEMANTIC_SEARCH] ✅ Step 4 (Fallback): Fetched {len(queries)} query history records")
            
            # Step 5 (Fallback): Sort by similarity
            self.logger.debug("[SEMANTIC_SEARCH] Step 5 (Fallback): Sorting results by similarity...")
            query_dict = {q.id: q for q in queries}
            sorted_queries = [query_dict[qid] for qid in top_query_ids if qid in query_dict]
            
            self.logger.info(f"[SEMANTIC_SEARCH] ✅ Fallback cosine similarity search completed: Found {len(sorted_queries)} similar queries")
            return sorted_queries
            
        except Exception as e:
            self.logger.error(f"[SEMANTIC_SEARCH] ❌ Error finding similar queries: {e}", exc_info=True)
            return []
    
    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        try:
            import numpy as np
            vec1 = np.array(vec1)
            vec2 = np.array(vec2)
            dot_product = np.dot(vec1, vec2)
            norm1 = np.linalg.norm(vec1)
            norm2 = np.linalg.norm(vec2)
            if norm1 == 0 or norm2 == 0:
                return 0.0
            return float(dot_product / (norm1 * norm2))
        except ImportError:
            # Fallback without numpy
            if len(vec1) != len(vec2):
                return 0.0
            dot_product = sum(a * b for a, b in zip(vec1, vec2))
            norm1 = sum(a * a for a in vec1) ** 0.5
            norm2 = sum(b * b for b in vec2) ** 0.5
            if norm1 == 0 or norm2 == 0:
                return 0.0
            return dot_product / (norm1 * norm2)
    
    # =====================================================
    # CONTEXT RETRIEVAL FOR AI AGENT
    # =====================================================
    
    async def get_context_for_query(
        self,
        db: AsyncSession,
        user_id: int,
        session_id: Optional[str] = None,
        recent_limit: int = 5,
        memory_limit: int = 10
    ) -> Dict[str, Any]:
        """
        Get context for AI agent query (recent queries + memories).
        
        Args:
            db: Database session
            user_id: User ID
            session_id: Optional session ID
            recent_limit: Number of recent queries to include
            memory_limit: Number of memories to include
            
        Returns:
            Dictionary with context information
        """
        self.logger.info(f"[CONTEXT] 🔍 Retrieving context for user {user_id} (session_id={session_id})")
        try:
            context = {
                "recent_queries": [],
                "memories": [],
                "session_summary": None
            }
            
            # Get recent queries
            self.logger.debug(f"[CONTEXT] Step 1: Fetching recent queries (limit={recent_limit})...")
            recent_queries = await self.get_user_query_history(
                db, user_id, limit=recent_limit, session_id=session_id
            )
            context["recent_queries"] = [
                {
                    "query": q.query_text,
                    "response": q.response_text[:200] if q.response_text else None,
                    "timestamp": q.query_timestamp.isoformat(),
                    "success": q.success
                }
                for q in recent_queries
            ]
            self.logger.info(f"[CONTEXT] ✅ Step 1: Retrieved {len(context['recent_queries'])} recent queries")
            
            # Get relevant memories
            self.logger.debug(f"[CONTEXT] Step 2: Fetching relevant memories (limit={memory_limit})...")
            memories = await self.get_user_memories(
                db, user_id, min_importance=30, include_expired=False
            )
            context["memories"] = [
                {
                    "type": m.memory_type.value,
                    "key": m.memory_key,
                    "value": m.memory_value,
                    "importance": m.importance_score
                }
                for m in memories[:memory_limit]
            ]
            self.logger.info(f"[CONTEXT] ✅ Step 2: Retrieved {len(context['memories'])} memories")
            
            # Get session summary if session_id provided
            if session_id:
                self.logger.debug(f"[CONTEXT] Step 3: Fetching session summary for session_id={session_id}...")
                result = await db.execute(
                    select(UserConversationSession).where(
                        and_(
                            UserConversationSession.user_id == user_id,
                            UserConversationSession.session_id == session_id
                        )
                    )
                )
                session = result.scalar_one_or_none()
                if session:
                    context["session_summary"] = session.context_summary
                    self.logger.info(f"[CONTEXT] ✅ Step 3: Session summary retrieved")
                else:
                    self.logger.debug(f"[CONTEXT] ⚠️ Step 3: Session not found")
            
            self.logger.info(f"[CONTEXT] ✅ Context retrieval completed successfully")
            return context
            
        except Exception as e:
            self.logger.error(f"[CONTEXT] ❌ Error getting context: {e}", exc_info=True)
            return {"recent_queries": [], "memories": [], "session_summary": None}
    
    # =====================================================
    # MEMORY EXTRACTION (from query responses)
    # =====================================================
    
    async def extract_and_save_memories(
        self,
        db: AsyncSession,
        user_id: int,
        query_id: int,
        query_text: str,
        response_text: str,
        session_id: Optional[str] = None
    ):
        """
        Extract important information from query/response and save as memories.
        
        This is a simplified extraction. For production, you might want to use
        LLM-based extraction or more sophisticated NLP.
        
        Args:
            db: Database session
            user_id: User ID
            query_id: Query history ID
            query_text: User's query
            response_text: AI response
            session_id: Session ID
        """
        try:
            # Extract preferences (simplified - look for patterns)
            preferences = []
            
            # Check for date range preferences
            if "last 7 days" in query_text.lower() or "past week" in query_text.lower():
                preferences.append({
                    "type": "preference",
                    "key": "preferred_date_range",
                    "value": {"range": "7_days", "description": "User prefers 7-day date ranges"},
                    "importance": 60
                })
            
            # Check for common query patterns
            if "track" in query_text.lower() or "follow" in query_text.lower():
                preferences.append({
                    "type": "pattern",
                    "key": "query_pattern_tracking",
                    "value": {"pattern": "tracking_queries", "count": 1},
                    "importance": 40
                })
            
            # Save extracted memories
            for pref in preferences:
                memory_type = MemoryType(pref["type"])
                await self.save_memory(
                    db=db,
                    user_id=user_id,
                    memory_type=memory_type,
                    memory_key=pref["key"],
                    memory_value=pref["value"],
                    importance_score=pref["importance"],
                    source_query_id=query_id,
                    source_session_id=session_id
                )
            
            if preferences:
                self.logger.debug(f"[MEMORY] Extracted {len(preferences)} memories from query {query_id}")
                
        except Exception as e:
            self.logger.warning(f"[MEMORY] Error extracting memories: {e}", exc_info=True)
            # Don't fail the main operation if memory extraction fails


# Global service instance
user_query_history_service = UserQueryHistoryService()

