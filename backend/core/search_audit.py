"""
Search audit — the ONE writer of `search_history`.

Every image search — the quick `/api/search/by-image`, the advanced multi-face
search and the batch search — records exactly one `search_history` row through
`record_image_search`. Callers never build the row themselves, so audit
fidelity cannot drift between entry points. No image bytes are stored: the
input is identified by its SHA-256 only.
"""
import logging
import uuid
from typing import Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from db_models import SearchHistory, SearchType

logger = logging.getLogger(__name__)


async def record_image_search(
    db: AsyncSession,
    *,
    search_id: str,
    user_id: Optional[int],
    search_type: SearchType,
    scope: Optional[str] = None,
    top_k: Optional[int] = None,
    filters: Optional[Dict] = None,
    exclude_identity_ids: Optional[List[str]] = None,
    exclude_watchlist_ids: Optional[List[str]] = None,
    image_hash: Optional[str] = None,
    faces_count: Optional[int] = None,
    quality_scores: Optional[List[float]] = None,
    results_count: int = 0,
    watchlist_alerts_count: int = 0,
    unique_identities: Optional[int] = None,
    processing_time_ms: Optional[int] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    commit: bool = True,
) -> Optional[SearchHistory]:
    """Insert the search_history row for one search. `search_id` becomes the
    row's primary key (it is also what watchlist_alerts.search_id references).
    Returns the row, or None if the write failed (logged; a failed audit row
    never fails the search response — the search itself has no side effects)."""
    try:
        history = SearchHistory(
            id=uuid.UUID(str(search_id)),
            user_id=user_id,
            search_type=search_type,
            scope=scope,
            top_k=top_k,
            filters=filters,
            exclude_identity_ids=exclude_identity_ids,
            exclude_watchlist_ids=exclude_watchlist_ids,
            input_image_hash=image_hash,
            input_faces_count=faces_count,
            input_quality_scores=quality_scores,
            results_count=results_count,
            watchlist_alerts_count=watchlist_alerts_count,
            unique_identities_count=unique_identities,
            processing_time_ms=processing_time_ms,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.add(history)
        if commit:
            await db.commit()
        else:
            await db.flush()
        logger.debug("[SEARCH_AUDIT] recorded %s search %s", search_type.value, search_id)
        return history
    except Exception as e:
        logger.error("[SEARCH_AUDIT] failed to record search %s: %s", search_id, e)
        try:
            await db.rollback()
        except Exception:
            pass
        return None
