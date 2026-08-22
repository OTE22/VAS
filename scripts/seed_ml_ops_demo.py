"""
Deterministic ML-Ops DEMO seed — exercises the REAL pipeline end to end and
leaves ~1,000 rows of fully-linked lineage behind for review and for
tests/test_ml_ops_lineage.py.

    dry-run (default)   prints the plan and the current seed row counts
    --apply --yes-i-understand
                        seed:  cameras → identities/appearances → collector →
                               assessments → labels (some reviewed / superseded)
                               → supervised dataset → two trainings →
                               shadow-approve v1 → shadow predictions →
                               shadow-approve v2 (displaces v1, retires its
                               threshold set) → shadow predictions again →
                               drift reports → post-seed assertions
    --remove --yes-i-understand
                        remove everything the seed created, RESTRICT-safe order

Rules
    * refuses when settings.ENVIRONMENT is production;
    * positive database-name check: `face_recognition` (development) or an
      isolated scratch (`face_recognition_regression_*`,
      `face_recognition_migration_test_*`) — anything else is refused;
    * the three cameras `seed-pipeline-01..03` are created through the SAME
      registration contract live ingest uses (image_processing.
      ensure_pipeline_registered) and get coordinates the way the coordinates
      route sets them — never an invented free string; every seeded appearance
      references only those ids (FK RESTRICT would refuse anything else);
    * everything is deterministic (random.Random(20260815)); every seed row is
      recognisable: identities `seed_person_NNN`, jobs `seed-mlops-*`,
      assessments `seed-assess-*`, labels source `seed-*`, cameras
      `seed-pipeline-*`;
    * NOTE: only `behavior_anomaly_model` is implemented in this release — the
      other anomaly model types are reserved interfaces (the API answers
      MODEL_TYPE_NOT_IMPLEMENTED); the seed trains that one type twice
      (isolation_forest, mad_baseline) so v1/v2 lineage exists;
    * entering shadow displaces (archives) any CURRENT shadow model of the
      type — the one-shadow invariant. On the development database that is
      demo data by the owner's rule; the script prints what it displaced.

Run inside the api container:
    docker exec -w /app face_recognition_api python scripts/seed_ml_ops_demo.py
    docker exec -w /app face_recognition_api python scripts/seed_ml_ops_demo.py --apply --yes-i-understand
    docker exec -w /app face_recognition_api python scripts/seed_ml_ops_demo.py --remove --yes-i-understand
"""
import argparse
import asyncio
import os
import random
import re
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from backend.ml.constants import FEATURE_SET_VERSION  # noqa: E402

SEED = 20260815
PIPELINES = [
    ("seed-pipeline-01", "Seed Lobby", 33.8938, 35.5018),
    ("seed-pipeline-02", "Seed Parking", 33.8951, 35.5030),
    ("seed-pipeline-03", "Seed Loading Dock", 33.8929, 35.5005),
]
# Base volumes for --scale 1 (~1,000 linked rows). --scale N multiplies
# identities and every per-identity stage, so --scale 10 exercises the
# pipeline on ~10k+ rows without changing any ratio the seed relies on.
SCALE = 1
IDENTITIES = 120
LABELS = 60
JOB_PREFIX = "seed-mlops-"
ALLOWED_DB = re.compile(r"^face_recognition(_regression_.*|_migration_test_.*)?$")


# ---------------------------------------------------------------- guards / plumbing

async def _guards(db):
    problems = []
    from config import settings
    if str(settings.ENVIRONMENT).strip().lower() in ("production", "prod"):
        problems.append("ENVIRONMENT is production")
    name = (await db.execute(text("SELECT current_database()"))).scalar()
    if not ALLOWED_DB.match(name or ""):
        problems.append(f"connected database {name!r} is not the development database nor an isolated scratch")
    return problems


async def _counts(db):
    q = {
        "pipelines": "SELECT count(*) FROM pipelines WHERE pipeline_id LIKE 'seed-pipeline-%'",
        "identities": "SELECT count(*) FROM identities WHERE display_name LIKE 'seed_person_%'",
        "appearances": "SELECT count(*) FROM identity_appearances WHERE pipeline_id LIKE 'seed-pipeline-%'",
        "snapshots": "SELECT count(*) FROM ml_feature_snapshots WHERE entity_id IN "
                     "(SELECT id::text FROM identities WHERE display_name LIKE 'seed_person_%')",
        "assessments": "SELECT count(*) FROM threat_assessments WHERE idempotency_key LIKE 'seed-assess-%'",
        "labels": "SELECT count(*) FROM ml_labels WHERE source LIKE 'seed-%'",
        "outcomes": "SELECT count(*) FROM ml_predictions WHERE outcome_label_id IS NOT NULL AND subject_id IN "
                    "(SELECT id::text FROM identities WHERE display_name LIKE 'seed_person_%')",
        "datasets": "SELECT count(*) FROM ml_datasets WHERE build_job_id LIKE 'seed-mlops-%' OR name LIKE 'seed-%'",
        "models": "SELECT count(*) FROM ml_models WHERE training_job_id LIKE 'seed-mlops-%'",
        "thresholds": "SELECT count(*) FROM ml_model_thresholds WHERE model_id IN "
                      "(SELECT id FROM ml_models WHERE training_job_id LIKE 'seed-mlops-%')",
        "predictions": "SELECT count(*) FROM ml_predictions WHERE subject_id IN "
                       "(SELECT id::text FROM identities WHERE display_name LIKE 'seed_person_%')",
        "comparisons": "SELECT count(*) FROM ml_shadow_comparisons WHERE subject_id IN "
                       "(SELECT id::text FROM identities WHERE display_name LIKE 'seed_person_%')",
        "drift_reports": "SELECT count(*) FROM ml_drift_reports WHERE model_id IN "
                         "(SELECT id FROM ml_models WHERE training_job_id LIKE 'seed-mlops-%')",
        "task_history": "SELECT count(*) FROM background_task_history WHERE job_id LIKE 'seed-mlops-%'",
    }
    out = {}
    for k, sql in q.items():
        out[k] = (await db.execute(text(sql))).scalar()
    out["TOTAL"] = sum(v for k, v in out.items() if k not in ("TOTAL", "outcomes"))   # outcomes are a subset
    return out


def _print_counts(title, counts):
    print(f"\n{title}")
    for k, v in counts.items():
        print(f"  {k:14s} {v}")


# ---------------------------------------------------------------- seed steps

async def _seed_pipelines(db):
    from backend.services.image_processing import ensure_pipeline_registered
    from db_models import Pipeline
    from sqlalchemy import select
    for pid, name, lat, lon in PIPELINES:
        await ensure_pipeline_registered(db, pid, location_name=name)     # the live-ingest contract
        row = (await db.execute(select(Pipeline).where(Pipeline.pipeline_id == pid))).scalar_one()
        row.latitude, row.longitude, row.location_name = lat, lon, name   # what PUT /pipelines/{id}/coordinates sets
        row.is_active = 1
        row.updated_at = datetime.utcnow()
    await db.commit()
    print(f"[1] cameras registered: {[p[0] for p in PIPELINES]}")


async def _seed_identities(db, rng):
    now = datetime.utcnow().replace(second=0, microsecond=0)
    ids = []
    total_app = 0
    for i in range(IDENTITIES * SCALE):
        visits = 1 + rng.randint(0, 5)                    # 1..6
        first_day = 3 + rng.randint(0, 26)                # first sighting 3..29 days back
        stamps = []
        for v in range(visits):
            day = max(1, first_day - v * rng.randint(1, 4))
            hour = (1 + rng.randint(0, 3)) if i % 3 == 0 else (8 + rng.randint(0, 10))   # some night visitors
            stamps.append((now - timedelta(days=day)).replace(hour=hour, minute=rng.randint(0, 59)))
        stamps.sort()
        ident = (await db.execute(text(
            "INSERT INTO identities (id, type, status, display_name, first_seen_at, last_seen_at, created_at, "
            " updated_at, appearances_count) VALUES (gen_random_uuid(), :t, 'ACTIVE', :n, :f, :l, now(), now(), :c) "
            "RETURNING id"),
            {"t": "KNOWN" if i % 4 == 0 else "UNKNOWN", "n": f"seed_person_{i:05d}", "f": stamps[0], "l": stamps[-1],
             "c": len(stamps)})).scalar()
        for v, ts in enumerate(stamps):
            pid = PIPELINES[(i + v) % 3][0] if i % 5 == 0 else PIPELINES[i % 3][0]   # some multi-camera identities
            await db.execute(text(
                "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, end_time, created_at) "
                "VALUES (:i, :p, :s, :e, now())"),
                {"i": ident, "p": pid, "s": ts, "e": ts + timedelta(minutes=rng.randint(1, 20))})
            total_app += 1
        ids.append((str(ident), stamps[-1]))
    await db.commit()
    print(f"[2] identities: {len(ids)}, appearances: {total_app}")
    return ids


async def _collect(db):
    from backend.ml.collector import run_collection
    stats = await run_collection(db, run_id=f"{JOB_PREFIX}collect", full_rebuild=True)
    await db.commit()
    print(f"[3] collector: {stats}")
    return stats


async def _assessments(db, rng, ids):
    """Resolved / open assessments for a third of the identities (labels need
    a RESOLVED assessment for manual kind; open ones may seed weak labels)."""
    out = []
    for n, (ident, last) in enumerate(ids):
        if n % 3:
            continue
        aid = str(uuid.uuid4())
        status = "resolved" if n % 2 == 0 else "open"
        sev = rng.choice(["low", "moderate", "high"])
        await db.execute(text(
            "INSERT INTO threat_assessments (id, subject_type, subject_id, person_id, pipeline_id, total_risk_score, "
            " severity, confidence, signals, model_version, status, source_timestamp, idempotency_key, created_at, "
            " updated_at) VALUES (CAST(:i AS uuid), 'identity', :s, CAST(:pu AS uuid), :p, :score, :sev, 0.8, '[]', "
            " 'rules-v1', :st, :t, :k, now(), now())"),
            {"i": aid, "s": ident, "pu": ident, "p": PIPELINES[n % 3][0], "score": round(rng.uniform(0.1, 0.9), 3), "sev": sev,
             "st": status, "t": last, "k": f"seed-assess-{aid}"})
        out.append((ident, aid, status, last, sev))
    await db.commit()
    print(f"[4] assessments: {len(out)}")
    return out


async def _labels(db, rng, assessments, ids):
    from backend.ml.labeling_service import labeling_service
    created, superseded, reviewed = [], 0, 0
    # 40 labels from assessments (manual when resolved, weak when open)
    for k, (ident, aid, status, when, sev) in enumerate(assessments[:40 * SCALE]):
        kind = "manual" if status == "resolved" else "weak"
        out = await labeling_service.create_label(
            db, subject_id=ident, label=rng.choice(["positive", "negative", "unknown"]), label_kind=kind,
            source=f"seed-{kind}", event_time=when, created_by="seed", assessment_id=aid,
            notes="seed label from assessment")
        created.append(out["id"])
    # 20 plain weak labels on other identities (same-day bucket linkage)
    for k, (ident, last) in enumerate(ids[80 * SCALE:100 * SCALE]):
        out = await labeling_service.create_label(
            db, subject_id=ident, label="negative" if k % 2 else "positive", label_kind="weak",
            source="seed-weak-plain", event_time=last, created_by="seed")
        created.append(out["id"])
    # reviews: confirm 30, dispute 4; supersede 6 (corrections). Confirmations
    # were 12 per scale unit, which yielded ~38 supervised rows at scale 10 —
    # under the 50-row floor — so the supervised training path was never
    # actually exercised by this seed. 30 clears the floor from scale 4 up.
    for lid in created[:30 * SCALE]:
        await labeling_service.review_label(db, lid, action="confirm", actor="seed-reviewer")
        reviewed += 1
    for lid in created[30 * SCALE:34 * SCALE]:
        await labeling_service.review_label(db, lid, action="dispute", actor="seed-reviewer", notes="seed dispute")
        reviewed += 1
    for lid in created[34 * SCALE:40 * SCALE]:
        row = await labeling_service.supersede_label(db, lid, label="unknown", actor="seed-reviewer",
                                                     notes="seed correction")
        if row:
            superseded += 1
    print(f"[5] labels: {len(created)} created, {reviewed} reviewed, {superseded} superseded")
    return created


async def _outcomes(db, rng, ids, assessments, created):
    """Outcome linkage happens inside label transactions — labels created or
    confirmed AFTER predictions exist take those predictions over (assessment
    link ∪ same-subject UTC-day bucket)."""
    from backend.ml.labeling_service import labeling_service
    linked = 0
    for k, (ident, last) in enumerate(ids[100:120]):
        out = await labeling_service.create_label(
            db, subject_id=ident, label="positive" if k % 3 else "negative", label_kind="weak",
            source="seed-outcome", event_time=last, created_by="seed")
        linked += out["linked_predictions"]
    for lid in created[26:36]:                        # confirm → re-link (manual+reviewed outranks weak)
        await labeling_service.review_label(db, lid, action="confirm", actor="seed-reviewer")
    n = (await db.execute(text(
        "SELECT count(*) FROM ml_predictions WHERE outcome_label_id IS NOT NULL AND subject_id IN "
        "(SELECT id::text FROM identities WHERE display_name LIKE 'seed_person_%')"))).scalar()
    print(f"[11b] outcome linkage: {linked} linked by new labels; predictions with an outcome now: {n}")


async def _dataset(db):
    from backend.ml.dataset_builder import build_dataset
    meta = await build_dataset(db, name="seed-supervised", kind="supervised", build_job_id=f"{JOB_PREFIX}dataset")
    await db.commit()
    print(f"[6] supervised dataset: status={meta.get('status')} rows={meta.get('row_count')} "
          f"lineage={meta.get('lineage_summary')}")


async def _train(job_id, algorithm):
    from backend.core.task_history import task_history_manager
    from backend.ml import trainer
    busy = trainer.try_acquire_training(job_id)
    if busy:
        raise RuntimeError(f"a training job is already running: {busy}")
    await task_history_manager.create_job(job_id=job_id, task_type="ml_training",
                                          task_name="seed training", description=f"seed {algorithm}")
    await trainer.run_training_job(job_id, algorithm=algorithm)
    task = await task_history_manager.get_task_by_job_id(job_id)
    if not task or task.get("status") != "completed":
        raise RuntimeError(f"training {job_id} did not complete: {task}")
    return task["result"]


def _approval(model, version_tag):
    return {
        "approved_by_user_id": 1, "approved_by": "seed-admin",
        "reason": f"seed shadow evaluation {version_tag}",
        "dataset_version": model["dataset_id"],
        "evaluation_report_ref": f"ml_models:{model['model_id']}:evaluation_report",
        "artifact_checksum": model["artifact_hash"], "feature_set_version": FEATURE_SET_VERSION,
        "intended_scope": "all_pipelines", "rollback_target": "stop shadow + return to rules-only observation",
    }


async def _approve(db, model, tag):
    from backend.ml.registry_service import registry_service
    displaced = await registry_service.get_stage_model(db, model["model_type"], "shadow")
    if displaced is not None:
        print(f"    (entering shadow displaces current shadow model {displaced.id} v{displaced.version})")
    out = await registry_service.transition(db, model["model_id"], to_stage="shadow", actor="seed-admin",
                                            actor_user_id=1, reason=f"seed {tag}",
                                            shadow_approval=_approval(model, tag))
    return out


async def _shadow_all(rng, ids, assessments):
    from backend.ml.shadow_service import shadow_service
    by_ident = {a[0]: a for a in assessments}
    n = 0
    for ident, last in ids:
        a = by_ident.get(ident)
        await shadow_service._run(identity_id=ident, rule_score=round(rng.uniform(0.05, 0.95), 3),
                                  rule_severity=(a[4] if a else rng.choice(["low", "moderate", "high"])),
                                  assessment_id=(a[1] if a else None),
                                  event_time=(a[3] if a else last))
        n += 1
    return n


async def _drift(db):
    from backend.ml.drift_service import drift_service
    out = await drift_service.run_all(db, job_id=f"{JOB_PREFIX}drift")
    await db.commit()
    return out


async def _post_seed_assertions(db):
    checks = {
        "seed appearances with a pipeline_id not in pipelines":
            "SELECT count(*) FROM identity_appearances a WHERE a.pipeline_id LIKE 'seed-pipeline-%' AND NOT EXISTS "
            "(SELECT 1 FROM pipelines p WHERE p.pipeline_id = a.pipeline_id)",
        "seed embeddings with a pipeline_id not in pipelines":
            "SELECT count(*) FROM identity_embeddings e WHERE e.pipeline_id LIKE 'seed-pipeline-%' AND NOT EXISTS "
            "(SELECT 1 FROM pipelines p WHERE p.pipeline_id = e.pipeline_id)",
        "successful shadow predictions without threshold/model lineage":
            "SELECT count(*) FROM ml_predictions WHERE actual_mode_used = 'shadow' AND fallback_reason IS NULL "
            "AND (threshold_id IS NULL OR threshold_version IS NULL OR model_id IS NULL)",
        "drift reports without a model": "SELECT count(*) FROM ml_drift_reports WHERE model_id IS NULL",
        "snapshots with empty features": "SELECT count(*) FROM ml_feature_snapshots WHERE features = '{}'::jsonb",
        "more than one active threshold set per (model, scope)":
            "SELECT count(*) FROM (SELECT model_id, scope_type, scope_id FROM ml_model_thresholds WHERE status = 'active' "
            "GROUP BY 1,2,3 HAVING count(*) > 1) x",
        "predictions pointing at an ineligible label":
            "SELECT count(*) FROM ml_predictions p JOIN ml_labels l ON l.id = p.outcome_label_id "
            "WHERE l.status <> 'active' OR l.review_status = 'disputed'",
    }
    failed = []
    for what, sql in checks.items():
        n = (await db.execute(text(sql))).scalar()
        print(f"  [{'OK ' if n == 0 else 'FAIL'}] {what}: {n}")
        if n:
            failed.append(what)
    return failed


async def _apply(db, db_manager):
    existing = await _counts(db)
    if existing["TOTAL"]:
        _print_counts("REFUSED: seed rows already exist — run --remove --yes-i-understand first", existing)
        return 2
    # Redis version marker: registry transitions bump it so EVERY process (this
    # script and the running API workers) reloads the shadow model + threshold
    # set immediately instead of after the process-local TTL.
    from backend.core.cache_manager import cache_manager
    await cache_manager.initialize()
    if not getattr(cache_manager, "redis_client", None):
        print("REFUSED: Redis is not reachable — the model version marker cannot be bumped")
        return 2
    rng = random.Random(SEED)
    await _seed_pipelines(db)
    ids = await _seed_identities(db, rng)
    await _collect(db)
    assessments = await _assessments(db, rng, ids)
    created = await _labels(db, rng, assessments, ids)
    await _dataset(db)
    v1 = await _train(f"{JOB_PREFIX}{uuid.uuid4().hex[:8]}", "isolation_forest")
    print(f"[7] trained v1: model={v1['model_id']} stage={v1['stage']} gates={v1['quality_gates']['passed']}")
    v2 = await _train(f"{JOB_PREFIX}{uuid.uuid4().hex[:8]}", "mad_baseline")
    print(f"[7] trained v2: model={v2['model_id']} stage={v2['stage']} gates={v2['quality_gates']['passed']}")
    async with db_manager.get_session() as s:
        out = await _approve(s, v1, "v1")
    print(f"[8] shadow-approved v1 (thresholds active): stage={out['stage']}")
    n = await _shadow_all(rng, ids, assessments)
    print(f"[9] shadow predictions round 1: {n}")
    async with db_manager.get_session() as s:
        out = await _approve(s, v2, "v2")
    print(f"[10] shadow-approved v2 (v1 archived, its threshold set retired): stage={out['stage']}")
    n = await _shadow_all(rng, ids, assessments)
    print(f"[11] shadow predictions round 2: {n}")
    async with db_manager.get_session() as s:
        await _outcomes(s, rng, ids, assessments, created)
    async with db_manager.get_session() as s:
        drift = await _drift(s)
    print(f"[12] drift: {[(r['model_type'], r['data_drift'].get('severity'), r['prediction_drift'].get('severity')) for r in drift.get('reports', [])] or drift}")
    print("\n[13] post-seed assertions")
    async with db_manager.get_session() as s:
        failed = await _post_seed_assertions(s)
        _print_counts("row counts", await _counts(s))
    return 1 if failed else 0


async def _remove(db):
    stmts = [
        # ML rows for seed subjects first (models later: predictions must not lose lineage by SET NULL)
        "DELETE FROM ml_shadow_comparisons WHERE subject_id IN (SELECT id::text FROM identities WHERE display_name LIKE 'seed_person_%')",
        "UPDATE threat_assessments SET ml_prediction_id = NULL WHERE idempotency_key LIKE 'seed-assess-%'",
        "DELETE FROM ml_predictions WHERE subject_id IN (SELECT id::text FROM identities WHERE display_name LIKE 'seed_person_%')",
        "DELETE FROM ml_audit_log WHERE object_type = 'ml_label' AND object_id IN (SELECT id::text FROM ml_labels WHERE source LIKE 'seed-%')",
        "DELETE FROM ml_labels WHERE source LIKE 'seed-%'",
        "DELETE FROM threat_assessments WHERE idempotency_key LIKE 'seed-assess-%'",
        "DELETE FROM ml_drift_reports WHERE model_id IN (SELECT id FROM ml_models WHERE training_job_id LIKE 'seed-mlops-%')",
        "DELETE FROM ml_audit_log WHERE object_type = 'ml_model_threshold' AND object_id IN (SELECT id::text FROM ml_model_thresholds WHERE model_id IN (SELECT id FROM ml_models WHERE training_job_id LIKE 'seed-mlops-%'))",
        "DELETE FROM ml_audit_log WHERE object_type = 'ml_model' AND object_id IN (SELECT id::text FROM ml_models WHERE training_job_id LIKE 'seed-mlops-%')",
        "DELETE FROM ml_models WHERE training_job_id LIKE 'seed-mlops-%'",           # thresholds cascade
        "DELETE FROM ml_datasets WHERE build_job_id LIKE 'seed-mlops-%' OR name LIKE 'seed-%'",
        "DELETE FROM background_task_history WHERE job_id LIKE 'seed-mlops-%'",
        "DELETE FROM ml_feature_snapshots WHERE entity_id IN (SELECT id::text FROM identities WHERE display_name LIKE 'seed_person_%')",
        "DELETE FROM identity_appearances WHERE pipeline_id LIKE 'seed-pipeline-%'",
        "DELETE FROM identity_embeddings WHERE pipeline_id LIKE 'seed-pipeline-%'",
        "DELETE FROM identities WHERE display_name LIKE 'seed_person_%'",
        "DELETE FROM user_pipeline_access WHERE pipeline_id LIKE 'seed-pipeline-%'",
        "DELETE FROM pipelines WHERE pipeline_id LIKE 'seed-pipeline-%'",              # RESTRICT: children are gone
    ]
    paths = [r[0] for r in (await db.execute(text(
        "SELECT artifact_path FROM ml_models WHERE training_job_id LIKE 'seed-mlops-%' UNION ALL "
        "SELECT storage_path FROM ml_datasets WHERE build_job_id LIKE 'seed-mlops-%' OR name LIKE 'seed-%'"))).all()]
    for s in stmts:
        n = (await db.execute(text(s))).rowcount
        print(f"  {n:5d}  {s[:100]}")
    await db.commit()
    for p in paths:
        if p and os.path.exists(p):
            os.remove(p)
    _print_counts("row counts after removal", await _counts(db))


async def main(args):
    from db_connection import db_manager
    await db_manager.init_db()
    try:
        async with db_manager.get_session() as db:
            problems = await _guards(db)
            if problems:
                print("REFUSED:\n  - " + "\n  - ".join(problems))
                return 2
            if not (args.apply or args.remove):
                print(__doc__)
                _print_counts("current seed row counts (dry-run, nothing changed)", await _counts(db))
                return 0
            if not args.yes_i_understand:
                print("REFUSED: --apply/--remove need --yes-i-understand (this writes demo data)")
                return 2
            if args.remove:
                await _remove(db)
                return 0
            return await _apply(db, db_manager)
    finally:
        try:
            from backend.core.cache_manager import cache_manager
            await cache_manager.close()
        except Exception:
            pass
        await db_manager.close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deterministic ML-Ops demo seed (dev/demo only)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--yes-i-understand", action="store_true")
    parser.add_argument("--scale", type=int, default=1,
                        help="volume multiplier (1 = ~1k rows, 10 = ~10k+ rows)")
    parsed = parser.parse_args()
    if parsed.scale < 1 or parsed.scale > 50:
        parser.error("--scale must be between 1 and 50")
    SCALE = parsed.scale
    sys.exit(asyncio.run(main(parsed)))
