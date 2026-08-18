"""
Training jobs (first release: unsupervised behavioral anomaly only).

Algorithms: robust median/MAD baseline + IsolationForest (sklearn, seeded).
Supervised families (logreg / random_forest / gradient_boosting) are
scaffolded but GATED: below the reviewed-label minimums the job finishes
with the structured refusal — no fabricated training. XGBoost/Optuna/SHAP/
MLflow are optional-flag guarded and not consumed this release.

Every metric recorded is something that was truthfully measured on this
data: score distributions, band cutpoints (train quantiles), seed
stability, and a DESCRIPTIVE anomaly-band × rule-severity table (different
concepts, shown side by side, never differenced).

Output is ALWAYS a candidate (training -> validated at best). Shadow entry
is a separate, explicitly-approved administrator action.
"""

import asyncio
import json
import logging
import os
import threading
import time
import uuid as uuid_mod
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.ml.constants import ANOMALY_BANDS, FEATURE_SET_VERSION, MODEL_TYPE_BEHAVIOR_ANOMALY
from backend.ml.registry_service import (
    RegistryError, current_dependency_versions, registry_service,
    save_artifact, score_with_payload)
from config import settings

logger = logging.getLogger(__name__)

TRAINING_SEED = 42
MIN_TRAIN_ROWS = 20
MIN_USABLE_FEATURES = 5
FEATURE_COVERAGE_FLOOR = 0.7   # a feature must exist in >=70% of train rows

QUALITY_GATES = {
    "minimum_train_rows": MIN_TRAIN_ROWS,
    "minimum_usable_features": MIN_USABLE_FEATURES,
    "score_distribution_nondegenerate": True,   # std > 0 on train scores
    "reload_determinism": True,                  # saved artifact rescores identically
}

SUPERVISED_ALGORITHMS = ("logreg", "random_forest", "gradient_boosting")
UNSUPERVISED_ALGORITHMS = ("isolation_forest", "mad_baseline")

# ---- single-flight (in-process layer; routes add DistributedLock) ---------
_training_lock = threading.Lock()
_TRAINING_JOB = {"job_id": None, "started_at": None}
_TRAINING_JOB_MAX_AGE_SECONDS = 1800
_cancel_events: Dict[str, threading.Event] = {}


def try_acquire_training(job_id: str) -> Optional[str]:
    with _training_lock:
        current = _TRAINING_JOB["job_id"]
        started = _TRAINING_JOB["started_at"]
        if current and started and (time.time() - started) < _TRAINING_JOB_MAX_AGE_SECONDS:
            return current
        _TRAINING_JOB["job_id"] = job_id
        _TRAINING_JOB["started_at"] = time.time()
        _cancel_events[job_id] = threading.Event()
        return None


def release_training(job_id: str) -> None:
    with _training_lock:
        if _TRAINING_JOB["job_id"] == job_id:
            _TRAINING_JOB["job_id"] = None
            _TRAINING_JOB["started_at"] = None
        _cancel_events.pop(job_id, None)


def running_training_job() -> Optional[str]:
    with _training_lock:
        current = _TRAINING_JOB["job_id"]
        started = _TRAINING_JOB["started_at"]
        if current and started and (time.time() - started) < _TRAINING_JOB_MAX_AGE_SECONDS:
            return current
        return None


def request_cancel(job_id: str) -> bool:
    event = _cancel_events.get(job_id)
    if event is None:
        return False
    event.set()
    return True


def _cancelled(job_id: str) -> bool:
    event = _cancel_events.get(job_id)
    return bool(event and event.is_set())


# ---------------------------------------------------------------------------
# Matrix assembly (imputation policy lives IN the artifact)
# ---------------------------------------------------------------------------

def _assemble_matrix(rows: List[Dict[str, Any]]):
    """Choose covered features, compute train medians, impute. Returns
    (feature_names, medians, matrix builder)."""
    import numpy as np

    coverage: Dict[str, int] = {}
    for row in rows:
        for name in row["features"]:
            coverage[name] = coverage.get(name, 0) + 1
    usable = sorted(
        name for name, count in coverage.items()
        if count / max(1, len(rows)) >= FEATURE_COVERAGE_FLOOR)
    medians: Dict[str, float] = {}
    for name in usable:
        values = [row["features"][name] for row in rows if name in row["features"]]
        medians[name] = float(np.median(values)) if values else 0.0

    def matrix_of(part: List[Dict[str, Any]]):
        out = []
        for row in part:
            out.append([float(row["features"].get(name, medians[name])) for name in usable])
        return np.array(out) if out else np.empty((0, len(usable)))

    return usable, medians, matrix_of


def _fit_unsupervised(algorithm: str, matrix, seed: int):
    import numpy as np
    if algorithm == "isolation_forest":
        from sklearn.ensemble import IsolationForest
        model = IsolationForest(n_estimators=200, random_state=seed,
                                contamination="auto")
        model.fit(matrix)
        raw_train = -model.score_samples(matrix)
        return model, raw_train
    if algorithm == "mad_baseline":
        params = {}
        for i in range(matrix.shape[1]):
            column = matrix[:, i]
            median = float(np.median(column))
            mad = float(np.median(np.abs(column - median)))
            params[i] = {"median": median, "mad": mad}
        return params, None
    raise RegistryError("UNKNOWN_ALGORITHM", f"unsupported algorithm {algorithm!r}")


def _band_for(score: float, cutpoints: Dict[str, float]) -> str:
    if score >= cutpoints["highly_unusual"]:
        return "highly_unusual"
    if score >= cutpoints["unusual"]:
        return "unusual"
    if score >= cutpoints["elevated"]:
        return "elevated"
    return "normal"


async def _rule_severity_crosstab(db, rows_by_entity: Dict[str, str]) -> Dict[str, Any]:
    """DESCRIPTIVE ONLY: anomaly band distribution by current rule severity
    for entities that have a persisted assessment. Bands and severities are
    DIFFERENT CONCEPTS — this table never differences them."""
    from db_models import ThreatAssessmentRecord
    crosstab: Dict[str, Dict[str, int]] = {}
    rows = (await db.execute(
        select(ThreatAssessmentRecord.subject_id, ThreatAssessmentRecord.severity)
        .order_by(ThreatAssessmentRecord.created_at.desc()).limit(1000))).all()
    latest_severity = {}
    for subject_id, severity in rows:
        latest_severity.setdefault(subject_id, severity)
    for entity_id, band in rows_by_entity.items():
        severity = latest_severity.get(entity_id)
        if severity is None:
            continue
        crosstab.setdefault(severity, {b: 0 for b in ANOMALY_BANDS})
        crosstab[severity][band] += 1
    return {
        "note": ("descriptive comparison only — anomaly bands and rule threat "
                 "severity are different concepts and are never numerically "
                 "combined or differenced"),
        "band_distribution_by_rule_severity": crosstab,
        "entities_with_assessments": sum(len(v) and sum(v.values()) for v in crosstab.values()),
    }


# ---------------------------------------------------------------------------
# The training job
# ---------------------------------------------------------------------------

async def run_training_job(job_id: str, *, model_type: str = MODEL_TYPE_BEHAVIOR_ANOMALY,
                           algorithm: str = "isolation_forest",
                           requested_by: Optional[int] = None) -> None:
    from backend.core.task_history import task_history_manager
    from db_connection import db_manager

    started = time.monotonic()
    await task_history_manager.mark_running(job_id)

    async def stage(name: str, percent: int):
        logger.info("[ML_OPS] job_id=%s training_stage=%s progress_percent=%s",
                    job_id, name, percent)
        await task_history_manager.update_progress(job_id, percent, details={"stage": name})

    try:
        async with db_manager.get_session() as db:
            # ---- supervised gate: honest structured refusal ---------------
            if algorithm in SUPERVISED_ALGORITHMS:
                from backend.ml.labeling_service import labeling_service
                stats = await labeling_service.label_stats(db)
                if not stats["supervised_gate_open"]:
                    refusal = labeling_service.supervised_refusal(stats)
                    await task_history_manager.finish_job(
                        job_id, success=False,
                        error_code="INSUFFICIENT_REVIEWED_LABELS",
                        error_message=json.dumps(refusal)[:2000],
                        cancelled=False)
                    logger.info("[ML_OPS] supervised training refused job_id=%s: %s",
                                job_id, refusal)
                    return
                # Gate open (future state): still not implemented this release.
                await task_history_manager.finish_job(
                    job_id, success=False, error_code="SUPERVISED_NOT_ENABLED",
                    error_message="supervised training is scaffolded but not "
                                  "enabled in this release")
                return
            if algorithm not in UNSUPERVISED_ALGORITHMS:
                raise RegistryError("UNKNOWN_ALGORITHM", f"algorithm {algorithm!r}")

            # ---- dataset ---------------------------------------------------
            await stage("building_dataset", 15)
            from backend.ml.dataset_builder import build_dataset
            dataset = await build_dataset(
                db, name=f"{model_type}-train", kind="unsupervised",
                created_by=requested_by, build_job_id=job_id)
            if dataset["status"] != "built":
                await task_history_manager.finish_job(
                    job_id, success=False, error_code="DATASET_VALIDATION_FAILED",
                    error_message=json.dumps(dataset.get("quality_report", {}))[:2000])
                return
            if _cancelled(job_id):
                raise asyncio.CancelledError()

            # Load the parquet through its registered path
            from db_models import MLDataset
            dataset_row = (await db.execute(
                select(MLDataset).where(MLDataset.id == uuid_mod.UUID(dataset["dataset_id"]))
            )).scalar_one()
            import pyarrow.parquet as pq
            table = pq.read_table(dataset_row.storage_path)
            records = table.to_pylist()
            rows = [
                {"entity_id": r["entity_id"],
                 "as_of": datetime.fromisoformat(r["as_of"]),
                 "split": r["split"],
                 "features": json.loads(r["features_json"])}
                for r in records
            ]
            train = [r for r in rows if r["split"] == "train"]
            val = [r for r in rows if r["split"] == "val"]
            test = [r for r in rows if r["split"] == "test"]

            # ---- features + training --------------------------------------
            await stage("training", 45)
            feature_names, medians, matrix_of = _assemble_matrix(train)
            gates: Dict[str, Dict[str, Any]] = {
                "minimum_train_rows": {"required": MIN_TRAIN_ROWS,
                                       "actual": len(train),
                                       "passed": len(train) >= MIN_TRAIN_ROWS},
                "minimum_usable_features": {"required": MIN_USABLE_FEATURES,
                                            "actual": len(feature_names),
                                            "passed": len(feature_names) >= MIN_USABLE_FEATURES},
            }
            if not all(g["passed"] for g in gates.values()):
                await task_history_manager.finish_job(
                    job_id, success=False, error_code="QUALITY_GATES_FAILED",
                    error_message=json.dumps(gates)[:2000])
                return

            import numpy as np
            train_matrix = matrix_of(train)
            model_obj, raw_train = _fit_unsupervised(algorithm, train_matrix, TRAINING_SEED)
            if raw_train is None:  # mad_baseline scores via payload path
                keyed = {feature_names[i]: v for i, v in model_obj.items()}
                model_obj = keyed
                probe_payload = {
                    "algorithm": algorithm, "model": model_obj,
                    "feature_names": feature_names,
                    "normalization": {"min": 0.0, "max": 1.0},
                    "imputation_medians": medians,
                }
                raw_train = np.array([
                    v for v in _raw_scores(probe_payload, train_matrix)])

            norm = {"min": float(np.min(raw_train)), "max": float(np.max(raw_train))}
            gates["score_distribution_nondegenerate"] = {
                "required": "std > 0", "actual": float(np.std(raw_train)),
                "passed": float(np.std(raw_train)) > 0.0}

            payload = {
                "algorithm": algorithm,
                "model": model_obj,
                "feature_names": feature_names,
                "feature_set_version": FEATURE_SET_VERSION,
                "imputation_medians": medians,
                "normalization": norm,
                "band_cutpoints": {},   # filled below from train quantiles
                "dependency_versions": current_dependency_versions(),
                "metadata": {
                    "model_type": model_type,
                    "dataset_id": dataset["dataset_id"],
                    "dataset_checksum": dataset["checksum"],
                    "seed": TRAINING_SEED,
                    "trained_at": datetime.utcnow().isoformat() + "Z",
                    "job_id": job_id,
                },
                "saved_at": datetime.utcnow().isoformat() + "Z",
            }
            train_scores = score_with_payload(payload, train_matrix)
            cutpoints = {
                "elevated": float(np.quantile(train_scores, 0.90)),
                "unusual": float(np.quantile(train_scores, 0.97)),
                "highly_unusual": float(np.quantile(train_scores, 0.99)),
            }
            payload["band_cutpoints"] = cutpoints

            # ---- honest evaluation ----------------------------------------
            await stage("evaluating", 65)
            evaluation: Dict[str, Any] = {
                "score_type": "anomaly_score",
                "is_probability": False,
                "calibration_status": "not_applicable",
                "note": ("unsupervised behavioral anomaly model — no labels "
                         "exist, so no precision/recall/AUC is reported; only "
                         "distributional and stability measurements"),
                "splits": {},
                "band_cutpoints": cutpoints,
            }
            band_by_entity: Dict[str, str] = {}
            for split_name, part in (("train", train), ("val", val), ("test", test)):
                if not part:
                    evaluation["splits"][split_name] = {"rows": 0, "insufficient_data": True}
                    continue
                scores = score_with_payload(payload, matrix_of(part))
                bands = [_band_for(s, cutpoints) for s in scores]
                for row, band in zip(part, bands):
                    band_by_entity[row["entity_id"]] = band
                evaluation["splits"][split_name] = {
                    "rows": len(part),
                    "score_p50": float(np.quantile(scores, 0.5)),
                    "score_p90": float(np.quantile(scores, 0.9)),
                    "score_p99": float(np.quantile(scores, 0.99)),
                    "band_counts": {b: bands.count(b) for b in ANOMALY_BANDS},
                }
            # Seed stability (IsolationForest only): score correlation across seeds
            if algorithm == "isolation_forest" and len(train) >= MIN_TRAIN_ROWS:
                from sklearn.ensemble import IsolationForest
                alt = IsolationForest(n_estimators=200, random_state=TRAINING_SEED + 1,
                                      contamination="auto").fit(train_matrix)
                alt_scores = -alt.score_samples(train_matrix)
                corr = float(np.corrcoef(raw_train, alt_scores)[0, 1])
                evaluation["seed_stability_correlation"] = round(corr, 4)
            evaluation["rule_severity_crosstab"] = await _rule_severity_crosstab(
                db, band_by_entity)

            gates["reload_determinism"] = {"required": True, "actual": None, "passed": True}

            # ---- artifact + registration ----------------------------------
            await stage("saving_candidate", 85)
            version = await registry_service.next_version(db, model_type)
            artifact_dir = str(settings.ML_ARTIFACT_DIR)
            artifact_name = f"{model_type}-v{version}.pkl"
            artifact_path = os.path.join(artifact_dir, "candidates", artifact_name)
            artifact_hash = save_artifact(payload, artifact_path)

            # reload determinism: rescore through the saved artifact
            from backend.ml.registry_service import validate_artifact
            reloaded = validate_artifact(
                artifact_path, expected_hash=artifact_hash,
                expected_feature_names=feature_names,
                expected_dependencies=payload["dependency_versions"])
            rescored = score_with_payload(reloaded, train_matrix[:5])
            original = score_with_payload(payload, train_matrix[:5])
            deterministic = all(abs(a - b) < 1e-9 for a, b in zip(rescored, original))
            gates["reload_determinism"] = {"required": True, "actual": deterministic,
                                           "passed": deterministic}
            gates_passed = all(g["passed"] for g in gates.values())

            await stage("registering", 95)
            from db_models import MLModel
            model_row = MLModel(
                id=uuid_mod.uuid4(),
                model_type=model_type, version=version,
                stage="training",
                algorithm=algorithm,
                model_purpose="behavioral_anomaly_detection",
                score_type="anomaly_score",
                is_probability=False,
                calibration_status="not_applicable",
                artifact_name=artifact_name,
                artifact_path=artifact_path,
                artifact_hash=artifact_hash,
                artifact_size_bytes=os.path.getsize(artifact_path),
                dependency_versions=payload["dependency_versions"],
                feature_set_version=FEATURE_SET_VERSION,
                feature_names=feature_names,
                dataset_id=uuid_mod.UUID(dataset["dataset_id"]),
                training_job_id=job_id,
                seed=TRAINING_SEED,
                hyperparameters={"n_estimators": 200} if algorithm == "isolation_forest" else {},
                metrics={"evaluation": evaluation},
                quality_gates={"passed": gates_passed, "gates": gates},
                evaluation_report=evaluation,
                submitted_at=datetime.utcnow(),
                created_at=datetime.utcnow(),
                created_by=requested_by,
            )
            db.add(model_row)
            await db.flush()
            # ONE candidate threshold SET (the three band cutpoints) — the row a
            # prediction will name as its exact provenance once activated on
            # shadow entry. The artifact keeps band_cutpoints as training-time
            # record; activation refuses if the two ever disagree.
            from backend.ml.threshold_service import threshold_service
            await threshold_service.create_candidate(
                db, model_id=model_row.id,
                cutpoints={k: float(cutpoints[k]) for k in ("elevated", "unusual", "highly_unusual")},
                quantiles={"elevated": 0.90, "unusual": 0.97, "highly_unusual": 0.99},
                sample_count=len(train), source="training",
                expected_metrics={"train_quantiles": {"elevated": 0.90, "unusual": 0.97,
                                                      "highly_unusual": 0.99}})
            await db.commit()

            if gates_passed:
                await registry_service.transition(
                    db, str(model_row.id), to_stage="validated",
                    actor="training-job", reason=f"quality gates passed (job {job_id})")
            result = {
                "model_id": str(model_row.id),
                "model_type": model_type,
                "version": version,
                "algorithm": algorithm,
                "stage": "validated" if gates_passed else "training",
                "artifact_hash": artifact_hash,
                "dataset_id": dataset["dataset_id"],
                "dataset_checksum": dataset["checksum"],
                "quality_gates": {"passed": gates_passed, "gates": gates},
                "evaluation": evaluation,
                "awaiting_shadow_approval": gates_passed,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            await task_history_manager.finish_job(job_id, success=True, result=result)
            logger.info("[ML_OPS] training complete job_id=%s model=%s v%s gates=%s",
                        job_id, model_type, version, gates_passed)

    except asyncio.CancelledError:
        await task_history_manager.finish_job(
            job_id, success=False, error_code="CANCELLED",
            error_message="training cancelled", cancelled=True)
    except Exception as e:
        logger.error("[ML_OPS] training failed job_id=%s: %s", job_id, e, exc_info=True)
        await task_history_manager.finish_job(
            job_id, success=False, error_code="TRAINING_FAILED",
            error_message=str(e)[:500])
    finally:
        release_training(job_id)


def _raw_scores(payload: Dict[str, Any], matrix) -> List[float]:
    """Un-normalized scores for normalization-bound discovery (mad path)."""
    import numpy as np
    params = payload["model"]
    names = payload["feature_names"]
    out = []
    for row in np.asarray(matrix):
        zs = []
        for i, name in enumerate(names):
            mad = params[name]["mad"]
            if mad <= 0:
                continue
            zs.append(abs(row[i] - params[name]["median"]) / (1.4826 * mad))
        out.append(max(zs) if zs else 0.0)
    return out
