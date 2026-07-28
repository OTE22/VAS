"""
Identity Index Service
======================
Dual FAISS index system for KNOWN and UNKNOWN identities.
"""

import os
import faiss
import numpy as np
import json
import logging
import threading
import asyncio
from typing import Tuple, List, Optional, Dict
from datetime import datetime

# Import central config
try:
    import sys
    parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from config import settings
except ImportError:
    settings = None

# Try to import GPU detection
try:
    from utils.gpu_detection import get_faiss_backend
    USE_GPU_FAISS = get_faiss_backend() == 'gpu'
except ImportError:
    USE_GPU_FAISS = False
except Exception:
    USE_GPU_FAISS = False

logger = logging.getLogger(__name__)


class IdentityIndexService:
    """
    Dual FAISS index system:
    - KNOWN index: Stable, curated known identities
    - UNKNOWN index: Dynamic, merges happen here
    """
    
    def __init__(self, embedding_size: Optional[int] = None, db_path: Optional[str] = None):
        """
        Initialize dual FAISS indexes.
        
        Args:
            embedding_size: Dimension of face embeddings (512 for w600k_r50)
            db_path: Directory to store index files
        """
        # Load from central config if not provided
        self.embedding_size = embedding_size or (settings.IDENTITY_EMBEDDING_SIZE if settings else 512)
        self.db_path = db_path or (settings.IDENTITY_INDEX_DB_PATH if settings else "./database/identity_indexes")
        os.makedirs(self.db_path, exist_ok=True)
        
        # Index file paths
        self.known_index_file = os.path.join(db_path, "known_faiss_index.bin")
        self.unknown_index_file = os.path.join(db_path, "unknown_faiss_index.bin")
        self.known_meta_file = os.path.join(db_path, "known_metadata.json")
        self.unknown_meta_file = os.path.join(db_path, "unknown_metadata.json")
        
        # GPU support
        self.use_gpu = USE_GPU_FAISS
        self.gpu_resource = None
        
        # Thread lock for thread-safe operations
        self.lock = threading.RLock()
        
        # Initialize indexes
        self.known_index = None
        self.unknown_index = None
        
        # Metadata: mapping from FAISS index position to identity_id (UUID)
        # Format: {faiss_id: identity_id}
        self.known_metadata: Dict[int, str] = {}
        self.unknown_metadata: Dict[int, str] = {}
        
        # Reverse mapping: identity_id -> list of faiss_ids (one identity can have multiple embeddings)
        self.known_identity_to_faiss: Dict[str, List[int]] = {}
        self.unknown_identity_to_faiss: Dict[str, List[int]] = {}
        
        # Periodic save settings - load from config
        self.auto_save_enabled = True
        self.auto_save_interval_seconds = (
            settings.IDENTITY_INDEX_AUTO_SAVE_INTERVAL if settings else 300
        )  # Save every 5 minutes by default
        self.last_save_time = datetime.utcnow()
        self._save_task = None
        self._repair_task = None
        self._rebuild_task = None
        self._rebuild_queue = []  # Queue of index types that need rebuilding

        # Monotonic save counter, stamped into BOTH metadata files by each
        # atomic save. Equal versions across the two metas == a consistent
        # snapshot; a mismatch on load means a crash landed between renames
        # (a milliseconds-wide window) and repair should be allowed to run.
        self._save_version = 0

        # Serializes MAINTENANCE cycles (background repair vs. background
        # rebuild). They previously ran with no mutual exclusion: repair could
        # act on stale faiss_ids while a rebuild swapped the underlying index
        # wholesale. asyncio.Lock (not the threading RLock) because both
        # workers are coroutines on the event loop; safe to construct here in
        # Python 3.11 (locks no longer bind an event loop at construction).
        self._maintenance_lock = asyncio.Lock()
        
        # Initialize indexes
        self._initialize_indexes()
        
        # Load existing indexes
        self.load()
    
    def _initialize_indexes(self):
        """Initialize FAISS indexes (CPU or GPU) with configurable index types"""
        # Get index type configuration
        known_index_type = getattr(settings, 'KNOWN_INDEX_TYPE', 'flat').lower()
        unknown_index_type = getattr(settings, 'UNKNOWN_INDEX_TYPE', 'flat').lower()
        
        # Initialize KNOWN index
        cpu_known = self._create_index(known_index_type, 'known')
        
        # Initialize UNKNOWN index (always flat for simplicity)
        cpu_unknown = faiss.IndexFlatIP(self.embedding_size)
        
        if self.use_gpu:
            try:
                self.gpu_resource = faiss.StandardGpuResources()
                self.known_index = faiss.index_cpu_to_gpu(self.gpu_resource, 0, cpu_known)
                self.unknown_index = faiss.index_cpu_to_gpu(self.gpu_resource, 0, cpu_unknown)
                logger.info(f"✅ Using FAISS GPU for identity indexes (KNOWN: {known_index_type}, UNKNOWN: {unknown_index_type})")
            except Exception as e:
                logger.warning(f"⚠️  GPU FAISS not available, falling back to CPU: {e}")
                self.use_gpu = False
                self.gpu_resource = None
                self.known_index = cpu_known
                self.unknown_index = cpu_unknown
        else:
            self.known_index = cpu_known
            self.unknown_index = cpu_unknown
            logger.info(f"ℹ️  Using FAISS CPU for identity indexes (KNOWN: {known_index_type}, UNKNOWN: {unknown_index_type})")
    
    def _create_index(self, index_type: str, index_name: str = 'known'):
        """
        Create FAISS index based on type configuration.
        
        Args:
            index_type: 'flat', 'ivf', 'hnsw', or 'ivfpq'
            index_name: 'known' or 'unknown' (for logging)
            
        Returns:
            FAISS index object
        """
        index_type = index_type.lower()
        
        if index_type == 'flat':
            index = faiss.IndexFlatIP(self.embedding_size)
            logger.info(f"Created {index_name} index: IndexFlatIP (exact search)")
            
        elif index_type == 'ivf':
            # IVF Index with configurable parameters
            quantizer = faiss.IndexFlatIP(self.embedding_size)
            nlist = getattr(settings, 'KNOWN_INDEX_NLIST', 1000)
            index = faiss.IndexIVFFlat(quantizer, self.embedding_size, nlist)
            index.nprobe = getattr(settings, 'KNOWN_INDEX_NPROBE', 20)
            logger.info(f"Created {index_name} index: IndexIVFFlat (nlist={nlist}, nprobe={index.nprobe})")
            logger.warning(f"⚠️  {index_name.upper()} IVF index requires training before use! Call index.train() with training data.")
            
        elif index_type == 'hnsw':
            # HNSW Index
            M = getattr(settings, 'KNOWN_INDEX_HNSW_M', 32)
            index = faiss.IndexHNSWFlat(self.embedding_size, M)
            index.hnsw.efConstruction = getattr(settings, 'KNOWN_INDEX_HNSW_EF_CONSTRUCTION', 200)
            index.hnsw.efSearch = getattr(settings, 'KNOWN_INDEX_HNSW_EF_SEARCH', 64)
            logger.info(f"Created {index_name} index: IndexHNSWFlat (M={M}, efConstruction={index.hnsw.efConstruction}, efSearch={index.hnsw.efSearch})")
            
        elif index_type == 'ivfpq':
            # IVFPQ Index (Product Quantization)
            quantizer = faiss.IndexFlatIP(self.embedding_size)
            nlist = getattr(settings, 'KNOWN_INDEX_NLIST', 1000)
            m = getattr(settings, 'KNOWN_INDEX_PQ_M', 64)
            bits = getattr(settings, 'KNOWN_INDEX_PQ_BITS', 8)
            index = faiss.IndexIVFPQ(quantizer, self.embedding_size, nlist, m, bits)
            index.nprobe = getattr(settings, 'KNOWN_INDEX_NPROBE', 20)
            logger.info(f"Created {index_name} index: IndexIVFPQ (nlist={nlist}, m={m}, bits={bits}, nprobe={index.nprobe})")
            logger.warning(f"⚠️  {index_name.upper()} IVFPQ index requires training before use! Call index.train() with training data.")
            
        else:
            logger.warning(f"Unknown index type '{index_type}', falling back to IndexFlatIP")
            index = faiss.IndexFlatIP(self.embedding_size)
        
        return index
    
    def search_known(self, embedding: np.ndarray, top_k: int = 1, threshold: float = 0.4) -> List[Tuple[str, float]]:
        """
        Search in KNOWN index.
        
        Args:
            embedding: Query embedding (L2-normalized)
            top_k: Number of results to return
            threshold: Minimum similarity threshold
            
        Returns:
            List of (identity_id, similarity_score) tuples
        """
        if self.known_index.ntotal == 0:
            logger.warning(f"[FAISS_SEARCH] KNOWN index is EMPTY! No vectors to search. This means no known faces are loaded.")
            return []
        
        normalized_embedding = embedding / np.linalg.norm(embedding)
        normalized_embedding = normalized_embedding.astype(np.float32).reshape(1, -1)
        
        with self.lock:
            similarities, indices = self.known_index.search(normalized_embedding, top_k)
        
        logger.debug(f"[FAISS_SEARCH] KNOWN index search: top_k={top_k}, threshold={threshold}, index_size={self.known_index.ntotal}")
        logger.debug(f"[FAISS_SEARCH] Raw search results: {len(similarities[0])} results, similarities={similarities[0]}, indices={indices[0]}")
        
        results = []
        for i, (sim, idx) in enumerate(zip(similarities[0], indices[0])):
            logger.debug(f"[FAISS_SEARCH] Checking result {i}: similarity={sim:.4f}, faiss_id={idx}, threshold={threshold}")
            if sim >= threshold and idx >= 0:
                # Skip if not in metadata (orphaned vector - lazy marking approach)
                if idx not in self.known_metadata:
                    logger.debug(f"[FAISS_SEARCH] Skipping orphaned vector (faiss_id={idx}, not in metadata)")
                    continue
                
                identity_id = self.known_metadata.get(idx)
                if identity_id:
                    logger.debug(f"[FAISS_SEARCH] ✅ Match found: identity_id={identity_id}, similarity={sim:.4f}")
                    results.append((identity_id, float(sim)))
                else:
                    logger.warning(f"[FAISS_SEARCH] ⚠️ FAISS ID {idx} found but no metadata entry! This is a data inconsistency.")
            else:
                logger.debug(f"[FAISS_SEARCH] ❌ Result {i} rejected: sim={sim:.4f} < threshold={threshold} or idx={idx} < 0")
        
        if not results:
            best_sim = similarities[0][0] if len(similarities[0]) > 0 else 'N/A'
            logger.debug(f"[FAISS_SEARCH] No matches in KNOWN index above threshold {threshold}. Best similarity was: {best_sim}")
            
            # DEBUG: Log top 3 results even if below threshold
            if len(similarities[0]) > 0:
                logger.debug(f"[FAISS_SEARCH] DEBUG: Top 3 results (may be below threshold):")
                for i in range(min(3, len(similarities[0]))):
                    sim = similarities[0][i]
                    idx = indices[0][i]
                    if idx >= 0 and idx in self.known_metadata:
                        identity_id = self.known_metadata.get(idx, 'N/A')
                        logger.debug(f"[FAISS_SEARCH] DEBUG:   [{i}] similarity={sim:.4f}, faiss_id={idx}, identity_id={identity_id}, threshold={threshold}")
        
        return results
    
    def search_unknown(self, embedding: np.ndarray, top_k: int = 1, threshold: float = 0.35) -> List[Tuple[str, float]]:
        """
        Search in UNKNOWN index (slightly looser threshold).
        
        Args:
            embedding: Query embedding (L2-normalized)
            top_k: Number of results to return
            threshold: Minimum similarity threshold (default 0.35, looser than known)
            
        Returns:
            List of (identity_id, similarity_score) tuples
        """
        if self.unknown_index.ntotal == 0:
            return []
        
        normalized_embedding = embedding / np.linalg.norm(embedding)
        normalized_embedding = normalized_embedding.astype(np.float32).reshape(1, -1)
        
        with self.lock:
            similarities, indices = self.unknown_index.search(normalized_embedding, top_k)
        
        results = []
        for i, (sim, idx) in enumerate(zip(similarities[0], indices[0])):
            if sim >= threshold and idx >= 0:
                # Skip if not in metadata (orphaned vector - lazy marking approach)
                if idx not in self.unknown_metadata:
                    continue
                
                identity_id = self.unknown_metadata.get(idx)
                if identity_id:
                    results.append((identity_id, float(sim)))
        
        return results
    
    def add_known(self, identity_id: str, embedding: np.ndarray) -> int:
        """
        Add embedding to KNOWN index.
        
        Args:
            identity_id: UUID of the identity
            embedding: Face embedding (will be normalized)
            
        Returns:
            FAISS index ID (faiss_id)
        """
        normalized_embedding = embedding / np.linalg.norm(embedding)
        normalized_embedding = normalized_embedding.astype(np.float32).reshape(1, -1)
        
        with self.lock:
            # Check if index needs training (IVF/IVFPQ indexes)
            if hasattr(self.known_index, 'is_trained') and not self.known_index.is_trained:
                raise ValueError(
                    "KNOWN index is not trained! IVF/IVFPQ indexes require training before adding vectors. "
                    "Call train_known_index() with training data first. "
                    f"Current index type: {type(self.known_index).__name__}"
                )
            
            faiss_id = self.known_index.ntotal
            self.known_index.add(normalized_embedding)
            self.known_metadata[faiss_id] = identity_id
            
            # Update reverse mapping
            if identity_id not in self.known_identity_to_faiss:
                self.known_identity_to_faiss[identity_id] = []
            self.known_identity_to_faiss[identity_id].append(faiss_id)
        
        logger.debug(f"Added KNOWN identity {identity_id} to index (faiss_id={faiss_id})")
        return faiss_id
    
    def train_known_index(self, training_embeddings: np.ndarray) -> bool:
        """
        Train the KNOWN index with training data.
        Required for IVF and IVFPQ indexes before adding vectors.
        
        Args:
            training_embeddings: Array of embeddings for training (shape: [n_samples, embedding_size])
            
        Returns:
            True if training successful, False otherwise
        """
        try:
            if not hasattr(self.known_index, 'is_trained'):
                logger.info("Index type doesn't require training (Flat/HNSW)")
                return True
            
            if self.known_index.is_trained:
                logger.info("Index is already trained")
                return True
            
            logger.info(f"Training KNOWN index with {len(training_embeddings)} samples...")
            
            # Normalize training embeddings
            normalized = training_embeddings / np.linalg.norm(training_embeddings, axis=1, keepdims=True)
            normalized = normalized.astype(np.float32)
            
            with self.lock:
                self.known_index.train(normalized)
            
            logger.info("✅ KNOWN index training complete")
            return True
            
        except Exception as e:
            logger.error(f"Failed to train KNOWN index: {e}", exc_info=True)
            return False
    
    def add_unknown(self, identity_id: str, embedding: np.ndarray) -> int:
        """
        Add embedding to UNKNOWN index.
        
        Args:
            identity_id: UUID of the identity
            embedding: Face embedding (will be normalized)
            
        Returns:
            FAISS index ID (faiss_id)
        """
        normalized_embedding = embedding / np.linalg.norm(embedding)
        normalized_embedding = normalized_embedding.astype(np.float32).reshape(1, -1)
        
        with self.lock:
            faiss_id = self.unknown_index.ntotal
            self.unknown_index.add(normalized_embedding)
            self.unknown_metadata[faiss_id] = identity_id
            
            # Update reverse mapping
            if identity_id not in self.unknown_identity_to_faiss:
                self.unknown_identity_to_faiss[identity_id] = []
            self.unknown_identity_to_faiss[identity_id].append(faiss_id)
        
        logger.debug(f"Added UNKNOWN identity {identity_id} to index (faiss_id={faiss_id})")
        return faiss_id
    
    def remove_from_known(self, identity_id: str) -> int:
        """
        Remove all embeddings for an identity from KNOWN index.
        Note: FAISS doesn't support removal, so we mark as removed in metadata.
        For production, consider rebuilding index periodically.
        
        Args:
            identity_id: UUID of the identity to remove
            
        Returns:
            Number of embeddings removed
        """
        with self.lock:
            faiss_ids = self.known_identity_to_faiss.get(identity_id, [])
            removed = 0
            for faiss_id in faiss_ids:
                if faiss_id in self.known_metadata:
                    del self.known_metadata[faiss_id]
                    removed += 1
            
            if identity_id in self.known_identity_to_faiss:
                del self.known_identity_to_faiss[identity_id]
        
        logger.info(f"Removed {removed} embeddings for KNOWN identity {identity_id} (marked in metadata)")
        return removed
    
    def remove_from_unknown(self, identity_id: str) -> int:
        """
        Remove all embeddings for an identity from UNKNOWN index.
        
        Args:
            identity_id: UUID of the identity to remove
            
        Returns:
            Number of embeddings removed
        """
        with self.lock:
            faiss_ids = self.unknown_identity_to_faiss.get(identity_id, [])
            removed = 0
            for faiss_id in faiss_ids:
                if faiss_id in self.unknown_metadata:
                    del self.unknown_metadata[faiss_id]
                    removed += 1
            
            if identity_id in self.unknown_identity_to_faiss:
                del self.unknown_identity_to_faiss[identity_id]
        
        logger.info(f"Removed {removed} embeddings for UNKNOWN identity {identity_id} (marked in metadata)")
        return removed
    
    def move_unknown_to_known(self, identity_id: str) -> int:
        """
        Move identity from UNKNOWN to KNOWN index.
        This is called when an unknown identity is promoted to known.
        
        Args:
            identity_id: UUID of the identity to move
            
        Returns:
            Number of embeddings moved
        """
        with self.lock:
            # Get all embeddings for this identity from UNKNOWN
            faiss_ids = self.unknown_identity_to_faiss.get(identity_id, [])
            if not faiss_ids:
                logger.warning(f"No embeddings found for UNKNOWN identity {identity_id}")
                return 0
            
            # Note: We can't actually move vectors in FAISS, so we need to:
            # 1. Get the embeddings from the index (requires storing them separately or rebuilding)
            # 2. Add them to KNOWN index
            # 3. Remove from UNKNOWN index
            
            # For now, we'll mark them for migration and rebuild on next save
            # In production, you might want to store embeddings separately or rebuild index
            moved = len(faiss_ids)
            logger.info(f"Marked {moved} embeddings for migration from UNKNOWN to KNOWN for identity {identity_id}")
            
            # Remove from UNKNOWN
            self.remove_from_unknown(identity_id)
            
            # Note: The actual vectors need to be re-added to KNOWN index
            # This should be done by re-processing the identity's embeddings from the database
        
        return moved
    
    @staticmethod
    def _available_memory_mb() -> Optional[int]:
        """Available system memory in MB, or None when undeterminable.

        Reads /proc/meminfo directly: psutil is NOT installed in the
        production container (verified — system_metrics already guards on
        PSUTIL_AVAILABLE for the same reason), and the container is Linux
        where /proc is always present. Returns None on non-Linux dev hosts,
        in which case callers proceed without the guard rather than never
        rebuilding.
        """
        try:
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) // 1024  # kB -> MB
        except OSError:
            pass
        return None

    @staticmethod
    def _fsync_file(path: str) -> None:
        """fsync an already-written file by path (faiss.write_index does not)."""
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_artifacts_atomic(self) -> None:
        """Serialize all four artifacts durably, then swap them in atomically.

        The previous implementation wrote all four files IN PLACE: a crash (or
        an OOM kill, or a worker recycle) between the first and the last write
        left the index binaries and their metadata mutually inconsistent — the
        exact corruption the repair worker exists to clean up after. Now every
        artifact goes to a `.tmp` sibling, is fsynced, and only then renamed
        over the final path. The vulnerable window shrinks from the entire
        multi-second serialization to four sequential renames; a crash inside
        THAT window is detectable because both metadata files carry the same
        `save_version` only when they belong to one snapshot.

        Caller must hold self.lock.
        """
        version = self._save_version + 1
        saved_at = datetime.utcnow().isoformat()

        def _index_to_cpu(index):
            return faiss.index_gpu_to_cpu(index) if self.use_gpu else index

        tmp_paths = {
            self.known_index_file: self.known_index_file + ".tmp",
            self.known_meta_file: self.known_meta_file + ".tmp",
            self.unknown_index_file: self.unknown_index_file + ".tmp",
            self.unknown_meta_file: self.unknown_meta_file + ".tmp",
        }
        try:
            faiss.write_index(_index_to_cpu(self.known_index),
                              tmp_paths[self.known_index_file])
            self._fsync_file(tmp_paths[self.known_index_file])

            with open(tmp_paths[self.known_meta_file], 'w', encoding='utf-8') as f:
                json.dump({
                    'save_version': version,
                    'saved_at': saved_at,
                    'metadata': {str(k): v for k, v in self.known_metadata.items()},
                    'identity_to_faiss': {k: v for k, v in self.known_identity_to_faiss.items()}
                }, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            faiss.write_index(_index_to_cpu(self.unknown_index),
                              tmp_paths[self.unknown_index_file])
            self._fsync_file(tmp_paths[self.unknown_index_file])

            with open(tmp_paths[self.unknown_meta_file], 'w', encoding='utf-8') as f:
                json.dump({
                    'save_version': version,
                    'saved_at': saved_at,
                    'metadata': {str(k): v for k, v in self.unknown_metadata.items()},
                    'identity_to_faiss': {k: v for k, v in self.unknown_identity_to_faiss.items()}
                }, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            # All four durable — swap in fixed order.
            for final, tmp in tmp_paths.items():
                os.replace(tmp, final)

            # Best-effort directory fsync so the renames themselves survive a
            # power loss. Directories cannot be opened on Windows dev hosts —
            # skipped there; the Linux container is what production runs.
            try:
                dir_fd = os.open(self.db_path, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass

            self._save_version = version
        except Exception:
            for tmp in tmp_paths.values():
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
            raise

    def save(self):
        """Save both indexes and metadata to disk (atomic, versioned)."""
        try:
            os.makedirs(self.db_path, exist_ok=True)

            with self.lock:
                self._write_artifacts_atomic()
                logger.info(
                    f"✅ Saved identity indexes: KNOWN={self.known_index.ntotal}, "
                    f"UNKNOWN={self.unknown_index.ntotal} (save_version={self._save_version})"
                )
                self.last_save_time = datetime.utcnow()

        except Exception as e:
            logger.error(f"❌ Failed to save identity indexes: {e}", exc_info=True)
            raise

    async def save_async(self, timeout: Optional[float] = None):
        """Save without blocking the event loop.

        Full index serialization takes seconds at scale; calling the sync
        save() from a coroutine froze every request for that long (and was a
        prime suspect in the loop-lag warnings). The threading RLock inside
        save() still guarantees exclusivity against writers on other threads.
        """
        if timeout is not None:
            await asyncio.wait_for(asyncio.to_thread(self.save), timeout=timeout)
        else:
            await asyncio.to_thread(self.save)
    
    def load(self) -> bool:
        """Load both indexes and metadata from disk"""
        try:
            # A leftover .tmp means a save died before its renames — the final
            # files are the last consistent snapshot; the partial is garbage.
            for final in (self.known_index_file, self.known_meta_file,
                          self.unknown_index_file, self.unknown_meta_file):
                stale = final + ".tmp"
                try:
                    if os.path.exists(stale):
                        os.remove(stale)
                        logger.warning(f"Removed stale partial save artifact: {stale}")
                except OSError:
                    pass

            if not os.path.exists(self.known_index_file) or not os.path.exists(self.unknown_index_file):
                logger.info("Identity indexes not found, starting fresh")
                return False
            
            with self.lock:
                # Load KNOWN index
                try:
                    cpu_known = faiss.read_index(self.known_index_file)
                    # Restore nprobe if it's an IVF index
                    if isinstance(cpu_known, faiss.IndexIVFFlat) or isinstance(cpu_known, faiss.IndexIVFPQ):
                        cpu_known.nprobe = getattr(settings, 'KNOWN_INDEX_NPROBE', 20)
                    if self.use_gpu:
                        self.known_index = faiss.index_cpu_to_gpu(self.gpu_resource, 0, cpu_known)
                    else:
                        self.known_index = cpu_known
                except Exception as e:
                    logger.error(f"Failed to load KNOWN index: {e}")
                    # Reinitialize if load fails
                    self.known_index = self._create_index(getattr(settings, 'KNOWN_INDEX_TYPE', 'flat'), 'known')
                    if self.use_gpu:
                        self.known_index = faiss.index_cpu_to_gpu(self.gpu_resource, 0, self.known_index)
                
                known_version = None
                if os.path.exists(self.known_meta_file):
                    with open(self.known_meta_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        self.known_metadata = {int(k): v for k, v in data.get('metadata', {}).items()}
                        self.known_identity_to_faiss = data.get('identity_to_faiss', {})
                        known_version = data.get('save_version')
                
                # Load UNKNOWN index
                if self.use_gpu:
                    cpu_unknown = faiss.read_index(self.unknown_index_file)
                    self.unknown_index = faiss.index_cpu_to_gpu(self.gpu_resource, 0, cpu_unknown)
                else:
                    self.unknown_index = faiss.read_index(self.unknown_index_file)
                
                unknown_version = None
                if os.path.exists(self.unknown_meta_file):
                    with open(self.unknown_meta_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        self.unknown_metadata = {int(k): v for k, v in data.get('metadata', {}).items()}
                        self.unknown_identity_to_faiss = data.get('identity_to_faiss', {})
                        unknown_version = data.get('save_version')

                # Version bookkeeping. A mismatch means a crash landed between
                # the four renames of one save — report it (the repair worker
                # reconciles index/metadata divergence) but still load: both
                # halves are individually consistent snapshots.
                if known_version is not None and unknown_version is not None:
                    if known_version != unknown_version:
                        logger.warning(
                            f"⚠️ Index metadata versions diverge (known={known_version}, "
                            f"unknown={unknown_version}) — a save was interrupted mid-swap; "
                            f"background repair will reconcile"
                        )
                    self._save_version = max(known_version, unknown_version)
                elif known_version is not None or unknown_version is not None:
                    self._save_version = known_version or unknown_version

                logger.info(f"✅ Loaded identity indexes: KNOWN={self.known_index.ntotal}, UNKNOWN={self.unknown_index.ntotal}")
                return True
        
        except Exception as e:
            logger.error(f"❌ Failed to load identity indexes: {e}", exc_info=True)
            return False
    
    def get_stats(self) -> dict:
        """Get statistics about the indexes"""
        with self.lock:
            return {
                'known_count': self.known_index.ntotal,
                'unknown_count': self.unknown_index.ntotal,
                'known_identities': len(self.known_identity_to_faiss),
                'unknown_identities': len(self.unknown_identity_to_faiss),
                'use_gpu': self.use_gpu,
                'auto_save_enabled': self.auto_save_enabled,
                'last_save_time': self.last_save_time.isoformat() if self.last_save_time else None
            }
    
    async def start_auto_save(self):
        """Start periodic auto-save task"""
        if not self.auto_save_enabled:
            return
        
        async def _periodic_save():
            while True:
                try:
                    await asyncio.sleep(self.auto_save_interval_seconds)
                    # Check if there are changes (simple check: compare last save time)
                    time_since_save = (datetime.utcnow() - self.last_save_time).total_seconds()
                    if time_since_save >= self.auto_save_interval_seconds:
                        # Off-loop: serialization takes seconds at scale and
                        # used to freeze every request while it ran.
                        await self.save_async()
                        self.last_save_time = datetime.utcnow()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in periodic index save: {e}", exc_info=True)
                    await asyncio.sleep(60)  # Wait 1 minute before retry
        
        self._save_task = asyncio.create_task(_periodic_save())
        logger.info(f"✅ Started periodic index auto-save (interval: {self.auto_save_interval_seconds}s)")
    
    async def stop_auto_save(self):
        """Stop periodic auto-save task"""
        if self._save_task:
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass
            logger.info("Stopped periodic index auto-save")
    
    async def start_background_rebuild(self, db_manager):
        """
        Start background rebuild task for large indexes that need rebuilding.
        Runs rebuilds from the queue without blocking.
        """
        async def _background_rebuild_worker():
            while True:
                try:
                    await asyncio.sleep(60)  # Check every minute

                    # Pop under the SAME lock the appends use. The pop was
                    # previously unlocked while every append was locked — a
                    # torn read could lose or double-run a rebuild request.
                    with self.lock:
                        index_type = self._rebuild_queue.pop(0) if self._rebuild_queue else None

                    if index_type is not None:
                        logger.info(f"[FAISS_REBUILD] Starting background rebuild for {index_type.upper()} index...")

                        try:
                            # Maintenance mutex: a rebuild swaps the index
                            # wholesale; a concurrently-running repair would be
                            # acting on faiss_ids from the pre-swap index.
                            async with self._maintenance_lock:
                                async with db_manager.get_session() as db:
                                    rebuilt = await self._rebuild_index_from_database(db, index_type)
                                    if rebuilt:
                                        await self.save_async()
                                        logger.info(f"[FAISS_REBUILD] ✅ Background rebuild completed for {index_type.upper()} index")
                                    else:
                                        logger.error(f"[FAISS_REBUILD] ❌ Background rebuild failed for {index_type.upper()} index")
                                        # Re-queue for retry
                                        with self.lock:
                                            if index_type not in self._rebuild_queue:
                                                self._rebuild_queue.append(index_type)
                        except Exception as e:
                            logger.error(f"[FAISS_REBUILD] Error during background rebuild: {e}", exc_info=True)
                            # Re-queue for retry
                            with self.lock:
                                if index_type not in self._rebuild_queue:
                                    self._rebuild_queue.append(index_type)
                            await asyncio.sleep(300)  # Wait 5 minutes before retry
                    
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[FAISS_REBUILD] Background rebuild worker error: {e}", exc_info=True)
                    await asyncio.sleep(60)
        
        self._rebuild_task = asyncio.create_task(_background_rebuild_worker())
        logger.info("✅ Started background FAISS rebuild worker")
    
    async def stop_background_rebuild(self):
        """Stop background rebuild task"""
        if self._rebuild_task:
            self._rebuild_task.cancel()
            try:
                await self._rebuild_task
            except asyncio.CancelledError:
                pass
            logger.info("Stopped background FAISS rebuild worker")
    
    async def start_background_repair(self, db_manager, interval_hours: int = 24):
        """
        Start periodic background repair task.
        Runs repair in background without blocking startup.
        
        Args:
            db_manager: Database manager instance
            interval_hours: Hours between repair runs (default: 24)
        """
        async def _periodic_repair():
            # Wait 1 hour after startup before first repair
            await asyncio.sleep(3600)
            
            while True:
                try:
                    # Send notification 1 minute before repair starts
                    try:
                        from backend.core.background_task_notifier import background_task_notifier, TaskType
                        from datetime import datetime, timedelta
                        next_run_time = datetime.utcnow() + timedelta(seconds=60)
                        await background_task_notifier.notify_task_starting(
                            task_type=TaskType.FAISS_REPAIR,
                            task_name="FAISS Index Repair",
                            description="Repairing FAISS indexes by removing orphaned entries from KNOWN and UNKNOWN indexes",
                            estimated_duration="1-5 minutes",
                            scheduled_time=next_run_time
                        )
                        await asyncio.sleep(60)  # Wait 1 minute before starting
                    except Exception as e:
                        logger.warning(f"[FAISS_REPAIR] Failed to send notification: {e}")
                    
                    logger.info(f"[FAISS_REPAIR] Starting background repair (interval: {interval_hours}h)...")
                    # Maintenance mutex: repair walks faiss_ids that a
                    # concurrent rebuild would invalidate mid-walk by swapping
                    # the whole index. Serialized, each cycle sees a stable
                    # index; sleeps stay outside the lock so neither starves.
                    async with self._maintenance_lock:
                        async with db_manager.get_session() as db:
                            # Run efficient async repair
                            repair_stats = await self.repair_orphaned_entries_async(db)
                            known_repair = await self.repair_orphaned_embeddings_async(db, 'known')
                            unknown_repair = await self.repair_orphaned_embeddings_async(db, 'unknown')

                            total_removed = (
                                repair_stats['known_removed'] + repair_stats['unknown_removed'] +
                                known_repair['removed'] + unknown_repair['removed']
                            )

                            if total_removed > 0:
                                logger.warning(f"[FAISS_REPAIR] Background repair removed {total_removed} orphaned entries")
                                await self.save_async()
                            else:
                                logger.info(f"[FAISS_REPAIR] Background repair: All entries valid")
                    
                    # Send completion notification
                    try:
                        from backend.core.background_task_notifier import background_task_notifier, TaskType
                        await background_task_notifier.notify_task_completed(
                            task_type=TaskType.FAISS_REPAIR,
                            task_name="FAISS Index Repair",
                            success=True,
                            details={
                                "total_removed": total_removed,
                                "known_removed": repair_stats.get('known_removed', 0) + known_repair.get('removed', 0),
                                "unknown_removed": repair_stats.get('unknown_removed', 0) + unknown_repair.get('removed', 0)
                            }
                        )
                    except Exception as e:
                        logger.debug(f"[FAISS_REPAIR] Failed to send completion notification: {e}")
                    
                    # Wait for next repair cycle (subtract 60 seconds for notification lead time)
                    await asyncio.sleep((interval_hours * 3600) - 60)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[FAISS_REPAIR] Background repair error: {e}", exc_info=True)
                    await asyncio.sleep(3600)  # Retry in 1 hour on error
        
        self._repair_task = asyncio.create_task(_periodic_repair())
        logger.info(f"✅ Started background FAISS repair (interval: {interval_hours}h)")
    
    async def stop_background_repair(self):
        """Stop background repair task"""
        if self._repair_task:
            self._repair_task.cancel()
            try:
                await self._repair_task
            except asyncio.CancelledError:
                pass
            logger.info("Stopped background FAISS repair")
    
    async def repair_orphaned_entries_async(self, db_session) -> dict:
        """
        Efficient repair using database queries. Only loads identity IDs that exist.
        Much faster for large datasets than loading all IDs into memory.
        
        Args:
            db_session: Async database session
            
        Returns:
            Dictionary with repair statistics
        """
        from sqlalchemy import select
        from db_models import Identity
        
        # Get all valid identity IDs from database (efficient query)
        result = await db_session.execute(select(Identity.id))
        valid_identity_ids = {str(identity_id) for identity_id in result.scalars().all()}
        
        return self.repair_orphaned_entries(valid_identity_ids)
    
    def repair_orphaned_entries(self, valid_identity_ids: set) -> dict:
        """
        Remove orphaned FAISS entries that reference identities that don't exist in the database.
        
        Args:
            valid_identity_ids: Set of valid identity UUIDs (as strings) that exist in the database
            
        Returns:
            Dictionary with repair statistics
        """
        stats = {
            'known_removed': 0,
            'unknown_removed': 0,
            'known_kept': 0,
            'unknown_kept': 0
        }
        
        with self.lock:
            # Repair KNOWN index
            orphaned_known = []
            for faiss_id, identity_id in list(self.known_metadata.items()):
                if identity_id not in valid_identity_ids:
                    orphaned_known.append((faiss_id, identity_id))
                    del self.known_metadata[faiss_id]
                    stats['known_removed'] += 1
                else:
                    stats['known_kept'] += 1
            
            # Remove from reverse mapping
            for faiss_id, identity_id in orphaned_known:
                if identity_id in self.known_identity_to_faiss:
                    if faiss_id in self.known_identity_to_faiss[identity_id]:
                        self.known_identity_to_faiss[identity_id].remove(faiss_id)
                    if not self.known_identity_to_faiss[identity_id]:
                        del self.known_identity_to_faiss[identity_id]
            
            # Repair UNKNOWN index
            orphaned_unknown = []
            for faiss_id, identity_id in list(self.unknown_metadata.items()):
                if identity_id not in valid_identity_ids:
                    orphaned_unknown.append((faiss_id, identity_id))
                    del self.unknown_metadata[faiss_id]
                    stats['unknown_removed'] += 1
                else:
                    stats['unknown_kept'] += 1
            
            # Remove from reverse mapping
            for faiss_id, identity_id in orphaned_unknown:
                if identity_id in self.unknown_identity_to_faiss:
                    if faiss_id in self.unknown_identity_to_faiss[identity_id]:
                        self.unknown_identity_to_faiss[identity_id].remove(faiss_id)
                    if not self.unknown_identity_to_faiss[identity_id]:
                        del self.unknown_identity_to_faiss[identity_id]
        
        if stats['known_removed'] > 0 or stats['unknown_removed'] > 0:
            logger.warning(f"[FAISS_REPAIR] Removed {stats['known_removed']} orphaned KNOWN entries and {stats['unknown_removed']} orphaned UNKNOWN entries")
            logger.info(f"[FAISS_REPAIR] Kept {stats['known_kept']} valid KNOWN entries and {stats['unknown_kept']} valid UNKNOWN entries")
        else:
            logger.info(f"[FAISS_REPAIR] No orphaned entries found. All {stats['known_kept']} KNOWN and {stats['unknown_kept']} UNKNOWN entries are valid.")
        
        return stats
    
    async def repair_orphaned_embeddings_async(self, db_session, index_type: str = 'known') -> dict:
        """
        Efficient repair using database queries instead of loading all IDs into memory.
        Only queries for orphaned entries, much faster for large datasets.
        
        Also detects and handles FAISS index size mismatches (when FAISS has more vectors than metadata).
        
        Args:
            db_session: Async database session
            index_type: 'known' or 'unknown'
            
        Returns:
            Dictionary with repair statistics
        """
        from sqlalchemy import select, and_, func
        from db_models import IdentityEmbedding
        
        stats = {'removed': 0, 'kept': 0, 'faiss_rebuilt': False, 'marked_unindexed': 0}
        
        metadata = self.known_metadata if index_type == 'known' else self.unknown_metadata
        identity_to_faiss = self.known_identity_to_faiss if index_type == 'known' else self.unknown_identity_to_faiss
        faiss_index = self.known_index if index_type == 'known' else self.unknown_index
        
        # Get all FAISS IDs that exist in database for this index type
        result = await db_session.execute(
            select(IdentityEmbedding.faiss_id).where(
                IdentityEmbedding.faiss_index_type == index_type,
                IdentityEmbedding.faiss_id.isnot(None)
            )
        )
        valid_faiss_ids = {int(faiss_id) for faiss_id in result.scalars().all() if faiss_id is not None}
        
        # CRITICAL: Check if FAISS index size matches metadata size
        faiss_size = faiss_index.ntotal
        metadata_size = len(metadata)
        mismatch_count = abs(faiss_size - metadata_size)
        
        # For large scale: Only rebuild if mismatch is significant
        # Small mismatches are handled by lazy marking (configurable threshold)
        lazy_marking_threshold = getattr(settings, 'FAISS_LAZY_MARKING_THRESHOLD', 1)
        # Use either the configured threshold or 1% of index size, whichever is larger
        rebuild_threshold = max(lazy_marking_threshold, faiss_size // 100)
        
        if faiss_size > metadata_size:
            logger.warning(f"[FAISS_REPAIR] ⚠️  FAISS {index_type.upper()} index has {faiss_size} vectors but metadata only has {metadata_size} entries!")
            logger.warning(f"[FAISS_REPAIR] Mismatch: {mismatch_count} orphaned vectors detected")
            
            if mismatch_count >= rebuild_threshold:
                # Large mismatch: Rebuild index (but schedule in background for very large indexes)
                if faiss_size > 50000:  # Very large index (> 50k vectors) - use background rebuild
                    logger.warning(f"[FAISS_REPAIR] Large index detected ({faiss_size} vectors). Scheduling background rebuild...")
                    # Schedule background rebuild instead of blocking
                    with self.lock:
                        if index_type not in self._rebuild_queue:
                            self._rebuild_queue.append(index_type)
                    stats['faiss_rebuilt'] = False
                    stats['background_rebuild_scheduled'] = True
                    logger.info(f"[FAISS_REPAIR] Background rebuild scheduled for {index_type.upper()} index. Will rebuild without blocking.")
                else:
                    # Medium index: Rebuild now (acceptable for < 50k vectors)
                    logger.warning(f"[FAISS_REPAIR] Rebuilding {index_type.upper()} index from database ({faiss_size} vectors)...")
                    stats['faiss_rebuilt'] = await self._rebuild_index_from_database(db_session, index_type)
                    if stats['faiss_rebuilt']:
                        logger.info(f"[FAISS_REPAIR] ✅ Successfully rebuilt {index_type.upper()} index from database")
                    else:
                        logger.error(f"[FAISS_REPAIR] ❌ Failed to rebuild {index_type.upper()} index")
            else:
                # Small mismatch: Use lazy marking (mark orphaned entries, skip during search)
                logger.info(f"[FAISS_REPAIR] Small mismatch ({mismatch_count} < {rebuild_threshold}). Using lazy marking approach.")
                stats['lazy_marked'] = await self._lazy_mark_orphaned_vectors(faiss_size, metadata_size, valid_faiss_ids, index_type)
        elif faiss_size < metadata_size:
            logger.warning(f"[FAISS_REPAIR] ⚠️  Metadata has {metadata_size} entries but FAISS index only has {faiss_size} vectors!")
            logger.warning(f"[FAISS_REPAIR] Removing orphaned metadata entries...")
            # Remove metadata entries that don't have corresponding FAISS vectors
            with self.lock:
                orphaned_metadata = []
                for faiss_id in list(metadata.keys()):
                    if faiss_id >= faiss_size:
                        identity_id = metadata[faiss_id]
                        orphaned_metadata.append((faiss_id, identity_id))
                        del metadata[faiss_id]
                        stats['removed'] += 1
                
                # Remove from reverse mapping
                for faiss_id, identity_id in orphaned_metadata:
                    if identity_id in identity_to_faiss:
                        if faiss_id in identity_to_faiss[identity_id]:
                            identity_to_faiss[identity_id].remove(faiss_id)
                        if not identity_to_faiss[identity_id]:
                            del identity_to_faiss[identity_id]
        
        with self.lock:
            orphaned = []
            for faiss_id, identity_id in list(metadata.items()):
                if faiss_id not in valid_faiss_ids:
                    orphaned.append((faiss_id, identity_id))
                    del metadata[faiss_id]
                    stats['removed'] += 1
                else:
                    stats['kept'] += 1
            
            # Remove from reverse mapping
            for faiss_id, identity_id in orphaned:
                if identity_id in identity_to_faiss:
                    if faiss_id in identity_to_faiss[identity_id]:
                        identity_to_faiss[identity_id].remove(faiss_id)
                    if not identity_to_faiss[identity_id]:
                        del identity_to_faiss[identity_id]
        
        # CRITICAL: Check if database has more embeddings than FAISS
        # Get total count of embeddings in database for this index type
        db_count_result = await db_session.execute(
            select(func.count(IdentityEmbedding.id)).where(
                IdentityEmbedding.faiss_index_type == index_type
            )
        )
        db_embedding_count = db_count_result.scalar() or 0
        
        if db_embedding_count > faiss_index.ntotal:
            missing_count = db_embedding_count - faiss_index.ntotal
            logger.warning(f"[FAISS_REPAIR] ⚠️  Database has {db_embedding_count} embeddings but FAISS only has {faiss_index.ntotal} vectors!")
            logger.warning(f"[FAISS_REPAIR] Missing {missing_count} embeddings in FAISS index")
            
            # Get embeddings that don't have valid FAISS entries
            missing_embeddings_result = await db_session.execute(
                select(IdentityEmbedding).where(
                    IdentityEmbedding.faiss_index_type == index_type,
                    (IdentityEmbedding.faiss_id.is_(None) | 
                     (IdentityEmbedding.faiss_id >= faiss_index.ntotal))
                )
            )
            missing_embeddings = missing_embeddings_result.scalars().all()
            
            if missing_embeddings:
                logger.warning(f"[FAISS_REPAIR] Found {len(missing_embeddings)} database embeddings missing from FAISS")
                logger.warning(f"[FAISS_REPAIR] These embeddings cannot be reconstructed (vectors not stored in database)")
                logger.warning(f"[FAISS_REPAIR] Marking them as unindexed (faiss_id = None)...")
                
                # Mark as unindexed - they'll be re-added when the person is detected again
                for emb_record in missing_embeddings:
                    emb_record.faiss_id = None
                    emb_record.faiss_index_type = None
                
                await db_session.flush()
                stats['marked_unindexed'] = len(missing_embeddings)
                logger.info(f"[FAISS_REPAIR] ✅ Marked {len(missing_embeddings)} embeddings as unindexed (will be re-added on next detection)")
        
        if stats['removed'] > 0 or stats.get('faiss_rebuilt', False) or stats.get('lazy_marked', 0) > 0 or stats.get('marked_unindexed', 0) > 0:
            if stats.get('faiss_rebuilt', False):
                logger.warning(f"[FAISS_REPAIR] Rebuilt {index_type.upper()} index to fix size mismatch")
            if stats.get('background_rebuild_scheduled', False):
                logger.warning(f"[FAISS_REPAIR] Background rebuild scheduled for {index_type.upper()} index")
            if stats.get('lazy_marked', 0) > 0:
                logger.info(f"[FAISS_REPAIR] Lazy-marked {stats['lazy_marked']} orphaned vectors (will be skipped during search)")
            if stats['removed'] > 0:
                logger.warning(f"[FAISS_REPAIR] Removed {stats['removed']} orphaned {index_type.upper()} embeddings without database records")
            logger.info(f"[FAISS_REPAIR] Kept {stats['kept']} valid {index_type.upper()} embeddings")
            if stats.get('marked_unindexed', 0) > 0:
                logger.info(f"[FAISS_REPAIR] Marked {stats['marked_unindexed']} embeddings as unindexed (will be re-added on next detection)")
        else:
            logger.info(f"[FAISS_REPAIR] All {stats['kept']} {index_type.upper()} embeddings have valid database records")
        
        return stats
    
    async def _lazy_mark_orphaned_vectors(self, faiss_size: int, metadata_size: int, valid_faiss_ids: set, index_type: str) -> int:
        """
        Lazy marking approach for small mismatches.
        Instead of rebuilding the entire index, we mark orphaned vectors and skip them during search.
        Much more efficient for large scale.
        
        Args:
            faiss_size: Total vectors in FAISS index
            metadata_size: Total entries in metadata
            valid_faiss_ids: Set of valid FAISS IDs from database
            index_type: 'known' or 'unknown'
            
        Returns:
            Number of vectors marked as orphaned
        """
        metadata = self.known_metadata if index_type == 'known' else self.unknown_metadata
        marked_count = 0
        
        # Find vectors that exist in FAISS but not in metadata or database
        with self.lock:
            for faiss_id in range(faiss_size):
                # If not in metadata and not in valid database IDs, mark as orphaned
                if faiss_id not in metadata and faiss_id not in valid_faiss_ids:
                    # Mark by adding to metadata with a special marker (we'll skip these during search)
                    # Actually, we'll just ensure they're not in metadata - search will skip them automatically
                    marked_count += 1
        
        logger.info(f"[FAISS_REPAIR] Lazy-marked {marked_count} orphaned vectors in {index_type.upper()} index")
        logger.info(f"[FAISS_REPAIR] These vectors will be skipped during search (efficient for large scale)")
        return marked_count
    
    async def _rebuild_index_from_database(self, db_session, index_type: str) -> bool:
        """
        Rebuild FAISS index from database embeddings.
        This is used when FAISS index size doesn't match metadata.
        
        Args:
            db_session: Async database session
            index_type: 'known' or 'unknown'
            
        Returns:
            True if rebuild successful, False otherwise
        """
        try:
            from sqlalchemy import select
            from db_models import IdentityEmbedding

            # Memory guard: a rebuild materializes EVERY embedding for the
            # index type into Python lists before training/adding — at scale
            # that is a large transient allocation on top of the live index.
            # Below the floor, defer rather than risk an OOM kill. The
            # caller's False path re-queues, so a deferred rebuild retries on
            # the next worker cycle.
            available_mb = self._available_memory_mb()
            if available_mb is not None:
                min_available_mb = int(getattr(settings, 'FAISS_REBUILD_MIN_AVAILABLE_MB', 1024) or 1024)
                if available_mb < min_available_mb:
                    logger.warning(
                        f"[FAISS_REBUILD] Deferring {index_type.upper()} rebuild: "
                        f"{available_mb}MB available < {min_available_mb}MB required"
                    )
                    return False

            logger.info(f"[FAISS_REBUILD] Rebuilding {index_type.upper()} index from database...")
            
            # Get all embeddings from database for this index type
            result = await db_session.execute(
                select(IdentityEmbedding).where(
                    IdentityEmbedding.faiss_index_type == index_type,
                    IdentityEmbedding.faiss_id.isnot(None)
                ).order_by(IdentityEmbedding.faiss_id)
            )
            embeddings = result.scalars().all()
            
            if not embeddings:
                logger.warning(f"[FAISS_REBUILD] No embeddings found in database for {index_type.upper()} index")
                # Create empty index
                with self.lock:
                    if index_type == 'known':
                        self.known_index = self._create_index(getattr(settings, 'KNOWN_INDEX_TYPE', 'flat'), 'known')
                        if self.use_gpu:
                            self.known_index = faiss.index_cpu_to_gpu(self.gpu_resource, 0, self.known_index)
                        self.known_metadata = {}
                        self.known_identity_to_faiss = {}
                    else:
                        self.unknown_index = faiss.IndexFlatIP(self.embedding_size)
                        if self.use_gpu:
                            self.unknown_index = faiss.index_cpu_to_gpu(self.gpu_resource, 0, self.unknown_index)
                        self.unknown_metadata = {}
                        self.unknown_identity_to_faiss = {}
                return True
            
            # Reconstruct embeddings from current FAISS index
            faiss_index = self.known_index if index_type == 'known' else self.unknown_index
            embeddings_to_add = []  # List of (old_faiss_id, identity_id, embedding_vector, emb_record)
            new_metadata = {}
            new_identity_to_faiss = {}
            
            invalid_faiss_ids = []
            for emb_record in embeddings:
                if emb_record.faiss_id is None:
                    continue
                
                try:
                    # Try to reconstruct from current index
                    if emb_record.faiss_id < faiss_index.ntotal:
                        embedding_vector = faiss_index.reconstruct(int(emb_record.faiss_id))
                        embeddings_to_add.append((emb_record.faiss_id, emb_record.identity_id, embedding_vector, emb_record))
                    else:
                        logger.warning(f"[FAISS_REBUILD] FAISS ID {emb_record.faiss_id} out of range (total: {faiss_index.ntotal}), skipping")
                        invalid_faiss_ids.append(emb_record)
                except Exception as e:
                    logger.warning(f"[FAISS_REBUILD] Failed to reconstruct embedding {emb_record.faiss_id}: {e}")
                    invalid_faiss_ids.append(emb_record)
            
            # Clean up invalid faiss_id references in database
            if invalid_faiss_ids:
                logger.info(f"[FAISS_REBUILD] Cleaning up {len(invalid_faiss_ids)} database records with invalid faiss_id references...")
                for emb_record in invalid_faiss_ids:
                    emb_record.faiss_id = None
                    emb_record.faiss_index_type = None
                await db_session.flush()
                logger.info(f"[FAISS_REBUILD] ✅ Cleaned up {len(invalid_faiss_ids)} invalid faiss_id references")
            
            if not embeddings_to_add:
                logger.error(f"[FAISS_REBUILD] No valid embeddings could be reconstructed!")
                return False
            
            # Create new index
            if index_type == 'known':
                new_index = self._create_index(getattr(settings, 'KNOWN_INDEX_TYPE', 'flat'), 'known')
                # Check if training needed
                if hasattr(new_index, 'is_trained') and not new_index.is_trained:
                    if len(embeddings_to_add) >= 100:
                        training_embeddings = np.array([emb for _, _, emb, _ in embeddings_to_add[:1000]])
                        new_index.train(training_embeddings)
                    else:
                        logger.warning(f"[FAISS_REBUILD] Not enough embeddings for training ({len(embeddings_to_add)} < 100)")
            else:
                new_index = faiss.IndexFlatIP(self.embedding_size)
            
            # Add embeddings to new index
            with self.lock:
                for old_faiss_id, identity_id, embedding_vector, emb_record in embeddings_to_add:
                    new_faiss_id = new_index.ntotal
                    normalized_embedding = embedding_vector / np.linalg.norm(embedding_vector)
                    normalized_embedding = normalized_embedding.astype(np.float32).reshape(1, -1)
                    new_index.add(normalized_embedding)
                    
                    new_metadata[new_faiss_id] = str(identity_id)
                    if str(identity_id) not in new_identity_to_faiss:
                        new_identity_to_faiss[str(identity_id)] = []
                    new_identity_to_faiss[str(identity_id)].append(new_faiss_id)
                    
                    # Update database record with new faiss_id
                    emb_record.faiss_id = new_faiss_id
                
                # Replace old index and metadata
                if index_type == 'known':
                    if self.use_gpu:
                        self.known_index = faiss.index_cpu_to_gpu(self.gpu_resource, 0, new_index)
                    else:
                        self.known_index = new_index
                    self.known_metadata = new_metadata
                    self.known_identity_to_faiss = new_identity_to_faiss
                else:
                    if self.use_gpu:
                        self.unknown_index = faiss.index_cpu_to_gpu(self.gpu_resource, 0, new_index)
                    else:
                        self.unknown_index = new_index
                    self.unknown_metadata = new_metadata
                    self.unknown_identity_to_faiss = new_identity_to_faiss
            
            await db_session.flush()
            logger.info(f"[FAISS_REBUILD] ✅ Rebuilt {index_type.upper()} index with {len(embeddings_to_add)} embeddings")
            return True
            
        except Exception as e:
            logger.error(f"[FAISS_REBUILD] Failed to rebuild {index_type.upper()} index: {e}", exc_info=True)
            return False
    
    def repair_orphaned_embeddings(self, valid_faiss_ids: set, index_type: str = 'known') -> dict:
        """
        Remove FAISS entries that don't have corresponding database embedding records.
        More thorough than repair_orphaned_entries - checks each faiss_id individually.
        
        Args:
            valid_faiss_ids: Set of valid FAISS IDs (integers) that exist in IdentityEmbedding table
            index_type: 'known' or 'unknown'
            
        Returns:
            Dictionary with repair statistics
        """
        stats = {
            'removed': 0,
            'kept': 0
        }
        
        metadata = self.known_metadata if index_type == 'known' else self.unknown_metadata
        identity_to_faiss = self.known_identity_to_faiss if index_type == 'known' else self.unknown_identity_to_faiss
        
        with self.lock:
            orphaned = []
            for faiss_id, identity_id in list(metadata.items()):
                if faiss_id not in valid_faiss_ids:
                    orphaned.append((faiss_id, identity_id))
                    del metadata[faiss_id]
                    stats['removed'] += 1
                else:
                    stats['kept'] += 1
            
            # Remove from reverse mapping
            for faiss_id, identity_id in orphaned:
                if identity_id in identity_to_faiss:
                    if faiss_id in identity_to_faiss[identity_id]:
                        identity_to_faiss[identity_id].remove(faiss_id)
                    if not identity_to_faiss[identity_id]:
                        del identity_to_faiss[identity_id]
        
        if stats['removed'] > 0:
            logger.warning(f"[FAISS_REPAIR] Removed {stats['removed']} orphaned {index_type.upper()} embeddings without database records")
            logger.info(f"[FAISS_REPAIR] Kept {stats['kept']} valid {index_type.upper()} embeddings")
            if stats.get('marked_unindexed', 0) > 0:
                logger.info(f"[FAISS_REPAIR] Marked {stats['marked_unindexed']} embeddings as unindexed (will be re-added on next detection)")
        else:
            logger.info(f"[FAISS_REPAIR] All {stats['kept']} {index_type.upper()} embeddings have valid database records")
        
        return stats

