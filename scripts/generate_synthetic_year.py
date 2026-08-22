"""
Synthetic one-year behavioural corpus — to exercise the REAL ML pipeline.

    python scripts/generate_synthetic_year.py                       dry run: plan + current counts
    python scripts/generate_synthetic_year.py --apply --yes-i-understand [--days 365] [--identities 300]
            [--seed 20260822] [--export-parquet DIR] [--with-labels]
            [--build-dataset] [--train]
    python scripts/generate_synthetic_year.py --remove --yes-i-understand

What it does (deterministic, random.Random(seed)):
  1. registers 5 synthetic cameras through the live-ingest contract;
  2. creates identities with PERSONAS (regular daytime, night shift, weekend
     visitor, occasional, new arrival, churned) and writes ONE YEAR of camera
     appearances for them, row by row, into identity_appearances — the same
     table the collector reads;
  3. plants anomalies in ~8 % of identities inside the most recent 30 days
     (off-hours burst, new camera, frequency spike, location hop) and writes a
     GROUND-TRUTH file so a run can be checked against what was planted;
  4. optionally: exports the raw observations as Parquet; runs the real
     collector (full rebuild) so secintel-features-v2 snapshots exist; builds
     a dataset through the real builder (definition behavior_anomaly_person@v2,
     whole history) — the Parquet + manifest + hashes are then the SYSTEM's;
     trains a candidate through the real trainer and prints a synthetic
     sanity check (planted vs. not-planted score distribution on the test split).

Honesty rules
  * Refuses ENVIRONMENT=production. Refuses the development database unless
    --allow-development is given: the intended home is the isolated regression
    stack (docker/docker-compose.regression.yml).
  * Everything it writes is recognisable: cameras `synth-cam-*`, identities
    `synth_person_*`, datasets `synthetic-year-*`, jobs `synth-*`, label source
    `synthetic-manual`. --remove deletes exactly that and nothing else.
  * A synthetic corpus proves the PIPELINE works. It is not evidence about
    real behaviour: the scientific gate, the evidence report and the mapping
    stay INSUFFICIENT_EVIDENCE / REQUIRES_VALIDATION on purpose.
"""

import argparse
import asyncio
import json
import os
import random
import re
import sys
import uuid
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from backend.ml.constants import FEATURE_SET_VERSION  # noqa: E402

DEFAULT_SEED = 20260822
JOB_PREFIX = "synth-"
DATASET_NAME = "synthetic-year"
LABEL_SOURCE = "synthetic-manual"
ALLOWED_DB = re.compile(r"^face_recognition(_regression_.*|_migration_test_.*)?$")

CAMERAS = [
    ("synth-cam-01", "Synthetic Lobby", 33.8938, 35.5018),
    ("synth-cam-02", "Synthetic Parking", 33.8951, 35.5030),
    ("synth-cam-03", "Synthetic Loading Dock", 33.8929, 35.5005),
    ("synth-cam-04", "Synthetic Server Room", 33.8944, 35.5012),
    ("synth-cam-05", "Synthetic Back Gate", 33.8920, 35.5040),
]

PERSONAS = (
    # name, share, description
    ("regular_daytime", 0.45, "weekdays 08-18, 1-3 visits/day, 1-2 home cameras"),
    ("night_shift", 0.12, "weeknights 22-05, 1-2 visits/night"),
    ("weekend_visitor", 0.08, "weekends 10-16, most weekends"),
    ("occasional", 0.15, "every 1-4 weeks, daytime"),
    ("new_arrival", 0.10, "first seen in the last 45 days, then regular daytime"),
    ("churned", 0.10, "regular in the first half of the year, then gone"),
)
ANOMALY_KINDS = ("off_hours_burst", "new_camera", "frequency_spike", "location_hop")
ANOMALY_SHARE = 0.08


def _pick_persona(rng):
    r = rng.random()
    acc = 0.0
    for name, share, _ in PERSONAS:
        acc += share
        if r <= acc:
            return name
    return PERSONAS[0][0]


def _home_cameras(rng, persona):
    n = 2 if rng.random() < 0.4 else 1
    pool = [c[0] for c in CAMERAS[:3]] if persona != "night_shift" else [CAMERAS[2][0], CAMERAS[4][0]]
    return rng.sample(pool, min(n, len(pool)))


def _visits_for_day(rng, persona, day, start, end, first_day, churn_day):
    """Visit hours for one calendar day under a persona; [] when absent."""
    weekday = day.weekday()
    if day < first_day or (churn_day is not None and day > churn_day):
        return []
    if persona in ("regular_daytime", "new_arrival"):
        if weekday >= 5 or rng.random() < 0.12:            # weekends off, occasional absence
            return []
        return sorted(rng.sample(range(8, 18), rng.randint(1, 3)))
    if persona == "night_shift":
        if weekday >= 5 or rng.random() < 0.15:
            return []
        return sorted(rng.sample([22, 23, 0, 1, 2, 3, 4, 5], rng.randint(1, 2)))
    if persona == "weekend_visitor":
        if weekday < 5 or rng.random() < 0.3:
            return []
        return sorted(rng.sample(range(10, 16), rng.randint(1, 2)))
    if persona == "occasional":
        return [rng.randint(9, 17)] if rng.random() < (1 / (7 * rng.randint(1, 4))) else []
    if persona == "churned":
        if weekday >= 5 or rng.random() < 0.12:
            return []
        return sorted(rng.sample(range(8, 18), rng.randint(1, 2)))
    return []


def default_anchor() -> datetime:
    """Today 00:00 UTC — the corpus ends at a day boundary so the same seed
    reproduces the same corpus all day (not only within one hour)."""
    return datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


def generate(days: int, identities: int, seed: int, end: Optional[datetime] = None):
    """Pure generation — no database. Returns (identities, appearances, ground_truth).
    Deterministic in (days, identities, seed, end); `end` defaults to today 00:00 UTC."""
    rng = random.Random(seed)
    now = (end or default_anchor()).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=days)
    people, rows, truth = [], [], []
    planted_ids = set(rng.sample(range(identities), max(1, int(identities * ANOMALY_SHARE))))
    for i in range(identities):
        persona = _pick_persona(rng)
        homes = _home_cameras(rng, persona)
        first_day = start + timedelta(days=rng.randint(0, 20))
        if persona == "new_arrival":
            first_day = now - timedelta(days=rng.randint(10, 45))
        churn_day = (start + timedelta(days=days // 2 + rng.randint(-20, 20))) if persona == "churned" else None
        known = rng.random() < 0.6
        # a KNOWN identity promoted mid-year exercises the v2 point-in-time type
        promoted_at = (first_day + timedelta(days=rng.randint(5, max(6, days // 2)))) if (known and rng.random() < 0.5) else None
        person = {"index": i, "display_name": f"synth_person_{i:05d}", "persona": persona,
                  "type": "KNOWN" if known else "UNKNOWN", "home_cameras": homes,
                  "first_day": first_day, "promoted_at": promoted_at}
        day = first_day.replace(hour=0)
        while day <= now:
            for hour in _visits_for_day(rng, persona, day, start, now, first_day, churn_day):
                ts = day.replace(hour=hour % 24, minute=rng.randint(0, 59))
                if hour >= 22 and persona == "night_shift":
                    pass
                elif hour < 6 and persona == "night_shift":
                    ts = ts + timedelta(days=1)
                if ts > now:
                    continue
                rows.append({"index": i, "pipeline_id": rng.choice(homes), "start_time": ts,
                             "end_time": ts + timedelta(minutes=rng.randint(1, 25)), "planted": None})
            day += timedelta(days=1)
        if i in planted_ids and persona != "churned":
            kind = rng.choice(ANOMALY_KINDS)
            window_start = now - timedelta(days=rng.randint(3, 28))
            planted = []
            if kind == "off_hours_burst":
                for n in range(rng.randint(3, 6)):
                    ts = (window_start + timedelta(days=n)).replace(hour=rng.choice([1, 2, 3, 4]), minute=rng.randint(0, 59))
                    planted.append({"pipeline_id": rng.choice(homes), "start_time": ts})
            elif kind == "new_camera":
                foreign = rng.choice([c[0] for c in CAMERAS if c[0] not in homes])
                for n in range(rng.randint(2, 4)):
                    ts = (window_start + timedelta(days=n)).replace(hour=rng.randint(8, 18), minute=rng.randint(0, 59))
                    planted.append({"pipeline_id": foreign, "start_time": ts})
            elif kind == "frequency_spike":
                for n in range(rng.randint(12, 25)):
                    ts = window_start + timedelta(days=rng.randint(0, 2), hours=rng.randint(7, 19), minutes=rng.randint(0, 59))
                    planted.append({"pipeline_id": rng.choice(homes), "start_time": ts})
            else:  # location_hop: several cameras within minutes
                base = window_start.replace(hour=rng.randint(9, 16), minute=0)
                for n, cam in enumerate(rng.sample([c[0] for c in CAMERAS], 4)):
                    planted.append({"pipeline_id": cam, "start_time": base + timedelta(minutes=3 * n)})
            planted = [p for p in planted if p["start_time"] <= now]
            for p in planted:
                rows.append({"index": i, "pipeline_id": p["pipeline_id"], "start_time": p["start_time"],
                             "end_time": p["start_time"] + timedelta(minutes=rng.randint(1, 15)), "planted": kind})
            truth.append({"display_name": person["display_name"], "kind": kind,
                          "window_start": min(p["start_time"] for p in planted).isoformat() + "Z" if planted else None,
                          "planted_events": len(planted)})
        people.append(person)
    rows.sort(key=lambda r: r["start_time"])
    return people, rows, truth


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

async def _guards(db, allow_development: bool):
    problems = []
    from config import settings
    if str(settings.ENVIRONMENT).strip().lower() in ("production", "prod"):
        problems.append("ENVIRONMENT is production")
    name = (await db.execute(text("SELECT current_database()"))).scalar()
    if not ALLOWED_DB.match(name or ""):
        problems.append(f"connected database {name!r} is not the development database nor an isolated scratch")
    # The isolated regression stack carries the REGRESSION_ISOLATION_ID marker
    # (the same one scripts/regression_isolation_check.py asserts); without it
    # this is a development container and needs the explicit opt-in.
    if not os.environ.get("REGRESSION_ISOLATION_ID") and not allow_development:
        problems.append("no REGRESSION_ISOLATION_ID marker — this is not the isolated regression stack; pass "
                        "--allow-development to write synthetic data into the development database")
    return problems


async def _counts(db):
    q = {
        "cameras": "SELECT count(*) FROM pipelines WHERE pipeline_id LIKE 'synth-cam-%'",
        "identities": "SELECT count(*) FROM identities WHERE display_name LIKE 'synth_person_%'",
        "appearances": "SELECT count(*) FROM identity_appearances WHERE identity_id IN (SELECT id FROM identities WHERE display_name LIKE 'synth_person_%')",
        "snapshots": "SELECT count(*) FROM ml_feature_snapshots WHERE entity_id IN (SELECT id::text FROM identities WHERE display_name LIKE 'synth_person_%')",
        "labels": f"SELECT count(*) FROM ml_labels WHERE source = '{LABEL_SOURCE}'",
        "datasets": f"SELECT count(*) FROM ml_datasets WHERE name LIKE '{DATASET_NAME}%'",
        "models": f"SELECT count(*) FROM ml_models WHERE training_job_id LIKE '{JOB_PREFIX}%'",
    }
    return {k: (await db.execute(text(v))).scalar() for k, v in q.items()}


async def _write(db, people, rows):
    from backend.services.image_processing import ensure_pipeline_registered
    from db_models import Pipeline
    from sqlalchemy import select
    for pid, name, lat, lon in CAMERAS:
        await ensure_pipeline_registered(db, pid, location_name=name)
        row = (await db.execute(select(Pipeline).where(Pipeline.pipeline_id == pid))).scalar_one()
        row.latitude, row.longitude, row.location_name, row.is_active = lat, lon, name, 1
        row.updated_at = datetime.utcnow()
    await db.commit()
    ids = {}
    for p in people:
        first = min((r["start_time"] for r in rows if r["index"] == p["index"]), default=p["first_day"])
        last = max((r["start_time"] for r in rows if r["index"] == p["index"]), default=p["first_day"])
        ident = (await db.execute(text(
            "INSERT INTO identities (id, type, status, display_name, first_seen_at, last_seen_at, created_at, "
            " updated_at, appearances_count) VALUES (gen_random_uuid(), :t, 'ACTIVE', :n, :f, :l, :f, now(), 0) RETURNING id"),
            {"t": p["type"], "n": p["display_name"], "f": first, "l": last})).scalar()
        ids[p["index"]] = ident
        if p["promoted_at"] is not None:
            await db.execute(text(
                "INSERT INTO identity_audit_log (user_id, username, action_type, identity_id, action_details, success, created_at) "
                "VALUES (NULL, 'synthetic-generator', 'promote', :i, '{\"synthetic\": true}'::jsonb, true, :t)"),
                {"i": ident, "t": p["promoted_at"]})
    batch = []
    for r in rows:
        batch.append({"i": ids[r["index"]], "p": r["pipeline_id"], "s": r["start_time"], "e": r["end_time"]})
        if len(batch) >= 1000:
            await db.execute(text(
                "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, end_time, created_at) "
                "VALUES (:i, :p, :s, :e, now())"), batch)
            batch = []
    if batch:
        await db.execute(text(
            "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, end_time, created_at) "
            "VALUES (:i, :p, :s, :e, now())"), batch)
    await db.execute(text(
        "UPDATE identities SET appearances_count = sub.c FROM (SELECT identity_id, count(*) c FROM identity_appearances "
        "GROUP BY identity_id) sub WHERE identities.id = sub.identity_id AND identities.display_name LIKE 'synth_person_%'"))
    await db.commit()
    return ids


def export_parquet(out_dir, people, rows, truth, ids, params=None):
    import pyarrow as pa
    import pyarrow.parquet as pq
    os.makedirs(out_dir, exist_ok=True)
    by_index = {p["index"]: p for p in people}
    table = pa.Table.from_pylist([{
        "identity_id": str(ids.get(r["index"], "")), "display_name": by_index[r["index"]]["display_name"],
        "persona": by_index[r["index"]]["persona"], "identity_type": by_index[r["index"]]["type"],
        "pipeline_id": r["pipeline_id"], "start_time": r["start_time"].isoformat() + "Z",
        "end_time": r["end_time"].isoformat() + "Z", "planted_anomaly": r["planted"],
    } for r in rows])
    path = os.path.join(out_dir, "synthetic_year_observations.parquet")
    pq.write_table(table, path)
    with open(os.path.join(out_dir, "synthetic_year_ground_truth.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.utcnow().isoformat() + "Z", "planted": truth,
                   "reproduce": dict(params or {}, note="same seed + same end + same days/identities = identical corpus"),
                   "note": "synthetic corpus — proves the pipeline, not real behaviour"}, f, indent=2)
    return path


async def _collect_build_train(db, train: bool, seed: int, split_strategy: str):
    from backend.ml.collector import run_collection
    from backend.ml.dataset_builder import build_dataset
    from backend.ml.dataset_definitions import get_definition
    stats = await run_collection(db, run_id=f"{JOB_PREFIX}collect-{uuid.uuid4().hex[:6]}", full_rebuild=True)
    await db.commit()
    print(f"[4] collector (full rebuild, {FEATURE_SET_VERSION}): {stats}")
    definition = get_definition("behavior_anomaly_person", "v2")
    built = await build_dataset(db, name=DATASET_NAME, kind="unsupervised", definition=definition,
                                split_strategy=split_strategy,
                                build_job_id=f"{JOB_PREFIX}build-{uuid.uuid4().hex[:6]}")
    q = built.get("quality_report", {})
    pop = q.get("population", {})
    split = built.get("split", {}) or {}
    print(f"[5] dataset: {built['status']} {built.get('dataset_id')} rows={built.get('row_count')} "
          f"split={split.get('counts')} history_span_days={pop.get('history_span_days')} "
          f"median_appearances={(pop.get('appearances_per_entity') or {}).get('median')}")
    print(f"    split strategy={split.get('method')} dropped_for_group_integrity="
          f"{split.get('dropped_for_group_integrity')} entity_overlap={split.get('entity_overlap')}")
    maturity = q.get("maturity", {})
    print(f"    maturity: pipeline_usable={maturity.get('pipeline_technically_usable')} "
          f"population={maturity.get('behavioral_population_maturity')}")
    if built["status"] != "built" or not train:
        return built, None
    from backend.core.task_history import task_history_manager
    from backend.ml import trainer
    job_id = f"{JOB_PREFIX}train-{uuid.uuid4().hex[:6]}"
    assert trainer.try_acquire_training(job_id) is None, "a training job is already running"
    await task_history_manager.create_job(job_id=job_id, task_type="ml_training",
                                          task_name="synthetic-year training", description="synthetic")
    await trainer.run_training_job(job_id, dataset_id=built["dataset_id"], seed=seed)
    task = await task_history_manager.get_task_by_job_id(job_id)
    if task["status"] != "completed":
        print(f"[6] training FAILED: {task.get('error_code')} {task.get('error_message')}")
        return built, None
    res = task["result"]
    ev = res["evaluation"]
    print(f"[6] model v{res['version']} {res['stage']} engineering={res.get('engineering_gate')} "
          f"scientific={res.get('scientific_gate')} seed_stability={ev.get('seed_stability_correlation')}")
    return built, res


async def _sanity(db, built, model, truth):
    """Synthetic sanity check: do planted identities score higher than the rest
    on the untouched test split? Descriptive; says nothing about real data."""
    from backend.ml.registry_service import score_with_payload, validate_artifact, preprocess_feature_vector
    from backend.ml.trainer import _load_parquet_rows
    from db_models import MLDataset, MLModel
    from sqlalchemy import select
    mrow = (await db.execute(select(MLModel).where(MLModel.id == uuid.UUID(model["model_id"])))).scalar_one()
    drow = (await db.execute(select(MLDataset).where(MLDataset.id == uuid.UUID(built["dataset_id"])))).scalar_one()
    payload = validate_artifact(mrow.artifact_path, expected_hash=mrow.artifact_hash,
                                expected_feature_names=list(mrow.feature_names), expected_dependencies=mrow.dependency_versions)
    rows = [r for r in _load_parquet_rows(drow.storage_path) if r["split"] == "test"]
    if not rows:
        print("[7] sanity: no test rows"); return
    names = {r[0]: r[1] for r in (await db.execute(text(
        "SELECT id::text, display_name FROM identities WHERE display_name LIKE 'synth_person_%'"))).all()}
    planted = {t["display_name"] for t in truth}
    scores = score_with_payload(payload, [preprocess_feature_vector(payload, r["features"])[0] for r in rows])
    a = sorted(float(s) for r, s in zip(rows, scores) if names.get(r["entity_id"]) in planted)
    b = sorted(float(s) for r, s in zip(rows, scores) if names.get(r["entity_id"]) not in planted)
    med = lambda xs: xs[len(xs) // 2] if xs else None
    print(f"[7] synthetic sanity on the test split: planted n={len(a)} median={med(a)} | "
          f"not planted n={len(b)} median={med(b)} — descriptive only, synthetic data")


async def _remove(db):
    import os as _os
    stmts = [
        f"DELETE FROM ml_shadow_comparisons WHERE model_id IN (SELECT id FROM ml_models WHERE training_job_id LIKE '{JOB_PREFIX}%')",
        f"DELETE FROM ml_predictions WHERE model_id IN (SELECT id FROM ml_models WHERE training_job_id LIKE '{JOB_PREFIX}%')",
        "DELETE FROM ml_predictions WHERE subject_id IN (SELECT id::text FROM identities WHERE display_name LIKE 'synth_person_%')",
        f"DELETE FROM ml_labels WHERE source = '{LABEL_SOURCE}'",
        f"DELETE FROM ml_drift_reports WHERE model_id IN (SELECT id FROM ml_models WHERE training_job_id LIKE '{JOB_PREFIX}%')",
        f"DELETE FROM ml_audit_log WHERE object_id IN (SELECT id::text FROM ml_models WHERE training_job_id LIKE '{JOB_PREFIX}%')",
    ]
    for s in stmts:
        await db.execute(text(s))
    for path in (await db.execute(text(f"SELECT artifact_path FROM ml_models WHERE training_job_id LIKE '{JOB_PREFIX}%'"))).scalars():
        if path and _os.path.exists(path):
            _os.remove(path)
    await db.execute(text(f"DELETE FROM ml_models WHERE training_job_id LIKE '{JOB_PREFIX}%'"))
    for storage, manifest in (await db.execute(text(
            f"SELECT storage_path, manifest_path FROM ml_datasets WHERE name LIKE '{DATASET_NAME}%'"))).all():
        for path in (storage, manifest):
            if path and _os.path.exists(path):
                _os.remove(path)
    for s in (
        f"DELETE FROM ml_datasets WHERE name LIKE '{DATASET_NAME}%'",
        f"DELETE FROM background_task_history WHERE job_id LIKE '{JOB_PREFIX}%'",
        "DELETE FROM threat_assessments WHERE person_id IN (SELECT id FROM identities WHERE display_name LIKE 'synth_person_%')",
        "DELETE FROM ml_feature_snapshots WHERE entity_id IN (SELECT id::text FROM identities WHERE display_name LIKE 'synth_person_%')",
        "DELETE FROM identity_audit_log WHERE username = 'synthetic-generator'",
        "DELETE FROM identity_appearances WHERE identity_id IN (SELECT id FROM identities WHERE display_name LIKE 'synth_person_%')",
        "DELETE FROM identities WHERE display_name LIKE 'synth_person_%'",
        "DELETE FROM pipelines WHERE pipeline_id LIKE 'synth-cam-%'",
    ):
        await db.execute(text(s))
    await db.commit()


async def main(args):
    from db_connection import db_manager
    await db_manager.init_db()
    async with db_manager.get_session() as db:
        problems = await _guards(db, args.allow_development)
        if problems:
            print("REFUSED:", "; ".join(problems)); return 2
        existing = await _counts(db)
        if args.remove:
            if not args.yes_i_understand:
                print("REFUSED: --remove needs --yes-i-understand"); return 2
            await _remove(db)
            print("removed:", await _counts(db)); return 0
        anchor = datetime.fromisoformat(args.end) if args.end else default_anchor()
        if not args.apply:
            people, rows, truth = generate(args.days, args.identities, args.seed, end=anchor)
            print(f"DRY RUN — would write {len(people)} identities, {len(rows)} appearances over {args.days} days, "
                  f"{len(truth)} planted anomalies; current synthetic rows: {existing}")
            return 0
        if not args.yes_i_understand:
            print("REFUSED: --apply needs --yes-i-understand"); return 2
        if existing["identities"]:
            print(f"REFUSED: synthetic rows already exist ({existing}) — run --remove --yes-i-understand first"); return 2
        people, rows, truth = generate(args.days, args.identities, args.seed, end=anchor)
        print(f"[1] generated {len(people)} identities, {len(rows)} appearances over {args.days} days, "
              f"{len(truth)} planted anomalies (seed {args.seed}, end {anchor.isoformat()}Z)")
        ids = await _write(db, people, rows)
        print(f"[2] written: {await _counts(db)}")
        out_dir = args.export_parquet or os.path.join("logs", "audit", "synthetic")
        path = export_parquet(out_dir, people, rows, truth, ids,
                              params={"seed": args.seed, "end": anchor.isoformat() + "Z",
                                      "days": args.days, "identities": args.identities})
        print(f"[3] raw observations exported: {path} (+ synthetic_year_ground_truth.json)")
        if args.with_labels:
            from backend.ml.labeling_service import labeling_service  # noqa: F401
            print("[3b] --with-labels: labels need RESOLVED assessments; create them through the UI/API so the "
                  "selection metadata and review flow are exercised (not fabricated here)")
        built, model = (None, None)
        if args.build_dataset or args.train:
            built, model = await _collect_build_train(db, args.train, args.seed, args.split_strategy)
            if model is not None:
                await _sanity(db, built, model, truth)
        print("[done] counts:", await _counts(db))
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--yes-i-understand", action="store_true")
    parser.add_argument("--allow-development", action="store_true")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--identities", type=int, default=300)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--end", metavar="ISO_UTC", default=None,
                        help="corpus end instant (UTC, hour precision); default today 00:00 UTC. "
                             "Same seed + same end = identical corpus")
    parser.add_argument("--export-parquet", metavar="DIR")
    parser.add_argument("--with-labels", action="store_true")
    parser.add_argument("--build-dataset", action="store_true")
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--split-strategy", choices=["temporal_group", "temporal"], default="temporal",
                        help="temporal_group (entity isolation; a year of regulars leaves ~no val/test rows) "
                             "or temporal (entities recur across splits, overlap measured) — default temporal")
    sys.exit(asyncio.run(main(parser.parse_args())))
