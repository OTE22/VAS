"""
Detection evidence — the ONE write path for a processed frame.

    Detection + Faces + Appearance + exact embedding→detection link + counter
        (CORE, one transaction, all-or-nothing)
    + live-alert triggers      (OPTIONAL, savepoint A)
    + watchlist alerts         (OPTIONAL, savepoint B)
        ↓ COMMIT (by the caller's session exit)
    broadcast `detection_alerts` for the rows that were actually persisted

Used identically by the batch writer and by the direct-write path, so there is
no second implementation to drift.

Failure classes (documented in Docs/61):

  A. core evidence failure — detection/face insert, appearance, or an
     EmbeddingLinkError (CROSS_LINK_REFUSED, EMBEDDING_MISSING with a supplied
     id): the whole per-detection transaction rolls back. No detection, face,
     appearance, link, alert row; no broadcast. metric reason="detection_core".
     The caller then removes the embeddings this very frame created
     (`compensate_failed_detection`) so no unexplained camera evidence remains.
  B. optional alert-enrichment failure — live-alert lookup/insert (savepoint A)
     or watchlist lookup/insert (savepoint B): only that savepoint rolls back;
     core evidence and the OTHER subsystem's rows commit. metric
     reason="alert_enrichment_live" / "alert_enrichment_watchlist". Nothing is
     broadcast for a subsystem whose savepoint rolled back.
  C. post-commit broadcast failure — rows stay committed; log + metric
     reason="alert_broadcast". The database is authoritative.

Reliability note (deliberately NOT a fallback): if enrichment fails after valid
core evidence, the alert is not recreated automatically today. A durable
alert-evaluation retry/outbox over committed detections is a future enhancement.
"""
from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, text, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db_models import Detection, Face, Identity, IdentityEmbedding, IdentityType, Pipeline

logger = logging.getLogger(__name__)

# keys of a face dict that are ingest-internal and never Face columns
_FACE_INTERNAL_KEYS = ("quality", "quality_scorer")


def _fail_metric(reason: str) -> None:
    try:
        from backend.core.metrics import metrics_db_operation_failures
        if metrics_db_operation_failures:
            metrics_db_operation_failures.labels(reason=reason).inc()
    except Exception:  # pragma: no cover - metrics never break persistence
        pass


# ---------------------------------------------------------------- embedding link

class LinkOutcome(str, enum.Enum):
    LINKED = "LINKED"                      # UPDATE hit exactly this row
    ALREADY_LINKED = "ALREADY_LINKED"      # idempotent retry: already this detection
    NO_EMBEDDING = "NO_EMBEDDING"          # the frame wrote no embedding (quality gate)
    CROSS_LINK_REFUSED = "CROSS_LINK_REFUSED"  # row belongs to another detection
    EMBEDDING_MISSING = "EMBEDDING_MISSING"    # explicit id supplied, row gone


class EmbeddingLinkError(RuntimeError):
    """Broken embedding→detection provenance. Propagates: it fails the whole
    detection transaction — a detection is never reported as persisted while
    its exact embedding provenance is known to be inconsistent."""

    def __init__(self, outcome: LinkOutcome, embedding_id: int, detection_id: int,
                 existing_detection_id: Optional[int] = None):
        self.outcome = outcome
        self.embedding_id = embedding_id
        self.detection_id = detection_id
        self.existing_detection_id = existing_detection_id
        super().__init__(
            f"{outcome.value}: embedding {embedding_id} → detection {detection_id}"
            + (f" (already linked to {existing_detection_id})" if existing_detection_id else ""))


async def link_embedding_to_detection(db: AsyncSession, *, embedding_id: Optional[int],
                                      detection_id: int) -> LinkOutcome:
    """Exact back-link. `UPDATE … WHERE id = :e AND detection_id IS NULL`; the
    NULL guard is the only lock needed — an embedding id is produced by exactly
    one frame, so writers never contend on the same row. Never a heuristic."""
    if embedding_id is None:
        return LinkOutcome.NO_EMBEDDING
    res = await db.execute(
        sa_update(IdentityEmbedding)
        .where(IdentityEmbedding.id == embedding_id, IdentityEmbedding.detection_id.is_(None))
        .values(detection_id=detection_id)
        .returning(IdentityEmbedding.id))
    if res.scalar_one_or_none() is not None:
        return LinkOutcome.LINKED
    current = (await db.execute(
        select(IdentityEmbedding.detection_id).where(IdentityEmbedding.id == embedding_id)
    )).one_or_none()
    if current is None:
        raise EmbeddingLinkError(LinkOutcome.EMBEDDING_MISSING, embedding_id, detection_id)
    if current[0] == detection_id:
        return LinkOutcome.ALREADY_LINKED
    raise EmbeddingLinkError(LinkOutcome.CROSS_LINK_REFUSED, embedding_id, detection_id,
                             existing_detection_id=current[0])


# ---------------------------------------------------------------- payload types

@dataclass
class FaceEvidence:
    identity_id: Optional[str]
    embedding_id: Optional[int]           # row THIS frame inserted (never pre-existing)
    embedding_created: bool               # ownership flag for compensation
    secondary_embedding_ids: List[int]    # further rows THIS frame inserted (enrichment)
    identity_created: bool
    face_image_path: Optional[str]
    similarity: float
    quality: Optional[float]
    quality_scorer: Optional[str]
    name: str
    is_known: bool
    event_id: Optional[str]

    @classmethod
    def from_face_dict(cls, f: Dict[str, Any]) -> "FaceEvidence":
        label_state = f.get("label_state")
        label_value = getattr(label_state, "value", label_state)
        return cls(
            identity_id=str(f["identity_id"]) if f.get("identity_id") else None,
            embedding_id=f.get("_embedding_id"),
            embedding_created=bool(f.get("_embedding_created_by_this_frame")),
            secondary_embedding_ids=[int(x) for x in (f.get("_secondary_embedding_ids") or []) if x is not None],
            identity_created=bool(f.get("_identity_created_by_this_frame")),
            face_image_path=f.get("face_image_path"),
            similarity=float(f.get("similarity") or 0.0),
            quality=f.get("quality"),
            quality_scorer=f.get("quality_scorer"),
            name=str(f.get("name") or "Unknown"),
            is_known=(label_value == "auto_known") or bool(f.get("_is_known")),
            event_id=f.get("_event_id"),
        )


@dataclass
class DetectionAlertBundle:
    """Alerts persisted for one (detection, identity) — only rows inserted in
    THIS call, so a retry that inserts nothing broadcasts nothing."""
    detection_id: int
    pipeline_id: str
    timestamp: datetime
    event_id: Optional[str]
    identity_id: str
    identity_name: Optional[str]
    is_known: bool
    similarity: float
    live_alerts: List[Dict[str, Any]] = field(default_factory=list)
    watchlist_alerts: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class DetectionPersistOutcome:
    detection_id: int
    faces_persisted: int
    bundles: List[DetectionAlertBundle]
    link_outcomes: Dict[int, str]          # embedding_id -> LinkOutcome value


def face_row_columns(f: Dict[str, Any]) -> Dict[str, Any]:
    """The Face columns from an ingest face dict — internal keys stripped."""
    return {k: v for k, v in f.items()
            if not k.startswith("_") and k not in _FACE_INTERNAL_KEYS}


# ---------------------------------------------------------------- persistence

async def persist_detection(db: AsyncSession, *, detection_data: Dict[str, Any]) -> DetectionPersistOutcome:
    """Persist one frame — flush only; the caller's session commits on exit.

    CORE (no savepoint; any failure propagates and rolls back everything):
      detection → faces → per face: identity load, create_appearance, exact
      embedding link → pipeline counter.
    OPTIONAL per face: SAVEPOINT A live alerts, SAVEPOINT B watchlist alerts.
    """
    from backend.core.identity_service import identity_service

    pipeline_id = detection_data["pipeline_id"]
    det_cols = dict(detection_data["detection"])
    detection = Detection(**det_cols)
    db.add(detection)
    await db.flush()
    detection_id = int(detection.id)
    timestamp = detection.timestamp or datetime.utcnow()

    faces = [FaceEvidence.from_face_dict(f) for f in detection_data.get("faces", [])]
    for f in detection_data.get("faces", []):
        db.add(Face(detection_id=detection_id, **face_row_columns(f)))
    await db.flush()

    bundles: List[DetectionAlertBundle] = []
    link_outcomes: Dict[int, str] = {}

    for face in faces:
        if not face.identity_id or identity_service is None:
            continue
        # ---- CORE: identity, appearance, exact link
        identity = (await db.execute(
            select(Identity).where(Identity.id == uuid.UUID(face.identity_id))
        )).scalar_one_or_none()
        if identity is None:
            raise RuntimeError(f"identity {face.identity_id} vanished before its detection was persisted")
        await identity_service.create_appearance(
            identity=identity, pipeline_id=pipeline_id, track_id=None, start_time=timestamp,
            best_snapshot_path=face.face_image_path, db=db,
            quality_score=face.quality, quality_scorer_version=face.quality_scorer,
            similarity=face.similarity)
        for emb_id in ([face.embedding_id] if face.embedding_id is not None else []) + face.secondary_embedding_ids:
            outcome = await link_embedding_to_detection(db, embedding_id=emb_id, detection_id=detection_id)
            link_outcomes[emb_id] = outcome.value

        # ---- OPTIONAL enrichment, two independent savepoints
        live_rows: List[Dict[str, Any]] = []
        wl_rows: List[Dict[str, Any]] = []
        if settings.LIVE_ALERTS_ENABLED:
            try:
                async with db.begin_nested():
                    from backend.core.live_alert_service import live_alert_service
                    triggers = await live_alert_service.check_detection_against_alerts(
                        db=db, identity_id=face.identity_id, similarity=face.similarity,
                        pipeline_id=pipeline_id, detection_id=detection_id,
                        snapshot_path=face.face_image_path, defer_commit=True)
                    live_rows = [{
                        "trigger_id": t.trigger_id, "alert_id": t.alert_id, "alert_name": t.alert_name,
                        "identity_name": t.identity_name, "similarity": t.similarity,
                        "sound_alert": t.sound_alert, "should_notify_dashboard": t.should_notify_dashboard,
                        "detection_id": detection_id,
                    } for t in triggers]
            except Exception:
                live_rows = []
                _fail_metric("alert_enrichment_live")
                logger.exception("[EVIDENCE] live-alert enrichment failed detection_id=%s identity=%s "
                                 "(core evidence kept)", detection_id, face.identity_id)
        if settings.WATCHLIST_ENABLED:
            try:
                async with db.begin_nested():
                    from backend.core.watchlist_service import watchlist_service
                    wl_rows = await watchlist_service.record_detection_alerts(
                        db, identity_id=face.identity_id, detection_id=detection_id,
                        pipeline_id=pipeline_id, similarity=face.similarity,
                        snapshot_path=face.face_image_path)
            except Exception:
                wl_rows = []
                _fail_metric("alert_enrichment_watchlist")
                logger.exception("[EVIDENCE] watchlist enrichment failed detection_id=%s identity=%s "
                                 "(core evidence kept)", detection_id, face.identity_id)
        if live_rows or wl_rows:
            bundles.append(DetectionAlertBundle(
                detection_id=detection_id, pipeline_id=pipeline_id, timestamp=timestamp,
                event_id=face.event_id, identity_id=face.identity_id,
                identity_name=identity.display_name, is_known=(identity.type == IdentityType.KNOWN),
                similarity=face.similarity, live_alerts=live_rows, watchlist_alerts=wl_rows))

    # CORE: counter (one row per frame)
    await db.execute(
        sa_update(Pipeline).where(Pipeline.pipeline_id == pipeline_id)
        .values(total_detections=Pipeline.total_detections + 1, updated_at=datetime.utcnow()))
    await db.flush()
    return DetectionPersistOutcome(detection_id=detection_id, faces_persisted=len(faces),
                                   bundles=bundles, link_outcomes=link_outcomes)


async def compensate_failed_detection(detection_data: Dict[str, Any]) -> Dict[str, int]:
    """After a rolled-back core transaction: remove ONLY the embeddings (and an
    evidence-free identity) that THIS frame created — decided by the in-memory
    ownership flags, never inferred from pipeline_id. Runs in its own short
    transaction. Pre-existing, enrollment and preload rows are never touched."""
    from db_connection import db_manager
    from backend.core.vector_index.access import remove_embedding_keys

    owned = []
    for f in detection_data.get("faces", []):
        if f.get("_embedding_created_by_this_frame") and f.get("_embedding_id") is not None:
            owned.append(int(f["_embedding_id"]))
        owned.extend(int(x) for x in (f.get("_secondary_embedding_ids") or []) if x is not None)
    frame_identities = [str(f["identity_id"]) for f in detection_data.get("faces", [])
                        if f.get("_identity_created_by_this_frame") and f.get("identity_id")]
    removed = {"embeddings": 0, "identities": 0}
    if not owned and not frame_identities:
        return removed
    try:
        async with db_manager.get_session() as db:
            if owned:
                await remove_embedding_keys(db, owned)
                res = await db.execute(text(
                    "DELETE FROM identity_embeddings WHERE id = ANY(CAST(:ids AS int[])) "
                    "AND detection_id IS NULL"), {"ids": owned})
                removed["embeddings"] = res.rowcount or 0
            for iid in frame_identities:
                res = await db.execute(text("""
                    DELETE FROM identities i WHERE i.id = CAST(:iid AS uuid)
                      AND NOT EXISTS (SELECT 1 FROM identity_embeddings e WHERE e.identity_id = i.id)
                      AND NOT EXISTS (SELECT 1 FROM identity_appearances a WHERE a.identity_id = i.id)
                      AND NOT EXISTS (SELECT 1 FROM faces f WHERE f.identity_id = i.id)
                      AND NOT EXISTS (SELECT 1 FROM identity_images g WHERE g.identity_id = i.id)
                """), {"iid": iid})
                removed["identities"] += res.rowcount or 0
        logger.warning("[EVIDENCE] compensated failed detection: removed %s frame-created embedding(s), "
                       "%s evidence-free identity(ies)", removed["embeddings"], removed["identities"])
    except Exception:
        _fail_metric("embedding_compensation")
        logger.exception("[EVIDENCE] compensation failed for frame-created embeddings %s "
                         "(stale-embedding reconciliation will remove them)", owned)
    return removed


# ---------------------------------------------------------------- broadcast

async def broadcast_detection_alerts(bundles: List[DetectionAlertBundle], *,
                                     location_name: Optional[str]) -> int:
    """Call ONLY after the owning transaction committed. One `detection_alerts`
    event per bundle; failures are logged + counted and never undo rows."""
    if not bundles:
        return 0
    from backend.core import ws_manager
    sent = 0
    for b in bundles:
        payload = {
            "type": "detection_alerts",
            "data": {
                "event_id": f"detalerts:{b.detection_id}:{b.identity_id}",
                "detection_event_id": b.event_id,
                "detection_id": b.detection_id,
                "pipeline_id": b.pipeline_id,
                "location_name": location_name,
                "timestamp": b.timestamp.isoformat() + "Z",
                "created_at": datetime.utcnow().isoformat() + "Z",
                "identity_id": b.identity_id,
                "identity_name": b.identity_name,
                "is_known": b.is_known,
                "similarity": b.similarity,
                "live_alerts": b.live_alerts,
                "watchlist_alerts": b.watchlist_alerts,
            },
        }
        try:
            await ws_manager.broadcast(payload, pipeline_id=b.pipeline_id)
            sent += 1
        except Exception:
            _fail_metric("alert_broadcast")
            logger.exception("[EVIDENCE] detection_alerts broadcast failed detection_id=%s "
                             "(rows are committed and authoritative)", b.detection_id)
    return sent
