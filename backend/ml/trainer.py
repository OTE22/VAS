"""
Governed training jobs for behavioral, relational and ranking models.

Algorithms: robust median/MAD baseline + IsolationForest (sklearn, seeded).
Supervised threat ranking (logreg / random_forest / gradient_boosting) is
GATED: below the reviewed-label minimums the job finishes with a structured
refusal — no fabricated training. Its output is a relative review-rank score,
not a calibrated probability. XGBoost/Optuna/SHAP/MLflow are optional-flag
guarded and not consumed this release.

Every metric recorded is something truthfully measured on the selected data:
anomaly score distributions/cutpoints/stability, or ranking ROC-AUC and
average precision only when a split contains both classes.

Output is ALWAYS a candidate (training -> validated at best). Shadow entry
is a separate, explicitly-approved administrator action.

Datasets are reusable: a job may train from an EXISTING built dataset
(`dataset_id`) — after proving both the logical row fingerprint and the
Parquet file hash still match the registry — so one immutable dataset can
back many experiments. Every model records the complete `training_config`
it was produced with (algorithm, seed, every hyperparameter, dataset
lineage) and the trainer's code revision.
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
from backend.ml.model_specs import get_model_spec
from backend.ml.dataset_definitions import feature_set_limitations
from backend.ml.registry_service import (
    RegistryError, current_dependency_versions, preprocess_feature_vector,
    registry_service, save_artifact, score_with_payload)
from backend.ml.dataset_builder import _code_version
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

# Complete default hyperparameters per algorithm — persisted verbatim on the
# model row so a run is reproducible from its registry record alone.
DEFAULT_HYPERPARAMETERS: Dict[str, Dict[str, Any]] = {
    "isolation_forest": {"n_estimators": 200, "contamination": "auto", "max_samples": "auto"},
    # mad_baseline has no tunable knob: the 1.4826 MAD-to-sigma constant is
    # part of the scoring contract (registry_service.score_with_payload), not
    # a hyperparameter — recording it as one would claim a knob that does nothing.
    "mad_baseline": {},
    "logreg": {"C": 1.0, "max_iter": 1000, "class_weight": "balanced"},
    "random_forest": {"n_estimators": 300, "max_depth": None,
                      "min_samples_leaf": 2, "class_weight": "balanced"},
    "gradient_boosting": {"n_estimators": 200, "learning_rate": 0.05,
                          "max_depth": 3},
}
BAND_QUANTILES = {"elevated": 0.90, "unusual": 0.97, "highly_unusual": 0.99}


class DatasetRefusal(Exception):
    """A requested dataset cannot be trained from; `code` is stable."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_hyperparameters(algorithm: str, overrides: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = dict(DEFAULT_HYPERPARAMETERS.get(algorithm, {}))
    for key, value in (overrides or {}).items():
        if key not in base:
            raise RegistryError("UNKNOWN_HYPERPARAMETER",
                                f"{key!r} is not a hyperparameter of {algorithm}")
        base[key] = value
    return base

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

    contract = {"feature_names": usable, "imputation_medians": medians}

    def matrix_of(part: List[Dict[str, Any]]):
        # The SAME preprocessing rule inference applies (registry_service.
        # preprocess_feature_vector): present -> value, missing -> train median.
        out = [preprocess_feature_vector(contract, row["features"])[0] for row in part]
        return np.array(out) if out else np.empty((0, len(usable)))

    return usable, medians, matrix_of


def _fit_unsupervised(algorithm: str, matrix, seed: int,
                      hyperparameters: Optional[Dict[str, Any]] = None):
    import numpy as np
    hp = resolve_hyperparameters(algorithm, hyperparameters)
    if algorithm == "isolation_forest":
        from sklearn.ensemble import IsolationForest
        model = IsolationForest(n_estimators=int(hp["n_estimators"]), random_state=seed,
                                contamination=hp["contamination"],
                                max_samples=hp["max_samples"])
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


def _fit_supervised(algorithm: str, matrix, labels, seed: int,
                    hyperparameters: Optional[Dict[str, Any]] = None):
    """Fit an explicitly supported binary ranker.

    The returned score is used for ordering analyst work.  It is not exposed
    as a calibrated probability and never maps directly to threat severity.
    """
    hp = resolve_hyperparameters(algorithm, hyperparameters)
    if algorithm == "logreg":
        from sklearn.linear_model import LogisticRegression
        model = LogisticRegression(C=float(hp["C"]), max_iter=int(hp["max_iter"]),
                                   class_weight=hp["class_weight"],
                                   random_state=seed)
    elif algorithm == "random_forest":
        from sklearn.ensemble import RandomForestClassifier
        model = RandomForestClassifier(
            n_estimators=int(hp["n_estimators"]), max_depth=hp["max_depth"],
            min_samples_leaf=int(hp["min_samples_leaf"]),
            class_weight=hp["class_weight"], random_state=seed, n_jobs=1)
    elif algorithm == "gradient_boosting":
        from sklearn.ensemble import GradientBoostingClassifier
        model = GradientBoostingClassifier(
            n_estimators=int(hp["n_estimators"]), learning_rate=float(hp["learning_rate"]),
            max_depth=int(hp["max_depth"]), random_state=seed)
    else:
        raise RegistryError("UNKNOWN_ALGORITHM", f"unsupported supervised algorithm {algorithm!r}")
    model.fit(matrix, labels)
    return model, hp


def _binary_ranking_metrics(labels, scores) -> Dict[str, Any]:
    """Only report metrics that the split can actually support."""
    import numpy as np
    from sklearn.metrics import average_precision_score, roc_auc_score
    y = np.asarray(labels, dtype=int)
    s = np.asarray(scores, dtype=float)
    out: Dict[str, Any] = {"rows": int(len(y)), "positive": int(y.sum()),
                           "negative": int(len(y) - y.sum())}
    if len(set(y.tolist())) < 2:
        out["insufficient_classes"] = True
        return out
    out["roc_auc"] = float(roc_auc_score(y, s))
    out["average_precision"] = float(average_precision_score(y, s))
    out["score_p50"] = float(np.quantile(s, 0.5))
    out["score_p90"] = float(np.quantile(s, 0.9))
    return out


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
# Dataset loading — the persisted artifact is the source of truth
# ---------------------------------------------------------------------------

def _load_parquet_rows(storage_path: str) -> List[Dict[str, Any]]:
    import pyarrow.parquet as pq
    table = pq.read_table(storage_path)
    return [
        {"entity_id": r["entity_id"],
         "as_of": datetime.fromisoformat(r["as_of"]),
         "split": r["split"],
         "features": json.loads(r["features_json"]),
         "label": r.get("label")}
        for r in table.to_pylist()
    ]


async def load_training_dataset(db, dataset_id: str, *, expected_kind: str,
                                expected_feature_set_version: Optional[str] = None):
    """Load an EXISTING dataset for training after proving it is the dataset
    the registry describes: status built, right kind and feature contract,
    file present, Parquet bytes hash equal to `parquet_sha256` AND the
    canonical-row fingerprint recomputed from the reloaded rows equal to
    `checksum`. Any mismatch is a refusal with a stable code — a dataset
    that cannot be proven intact is never trained from."""
    from db_models import MLDataset
    from backend.ml.dataset_builder import _sha256_file, dataset_fingerprint
    try:
        row_uuid = uuid_mod.UUID(str(dataset_id))
    except (ValueError, TypeError):
        raise DatasetRefusal("DATASET_NOT_FOUND", f"{dataset_id!r} is not a dataset id")
    row = (await db.execute(select(MLDataset).where(MLDataset.id == row_uuid))).scalar_one_or_none()
    if row is None:
        raise DatasetRefusal("DATASET_NOT_FOUND", f"dataset {dataset_id} does not exist")
    if row.status != "built":
        raise DatasetRefusal("DATASET_NOT_BUILT", f"dataset {row.name} v{row.version} is {row.status}")
    if row.kind != expected_kind:
        raise DatasetRefusal("DATASET_KIND_MISMATCH",
                             f"dataset is {row.kind}, training needs {expected_kind}")
    if expected_feature_set_version and row.feature_set_version != expected_feature_set_version:
        raise DatasetRefusal("DATASET_FEATURE_SET_MISMATCH",
                             f"dataset feature set {row.feature_set_version} != "
                             f"{expected_feature_set_version}")
    if not row.storage_path or not os.path.exists(row.storage_path):
        raise DatasetRefusal("DATASET_FILE_MISSING",
                             f"the Parquet file of dataset {row.name} v{row.version} is absent")
    stored_file_hash = getattr(row, "parquet_sha256", None)
    if not stored_file_hash:
        raise DatasetRefusal("DATASET_INTEGRITY_UNVERIFIABLE",
                             "dataset predates Parquet file hashing; rebuild it to train from it")
    actual_file_hash = _sha256_file(row.storage_path)
    if actual_file_hash != stored_file_hash:
        raise DatasetRefusal("DATASET_FILE_HASH_MISMATCH",
                             "Parquet bytes differ from the registry hash — the file was "
                             "replaced or corrupted; refusing to train from it")
    rows = _load_parquet_rows(row.storage_path)
    recomputed = dataset_fingerprint([
        {"entity_id": r["entity_id"], "as_of": r["as_of"],
         "features": r["features"], "label": r.get("label")} for r in rows]) if rows else "empty"
    if recomputed != row.checksum:
        raise DatasetRefusal("DATASET_CHECKSUM_MISMATCH",
                             "canonical-row fingerprint of the reloaded rows differs from the "
                             "registry checksum")
    return row, rows


async def _train_supervised_ranking(
        db, job_id: str, *, model_type: str, algorithm: str,
        requested_by: Optional[int], dataset_id: Optional[str], seed: int,
        hyperparameters: Optional[Dict[str, Any]], sampling_policy: Optional[str],
        stage) -> Dict[str, Any]:
    """Train and register a review-queue ranker from reviewed labels only."""
    import numpy as np
    from backend.ml.labeling_service import labeling_service

    spec = get_model_spec(model_type)
    stats = await labeling_service.label_stats(db)
    if not stats["supervised_gate_open"]:
        refusal = labeling_service.supervised_refusal(stats)
        return {"failure": {"code": "INSUFFICIENT_REVIEWED_LABELS",
                            "message": json.dumps(refusal)[:2000]}}
    try:
        resolved_hp = resolve_hyperparameters(algorithm, hyperparameters)
    except RegistryError as exc:
        return {"failure": {"code": exc.code, "message": exc.message}}

    if dataset_id:
        await stage("loading_dataset", 15)
        try:
            dataset_row, rows = await load_training_dataset(
                db, dataset_id, expected_kind="supervised",
                expected_feature_set_version=spec.feature_set_version)
        except DatasetRefusal as refusal:
            return {"failure": {"code": refusal.code, "message": refusal.message}}
        dataset = {"dataset_id": str(dataset_row.id), "checksum": dataset_row.checksum,
                   "reused": True}
    else:
        await stage("building_dataset", 15)
        from backend.ml.dataset_builder import build_dataset
        from backend.ml.dataset_definitions import get_definition
        dataset = await build_dataset(
            db, name=f"{model_type}-train", kind="supervised",
            created_by=requested_by, build_job_id=job_id,
            definition=get_definition(spec.dataset_definition),
            sampling_policy=sampling_policy)
        if dataset["status"] != "built":
            return {"failure": {
                "code": dataset.get("refusal") or "DATASET_VALIDATION_FAILED",
                "message": json.dumps(dataset.get("quality_report", {}))[:2000]}}
        dataset["reused"] = False
        from db_models import MLDataset
        dataset_row = (await db.execute(select(MLDataset).where(
            MLDataset.id == uuid_mod.UUID(dataset["dataset_id"])))).scalar_one()
        rows = _load_parquet_rows(dataset_row.storage_path)

    train = [row for row in rows if row["split"] == "train"]
    val = [row for row in rows if row["split"] == "val"]
    test = [row for row in rows if row["split"] == "test"]
    await stage("training", 45)
    feature_names, medians, matrix_of = _assemble_matrix(train)
    y_train = np.asarray([1 if row.get("label") == "positive" else 0 for row in train])
    gates: Dict[str, Dict[str, Any]] = {
        "minimum_train_rows": {"required": MIN_TRAIN_ROWS, "actual": len(train),
                               "passed": len(train) >= MIN_TRAIN_ROWS},
        "minimum_usable_features": {"required": MIN_USABLE_FEATURES,
                                    "actual": len(feature_names),
                                    "passed": len(feature_names) >= MIN_USABLE_FEATURES},
        "both_classes_in_train": {"required": ["negative", "positive"],
                                  "actual": sorted(set(y_train.tolist())),
                                  "passed": len(set(y_train.tolist())) == 2},
    }
    if not all(g["passed"] for g in gates.values()):
        return {"failure": {"code": "QUALITY_GATES_FAILED",
                            "message": json.dumps(gates)[:2000]}}
    model_obj, resolved_hp = _fit_supervised(
        algorithm, matrix_of(train), y_train, seed, resolved_hp)
    train_scores = model_obj.predict_proba(matrix_of(train))[:, 1]
    gates["score_distribution_nondegenerate"] = {
        "required": "std > 0", "actual": float(np.std(train_scores)),
        "passed": float(np.std(train_scores)) > 0.0}
    payload = {
        "algorithm": algorithm, "model": model_obj,
        "feature_names": feature_names,
        "feature_set_version": dataset_row.feature_set_version,
        "imputation_medians": medians,
        "normalization": {"min": 0.0, "max": 1.0},
        "band_cutpoints": {},
        "dependency_versions": current_dependency_versions(),
        "metadata": {"model_type": model_type, "model_purpose": spec.model_purpose,
                     "score_type": spec.score_type, "dataset_id": dataset["dataset_id"],
                     "dataset_checksum": dataset["checksum"], "seed": seed,
                     "hyperparameters": resolved_hp,
                     "trained_at": datetime.utcnow().isoformat() + "Z", "job_id": job_id},
        "saved_at": datetime.utcnow().isoformat() + "Z",
    }
    await stage("evaluating", 65)
    evaluation = {
        "score_type": spec.score_type, "is_probability": False,
        "calibration_status": spec.calibration_status,
        "note": ("supervised relative ranking of analyst review priority; classifier output is "
                 "not a calibrated probability of threat and never replaces rules"),
        "splits": {},
    }
    for split_name, part in (("train", train), ("val", val), ("test", test)):
        if not part:
            evaluation["splits"][split_name] = {"rows": 0, "insufficient_data": True}
            continue
        y = [1 if row.get("label") == "positive" else 0 for row in part]
        evaluation["splits"][split_name] = _binary_ranking_metrics(
            y, score_with_payload(payload, matrix_of(part)))

    await stage("saving_candidate", 85)
    version = await registry_service.next_version(db, model_type)
    artifact_name = f"{model_type}-v{version}.pkl"
    artifact_path = os.path.join(str(settings.ML_ARTIFACT_DIR), "candidates", artifact_name)
    artifact_hash = save_artifact(payload, artifact_path)
    from backend.ml.registry_service import validate_artifact
    reloaded = validate_artifact(
        artifact_path, expected_hash=artifact_hash,
        expected_feature_names=feature_names,
        expected_dependencies=payload["dependency_versions"])
    before = score_with_payload(payload, matrix_of(train)[:5])
    after = score_with_payload(reloaded, matrix_of(train)[:5])
    deterministic = all(abs(a - b) < 1e-9 for a, b in zip(before, after))
    gates["reload_determinism"] = {"required": True, "actual": deterministic,
                                   "passed": deterministic}
    gates_passed = all(g["passed"] for g in gates.values())

    await stage("registering", 95)
    from db_models import MLModel
    from backend.ml.readiness import PREPROCESSOR_VERSION, feature_schema_hash
    model_row = MLModel(
        id=uuid_mod.uuid4(), model_type=model_type, version=version, stage="training",
        algorithm=algorithm, model_purpose=spec.model_purpose,
        score_type=spec.score_type, is_probability=False,
        calibration_status=spec.calibration_status,
        artifact_name=artifact_name, artifact_path=artifact_path,
        artifact_hash=artifact_hash, artifact_size_bytes=os.path.getsize(artifact_path),
        dependency_versions=payload["dependency_versions"],
        feature_set_version=dataset_row.feature_set_version,
        feature_names=feature_names, dataset_id=uuid_mod.UUID(dataset["dataset_id"]),
        training_job_id=job_id, seed=seed, hyperparameters=resolved_hp,
        training_config={"algorithm": algorithm, "seed": seed,
                         "hyperparameters": resolved_hp,
                         "feature_schema_hash": feature_schema_hash(feature_names),
                         "preprocessor_version": PREPROCESSOR_VERSION,
                         "rows": {"train": len(train), "val": len(val), "test": len(test)},
                         "dataset_id": dataset["dataset_id"],
                         "dataset_checksum": dataset["checksum"],
                         "dataset_reused": dataset["reused"],
                         "serving_mode": spec.serving_mode,
                         "engineering_gate": "PASS" if gates_passed else "FAIL",
                         "scientific_gate": "REQUIRES_CALIBRATION"},
        code_version=_code_version(), metrics={"evaluation": evaluation},
        quality_gates={"passed": gates_passed, "gates": gates},
        evaluation_report=evaluation, submitted_at=datetime.utcnow(),
        created_at=datetime.utcnow(), created_by=requested_by)
    db.add(model_row)
    await db.commit()
    if gates_passed:
        await registry_service.transition(
            db, str(model_row.id), to_stage="validated", actor="training-job",
            reason=f"ranking engineering gates passed (job {job_id})")
    return {"model_id": str(model_row.id), "model_type": model_type,
            "version": version, "algorithm": algorithm,
            "stage": "validated" if gates_passed else "training",
            "artifact_hash": artifact_hash, "dataset_id": dataset["dataset_id"],
            "dataset_reused": dataset["reused"], "quality_gates": model_row.quality_gates,
            "evaluation": evaluation, "serving_mode": spec.serving_mode,
            "awaiting_shadow_approval": False}


# ---------------------------------------------------------------------------
# The training job
# ---------------------------------------------------------------------------

async def run_training_job(job_id: str, *, model_type: str = MODEL_TYPE_BEHAVIOR_ANOMALY,
                           algorithm: str = "isolation_forest",
                           requested_by: Optional[int] = None,
                           dataset_id: Optional[str] = None,
                           seed: Optional[int] = None,
                           hyperparameters: Optional[Dict[str, Any]] = None,
                           sampling_policy: Optional[str] = None) -> None:
    """dataset_id: train from that EXISTING built dataset (verified) instead
    of building a new one. seed / hyperparameters: experiment knobs, defaults
    are the release defaults; whatever was used is persisted verbatim."""
    from backend.core.task_history import task_history_manager
    from db_connection import db_manager

    started = time.monotonic()
    seed = TRAINING_SEED if seed is None else int(seed)
    await task_history_manager.mark_running(job_id)
    # Durable lineage: background_task_history is transient (30-day
    # retention); the request itself - who, when, what - must survive in
    # ml_audit_log regardless of the entry point (API or CLI) and outcome.
    await _audit_training_event("training_started", job_id, requested_by, {
        "model_type": model_type, "algorithm": algorithm, "dataset_id": dataset_id,
        "seed": seed, "hyperparameters": hyperparameters, "sampling_policy": sampling_policy})

    # Defense in depth: the HTTP boundary refuses reserved model types, but
    # it is not the only caller. A reserved type must never be trained here
    # either — an IsolationForest registered as threat_ranking_model would
    # not even be shadow-capped (its name lacks "anomaly").
    from backend.ml.constants import IMPLEMENTED_MODEL_TYPES
    if model_type not in IMPLEMENTED_MODEL_TYPES:
        await task_history_manager.finish_job(
            job_id, success=False, error_code="MODEL_TYPE_NOT_IMPLEMENTED",
            error_message=f"{model_type} is a reserved interface; only "
                          f"{', '.join(IMPLEMENTED_MODEL_TYPES)} trains in this release")
        release_training(job_id)
        await _observe_training_outcome_async(job_id)
        return
    spec = get_model_spec(model_type)
    if algorithm not in spec.algorithms:
        await task_history_manager.finish_job(
            job_id, success=False, error_code="ALGORITHM_NOT_SUPPORTED_FOR_MODEL",
            error_message=(f"{algorithm!r} is not valid for {model_type}; "
                           f"choose one of {', '.join(spec.algorithms)}"))
        release_training(job_id)
        await _observe_training_outcome_async(job_id)
        return

    async def stage(name: str, percent: int):
        logger.info("[ML_OPS] job_id=%s training_stage=%s progress_percent=%s",
                    job_id, name, percent)
        await task_history_manager.update_progress(job_id, percent, details={"stage": name})

    try:
        async with db_manager.get_session() as db:
            if spec.dataset_kind == "supervised":
                outcome = await _train_supervised_ranking(
                    db, job_id, model_type=model_type, algorithm=algorithm,
                    requested_by=requested_by, dataset_id=dataset_id, seed=seed,
                    hyperparameters=hyperparameters, sampling_policy=sampling_policy,
                    stage=stage)
                failure = outcome.get("failure")
                if failure:
                    await task_history_manager.finish_job(
                        job_id, success=False, error_code=failure["code"],
                        error_message=failure["message"][:2000])
                else:
                    outcome["duration_ms"] = int((time.monotonic() - started) * 1000)
                    await task_history_manager.finish_job(job_id, success=True, result=outcome)
                return
            if algorithm not in UNSUPERVISED_ALGORITHMS:
                raise RegistryError("UNKNOWN_ALGORITHM", f"algorithm {algorithm!r}")

            # ---- dataset ---------------------------------------------------
            try:
                resolved_hp = resolve_hyperparameters(algorithm, hyperparameters)
            except RegistryError as e:
                await task_history_manager.finish_job(
                    job_id, success=False, error_code=e.code, error_message=e.message)
                return
            if dataset_id:
                await stage("loading_dataset", 15)
                try:
                    # Any built dataset trains; the model is stamped with the
                    # dataset's feature set (a v1 dataset reproduces a v1
                    # experiment — never relabelled as the current set).
                    dataset_row, rows = await load_training_dataset(
                        db, dataset_id, expected_kind="unsupervised",
                        expected_feature_set_version=(None if model_type == MODEL_TYPE_BEHAVIOR_ANOMALY
                                                      else spec.feature_set_version))
                except DatasetRefusal as refusal:
                    await task_history_manager.finish_job(
                        job_id, success=False, error_code=refusal.code,
                        error_message=refusal.message[:2000])
                    logger.warning("[ML_OPS] training refused job_id=%s dataset=%s: %s",
                                   job_id, dataset_id, refusal.code)
                    return
                dataset = {"dataset_id": str(dataset_row.id), "checksum": dataset_row.checksum,
                           "reused": True}
            else:
                await stage("building_dataset", 15)
                from backend.ml.dataset_builder import build_dataset
                from backend.ml.dataset_definitions import get_definition
                dataset = await build_dataset(
                    db, name=f"{model_type}-train", kind="unsupervised",
                    created_by=requested_by, build_job_id=job_id,
                    definition=get_definition(spec.dataset_definition),
                    sampling_policy=sampling_policy)
                if dataset["status"] != "built":
                    await task_history_manager.finish_job(
                        job_id, success=False,
                        error_code=dataset.get("refusal") or "DATASET_VALIDATION_FAILED",
                        error_message=json.dumps(dataset.get("quality_report", {}))[:2000])
                    return
                dataset["reused"] = False
                # Load the parquet through its registered path
                from db_models import MLDataset
                dataset_row = (await db.execute(
                    select(MLDataset).where(MLDataset.id == uuid_mod.UUID(dataset["dataset_id"]))
                )).scalar_one()
                rows = _load_parquet_rows(dataset_row.storage_path)
            if _cancelled(job_id):
                raise asyncio.CancelledError()
            feature_set_version = dataset_row.feature_set_version
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
            model_obj, raw_train = _fit_unsupervised(algorithm, train_matrix, seed, resolved_hp)
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
                "feature_set_version": feature_set_version,
                "imputation_medians": medians,
                "normalization": norm,
                "band_cutpoints": {},   # filled below from train quantiles
                "dependency_versions": current_dependency_versions(),
                "metadata": {
                    "model_type": model_type,
                    "dataset_id": dataset["dataset_id"],
                    "dataset_checksum": dataset["checksum"],
                    "dataset_parquet_sha256": getattr(dataset_row, "parquet_sha256", None),
                    "seed": seed,
                    "hyperparameters": resolved_hp,
                    "trained_at": datetime.utcnow().isoformat() + "Z",
                    "job_id": job_id,
                },
                "saved_at": datetime.utcnow().isoformat() + "Z",
            }
            train_scores = score_with_payload(payload, train_matrix)
            cutpoints = {
                name: float(np.quantile(train_scores, q)) for name, q in BAND_QUANTILES.items()
            }
            payload["band_cutpoints"] = cutpoints

            # ---- honest evaluation ----------------------------------------
            await stage("evaluating", 65)
            evaluation: Dict[str, Any] = {
                "score_type": "anomaly_score",
                "is_probability": False,
                "calibration_status": "not_applicable",
                "note": (f"unsupervised {spec.model_purpose} model — no labels "
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
                alt = IsolationForest(n_estimators=int(resolved_hp["n_estimators"]),
                                      random_state=seed + 1,
                                      contamination=resolved_hp["contamination"],
                                      max_samples=resolved_hp["max_samples"]).fit(train_matrix)
                alt_scores = -alt.score_samples(train_matrix)
                corr = float(np.corrcoef(raw_train, alt_scores)[0, 1])
                evaluation["seed_stability_correlation"] = round(corr, 4)
            evaluation["rule_severity_crosstab"] = await _rule_severity_crosstab(
                db, band_by_entity)
            evaluation["feature_set_limitations"] = feature_set_limitations(feature_set_version)

            # ---- temporal shift, separated into its components -------------
            # score drift | feature availability drift | population cold-start
            from backend.ml.readiness import feature_availability_by_split
            availability = feature_availability_by_split(rows, feature_names)
            evaluation["feature_availability_by_split"] = availability
            train_scores_list = [float(x) for x in train_scores]
            test_scores_list = ([float(x) for x in score_with_payload(payload, matrix_of(test))]
                                if test else [])
            shift: Dict[str, Any] = {
                "train_p50": evaluation["splits"].get("train", {}).get("score_p50"),
                "train_p90": evaluation["splits"].get("train", {}).get("score_p90"),
                "test_p50": evaluation["splits"].get("test", {}).get("score_p50"),
                "test_p90": evaluation["splits"].get("test", {}).get("score_p90"),
                "train_mean": float(np.mean(train_scores_list)) if train_scores_list else None,
                "train_std": float(np.std(train_scores_list)) if train_scores_list else None,
                "test_mean": float(np.mean(test_scores_list)) if test_scores_list else None,
                "test_std": float(np.std(test_scores_list)) if test_scores_list else None,
                "test_share_highly_unusual": (
                    round(sum(1 for x in test_scores_list if x >= cutpoints["highly_unusual"]) / len(test_scores_list), 4)
                    if test_scores_list else None),
            }
            try:
                from backend.ml.drift_service import psi
                shift["score_psi_train_to_test"] = psi(train_scores_list, test_scores_list) \
                    if len(train_scores_list) >= 2 and len(test_scores_list) >= 2 else None
            except Exception:
                shift["score_psi_train_to_test"] = None
            try:
                from scipy.stats import ks_2samp
                if len(train_scores_list) >= 2 and len(test_scores_list) >= 2:
                    ks = ks_2samp(train_scores_list, test_scores_list)
                    shift["score_ks_train_to_test"] = {"statistic": float(ks.statistic), "p_value": float(ks.pvalue)}
                else:
                    shift["score_ks_train_to_test"] = None
            except Exception:
                shift["score_ks_train_to_test"] = None
            shift["availability_gains_train_to_test"] = availability.get("largest_train_to_test_availability_gains", [])
            shift["note"] = ("score drift between the training period and the untouched test period is "
                             "shown next to the feature-availability shift: when history-dependent "
                             "features become available only in the newer period, part of the "
                             "score shift is population maturity (cold start), not behaviour")
            evaluation["temporal_shift"] = shift

            # Candidate vs the model currently in shadow — same rows, each
            # model under its own contract; descriptive, never a verdict.
            from backend.ml.evaluation import compare_with_incumbent, load_incumbent
            incumbent_row, incumbent_payload = await load_incumbent(db, model_type)
            evaluation["incumbent_comparison"] = compare_with_incumbent(
                candidate_payload=payload,
                candidate_meta={"model_type": model_type,
                                "model_purpose": spec.model_purpose,
                                "score_type": "anomaly_score",
                                "feature_set_version": feature_set_version,
                                "artifact_size_bytes": None},
                incumbent_row=incumbent_row, incumbent_payload=incumbent_payload,
                rows_by_split={"val": val, "test": test})

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

            # ---- the two gate families, kept apart ---------------------------
            from backend.ml.readiness import (
                PREPROCESSOR_VERSION, engineering_gate, feature_schema_hash, scientific_gate)
            code_version = _code_version()
            engineering = engineering_gate(
                quality_gates=gates, dataset_quality_passed=True,
                split_meta=dataset_row.split_config or {}, artifact_hash=artifact_hash,
                code_version=code_version, feature_names=feature_names, medians=medians,
                seed_stability=evaluation.get("seed_stability_correlation"),
                dataset_checksum=dataset["checksum"],
                parquet_sha256=getattr(dataset_row, "parquet_sha256", None))
            dq = dataset_row.quality_report or {}
            scientific = scientific_gate(
                population=dq.get("population") or {},
                availability=evaluation.get("feature_availability_by_split") or {},
                score_shift=evaluation.get("temporal_shift"),
                evidence_coverage=None,        # a fresh candidate has no reviewed outcomes yet
                mapping_validated=False,
                split_meta=dataset_row.split_config or {})
            evaluation["engineering_gate"] = engineering
            evaluation["scientific_gate"] = scientific
            gates_passed = gates_passed and engineering["status"] == "PASS"

            await stage("registering", 95)
            from db_models import MLModel
            model_row = MLModel(
                id=uuid_mod.uuid4(),
                model_type=model_type, version=version,
                stage="training",
                algorithm=algorithm,
                model_purpose=spec.model_purpose,
                score_type=spec.score_type,
                is_probability=False,
                calibration_status="not_applicable",
                artifact_name=artifact_name,
                artifact_path=artifact_path,
                artifact_hash=artifact_hash,
                artifact_size_bytes=os.path.getsize(artifact_path),
                dependency_versions=payload["dependency_versions"],
                feature_set_version=feature_set_version,
                feature_names=feature_names,
                dataset_id=uuid_mod.UUID(dataset["dataset_id"]),
                training_job_id=job_id,
                seed=seed,
                hyperparameters=resolved_hp,
                training_config={
                    "algorithm": algorithm, "seed": seed, "hyperparameters": resolved_hp,
                    "feature_schema_hash": feature_schema_hash(feature_names),
                    "preprocessor_version": PREPROCESSOR_VERSION,
                    "rows": {"train": len(train), "val": len(val), "test": len(test)},
                    "train_entities": len({r["entity_id"] for r in train}),
                    "engineering_gate": engineering["status"],
                    "scientific_gate": scientific["status"],
                    "dataset_id": dataset["dataset_id"], "dataset_checksum": dataset["checksum"],
                    "dataset_parquet_sha256": getattr(dataset_row, "parquet_sha256", None),
                    "dataset_reused": dataset.get("reused", False),
                    "feature_set_version": feature_set_version,
                    "feature_coverage_floor": FEATURE_COVERAGE_FLOOR,
                    "minimum_train_rows": MIN_TRAIN_ROWS,
                    "band_quantiles": BAND_QUANTILES,
                    "dependency_versions": payload["dependency_versions"],
                },
                code_version=code_version,
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
                quantiles=dict(BAND_QUANTILES),
                sample_count=len(train), source="training",
                expected_metrics={"train_quantiles": dict(BAND_QUANTILES)})
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
                "dataset_reused": dataset.get("reused", False),
                "training_config": model_row.training_config,
                "code_version": model_row.code_version,
                "incumbent_comparison_status": evaluation["incumbent_comparison"]["status"],
                "engineering_gate": engineering["status"],
                "scientific_gate": scientific["status"],
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
        await _observe_training_outcome_async(job_id)


async def _audit_training_event(action: str, job_id: str, requested_by: Optional[int],
                                after: Dict[str, Any]) -> None:
    """Own session: the training session may be rolled back or closed."""
    try:
        from db_connection import db_manager
        from backend.ml.audit import ml_audit
        async with db_manager.get_session() as db:
            await ml_audit(db, action=action, actor_user_id=requested_by,
                           actor_username=("user" if requested_by is not None else "cli"),
                           object_type="ml_training_job", object_id=job_id, after=after)
            await db.commit()
    except Exception:
        logger.debug("[ML_OPS] training audit %s failed", action, exc_info=True)


async def _observe_training_outcome_async(job_id: str) -> None:
    """One metric per finished job: completed | failed:<stable code>. The
    error codes are a small fixed vocabulary (refusal/gate codes), never
    free text. The outcome is also audited durably (model id when one was
    registered, the error code when not) - a failed training leaves no
    model row, so without this it would vanish with task-history retention."""
    try:
        from backend.core.task_history import task_history_manager
        from backend.ml import metrics as ml_metrics
        task = await task_history_manager.get_task_by_job_id(job_id)
        if not task:
            return
        result = task.get("result") or {}
        if task.get("status") == "completed":
            ml_metrics.observe_training_job("completed")
            await _audit_training_event("training_finished", job_id, None, {
                "status": "completed", "model_id": result.get("model_id"),
                "version": result.get("version"), "dataset_id": result.get("dataset_id"),
                "artifact_hash": result.get("artifact_hash")})
        else:
            code = str(task.get("error_code") or "UNKNOWN")[:48]
            ml_metrics.observe_training_job(f"failed:{code}")
            await _audit_training_event("training_finished", job_id, None, {
                "status": task.get("status"), "error_code": code,
                "error_message": str(task.get("error_message") or "")[:200]})
    except Exception:
        pass


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
