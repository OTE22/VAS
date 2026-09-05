"""MLflow is an evidence mirror. Operational promotion remains locally governed.

Every external write can be retried using stable job/model tags. The PostgreSQL
tracking row survives worker crashes and MLflow outages; a failed mirror does
not erase a trained local model or claim successful registration.
"""
import hashlib
import json
import math
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from sqlalchemy import select, or_
from config import settings


def client():
    from backend.ml.capabilities import tracking_uri, require_capability
    require_capability("mlflow")
    uri = tracking_uri()
    if uri.startswith("sqlite:"):
        (Path(settings.ML_ARTIFACT_DIR) / "tracking").mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "10")
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
    from mlflow import MlflowClient
    return MlflowClient(tracking_uri=uri, registry_uri=uri)


def ensure_run(c, job_id, manifest):
    name = settings.MLFLOW_EXPERIMENT_NAME
    experiment = c.get_experiment_by_name(name)
    if experiment is None:
        location = None if settings.MLFLOW_TRACKING_URI else (Path(settings.ML_ARTIFACT_DIR).resolve() / "tracking" / "artifacts").as_uri()
        try:
            eid = c.create_experiment(name, artifact_location=location)
        except Exception:
            existing = c.get_experiment_by_name(name)
            if existing is None: raise
            eid = existing.experiment_id
    else:
        eid = experiment.experiment_id
    key = hashlib.sha256(job_id.encode()).hexdigest()
    runs = c.search_runs([eid], filter_string=f"tags.`platform.job_key` = '{key}'", max_results=1)
    if runs:
        return runs[0].info.run_id
    return c.create_run(eid, tags={"platform.job_key": key, "platform.job_id": job_id,
        "mlflow.runName": job_id, "platform.git_commit": manifest.get("git_commit") or "unavailable"}).info.run_id


def numeric_metrics(value, prefix=""):
    out = {}
    for key, item in (value or {}).items():
        name = (prefix + "." + key).strip(".")
        if isinstance(item, dict): out.update(numeric_metrics(item, name))
        elif isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item): out[name] = float(item)
    return out


def export_model(c, run_id, model, payload, manifest, directory=None):
    """Create a portable pyfunc using the SAME scoring module as local inference."""
    import mlflow.pyfunc
    from mlflow.entities import Metric, Param
    import time

    class GovernedModel(mlflow.pyfunc.PythonModel):
        def __init__(self, payload): self.payload = payload
        def predict(self, context, model_input, params=None):
            from portable_ml_scoring import preprocess_feature_vector, score_with_payload
            import numpy as np
            records = model_input.to_dict(orient="records") if hasattr(model_input, "to_dict") else model_input
            matrix = np.asarray([preprocess_feature_vector(self.payload, row)[0] for row in records])
            return score_with_payload(self.payload, matrix)

    c.log_batch(run_id, params=[Param(k, json.dumps(v, sort_keys=True)[:5900]) for k, v in {
        "algorithm": model.algorithm, "seed": model.seed, "dataset_id": str(model.dataset_id),
        **(model.hyperparameters or {})}.items()], metrics=[Metric(k, v, int(time.time() * 1000), 0)
        for k, v in numeric_metrics(model.evaluation_report).items()])
    with tempfile.TemporaryDirectory(prefix="mlflow-export-") as temp:
        root = Path(temp)
        (root / "reproducibility.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        c.log_artifact(run_id, str(root / "reproducibility.json"))
        (root / "evaluation.json").write_text(json.dumps(model.evaluation_report, indent=2), encoding="utf-8")
        c.log_artifact(run_id, str(root / "evaluation.json"))
        scoring = root / "portable_ml_scoring.py"
        shutil.copyfile(Path(__file__).with_name("scoring.py"), scoring)
        packages = manifest.get("dependencies", {})
        pip_requirements = [f"{k}=={v}" for k, v in packages.items() if k in ("numpy", "scikit-learn", "xgboost", "xgboost-cpu", "mlflow")]
        mlflow.pyfunc.save_model(path=str(root / "model"), python_model=GovernedModel(payload),
                                code_paths=[str(scoring)], pip_requirements=pip_requirements)
        c.log_artifacts(run_id, str(root / "model"), artifact_path="model")
        c.log_artifact(run_id, model.artifact_path, artifact_path="platform")
        if directory and Path(directory).is_dir():
            c.log_artifacts(run_id, str(directory), artifact_path="explanations")
    name = "platform-" + model.model_type
    existing = c.get_registered_model(name) if any(m.name == name for m in c.search_registered_models(filter_string=f"name = '{name}'")) else None
    if existing is None:
        c.create_registered_model(name, tags={"platform.authority": "local-governed-registry"})
    versions = c.search_model_versions(f"name = '{name}'")
    version = next((v for v in versions if v.tags.get("platform.model_id") == str(model.id)), None)
    if version is None:
        version = c.create_model_version(name, source=f"runs:/{run_id}/model", run_id=run_id,
            tags={"platform.model_id": str(model.id), "platform.artifact_sha256": model.artifact_hash,
                  "platform.local_version": str(model.version)})
    c.set_model_version_tag(name, version.version, "platform.stage", model.stage)
    c.set_registered_model_alias(name, "version-" + str(model.version), version.version)
    if model.stage in ("shadow", "approved", "production"):
        c.set_registered_model_alias(name, model.stage, version.version)
    else:
        for alias in ("shadow", "approved", "production"):
            current = c.get_registered_model(name).aliases.get(alias)
            if str(current) == str(version.version): c.delete_registered_model_alias(name, alias)
    c.set_terminated(run_id, "FINISHED")
    return name, str(version.version)


def serialize(row):
    return {"job_id": row.job_id, "model_id": str(row.model_id) if row.model_id else None,
            "run_id": row.run_id, "registered_name": row.registered_name, "registered_version": row.registered_version,
            "status": row.status, "last_error": row.last_error, "attempts": row.attempts,
            "manifest": row.manifest, "updated_at": row.updated_at.isoformat() + "Z"}


async def record_start(db, job_id, manifest):
    from db_models import MLTrackingRun
    row = await db.get(MLTrackingRun, job_id)
    if row is None:
        row = MLTrackingRun(job_id=job_id, status="pending", manifest=manifest, attempts=0)
        db.add(row)
    else:
        row.manifest = manifest
    await db.commit()
    return row


async def sync_job(job_id):
    from db_connection import db_manager
    from db_models import MLTrackingRun, MLModel
    from starlette.concurrency import run_in_threadpool
    from backend.ml.registry_service import validate_artifact
    from backend.ml.audit import ml_audit
    async with db_manager.get_session() as db:
        row = (await db.execute(select(MLTrackingRun).where(MLTrackingRun.job_id == job_id).with_for_update())).scalar_one_or_none()
        if row is None: return {"status": "not_recorded"}
        if not settings.MLFLOW_ENABLED:
            row.status = "disabled"; await db.commit(); return serialize(row)
        model = (await db.execute(select(MLModel).where(MLModel.training_job_id == job_id))).scalar_one_or_none()
        row.attempts += 1
        try:
            c = await run_in_threadpool(client)
            row.run_id = await run_in_threadpool(ensure_run, c, job_id, row.manifest)
            if not model:
                await run_in_threadpool(c.log_dict, row.run_id, row.manifest, "requested-run.json")
            if model:
                row.model_id = model.id
                row.manifest = (model.training_config or {}).get("reproducibility", row.manifest)
                payload = await run_in_threadpool(validate_artifact, model.artifact_path, expected_hash=model.artifact_hash,
                    expected_feature_names=model.feature_names, expected_dependencies=model.dependency_versions)
                directory = Path(settings.ML_ARTIFACT_DIR).resolve() / "explanations" / str(model.id)
                row.registered_name, row.registered_version = await run_in_threadpool(export_model, c, row.run_id, model, payload, row.manifest, directory)
                row.status = "synchronized"
            else:
                from backend.core.task_history import task_history_manager
                job = await task_history_manager.get_task_by_job_id(job_id)
                terminal = job and job.get("status") in ("failed", "cancelled")
                if terminal:
                    await run_in_threadpool(c.set_tag, row.run_id, "platform.failure_code", job.get("error_code") or job["status"])
                    await run_in_threadpool(c.set_terminated, row.run_id, "KILLED" if job["status"] == "cancelled" else "FAILED")
                row.status = "synchronized" if terminal else "running"
            row.last_error = None
        except Exception:
            row.status = "failed"
            row.last_error = "MLflow synchronization failed. Check capability status, service credentials and writable tracking storage; retry synchronization. The local model and evidence are retained."
        row.updated_at = datetime.utcnow()
        await ml_audit(db, action="mlflow_sync", actor_username="ml-worker", object_type="ml_training_job", object_id=job_id,
                       after={"status": row.status, "run_id": row.run_id, "registered_version": row.registered_version})
        await db.commit()
        return serialize(row)


async def mark_pending(db, model):
    from db_models import MLTrackingRun
    row = await db.get(MLTrackingRun, model.training_job_id) if model.training_job_id else None
    if row:
        row.status = "pending"
        row.model_id = model.id
        row.updated_at = datetime.utcnow()


async def defer_sync(job_id):
    from db_models import MLTrackingRun
    from db_connection import db_manager
    async with db_manager.get_session() as db:
        row = await db.get(MLTrackingRun, job_id)
        if row:
            row.status = "pending"
            row.updated_at = datetime.utcnow()
            await db.commit()


async def enqueue_pending():
    from db_models import MLTrackingRun, BackgroundTaskHistory
    from db_connection import db_manager
    from backend.ml.job_service import enqueue_ml_job, MLJobConflict
    async with db_manager.get_session() as db:
        row = (await db.execute(select(MLTrackingRun).outerjoin(BackgroundTaskHistory, BackgroundTaskHistory.job_id == MLTrackingRun.job_id).where(MLTrackingRun.status == "pending", or_(MLTrackingRun.model_id.is_not(None), BackgroundTaskHistory.status.in_(("completed", "failed", "cancelled"))))
                               .order_by(MLTrackingRun.updated_at).limit(1))).scalar_one_or_none()
        if row:
            try:
                await enqueue_ml_job(db, kind="tracking", payload={"training_job_id": row.job_id}, description="Synchronize governed model lifecycle to MLflow")
                await db.commit()
            except MLJobConflict:
                await db.rollback()
