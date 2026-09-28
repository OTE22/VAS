"""Service-scoped ML workflow projection and governed selection.

The model registry is the versioned global binding. No second, divergent
model selector or camera-scope promise is introduced. Reads never promote,
compute features, refresh readiness records, or run inference.
"""
from datetime import datetime
import os
from typing import Any, Dict

from sqlalchemy import String, cast, func, select

from backend.ml.model_specs import MODEL_SPECS, get_model_spec
from backend.ml.registry_service import RegistryError, registry_service
from backend.utils.time_utils import iso_utc

SERVICE_INFO = {
    "behavior_anomaly_model": ("Behavioral assessment", "Threat assessment", "/admin/security-intelligence", "decision_service"),
    "coappearance_anomaly_model": ("Pair investigation", "Security Intelligence network analysis", "/admin/security-intelligence", "on_demand_observation"),
    "social_graph_anomaly_model": ("Graph investigation", "Security Intelligence network analysis", "/admin/security-intelligence", "on_demand_observation"),
    "threat_ranking_model": ("Analyst review priority", "ML Ops analyst review", "/admin/ml-ops", "analyst_review"),
    "tabular_regression_model": ("Numeric experiments", "Offline experiments", "/admin/ml-ops", "offline_only"),
}


def model_summary(row):
    if row is None:
        return None
    report = row.evaluation_report or {}
    config = row.training_config or {}
    return {"id": str(row.id), "model_type": row.model_type,
            "version": row.version, "stage": row.stage,
            "artifact_hash": row.artifact_hash,
            "feature_set_version": row.feature_set_version,
            "engineering_gate": (report.get("engineering_gate") or {}).get("status") or config.get("engineering_gate") or "NOT_RECORDED",
            "scientific_gate": (report.get("scientific_gate") or {}).get("status") or config.get("scientific_gate") or "NOT_RECORDED",
            "quality_passed": (row.quality_gates or {}).get("passed") is True,
            "dataset_id": str(row.dataset_id) if row.dataset_id else None,
            "created_at": iso_utc(row.created_at) if row.created_at else None}


def deployment_blockers(row, model_type):
    """Recorded evidence only. Artifact integrity is checked at deployment/load."""
    spec = get_model_spec(model_type)
    if spec.serving_mode == "offline_regression":
        return [{"code": "OFFLINE_ONLY", "message": "Numeric regression is an offline experiment; no live service deployment exists.", "action": "advanced"}]
    if row is None:
        return [{"code": "NO_MODEL", "message": "Prepare data and train a model for this service.", "action": "prepare_train"}]
    out = []
    summary = model_summary(row)
    if row.feature_set_version != spec.feature_set_version:
        out.append({"code": "FEATURE_SCHEMA_MISMATCH", "message": "Train a model using this service's current feature schema.", "action": "prepare_train"})
    if not summary["quality_passed"] or summary["engineering_gate"] != "PASS":
        out.append({"code": "ENGINEERING_REVIEW_REQUIRED", "message": "Engineering checks must pass. Open model readiness to review or recompute them.", "action": "review"})
    return out


def _empty_consumption(note, source):
    return {"available": True, "last_success_at": None, "last_attempt_at": None,
            "model_id": None, "model_version": None, "actual_mode_used": None,
            "fallback_reason": None, "source": source, "note": note}


async def _consumption(db, model_type, selected):
    from db_models import MLAuditLog, MLModel, MLPrediction
    if model_type == "behavior_anomaly_model":
        # Assessment linkage distinguishes actual service calls from test predictions.
        base = select(MLPrediction).where(MLPrediction.model_type == model_type,
                                         MLPrediction.assessment_id.is_not(None))
        last = (await db.execute(base.order_by(MLPrediction.created_at.desc()).limit(1))).scalars().first()
        success = (await db.execute(base.where(MLPrediction.fallback_reason.is_(None),
            MLPrediction.behavioral_anomaly_score.is_not(None),
            MLPrediction.model_id == selected.id if selected else MLPrediction.model_id.is_not(None))
            .order_by(MLPrediction.created_at.desc()).limit(1))).scalars().first()
        out = _empty_consumption("Only predictions linked to a real threat assessment count as service use.", "ml_predictions")
        if last:
            out.update(last_attempt_at=iso_utc(last.created_at), model_id=str(last.model_id) if last.model_id else None,
                       model_version=last.model_version_label, actual_mode_used=last.actual_mode_used,
                       fallback_reason=last.fallback_reason, latency_ms=last.latency_ms,
                       feature_set_version=last.feature_set_version, threshold_version=last.threshold_version)
        if success:
            out["last_success_at"] = iso_utc(success.created_at)
        out["selected_model_used"] = bool(selected and last and last.model_id == selected.id and not last.fallback_reason)
        return out
    if model_type == "tabular_regression_model":
        out = _empty_consumption("Offline experiment; there is no live consuming service.", "offline")
        out["available"] = False
        return out
    # Join by persisted immutable model id; never infer use from a registry row,
    # a successful container, or an old audit with the literal latest_validated.
    query = select(MLAuditLog, MLModel.version).join(MLModel,
        MLAuditLog.object_id == cast(MLModel.id, String)).where(
        MLAuditLog.object_type == "ml_model", MLModel.model_type == model_type,
        MLAuditLog.action.in_(("ml_service_consumed", "relational_shadow_scored")))
    row = (await db.execute(query.order_by(MLAuditLog.created_at.desc()).limit(1))).first()
    out = _empty_consumption("ML Ops tests are separate from requests by the consuming service; observational scores do not change live decisions.", "ml_audit_log")
    if row:
        event, version = row
        meta = event.after or {}
        out.update(last_attempt_at=iso_utc(event.created_at), model_id=event.object_id,
                   model_version=f"{model_type}-v{version}", actual_mode_used="observation",
                   latency_ms=meta.get("latency_ms"), feature_set_version=meta.get("feature_set_version"),
                   threshold_version=meta.get("threshold_version"), consumer=meta.get("consumer", "legacy_unspecified"))
        expected_consumer = "ml_ops_analyst_review" if model_type == "threat_ranking_model" else "security_intelligence"
        out["selected_model_used"] = bool(selected and event.object_id == str(selected.id) and meta.get("consumer") == expected_consumer)
        if meta.get("consumer") == "ml_ops":
            out["last_test_at"] = iso_utc(event.created_at)
        if selected:
            successful = (await db.execute(query.where(
                MLAuditLog.object_id == str(selected.id),
                MLAuditLog.after["consumer"].astext == expected_consumer)
                .order_by(MLAuditLog.created_at.desc()).limit(1))).first()
            if successful:
                out["last_success_at"] = iso_utc(successful[0].created_at)
    return out


async def service_status(db) -> Dict[str, Any]:
    from db_models import MLDataset, MLFeatureSnapshot, MLModel
    from backend.ml.decision_service import decision_service
    from backend.ml.labeling_service import labeling_service
    availability = await decision_service.mode_availability(db)
    labels = await labeling_service.label_stats(db)
    snapshot_counts = {(a, b): int(c) for a, b, c in (await db.execute(
        select(MLFeatureSnapshot.entity_type, MLFeatureSnapshot.feature_set_version, func.count())
        .group_by(MLFeatureSnapshot.entity_type, MLFeatureSnapshot.feature_set_version))).all()}
    dataset_counts = {(a, b): int(c) for a, b, c in (await db.execute(
        select(MLDataset.definition_name, MLDataset.feature_set_version, func.count())
        .where(MLDataset.status == "built").group_by(MLDataset.definition_name, MLDataset.feature_set_version))).all()}
    model_counts = {a: int(b) for a, b in (await db.execute(
        select(MLModel.model_type, func.count()).group_by(MLModel.model_type))).all()}
    items = []
    for model_type, spec in MODEL_SPECS.items():
        title, destination, url, mode = SERVICE_INFO[model_type]
        stage = "approved" if spec.serving_mode == "offline_ranking" else "shadow"
        selected = None if spec.serving_mode == "offline_regression" else await registry_service.get_stage_model(db, model_type, stage)
        candidate = await registry_service.get_stage_model(db, model_type, "validated")
        blockers = deployment_blockers(candidate or selected, model_type)
        count = snapshot_counts.get((spec.entity_type, spec.feature_set_version), 0)
        if model_type == "threat_ranking_model" and not labels["supervised_gate_open"]:
            blockers.append({"code": "REVIEWED_LABELS_REQUIRED", "message": "Review sufficient positive and negative outcomes before supervised training.", "action": "labels"})
        selected_blockers = deployment_blockers(selected, model_type) if selected else []
        if selected and not os.path.isfile(selected.artifact_path):
            selected_blockers.append({"code": "ARTIFACT_MISSING", "message": "The selected model file is unavailable. Restore its artifact or select a reviewed replacement.", "action": "review"})
        if selected and spec.serving_mode in ("shadow", "on_demand_shadow"):
            from backend.ml.threshold_service import threshold_service
            if await threshold_service.get_active(db, model_id=selected.id) is None:
                selected_blockers.append({"code": "THRESHOLD_UNRESOLVED", "message": "The selected model has no active compatible threshold set.", "action": "review"})
        consumption = await _consumption(db, model_type, selected)
        state = "offline_only" if spec.serving_mode == "offline_regression" else "not_configured"
        if selected:
            state = "ready_for_requests"
            if model_type == "behavior_anomaly_model":
                state = {"rules": "selected_rules_only", "shadow": "testing_alongside_rules", "ml": "ml_input_enabled"}.get(availability["current_mode"], "mode_gated")
            if selected_blockers or (consumption.get("fallback_reason") and consumption.get("model_id") == str(selected.id)):
                state = "falling_back"
        elif candidate:
            state = "review_candidate"
        next_action = ("advanced" if spec.serving_mode == "offline_regression" else
                       "enable_shadow" if selected and model_type == "behavior_anomaly_model" and availability["current_mode"] == "rules" else
                       "monitor" if selected else "review" if candidate else "prepare_train")
        items.append({"service_id": model_type, "model_type": model_type, "title": title,
            "destination": {"name": destination, "url": url, "mode": mode},
            "serving_mode": spec.serving_mode, "scope": "all_pipelines", "state": state,
            "counts": {"snapshots": count, "datasets": dataset_counts.get((spec.dataset_definition, spec.feature_set_version), 0), "models": model_counts.get(model_type, 0)},
            "counts_note": "Family/schema-matched snapshot counts do not establish usable sample coverage or readiness.",
            "selected_model": model_summary(selected), "candidate": model_summary(candidate),
            "blockers": selected_blockers if selected else blockers,
            "candidate_blockers": blockers if candidate else [],
            "next_action": "review" if selected_blockers else next_action, "consumption": consumption,
            "decision_mode": availability["current_mode"] if model_type == "behavior_anomaly_model" else None,
            "allowed_modes": availability["modes"] if model_type == "behavior_anomaly_model" else {},
            "rollback": {"available": selected is not None, "target": "rules" if model_type == "behavior_anomaly_model" else "no_model", "label": "Stop using model", "previous_version_restore_available": False}})
    return {"version": 1, "scope": "all_pipelines", "items": items, "generated_at": iso_utc(datetime.utcnow())}


async def deploy_service(db, model_type, *, model_id, artifact_hash, reason, actor, actor_id):
    spec = get_model_spec(model_type)
    row = await registry_service.get_model(db, model_id)
    if row is None or row.model_type != model_type:
        raise RegistryError("MODEL_NOT_FOUND", "Select a model trained for this service.")
    if row.artifact_hash != artifact_hash:
        raise RegistryError("ARTIFACT_CHECKSUM_MISMATCH", "The reviewed model changed. Refresh and review it again.")
    blockers = deployment_blockers(row, model_type)
    if blockers:
        raise RegistryError(blockers[0]["code"], blockers[0]["message"])
    if row.stage != "validated":
        raise RegistryError("INVALID_TRANSITION", "Only a validated candidate can be selected for this service.")
    # The registry validates artifact hash/schema/dependencies with a real smoke
    # prediction and activates matching anomaly thresholds in its transaction.
    target = "approved" if spec.serving_mode == "offline_ranking" else "shadow"
    existing = await registry_service.get_stage_model(db, model_type, target)
    approval = {"approved_by_user_id": actor_id, "approved_by": actor, "reason": reason,
        "dataset_version": str(row.dataset_id) if row.dataset_id else "unknown",
        "evaluation_report_ref": f"ml_models:{row.id}:evaluation_report",
        "artifact_checksum": row.artifact_hash, "feature_set_version": row.feature_set_version,
        "intended_scope": "all_pipelines", "service_id": model_type,
        "previous_model_id": str(existing.id) if existing else None,
        "rollback_target": "Stop using model through the service action. Archived versions require a new governed review; no automatic restore."}
    result = await registry_service.transition(db, str(row.id), to_stage=target, actor=actor,
        actor_user_id=actor_id, reason=reason, shadow_approval=approval if target == "shadow" else None)
    return {"success": True, "service_id": model_type, "model": result,
            "decision_mode_changed": False, "scope": "all_pipelines",
            "note": "Model selected. Behavioral shadow collection must be enabled separately; observational models do not change live decisions."}


async def stop_service(db, model_type, *, model_id, reason, actor, actor_id):
    spec = get_model_spec(model_type)
    if spec.serving_mode == "offline_regression":
        raise RegistryError("OFFLINE_ONLY", "This model family has no service deployment.")
    stage = "approved" if spec.serving_mode == "offline_ranking" else "shadow"
    selected = await registry_service.get_stage_model(db, model_type, stage)
    if selected is None or str(selected.id) != str(model_id):
        raise RegistryError("SERVICE_SELECTION_CHANGED", "The selected model changed. Refresh before stopping it.")
    # Restore the safe decision path first. If archival fails, rules continue
    # serving and the still-selected model remains visible for an explicit retry.
    if model_type == "behavior_anomaly_model":
        from backend.ml.mode_service import change_decision_mode
        await change_decision_mode(db, target_mode="rules", action="service_stopped",
            actor_username=actor, actor_user_id=actor_id, reason=reason)
    result = await registry_service.transition(db, str(selected.id), to_stage="archived",
        actor=actor, actor_user_id=actor_id, reason=reason)
    return {"success": True, "service_id": model_type, "archived_model": result,
            "target": "rules" if model_type == "behavior_anomaly_model" else "no_model",
            "note": "Model stopped. Previous versions are not restored automatically."}
