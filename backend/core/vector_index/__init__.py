"""Vector search indexes over the authoritative PostgreSQL embedding store.

PostgreSQL holds every vector in `identity_embeddings.embedding`. Anything in
here is a disposable acceleration layer, keyed by `identity_embeddings.id`, and
fully reconstructible from the database.
"""

from backend.core.vector_index.base import (EMBEDDING_DIM, SUPPORTED_INDEX_TYPES,
                                            SYNC_FAILED, SYNC_PENDING,
                                            SYNC_STATES, SYNC_SYNCED,
                                            InvalidVector, LoadResult,
                                            RebuildReport, UnsupportedIndexType,
                                            UnsupportedOperation, VectorIndex,
                                            VectorIndexError, build_index,
                                            coerce_vector, validate_vector,
                                            vector_checksum)
from backend.core.vector_index.factory import (BackendSelection,
                                               VectorBackendUnavailable,
                                               select_backend)
from backend.core.vector_index.flat_faiss import FlatFaissIndex
from backend.core.vector_index.pgvector_index import PgVectorIndex

__all__ = [
    "EMBEDDING_DIM", "SUPPORTED_INDEX_TYPES",
    "SYNC_PENDING", "SYNC_SYNCED", "SYNC_FAILED", "SYNC_STATES",
    "VectorIndex", "VectorIndexError", "InvalidVector",
    "UnsupportedOperation", "UnsupportedIndexType",
    "LoadResult", "RebuildReport",
    "FlatFaissIndex", "PgVectorIndex",
    "BackendSelection", "VectorBackendUnavailable", "select_backend",
    "build_index", "coerce_vector", "validate_vector", "vector_checksum",
]
