"""
ML outcome linkage (plan §9): predictions carry their outcome as durable
columns (outcome_label_id / outcome_label / outcome_recorded_at) written INSIDE
the label transaction — never inferred from "current labels" at read time.

    candidates   assessment_id = L.assessment_id  ∪  same subject, event_time in
                 L's UTC-day bucket; NULL event_time reachable only via assessment
    eligibility  status='active' AND review_status != 'disputed'
    rank         (manual, reviewed, created_at) — highest wins; take-over only upward
    unlink       dispute / retract free the predictions and re-resolve them to the
                 best remaining eligible label; confirm re-links
    supersede    outcome links re-point to the new row in the same transaction

    docker exec face_recognition_api python -m pytest tests/test_ml_outcome_linkage.py -q
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from conftest import run_on_shared_loop as run_async

DAY = datetime(2026, 3, 10, 12, 0, 0)          # fixed UTC day bucket for the suite


async def _db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()
    return db_manager


def _sql(statement, params=None, fetch="all"):
    async def _run():
        dm = await _db()
        async with dm.get_session() as db:
            res = await db.execute(text(statement), params or {})
            if not res.returns_rows:
                await db.commit()
                return res.rowcount
            value = res.scalar() if fetch == "scalar" else res.all()
            await db.commit()
            return value
    return run_async(_run())


def _svc():
    from backend.ml.labeling_service import labeling_service
    return labeling_service


def _call(coro_fn, *a, **kw):
    async def _run():
        dm = await _db()
        async with dm.get_session() as db:
            return await coro_fn(db, *a, **kw)
    return run_async(_run())


class Subject:
    def __init__(self):
        self.id = str(uuid.uuid4())      # a real identity: labels resolve person_id → identities FK
        _sql("INSERT INTO identities (id, type, status, display_name, first_seen_at, last_seen_at, appearances_count, "
             " created_at, updated_at) VALUES (CAST(:i AS uuid), 'UNKNOWN', 'ACTIVE', :n, now(), now(), 0, now(), now())",
             {"i": self.id, "n": f"qa_outcome_{self.id[:8]}"})
        self.assessment_id = None
        self.pred_ids = []
        self.label_ids = []

    def assessment(self, status="resolved"):
        aid = str(uuid.uuid4())
        _sql("INSERT INTO threat_assessments (id, subject_type, subject_id, total_risk_score, severity, confidence, "
             " signals, model_version, status, source_timestamp, idempotency_key, created_at, updated_at) "
             "VALUES (CAST(:i AS uuid), 'identity', :s, 0.5, 'medium', 0.9, '[]', 'qa', :st, :t, :k, now(), now())",
             {"i": aid, "s": self.id, "st": status, "t": DAY, "k": f"qa-outcome-{aid}"})
        self.assessment_id = aid
        return aid

    def prediction(self, *, event_time=DAY, assessment_id=None):
        pid = str(uuid.uuid4())
        _sql("INSERT INTO ml_predictions (id, subject_type, subject_id, model_type, model_version_label, model_purpose, "
             " requested_mode, actual_mode_used, score_type, is_probability, calibration_status, assessment_id, "
             " event_time, as_of_timestamp, idempotency_key, created_at) "
             "VALUES (CAST(:i AS uuid), 'identity', :s, 'behavior_anomaly_model', 'qa', 'behavioral_anomaly_detection', "
             " 'shadow', 'rules', 'anomaly_score', false, 'not_applicable', CAST(:a AS uuid), :e, now(), :k, now())",
             {"i": pid, "s": self.id, "a": assessment_id, "e": event_time, "k": f"qa-outcome-{pid}"})
        self.pred_ids.append(pid)
        return pid

    def outcome(self, pid):
        return _sql("SELECT outcome_label_id::text, outcome_label, outcome_recorded_at FROM ml_predictions "
                    "WHERE id = CAST(:i AS uuid)", {"i": pid})[0]

    def label(self, *, kind="weak", source="qa-src", label="positive", event_time=DAY, assessment_id=None):
        out = _call(_svc().create_label, subject_id=self.id, label=label, label_kind=kind, source=source,
                    event_time=event_time, created_by="qa", assessment_id=assessment_id)
        self.label_ids.append(out["id"])
        return out

    def cleanup(self):
        _sql("DELETE FROM ml_predictions WHERE subject_id = :s", {"s": self.id})
        _sql("DELETE FROM ml_audit_log WHERE object_type = 'ml_label' AND object_id IN "
             "(SELECT id::text FROM ml_labels WHERE subject_id = :s)", {"s": self.id})
        _sql("DELETE FROM ml_labels WHERE subject_id = :s", {"s": self.id})
        _sql("DELETE FROM threat_assessments WHERE subject_id = :s", {"s": self.id})
        _sql("DELETE FROM identities WHERE id = CAST(:s AS uuid)", {"s": self.id})


@pytest.fixture
def subj():
    s = Subject()
    try:
        yield s
    finally:
        s.cleanup()


# ---------------------------------------------------------------- linking rules

def test_assessment_link_and_null_event_time_reachable_only_via_assessment(subj):
    aid = subj.assessment()
    via_assessment = subj.prediction(event_time=None, assessment_id=aid)
    orphan = subj.prediction(event_time=None)                       # no event_time, no assessment
    out = subj.label(kind="weak", assessment_id=aid)
    assert out["deduplicated"] is False and out["linked_predictions"] == 1, out
    assert subj.outcome(via_assessment)[0] == out["id"] and subj.outcome(via_assessment)[1] == "positive"
    assert subj.outcome(orphan) == (None, None, None)


def test_same_day_bucket_links_and_manual_outranks_weak_but_not_the_reverse(subj):
    p = subj.prediction(event_time=DAY + timedelta(hours=5))
    other_day = subj.prediction(event_time=DAY + timedelta(days=1))
    weak = subj.label(kind="weak", source="weak-src")
    assert subj.outcome(p)[0] == weak["id"]
    assert subj.outcome(other_day) == (None, None, None), "outside the UTC-day bucket"
    manual = subj.label(kind="manual", source="manual-src", label="negative")
    o = subj.outcome(p)
    assert o[0] == manual["id"] and o[1] == "negative", "manual takes over from weak"
    later_weak = subj.label(kind="weak", source="weak-src-2")     # newer, lower rank
    assert subj.outcome(p)[0] == manual["id"], "a lower-ranked newer label never takes over"
    assert later_weak["linked_predictions"] == 0


def test_dispute_unlinks_and_re_resolves_confirm_relinks(subj):
    p = subj.prediction()
    weak = subj.label(kind="weak", source="w")
    manual = subj.label(kind="manual", source="m")
    assert subj.outcome(p)[0] == manual["id"]
    disputed = _call(_svc().review_label, manual["id"], action="dispute", actor="qa", notes="wrong")
    assert disputed["review_status"] == "disputed"
    assert subj.outcome(p)[0] == weak["id"], "freed prediction re-resolves to the best remaining eligible label"
    confirmed = _call(_svc().review_label, manual["id"], action="confirm", actor="qa")
    assert confirmed["review_status"] == "reviewed"
    assert subj.outcome(p)[0] == manual["id"], "confirm re-links (manual+reviewed outranks weak)"
    _call(_svc().review_label, manual["id"], action="retract", actor="qa")
    assert subj.outcome(p)[0] == weak["id"]
    _call(_svc().review_label, weak["id"], action="retract", actor="qa")
    assert subj.outcome(p) == (None, None, None), "no eligible label left → outcome cleared, not guessed"


def test_deduplicated_create_does_not_churn_outcome_recorded_at(subj):
    p = subj.prediction()
    first = subj.label(kind="weak", source="dedup")
    recorded = subj.outcome(p)[2]
    again = subj.label(kind="weak", source="dedup")               # same idempotency key (subject/source/day)
    assert again["deduplicated"] is True and again["id"] == first["id"] and again["linked_predictions"] == 0
    assert subj.outcome(p)[2] == recorded


def test_supersede_re_points_outcomes_and_keeps_history(subj):
    p = subj.prediction()
    old = subj.label(kind="manual", source="m", label="positive")
    assert subj.outcome(p)[0] == old["id"]
    new = _call(_svc().supersede_label, old["id"], label="negative", actor="qa", notes="corrected")
    subj.label_ids.append(new["id"])
    assert new["supersedes_id"] == old["id"] and new["status"] == "active" and new["label"] == "negative"
    o = subj.outcome(p)
    assert o[0] == new["id"] and o[1] == "negative"
    assert _sql("SELECT status FROM ml_labels WHERE id = CAST(:i AS uuid)", {"i": old["id"]}, fetch="scalar") == "superseded"
    with pytest.raises(ValueError) as exc:
        _call(_svc().supersede_label, old["id"], label="unknown", actor="qa")
    assert "LABEL_NOT_ACTIVE" in str(exc.value)
    # a superseded label never explains an outcome
    assert _sql("SELECT count(*) FROM ml_predictions WHERE outcome_label_id = CAST(:i AS uuid)",
                {"i": old["id"]}, fetch="scalar") == 0


def test_no_prediction_points_at_an_ineligible_label():
    """Data quality: every recorded outcome resolves to an active, non-disputed label."""
    n = _sql("SELECT count(*) FROM ml_predictions p JOIN ml_labels l ON l.id = p.outcome_label_id "
             "WHERE l.status <> 'active' OR l.review_status = 'disputed'", fetch="scalar")
    assert n == 0, f"{n} predictions point at an ineligible label"
    assert _sql("SELECT count(*) FROM ml_predictions WHERE (outcome_label_id IS NULL) <> (outcome_label IS NULL)",
                fetch="scalar") == 0, "outcome_label_id and outcome_label are written together"
