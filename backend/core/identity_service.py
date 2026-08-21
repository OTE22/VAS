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
from typing import Optional, Tuple, List, Union, NamedTuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, text as sa_text

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_models import (
    Identity, IdentityAppearance, IdentityEmbedding, IdentityImage, Face,
    Detection, IdentityType, IdentityStatus, LabelState
)

# The canonical sync-state literals. Imported from the contract's base module
# rather than re-spelled here, so 'pending'/'synced'/'failed' can never drift
# apart between the writer and the reconciler that reads them back.
from backend.core.vector_index.base import (SYNC_FAILED, SYNC_PENDING,
                                            SYNC_SYNCED)

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
    VECTOR_BACKEND = settings.VECTOR_BACKEND.lower()
except ImportError:
    settings = None
    VECTOR_BACKEND = 'faiss'

USE_PGVECTOR = VECTOR_BACKEND == 'pgvector' and PGVECTOR_AVAILABLE

logger = logging.getLogger(__name__)
logger.info(f"[IDENTITY_SERVICE] Vector backend: {VECTOR_BACKEND} (pgvector_available={PGVECTOR_AVAILABLE}, use_pgvector={USE_PGVECTOR})")


class UnmergeConflict(Exception):
    """An unmerge refusal, raised before any mutation.

    Carries a stable machine-readable `reason` so callers and tests can assert
    on WHY it refused rather than parsing prose. `unmerge_identity` raises this
    only from its verification phase, so catching it never implies a partial
    write.
    """

    def __init__(self, reason: str, detail: str, status_code: int = 409):
        self.reason = reason
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def _count_identity_op(op: str) -> None:
    """Count a destructive identity operation (bounded op enum — never an id).

    These operations previously had no Prometheus signal at all; their only
    trace was the audit table.
    """
    try:
        from backend.core.metrics import metrics_identity_ops
        if metrics_identity_ops:
            metrics_identity_ops.labels(op=op).inc()
    except Exception:                                          # noqa: BLE001
        pass


class IdentityResolution(NamedTuple):
    """What one frame's face resolved to. `embedding_id` is the row THIS
    processing attempt inserted for the frame (None when quality was below the
    threshold and nothing was written) — never a pre-existing row, so the
    caller can link it to the detection exactly and, if the detection can never
    be persisted, remove exactly it. `identity_created` marks an identity that
    exists only because of this frame."""
    identity: Identity
    is_new_identity: bool
    similarity: float
    embedding_id: Optional[int]
    identity_created: bool


# One canonical lifecycle handler for merge_suggestions.identity_ids (JSONB —
# it cannot carry a FK): every PENDING suggestion that names an identity which
# stops being actionable (merged, retired to INACTIVE, hard-deleted by a
# maintenance script) becomes INVALIDATED in the SAME transaction, so a stale
# suggestion can never be approved against an identity that no longer resolves.
async def invalidate_merge_suggestions(db, identity_ids, reason: str) -> int:
    """Flush only; joins the caller's transaction. Returns rows invalidated."""
    from db_models import MergeSuggestion, MergeSuggestionStatus
    from sqlalchemy import or_, text as _text
    ids = [str(i) for i in identity_ids if i]
    if not ids:
        return 0
    conds = [_text("identity_ids::jsonb @> CAST(:id" + str(n) + " AS jsonb)").bindparams(
        **{f"id{n}": f'["{i}"]'}) for n, i in enumerate(ids)]
    res = await db.execute(
        update(MergeSuggestion)
        .where(MergeSuggestion.status == MergeSuggestionStatus.PENDING, or_(*conds))
        .values(status=MergeSuggestionStatus.INVALIDATED,
                invalidated_reason=reason[:255], invalidated_at=datetime.utcnow())
        .execution_options(synchronize_session=False))
    return res.rowcount or 0


# ---------------------------------------------------------------------------
# Watchlist membership + live alerts follow the person through a merge.
#
# watchlist_alerts.watchlist_entry_id is FK ... ON DELETE CASCADE NOT NULL and
# is read by the alerts API, so a membership row is NEVER deleted by merge:
#   * winner has no row for the list  -> the loser's row is re-pointed
#     (identity_id = winner); its historical alerts follow the entry untouched;
#   * winner already has the pair       -> the winner's row stays operational
#     (priority = max, expires_at = later); the loser's row is RETIRED in place
#     (is_active = false) and kept for provenance with its alerts. It can never
#     match at runtime: matching selects by the WINNER's id and filters
#     is_active, and the loser identity is MERGED.
# live_search_alerts.identity_id (CASCADE) is re-pointed to the winner.
# Everything is recorded in the merge provenance and reversed by unmerge.
# ---------------------------------------------------------------------------
_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


async def transfer_watchlist_membership(db, loser_id, winner_id) -> dict:
    """Flush only; joins the merge transaction. Returns the provenance block."""
    from db_models import WatchlistEntry, LiveSearchAlert
    moves = []
    loser_entries = (await db.execute(
        select(WatchlistEntry).where(WatchlistEntry.identity_id == loser_id))).scalars().all()
    if loser_entries:
        winner_by_list = {e.watchlist_id: e for e in (await db.execute(
            select(WatchlistEntry).where(WatchlistEntry.identity_id == winner_id))).scalars().all()}
        for entry in loser_entries:
            before = {"identity_id": str(entry.identity_id), "is_active": bool(entry.is_active),
                      "priority": getattr(entry.priority, "value", entry.priority),
                      "expires_at": entry.expires_at.isoformat() if entry.expires_at else None}
            twin = winner_by_list.get(entry.watchlist_id)
            if twin is None:
                entry.identity_id = winner_id
                moves.append({"entry_id": str(entry.id), "watchlist_id": str(entry.watchlist_id),
                              "action": "moved", "before": before})
            else:
                winner_before = {"priority": getattr(twin.priority, "value", twin.priority),
                                 "expires_at": twin.expires_at.isoformat() if twin.expires_at else None,
                                 "is_active": bool(twin.is_active)}
                lp = _PRIORITY_RANK.get(getattr(entry.priority, "value", entry.priority), 0)
                wp = _PRIORITY_RANK.get(getattr(twin.priority, "value", twin.priority), 0)
                if lp > wp:
                    twin.priority = entry.priority
                if entry.expires_at and twin.expires_at and entry.expires_at > twin.expires_at:
                    twin.expires_at = entry.expires_at
                elif entry.expires_at is None and twin.expires_at is not None and entry.is_active:
                    twin.expires_at = None
                if entry.is_active and not twin.is_active:
                    twin.is_active = True
                entry.is_active = False
                moves.append({"entry_id": str(entry.id), "watchlist_id": str(entry.watchlist_id),
                              "action": "retired_duplicate", "winner_entry_id": str(twin.id),
                              "before": before, "winner_before": winner_before})
    live_ids = [str(r) for r in (await db.execute(
        select(LiveSearchAlert.id).where(LiveSearchAlert.identity_id == loser_id))).scalars().all()]
    if live_ids:
        await db.execute(update(LiveSearchAlert).where(LiveSearchAlert.identity_id == loser_id)
                         .values(identity_id=winner_id))
    await db.flush()
    return {"watchlist_entry_moves": moves, "live_alert_ids": live_ids}


async def restore_watchlist_membership(db, provenance: dict, loser_id, winner_id):
    """Inverse of transfer_watchlist_membership for unmerge. Refuses with
    UnmergeConflict when the loser independently regained a pair the merge
    moved (mirrors the gallery post_merge conflict rule)."""
    from db_models import WatchlistEntry, LiveSearchAlert, WatchlistEntryPriority
    moves = provenance.get("watchlist_entry_moves") or []
    live_ids = provenance.get("live_alert_ids") or []
    restored = {"moved_back": 0, "reactivated": 0, "live_alerts": 0}
    for m in moves:
        entry = await db.get(WatchlistEntry, uuid.UUID(m["entry_id"]))
        if entry is None:
            continue
        if m["action"] == "moved":
            if entry.identity_id != winner_id:
                continue  # moved on since; belongs to whoever holds it
            clash = (await db.execute(select(WatchlistEntry.id).where(
                WatchlistEntry.watchlist_id == entry.watchlist_id,
                WatchlistEntry.identity_id == loser_id))).scalar_one_or_none()
            if clash:
                raise UnmergeConflict(
                    "post_merge_watchlist_conflict",
                    f"The loser was independently re-added to watchlist {entry.watchlist_id} after the merge; "
                    f"reversing the membership move would collide with entry {clash}.")
            entry.identity_id = loser_id
            restored["moved_back"] += 1
        elif m["action"] == "retired_duplicate":
            before = m.get("before") or {}
            entry.is_active = bool(before.get("is_active", True))
            twin = await db.get(WatchlistEntry, uuid.UUID(m["winner_entry_id"])) if m.get("winner_entry_id") else None
            wb = m.get("winner_before") or {}
            if twin is not None and wb:
                try:
                    twin.priority = WatchlistEntryPriority(wb["priority"]) if wb.get("priority") else twin.priority
                except Exception:
                    pass
                twin.expires_at = datetime.fromisoformat(wb["expires_at"]) if wb.get("expires_at") else None
                twin.is_active = bool(wb.get("is_active", twin.is_active))
            restored["reactivated"] += 1
    if live_ids:
        res = await db.execute(update(LiveSearchAlert)
                               .where(LiveSearchAlert.id.in_([uuid.UUID(i) for i in live_ids]),
                                      LiveSearchAlert.identity_id == winner_id)
                               .values(identity_id=loser_id))
        restored["live_alerts"] = res.rowcount or 0
    await db.flush()
    return restored


class IdentityService:
    """
    Service for managing identities.
    
    Supports dual vector backends:
    - FAISS (default): in-process search via the VectorIndex contract
    - pgvector: PostgreSQL-based search via IdentityIndexPgVector
    """
    
    def __init__(
        self,
        identity_index=None,
        pgvector_index: Optional[IdentityIndexPgVector] = None,
        vector_index=None
    ):
        # `identity_index` is the retired first positional parameter. It is
        # accepted and ignored so the many `IdentityService(None, ...)` call
        # sites keep working; passing anything non-None is a programming error
        # rather than a silent partial configuration.
        if identity_index is not None:
            raise TypeError(
                "IdentityService no longer takes a legacy identity index. "
                "Pass vector_index=<VectorIndex implementation> instead.")
        # The VectorIndex contract implementation (FlatFaissIndex / PgVectorIndex).
        self.vector_index = vector_index
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
        self.quality_threshold_known = settings.IDENTITY_QUALITY_THRESHOLD_KNOWN
        self.quality_threshold_unknown = settings.IDENTITY_QUALITY_THRESHOLD_UNKNOWN
        # Legacy threshold (for backward compatibility)
        self.quality_threshold = self.quality_threshold_known

        # Provenance stamped on every vector we store. Reconciliation compares it,
        # so a model swap invalidates stale index entries instead of silently
        # mixing incompatible embedding spaces.
        self.embedding_model_version = self._resolve_model_version()

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
        return float(settings.SIMILARITY_THRESHOLD)

    @property
    def unknown_threshold(self) -> float:
        """Live read: UNKNOWN_SIMILARITY_THRESHOLD governs UNKNOWN matching (dynamic)."""
        return float(settings.UNKNOWN_SIMILARITY_THRESHOLD)

    @property
    def snapshot_replace_min_similarity(self) -> float:
        """How good a match must be to become an identity's displayed face.

        Only consulted when there is no quality score to compare against.
        Replacing on any positive similarity made the avatar "whatever arrived
        last", which is how a correct match came to show the wrong person.
        """
        return float(settings.IDENTITY_SNAPSHOT_REPLACE_MIN_SIMILARITY)

    @property
    def auto_enrich_enabled(self) -> bool:
        """Whether a recognised face may be written back as a new embedding.

        Off by default. When on, it silently teaches an identity new faces:
        anything admitted by the KNOWN threshold and clearing the enrich bar is
        stored permanently under that identity, with no provenance flag telling
        an operator-enrolled vector from an auto-learned one. One wrong match
        then makes every subsequent photo of the wrong person a ~1.0 hit.
        """
        return bool(settings.IDENTITY_AUTO_ENRICH_ENABLED)

    async def find_or_create_identity(
        self,
        embedding: np.ndarray,
        pipeline_id: str,
        detection_id: Optional[int],
        db: AsyncSession,
        quality_score: Optional[float] = None,
        quality_scorer_version: Optional[str] = None
    ) -> IdentityResolution:
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
                embedding, pipeline_id, detection_id, db, quality_score,
                quality_scorer_version
            )
        else:
            logger.info(f"[IDENTITY_SEARCH] Using FAISS backend (use_pgvector={self.use_pgvector}, pgvector_index={self.pgvector_index is not None})")
        
        # FAISS backend, through the VectorIndex contract.
        #
        # The index answers with EMBEDDING KEYS (identity_embeddings.id) and has
        # no notion of a person; search_vector_index resolves those keys through
        # PostgreSQL and groups them by identity. That indirection is the whole
        # point — it is what lets the index implementation change without
        # touching a schema or a route, and it means a stale key that no longer
        # has an ACTIVE row simply cannot win a match.
        if self.vector_index is None:
            logger.error("[IDENTITY_SEARCH] No vector index is configured; "
                         "cannot search. Creating a new UNKNOWN identity.")
            identity, emb_id = await self._create_unknown_identity(
                embedding, pipeline_id, detection_id, db, quality_score,
                quality_scorer_version)
            return IdentityResolution(identity, True, 0.0, emb_id, True)

        # Step 1: KNOWN
        known_matches = await self.search_vector_index(
            db, embedding, top_k=1, threshold=self.known_threshold,
            index_type='known')
        if known_matches:
            identity_id_str, similarity = known_matches[0]
            identity = (await db.execute(
                select(Identity).where(Identity.id == uuid.UUID(identity_id_str))
            )).scalar_one_or_none()
            if identity is not None:
                logger.info(f"[IDENTITY] Matched KNOWN identity {identity.id} "
                            f"(sim={similarity:.3f})")
                await self._update_identity_seen(identity, db)
                return IdentityResolution(identity, False, similarity, None, False)
            logger.warning("[IDENTITY] KNOWN match %s has no row; treating as no match",
                           identity_id_str)

        # Step 2: UNKNOWN
        unknown_matches = await self.search_vector_index(
            db, embedding, top_k=1, threshold=self.unknown_threshold,
            index_type='unknown')
        if unknown_matches:
            identity_id_str, similarity = unknown_matches[0]
            identity = (await db.execute(
                select(Identity).where(Identity.id == uuid.UUID(identity_id_str))
            )).scalar_one_or_none()
            if identity is not None:
                logger.info(f"[IDENTITY] Matched UNKNOWN identity {identity.id} "
                            f"(sim={similarity:.3f})")
                await self._update_identity_seen(identity, db)
                return IdentityResolution(identity, False, similarity, None, False)

        # Step 3: nobody matched — a new person
        logger.info("[IDENTITY_SEARCH] No match in KNOWN or UNKNOWN "
                    f"(thresholds known={self.known_threshold} "
                    f"unknown={self.unknown_threshold}); creating a new identity")
        identity, emb_id = await self._create_unknown_identity(
            embedding, pipeline_id, detection_id, db, quality_score)
        return IdentityResolution(identity, True, 0.0, emb_id, True)

    async def _create_unknown_identity(
        self,
        embedding: np.ndarray,
        pipeline_id: str,
        detection_id: Optional[int],
        db: AsyncSession,
        quality_score: Optional[float] = None,
        quality_scorer_version: Optional[str] = None
    ) -> Tuple[Identity, Optional[int]]:
        """Create a new unknown identity. Returns (identity, embedding row id or
        None when quality was below the unknown threshold)."""
        identity_id = uuid.uuid4()
        emb_id: Optional[int] = None
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
                # add_embedding predates these columns; stamp provenance on the
                # row it just created. Without this, every new identity's FIRST
                # embedding looks like a legacy-scorer row and is excluded from
                # the version-scoped best-snapshot comparison forever after.
                #
                # embedding_model_version was missing from this same patch-up
                # while save_embedding stamped it — so every identity created by
                # camera ingest carried a NULL model version, and reconciliation
                # could never converge on those rows. Both columns are set here
                # for the same reason and by the same rule as save_embedding.
                if emb_id:
                    stamp = {
                        "embedding_model_version": self.embedding_model_version,
                        "vector_index_sync_state": SYNC_SYNCED,
                    }
                    if quality_scorer_version:
                        stamp["quality_scorer_version"] = quality_scorer_version
                    await db.execute(
                        update(IdentityEmbedding)
                        .where(IdentityEmbedding.id == emb_id)
                        .values(**stamp))
                logger.debug(f"[IDENTITY] Added embedding to pgvector (emb_id={emb_id})")
            else:
                # FAISS backend. The embedding row is created first and its id
                # IS the index key — the positional faiss_id column this used
                # to null-fill was dropped in migration c5d6e7f8a9b0.
                embedding_record = IdentityEmbedding(
                    identity_id=identity_id,
                    detection_id=detection_id,
                    pipeline_id=pipeline_id,
                    faiss_index_type='unknown',
                    quality=quality_score,
                    quality_scorer_version=quality_scorer_version,
                    created_at=now
                )
                db.add(embedding_record)
                await db.flush()
                emb_id = embedding_record.id
                logger.debug("[IDENTITY] Added embedding row (key = row id)")
        else:
            logger.warning(f"[IDENTITY] ⚠️ Skipping embedding save for UNKNOWN identity (quality {quality_score:.2f} < threshold {quality_threshold:.2f})")
            logger.warning(f"[IDENTITY] This identity will NOT appear in merge suggestions/clustering!")
            logger.warning(f"[IDENTITY] Consider lowering IDENTITY_QUALITY_THRESHOLD_UNKNOWN if you need merge suggestions for low-quality faces")
        
        await db.flush()
        logger.info(f"[IDENTITY] Created UNKNOWN identity: {identity_id}")
        return identity, emb_id

    async def _find_or_create_identity_pgvector(
        self,
        embedding: np.ndarray,
        pipeline_id: str,
        detection_id: Optional[int],
        db: AsyncSession,
        quality_score: Optional[float] = None,
        quality_scorer_version: Optional[str] = None
    ) -> IdentityResolution:
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
            top_k=int(settings.IDENTITY_INGEST_TOP_K),
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
                    enriched_id = await self._maybe_enrich_identity(
                        identity, embedding, similarity, quality_score,
                        detection_id, pipeline_id, db
                    )
                    return IdentityResolution(identity, False, similarity, enriched_id, False)
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
                
                # REMOVED: a second KNOWN search at max(0.2, known_threshold - 0.1).
                #
                # It re-searched KNOWN at an effective floor of 0.30 and returned
                # that identity anyway, so SIMILARITY_THRESHOLD did not mean what
                # it says: a face the operator's configured bar had just rejected
                # was attached to a person regardless. Its stated purpose was to
                # avoid creating a duplicate UNKNOWN, but it bought that by
                # attributing faces to the wrong people at a threshold nobody
                # configured — and every such attribution then became a
                # permanent embedding under that identity via enrichment.
                #
                # A face below SIMILARITY_THRESHOLD is now what the operator said
                # it is: not this person. Clustering and merge review exist to
                # reconcile duplicate UNKNOWNs after the fact, with a human.

                # Check for type mismatch (shouldn't happen with pgvector since query filters by type)
                if identity.type == IdentityType.KNOWN:
                    logger.warning(f"[IDENTITY_SEARCH] [PGVECTOR] ⚠️ Identity {identity_id} is KNOWN but found in UNKNOWN search - returning anyway")
                
                await self._update_identity_seen(identity, db)
                return IdentityResolution(identity, False, similarity, None, False)
        else:
            logger.debug(f"[IDENTITY_SEARCH] [PGVECTOR] No match in UNKNOWN identities (threshold={self.unknown_threshold})")
        
        # Step 3: Create new UNKNOWN identity
        logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] No matches found - creating new UNKNOWN identity")
        identity, emb_id = await self._create_unknown_identity(
            embedding, pipeline_id, detection_id, db, quality_score,
            quality_scorer_version)
        logger.info(f"[IDENTITY_SEARCH] [PGVECTOR] ===== Search complete: NEW UNKNOWN identity created: {identity.id} =====")
        return IdentityResolution(identity, True, 0.0, emb_id, True)
    
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
    ) -> Optional[int]:
        """Auto-enrichment: add a confidently-matched runtime embedding to the identity.
        Returns the id of the embedding row it inserted, or None.

        Enrolled identities historically held a SINGLE view, so any pose/lighting
        deviation dropped below the match threshold. By adding high-confidence,
        sufficiently-different embeddings over time, each identity learns the
        person's appearance range. Guards: min similarity, min quality, near-duplicate
        skip, and a hard cap per identity. Never raises - enrichment is best-effort.
        """
        try:
            # OFF BY DEFAULT. Enrichment writes a runtime observation into an
            # enrolled person permanently, and the admission bar upstream is
            # SIMILARITY_THRESHOLD (0.4). One wrong attribution therefore does
            # not stay a single wrong frame — it becomes a stored vector, and
            # every later photo of that wrong person then matches the identity
            # at ~1.0. There is no provenance column distinguishing an
            # auto-learned vector from an operator-enrolled one, so the mistake
            # is also invisible afterwards. Opt in deliberately.
            if not self.auto_enrich_enabled:
                return

            min_sim = float(settings.IDENTITY_ENRICH_MIN_SIMILARITY)
            min_quality = float(settings.IDENTITY_ENRICH_MIN_QUALITY)
            # Same cap the retention job prunes to. Growing past it here just
            # queued work for the nightly sweep to undo.
            max_embeddings = int(settings.MAX_EMBEDDINGS_PER_IDENTITY)

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
            if max_sim is not None and max_sim >= float(settings.IDENTITY_NEAR_DUPLICATE_MIN):
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
                # Same patch-up as _create_unknown_identity: add_embedding does
                # not write provenance columns, so stamp them here rather than
                # leaving an enriched view indistinguishable from a legacy row.
                await db.execute(
                    update(IdentityEmbedding)
                    .where(IdentityEmbedding.id == emb_id)
                    .values(embedding_model_version=self.embedding_model_version,
                            vector_index_sync_state=SYNC_SYNCED))
            if emb_id:
                logger.info(
                    f"[ENRICH] ✅ Identity '{identity.display_name}' learned a new view "
                    f"(sim={similarity:.3f}, novelty={1 - (max_sim or 0):.3f}, embeddings={count + 1})"
                )
            return emb_id
        except Exception as e:
            logger.warning(f"[ENRICH] Enrichment failed for {identity.display_name}: {e}")
        return None
    
    async def create_appearance(
        self,
        identity: Identity,
        pipeline_id: str,
        track_id: Optional[str],
        start_time: datetime,
        best_snapshot_path: Optional[str],
        db: AsyncSession,
        quality_score: Optional[float] = None,
        similarity: float = 0.0,
        quality_scorer_version: Optional[str] = None
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
                                IdentityEmbedding.quality.isnot(None),
                                # Same scorer only. Values from the legacy
                                # scorer sat near 0.5 by construction, so
                                # comparing a new-scale score against them would
                                # make EVERY new frame win and the best snapshot
                                # thrash to whatever arrived last.
                                IdentityEmbedding.quality_scorer_version
                                == quality_scorer_version,
                            )
                        )
                        best_quality = best_quality_result.scalar_one_or_none()
                        
                        # Compare quality scores - update if new quality is better
                        if best_quality is None or quality_score > best_quality:
                            should_update = True
                            # `{x:.3f if x else 'N/A'}` is NOT a conditional —
                            # it is an invalid format spec, and an f-string is
                            # built eagerly regardless of log level, so this
                            # line raised ValueError every time it ran.
                            best_display = f"{best_quality:.3f}" if best_quality is not None else "N/A"
                            logger.debug(f"[IDENTITY] Updating appearance best snapshot: new quality {quality_score:.3f} > best {best_display}")
                    elif similarity >= self.snapshot_replace_min_similarity:
                        # The same rule as the identity snapshot, and for the
                        # same reason: `similarity > 0.0` is true for
                        # essentially every match, so the appearance thumbnail
                        # became the most recent frame regardless of who it
                        # showed.
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
        
        # Update identity's best_snapshot_path if new one is better.
        #
        # CONTRACT: identities.best_snapshot_path is the REPRESENTATIVE FACE
        # CROP — the gallery primary for an enrolled person, else the best
        # observed aligned crop. Per-sighting evidence lives in
        # faces.face_image_path / identity_appearances.best_snapshot_path and
        # is never affected by this block. Full frames are never persisted.
        if best_snapshot_path:
            logger.info(f"[IDENTITY] 📸 Checking if best_snapshot_path should be updated for {identity.display_name} (ID: {identity.id})")
            logger.info(f"[IDENTITY]   Current best_snapshot_path: {identity.best_snapshot_path}")
            logger.info(f"[IDENTITY]   New best_snapshot_path: {best_snapshot_path}")

            # A KNOWN person's representative image is governed by enrollment,
            # not by whatever the cameras saw last. Observed live before this
            # guard existed: "asasa" was promoted and, ten seconds later, a
            # routine re-recognition re-pointed the avatar at a pipeline file
            # the gallery does not own — the enrolled photo silently stopped
            # being the face of that person in every search result and card.
            known_gate_active = False
            if (identity.type == IdentityType.KNOWN
                    and identity.best_snapshot_path):
                has_primary = (await db.execute(
                    select(func.count(IdentityImage.id)).where(
                        IdentityImage.identity_id == identity.id,
                        IdentityImage.is_primary.is_(True)))).scalar() or 0
                if has_primary:
                    # Enrolled provenance wins outright: upload, promotion,
                    # merge and manual enrollment all set a gallery primary,
                    # and ingest must never displace it.
                    logger.debug(
                        f"[IDENTITY]   🔒 KNOWN identity has a gallery primary - "
                        f"best_snapshot_path is frozen against ingest")
                    best_snapshot_path = None  # falls through; nothing below runs
                else:
                    # No gallery yet (e.g. legacy known): a camera crop may
                    # still represent them, but only through the KNOWN quality
                    # bar — never on similarity alone, which any correct
                    # re-recognition trivially clears.
                    known_gate_active = True

        if best_snapshot_path:
            should_update_identity = False
            if not identity.best_snapshot_path:
                should_update_identity = True
                logger.info(f"[IDENTITY]   ✅ No existing snapshot - will set new one")
            elif known_gate_active and (
                    quality_score is None
                    or quality_score < float(settings.IDENTITY_QUALITY_THRESHOLD_KNOWN)):
                # KNOWN without a gallery: an unscored candidate, or one below
                # the same bar that gates saving a KNOWN embedding, cannot
                # become the face of the person. The similarity fallback is
                # deliberately unreachable for KNOWN identities.
                logger.debug(
                    f"[IDENTITY]   ⏭️ KNOWN gate: candidate quality "
                    f"{quality_score} below IDENTITY_QUALITY_THRESHOLD_KNOWN - keeping snapshot")
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
                    best_display = f"{best_quality:.3f}" if best_quality is not None else "N/A"
                    logger.info(f"[IDENTITY]   ✅ Updating identity best snapshot: new quality {quality_score:.3f} > best {best_display}")
                else:
                    logger.info(f"[IDENTITY]   ⏭️ Keeping existing snapshot: new quality {quality_score:.3f} <= best {best_quality:.3f}")
            elif known_gate_active:
                # Quality passed the KNOWN bar but there is nothing stored to
                # compare against and no similarity shortcut is allowed.
                # Unreachable in practice (the branch above covers it), kept
                # explicit so the similarity fallback below is provably
                # UNKNOWN-only.
                logger.debug("[IDENTITY]   ⏭️ KNOWN gate: no similarity fallback")
            elif similarity >= self.snapshot_replace_min_similarity:
                # No quality score to compare, so similarity is the only signal
                # available — but it must clear a real bar.
                #
                # This branch used to read `similarity > 0.0`, which is true for
                # essentially every match, so the identity's avatar became
                # whatever arrived last. Combined with enrollment writing
                # quality=None, that meant a CORRECT 100% match could display a
                # completely different person's photo: the score was right and
                # the picture was wrong. best_snapshot_path feeds _build_match's
                # snapshot_url, i.e. the face shown in every search result.
                should_update_identity = True
                logger.info(f"[IDENTITY]   ✅ Updating identity best snapshot: similarity {similarity:.3f}")
            else:
                logger.debug(
                    f"[IDENTITY]   ⏭️ Keeping existing snapshot: similarity "
                    f"{similarity:.3f} below replace floor "
                    f"{self.snapshot_replace_min_similarity}")

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
    
    async def search_vector_index(self, db: AsyncSession, embedding: np.ndarray,
                                  *, top_k: int = 1, threshold: float = 0.0,
                                  index_type: str = 'known'):
        """Search the index, then resolve results through PostgreSQL.

        The index speaks EMBEDDING KEYS (identity_embeddings.id) and nothing
        else — it has no notion of a person. Turning keys into identities is
        done here, against the database:

            key -> identity_embeddings row -> identity (ACTIVE only)

        and multiple embeddings of the same person collapse to that person's
        best score. Keeping identity resolution out of the index is what lets
        the index implementation change without touching a schema or a route.

        Returns [(identity_id, similarity)] — the shape callers already expect.
        """
        index = getattr(self, 'vector_index', None)
        if index is None:
            return []

        # Over-fetch: several keys can belong to one identity, so top_k
        # identities needs more than top_k keys.
        fetch_k = max(int(top_k) * 5, int(top_k))
        try:
            if hasattr(index, 'search_async'):
                hits = await index.search_async(db, embedding, top_k=fetch_k,
                                                threshold=threshold)
            else:
                hits = index.search(embedding, top_k=fetch_k, threshold=threshold)
        except Exception as exc:
            logger.error("[IDENTITY] vector index search failed: %s", exc, exc_info=True)
            return []
        if not hits:
            return []

        keys = [key for key, _score in hits]
        rows = (await db.execute(
            sa_text(
                "SELECT e.id, e.identity_id, i.type "
                "FROM identity_embeddings e JOIN identities i ON i.id = e.identity_id "
                "WHERE e.id = ANY(:keys) AND i.status::text IN ('ACTIVE', 'PROMOTED')"),
            {"keys": keys})).all()
        owner = {int(r[0]): (str(r[1]), str(r[2]).lower()) for r in rows}

        wanted = 'known' if index_type == 'known' else 'unknown'
        best: Dict[str, float] = {}
        for key, score in hits:
            entry = owner.get(int(key))
            if entry is None:
                # Indexed key with no ACTIVE row: stale. Reconciliation removes
                # it; skipping here means it can never win a match meanwhile.
                continue
            identity_id, identity_type = entry
            if identity_type != wanted:
                continue
            if score > best.get(identity_id, float('-inf')):
                best[identity_id] = float(score)

        ranked = sorted(best.items(), key=lambda item: item[1], reverse=True)
        return ranked[:int(top_k)]

    async def remove_from_vector_index(self, db: AsyncSession, identity_id) -> int:
        """Drop every indexed vector belonging to an identity."""
        index = getattr(self, 'vector_index', None)
        if index is None:
            return 0
        rows = (await db.execute(
            sa_text("SELECT id FROM identity_embeddings WHERE identity_id = :i"),
            {"i": str(identity_id)})).all()
        keys = [int(r[0]) for r in rows]
        if not keys:
            return 0
        try:
            return int(index.remove(keys))
        except Exception as exc:
            logger.warning("[IDENTITY] could not remove %s from the index: %s",
                           identity_id, exc)
            return 0

    @staticmethod
    def _resolve_model_version() -> Optional[str]:
        """Identifier for the recognition model that produces our vectors.

        Derived from the configured weights file (e.g. 'w600k_r50'), so changing
        the model changes the stamp without anyone having to remember to bump a
        constant. Returns None rather than guessing if it cannot be determined —
        unknown provenance is recorded honestly.
        """
        import os as _os

        path = settings.RECOGNITION_MODEL
        if not path:
            return None
        name = _os.path.splitext(_os.path.basename(str(path)))[0]
        return name[:64] or None

    @staticmethod
    def _normalize_for_storage(embedding: np.ndarray) -> np.ndarray:
        """L2-normalized float32 copy, or ValueError for an unusable vector.

        Every stored vector is unit-length so inner product IS cosine similarity,
        and callers cannot smuggle in a zero/NaN vector that would silently
        occupy an index slot forever.
        """
        vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if not np.all(np.isfinite(vector)):
            raise ValueError("embedding contains non-finite values")
        norm = float(np.linalg.norm(vector))
        if norm <= 0.0 or not np.isfinite(norm):
            raise ValueError("embedding has zero magnitude")
        return (vector / norm).astype(np.float32)

    async def save_embedding(
        self,
        identity: Identity,
        embedding: np.ndarray,
        detection_id: Optional[int],
        pipeline_id: Optional[str],   # None = not a camera sighting (enrollment/preload)
        quality_score: Optional[float],
        db: AsyncSession,
        *,
        defer_commit: bool = False,
        quality_scorer_version: Optional[str] = None
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
            defer_commit: hand the transaction back to the caller — see below

        Returns:
            IdentityEmbedding record if saved, None if skipped due to quality

        ``defer_commit`` exists for callers that must keep this write inside a
        larger atomic unit. `enroll_image` is the motivating case: it moves the
        uploaded file into place immediately before ITS commit, so a commit
        taken here would split enrollment across two transactions and leave a
        committed `identity_images` row pointing at a file that never landed.

        When deferred, this defers BOTH the commit and the index call — never
        just the commit. Calling `vector_index.add()` against an uncommitted row
        would leave a phantom index entry keyed to an id that a rollback then
        erases, which is worse than the ordering bug it would be fixing. The row
        is left `vector_index_sync_state='pending'`, which is precisely the state
        reconciliation exists to repair, and the caller is expected to call
        `sync_pending_embedding()` after its own commit.
        """
        # Use appropriate quality threshold based on identity type
        quality_threshold = self.quality_threshold_known if identity.type == IdentityType.KNOWN else self.quality_threshold_unknown
        
        if quality_score is not None and quality_score < quality_threshold:
            logger.debug(f"[IDENTITY] Skipping embedding save (quality {quality_score:.2f} < threshold {quality_threshold:.2f} for {identity.type.value})")
            return None
        
        # Determine which index to use
        index_type = 'known' if identity.type == IdentityType.KNOWN else 'unknown'
        
        if self.use_pgvector and self.pgvector_index:
            # pgvector backend - store embedding directly in PostgreSQL.
            #
            # `defer_commit` is intentionally inert here: this branch only ever
            # flushes, never commits, so the caller already owns the transaction
            # boundary. Do not "fix" this by adding a commit — enroll_image
            # depends on the absence of one.
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
                if embedding_record is not None:
                    # Under pgvector the row IS the index — storing the vector and
                    # indexing it are the same act, so it is synchronized by
                    # construction and there is no window to reconcile.
                    embedding_record.vector_index_sync_state = 'synced'
                    embedding_record.embedding_model_version = self.embedding_model_version
                    if quality_scorer_version is not None:
                        embedding_record.quality_scorer_version = quality_scorer_version
                    await db.flush()
                logger.debug(f"[IDENTITY] [PGVECTOR] Saved embedding: id={emb_id}")
                return embedding_record
            else:
                logger.warning(f"[IDENTITY] [PGVECTOR] Failed to save embedding for identity {identity.id}")
                return None
        else:
            # FAISS backend. PostgreSQL is the source of truth and the index is a
            # disposable acceleration layer, so the ORDER IS FIXED:
            #
            #   persist the vector with state='pending'  ->  COMMIT
            #     ->  synchronize the index  ->  mark 'synced' (or 'failed')
            #
            # This previously ran backwards — it called add_known() FIRST and
            # never wrote `embedding` at all, so the vector existed only inside
            # the index file. That is what made a lost index unrecoverable and
            # forced "rebuild from database" to reconstruct from the very index
            # it was rebuilding.
            #
            # A failure to synchronize NEVER deletes or rolls back the committed
            # vector; it only moves this row's state to 'failed' for
            # reconciliation to retry.
            logger.debug(f"[IDENTITY] [FAISS] Saving embedding: identity={str(identity.id)[:8]}..., type={index_type}, quality={quality_score}")

            normalized = self._normalize_for_storage(embedding)
            embedding_record = IdentityEmbedding(
                identity_id=identity.id,
                detection_id=detection_id,
                pipeline_id=pipeline_id,
                embedding=normalized.tolist(),      # authoritative copy
                faiss_index_type=index_type,
                quality=quality_score,
                quality_scorer_version=quality_scorer_version,
                vector_index_sync_state='pending',
                embedding_model_version=self.embedding_model_version,
                created_at=datetime.utcnow()
            )
            db.add(embedding_record)
            await db.flush()          # assigns embedding_record.id — the index key

            if defer_commit:
                # The caller owns the transaction. Return with the row pending
                # and the index untouched: indexing an uncommitted key would
                # survive a rollback as a phantom entry. The caller commits and
                # then calls sync_pending_embedding(); if it never does,
                # reconciliation adds the vector within its interval anyway.
                logger.debug(
                    "[IDENTITY] [FAISS] Embedding %s staged pending; caller owns "
                    "the commit and the index sync", embedding_record.id)
                return embedding_record

            await db.commit()         # the vector is durable BEFORE any index call

            # Index synchronization is best-effort from here on. The key is the
            # database row id, so the entry survives any rebuild.
            try:
                if self.vector_index is None:
                    raise RuntimeError("no vector index is configured")
                self.vector_index.add(embedding_record.id, normalized,
                                      model_version=self.embedding_model_version)
                embedding_record.vector_index_sync_state = 'synced'
            except Exception as exc:
                embedding_record.vector_index_sync_state = 'failed'
                logger.error(
                    "[IDENTITY] [FAISS] index sync failed for embedding %s "
                    "(vector is committed and safe; reconciliation will retry): %s",
                    embedding_record.id, exc, exc_info=True)
            try:
                await db.commit()
            except Exception as exc:
                await db.rollback()
                logger.warning("[IDENTITY] [FAISS] could not record sync state for "
                               "embedding %s: %s", embedding_record.id, exc)

            logger.debug(f"[IDENTITY] [FAISS] Saved embedding: id={embedding_record.id} "
                         f"state={embedding_record.vector_index_sync_state}")
            return embedding_record

    async def sync_pending_embedding(self, embedding_id: int,
                                     vector: np.ndarray,
                                     db: AsyncSession) -> str:
        """Index a row that `save_embedding(defer_commit=True)` left pending.

        Call this only AFTER the caller's transaction has committed — that
        ordering is the whole point of the deferred path.

        Never raises. The vector is already durable in PostgreSQL, so a failure
        here costs latency, not data: the row stays `pending`/`failed`,
        `fr_vector_index_pending` reports it, and reconciliation re-adds it
        within `VECTOR_INDEX_RECONCILE_INTERVAL_SECONDS`. This call exists so a
        freshly enrolled person is findable immediately rather than within the
        hour — it is an optimization over a guarantee that already holds.

        Returns the resulting sync state: 'synced' or 'failed'.
        """
        # Under pgvector the row IS the index; there is nothing to synchronize.
        if self.use_pgvector and self.pgvector_index:
            return SYNC_SYNCED

        state = SYNC_SYNCED
        try:
            if self.vector_index is None:
                raise RuntimeError("no vector index is configured")
            normalized = self._normalize_for_storage(vector)
            self.vector_index.add(int(embedding_id), normalized,
                                  model_version=self.embedding_model_version)
        except Exception as exc:                                  # noqa: BLE001
            state = SYNC_FAILED
            logger.error(
                "[IDENTITY] [FAISS] deferred index sync failed for embedding %s "
                "(the vector is committed and safe; reconciliation will retry): %s",
                embedding_id, exc, exc_info=True)

        try:
            await db.execute(
                sa_text("UPDATE identity_embeddings SET vector_index_sync_state = :s "
                        "WHERE id = :i"),
                {"s": state, "i": int(embedding_id)})
            await db.commit()
        except Exception as exc:                                  # noqa: BLE001
            await self._rollback_quietly(db)
            logger.warning(
                "[IDENTITY] [FAISS] could not record sync state for embedding %s: %s",
                embedding_id, exc)
        return state

    @staticmethod
    async def _rollback_quietly(db: AsyncSession) -> None:
        try:
            await db.rollback()
        except Exception:                                         # noqa: BLE001
            pass


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
        
        # ONE path, both backends: promotion is a database fact.
        #
        # `search_vector_index` reads `identities.type` out of PostgreSQL when
        # it resolves keys, so the index is already correct the moment this
        # transaction commits and every embedding key stays where it was. The
        # old FAISS branch instead reconstructed each vector out of the UNKNOWN
        # index, added it to the KNOWN index and deleted the original — 380
        # lines that could lose a vector outright if the process died mid-move,
        # because the index was then the only copy.
        return await self._promote_identity_in_db(
            identity, identity_id, display_name, user_id, db)

    async def _promote_identity_in_db(
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
        
        # Step 3: adopt the best snapshot into the identity's UUID folder
        # (same shared implementation as the non-pgvector path above).
        best_image_path = identity.best_snapshot_path
        if best_image_path:
            from backend.core.enrollment_service import adopt_existing_file

            adopted = await adopt_existing_file(
                db, identity, best_image_path, source_type="promotion")
            if adopted is not None:
                identity.best_snapshot_path = adopted.storage_path
                logger.info(f"[IDENTITY_PROMOTE] [PGVECTOR]   ✅ Adopted image -> {adopted.storage_path}")
                await db.execute(
                    update(IdentityAppearance).where(
                        IdentityAppearance.identity_id == identity_id,
                        IdentityAppearance.best_snapshot_path == best_image_path
                    ).values(
                        best_snapshot_path=adopted.storage_path
                    )
                )
            else:
                logger.warning("[IDENTITY_PROMOTE] [PGVECTOR]   ⚠️ Could not adopt best image")
        else:
            logger.warning("[IDENTITY_PROMOTE] [PGVECTOR]   ⚠️ No best image path available")
        
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
        _count_identity_op("promote")
        return identity
    
    async def _snapshot_quality(self, db: AsyncSession, absolute_path: str):
        """(quality, source) for the EXACT file at absolute_path, or (None, reason).

        The quality that gates gallery adoption must belong to the file being
        adopted — never to "an" embedding of the same identity, whose score says
        nothing about this particular frame. Two honest sources, in order:

          1. A stored identity_images row whose storage_path matches this exact
             path (matched on the normalized relative form, which is how
             gallery rows store it). That is the one table pairing a path with
             a quality, and its value is authoritative when present.
          2. Otherwise, score the file itself with the same scorer the pipeline
             uses: decode -> best-face detect -> assess_face_quality. The
             result provably describes this file.

        Returns (None, "unavailable_snapshot_metadata") when neither works —
        the caller then completes the merge and adopts nothing, because a
        snapshot that cannot be judged must not enter a gallery on hope.
        """
        from backend.core.enrollment_service import decode_and_validate_image
        from backend.utils.path_utils import normalize_storage_path
        from db_models import IdentityImage

        relative = normalize_storage_path(absolute_path)
        if relative:
            stored = (await db.execute(
                select(IdentityImage.quality_score)
                .where(IdentityImage.storage_path == relative,
                       IdentityImage.quality_score.isnot(None))
                .limit(1))).scalar()
            if stored is not None:
                return float(stored), "stored"

        try:
            with open(absolute_path, "rb") as handle:
                payload = handle.read()
            image = decode_and_validate_image(payload)

            from backend.core.face_extraction import extract_single_face
            from backend.core.face_quality import assess_face_quality
            # on_multiple="best": read-only scoring of the largest face, the
            # same contract the other read paths use. Pipeline snapshots are
            # tight crops, so allow the padded retry exactly as promote does.
            face = extract_single_face(image, on_multiple="best",
                                       allow_padded_retry=True)
            bbox = face.bbox_int(image.shape)
            crop = image[bbox[1]:bbox[3], bbox[0]:bbox[2]]
            assessment = assess_face_quality(crop, bbox=bbox,
                                             landmarks=getattr(face, "landmarks", None),
                                             full_image=image)
            score = getattr(assessment, "overall_score", None)
            if score is None:
                return None, "unavailable_snapshot_metadata"
            return float(score), "scored"
        except Exception as exc:                               # noqa: BLE001
            logger.info("[MERGE] snapshot could not be scored (%s): %s",
                        absolute_path, exc)
            return None, "unavailable_snapshot_metadata"

    async def _consolidate_loser_assets(
        self,
        db: AsyncSession,
        from_identity: Identity,
        to_identity: Identity,
        copied_files: Optional[list] = None,
        loser_status_before: Optional[IdentityStatus] = None,
    ) -> dict:
        """Move the loser's gallery onto the winner and record what moved.

        Runs BEFORE the blanket re-parenting UPDATEs, inside the caller's
        transaction. Returns the provenance dict stored on the IdentityMerge
        row — the record that makes a future unmerge possible.

        Two database constraints shape this (db_models.py identity_images):

          * uq_identity_image_checksum — (identity_id, file_checksum) unique.
            When the winner already owns the same photo, the loser's row is NOT
            moved (it would collide); instead any embeddings referencing it are
            re-pointed at the winner's copy, and the duplicate row stays on the
            soft-deleted loser. Recorded as `deduplicated_into`.
          * uq_identity_image_one_primary — at most one primary per identity.
            Every loser primary is demoted before any row changes owner, so
            the winner's own primary is never contested.

        Files are COPIED via enrollment_service.consolidate_image_file (the
        only module allowed to place files under FACES_DIR); the loser's folder
        is never touched, so no failure here can lose a file. A source file
        that cannot be copied still has its ROW re-parented, keeping its old
        path — readable precisely because the loser folder survives. Absolute
        paths of successful copies are appended to `copied_files` so the route
        can unlink them if the transaction later fails to commit.
        """
        from backend.core.enrollment_service import (
            consolidate_image_file, pending_absolute_path)
        from db_models import IdentityImage

        from_identity_id = from_identity.id
        to_identity_id = to_identity.id
        status_before = (loser_status_before if loser_status_before is not None
                         else from_identity.status)

        # Row ids per table, captured BEFORE the UPDATEs overwrite identity_id
        # — afterwards nothing can tell these rows from the winner's own.
        async def _ids(sql: str) -> list:
            rows = (await db.execute(sa_text(sql), {"i": str(from_identity_id)})).all()
            return [r[0] for r in rows]

        provenance: dict = {
            "appearance_ids": await _ids(
                "SELECT id FROM identity_appearances WHERE identity_id = :i"),
            "embedding_ids": await _ids(
                "SELECT id FROM identity_embeddings WHERE identity_id = :i"),
            "face_ids": await _ids(
                "SELECT id FROM faces WHERE identity_id = :i"),
            "images": [],
            "loser_type": str(getattr(from_identity.type, "value", from_identity.type) or ""),
            "loser_display_name": from_identity.display_name,
            # The status to restore on unmerge. Without it an unmerge would
            # have to assume ACTIVE, which would resurrect a retired
            # (INACTIVE) identity or silently demote a PROMOTED one. Merge
            # rows written before this key existed are refused by unmerge
            # rather than guessed at.
            #
            # Taken from loser_status_before, NOT from from_identity.status:
            # both callers set the loser to MERGED before calling this, so
            # reading it live records the value that is impossible to restore.
            "loser_status": str(getattr(status_before, "value", status_before) or ""),
        }

        winner_images = (await db.execute(
            select(IdentityImage).where(IdentityImage.identity_id == to_identity_id)
        )).scalars().all()
        winner_by_checksum = {img.file_checksum: img for img in winner_images}

        loser_images = (await db.execute(
            select(IdentityImage).where(IdentityImage.identity_id == from_identity_id)
        )).scalars().all()

        for image in loser_images:
            was_primary = bool(image.is_primary)
            record = {
                "id": image.id,
                "original_path": image.storage_path,
                "new_path": None,
                "file_copied": False,
                "was_primary": was_primary,
                "deduplicated_into": None,
            }

            duplicate = winner_by_checksum.get(image.file_checksum)
            if duplicate is not None:
                # The winner already owns this exact photo. Moving the row
                # would violate uq_identity_image_checksum, so re-point any
                # embeddings at the winner's copy and leave the duplicate row
                # on the soft-deleted loser.
                await db.execute(
                    update(IdentityEmbedding)
                    .where(IdentityEmbedding.image_id == image.id)
                    .values(image_id=duplicate.id))
                record["deduplicated_into"] = duplicate.id
                provenance["images"].append(record)
                continue

            new_path, copied = consolidate_image_file(image.storage_path, to_identity_id)
            image.identity_id = to_identity_id
            # Demoted unconditionally: the winner's primary (if any) keeps its
            # slot, and uq_identity_image_one_primary can never be violated.
            image.is_primary = False
            if copied and new_path:
                image.storage_path = new_path
                record["new_path"] = new_path
                record["file_copied"] = True
                if copied_files is not None:
                    try:
                        copied_files.append(pending_absolute_path(new_path))
                    except Exception:                          # noqa: BLE001
                        pass
                # The winner now owns this checksum: a second loser image with
                # the same bytes must deduplicate against it, not collide.
                winner_by_checksum[image.file_checksum] = image
            # copy failed: the row moves with its OLD path, which stays
            # readable because the loser folder is never deleted.
            provenance["images"].append(record)

        # ------------------------------------------------------------------
        # Fallback: the loser brought NO gallery. Unknowns never have
        # identity_images rows (galleries are created at promotion), so before
        # this step an unknown->known merge left the winner's profile without
        # the very photo that justified the merge, pointing forever into a
        # pipeline unknown/ folder that routine cleanup deletes.
        #
        # Adopt exactly ONE image — best_snapshot_path — and only when THAT
        # FILE passes the KNOWN gallery bar. The merge decision ("same
        # person?") and the adoption decision ("good enough to keep?") are
        # separate questions; a 0.95 similarity does not make a blurry frame a
        # profile photo. Every failure here completes the merge and adopts
        # nothing.
        # ------------------------------------------------------------------
        provenance["loser_best_snapshot_path"] = from_identity.best_snapshot_path
        provenance["adopted_snapshot"] = None

        def _skip(reason: str) -> dict:
            provenance["snapshot_adoption"] = {"eligible": False, "reason": reason}
            logger.info("[MERGE] snapshot not adopted for %s -> %s: %s",
                        from_identity_id, to_identity_id, reason)
            return provenance

        if loser_images:
            # The normal consolidation above already moved a real gallery;
            # adding best_snapshot_path on top would double-import.
            provenance["snapshot_adoption"] = {"eligible": False,
                                               "reason": "loser_has_gallery"}
            return provenance

        if not from_identity.best_snapshot_path:
            return _skip("missing_snapshot")

        from backend.utils.path_utils import normalize_storage_path
        try:
            relative = normalize_storage_path(from_identity.best_snapshot_path)
            absolute = pending_absolute_path(
                relative or str(from_identity.best_snapshot_path))
        except Exception:                                      # noqa: BLE001
            return _skip("unsafe_storage_path")

        if not os.path.isfile(absolute):
            return _skip("missing_snapshot_file")

        quality, quality_source = await self._snapshot_quality(db, absolute)
        if quality is None:
            return _skip("unavailable_snapshot_metadata")

        # The KNOWN bar, because the image is entering a KNOWN gallery. Using
        # the loser's unknown bar (0.1) would be a merge-specific lowering: an
        # unknown snapshot only ever had to clear 0.1 to exist as evidence.
        threshold = float(settings.IDENTITY_QUALITY_THRESHOLD_KNOWN)
        if quality < threshold:
            skipped = _skip("below_face_quality_threshold")
            skipped["snapshot_adoption"]["quality_score"] = round(quality, 4)
            skipped["snapshot_adoption"]["threshold"] = threshold
            return skipped

        from backend.core.enrollment_service import adopt_existing_file

        # Ids the winner owned BEFORE adoption. adopt_existing_file returns the
        # EXISTING row on a checksum-dedup hit; only a row whose id was not in
        # this set was created by this merge. That distinction is what keeps a
        # later rollback from unlinking a file the winner owned all along.
        existing_ids = {
            row[0] for row in (await db.execute(
                select(IdentityImage.id)
                .where(IdentityImage.identity_id == to_identity_id))).all()
        }

        adopted = await adopt_existing_file(db, to_identity, absolute,
                                            source_type="merge")
        if adopted is None:
            return _skip("unavailable_snapshot_metadata")

        created_by_merge = adopted.id not in existing_ids
        if created_by_merge:
            # Persist the score that was measured from THIS file. This is the
            # path<->quality link that was missing for unknowns; the next merge
            # or unmerge reads stored metadata instead of rescoring. A deduped
            # row keeps its own trusted metadata untouched.
            from backend.core.face_quality import QUALITY_SCORER_VERSION
            if quality_source == "scored":
                adopted.quality_score = float(quality)
                adopted.quality_scorer_version = QUALITY_SCORER_VERSION
            if copied_files is not None:
                try:
                    copied_files.append(pending_absolute_path(adopted.storage_path))
                except Exception:                              # noqa: BLE001
                    pass

        provenance["adopted_snapshot"] = {
            "image_id": adopted.id,
            "source_path": from_identity.best_snapshot_path,
            "new_path": adopted.storage_path,
            "created_by_merge": created_by_merge,
            "quality_score": round(float(quality), 4),
            "quality_source": quality_source,
            # adopt_existing_file sets is_primary only when the winner had no
            # primary at all. Unmerge verifies the flag against THIS value: a
            # blanket "expect False" would misread a legitimate adoption as an
            # administrator having promoted the image after the merge.
            "became_primary": bool(adopted.is_primary),
        }
        provenance["snapshot_adoption"] = {"eligible": True}
        logger.info("[MERGE] snapshot adopted for %s -> %s: image_id=%s "
                    "created=%s quality=%.3f (%s)",
                    from_identity_id, to_identity_id, adopted.id,
                    created_by_merge, quality, quality_source)
        return provenance

    async def _gate_merge_compatibility(
        self,
        db: AsyncSession,
        identity_ids: list,
        user_id: Optional[int],
        confirm_merge_risk: bool,
    ):
        """The one merge-safety choke point, run BEFORE any mutation.

        Blocks (raises MergeCompatibilityBlocked) when the identities are not
        visually defensible as one person and the caller has not explicitly
        overridden. On an override the assessment is RECOMPUTED here — a
        previously displayed frontend score is never trusted — and the
        override is written to the audit trail inside the caller's
        transaction, so it commits with the merge or not at all.
        """
        from backend.core.merge_compatibility import (
            MergeCompatibilityBlocked, assess_merge_compatibility)

        assessment = await assess_merge_compatibility(db, identity_ids)
        if not assessment.requires_confirmation:
            return assessment
        if not confirm_merge_risk:
            raise MergeCompatibilityBlocked(assessment)

        from db_models import IdentityAuditLog, User as UserModel
        username = None
        if user_id is not None:
            username = (await db.execute(
                select(UserModel.username).where(UserModel.id == user_id))).scalar()
        db.add(IdentityAuditLog(
            user_id=user_id,
            username=username or "system",
            action_type="merge_risk_override",
            identity_id=uuid.UUID(str(identity_ids[-1])),
            related_identity_id=uuid.UUID(str(identity_ids[0])),
            action_details={
                "identity_ids": [str(i) for i in identity_ids],
                "override": True,
                **assessment.to_dict(),
            },
            success=True,
            created_at=datetime.utcnow(),
        ))
        logger.warning(
            "[MERGE] risk override by user_id=%s for %s: %s robust=%s threshold=%s",
            user_id, [str(i) for i in identity_ids], assessment.risk_level,
            assessment.robust_similarity, assessment.threshold)
        return assessment

    async def merge_identities(
        self,
        from_identity_id: uuid.UUID,
        to_identity_id: uuid.UUID,
        user_id: int,
        notes: Optional[str],
        db: AsyncSession,
        copied_files: Optional[list] = None,
        confirm_merge_risk: bool = False,
    ) -> Identity:
        """
        Merge two identities into one.
        All data from from_identity is moved to to_identity.

        Refuses with MergeCompatibilityBlocked (no mutation) when the two
        identities do not look like the same person, unless
        confirm_merge_risk explicitly overrides — see _gate_merge_compatibility.
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

        # SAFETY GATE — must precede every write in this method.
        await self._gate_merge_compatibility(
            db, [from_identity_id, to_identity_id], user_id, confirm_merge_risk)

        logger.info(f"[IDENTITY] Merging identity {from_identity_id} into {to_identity_id}")

        # Captured before the overwrite below: this is the status an unmerge
        # restores, and reading it afterwards would only ever yield MERGED.
        loser_status_before = from_identity.status

        # Update from_identity
        from_identity.status = IdentityStatus.MERGED
        # the loser can no longer be acted on: retire its pending suggestions now
        await invalidate_merge_suggestions(db, [from_identity_id], f"identity merged into {to_identity_id}")
        from_identity.merged_into_id = to_identity_id
        from_identity.updated_at = datetime.utcnow()

        # Gallery consolidation + provenance capture. Must run BEFORE the
        # blanket UPDATEs below: it records which rows belonged to the loser,
        # and after the UPDATEs nothing can tell them apart from the winner's.
        provenance = await self._consolidate_loser_assets(
            db, from_identity, to_identity, copied_files,
            loser_status_before=loser_status_before)

        # Watchlist membership + live alerts follow the person (same TX)
        provenance.update(await transfer_watchlist_membership(db, from_identity_id, to_identity_id))

        # Move appearances
        await db.execute(
            update(IdentityAppearance).where(
                IdentityAppearance.identity_id == from_identity_id
            ).values(identity_id=to_identity_id)
        )

        # Move embeddings
        embeddings_result = await db.execute(
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

        # ---- consolidate the sighting window + vector labels ---------------
        #
        # merge_multiple_identities has done both of these for as long as it
        # has existed; this pairwise path did neither, so the SAME user action
        # ("merge these people") produced different data depending on whether
        # two or three identities were selected. Found by the lifecycle audit:
        # after a pairwise merge the winner still claimed its own, older
        # last_seen_at even when the loser had been seen days later, and an
        # absorbed unknown's vectors kept their 'unknown' label.
        #
        # Both identities' timestamps are real sighting history, so the merged
        # person's window is the union of the two. The winner's prior values
        # go into provenance first so unmerge can put them back exactly.
        provenance["winner_first_seen_before"] = (
            to_identity.first_seen_at.isoformat()
            if to_identity.first_seen_at else None)
        provenance["winner_last_seen_before"] = (
            to_identity.last_seen_at.isoformat()
            if to_identity.last_seen_at else None)
        seen_first = [t for t in (to_identity.first_seen_at,
                                  from_identity.first_seen_at) if t]
        seen_last = [t for t in (to_identity.last_seen_at,
                                 from_identity.last_seen_at) if t]
        if seen_first:
            to_identity.first_seen_at = min(seen_first)
        if seen_last:
            to_identity.last_seen_at = max(seen_last)

        # Vectors absorbed by a KNOWN/PROMOTED person must carry the 'known'
        # label — same rule as promote, and as the multi-merge. The label is
        # cosmetic under pgvector (search resolves type from identities.type)
        # but decides WHICH index a vector loads into under FAISS, so a stale
        # 'unknown' would silently drop these vectors from known-search after
        # a restart. Only the repointed rows are touched, and their ids are
        # recorded so unmerge can flip them back.
        provenance["relabelled_embedding_ids"] = []
        if to_identity.type != IdentityType.UNKNOWN and provenance.get("embedding_ids"):
            relabelled = (await db.execute(
                update(IdentityEmbedding)
                .where(IdentityEmbedding.id.in_(provenance["embedding_ids"]),
                       IdentityEmbedding.faiss_index_type == 'unknown')
                .values(faiss_index_type='known')
                .returning(IdentityEmbedding.id))).scalars().all()
            provenance["relabelled_embedding_ids"] = [int(i) for i in relabelled]
            if relabelled:
                logger.info("[IDENTITY] [MERGE] relabelled %d absorbed vector(s) "
                            "unknown -> known", len(relabelled))

        # Create merge audit record
        from db_models import IdentityMerge
        merge_record = IdentityMerge(
            from_identity_id=from_identity_id,
            to_identity_id=to_identity_id,
            merged_by=user_id,
            merged_at=datetime.utcnow(),
            notes=notes,
            provenance=provenance,
        )
        db.add(merge_record)

        # No vector-index removal, on EITHER backend, and that is correct:
        # index keys are identity_embeddings.id, which the UPDATE above did not
        # change — they now resolve to the winner, and search excludes the
        # MERGED loser by joining identities.status. The old FAISS branch here
        # called remove_from_vector_index AFTER the re-parent, so its
        # SELECT ... WHERE identity_id=<loser> matched zero rows and it removed
        # nothing — while logging "Removed N vector(s)". Say what actually
        # happened instead.
        logger.info(
            "[IDENTITY] Re-parented %d embeddings from %s to %s; embedding IDs "
            "remain valid and no vector deletion was required",
            embeddings_result.rowcount or 0, from_identity_id, to_identity_id)

        await db.flush()
        logger.info(f"[IDENTITY] Successfully merged identity {from_identity_id} into {to_identity_id}")
        _count_identity_op("merge")
        return to_identity

    async def unmerge_identity(
        self,
        merge_id: int,
        *,
        user_id: int,
        username: str,
        db: AsyncSession,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        notes: Optional[str] = None,
        files_to_delete: Optional[list] = None,
    ) -> dict:
        """Reverse one pair merge using the provenance that merge recorded.

        THE SHAPE OF THIS METHOD IS THE SAFETY ARGUMENT. It runs in two halves
        with a hard line between them:

          Phase 1  read everything, verify everything, build a plan.
                   Not one write. Every refusal happens here, so "no partial
                   changes on refusal" is structural rather than a promise.
          Phase 2  execute the plan and stamp the reversal marker, in the
                   caller's single transaction. Either all of it commits or
                   none of it does.

        Filesystem deletions are never performed here. Paths are appended to
        `files_to_delete` for the caller to unlink AFTER a successful commit —
        unlinking before a rollback would destroy a live file the database
        still references. Merge stages copies and undoes them on failure;
        unmerge stages deletions and only performs them on success.

        Restoration reads the id lists in provenance and nothing else. Selecting
        "everything the winner owns" would steal every detection, embedding and
        photo the winner legitimately gained after the merge.

        Raises UnmergeConflict for every refusal; the caller maps .status_code
        and .reason onto HTTP.
        """
        from db_models import IdentityAuditLog, IdentityMerge

        files_to_delete = files_to_delete if files_to_delete is not None else []

        # ==================================================================
        # PHASE 1 — VERIFY.  No writes below this line until PHASE 2.
        # ==================================================================
        merge = (await db.execute(
            select(IdentityMerge).where(IdentityMerge.id == merge_id))).scalar_one_or_none()
        if merge is None:
            raise UnmergeConflict("merge_not_found",
                                  f"No merge record with id {merge_id}.", 404)

        # The reversal marker is checked FIRST and is the sole authority on
        # "already done". loser.status is NOT a sound idempotency test: a later
        # merge, a retention sweep or an admin edit can change it independently,
        # which would either mask a genuine second unmerge or misreport an
        # unrelated state change as "already unmerged".
        marker = (await db.execute(sa_text(
            "SELECT created_at, username FROM identity_audit_log "
            " WHERE action_type = 'unmerge' AND success = true "
            "   AND action_details ->> 'merge_id' = :m "
            " ORDER BY created_at LIMIT 1"), {"m": str(merge_id)})).first()
        if marker:
            raise UnmergeConflict(
                "already_unmerged",
                f"Merge {merge_id} was already reversed at "
                f"{marker[0].isoformat()} by {marker[1]}.")

        provenance = merge.provenance
        if not provenance:
            raise UnmergeConflict(
                "provenance_missing",
                f"Merge {merge_id} predates provenance recording; nothing was "
                f"captured about what it moved, so it cannot be reversed.")
        if provenance.get("multi_merge"):
            raise UnmergeConflict(
                "multi_merge_unsupported",
                f"Merge {merge_id} was part of a batch of "
                f"{provenance['multi_merge'].get('batch_size', '?')} sources. "
                f"Batch merges apply target-level effects with no per-source "
                f"record, so reversing one source alone cannot restore them.")

        raw_status = provenance.get("loser_status")
        if not raw_status:
            raise UnmergeConflict(
                "provenance_missing_loser_status",
                f"Merge {merge_id} did not record the source identity's status. "
                f"Restoring it to a guessed status could resurrect a retired "
                f"identity or demote a promoted one.")
        restore_status = None
        for candidate in (raw_status, str(raw_status).lower(), str(raw_status).upper()):
            try:
                restore_status = IdentityStatus(candidate)
                break
            except ValueError:
                try:
                    restore_status = IdentityStatus[str(candidate).upper()]
                    break
                except KeyError:
                    continue
        if restore_status is None or restore_status == IdentityStatus.MERGED:
            raise UnmergeConflict(
                "provenance_invalid_loser_status",
                f"Recorded source status {raw_status!r} is not a status this "
                f"unmerge can restore.")

        loser_id, winner_id = merge.from_identity_id, merge.to_identity_id
        found = {i.id: i for i in (await db.execute(
            select(Identity).where(Identity.id.in_([loser_id, winner_id])))).scalars().all()}
        loser, winner = found.get(loser_id), found.get(winner_id)
        if loser is None or winner is None:
            raise UnmergeConflict(
                "identity_missing",
                "The source or target identity of this merge no longer exists.", 404)

        # Consistency assertions — deliberately distinct reasons from
        # already_unmerged, because they mean entirely different things.
        if loser.status != IdentityStatus.MERGED:
            raise UnmergeConflict(
                "loser_not_merged",
                f"Identity {loser_id} is {loser.status.value}, not merged. Its "
                f"state was changed by something other than this merge.")
        if loser.merged_into_id != winner_id:
            raise UnmergeConflict(
                "provenance_mismatch",
                f"Identity {loser_id} is merged into {loser.merged_into_id}, not "
                f"into {winner_id} as this merge record claims.")

        # ---------------- gallery verification -----------------------------
        # An image id in provenance is NOT licence to overwrite that row.
        # Provenance describes the world as the merge left it, not as it is.
        image_records = provenance.get("images") or []
        wanted_ids = [r["id"] for r in image_records if r.get("id") is not None]
        dedup_targets = [r["deduplicated_into"] for r in image_records
                         if r.get("deduplicated_into") is not None]
        adopted = provenance.get("adopted_snapshot") or None
        if adopted and adopted.get("image_id") is not None:
            wanted_ids.append(adopted["image_id"])

        current: dict = {}
        if wanted_ids or dedup_targets:
            rows = (await db.execute(
                select(IdentityImage).where(
                    IdentityImage.id.in_(list(set(wanted_ids + dedup_targets))))
            )).scalars().all()
            current = {row.id: row for row in rows}

        def _conflict(image_id, what):
            raise UnmergeConflict(
                "post_merge_gallery_conflict",
                f"Gallery image {image_id} changed after the merge ({what}). "
                f"Reversing it now would overwrite that change.")

        moved_plan, dedup_plan = [], []
        for record in image_records:
            image_id = record.get("id")
            row = current.get(image_id)
            if row is None:
                _conflict(image_id, "the row no longer exists")

            if record.get("deduplicated_into") is not None:
                # This row never moved — the winner already owned the same
                # bytes, so only the embeddings pointing at it were re-pointed.
                if row.identity_id != loser_id:
                    _conflict(image_id, f"it is now owned by {row.identity_id}, "
                                        f"not by the source identity")
                target = current.get(record["deduplicated_into"])
                if target is None or target.identity_id != winner_id:
                    _conflict(image_id, "the winner's duplicate of it is gone or "
                                        "has changed owner")
                dedup_plan.append((image_id, record["deduplicated_into"]))
                continue

            expected_path = (record.get("new_path") if record.get("file_copied")
                             else record.get("original_path"))
            if row.identity_id != winner_id:
                _conflict(image_id, f"it is now owned by {row.identity_id}, not by "
                                    f"the merge target")
            if row.storage_path != expected_path:
                _conflict(image_id, f"its path is {row.storage_path!r}, not the "
                                    f"{expected_path!r} the merge left")
            if row.is_primary:
                # The merge demoted every moved row unconditionally, so a
                # primary flag here was set by somebody afterwards.
                _conflict(image_id, "it was made the target's primary photo after "
                                    "the merge")
            moved_plan.append((row, record))

        # At most one primary per identity is a database constraint, so a
        # was_primary restoration is checked against the loser's CURRENT state
        # before anything moves. A deduplicated row kept its flag on the loser,
        # which is exactly the case this catches.
        restoring_primary = [r for _, r in moved_plan if r.get("was_primary")]
        if restoring_primary:
            held = (await db.execute(
                select(IdentityImage.id).where(
                    IdentityImage.identity_id == loser_id,
                    IdentityImage.is_primary.is_(True)))).scalars().all()
            if held:
                raise UnmergeConflict(
                    "post_merge_gallery_conflict",
                    f"Identity {loser_id} already has primary image {held[0]}, so "
                    f"restoring image {restoring_primary[0]['id']} as primary "
                    f"would violate one-primary-per-identity. Refusing rather "
                    f"than demoting the current one.")

        # ---------------- adopted snapshot ---------------------------------
        adopted_plan = None
        if adopted and adopted.get("image_id") is not None:
            image_id = adopted["image_id"]
            row = current.get(image_id)
            if not adopted.get("created_by_merge"):
                # The winner owned this photo before the merge. The row and the
                # file are theirs; the merge only pointed at them. Touching
                # either would delete data this merge did not create.
                logger.info("[UNMERGE] adopted image %s predates the merge - "
                            "left untouched", image_id)
            elif row is None:
                _conflict(image_id, "the adopted row no longer exists")
            elif row.identity_id != winner_id:
                _conflict(image_id, f"the adopted row is now owned by "
                                    f"{row.identity_id}")
            elif row.storage_path != adopted.get("new_path"):
                _conflict(image_id, f"the adopted row's path is "
                                    f"{row.storage_path!r}, not "
                                    f"{adopted.get('new_path')!r}")
            elif bool(row.is_primary) != bool(adopted.get("became_primary")):
                _conflict(image_id, "its primary flag changed after the merge")
            else:
                # Unlink only what this merge created, only if nothing else
                # points at it, and only after the commit.
                absolute = None
                others = (await db.execute(
                    select(func.count(IdentityImage.id)).where(
                        IdentityImage.storage_path == row.storage_path,
                        IdentityImage.id != row.id))).scalar() or 0
                if others:
                    logger.info("[UNMERGE] file %s is referenced by %d other "
                                "gallery row(s); row removed, file kept",
                                row.storage_path, others)
                else:
                    try:
                        from backend.core.enrollment_service import pending_absolute_path
                        absolute = pending_absolute_path(row.storage_path)
                    except Exception as exc:                   # noqa: BLE001
                        # Containment failed: the row still goes, the file stays.
                        logger.warning("[UNMERGE] refusing to stage %s for "
                                       "deletion: %s", row.storage_path, exc)
                adopted_plan = (row, absolute)

        # ==================================================================
        # PHASE 2 — EXECUTE.  Everything below commits together or not at all.
        # ==================================================================
        appearance_ids = provenance.get("appearance_ids") or []
        embedding_ids = provenance.get("embedding_ids") or []
        face_ids = provenance.get("face_ids") or []

        async def _restore(model, ids) -> int:
            """Restore ONLY recorded ids, and only where the winner still holds
            them — a row that has since moved on belongs to whoever holds it."""
            if not ids:
                return 0
            result = await db.execute(
                update(model)
                .where(model.id.in_(ids), model.identity_id == winner_id)
                .values(identity_id=loser_id))
            return result.rowcount or 0

        restored = {
            "appearances": await _restore(IdentityAppearance, appearance_ids),
            "embeddings": await _restore(IdentityEmbedding, embedding_ids),
            "faces": await _restore(Face, face_ids),
        }
        expected = {"appearances": len(appearance_ids),
                    "embeddings": len(embedding_ids),
                    "faces": len(face_ids)}

        for row, record in moved_plan:
            row.identity_id = loser_id
            row.storage_path = record.get("original_path") or row.storage_path
            row.is_primary = bool(record.get("was_primary"))
            row.updated_at = datetime.utcnow()

        for original_image_id, duplicate_id in dedup_plan:
            # Only embeddings this merge re-pointed: recorded ids that still
            # sit on the winner's duplicate.
            if embedding_ids:
                await db.execute(
                    update(IdentityEmbedding)
                    .where(IdentityEmbedding.id.in_(embedding_ids),
                           IdentityEmbedding.image_id == duplicate_id)
                    .values(image_id=original_image_id))

        adopted_removed = None
        if adopted_plan is not None:
            row, absolute = adopted_plan
            adopted_removed = {"image_id": row.id, "storage_path": row.storage_path,
                               "file_staged_for_deletion": bool(absolute)}
            await db.delete(row)
            if absolute:
                files_to_delete.append(absolute)

        before_state = {"id": str(loser_id), "status": loser.status.value,
                        "merged_into_id": str(winner_id),
                        "appearances_count": loser.appearances_count}

        watchlist_restored = await restore_watchlist_membership(db, provenance, loser_id, winner_id)

        loser.status = restore_status
        loser.merged_into_id = None
        loser.updated_at = datetime.utcnow()

        # ---- reverse the merge-time consolidation ---------------------------
        # The merge widened the winner's sighting window to the union of both
        # and relabelled absorbed 'unknown' vectors 'known'. Both were recorded
        # in provenance precisely so this can put them back; merge rows written
        # before those keys existed simply skip the restore (guarded .get).
        for key, attr in (("winner_first_seen_before", "first_seen_at"),
                          ("winner_last_seen_before", "last_seen_at")):
            recorded_ts = provenance.get(key)
            if recorded_ts:
                try:
                    setattr(winner, attr, datetime.fromisoformat(recorded_ts))
                except ValueError:
                    logger.warning("[IDENTITY] [UNMERGE] unparseable %s %r; "
                                   "leaving winner.%s as merged", key, recorded_ts, attr)

        relabelled_ids = provenance.get("relabelled_embedding_ids") or []
        if relabelled_ids:
            await db.execute(
                update(IdentityEmbedding)
                .where(IdentityEmbedding.id.in_(relabelled_ids))
                .values(faiss_index_type='unknown'))

        await db.flush()

        # Recount BOTH sides from the restored rows rather than arithmetic.
        for identity in (loser, winner):
            identity.appearances_count = (await db.execute(
                select(func.count(IdentityAppearance.id)).where(
                    IdentityAppearance.identity_id == identity.id))).scalar() or 0

        after_state = {"id": str(loser_id), "status": loser.status.value,
                       "merged_into_id": None,
                       "appearances_count": loser.appearances_count}

        details = {
            # THE reversal marker. Read back by the check at the top of this
            # method, so it must be a plain JSON string under this exact key.
            "merge_id": str(merge_id),
            "merge_row_id": merge_id,
            "from_identity_id": str(loser_id),
            "to_identity_id": str(winner_id),
            "restored": restored,
            "recorded": expected,
            "restored_images": len(moved_plan),
            "dedup_links_restored": len(dedup_plan),
            "adopted_image_removed": adopted_removed,
            "restored_status": restore_status.value,
            "watchlist": watchlist_restored,
            "winner_has_primary_after": None,
        }
        if restored != expected:
            # Visible, not silent: rows that moved on after the merge are left
            # with their current owner and the shortfall is recorded.
            details["partial"] = True
            logger.warning("[UNMERGE] merge %s: restored %s of %s recorded rows; "
                           "the remainder moved on after the merge",
                           merge_id, restored, expected)

        if adopted_removed:
            details["winner_has_primary_after"] = bool((await db.execute(
                select(func.count(IdentityImage.id)).where(
                    IdentityImage.identity_id == winner_id,
                    IdentityImage.is_primary.is_(True)))).scalar() or 0)

        # Built directly rather than through IdentityAuditLogger.log_action:
        # that helper swallows every exception and rolls back on failure, which
        # is right for a record that must never fail an operation and fatal for
        # one the NEXT request depends on. The marker and the restoration must
        # be the same transaction. This row IS the audit event; no second one
        # is written. log_merge is also wrong here — it forces a
        # create_new|merge_existing decision through coerce_decision, and an
        # unmerge is neither.
        db.add(IdentityAuditLog(
            user_id=user_id,
            username=username,
            action_type="unmerge",
            identity_id=winner_id,
            related_identity_id=loser_id,
            action_details=details,
            before_state=before_state,
            after_state=after_state,
            ip_address=ip_address,
            user_agent=user_agent,
            success=True,
            notes=notes,
            created_at=datetime.utcnow(),
        ))

        # The identity_merges row is never deleted or edited: the merge and its
        # reversal both stay readable, linked by merge_id.
        await db.flush()
        logger.info("[UNMERGE] merge %s reversed: %s restored to %s (%s)",
                    merge_id, restored, loser_id, restore_status.value)
        _count_identity_op("unmerge")
        return details

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
        db: AsyncSession = None,
        copied_files: Optional[list] = None,
        confirm_merge_risk: bool = False,
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

        # SAFETY GATE — assessed over the WHOLE group before any write, so a
        # single unrelated member ("outlier") blocks the batch rather than
        # being averaged away by the compatible majority.
        await self._gate_merge_compatibility(
            db, list(unique_ids), user_id, confirm_merge_risk)

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
            
            # Captured before the overwrite: reading it afterwards would only
            # ever yield MERGED (same reasoning as the pair path).
            source_status_before = source_identity.status

            # Update source identity status
            source_identity.status = IdentityStatus.MERGED
            await invalidate_merge_suggestions(db, [source_identity.id], f"identity merged into {target_id}")
            source_identity.merged_into_id = target_id
            source_identity.updated_at = datetime.utcnow()

            # Gallery consolidation + provenance, BEFORE the blanket UPDATEs
            # below erase which rows were this source's (same reasoning as the
            # pair path in merge_identities).
            provenance = await self._consolidate_loser_assets(
                db, source_identity, target_identity, copied_files,
                loser_status_before=source_status_before)

            # Mark the row as part of a batch. Multi-merge applies TARGET-level
            # effects with no per-source record — type promotion, first/last-seen
            # consolidation, the faiss_index_type flip — so reversing one source
            # in isolation cannot restore them. Unmerge refuses these rows
            # outright rather than half-restoring; the pair path has no such
            # effects and is cleanly invertible.
            provenance["multi_merge"] = {"batch_size": len(source_ids)}
            provenance.update(await transfer_watchlist_membership(db, source_id, target_id))

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
                notes=f"{notes or ''} | pipelines: {source_pipelines} | embeddings: {embedding_count}",
                provenance=provenance,
            )
            db.add(merge_record)

            # No vector-index removal, on EITHER backend: index keys are
            # identity_embeddings.id, unchanged by the re-parent above, and
            # search excludes the MERGED source by joining identities.status.
            # The old FAISS branch called remove_from_vector_index AFTER the
            # re-parent, matched zero rows, removed nothing — and logged
            # "Removed N vectors". Log what actually happened.
            logger.info(
                "[IDENTITY] [MERGE] Re-parented %d embeddings from %s to %s; "
                "embedding IDs remain valid and no vector deletion was required",
                embedding_count, source_id, target_id)

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
            # One path for both backends: the type change is a database fact.
            # Search resolves KNOWN vs UNKNOWN from `identities.type`, so no
            # vector is reconstructed, re-keyed, or moved between indexes.
            #
            # The old FAISS branch here was actively destructive: it added the
            # vectors to the KNOWN index and then removed every vector for the
            # same identity, deleting what it had just written.
            logger.info(f"[IDENTITY] [MERGE] Updating embedding type: unknown → known")
            await db.execute(
                update(IdentityEmbedding).where(
                    IdentityEmbedding.identity_id == target_id,
                    IdentityEmbedding.faiss_index_type == 'unknown'
                ).values(faiss_index_type='known')
            )
            logger.info(f"[IDENTITY] [MERGE] ✅ Embeddings updated to KNOWN type")

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
        _count_identity_op("merge_multiple")
        return result
    
    def compute_quality_score(
        self,
        face_size: int,
        blur_score: Optional[float] = None,
        confidence: Optional[float] = None,
        pose_score: Optional[float] = None,
        *,
        sharpness: Optional[float] = None,
    ) -> float:
        """Fallback quality score in [0, 1] from whatever signals are available.

        **Renormalized by the weight actually supplied.** The previous version
        simply skipped a missing term, so its maximum was the sum of the weights
        it happened to receive. The detection path passed only `face_size` and
        `confidence` — 0.3 + 0.2 — capping every camera detection at 0.5 against
        a KNOWN threshold that defaults to exactly 0.5. Renormalizing means a
        face that is excellent on every axis it was measured on scores 1.0,
        whether that is four axes or two.

        `sharpness` is 0..1 where HIGHER IS BETTER, matching
        `face_quality.assess_face_quality`. `blur_score` is the legacy parameter
        with the OPPOSITE convention (higher = blurrier, on a Laplacian-variance
        scale); the two must never be crossed, which is why this takes both
        rather than reusing one name for both meanings.
        """
        weights = {"size": 0.3, "blur": 0.3, "confidence": 0.2, "pose": 0.2}
        terms = {}

        # Face size (larger is better). 100x100 = 10000 px is "good".
        if face_size and face_size > 0:
            terms["size"] = min(1.0, face_size / 10000.0)

        if sharpness is not None:
            terms["blur"] = max(0.0, min(1.0, float(sharpness)))
        elif blur_score is not None:
            # Legacy convention: higher input = blurrier = worse.
            terms["blur"] = max(0.0, min(1.0, 1.0 - float(blur_score) / 100.0))

        if confidence is not None:
            terms["confidence"] = max(0.0, min(1.0, float(confidence)))

        if pose_score is not None:
            terms["pose"] = max(0.0, min(1.0, float(pose_score)))

        if not terms:
            # 0.0, never None. Every gate is written
            # `if quality_score is not None and quality_score < threshold`, so a
            # None here would bypass all of them and admit everything.
            return 0.0

        total_weight = sum(weights[name] for name in terms)
        combined = sum(weights[name] * value for name, value in terms.items())
        return max(0.0, min(1.0, combined / total_weight))


# Global instance - will be set during startup in lifespan.py
identity_service: Optional[IdentityService] = None

