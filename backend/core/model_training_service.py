"""
Similarity-Model Training Service (MLOps lifecycle)
===================================================
Background training jobs for the merge-suggestion similarity model.

Guarantees:
  * Training data comes from the PERSISTENT similarity_training_data table
    (the old in-memory sample list vanished on every restart).
  * Train/validation split is GROUP-AWARE by identity pair — rows about the
    same pair can never leak across the split. Deterministic seed stored.
  * Candidates are written atomically to their own artifact (tmp file ->
    reload-verify -> inference smoke test -> sha256 -> os.replace). The
    active model file is NEVER touched by training.
  * Evaluation includes operational merge-decision metrics (precision,
    recall, F1, false-merge rate, missed-merge rate, confusion matrix) —
    not just R²/MSE — plus quality gates and a comparison against the
    currently active model.
  * One training job at a time (single-flight lock + idempotent registry).
  * Activation/rollback are transactional against the model registry
    (partial unique index: one active per model_type) and refresh the
    runtime model in place.
"""

import hashlib
import logging
import os
import pickle
import random
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db_models import SimilarityTrainingData, SimilarityModelRegistry

logger = logging.getLogger(__name__)

MODEL_TYPE = "merge_similarity"
FEATURE_SCHEMA_VERSION = "similarity-features-v1"
FEATURE_NAMES = [
    "embedding_similarity", "pipeline_overlap", "quality_score_1",
    "quality_score_2", "appearances_diff", "is_cross_pipeline",
]
TRAINING_SEED = 42
VALIDATION_FRACTION = 0.2
DECISION_THRESHOLD = 0.5
CANDIDATE_DIR = "models/candidates"

# Quality gates — a candidate failing these cannot be activated.
# False identity merges are far more harmful than missed suggestions,
# so the gates are precision/false-merge oriented.
QUALITY_GATES = {
    "minimum_precision": 0.80,
    "maximum_false_merge_rate": 0.20,
    "minimum_validation_samples": 8,
}


class TrainingConflict(Exception):
    def __init__(self, job_id: str):
        self.job_id = job_id
        super().__init__(f"training already running: {job_id}")


class DatasetNotReady(Exception):
    def __init__(self, readiness: Dict):
        self.readiness = readiness
        super().__init__(readiness.get("readiness_reason") or "dataset not ready")


class ActivationError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# ---- single-flight training lock (WORKERS=1 -> in-process authoritative) ----
_training_lock = threading.Lock()
_TRAINING_JOB = {"job_id": None, "started_at": None}
_TRAINING_JOB_MAX_AGE_SECONDS = 1800
_cancel_events: Dict[str, threading.Event] = {}


def try_acquire_training(job_id: str) -> Optional[str]:
    with _training_lock:
        current, started = _TRAINING_JOB["job_id"], _TRAINING_JOB["started_at"]
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
        current, started = _TRAINING_JOB["job_id"], _TRAINING_JOB["started_at"]
        if current and started and (time.time() - started) < _TRAINING_JOB_MAX_AGE_SECONDS:
            return current
        return None


def request_cancel(job_id: str) -> bool:
    with _training_lock:
        event = _cancel_events.get(job_id)
        if event:
            event.set()
            return True
        return False


def _cancelled(job_id: str) -> bool:
    event = _cancel_events.get(job_id)
    return bool(event and event.is_set())


# =====================================================
# Dataset readiness
# =====================================================

async def dataset_readiness(db: AsyncSession, min_samples: Optional[int] = None) -> Dict:
    """Structured readiness checks — sample count alone is NOT enough."""
    required_total = int(min_samples or getattr(settings, "SIMILARITY_MODEL_MIN_SAMPLES", 50))
    required_per_class = max(5, required_total // 10)
    minimum_balance_ratio = 0.2

    total = (await db.execute(
        select(func.count(SimilarityTrainingData.id)))).scalar() or 0
    approved = (await db.execute(
        select(func.count(SimilarityTrainingData.id))
        .where(SimilarityTrainingData.label >= DECISION_THRESHOLD))).scalar() or 0
    rejected = total - approved
    unique_pairs = (await db.execute(
        select(func.count(func.distinct(
            func.concat(SimilarityTrainingData.identity_id_1, ':',
                        SimilarityTrainingData.identity_id_2)))))).scalar() or 0

    minority = min(approved, rejected)
    balance_ratio = round(minority / total, 4) if total else 0.0

    checks = {
        "total_samples": {"passed": total >= required_total,
                          "current": total, "required": required_total},
        "approved_samples": {"passed": approved >= required_per_class,
                             "current": approved, "required": required_per_class},
        "rejected_samples": {"passed": rejected >= required_per_class,
                             "current": rejected, "required": required_per_class},
        "class_balance": {"passed": balance_ratio >= minimum_balance_ratio,
                          "ratio": balance_ratio, "minimum_ratio": minimum_balance_ratio},
        "unique_identity_pairs": {"passed": unique_pairs >= max(2, required_total // 5),
                                  "current": unique_pairs,
                                  "required": max(2, required_total // 5)},
    }
    failed = [name for name, check in checks.items() if not check["passed"]]
    return {
        "ready_to_train": not failed,
        "readiness_reason": ("insufficient_" + failed[0]) if failed else None,
        "readiness_checks": checks,
        "total_samples": total,
        "approved_samples": approved,
        "rejected_samples": rejected,
        "unique_identity_pairs": unique_pairs,
    }


async def load_dataset(db: AsyncSession) -> List[Dict]:
    """Load the PERSISTENT dataset (deterministic order for hashing)."""
    rows = (await db.execute(
        select(SimilarityTrainingData).order_by(SimilarityTrainingData.id)
    )).scalars().all()
    dataset = []
    for row in rows:
        pair = sorted([str(row.identity_id_1 or ''), str(row.identity_id_2 or '')])
        dataset.append({
            "id": row.id,
            "group": f"{pair[0]}|{pair[1]}" if any(pair) else f"row-{row.id}",
            "features": [
                float(row.embedding_similarity), float(row.pipeline_overlap),
                float(row.quality_score_1), float(row.quality_score_2),
                float(row.appearances_diff),
                1.0 if row.is_cross_pipeline else 0.0,
            ],
            "label": float(row.label),
        })
    return dataset


def dataset_fingerprint(dataset: List[Dict]) -> str:
    h = hashlib.sha256()
    for row in dataset:
        h.update(repr((row["id"], row["group"], row["features"], row["label"])).encode())
    return h.hexdigest()


def group_aware_split(dataset: List[Dict], seed: int = TRAINING_SEED,
                      val_fraction: float = VALIDATION_FRACTION) -> Tuple[List[Dict], List[Dict], Dict]:
    """Identity-pair grouped split: all rows of a pair stay on one side.

    Deterministic (seeded shuffle of the group list) so the split is
    reproducible from (dataset, seed) alone.
    """
    groups: Dict[str, List[Dict]] = {}
    for row in dataset:
        groups.setdefault(row["group"], []).append(row)

    group_keys = sorted(groups.keys())
    random.Random(seed).shuffle(group_keys)

    target_val = max(1, int(len(dataset) * val_fraction))
    val_rows: List[Dict] = []
    train_rows: List[Dict] = []
    for key in group_keys:
        bucket = val_rows if len(val_rows) < target_val else train_rows
        bucket.extend(groups[key])
    # Degenerate protection: never let either side be empty
    if not train_rows and val_rows:
        train_rows = val_rows[len(val_rows) // 2:]
        val_rows = val_rows[:len(val_rows) // 2]

    meta = {
        "split_method": "identity_pair_grouped",
        "seed": seed,
        "validation_fraction": val_fraction,
        "group_count": len(group_keys),
        "train_count": len(train_rows),
        "validation_count": len(val_rows),
    }
    return train_rows, val_rows, meta


# =====================================================
# Metrics + quality gates
# =====================================================

def compute_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict:
    """Regression + operational merge-decision metrics."""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    errors = y_true - y_pred
    mse = float(np.mean(errors ** 2))
    variance = float(np.var(y_true))
    r2 = float(1 - mse / variance) if variance > 0 else 0.0

    predicted_merge = y_pred >= DECISION_THRESHOLD
    actual_merge = y_true >= DECISION_THRESHOLD
    tp = int(np.sum(predicted_merge & actual_merge))
    fp = int(np.sum(predicted_merge & ~actual_merge))
    tn = int(np.sum(~predicted_merge & ~actual_merge))
    fn = int(np.sum(~predicted_merge & actual_merge))

    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and (precision + recall) else None)
    # False merge: the model would merge two people who are NOT the same
    false_merge_rate = fp / (fp + tn) if (fp + tn) else None
    missed_merge_rate = fn / (fn + tp) if (fn + tp) else None

    return {
        "r2": round(r2, 6),
        "mse": round(mse, 6),
        "rmse": round(float(np.sqrt(mse)), 6),
        "mae": round(float(np.mean(np.abs(errors))), 6),
        "decision_threshold": DECISION_THRESHOLD,
        "precision": round(precision, 6) if precision is not None else None,
        "recall": round(recall, 6) if recall is not None else None,
        "f1": round(f1, 6) if f1 is not None else None,
        "false_merge_rate": round(false_merge_rate, 6) if false_merge_rate is not None else None,
        "missed_merge_rate": round(missed_merge_rate, 6) if missed_merge_rate is not None else None,
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "sample_count": int(len(y_true)),
    }


def evaluate_quality_gates(validation_metrics: Dict) -> Dict:
    """Gates protect production from unsafe candidates."""
    precision = validation_metrics.get("precision")
    false_merge = validation_metrics.get("false_merge_rate")
    val_count = validation_metrics.get("sample_count", 0)

    gates = {
        "minimum_precision": {
            "required": QUALITY_GATES["minimum_precision"],
            "actual": precision,
            "passed": precision is not None and precision >= QUALITY_GATES["minimum_precision"],
        },
        "maximum_false_merge_rate": {
            "required": QUALITY_GATES["maximum_false_merge_rate"],
            "actual": false_merge,
            "passed": false_merge is not None and false_merge <= QUALITY_GATES["maximum_false_merge_rate"],
        },
        "minimum_validation_samples": {
            "required": QUALITY_GATES["minimum_validation_samples"],
            "actual": val_count,
            "passed": val_count >= QUALITY_GATES["minimum_validation_samples"],
        },
    }
    return {"passed": all(g["passed"] for g in gates.values()), "gates": gates}


# =====================================================
# Artifact handling (atomic, verify-before-promote)
# =====================================================

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _smoke_test(model, scaler) -> float:
    """A candidate that cannot serve one inference is never registered."""
    features = np.array([[0.9, 0.5, 0.8, 0.8, 0.1, 0.0]], dtype=np.float32)
    prediction = float(model.predict(scaler.transform(features))[0])
    if not np.isfinite(prediction):
        raise ValueError("smoke-test prediction is not finite")
    return prediction


def save_candidate_artifact(model, scaler, artifact_path: str, metadata: Dict) -> str:
    """tmp-write -> flush -> reload-verify -> smoke test -> atomic replace.

    The ACTIVE artifact is never written here — candidates get their own
    immutable file; a failed save can never corrupt production.
    """
    os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
    tmp_path = artifact_path + ".tmp"
    payload = {
        "model": model,
        "scaler": scaler,
        "is_trained": True,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": FEATURE_NAMES,
        "metadata": metadata,
        "saved_at": datetime.utcnow().isoformat() + "Z",
    }
    with open(tmp_path, "wb") as f:
        pickle.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())

    # Reload-verify the bytes we just wrote
    with open(tmp_path, "rb") as f:
        loaded = pickle.load(f)
    for key in ("model", "scaler", "feature_schema_version", "feature_names"):
        if key not in loaded:
            raise ValueError(f"artifact verification failed: missing {key}")
    if loaded["feature_names"] != FEATURE_NAMES:
        raise ValueError("artifact verification failed: feature schema mismatch")
    _smoke_test(loaded["model"], loaded["scaler"])

    os.replace(tmp_path, artifact_path)
    return _sha256_file(artifact_path)


def validate_artifact_compatibility(path: str, expected_hash: Optional[str] = None) -> Dict:
    """Pre-activation compatibility check for the runtime contract."""
    if not os.path.exists(path):
        raise ActivationError("ARTIFACT_MISSING", "Model artifact file is missing")
    if expected_hash:
        actual = _sha256_file(path)
        if actual != expected_hash:
            raise ActivationError("ARTIFACT_HASH_MISMATCH",
                                  "Artifact content does not match its registered hash")
    with open(path, "rb") as f:
        payload = pickle.load(f)
    if payload.get("feature_names", FEATURE_NAMES) != FEATURE_NAMES:
        raise ActivationError("FEATURE_SCHEMA_MISMATCH",
                              "Artifact feature schema is incompatible with the runtime")
    _smoke_test(payload["model"], payload["scaler"])
    return payload


# =====================================================
# Training job
# =====================================================

async def _next_version(db: AsyncSession) -> int:
    current = (await db.execute(
        select(func.max(SimilarityModelRegistry.version))
        .where(SimilarityModelRegistry.model_type == MODEL_TYPE))).scalar()
    return (current or 0) + 1


async def get_active_model(db: AsyncSession) -> Optional[SimilarityModelRegistry]:
    return (await db.execute(
        select(SimilarityModelRegistry).where(
            SimilarityModelRegistry.model_type == MODEL_TYPE,
            SimilarityModelRegistry.status == "active")
    )).scalar_one_or_none()


async def get_latest_candidate(db: AsyncSession) -> Optional[SimilarityModelRegistry]:
    return (await db.execute(
        select(SimilarityModelRegistry).where(
            SimilarityModelRegistry.model_type == MODEL_TYPE,
            SimilarityModelRegistry.status == "candidate")
        .order_by(SimilarityModelRegistry.created_at.desc()).limit(1)
    )).scalar_one_or_none()


def _compare(candidate_metrics: Dict, active_metrics: Optional[Dict]) -> Dict:
    if not active_metrics:
        return {"active_available": False, "recommendation": "promote"}
    diff = {}
    for key in ("precision", "recall", "f1", "false_merge_rate", "r2"):
        c, a = candidate_metrics.get(key), active_metrics.get(key)
        if c is not None and a is not None:
            diff[key] = {"candidate": c, "active": a, "difference": round(c - a, 6)}
    worse_precision = ("precision" in diff and diff["precision"]["difference"] < -0.02)
    worse_false_merge = ("false_merge_rate" in diff and diff["false_merge_rate"]["difference"] > 0.02)
    return {
        "active_available": True,
        "differences": diff,
        "recommendation": "review" if (worse_precision or worse_false_merge) else "promote",
    }


async def run_training_job(job_id: str, min_samples: Optional[int], requested_by: Optional[int]):
    """Staged background training. Never touches the active artifact."""
    from backend.core.task_history import task_history_manager
    from db_connection import db_manager

    started = time.monotonic()
    await task_history_manager.mark_running(job_id)

    async def stage(name: str, percent: int):
        logger.info("[MODEL_TRAINING] job_id=%s training_stage=%s progress_percent=%s",
                    job_id, name, percent)
        await task_history_manager.update_progress(job_id, percent, details={"stage": name})

    try:
        async with db_manager.get_session() as db:
            await stage("collecting_dataset", 10)
            readiness = await dataset_readiness(db, min_samples)
            if not readiness["ready_to_train"]:
                await task_history_manager.finish_job(
                    job_id, success=False, error_code="DATASET_NOT_READY",
                    error_message=readiness["readiness_reason"] or "dataset not ready",
                    result={"readiness": readiness})
                return

            dataset = await load_dataset(db)
            if _cancelled(job_id):
                await task_history_manager.finish_job(job_id, success=False, error_code="CANCELLED")
                return

            await stage("validating_dataset", 20)
            dataset_hash = dataset_fingerprint(dataset)

            await stage("splitting_dataset", 35)
            train_rows, val_rows, split_meta = group_aware_split(dataset, TRAINING_SEED)

            await stage("training", 55)
            from sklearn.neural_network import MLPRegressor
            from sklearn.preprocessing import StandardScaler

            X_train = np.array([r["features"] for r in train_rows], dtype=np.float32)
            y_train = np.array([r["label"] for r in train_rows], dtype=np.float32)
            X_val = np.array([r["features"] for r in val_rows], dtype=np.float32)
            y_val = np.array([r["label"] for r in val_rows], dtype=np.float32)

            scaler = StandardScaler()
            X_train_scaled = scaler.fit_transform(X_train)
            X_val_scaled = scaler.transform(X_val)

            model = MLPRegressor(
                hidden_layer_sizes=(64, 32), activation="relu", solver="adam",
                alpha=0.001, learning_rate="adaptive", learning_rate_init=0.001,
                max_iter=500, shuffle=True, random_state=TRAINING_SEED, tol=1e-4,
                early_stopping=len(X_train) >= 20, validation_fraction=0.1,
                n_iter_no_change=10,
            )
            model.fit(X_train_scaled, y_train)
            if _cancelled(job_id):
                await task_history_manager.finish_job(job_id, success=False, error_code="CANCELLED")
                return

            await stage("evaluating", 70)
            train_metrics = compute_metrics(y_train, model.predict(X_train_scaled))
            val_metrics = compute_metrics(y_val, model.predict(X_val_scaled))
            gates = evaluate_quality_gates(val_metrics)

            active_row = await get_active_model(db)
            active_val_metrics = (active_row.metrics or {}).get("validation") if active_row else None
            comparison = _compare(val_metrics, active_val_metrics)

            await stage("saving_candidate", 85)
            version = await _next_version(db)
            artifact_name = f"similarity-model-v{version}"
            artifact_path = os.path.join(CANDIDATE_DIR, f"{artifact_name}.pkl")
            metadata = {
                "training_job_id": job_id,
                "dataset_version": job_id,
                "dataset_hash": dataset_hash,
                "seed": TRAINING_SEED,
                "split": split_meta,
                "hyperparameters": {"hidden_layer_sizes": [64, 32], "solver": "adam",
                                    "alpha": 0.001, "max_iter": 500},
            }
            artifact_hash = save_candidate_artifact(model, scaler, artifact_path, metadata)

            await stage("validating_artifact", 92)
            row = SimilarityModelRegistry(
                model_type=MODEL_TYPE,
                version=version,
                status="candidate",
                artifact_name=artifact_name,
                artifact_path=artifact_path,
                artifact_hash=artifact_hash,
                training_job_id=job_id,
                dataset_version=job_id,
                dataset_hash=dataset_hash,
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                seed=TRAINING_SEED,
                metrics={"training": train_metrics, "validation": val_metrics,
                         "split": split_meta, "sample_total": len(dataset)},
                quality_gates=gates,
                comparison=comparison,
                created_by=requested_by,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)

            duration_ms = int((time.monotonic() - started) * 1000)
            result = {
                "model_id": row.id,
                "version": version,
                "artifact_name": artifact_name,
                "artifact_hash": artifact_hash,
                "dataset_hash": dataset_hash,
                "sample_total": len(dataset),
                "split": split_meta,
                "training_metrics": train_metrics,
                "validation_metrics": val_metrics,
                "quality_gates": gates,
                "comparison": comparison,
                "awaiting_approval": True,
                "duration_ms": duration_ms,
            }
            await task_history_manager.finish_job(job_id, success=True, result=result)
            logger.info(
                "[MODEL_TRAINING] job_id=%s status=completed model_id=%s model_version=%s "
                "sample_count=%s precision=%s false_merge_rate=%s gates_passed=%s duration=%sms",
                job_id, row.id, version, len(dataset),
                val_metrics.get("precision"), val_metrics.get("false_merge_rate"),
                gates["passed"], duration_ms)

    except Exception as e:
        logger.error("[MODEL_TRAINING] job_id=%s status=failed error=%s", job_id, e, exc_info=True)
        try:
            await task_history_manager.finish_job(
                job_id, success=False, error_code="TRAINING_FAILED",
                error_message=str(e)[:500])
        except Exception:
            pass
    finally:
        release_training(job_id)


# =====================================================
# Activation / rejection / rollback
# =====================================================

async def activate_model(db: AsyncSession, model_id: int, user_id: Optional[int],
                         reason: Optional[str] = None, is_rollback: bool = False) -> Dict:
    """Atomic activation: archive previous active, promote target, refresh runtime.

    The target must be a quality-gated candidate — or, for rollback, a
    previously-active archived version (already proven in production).
    """
    row = (await db.execute(
        select(SimilarityModelRegistry).where(SimilarityModelRegistry.id == model_id)
    )).scalar_one_or_none()
    if not row or row.model_type != MODEL_TYPE:
        raise ActivationError("MODEL_NOT_FOUND", "Model version not found")

    if is_rollback:
        if row.status != "archived":
            raise ActivationError("INVALID_STATUS", "Rollback target must be an archived model")
    else:
        if row.status != "candidate":
            raise ActivationError("INVALID_STATUS", "Only candidates can be activated")
        if not (row.quality_gates or {}).get("passed"):
            raise ActivationError("QUALITY_GATES_FAILED",
                                  "This candidate failed its quality gates and cannot be activated")

    # Verify artifact integrity + runtime compatibility BEFORE any DB change
    validate_artifact_compatibility(row.artifact_path, row.artifact_hash)

    previous = await get_active_model(db)
    now = datetime.utcnow()
    if previous and previous.id != row.id:
        previous.status = "archived"
        previous.archived_at = now
        # The one-active partial unique index is enforced per statement:
        # the archive UPDATE must reach the database before the promote.
        await db.flush()
    row.status = "active"
    row.activated_at = now
    row.activated_by = user_id
    if reason:
        row.notes = ((row.notes or "") + f"\n[{now.isoformat()}Z] "
                     f"{'rollback' if is_rollback else 'activation'}: {reason}").strip()
    await db.commit()

    # Runtime refresh (artifact already load-tested above)
    degraded = False
    try:
        from backend.core.similarity_model import similarity_model
        if not similarity_model.reload_from_path(row.artifact_path):
            degraded = True
        else:
            _sync_legacy_model_path(row.artifact_path)
    except Exception as e:
        logger.critical("[MODEL_ACTIVATION] runtime reload failed model_id=%s error=%s",
                        row.id, e, exc_info=True)
        degraded = True

    return {
        "model_id": row.id,
        "version": row.version,
        "artifact_name": row.artifact_name,
        "previous_model_id": previous.id if previous and previous.id != row.id else None,
        "previous_version": previous.version if previous and previous.id != row.id else None,
        "runtime_degraded": degraded,
    }


def _sync_legacy_model_path(artifact_path: str) -> None:
    """Keep the legacy startup path pointing at the ACTIVE artifact so a
    process restart loads the same model. Atomic copy (tmp + replace)."""
    legacy_path = getattr(settings, "SIMILARITY_MODEL_PATH", "models/similarity_model.pkl")
    try:
        os.makedirs(os.path.dirname(legacy_path) or ".", exist_ok=True)
        tmp = legacy_path + ".tmp"
        with open(artifact_path, "rb") as src, open(tmp, "wb") as dst:
            dst.write(src.read())
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, legacy_path)
    except Exception as e:
        logger.error("[MODEL_ACTIVATION] legacy path sync failed: %s", e)


async def reject_candidate(db: AsyncSession, model_id: int, user_id: Optional[int],
                           reason: Optional[str] = None) -> SimilarityModelRegistry:
    row = (await db.execute(
        select(SimilarityModelRegistry).where(SimilarityModelRegistry.id == model_id)
    )).scalar_one_or_none()
    if not row or row.model_type != MODEL_TYPE:
        raise ActivationError("MODEL_NOT_FOUND", "Model version not found")
    if row.status != "candidate":
        raise ActivationError("INVALID_STATUS", "Only candidates can be rejected")
    row.status = "rejected"
    if reason:
        row.notes = ((row.notes or "") +
                     f"\n[{datetime.utcnow().isoformat()}Z] rejected: {reason}").strip()
    await db.commit()
    await db.refresh(row)
    return row


def serialize_registry_row(row: SimilarityModelRegistry) -> Dict:
    """Client-safe serialization — artifact_path (filesystem) never leaves
    the server; clients get the logical artifact_name + hash."""
    def _z(dt):
        return (dt.isoformat() + "Z") if dt else None
    return {
        "id": row.id,
        "model_type": row.model_type,
        "version": row.version,
        "status": row.status,
        "artifact_name": row.artifact_name,
        "artifact_hash": row.artifact_hash,
        "training_job_id": row.training_job_id,
        "dataset_version": row.dataset_version,
        "dataset_hash": row.dataset_hash,
        "feature_schema_version": row.feature_schema_version,
        "seed": row.seed,
        "metrics": row.metrics,
        "quality_gates": row.quality_gates,
        "comparison": row.comparison,
        "created_at": _z(row.created_at),
        "created_by": row.created_by,
        "activated_at": _z(row.activated_at),
        "activated_by": row.activated_by,
        "archived_at": _z(row.archived_at),
        "failure_code": row.failure_code,
    }
