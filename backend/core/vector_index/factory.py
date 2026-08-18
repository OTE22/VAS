"""
Backend selection — fail closed, never silently downgrade.
==========================================================

If an operator configures `VECTOR_BACKEND=faiss` and FAISS cannot be used, the
service must say so rather than quietly answering searches from a different
index. The previous behaviour was the opposite: an `ImportError` was caught in
lifespan, logged as a warning, and set `identity_service = None` — which took
pgvector down too and left the app running with recognition silently disabled.

Degrading is still possible, but only as a deliberate configuration:
`VECTOR_INDEX_FALLBACK=pgvector`. It is logged at CRITICAL and surfaced as
degraded health, so nobody discovers it from a recognition miss weeks later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from backend.core.vector_index.base import (SUPPORTED_INDEX_TYPES,
                                            UnsupportedIndexType, build_index)

logger = logging.getLogger(__name__)


class VectorBackendUnavailable(RuntimeError):
    """The configured backend cannot start and no fallback was authorized."""


@dataclass
class BackendSelection:
    index: Any
    backend: str                 # what is actually running
    requested: str               # what was configured
    degraded: bool = False
    reason: Optional[str] = None

    def health(self) -> dict:
        out = {
            "backend": self.backend,
            "requested_backend": self.requested,
            "degraded": self.degraded,
            "reason": self.reason,
        }
        try:
            out.update(self.index.health())
        except Exception:                     # noqa: BLE001 - health must not raise
            pass
        return out


def select_backend(settings, *, storage_dir: Optional[str] = None,
                   model_version: Optional[str] = None) -> BackendSelection:
    """Build the configured index, or fail loudly with an actionable message."""
    requested = str(settings.VECTOR_BACKEND or "pgvector").strip().lower()
    fallback = str(settings.VECTOR_INDEX_FALLBACK or "").strip().lower()

    if requested == "pgvector":
        from backend.core.vector_index.pgvector_index import PgVectorIndex
        return BackendSelection(index=PgVectorIndex(model_version=model_version),
                                backend="pgvector", requested=requested)

    if requested != "faiss":
        raise VectorBackendUnavailable(
            f"VECTOR_BACKEND={requested!r} is not a known backend. "
            "Supported: 'pgvector', 'faiss'.")

    index_type = str(settings.KNOWN_INDEX_TYPE or "flat").strip().lower()
    try:
        index = build_index(index_type,
                            storage_dir=storage_dir or settings.IDENTITY_INDEX_DB_PATH,
                            model_version=model_version)
        return BackendSelection(index=index, backend="faiss", requested=requested)
    except (UnsupportedIndexType, ImportError, Exception) as exc:
        reason = str(exc)
        if isinstance(exc, ImportError):
            reason = (f"the faiss library could not be imported ({exc}). "
                      "Install faiss-cpu, or configure a different backend.")
        elif isinstance(exc, UnsupportedIndexType):
            reason = (f"{exc} Supported index types: "
                      f"{', '.join(SUPPORTED_INDEX_TYPES)}.")

        if fallback == "pgvector":
            logger.critical(
                "[VECTOR_INDEX] DEGRADED: VECTOR_BACKEND=faiss is unusable — %s "
                "Falling back to pgvector because VECTOR_INDEX_FALLBACK=pgvector "
                "is explicitly configured. Searches are being served by a "
                "DIFFERENT backend than configured.", reason)
            from backend.core.vector_index.pgvector_index import PgVectorIndex
            return BackendSelection(
                index=PgVectorIndex(model_version=model_version),
                backend="pgvector", requested=requested,
                degraded=True, reason=reason)

        raise VectorBackendUnavailable(
            f"VECTOR_BACKEND=faiss cannot start: {reason} "
            "Refusing to start rather than silently serving searches from a "
            "different index. Fix the configuration, or set "
            "VECTOR_INDEX_FALLBACK=pgvector to accept a logged, degraded "
            "fallback."
        ) from exc
