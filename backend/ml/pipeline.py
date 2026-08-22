"""
Reusable training pipeline — the stages as plain functions, plus a CLI.

    python -m backend.ml.pipeline list-definitions
    python -m backend.ml.pipeline build-dataset --definition behavior_anomaly_person
            [--definition-version v1] [--name NAME] [--start ISO] [--end ISO]
            [--sampling-policy refuse|newest_first|oldest_first]
    python -m backend.ml.pipeline describe-dataset --dataset-id UUID
    python -m backend.ml.pipeline train --dataset-id UUID [--algorithm isolation_forest]
            [--seed 42] [--hyperparameters '{"n_estimators": 100}']
    python -m backend.ml.pipeline evaluate --model-id UUID
    python -m backend.ml.pipeline lineage --model-id UUID
    python -m backend.ml.pipeline readiness --model-id UUID [--no-persist]
    python -m backend.ml.pipeline collect [--full-rebuild]
    python -m backend.ml.pipeline shadow-evidence [--days 90] [--model-id UUID]
    python -m backend.ml.pipeline backfill-dataset-hashes
    python -m backend.ml.pipeline archive-dataset --dataset-id UUID [--reason TEXT]

Run inside the application container (`docker exec -w /app <api> python -m
backend.ml.pipeline ...`) — the same database, artifact directory and
single-flight training lock the API uses, so a CLI run and an API run can
never train at the same time. The web UI triggers the same functions.

The pipeline is the existing infrastructure composed in order:
    extract (dataset_builder, definition-driven) -> validate (data_validator)
    -> split (temporal_group_split) -> train (trainer) -> evaluate
    (trainer + evaluation) -> package (registry_service.save_artifact)
    -> register candidate (ml_models, stage validated at best)
Nothing here promotes: shadow entry stays an explicit administrator action.
Exit codes: 0 success, 2 refusal/failure (stable code printed as JSON), 3 usage.
"""

import argparse
import asyncio
import json
import sys
import uuid as uuid_mod
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.ml.constants import FEATURE_SET_VERSION, MODEL_TYPE_BEHAVIOR_ANOMALY
from backend.ml.dataset_definitions import (
    SAMPLING_POLICIES, feature_set_limitations, get_definition, list_definitions)


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()
    return db_manager


# ---------------------------------------------------------------------------
# Stages (callable from the CLI, the API and tests)
# ---------------------------------------------------------------------------

async def stage_build_dataset(*, definition_name: str, definition_version: Optional[str] = None,
                              name: Optional[str] = None, start: Optional[datetime] = None,
                              end: Optional[datetime] = None, sampling_policy: Optional[str] = None,
                              split_strategy: Optional[str] = None,
                              created_by: Optional[int] = None,
                              build_job_id: Optional[str] = None) -> Dict[str, Any]:
    from backend.ml.dataset_builder import build_dataset
    definition = get_definition(definition_name, definition_version)
    db_manager = await _ensure_db()
    async with db_manager.get_session() as db:
        return await build_dataset(
            db, name=name or definition.name, kind=definition.kind,
            definition=definition, time_range_start=start, time_range_end=end,
            sampling_policy=sampling_policy, split_strategy=split_strategy,
            created_by=created_by, build_job_id=build_job_id)


async def stage_describe_dataset(dataset_id: str) -> Dict[str, Any]:
    from db_models import MLDataset
    from backend.ml.dataset_builder import read_manifest, serialize_dataset
    db_manager = await _ensure_db()
    async with db_manager.get_session() as db:
        row = (await db.execute(
            select(MLDataset).where(MLDataset.id == uuid_mod.UUID(dataset_id)))).scalar_one_or_none()
        if row is None:
            return {"status": "failed", "code": "DATASET_NOT_FOUND"}
        out = serialize_dataset(row)
        manifest = read_manifest(row)
        out["manifest"] = manifest
        out["feature_set_limitations"] = feature_set_limitations(row.feature_set_version)
        return out


async def stage_collect(full_rebuild: bool = False) -> Dict[str, Any]:
    """Feature collection through the same job path the API uses. A full
    rebuild recomputes every snapshot under the CURRENT feature set — the
    one-time step after a feature-set version bump."""
    from backend.core.task_history import task_history_manager
    from backend.ml.collector import launch_collection_job
    await _ensure_db()
    outcome = await launch_collection_job(full_rebuild=full_rebuild)
    if outcome.get("status") == "busy":
        return {"status": "failed", "code": "JOB_ALREADY_RUNNING", "job_id": outcome.get("job_id")}
    job_id = outcome.get("job_id")
    # the launcher schedules a background task on this loop; wait for it
    task = None
    for _ in range(3600):
        task = await task_history_manager.get_task_by_job_id(job_id)
        if task and task.get("status") in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(1)
    if not task or task.get("status") != "completed":
        return {"status": "failed", "job_id": job_id,
                "code": (task or {}).get("error_code") or "COLLECTION_FAILED",
                "message": (task or {}).get("error_message")}
    result = dict(task.get("result") or {})
    result.update({"status": "completed", "job_id": job_id, "full_rebuild": full_rebuild})
    return result


async def stage_backfill_dataset_hashes() -> Dict[str, Any]:
    """Legacy datasets: record the Parquet file hash only after the reloaded
    rows reproduce the registered checksum; never rewrites lineage."""
    from backend.ml.dataset_builder import backfill_dataset_file_hashes
    db_manager = await _ensure_db()
    async with db_manager.get_session() as db:
        report = await backfill_dataset_file_hashes(db)
    report["status"] = "ok"
    return report


async def stage_archive_dataset(dataset_id: str, *, reason: str = "",
                                actor: Optional[str] = None) -> Dict[str, Any]:
    from backend.ml.dataset_builder import DatasetArchiveRefusal, archive_dataset
    db_manager = await _ensure_db()
    async with db_manager.get_session() as db:
        try:
            return await archive_dataset(db, dataset_id, actor=actor, reason=reason)
        except DatasetArchiveRefusal as refusal:
            return {"status": "failed", "code": refusal.code, "message": refusal.message}


async def stage_shadow_evidence(days: int = 90, model_id: Optional[str] = None) -> Dict[str, Any]:
    from backend.ml.evaluation import shadow_evidence_report
    db_manager = await _ensure_db()
    async with db_manager.get_session() as db:
        return await shadow_evidence_report(db, days=days, model_id=model_id)


async def stage_train(*, dataset_id: Optional[str], algorithm: str = "isolation_forest",
                      seed: Optional[int] = None, hyperparameters: Optional[Dict[str, Any]] = None,
                      sampling_policy: Optional[str] = None,
                      requested_by: Optional[int] = None,
                      job_id: Optional[str] = None) -> Dict[str, Any]:
    """Runs the SAME job the API runs, under the same single-flight locks."""
    from backend.core.distributed_lock import DistributedLock
    from backend.core.task_history import task_history_manager
    from backend.ml import trainer

    await _ensure_db()
    job_id = job_id or f"mltrain-{uuid_mod.uuid4().hex[:8]}"
    running = trainer.try_acquire_training(job_id)
    if running is not None:
        return {"status": "failed", "code": "TRAINING_ALREADY_RUNNING", "job_id": running}
    dlock = DistributedLock("ml-training-job", ttl_seconds=1800)
    if not await dlock.acquire(holder_label=job_id):
        trainer.release_training(job_id)
        return {"status": "failed", "code": "TRAINING_ALREADY_RUNNING",
                "job_id": dlock.holder_hint or "unknown"}
    try:
        await task_history_manager.create_job(
            job_id=job_id, task_type="ml_training",
            task_name="ML Anomaly Model Training (pipeline)",
            description=f"{MODEL_TYPE_BEHAVIOR_ANOMALY} / {algorithm}"
                        + (f" / dataset {dataset_id}" if dataset_id else ""),
            created_by_user_id=requested_by)
        await trainer.run_training_job(
            job_id, model_type=MODEL_TYPE_BEHAVIOR_ANOMALY, algorithm=algorithm,
            requested_by=requested_by, dataset_id=dataset_id, seed=seed,
            hyperparameters=hyperparameters, sampling_policy=sampling_policy)
    finally:
        await dlock.release()
    task = await task_history_manager.get_task_by_job_id(job_id)
    if not task or task.get("status") != "completed":
        return {"status": "failed", "job_id": job_id,
                "code": (task or {}).get("error_code") or "TRAINING_FAILED",
                "message": (task or {}).get("error_message")}
    result = dict(task.get("result") or {})
    result.update({"status": "completed", "job_id": job_id})
    return result


async def stage_evaluate(model_id: str) -> Dict[str, Any]:
    """Re-run the descriptive candidate-vs-incumbent comparison for an
    existing model against TODAY's shadow model, on the model's own dataset
    val/test rows. Read-only: prints, never persists, never promotes."""
    from db_models import MLModel, MLDataset
    from backend.ml.evaluation import compare_with_incumbent, load_incumbent
    from backend.ml.registry_service import validate_artifact
    from backend.ml.trainer import _load_parquet_rows
    db_manager = await _ensure_db()
    async with db_manager.get_session() as db:
        row = (await db.execute(
            select(MLModel).where(MLModel.id == uuid_mod.UUID(model_id)))).scalar_one_or_none()
        if row is None:
            return {"status": "failed", "code": "MODEL_NOT_FOUND"}
        try:
            payload = validate_artifact(
                row.artifact_path, expected_hash=row.artifact_hash,
                expected_feature_names=list(row.feature_names or []),
                expected_dependencies=row.dependency_versions)
        except Exception as e:
            return {"status": "failed", "code": getattr(e, "code", "ARTIFACT_INVALID"),
                    "message": str(e)}
        dataset = None
        if row.dataset_id:
            dataset = (await db.execute(
                select(MLDataset).where(MLDataset.id == row.dataset_id))).scalar_one_or_none()
        if dataset is None or not dataset.storage_path:
            return {"status": "failed", "code": "DATASET_NOT_FOUND",
                    "message": "the model's dataset is gone; nothing to evaluate on"}
        rows = _load_parquet_rows(dataset.storage_path)
        incumbent_row, incumbent_payload = await load_incumbent(db, row.model_type)
        if incumbent_row is not None and incumbent_row.id == row.id:
            return {"status": "NOT_APPLICABLE", "reason": "the model IS the incumbent"}
        report = compare_with_incumbent(
            candidate_payload=payload,
            candidate_meta={"model_type": row.model_type, "model_purpose": row.model_purpose,
                            "score_type": row.score_type,
                            "feature_set_version": row.feature_set_version,
                            "artifact_size_bytes": row.artifact_size_bytes},
            incumbent_row=incumbent_row, incumbent_payload=incumbent_payload,
            rows_by_split={"val": [r for r in rows if r["split"] == "val"],
                           "test": [r for r in rows if r["split"] == "test"]})
        return {"status": "ok", "model_id": str(row.id), "version": row.version,
                "dataset": {"id": str(dataset.id), "name": dataset.name,
                            "version": dataset.version, "checksum": dataset.checksum},
                "incumbent_comparison": report}


async def stage_readiness(model_id: str, persist: bool = True) -> Dict[str, Any]:
    """Engineering + scientific gates for an existing model, computed from its
    registered artifact and dataset (no retraining); persisted on the model
    as computed_post_hoc."""
    from db_models import MLModel
    from backend.ml.readiness import compute_model_readiness
    db_manager = await _ensure_db()
    async with db_manager.get_session() as db:
        row = (await db.execute(
            select(MLModel).where(MLModel.id == uuid_mod.UUID(model_id)))).scalar_one_or_none()
        if row is None:
            return {"status": "failed", "code": "MODEL_NOT_FOUND"}
        out = await compute_model_readiness(db, row, persist=persist)
        out["status"] = "ok"
        return out


async def stage_lineage(model_id: str) -> Dict[str, Any]:
    """Assessment-side lineage: model -> artifact -> training run -> dataset
    -> fingerprint/file hash -> feature schema -> extraction definition."""
    from db_models import MLModel, MLDataset
    from backend.core.task_history import task_history_manager
    from backend.ml.dataset_builder import serialize_dataset, read_manifest
    from backend.ml.registry_service import serialize_model_row
    db_manager = await _ensure_db()
    async with db_manager.get_session() as db:
        row = (await db.execute(
            select(MLModel).where(MLModel.id == uuid_mod.UUID(model_id)))).scalar_one_or_none()
        if row is None:
            return {"status": "failed", "code": "MODEL_NOT_FOUND"}
        model = serialize_model_row(row)
        model["training_config"] = getattr(row, "training_config", None)
        model["code_version"] = getattr(row, "code_version", None)
        run = await task_history_manager.get_task_by_job_id(row.training_job_id) \
            if row.training_job_id else None
        dataset = None
        if row.dataset_id:
            drow = (await db.execute(
                select(MLDataset).where(MLDataset.id == row.dataset_id))).scalar_one_or_none()
            if drow is not None:
                dataset = serialize_dataset(drow)
                dataset["manifest"] = read_manifest(drow)
        return {
            "status": "ok",
            "model": model,
            "training_run": ({"job_id": run.get("job_id"), "status": run.get("status"),
                              "started_at": run.get("started_at"),
                              "completed_at": run.get("completed_at"),
                              "error_code": run.get("error_code")} if run else None),
            "dataset": dataset,
            "feature_set": {"version": row.feature_set_version,
                            "limitations": feature_set_limitations(row.feature_set_version)},
            "extraction_definition": (dataset or {}).get("definition_name"),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _emit(payload: Dict[str, Any]) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    status = payload.get("status")
    return 0 if status in ("ok", "built", "completed", "archived", "NOT_APPLICABLE") else 2


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="backend.ml.pipeline", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list-definitions")

    b = sub.add_parser("build-dataset")
    b.add_argument("--definition", required=True)
    b.add_argument("--definition-version")
    b.add_argument("--name")
    b.add_argument("--start")
    b.add_argument("--end")
    b.add_argument("--sampling-policy", choices=SAMPLING_POLICIES)
    b.add_argument("--split-strategy", choices=["temporal_group", "temporal"], default=None,
                    help="override the definition's split strategy (recorded on the dataset)")

    d = sub.add_parser("describe-dataset")
    d.add_argument("--dataset-id", required=True)

    t = sub.add_parser("train")
    t.add_argument("--dataset-id")
    t.add_argument("--algorithm", default="isolation_forest")
    t.add_argument("--seed", type=int)
    t.add_argument("--hyperparameters", help="JSON object")
    t.add_argument("--sampling-policy", choices=SAMPLING_POLICIES,
                   help="only when no --dataset-id (a fresh dataset is built)")

    co = sub.add_parser("collect")
    co.add_argument("--full-rebuild", action="store_true")

    sub.add_parser("backfill-dataset-hashes")

    se = sub.add_parser("shadow-evidence")
    se.add_argument("--days", type=int, default=90)
    se.add_argument("--model-id")

    a = sub.add_parser("archive-dataset")
    a.add_argument("--dataset-id", required=True)
    a.add_argument("--reason", default="")

    e = sub.add_parser("evaluate")
    e.add_argument("--model-id", required=True)

    rd = sub.add_parser("readiness")
    rd.add_argument("--model-id", required=True)
    rd.add_argument("--no-persist", action="store_true")

    li = sub.add_parser("lineage")
    li.add_argument("--model-id", required=True)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 3

    if args.command == "list-definitions":
        return _emit({"status": "ok", "feature_set_version": FEATURE_SET_VERSION,
                      "definitions": [x.to_manifest() for x in list_definitions()]})
    if args.command == "build-dataset":
        try:
            return _emit(asyncio.run(stage_build_dataset(
                definition_name=args.definition, definition_version=args.definition_version,
                name=args.name, start=_parse_dt(args.start), end=_parse_dt(args.end),
                sampling_policy=args.sampling_policy, split_strategy=args.split_strategy)))
        except (KeyError, ValueError) as exc:
            return _emit({"status": "failed", "code": "INVALID_ARGUMENTS", "message": str(exc)})
    if args.command == "describe-dataset":
        return _emit(asyncio.run(stage_describe_dataset(args.dataset_id)))
    if args.command == "train":
        hp = json.loads(args.hyperparameters) if args.hyperparameters else None
        return _emit(asyncio.run(stage_train(
            dataset_id=args.dataset_id, algorithm=args.algorithm, seed=args.seed,
            hyperparameters=hp, sampling_policy=args.sampling_policy)))
    if args.command == "collect":
        return _emit(asyncio.run(stage_collect(full_rebuild=args.full_rebuild)))
    if args.command == "shadow-evidence":
        return _emit(asyncio.run(stage_shadow_evidence(days=args.days, model_id=args.model_id)))
    if args.command == "backfill-dataset-hashes":
        return _emit(asyncio.run(stage_backfill_dataset_hashes()))
    if args.command == "archive-dataset":
        return _emit(asyncio.run(stage_archive_dataset(args.dataset_id, reason=args.reason, actor="cli")))
    if args.command == "evaluate":
        return _emit(asyncio.run(stage_evaluate(args.model_id)))
    if args.command == "readiness":
        return _emit(asyncio.run(stage_readiness(args.model_id, persist=not args.no_persist)))
    if args.command == "lineage":
        return _emit(asyncio.run(stage_lineage(args.model_id)))
    return 3


if __name__ == "__main__":
    sys.exit(main())
