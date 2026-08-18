"""
Reviewed-label workflow.

The rules this module enforces (approval-binding):
- An unresolved or automatically generated threat assessment can seed at
  most a WEAK label — never a manual/confirmed one.
- Only manual labels with review_status='reviewed' count toward the
  supervised-training minimums. Weak labels never do.
- Labels are idempotent (unique key), superseded rather than edited, and
  every review action is audited.
- Nothing here retrains anything: label creation and review only ever
  change data readiness, which the trainer re-checks at its own gate.
"""

import logging
import uuid as uuid_mod
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func as sa_func, update, and_, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.audit import ml_audit
from backend.ml.constants import LABEL_DEFINITION_VERSION
from config import settings
from backend.utils.time_utils import iso_utc

logger = logging.getLogger(__name__)

VALID_LABELS = ("positive", "negative", "unknown")
VALID_KINDS = ("manual", "weak")
REVIEW_ACTIONS = ("confirm", "dispute", "retract")


def _label_idempotency_key(subject_type: str, subject_id: str, source: str,
                           event_time: datetime) -> str:
    day_bucket = event_time.strftime("%Y%m%d")
    return f"{subject_type}:{subject_id}:{LABEL_DEFINITION_VERSION}:{source}:{day_bucket}"


def serialize_label(row) -> Dict[str, Any]:
    def iso(dt):
        return iso_utc(dt) if dt else None
    return {
        "id": str(row.id),
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "person_id": str(row.person_id) if row.person_id else None,
        "assessment_id": str(row.assessment_id) if row.assessment_id else None,
        "label": row.label,
        "label_kind": row.label_kind,
        "label_definition_version": row.label_definition_version,
        "confidence": row.confidence,
        "source": row.source,
        "event_time": iso(row.event_time),
        "status": row.status,
        "review_status": row.review_status,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": iso(row.reviewed_at),
        "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
        "notes": row.notes,
        "created_at": iso(row.created_at),
        "created_by": row.created_by,
    }


# ---------------------------------------------------------------------------
# Outcome linkage — deterministic, persisted, never inferred at read time.
#
# A label L is eligible when status='active' AND review_status != 'disputed'.
# Rank (higher wins): (label_kind == 'manual', review_status == 'reviewed',
# created_at). Candidate predictions for L:
#   * every prediction with assessment_id = L.assessment_id (when set), UNION
#   * every prediction of the same subject whose event_time falls in L's UTC
#     day bucket — the same bucket L's idempotency key uses. Predictions with
#     NULL event_time are reachable only through assessment_id.
# A prediction is (re)linked when its current label is NULL, ineligible or
# lower-ranked. Runs INSIDE the label transaction (flush only).
# ---------------------------------------------------------------------------
def _label_rank(row) -> tuple:
    return (1 if row.label_kind == "manual" else 0,
            1 if row.review_status == "reviewed" else 0,
            row.created_at or datetime.min)


def _label_eligible(row) -> bool:
    return row.status == "active" and row.review_status != "disputed"


async def _candidate_predictions(db: AsyncSession, label_row):
    """Prediction rows the label may explain (assessment ∪ same-subject/day)."""
    from db_models import MLPrediction
    day_start = datetime(label_row.event_time.year, label_row.event_time.month, label_row.event_time.day)
    day_end = day_start + timedelta(days=1)
    conds = [and_(MLPrediction.subject_type == label_row.subject_type,
                  MLPrediction.subject_id == label_row.subject_id,
                  MLPrediction.event_time >= day_start,
                  MLPrediction.event_time < day_end)]
    if label_row.assessment_id is not None:
        conds.append(MLPrediction.assessment_id == label_row.assessment_id)
    return (await db.execute(select(MLPrediction).where(or_(*conds)))).scalars().all()


async def _link_predictions_for_label(db: AsyncSession, label_row) -> int:
    """Take over every candidate prediction whose current label is NULL,
    ineligible or lower-ranked than this label. Returns rows linked."""
    from db_models import MLLabel, MLPrediction
    if not _label_eligible(label_row):
        return 0
    preds = await _candidate_predictions(db, label_row)
    if not preds:
        return 0
    current_ids = {p.outcome_label_id for p in preds if p.outcome_label_id}
    current = {}
    if current_ids:
        current = {r.id: r for r in (await db.execute(
            select(MLLabel).where(MLLabel.id.in_(list(current_ids))))).scalars().all()}
    take = []
    my_rank = _label_rank(label_row)
    for p in preds:
        cur = current.get(p.outcome_label_id) if p.outcome_label_id else None
        if cur is None or cur.id == label_row.id or not _label_eligible(cur) or _label_rank(cur) < my_rank:
            take.append(p.id)
    if not take:
        return 0
    res = await db.execute(
        update(MLPrediction).where(MLPrediction.id.in_(take))
        .values(outcome_label_id=label_row.id, outcome_label=label_row.label,
                outcome_recorded_at=datetime.utcnow())
        .execution_options(synchronize_session=False))
    return res.rowcount or 0


async def _unlink_label(db: AsyncSession, label_row) -> Dict[str, int]:
    """Clear this label's outcome links and re-resolve each freed prediction
    to the best remaining eligible label (same candidate rule, inverted)."""
    from db_models import MLLabel, MLPrediction
    freed = (await db.execute(
        update(MLPrediction).where(MLPrediction.outcome_label_id == label_row.id)
        .values(outcome_label_id=None, outcome_label=None, outcome_recorded_at=None)
        .returning(MLPrediction.id).execution_options(synchronize_session=False))).scalars().all()
    relinked = 0
    for pid in freed:
        p = await db.get(MLPrediction, pid)
        if p is None:
            continue
        conds = []
        if p.assessment_id is not None:
            conds.append(MLLabel.assessment_id == p.assessment_id)
        if p.event_time is not None:
            day_start = datetime(p.event_time.year, p.event_time.month, p.event_time.day)
            conds.append(and_(MLLabel.subject_type == p.subject_type,
                              MLLabel.subject_id == p.subject_id,
                              MLLabel.event_time >= day_start,
                              MLLabel.event_time < day_start + timedelta(days=1)))
        if not conds:
            continue
        cands = [l for l in (await db.execute(
            select(MLLabel).where(MLLabel.id != label_row.id, or_(*conds)))).scalars().all()
                 if _label_eligible(l)]
        if not cands:
            continue
        best = max(cands, key=_label_rank)
        p.outcome_label_id = best.id
        p.outcome_label = best.label
        p.outcome_recorded_at = datetime.utcnow()
        relinked += 1
    await db.flush()
    return {"unlinked": len(freed), "relinked": relinked}


class LabelingService:

    async def create_label(self, db: AsyncSession, *, subject_id: str,
                           label: str, label_kind: str, source: str,
                           event_time: datetime, created_by: str,
                           confidence: float = 1.0,
                           assessment_id: Optional[str] = None,
                           notes: Optional[str] = None,
                           actor_user_id: Optional[int] = None) -> Dict[str, Any]:
        """Idempotent label creation. Raises ValueError with a client-safe
        message on rule violations (routes map it to 422)."""
        from db_models import MLLabel, ThreatAssessmentRecord

        if label not in VALID_LABELS:
            raise ValueError(f"label must be one of {VALID_LABELS}")
        if label_kind not in VALID_KINDS:
            raise ValueError(f"label_kind must be one of {VALID_KINDS}")
        confidence = max(0.0, min(1.0, float(confidence)))
        if label_kind == "weak" and confidence >= 1.0:
            confidence = 0.5  # weak labels never carry full confidence

        assessment_uuid = None
        if assessment_id:
            try:
                assessment_uuid = uuid_mod.UUID(str(assessment_id))
            except (ValueError, TypeError):
                raise ValueError("assessment_id is not a valid id")
            assessment = (await db.execute(
                select(ThreatAssessmentRecord)
                .where(ThreatAssessmentRecord.id == assessment_uuid)
            )).scalar_one_or_none()
            if assessment is None:
                raise ValueError("assessment_id not found")
            # THE rule: an unresolved (or merely auto-generated) assessment
            # is not evidence. It can seed at most a WEAK label.
            if label_kind == "manual" and assessment.status != "resolved":
                raise ValueError(
                    "UNREVIEWED_ALERT: an unresolved assessment cannot back a "
                    "manual label — resolve the assessment first or record a "
                    "weak label")

        person_uuid = None
        try:
            person_uuid = uuid_mod.UUID(str(subject_id))
        except (ValueError, TypeError):
            pass  # non-identity subjects keep person_id NULL

        key = _label_idempotency_key("identity", subject_id, source, event_time)
        row_id = uuid_mod.uuid4()
        insert_stmt = pg_insert(MLLabel).values(
            id=row_id,
            subject_type="identity",
            subject_id=subject_id,
            person_id=person_uuid,
            assessment_id=assessment_uuid,
            label=label,
            label_kind=label_kind,
            label_definition_version=LABEL_DEFINITION_VERSION,
            confidence=confidence,
            source=source[:64],
            event_time=event_time,
            status="active",
            review_status="unreviewed",
            notes=notes,
            idempotency_key=key,
            created_at=datetime.utcnow(),
            created_by=created_by[:255],
        ).on_conflict_do_nothing(index_elements=["idempotency_key"])
        result = await db.execute(insert_stmt)
        inserted = bool(result.rowcount)

        row = (await db.execute(
            select(MLLabel).where(MLLabel.idempotency_key == key))).scalar_one()
        linked = 0
        if inserted:
            linked = await _link_predictions_for_label(db, row)
            await ml_audit(db, action="label_create", actor_username=created_by,
                           actor_user_id=actor_user_id, object_type="ml_label",
                           object_id=str(row.id),
                           after={"label": label, "kind": label_kind, "source": source,
                                  "linked_predictions": linked})
        await db.commit()
        payload = serialize_label(row)
        payload["deduplicated"] = not inserted
        payload["linked_predictions"] = linked
        return payload

    async def review_label(self, db: AsyncSession, label_id: str, *,
                           action: str, actor: str, notes: Optional[str] = None,
                           actor_user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """confirm | dispute | retract. Only CONFIRMED manual labels count
        toward supervised gates. Returns None for an unknown id."""
        from db_models import MLLabel

        if action not in REVIEW_ACTIONS:
            raise ValueError(f"action must be one of {REVIEW_ACTIONS}")
        try:
            row_uuid = uuid_mod.UUID(str(label_id))
        except (ValueError, TypeError):
            return None
        row = (await db.execute(
            select(MLLabel).where(MLLabel.id == row_uuid))).scalar_one_or_none()
        if row is None:
            return None
        if row.status == "retracted":
            raise ValueError("label is retracted and can no longer be reviewed")

        before = {"review_status": row.review_status, "status": row.status}
        now = datetime.utcnow()
        if action == "confirm":
            row.review_status = "reviewed"
        elif action == "dispute":
            row.review_status = "disputed"
        else:  # retract
            row.status = "retracted"
        row.reviewed_by = actor[:255]
        row.reviewed_at = now
        if notes:
            row.notes = (row.notes + "\n" if row.notes else "") + f"[{action}] {notes}"[:2000]

        # Outcome linkage follows eligibility/rank in the same transaction.
        link_stats: Dict[str, int] = {}
        if action == "confirm":
            link_stats = {"linked": await _link_predictions_for_label(db, row)}
        else:  # dispute / retract: this label can no longer explain outcomes
            link_stats = await _unlink_label(db, row)

        await ml_audit(db, action="label_review", actor_username=actor,
                       actor_user_id=actor_user_id, object_type="ml_label",
                       object_id=str(row.id), before=before,
                       after={"review_status": row.review_status, "status": row.status, **link_stats},
                       reason=notes)
        await db.commit()
        return serialize_label(row)

    async def supersede_label(self, db: AsyncSession, label_id: str, *, label: str,
                              actor: str, notes: Optional[str] = None,
                              actor_user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Correct a label by SUPERSESSION, never by editing in place: the old
        row becomes status='superseded' (history kept), a new ACTIVE row is
        created with supersedes_id = old.id and its own idempotency key, and
        every outcome the old label explained is re-pointed to the new one in
        the same transaction. Only an ACTIVE label can be superseded (chains,
        never cycles). Returns None for an unknown id; raises ValueError with a
        stable code for rule violations."""
        from db_models import MLLabel, MLPrediction
        if label not in VALID_LABELS:
            raise ValueError(f"label must be one of {VALID_LABELS}")
        try:
            row_uuid = uuid_mod.UUID(str(label_id))
        except (ValueError, TypeError):
            return None
        old = (await db.execute(select(MLLabel).where(MLLabel.id == row_uuid))).scalar_one_or_none()
        if old is None:
            return None
        if old.status != "active":
            raise ValueError(f"LABEL_NOT_ACTIVE: only an active label can be superseded (status={old.status})")
        now = datetime.utcnow()
        new_id = uuid_mod.uuid4()
        new = MLLabel(
            id=new_id, subject_type=old.subject_type, subject_id=old.subject_id,
            person_id=old.person_id, assessment_id=old.assessment_id,
            label=label, label_kind=old.label_kind,
            label_definition_version=old.label_definition_version,
            confidence=old.confidence, source=old.source, event_time=old.event_time,
            status="active", review_status="unreviewed", supersedes_id=old.id,
            notes=notes, idempotency_key=f"{old.idempotency_key}:supersedes:{old.id}",
            created_at=now, created_by=actor[:255])
        db.add(new)
        old.status = "superseded"
        await db.flush()
        # outcomes explained by the old label now belong to the new one
        moved = (await db.execute(
            update(MLPrediction).where(MLPrediction.outcome_label_id == old.id)
            .values(outcome_label_id=new.id, outcome_label=new.label, outcome_recorded_at=now)
            .execution_options(synchronize_session=False))).rowcount or 0
        moved += await _link_predictions_for_label(db, new)
        await ml_audit(db, action="label_supersede", actor_username=actor,
                       actor_user_id=actor_user_id, object_type="ml_label",
                       object_id=str(new.id),
                       before={"superseded_label_id": str(old.id), "label": old.label},
                       after={"label": new.label, "relinked_predictions": moved},
                       reason=notes)
        await db.commit()
        payload = serialize_label(new)
        payload["superseded_label_id"] = str(old.id)
        payload["relinked_predictions"] = moved
        return payload

    async def list_labels(self, db: AsyncSession, *, label: Optional[str] = None,
                          label_kind: Optional[str] = None,
                          review_status: Optional[str] = None,
                          subject_id: Optional[str] = None,
                          page: int = 1, page_size: int = 25) -> Dict[str, Any]:
        from db_models import MLLabel
        query = select(MLLabel)
        if label:
            query = query.where(MLLabel.label == label)
        if label_kind:
            query = query.where(MLLabel.label_kind == label_kind)
        if review_status:
            query = query.where(MLLabel.review_status == review_status)
        if subject_id:
            query = query.where(MLLabel.subject_id == subject_id)
        total = (await db.execute(
            select(sa_func.count()).select_from(query.subquery()))).scalar() or 0
        rows = (await db.execute(
            query.order_by(MLLabel.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size))).scalars().all()
        return {"items": [serialize_label(r) for r in rows], "total": int(total),
                "page": page, "page_size": page_size}

    async def label_stats(self, db: AsyncSession) -> Dict[str, Any]:
        """The honest supervised-gate payload: what counts, what doesn't,
        and how far the gate is from opening. No number here is fabricated."""
        from db_models import MLLabel
        counted = select(MLLabel).where(
            MLLabel.status == "active",
            MLLabel.label_kind == "manual",
            MLLabel.review_status == "reviewed",
        ).subquery()
        rows = (await db.execute(
            select(counted.c.label, sa_func.count()).group_by(counted.c.label))).all()
        by_class = {label: int(count) for label, count in rows}
        reviewed_total = sum(by_class.values())

        all_rows = (await db.execute(
            select(MLLabel.label_kind, MLLabel.review_status, sa_func.count())
            .where(MLLabel.status == "active")
            .group_by(MLLabel.label_kind, MLLabel.review_status))).all()

        required_total = int(settings.ML_SUPERVISED_MIN_LABELS)
        required_per_class = int(settings.ML_SUPERVISED_MIN_PER_CLASS)
        positive = by_class.get("positive", 0)
        negative = by_class.get("negative", 0)
        gate_open = (reviewed_total >= required_total
                     and positive >= required_per_class
                     and negative >= required_per_class)
        return {
            "counted_reviewed_manual": {
                "total": reviewed_total, "positive": positive, "negative": negative,
                "unknown": by_class.get("unknown", 0),
            },
            "not_counted": [
                {"label_kind": kind, "review_status": status, "count": int(count)}
                for kind, status, count in all_rows
                if not (kind == "manual" and status == "reviewed")
            ],
            "required_total": required_total,
            "required_per_class": required_per_class,
            "supervised_gate_open": gate_open,
        }

    def supervised_refusal(self, stats: Dict[str, Any]) -> Dict[str, Any]:
        """The structured refusal the trainer returns below the gate."""
        counted = stats["counted_reviewed_manual"]
        return {
            "status": "blocked",
            "reason": "INSUFFICIENT_REVIEWED_LABELS",
            "required_total": stats["required_total"],
            "required_per_class": stats["required_per_class"],
            "available_total": counted["total"],
            "available_positive": counted["positive"],
            "available_negative": counted["negative"],
        }


# Global instance
labeling_service = LabelingService()
